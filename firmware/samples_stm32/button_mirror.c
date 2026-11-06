/* button_mirror: PA0 input with pull-down (user button to VDD on ST boards); PB0 output = PA0. */
#include "stm32f405.h"

int main(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN | RCC_AHB1ENR_GPIOBEN;
    gpio_mode(GPIOA, 0, GPIO_MODE_INPUT);
    gpio_pull(GPIOA, 0, GPIO_PULL_DOWN);
    gpio_mode(GPIOB, 0, GPIO_MODE_OUTPUT);

    for (;;) {
        if (GPIOA->IDR & (1u << 0))
            GPIOB->BSRR = 1u << 0;
        else
            GPIOB->BSRR = 1u << (0 + 16);
    }
}
