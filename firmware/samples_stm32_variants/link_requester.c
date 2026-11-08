/* link_requester: chip A of boards/stm32f405rg_f407ig_uart.yaml, written for the STM32F405RG (LQFP64).
 * Clock tree as in the ST HAL examples: HSE 8 MHz -> PLL (M=8 N=336 P=2) -> SYSCLK 168 MHz, APB1 /4 = 42 MHz
 * (timer clock 84 MHz), APB2 /2 = 84 MHz, flash 5 wait states.
 * USART2 on PA2 (TX, LQFP64 pin 16) / PA3 (RX, pin 17), AF7, 230400 8N1 from PCLK1 = 42 MHz (BRR 182: 230769 baud).
 * TIM2 (PSC 83 -> 1 MHz, ARR 1999) interrupts every 2 ms; each tick sends "REQ <n>\r\n" (n = 1, 2, ...).
 * The peer answers "ACK <n> <n*n>\r\n". A correct answer increments `acks_ok` and toggles PB0 (LED, pin 26); a
 * wrong one increments `ack_errors` and drives PB1 (pin 27) high. PC13 (pin 2) goes high at the first correct ACK.
 * The main loop sleeps in WFI with PRIMASK set between the "anything to do?" check and the WFI. */
#define LINK_UART            USART2
#define LINK_UART_IRQHandler USART2_IRQHandler
#include "lineio.h"

#define RCC_APB1ENR_TIM2EN_ (1u << 0)

volatile uint32_t req_n;                           /* last request number sent */
volatile uint32_t acks_ok;                         /* correct ACK lines */
volatile uint32_t ack_errors;                      /* ACK lines with a wrong number or square */
static volatile uint32_t ticks;

void TIM2_IRQHandler(void)
{
    TIM2->SR = ~TIM_SR_UIF;                        /* rc_w0 */
    ticks++;
}

static void clock_168mhz(void)
{
    RCC_APB1ENR |= RCC_APB1ENR_PWREN;
    PWR_CR |= PWR_CR_VOS;
    RCC_CR |= RCC_CR_HSEON;
    while (!(RCC_CR & RCC_CR_HSERDY)) { }
    RCC_PLLCFGR = 8u | (336u << 6) | (0u << 16) | RCC_PLLCFGR_SRC_HSE | (7u << 24);
    RCC_CR |= RCC_CR_PLLON;
    while (!(RCC_CR & RCC_CR_PLLRDY)) { }
    FLASH_ACR = FLASH_ACR_LATENCY_5WS | FLASH_ACR_PRFTEN | FLASH_ACR_ICEN | FLASH_ACR_DCEN;
    RCC_CFGR = RCC_CFGR_PPRE1_DIV4 | RCC_CFGR_PPRE2_DIV2 | RCC_CFGR_SW_PLL;
    while ((RCC_CFGR & RCC_CFGR_SWS_MASK) != RCC_CFGR_SWS_PLL) { }
}

static void handle_line(const char *s)
{
    uint32_t n, sq;
    if (!starts_with(s, "ACK "))
        return;
    s += 4;
    if (get_dec(&s, &n) && *s++ == ' ' && get_dec(&s, &sq) && n == req_n && sq == n * n) {
        acks_ok++;
        GPIOB->ODR ^= 1u << 0;
        GPIOC->BSRR = 1u << 13;
    } else {
        ack_errors++;
        GPIOB->BSRR = 1u << 1;
    }
}

int main(void)
{
    clock_168mhz();
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN | RCC_AHB1ENR_GPIOBEN | RCC_AHB1ENR_GPIOCEN;
    RCC_APB1ENR |= RCC_APB1ENR_USART2EN | RCC_APB1ENR_TIM2EN_;
    gpio_af(GPIOA, 2, 7);                          /* USART2_TX */
    gpio_af(GPIOA, 3, 7);                          /* USART2_RX */
    gpio_pull(GPIOA, 3, GPIO_PULL_UP);
    gpio_mode(GPIOB, 0, GPIO_MODE_OUTPUT);
    gpio_mode(GPIOB, 1, GPIO_MODE_OUTPUT);
    gpio_mode(GPIOC, 13, GPIO_MODE_OUTPUT);

    USART2->BRR = USART_BRR(42000000u, 230400u);
    USART2->CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE | USART_CR1_RXNEIE;

    TIM2->PSC = 84u - 1u;                          /* 84 MHz timer clock -> 1 MHz */
    TIM2->ARR = 2000u - 1u;                        /* 2 ms */
    TIM2->EGR = TIM_EGR_UG;
    TIM2->SR = 0;
    TIM2->DIER = TIM_DIER_UIE;
    TIM2->CR1 = TIM_CR1_CEN;

    NVIC_EnableIRQ(USART2_IRQn);
    NVIC_EnableIRQ(TIM2_IRQn);
    __enable_irq();

    for (;;) {
        drain_rx();
        if (ticks) {
            __disable_irq();
            ticks--;
            __enable_irq();
            req_n++;
            puts_("REQ ");
            put_dec(req_n);
            puts_("\r\n");
            continue;
        }
        __disable_irq();
        if (ring_tail == ring_head && !ticks)
            __WFI();
        __enable_irq();
    }
}
