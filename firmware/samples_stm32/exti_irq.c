/* exti_irq: PA0 input with pull-down, EXTI line 0 on the rising edge (SYSCFG_EXTICR1 = port A); EXTI0_IRQHandler
 * (IRQ 6) clears EXTI_PR and toggles PB0. */
#include "stm32f405.h"

volatile uint32_t presses;

void EXTI0_IRQHandler(void)
{
    EXTI_PR = 1u << 0;                             /* w1c */
    presses++;
    GPIOB->ODR ^= 1u << 0;
}

int main(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN | RCC_AHB1ENR_GPIOBEN;
    RCC_APB2ENR |= RCC_APB2ENR_SYSCFGEN;
    gpio_mode(GPIOA, 0, GPIO_MODE_INPUT);
    gpio_pull(GPIOA, 0, GPIO_PULL_DOWN);
    gpio_mode(GPIOB, 0, GPIO_MODE_OUTPUT);

    SYSCFG_EXTICR1 &= ~0xFu;                       /* EXTI0 <- PA0 */
    EXTI_RTSR |= 1u << 0;
    EXTI_FTSR &= ~(1u << 0);
    EXTI_PR = 1u << 0;
    EXTI_IMR |= 1u << 0;

    NVIC_ClearPendingIRQ(EXTI0_IRQn);
    NVIC_EnableIRQ(EXTI0_IRQn);
    __enable_irq();
    for (;;)
        __WFI();
}
