/* blinky: PTC5 as GPIO output, toggled every 1000 iterations of a volatile counter. */
#include "k22.h"

int main(void)
{
    WDOG_Disable();
    SIM_SCGC5 |= SIM_SCGC5_PORTC;
    PORTC->PCR[5] = PORT_PCR_MUX(1) | PORT_PCR_DSE;
    GPIOC->PDDR |= 1u << 5;
    GPIOC->PCOR = 1u << 5;

    volatile uint32_t counter = 0;
    for (;;) {
        if (++counter >= 1000) {
            counter = 0;
            GPIOC->PTOR = 1u << 5;
        }
    }
}
