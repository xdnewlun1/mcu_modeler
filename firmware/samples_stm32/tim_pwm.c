/* tim_pwm: TIM3 channel 1 in PWM mode 1 on PA6 (AF2 = TIM3_CH1), 1 kHz with 25 % duty, at the reset clock (HSI
 * 16 MHz, APB1 /1 -> TIM3 kernel clock 16 MHz).  PSC = 15 (1 MHz counter), ARR = 999 (1 ms period), CCR1 = 250:
 * OC1REF is high while CNT < CCR1 (RM0090 18.3.9), preload on CCR1 and ARR, CC1E with CC1P = 0 (active high).
 * No interrupts: the CPU sleeps. */
#include "stm32f405.h"

#define RCC_APB1ENR_TIM3EN  (1u << 1)
#define TIM3 ((TIM_TypeDef *)0x40000400u)
#define TIM_CR1_ARPE        (1u << 7)
#define TIM_CCMR1_OC1PE     (1u << 3)
#define TIM_CCMR1_OC1M_PWM1 (6u << 4)
#define TIM_CCER_CC1E       (1u << 0)

int main(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN;
    RCC_APB1ENR |= RCC_APB1ENR_TIM3EN;
    gpio_af(GPIOA, 6, 2);                          /* PA6 = TIM3_CH1 */

    TIM3->PSC = 16u - 1u;                          /* 16 MHz / 16 = 1 MHz */
    TIM3->ARR = 1000u - 1u;                        /* 1 kHz */
    TIM3->CCR[0] = 250u;                           /* 25 % */
    TIM3->CCMR1 = TIM_CCMR1_OC1M_PWM1 | TIM_CCMR1_OC1PE;   /* CC1S = 00: output */
    TIM3->CCER = TIM_CCER_CC1E;
    TIM3->EGR = TIM_EGR_UG;                        /* load PSC/ARR/CCR1 shadows */
    TIM3->CR1 = TIM_CR1_ARPE | TIM_CR1_CEN;
    for (;;)
        __WFI();
}
