/* tim_irq: clock tree as the ST HAL examples set it up (HSE 8 MHz, PLL M=8 N=336 P=2 -> SYSCLK 168 MHz, AHB /1,
 * APB1 /4 = 42 MHz, APB2 /2 = 84 MHz, flash 5 wait states), then TIM2 (APB1 timer clock 84 MHz) with PSC = 83
 * (1 MHz counter) and ARR = 999: one update event per millisecond. TIM2_IRQHandler (IRQ 28) toggles PB0. */
#include "stm32f405.h"

volatile uint32_t tim_ticks;

void TIM2_IRQHandler(void)
{
    TIM2->SR = ~TIM_SR_UIF;                        /* rc_w0 */
    tim_ticks++;
    GPIOB->ODR ^= 1u << 0;
}

static void clock_168mhz(void)
{
    RCC_APB1ENR |= RCC_APB1ENR_PWREN;
    PWR_CR |= PWR_CR_VOS;                          /* scale 1 */
    RCC_CR |= RCC_CR_HSEON;
    while (!(RCC_CR & RCC_CR_HSERDY)) { }
    RCC_PLLCFGR = 8u | (336u << 6) | (0u << 16) | RCC_PLLCFGR_SRC_HSE | (7u << 24);   /* M=8 N=336 P=2 Q=7 */
    RCC_CR |= RCC_CR_PLLON;
    while (!(RCC_CR & RCC_CR_PLLRDY)) { }
    FLASH_ACR = FLASH_ACR_LATENCY_5WS | FLASH_ACR_PRFTEN | FLASH_ACR_ICEN | FLASH_ACR_DCEN;
    RCC_CFGR = RCC_CFGR_PPRE1_DIV4 | RCC_CFGR_PPRE2_DIV2 | RCC_CFGR_SW_PLL;
    while ((RCC_CFGR & RCC_CFGR_SWS_MASK) != RCC_CFGR_SWS_PLL) { }
}

int main(void)
{
    clock_168mhz();
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOBEN;
    RCC_APB1ENR |= RCC_APB1ENR_TIM2EN;
    gpio_mode(GPIOB, 0, GPIO_MODE_OUTPUT);

    TIM2->PSC = 84u - 1u;                          /* 84 MHz / 84 = 1 MHz */
    TIM2->ARR = 1000u - 1u;                        /* 1 ms */
    TIM2->EGR = TIM_EGR_UG;                        /* load PSC/ARR now */
    TIM2->SR = 0;
    TIM2->DIER = TIM_DIER_UIE;
    TIM2->CR1 = TIM_CR1_CEN;

    NVIC_ClearPendingIRQ(TIM2_IRQn);
    NVIC_EnableIRQ(TIM2_IRQn);
    __enable_irq();
    for (;;)
        __WFI();
}
