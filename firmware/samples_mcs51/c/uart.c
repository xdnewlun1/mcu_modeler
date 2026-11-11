/* uart.c - see uart.h.  RX: the ISR moves SBUF into a 16-byte ring (bytes are dropped when it is full); TX: putchar
 * queues into a 32-byte ring, the ISR sends the next byte on TI.  TI is cleared only by the ISR, so `tx_busy` tells
 * putchar whether it has to start a transmission itself. */
#include "uart.h"

#define RXN 16
#define TXN 32
static __idata volatile unsigned char rxbuf[RXN];
static __idata volatile unsigned char txbuf[TXN];
static volatile unsigned char rx_head, rx_tail, tx_head, tx_tail;
static volatile __bit tx_busy;

void uart_init(void)
{
    SCON = 0x50;            /* mode 1, REN */
    TMOD = (TMOD & 0x0F) | 0x20;   /* Timer 1 mode 2 (8-bit auto reload) */
    TH1 = 0xFD;             /* 11.0592 MHz / 12 / 32 / 3 = 9600 baud */
    TL1 = 0xFD;
    TR1 = 1;
    rx_head = rx_tail = tx_head = tx_tail = 0;
    tx_busy = 0;
    ES = 1;
    EA = 1;
}

void uart_isr(void) __interrupt(SI0_VECTOR) __using(2)
{
    if (RI) {
        unsigned char n = (rx_head + 1) & (RXN - 1);
        RI = 0;
        if (n != rx_tail) {
            rxbuf[rx_head] = SBUF;
            rx_head = n;
        }
    }
    if (TI) {
        TI = 0;
        if (tx_tail != tx_head) {
            SBUF = txbuf[tx_tail];
            tx_tail = (tx_tail + 1) & (TXN - 1);
        } else {
            tx_busy = 0;
        }
    }
}

int putchar(int c)
{
    unsigned char n = (tx_head + 1) & (TXN - 1);
    while (n == tx_tail)
        ;                   /* ring full: wait for the ISR */
    ES = 0;
    if (!tx_busy) {
        tx_busy = 1;
        SBUF = c;
    } else {
        txbuf[tx_head] = c;
        tx_head = n;
    }
    ES = 1;
    return c;
}

unsigned char uart_rx_avail(void)
{
    return rx_head != rx_tail;
}

int getchar(void)
{
    unsigned char c;
    while (rx_head == rx_tail)
        PCON |= PCON_IDL;   /* Idle until the next interrupt (RX, TX or another source) */
    c = rxbuf[rx_tail];
    rx_tail = (rx_tail + 1) & (RXN - 1);
    return c;
}

void uart_flush(void)
{
    while (tx_busy)
        ;
}
