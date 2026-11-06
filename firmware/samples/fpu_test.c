/* fpu_test: single-precision VFP math; PTC5 = 1 if 1.5f * 2.0f == 3.0f (and a few more checks pass). */
#include "k22.h"

volatile float fa = 1.5f, fb = 2.0f;   /* volatile: force real VFP instructions at run time */
volatile uint32_t result_bits;

int main(void)
{
    WDOG_Disable();
    SIM_SCGC5 |= SIM_SCGC5_PORTC;
    PORTC->PCR[5] = PORT_PCR_MUX(1) | PORT_PCR_DSE;
    PORTC->PCR[4] = PORT_PCR_MUX(1) | PORT_PCR_DSE;
    GPIOC->PDDR |= (1u << 5) | (1u << 4);

    float prod = fa * fb;
    float q = prod / fb;                 /* 1.5 */
    float s = prod - fa + 0.25f;         /* 1.75 */
    union { float f; uint32_t u; } bits = { .f = prod };
    result_bits = bits.u;                /* 0x40400000 */

    if (prod == 3.0f)
        GPIOC->PSOR = 1u << 5;
    else
        GPIOC->PCOR = 1u << 5;
    if (q == 1.5f && s == 1.75f && bits.u == 0x40400000u)
        GPIOC->PSOR = 1u << 4;           /* PTC4: extended check */
    else
        GPIOC->PCOR = 1u << 4;

    for (;;)
        __WFI();
}
