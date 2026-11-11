/* f28027_launchpad.c -- TMS320F28027 (LAUNCHXL-F28027) demo in C (TI compiler, cl2000), register definitions in
 * piccolo.h (written for this repository).
 *   init_sysctrl(): the clock setup of TI's InitSysCtrl in order (SPRUI09A 1.3.2, Figure 1-23): watchdog off,
 *     Device_cal() through its fixed pointer 0x3D7C80 with the ADC clock on, INTOSC1 as OSCCLK (CLKCTL), DIVSEL = 0,
 *     MCLKOFF while locking, PLLCR = 12, wait PLLLOCKS, MCLKOFF = 0, DIVSEL = 2 -> 60 MHz; LSPCLK = SYSCLKOUT (LOSPCP = 0).
 *   CPU timer 0 interrupt (PIE 1.7) every 1 ms: the four LaunchPad LEDs GPIO0-3 count milliseconds / 64.
 *   The internal temperature sensor (ADCCTL1.TEMPCONV, ADCINA5) converted by a software SOC0, converted to deg C
 *     with the TI-reserved OTP getters getTempSlope() / getTempOffset() (SPRUI09 6.12.1).
 *   SCI-A console on the LaunchPad backchannel (GPIO28 RX / GPIO29 TX, 115200 8N1 from LSPCLK 60 MHz, BRR 64
 *     = 115385 baud):
 *     "F28027 60MHz T=<deg>C\r\n", then echoes every received byte; the SCI FIFOs (4 words on the
 *     Piccolo, SPRUI09 9.1) take the bytes that arrive while the banner is still going out.
 * Run: chipem run firmware/samples_c2000_c/build/f28027_launchpad.out --chip tms320f28027 --for 20ms */
#include "piccolo.h"

volatile unsigned int ms_ticks, temp_raw;
volatile int temp_c;

__interrupt void timer0_isr(void)
{
    ms_ticks++;
    unsigned int v = (ms_ticks >> 6) & 0xFu;
    GpioDataRegs.GPASET = v;                            /* SET/CLEAR: no read-modify-write of GPADAT */
    GpioDataRegs.GPACLEAR = ~v & 0xFu;
    PieCtrlRegs.PIEACK = PIEACK_GROUP(1);
}

static void init_sysctrl(void)
{
    EALLOW;
    WDCR = WDCR_DISABLE;
    PCLKCR0 |= PCLKCR0_ADCENCLK;
    (*Device_cal)();
    EALLOW;                                          /* Device_cal ends with EDIS */
    PCLKCR0 &= ~PCLKCR0_ADCENCLK;
    CLKCTL = 0x6400;                                 /* INTOSC1; INTOSC2, crystal, XCLKIN off */
    if (!(PLLSTS & PLLSTS_MCLKSTS)) {
        PLLSTS &= ~PLLSTS_DIVSEL;                    /* /4 before changing PLLCR */
        PLLSTS |= PLLSTS_MCLKOFF;
        PLLCR = 12;
        while (!(PLLSTS & PLLSTS_PLLLOCKS)) { }
        PLLSTS &= ~PLLSTS_MCLKOFF;
        PLLSTS = (PLLSTS & ~PLLSTS_DIVSEL) | 0x0100u;  /* DIVSEL = 2: 10 MHz x 12 / 2 = 60 MHz */
    }
    EDIS;
}

static void putc_(char c)
{
    while (((SciaRegs.SCIFFTX >> 8) & 0x1Fu) >= 4u) { }   /* TXFFST: wait for room in the 4-word TX FIFO */
    SciaRegs.SCITXBUF = (unsigned int)c & 0xFFu;
}

static void puts_(const char *s)
{
    while (*s)
        putc_(*s++);
}

static void putdec(int v)
{
    char buf[7];
    int n = 0;
    if (v < 0) { putc_('-'); v = -v; }
    do { buf[n++] = (char)('0' + v % 10); v /= 10; } while (v && n < 6);
    while (n)
        putc_(buf[--n]);
}

void main(void)
{
    DINT;
    init_sysctrl();
    EALLOW;
    LOSPCP = 0;                                      /* LSPCLK = 60 MHz */
    PCLKCR0 |= PCLKCR0_SCIAENCLK | PCLKCR0_ADCENCLK;
    GpioCtrlRegs.GPAMUX1 &= ~0xFFul;                 /* GPIO0-3 = GPIO */
    GpioDataRegs.GPACLEAR = 0xFul;
    GpioCtrlRegs.GPADIR |= 0xFul;
    GpioCtrlRegs.GPAMUX2 |= (1ul << 24) | (1ul << 26);   /* GPIO28 SCIRXDA, GPIO29 SCITXDA */
    GpioCtrlRegs.GPAPUD &= ~((1ul << 28) | (1ul << 29));
    PIE_VECT[PIE_VECNUM(1, 7)] = (unsigned long)timer0_isr;
    ADCCTL1 = ADCCTL1_POWERUP | ADCCTL1_TEMPCONV | 0x0004u;  /* temperature sensor on ADCINA5; INTPULSEPOS = 1:
                                                        ADCINT1 just before the result latches, not at the
                                                        start of the conversion */
    ADCSOCCTL(0) = (5u << 6) | 6u;                    /* SOC0: software trigger, CHSEL 5, ACQPS 6 */
    INTSEL1N2 = 0x0020u;                             /* ADCINT1 = EOC0, enabled (polled, PIE 1.1 off) */
    EDIS;
    SciaRegs.SCICCR = 0x0007;
    SciaRegs.SCICTL1 = SCICTL1_TXENA | SCICTL1_RXENA;
    SciaRegs.SCIHBAUD = 0;
    SciaRegs.SCILBAUD = 64;                          /* 60 MHz / (65 * 8) = 115385 baud */
    SciaRegs.SCIFFTX = 0xE040u;                      /* SCIRST, SCIFFENA, TX FIFO out of reset */
    SciaRegs.SCIFFRX = 0x2044u;                      /* RX FIFO out of reset (4 words deep on the Piccolo) */
    SciaRegs.SCICTL1 = SCICTL1_TXENA | SCICTL1_RXENA | SCICTL1_SWRESET;

    CpuTimer0Regs.TCR = TCR_TSS;
    CpuTimer0Regs.PRD = 60000ul - 1;                 /* 1 ms at 60 MHz */
    CpuTimer0Regs.TPR = 0;
    CpuTimer0Regs.TPRH = 0;
    CpuTimer0Regs.TCR = TCR_TRB | TCR_TIE;
    PieCtrlRegs.PIECTRL = 1;
    PIEIER(1) |= 1u << 6;                            /* INT1.7 TINT0 */
    IER |= 0x0001;
    EINT;

    while (ms_ticks < 1) { }                         /* the ADC needs 1 ms after power-up (SPRUI09 6.8) */
    ADCINTFLGCLR = 1;
    ADCSOCFRC1 = 1;
    while (!(ADCINTFLG & 1)) { }
    ADCINTFLGCLR = 1;
    temp_raw = ADCRESULT(0);
    temp_c = (int)(((long)((int)temp_raw - getTempOffset()) * getTempSlope()) >> 15);
    puts_("F28027 60MHz T=");
    putdec(temp_c);
    puts_("C\r\n");
    for (;;) {
        while ((SciaRegs.SCIFFRX >> 8) & 0x1Fu)    /* RXFFST: words in the 4-deep RX FIFO */
            putc_((char)(SciaRegs.SCIRXBUF & 0xFFu));
    }
}
