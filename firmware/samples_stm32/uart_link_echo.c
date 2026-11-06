/* uart_link_echo: chip B of boards/k22_stm32_uart.yaml (same protocol as firmware/samples/uart_link_echo.c).
 * USART1 on PA9 (TX) / PA10 (RX), AF7, 115200 8N1 from PCLK2 = 16 MHz HSI.
 * Echoes every received byte back uppercased ('a'..'z' -> 'A'..'Z') and toggles PB0 per received byte. */
#include "stm32f405.h"

int main(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN | RCC_AHB1ENR_GPIOBEN;
    RCC_APB2ENR |= RCC_APB2ENR_USART1EN;
    gpio_af(GPIOA, 9, 7);                          /* USART1_TX */
    gpio_af(GPIOA, 10, 7);                         /* USART1_RX */
    gpio_pull(GPIOA, 10, GPIO_PULL_UP);
    gpio_mode(GPIOB, 0, GPIO_MODE_OUTPUT);

    USART1->BRR = USART_BRR(HSI_HZ, 115200u);
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;

    for (;;) {
        if (USART1->SR & USART_SR_RXNE) {
            char c = (char)USART1->DR;
            GPIOB->ODR ^= 1u << 0;
            if (c >= 'a' && c <= 'z')
                c = (char)(c - 'a' + 'A');
            while (!(USART1->SR & USART_SR_TXE)) { }
            USART1->DR = (uint8_t)c;
        }
    }
}
