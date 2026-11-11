/* USART1 console helpers for the STM32F103 samples: PA9 TX (AF push-pull), PA10 RX (input floating), 8N1. */
#ifndef CONSOLE_H
#define CONSOLE_H
#include "stm32f103.h"

static inline void console_init(uint32_t pclk2_hz, uint32_t baud)
{
    RCC_APB2ENR |= RCC_APB2ENR_IOPAEN | RCC_APB2ENR_USART1EN;
    GPIO_CONFIG(GPIOA_BASE, 9, GPIO_AF_PP_50MHZ);
    GPIO_CONFIG(GPIOA_BASE, 10, GPIO_IN_FLOATING);
    USART_BRR(USART1_BASE) = (pclk2_hz + baud / 2) / baud;          /* 16x oversampling: mantissa.fraction */
    USART_CR1(USART1_BASE) = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;
}

static inline void putc_(char c)
{
    while (!(USART_SR(USART1_BASE) & USART_SR_TXE))
        ;
    USART_DR(USART1_BASE) = (uint8_t)c;
}

static inline void puts_(const char *s)
{
    while (*s)
        putc_(*s++);
}

static inline void puthex(uint32_t v, int digits)
{
    while (digits--)
        putc_("0123456789ABCDEF"[(v >> (4 * digits)) & 0xF]);
}

static inline void putdec(uint32_t v)
{
    char b[11];
    int i = 0;
    do {
        b[i++] = (char)('0' + v % 10);
        v /= 10;
    } while (v);
    while (i)
        putc_(b[--i]);
}

static inline void flush_(void)
{
    while (!(USART_SR(USART1_BASE) & USART_SR_TC))
        ;
}

static inline void enable_irq(void) { __asm__ volatile("cpsie i" ::: "memory"); }
#endif
