/* adc_dma.c -- TMS320F28232 ePWM1 SOCA -> ADC SEQ1 (4 channels) -> DMA CH1 ping-pong buffer, in TI cl2000 C:
 *   100 MHz SYSCLKOUT (PLL x10 /2).  ePWM1 up count, TBPRD = 9999 -> SOCA at CTR=PRD every 100 us (10 kHz).
 *   ADC dual-sequencer mode, start/stop: each SOCA converts SEQ1 MAX_CONV1 = 3 -> 4 states; CONV00-03 and CONV04-07
 *   both select ADCINA0-A3, so successive sequences fill RESULT0-3 and RESULT4-7 (the 8-state SEQ1 wraps).  The
 *   SEQ1 interrupt (INT_ENA_SEQ1; not enabled in the PIE) is the DMA trigger.
 *   DMA CH1 (SPRUI07 ch. 8): PERINTSEL = SEQ1INT, 16-bit, burst = 4 words from the ADC result registers in PF0
 *   (0x0B00) with SRC_BURST_STEP 1 and SRC_TRANSFER_STEP 1, source wrap after 2 bursts back to 0x0B00 (follows the
 *   ADC's RESULT0-3 / RESULT4-7 alternation); destination steps 1 through a 32-word buffer in L4 SARAM;
 *   TRANSFER_SIZE = 8 bursts; CONTINUOUS with the channel interrupt at the beginning of each transfer (CHINTMODE = 0):
 *   dma_isr (DINTCH1, PIE 7.1) counts `dma_isr_count` and points the destination shadow at the other buffer, so the
 *   buffer filled by the previous transfer (`adc_buf[ready_buf]`) is complete.  A buffer fills in 800 us.
 *   main prints "ADC DMA" and then, for each completed buffer it gets to (SCI-A 115200 8N1 on GPIO29), a line
 *   "DMA <transfer> BUF<0|1> <A0> <A1> <A2> <A3> SUM <sum of the 32 words>" in hex, from a copy taken at once.
 * Run: chipem run firmware/samples_c2000_c/build/adc_dma.out --chip tms320f28232 --for 20ms --adc ADCINA0=0x400 */
#include "f28232.h"

/* ---- ePWM1, 0x6800 ---- */
#define EPWM1_TBCTL  REG16(0x6800)
#define EPWM1_TBPHS  REG16(0x6803)
#define EPWM1_TBCTR  REG16(0x6804)
#define EPWM1_TBPRD  REG16(0x6805)
#define EPWM1_ETSEL  REG16(0x6819)
#define EPWM1_ETPS   REG16(0x681A)
#define TB_COUNT_UP     0x0000u
#define TB_FREEZE       0x0003u
#define TB_SYNC_DISABLE 0x0030u
#define ET_SOCASEL_PRD  0x0200u
#define ET_SOCAEN       0x0800u
#define ET_SOCAPRD_1ST  0x0100u

/* ---- ADC, 0x7100 ---- */
#define ADCTRL1      REG16(0x7100)
#define ADCTRL2      REG16(0x7101)
#define ADCMAXCONV   REG16(0x7102)
#define ADCCHSELSEQ1 REG16(0x7103)
#define ADCCHSELSEQ2 REG16(0x7104)
#define ADCTRL3      REG16(0x7118)
#define ADCTRL2_EPWM_SOCA_SEQ1 0x0100u
#define ADCTRL2_INT_ENA_SEQ1   0x0800u
#define ADCTRL2_RST_SEQ1       0x4000u
#define ADCTRL3_POWER_UP       0x00E0u   /* ADCBGRFDN = 11, ADCPWDN = 1 */
#define ADCTRL3_ADCCLKPS_2     0x0002u   /* ADCCLKPS (bits 4:1) = 1: ADCCLK = HSPCLK / 2 = 25 MHz, the part's maximum */
#define ADC_RESULT_PF0         0x0B00ul  /* right-justified results, on the DMA bus */

