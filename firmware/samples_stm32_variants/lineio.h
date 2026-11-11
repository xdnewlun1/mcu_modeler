/* Line-oriented, interrupt-driven UART I/O shared by link_requester.c and link_responder.c.
 * Before including, define LINK_UART (a USART_TypeDef pointer) and LINK_UART_IRQHandler (its vector name).
 * RX: RXNE interrupt -> 64-byte ring. TX: polled on TXE. Lines end in "\n"; '\r' is ignored. */
#ifndef LINEIO_H
#define LINEIO_H
#ifndef STM32F42X_H                                 /* f42x_*.c include f42x/stm32f42x.h first */
#include "variants.h"
#endif

#define RING      64u
#define LINE_MAX  32u

static volatile uint8_t ring[RING];
static volatile uint32_t ring_head, ring_tail;
static char line[LINE_MAX + 1];
static uint32_t line_len;

void LINK_UART_IRQHandler(void)
{
    if (LINK_UART->SR & USART_SR_RXNE) {
        uint8_t c = (uint8_t)LINK_UART->DR;        /* reading DR clears RXNE */
        uint32_t h = ring_head;
        if (((h + 1u) & (RING - 1u)) != ring_tail) {
            ring[h] = c;
            ring_head = (h + 1u) & (RING - 1u);
        }
    }
}

static void putc_(char c)
{
    while (!(LINK_UART->SR & USART_SR_TXE)) { }
    LINK_UART->DR = (uint8_t)c;
}

static void puts_(const char *s)
{
    while (*s)
        putc_(*s++);
}

static void put_dec(uint32_t v)
{
    char buf[10];
    unsigned n = 0;
    do {
        buf[n++] = (char)('0' + v % 10u);
        v /= 10u;
    } while (v);
    while (n)
        putc_(buf[--n]);
}

/* Parses a decimal number at *p, advancing *p; returns 0 when there is no digit. */
static int get_dec(const char **p, uint32_t *out)
{
    const char *s = *p;
    uint32_t v = 0;
    if (*s < '0' || *s > '9')
        return 0;
    while (*s >= '0' && *s <= '9')
        v = v * 10u + (uint32_t)(*s++ - '0');
    *p = s;
    *out = v;
    return 1;
}

static int starts_with(const char *s, const char *prefix)
{
    while (*prefix)
        if (*s++ != *prefix++)
            return 0;
    return 1;
}

static void handle_line(const char *s);

/* Takes the bytes received so far and calls handle_line() for each complete line. */
static void drain_rx(void)
{
    while (ring_tail != ring_head) {
        char c = (char)ring[ring_tail];
        ring_tail = (ring_tail + 1u) & (RING - 1u);
        if (c == '\r')
            continue;
        if (c == '\n') {
            line[line_len] = 0;
            line_len = 0;
            handle_line(line);
        } else if (line_len < LINE_MAX) {
            line[line_len++] = c;
        }
    }
}

#endif
