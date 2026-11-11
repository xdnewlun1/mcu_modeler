/* can_dual: the two bxCANs of the connectivity line on one bus (RM0008 ch. 24): CAN1 (master, PA11 RX / PA12 TX) and
 * CAN2 (slave, PB12 RX / PB13 TX, no remap) at 500 kbit/s from PCLK1 = 36 MHz (72 MHz clock tree through PLL2; BRP 4
 * -> 9 MHz tq, 1 + 13 + 4 = 18 tq).  The 28 filter banks live in CAN1's register space: FMR.CAN2SB = 14 (reset value)
 * gives banks 0-13 to CAN1 and 14-27 to CAN2 (24.9.5); FMR.FINIT must be set to change them.  Bank 0 (CAN1) accepts
 * ID 0x201, bank 14 (CAN2) accepts ID 0x101, both 32-bit mask mode into FIFO 0.
 * Every 1 ms (SysTick) CAN1 sends ID 0x101 with a counter; the CAN2_RX0 handler answers with ID 0x201 carrying the
 * counter + 1; the CAN1_RX0 handler checks it.  USART1 (PA9, 115200) prints "TX n" / "RX2 n" / "RX1 n".
 * Run: chipem run --board boards/stm32f105rc_dual_can.yaml --for 10ms */
#include "console.h"

#define CANR(b, off) REG32((b) + (off))
#define MCR   0x000
#define MSR   0x004
#define TSR   0x008
#define RF0R  0x00C
#define IER   0x014
#define BTR   0x01C
#define TI0R  0x180
#define TDT0R 0x184
#define TDL0R 0x188
#define TDH0R 0x18C
#define RI0R  0x1B0
#define RDT0R 0x1B4
#define RDL0R 0x1B8
#define FMR   0x200
#define FM1R  0x204
#define FS1R  0x20C
#define FFA1R 0x214
#define FA1R  0x21C
#define FR1(n) (0x240 + 8 * (n))
#define FR2(n) (0x244 + 8 * (n))

volatile uint32_t sent, rx1, rx2, bad, ticks, can2sb;

void CAN2_RX0_IRQHandler(void)
{
    while (CANR(CAN2_BASE, RF0R) & 3u) {
        uint32_t id = CANR(CAN2_BASE, RI0R) >> 21, d = CANR(CAN2_BASE, RDL0R);
        if (id != 0x101u)
            bad++;
        rx2++;
        CANR(CAN2_BASE, RF0R) = 1u << 5;                  /* RFOM0 */
        if (CANR(CAN2_BASE, TSR) & (1u << 26)) {          /* TME0: answer as ID 0x201 */
            CANR(CAN2_BASE, TDT0R) = 4u;
            CANR(CAN2_BASE, TDL0R) = d + 1u;
            CANR(CAN2_BASE, TDH0R) = 0;
            CANR(CAN2_BASE, TI0R) = (0x201u << 21) | 1u;
        }
    }
}

void CAN1_RX0_IRQHandler(void)
{
    while (CANR(CAN1_BASE, RF0R) & 3u) {
        uint32_t id = CANR(CAN1_BASE, RI0R) >> 21, d = CANR(CAN1_BASE, RDL0R);
        if (id != 0x201u || d != sent)
            bad++;
        rx1++;
        CANR(CAN1_BASE, RF0R) = 1u << 5;
    }
}

void SysTick_Handler(void) { ticks++; }

static void can_start(uint32_t b)
{
    CANR(b, MCR) = 1u;                                    /* INRQ (leaves sleep) */
    while (!(CANR(b, MSR) & 1u))
        ;
    CANR(b, BTR) = (3u << 20) | (12u << 16) | 3u;         /* TS2 = 4, TS1 = 13, BRP = 4 */
    CANR(b, IER) = 1u << 1;                               /* FMPIE0 */
}

static void can_go(uint32_t b)
{
    CANR(b, MCR) = 1u << 6;                               /* ABOM, INRQ = 0: normal mode after 11 recessive bits */
    while (CANR(b, MSR) & 1u)
        ;
}

int main(void)
{
    clock_72mhz_pll2(0);
    console_init(72000000u, 115200u);
    RCC_APB2ENR |= RCC_APB2ENR_IOPAEN | RCC_APB2ENR_IOPBEN;
    RCC_APB1ENR |= RCC_APB1ENR_CAN1EN | RCC_APB1ENR_CAN2EN;     /* CAN2 needs the CAN1 clock too */
    GPIO_CONFIG(GPIOA_BASE, 11, GPIO_IN_FLOATING);        /* CAN1_RX */
    GPIO_CONFIG(GPIOA_BASE, 12, GPIO_AF_PP_50MHZ);        /* CAN1_TX */
    GPIO_CONFIG(GPIOB_BASE, 12, GPIO_IN_FLOATING);        /* CAN2_RX */
    GPIO_CONFIG(GPIOB_BASE, 13, GPIO_AF_PP_50MHZ);        /* CAN2_TX */
    can_start(CAN1_BASE);
    can_start(CAN2_BASE);
    CANR(CAN1_BASE, FMR) |= 1u;                           /* FINIT */
    can2sb = (CANR(CAN1_BASE, FMR) >> 8) & 0x3Fu;         /* 14 after reset */
    CANR(CAN1_BASE, FMR) = (CANR(CAN1_BASE, FMR) & ~(0x3Fu << 8)) | (14u << 8) | 1u;
    CANR(CAN1_BASE, FM1R) = 0;                            /* mask mode */
    CANR(CAN1_BASE, FS1R) = (1u << 0) | (1u << 14);       /* 32-bit */
    CANR(CAN1_BASE, FFA1R) = 0;                           /* FIFO 0 */
    CANR(CAN1_BASE, FR1(0)) = 0x201u << 21;
    CANR(CAN1_BASE, FR2(0)) = (0x7FFu << 21) | 6u;        /* match STID, IDE = 0, RTR = 0 */
    CANR(CAN1_BASE, FR1(14)) = 0x101u << 21;
    CANR(CAN1_BASE, FR2(14)) = (0x7FFu << 21) | 6u;
    CANR(CAN1_BASE, FA1R) = (1u << 0) | (1u << 14);
    CANR(CAN1_BASE, FMR) &= ~1u;
    NVIC_ENABLE(CAN1_RX0_IRQn);
    NVIC_ENABLE(CAN2_RX0_IRQn);
    can_go(CAN1_BASE);
    can_go(CAN2_BASE);
    SYST_RVR = 72000 - 1;                                 /* 1 ms */
    SYST_CVR = 0;
    SYST_CSR = 7;
    enable_irq();
    puts_("dual CAN, CAN2SB ");
    putdec(can2sb);
    puts_("\r\n");
    uint32_t t = 0, s1 = 0, s2 = 0;
    for (;;) {
        if (ticks != t && (CANR(CAN1_BASE, TSR) & (1u << 26))) {
            t = ticks;
            sent++;
            CANR(CAN1_BASE, TDT0R) = 4u;
            CANR(CAN1_BASE, TDL0R) = sent - 1u;
            CANR(CAN1_BASE, TDH0R) = 0;
            CANR(CAN1_BASE, TI0R) = (0x101u << 21) | 1u;
            puts_("TX ");
            putdec(sent);
            puts_("\r\n");
        }
        if (rx2 != s2) {
            s2 = rx2;
            puts_("RX2 ");
            putdec(s2);
            puts_("\r\n");
        }
        if (rx1 != s1) {
            s1 = rx1;
            puts_("RX1 ");
            putdec(s1);
            puts_("\r\n");
        }
        __asm__ volatile("wfi");
    }
}
