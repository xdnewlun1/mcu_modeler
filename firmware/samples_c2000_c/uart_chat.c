/* uart_chat.c -- TMS320F28232 two-way UART chat in C for the TI compiler (cl2000); same protocol as
 * firmware/samples_c2000/uart_chat.asm (and firmware/samples_stm32/uart_chat.c on the other end):
 *   115200 8N1, ASCII lines ending "\r\n".  At boot send "HELLO C2000"; every 5 ms send "PING <n>" (n = 1, 2, ...);
 *   a received "PING <n>" is answered with "PONG <n>" (digits echoed as received); a received "PONG <n>" toggles
 *   GPIO34 and increments `pongs`; a received "HELLO <x>" sets `peer_seen`, drives GPIO35 high, and the first one
 *   is answered once with our own "HELLO C2000".  Unknown lines are ignored, '\r' is optional.
 * Clocks: 20 MHz OSCCLK, PLLCR = 10, DIVSEL = /2 -> 100 MHz SYSCLKOUT, LSPCLK 25 MHz, SCI-A BRR 26 (115741 baud).
 * RX by the SCI-A RX interrupt (PIE INT9.1) into a ring; tick by CPU timer 0 (PIE INT1.7) every 5 ms.
 * Run: chipem run firmware/samples_c2000_c/build/uart_chat.out --chip tms320f28232 --for 30ms --uart-in 'SCIA:PING 7\r\n'
 *      or on a board in place of uart_chat.c28img (boards/c2000_stm32_chat.yaml). */
#include "f28232.h"

#define LED_BIT   GPB_BIT(34)
#define PEER_BIT  GPB_BIT(35)
#define RING      64u
#define LINE_MAX  31u
#define TICK_CYCLES 500000ul             /* 5 ms at 100 MHz */

volatile unsigned int ping_n;            /* last PING number sent */
volatile unsigned int pongs;             /* PONG lines received */
volatile unsigned int peer_seen;         /* 1 after a HELLO line */
volatile unsigned int pings_answered;    /* PING lines answered */
volatile unsigned int hello_answered;    /* 1 once the first received HELLO was answered */
static volatile unsigned int ticks;
static volatile unsigned int ring[RING];
static volatile unsigned int ring_head, ring_tail;
static char line[LINE_MAX + 1];
static unsigned int line_len;

__interrupt void cpu_timer0_isr(void)
{
    ticks++;
    PieCtrlRegs.PIEACK = PIEACK_GROUP(1);
}

__interrupt void scia_rx_isr(void)
{
    unsigned int c = SciaRegs.SCIRXBUF & 0xFFu;          /* clears RXRDY */
    unsigned int h = ring_head;
    if (((h + 1u) & (RING - 1u)) != ring_tail) {
        ring[h] = c;
        ring_head = (h + 1u) & (RING - 1u);
    }
    PieCtrlRegs.PIEACK = PIEACK_GROUP(9);
}

static void putc_(char c)
{
    while (!(SciaRegs.SCICTL2 & SCICTL2_TXRDY)) { }
    SciaRegs.SCITXBUF = (unsigned int)c & 0xFFu;
}

static void puts_(const char *s)
{
    while (*s)
        putc_(*s++);
}

static void put_dec(unsigned int v)
{
    char buf[6];
    unsigned int n = 0;
    do {
        buf[n++] = (char)('0' + v % 10u);
        v /= 10u;
    } while (v);
    while (n)
        putc_(buf[--n]);
}

static int starts_with(const char *s, const char *prefix)
{
    while (*prefix)
        if (*s++ != *prefix++)
            return 0;
    return 1;
}

static int is_digit(char c) { return c >= '0' && c <= '9'; }

static void handle_line(void)
{
    if (starts_with(line, "PING ") && is_digit(line[5])) {
        const char *p;
        puts_("PONG ");
        for (p = line + 5; is_digit(*p); p++)
            putc_(*p);
        puts_("\r\n");
        pings_answered++;
    } else if (starts_with(line, "PONG ")) {
        GpioDataRegs.GPBTOGGLE = LED_BIT;
        pongs++;
    } else if (starts_with(line, "HELLO ")) {
        peer_seen = 1;
        GpioDataRegs.GPBSET = PEER_BIT;
        if (!hello_answered) {
            hello_answered = 1;
            puts_("HELLO C2000\r\n");
        }
    }
}

