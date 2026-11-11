/* STM32F103T6 button_exti: PA0 input with pull-up (button to GND), EXTI0 on both edges through AFIO_EXTICR1; the
 * EXTI0 handler mirrors the pin on PB1 (push-pull output) and counts falling edges (presses).  HSI 8 MHz. */
#include "stm32f103.h"

volatile uint32_t presses, edges;

void EXTI0_IRQHandler(void)
{
    EXTI_PR = 1u << 0;                               /* write 1 to clear */
    edges++;
    if (GPIO_IDR(GPIOA_BASE) & 1u) {
        GPIO_BSRR(GPIOB_BASE) = 1u << 1;
    } else {
        GPIO_BRR(GPIOB_BASE) = 1u << 1;
        presses++;
    }
}

int main(void)
{
    RCC_APB2ENR |= RCC_APB2ENR_IOPAEN | RCC_APB2ENR_IOPBEN | RCC_APB2ENR_AFIOEN;
    GPIO_CONFIG(GPIOA_BASE, 0, GPIO_IN_PULL);
    GPIO_BSRR(GPIOA_BASE) = 1u << 0;                 /* ODR = 1: pull-up */
    GPIO_CONFIG(GPIOB_BASE, 1, GPIO_OUT_PP_2MHZ);
    GPIO_BSRR(GPIOB_BASE) = 1u << 1;
    AFIO_EXTICR(0) = (AFIO_EXTICR(0) & ~0xFu) | 0u;  /* EXTI0 = PA0 */
    EXTI_RTSR |= 1u;
    EXTI_FTSR |= 1u;
    EXTI_IMR |= 1u;
    NVIC_ENABLE(EXTI0_IRQn);
    for (;;)
        __asm__ volatile("wfi");
}
