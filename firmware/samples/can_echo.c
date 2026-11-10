/* can_echo: FlexCAN0 at 500 kbit/s on PTB18 (CAN0_TX) / PTB19 (CAN0_RX), ALT2; console on UART0 (PTA2 TX, 115200).
 * Every 10 ms (SysTick, 120 MHz core) message buffer 8 sends standard ID 0x100, DLC 4: counter (big endian, 16 bit),
 * 'K', '2'.  Message buffer 4 receives IDs 0x200-0x27F (RXMGMASK compares ID bits 10:7); each received frame is printed
 * as "RX 201 [4] 00 01 4B 32\r\n".  A sent frame prints "TX <n>\r\n".  Bit timing: CAN engine clock = bus clock
 * (CTRL1.CLKSRC = 1, 60 MHz), PRESDIV 5 -> 10 MHz time quanta, 1 + 8 + 8 + 3 = 20 tq per bit.
 * Run: boards/k22_c2000_can.yaml (with firmware/samples_c2000_c/can_echo.c) or alone (frames are then not
 * acknowledged: ACK errors, error passive). */
#include "k22.h"

#define CAN0_BASE    0x40024000u
#define CAN0_MCR     REG32(CAN0_BASE + 0x00)
#define CAN0_CTRL1   REG32(CAN0_BASE + 0x04)
#define CAN0_TIMER   REG32(CAN0_BASE + 0x08)
#define CAN0_RXMGMASK REG32(CAN0_BASE + 0x10)
#define CAN0_ECR     REG32(CAN0_BASE + 0x1C)
#define CAN0_IFLAG1  REG32(CAN0_BASE + 0x30)
#define CAN0_CS(n)   REG32(CAN0_BASE + 0x80 + 16u * (n))
#define CAN0_ID(n)   REG32(CAN0_BASE + 0x84 + 16u * (n))
#define CAN0_W0(n)   REG32(CAN0_BASE + 0x88 + 16u * (n))
#define CAN0_W1(n)   REG32(CAN0_BASE + 0x8C + 16u * (n))
#define MCR_MDIS     (1u << 31)
#define MCR_FRZ      (1u << 30)
#define MCR_HALT     (1u << 28)
#define MCR_FRZACK   (1u << 24)
#define MCR_SRXDIS   (1u << 17)
#define CTRL1_CLKSRC (1u << 13)
#define CODE(x)      ((uint32_t)(x) << 24)
#define SIM_SCGC6_FLEXCAN0 (1u << 4)
#define TX_MB 8u
#define RX_MB 4u

volatile uint32_t sent, received;

static void putc_(char c)
{
    while (!(UART0_S1 & UART_S1_TDRE)) { }
    UART0_D = (uint8_t)c;
}

static void puts_(const char *s)
{
    while (*s)
        putc_(*s++);
}

static void puthex(uint32_t v, int digits)
{
    while (digits--)
        putc_("0123456789ABCDEF"[(v >> (4 * digits)) & 0xF]);
}

static void putdec(uint32_t v)
{
    char b[10];
    int n = 0;
    do {
        b[n++] = (char)('0' + v % 10u);
        v /= 10u;
    } while (v);
    while (n)
        putc_(b[--n]);
}

static void uart0_init(void)
{
    uint32_t sbr = CORE_CLK_HZ / (16u * 115200u);
    uint32_t brfa = ((CORE_CLK_HZ * 4u / 115200u + 1u) / 2u - sbr * 32u) & 0x1Fu;
    PORTA->PCR[2] = PORT_PCR_MUX(2);
    UART0_C2 = 0;
    UART0_BDH = (uint8_t)((sbr >> 8) & 0x1F);
    UART0_BDL = (uint8_t)(sbr & 0xFF);
    UART0_C4 = UART_C4_BRFA(brfa);
    UART0_C1 = 0;
    UART0_C2 = UART_C2_TE;
}

