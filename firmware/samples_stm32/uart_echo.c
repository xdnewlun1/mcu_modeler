/* uart_echo: USART1 on PA9 (TX) / PA10 (RX), AF7, 115200 8N1 from PCLK2 = 16 MHz HSI (BRR = 139 -> 115,108 baud);
 * prints "hello\r\n" then echoes bytes (polling). */
#include "stm32f405.h"

static void usart1_putc(char c)
{
    while (!(USART1->SR & USART_SR_TXE)) { }
    USART1->DR = (uint8_t)c;
}

static void usart1_puts(const char *s)
{
    while (*s)
        usart1_putc(*s++);
}

int main(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN;
    RCC_APB2ENR |= RCC_APB2ENR_USART1EN;
    gpio_af(GPIOA, 9, 7);                          /* USART1_TX */
    gpio_af(GPIOA, 10, 7);                         /* USART1_RX */
    gpio_pull(GPIOA, 10, GPIO_PULL_UP);

    USART1->BRR = USART_BRR(HSI_HZ, 115200u);
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;

    usart1_puts("hello\r\n");
    for (;;) {
        if (USART1->SR & USART_SR_RXNE) {
            char c = (char)USART1->DR;
            usart1_putc(c);
        }
    }
}
