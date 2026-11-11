/* blinky.c - P1.0 toggles every 50 ms: Timer 0 mode 1 polled (46080 counts at 11.0592 MHz / 12). */
#include "p89cv51.h"

static void delay_50ms(void)
{
    TR0 = 0;
    TH0 = (65536 - 46080) >> 8;
    TL0 = (65536 - 46080) & 0xFF;
    TF0 = 0;
    TR0 = 1;
    while (!TF0)
        ;
}

void main(void)
{
    TMOD = 0x01;
    for (;;) {
        P1_0 = !P1_0;
        delay_50ms();
    }
}