/* ---- DMA, 0x1000 (all registers EALLOW protected); channel n at 0x1000 + 0x20 n (TI DSP2833x_DMA.h) ---- */
struct DMA_CH_REGS {
    volatile unsigned int MODE;               /* +0x00 */
    volatile unsigned int CONTROL;            /* +0x01 */
    volatile unsigned int BURST_SIZE;         /* +0x02 */
    volatile unsigned int BURST_COUNT;        /* +0x03 */
    volatile int SRC_BURST_STEP;              /* +0x04 */
    volatile int DST_BURST_STEP;              /* +0x05 */
    volatile unsigned int TRANSFER_SIZE;      /* +0x06 */
    volatile unsigned int TRANSFER_COUNT;     /* +0x07 */
    volatile int SRC_TRANSFER_STEP;           /* +0x08 */
    volatile int DST_TRANSFER_STEP;           /* +0x09 */
    volatile unsigned int SRC_WRAP_SIZE;      /* +0x0A */
    volatile unsigned int SRC_WRAP_COUNT;     /* +0x0B */
    volatile int SRC_WRAP_STEP;               /* +0x0C */
    volatile unsigned int DST_WRAP_SIZE;      /* +0x0D */
    volatile unsigned int DST_WRAP_COUNT;     /* +0x0E */
    volatile int DST_WRAP_STEP;               /* +0x0F */
    volatile unsigned long SRC_BEG_ADDR_SHADOW;   /* +0x10 */
    volatile unsigned long SRC_ADDR_SHADOW;       /* +0x12 */
    volatile unsigned long SRC_BEG_ADDR_ACTIVE;   /* +0x14 */
    volatile unsigned long SRC_ADDR_ACTIVE;       /* +0x16 */
    volatile unsigned long DST_BEG_ADDR_SHADOW;   /* +0x18 */
    volatile unsigned long DST_ADDR_SHADOW;       /* +0x1A */
    volatile unsigned long DST_BEG_ADDR_ACTIVE;   /* +0x1C */
    volatile unsigned long DST_ADDR_ACTIVE;       /* +0x1E */
};
#define DMACTRL       REG16(0x1000)
#define PRIORITYCTRL1 REG16(0x1004)
#define DmaCh1 (*(volatile struct DMA_CH_REGS *)0x1020)
#define DMACTRL_HARDRESET   0x0001u
#define MODE_PERINTSEL_SEQ1 0x0001u
#define MODE_PERINTE        0x0100u
#define MODE_CONTINUOUS     0x0800u
#define MODE_CHINTE         0x8000u
#define CTRL_RUN            0x0001u
#define CTRL_PERINTCLR      0x0010u
#define CTRL_SYNCCLR        0x0040u
#define CTRL_ERRCLR         0x0080u

#define PCLKCR0_ADCENCLK   0x0008u
#define PCLKCR0_TBCLKSYNC  0x0004u
#define PCLKCR1_EPWM1ENCLK 0x0001u
#define PCLKCR3_DMAENCLK   0x0800u
#define VEC_DINTCH1 PIE_VECNUM(7, 1)
#define M_INT7 0x0040u

#define SETS    8u                       /* sample sets (bursts) per transfer */
#define NCH     4u
#define BUFLEN  (SETS * NCH)
#define EPWM1_PERIOD 9999u               /* 100 us at TBCLK = 100 MHz */

volatile unsigned int adc_buf[2][BUFLEN];  /* .ebss / .bss: L4 SARAM (on the DMA bus) */
volatile unsigned long dma_isr_count;    /* transfers started */
volatile unsigned int ready_buf;         /* buffer completed by the previous transfer */
volatile unsigned long ready_seq;        /* number of that transfer (0: none yet) */
volatile unsigned int lines_printed;

