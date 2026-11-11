/* eth_ping: bare-metal Ethernet on the STM32F107VC (RMII, TI DP83848 PHY at MDIO address 1, as on ST's STM3210C-EVAL
 * board), no IP stack.  72 MHz from the connectivity-line reference clock tree (HSE 25 MHz, PLL2, PLL x9), PLL3 x10 =
 * 50 MHz on MCO (PA8) clocks the PHY and its RMII REF_CLK (RM0008 8.2.10, 29.4.4); console on USART1 (PA9, 115200).
 * RMII pins (RM0008 29.3 / 9.1.11 Table 32; no remap): PA1 REF_CLK, PA7 CRS_DV, PC4 RXD0, PC5 RXD1 (inputs floating),
 * PA2 MDIO, PC1 MDC, PB11 TX_EN, PB12 TXD0, PB13 TXD1 (AF push-pull 50 MHz).
 * Steps: AFIO_MAPR.MII_RMII_SEL = 1 before the MAC clocks are enabled (29.4.4), DMABMR software reset (completes only
 * with the PHY's 50 MHz REF_CLK), PHY identification and soft reset over MDIO (MDC = HCLK/42: MACMIIAR.CR = 000 for
 * 60-72 MHz, 29.8.1), wait for auto-negotiation (BMSR read twice: the link bit latches low), speed/duplex from the
 * DP83848 PHYSTS register (0x10) -> MACCR.FES/DM, four chained TX and RX descriptors (normal 16-byte format: the F107
 * has no enhanced descriptors), MAC address 02:00:00:00:00:10, IP 192.168.0.10.
 * It sends a gratuitous ARP, then answers ARP requests for its address and ICMP echo requests (the reply checksums
 * are inserted by the MAC: TDES0.CIC = 11) and prints one line per event:
 *   "PHY 2000:5C90", "link 100FD", "garp", "arp 192.168.0.1", "ping 1" ...
 * Run: chipem run --board boards/stm32f107vc_eth_ping.yaml --for 20ms   (the board's host node pings it) */
#include "console.h"

#define ETH_MACCR    REG32(ETH_BASE + 0x0000)
#define ETH_MACFFR   REG32(ETH_BASE + 0x0004)
#define ETH_MACMIIAR REG32(ETH_BASE + 0x0010)
#define ETH_MACMIIDR REG32(ETH_BASE + 0x0014)
#define ETH_MACA0HR  REG32(ETH_BASE + 0x0040)
#define ETH_MACA0LR  REG32(ETH_BASE + 0x0044)
#define ETH_DMABMR   REG32(ETH_BASE + 0x1000)
#define ETH_DMATPDR  REG32(ETH_BASE + 0x1004)
#define ETH_DMARPDR  REG32(ETH_BASE + 0x1008)
#define ETH_DMARDLAR REG32(ETH_BASE + 0x100C)
#define ETH_DMATDLAR REG32(ETH_BASE + 0x1010)
#define ETH_DMASR    REG32(ETH_BASE + 0x1014)
#define ETH_DMAOMR   REG32(ETH_BASE + 0x1018)

#define MACCR_RE   (1u << 2)
#define MACCR_TE   (1u << 3)
#define MACCR_IPCO (1u << 10)
#define MACCR_DM   (1u << 11)
#define MACCR_FES  (1u << 14)
#define DMAOMR_SR  (1u << 1)
#define DMAOMR_ST  (1u << 13)
#define DMAOMR_TSF (1u << 21)
#define DMAOMR_RSF (1u << 25)
#define DMASR_TBUS (1u << 2)
#define DMASR_RBUS (1u << 7)

#define TDES0_OWN  (1u << 31)
#define TDES0_IC   (1u << 30)
#define TDES0_LS   (1u << 29)
#define TDES0_FS   (1u << 28)
#define TDES0_CIC3 (3u << 22)
#define TDES0_TCH  (1u << 20)
#define RDES0_OWN  (1u << 31)
#define RDES0_ES   (1u << 15)
#define RDES1_RCH  (1u << 14)

#define PHY_ADDR 1u
#define NDESC 4u
#define BUFSZ 1524u

struct desc { volatile uint32_t d0, d1, d2, d3; };
static struct desc txd[NDESC] __attribute__((aligned(4)));
static struct desc rxd[NDESC] __attribute__((aligned(4)));
static uint8_t txbuf[NDESC][BUFSZ] __attribute__((aligned(4)));
static uint8_t rxbuf[NDESC][BUFSZ] __attribute__((aligned(4)));
static unsigned tx_i, rx_i;

static const uint8_t my_mac[6] = {0x02, 0x00, 0x00, 0x00, 0x00, 0x10};
static const uint8_t my_ip[4] = {192, 168, 0, 10};
volatile uint32_t pings, arps, frames_rx;

static void putu(uint32_t v) { putdec(v); }

static void puthex16(uint32_t v) { puthex(v, 4); }

