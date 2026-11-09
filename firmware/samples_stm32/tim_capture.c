/* tim_capture: TIM3 channel 2 input capture on PA7 (AF2 = TIM3_CH2, pull-down), rising edges, at the reset clock
 * (HSI 16 MHz, APB1 /1 -> TIM3 kernel clock 16 MHz).  PSC = 15: 1 MHz counter (1 us per tick), ARR = 0xFFFF
 * (free-running 16-bit).  CC2S = 01 (IC2 = TI2), CC2P = CC2NP = 0 (rising), IC2PSC = 00, CC2E, CC2IE.
 * TIM3_IRQHandler (IRQ 29) reads CCR2 (which clears CC2IF, RM0090 18.4.5) and computes the period between two
 * captures modulo 2^16; the main loop prints "period <n> us" on USART1 (PA9, 115200 8N1) for each new period.
 * Drive PA7 from outside: `chipem run ... --at 1ms:PA7=1 --at 1.5ms:PA7=0 --at 2ms:PA7=1 ...` or a board net. */
#include "stm32f405.h"

#define RCC_APB1ENR_TIM3EN  (1u << 1)
#define TIM3 ((TIM_TypeDef *)0x40000400u)
#define TIM3_IRQn           29
#define TIM_CCMR1_CC2S_TI2  (1u << 8)
#define TIM_CCER_CC2E       (1u << 4)
#define TIM_DIER_CC2IE      (1u << 2)
#define TIM_SR_CC2IF        (1u << 2)
#define TIM_SR_CC2OF        (1u << 10)

volatile uint32_t captures, overcaptures, period_us, have_period;
static uint16_t last;

void TIM3_IRQHandler(void)
{
    if (TIM3->SR & TIM_SR_CC2OF) {
        overcaptures++;
        TIM3->SR = ~TIM_SR_CC2OF;                  /* rc_w0 */
    }
    uint16_t now = (uint16_t)TIM3->CCR[1];         /* clears CC2IF */
    if (captures++) {
        period_us = (uint16_t)(now - last);
        have_period = 1;
    }
    last = now;
}

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

static void usart1_putu(uint32_t v)
{
    char buf[11];
    int i = 0;
    do {
        buf[i++] = (char)('0' + v % 10u);
        v /= 10u;
    } while (v);
    while (i)
        usart1_putc(buf[--i]);
}

int main(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN;
    RCC_APB1ENR |= RCC_APB1ENR_TIM3EN;
    RCC_APB2ENR |= RCC_APB2ENR_USART1EN;
    gpio_af(GPIOA, 9, 7);                          /* USART1_TX */
    gpio_af(GPIOA, 7, 2);                          /* TIM3_CH2 */
    gpio_pull(GPIOA, 7, GPIO_PULL_DOWN);
    USART1->BRR = USART_BRR(HSI_HZ, 115200u);
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE;

    TIM3->PSC = 16u - 1u;                          /* 1 MHz counter */
    TIM3->ARR = 0xFFFFu;
    TIM3->EGR = TIM_EGR_UG;
    TIM3->SR = 0;
    TIM3->CCMR1 = TIM_CCMR1_CC2S_TI2;              /* IC2 = TI2, no prescaler, no filter */
    TIM3->CCER = TIM_CCER_CC2E;                    /* rising edge, capture enabled */
    TIM3->DIER = TIM_DIER_CC2IE;
    TIM3->CR1 = TIM_CR1_CEN;
    NVIC_ClearPendingIRQ(TIM3_IRQn);
    NVIC_EnableIRQ(TIM3_IRQn);
    __enable_irq();

    usart1_puts("capture ready\r\n");
    for (;;) {
        __asm volatile ("cpsid i" ::: "memory");
        uint32_t p = period_us, h = have_period;
        have_period = 0;
        __asm volatile ("cpsie i" ::: "memory");
        if (h) {
            usart1_puts("period ");
            usart1_putu(p);
            usart1_puts(" us\r\n");
        } else {
            __WFI();
        }
    }
}
