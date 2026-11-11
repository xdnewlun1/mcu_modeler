/* f2808.h -- minimal TMS320F2808 register definitions for the CHIP_EM C samples (TI C2000 compiler, cl2000).
 *
 * Written for this repository (not TI's DSP280x headers), in the style of f28232.h: `volatile unsigned int`
 * (16 bit) and `volatile unsigned long` (32 bit) registers at absolute word addresses (SPRS230P Tables 9-2..9-31,
 * SPRU712H).  What differs from the F28232: PLLSTS.CLKINDIV (bit 1) instead of DIVSEL, PCLKCR0 gates SPI-B/C/D,
 * no PCLKCR3 (CPU timers and GPIO inputs are not gated), GPIO ports A/B only, eCAN clock = SYSCLKOUT.
 * C28x: char = int = 16 bits, long = 32 bits; a 32-bit register is low word first. */
#ifndef F2808_H
#define F2808_H

#define REG16(a) (*(volatile unsigned int *)(a))
#define REG32(a) (*(volatile unsigned long *)(a))

#define EALLOW asm(" EALLOW")
#define EDIS   asm(" EDIS")
#define EINT   asm(" clrc INTM")
#define DINT   asm(" setc INTM")
#define IDLE   asm(" IDLE")

extern __cregister volatile unsigned int IER;
extern __cregister volatile unsigned int IFR;

/* ---- system control, 0x7010 (EALLOW protected), SPRU712H Table 3-1 ---- */
#define XCLK       REG16(0x7010)
#define PLLSTS     REG16(0x7011)     /* bit 0 PLLLOCKS, bit 1 CLKINDIV */
#define HISPCP     REG16(0x701A)     /* HSPCLK = SYSCLKOUT / (2 * HSPCLK), reset 1: /2 */
#define LOSPCP     REG16(0x701B)     /* LSPCLK = SYSCLKOUT / (2 * LSPCLK), reset 2: /4 */
#define PCLKCR0    REG16(0x701C)
#define PCLKCR1    REG16(0x701D)
#define LPMCR0     REG16(0x701E)
#define PLLCR      REG16(0x7021)     /* PLL multiplier (0..10) */
#define SCSR       REG16(0x7022)
#define WDCNTR     REG16(0x7023)
#define WDKEY      REG16(0x7025)
#define WDCR       REG16(0x7029)     /* 0x0068 = WDDIS | WDCHK(101b) */

#define PLLSTS_PLLLOCKS   0x0001u
#define PLLSTS_CLKINDIV   0x0002u    /* 0: CLKIN = PLL output / 2 (reset) */
#define PCLKCR0_TBCLKSYNC 0x0004u
#define PCLKCR0_ADCENCLK  0x0008u
#define PCLKCR0_I2CAENCLK 0x0010u
#define PCLKCR0_SPICENCLK 0x0040u
#define PCLKCR0_SPIDENCLK 0x0080u
#define PCLKCR0_SPIAENCLK 0x0100u
#define PCLKCR0_SPIBENCLK 0x0200u
#define PCLKCR0_SCIAENCLK 0x0400u
#define PCLKCR0_SCIBENCLK 0x0800u
#define PCLKCR0_ECANAENCLK 0x4000u
#define PCLKCR0_ECANBENCLK 0x8000u
#define PCLKCR1_EPWM1ENCLK 0x0001u
#define WDCR_DISABLE      0x0068u

/* ---- CPU timers, 0x0C00 / 0x0C08 / 0x0C10 ---- */
struct CPUTIMER_REGS {
    volatile unsigned long TIM;      /* +0 counter */
    volatile unsigned long PRD;      /* +2 period */
    volatile unsigned int  TCR;      /* +4 control: bit 4 TSS, bit 5 TRB, bit 14 TIE, bit 15 TIF (w1c) */
    volatile unsigned int  rsvd1;
    volatile unsigned int  TPR;      /* +6 */
    volatile unsigned int  TPRH;     /* +7 */
};
#define CpuTimer0Regs (*(volatile struct CPUTIMER_REGS *)0x0C00)
#define TCR_TSS 0x0010u
#define TCR_TRB 0x0020u
#define TCR_TIE 0x4000u
#define TCR_TIF 0x8000u