static void putip(const uint8_t *ip)
{
    for (int i = 0; i < 4; i++) {
        putu(ip[i]);
        if (i < 3)
            putc_('.');
    }
}

static uint32_t mdio_read(uint32_t reg)
{
    ETH_MACMIIAR = (PHY_ADDR << 11) | (reg << 6) | (0u << 2) | 1u;    /* CR=000: HCLK 60-72 MHz, /42 */
    while (ETH_MACMIIAR & 1u) { }
    return ETH_MACMIIDR & 0xFFFFu;
}

static void mdio_write(uint32_t reg, uint32_t v)
{
    ETH_MACMIIDR = v;
    ETH_MACMIIAR = (PHY_ADDR << 11) | (reg << 6) | (0u << 2) | 2u | 1u;
    while (ETH_MACMIIAR & 1u) { }
}

static void copy(uint8_t *d, const uint8_t *s, unsigned n)
{
    while (n--)
        *d++ = *s++;
}

static int same(const uint8_t *a, const uint8_t *b, unsigned n)
{
    while (n--)
        if (*a++ != *b++)
            return 0;
    return 1;
}

/* send `len` bytes already placed in the current TX buffer */
static void tx_send(unsigned len, uint32_t cic)
{
    struct desc *d = &txd[tx_i];
    d->d1 = len;
    d->d0 = TDES0_OWN | TDES0_FS | TDES0_LS | TDES0_TCH | cic;
    tx_i = (tx_i + 1u) % NDESC;
    if (ETH_DMASR & DMASR_TBUS) {
        ETH_DMASR = DMASR_TBUS;
    }
    ETH_DMATPDR = 0;                                  /* poll demand: resume a suspended transmit process */
}

static uint8_t *tx_buffer(void)
{
    while (txd[tx_i].d0 & TDES0_OWN) { }              /* wait until the DMA released it */
    return txbuf[tx_i];
}

static void arp(uint16_t op, const uint8_t *tha, const uint8_t *tpa, const uint8_t *dst)
{
    uint8_t *f = tx_buffer();
    copy(f, dst, 6);
    copy(f + 6, my_mac, 6);
    f[12] = 0x08; f[13] = 0x06;
    f[14] = 0; f[15] = 1; f[16] = 0x08; f[17] = 0; f[18] = 6; f[19] = 4;
    f[20] = 0; f[21] = (uint8_t)op;
    copy(f + 22, my_mac, 6);
    copy(f + 28, my_ip, 4);
    copy(f + 32, tha, 6);
    copy(f + 38, tpa, 4);
    tx_send(42, 0);                                   /* the MAC pads to 60 bytes and appends the FCS */
}

