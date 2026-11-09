/* ftm_capture: FTM1 channel 0 input capture measures the period of a pulse train and prints it on UART0.
 *
 *  - FTM0 CH0: edge-aligned PWM on PTC1 (ALT4), 10 kHz (MOD = 5999 at the 60 MHz bus), 25 % high.
 *  - FTM1 CH0: input capture of rising edges on PTA12 (ALT3 = FTM1_CH0; QUADEN=DECAPEN=COMBINE=CPWMS=0,
 *    MSnB:MSnA=00, ELSnB:ELSnA=01, K22P100M120SF5V2RM 38.4.4), FTM1 free running from the bus clock (FTMEN=0,
 *    MOD=0xFFFF). The FTM1 interrupt (IRQ 43) takes C0V, the period is the difference of two captures modulo 2^16.
 *  - main prints "period <ticks>\r\n" on UART0 (PTA2, 115200 8N1) for the first 5 measurements.
 *
 * Wire PTC1 to PTA12 (boards/k22_ftm_capture.yaml) to measure the PWM: 6000 ticks = 100 us. Without the wire, edges
 * on PTA12 from --at are measured instead, e.g. --at 1ms:PTA12=1 --at 1.1ms:PTA12=0 --at 1.25ms:PTA12=1 gives
 * "period 15000" (250 us). Like ftm_pwm it does not program the MCG: run it with --clock 60e6 (bus = core 60 MHz). */
#include "k22.h"

#define SIM_SCGC6_FTM0   (1u << 24)
#define SIM_SCGC6_FTM1   (1u << 25)

#define FTM_SC(b)     REG32((b) + 0x00)
#define FTM_CNT(b)    REG32((b) + 0x04)
#define FTM_MOD(b)    REG32((b) + 0x08)
#define FTM_C0SC(b)   REG32((b) + 0x0C)
#define FTM_C0V(b)    REG32((b) + 0x10)
#define FTM_CNTIN(b)  REG32((b) + 0x4C)
#define FTM_MODE(b)   REG32((b) + 0x54)
#define FTM0_BASE     0x40038000u
#define FTM1_BASE     0x40039000u
#define FTM_SC_CLKS_SYSTEM  (1u << 3)
#define FTM_CnSC_CHF        (1u << 7)
#define FTM_CnSC_CHIE       (1u << 6)
#define FTM_CnSC_MSB        (1u << 5)
#define FTM_CnSC_ELSB       (1u << 3)
#define FTM_CnSC_ELSA       (1u << 2)
#define FTM_MODE_WPDIS      (1u << 2)
#define FTM1_IRQn           43

#define PWM_MOD     (BUS_CLK_HZ / 10000u - 1u)          /* 5999: 10 kHz */
#define BAUD        115200u
#define N_PRINT     5u

static volatile uint32_t last_cv, have_last, period, seq;

void FTM1_IRQHandler(void)
{
    uint32_t sc = FTM_C0SC(FTM1_BASE);                  /* read CHF = 1 ... */
    uint32_t cv = FTM_C0V(FTM1_BASE);
    FTM_C0SC(FTM1_BASE) = sc & ~FTM_CnSC_CHF;           /* ... then write 0: cleared (38.3.6) */
    if (have_last) {
        period = (cv - last_cv) & 0xFFFFu;
        seq++;
    }
    last_cv = cv;
    have_last = 1;
}

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
    SIM_SCGC5 |= SIM_SCGC5_PORTA | SIM_SCGC5_PORTC;
    SIM_SCGC4 |= SIM_SCGC4_UART0;
    SIM_SCGC6 |= SIM_SCGC6_FTM0 | SIM_SCGC6_FTM1;
    PORTA->PCR[1] = PORT_PCR_MUX(2);                    /* UART0_RX */
    PORTA->PCR[2] = PORT_PCR_MUX(2);                    /* UART0_TX */
    PORTC->PCR[1] = PORT_PCR_MUX(4) | PORT_PCR_DSE;     /* PTC1 = FTM0_CH0 (PWM out) */
    PORTA->PCR[12] = PORT_PCR_MUX(3);                   /* PTA12 = FTM1_CH0 (capture in) */

    uint32_t sbr = BUS_CLK_HZ / (16u * BAUD);
    uint32_t brfa = ((BUS_CLK_HZ * 4u / BAUD + 1u) / 2u - sbr * 32u) & 0x1Fu;
    UART0_C2 = 0;
    UART0_BDH = (uint8_t)((sbr >> 8) & 0x1F);
    UART0_BDL = (uint8_t)(sbr & 0xFF);
    UART0_C4 = (UART0_C4 & ~0x1Fu) | UART_C4_BRFA(brfa);
    UART0_C1 = 0;
    UART0_C2 = UART_C2_TE | UART_C2_RE;

    /* FTM1: free running capture timer */
    FTM_MODE(FTM1_BASE) = FTM_MODE_WPDIS;
    FTM_SC(FTM1_BASE) = 0;
    FTM_CNTIN(FTM1_BASE) = 0;
    FTM_MOD(FTM1_BASE) = 0xFFFFu;
    FTM_CNT(FTM1_BASE) = 0;
    FTM_C0SC(FTM1_BASE) = FTM_CnSC_ELSA | FTM_CnSC_CHIE;    /* input capture, rising edges, interrupt */
    NVIC_EnableIRQ(FTM1_IRQn);
    __enable_irq();
    FTM_SC(FTM1_BASE) = FTM_SC_CLKS_SYSTEM;

    /* FTM0: the PWM to measure */
    FTM_MODE(FTM0_BASE) = FTM_MODE_WPDIS;
    FTM_SC(FTM0_BASE) = 0;
    FTM_CNTIN(FTM0_BASE) = 0;
    FTM_CNT(FTM0_BASE) = 0;
    FTM_MOD(FTM0_BASE) = PWM_MOD;
    FTM_C0SC(FTM0_BASE) = FTM_CnSC_MSB | FTM_CnSC_ELSB;     /* EPWM, high-true */
    FTM_C0V(FTM0_BASE) = (PWM_MOD + 1u) / 4u;
    FTM_SC(FTM0_BASE) = FTM_SC_CLKS_SYSTEM;

    uart0_puts("ftm_capture\r\n");
    uint32_t printed = 0, seen = 0;
    for (;;) {
        if (seq != seen && printed < N_PRINT) {
            seen = seq;
            uint32_t p = period;
            uart0_puts("period ");
            uart0_putu(p);
            uart0_puts("\r\n");
            printed++;
            continue;
        }
        __disable_irq();                                /* sleep only if no capture came in since the check: */
        if (seq == seen || printed >= N_PRINT)          /* a pending IRQ still wakes WFI with PRIMASK set */
            __WFI();
        __enable_irq();
    }
}
