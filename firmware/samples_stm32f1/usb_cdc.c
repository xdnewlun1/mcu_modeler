/* usb_cdc: bare-metal USB CDC-ACM virtual COM port on the STM32F103T6 USB device peripheral (PA11 USBDM / PA12
 * USBDP; the D+ pull-up is an external 1.5 k resistor on the board, as on the "blue pill").  No ST library.
 * Clocks: HSE 8 MHz x 9 = 72 MHz SYSCLK (clock_72mhz), USBPRE = 0: USBCLK = 72 / 1.5 = 48 MHz.
 * Packet memory (512 bytes, CPU address = 0x40006000 + 2 * local address, 16 bits per 32-bit slot):
 *   0x000 BTABLE (4 endpoints x 8 bytes), 0x040 EP0 TX 64, 0x080 EP0 RX 64, 0x0C0 EP1 TX 64, 0x100 EP1 RX 64,
 *   0x140 EP2 TX 16.
 * EP0 control, EP1 bulk IN/OUT 64 (CDC data), EP2 interrupt IN 16 (CDC notifications, always NAKed).
 * Handles GET_DESCRIPTOR (device, configuration, string), SET_ADDRESS, SET_CONFIGURATION, GET_CONFIGURATION,
 * GET_STATUS, SET/GET_LINE_CODING, SET_CONTROL_LINE_STATE; anything else is STALLed.  When the host raises DTR it
 * sends "F103 CDC ready\r\n"; every packet received on EP1 OUT is echoed back uppercased.
 * Run: chipem run --board boards/stm32f103_usb_cdc.yaml --for 300ms
 * Counters for tests: usb_resets, usb_suspends, usb_wakeups, usb_configured, cdc_rx, cdc_dtr. */
#include "stm32f103.h"

#define USB_EPR(n)   REG32(USB_BASE + 4u * (n))
#define USB_CNTR     REG32(USB_BASE + 0x40)
#define USB_ISTR     REG32(USB_BASE + 0x44)
#define USB_FNR      REG32(USB_BASE + 0x48)
#define USB_DADDR    REG32(USB_BASE + 0x4C)
#define USB_BTABLE   REG32(USB_BASE + 0x50)
#define PMA16(a)     REG32(PMA_BASE + 2u * (a))       /* 16-bit PMA word at local address a (even) */

#define CNTR_FRES    (1u << 0)
#define CNTR_PDWN    (1u << 1)
#define CNTR_FSUSP   (1u << 3)
#define CNTR_SUSPM   (1u << 11)
#define CNTR_WKUPM   (1u << 12)
#define CNTR_RESETM  (1u << 10)
#define CNTR_CTRM    (1u << 15)
#define ISTR_CTR     (1u << 15)
#define ISTR_WKUP    (1u << 12)
#define ISTR_SUSP    (1u << 11)
#define ISTR_RESET   (1u << 10)
#define ISTR_SOF     (1u << 9)
#define ISTR_ESOF    (1u << 8)
#define EP_CTR_RX    (1u << 15)
#define EP_SETUP     (1u << 11)
#define EP_CTR_TX    (1u << 7)
#define EP_T_BULK    (0u << 9)
#define EP_T_CONTROL (1u << 9)
#define EP_T_INTR    (3u << 9)
#define EP_KIND      (1u << 8)
/* bits written as-is (no toggle, CTR kept by writing 1): CTR_RX SETUP EP_TYPE EP_KIND CTR_TX EA */
#define EPREG_MASK   (EP_CTR_RX | EP_SETUP | (3u << 9) | EP_KIND | EP_CTR_TX | 0xFu)
#define ST_DIS 0u
#define ST_STALL 1u
#define ST_NAK 2u
#define ST_VALID 3u

#define EP0_TX 0x040u
#define EP0_RX 0x080u
#define EP1_TX 0x0C0u
#define EP1_RX 0x100u
#define EP2_TX 0x140u
#define RX_64  ((1u << 15) | (1u << 10))              /* BL_SIZE = 1, NUM_BLOCK = 1: 64 bytes */

volatile uint32_t usb_resets, usb_suspends, usb_wakeups, usb_configured, cdc_rx, cdc_dtr;