__interrupt void dma_isr(void)
{
    unsigned long n = ++dma_isr_count;          /* transfer n has just started on adc_buf[(n - 1) & 1] */
    unsigned int filling = (unsigned int)((n - 1u) & 1u);
    EALLOW;
    DmaCh1.DST_BEG_ADDR_SHADOW = (unsigned long)&adc_buf[filling ^ 1u][0];   /* transfer n + 1 */
    DmaCh1.DST_ADDR_SHADOW = (unsigned long)&adc_buf[filling ^ 1u][0];
    EDIS;
    if (n > 1u) {
        ready_buf = filling ^ 1u;
        ready_seq = n - 1u;
    }
    PieCtrlRegs.PIEACK = PIEACK_GROUP(7);
}

static void putc_(char c)
{
    while (!(SciaRegs.SCICTL2 & SCICTL2_TXRDY)) { }
    SciaRegs.SCITXBUF = (unsigned int)c & 0xFFu;
}

static void puts_(const char *s)
{
    while (*s)
        putc_(*s++);
}

static void put_hex(unsigned long v, unsigned int digits)
{
    while (digits--)
        putc_("0123456789ABCDEF"[(v >> (4u * digits)) & 0xFu]);
}

static void put_dec(unsigned long v)
{
    char buf[11];
    unsigned int n = 0;
    do {
        buf[n++] = (char)('0' + v % 10u);
        v /= 10u;
    } while (v);
    while (n)
        putc_(buf[--n]);
}

static void init_sysctrl(void)
{
    EALLOW;
    WDCR = WDCR_DISABLE;
    PCLKCR0 |= PCLKCR0_SCIAENCLK | PCLKCR0_ADCENCLK;
    PCLKCR1 |= PCLKCR1_EPWM1ENCLK;
    PCLKCR3 |= PCLKCR3_DMAENCLK;
    GpioCtrlRegs.GPAMUX2 |= (1ul << 24) | (1ul << 26);  /* GPIO28 -> SCIRXDA, GPIO29 -> SCITXDA */
    PLLSTS &= ~PLLSTS_DIVSEL_MSK;
    PLLCR = 10;
    while (!(PLLSTS & PLLSTS_PLLLOCKS)) { }
    PLLSTS |= PLLSTS_DIVSEL_2;                          /* 20 MHz x 10 / 2 = 100 MHz */
    PIE_VECT[VEC_DINTCH1] = (unsigned long)dma_isr;
    EDIS;
}

static void init_scia(void)
{
    SciaRegs.SCICCR = 0x0007;                           /* 8N1 */
    SciaRegs.SCICTL1 = SCICTL1_RXENA | SCICTL1_TXENA;
    SciaRegs.SCICTL2 = 0;
    SciaRegs.SCIHBAUD = 0;
    SciaRegs.SCILBAUD = 26;                             /* LSPCLK 25 MHz -> 115741 baud */
    SciaRegs.SCICTL1 = SCICTL1_RXENA | SCICTL1_TXENA | SCICTL1_SWRESET;
}

static void init_epwm1(void)
{
    EPWM1_TBCTL = TB_FREEZE | TB_SYNC_DISABLE;
    EPWM1_TBPRD = EPWM1_PERIOD;
    EPWM1_TBPHS = 0;
    EPWM1_TBCTR = 0;
    EPWM1_ETSEL = ET_SOCASEL_PRD | ET_SOCAEN;
    EPWM1_ETPS = ET_SOCAPRD_1ST;
    EPWM1_TBCTL = TB_COUNT_UP | TB_SYNC_DISABLE;
}

static void init_adc(void)
{
    ADCTRL3 = ADCTRL3_POWER_UP | ADCTRL3_ADCCLKPS_2;
    ADCTRL1 = 0;                                        /* dual sequencers, start/stop mode */
    ADCMAXCONV = 3;                                     /* SEQ1: 4 conversions */
    ADCCHSELSEQ1 = 0x3210;                              /* CONV00-03 = ADCINA0-A3 */
    ADCCHSELSEQ2 = 0x3210;                              /* CONV04-07 = ADCINA0-A3 */
    ADCTRL2 = ADCTRL2_RST_SEQ1;
    ADCTRL2 = ADCTRL2_EPWM_SOCA_SEQ1 | ADCTRL2_INT_ENA_SEQ1;
}

