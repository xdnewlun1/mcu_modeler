/* ftm_pwm: FTM0 channel 0 edge-aligned PWM, high-true pulses (EPWM: QUADEN=DECAPEN=COMBINE=CPWMS=0, MSnB=1,
 * ELSnB=1), 1 kHz with 25 % duty on PTC1 (ALT4 = FTM0_CH0).  The FTM counts the bus clock (CLKS=01, PS=/1) from
 * CNTIN=0 to MOD = BUS/1000 - 1; the output is set at the counter reload and cleared at the C0V match
 * (K22P100M120SF5V2RM 38.4.6).  Like pit_irq it does not program the MCG: run it with --clock 60e6
 * (SIM_CLKDIV1 reset value: bus = core) so that the bus is 60 MHz.  No interrupts: the CPU sleeps. */
#include "k22.h"

#define SIM_SCGC6_FTM0   (1u << 24)

#define FTM0_BASE   0x40038000u
#define FTM0_SC     REG32(FTM0_BASE + 0x00)
#define FTM0_CNT    REG32(FTM0_BASE + 0x04)
#define FTM0_MOD    REG32(FTM0_BASE + 0x08)
#define FTM0_C0SC   REG32(FTM0_BASE + 0x0C)
#define FTM0_C0V    REG32(FTM0_BASE + 0x10)
#define FTM0_CNTIN  REG32(FTM0_BASE + 0x4C)
#define FTM0_MODE   REG32(FTM0_BASE + 0x54)
#define FTM_SC_CLKS_SYSTEM  (1u << 3)          /* CLKS = 01: system (bus) clock */
#define FTM_SC_PS(x)        ((uint32_t)(x) & 7u)
#define FTM_CnSC_MSB        (1u << 5)
#define FTM_CnSC_ELSB       (1u << 3)
#define FTM_MODE_WPDIS      (1u << 2)

#define PWM_HZ      1000u
#define PWM_MOD     (BUS_CLK_HZ / PWM_HZ - 1u)        /* 59999 */
#define PWM_C0V     ((PWM_MOD + 1u) / 4u)              /* 15000: 25 % high */

int main(void)
{
    WDOG_Disable();
    SIM_SCGC5 |= SIM_SCGC5_PORTC;
    SIM_SCGC6 |= SIM_SCGC6_FTM0;
    PORTC->PCR[1] = PORT_PCR_MUX(4) | PORT_PCR_DSE;     /* PTC1 = FTM0_CH0 */

    FTM0_MODE = FTM_MODE_WPDIS;                    /* FTMEN = 0: TPM-compatible, registers update at once */
    FTM0_SC = 0;                                   /* counter stopped while configuring */
    FTM0_CNTIN = 0;
    FTM0_CNT = 0;
    FTM0_MOD = PWM_MOD;
    FTM0_C0SC = FTM_CnSC_MSB | FTM_CnSC_ELSB;      /* edge-aligned PWM, high-true */
    FTM0_C0V = PWM_C0V;
    FTM0_SC = FTM_SC_CLKS_SYSTEM | FTM_SC_PS(0);   /* start: bus clock / 1 */
    for (;;)
        __WFI();
}
