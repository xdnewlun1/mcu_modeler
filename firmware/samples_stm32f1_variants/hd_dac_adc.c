/* STM32F103 high density: DAC channel 1 read back by ADC1, a TIM6-triggered DAC wave fed by DMA2 channel 3, and an
 * ADC3 scan moved by DMA2 channel 5.  HSE 8 MHz x 9 = 72 MHz, APB1 36 MHz, ADC clock 72 / 6 = 12 MHz.
 * 1. DAC_OUT1 (PA4, analog mode), TEN1 = 0: DHR12R1 = 0x800 -> DOR1 one APB1 cycle later (RM0008 12.3.5); ADC1
 *    channel 4 (the same pin) converts it: "DAC 2048 ADC1 nnnn\r\n" (VDDA = VREF+ = 3.3 V on both: about 2048).
 * 2. TEN1 = 1, TSEL1 = 000 (TIM6 TRGO = update, 10 kHz), DMAEN1: DMA2 channel 3 (request DAC1, RM0008 13.3.7
 *    Table 79) writes a 4-entry table circularly to DHR12R1; TCIE counts complete tables (vector 58) for 2 ms:
 *    "WAVE n\r\n" (about 5).
 * 3. ADC3 scan of channel 4 (PF6, LQFP144 only) and channel 0 (PA0), 239.5-cycle sampling, DMA mode: DMA2 channel 5
 *    (request ADC3) moves both results: "ADC3 aaaa bbbb\r\n" (set the inputs with --adc PF6=1.0 --adc PA0=2.0).
 * Prints "done\r\n" at the end. */
#include "f1x.h"
#include "console.h"

static const uint32_t wave[4] = {0, 1024, 2048, 4095};
static volatile uint16_t adc3_buf[2];
volatile uint32_t wave_tc, dac_code, adc1_code;

void DMA2_Channel3_IRQHandler(void)
{
    if (DMA2_ISR & (1u << 9)) {                     /* TCIF3 */
        DMA2_IFCR = 1u << 8;                        /* CGIF3 */
        wave_tc++;
    }
}

static void adc_on_and_calibrate(uint32_t b)
{
    ADC_CR2(b) = ADC_CR2_ADON;                      /* power up, then tSTAB */
    for (volatile int i = 0; i < 100; i++)
        ;
    ADC_CR2(b) |= ADC_CR2_RSTCAL;
    while (ADC_CR2(b) & ADC_CR2_RSTCAL)
        ;
    ADC_CR2(b) |= ADC_CR2_CAL;
    while (ADC_CR2(b) & ADC_CR2_CAL)
        ;
}

