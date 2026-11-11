/* epwm.c -- TMS320F28232 ePWM1 in TI's InitEPwm style, with its event-trigger interrupt and an ADC start of
 * conversion (TI cl2000):
 *   100 MHz SYSCLKOUT (PLL x10 /2), TBCLK = SYSCLKOUT, up count, TBPRD = 4999 -> 20 kHz (50 us) on GPIO0 (EPWM1A):
 *   AQCTLA ZRO set, CAU clear, CMPA = 1250 -> 25 % duty (12.5 us high).
 *   ETSEL: EPWM1_INT at CTR=ZERO every period (PIE 3.1) -> epwm1_isr counts `epwm_isr_count`, clears ETFLG.INT via
 *   ETCLR and acknowledges PIE group 3.  SOCA at CTR=PRD every period starts SEQ1 (cascaded, one conversion of
 *   ADCINA0) -> ADCINT (PIE 1.6) -> adc_isr stores ADCRESULT0 >> 4 in `adc_last`, counts `adc_isr_count`, resets
 *   SEQ1 to CONV00 (RST_SEQ1), clears INT_SEQ1 and acknowledges PIE group 1.
 *   Time bases start with PCLKCR0.TBCLKSYNC after the module is configured, as in TI's examples.
 * Run: chipem run firmware/samples_c2000_c/build/epwm.out --chip tms320f28232 --for 10ms */
#include "f28232.h"

/* ---- ePWM1, 0x6800 (TZ registers EALLOW protected) ---- */
struct EPWM_REGS {
    volatile unsigned int TBCTL;     /* +0x00 */
    volatile unsigned int TBSTS;     /* +0x01 */
    volatile unsigned int TBPHSHR;   /* +0x02 */
    volatile unsigned int TBPHS;     /* +0x03 */
    volatile unsigned int TBCTR;     /* +0x04 */
    volatile unsigned int TBPRD;     /* +0x05 */
    volatile unsigned int rsvd1;     /* +0x06 */
    volatile unsigned int CMPCTL;    /* +0x07 */
    volatile unsigned int CMPAHR;    /* +0x08 */
    volatile unsigned int CMPA;      /* +0x09 */
    volatile unsigned int CMPB;      /* +0x0A */
    volatile unsigned int AQCTLA;    /* +0x0B */
    volatile unsigned int AQCTLB;    /* +0x0C */
    volatile unsigned int AQSFRC;    /* +0x0D */
    volatile unsigned int AQCSFRC;   /* +0x0E */
    volatile unsigned int DBCTL;     /* +0x0F */
    volatile unsigned int DBRED;     /* +0x10 */
    volatile unsigned int DBFED;     /* +0x11 */
    volatile unsigned int TZSEL;     /* +0x12 */
    volatile unsigned int TZDCSEL;   /* +0x13 */
    volatile unsigned int TZCTL;     /* +0x14 */
    volatile unsigned int TZEINT;    /* +0x15 */
    volatile unsigned int TZFLG;     /* +0x16 */
    volatile unsigned int TZCLR;     /* +0x17 */
    volatile unsigned int TZFRC;     /* +0x18 */
    volatile unsigned int ETSEL;     /* +0x19 */
    volatile unsigned int ETPS;      /* +0x1A */
    volatile unsigned int ETFLG;     /* +0x1B */
    volatile unsigned int ETCLR;     /* +0x1C */
    volatile unsigned int ETFRC;     /* +0x1D */
};
#define EPwm1Regs (*(volatile struct EPWM_REGS *)0x6800)

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

/* ---- ADC, 0x7100 ---- */
struct ADC_REGS {
    volatile unsigned int ADCTRL1;       /* +0x00 */
    volatile unsigned int ADCTRL2;       /* +0x01 */
    volatile unsigned int ADCMAXCONV;    /* +0x02 */
    volatile unsigned int ADCCHSELSEQ1;  /* +0x03 */
    volatile unsigned int ADCCHSELSEQ2;  /* +0x04 */
    volatile unsigned int ADCCHSELSEQ3;  /* +0x05 */
    volatile unsigned int ADCCHSELSEQ4;  /* +0x06 */
    volatile unsigned int ADCASEQSR;     /* +0x07 */
    volatile unsigned int ADCRESULT[16]; /* +0x08 left-justified */
    volatile unsigned int ADCTRL3;       /* +0x18 */
    volatile unsigned int ADCST;         /* +0x19 */
};
#define AdcRegs (*(volatile struct ADC_REGS *)0x7100)
#define ADCTRL1_SEQ_CASC       0x0010u
#define ADCTRL2_EPWM_SOCA_SEQ1 0x0100u
#define ADCTRL2_INT_ENA_SEQ1   0x0800u
#define ADCTRL2_RST_SEQ1       0x4000u
#define ADCTRL3_POWER_UP       0x00E0u   /* ADCBGRFDN = 11, ADCPWDN = 1 */
#define ADCTRL3_ADCCLKPS_2     0x0002u   /* ADCCLKPS (bits 4:1) = 1 */
/* ADCCLK = HSPCLK / 2: 50 MHz (HISPCP reset value, SYSCLKOUT / 2) -> 25 MHz,
                                            the ADCCLK maximum of the F2823x / F2833x (SPRS439) */
#define ADCST_INT_SEQ1_CLR     0x0010u

