/* usb_cdc: bare-metal USB CDC-ACM virtual COM port on the STM32F105RC OTG_FS (PA11 DM / PA12 DP, VBUS sensed on PA9;
 * RM0008 ch. 28).  Port of firmware/samples_stm32/usb_cdc.c (the same DWC2 device driver) to the connectivity line:
 * clocks from the reference tree (HSE 25 MHz, PLL2, PLL x9 = 72 MHz; OTGFSCLK = PLL VCO 144 MHz / 3 = 48 MHz,
 * RCC_CFGR.OTGFSPRE = 0, RM0008 8.2), AHB 72 MHz (TRDT 6, Table 206), VBUS sensing B-device (GCCFG.VBUSBSEN: the
 * F105 has no NOVBUSSENS, 28.6.2), no GPIO setup for DM/DP (Table 30).  RX FIFO 128 words, TX FIFO0 16 words, TX FIFO1
 * 64 words (EP1 IN bulk), TX FIFO2 16 words (EP2 IN interrupt) in the 320-word FIFO RAM.  When the host raises DTR it
 * prints "F105 CDC ready\r\n"; every packet received on EP1 OUT is echoed on EP1 IN ("echo: " at line starts).
 * No USART console: PA9 (USART1_TX) is the VBUS sense input here.
 * Run: chipem run --board boards/stm32f105rc_usb_cdc.yaml --for 300ms
 * Counters for tests: usb_resets, usb_suspends, usb_resumes, usb_configured, cdc_rx, cdc_dtr. */
#include "console.h"

#define OTG            0x50000000u
#define GOTGINT        REG32(OTG + 0x004)
#define GAHBCFG        REG32(OTG + 0x008)
#define GUSBCFG        REG32(OTG + 0x00C)
#define GRSTCTL        REG32(OTG + 0x010)
#define GINTSTS        REG32(OTG + 0x014)
#define GINTMSK        REG32(OTG + 0x018)
#define GRXSTSP        REG32(OTG + 0x020)
#define GRXFSIZ        REG32(OTG + 0x024)
#define DIEPTXF0       REG32(OTG + 0x028)
#define GCCFG          REG32(OTG + 0x038)
#define DIEPTXF(n)     REG32(OTG + 0x100 + 4u * (n))
#define DCFG           REG32(OTG + 0x800)
#define DCTL           REG32(OTG + 0x804)
#define DSTS           REG32(OTG + 0x808)
#define DIEPMSK        REG32(OTG + 0x810)
#define DOEPMSK        REG32(OTG + 0x814)
#define DAINT          REG32(OTG + 0x818)
#define DAINTMSK       REG32(OTG + 0x81C)
#define DIEPCTL(n)     REG32(OTG + 0x900 + 0x20u * (n))
#define DIEPINT(n)     REG32(OTG + 0x908 + 0x20u * (n))
#define DIEPTSIZ(n)    REG32(OTG + 0x910 + 0x20u * (n))
#define DTXFSTS(n)     REG32(OTG + 0x918 + 0x20u * (n))
#define DOEPCTL(n)     REG32(OTG + 0xB00 + 0x20u * (n))
#define DOEPINT(n)     REG32(OTG + 0xB08 + 0x20u * (n))
#define DOEPTSIZ(n)    REG32(OTG + 0xB10 + 0x20u * (n))
#define PCGCCTL        REG32(OTG + 0xE00)
#define FIFO(n)        REG32(OTG + 0x1000u * ((n) + 1u))

#define GINT_OTGINT  (1u << 2)
#define GINT_RXFLVL  (1u << 4)
#define GINT_USBSUSP (1u << 11)
#define GINT_USBRST  (1u << 12)
#define GINT_ENUMDNE (1u << 13)
#define GINT_IEPINT  (1u << 18)
#define GINT_OEPINT  (1u << 19)
#define GINT_SRQINT  (1u << 30)
#define GINT_WKUINT  (1u << 31)
#define EP_EPENA     (1u << 31)
#define EP_SNAK      (1u << 27)
#define EP_CNAK      (1u << 26)
#define EP_STALL     (1u << 21)
#define EP_SD0PID    (1u << 28)
#define EP_USBAEP    (1u << 15)

