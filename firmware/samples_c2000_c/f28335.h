/* f28335.h -- TMS320F28335 register definitions for the CHIP_EM C samples (TI C2000 compiler, cl2000, fpu32).
 *
 * The F28335 is the same die as the F28232 (SPRS439Q): every register used here has the F28232 address, so this
 * file reuses f28232.h and adds the ePWM and ADC blocks the FPU samples need.  Written for this repository (not
 * TI's DSP2833x headers): plain volatile 16/32-bit members at absolute word addresses, no bitfields. */
#ifndef F28335_H
#define F28335_H
#include "f28232.h"

/* ---- ePWM1..6, 0x6800 + 0x40 (n - 1) (TZ registers EALLOW protected), SPRS439Q Table 8-4 ---- */
struct EPWM_REGS {
    volatile unsigned int TBCTL, TBSTS, TBPHSHR, TBPHS, TBCTR, TBPRD, rsvd1, CMPCTL;   /* +0x00 .. +0x07 */
    volatile unsigned int CMPAHR, CMPA, CMPB, AQCTLA, AQCTLB, AQSFRC, AQCSFRC;         /* +0x08 .. +0x0E */
    volatile unsigned int DBCTL, DBRED, DBFED, TZSEL, TZDCSEL, TZCTL, TZEINT, TZFLG;   /* +0x0F .. +0x16 */
    volatile unsigned int TZCLR, TZFRC, ETSEL, ETPS, ETFLG, ETCLR, ETFRC;              /* +0x17 .. +0x1D */
};
#define EPwm1Regs (*(volatile struct EPWM_REGS *)0x6800)
#define EPwm2Regs (*(volatile struct EPWM_REGS *)0x6840)
#define TB_COUNT_UP     0x0000u
#define TB_FREEZE       0x0003u
#define TB_SYNC_DISABLE 0x0030u      /* SYNCOSEL = 11 */
#define AQ_ZRO_SET      0x0002u
#define AQ_CAU_CLEAR    0x0010u
#define ET_INTSEL_ZRO   0x0001u
#define ET_INTEN        0x0008u
#define ET_SOCASEL_PRD  0x0200u
#define ET_SOCAEN       0x0800u
#define ET_INTPRD_1ST   0x0001u
#define ET_SOCAPRD_1ST  0x0100u
#define ET_INT          0x0001u

/* ---- ADC, 0x7100 (results left-justified) ---- */
struct ADC_REGS {
    volatile unsigned int ADCTRL1, ADCTRL2, ADCMAXCONV;                                 /* +0x00 .. +0x02 */
    volatile unsigned int ADCCHSELSEQ1, ADCCHSELSEQ2, ADCCHSELSEQ3, ADCCHSELSEQ4;       /* +0x03 .. +0x06 */
    volatile unsigned int ADCASEQSR;                                                    /* +0x07 */
    volatile unsigned int ADCRESULT[16];                                                /* +0x08 */
    volatile unsigned int ADCTRL3, ADCST;                                               /* +0x18, +0x19 */
};
#define AdcRegs (*(volatile struct ADC_REGS *)0x7100)
#define ADCTRL1_SEQ_CASC       0x0010u
#define ADCTRL2_EPWM_SOCA_SEQ1 0x0100u
#define ADCTRL2_INT_ENA_SEQ1   0x0800u
#define ADCTRL2_RST_SEQ1       0x4000u
#define ADCTRL3_POWER_UP       0x00E0u   /* ADCBGRFDN = 11, ADCPWDN = 1 */
#define ADCTRL3_ADCCLKPS_3     0x0006u   /* HSPCLK / 6 */
#define ADCST_INT_SEQ1_CLR     0x0010u

#define PCLKCR0_ADCENCLK   0x0008u
#define PCLKCR0_TBCLKSYNC  0x0004u
#define PCLKCR1_EPWM1ENCLK 0x0001u
#define PCLKCR1_EPWM2ENCLK 0x0002u
#define VEC_ADCINT    PIE_VECNUM(1, 6)
#define VEC_SEQ1INT   PIE_VECNUM(1, 1)
#define M_INT3 0x0004u

#endif
