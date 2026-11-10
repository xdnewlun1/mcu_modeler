/* uart_console: an interrupt-driven command console on UART0 (PTA1 RX / PTA2 TX, ALT2), 115200 8N1 from the
 * 120 MHz core clock (run with --clock 120e6 or a board `clock_hz: 120e6`). The core sleeps in WFI between
 * characters, so a real-time run (chipem/realtime.py) keeps up and costs no CPU while idle.
 *
 * Board signals (boards/k22_rs232_console.yaml, through an RS-232 transceiver, so the TTL levels are inverted):
 *   PTC5 out  LED ("led on" / "led off")
 *   PTC6 in   DTR from the PC (TTL low = asserted)
 *   PTC7 out  CTS to the PC   (driven low = asserted once the console is up)
 *
 * Prints a banner and a "> " prompt, echoes what is typed (backspace erases), and on CR runs:
 *   help | led on | led off | dtr | uptime | echo TEXT
 * A line typed while DTR is not asserted still works (the firmware only reports DTR). */
#include "k22.h"

#define BAUD 115200u
#define RXQ  64u

static volatile uint8_t rxq[RXQ];
static volatile uint32_t rx_head, rx_tail;
static volatile uint32_t ticks_10ms;

void UART0_RX_TX_IRQHandler(void)
{
    if (UART0_S1 & UART_S1_RDRF) {                 /* S1 read then D read clears RDRF */
        uint8_t c = UART0_D;
        uint32_t n = (rx_head + 1u) % RXQ;
        if (n != rx_tail) {
            rxq[rx_head] = c;
            rx_head = n;
        }
    }
}

void SysTick_Handler(void)
{
    ticks_10ms++;
}

static void putc0(char c)
{
    while (!(UART0_S1 & UART_S1_TDRE)) { }
    UART0_D = (uint8_t)c;
}

static void puts0(const char *s)
{
    while (*s)
        putc0(*s++);
}

static void putu(uint32_t v)
{
    char buf[11];
    int i = 10;
    buf[i] = 0;
    do {
        buf[--i] = (char)('0' + v % 10u);
        v /= 10u;
    } while (v && i > 0);
    puts0(&buf[i]);
}

static int streq(const char *a, const char *b)
{
    while (*a && *a == *b) {
        a++;
        b++;
    }
    return *a == *b;
}

static int starts(const char *a, const char *p)
{
    while (*p) {
        if (*a++ != *p++)
            return 0;
    }
    return 1;
}

static void run(const char *line)
{
    if (!*line)
        return;
    if (streq(line, "help")) {
        puts0("commands: help, led on, led off, dtr, uptime, echo TEXT\r\n");
    } else if (streq(line, "led on")) {
        GPIOC->PSOR = 1u << 5;
        puts0("LED on\r\n");
    } else if (streq(line, "led off")) {
        GPIOC->PCOR = 1u << 5;
        puts0("LED off\r\n");
    } else if (streq(line, "dtr")) {
        puts0((GPIOC->PDIR & (1u << 6)) ? "DTR off\r\n" : "DTR on\r\n");
    } else if (streq(line, "uptime")) {
        putu(ticks_10ms * 10u);
        puts0(" ms\r\n");
    } else if (starts(line, "echo ")) {
        puts0(line + 5);
        puts0("\r\n");
    } else {
        puts0("? ");
        puts0(line);
        puts0("\r\n");
    }
}

int main(void)
{
    WDOG_Disable();
    SIM_SCGC5 |= SIM_SCGC5_PORTA | SIM_SCGC5_PORTC;
    SIM_SCGC4 |= SIM_SCGC4_UART0;
    PORTA->PCR[1] = PORT_PCR_MUX(2);               /* UART0_RX */
    PORTA->PCR[2] = PORT_PCR_MUX(2);               /* UART0_TX */
    PORTC->PCR[5] = PORT_PCR_MUX(1) | PORT_PCR_DSE;
    PORTC->PCR[6] = PORT_PCR_MUX(1);               /* DTR in (driven by the transceiver) */
    PORTC->PCR[7] = PORT_PCR_MUX(1) | PORT_PCR_DSE;
    GPIOC->PCOR = 1u << 5;
    GPIOC->PSOR = 1u << 7;                         /* CTS deasserted until the console is up */
    GPIOC->PDDR |= (1u << 5) | (1u << 7);

    uint32_t sbr = CORE_CLK_HZ / (16u * BAUD);
    uint32_t brfa = ((CORE_CLK_HZ * 4u / BAUD + 1u) / 2u - sbr * 32u) & 0x1Fu;
    UART0_C2 = 0;
    UART0_BDH = (uint8_t)((sbr >> 8) & 0x1F);
    UART0_BDL = (uint8_t)(sbr & 0xFF);
    UART0_C4 = (UART0_C4 & ~0x1Fu) | UART_C4_BRFA(brfa);
    UART0_C1 = 0;
    UART0_C2 = UART_C2_TE | UART_C2_RE | UART_C2_RIE;

    SYST_RVR = CORE_CLK_HZ / 100u - 1u;            /* 10 ms */
    SYST_CVR = 0;
    SYST_CSR = SYST_CSR_CLKSOURCE | SYST_CSR_TICKINT | SYST_CSR_ENABLE;

    NVIC_ClearPendingIRQ(UART0_RX_TX_IRQn);
    NVIC_EnableIRQ(UART0_RX_TX_IRQn);
    __enable_irq();

    puts0("\r\nCHIP_EM K22 console - type help\r\n> ");
    GPIOC->PCOR = 1u << 7;                         /* CTS asserted */

    char line[48];
    uint32_t n = 0;
    for (;;) {
        __disable_irq();
        if (rx_tail == rx_head) {
            __WFI();                               /* wakes on the UART IRQ even with PRIMASK set */
            __enable_irq();
            continue;
        }
        __enable_irq();
        char c = (char)rxq[rx_tail];
        rx_tail = (rx_tail + 1u) % RXQ;
        if (c == '\r' || c == '\n') {
            puts0("\r\n");
            line[n] = 0;
            run(line);
            n = 0;
            puts0("> ");
        } else if (c == 8 || c == 127) {
            if (n) {
                n--;
                puts0("\b \b");
            }
        } else if (n + 1 < sizeof line && c >= 32 && c < 127) {
            line[n++] = c;
            putc0(c);
        }
    }
}