static void can0_init(void)
{
    PORTB->PCR[18] = PORT_PCR_MUX(2);                 /* CAN0_TX */
    PORTB->PCR[19] = PORT_PCR_MUX(2);                 /* CAN0_RX */
    CAN0_MCR |= MCR_MDIS;
    CAN0_CTRL1 = CTRL1_CLKSRC;                         /* bus clock, selected while disabled */
    CAN0_MCR = MCR_FRZ | MCR_HALT | MCR_SRXDIS | 0x0Fu;   /* enable, freeze, 16 MBs, no self reception */
    while (!(CAN0_MCR & MCR_FRZACK)) { }
    CAN0_CTRL1 = (5u << 24) | (1u << 22) | (7u << 19) | (2u << 16) | CTRL1_CLKSRC | 7u;
    for (uint32_t n = 0; n < 16u; n++)
        CAN0_CS(n) = 0;
    CAN0_RXMGMASK = 0x1E000000u;                       /* compare std ID bits 10:7 */
    CAN0_ID(RX_MB) = 0x200u << 18;
    CAN0_CS(RX_MB) = CODE(0x4);                        /* Rx EMPTY */
    CAN0_CS(TX_MB) = CODE(0x8);                        /* Tx INACTIVE */
    CAN0_IFLAG1 = 0xFFFFFFFFu;
    CAN0_MCR &= ~(MCR_FRZ | MCR_HALT);
    while (CAN0_MCR & MCR_FRZACK) { }
}

static void send_frame(uint32_t n)
{
    CAN0_IFLAG1 = 1u << TX_MB;
    CAN0_ID(TX_MB) = 0x100u << 18;
    CAN0_W0(TX_MB) = ((n & 0xFFFFu) << 16) | ((uint32_t)'K' << 8) | '2';
    CAN0_CS(TX_MB) = CODE(0xC) | (4u << 16);            /* DATA, DLC 4 */
}

static void poll_rx(void)
{
    if (!(CAN0_IFLAG1 & (1u << RX_MB)))
        return;
    uint32_t cs = CAN0_CS(RX_MB);                       /* locks the MB */
    uint32_t id = (CAN0_ID(RX_MB) >> 18) & 0x7FFu;
    uint32_t w[2] = {CAN0_W0(RX_MB), CAN0_W1(RX_MB)};
    (void)CAN0_TIMER;                                   /* unlock */
    CAN0_IFLAG1 = 1u << RX_MB;
    uint32_t dlc = (cs >> 16) & 0xFu;
    received++;
    puts_("RX ");
    puthex(id, 3);
    puts_(" [");
    putdec(dlc);
    putc_(']');
    for (uint32_t i = 0; i < dlc && i < 8u; i++) {
        putc_(' ');
        puthex(w[i >> 2] >> (24u - 8u * (i & 3u)), 2);
    }
    puts_("\r\n");
}

int main(void)
{
    WDOG_Disable();
    SIM_CLKDIV1 = (1u << 24) | (4u << 16);             /* OUTDIV2 /2: bus = 60 MHz at the 120 MHz core clock */
    SIM_SCGC5 |= SIM_SCGC5_PORTA | SIM_SCGC5_PORTB;
    SIM_SCGC4 |= SIM_SCGC4_UART0;
    SIM_SCGC6 |= SIM_SCGC6_FLEXCAN0;
    uart0_init();
    can0_init();
    puts_("CAN ECHO K22\r\n");

    SYST_RVR = CORE_CLK_HZ / 100u - 1u;                 /* 10 ms */
    SYST_CVR = 0;
    SYST_CSR = SYST_CSR_ENABLE | SYST_CSR_CLKSOURCE;
    uint32_t n = 0;
    for (;;) {
        poll_rx();
        if (SYST_CSR & SYST_CSR_COUNTFLAG) {
            int done = n && (CAN0_IFLAG1 & (1u << TX_MB));   /* the previous frame was acknowledged */
            send_frame(n++);                                  /* first: keeps the 10 ms period exact */
            if (done) {
                sent++;
                puts_("TX ");
                putdec(sent);
                puts_("\r\n");
            }
        }
    }
}
