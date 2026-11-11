/* STM32F103T6 uart_link_sender: chip A of boards/stm32f103_stm32f405_uart.yaml.  72 MHz (HSE x 9), USART1 PA9 (TX) /
 * PA10 (RX) at 115200 (BRR 625).  Every 2 ms (SysTick) it sends "ping N\r\n"; the peer (STM32F405 uart_link_echo)
 * answers each byte uppercased.  Received bytes are collected in rx[]; every '\n' counts a reply line and toggles PB0. */
#include "console.h"

volatile uint32_t replies, ticks, rx_bytes;
volatile char rx[256];

void SysTick_Handler(void) { ticks++; }

void USART1_IRQHandler(void)
{
    if (USART_SR(USART1_BASE) & USART_SR_RXNE) {
        char c = (char)USART_DR(USART1_BASE);
        rx[rx_bytes & 255u] = c;
        rx_bytes++;
        if (c == '\n') {
            replies++;
            GPIO_ODR(GPIOB_BASE) ^= 1u;
        }
    }
}

int main(void)
{
    clock_72mhz();
    console_init(72000000u, 115200u);
    RCC_APB2ENR |= RCC_APB2ENR_IOPBEN;
    GPIO_CONFIG(GPIOB_BASE, 0, GPIO_OUT_PP_2MHZ);
    USART_CR1(USART1_BASE) |= USART_CR1_RXNEIE;
    NVIC_ENABLE(USART1_IRQn);
    SYST_RVR = 72000 * 2 - 1;
    SYST_CVR = 0;
    SYST_CSR = 7;
    enable_irq();
    uint32_t n = 0, t = 0;
    for (;;) {
        while (ticks == t)
            __asm__ volatile("wfi");
        t = ticks;
        puts_("ping ");
        putdec(++n);
        puts_("\r\n");
    }
}
