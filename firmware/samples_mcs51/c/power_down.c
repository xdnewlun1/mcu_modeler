/* power_down.c - Idle and Power-down from C (data sheet sec. 6.12): five Timer 0 interrupts wake the CPU from Idle
 * (`idle_wakes`), then Power-down with INT0 (P3.2, level-sensitive) as the wake-up source.  After the wake-up the
 * INT0 handler has run (`pd_wakes`), main() sets `state` = 0xA5 and toggles P1.0 every ~1 ms. */
#include "p89cv51.h"

volatile unsigned char idle_wakes, pd_wakes, state;

void t0_isr(void) __interrupt(TF0_VECTOR)
{
    TH0 = 0xDC;                          /* 10 ms */
    TL0 = 0x00;
    idle_wakes++;
    P1_1 = !P1_1;
}

void int0_isr(void) __interrupt(IE0_VECTOR)
{
    EX0 = 0;                             /* level-sensitive: the pin is still low */
    pd_wakes++;
    P1_2 = !P1_2;
}

void main(void)
{
    unsigned int i;
    TMOD = 0x01;
    TH0 = 0xDC;
    TL0 = 0x00;
    ET0 = 1;
    EA = 1;
    TR0 = 1;
    while (idle_wakes < 5)
        PCON |= PCON_IDL;
    TR0 = 0;
    ET0 = 0;
    IT0 = 0;
    EX0 = 1;
    PCON |= PCON_PD;
    state = 0xA5;
    for (;;) {
        P1_0 = !P1_0;
        for (i = 0; i < 100; i++)
            ;
    }
}
