/* STM32F103T6 rtc_alarm: RTC on the LSE (32.768 kHz), prescaler 32767 -> 1 Hz counter.  Enables PWR/BKP clocks and
 * PWR_CR.DBP, starts the LSE, selects it (BDCR.RTCSEL = 01, RTCEN), waits for RSF, enters configuration mode (CNF) to
 * load PRL = 32767, CNT = 0 and ALR = 2, leaves it and waits for RTOFF.  The second interrupt (SECIE, RTC vector)
 * toggles PB0; the alarm reaches the RTC_Alarm vector through EXTI line 17 (rising edge) and sets PB1.  The backup
 * register DR1 counts the boots.  The core sleeps in WFI. */
#include "stm32f103.h"

volatile uint32_t seconds, alarms;

void RTC_IRQHandler(void)
{
    if (RTC_CRL & RTC_CRL_SECF) {
        RTC_CRL &= ~RTC_CRL_SECF;
        seconds++;
        GPIO_ODR(GPIOB_BASE) ^= 1u;
    }
}

void RTC_Alarm_IRQHandler(void)
{
    EXTI_PR = 1u << 17;
    RTC_CRL &= ~RTC_CRL_ALRF;
    alarms++;
    GPIO_BSRR(GPIOB_BASE) = 1u << 1;
}

static void rtc_wait_write(void)
{
    while (!(RTC_CRL & RTC_CRL_RTOFF))
        ;
}

int main(void)
{
    RCC_APB2ENR |= RCC_APB2ENR_IOPBEN;
    RCC_APB1ENR |= RCC_APB1ENR_PWREN | RCC_APB1ENR_BKPEN;
    GPIO_CONFIG(GPIOB_BASE, 0, GPIO_OUT_PP_2MHZ);
    GPIO_CONFIG(GPIOB_BASE, 1, GPIO_OUT_PP_2MHZ);
    PWR_CR |= PWR_CR_DBP;
    BKP_DR(1) = BKP_DR(1) + 1;
    RCC_BDCR |= RCC_BDCR_LSEON;
    while (!(RCC_BDCR & RCC_BDCR_LSERDY))
        ;
    RCC_BDCR |= RCC_BDCR_RTCSEL_LSE | RCC_BDCR_RTCEN;
    RTC_CRL &= ~RTC_CRL_RSF;
    while (!(RTC_CRL & RTC_CRL_RSF))
        ;
    rtc_wait_write();
    RTC_CRL |= RTC_CRL_CNF;
    RTC_PRLH = 0;
    RTC_PRLL = 32767;
    RTC_CNTH = 0;
    RTC_CNTL = 0;
    RTC_ALRH = 0;
    RTC_ALRL = 2;
    RTC_CRL &= ~RTC_CRL_CNF;
    rtc_wait_write();
    RTC_CRH = RTC_CRH_SECIE;
    EXTI_RTSR |= 1u << 17;
    EXTI_IMR |= 1u << 17;
    NVIC_ENABLE(RTC_IRQn);
    NVIC_ENABLE(RTC_Alarm_IRQn);
    __asm__ volatile("cpsie i");
    for (;;)
        __asm__ volatile("wfi");
}
