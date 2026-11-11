/* f42x_link_responder: chip B of boards/stm32f427vg_f405rg_uart7.yaml (STM32F427VG, LQFP100). 180 MHz with
 * over-drive (clock_180mhz(): HSE 8 MHz, PLL 360/8/2, APB1 45 MHz). UART7 on PE8 (TX, LQFP100 pin 39) / PE7 (RX,
 * pin 38), AF8 (DS9405 Table 12), 230400 8N1 from PCLK1 = 45 MHz (BRR 195: 230769 baud, the requester's rate).
 * Answers every "REQ <n>" line with "ACK <n> <n*n>\r\n" (same protocol as link_responder.c), toggles PB0 (pin 35) per
 * answered request and counts them in `requests`; any other line counts in `bad_lines`. */
#include "stm32f42x.h"
#define LINK_UART            UART7
#define LINK_UART_IRQHandler UART7_IRQHandler
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
            GPIOB->ODR ^= 1u;
            return;
        }
    }
    bad_lines++;
}

int main(void)
{
    clock_180mhz();
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOBEN | (1u << 4);   /* GPIOB, GPIOE */
    RCC_APB1ENR |= RCC_APB1ENR_UART7EN;
    gpio_af(GPIOE, 8, 8);                          /* UART7_TX */
    gpio_af(GPIOE, 7, 8);                          /* UART7_RX */
    gpio_pull(GPIOE, 7, GPIO_PULL_UP);
    gpio_mode(GPIOB, 0, GPIO_MODE_OUTPUT);

    UART7->BRR = USART_BRR(45000000u, 230400u);
    UART7->CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE | USART_CR1_RXNEIE;
    NVIC_EnableIRQ(UART7_IRQn);
    __enable_irq();

    for (;;) {
        drain_rx();
        __disable_irq();
        if (ring_tail == ring_head)
            __WFI();
        __enable_irq();
    }
}