volatile uint32_t usb_resets, usb_suspends, usb_resumes, usb_configured, cdc_rx, cdc_dtr, usb_stalls;

static const uint8_t dev_desc[18] = {
    18, 1, 0x00, 0x02, 0x02, 0x00, 0x00, 64,            /* USB 2.0, class CDC (2), EP0 64 bytes */
    0x83, 0x04, 0x40, 0x57, 0x00, 0x02, 1, 2, 3, 1       /* VID 0483 PID 5740, strings 1/2/3 */
};
static const uint8_t cfg_desc[67] = {
    9, 2, 67, 0, 2, 1, 0, 0xC0, 50,                      /* 2 interfaces, self powered, 100 mA */
    9, 4, 0, 0, 1, 0x02, 0x02, 0x01, 0,                  /* IF0: CDC communication, ACM, AT commands */
    5, 0x24, 0x00, 0x10, 0x01,                           /* header 1.10 */
    5, 0x24, 0x01, 0x00, 0x01,                           /* call management: data IF 1 */
    4, 0x24, 0x02, 0x02,                                 /* ACM: line coding + serial state */
    5, 0x24, 0x06, 0x00, 0x01,                           /* union: master 0, slave 1 */
    7, 5, 0x82, 0x03, 16, 0, 16,                         /* EP2 IN interrupt 16 bytes, 16 ms */
    9, 4, 1, 0, 2, 0x0A, 0x00, 0x00, 0,                  /* IF1: CDC data */
    7, 5, 0x01, 0x02, 64, 0, 0,                          /* EP1 OUT bulk 64 */
    7, 5, 0x81, 0x02, 64, 0, 0,                          /* EP1 IN bulk 64 */
};
static const uint8_t str0[4] = {4, 3, 0x09, 0x04};

static uint8_t line_coding[7] = {0x00, 0xC2, 0x01, 0x00, 0, 0, 8};   /* 115200 8N1 */
static uint8_t setup[8];
static uint8_t ep0_buf[64];
static uint32_t ep0_rx_len;
static const uint8_t *ep0_tx;
static uint32_t ep0_tx_left;
static int ep0_tx_zlp;
static uint8_t pending_req;            /* class request with an OUT data stage */
static uint8_t rx_buf[64];
static volatile uint32_t rx_len;
static volatile int tx_busy;
static uint8_t tx_buf[128];
static volatile int line_start = 1;
static volatile int want_banner;

static void delay_cycles(volatile uint32_t n) { while (n--) { } }

static void fifo_write(unsigned ep, const uint8_t *p, uint32_t n)
{
    for (uint32_t i = 0; i < n; i += 4) {
        uint32_t w = 0;
        for (uint32_t k = 0; k < 4 && i + k < n; k++)
            w |= (uint32_t)p[i + k] << (8 * k);
        FIFO(ep) = w;
    }
}

static void fifo_read(uint8_t *p, uint32_t n)
{
    for (uint32_t i = 0; i < n; i += 4) {
        uint32_t w = FIFO(0);
        for (uint32_t k = 0; k < 4 && i + k < n; k++)
            p[i + k] = (uint8_t)(w >> (8 * k));
    }
}

static void ep0_out_arm(void)
{
    DOEPTSIZ(0) = (3u << 29) | (1u << 19) | 64u;
    DOEPCTL(0) |= EP_EPENA | EP_CNAK;
}

static void ep0_send_packet(void)
{
    uint32_t n = ep0_tx_left > 64 ? 64 : ep0_tx_left;
    DIEPTSIZ(0) = (1u << 19) | n;
    DIEPCTL(0) |= EP_EPENA | EP_CNAK;
    fifo_write(0, ep0_tx, n);
    ep0_tx += n;
    ep0_tx_left -= n;
    if (n == 64 && ep0_tx_left == 0 && ep0_tx_zlp)
        ep0_tx_zlp = 2;                 /* one more zero-length packet ends the data stage */
    else if (n < 64)
        ep0_tx_zlp = 0;
}

