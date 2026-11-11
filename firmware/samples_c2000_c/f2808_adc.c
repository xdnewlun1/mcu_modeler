/* f2808_adc.c -- TMS320F2808 ePWM-triggered ADC with a C interrupt handler (TI compiler, cl2000).
 *   ePWM1 up-counts with TBPRD = 49999 at TBCLK = SYSCLKOUT = 100 MHz: SOCA every 500 us (ETSEL.SOCASEL = CTR=0).
 *   ADC: powered up (ADCTRL3 = 0x00E4: bandgap/reference, core, ADCCLK = HSPCLK 50 MHz / 4 = 12.5 MHz), 5 ms settling
 *   (TI InitAdc), cascaded SEQ1 with CONV00 = ADCINA0, CONV01 = ADCINA1, interrupt at the end of each sequence.
 *   seq1_isr (PIE 1.1) copies the results from the right-justified mirror, resets SEQ1, clears INT_SEQ1 and
 *   acknowledges PIE group 1.  Every 10th sample the main loop prints "ADC <n> <a0> <a1>\r\n" on SCI-A (GPIO29,
 *   115200 8N1, LSPCLK 25 MHz, BRR 26), i.e. one line every 5 ms.
 * Drive the inputs with: chipem run build/f2808_adc.out --chip tms320f2808 --adc ADCINA0=1234 --adc ADCINA1=4000 */
#include "f2808.h"

volatile unsigned int adc_a0, adc_a1;
volatile unsigned long adc_count;

interrupt void seq1_isr(void)
{
    adc_a0 = ADCRESULT_M(0);
    adc_a1 = ADCRESULT_M(1);
    adc_count++;
    ADCTRL2 |= 0x4000u;                         /* RST_SEQ1 */
    ADCST = 0x0010u;                            /* INT_SEQ1_CLR */
    PieCtrlRegs.PIEACK = PIEACK_GROUP(1);
}

static void putc_(char c)
{
    while (!(SciaRegs.SCICTL2 & SCICTL2_TXRDY)) { }
    SciaRegs.SCITXBUF = (unsigned int)c & 0xFFu;
}

static void putdec(unsigned long v)
{
    char buf[11];
    int n = 0;
    do { buf[n++] = (char)('0' + (unsigned int)(v % 10)); v /= 10; } while (v);
    while (n)
        putc_(buf[--n]);
}

static void delay_cycles(unsigned long n)       /* CPU timer 0, polled */
{
    CpuTimer0Regs.TCR = TCR_TSS;
    CpuTimer0Regs.PRD = n - 1;
    CpuTimer0Regs.TPR = 0;
    CpuTimer0Regs.TPRH = 0;
    CpuTimer0Regs.TCR = TCR_TIF | TCR_TRB;
    while (!(CpuTimer0Regs.TCR & TCR_TIF)) { }
    CpuTimer0Regs.TCR = TCR_TSS | TCR_TIF;
}

void main(void)
{
    unsigned long printed = 0, n;
    DINT;
    adc_count = 0;
    EALLOW;
    WDCR = WDCR_DISABLE;
    PLLSTS &= ~PLLSTS_CLKINDIV;
    PLLCR = 10;
    while (!(PLLSTS & PLLSTS_PLLLOCKS)) { }     /* 100 MHz */
    PCLKCR0 = (PCLKCR0 & ~PCLKCR0_TBCLKSYNC) | PCLKCR0_ADCENCLK | PCLKCR0_SCIAENCLK;
    PCLKCR1 |= PCLKCR1_EPWM1ENCLK;
    GpioCtrlRegs.GPAMUX2 |= (1ul << 24) | (1ul << 26);   /* GPIO28 SCIRXDA, GPIO29 SCITXDA */
    PIE_VECT[PIE_VECNUM(1, 1)] = (unsigned long)seq1_isr;
    EDIS;

    SciaRegs.SCICCR = 0x0007;
    SciaRegs.SCICTL1 = SCICTL1_TXENA;
    SciaRegs.SCIHBAUD = 0;
    SciaRegs.SCILBAUD = 26;
    SciaRegs.SCICTL1 = SCICTL1_TXENA | SCICTL1_SWRESET;

    ADCTRL3 = 0x00E4u;                          /* ADCBGRFDN = 11, ADCPWDN = 1, ADCCLKPS = 2 */
    delay_cycles(500000ul);                     /* 5 ms */
    ADCTRL1 = 0x0110u;                          /* ACQ_PS = 1, SEQ_CASC = 1 */
    ADCMAXCONV = 1;
    ADCCHSELSEQ1 = 0x0010u;                     /* A0, A1 */
    ADCTRL2 = 0x0900u;                          /* EPWM_SOCA_SEQ1 | INT_ENA_SEQ1 */

    TBPRD1 = 49999;
    ETSEL1 = 0x0900u;                           /* SOCAEN, SOCASEL = CTR=0 */
    ETPS1 = 0x0100u;                            /* SOCAPRD = 1 */
    TBCTL1 = 0x0030u;                           /* up count, TBCLK = SYSCLKOUT */

    PieCtrlRegs.PIECTRL = 1;
    PIEIER(1) |= 0x0001u;
    PieCtrlRegs.PIEACK = 0xFFFFu;
    IER |= M_INT1;
    EALLOW;
    PCLKCR0 |= PCLKCR0_TBCLKSYNC;               /* start ePWM1 */
    EDIS;
    EINT;
    for (;;) {
        n = adc_count;
        if (n >= printed + 10) {
            printed = n - n % 10;
            putc_('A'); putc_('D'); putc_('C'); putc_(' ');
            putdec(n); putc_(' ');
            putdec(adc_a0); putc_(' ');
            putdec(adc_a1);
            putc_('\r'); putc_('\n');
        }
    }
}
