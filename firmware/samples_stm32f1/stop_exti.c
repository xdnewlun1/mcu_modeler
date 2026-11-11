/* STM32F103T6 stop_exti: runs at 72 MHz (HSE x 9), blinks PB0 4 times (SysTick 1 ms), then enters Stop mode
 * (SLEEPDEEP, PWR_CR.PDDS = 0, LPDS = 1, WFI) with the button on PA0 (pull-up, EXTI0 falling edge) as wake-up source.
 * After the wake-up the clock is HSI 8 MHz (HSE/PLL stopped in Stop): the firmware records RCC_CFGR.SWS, switches
 * back to 72 MHz, counts the wake-up and prints "WAKE n" on USART1 (the UART is re-programmed for 72 MHz). */
#include "console.h"

volatile uint32_t wakes, sws_after_wake, ms;

void SysTick_Handler(void) { ms++; }

void EXTI0_IRQHandler(void)
{
    EXTI_PR = 1u;
}

static void delay_ms(uint32_t n)
{
    uint32_t t = ms;
    while (ms - t < n)
        __asm__ volatile("wfi");
}

int main(void)
{
    clock_72mhz();
    console_init(72000000u, 115200u);
    RCC_APB2ENR |= RCC_APB2ENR_IOPBEN | RCC_APB2ENR_AFIOEN;
    RCC_APB1ENR |= RCC_APB1ENR_PWREN;
    GPIO_CONFIG(GPIOB_BASE, 0, GPIO_OUT_PP_2MHZ);
    GPIO_CONFIG(GPIOA_BASE, 0, GPIO_IN_PULL);
    GPIO_BSRR(GPIOA_BASE) = 1u;
    EXTI_FTSR |= 1u;
    EXTI_IMR |= 1u;
    NVIC_ENABLE(EXTI0_IRQn);
    SYST_RVR = 72000 - 1;
    SYST_CVR = 0;
    SYST_CSR = 7;
    __asm__ volatile("cpsie i");
    puts_("STOP DEMO\r\n");
    for (;;) {
        for (int i = 0; i < 8; i++) {
            GPIO_ODR(GPIOB_BASE) ^= 1u;
            delay_ms(1);
        }
        flush_();
        SYST_CSR = 0;                                /* SysTick would stop in Stop anyway (HCLK off) */
        PWR_CR = (PWR_CR & ~PWR_CR_PDDS) | PWR_CR_LPDS | PWR_CR_CWUF;
        SCB_SCR |= SCB_SCR_SLEEPDEEP;
        __asm__ volatile("wfi");
        SCB_SCR &= ~SCB_SCR_SLEEPDEEP;
        sws_after_wake = (RCC_CFGR >> 2) & 3u;
        clock_72mhz();
        SYST_CVR = 0;
        SYST_CSR = 7;
        wakes++;
        puts_("WAKE ");
        putdec(wakes);
        puts_("\r\n");
    }
}