static void ep0_send(const uint8_t *p, uint32_t n, uint32_t wlength)
{
    if (n > wlength)
        n = wlength;
    ep0_tx = p;
    ep0_tx_left = n;
    ep0_tx_zlp = (n < wlength) && (n % 64 == 0);
    ep0_send_packet();
    ep0_out_arm();                      /* status stage (host OUT ZLP) */
}

static void ep0_status_in(void)
{
    DIEPTSIZ(0) = (1u << 19) | 0u;
    DIEPCTL(0) |= EP_EPENA | EP_CNAK;
}

static void ep0_stall(void)
{
    DIEPCTL(0) |= EP_STALL;
    DOEPCTL(0) |= EP_STALL;
    usb_stalls++;
}

static void ep1_out_arm(void)
{
    DOEPTSIZ(1) = (1u << 19) | 64u;
    DOEPCTL(1) |= EP_EPENA | EP_CNAK;
}

static uint32_t string_desc(unsigned idx, uint8_t *out)
{
    const char *s = idx == 1 ? "CHIP_EM" : idx == 2 ? "F105 bare-metal CDC" : "0001";
    uint32_t n = 2;
    while (*s && n < 62) {
        out[n++] = (uint8_t)*s++;
        out[n++] = 0;
    }
    out[0] = (uint8_t)n;
    out[1] = 3;
    return n;
}

static void configure_endpoints(void)
{
    DIEPCTL(1) = 64u | (2u << 18) | (1u << 22) | EP_SD0PID | EP_USBAEP | EP_SNAK;   /* bulk, TX FIFO 1 */
    DIEPCTL(2) = 16u | (3u << 18) | (2u << 22) | EP_SD0PID | EP_USBAEP | EP_SNAK;   /* interrupt, TX FIFO 2 */
    DOEPCTL(1) = 64u | (2u << 18) | EP_SD0PID | EP_USBAEP;
    DAINTMSK |= (1u << 1) | (1u << 2) | (1u << 17);
    ep1_out_arm();
}

static void handle_setup(void)
{
    uint8_t bm = setup[0], req = setup[1];
    uint16_t value = (uint16_t)(setup[2] | (setup[3] << 8));
    uint16_t len = (uint16_t)(setup[6] | (setup[7] << 8));
    static uint8_t tmp[64];

    if ((bm & 0x60) == 0x00) {                          /* standard */
        switch (req) {
        case 6:                                         /* GET_DESCRIPTOR */
            switch (value >> 8) {
            case 1: ep0_send(dev_desc, sizeof dev_desc, len); return;
            case 2: ep0_send(cfg_desc, sizeof cfg_desc, len); return;
            case 3:
                if ((value & 0xFF) == 0) { ep0_send(str0, sizeof str0, len); return; }
                if ((value & 0xFF) <= 3) { ep0_send(tmp, string_desc(value & 0xFF, tmp), len); return; }
                break;
            }
            break;
        case 5:                                         /* SET_ADDRESS: DWC2 takes DAD now, applies it after status */
            DCFG = (DCFG & ~(0x7Fu << 4)) | ((value & 0x7Fu) << 4);
            ep0_status_in();
            return;
        case 9:                                         /* SET_CONFIGURATION */
            if (value == 1) {
                configure_endpoints();
                usb_configured = 1;
            } else {
                usb_configured = 0;
            }
            ep0_status_in();
            return;
        case 8:                                         /* GET_CONFIGURATION */
            tmp[0] = (uint8_t)usb_configured;
            ep0_send(tmp, 1, len);
            return;
        case 0:                                         /* GET_STATUS */
            tmp[0] = (bm & 0x1F) == 0 ? 1 : 0;          /* self powered */
            tmp[1] = 0;
            ep0_send(tmp, 2, len);
            return;
        case 1: case 3:                                 /* CLEAR/SET_FEATURE */
            ep0_status_in();
            return;
        }
    } else if ((bm & 0x60) == 0x20) {                   /* class (CDC) */
        switch (req) {
        case 0x20:                                      /* SET_LINE_CODING: OUT data stage */
            pending_req = req;
            ep0_out_arm();
            return;
        case 0x21:
            ep0_send(line_coding, 7, len);
            return;
        case 0x22:                                      /* SET_CONTROL_LINE_STATE */
            if ((value & 1) && !cdc_dtr)
                want_banner = 1;
            cdc_dtr = value & 1;
            ep0_status_in();
            return;
        case 0x23:
            ep0_status_in();
            return;
        }
    }
    ep0_stall();
}