#define PCLKCR0_TBCLKSYNC 0x0004u
#define PCLKCR0_ADCENCLK  0x0008u
#define PCLKCR1_EPWM1ENCLK 0x0001u
#define VEC_ADCINT    PIE_VECNUM(1, 6)
#define VEC_EPWM1_INT PIE_VECNUM(3, 1)
#define M_INT3 0x0004u

#define EPWM1_TBPRD 4999u                /* 20 kHz at TBCLK = 100 MHz */
#define EPWM1_CMPA  1250u                /* 25 % */

volatile unsigned long epwm_isr_count;
volatile unsigned long adc_isr_count;
volatile unsigned int adc_last;

__interrupt void epwm1_isr(void)
{
    epwm_isr_count++;
    EPwm1Regs.ETCLR = ET_INT;                           /* clear INT so the next event can interrupt */
    PieCtrlRegs.PIEACK = PIEACK_GROUP(3);
}

__interrupt void adc_isr(void)
{
    adc_last = AdcRegs.ADCRESULT[0] >> 4;
    adc_isr_count++;
    AdcRegs.ADCTRL2 |= ADCTRL2_RST_SEQ1;                /* next SOC converts CONV00 again */
    AdcRegs.ADCST = ADCST_INT_SEQ1_CLR;
    PieCtrlRegs.PIEACK = PIEACK_GROUP(1);
}

static void init_sysctrl(void)
{
    EALLOW;
    WDCR = WDCR_DISABLE;
    PLLSTS &= ~PLLSTS_DIVSEL_MSK;
    PLLCR = 10;
    while (!(PLLSTS & PLLSTS_PLLLOCKS)) { }
    PLLSTS |= PLLSTS_DIVSEL_2;                          /* 20 MHz x 10 / 2 = 100 MHz */
    PCLKCR0 |= PCLKCR0_ADCENCLK;
    PCLKCR1 |= PCLKCR1_EPWM1ENCLK;
    PIE_VECT[VEC_EPWM1_INT] = (unsigned long)epwm1_isr;
    PIE_VECT[VEC_ADCINT] = (unsigned long)adc_isr;
    EDIS;
}

static void init_epwm1_gpio(void)
{
    EALLOW;
    GpioCtrlRegs.GPAPUD |= 1ul;                         /* no pull-up on GPIO0 */
    GpioCtrlRegs.GPAMUX1 = (GpioCtrlRegs.GPAMUX1 & ~3ul) | 1ul;   /* GPIO0 = EPWM1A */
    EDIS;
}

static void init_epwm1(void)
{
    EPwm1Regs.TBCTL = TB_FREEZE | TB_SYNC_DISABLE;      /* HSPCLKDIV = CLKDIV = /1 */
    EPwm1Regs.TBPRD = EPWM1_TBPRD;
    EPwm1Regs.TBPHS = 0;
    EPwm1Regs.TBCTR = 0;
    EPwm1Regs.CMPCTL = 0;                               /* CMPA shadowed, loaded at CTR=ZERO */
    EPwm1Regs.CMPA = EPWM1_CMPA;
    EPwm1Regs.AQCTLA = AQ_ZRO_SET | AQ_CAU_CLEAR;
    EPwm1Regs.ETSEL = ET_INTSEL_ZRO | ET_INTEN | ET_SOCASEL_PRD | ET_SOCAEN;
    EPwm1Regs.ETPS = ET_INTPRD_1ST | ET_SOCAPRD_1ST;
    EPwm1Regs.TBCTL = TB_COUNT_UP | TB_SYNC_DISABLE;    /* counts once TBCLKSYNC is set */
}

static void init_adc(void)
{
    AdcRegs.ADCTRL3 = ADCTRL3_POWER_UP | ADCTRL3_ADCCLKPS_2;
    AdcRegs.ADCTRL1 = ADCTRL1_SEQ_CASC;                 /* cascaded, start/stop mode */
    AdcRegs.ADCMAXCONV = 0;                             /* one conversion */
    AdcRegs.ADCCHSELSEQ1 = 0;                           /* CONV00 = ADCINA0 */
    AdcRegs.ADCTRL2 = ADCTRL2_EPWM_SOCA_SEQ1 | ADCTRL2_INT_ENA_SEQ1;
}

static void init_pie(void)
{
    PieCtrlRegs.PIECTRL = 1;                            /* ENPIE */
    PIEIER(1) |= 1u << 5;                               /* INT1.6 ADCINT */
    PIEIER(3) |= 1u << 0;                               /* INT3.1 EPWM1_INT */
    PieCtrlRegs.PIEACK = 0xFFFF;
    IER |= M_INT1 | M_INT3;
}

void main(void)
{
    DINT;
    epwm_isr_count = adc_isr_count = 0;                 /* COFF ABI: .ebss is not zeroed */
    adc_last = 0;
    init_sysctrl();
    init_epwm1_gpio();
    EALLOW;
    PCLKCR0 &= ~PCLKCR0_TBCLKSYNC;                      /* stop the time bases while configuring */
    EDIS;
    init_epwm1();
    init_adc();
    EALLOW;
    PCLKCR0 |= PCLKCR0_TBCLKSYNC;                       /* start them together */
    EDIS;
    init_pie();
    EINT;
    for (;;) { }
}
