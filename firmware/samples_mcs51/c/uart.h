/* uart.h - interrupt-driven UART (mode 1, 9600 8N1 from Timer 1 at 11.0592 MHz) with TX and RX ring buffers;
 * putchar()/getchar() for SDCC's stdio (printf, gets).  The ISR prototype must be visible in the file that holds
 * main() (SDCC places the vector there). */
#ifndef UART_H
#define UART_H
#include "p89cv51.h"

void uart_init(void);
int putchar(int c);
int getchar(void);
unsigned char uart_rx_avail(void);
void uart_flush(void);
void uart_isr(void) __interrupt(SI0_VECTOR) __using(2);

#endif
