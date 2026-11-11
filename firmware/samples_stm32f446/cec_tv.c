/* HDMI-CEC follower (STM32F446RE "B", logical address 0 = TV; board boards/stm32f446re_cec_pair.yaml with cec_ping.c
 * on "A").  HSI 16 MHz, CEC kernel clock HSI/488, CEC on PB6 (AF3, open drain), console on USART2 (PA2, 115200).
 * Interrupt driven reception (CEC IRQ 93: RXBR / RXEND / receive errors, RM0390 32.6): every message addressed to 0
 * is acknowledged by the controller; broadcasts are received without ACK.  Each message is printed; a
 * <Get CEC Version> (9F) is answered with <CEC Version> 1.4 (04 9E 05). */
#include "cec_common.h"

static volatile uint8_t rx[16];
static volatile unsigned rx_n, msg_len, errors;

void CEC_IRQHandler(void)
{
    unsigned isr = CEC->ISR & CEC->IER;
    if (isr & CEC_ISR_RXBR) {
        if (rx_n < sizeof rx)
            rx[rx_n++] = (uint8_t)CEC->RXDR;
        CEC->ISR = CEC_ISR_RXBR;
    }
    if (isr & CEC_ISR_RXEND) {
        CEC->ISR = CEC_ISR_RXEND;
        msg_len = rx_n;
        rx_n = 0;
    }
    if (isr & (CEC_ISR_RXOVR | CEC_ISR_BRE | CEC_ISR_SBPE | CEC_ISR_LBPE | CEC_ISR_RXACKE)) {
        CEC->ISR = isr & 0x7Cu;
        errors++;
        rx_n = 0;
    }
}

static unsigned send(const uint8_t *msg, unsigned n)
{
    CEC->ISR = 0x1F00u;
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
        if (isr & (CEC_ISR_TXEND | CEC_ISR_TXACKE | CEC_ISR_TXERR | CEC_ISR_TXUDR)) {
            CEC->ISR = isr & 0x1E00u;
            return isr;
        }
    }
}

int main(void)
{
    console_and_cec_pin();
    CEC->CR = 0;
    CEC->CFGR = CEC_CFGR_OAR(0);
    CEC->IER = CEC_ISR_RXBR | CEC_ISR_RXEND | CEC_ISR_RXOVR | CEC_ISR_BRE | CEC_ISR_SBPE | CEC_ISR_LBPE |
               CEC_ISR_RXACKE;
    NVIC_EnableIRQ(CEC_IRQn);
    CEC->CR = CEC_CR_CECEN;
    puts2("CEC B addr 0\r\n");
    unsigned msgs = 0;
    while (msgs < 3) {               /* ping 0, 40 9F, broadcast 4F 82 10 00 */
        while (!msg_len)
            __asm volatile("wfi");
        __asm volatile("cpsid i");
        unsigned n = msg_len;
        uint8_t m[16];
        for (unsigned i = 0; i < n; i++)
            m[i] = rx[i];
        msg_len = 0;
        __asm volatile("cpsie i");
        msgs++;
        puts2("rx");
        for (unsigned i = 0; i < n; i++) {
            putc2(' ');
            hex2(m[i]);
        }
        puts2("\r\n");
        if (n >= 2 && m[1] == 0x9F) {
            static const uint8_t ver[] = {0x04, 0x9E, 0x05};
            unsigned isr = send(ver, 3);
            puts2(isr & CEC_ISR_TXEND ? "tx 04 9E 05: ack\r\n" : "tx 04 9E 05: failed\r\n");
        }
    }
    puts2("errors ");
    hex2(errors);
    puts2("\r\ndone\r\n");
    for (;;)
        __asm volatile("wfi");
}
