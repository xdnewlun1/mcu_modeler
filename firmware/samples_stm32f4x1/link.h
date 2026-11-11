/* Line-oriented UART I/O for f411_link_requester.c / f401_link_responder.c (boards/stm32f411re_f401re_uart.yaml).
 * Define LINK_UART (USART_TypeDef pointer) and LINK_UART_IRQHandler before including.  RX: RXNE interrupt into a
 * 64-byte ring; TX polled on TXE; lines end in '\n', '\r' is ignored. */
#ifndef LINK_H
#define LINK_H
#include "stm32f4x1.h"

#define RING 64u
static volatile uint8_t ring[RING];
static volatile uint32_t ring_head, ring_tail;
static char line[33];
static uint32_t line_len;

void LINK_UART_IRQHandler(void)
{
    if (LINK_UART->SR & USART_SR_RXNE) {
        uint8_t c = (uint8_t)LINK_UART->DR;
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
    char b[10];
    unsigned n = 0;
    do {
        b[n++] = (char)('0' + v % 10u);
        v /= 10u;
    } while (v);
    while (n)
        putc_(b[--n]);
}

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

static void drain_rx(void)
{
    while (ring_tail != ring_head) {
        char c = (char)ring[ring_tail];
        ring_tail = (ring_tail + 1u) & (RING - 1u);
        if (c == '\r')
            continue;
        if (c == '\n') {
            line[line_len] = 0;
            handle_line(line);
            line_len = 0;
        } else if (line_len < sizeof line - 1u) {
            line[line_len++] = c;
        }
    }
}

#endif