int main(void)
{
    clock_72mhz();
    console_init(72000000u, 115200u);
    RCC_APB2ENR |= RCC_APB2ENR_IOPAEN | RCC_APB2ENR_IOPFEN | RCC_APB2ENR_ADC1EN | RCC_APB2ENR_ADC3EN;
    RCC_APB1ENR |= RCC_APB1ENR_DACEN | RCC_APB1ENR_TIM6EN;
    RCC_AHBENR |= RCC_AHBENR_DMA2EN;
    GPIO_CONFIG(GPIOA_BASE, 4, GPIO_IN_ANALOG);
    GPIO_CONFIG(GPIOA_BASE, 0, GPIO_IN_ANALOG);
    GPIO_CONFIG(GPIOF_BASE, 6, GPIO_IN_ANALOG);

    /* 1. static DAC level, ADC1 channel 4 single conversion */
    DAC_CR = 1u;                                    /* EN1 */
    DAC_DHR12R1 = 0x800;
    for (volatile int i = 0; i < 10; i++)
        ;
    dac_code = DAC_DOR1;
    adc_on_and_calibrate(ADC1_BASE);
    ADC_SMPR2(ADC1_BASE) = 7u << (3 * 4);            /* channel 4: 239.5 cycles */
    ADC_SQR1(ADC1_BASE) = 0;                         /* 1 conversion */
    ADC_SQR3(ADC1_BASE) = 4;
    ADC_CR2(ADC1_BASE) |= ADC_CR2_EXTSEL_SWSTART | ADC_CR2_EXTTRIG;
    ADC_CR2(ADC1_BASE) |= ADC_CR2_SWSTART;
    while (!(ADC_SR(ADC1_BASE) & ADC_SR_EOC))
        ;
    adc1_code = ADC_DR(ADC1_BASE);
    puts_("DAC ");
    putdec(dac_code);
    puts_(" ADC1 ");
    putdec(adc1_code);
    puts_("\r\n");

    /* 2. TIM6-triggered wave through DMA2 channel 3 */
    DMA2_CPAR(3) = DAC_BASE + 0x08;                 /* DHR12R1 */
    DMA2_CMAR(3) = (uint32_t)wave;
    DMA2_CNDTR(3) = 4;
    DMA2_CCR(3) = DMA_CCR_DIR | DMA_CCR_CIRC | DMA_CCR_MINC | DMA_CCR_PSIZE_32 | DMA_CCR_MSIZE_32 | DMA_CCR_TCIE;
    DMA2_CCR(3) |= DMA_CCR_EN;
    NVIC_ENABLE(DMA2_Channel3_IRQn);
    DAC_CR = 0;                                     /* TSEL1 can only change with EN1 = 0 (12.5.1) */
    DAC_CR = (1u << 2) | (0u << 3) | (1u << 12);    /* TEN1, TSEL1 = TIM6 TRGO, DMAEN1 */
    DAC_CR |= 1u;                                   /* EN1 */
    TIM_PSC(TIM6_BASE) = 0;
    TIM_ARR(TIM6_BASE) = 7199;                      /* 72 MHz / 7200 = 10 kHz */
    TIM_CR2(TIM6_BASE) = 2u << 4;                   /* MMS = update -> TRGO */
    TIM_EGR(TIM6_BASE) = 1;
    enable_irq();
    TIM_CR1(TIM6_BASE) = 1;
    for (volatile uint32_t i = 0; i < 2000 * 72 / 6; i++)     /* about 2 ms */
        ;
    TIM_CR1(TIM6_BASE) = 0;
    DMA2_CCR(3) &= ~DMA_CCR_EN;
    puts_("WAVE ");
    putdec(wave_tc);
    puts_("\r\n");

    /* 3. ADC3 scan (channels 4, 0) -> DMA2 channel 5 */
    adc_on_and_calibrate(ADC3_BASE);
    ADC_SMPR2(ADC3_BASE) = (7u << (3 * 4)) | 7u;      /* channels 4 and 0: 239.5 cycles */
    ADC_SQR1(ADC3_BASE) = 1u << 20;                   /* L = 1: 2 conversions */
    ADC_SQR3(ADC3_BASE) = 4u | (0u << 5);
    ADC_CR1(ADC3_BASE) = ADC_CR1_SCAN;
    DMA2_CPAR(5) = ADC3_BASE + 0x4C;
    DMA2_CMAR(5) = (uint32_t)adc3_buf;
    DMA2_CNDTR(5) = 2;
    DMA2_CCR(5) = DMA_CCR_MINC | DMA_CCR_PSIZE_16 | DMA_CCR_MSIZE_16;
    DMA2_CCR(5) |= DMA_CCR_EN;
    ADC_CR2(ADC3_BASE) |= ADC_CR2_DMA | ADC_CR2_EXTSEL_SWSTART | ADC_CR2_EXTTRIG;
    ADC_CR2(ADC3_BASE) |= ADC_CR2_SWSTART;
    while (!(DMA2_ISR & (1u << 17)))                  /* TCIF5 */
        ;
    puts_("ADC3 ");
    putdec(adc3_buf[0]);
    puts_(" ");
    putdec(adc3_buf[1]);
    puts_("\r\ndone\r\n");
    for (;;)
        __asm__ volatile("wfi");
}