static void usb_reset(void)
{
    usb_resets++;
    usb_configured = 0;
    tx_busy = 0;
    DCTL &= ~1u;                                        /* RWUSIG */
    GRSTCTL = (0x10u << 6) | (1u << 5);                 /* flush all TX FIFOs */
    while (GRSTCTL & (1u << 5)) { }
    for (unsigned n = 0; n < 4; n++) {
        DIEPINT(n) = 0xFB7Fu;
        DOEPINT(n) = 0xFB7Fu;
        DIEPCTL(n) &= ~EP_STALL;
        DOEPCTL(n) = (DOEPCTL(n) & ~EP_STALL) | EP_SNAK;
    }
    DAINTMSK = (1u << 0) | (1u << 16);
    DOEPMSK = (1u << 3) | (1u << 0);                    /* STUP, XFRC */
    DIEPMSK = (1u << 0);                                /* XFRC */
    DCFG &= ~(0x7Fu << 4);
    DOEPTSIZ(0) = (3u << 29) | (1u << 19) | 24u;        /* up to 3 back-to-back SETUPs */
}

static void rx_level(void)
{
    uint32_t st = GRXSTSP;
    uint32_t ep = st & 0xF, n = (st >> 4) & 0x7FF, kind = (st >> 17) & 0xF;
    if (kind == 6) {                                    /* SETUP data */
        fifo_read(setup, 8);
    } else if (kind == 2 && n) {                        /* OUT data */
        if (ep == 0) {
            fifo_read(ep0_buf, n);
            ep0_rx_len = n;
        } else {
            fifo_read(rx_buf, n);
            rx_len = n;
        }
    }
}

static void tx_packet(const uint8_t *p, uint32_t n)
{
    tx_busy = 1;
    DIEPTSIZ(1) = (1u << 19) | n;
    DIEPCTL(1) |= EP_EPENA | EP_CNAK;
    fifo_write(1, p, n);
}

void OTG_FS_IRQHandler(void)
{
    uint32_t s = GINTSTS & GINTMSK;
    if (s & GINT_RXFLVL)
        rx_level();
    if (s & GINT_USBRST) {
        GINTSTS = GINT_USBRST;
        usb_reset();
    }
    if (s & GINT_ENUMDNE) {
        GINTSTS = GINT_ENUMDNE;
        DIEPCTL(0) &= ~3u;                              /* EP0 MPS 64 */
        DCTL |= 1u << 8;                                /* CGINAK */
    }
    if (s & GINT_OEPINT) {
        uint32_t daint = DAINT & DAINTMSK;
        if (daint & (1u << 16)) {
            uint32_t i = DOEPINT(0) & DOEPMSK;
            DOEPINT(0) = i;
            if (i & (1u << 0)) {                        /* EP0 OUT transfer done: data stage or status */
                if (pending_req == 0x20) {
                    for (unsigned k = 0; k < 7 && k < ep0_rx_len; k++)
                        line_coding[k] = ep0_buf[k];
                    pending_req = 0;
                    ep0_status_in();
                }
            }
            if (i & (1u << 3))                          /* SETUP stage done */
                handle_setup();
        }
        if (daint & (1u << 17)) {
            uint32_t i = DOEPINT(1);
            DOEPINT(1) = i;
            if (i & 1u) {
                cdc_rx += rx_len;
                if (!tx_busy && rx_len) {
                    uint32_t n = 0;
                    if (line_start) {
                        const char *pre = "echo: ";
                        while (*pre)
                            tx_buf[n++] = (uint8_t)*pre++;
                    }
                    for (uint32_t k = 0; k < rx_len; k++)
                        tx_buf[n++] = rx_buf[k];
                    line_start = rx_buf[rx_len - 1] == '\n' || rx_buf[rx_len - 1] == '\r';
                    if (n > 64) {
                        tx_packet(tx_buf, 64);          /* (rare) the rest is dropped: keep the sample small */
                    } else {
                        tx_packet(tx_buf, n);
                    }
                }
                rx_len = 0;
                ep1_out_arm();
            }
        }
    }
    if (s & GINT_IEPINT) {
        uint32_t daint = DAINT & DAINTMSK;
        if (daint & 1u) {
            uint32_t i = DIEPINT(0);
            DIEPINT(0) = i & 0x7Fu;
            if ((i & 1u) && (ep0_tx_left || ep0_tx_zlp == 2)) {
                if (ep0_tx_left == 0)
                    ep0_tx_zlp = 0;
                ep0_send_packet();
            }
        }
        if (daint & 2u) {
            uint32_t i = DIEPINT(1);
            DIEPINT(1) = i & 0x7Fu;
            if (i & 1u)
                tx_busy = 0;
        }
        if (daint & 4u)
            DIEPINT(2) = DIEPINT(2) & 0x7Fu;
    }
    if (s & GINT_USBSUSP) {
        GINTSTS = GINT_USBSUSP | (1u << 10);
        usb_suspends++;
    }
    if (s & GINT_WKUINT) {
        GINTSTS = GINT_WKUINT;
        usb_resumes++;
    }
    if (s & GINT_SRQINT)
        GINTSTS = GINT_SRQINT;
    if (s & GINT_OTGINT)
        GOTGINT = GOTGINT;
}

