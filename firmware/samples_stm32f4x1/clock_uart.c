/* clock_uart: NUCLEO-F401RE / NUCLEO-F411RE style bring-up, built twice (build.sh): f401_clock_uart (-DLINE_F401,
 * 84 MHz) and f411_clock_uart (-DLINE_F411, 100 MHz); clock tree in stm32f4x1.h clock_init().
 * USART2 on PA2 (TX) / PA3 (RX), AF7 (the ST-LINK virtual COM port on the Nucleo), 115200 8N1 from PCLK1 = 42 / 50 MHz:
 * prints "hello F401 84 MHz\r\n" (F411: "hello F411 100 MHz\r\n"), then echoes every received byte in upper case.
 * TIM2 (APB1 timer clock = 2 x PCLK1 = HCLK) with PSC = HCLK/1 MHz - 1, ARR = 999 interrupts every 1 ms and toggles
 * PA5 (the Nucleo user LED LD2).  After 10 ticks DCKCFGR.TIMPRE = 1 (APB1 /2: the timer clock stays HCLK, so the
 * period does not change; tests/test_stm32f4x1_samples.py checks the 1 ms period on both sides of the write). */
#include "stm32f4x1.h"

#ifdef LINE_F401
#define BANNER "hello F401 84 MHz\r\n"
#else
#define BANNER "hello F411 100 MHz\r\n"
#endif

volatile uint32_t ticks;
volatile uint32_t echoed;

void TIM2_IRQHandler(void)
{
    TIM2->SR = ~TIM_SR_UIF;
    GPIOA->ODR ^= 1u << 5;
    ticks++;
}

static void putc2(char c)
{
    while (!(USART2->SR & USART_SR_TXE)) { }
    USART2->DR = (uint8_t)c;
}

int main(void)
{
    clock_init();
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN;
    RCC_APB1ENR |= RCC_APB1ENR_USART2EN | RCC_APB1ENR_TIM2EN;
    gpio_mode(GPIOA, 5, GPIO_MODE_OUTPUT);
    gpio_af(GPIOA, 2, 7);                       /* USART2_TX */
    gpio_af(GPIOA, 3, 7);                       /* USART2_RX */
    gpio_pull(GPIOA, 3, GPIO_PULL_UP);
    USART2->BRR = USART_BRR(PCLK1_HZ, 115200u);
    USART2->CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;

    TIM2->PSC = SYSCLK_HZ / 1000000u - 1u;      /* timer clock HCLK -> 1 MHz */
    TIM2->ARR = 1000u - 1u;                     /* 1 ms */
    TIM2->EGR = TIM_EGR_UG;
    TIM2->SR = 0;
    TIM2->DIER = TIM_DIER_UIE;
    NVIC_EnableIRQ(TIM2_IRQn);
    TIM2->CR1 = TIM_CR1_CEN;
    __enable_irq();

    for (const char *s = BANNER; *s; s++)
        putc2(*s);
    int timpre = 0;
    for (;;) {
        if (!timpre && ticks >= 10) {
            timpre = 1;
            RCC_DCKCFGR |= RCC_DCKCFGR_TIMPRE;
        }
        if (USART2->SR & USART_SR_RXNE) {
            char c = (char)USART2->DR;
            if (c >= 'a' && c <= 'z')
                c = (char)(c - 'a' + 'A');
            putc2(c);
            echoed++;
        }
    }
}
