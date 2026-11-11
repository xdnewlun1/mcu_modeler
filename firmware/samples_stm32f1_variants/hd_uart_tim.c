/* STM32F103 high density (ZE/VE/RC ...): USART3 / UART4 / UART5, TIM4 + TIM8 PWM, TIM5/6/7 interrupts, GPIOE-G.
 * HSE 8 MHz x 9 = 72 MHz, APB1 36 MHz (timers x2 = 72 MHz), APB2 72 MHz.
 * - USART1 PA9/PA10 115200 console: "HD ID xxxxxxxx FLASH nnnK\r\n", later "T5 a T6 b T7 c\r\n" and "done\r\n".
 * - USART3 PB10 TX (BRR 313 from 36 MHz) sends "USART3\r\n"; UART5 PC12 TX sends "UART5\r\n"; UART4 PC10 TX sends
 *   "UART4\r\n" and then echoes every byte received on PC11 (RXNE interrupt, vector 52) uppercased.
 * - TIM8 CH1 PWM on PC6 (advanced timer: BDTR.MOE): 10 kHz, 25 % duty.  TIM4 CH1 PWM on PB6: 1 kHz, 50 %.
 * - TIM6 update every 1 ms (vector 54) toggles PE0, PF0 and PG0 (only PE0 has a pin on LQFP100, none on LQFP64);
 *   TIM7 every 0.5 ms (vector 55), TIM5 every 2 ms (vector 50) only count.
 * After 10 TIM6 periods the counters are printed, then "done". */
#include "f1x.h"
#include "console.h"

volatile uint32_t t5, t6, t7, u4_rx;

void TIM6_IRQHandler(void)
{
    TIM_SR(TIM6_BASE) = 0;
    t6++;
    GPIO_ODR(GPIOE_BASE) ^= 1u;
    GPIO_ODR(GPIOF_BASE) ^= 1u;
    GPIO_ODR(GPIOG_BASE) ^= 1u;
}

void TIM7_IRQHandler(void)
{
    TIM_SR(TIM7_BASE) = 0;
    t7++;
}

void TIM5_IRQHandler(void)
{
    TIM_SR(TIM5_BASE) = 0;
    t5++;
}

void UART4_IRQHandler(void)
{
    if (USART_SR(UART4_BASE) & USART_SR_RXNE) {
        char c = (char)USART_DR(UART4_BASE);
        u4_rx++;
        if (c >= 'a' && c <= 'z')
            c = (char)(c - 32);
        USART_DR(UART4_BASE) = (uint8_t)c;          /* TXE is set: 1 byte in, 1 byte out at the same baud */
    }
}

static void uart_init(uint32_t base, uint32_t pclk)
{
    USART_BRR(base) = (pclk + 115200u / 2) / 115200u;
    USART_CR1(base) = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;
}

static void basic_timer(uint32_t base, uint32_t psc, uint32_t arr)
{
    TIM_PSC(base) = psc;
    TIM_ARR(base) = arr;
    TIM_EGR(base) = 1;                              /* load PSC */
    TIM_SR(base) = 0;
    TIM_DIER(base) = 1;                             /* UIE */
    TIM_CR1(base) = 1;
}

