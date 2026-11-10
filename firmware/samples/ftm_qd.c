/* ftm_qd: FTM1 quadrature decoder counts a quadrature encoder and prints the count every 10 ms on UART0.
 *
 *  - FTM1 phase A on PTB0 (ALT6 = FTM1_QD_PHA), phase B on PTB1 (ALT6 = FTM1_QD_PHB).
 *  - FTM1: MODE.FTMEN = 1, QDCTRL.QUADEN = 1, QUADMODE = 0 (phase A / phase B encoding: every edge of A and B counts,
 *    K22P100M120SF5V2RM 38.4.25), CNTIN = 0, MOD = 0xFFFF (the count wraps 0xFFFF <-> 0), CLKS = system clock (the
 *    decoder needs a clock source selected; the counter itself is clocked by the phase inputs).
 *  - Every 10 ms (SysTick COUNTFLAG, polled) main prints "qd <CNT> up|down\r\n" (QDCTRL.QUADIR = direction of the last
 *    count) on UART0 (PTA2, 115200 8N1).
 *
 * Board: boards/k22_ftm_qd_encoder.yaml (a quadrature_encoder part on PTB0/PTB1, 100 lines at 600 rpm = 4000 edges
 * per second: +40 per line, reversed at 30 ms).  Like ftm_capture it does not program the MCG: run it with
 * --clock 60e6 (core = bus = 60 MHz). */
#include "k22.h"

#define SIM_SCGC6_FTM1   (1u << 25)

#define FTM1_BASE     0x40039000u
#define FTM1_SC       REG32(FTM1_BASE + 0x00)
#define FTM1_CNT      REG32(FTM1_BASE + 0x04)
#define FTM1_MOD      REG32(FTM1_BASE + 0x08)
#define FTM1_CNTIN    REG32(FTM1_BASE + 0x4C)
#define FTM1_MODE     REG32(FTM1_BASE + 0x54)
#define FTM1_QDCTRL   REG32(FTM1_BASE + 0x80)
#define FTM_SC_CLKS_SYSTEM  (1u << 3)
#define FTM_MODE_FTMEN      (1u << 0)
#define FTM_MODE_WPDIS      (1u << 2)
#define FTM_QDCTRL_QUADEN   (1u << 0)
#define FTM_QDCTRL_QUADIR   (1u << 2)

#define BAUD        115200u
#define TICK_10MS   (BUS_CLK_HZ / 100u)                 /* SysTick on the core clock = 60 MHz with --clock 60e6 */

static void uart0_putc(char c)
{
    while (!(UART0_S1 & UART_S1_TDRE)) { }
    UART0_D = (uint8_t)c;
}

static void uart0_puts(const char *s)
{
    while (*s)
        uart0_putc(*s++);
}

static void uart0_putu(uint32_t v)
{
    char buf[11];
    int i = 0;
    do {
        buf[i++] = (char)('0' + v % 10u);
        v /= 10u;
    } while (v);
    while (i)
        uart0_putc(buf[--i]);
}

int main(void)
{
    WDOG_Disable();
    SIM_SCGC5 |= SIM_SCGC5_PORTA | SIM_SCGC5_PORTB;
    SIM_SCGC4 |= SIM_SCGC4_UART0;
    SIM_SCGC6 |= SIM_SCGC6_FTM1;
    PORTA->PCR[1] = PORT_PCR_MUX(2);                    /* UART0_RX */
    PORTA->PCR[2] = PORT_PCR_MUX(2);                    /* UART0_TX */
    PORTB->PCR[0] = PORT_PCR_MUX(6);                    /* FTM1_QD_PHA */
    PORTB->PCR[1] = PORT_PCR_MUX(6);                    /* FTM1_QD_PHB */

    uint32_t sbr = BUS_CLK_HZ / (16u * BAUD);
    uint32_t brfa = ((BUS_CLK_HZ * 4u / BAUD + 1u) / 2u - sbr * 32u) & 0x1Fu;
    UART0_C2 = 0;
    UART0_BDH = (uint8_t)((sbr >> 8) & 0x1F);
    UART0_BDL = (uint8_t)(sbr & 0xFF);
    UART0_C4 = (UART0_C4 & ~0x1Fu) | UART_C4_BRFA(brfa);
    UART0_C1 = 0;
    UART0_C2 = UART_C2_TE | UART_C2_RE;

    FTM1_MODE = FTM_MODE_WPDIS | FTM_MODE_FTMEN;
    FTM1_SC = 0;
    FTM1_CNTIN = 0;
    FTM1_MOD = 0xFFFFu;
    FTM1_CNT = 0;
    FTM1_QDCTRL = FTM_QDCTRL_QUADEN;                    /* phase A / phase B encoding, no inversion */
    FTM1_SC = FTM_SC_CLKS_SYSTEM;

    SYST_RVR = TICK_10MS - 1u;
    SYST_CVR = 0;
    SYST_CSR = SYST_CSR_ENABLE | SYST_CSR_CLKSOURCE;

    uart0_puts("ftm_qd\r\n");
    for (;;) {
        while (!(SYST_CSR & SYST_CSR_COUNTFLAG)) { }
        uint32_t cnt = FTM1_CNT & 0xFFFFu;
        uint32_t up = FTM1_QDCTRL & FTM_QDCTRL_QUADIR;
        uart0_puts("qd ");
        uart0_putu(cnt);
        uart0_puts(up ? " up\r\n" : " down\r\n");
    }
}
