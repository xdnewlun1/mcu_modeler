/* adc_tim_trigger: ADC1 channel 0 (PA0, analog mode) converted on every TIM3 update (TIM3 TRGO, MMS = 010) at
 * 1 kHz, at the reset clock (HSI 16 MHz, APB1 = APB2 = 16 MHz).  TIM3: PSC = 15 (1 MHz counter), ARR = 999 (1 ms).
 * ADC1: ADCCLK = PCLK2 / 2 = 8 MHz, SMP0 = 84 cycles, 12-bit right-aligned, EXTSEL = 1000 (TIM3_TRGO), EXTEN = 01
 * (rising edge), EOCS = 1, EOCIE.  ADC_IRQHandler (IRQ 18) reads DR (which clears EOC); the main loop prints
 * "adc <code>" on USART1 (PA9, 115200 8N1) for each new result (10 characters = 0.87 ms, less than the period).
 * Feed the input with `chipem run ... --adc PA0=1.65V` (or ADC1_IN0=2048). */
#include "stm32f405.h"

#define RCC_APB1ENR_TIM3EN  (1u << 1)
#define RCC_APB2ENR_ADC1EN  (1u << 8)
#define TIM3 ((TIM_TypeDef *)0x40000400u)
#define TIM_CR2_MMS_UPDATE  (2u << 4)

typedef struct {
    volatile uint32_t SR, CR1, CR2, SMPR1, SMPR2, JOFR[4], HTR, LTR, SQR1, SQR2, SQR3, JSQR, JDR[4], DR;
} ADC_TypeDef;
#define ADC1 ((ADC_TypeDef *)0x40012000u)
#define ADC_CCR              REG32(0x40012304u)
#define ADC_SR_EOC           (1u << 1)
#define ADC_SR_OVR           (1u << 5)
#define ADC_CR1_EOCIE        (1u << 5)
#define ADC_CR2_ADON         (1u << 0)
#define ADC_CR2_EOCS         (1u << 10)
#define ADC_CR2_EXTSEL_TIM3_TRGO (8u << 24)
#define ADC_CR2_EXTEN_RISING (1u << 28)
#define ADC_SMP_84           4u
#define ADC_IRQn             18

volatile uint32_t samples, last_code, overruns;

void ADC_IRQHandler(void)
{
    if (ADC1->SR & ADC_SR_OVR) {
        overruns++;
        ADC1->SR = ~ADC_SR_OVR;                    /* rc_w0 */
    }
    if (ADC1->SR & ADC_SR_EOC) {
        last_code = ADC1->DR;                      /* clears EOC */
        samples++;
    }
}

static void usart1_putc(char c)
{
    while (!(USART1->SR & USART_SR_TXE)) { }
    USART1->DR = (uint8_t)c;
}

static void usart1_puts(const char *s)
{
    while (*s)
        usart1_putc(*s++);
}

static void usart1_putu(uint32_t v)
{
    char buf[11];
    int i = 0;
    do {
        buf[i++] = (char)('0' + v % 10u);
        v /= 10u;
    } while (v);
    while (i)
        usart1_putc(buf[--i]);
}

int main(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN;
    RCC_APB1ENR |= RCC_APB1ENR_TIM3EN;
    RCC_APB2ENR |= RCC_APB2ENR_USART1EN | RCC_APB2ENR_ADC1EN;
    gpio_af(GPIOA, 9, 7);                          /* USART1_TX */
    gpio_mode(GPIOA, 0, GPIO_MODE_ANALOG);         /* PA0 = ADC123_IN0 */
    USART1->BRR = USART_BRR(HSI_HZ, 115200u);
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE;

    TIM3->PSC = 16u - 1u;                          /* 1 MHz counter */
    TIM3->ARR = 1000u - 1u;                        /* 1 ms */
    TIM3->EGR = TIM_EGR_UG;                        /* load PSC; also a TRGO pulse (MMS = 000), before the ADC listens */
    TIM3->SR = 0;
    TIM3->CR2 = TIM_CR2_MMS_UPDATE;                /* TRGO = update event */

    ADC_CCR = 0;                                   /* ADCPRE = /2 */
    ADC1->SMPR2 = ADC_SMP_84 << 0;                 /* channel 0: 84 sampling cycles */
    ADC1->SQR1 = 0;                                /* one conversion */
    ADC1->SQR3 = 0;                                /* SQ1 = channel 0 */
    ADC1->CR1 = ADC_CR1_EOCIE;
    ADC1->CR2 = ADC_CR2_ADON | ADC_CR2_EOCS | ADC_CR2_EXTSEL_TIM3_TRGO | ADC_CR2_EXTEN_RISING;
    NVIC_ClearPendingIRQ(ADC_IRQn);
    NVIC_EnableIRQ(ADC_IRQn);
    __enable_irq();
    TIM3->CR1 = TIM_CR1_CEN;

    usart1_puts("adc ready\r\n");
    uint32_t seen = 0;
    for (;;) {
        __asm volatile ("cpsid i" ::: "memory");
        uint32_t n = samples, code = last_code;
        __asm volatile ("cpsie i" ::: "memory");
        if (n != seen) {
            seen = n;
            usart1_puts("adc ");
            usart1_putu(code);
            usart1_puts("\r\n");
        } else {
            __WFI();
        }
    }
}
