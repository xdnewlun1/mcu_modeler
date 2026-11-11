/* f401_link_responder: chip B of boards/stm32f411re_f401re_uart.yaml (STM32F401RE, 84 MHz, clock_init()).
 * USART6 on PA11 (TX) / PA12 (RX), AF8 (the F401's second USART6 position, open pin data / HAL GPIO_AF8_USART6),
 * 230400 8N1 from PCLK2 = 84 MHz (BRR 365: 230137 baud).  Every "REQ <n>" line is answered with "ACK <n> <n*n>\r\n"
 * and toggles PA5 (LD2); `requests` counts them, `bad_lines` counts anything else. */
#define LINK_UART            USART6
#define LINK_UART_IRQHandler USART6_IRQHandler
#include "link.h"

volatile uint32_t requests, bad_lines;

static void handle_line(const char *s)
{
    uint32_t n;
    if (starts_with(s, "REQ ")) {
        s += 4;
        if (get_dec(&s, &n) && !*s) {
            puts_("ACK ");
            put_dec(n);
            putc_(' ');
            put_dec(n * n);
            puts_("\r\n");
            requests++;
            GPIOA->ODR ^= 1u << 5;
            return;
        }
    }
    bad_lines++;
}

int main(void)
{
    clock_init();
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN;
    RCC_APB2ENR |= RCC_APB2ENR_USART6EN;
    gpio_af(GPIOA, 11, 8);
    gpio_af(GPIOA, 12, 8);
    gpio_pull(GPIOA, 12, GPIO_PULL_UP);
    gpio_mode(GPIOA, 5, GPIO_MODE_OUTPUT);
    USART6->BRR = USART_BRR(PCLK2_HZ, 230400u);
    USART6->CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE | USART_CR1_RXNEIE;
    NVIC_EnableIRQ(USART6_IRQn);
    __enable_irq();
    for (;;)
        drain_rx();
}
