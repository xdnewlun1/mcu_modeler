/* can_echo: bxCAN1 at 500 kbit/s on PB8 (CAN1_RX) / PB9 (CAN1_TX), AF9; console on USART1 (PA9, 115200).
 * PCLK1 = 16 MHz (HSI, reset clocks): BRP 1 -> 8 MHz time quanta, 1 + 13 (TS1 12) + 2 (TS2 1) = 16 tq per bit.
 * Filter bank 0 (32-bit mask, FIFO 0) accepts standard data frame 0x100; bank 1 (16-bit list, FIFO 1) the IDs
 * 0x300-0x303.  The FIFO 0/1 interrupts (CAN1_RX0/RX1) copy received frames into a ring; the main loop prints each
 * one as "RX 100 [4] 00 01 4B 32 F0\r\n" (F<fifo><filter match index>) and echoes frames from FIFO 0 as ID 0x202
 * (same data) from the mailbox TSR.CODE names.  "TX 202\r\n" when a mailbox reports TXOK, "TERR\r\n" on an error.
 * Run: boards/k22_stm32_can.yaml (with firmware/samples/can_echo.c on a K22) or
 *      boards/k22_c2000_stm32_can.yaml (three nodes: the F28232 echoes the same frame as 0x201). */
#include "stm32f405.h"

#define CAN1_BASE  0x40006400u
#define CAN_MCR    REG32(CAN1_BASE + 0x000)
#define CAN_MSR    REG32(CAN1_BASE + 0x004)
#define CAN_TSR    REG32(CAN1_BASE + 0x008)
#define CAN_RFR(f) REG32(CAN1_BASE + 0x00C + 4u * (f))
#define CAN_IER    REG32(CAN1_BASE + 0x014)
#define CAN_ESR    REG32(CAN1_BASE + 0x018)
#define CAN_BTR    REG32(CAN1_BASE + 0x01C)
#define CAN_TIR(k)  REG32(CAN1_BASE + 0x180 + 0x10u * (k))
#define CAN_TDTR(k) REG32(CAN1_BASE + 0x184 + 0x10u * (k))
#define CAN_TDLR(k) REG32(CAN1_BASE + 0x188 + 0x10u * (k))
#define CAN_TDHR(k) REG32(CAN1_BASE + 0x18C + 0x10u * (k))
#define CAN_RIR(f)  REG32(CAN1_BASE + 0x1B0 + 0x10u * (f))
#define CAN_RDTR(f) REG32(CAN1_BASE + 0x1B4 + 0x10u * (f))
#define CAN_RDLR(f) REG32(CAN1_BASE + 0x1B8 + 0x10u * (f))
#define CAN_RDHR(f) REG32(CAN1_BASE + 0x1BC + 0x10u * (f))
#define CAN_FMR    REG32(CAN1_BASE + 0x200)
#define CAN_FM1R   REG32(CAN1_BASE + 0x204)
#define CAN_FS1R   REG32(CAN1_BASE + 0x20C)
#define CAN_FFA1R  REG32(CAN1_BASE + 0x214)
#define CAN_FA1R   REG32(CAN1_BASE + 0x21C)
#define CAN_FR1(b) REG32(CAN1_BASE + 0x240 + 8u * (b))
#define CAN_FR2(b) REG32(CAN1_BASE + 0x244 + 8u * (b))
#define MCR_INRQ   (1u << 0)
#define MCR_SLEEP  (1u << 1)
#define MCR_ABOM   (1u << 6)
#define MSR_INAK   (1u << 0)
#define MSR_SLAK   (1u << 1)
#define TIR_TXRQ   (1u << 0)
#define RFR_RFOM   (1u << 5)
#define IER_FMPIE0 (1u << 1)
#define IER_FMPIE1 (1u << 4)
#define RCC_APB1ENR_CAN1EN (1u << 25)
#define CAN1_RX0_IRQn 20
#define CAN1_RX1_IRQn 21
#define ECHO_ID 0x202u

struct frame { uint32_t ri, rdt, lo, hi; uint8_t fifo; };
static struct frame ring[8];
static volatile uint32_t head, tail;
volatile uint32_t received, echoed;