int main(void)
{
    clock_72mhz();
    console_init(72000000u, 115200u);
    puts_("HD ID ");
    puthex(DBGMCU_IDCODE, 8);
    puts_(" FLASH ");
    putdec(F_SIZE);
    puts_("K\r\n");

    RCC_APB2ENR |= RCC_APB2ENR_IOPBEN | RCC_APB2ENR_IOPCEN | RCC_APB2ENR_IOPDEN | RCC_APB2ENR_IOPEEN |
                   RCC_APB2ENR_IOPFEN | RCC_APB2ENR_IOPGEN | RCC_APB2ENR_TIM8EN;
    RCC_APB1ENR |= RCC_APB1ENR_USART3EN | RCC_APB1ENR_UART4EN | RCC_APB1ENR_UART5EN | RCC_APB1ENR_TIM4EN |
                   RCC_APB1ENR_TIM5EN | RCC_APB1ENR_TIM6EN | RCC_APB1ENR_TIM7EN;

    /* serial ports (default AFIO mapping, RM0008 9.3.8 Table 52; UART4/5 are not remappable) */
    GPIO_CONFIG(GPIOB_BASE, 10, GPIO_AF_PP_50MHZ);   /* USART3_TX */
    GPIO_CONFIG(GPIOB_BASE, 11, GPIO_IN_FLOATING);   /* USART3_RX */
    GPIO_CONFIG(GPIOC_BASE, 10, GPIO_AF_PP_50MHZ);   /* UART4_TX */
    GPIO_CONFIG(GPIOC_BASE, 11, GPIO_IN_FLOATING);   /* UART4_RX */
    GPIO_CONFIG(GPIOC_BASE, 12, GPIO_AF_PP_50MHZ);   /* UART5_TX */
    GPIO_CONFIG(GPIOD_BASE, 2, GPIO_IN_FLOATING);    /* UART5_RX */
    uart_init(USART3_BASE, 36000000u);
    uart_init(UART4_BASE, 36000000u);
    uart_init(UART5_BASE, 36000000u);
    uputs(USART3_BASE, "USART3\r\n");
    uputs(UART4_BASE, "UART4\r\n");
    uputs(UART5_BASE, "UART5\r\n");

    /* TIM8 CH1 (PC6) PWM 10 kHz 25 %: 72 MHz / 72 = 1 MHz, ARR 99, CCR1 25 */
    GPIO_CONFIG(GPIOC_BASE, 6, GPIO_AF_PP_50MHZ);
    TIM_PSC(TIM8_BASE) = 71;
    TIM_ARR(TIM8_BASE) = 99;
    TIM_CCR1(TIM8_BASE) = 25;
    TIM_CCMR1(TIM8_BASE) = (6u << 4) | (1u << 3);    /* OC1M = PWM mode 1, OC1PE */
    TIM_CCER(TIM8_BASE) = 1u;                        /* CC1E */
    TIM_BDTR(TIM8_BASE) = 1u << 15;                  /* MOE */
    TIM_EGR(TIM8_BASE) = 1;
    TIM_CR1(TIM8_BASE) = (1u << 7) | 1u;             /* ARPE, CEN */

    /* TIM4 CH1 (PB6) PWM 1 kHz 50 %: 72 MHz timer clock / 72, ARR 999, CCR1 500 */
    GPIO_CONFIG(GPIOB_BASE, 6, GPIO_AF_PP_50MHZ);
    TIM_PSC(TIM4_BASE) = 71;
    TIM_ARR(TIM4_BASE) = 999;
    TIM_CCR1(TIM4_BASE) = 500;
    TIM_CCMR1(TIM4_BASE) = (6u << 4) | (1u << 3);
    TIM_CCER(TIM4_BASE) = 1u;
    TIM_EGR(TIM4_BASE) = 1;
    TIM_CR1(TIM4_BASE) = (1u << 7) | 1u;

    /* GPIOE/F/G outputs */
    GPIO_CONFIG(GPIOE_BASE, 0, GPIO_OUT_PP_2MHZ);
    GPIO_CONFIG(GPIOF_BASE, 0, GPIO_OUT_PP_2MHZ);
    GPIO_CONFIG(GPIOG_BASE, 0, GPIO_OUT_PP_2MHZ);

    USART_CR1(UART4_BASE) |= USART_CR1_RXNEIE;
    NVIC_ENABLE(UART4_IRQn);
    NVIC_ENABLE(TIM5_IRQn);
    NVIC_ENABLE(TIM6_IRQn);
    NVIC_ENABLE(TIM7_IRQn);
    basic_timer(TIM6_BASE, 71, 999);                 /* 1 ms */
    basic_timer(TIM7_BASE, 71, 499);                 /* 0.5 ms */
    basic_timer(TIM5_BASE, 71, 1999);                /* 2 ms */
    enable_irq();

    while (t6 < 10)
        __asm__ volatile("wfi");
    uint32_t a = t5, b = t6, c = t7;
    puts_("T5 ");
    putdec(a);
    puts_(" T6 ");
    putdec(b);
    puts_(" T7 ");
    putdec(c);
    puts_("\r\ndone\r\n");
    for (;;)
        __asm__ volatile("wfi");
}