static void init_dma(void)
{
    EALLOW;
    DMACTRL = DMACTRL_HARDRESET;
    asm(" NOP");                                        /* one cycle before the next DMA access */
    PRIORITYCTRL1 = 0;
    DmaCh1.BURST_SIZE = NCH - 1u;                       /* 4 words per burst */
    DmaCh1.SRC_BURST_STEP = 1;
    DmaCh1.DST_BURST_STEP = 1;
    DmaCh1.TRANSFER_SIZE = SETS - 1u;                   /* 8 bursts per transfer */
    DmaCh1.SRC_TRANSFER_STEP = 1;                       /* RESULT3 -> RESULT4 */
    DmaCh1.DST_TRANSFER_STEP = 1;
    DmaCh1.SRC_WRAP_SIZE = 1;                           /* after 2 bursts back to RESULT0 */
    DmaCh1.SRC_WRAP_STEP = 0;
    DmaCh1.DST_WRAP_SIZE = 0xFFFF;                      /* no destination wrap */
    DmaCh1.DST_WRAP_STEP = 0;
    DmaCh1.SRC_BEG_ADDR_SHADOW = ADC_RESULT_PF0;
    DmaCh1.SRC_ADDR_SHADOW = ADC_RESULT_PF0;
    DmaCh1.DST_BEG_ADDR_SHADOW = (unsigned long)&adc_buf[0][0];
    DmaCh1.DST_ADDR_SHADOW = (unsigned long)&adc_buf[0][0];
    DmaCh1.MODE = MODE_CHINTE | MODE_CONTINUOUS | MODE_PERINTE | MODE_PERINTSEL_SEQ1;   /* CHINTMODE = 0 */
    DmaCh1.CONTROL = CTRL_PERINTCLR | CTRL_SYNCCLR | CTRL_ERRCLR;
    DmaCh1.CONTROL = CTRL_RUN;
    EDIS;
}

static void init_pie(void)
{
    PieCtrlRegs.PIECTRL = 1;                            /* ENPIE */
    PIEIER(7) |= 1u << 0;                               /* INT7.1 DINTCH1 */
    PieCtrlRegs.PIEACK = 0xFFFF;
    IER |= M_INT7;
}

void main(void)
{
    unsigned long printed = 0;
    unsigned int copy[BUFLEN];
    unsigned int i, k;

    DINT;
    dma_isr_count = ready_seq = 0;                      /* COFF ABI: .ebss is not zeroed */
    ready_buf = lines_printed = 0;
    for (k = 0; k < 2u; k++)
        for (i = 0; i < BUFLEN; i++)
            adc_buf[k][i] = 0;
    init_sysctrl();
    init_scia();
    init_pie();
    EINT;
    puts_("ADC DMA\r\n");
    EALLOW;
    PCLKCR0 &= ~PCLKCR0_TBCLKSYNC;
    EDIS;
    init_epwm1();
    init_adc();
    init_dma();
    EALLOW;
    PCLKCR0 |= PCLKCR0_TBCLKSYNC;                       /* ePWM1 starts: SOCA every 100 us */
    EDIS;
    for (;;) {
        unsigned long seq;
        unsigned int b;
        unsigned long sum = 0;
        DINT;
        seq = ready_seq;
        b = ready_buf;
        if (seq != printed)
            for (i = 0; i < BUFLEN; i++)                 /* copy before the DMA comes back to it (800 us) */
                copy[i] = adc_buf[b][i];
        EINT;
        if (seq == printed)
            continue;
        printed = seq;
        for (i = 0; i < BUFLEN; i++)
            sum += copy[i];
        puts_("DMA ");
        put_dec(seq);
        puts_(" BUF");
        putc_((char)('0' + b));
        for (i = 0; i < NCH; i++) {
            putc_(' ');
            put_hex(copy[i], 3);
        }
        puts_(" SUM ");
        put_hex(sum, 5);
        puts_("\r\n");
        lines_printed++;
    }
}
