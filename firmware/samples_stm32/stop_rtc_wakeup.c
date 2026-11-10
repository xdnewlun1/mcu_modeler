/* stop_rtc_wakeup: low-power modes (RM0090 5.3). The clock tree is the HAL one (HSE 8 MHz, PLL 168 MHz, APB2 84 MHz).
 * The RTC wakeup timer (LSE / 16, WUTR = 1023: every 0.5 s) drives EXTI line 22 -> RTC_WKUP IRQ. The CPU enters Stop
 * mode (SLEEPDEEP, PDDS = 0, WFI) three times; after each wake-up it reports the clock it woke on (Stop exits on HSI
 * with the PLL off, so it re-initialises the PLL before printing on USART1 at 115200 8N1, PA9):
 *   "boot\r\n", "stop 1 woke on HSI, PLL off\r\n", ... "stop 3 ...", "standby\r\n"
 * then it enters Standby (PDDS = 1). The next RTC wake-up resets the chip; PWR_CSR.SBF tells the new boot it came out
 * of Standby: it prints "standby wake-up (SBF WUF)\r\n" and sleeps in Stop for good (no more output). PB0 is set
 * while the CPU runs and cleared just before each Stop / Standby entry. */
#include "stm32f405.h"

#define RCC_BDCR      REG32(RCC_BASE + 0x70)
#define PWR_CSR       REG32(0x40007004u)
#define PWR_CR_LPDS   (1u << 0)
#define PWR_CR_PDDS   (1u << 1)
#define PWR_CR_CWUF   (1u << 2)
#define PWR_CR_CSBF   (1u << 3)
#define PWR_CR_DBP    (1u << 8)
#define PWR_CSR_WUF   (1u << 0)
#define PWR_CSR_SBF   (1u << 1)
#define RTC_BASE      0x40002800u
#define RTC_CR        REG32(RTC_BASE + 0x08)
#define RTC_ISR       REG32(RTC_BASE + 0x0C)
#define RTC_WUTR      REG32(RTC_BASE + 0x14)
#define RTC_WPR       REG32(RTC_BASE + 0x24)
#define RTC_ISR_WUTWF (1u << 2)
#define RTC_ISR_INIT  (1u << 7)
#define RTC_ISR_WUTF  (1u << 10)
#define RTC_CR_WUTE   (1u << 10)
#define RTC_CR_WUTIE  (1u << 14)
#define RTC_WKUP_IRQn 3
#define SCB_SCR       REG32(0xE000ED10u)
#define SCR_SLEEPDEEP (1u << 2)

volatile uint32_t wakeups;

void RTC_WKUP_IRQHandler(void)
{
    RTC_ISR = ~(RTC_ISR_WUTF | RTC_ISR_INIT) & 0xFFFFu;   /* rc_w0 flag, keep INIT = 0 */
    EXTI_PR = 1u << 22;
    wakeups++;
}

static void clock_168mhz(void)
{
    PWR_CR |= PWR_CR_VOS;
    RCC_CR |= RCC_CR_HSEON;
    while (!(RCC_CR & RCC_CR_HSERDY)) { }
    RCC_PLLCFGR = 8u | (336u << 6) | (0u << 16) | RCC_PLLCFGR_SRC_HSE | (7u << 24);   /* M=8 N=336 P=2 Q=7 */
    RCC_CR |= RCC_CR_PLLON;
    while (!(RCC_CR & RCC_CR_PLLRDY)) { }
    FLASH_ACR = FLASH_ACR_LATENCY_5WS | FLASH_ACR_PRFTEN | FLASH_ACR_ICEN | FLASH_ACR_DCEN;
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
    while (!(USART1->SR & USART_SR_TC)) { }       /* the last character must leave before Stop freezes USART1 */
}

static void enter_low_power(uint32_t pdds)
{
    GPIOB->ODR &= ~1u;
    PWR_CR = (PWR_CR & ~(PWR_CR_PDDS | PWR_CR_LPDS)) | pdds | PWR_CR_LPDS;
    SCB_SCR |= SCR_SLEEPDEEP;
    __WFI();
    SCB_SCR &= ~SCR_SLEEPDEEP;
    GPIOB->ODR |= 1u;
}

int main(void)
{
    RCC_APB1ENR |= RCC_APB1ENR_PWREN;
    clock_168mhz();
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN | RCC_AHB1ENR_GPIOBEN;
    RCC_APB2ENR |= RCC_APB2ENR_USART1EN;
    gpio_af(GPIOA, 9, 7);
    gpio_mode(GPIOB, 0, GPIO_MODE_OUTPUT);
    GPIOB->ODR |= 1u;
    USART1->BRR = USART_BRR(84000000u, 115200u);
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE;

    uint32_t from_standby = PWR_CSR & PWR_CSR_SBF;
    if (from_standby) {
        puts1((PWR_CSR & PWR_CSR_WUF) ? "standby wake-up (SBF WUF)\r\n" : "standby exit (SBF)\r\n");
        PWR_CR |= PWR_CR_CSBF | PWR_CR_CWUF | PWR_CR_DBP;
        RTC_ISR = ~(RTC_ISR_WUTF | RTC_ISR_INIT) & 0xFFFFu;  /* Stop entry is ignored while WUTF is set (RM0090 5.3.4) */
        for (;;)
            enter_low_power(0);                     /* Stop (EXTI line 22 is off after the reset: stays stopped) */
    }
    puts1("boot\r\n");

    PWR_CR |= PWR_CR_DBP;                           /* backup domain writes */
    RCC_BDCR |= 1u;                                 /* LSEON */
    while (!(RCC_BDCR & 2u)) { }
    RCC_BDCR |= (1u << 8) | (1u << 15);             /* RTCSEL = LSE, RTCEN */
    RTC_WPR = 0xCA;
    RTC_WPR = 0x53;
    RTC_CR &= ~RTC_CR_WUTE;
    while (!(RTC_ISR & RTC_ISR_WUTWF)) { }
    RTC_WUTR = 1023u;                               /* 32768 / 16 = 2048 Hz: 0.5 s */
    RTC_CR |= RTC_CR_WUTE | RTC_CR_WUTIE;           /* WUCKSEL = 000 */
    EXTI_IMR |= 1u << 22;
    EXTI_RTSR |= 1u << 22;
    NVIC_EnableIRQ(RTC_WKUP_IRQn);
    __enable_irq();

    for (uint32_t n = 1; n <= 3; n++) {
        enter_low_power(0);                         /* Stop */
        uint32_t sws = RCC_CFGR & RCC_CFGR_SWS_MASK;
        uint32_t pll = RCC_CR & RCC_CR_PLLON;
        clock_168mhz();                             /* Stop exits on HSI: restore the PLL */
        puts1("stop ");
        putc1((char)('0' + n));
        puts1(sws == 0 && !pll ? " woke on HSI, PLL off\r\n" : " woke on ?\r\n");
    }
    puts1("standby\r\n");
    PWR_CR |= PWR_CR_CWUF;
    enter_low_power(PWR_CR_PDDS);                   /* Standby: the RTC wake-up resets the chip */
    for (;;) { }
}