int main(void)
{
    clock_72mhz_pll2(0);                                /* PLL 72 MHz: OTGFSCLK = 144 / 3 = 48 MHz */
    /* PA11/PA12 need no GPIO setup: the OTG PHY takes them while GCCFG.PWRDWN = 1 (RM0008 9.1.11 Table 30); PA9
     * (VBUS) stays an input floating (reset state) */
    RCC_CFGR &= ~RCC_CFGR_OTGFSPRE;                     /* VCO / 3 (PLL at 72 MHz), before the OTG clock is enabled */
    RCC_AHBENR |= RCC_AHBENR_OTGFSEN;

    GUSBCFG |= 1u << 6;                                 /* PHYSEL (embedded FS PHY) */
    while (!(GRSTCTL & (1u << 31))) { }                 /* AHBIDL */
    GRSTCTL |= 1u;                                      /* core soft reset */
    while (GRSTCTL & 1u) { }
    DCTL |= 2u;                                         /* SDIS: stay disconnected until the core is set up */
    GCCFG = (1u << 16) | (1u << 19);                    /* PWRDWN, VBUSBSEN (no NOVBUSSENS on the F105) */
    GUSBCFG = (GUSBCFG & ~((0xFu << 10) | (1u << 29))) | (1u << 30) | (6u << 10);   /* force device, TRDT 6 (Table 206: >= 32 MHz) */
    delay_cycles(72000u * 30u / 4u);                    /* ~30 ms for the forced mode */
    while (GINTSTS & 1u) { }                            /* CMOD = device */
    PCGCCTL = 0;
    DCFG = (DCFG & ~3u) | 3u;                           /* full speed (internal PHY) */
    GRXFSIZ = 128;
    DIEPTXF0 = (16u << 16) | 128u;
    DIEPTXF(1) = (64u << 16) | 144u;
    DIEPTXF(2) = (16u << 16) | 208u;
    GRSTCTL = (0x10u << 6) | (1u << 5);
    while (GRSTCTL & (1u << 5)) { }
    GRSTCTL = 1u << 4;
    while (GRSTCTL & (1u << 4)) { }
    GINTSTS = 0xBFFFFFFFu;
    GINTMSK = GINT_RXFLVL | GINT_USBRST | GINT_ENUMDNE | GINT_IEPINT | GINT_OEPINT | GINT_USBSUSP | GINT_WKUINT
            | GINT_SRQINT | GINT_OTGINT;
    GAHBCFG = 1u;                                       /* GINT */
    NVIC_ENABLE(OTG_FS_IRQn);
    enable_irq();
    DCTL &= ~2u;                                        /* SDIS = 0: D+ pull-up on (when VBUS is present) */

    for (;;) {
        if (want_banner && usb_configured && !tx_busy) {
            static const char banner[] = "F105 CDC ready\r\n";
            want_banner = 0;
            tx_packet((const uint8_t *)banner, sizeof banner - 1);
        }
        __asm__ volatile("wfi");
    }
}
