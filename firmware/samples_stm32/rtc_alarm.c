/* rtc_alarm: RTC from the 32.768 kHz LSE, calendar set to 2024-02-28 23:59:58 (Wednesday), alarm A every second
 * (all fields masked) through EXTI line 17 -> RTC_Alarm IRQ. The CPU sleeps in WFI; each alarm toggles PB0 and
 * prints the time over USART1 (PA9, 115200 8N1, 16 MHz HSI): "RTC 2024-02-28 23:59:59\r\n", ... The first
 * boot also writes a magic word into RTC_BKP0R and reports "cold boot", a reset with the magic still there
 * reports "warm boot" (the backup domain survives system resets). */
#include "stm32f405.h"

#define RCC_BDCR      REG32(RCC_BASE + 0x70)
#define RCC_APB1ENR_PWREN (1u << 28)
#define PWR_CR_DBP    (1u << 8)
#define RTC_BASE      0x40002800u
#define RTC_TR        REG32(RTC_BASE + 0x00)
#define RTC_DR        REG32(RTC_BASE + 0x04)
#define RTC_CR        REG32(RTC_BASE + 0x08)
#define RTC_ISR       REG32(RTC_BASE + 0x0C)
#define RTC_ALRMAR    REG32(RTC_BASE + 0x1C)
#define RTC_WPR       REG32(RTC_BASE + 0x24)
#define RTC_BKP0R     REG32(RTC_BASE + 0x50)
#define RTC_ISR_INITF (1u << 6)
#define RTC_ISR_INIT  (1u << 7)
#define RTC_ISR_RSF   (1u << 5)
#define RTC_ISR_ALRAF (1u << 8)
#define RTC_ISR_ALRAWF (1u << 0)
#define RTC_CR_ALRAE  (1u << 8)
#define RTC_CR_ALRAIE (1u << 12)
#define RTC_Alarm_IRQn 41
#define MAGIC 0xB00710ADu

volatile uint32_t alarms;

void RTC_Alarm_IRQHandler(void)
{
    RTC_ISR = ~(RTC_ISR_ALRAF | RTC_ISR_INIT) & 0xFFFFu;   /* rc_w0 flag, keep INIT = 0 */
    EXTI_PR = 1u << 17;
    GPIOB->ODR ^= 1u;
    alarms++;
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

static void bcd2(unsigned v)
{
    putc1((char)('0' + ((v >> 4) & 0xF)));
    putc1((char)('0' + (v & 0xF)));
}

int main(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN | RCC_AHB1ENR_GPIOBEN;
    RCC_APB2ENR |= RCC_APB2ENR_USART1EN | RCC_APB2ENR_SYSCFGEN;
    RCC_APB1ENR |= RCC_APB1ENR_PWREN;
    gpio_af(GPIOA, 9, 7);
    gpio_mode(GPIOB, 0, GPIO_MODE_OUTPUT);
    USART1->BRR = USART_BRR(HSI_HZ, 115200u);
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE;

    PWR_CR |= PWR_CR_DBP;                          /* backup domain writes */
    if (RTC_BKP0R == MAGIC) {
        puts1("warm boot\r\n");
    } else {
        puts1("cold boot\r\n");
        RCC_BDCR |= 1u;                            /* LSEON */
        while (!(RCC_BDCR & 2u)) { }               /* LSERDY */
        RCC_BDCR |= (1u << 8) | (1u << 15);        /* RTCSEL = LSE, RTCEN */
        RTC_WPR = 0xCA;
        RTC_WPR = 0x53;
        RTC_ISR = RTC_ISR_INIT;
        while (!(RTC_ISR & RTC_ISR_INITF)) { }
        RTC_TR = 0x235958u;                        /* 23:59:58 */
        RTC_DR = 0x24u << 16 | 3u << 13 | 0x02u << 8 | 0x28u;   /* Wed 2024-02-28 */
        RTC_ISR &= ~RTC_ISR_INIT;
        RTC_CR &= ~RTC_CR_ALRAE;
        while (!(RTC_ISR & RTC_ISR_ALRAWF)) { }
        RTC_ALRMAR = 0x80808080u;                  /* MSK4..MSK1: every second */
        RTC_CR |= RTC_CR_ALRAE | RTC_CR_ALRAIE;
        RTC_WPR = 0xFF;                            /* lock again */
        RTC_BKP0R = MAGIC;
    }
    EXTI_IMR |= 1u << 17;
    EXTI_RTSR |= 1u << 17;
    NVIC_EnableIRQ(RTC_Alarm_IRQn);
    __enable_irq();

    uint32_t seen = 0;
    for (;;) {
        __WFI();
        if (alarms != seen) {
            seen = alarms;
            RTC_ISR &= ~RTC_ISR_RSF;
            while (!(RTC_ISR & RTC_ISR_RSF)) { }   /* shadow registers in sync */
            uint32_t tr = RTC_TR, dr = RTC_DR;      /* TR first: locks DR */
            puts1("RTC 20");
            bcd2(dr >> 16); putc1('-'); bcd2((dr >> 8) & 0x1F); putc1('-'); bcd2(dr & 0x3F);
            putc1(' ');
            bcd2((tr >> 16) & 0x3F); putc1(':'); bcd2((tr >> 8) & 0x7F); putc1(':'); bcd2(tr & 0x7F);
            puts1("\r\n");
        }
    }
}
