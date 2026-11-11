/* uart_console.c - printf/getchar console on the UART (9600 8N1, interrupt-driven RX/TX rings, uart.c).
 * Commands (one letter + Enter): l = toggle the LED on P1.0, c = print the counters, anything else is echoed in
 * upper case.  Exercises SDCC's printf (%s %u %lx %c) through putchar. */
#include <stdio.h>
#include <ctype.h>
#include "uart.h"

static char line[24];
static unsigned int ncmd;
static unsigned long nchars;

static void read_line(void)
{
    unsigned char n = 0;
    for (;;) {
        int c = getchar();
        nchars++;
        if (c == '\r' || c == '\n') {
            putchar('\r');
            putchar('\n');
            break;
        }
        if (n < sizeof line - 1) {
            line[n++] = c;
            putchar(c);
        }
    }
    line[n] = 0;
}

void main(void)
{
    uart_init();
    printf("\r\nP89CV51 SDCC console (%s)\r\n", "9600 8N1");
    for (;;) {
        printf("> ");
        read_line();
        ncmd++;
        if (line[0] == 'l' && line[1] == 0) {
            P1_0 = !P1_0;
            printf("LED %s\r\n", P1_0 ? "off" : "on");
        } else if (line[0] == 'c' && line[1] == 0) {
            printf("cmds %u chars %lx\r\n", ncmd, nchars);
        } else {
            char *p;
            for (p = line; *p; p++)
                putchar(toupper(*p));
            printf("\r\n");
        }
    }
}
