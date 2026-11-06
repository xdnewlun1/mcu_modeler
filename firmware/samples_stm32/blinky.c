/* blinky: PB0 and PB7 as push-pull outputs (the LD1/LD2 pins of ST Nucleo-144 boards), toggled in opposite phase
 * every 1000 iterations of a volatile counter (16 MHz HSI, no clock setup). */
#include "stm32f405.h"

int main(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOBEN;
    gpio_mode(GPIOB, 0, GPIO_MODE_OUTPUT);
    gpio_mode(GPIOB, 7, GPIO_MODE_OUTPUT);
    GPIOB->BSRR = (1u << 0) | (1u << (7 + 16));    /* PB0 high, PB7 low */

    volatile uint32_t counter = 0;
    for (;;) {
        if (++counter >= 1000) {
            counter = 0;
            GPIOB->ODR ^= (1u << 0) | (1u << 7);
        }
    }
}
