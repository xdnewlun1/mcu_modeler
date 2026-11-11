/* clock120_uart: STM32F2 clock tree at its 120 MHz maximum (RM0033 5.2, ST's SystemInit for the STM32F2 with an 8 MHz
 * HSE: PLL M=8 N=240 P=2 Q=5 -> VCO 240 MHz, SYSCLK 120 MHz, PLL48CLK 48 MHz; AHB /1, APB1 /4 = 30 MHz, APB2 /2 =
 * 60 MHz; FLASH_ACR 3 wait states, RM0033 Table 3 at 2.7-3.6 V), then
 *   - USART1 (PA9/PA10, AF7) at 115200 from PCLK2 = 60 MHz: prints the DBGMCU IDCODE and the PWR_CR / RCC_AHB1ENR
 *     read back after writing the STM32F4-only bits PWR_CR.VOS (bit 14) and RCC_AHB1ENR.CCMDATARAMEN (bit 20)
 *     (reserved on the F2: both read 0), then echoes received bytes;
 *   - TIM2 (APB1 timer clock 2 x 30 = 60 MHz) with PSC = 59 (1 MHz) and ARR = 999: TIM2_IRQHandler (IRQ 28) toggles
 *     PB0 every millisecond. */
#include "stm32f405.h"

#define DBGMCU_IDCODE REG32(0xE0042000u)
#define FLASH_ACR_LATENCY_3WS 3u

static void clock_120mhz(void)
{
    RCC_CR |= RCC_CR_HSEON;
    while (!(RCC_CR & RCC_CR_HSERDY)) { }
    RCC_PLLCFGR = 8u | (240u << 6) | (0u << 16) | RCC_PLLCFGR_SRC_HSE | (5u << 24);   /* M=8 N=240 P=2 Q=5 */
    RCC_CR |= RCC_CR_PLLON;
    while (!(RCC_CR & RCC_CR_PLLRDY)) { }
    FLASH_ACR = FLASH_ACR_LATENCY_3WS | FLASH_ACR_PRFTEN | FLASH_ACR_ICEN | FLASH_ACR_DCEN;
    RCC_CFGR = RCC_CFGR_PPRE1_DIV4 | RCC_CFGR_PPRE2_DIV2 | RCC_CFGR_SW_PLL;
    while ((RCC_CFGR & RCC_CFGR_SWS_MASK) != RCC_CFGR_SWS_PLL) { }
}

static void putc1(char c)
{
    while (!(USART1->SR & USART_SR_TXE)) { }
    USART1->DR = (uint8_t)c;
}

static void puts1(const char *s)
{
    while (*s)
        putc1(*s++);
}

static void hex32(uint32_t v)
{
    for (int i = 28; i >= 0; i -= 4)
        putc1("0123456789ABCDEF"[(v >> i) & 0xF]);
}

void TIM2_IRQHandler(void)
{
    TIM2->SR = ~TIM_SR_UIF;
    GPIOB->ODR ^= 1u;
}

int main(void)
{
    clock_120mhz();
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN | RCC_AHB1ENR_GPIOBEN;
    RCC_APB1ENR |= RCC_APB1ENR_TIM2EN | RCC_APB1ENR_PWREN;
    RCC_APB2ENR |= RCC_APB2ENR_USART1EN;
    gpio_mode(GPIOB, 0, GPIO_MODE_OUTPUT);
    gpio_af(GPIOA, 9, 7);
    gpio_af(GPIOA, 10, 7);
    gpio_pull(GPIOA, 10, GPIO_PULL_UP);
    USART1->BRR = USART_BRR(60000000u, 115200u);
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;

    PWR_CR |= PWR_CR_VOS;                          /* STM32F4 regulator scale bit: reserved on the F2 */
    RCC_AHB1ENR |= 1u << 20;                       /* STM32F4 CCMDATARAMEN: reserved on the F2 */
    puts1("F2 120MHz id=");
    hex32(DBGMCU_IDCODE);
    puts1(" pwr=");
    hex32(PWR_CR);
    puts1(" ahb1=");
    hex32(RCC_AHB1ENR);
    puts1("\r\n");

    TIM2->PSC = 60u - 1u;                          /* 60 MHz / 60 = 1 MHz */
    TIM2->ARR = 1000u - 1u;                        /* 1 ms */
    TIM2->EGR = TIM_EGR_UG;
    TIM2->SR = 0;
    TIM2->DIER = TIM_DIER_UIE;
    NVIC_EnableIRQ(TIM2_IRQn);
    TIM2->CR1 = TIM_CR1_CEN;
    __enable_irq();

    for (;;) {
        if (USART1->SR & USART_SR_RXNE)
            putc1((char)USART1->DR);
    }
}
