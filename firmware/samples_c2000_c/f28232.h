/* f28232.h -- minimal TMS320F28232 register definitions for the CHIP_EM C samples (TI C2000 compiler, cl2000).
 *
 * Written for this repository (not TI's DSP2833x headers): plain `volatile unsigned int` (16 bit) and
 * `volatile unsigned long` (32 bit) members, no bitfields, structs overlaid on the absolute addresses with pointer
 * casts (no DATA_SECTION / linker placement needed).  Addresses are 16-bit word addresses (SPRS439, SPRUFB0,
 * SPRUFZ5); they are the same ones the assembler samples in firmware/samples_c2000/ use.
 * C28x: char = int = 16 bits, long = 32 bits; a 32-bit register is low word first. */
#ifndef F28232_H
#define F28232_H

#define REG16(a) (*(volatile unsigned int *)(a))
#define REG32(a) (*(volatile unsigned long *)(a))

#define EALLOW asm(" EALLOW")
#define EDIS   asm(" EDIS")
#define EINT   asm(" clrc INTM")
#define DINT   asm(" setc INTM")
#define IDLE   asm(" IDLE")

/* CPU interrupt enable / flag registers (TI compiler control registers) */
extern __cregister volatile unsigned int IER;
extern __cregister volatile unsigned int IFR;

/* ---- system control, 0x7010 (EALLOW protected) ---- */
#define PLLSTS     REG16(0x7011)     /* bit 0 PLLLOCKS, bits 8:7 DIVSEL */
#define HISPCP     REG16(0x701A)
#define LOSPCP     REG16(0x701B)     /* LSPCLK = SYSCLKOUT / (2 * LSPCLK), reset 2: /4 */
#define PCLKCR0    REG16(0x701C)     /* bit 10 SCIAENCLK, bit 11 SCIBENCLK */
#define PCLKCR1    REG16(0x701D)
#define PCLKCR3    REG16(0x7020)     /* bit 13 GPIOINENCLK */
#define PLLCR      REG16(0x7021)     /* PLL multiplier (0..10) */
#define SCSR       REG16(0x7022)
#define WDCNTR     REG16(0x7023)
#define WDKEY      REG16(0x7025)
#define WDCR       REG16(0x7029)     /* 0x0068 = WDDIS | WDCHK(101b) */

#define PLLSTS_PLLLOCKS   0x0001u
#define PLLSTS_DIVSEL_MSK 0x0180u
#define PLLSTS_DIVSEL_2   0x0100u    /* /2 */
#define PCLKCR0_SCIAENCLK 0x0400u
#define WDCR_DISABLE      0x0068u

/* ---- CPU timers, 0x0C00 / 0x0C08 / 0x0C10 ---- */
struct CPUTIMER_REGS {
    volatile unsigned long TIM;      /* +0 counter */
    volatile unsigned long PRD;      /* +2 period */
    volatile unsigned int  TCR;      /* +4 control: bit 4 TSS, bit 5 TRB, bit 14 TIE, bit 15 TIF (w1c) */
    volatile unsigned int  rsvd1;    /* +5 */
    volatile unsigned int  TPR;      /* +6 prescale low (TDDR, PSC) */
    volatile unsigned int  TPRH;     /* +7 prescale high */
};
#define CpuTimer0Regs (*(volatile struct CPUTIMER_REGS *)0x0C00)
#define CpuTimer1Regs (*(volatile struct CPUTIMER_REGS *)0x0C08)
#define CpuTimer2Regs (*(volatile struct CPUTIMER_REGS *)0x0C10)
#define TCR_TSS 0x0010u
#define TCR_TRB 0x0020u
#define TCR_TIE 0x4000u
#define TCR_TIF 0x8000u

/* ---- PIE, 0x0CE0 (registers) and 0x0D00 (vector table, 128 x 32 bit, EALLOW protected on silicon) ---- */
struct PIE_GROUP { volatile unsigned int IER, IFR; };
struct PIE_CTRL_REGS {
    volatile unsigned int PIECTRL;   /* +0 bit 0 ENPIE */
    volatile unsigned int PIEACK;    /* +1 write 1 to acknowledge group x (bit x-1) */
    struct PIE_GROUP      GRP[12];   /* +2 + 2(x-1): PIEIERx, PIEIFRx */
};
#define PieCtrlRegs (*(volatile struct PIE_CTRL_REGS *)0x0CE0)
#define PIEIER(x) (PieCtrlRegs.GRP[(x) - 1].IER)
#define PIE_VECT  ((volatile unsigned long *)0x0D00)     /* vector n at 0x0D00 + 2n */
#define PIE_VECNUM(x, y) (32u + 8u * ((x) - 1u) + ((y) - 1u))
#define PIEACK_GROUP(x) (1u << ((x) - 1u))
#define VEC_TINT0     PIE_VECNUM(1, 7)    /* 38: 0x0D4C */
#define VEC_SCIRXINTA PIE_VECNUM(9, 1)    /* 96: 0x0DC0 */
#define VEC_SCITXINTA PIE_VECNUM(9, 2)
#define M_INT1 0x0001u
#define M_INT9 0x0100u

