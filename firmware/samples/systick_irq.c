/* systick_irq: SysTick every 1 ms (core clock 120 MHz, RVR = 119999); the handler toggles PTC5. */
#include "k22.h"

volatile uint32_t ticks;

void SysTick_Handler(void)
{
    ticks++;
    GPIOC->PTOR = 1u << 5;
}

int main(void)
{
    WDOG_Disable();
    SIM_SCGC5 |= SIM_SCGC5_PORTC;
    PORTC->PCR[5] = PORT_PCR_MUX(1) | PORT_PCR_DSE;
    GPIOC->PDDR |= 1u << 5;

    SYST_RVR = CORE_CLK_HZ / 1000u - 1u;          /* 119999 */
    SYST_CVR = 0;
    SYST_CSR = SYST_CSR_CLKSOURCE | SYST_CSR_TICKINT | SYST_CSR_ENABLE;
    __enable_irq();
    for (;;)
        __WFI();
}
