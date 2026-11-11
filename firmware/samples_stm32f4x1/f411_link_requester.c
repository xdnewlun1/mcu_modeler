/* f411_link_requester: chip A of boards/stm32f411re_f401re_uart.yaml (STM32F411RE, 100 MHz, clock_init()).
 * USART1 on PA9 (TX) / PA10 (RX), AF7, 230400 8N1 from PCLK2 = 100 MHz (BRR 434: 230415 baud).  TIM2 (100 MHz timer
 * clock, PSC 99 -> 1 MHz, ARR 1999) interrupts every 2 ms; each tick sends "REQ <n>\r\n".  The peer answers
 * "ACK <n> <n*n>\r\n": a correct answer increments `acks_ok` and toggles PA5 (LD2), a wrong one increments
 * `ack_errors` and drives PC0 high. */
#define LINK_UART            USART1
#define LINK_UART_IRQHandler USART1_IRQHandler
#include "link.h"

volatile uint32_t req_n, acks_ok, ack_errors;
static volatile uint32_t ticks;

void TIM2_IRQHandler(void)
{
    TIM2->SR = ~TIM_SR_UIF;
    ticks++;
}

static void handle_line(const char *s)
{
    uint32_t n, sq;
    if (!starts_with(s, "ACK "))
        return;
    s += 4;
    if (get_dec(&s, &n) && *s++ == ' ' && get_dec(&s, &sq) && n == req_n && sq == n * n) {
        acks_ok++;
        GPIOA->ODR ^= 1u << 5;
    } else {
        ack_errors++;
        GPIOC->BSRR = 1u << 0;
    }
}

int main(void)
{
    clock_init();
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN | RCC_AHB1ENR_GPIOCEN;
    RCC_APB2ENR |= RCC_APB2ENR_USART1EN;
    RCC_APB1ENR |= RCC_APB1ENR_TIM2EN;
    gpio_af(GPIOA, 9, 7);
    gpio_af(GPIOA, 10, 7);
    gpio_pull(GPIOA, 10, GPIO_PULL_UP);
    gpio_mode(GPIOA, 5, GPIO_MODE_OUTPUT);
    gpio_mode(GPIOC, 0, GPIO_MODE_OUTPUT);
    USART1->BRR = USART_BRR(PCLK2_HZ, 230400u);
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE | USART_CR1_RXNEIE;

    TIM2->PSC = SYSCLK_HZ / 1000000u - 1u;
    TIM2->ARR = 2000u - 1u;
    TIM2->EGR = TIM_EGR_UG;
    TIM2->SR = 0;
    TIM2->DIER = TIM_DIER_UIE;
    TIM2->CR1 = TIM_CR1_CEN;
    NVIC_EnableIRQ(USART1_IRQn);
    NVIC_EnableIRQ(TIM2_IRQn);
    __enable_irq();

    uint32_t seen = 0;
    for (;;) {
        drain_rx();
        if (ticks != seen) {
            seen = ticks;
            req_n++;
            puts_("REQ ");
            put_dec(req_n);
            puts_("\r\n");
        }
    }
}
