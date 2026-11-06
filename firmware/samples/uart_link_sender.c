/* uart_link_sender: chip A of boards/two_k22_uart.yaml.
 * UART0 on PTA1 (RX) / PTA2 (TX), ALT2, 115200 8N1 from the core clock (UART0/1 are core-clocked; the board runs
 * the chips at 120 MHz with clock_hz). Every 2 ms (SysTick COUNTFLAG, polled) it sends "ping N\r\n", N = 1, 2, ...
 * Every received '\n' (end of a reply line from the peer) toggles PTC5. RX is polled while waiting to transmit too. */
#include "k22.h"

#define UART_CLK_HZ CORE_CLK_HZ
#define BAUD        115200u

volatile uint32_t replies;          /* reply lines received */

static void poll_rx(void)
{
    if (UART0_S1 & UART_S1_RDRF) {
        char c = (char)UART0_D;
        if (c == '\n') {
            replies++;
            GPIOC->PTOR = 1u << 5;
        }
    }
}

static void uart0_putc(char c)
{
    while (!(UART0_S1 & UART_S1_TDRE))
        poll_rx();
    UART0_D = (uint8_t)c;
}

static void send_ping(uint32_t n)
{
    char digits[10];
    int i = 0;
    const char *p = "ping ";
    while (*p)
        uart0_putc(*p++);
    do {
        digits[i++] = (char)('0' + n % 10u);
        n /= 10u;
    } while (n && i < 10);
    while (i)
        uart0_putc(digits[--i]);
    uart0_putc('\r');
    uart0_putc('\n');
}

int main(void)
{
    WDOG_Disable();
    SIM_SCGC5 |= SIM_SCGC5_PORTA | SIM_SCGC5_PORTC;
    SIM_SCGC4 |= SIM_SCGC4_UART0;
    PORTA->PCR[1] = PORT_PCR_MUX(2);               /* UART0_RX */
    PORTA->PCR[2] = PORT_PCR_MUX(2);               /* UART0_TX */
    PORTC->PCR[5] = PORT_PCR_MUX(1) | PORT_PCR_DSE;
    GPIOC->PDDR |= 1u << 5;
    GPIOC->PCOR = 1u << 5;

    uint32_t sbr = UART_CLK_HZ / (16u * BAUD);                                  /* 65 */
    uint32_t brfa = ((UART_CLK_HZ * 4u / BAUD + 1u) / 2u - sbr * 32u) & 0x1Fu;  /* 3 */
    UART0_C2 = 0;
    UART0_BDH = (uint8_t)((sbr >> 8) & 0x1F);
    UART0_BDL = (uint8_t)(sbr & 0xFF);
    UART0_C4 = (UART0_C4 & ~0x1Fu) | UART_C4_BRFA(brfa);
    UART0_C1 = 0;
    UART0_C2 = UART_C2_TE | UART_C2_RE;

    SYST_RVR = CORE_CLK_HZ / 500u - 1u;            /* 2 ms */
    SYST_CVR = 0;
    SYST_CSR = SYST_CSR_CLKSOURCE | SYST_CSR_ENABLE;

    uint32_t n = 0;
    for (;;) {
        if (SYST_CSR & SYST_CSR_COUNTFLAG)
            send_ping(++n);
        poll_rx();
    }
}
