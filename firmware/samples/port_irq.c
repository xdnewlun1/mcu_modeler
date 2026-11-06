/* port_irq: PTC6 input with pull-up, interrupt on falling edge (PORTC IRQ 61); the handler clears ISFR and
 * toggles PTC5. */
#include "k22.h"

volatile uint32_t presses;

void PORTC_IRQHandler(void)
{
    uint32_t flags = PORTC->ISFR;
    PORTC->ISFR = flags;                           /* w1c */
    if (flags & (1u << 6)) {
        presses++;
        GPIOC->PTOR = 1u << 5;
    }
}

int main(void)
{
    WDOG_Disable();
    SIM_SCGC5 |= SIM_SCGC5_PORTC;
    PORTC->PCR[5] = PORT_PCR_MUX(1) | PORT_PCR_DSE;
    PORTC->PCR[6] = PORT_PCR_MUX(1) | PORT_PCR_PE | PORT_PCR_PS | PORT_PCR_IRQC(PORT_IRQC_FALLING) | PORT_PCR_ISF;
    GPIOC->PDDR = (GPIOC->PDDR | (1u << 5)) & ~(1u << 6);

    NVIC_ClearPendingIRQ(PORTC_IRQn);
    NVIC_EnableIRQ(PORTC_IRQn);
    __enable_irq();
    for (;;)
        __WFI();
}
