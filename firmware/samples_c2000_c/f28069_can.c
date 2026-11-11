/* f28069_can.c -- TMS320F28069 (LAUNCHXL-F28069M) eCAN-A node for boards/f28069_stm32_can.yaml (TI compiler cl2000,
 * --float_support=fpu32).  Same protocol as f2808_can.c, on the LaunchPad's CAN transceiver pins:
 *   init_sysctrl(): TI's InitSysCtrl order (SPRUH18I 1.3.2): watchdog off, Device_cal() (0x3D7C80), INTOSC1, DIVSEL 0,
 *     PLLCR = 18, PLLLOCKS, DIVSEL = 2 -> 90 MHz.
 *   eCAN-A at 500 kbit/s on GPIO30 (CANRXA) / GPIO31 (CANTXA), GPAMUX2 = 1.  The F2806x eCAN module clock is
 *     SYSCLKOUT / 2 = 45 MHz (SPRUH18 Table 1-15, PCLKCR0.ECANAENCLK): CANBTC BRPreg 8 (5 MHz tq), TSEG1reg 6,
 *     TSEG2reg 1 -> 10 tq per bit.
 *   Every 10 ms (CPU timer 0, polled) mailbox 0 sends 100#00<n>4B32 and LED D9 (GPIO34) toggles; mailbox 16 receives
 *     standard ID 0x202 (the STM32 can_echo answer), toggles LED D10 (GPIO39) and updates a float running average
 *     (FPU32) of the echoed counter (avg_x100).
 *   Console on SCI-A (GPIO29 SCITXDA, LSPCLK = 90 MHz, BRR 96 -> 115979 baud): "CAN F28069\r\n", "TX 100 <n>",
 *     "RX 202 [4] 00 nn 4B 32".
 * Registers are accessed 32 bits wide (SPRUH18 16.x). */
#define PICCOLO_2806X
#include "piccolo.h"

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
volatile long avg_x100;                                 /* FPU32 running average x 100 */

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
    CANBTC = (8ul << 16) | (6ul << 3) | 1ul;            /* BRPreg 8: 45 MHz / 9 = 5 MHz tq, 10 tq/bit */
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

static void init_sysctrl(void)
{
    EALLOW;
    WDCR = WDCR_DISABLE;
    PCLKCR0 |= PCLKCR0_ADCENCLK;
    (*Device_cal)();
    EALLOW;
    PCLKCR0 &= ~PCLKCR0_ADCENCLK;
    CLKCTL = 0x6400;                                    /* INTOSC1 (the LaunchPad has no crystal) */
    PLLSTS &= ~PLLSTS_DIVSEL;
    PLLSTS |= PLLSTS_MCLKOFF;
    PLLCR = 18;                                         /* 10 MHz x 18 / 2 = 90 MHz */
    while (!(PLLSTS & PLLSTS_PLLLOCKS)) { }
    PLLSTS &= ~PLLSTS_MCLKOFF;
    PLLSTS = (PLLSTS & ~PLLSTS_DIVSEL) | 0x0100u;
    EDIS;
}

void main(void)
{
    unsigned int seq = 0;
    float avg = 0.0f;
    DINT;
    frames_rx = frames_tx = 0;
    init_sysctrl();
    EALLOW;
    LOSPCP = 0;                                         /* LSPCLK = 90 MHz for the SCI */
    PCLKCR0 |= PCLKCR0_SCIAENCLK | PCLKCR0_ECANAENCLK;
    GpioCtrlRegs.GPAMUX2 |= (1ul << 26) | (1ul << 28) | (1ul << 30);   /* GPIO29 SCITXDA, GPIO30 CANRXA, GPIO31 CANTXA */
    GpioDataRegs.GPBSET = GPB_BIT(34) | GPB_BIT(39);    /* LEDs D9/D10 off (active low) */
    GpioCtrlRegs.GPBDIR |= GPB_BIT(34) | GPB_BIT(39);
    EDIS;
    SciaRegs.SCICCR = 0x0007;
    SciaRegs.SCICTL1 = SCICTL1_TXENA;
    SciaRegs.SCIHBAUD = 0;
    SciaRegs.SCILBAUD = 96;                             /* 90 MHz / (97 * 8) = 115979 baud (+0.7 %) */
    SciaRegs.SCICTL1 = SCICTL1_TXENA | SCICTL1_SWRESET;
    init_ecan();
    puts_("CAN F28069\r\n");
    CpuTimer0Regs.TCR = TCR_TSS;
    CpuTimer0Regs.PRD = 900000ul - 1;                   /* 10 ms at 90 MHz */
    CpuTimer0Regs.TPR = 0;
    CpuTimer0Regs.TPRH = 0;
    CpuTimer0Regs.TCR = TCR_TIF | TCR_TRB;
    for (;;) {
        if (CpuTimer0Regs.TCR & TCR_TIF) {
            CpuTimer0Regs.TCR |= TCR_TIF;
            MBOX(TX_MB, 4) = ((unsigned long)(seq & 0xFFu) << 16) | 0x4B32ul;
            CANTRS = 1ul << TX_MB;
            GpioDataRegs.GPBTOGGLE = GPB_BIT(34);
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
            avg = 0.75f * avg + 0.25f * (float)((mdl >> 16) & 0xFFu);   /* FPU32: running average of the echoed n */
            avg_x100 = (long)(avg * 100.0f);
            GpioDataRegs.GPBTOGGLE = GPB_BIT(39);
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
