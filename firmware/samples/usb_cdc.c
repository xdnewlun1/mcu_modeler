/* usb_cdc: bare-metal USB CDC-ACM virtual COM port on the K22 USB0 full-speed controller (USBFSOTG, RM ch. 43).
 * Clocks: 8 MHz crystal -> MCG PEE, PLL 8 MHz / 2 x 30 = 120 MHz core, 60 MHz bus, 40 MHz FlexBus, 24 MHz flash;
 * USB 48 MHz = MCGPLLCLK x (USBFRAC+1)/(USBDIV+1) = 120 MHz x 2/5 (SIM_SOPT2 USBSRC + PLLFLLSEL, SIM_CLKDIV2).
 * Register-level KHCI-style device driver: a 512-byte Buffer Descriptor Table, EP0 control (64 bytes, both RX
 * ping-pong BDs always primed), EP1 OUT/IN bulk (64 bytes, DTS data-toggle sync, ping-pong), EP2 IN interrupt
 * (CDC notifications: never sent, the SIE NAKs).  Handles the standard requests (GET_DESCRIPTOR device /
 * configuration / string, SET_ADDRESS (ADDR written after the status stage, as NXP's stack does),
 * SET_CONFIGURATION, GET_STATUS, GET_CONFIGURATION, CLEAR/SET_FEATURE) and the CDC ones (SET/GET_LINE_CODING,
 * SET_CONTROL_LINE_STATE); anything else is STALLed (EPSTALL, cleared in the STALL interrupt).  After a SETUP the
 * SIE holds CTL.TXSUSPENDTOKENBUSY until the reply BDs are ready.  When the host raises DTR it prints
 * "K22 CDC ready\r\n"; every packet received on EP1 OUT is echoed back on EP1 IN, prefixed by "echo: " when a
 * line starts.
 * Run: chipem run --board boards/k22_usb_cdc.yaml --for 300ms   (the host enumerates, opens the port, types).
 * Counters for tests: usb_resets, usb_suspends, usb_resumes, usb_configured, cdc_rx, cdc_dtr, usb_stalls. */
#include "k22.h"

#define SIM_CLKDIV2      REG32(SIM_BASE + 0x1048)
#define SIM_SCGC4_USBOTG (1u << 18)
#define SIM_SOPT2_USBSRC (1u << 18)
#define SIM_SOPT2_PLLFLLSEL (1u << 16)

#define MCG_BASE 0x40064000u
#define MCG_C1   REG8(MCG_BASE + 0x0)
#define MCG_C2   REG8(MCG_BASE + 0x1)
#define MCG_C5   REG8(MCG_BASE + 0x4)
#define MCG_C6   REG8(MCG_BASE + 0x5)
#define MCG_S    REG8(MCG_BASE + 0x6)

#define USB0         0x40072000u
#define USB_OTGISTAT REG8(USB0 + 0x010)
#define USB_ISTAT    REG8(USB0 + 0x080)
#define USB_INTEN    REG8(USB0 + 0x084)
#define USB_ERRSTAT  REG8(USB0 + 0x088)
#define USB_ERREN    REG8(USB0 + 0x08C)
#define USB_STAT     REG8(USB0 + 0x090)
#define USB_CTL      REG8(USB0 + 0x094)
#define USB_ADDR     REG8(USB0 + 0x098)
#define USB_BDTPAGE1 REG8(USB0 + 0x09C)
#define USB_BDTPAGE2 REG8(USB0 + 0x0B0)
#define USB_BDTPAGE3 REG8(USB0 + 0x0B4)
#define USB_ENDPT(n) REG8(USB0 + 0x0C0 + 4u * (n))
#define USB_USBCTRL  REG8(USB0 + 0x100)
#define USB_CONTROL  REG8(USB0 + 0x108)
#define USB_USBTRC0  REG8(USB0 + 0x10C)

#define IST_USBRST  0x01u
#define IST_ERROR   0x02u
#define IST_SOFTOK  0x04u
#define IST_TOKDNE  0x08u
#define IST_SLEEP   0x10u
#define IST_RESUME  0x20u
#define IST_STALL   0x80u
#define CTL_USBENSOFEN 0x01u
#define CTL_ODDRST     0x02u
#define CTL_TXSUSPENDTOKENBUSY 0x20u
#define EP_HSHK   0x01u
#define EP_STALL  0x02u
#define EP_TXEN   0x04u
#define EP_RXEN   0x08u
#define EP_CTLDIS 0x10u

