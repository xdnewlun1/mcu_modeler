/* pca_pwm.c - PCA module 0: 8-bit PWM on CEX0 (P1.3), 25 % high (CCAP0H = C0h); module 1: capture of both edges on
 * CEX1 (P1.4) in the PCA interrupt (__interrupt(PCA_VECTOR) = 0033h), first 8 captures kept in `caps`.
 * PCA clock fosc/12: 11.0592 MHz / 12 / 256 = 3600 Hz.  boards/p89cv51_pca_loopback_c.yaml wires P1.3 to P1.4. */
#include "p89cv51.h"

volatile unsigned char ncap;
__idata volatile unsigned int caps[8];

void pca_isr(void) __interrupt(PCA_VECTOR) __using(1)
{
    if (CCF1) {
        CCF1 = 0;
        if (ncap < 8)
            caps[ncap++] = (CCAP1H << 8) | CCAP1L;
    }
}

void main(void)
{
    CMOD = 0x00;                         /* fosc / 12, no overflow interrupt */
    CCAP0L = 0xC0;
    CCAP0H = 0xC0;
    CCAPM0 = CCAPM_ECOM | CCAPM_PWM;
    CCAPM1 = CCAPM_CAPP | CCAPM_CAPN | CCAPM_ECCF;
    EC = 1;
    EA = 1;
    CR = 1;
    for (;;)
        PCON |= PCON_IDL;
}
