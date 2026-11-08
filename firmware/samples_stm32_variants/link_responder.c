/* link_responder: chip B of boards/stm32f405rg_f407ig_uart.yaml, written for the STM32F407IG (LQFP176): it uses
 * port G and port I, which only the 144/176-pin packages bond out (PI1 also exists on the WLCSP90, PG9/PG14 do not).
 * 16 MHz HSI (reset clock, no clock setup). USART6 on PG14 (TX, LQFP176 pin 157) / PG9 (RX, pin 152), AF8,
 * 230400 8N1 from PCLK2 = 16 MHz (BRR 69: 231884 baud, 0.5 % from the requester's 230769).
 * Answers every "REQ <n>" line with "ACK <n> <n*n>\r\n", toggles PI1 (pin 132) per answered request and counts
 * them in `requests`; any other line counts in `bad_lines`. */
#define LINK_UART            USART6
#define LINK_UART_IRQHandler USART6_IRQHandler
#include "lineio.h"

volatile uint32_t requests;                        /* REQ lines answered */
volatile uint32_t bad_lines;                       /* lines that were not "REQ <n>" */

static void handle_line(const char *s)
{
    uint32_t n;
    if (starts_with(s, "REQ ")) {
        s += 4;
        if (get_dec(&s, &n) && *s == 0) {
            puts_("ACK ");
            put_dec(n);
            putc_(' ');
            put_dec(n * n);
            puts_("\r\n");
            requests++;
            GPIOI->ODR ^= 1u << 1;
            return;
        }
    }
    bad_lines++;
}

int main(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOGEN | RCC_AHB1ENR_GPIOIEN;
    RCC_APB2ENR |= RCC_APB2ENR_USART6EN;
    gpio_af(GPIOG, 14, 8);                         /* USART6_TX */
    gpio_af(GPIOG, 9, 8);                          /* USART6_RX */
    gpio_pull(GPIOG, 9, GPIO_PULL_UP);
    gpio_mode(GPIOI, 1, GPIO_MODE_OUTPUT);

    USART6->BRR = USART_BRR(HSI_HZ, 230400u);
    USART6->CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE | USART_CR1_RXNEIE;
    NVIC_EnableIRQ(USART6_IRQn);
    __enable_irq();

    for (;;) {
        drain_rx();
        __disable_irq();
        if (ring_tail == ring_head)
            __WFI();
        __enable_irq();
    }
}
