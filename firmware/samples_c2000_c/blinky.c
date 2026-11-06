/* blinky.c -- TMS320F28232: GPIO34 toggles every 1 ms, timed by CPU timer 0 (TIF polled), 100 MHz after the PLL.
 * Run: chipem run firmware/samples_c2000_c/build/blinky.out --chip tms320f28232 --for 12ms */
#include "f28232.h"

volatile unsigned long toggles;

void main(void)
{
    toggles = 0;                                        /* COFF ABI: .ebss is not zeroed */
    EALLOW;
    WDCR = WDCR_DISABLE;
    GpioCtrlRegs.GPBDIR |= GPB_BIT(34);
    PLLSTS &= ~PLLSTS_DIVSEL_MSK;
    PLLCR = 10;
    while (!(PLLSTS & PLLSTS_PLLLOCKS)) { }
    PLLSTS |= PLLSTS_DIVSEL_2;                          /* 20 MHz x 10 / 2 = 100 MHz */
    EDIS;

    CpuTimer0Regs.TCR = TCR_TSS;
    CpuTimer0Regs.PRD = 100000ul - 1ul;                 /* 1 ms */
    CpuTimer0Regs.TPR = 0;
    CpuTimer0Regs.TPRH = 0;
    CpuTimer0Regs.TCR = TCR_TIF | TCR_TRB;              /* run, no interrupt */

    for (;;) {
        while (!(CpuTimer0Regs.TCR & TCR_TIF)) { }
        CpuTimer0Regs.TCR |= TCR_TIF;                   /* write 1 to clear (TSS stays 0) */
        GpioDataRegs.GPBTOGGLE = GPB_BIT(34);
        toggles++;
    }
}