static void putc_(char c)
{
    while (!(USART1->SR & USART_SR_TXE)) { }
    USART1->DR = (uint8_t)c;
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

static void drain(uint32_t f)
{
    while (CAN_RFR(f) & 3u) {                      /* FMP: messages pending */
        struct frame *e = &ring[head & 7u];
        e->ri = CAN_RIR(f);
        e->rdt = CAN_RDTR(f);
        e->lo = CAN_RDLR(f);
        e->hi = CAN_RDHR(f);
        e->fifo = (uint8_t)f;
        head++;
        CAN_RFR(f) = RFR_RFOM;                     /* release the output mailbox */
    }
}

void CAN1_RX0_IRQHandler(void) { drain(0); }
void CAN1_RX1_IRQHandler(void) { drain(1); }

static void can1_init(void)
{
    gpio_af(GPIOB, 8, 9);                          /* CAN1_RX */
    gpio_af(GPIOB, 9, 9);                          /* CAN1_TX */
    gpio_pull(GPIOB, 8, GPIO_PULL_UP);
    CAN_MCR = MCR_INRQ | MCR_SLEEP;                /* request initialization (we start in sleep mode) */
    while (!(CAN_MSR & MSR_INAK)) { }
    CAN_MCR = MCR_INRQ | MCR_ABOM;                 /* leave sleep, automatic bus-off recovery */
    while (CAN_MSR & MSR_SLAK) { }
    CAN_BTR = (1u << 20) | (12u << 16) | 1u;       /* TS2 1, TS1 12, BRP 1: 500 kbit/s at PCLK1 = 16 MHz */

    CAN_FMR |= 1u;                                 /* FINIT */
    CAN_FS1R = 1u << 0;                            /* bank 0: 32 bit, bank 1: 16 bit */
    CAN_FM1R = 1u << 1;                            /* bank 0: mask, bank 1: identifier list */
    CAN_FFA1R = 1u << 1;                           /* bank 1 -> FIFO 1 */
    CAN_FR1(0) = 0x100u << 21;                     /* STID 0x100, IDE 0, RTR 0 */
    CAN_FR2(0) = (0x7FFu << 21) | 6u;              /* compare STID, IDE, RTR */
    CAN_FR1(1) = ((0x301u << 5) << 16) | (0x300u << 5);
    CAN_FR2(1) = ((0x303u << 5) << 16) | (0x302u << 5);
    CAN_FA1R = 3u;
    CAN_FMR &= ~1u;

    CAN_IER = IER_FMPIE0 | IER_FMPIE1;
    NVIC_EnableIRQ(CAN1_RX0_IRQn);
    NVIC_EnableIRQ(CAN1_RX1_IRQn);
    CAN_MCR &= ~MCR_INRQ;                          /* normal mode after 11 recessive bits */
    while (CAN_MSR & MSR_INAK) { }
}

static void echo(const struct frame *e)
{
    uint32_t tsr = CAN_TSR;
    if (!(tsr & (7u << 26)))                       /* no empty mailbox */
        return;
    uint32_t k = (tsr >> 24) & 3u;                 /* CODE: next free mailbox */
    CAN_TDTR(k) = e->rdt & 0xFu;
    CAN_TDLR(k) = e->lo;
    CAN_TDHR(k) = e->hi;
    CAN_TIR(k) = (ECHO_ID << 21) | TIR_TXRQ;
}

static void tx_status(void)
{
    uint32_t tsr = CAN_TSR;
    for (uint32_t k = 0; k < 3u; k++) {
        uint32_t s = tsr >> (8u * k);
        if (!(s & 1u))                             /* RQCP */
            continue;
        CAN_TSR = 1u << (8u * k);                  /* clear RQCP, TXOK, ALST, TERR */
        if (s & 2u) {
            echoed++;
            puts_("TX 202\r\n");
        } else {
            puts_("TERR\r\n");
        }
    }
}

int main(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN | RCC_AHB1ENR_GPIOBEN;
    RCC_APB2ENR |= RCC_APB2ENR_USART1EN;
    RCC_APB1ENR |= RCC_APB1ENR_CAN1EN;
    gpio_af(GPIOA, 9, 7);                          /* USART1_TX */
    USART1->BRR = USART_BRR(HSI_HZ, 115200u);
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE;
    can1_init();
    __enable_irq();
    puts_("CAN ECHO STM32\r\n");

    for (;;) {
        tx_status();
        if (tail == head)
            continue;
        const struct frame *e = &ring[tail & 7u];
        if (e->fifo == 0)
            echo(e);                               /* first: the echo goes out while we print */
        received++;
        uint32_t dlc = e->rdt & 0xFu;
        puts_("RX ");
        puthex(e->ri >> 21, 3);
        puts_(" [");
        putc_((char)('0' + dlc));
        putc_(']');
        for (uint32_t i = 0; i < dlc && i < 8u; i++) {
            putc_(' ');
            puthex((i < 4u ? e->lo : e->hi) >> (8u * (i & 3u)), 2);
        }
        puts_(" F");
        putc_((char)('0' + e->fifo));
        puthex((e->rdt >> 8) & 0xFFu, 1);
        puts_("\r\n");
        tail++;
    }
}
