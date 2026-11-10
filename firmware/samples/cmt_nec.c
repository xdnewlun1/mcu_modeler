/* cmt_nec: K22 CMT sends one NEC infrared frame (address 0x04, command 0x08) on CMT_IRO = PTD7 (ALT2), K22 RM ch. 41.
 *
 * Clocks: 120 MHz core, bus /3 = 40 MHz. CMT: PPS = /5 -> IF = 8 MHz, CMTDIV = 0 -> CMTCLK = 8 MHz, so the modulator
 * counts 1 us ticks (CMTCLK / 8). Carrier: 70 high + 140 low CMTCLK periods = 38.1 kHz, 33 % duty (CGH1 = 70,
 * CGL1 = 140). Time mode, IRO active high (OC.CMTPOL = 1, IROPEN = 1).
 * NEC frame: 9 ms leading mark, 4.5 ms space, 32 bits LSB first (address, ~address, command, ~command), each a
 * 560 us mark followed by a 560 us (0) or 1690 us (1) space, then a 560 us stop mark. One modulation cycle per
 * mark/space pair: tmark = (CMD1:CMD2 + 1) us, tspace = CMD3:CMD4 us. The CMD registers are double buffered: at every
 * end-of-cycle flag (MSC.EOCF, cleared by reading MSC and writing CMD2/CMD4) the next pair is written. After the stop
 * cycle has started, MCGEN is cleared: the cycle finishes and IRO returns to the IROL latch (low).
 * Prints "NEC 04 08 sent, <n> cycles" on UART0 (PTA2, 115200 8N1). */
#include "k22.h"

#define UART_CLK_HZ CORE_CLK_HZ
#define BAUD        115200u

#define CMT_BASE   0x40062000u
#define CMT_CGH1   REG8(CMT_BASE + 0x0)
#define CMT_CGL1   REG8(CMT_BASE + 0x1)
#define CMT_OC     REG8(CMT_BASE + 0x4)
#define CMT_MSC    REG8(CMT_BASE + 0x5)
#define CMT_CMD1   REG8(CMT_BASE + 0x6)
#define CMT_CMD2   REG8(CMT_BASE + 0x7)
#define CMT_CMD3   REG8(CMT_BASE + 0x8)
#define CMT_CMD4   REG8(CMT_BASE + 0x9)
#define CMT_PPS    REG8(CMT_BASE + 0xA)
#define CMT_OC_CMTPOL (1u << 6)
#define CMT_OC_IROPEN (1u << 5)
#define CMT_MSC_EOCF  (1u << 7)
#define CMT_MSC_MCGEN (1u << 0)
#define SIM_SCGC4_CMT (1u << 2)

volatile uint32_t cycles_sent, done;

static void putc0(char c)
{
    while (!(UART0_S1 & UART_S1_TDRE))
        ;
    UART0_D = (uint8_t)c;
}

static void puts0(const char *s)
{
    while (*s)
        putc0(*s++);
}

static void puthex(uint32_t v, int digits)
{
    while (digits--)
        putc0("0123456789ABCDEF"[(v >> (4 * digits)) & 0xF]);
}

static void set_cycle(uint32_t mark_us, uint32_t space_us)
{
    uint32_t mb = mark_us - 1u;
    CMT_CMD1 = (uint8_t)(mb >> 8);
    CMT_CMD2 = (uint8_t)mb;                         /* (clears EOCF after an MSC read) */
    CMT_CMD3 = (uint8_t)(space_us >> 8);
    CMT_CMD4 = (uint8_t)space_us;
}

static void wait_eocf(void)
{
    while (!(CMT_MSC & CMT_MSC_EOCF))
        ;
    cycles_sent++;
}

int main(void)
{
    WDOG_Disable();
    SIM_CLKDIV1 = (0u << 28) | (2u << 24) | (4u << 16);          /* core /1, bus /3 = 40 MHz, flash /5 */
    SIM_SCGC5 |= SIM_SCGC5_PORTA | SIM_SCGC5_PORTD;
    SIM_SCGC4 |= SIM_SCGC4_UART0 | SIM_SCGC4_CMT;
    PORTA->PCR[2] = PORT_PCR_MUX(2);                             /* UART0_TX */
    PORTD->PCR[7] = PORT_PCR_MUX(2) | PORT_PCR_DSE;              /* CMT_IRO */

    uint32_t sbr = UART_CLK_HZ / (16u * BAUD);
    uint32_t brfa = ((UART_CLK_HZ * 4u / BAUD + 1u) / 2u - sbr * 32u) & 0x1Fu;
    UART0_C2 = 0;
    UART0_BDH = (uint8_t)((sbr >> 8) & 0x1F);
    UART0_BDL = (uint8_t)(sbr & 0xFF);
    UART0_C4 = (UART0_C4 & ~0x1Fu) | UART_C4_BRFA(brfa);
    UART0_C1 = 0;
    UART0_C2 = UART_C2_TE;

    CMT_PPS = 4;                                                 /* 40 MHz / 5 = 8 MHz */
    CMT_CGH1 = 70;
    CMT_CGL1 = 140;
    CMT_OC = CMT_OC_CMTPOL | CMT_OC_IROPEN;

    const uint8_t addr = 0x04, cmd = 0x08;
    const uint32_t frame = (uint32_t)addr | ((uint32_t)(uint8_t)~addr << 8) | ((uint32_t)cmd << 16) |
                           ((uint32_t)(uint8_t)~cmd << 24);
    set_cycle(9000, 4500);                                       /* leading burst */
    CMT_MSC = CMT_MSC_MCGEN;
    for (int i = 0; i < 32; i++) {
        wait_eocf();                                             /* the previous cycle has been loaded */
        set_cycle(560, (frame >> i) & 1u ? 1690u : 560u);
    }
    wait_eocf();
    set_cycle(560, 0);                                           /* stop bit */
    wait_eocf();                                                 /* the stop cycle runs now */
    CMT_MSC = 0;                                                 /* finish it, then stop */
    puts0("NEC ");
    puthex(addr, 2);
    putc0(' ');
    puthex(cmd, 2);
    puts0(" sent, ");
    puthex(cycles_sent, 2);
    puts0(" cycles\r\n");
    done = 1;
    for (;;)
        __WFI();
}
