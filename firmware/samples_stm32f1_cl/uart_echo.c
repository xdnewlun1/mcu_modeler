/* STM32F105/F107 uart_echo: the connectivity-line reference clock tree (stm32f107.h clock_72mhz_pll2: HSE 25 MHz ->
 * PREDIV2 /5 -> PLL2 x8 = 40 MHz -> PREDIV1 /5 -> PLL x9 = 72 MHz), USART1 on PA9/PA10 at 115200 baud (BRR = 625 at
 * PCLK2 72 MHz).  Prints the device ID (DBGMCU_IDCODE DEV_ID 0x418) and the clock registers, then echoes every received
 * byte uppercased (RXNE interrupt into a ring buffer).  Run:
 *   chipem run firmware/samples_stm32f1_cl/build/uart_echo.elf --chip stm32f105rc --for 3ms --at 2ms:USART1=hi */
#include "console.h"

static volatile uint8_t ring[64];
static volatile uint32_t head, tail;
volatile uint32_t rx_count;

void USART1_IRQHandler(void)
{
    if (USART_SR(USART1_BASE) & USART_SR_RXNE) {
        ring[head & 63u] = (uint8_t)USART_DR(USART1_BASE);       /* reading DR clears RXNE */
        head++;
    }
}

int main(void)
{
    clock_72mhz_pll2(0);
    console_init(72000000u, 115200u);
    USART_CR1(USART1_BASE) |= USART_CR1_RXNEIE;
    NVIC_ENABLE(USART1_IRQn);
    enable_irq();
    puts_("CL DEV ");
    puthex(REG32(0xE0042000u) & 0xFFFu, 3);
    puts_(" CR ");
    puthex(RCC_CR, 8);
    puts_(" CFGR ");
    puthex(RCC_CFGR, 8);
    puts_(" CFGR2 ");
    puthex(RCC_CFGR2, 8);
    puts_("\r\n72 MHz\r\n");
    for (;;) {
        while (tail == head)
            __asm__ volatile("wfi");
        char c = (char)ring[tail & 63u];
        tail++;
        rx_count++;
        if (c >= 'a' && c <= 'z')
            c = (char)(c - 32);
        putc_(c);
    }
}
