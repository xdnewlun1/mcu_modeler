/* timer_tick.c - 1 ms system tick from the Timer 0 interrupt (mode 1 reload 65536 - 922 at 11.0592 MHz / 12),
 * handler in register bank 1 (__using(1)), high priority (PT0); P1.1 toggles every 10 ticks.  main() waits for
 * ticks in Idle mode and counts the 100 ms periods in `seconds10`. */
#include "p89cv51.h"

#define RELOAD (65536 - 922)
volatile unsigned int ticks;
volatile unsigned char seconds10;
volatile unsigned char isr_bank;            /* PSW.RS of the handler, stored by the handler */

void t0_isr(void) __interrupt(TF0_VECTOR) __using(1)
{
    static unsigned char div10;
    TH0 = RELOAD >> 8;
    TL0 = RELOAD & 0xFF;
    ticks++;
    isr_bank = (PSW >> 3) & 3;
    if (++div10 == 10) {
        div10 = 0;
        P1_1 = !P1_1;
    }
}

void main(void)
{
    unsigned int last = 0;
    TMOD = 0x01;
    TH0 = RELOAD >> 8;
    TL0 = RELOAD & 0xFF;
    PT0 = 1;
    ET0 = 1;
    EA = 1;
    TR0 = 1;
    for (;;) {
        PCON |= PCON_IDL;
        if (ticks - last >= 100) {
            last += 100;
            seconds10++;
        }
    }
}