static void handle(const uint8_t *f, unsigned len)
{
    static const uint8_t bcast[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
    unsigned type = (unsigned)f[12] << 8 | f[13];
    if (type == 0x0806 && len >= 42 && f[21] == 1 && same(f + 38, my_ip, 4)) {
        arp(2, f + 22, f + 28, f + 6);                /* ARP reply to the requester */
        arps++;
        puts_("arp ");
        putip(f + 28);
        puts_("\r\n");
    } else if (type == 0x0800 && len >= 42 && f[23] == 1 && same(f + 30, my_ip, 4) && f[34] == 8) {
        unsigned ihl = (f[14] & 0xFu) * 4u;
        unsigned tot = (unsigned)f[16] << 8 | f[17];
        if (14u + tot > len || tot > BUFSZ - 14u)
            return;
        uint8_t *r = tx_buffer();
        copy(r, f + 6, 6);
        copy(r + 6, my_mac, 6);
        copy(r + 12, f + 12, 14u + tot - 12u);
        copy(r + 26, f + 30, 4);                      /* swap the IP addresses */
        copy(r + 30, f + 26, 4);
        r[22] = 64;                                   /* TTL */
        r[24] = r[25] = 0;                            /* IP header checksum: inserted by the MAC */
        r[14 + ihl] = 0;                              /* echo reply */
        r[14 + ihl + 2] = r[14 + ihl + 3] = 0;        /* ICMP checksum: inserted by the MAC */
        tx_send(14u + tot, TDES0_CIC3);
        pings++;
        puts_("ping ");
        putu((unsigned)f[14 + ihl + 6] << 8 | f[14 + ihl + 7]);
        puts_("\r\n");
    }
    (void)bcast;
}

int main(void)
{
    clock_72mhz_pll2(1);                              /* + PLL3 50 MHz on MCO */
    console_init(72000000u, 115200u);
    puts_("eth_ping F107\r\n");
    RCC_APB2ENR |= RCC_APB2ENR_AFIOEN | RCC_APB2ENR_IOPAEN | RCC_APB2ENR_IOPBEN | RCC_APB2ENR_IOPCEN;
    GPIO_CONFIG(GPIOA_BASE, 8, GPIO_AF_PP_50MHZ);      /* MCO: 50 MHz to the PHY (XTAL) and REF_CLK */
    GPIO_CONFIG(GPIOA_BASE, 1, GPIO_IN_FLOATING);      /* REF_CLK */
    GPIO_CONFIG(GPIOA_BASE, 2, GPIO_AF_PP_50MHZ);      /* MDIO */
    GPIO_CONFIG(GPIOA_BASE, 7, GPIO_IN_FLOATING);      /* CRS_DV */
    GPIO_CONFIG(GPIOB_BASE, 11, GPIO_AF_PP_50MHZ);     /* TX_EN */
    GPIO_CONFIG(GPIOB_BASE, 12, GPIO_AF_PP_50MHZ);     /* TXD0 */
    GPIO_CONFIG(GPIOB_BASE, 13, GPIO_AF_PP_50MHZ);     /* TXD1 */
    GPIO_CONFIG(GPIOC_BASE, 1, GPIO_AF_PP_50MHZ);      /* MDC */
    GPIO_CONFIG(GPIOC_BASE, 4, GPIO_IN_FLOATING);      /* RXD0 */
    GPIO_CONFIG(GPIOC_BASE, 5, GPIO_IN_FLOATING);      /* RXD1 */
    AFIO_MAPR |= AFIO_MAPR_MII_RMII_SEL;              /* RMII: before the MAC clocks are enabled */
    RCC_AHBENR |= RCC_AHBENR_ETHMACEN | RCC_AHBENR_ETHMACTXEN | RCC_AHBENR_ETHMACRXEN;
    ETH_DMABMR |= 1u;                                 /* software reset: completes with the PHY's REF_CLK */
    while (ETH_DMABMR & 1u) { }

    uint32_t id1 = mdio_read(2), id2 = mdio_read(3);
    puts_("PHY ");
    puthex16(id1);
    putc_(':');
    puthex16(id2);
    puts_("\r\n");
    mdio_write(0, 0x8000u);                           /* PHY soft reset */
    while (mdio_read(0) & 0x8000u) { }
    mdio_write(4, 0x01E1u);                           /* advertise 10/100 half/full */
    mdio_write(0, 0x1200u);                           /* auto-negotiation enable + restart */
    while (1) {
        (void)mdio_read(1);                           /* link status latches low: read twice */
        uint32_t bmsr = mdio_read(1);
        if ((bmsr & 0x24u) == 0x24u)
            break;
    }
    uint32_t sts = mdio_read(0x10);                   /* DP83848 PHYSTS: bit 1 = 10 Mb/s, bit 2 = full duplex */
    uint32_t cr = MACCR_RE | MACCR_TE | MACCR_IPCO;
    if (!(sts & 2u))
        cr |= MACCR_FES;
    if (sts & 4u)
        cr |= MACCR_DM;
    puts_("link ");
    puts_(sts & 2u ? "10" : "100");
    puts_(sts & 4u ? "FD\r\n" : "HD\r\n");

    for (unsigned i = 0; i < NDESC; i++) {
        txd[i].d0 = TDES0_TCH;
        txd[i].d2 = (uint32_t)txbuf[i];
        txd[i].d3 = (uint32_t)&txd[(i + 1u) % NDESC];
        rxd[i].d1 = RDES1_RCH | BUFSZ;
        rxd[i].d2 = (uint32_t)rxbuf[i];
        rxd[i].d3 = (uint32_t)&rxd[(i + 1u) % NDESC];
        rxd[i].d0 = RDES0_OWN;
    }
    ETH_DMATDLAR = (uint32_t)txd;
    ETH_DMARDLAR = (uint32_t)rxd;
    ETH_MACA0HR = (uint32_t)my_mac[5] << 8 | my_mac[4];
    ETH_MACA0LR = (uint32_t)my_mac[3] << 24 | (uint32_t)my_mac[2] << 16 | (uint32_t)my_mac[1] << 8 | my_mac[0];
    ETH_MACFFR = 0;                                   /* perfect DA filter + broadcast */
    ETH_MACCR = cr;
    ETH_DMAOMR = DMAOMR_TSF | DMAOMR_RSF | DMAOMR_ST | DMAOMR_SR;

    static const uint8_t bcast[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
    static const uint8_t zero[6] = {0};
    arp(1, zero, my_ip, bcast);                       /* gratuitous ARP: who-has my_ip tell my_ip */
    puts_("garp\r\n");

    for (;;) {
        struct desc *d = &rxd[rx_i];
        if (d->d0 & RDES0_OWN)
            continue;
        uint32_t s = d->d0;
        if (!(s & RDES0_ES) && (s & (3u << 8)) == (3u << 8)) {      /* FS and LS: whole frame in one buffer */
            frames_rx++;
            handle(rxbuf[rx_i], (s >> 16) & 0x3FFFu);
        }
        d->d0 = RDES0_OWN;
        rx_i = (rx_i + 1u) % NDESC;
        if (ETH_DMASR & DMASR_RBUS) {
            ETH_DMASR = DMASR_RBUS;
            ETH_DMARPDR = 0;
        }
    }
}
