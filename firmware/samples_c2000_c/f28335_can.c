/* f28335_can.c -- TMS320F28335 eCAN-A node for boards/f28335_stm32_can.yaml (TI compiler, cl2000 --float_support=fpu32).
 *   eCAN-A at 500 kbit/s on GPIO30 (CANRXA) / GPIO31 (CANTXA), GPAMUX2 = 1 (SPRS439Q Table 8-18).
 *   150 MHz SYSCLKOUT (30 MHz crystal, PLLCR = 10, DIVSEL /2); the F2833x eCAN module clock is SYSCLKOUT / 2 = 75 MHz:
 *   CANBTC BRPreg 9 (7.5 MHz time quanta), TSEG1reg 10, TSEG2reg 2 -> 1 + 11 + 3 = 15 tq per bit.
 *   Every 10 ms (CPU timer 0, 1500000 cycles, polled) mailbox 0 sends 100#00<n>4B32; mailbox 16 receives standard ID
 *   0x202 (the STM32 can_echo answer).  Console on SCI-A (GPIO29 SCITXDA, BRR 40 from LSPCLK 37.5 MHz = 114329 baud):
 *   "CAN F28335\r\n", then "TX 100 <n>\r\n" when CANTA.0 sets and "RX 202 [4] 00 nn 4B 32\r\n" per received frame.
 * Registers are accessed 32 bits wide (SPRUEU1 / SPRUI07 ch. 13). */
#include "f28232.h"                                     /* same register addresses on the F28335 */

#define ECANA(off)   REG32(0x6000 + (off))
#define CANME   ECANA(0x00)
#define CANMD   ECANA(0x02)
#define CANTRS  ECANA(0x04)
#define CANTA   ECANA(0x08)
#define CANRMP  ECANA(0x0C)
#define CANMC   ECANA(0x14)
#define CANBTC  ECANA(0x16)
#define CANES   ECANA(0x18)
#define CANTIOC ECANA(0x2A)
#define CANRIOC ECANA(0x2C)
#define MBOX(n, w)  REG32(0x6100 + 8 * (n) + (w))      /* w: 0 MSGID, 2 MSGCTRL, 4 MDL, 6 MDH */
#define CANMC_SCB 0x2000ul
#define CANMC_CCR 0x1000ul
#define CANES_CCE 0x0010ul
#define RX_MB 16
#define TX_MB 0

volatile unsigned int frames_rx, frames_tx;

static void putc_(char c)
{
    while (!(SciaRegs.SCICTL2 & SCICTL2_TXRDY)) { }
    SciaRegs.SCITXBUF = (unsigned int)c & 0xFFu;
}

static void puts_(const char *s)
{
    while (*s)
        putc_(*s++);
}

static void puthex(unsigned long v, int digits)
{
    while (digits--)
        putc_("0123456789ABCDEF"[(unsigned int)(v >> (4 * digits)) & 0xFu]);
}

static void init_ecan(void)
{
    unsigned int n;
    EALLOW;
    CANTIOC = 0x8;                                      /* TXFUNC */
    CANRIOC = 0x8;                                      /* RXFUNC */
    CANMC = CANMC_SCB | CANMC_CCR;                      /* eCAN mode, stay in initialization */
    while (!(CANES & CANES_CCE)) { }
    CANBTC = (9ul << 16) | (10ul << 3) | 2ul;
    EDIS;
    CANME = 0;
    for (n = 0; n < 32; n++)
        MBOX(n, 2) = 0;
    MBOX(RX_MB, 0) = 0x202ul << 18;                     /* standard ID 0x202, no mask */
    MBOX(TX_MB, 0) = 0x100ul << 18;
    MBOX(TX_MB, 2) = 4;
    CANMD = 1ul << RX_MB;
    CANME = (1ul << RX_MB) | (1ul << TX_MB);
    EALLOW;
    CANMC = CANMC_SCB;                                  /* leave initialization mode */
    EDIS;
    while (CANES & CANES_CCE) { }
}

void main(void)
{
    unsigned int seq = 0;
    DINT;
    frames_rx = frames_tx = 0;
    EALLOW;
    WDCR = WDCR_DISABLE;
    PLLSTS &= ~PLLSTS_DIVSEL_MSK;
    PLLCR = 10;
    while (!(PLLSTS & PLLSTS_PLLLOCKS)) { }
    PLLSTS |= PLLSTS_DIVSEL_2;                          /* 150 MHz */
    PCLKCR0 |= PCLKCR0_SCIAENCLK | (1u << 14);          /* SCIAENCLK, ECANAENCLK */
    GpioCtrlRegs.GPAMUX2 |= (1ul << 26) | (1ul << 28) | (1ul << 30);  /* GPIO29 SCITXDA, GPIO30 CANRXA, GPIO31 CANTXA */
    EDIS;
    SciaRegs.SCICCR = 0x0007;
    SciaRegs.SCICTL1 = SCICTL1_TXENA;
    SciaRegs.SCIHBAUD = 0;
    SciaRegs.SCILBAUD = 40;
    SciaRegs.SCICTL1 = SCICTL1_TXENA | SCICTL1_SWRESET;
    init_ecan();
    puts_("CAN F28335\r\n");
    CpuTimer0Regs.TCR = TCR_TSS;
    CpuTimer0Regs.PRD = 1500000ul - 1;                  /* 10 ms at 150 MHz */
    CpuTimer0Regs.TPR = 0;
    CpuTimer0Regs.TPRH = 0;
    CpuTimer0Regs.TCR = TCR_TIF | TCR_TRB;
    for (;;) {
        if (CpuTimer0Regs.TCR & TCR_TIF) {
            CpuTimer0Regs.TCR |= TCR_TIF;
            MBOX(TX_MB, 4) = ((unsigned long)(seq & 0xFFu) << 16) | 0x4B32ul;
            CANTRS = 1ul << TX_MB;
        }
        if (CANTA & (1ul << TX_MB)) {
            CANTA = 1ul << TX_MB;
            frames_tx++;
            puts_("TX 100 ");
            puthex(seq, 2);
            puts_("\r\n");
            seq++;
        }
        if (CANRMP & (1ul << RX_MB)) {
            unsigned long ctrl = MBOX(RX_MB, 2), mdl = MBOX(RX_MB, 4), mdh = MBOX(RX_MB, 6);
            unsigned long id = (MBOX(RX_MB, 0) >> 18) & 0x7FFul;
            unsigned int dlc = (unsigned int)(ctrl & 0xFu), i;
            CANRMP = 1ul << RX_MB;
            frames_rx++;
            puts_("RX ");
            puthex(id, 3);
            puts_(" [");
            putc_((char)('0' + dlc));
            putc_(']');
            for (i = 0; i < dlc && i < 8; i++) {
                unsigned long w = i < 4 ? mdl : mdh;
                putc_(' ');
                puthex(w >> (24 - 8 * (i & 3)), 2);
            }
            puts_("\r\n");
        }
    }
}
