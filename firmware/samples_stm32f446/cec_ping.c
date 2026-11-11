/* HDMI-CEC initiator (STM32F446RE "A", logical address 4 = Playback device 1; board boards/stm32f446re_cec_pair.yaml with
 * cec_tv.c on "B", logical address 0 = TV).  HSI 16 MHz, CEC kernel clock HSI/488, CEC on PB6 (AF3, open drain),
 * console on USART2 (PA2, 115200).  Polled register sequence of RM0390 32.7 (TXDR before TXSOM, TXEOM before the last
 * TXDR write, TXBR -> next byte):
 *   ping 5 (nobody: TXACKE), ping 0 (acknowledged: TXEND), <Get CEC Version> 40 9F to the TV, wait for the TV's
 *   <CEC Version> reply 04 9E 05 (received: RXBR/RXEND), broadcast <Active Source> 4F 82 10 00 (TXEND). */
#include "cec_common.h"

static unsigned send(const uint8_t *msg, unsigned n)     /* returns the ISR flags that ended the message */
{
    CEC->ISR = 0x1FFFu;
    if (n == 1)
        CEC->CR |= CEC_CR_TXEOM;                         /* ping: header only */
    CEC->TXDR = msg[0];
    CEC->CR |= CEC_CR_TXSOM;
    unsigned i = 1;
    for (;;) {
        unsigned isr = CEC->ISR;
        if (isr & CEC_ISR_TXBR) {
            if (i == n - 1)
                CEC->CR |= CEC_CR_TXEOM;
            CEC->TXDR = msg[i++];
            CEC->ISR = CEC_ISR_TXBR;
        }
        if (isr & CEC_ISR_ARBLST)
            CEC->ISR = CEC_ISR_ARBLST;                   /* TXSOM stays set: the controller retries */
        if (isr & (CEC_ISR_TXEND | CEC_ISR_TXACKE | CEC_ISR_TXERR | CEC_ISR_TXUDR)) {
            CEC->ISR = isr & 0x1E00u;
            return isr;
        }
    }
}

static void report(const char *what, unsigned isr)
{
    puts2(what);
    puts2(isr & CEC_ISR_TXEND ? ": ack\r\n" : isr & CEC_ISR_TXACKE ? ": nack\r\n" : ": error\r\n");
}

int main(void)
{
    console_and_cec_pin();
    CEC->CR = 0;
    CEC->CFGR = CEC_CFGR_OAR(4) | CEC_CFGR_SFT(0);       /* SFT by hardware (2.5 / 4 / 6 bit periods) */
    CEC->IER = 0;
    CEC->CR = CEC_CR_CECEN;
    puts2("CEC A addr 4\r\n");

    static const uint8_t ping5[] = {0x45}, ping0[] = {0x40}, getver[] = {0x40, 0x9F};
    static const uint8_t active[] = {0x4F, 0x82, 0x10, 0x00};
    report("ping 5", send(ping5, 1));
    report("ping 0", send(ping0, 1));
    report("tx 40 9F", send(getver, 2));

    uint8_t rx[16];
    unsigned n = 0;
    for (;;) {                                            /* the TV's reply */
        unsigned isr = CEC->ISR;
        if (isr & CEC_ISR_RXBR) {
            if (n < sizeof rx)
                rx[n++] = (uint8_t)CEC->RXDR;
            CEC->ISR = CEC_ISR_RXBR;
            if (isr & CEC_ISR_RXEND) {
                CEC->ISR = CEC_ISR_RXEND;
                break;
            }
        }
    }
    puts2("rx");
    for (unsigned i = 0; i < n; i++) {
        putc2(' ');
        hex2(rx[i]);
    }
    puts2("\r\n");
    report("bcast 4F 82 10 00", send(active, 4));
    puts2("done\r\n");
    for (;;)
        __asm volatile("wfi");
}
