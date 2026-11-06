/* uart_link_echo: chip B of boards/two_k22_uart.yaml.
 * UART0 on PTA1 (RX) / PTA2 (TX), ALT2, 115200 8N1 from the core clock (board runs the chips at 120 MHz).
 * Echoes every received byte back uppercased ('a'..'z' -> 'A'..'Z') and toggles PTC5 per received byte. */
#include "k22.h"

#define UART_CLK_HZ CORE_CLK_HZ
#define BAUD        115200u

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

    uint32_t sbr = UART_CLK_HZ / (16u * BAUD);
    uint32_t brfa = ((UART_CLK_HZ * 4u / BAUD + 1u) / 2u - sbr * 32u) & 0x1Fu;
    UART0_C2 = 0;
    UART0_BDH = (uint8_t)((sbr >> 8) & 0x1F);
    UART0_BDL = (uint8_t)(sbr & 0xFF);
    UART0_C4 = (UART0_C4 & ~0x1Fu) | UART_C4_BRFA(brfa);
    UART0_C1 = 0;
    UART0_C2 = UART_C2_TE | UART_C2_RE;

    for (;;) {
        if (UART0_S1 & UART_S1_RDRF) {
            char c = (char)UART0_D;
            GPIOC->PTOR = 1u << 5;
            if (c >= 'a' && c <= 'z')
                c = (char)(c - 'a' + 'A');
            while (!(UART0_S1 & UART_S1_TDRE)) { }
            UART0_D = (uint8_t)c;
        }
    }
}
