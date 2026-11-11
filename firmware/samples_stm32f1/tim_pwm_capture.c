/* STM32F103T6 tim_pwm_capture: TIM3 CH1 PWM on PA6 (AF push-pull): 1 kHz, 25 % duty (HSI 8 MHz: PSC 7 -> 1 MHz
 * counter, ARR 999, CCR1 250).  TIM2 measures it on PA1 = TIM2_CH2 (input floating; wire PA6 to PA1): PWM input mode,
 * TI2 -> IC2 rising (period) and IC1 falling (high time, CC1S = 10 = TI2), slave reset mode on TI2FP2 (SMS 100,
 * TS 110), counter at 1 MHz.  The CC2 interrupt stores period/high (in us) and toggles PB0. */
#include "stm32f103.h"

volatile uint32_t period_us, high_us, captures;

void TIM2_IRQHandler(void)
{
    if (TIM_SR(TIM2_BASE) & (1u << 2)) {             /* CC2IF: cleared by reading CCR2 */
        period_us = TIM_CCR2(TIM2_BASE);
        high_us = TIM_CCR1(TIM2_BASE);
        captures++;
        GPIO_ODR(GPIOB_BASE) ^= 1u;
    }
}

int main(void)
{
    RCC_APB2ENR |= RCC_APB2ENR_IOPAEN | RCC_APB2ENR_IOPBEN;
    RCC_APB1ENR |= RCC_APB1ENR_TIM2EN | RCC_APB1ENR_TIM3EN;
    GPIO_CONFIG(GPIOA_BASE, 6, GPIO_AF_PP_50MHZ);    /* TIM3_CH1 */
    GPIO_CONFIG(GPIOA_BASE, 1, GPIO_IN_FLOATING);    /* TIM2_CH2 */
    GPIO_CONFIG(GPIOB_BASE, 0, GPIO_OUT_PP_2MHZ);

    TIM_PSC(TIM3_BASE) = 7;
    TIM_ARR(TIM3_BASE) = 999;
    TIM_CCR1(TIM3_BASE) = 250;
    TIM_CCMR1(TIM3_BASE) = (6u << 4) | (1u << 3);    /* OC1M = PWM mode 1, OC1PE */
    TIM_CCER(TIM3_BASE) = 1u;                        /* CC1E */
    TIM_EGR(TIM3_BASE) = 1u;                         /* UG: load PSC/ARR/CCR1 */
    TIM_CR1(TIM3_BASE) = (1u << 7) | 1u;             /* ARPE, CEN */

    TIM_PSC(TIM2_BASE) = 7;
    TIM_ARR(TIM2_BASE) = 0xFFFF;
    TIM_CCMR1(TIM2_BASE) = (1u << 8) | 2u;           /* CC2S = 01 (TI2), CC1S = 10 (TI2) */
    TIM_CCER(TIM2_BASE) = (1u << 4) | (1u << 0) | (1u << 1);   /* CC2E rising; CC1E, CC1P falling */
    TIM_SMCR(TIM2_BASE) = (6u << 4) | 4u;            /* TS = TI2FP2, SMS = reset mode */
    TIM_EGR(TIM2_BASE) = 1u;
    TIM_SR(TIM2_BASE) = 0;
    TIM_DIER(TIM2_BASE) = 1u << 2;                   /* CC2IE */
    TIM_CR1(TIM2_BASE) = 1u;
    NVIC_ENABLE(TIM2_IRQn);
    __asm__ volatile("cpsie i");
    for (;;)
        __asm__ volatile("wfi");
}
