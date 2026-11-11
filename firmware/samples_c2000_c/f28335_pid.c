/* f28335_pid.c -- TMS320F28335 float PI control loop (FPU32), ADC-driven, with float work in main at the same time.
 *
 *   150 MHz SYSCLKOUT (30 MHz OSCCLK, PLLCR = 10, DIVSEL = /2), HSPCLK 25 MHz, ADCCLK 12.5 MHz, LSPCLK 37.5 MHz.
 *   ePWM1: up count, TBPRD = 7499 -> 20 kHz on GPIO0 (EPWM1A, set at ZERO, cleared at CMPA); SOCA at PRD starts
 *   ADC SEQ1 (ADCINA0).  SEQ1INT (PIE 1.1) -> `__interrupt adc_isr`: y = result * 3.0 V / 4095, an 8-sample average
 *   (dot product with a run-time length: RPTB inside the ISR), PI controller e = 1.2 V - y, u = 0.8 e + integral
 *   (Ki = 400 / s, Ts = 50 us, anti-windup clamp 0..0.95), CMPA = u * TBPRD.  The compiler saves the FPU registers
 *   (R0H-R3H, STF, RB) the ISR uses, so the interrupted float code in main must not see any change.
 *   main: an endless check loop of float work (a 24-element RPTB dot product and a Horner polynomial) compared with
 *   the first pass: `chk_bad` counts differences (0 expected), `chk_pass` the passes; every 40 ISRs (2 ms) it prints
 *   "PI <isr count> y=<mV> u=<permille> bad=<chk_bad>" on SCI-A (GPIO29, 115200 8N1).
 * Board: boards/f28335_fpu_loop.yaml (an RC plant on GPIO0 feeding ADCINA0, a script part).
 * Run: chipem run --board boards/f28335_fpu_loop.yaml --for 20ms */
#include "f28335.h"

#define PWM_PERIOD 7499u
#define NAVG 8

volatile float setpoint = 1.2f, kp = 0.8f, ki_ts = 400.0f * 5.0e-5f;
volatile float y_avg, u_out, integ;
volatile unsigned long isr_count;
volatile unsigned long chk_pass, chk_bad;
volatile int navg = NAVG, nmain = 24;

float hist[NAVG];
float wavg[NAVG];
float xs[24], ys[24];
const float poly[6] = {0.5f, -1.25f, 2.0f, 0.375f, -3.0f, 1.0f};

float dot(const float *a, const float *b, int k)
{
    float s = 0.0f;
    int i;
    for (i = 0; i < k; i++)
        s += a[i] * b[i];
    return s;
}

__interrupt void adc_isr(void)
{
    float y, e, u, in;
    y = (float)(AdcRegs.ADCRESULT[0] >> 4) * (3.0f / 4095.0f);
    hist[(unsigned int)isr_count & (NAVG - 1)] = y;
    y = dot(hist, wavg, navg);
    e = setpoint - y;
    in = integ + ki_ts * e;
    if (in < 0.0f) in = 0.0f;
    if (in > 0.95f) in = 0.95f;
    integ = in;
    u = kp * e + in;
    if (u < 0.0f) u = 0.0f;
    if (u > 0.95f) u = 0.95f;
    EPwm1Regs.CMPA = (unsigned int)(u * (float)PWM_PERIOD);
    y_avg = y;
    u_out = u;
    isr_count++;
    AdcRegs.ADCTRL2 |= ADCTRL2_RST_SEQ1;
    AdcRegs.ADCST = ADCST_INT_SEQ1_CLR;
    PieCtrlRegs.PIEACK = PIEACK_GROUP(1);
}

static void putc_a(char c)
{
    while (!(SciaRegs.SCICTL2 & SCICTL2_TXRDY)) { }
    SciaRegs.SCITXBUF = (unsigned int)c & 0xFFu;
}

static void puts_a(const char *s) { while (*s) putc_a(*s++); }

static void putu(unsigned long v)
{
    char b[11];
    int i = 0;
    do { b[i++] = (char)('0' + v % 10u); v /= 10u; } while (v);
    while (i) putc_a(b[--i]);
}

static float check_pass(void)
{
    float a = dot(xs, ys, nmain), acc = poly[0];
    int i;
    for (i = 1; i < 6; i++)
        acc = acc * a + poly[i];
    return acc * 0.001f + a;
}

