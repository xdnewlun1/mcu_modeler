/* piccolo.h -- minimal TMS320F2802x / F2806x (Piccolo) register definitions for the CHIP_EM C samples (cl2000).
 *
 * Written for this repository (not TI's F2802x / F2806x headers), in the style of f2808.h: `volatile unsigned int`
 * (16 bit) and `volatile unsigned long` (32 bit) registers at absolute word addresses.  Addresses: SPRS523P Tables
 * 9-8..9-29 (F2802x) and SPRS698J Tables 8-6..8-9 (F2806x); bit fields: SPRUI09A / SPRUH18I.  What differs from the
 * 280x/2833x: CLKCTL / INTOSC clock sources, PLLSTS.DIVSEL with /1, PLLLOCKPRD, PCLKCR3 gates (CPU timers, COMPs),
 * the NMI watchdog (0x7060), the SOC-based type-3 ADC (0x7100, results at 0x0B00), comparators (0x6400), the
 * TI-reserved OTP routines Device_cal (0x3D7C80) and the temperature-sensor getters.
 * C28x: char = int = 16 bits, long = 32 bits; a 32-bit register is low word first. */
#ifndef PICCOLO_H
#define PICCOLO_H

#define REG16(a) (*(volatile unsigned int *)(a))
#define REG32(a) (*(volatile unsigned long *)(a))

#define EALLOW asm(" EALLOW")
#define EDIS   asm(" EDIS")
#define EINT   asm(" clrc INTM")
#define DINT   asm(" setc INTM")

extern __cregister volatile unsigned int IER;
extern __cregister volatile unsigned int IFR;

/* ---- TI-reserved OTP routines (SPRUI09 Example 1-1, 6.12.1; SPRUH18 8.12.1) ---- */
#define Device_cal ((void (*)(void))0x3D7C80)
#ifdef PICCOLO_2806X
#define getTempSlope()  ((*(int (*)(void))0x3D7E82)())
#define getTempOffset() ((*(int (*)(void))0x3D7E85)())
#else
#define getTempSlope()  ((*(int (*)(void))0x3D7E80)())
#define getTempOffset() ((*(int (*)(void))0x3D7E83)())
#endif

/* ---- system control, 0x7010 (EALLOW protected), SPRUI09 Table 1-14 ---- */
#define XCLK       REG16(0x7010)
#define PLLSTS     REG16(0x7011)     /* 0 PLLLOCKS, 3 MCLKSTS, 4 MCLKCLR, 6 MCLKOFF, 8:7 DIVSEL */
#define CLKCTL     REG16(0x7012)
#define PLLLOCKPRD REG16(0x7013)
#define LOSPCP     REG16(0x701B)
#define PCLKCR0    REG16(0x701C)
#define PCLKCR1    REG16(0x701D)
#define LPMCR0     REG16(0x701E)
#define PCLKCR3    REG16(0x7020)
#define PLLCR      REG16(0x7021)
#define WDCR       REG16(0x7029)
#define PLLSTS_PLLLOCKS 0x0001u
#define PLLSTS_MCLKSTS  0x0008u
#define PLLSTS_MCLKOFF  0x0040u
#define PLLSTS_DIVSEL   0x0180u
#define WDCR_DISABLE    0x0068u
#define PCLKCR0_TBCLKSYNC 0x0004u
#define PCLKCR0_ADCENCLK  0x0008u
#define PCLKCR0_SPIAENCLK 0x0100u
#define PCLKCR0_SCIAENCLK 0x0400u
#define PCLKCR0_ECANAENCLK 0x4000u          /* F2806x */
#define PCLKCR1_EPWM1ENCLK 0x0001u

/* ---- NMI watchdog, 0x7060 ---- */
#define NMIFLG     REG16(0x7061)

/* ---- CPU timers ---- */
struct CPUTIMER_REGS {
    volatile unsigned long TIM, PRD;
    volatile unsigned int  TCR, rsvd1, TPR, TPRH;
};
#define CpuTimer0Regs (*(volatile struct CPUTIMER_REGS *)0x0C00)
#define TCR_TSS 0x0010u
#define TCR_TRB 0x0020u
#define TCR_TIE 0x4000u
#define TCR_TIF 0x8000u

/* ---- PIE ---- */
struct PIE_GROUP { volatile unsigned int IER, IFR; };
struct PIE_CTRL_REGS { volatile unsigned int PIECTRL, PIEACK; struct PIE_GROUP GRP[12]; };
#define PieCtrlRegs (*(volatile struct PIE_CTRL_REGS *)0x0CE0)
#define PIEIER(x) (PieCtrlRegs.GRP[(x) - 1].IER)
#define PIE_VECT  ((volatile unsigned long *)0x0D00)
#define PIE_VECNUM(x, y) (32u + 8u * ((x) - 1u) + ((y) - 1u))
#define PIEACK_GROUP(x) (1u << ((x) - 1u))

/* ---- GPIO ---- */
struct GPIO_CTRL_REGS {
    volatile unsigned long GPACTRL, GPAQSEL1, GPAQSEL2, GPAMUX1, GPAMUX2, GPADIR, GPAPUD, rsvd1;
    volatile unsigned long GPBCTRL, GPBQSEL1, GPBQSEL2, GPBMUX1, GPBMUX2, GPBDIR, GPBPUD;
};
#define GpioCtrlRegs (*(volatile struct GPIO_CTRL_REGS *)0x6F80)
struct GPIO_DATA_REGS {
    volatile unsigned long GPADAT, GPASET, GPACLEAR, GPATOGGLE;
    volatile unsigned long GPBDAT, GPBSET, GPBCLEAR, GPBTOGGLE;
};
#define GpioDataRegs (*(volatile struct GPIO_DATA_REGS *)0x6FC0)
#define GPB_BIT(n) (1ul << ((n) - 32))

/* ---- SCI-A 0x7050 (F2806x SCI-B 0x7750); 4-word FIFOs ---- */
struct SCI_REGS {
    volatile unsigned int SCICCR, SCICTL1, SCIHBAUD, SCILBAUD, SCICTL2, SCIRXST, SCIRXEMU, SCIRXBUF;
    volatile unsigned int rsvd1, SCITXBUF, SCIFFTX, SCIFFRX, SCIFFCT, rsvd2[2], SCIPRI;
};
#define SciaRegs (*(volatile struct SCI_REGS *)0x7050)
#define SCICTL1_RXENA   0x0001u
#define SCICTL1_TXENA   0x0002u
#define SCICTL1_SWRESET 0x0020u
#define SCICTL2_TXRDY   0x0080u
#define SCIRXST_RXRDY   0x0040u

/* ---- type-3 ADC, 0x7100; results 0x0B00 (SPRUI09 Table 6-3) ---- */
#define ADCCTL1      REG16(0x7100)
#define ADCCTL2      REG16(0x7101)
#define ADCINTFLG    REG16(0x7104)
#define ADCINTFLGCLR REG16(0x7105)
#define INTSEL1N2    REG16(0x7108)
#define ADCSOCFRC1   REG16(0x711A)
#define ADCSOCCTL(n) REG16(0x7120 + (n))
#define ADCOFFTRIM   REG16(0x7141)
#define ADCRESULT(n) REG16(0x0B00 + (n))
#define ADCCTL1_TEMPCONV 0x0001u
#define ADCCTL1_POWERUP  0x40E0u       /* ADCENABLE | ADCPWDN | ADCBGPWD | ADCREFPWD */

/* ---- ePWM1 0x6800 ---- */
#define EPWM1(off) REG16(0x6800 + (off))
#endif
