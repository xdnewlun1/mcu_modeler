/* STM32F103T6 uart_echo: HSE 8 MHz x 9 = 72 MHz (PLL), APB1 36 MHz, APB2 72 MHz, flash 2 wait states; USART1 on PA9/PA10
 * at 115200 baud (BRR = 625).  Prints a banner, then echoes every received byte uppercased (RXNE interrupt into a
 * ring buffer).  PB0 toggles once per received byte.  Used alone (--at 2ms:USART1=hi) and as the link endpoint of
 * boards/stm32f103_stm32f405_uart.yaml. */
#include "console.h"

static volatile uint8_t ring[64];
static volatile uint32_t head, tail;
volatile uint32_t rx_count;

void USART1_IRQHandler(void)
{
    if (USART_SR(USART1_BASE) & USART_SR_RXNE) {
        ring[head & 63u] = (uint8_t)USART_DR(USART1_BASE);       /* reading DR clears RXNE */
        head++;
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
    enable_irq();
    puts_("STM32F103T6 72 MHz\r\n");
    for (;;) {
        while (tail == head)
            __asm__ volatile("wfi");
        char c = (char)ring[tail & 63u];
        tail++;
        rx_count++;
        GPIO_ODR(GPIOB_BASE) ^= 1u;
        if (c >= 'a' && c <= 'z')
            c = (char)(c - 32);
        putc_(c);
    }
}
