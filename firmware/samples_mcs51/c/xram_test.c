/* xram_test.c - the 768-byte on-chip expanded RAM (MOVX below 0300h while AUXR.EXTRAM = 0, data sheet sec. 6.2):
 * a __xdata array covering 0000h-02FFh is filled with an address pattern and checked; then AUXR.EXTRAM = 1 sends
 * MOVX to the external bus (nothing connected: floating FFh), and back.  Results in `pass`, `ext_read`. */
#include <stdio.h>
#include "uart.h"

__xdata __at (0x0000) unsigned char xram[768];
unsigned char pass, ext_read, kept;

void main(void)
{
    unsigned int i;
    uart_init();
    for (i = 0; i < sizeof xram; i++)
        xram[i] = (unsigned char)(i ^ (i >> 8) ^ 0x5A);
    pass = 1;
    for (i = 0; i < sizeof xram; i++)
        if (xram[i] != (unsigned char)(i ^ (i >> 8) ^ 0x5A))
            pass = 0;
    AUXR |= AUXR_EXTRAM;                 /* MOVX -> external bus */
    ext_read = xram[0x10];
    AUXR &= ~AUXR_EXTRAM;
    kept = xram[0x10] == (unsigned char)(0x10 ^ 0x5A);
    printf("XRAM %s ext %02X kept %u\r\n", pass ? "ok" : "BAD", (unsigned int)ext_read, (unsigned int)kept);
    uart_flush();
    for (;;)
        PCON |= PCON_IDL;
}
