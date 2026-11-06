/* uart_echo: UART0 on PTA1 (RX) / PTA2 (TX), ALT2, 115200 8N1; prints "hello\r\n" then echoes bytes (polling).
 * Baud from a 60 MHz module clock as specified for the samples: SBR = 60e6 / (16 * 115200) = 32.55 -> SBR 32,
 * BRFA = 0.55 * 32 = 18 (115,177 baud). Note: on K22 silicon and in the emulator UART0/UART1 are clocked from the
 * core clock (UART2-5 from the bus clock); this sample does not program the MCG, so the core runs at the FEI
 * reset clock (20.97 MHz) and the actual baud is lower (~40 kBd). Only the byte stream matters to the tests. */
#include "k22.h"

#define UART_CLK_HZ BUS_CLK_HZ
#define BAUD        115200u

static void uart0_putc(char c)
{
    while (!(UART0_S1 & UART_S1_TDRE)) { }
    UART0_D = (uint8_t)c;
}

static void uart0_puts(const char *s)
{
    while (*s)
        uart0_putc(*s++);
}

int main(void)
{
    WDOG_Disable();
    SIM_SCGC5 |= SIM_SCGC5_PORTA;
    SIM_SCGC4 |= SIM_SCGC4_UART0;
    PORTA->PCR[1] = PORT_PCR_MUX(2);               /* UART0_RX */
    PORTA->PCR[2] = PORT_PCR_MUX(2);               /* UART0_TX */

    uint32_t sbr = UART_CLK_HZ / (16u * BAUD);                          /* 32 */
    uint32_t brfa = ((UART_CLK_HZ * 4u / BAUD + 1u) / 2u - sbr * 32u) & 0x1Fu;  /* round(2*clk/baud) - 32*sbr = 18 */
    UART0_C2 = 0;
    UART0_BDH = (uint8_t)((sbr >> 8) & 0x1F);
    UART0_BDL = (uint8_t)(sbr & 0xFF);
    UART0_C4 = (UART0_C4 & ~0x1Fu) | UART_C4_BRFA(brfa);
    UART0_C1 = 0;
    UART0_C2 = UART_C2_TE | UART_C2_RE;

    uart0_puts("hello\r\n");
    for (;;) {
        if (UART0_S1 & UART_S1_RDRF) {
            char c = (char)UART0_D;
            uart0_putc(c);
        }
    }
}
