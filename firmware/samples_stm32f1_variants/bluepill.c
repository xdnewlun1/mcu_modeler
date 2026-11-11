/* STM32F103C8 "Blue Pill": HSE 8 MHz x 9 = 72 MHz (PLL), APB1 36 MHz, APB2 72 MHz, flash 2 wait states.
 * - PC13 (the board LED, active low) blinks from SysTick: toggles every 5 ms (1 kHz tick).
 * - USART1 PA9/PA10 at 115200 (BRR 625): banner "Blue Pill ID xxxxxxxx FLASH nnK\r\n" (DBGMCU_IDCODE, F_SIZE),
 *   then "ping N\r\n" every 2 ms; replies are received by the RXNE interrupt, PB0 toggles per complete reply line.
 * - TIM4 (medium-density only timer) update interrupt every 1 ms counts tim4_ticks.
 * Alone: --at 3ms:USART1=hi.  On boards/stm32f103c8_stm32f405_uart.yaml the STM32F405 echoes uppercased lines. */
#include "f1x.h"
#include "console.h"

volatile uint32_t ticks, tim4_ticks, lines, rx_bytes;
static volatile uint32_t pings_due;

void SysTick_Handler(void)
{
    ticks++;
    if (ticks % 5u == 0)
        GPIO_ODR(GPIOC_BASE) ^= 1u << 13;
    if (ticks % 2u == 0)
        pings_due++;
}

void TIM4_IRQHandler(void)
{
    TIM_SR(TIM4_BASE) = 0;                                   /* clear UIF (rc_w0) */
    tim4_ticks++;
}

void USART1_IRQHandler(void)
{
    if (USART_SR(USART1_BASE) & USART_SR_RXNE) {
        char c = (char)USART_DR(USART1_BASE);
        rx_bytes++;
        if (c == '\n') {
            lines++;
            GPIO_ODR(GPIOB_BASE) ^= 1u;
        }
    }
}

int main(void)
{
    clock_72mhz();
    console_init(72000000u, 115200u);
    RCC_APB2ENR |= RCC_APB2ENR_IOPBEN | RCC_APB2ENR_IOPCEN;
    GPIO_ODR(GPIOC_BASE) |= 1u << 13;                        /* LED off (active low) */
    GPIO_CONFIG(GPIOC_BASE, 13, GPIO_OUT_PP_2MHZ);
    GPIO_CONFIG(GPIOB_BASE, 0, GPIO_OUT_PP_2MHZ);

    puts_("Blue Pill ID ");
    puthex(DBGMCU_IDCODE, 8);
    puts_(" FLASH ");
    putdec(F_SIZE);
    puts_("K\r\n");
    flush_();

    RCC_APB1ENR |= RCC_APB1ENR_TIM4EN;                        /* TIM4: 72 MHz / 72 / 1000 = 1 kHz */
    TIM_PSC(TIM4_BASE) = 71;
    TIM_ARR(TIM4_BASE) = 999;
    TIM_EGR(TIM4_BASE) = 1;
    TIM_SR(TIM4_BASE) = 0;
    TIM_DIER(TIM4_BASE) = 1;
    TIM_CR1(TIM4_BASE) = 1;
    NVIC_ENABLE(TIM4_IRQn);

    USART_CR1(USART1_BASE) |= USART_CR1_RXNEIE;
    NVIC_ENABLE(USART1_IRQn);
    SYST_RVR = 72000u - 1u;                                  /* 1 ms at HCLK */
    SYST_CVR = 0;
    SYST_CSR = 7;                                            /* CLKSOURCE = HCLK, TICKINT, ENABLE */
    enable_irq();

    uint32_t n = 0;
    for (;;) {
        while (!pings_due)
            __asm__ volatile("wfi");
        pings_due--;
        puts_("ping ");
        putdec(n++);
        puts_("\r\n");
    }
}
