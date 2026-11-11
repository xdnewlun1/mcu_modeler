/* Shared helpers of the HDMI-CEC samples (cec_ping.c / cec_tv.c): USART2 console at 115200 (HSI 16 MHz, PA2 AF7) and
 * the CEC pin PB6 (AF3, open drain; the board net provides the pull-up). */
#ifndef CEC_COMMON_H
#define CEC_COMMON_H
#include "stm32f446.h"

static void putc2(char c)
{
    while (!(USART2->SR & USART_SR_TXE)) { }
    USART2->DR = (uint8_t)c;
}

static void puts2(const char *s)
{
    while (*s)
        putc2(*s++);
}

static void hex2(unsigned v)
{
    putc2("0123456789ABCDEF"[(v >> 4) & 15u]);
    putc2("0123456789ABCDEF"[v & 15u]);
}

static void console_and_cec_pin(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN | RCC_AHB1ENR_GPIOBEN;
    RCC_APB1ENR |= RCC_APB1ENR_USART2EN | RCC_APB1ENR_CECEN;
    gpio_af(GPIOA, 2, 7);
    gpio_mode(GPIOA, 2, GPIO_MODE_AF);
    USART2->BRR = USART_BRR(HSI_HZ, 115200u);
    USART2->CR1 = USART_CR1_TE | USART_CR1_UE;
    GPIOB->OTYPER |= 1u << 6;                    /* CEC: alternate function open drain (RM0390 32.3.1) */
    gpio_af(GPIOB, 6, 3);
    gpio_mode(GPIOB, 6, GPIO_MODE_AF);
}

#endif
