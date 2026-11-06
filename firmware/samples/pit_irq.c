/* pit_irq: PIT channel 0 every 1 ms (bus clock 60 MHz, LDVAL = 59999); PIT0_IRQHandler (IRQ 48) toggles PTC5. */
#include "k22.h"

volatile uint32_t pit_ticks;

void PIT0_IRQHandler(void)
{
    PIT_TFLG(0) = PIT_TFLG_TIF;                    /* w1c */
    pit_ticks++;
    GPIOC->PTOR = 1u << 5;
}

int main(void)
{
    WDOG_Disable();
    SIM_SCGC5 |= SIM_SCGC5_PORTC;
    SIM_SCGC6 |= SIM_SCGC6_PIT;
    PORTC->PCR[5] = PORT_PCR_MUX(1) | PORT_PCR_DSE;
    GPIOC->PDDR |= 1u << 5;

    PIT_MCR = 0;                                   /* MDIS = 0: module enabled */
    PIT_LDVAL(0) = BUS_CLK_HZ / 1000u - 1u;        /* 59999 */
    PIT_TFLG(0) = PIT_TFLG_TIF;
    PIT_TCTRL(0) = PIT_TCTRL_TIE | PIT_TCTRL_TEN;

    NVIC_ClearPendingIRQ(PIT0_IRQn);
    NVIC_EnableIRQ(PIT0_IRQn);
    __enable_irq();
    for (;;)
        __WFI();
}