static void init_sysctrl(void)
{
    EALLOW;
    WDCR = WDCR_DISABLE;
    PLLSTS &= ~PLLSTS_DIVSEL_MSK;
    PLLCR = 10;
    while (!(PLLSTS & PLLSTS_PLLLOCKS)) { }
    PLLSTS |= PLLSTS_DIVSEL_2;                          /* 30 MHz x 10 / 2 = 150 MHz */
    HISPCP = 3;                                         /* HSPCLK = 150 / 6 = 25 MHz */
    PCLKCR0 |= PCLKCR0_ADCENCLK | PCLKCR0_SCIAENCLK;
    PCLKCR1 |= PCLKCR1_EPWM1ENCLK;
    GpioCtrlRegs.GPAPUD |= 1ul;                         /* no pull-up on GPIO0 */
    GpioCtrlRegs.GPAMUX1 = (GpioCtrlRegs.GPAMUX1 & ~3ul) | 1ul;      /* GPIO0 = EPWM1A */
    GpioCtrlRegs.GPAMUX2 |= (1ul << 24) | (1ul << 26);  /* GPIO28 = SCIRXDA, GPIO29 = SCITXDA */
    PIE_VECT[VEC_SEQ1INT] = (unsigned long)adc_isr;
    EDIS;
}

void main(void)
{
    int i;
    float ref;
    unsigned long next = 40;

    DINT;
    isr_count = chk_pass = chk_bad = 0;                 /* COFF ABI: .ebss is not zeroed */
    integ = 0.0f;
    for (i = 0; i < NAVG; i++) {
        hist[i] = 0.0f;
        wavg[i] = 1.0f / NAVG;
    }
    for (i = 0; i < 24; i++) {
        xs[i] = 0.01f * (float)(i + 1);
        ys[i] = 1.0f / (float)(i + 3);
    }
    init_sysctrl();

    SciaRegs.SCICCR = 0x0007;                           /* 8N1 */
    SciaRegs.SCICTL1 = SCICTL1_TXENA;
    SciaRegs.SCIHBAUD = 0;
    SciaRegs.SCILBAUD = 40;                             /* 37.5 MHz / (8 x 41) = 114329 baud */
    SciaRegs.SCICTL1 = SCICTL1_TXENA | SCICTL1_SWRESET;

    AdcRegs.ADCTRL3 = ADCTRL3_POWER_UP | 0x0002u;       /* ADCCLKPS = 1: ADCCLK = 25 / 2 = 12.5 MHz */
    AdcRegs.ADCTRL1 = ADCTRL1_SEQ_CASC;
    AdcRegs.ADCMAXCONV = 0;
    AdcRegs.ADCCHSELSEQ1 = 0;                           /* CONV00 = ADCINA0 */
    AdcRegs.ADCTRL2 = ADCTRL2_EPWM_SOCA_SEQ1 | ADCTRL2_INT_ENA_SEQ1;

    EALLOW;
    PCLKCR0 &= ~PCLKCR0_TBCLKSYNC;
    EDIS;
    EPwm1Regs.TBCTL = TB_FREEZE | TB_SYNC_DISABLE;
    EPwm1Regs.TBPRD = PWM_PERIOD;
    EPwm1Regs.TBPHS = 0;
    EPwm1Regs.TBCTR = 0;
    EPwm1Regs.CMPCTL = 0;
    EPwm1Regs.CMPA = 0;
    EPwm1Regs.AQCTLA = AQ_ZRO_SET | AQ_CAU_CLEAR;
    EPwm1Regs.ETSEL = ET_SOCASEL_PRD | ET_SOCAEN;
    EPwm1Regs.ETPS = ET_SOCAPRD_1ST;
    EPwm1Regs.TBCTL = TB_COUNT_UP | TB_SYNC_DISABLE;

    PieCtrlRegs.PIECTRL = 1;
    PIEIER(1) |= 1u << 0;                               /* INT1.1 SEQ1INT */
    PieCtrlRegs.PIEACK = 0xFFFF;
    IER |= M_INT1;
    EALLOW;
    PCLKCR0 |= PCLKCR0_TBCLKSYNC;
    EDIS;
    EINT;

    puts_a("F28335 PI\r\n");
    ref = check_pass();
    for (;;) {
        if (check_pass() != ref)
            chk_bad++;
        chk_pass++;
        if (isr_count >= next) {
            next += 40;
            puts_a("PI ");
            putu(isr_count);
            puts_a(" y=");
            putu((unsigned long)(y_avg * 1000.0f + 0.5f));
            puts_a(" u=");
            putu((unsigned long)(u_out * 1000.0f + 0.5f));
            puts_a(" bad=");
            putu(chk_bad);
            puts_a("\r\n");
        }
    }
}