/* ---- PIE, 0x0CE0 (registers) and 0x0D00 (vector table) ---- */
struct PIE_GROUP { volatile unsigned int IER, IFR; };
struct PIE_CTRL_REGS {
    volatile unsigned int PIECTRL;   /* +0 bit 0 ENPIE */
    volatile unsigned int PIEACK;    /* +1 */
    struct PIE_GROUP      GRP[12];   /* +2 + 2(x-1) */
};
#define PieCtrlRegs (*(volatile struct PIE_CTRL_REGS *)0x0CE0)
#define PIEIER(x) (PieCtrlRegs.GRP[(x) - 1].IER)
#define PIE_VECT  ((volatile unsigned long *)0x0D00)
#define PIE_VECNUM(x, y) (32u + 8u * ((x) - 1u) + ((y) - 1u))
#define PIEACK_GROUP(x) (1u << ((x) - 1u))
#define M_INT1 0x0001u
#define M_INT3 0x0004u
#define M_INT9 0x0100u

/* ---- GPIO, 0x6F80 (control, EALLOW) / 0x6FC0 (data): ports A (GPIO0-31) and B (GPIO32-34) ---- */
struct GPIO_CTRL_REGS {
    volatile unsigned long GPACTRL, GPAQSEL1, GPAQSEL2, GPAMUX1, GPAMUX2, GPADIR, GPAPUD, rsvd1;   /* +0x00 */
    volatile unsigned long GPBCTRL, GPBQSEL1, GPBQSEL2, GPBMUX1, GPBMUX2, GPBDIR, GPBPUD;          /* +0x10 */
};
#define GpioCtrlRegs (*(volatile struct GPIO_CTRL_REGS *)0x6F80)
struct GPIO_DATA_REGS {
    volatile unsigned long GPADAT, GPASET, GPACLEAR, GPATOGGLE;
    volatile unsigned long GPBDAT, GPBSET, GPBCLEAR, GPBTOGGLE;
};
#define GpioDataRegs (*(volatile struct GPIO_DATA_REGS *)0x6FC0)
#define GPB_BIT(n) (1ul << ((n) - 32))

/* ---- SCI-A 0x7050, SCI-B 0x7750 ---- */
struct SCI_REGS {
    volatile unsigned int SCICCR, SCICTL1, SCIHBAUD, SCILBAUD, SCICTL2, SCIRXST, SCIRXEMU, SCIRXBUF;
    volatile unsigned int rsvd1, SCITXBUF, SCIFFTX, SCIFFRX, SCIFFCT, rsvd2[2], SCIPRI;
};
#define SciaRegs (*(volatile struct SCI_REGS *)0x7050)
#define ScibRegs (*(volatile struct SCI_REGS *)0x7750)
#define SCICTL1_RXENA   0x0001u
#define SCICTL1_TXENA   0x0002u
#define SCICTL1_SWRESET 0x0020u
#define SCICTL2_TXRDY   0x0080u
#define SCIRXST_RXRDY   0x0040u

/* ---- ePWM1 0x6800, ADC 0x7100 (results also right-justified at 0x0B00) ---- */
#define EPWM1(off)   REG16(0x6800 + (off))
#define TBCTL1   EPWM1(0x00)
#define TBPRD1   EPWM1(0x05)
#define CMPA1    EPWM1(0x09)
#define AQCTLA1  EPWM1(0x0B)
#define ETSEL1   EPWM1(0x19)
#define ETPS1    EPWM1(0x1A)
#define ADCTRL1      REG16(0x7100)
#define ADCTRL2      REG16(0x7101)
#define ADCMAXCONV   REG16(0x7102)
#define ADCCHSELSEQ1 REG16(0x7103)
#define ADCTRL3      REG16(0x7118)
#define ADCST        REG16(0x7119)
#define ADCRESULT_M(n) REG16(0x0B00 + (n))

#endif