static const uint8_t dev_desc[18] = {
    18, 1, 0x00, 0x02, 0x02, 0x00, 0x00, 64,            /* USB 2.0, class CDC (2), EP0 64 bytes */
    0x83, 0x04, 0x40, 0x57, 0x00, 0x01, 1, 2, 3, 1       /* VID 0483 PID 5740, bcdDevice 1.00, strings 1/2/3 */
};
static const uint8_t cfg_desc[67] = {
    9, 2, 67, 0, 2, 1, 0, 0x80, 50,                      /* 2 interfaces, bus powered, 100 mA */
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
static const char *const strings[3] = {"CHIP_EM", "F103 bare-metal CDC", "0001"};

static uint8_t line_coding[7] = {0x00, 0xC2, 0x01, 0x00, 0, 0, 8};   /* 115200 8N1 */
static uint8_t ep0_buf[64];
static const uint8_t *ep0_tx;
static uint32_t ep0_tx_left;
static int ep0_tx_zlp;
static uint8_t pending_addr;
static uint8_t pending_req;            /* class request with an OUT data stage */
static volatile int tx_busy, tx_zlp, want_banner;
static uint8_t echo_buf[64];
static uint32_t echo_len;              /* echo waiting for EP1 IN (EP1 OUT stays NAK meanwhile) */

/* ---- endpoint register helpers (toggle / rc_w0 semantics) ---- */
static void ep_stat_tx(unsigned n, uint32_t st)
{
    uint32_t v = USB_EPR(n) & (EPREG_MASK | (3u << 4));
    USB_EPR(n) = ((v ^ (st << 4)) | EP_CTR_RX | EP_CTR_TX) & ~EP_SETUP;
}

static void ep_stat_rx(unsigned n, uint32_t st)
{
    uint32_t v = USB_EPR(n) & (EPREG_MASK | (3u << 12));
    USB_EPR(n) = ((v ^ (st << 12)) | EP_CTR_RX | EP_CTR_TX) & ~EP_SETUP;
}

static void ep_clear_ctr_rx(unsigned n)
{
    USB_EPR(n) = (USB_EPR(n) & EPREG_MASK & ~(EP_CTR_RX | EP_SETUP)) | EP_CTR_TX;
}

static void ep_clear_ctr_tx(unsigned n)
{
    USB_EPR(n) = (USB_EPR(n) & EPREG_MASK & ~(EP_CTR_TX | EP_SETUP)) | EP_CTR_RX;
}

/* EPnR with type/address, both toggles cleared, the given STATs (from whatever the register holds now) */
static void ep_init(unsigned n, uint32_t type_ea, uint32_t rx, uint32_t tx)
{
    uint32_t v = USB_EPR(n);
    uint32_t t = v & ((1u << 14) | (1u << 6));            /* toggling a 1 clears DTOG */
    t |= ((v >> 12) & 3u) << 12 ^ (rx << 12);
    t |= ((v >> 4) & 3u) << 4 ^ (tx << 4);
    USB_EPR(n) = type_ea | t;                             /* CTR bits written 0: cleared */
}

static void pma_write(uint32_t a, const uint8_t *p, uint32_t n)
{
    for (uint32_t i = 0; i < n; i += 2) {
        uint32_t w = p[i];
        if (i + 1 < n)
            w |= (uint32_t)p[i + 1] << 8;
        PMA16(a + i) = w;
    }
}

static void pma_read(uint32_t a, uint8_t *p, uint32_t n)
{
    for (uint32_t i = 0; i < n; i += 2) {
        uint32_t w = PMA16(a + i);
        p[i] = (uint8_t)w;
        if (i + 1 < n)
            p[i + 1] = (uint8_t)(w >> 8);
    }
}

#define BT_ADDR_TX(n)  PMA16(8u * (n) + 0)
#define BT_COUNT_TX(n) PMA16(8u * (n) + 2)
#define BT_ADDR_RX(n)  PMA16(8u * (n) + 4)
#define BT_COUNT_RX(n) PMA16(8u * (n) + 6)

/* ---- control endpoint ---- */
static void ep0_send_packet(void)
{
    uint32_t n = ep0_tx_left > 64 ? 64 : ep0_tx_left;
    pma_write(EP0_TX, ep0_tx, n);
    BT_COUNT_TX(0) = n;
    ep0_tx += n;
    ep0_tx_left -= n;
    ep_stat_tx(0, ST_VALID);
}

static void ep0_send(const uint8_t *p, uint32_t len, uint32_t wlength)
{
    if (len > wlength)
        len = wlength;
    ep0_tx = p;
    ep0_tx_left = len;
    ep0_tx_zlp = len < wlength && (len % 64) == 0 && len != 0;
    ep0_send_packet();
    ep_stat_rx(0, ST_VALID);                            /* the status OUT may come at any time */
}

static void ep0_stall(void)
{
    ep_stat_tx(0, ST_STALL);
    ep_stat_rx(0, ST_STALL);                            /* a new SETUP is accepted anyway */
}

static void ep0_status_in(void)
{
    BT_COUNT_TX(0) = 0;
    ep0_tx_left = 0;
    ep0_tx_zlp = 0;
    ep_stat_tx(0, ST_VALID);
}

static uint32_t string_desc(unsigned idx)
{
    const char *s = strings[idx - 1];
    uint32_t n = 2;
    while (*s && n < sizeof ep0_buf) {
        ep0_buf[n++] = (uint8_t)*s++;
        ep0_buf[n++] = 0;
    }
    ep0_buf[0] = (uint8_t)n;
    ep0_buf[1] = 3;
    return n;
}

static void cdc_send(const uint8_t *p, uint32_t n)
{
    pma_write(EP1_TX, p, n);
    BT_COUNT_TX(1) = n;
    tx_busy = 1;
    tx_zlp = (n == 64);
    ep_stat_tx(1, ST_VALID);
}

static void send_banner(void)
{
    static const uint8_t banner[] = "F103 CDC ready\r\n";
    want_banner = 0;
    cdc_send(banner, sizeof banner - 1);
}

static void configure(void)
{
    BT_ADDR_TX(1) = EP1_TX;
    BT_COUNT_TX(1) = 0;
    BT_ADDR_RX(1) = EP1_RX;
    BT_COUNT_RX(1) = RX_64;
    BT_ADDR_TX(2) = EP2_TX;
    BT_COUNT_TX(2) = 0;
    ep_init(1, EP_T_BULK | 1u, ST_VALID, ST_NAK);
    ep_init(2, EP_T_INTR | 2u, ST_DIS, ST_NAK);
    tx_busy = 0;
    echo_len = 0;
    usb_configured = 1;
}

static void handle_setup(void)
{
    uint8_t s[8];
    pma_read(EP0_RX, s, 8);
    uint32_t bm = s[0], req = s[1];
    uint32_t value = s[2] | (uint32_t)s[3] << 8;
    uint32_t wlength = s[6] | (uint32_t)s[7] << 8;
    pending_req = 0;
    if ((bm & 0x60) == 0x00) {                          /* standard */
        switch (req) {
        case 6: {                                       /* GET_DESCRIPTOR */
            uint32_t type = value >> 8, idx = value & 0xFF;
            if (type == 1)
                ep0_send(dev_desc, sizeof dev_desc, wlength);
            else if (type == 2)
                ep0_send(cfg_desc, sizeof cfg_desc, wlength);
            else if (type == 3 && idx == 0)
                ep0_send(str0, sizeof str0, wlength);
            else if (type == 3 && idx <= 3)
                ep0_send(ep0_buf, string_desc(idx), wlength);
            else
                ep0_stall();
            return;
        }
        case 5:                                         /* SET_ADDRESS: applied after the status stage */
            pending_addr = (uint8_t)(value & 0x7F);
            ep0_status_in();
            return;
        case 9:                                         /* SET_CONFIGURATION */
            if (value == 1)
                configure();
            else
                usb_configured = 0;
            ep0_status_in();
            return;
        case 8:                                         /* GET_CONFIGURATION */
            ep0_buf[0] = (uint8_t)usb_configured;
            ep0_send(ep0_buf, 1, wlength);
            return;
        case 0:                                         /* GET_STATUS */
            ep0_buf[0] = 0;
            ep0_buf[1] = 0;
            ep0_send(ep0_buf, 2, wlength);
            return;
        default:
            break;
        }
    } else if ((bm & 0x60) == 0x20) {                   /* class (CDC) */
        switch (req) {
        case 0x20:                                      /* SET_LINE_CODING: 7 bytes OUT */
            pending_req = 0x20;
            ep_stat_rx(0, ST_VALID);
            return;
        case 0x21:                                      /* GET_LINE_CODING */
            ep0_send(line_coding, 7, wlength);
            return;
        case 0x22:                                      /* SET_CONTROL_LINE_STATE */
            cdc_dtr = value & 1;
            if (cdc_dtr && usb_configured) {
                if (tx_busy)
                    want_banner = 1;
                else
                    send_banner();
            }
            ep0_status_in();
            return;
        default:
            break;
        }
    }
    ep0_stall();
}

static void usb_reset(void)
{
    USB_BTABLE = 0;
    BT_ADDR_TX(0) = EP0_TX;
    BT_COUNT_TX(0) = 0;
    BT_ADDR_RX(0) = EP0_RX;
    BT_COUNT_RX(0) = RX_64;
    ep_init(0, EP_T_CONTROL | 0u, ST_VALID, ST_NAK);
    USB_DADDR = 0x80;                                   /* EF, address 0 */
    pending_addr = 0;
    usb_configured = 0;
    tx_busy = 0;
    usb_resets++;
}

static void ep0_ctr(uint32_t epr)
{
    if (epr & EP_CTR_RX) {
        ep_clear_ctr_rx(0);
        if (epr & EP_SETUP) {
            handle_setup();
        } else {
            uint32_t n = BT_COUNT_RX(0) & 0x3FF;
            if (pending_req == 0x20 && n >= 7) {
                pma_read(EP0_RX, ep0_buf, 7);
                for (int i = 0; i < 7; i++)
                    line_coding[i] = ep0_buf[i];
                pending_req = 0;
                ep0_status_in();
            }
            ep_stat_rx(0, ST_VALID);                    /* status OUT done (or data taken): ready for more */
        }
    }
    if (epr & EP_CTR_TX) {
        ep_clear_ctr_tx(0);
        if (pending_addr) {
            USB_DADDR = 0x80u | pending_addr;
            pending_addr = 0;
        }
        if (ep0_tx_left) {
            ep0_send_packet();
        } else if (ep0_tx_zlp) {
            ep0_tx_zlp = 0;
            BT_COUNT_TX(0) = 0;
            ep_stat_tx(0, ST_VALID);
        }
    }
}

static void ep1_ctr(uint32_t epr)
{
    if (epr & EP_CTR_RX) {
        ep_clear_ctr_rx(1);
        uint32_t n = BT_COUNT_RX(1) & 0x3FF;
        pma_read(EP1_RX, echo_buf, n);
        for (uint32_t i = 0; i < n; i++)
            if (echo_buf[i] >= 'a' && echo_buf[i] <= 'z')
                echo_buf[i] = (uint8_t)(echo_buf[i] - 32);
        cdc_rx += n;
        if (n && !tx_busy) {
            cdc_send(echo_buf, n);
            ep_stat_rx(1, ST_VALID);
        } else if (n) {
            echo_len = n;                               /* EP1 OUT stays NAK until the echo is out */
        } else {
            ep_stat_rx(1, ST_VALID);
        }
    }
    if (epr & EP_CTR_TX) {
        ep_clear_ctr_tx(1);
        tx_busy = 0;
        if (tx_zlp) {
            tx_zlp = 0;
            BT_COUNT_TX(1) = 0;
            tx_busy = 1;
            ep_stat_tx(1, ST_VALID);
        } else if (echo_len) {
            cdc_send(echo_buf, echo_len);
            echo_len = 0;
            ep_stat_rx(1, ST_VALID);
        } else if (want_banner) {
            send_banner();
        }
    }
}

void USB_LP_CAN1_RX0_IRQHandler(void)
{
    uint32_t istr = USB_ISTR;
    if (istr & ISTR_RESET) {
        USB_ISTR = (uint16_t)~ISTR_RESET;
        usb_reset();
        return;
    }
    while ((istr = USB_ISTR) & ISTR_CTR) {
        unsigned ep = istr & 0xFu;
        uint32_t epr = USB_EPR(ep);
        if (ep == 0)
            ep0_ctr(epr);
        else if (ep == 1)
            ep1_ctr(epr);
        else
            USB_EPR(ep) = USB_EPR(ep) & EPREG_MASK & ~(EP_CTR_RX | EP_CTR_TX | EP_SETUP);
    }
    if (istr & ISTR_SUSP) {
        USB_CNTR |= CNTR_FSUSP;
        USB_ISTR = (uint16_t)~ISTR_SUSP;
        usb_suspends++;
    }
    if (istr & ISTR_WKUP) {
        USB_CNTR &= ~CNTR_FSUSP;
        USB_ISTR = (uint16_t)~ISTR_WKUP;
        usb_wakeups++;
    }
    if (istr & (ISTR_SOF | ISTR_ESOF))
        USB_ISTR = (uint16_t)~(ISTR_SOF | ISTR_ESOF);
}

int main(void)
{
    clock_72mhz();                                      /* USBPRE = 0 (reset value): USBCLK = 48 MHz */
    RCC_APB2ENR |= RCC_APB2ENR_IOPAEN;
    RCC_APB1ENR |= RCC_APB1ENR_USBEN;
    USB_CNTR = CNTR_FRES;                               /* power the transceiver up, keep the reset */
    for (volatile int i = 0; i < 100; i++)              /* tSTARTUP (1 us) */
        ;
    USB_CNTR = 0;
    USB_ISTR = 0;
    USB_CNTR = CNTR_CTRM | CNTR_RESETM | CNTR_SUSPM | CNTR_WKUPM;
    NVIC_ENABLE(USB_LP_CAN1_RX0_IRQn);
    for (;;)
        __asm__ volatile("wfi");
}
