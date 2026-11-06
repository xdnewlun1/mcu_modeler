/* button_mirror: PTC6 input with pull-up; PTC5 output = !PTC6 (button to ground lights the LED). */
#include "k22.h"

int main(void)
{
    WDOG_Disable();
    SIM_SCGC5 |= SIM_SCGC5_PORTC;
    PORTC->PCR[5] = PORT_PCR_MUX(1) | PORT_PCR_DSE;
    PORTC->PCR[6] = PORT_PCR_MUX(1) | PORT_PCR_PE | PORT_PCR_PS;
    GPIOC->PDDR = (GPIOC->PDDR | (1u << 5)) & ~(1u << 6);

    for (;;) {
        if (GPIOC->PDIR & (1u << 6))
            GPIOC->PCOR = 1u << 5;
        else
            GPIOC->PSOR = 1u << 5;
    }
}