#define BD_OWN   0x80u
#define BD_DATA1 0x40u
#define BD_DTS   0x08u
#define BD_BC(n) ((uint32_t)(n) << 16)
#define BD_PID(c) (((c) >> 2) & 0xFu)
#define BD_LEN(c) (((c) >> 16) & 0x3FFu)
#define USB0_IRQn 53

typedef struct {
    volatile uint32_t ctrl;
    volatile uint32_t addr;
} bd_t;

/* 16 endpoints x {RX, TX} x {even, odd} BDs of 8 bytes, on a 512-byte boundary (RM 43.3.1) */
static bd_t bdt[64] __attribute__((aligned(512)));
#define BDX(ep, tx, odd) (((ep) << 2) | ((tx) << 1) | (odd))

volatile uint32_t usb_resets, usb_suspends, usb_resumes, usb_configured, cdc_rx, cdc_dtr, usb_stalls;

static const uint8_t dev_desc[18] = {
    18, 1, 0x00, 0x02, 0x02, 0x00, 0x00, 64,            /* USB 2.0, class CDC (2), EP0 64 bytes */
    0xC9, 0x1F, 0x94, 0x00, 0x00, 0x01, 1, 2, 3, 1       /* VID 1FC9 (NXP) PID 0094, strings 1/2/3 */
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
static uint8_t ep0_rx[2][64];
static uint8_t ep0_txbuf[2][64];
static uint8_t ep0_tx_odd;              /* the TX BD the SIE uses next (it flips after each completed IN) */
static uint8_t ep0_tx_data1;
static const uint8_t *ep0_tx_ptr;
static uint32_t ep0_tx_left;
static uint8_t ep0_tx_zlp;
static uint8_t pending_addr;
static uint8_t ctrl_out_req;            /* class request whose OUT data stage is pending */
static uint8_t desc_buf[64];

static uint8_t ep1_rx[2][64];
static uint8_t rx_wait[2];              /* RX BDs (odd index) received while TX was busy, oldest first */
static uint8_t rx_wait_n;
static uint8_t ep1_txbuf[2][64];
static uint8_t ep1_tx_odd, ep1_tx_data1;
static uint8_t tx_buf[160];
static uint32_t tx_len, tx_pos;
static uint8_t tx_zlp;
static volatile int tx_busy;
static int line_start = 1;
static volatile int want_banner;

static void copy(uint8_t *d, const uint8_t *s, uint32_t n)
{
    while (n--)
        *d++ = *s++;
}

static void clock_120mhz(void)
{
    SIM_CLKDIV1 = (0u << 28) | (1u << 24) | (2u << 20) | (4u << 16);   /* core /1, bus /2, FlexBus /3, flash /5 */
    MCG_C2 = (2u << 4) | (1u << 2);                     /* RANGE0 very high, EREFS0: crystal */
    MCG_C1 = (2u << 6) | (3u << 3);                     /* CLKS = external, FRDIV /256, IREFS = 0: FBE */
    while (!(MCG_S & (1u << 1))) { }                    /* OSCINIT0 */
    while (MCG_S & (1u << 4)) { }                       /* IREFST = 0 */
    while (((MCG_S >> 2) & 3u) != 2u) { }               /* CLKST = external */
    MCG_C5 = 1u;                                        /* PRDIV0: 8 MHz / 2 = 4 MHz */
    MCG_C6 = (1u << 6) | 6u;                            /* PLLS, VDIV0: x30 = 120 MHz: PBE */
    while (!(MCG_S & (1u << 5))) { }                    /* PLLST */
    while (!(MCG_S & (1u << 6))) { }                    /* LOCK0 */
    MCG_C1 = (3u << 3);                                 /* CLKS = PLL: PEE */
    while (((MCG_S >> 2) & 3u) != 3u) { }
}

/* ------------------------------------------------------------------------------------------------ EP0 */
static void ep0_rx_prime(unsigned odd)
{
    bdt[BDX(0, 0, odd)].addr = (uint32_t)ep0_rx[odd];
    bdt[BDX(0, 0, odd)].ctrl = BD_BC(64) | BD_OWN;      /* OWN written last */
}

static void ep0_send_next(void)
{
    uint32_t n = ep0_tx_left > 64 ? 64 : ep0_tx_left;
    bd_t *bd = &bdt[BDX(0, 1, ep0_tx_odd)];
    copy(ep0_txbuf[ep0_tx_odd], ep0_tx_ptr, n);
    if (n < 64)
        ep0_tx_zlp = 0;                                 /* a short packet (or the ZLP) ends the data stage */
    ep0_tx_ptr += n;
    ep0_tx_left -= n;
    bd->addr = (uint32_t)ep0_txbuf[ep0_tx_odd];
    bd->ctrl = BD_BC(n) | BD_OWN | BD_DTS | (ep0_tx_data1 ? BD_DATA1 : 0u);
    ep0_tx_data1 ^= 1u;
}

static void ep0_send(const uint8_t *p, uint32_t n, uint32_t wlength)
{
    if (n > wlength)
        n = wlength;
    ep0_tx_ptr = p;
    ep0_tx_left = n;
    ep0_tx_zlp = (n < wlength) && (n % 64u == 0) && n;
    ep0_send_next();
}

static void ep0_status_in(void)
{
    ep0_send(0, 0, 0);
}

static void ep0_stall(void)
{
    USB_ENDPT(0) |= EP_STALL;                           /* both directions; cleared in the STALL interrupt */
    usb_stalls++;
}

static uint32_t string_desc(unsigned idx, uint8_t *out)
{
    const char *s = idx == 1 ? "CHIP_EM" : idx == 2 ? "K22 bare-metal CDC" : "0001";
    uint32_t n = 2;
    while (*s && n < 62) {
        out[n++] = (uint8_t)*s++;
        out[n++] = 0;
    }
    out[0] = (uint8_t)n;
    out[1] = 3;
    return n;
}

/* ------------------------------------------------------------------------------------------------ EP1 */
static void ep1_rx_prime(unsigned odd)
{
    bdt[BDX(1, 0, odd)].addr = (uint32_t)ep1_rx[odd];
    bdt[BDX(1, 0, odd)].ctrl = BD_BC(64) | BD_OWN | BD_DTS | (odd ? BD_DATA1 : 0u);   /* even DATA0, odd DATA1 */
}

static void ep1_send_next(void)
{
    uint32_t n = tx_len - tx_pos;
    if (n > 64)
        n = 64;
    copy(ep1_txbuf[ep1_tx_odd], tx_buf + tx_pos, n);
    tx_pos += n;
    if (n < 64)
        tx_zlp = 0;
    bd_t *bd = &bdt[BDX(1, 1, ep1_tx_odd)];
    bd->addr = (uint32_t)ep1_txbuf[ep1_tx_odd];
    bd->ctrl = BD_BC(n) | BD_OWN | BD_DTS | (ep1_tx_data1 ? BD_DATA1 : 0u);
    ep1_tx_data1 ^= 1u;
}

static void tx_start(uint32_t n)
{
    tx_busy = 1;
    tx_len = n;
    tx_pos = 0;
    tx_zlp = (n % 64u) == 0;                            /* a full last packet needs a ZLP to end the transfer */
    ep1_send_next();
}

static void echo(unsigned odd)
{
    uint32_t len = BD_LEN(bdt[BDX(1, 0, odd)].ctrl);
    uint32_t n = 0;
    cdc_rx += len;
    if (len) {
        if (line_start) {
            const char *pre = "echo: ";
            while (*pre)
                tx_buf[n++] = (uint8_t)*pre++;
        }
        copy(tx_buf + n, ep1_rx[odd], len);
        n += len;
        line_start = ep1_rx[odd][len - 1] == '\n' || ep1_rx[odd][len - 1] == '\r';
        tx_start(n);
    }
    ep1_rx_prime(odd);
}

static void configure_endpoints(void)
{
    USB_ENDPT(1) = EP_HSHK | EP_TXEN | EP_RXEN | EP_CTLDIS;   /* bulk IN + OUT, no SETUP */
    USB_ENDPT(2) = EP_HSHK | EP_TXEN;                    /* interrupt IN (never primed: NAK) */
    ep1_tx_odd = 0;
    ep1_tx_data1 = 0;
    tx_busy = 0;
    rx_wait_n = 0;
    line_start = 1;
    ep1_rx_prime(0);
    ep1_rx_prime(1);
}

/* ------------------------------------------------------------------------------------------------ requests */
static void handle_setup(void)
{
    uint8_t bm = setup[0], req = setup[1];
    uint16_t value = (uint16_t)(setup[2] | (setup[3] << 8));
    uint16_t index = (uint16_t)(setup[4] | (setup[5] << 8));
    uint16_t len = (uint16_t)(setup[6] | (setup[7] << 8));

    if ((bm & 0x60) == 0x00) {                          /* standard */
        switch (req) {
        case 6:                                         /* GET_DESCRIPTOR */
            switch (value >> 8) {
            case 1: ep0_send(dev_desc, sizeof dev_desc, len); return;
            case 2: ep0_send(cfg_desc, sizeof cfg_desc, len); return;
            case 3:
                if ((value & 0xFF) == 0) { ep0_send(str0, sizeof str0, len); return; }
                if ((value & 0xFF) <= 3) { ep0_send(desc_buf, string_desc(value & 0xFF, desc_buf), len); return; }
                break;
            }
            break;
        case 5:                                         /* SET_ADDRESS: ADDR is written after the status stage */
            pending_addr = (uint8_t)(value & 0x7F);
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
            desc_buf[0] = (uint8_t)usb_configured;
            ep0_send(desc_buf, 1, len);
            return;
        case 0:                                         /* GET_STATUS */
            desc_buf[0] = (bm & 0x1F) == 0 ? 1 : 0;     /* self powered */
            desc_buf[1] = 0;
            if ((bm & 0x1F) == 2 && (index & 0x0F) < 16)
                desc_buf[0] = (USB_ENDPT(index & 0x0F) & EP_STALL) ? 1 : 0;
            ep0_send(desc_buf, 2, len);
            return;
        case 1:                                         /* CLEAR_FEATURE(ENDPOINT_HALT) */
            if ((bm & 0x1F) == 2 && (index & 0x0F) == 1) {
                USB_ENDPT(1) &= ~EP_STALL;
                if (index & 0x80)
                    ep1_tx_data1 = 0;
            }
            ep0_status_in();
            return;
        case 3:                                         /* SET_FEATURE */
            ep0_status_in();
            return;
        }
    } else if ((bm & 0x60) == 0x20) {                   /* class (CDC) */
        switch (req) {
        case 0x20:                                      /* SET_LINE_CODING: OUT data stage (EP0 RX is primed) */
            ctrl_out_req = req;
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
        case 0x23:                                      /* SEND_BREAK */
            ep0_status_in();
            return;
        }
    }
    ep0_stall();
}

static void token_done(uint8_t stat)
{
    unsigned ep = stat >> 4, tx = (stat >> 3) & 1u, odd = (stat >> 2) & 1u;
    uint32_t c = bdt[BDX(ep, tx, odd)].ctrl;

    if (ep == 0 && !tx) {
        if (BD_PID(c) == 0xD) {                         /* SETUP: the SIE holds TXSUSPENDTOKENBUSY */
            copy(setup, ep0_rx[odd], 8);
            ep0_rx_prime(odd);
            bdt[BDX(0, 1, 0)].ctrl = 0;                 /* dequeue whatever the last control transfer left */
            bdt[BDX(0, 1, 1)].ctrl = 0;
            ep0_tx_data1 = 1;                           /* data / status stages start with DATA1 */
            ep0_tx_left = 0;
            ep0_tx_zlp = 0;
            ctrl_out_req = 0;
            pending_addr = 0;
            handle_setup();
            USB_CTL &= (uint8_t)~CTL_TXSUSPENDTOKENBUSY;    /* resume token processing */
        } else {                                        /* OUT: data stage of a control write, or a status */
            if (ctrl_out_req == 0x20 && BD_LEN(c)) {
                copy(line_coding, ep0_rx[odd], BD_LEN(c) < 7 ? BD_LEN(c) : 7);
                ctrl_out_req = 0;
                ep0_status_in();
            }
            ep0_rx_prime(odd);
        }
    } else if (ep == 0) {                               /* EP0 IN done */
        ep0_tx_odd = (uint8_t)(odd ^ 1u);
        if (pending_addr) {
            USB_ADDR = pending_addr;
            pending_addr = 0;
        }
        if (ep0_tx_left || ep0_tx_zlp)
            ep0_send_next();
    } else if (ep == 1 && !tx) {                        /* EP1 OUT data */
        if (tx_busy) {
            rx_wait[rx_wait_n++] = (uint8_t)odd;        /* keep the BD: the SIE NAKs until it is primed again */
        } else {
            echo(odd);
        }
    } else if (ep == 1) {                               /* EP1 IN done */
        ep1_tx_odd = (uint8_t)(odd ^ 1u);
        if (tx_pos < tx_len || tx_zlp) {
            ep1_send_next();
        } else {
            tx_busy = 0;
            if (rx_wait_n) {
                unsigned o = rx_wait[0];
                rx_wait[0] = rx_wait[1];
                rx_wait_n--;
                echo(o);
            }
        }
    }
}

static void usb_bus_reset(void)
{
    usb_resets++;
    usb_configured = 0;
    cdc_dtr = 0;                                        /* a new host session raises DTR again: new banner */
    want_banner = 0;
    tx_busy = 0;
    rx_wait_n = 0;
    USB_CTL =CTL_USBENSOFEN | CTL_ODDRST;               /* all BDs back to EVEN; clears TXSUSPENDTOKENBUSY */
    USB_ADDR = 0;
    for (unsigned i = 0; i < 64; i++)
        bdt[i].ctrl = 0;
    USB_ENDPT(0) = EP_HSHK | EP_TXEN | EP_RXEN;         /* 0x0D: control endpoint */
    for (unsigned n = 1; n < 16; n++)
        USB_ENDPT(n) = 0;
    while (USB_ISTAT & IST_TOKDNE)                       /* drop stale token status */
        USB_ISTAT = IST_TOKDNE;
    ep0_tx_odd = 0;
    ep0_rx_prime(0);
    ep0_rx_prime(1);
    USB_ERRSTAT = 0xFF;
    USB_ISTAT = 0xFF;
    USB_INTEN &= (uint8_t)~IST_RESUME;
    USB_CTL = CTL_USBENSOFEN;
}

void USB0_IRQHandler(void)
{
    uint8_t s = USB_ISTAT & USB_INTEN;
    if (s & IST_USBRST) {
        usb_bus_reset();
        return;
    }
    if (s & IST_ERROR) {
        USB_ERRSTAT = 0xFF;
        USB_ISTAT = IST_ERROR;
    }
    if (s & IST_STALL) {
        USB_ENDPT(0) &= (uint8_t)~EP_STALL;             /* EP0 stall is a protocol stall: ends with this STALL */
        USB_ISTAT = IST_STALL;
    }
    while (USB_ISTAT & IST_TOKDNE) {
        token_done(USB_STAT);
        USB_ISTAT = IST_TOKDNE;                         /* loads the next STAT from the FIFO, if any */
    }
    if (s & IST_SLEEP) {
        usb_suspends++;
        USB_ISTAT = IST_SLEEP | IST_RESUME;
        USB_INTEN |= IST_RESUME;                        /* RM 43.4.9: RESUME only enabled in suspend */
    }
    if (s & IST_RESUME) {
        usb_resumes++;
        USB_ISTAT = IST_RESUME;
        USB_INTEN &= (uint8_t)~IST_RESUME;
    }
}

int main(void)
{
    WDOG_Disable();
    clock_120mhz();
    SIM_SOPT2 |= SIM_SOPT2_USBSRC | SIM_SOPT2_PLLFLLSEL; /* RM 3.9.1: source, divider, then the gate */
    SIM_CLKDIV2 = (4u << 1) | 1u;                       /* 120 MHz x 2 / 5 = 48 MHz */
    SIM_SCGC4 |= SIM_SCGC4_USBOTG;

    USB_USBTRC0 |= 0x80u;                               /* module reset */
    while (USB_USBTRC0 & 0x80u) { }
    USB_BDTPAGE1 = (uint8_t)((uint32_t)bdt >> 8);
    USB_BDTPAGE2 = (uint8_t)((uint32_t)bdt >> 16);
    USB_BDTPAGE3 = (uint8_t)((uint32_t)bdt >> 24);
    USB_ISTAT = 0xFF;
    USB_ERRSTAT = 0xFF;
    USB_OTGISTAT = 0xFF;
    USB_USBCTRL = 0;                                    /* transceiver out of suspend, weak pull-downs off */
    USB_ERREN = 0xFF;
    USB_INTEN = IST_USBRST | IST_ERROR | IST_TOKDNE | IST_SLEEP | IST_STALL;
    USB_ENDPT(0) = EP_HSHK | EP_TXEN | EP_RXEN;
    ep0_rx_prime(0);
    ep0_rx_prime(1);
    USB_CTL = CTL_USBENSOFEN | CTL_ODDRST;
    USB_CTL = CTL_USBENSOFEN;
    NVIC_EnableIRQ(USB0_IRQn);
    __enable_irq();
    USB_CONTROL = 0x10u;                                /* DPPULLUPNONOTG: D+ pull-up on, the host sees us */

    for (;;) {
        __disable_irq();
        if (want_banner && usb_configured && !tx_busy) {
            static const char banner[] = "K22 CDC ready\r\n";
            want_banner = 0;
            copy(tx_buf, (const uint8_t *)banner, sizeof banner - 1);
            line_start = 1;
            tx_start(sizeof banner - 1);
        }
        __enable_irq();
        __WFI();
    }
}
