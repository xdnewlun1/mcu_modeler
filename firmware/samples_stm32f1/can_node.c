/* STM32F103T6 can_node: chip A of boards/stm32f103_stm32f405_can.yaml (peer: the STM32F405 running
 * firmware/samples_stm32/can_echo.c, which echoes standard ID 0x100 frames as ID 0x202).  bxCAN on PA11 (CAN_RX) /
 * PA12 (CAN_TX), 500 kbit/s at PCLK1 = 36 MHz (72 MHz from HSE x 9; BRP 4 -> 9 MHz tq, 1 + 13 + 4 = 18 tq).
 * Every 10 ms mailbox 0 sends ID 0x100, DLC 4 (counter, 'F', '1'); filter bank 0 accepts ID 0x202 into FIFO 0; the
 * USB_LP_CAN1_RX0 handler counts echoes and checks their data; USART1 prints "TX n" / "ECHO n". */
#include "console.h"

#define CAN_MCR    REG32(CAN_BASE + 0x000)
#define CAN_MSR    REG32(CAN_BASE + 0x004)
#define CAN_TSR    REG32(CAN_BASE + 0x008)
#define CAN_RF0R   REG32(CAN_BASE + 0x00C)
#define CAN_IER    REG32(CAN_BASE + 0x014)
#define CAN_BTR    REG32(CAN_BASE + 0x01C)
#define CAN_TI0R   REG32(CAN_BASE + 0x180)
#define CAN_TDT0R  REG32(CAN_BASE + 0x184)
#define CAN_TDL0R  REG32(CAN_BASE + 0x188)
#define CAN_TDH0R  REG32(CAN_BASE + 0x18C)
#define CAN_RI0R   REG32(CAN_BASE + 0x1B0)
#define CAN_RDT0R  REG32(CAN_BASE + 0x1B4)
#define CAN_RDL0R  REG32(CAN_BASE + 0x1B8)
#define CAN_FMR    REG32(CAN_BASE + 0x200)
#define CAN_FM1R   REG32(CAN_BASE + 0x204)
#define CAN_FS1R   REG32(CAN_BASE + 0x20C)
#define CAN_FFA1R  REG32(CAN_BASE + 0x214)
#define CAN_FA1R   REG32(CAN_BASE + 0x21C)
#define CAN_F0R1   REG32(CAN_BASE + 0x240)
#define CAN_F0R2   REG32(CAN_BASE + 0x244)

volatile uint32_t sent, echoes, bad, ticks, last_data;

void USB_LP_CAN1_RX0_IRQHandler(void)
{
    while (CAN_RF0R & 3u) {
        uint32_t id = CAN_RI0R >> 21, data = CAN_RDL0R;
        if (id != 0x202u || (CAN_RDT0R & 0xFu) != 4u)
            bad++;
        last_data = data;
        echoes++;
        CAN_RF0R = 1u << 5;
    }
}

void SysTick_Handler(void) { ticks++; }

int main(void)
{
    clock_72mhz();
    console_init(72000000u, 115200u);
    RCC_APB1ENR |= RCC_APB1ENR_CANEN;
    GPIO_CONFIG(GPIOA_BASE, 11, GPIO_IN_PULL);
    GPIO_BSRR(GPIOA_BASE) = 1u << 11;
    GPIO_CONFIG(GPIOA_BASE, 12, GPIO_AF_PP_50MHZ);
    CAN_MCR = 1u;                                    /* INRQ, leave sleep */
    while (!(CAN_MSR & 1u))
        ;
    CAN_MCR = 1u | (1u << 6);                        /* + ABOM */
    CAN_BTR = (3u << 20) | (12u << 16) | 3u;         /* TS2 = 4, TS1 = 13, BRP = 4 */
    CAN_FMR |= 1u;
    CAN_FM1R = 0;
    CAN_FS1R = 1u;
    CAN_FFA1R = 0;
    CAN_F0R1 = 0x202u << 21;
    CAN_F0R2 = (0x7FFu << 21) | 6u;
    CAN_FA1R = 1u;
    CAN_FMR &= ~1u;
    CAN_IER = 1u << 1;
    NVIC_ENABLE(USB_LP_CAN1_RX0_IRQn);
    CAN_MCR = 1u << 6;
    while (CAN_MSR & 1u)
        ;
    SYST_RVR = 720000 - 1;                           /* 10 ms */
    SYST_CVR = 0;
    SYST_CSR = 7;
    enable_irq();
    puts_("CAN NODE F103\r\n");
    uint32_t t = 0, shown = 0;
    for (;;) {
        if (ticks != t && (CAN_TSR & (1u << 26))) {
            t = ticks;
            CAN_TDT0R = 4u;
            CAN_TDL0R = (sent >> 8 & 0xFFu) | (sent & 0xFFu) << 8 | ('F' << 16) | ('1' << 24);
            CAN_TDH0R = 0;
            CAN_TI0R = (0x100u << 21) | 1u;
            sent++;
            puts_("TX ");
            putdec(sent);
            puts_("\r\n");
        }
        if (echoes != shown) {
            shown = echoes;
            puts_("ECHO ");
            putdec(shown);
            puts_("\r\n");
        }
        __asm__ volatile("wfi");
    }
}
