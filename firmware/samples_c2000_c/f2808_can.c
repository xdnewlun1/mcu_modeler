/* f2808_can.c -- TMS320F2808 eCAN-B node for boards/f2808_stm32_can.yaml (TI compiler, cl2000).
 *   eCAN-B at 500 kbit/s on GPIO8 (CANTXB) / GPIO10 (CANRXB), GPAMUX1 = 2 (Table 9-17 peripheral selection 2).
 *   The 280x eCAN module clock is SYSCLKOUT = 100 MHz (SPRUEU0 3.1.2; the F2833x divides by 2): CANBTC BRPreg 19
 *   (5 MHz time quanta), TSEG1reg 6, TSEG2reg 1 -> 1 + 7 + 2 = 10 tq per bit.
 *   Every 10 ms (CPU timer 0, polled) mailbox 0 sends 100#00<n>4B32; mailbox 16 receives standard ID 0x202 (the
 *   STM32 can_echo answer).  Console on SCI-A (GPIO29 SCITXDA, 115200 8N1): "CAN F2808\r\n", then "TX 100 <n>\r\n"
 *   when CANTA.0 sets and "RX 202 [4] 00 nn 4B 32\r\n" for every received frame.
 * Registers are accessed 32 bits wide (SPRUEU0 2.4). */
#include "f2808.h"

#define ECANB(off)   REG32(0x6200 + (off))
#define CANME   ECANB(0x00)
#define CANMD   ECANB(0x02)
#define CANTRS  ECANB(0x04)
#define CANTA   ECANB(0x08)
#define CANRMP  ECANB(0x0C)
#define CANMC   ECANB(0x14)
#define CANBTC  ECANB(0x16)
#define CANES   ECANB(0x18)
#define CANTIOC ECANB(0x2A)
#define CANRIOC ECANB(0x2C)
#define MBOX(n, w)  REG32(0x6300 + 8 * (n) + (w))      /* w: 0 MSGID, 2 MSGCTRL, 4 MDL, 6 MDH */
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
    CANBTC = (19ul << 16) | (6ul << 3) | 1ul;
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
    PLLSTS &= ~PLLSTS_CLKINDIV;
    PLLCR = 10;
    while (!(PLLSTS & PLLSTS_PLLLOCKS)) { }             /* 100 MHz */
    PCLKCR0 |= PCLKCR0_SCIAENCLK | PCLKCR0_ECANBENCLK;
    GpioCtrlRegs.GPAMUX2 |= 1ul << 26;                  /* GPIO29 SCITXDA */
    GpioCtrlRegs.GPAMUX1 |= (2ul << 16) | (2ul << 20);  /* GPIO8 CANTXB, GPIO10 CANRXB */
    EDIS;
    SciaRegs.SCICCR = 0x0007;
    SciaRegs.SCICTL1 = SCICTL1_TXENA;
    SciaRegs.SCIHBAUD = 0;
    SciaRegs.SCILBAUD = 26;
    SciaRegs.SCICTL1 = SCICTL1_TXENA | SCICTL1_SWRESET;
    init_ecan();
    puts_("CAN F2808\r\n");
    CpuTimer0Regs.TCR = TCR_TSS;
    CpuTimer0Regs.PRD = 1000000ul - 1;                  /* 10 ms */
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