static void on_byte(char c)
{
    if (c == '\r')
        return;
    if (c == '\n') {
        line[line_len] = 0;
        line_len = 0;
        handle_line();
        return;
    }
    if (line_len < LINE_MAX)
        line[line_len++] = c;
}

static void init_sysctrl(void)
{
    EALLOW;
    WDCR = WDCR_DISABLE;
    PCLKCR0 |= PCLKCR0_SCIAENCLK;
    /* pins first, so a board net sees the SCI pin owners before the ~6.6 ms PLL relock */
    GpioCtrlRegs.GPAMUX2 |= (1ul << 24) | (1ul << 26);  /* GPIO28 -> SCIRXDA, GPIO29 -> SCITXDA (mux 1) */
    GpioCtrlRegs.GPBDIR |= LED_BIT | PEER_BIT;
    PLLSTS &= ~PLLSTS_DIVSEL_MSK;                       /* DIVSEL /4 while the PLL relocks */
    PLLCR = 10;
    while (!(PLLSTS & PLLSTS_PLLLOCKS)) { }
    PLLSTS |= PLLSTS_DIVSEL_2;                          /* /2: 100 MHz */
    PIE_VECT[VEC_TINT0] = (unsigned long)cpu_timer0_isr;
    PIE_VECT[VEC_SCIRXINTA] = (unsigned long)scia_rx_isr;
    EDIS;
}

static void init_pie(void)
{
    PieCtrlRegs.PIECTRL = 1;                            /* ENPIE */
    PIEIER(1) |= 1u << 6;                               /* INT1.7 TINT0 */
    PIEIER(9) |= 1u << 0;                               /* INT9.1 SCIRXINTA */
    PieCtrlRegs.PIEACK = 0xFFFF;
    IER |= M_INT1 | M_INT9;
}

static void init_scia(void)
{
    SciaRegs.SCICCR = 0x0007;                           /* 8N1 */
    SciaRegs.SCICTL1 = SCICTL1_RXENA | SCICTL1_TXENA;   /* still in software reset */
    SciaRegs.SCICTL2 = SCICTL2_RXBKINTENA;
    SciaRegs.SCIHBAUD = 0;
    SciaRegs.SCILBAUD = 26;
    SciaRegs.SCICTL1 = SCICTL1_RXENA | SCICTL1_TXENA | SCICTL1_SWRESET;
}

static void init_timer0(void)
{
    CpuTimer0Regs.TCR = TCR_TSS;
    CpuTimer0Regs.PRD = TICK_CYCLES - 1ul;
    CpuTimer0Regs.TPR = 0;
    CpuTimer0Regs.TPRH = 0;
    CpuTimer0Regs.TCR = TCR_TIF | TCR_TIE | TCR_TRB;    /* clear TIF, interrupt, reload, run */
}

void main(void)
{
    DINT;
    /* The COFF ABI does not zero uninitialised globals (only .cinit initialisers are applied), so clear them. */
    ping_n = pongs = peer_seen = pings_answered = hello_answered = 0;
    ticks = ring_head = ring_tail = line_len = 0;
    init_sysctrl();
    init_pie();
    init_scia();
    init_timer0();
    EINT;

    puts_("HELLO C2000\r\n");

    for (;;) {
        DINT;
        if (ring_head == ring_tail && ticks == 0) {
            IDLE;                                       /* wakes on IFR & IER even with INTM set */
            EINT;
            continue;
        }
        EINT;
        while (ring_head != ring_tail) {
            char c = (char)ring[ring_tail];
            ring_tail = (ring_tail + 1u) & (RING - 1u);
            on_byte(c);
        }
        if (ticks) {
            ticks = 0;
            ping_n++;
            puts_("PING ");
            put_dec(ping_n);
            puts_("\r\n");
        }
    }
}
