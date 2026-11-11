/* f2808_blinky.c -- TMS320F2808 C blinky for the TI compiler (cl2000): GPIO34 toggles every 1 ms, timed by CPU timer
 * 0 (polled TIF).  Clocks: 20 MHz OSCCLK, PLLCR = 10, CLKINDIV = 0 -> 100 MHz SYSCLKOUT.  Link: f2808.cmd. */
#include "f2808.h"

volatile unsigned long toggles;

static void init_sysctrl(void)
{
    EALLOW;
    WDCR = WDCR_DISABLE;
    PLLSTS &= ~PLLSTS_CLKINDIV;                 /* /2 before touching PLLCR (SPRU712H Figure 3-11) */
    PLLCR = 10;
    while (!(PLLSTS & PLLSTS_PLLLOCKS)) { }     /* 131072 OSCCLK = 6.55 ms */
    GpioCtrlRegs.GPBMUX1 &= ~(3ul << 4);        /* GPIO34 = GPIO */
    GpioDataRegs.GPBCLEAR = GPB_BIT(34);
    GpioCtrlRegs.GPBDIR |= GPB_BIT(34);
    EDIS;
}

void main(void)
{
    DINT;
    toggles = 0;
    init_sysctrl();
    CpuTimer0Regs.TCR = TCR_TSS;
    CpuTimer0Regs.PRD = 100000ul - 1;           /* 1 ms at 100 MHz */
    CpuTimer0Regs.TPR = 0;
    CpuTimer0Regs.TPRH = 0;
    CpuTimer0Regs.TCR = TCR_TIF | TCR_TRB;      /* clear TIF, reload, run */
    for (;;) {
        while (!(CpuTimer0Regs.TCR & TCR_TIF)) { }
        CpuTimer0Regs.TCR |= TCR_TIF;
        GpioDataRegs.GPBTOGGLE = GPB_BIT(34);
        toggles++;
    }
}
