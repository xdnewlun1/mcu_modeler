/* STM32F103T6 ADC + DMA: ADC1 scans PA0 (IN0), PA1 (IN1) and the temperature sensor (IN16) in continuous mode;
 * DMA1 channel 1 (the ADC1 request) moves each result into adc_buf[] in circular mode, and its transfer-complete
 * interrupt (one per sequence) copies the three results to results[], counts the sequences in tc_count and toggles PB5.
 * Clocks: HSE 8 MHz x 9 = 72 MHz, APB2 72 MHz, ADCPRE /6 -> ADCCLK 12 MHz.  Before converting, the ADC is powered up
 * (tSTAB), its calibration registers reset (RSTCAL) and calibrated (CAL); the calibration code read from DR is kept in
 * cal_code.  Sampling: IN0/IN1 55.5 cycles, IN16 239.5 cycles (the sensor needs >= 17.1 us), so one sequence takes
 * (55.5 + 12.5) x 2 + (239.5 + 12.5) = 388 ADCCLK = 32.33 us. */
#include "stm32f103.h"

#define N_CH 3
#define DMA_CCR_TCIE_ (1u << 1)
#define DMA_ISR_TCIF1 (1u << 1)
#define DMA_IFCR_CGIF1 (1u << 0)

volatile uint16_t adc_buf[N_CH];            /* DMA target, rewritten every sequence */
volatile uint16_t results[N_CH];            /* snapshot taken in the TC interrupt */
volatile uint32_t tc_count;
volatile uint32_t cal_code;

void DMA1_Channel1_IRQHandler(void)
{
    if (DMA1_ISR & DMA_ISR_TCIF1) {
        DMA1_IFCR = DMA_IFCR_CGIF1;
        for (int i = 0; i < N_CH; i++)
            results[i] = adc_buf[i];
        tc_count++;
        GPIO_ODR(GPIOB_BASE) ^= 1u << 5;
    }
}

int main(void)
{
    clock_72mhz();
    RCC_AHBENR |= RCC_AHBENR_DMA1EN;
    RCC_APB2ENR |= RCC_APB2ENR_IOPAEN | RCC_APB2ENR_IOPBEN | RCC_APB2ENR_ADC1EN;
    GPIO_CONFIG(GPIOA_BASE, 0, GPIO_IN_ANALOG);
    GPIO_CONFIG(GPIOA_BASE, 1, GPIO_IN_ANALOG);
    GPIO_CONFIG(GPIOB_BASE, 5, GPIO_OUT_PP_2MHZ);

    /* power up, wait tSTAB (1 us = 72 cycles), reset the calibration, calibrate */
    ADC_CR2(ADC1_BASE) = ADC_CR2_ADON;
    for (volatile int i = 0; i < 50; i++)
        ;
    ADC_CR2(ADC1_BASE) |= ADC_CR2_RSTCAL;
    while (ADC_CR2(ADC1_BASE) & ADC_CR2_RSTCAL)
        ;
    ADC_CR2(ADC1_BASE) |= ADC_CR2_CAL;
    while (ADC_CR2(ADC1_BASE) & ADC_CR2_CAL)
        ;
    cal_code = ADC_DR(ADC1_BASE);

    ADC_SMPR2(ADC1_BASE) = (5u << 0) | (5u << 3);         /* SMP0, SMP1 = 55.5 cycles */
    ADC_SMPR1(ADC1_BASE) = 7u << 18;                       /* SMP16 = 239.5 cycles */
    ADC_SQR1(ADC1_BASE) = (uint32_t)(N_CH - 1) << 20;      /* L = 2: three conversions */
    ADC_SQR3(ADC1_BASE) = 0u | (1u << 5) | (16u << 10);    /* SQ1 = IN0, SQ2 = IN1, SQ3 = IN16 */
    ADC_CR1(ADC1_BASE) = ADC_CR1_SCAN;

    DMA1_CPAR(1) = (uint32_t)&ADC_DR(ADC1_BASE);
    DMA1_CMAR(1) = (uint32_t)adc_buf;
    DMA1_CNDTR(1) = N_CH;
    DMA1_CCR(1) = DMA_CCR_MINC | DMA_CCR_PSIZE_16 | DMA_CCR_MSIZE_16 | DMA_CCR_CIRC | DMA_CCR_TCIE_;
    DMA1_CCR(1) |= DMA_CCR_EN;
    NVIC_ENABLE(DMA1_Channel1_IRQn);

    /* CONT + DMA + temperature sensor, software trigger (EXTSEL = 111 with EXTTRIG); changing these bits together with
     * ADON = 1 does not start a conversion, SWSTART does */
    ADC_CR2(ADC1_BASE) = ADC_CR2_ADON | ADC_CR2_CONT | ADC_CR2_DMA | ADC_CR2_TSVREFE | ADC_CR2_EXTSEL_SWSTART
                         | ADC_CR2_EXTTRIG;
    ADC_CR2(ADC1_BASE) |= ADC_CR2_SWSTART;
    for (;;)
        __asm__ volatile("wfi");
}
