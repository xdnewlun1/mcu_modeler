/* STM32F427/F437: 180 MHz with over-drive, then "hello 180\r\n" and an echo on UART7 (PE8 TX / PE7 RX, AF8,
 * DS9405 Table 12) at 115200 baud from PCLK1 = 45 MHz. TIM2 (APB1 timer clock 2 x 45 = 90 MHz) toggles PB0 every
 * 1 ms, proving the timer clock tree; TIMPRE = 1 (RCC_DCKCFGR) after 10 toggles makes it HCLK = 180 MHz, halving the
 * period (RM0090 6.3.25). */
#include "stm32f42x.h"

static void uart7_putc(char c)
{
    while (!(UART7->SR & USART_SR_TXE)) { }
    UART7->DR = (uint8_t)c;
}

static void uart7_puts(const char *s)
{
    while (*s)
        uart7_putc(*s++);
}

static volatile unsigned ticks;

void TIM2_IRQHandler(void)
{
    TIM2->SR = ~TIM_SR_UIF;
    GPIOB->ODR ^= 1u;
    ticks++;
}

int main(void)
{
    clock_180mhz();
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN | RCC_AHB1ENR_GPIOBEN | (1u << 4);   /* GPIOE */
    RCC_APB1ENR |= RCC_APB1ENR_UART7EN | RCC_APB1ENR_TIM2EN;
    gpio_mode(GPIOB, 0, GPIO_MODE_OUTPUT);
    gpio_af(GPIOE, 8, 8);                       /* PE8 = UART7_TX */
    gpio_af(GPIOE, 7, 8);                       /* PE7 = UART7_RX */
    gpio_pull(GPIOE, 7, GPIO_PULL_UP);
    UART7->BRR = USART_BRR(45000000u, 115200u);
    UART7->CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;

    TIM2->PSC = 90u - 1u;                       /* 90 MHz / 90 = 1 MHz */
    TIM2->ARR = 1000u - 1u;                     /* 1 ms */
    TIM2->EGR = TIM_EGR_UG;
    TIM2->SR = 0;
    TIM2->DIER = TIM_DIER_UIE;
    NVIC_EnableIRQ(TIM2_IRQn);
    TIM2->CR1 = TIM_CR1_CEN;
    __enable_irq();

    uart7_puts("hello 180\r\n");
    int timpre = 0;
    for (;;) {
        if (!timpre && ticks >= 10) {
            timpre = 1;
            RCC_DCKCFGR |= RCC_DCKCFGR_TIMPRE;   /* APB1 /4: TIMxCLK = 4 x PCLK1 = HCLK = 180 MHz */
        }
        if (UART7->SR & USART_SR_RXNE) {
            char c = (char)UART7->DR;
            if (c >= 'a' && c <= 'z')
                c = (char)(c - 'a' + 'A');
            uart7_putc(c);
        }
    }
}