/* ---- GPIO control, 0x6F80 (EALLOW protected) ---- */
struct GPIO_CTRL_REGS {
    volatile unsigned long GPACTRL;  /* +0x00 */
    volatile unsigned long GPAQSEL1; /* +0x02 */
    volatile unsigned long GPAQSEL2; /* +0x04 */
    volatile unsigned long GPAMUX1;  /* +0x06 GPIO0-15, 2 bits each */
    volatile unsigned long GPAMUX2;  /* +0x08 GPIO16-31 */
    volatile unsigned long GPADIR;   /* +0x0A */
    volatile unsigned long GPAPUD;   /* +0x0C 1 = pull-up disabled */
    volatile unsigned long rsvd1;    /* +0x0E */
    volatile unsigned long GPBCTRL;  /* +0x10 */
    volatile unsigned long GPBQSEL1; /* +0x12 */
    volatile unsigned long GPBQSEL2; /* +0x14 */
    volatile unsigned long GPBMUX1;  /* +0x16 GPIO32-47 */
    volatile unsigned long GPBMUX2;  /* +0x18 GPIO48-63 */
    volatile unsigned long GPBDIR;   /* +0x1A */
    volatile unsigned long GPBPUD;   /* +0x1C */
};
#define GpioCtrlRegs (*(volatile struct GPIO_CTRL_REGS *)0x6F80)

/* ---- GPIO data, 0x6FC0 (not protected) ---- */
struct GPIO_DATA_REGS {
    volatile unsigned long GPADAT, GPASET, GPACLEAR, GPATOGGLE;   /* +0x00 .. +0x06 */
    volatile unsigned long GPBDAT, GPBSET, GPBCLEAR, GPBTOGGLE;   /* +0x08 .. +0x0E */
    volatile unsigned long GPCDAT, GPCSET, GPCCLEAR, GPCTOGGLE;   /* +0x10 .. +0x16 */
};
#define GpioDataRegs (*(volatile struct GPIO_DATA_REGS *)0x6FC0)
#define GPB_BIT(n) (1ul << ((n) - 32))       /* GPIO32-63 */

/* ---- SCI-A 0x7050, SCI-B 0x7750 ---- */
struct SCI_REGS {
    volatile unsigned int SCICCR;    /* +0x0 bits 2:0 char length - 1, bit 4 LOOPBKENA, bit 5 PARITYENA, bit 7 STOP */
    volatile unsigned int SCICTL1;   /* +0x1 bit 0 RXENA, bit 1 TXENA, bit 5 SWRESET, bit 6 RXERRINTENA */
    volatile unsigned int SCIHBAUD;  /* +0x2 BRR = LSPCLK / (baud * 8) - 1 */
    volatile unsigned int SCILBAUD;  /* +0x3 */
    volatile unsigned int SCICTL2;   /* +0x4 bit 0 TXINTENA, bit 1 RXBKINTENA, bit 6 TXEMPTY, bit 7 TXRDY */
    volatile unsigned int SCIRXST;   /* +0x5 bit 6 RXRDY, bit 7 RXERROR */
    volatile unsigned int SCIRXEMU;  /* +0x6 */
    volatile unsigned int SCIRXBUF;  /* +0x7 reading clears RXRDY */
    volatile unsigned int rsvd1;     /* +0x8 */
    volatile unsigned int SCITXBUF;  /* +0x9 */
    volatile unsigned int SCIFFTX;   /* +0xA */
    volatile unsigned int SCIFFRX;   /* +0xB */
    volatile unsigned int SCIFFCT;   /* +0xC */
    volatile unsigned int rsvd2[2];  /* +0xD */
    volatile unsigned int SCIPRI;    /* +0xF */
};
#define SciaRegs (*(volatile struct SCI_REGS *)0x7050)
#define ScibRegs (*(volatile struct SCI_REGS *)0x7750)
#define SCICTL1_RXENA   0x0001u
#define SCICTL1_TXENA   0x0002u
#define SCICTL1_SWRESET 0x0020u
#define SCICTL2_RXBKINTENA 0x0002u
#define SCICTL2_TXRDY   0x0080u
#define SCIRXST_RXRDY   0x0040u

#endif
