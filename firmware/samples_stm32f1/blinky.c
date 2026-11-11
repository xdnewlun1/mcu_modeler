/* STM32F103T6 blinky: PB0 push-pull output toggled every 1 ms from the SysTick COUNTFLAG (HSI 8 MHz after reset,
 * CLKSOURCE = HCLK, reload 8000 - 1).  Checks: GPIO output via CRL, RCC_APB2ENR gate, reset clock 8 MHz. */
#include "stm32f103.h"

int main(void)
{
    RCC_APB2ENR |= RCC_APB2ENR_IOPBEN;
    GPIO_CONFIG(GPIOB_BASE, 0, GPIO_OUT_PP_2MHZ);
    SYST_RVR = 8000 - 1;
    SYST_CVR = 0;
    SYST_CSR = 5;                                   /* ENABLE | CLKSOURCE (HCLK) */
    for (;;) {
        while (!(SYST_CSR & (1u << 16)))            /* COUNTFLAG */
            ;
        GPIO_ODR(GPIOB_BASE) ^= 1u << 0;
    }
}
