/* Minimal STM32F105/F107 (connectivity line, RM0008 Rev 21) register definitions for the CHIP_EM samples: bases from
 * RM0008 3.3 Table 3 / the STM32F107xx SVD, offsets and bits from the RM0008 register maps (as in ST's CMSIS
 * stm32f107xc.h).  GPIO/AFIO/USART/TIM/SPI/I2C/ADC/DMA blocks are the F103 ones (firmware/samples_stm32f1/stm32f103.h). */
#ifndef STM32F107_H
#define STM32F107_H
#include <stdint.h>

#define REG32(a) (*(volatile uint32_t *)(a))
#define REG16(a) (*(volatile uint16_t *)(a))

/* bases (RM0008 Table 3) */
#define TIM2_BASE    0x40000000u
#define TIM3_BASE    0x40000400u
#define TIM4_BASE    0x40000800u
#define TIM5_BASE    0x40000C00u
#define TIM6_BASE    0x40001000u
#define TIM7_BASE    0x40001400u
#define RTC_BASE     0x40002800u
#define WWDG_BASE    0x40002C00u
#define IWDG_BASE    0x40003000u
#define SPI2_BASE    0x40003800u
#define SPI3_BASE    0x40003C00u
#define USART2_BASE  0x40004400u
#define USART3_BASE  0x40004800u
#define UART4_BASE   0x40004C00u
#define UART5_BASE   0x40005000u
#define I2C1_BASE    0x40005400u
#define I2C2_BASE    0x40005800u
#define CAN1_BASE    0x40006400u
#define CAN2_BASE    0x40006800u
#define BKP_BASE     0x40006C00u
#define PWR_BASE     0x40007000u
#define DAC_BASE     0x40007400u
#define AFIO_BASE    0x40010000u
#define EXTI_BASE    0x40010400u
#define GPIOA_BASE   0x40010800u
#define GPIOB_BASE   0x40010C00u
#define GPIOC_BASE   0x40011000u
#define GPIOD_BASE   0x40011400u
#define GPIOE_BASE   0x40011800u
#define ADC1_BASE    0x40012400u
#define ADC2_BASE    0x40012800u
#define TIM1_BASE    0x40012C00u
#define SPI1_BASE    0x40013000u
#define USART1_BASE  0x40013800u
#define DMA1_BASE    0x40020000u
#define DMA2_BASE    0x40020400u
#define RCC_BASE     0x40021000u
#define FLASH_BASE   0x40022000u
#define CRC_BASE     0x40023000u
#define ETH_BASE     0x40028000u
#define OTG_FS_BASE  0x50000000u

/* RCC (RM0008 8.3) */
#define RCC_CR       REG32(RCC_BASE + 0x00)
#define RCC_CFGR     REG32(RCC_BASE + 0x04)
#define RCC_CIR      REG32(RCC_BASE + 0x08)
#define RCC_APB2RSTR REG32(RCC_BASE + 0x0C)
#define RCC_APB1RSTR REG32(RCC_BASE + 0x10)
#define RCC_AHBENR   REG32(RCC_BASE + 0x14)
#define RCC_APB2ENR  REG32(RCC_BASE + 0x18)
#define RCC_APB1ENR  REG32(RCC_BASE + 0x1C)
#define RCC_BDCR     REG32(RCC_BASE + 0x20)
#define RCC_CSR      REG32(RCC_BASE + 0x24)
#define RCC_AHBRSTR  REG32(RCC_BASE + 0x28)
#define RCC_CFGR2    REG32(RCC_BASE + 0x2C)
#define RCC_CR_HSION (1u << 0)
#define RCC_CR_HSEON (1u << 16)
#define RCC_CR_HSERDY (1u << 17)
#define RCC_CR_PLLON (1u << 24)
#define RCC_CR_PLLRDY (1u << 25)
#define RCC_CR_PLL2ON (1u << 26)
#define RCC_CR_PLL2RDY (1u << 27)
#define RCC_CR_PLL3ON (1u << 28)
#define RCC_CR_PLL3RDY (1u << 29)
#define RCC_CFGR_SW_PLL 2u
#define RCC_CFGR_SWS_MASK (3u << 2)
#define RCC_CFGR_SWS_PLL (2u << 2)
#define RCC_CFGR_PPRE1_DIV2 (4u << 8)
#define RCC_CFGR_ADCPRE_DIV6 (2u << 14)
#define RCC_CFGR_PLLSRC (1u << 16)                    /* PREDIV1 output */
#define RCC_CFGR_PLLMUL9 (7u << 18)                   /* 0111: x9 (RM0008 8.3.2; x4..x9, 1101 = x6.5) */
#define RCC_CFGR_OTGFSPRE (1u << 22)
#define RCC_CFGR_MCO_PLL3 (0xBu << 24)                /* 1011: PLL3 clock (for the Ethernet PHY) */
#define RCC_CFGR_MCO_XT1 (0xAu << 24)
#define RCC_CFGR2_PREDIV1(d) ((uint32_t)((d) - 1) << 0)
#define RCC_CFGR2_PREDIV2(d) ((uint32_t)((d) - 1) << 4)
#define RCC_CFGR2_PLL2MUL8 (6u << 8)                  /* 0110: x8 */
#define RCC_CFGR2_PLL3MUL10 (8u << 12)                /* 1000: x10 */
#define RCC_CFGR2_PREDIV1SRC_PLL2 (1u << 16)
#define RCC_AHBENR_DMA1EN (1u << 0)
#define RCC_AHBENR_DMA2EN (1u << 1)
#define RCC_AHBENR_CRCEN (1u << 6)
#define RCC_AHBENR_OTGFSEN (1u << 12)
#define RCC_AHBENR_ETHMACEN (1u << 14)
#define RCC_AHBENR_ETHMACTXEN (1u << 15)
#define RCC_AHBENR_ETHMACRXEN (1u << 16)
#define RCC_APB2ENR_AFIOEN (1u << 0)
#define RCC_APB2ENR_IOPAEN (1u << 2)
#define RCC_APB2ENR_IOPBEN (1u << 3)
#define RCC_APB2ENR_IOPCEN (1u << 4)
#define RCC_APB2ENR_IOPDEN (1u << 5)
#define RCC_APB2ENR_IOPEEN (1u << 6)
#define RCC_APB2ENR_ADC1EN (1u << 9)
#define RCC_APB2ENR_ADC2EN (1u << 10)
#define RCC_APB2ENR_TIM1EN (1u << 11)
#define RCC_APB2ENR_SPI1EN (1u << 12)
#define RCC_APB2ENR_USART1EN (1u << 14)
#define RCC_APB1ENR_TIM2EN (1u << 0)
#define RCC_APB1ENR_TIM6EN (1u << 4)
#define RCC_APB1ENR_USART2EN (1u << 17)
#define RCC_APB1ENR_I2C1EN (1u << 21)
#define RCC_APB1ENR_CAN1EN (1u << 25)
#define RCC_APB1ENR_CAN2EN (1u << 26)
#define RCC_APB1ENR_BKPEN (1u << 27)
#define RCC_APB1ENR_PWREN (1u << 28)
#define RCC_APB1ENR_DACEN (1u << 29)

/* FLASH */
#define FLASH_ACR    REG32(FLASH_BASE + 0x00)
#define FLASH_KEYR   REG32(FLASH_BASE + 0x04)
#define FLASH_OPTKEYR REG32(FLASH_BASE + 0x08)
#define FLASH_SR     REG32(FLASH_BASE + 0x0C)
#define FLASH_CR     REG32(FLASH_BASE + 0x10)
#define FLASH_AR     REG32(FLASH_BASE + 0x14)
#define FLASH_OBR    REG32(FLASH_BASE + 0x1C)
#define FLASH_WRPR   REG32(FLASH_BASE + 0x20)
#define FLASH_KEY1   0x45670123u
#define FLASH_KEY2   0xCDEF89ABu
#define FLASH_SR_BSY (1u << 0)
#define FLASH_SR_PGERR (1u << 2)
#define FLASH_SR_WRPRTERR (1u << 4)
#define FLASH_SR_EOP (1u << 5)
#define FLASH_CR_PG  (1u << 0)
#define FLASH_CR_PER (1u << 1)
#define FLASH_CR_MER (1u << 2)
#define FLASH_CR_STRT (1u << 6)
#define FLASH_CR_LOCK (1u << 7)

/* GPIO: CRL/CRH 4 bits per pin = CNF[1:0] MODE[1:0] */
#define GPIO_CRL(b)  REG32((b) + 0x00)
#define GPIO_CRH(b)  REG32((b) + 0x04)
#define GPIO_IDR(b)  REG32((b) + 0x08)
#define GPIO_ODR(b)  REG32((b) + 0x0C)
#define GPIO_BSRR(b) REG32((b) + 0x10)
#define GPIO_BRR(b)  REG32((b) + 0x14)
#define GPIO_LCKR(b) REG32((b) + 0x18)
#define GPIO_IN_ANALOG   0x0u
#define GPIO_IN_FLOATING 0x4u
#define GPIO_IN_PULL     0x8u
#define GPIO_OUT_PP_2MHZ 0x2u
#define GPIO_OUT_PP_50MHZ 0x3u
#define GPIO_OUT_OD_2MHZ 0x6u
#define GPIO_AF_PP_50MHZ 0xBu
#define GPIO_AF_OD_50MHZ 0xFu
/* set the 4-bit configuration of pin n (constant) of port base b */
#define GPIO_CONFIG(b, n, cfg) do { \
    if ((n) < 8) GPIO_CRL(b) = (GPIO_CRL(b) & ~(0xFu << (4 * (n)))) | ((uint32_t)(cfg) << (4 * (n))); \
    else GPIO_CRH(b) = (GPIO_CRH(b) & ~(0xFu << (4 * ((n) - 8)))) | ((uint32_t)(cfg) << (4 * ((n) - 8))); \
} while (0)

/* AFIO / EXTI */
#define AFIO_EVCR    REG32(AFIO_BASE + 0x00)
#define AFIO_MAPR    REG32(AFIO_BASE + 0x04)
#define AFIO_EXTICR(k) REG32(AFIO_BASE + 0x08 + 4 * (k))      /* k = 0..3 */
#define AFIO_MAPR_USART1_REMAP (1u << 2)
#define AFIO_MAPR_SWJ_JTAGDISABLE (2u << 24)
#define AFIO_MAPR_CAN1_REMAP_PB89 (2u << 13)                    /* connectivity line (RM0008 9.4.2) */
#define AFIO_MAPR_ETH_REMAP (1u << 21)
#define AFIO_MAPR_CAN2_REMAP (1u << 22)
#define AFIO_MAPR_MII_RMII_SEL (1u << 23)
#define AFIO_MAPR_SPI3_REMAP (1u << 28)
#define EXTI_IMR     REG32(EXTI_BASE + 0x00)
#define EXTI_EMR     REG32(EXTI_BASE + 0x04)
#define EXTI_RTSR    REG32(EXTI_BASE + 0x08)
#define EXTI_FTSR    REG32(EXTI_BASE + 0x0C)
#define EXTI_SWIER   REG32(EXTI_BASE + 0x10)
#define EXTI_PR      REG32(EXTI_BASE + 0x14)

/* PWR / BKP */
#define PWR_CR       REG32(PWR_BASE + 0x00)
#define PWR_CSR      REG32(PWR_BASE + 0x04)
#define PWR_CR_LPDS  (1u << 0)
#define PWR_CR_PDDS  (1u << 1)
#define PWR_CR_CWUF  (1u << 2)
#define PWR_CR_CSBF  (1u << 3)
#define PWR_CR_DBP   (1u << 8)
#define BKP_DR(n)    REG32(BKP_BASE + ((n) <= 10 ? 0x04 * (n) : 0x40 + 0x04 * ((n) - 11)))   /* n = 1..42 */

/* RTC (16-bit halves) */
#define RTC_CRH      REG32(RTC_BASE + 0x00)
#define RTC_CRL      REG32(RTC_BASE + 0x04)
#define RTC_PRLH     REG32(RTC_BASE + 0x08)
#define RTC_PRLL     REG32(RTC_BASE + 0x0C)
#define RTC_DIVH     REG32(RTC_BASE + 0x10)
#define RTC_DIVL     REG32(RTC_BASE + 0x14)
#define RTC_CNTH     REG32(RTC_BASE + 0x18)
#define RTC_CNTL     REG32(RTC_BASE + 0x1C)
#define RTC_ALRH     REG32(RTC_BASE + 0x20)
#define RTC_ALRL     REG32(RTC_BASE + 0x24)
#define RTC_CRL_SECF (1u << 0)
#define RTC_CRL_ALRF (1u << 1)
#define RTC_CRL_OWF  (1u << 2)
#define RTC_CRL_RSF  (1u << 3)
#define RTC_CRL_CNF  (1u << 4)
#define RTC_CRL_RTOFF (1u << 5)
#define RTC_CRH_SECIE (1u << 0)
#define RTC_CRH_ALRIE (1u << 1)

/* USART (F1: no OVER8 / ONEBIT) */
#define USART_SR(b)  REG32((b) + 0x00)
#define USART_DR(b)  REG32((b) + 0x04)
#define USART_BRR(b) REG32((b) + 0x08)
#define USART_CR1(b) REG32((b) + 0x0C)
#define USART_CR2(b) REG32((b) + 0x10)
#define USART_CR3(b) REG32((b) + 0x14)
#define USART_SR_RXNE (1u << 5)
#define USART_SR_TC   (1u << 6)
#define USART_SR_TXE  (1u << 7)
#define USART_CR1_RE  (1u << 2)
#define USART_CR1_TE  (1u << 3)
#define USART_CR1_RXNEIE (1u << 5)
#define USART_CR1_UE  (1u << 13)

/* TIM */
#define TIM_CR1(b)   REG32((b) + 0x00)
#define TIM_CR2(b)   REG32((b) + 0x04)
#define TIM_SMCR(b)  REG32((b) + 0x08)
#define TIM_DIER(b)  REG32((b) + 0x0C)
#define TIM_SR(b)    REG32((b) + 0x10)
#define TIM_EGR(b)   REG32((b) + 0x14)
#define TIM_CCMR1(b) REG32((b) + 0x18)
#define TIM_CCMR2(b) REG32((b) + 0x1C)
#define TIM_CCER(b)  REG32((b) + 0x20)
#define TIM_CNT(b)   REG32((b) + 0x24)
#define TIM_PSC(b)   REG32((b) + 0x28)
#define TIM_ARR(b)   REG32((b) + 0x2C)
#define TIM_CCR1(b)  REG32((b) + 0x34)
#define TIM_CCR2(b)  REG32((b) + 0x38)
#define TIM_CCR3(b)  REG32((b) + 0x3C)
#define TIM_CCR4(b)  REG32((b) + 0x40)
#define TIM_BDTR(b)  REG32((b) + 0x44)

/* SPI / I2C */
#define SPI_CR1(b)   REG32((b) + 0x00)
#define SPI_CR2(b)   REG32((b) + 0x04)
#define SPI_SR(b)    REG32((b) + 0x08)
#define SPI_DR(b)    REG32((b) + 0x0C)
#define I2C_CR1(b)   REG32((b) + 0x00)
#define I2C_CR2(b)   REG32((b) + 0x04)
#define I2C_OAR1(b)  REG32((b) + 0x08)
#define I2C_DR(b)    REG32((b) + 0x10)
#define I2C_SR1(b)   REG32((b) + 0x14)
#define I2C_SR2(b)   REG32((b) + 0x18)
#define I2C_CCR(b)   REG32((b) + 0x1C)
#define I2C_TRISE(b) REG32((b) + 0x20)

/* ADC (F1 layout) */
#define ADC_SR(b)    REG32((b) + 0x00)
#define ADC_CR1(b)   REG32((b) + 0x04)
#define ADC_CR2(b)   REG32((b) + 0x08)
#define ADC_SMPR1(b) REG32((b) + 0x0C)
#define ADC_SMPR2(b) REG32((b) + 0x10)
#define ADC_SQR1(b)  REG32((b) + 0x2C)
#define ADC_SQR2(b)  REG32((b) + 0x30)
#define ADC_SQR3(b)  REG32((b) + 0x34)
#define ADC_DR(b)    REG32((b) + 0x4C)
#define ADC_SR_EOC   (1u << 1)
#define ADC_CR1_SCAN (1u << 8)
#define ADC_CR2_ADON (1u << 0)
#define ADC_CR2_CONT (1u << 1)
#define ADC_CR2_CAL  (1u << 2)
#define ADC_CR2_RSTCAL (1u << 3)
#define ADC_CR2_DMA  (1u << 8)
#define ADC_CR2_ALIGN (1u << 11)
#define ADC_CR2_EXTSEL_SWSTART (7u << 17)
#define ADC_CR2_EXTTRIG (1u << 20)
#define ADC_CR2_SWSTART (1u << 22)
#define ADC_CR2_TSVREFE (1u << 23)

/* DMA1 channel n = 1..7 */
/* DMA2 channel n = 1..5 (RM0008 13.4) */
#define DMA2_ISR     REG32(DMA2_BASE + 0x00)
#define DMA2_IFCR    REG32(DMA2_BASE + 0x04)
#define DMA2_CCR(n)  REG32(DMA2_BASE + 0x08 + 20 * ((n) - 1))
#define DMA2_CNDTR(n) REG32(DMA2_BASE + 0x0C + 20 * ((n) - 1))
#define DMA2_CPAR(n) REG32(DMA2_BASE + 0x10 + 20 * ((n) - 1))
#define DMA2_CMAR(n) REG32(DMA2_BASE + 0x14 + 20 * ((n) - 1))
#define DMA1_ISR     REG32(DMA1_BASE + 0x00)
#define DMA1_IFCR    REG32(DMA1_BASE + 0x04)
#define DMA1_CCR(n)  REG32(DMA1_BASE + 0x08 + 20 * ((n) - 1))
#define DMA1_CNDTR(n) REG32(DMA1_BASE + 0x0C + 20 * ((n) - 1))
#define DMA1_CPAR(n) REG32(DMA1_BASE + 0x10 + 20 * ((n) - 1))
#define DMA1_CMAR(n) REG32(DMA1_BASE + 0x14 + 20 * ((n) - 1))
#define DMA_CCR_EN   (1u << 0)
#define DMA_CCR_TCIE (1u << 1)
#define DMA_CCR_HTIE (1u << 2)
#define DMA_CCR_DIR  (1u << 4)
#define DMA_CCR_CIRC (1u << 5)
#define DMA_CCR_PINC (1u << 6)
#define DMA_CCR_MINC (1u << 7)
#define DMA_CCR_PSIZE_16 (1u << 8)
#define DMA_CCR_PSIZE_32 (2u << 8)
#define DMA_CCR_MSIZE_16 (1u << 10)
#define DMA_CCR_MSIZE_32 (2u << 10)
#define DMA_CCR_MEM2MEM (1u << 14)

/* SCB / NVIC / SysTick */
#define SCB_SCR      REG32(0xE000ED10u)
#define SCB_SCR_SLEEPDEEP (1u << 2)
#define NVIC_ISER(k) REG32(0xE000E100u + 4 * (k))
#define NVIC_ICPR(k) REG32(0xE000E280u + 4 * (k))
#define NVIC_ENABLE(irq) (NVIC_ISER((irq) >> 5) = 1u << ((irq) & 31))
#define SYST_CSR     REG32(0xE000E010u)
#define SYST_RVR     REG32(0xE000E014u)
#define SYST_CVR     REG32(0xE000E018u)

/* IRQ numbers (RM0008 Table 61, chips/stm32f107vc.yaml) */
#define WWDG_IRQn 0
#define RCC_IRQn 5
#define EXTI0_IRQn 6
#define DMA1_Channel1_IRQn 11
#define ADC1_2_IRQn 18
#define CAN1_TX_IRQn 19
#define CAN1_RX0_IRQn 20
#define CAN1_RX1_IRQn 21
#define CAN1_SCE_IRQn 22
#define TIM2_IRQn 28
#define USART1_IRQn 37
#define USART2_IRQn 38
#define OTG_FS_WKUP_IRQn 42
#define TIM6_IRQn 54
#define DMA2_Channel1_IRQn 56
#define DMA2_Channel2_IRQn 57
#define ETH_IRQn 61
#define ETH_WKUP_IRQn 62
#define CAN2_TX_IRQn 63
#define CAN2_RX0_IRQn 64
#define CAN2_RX1_IRQn 65
#define CAN2_SCE_IRQn 66
#define OTG_FS_IRQn 67

/* Clock tree of ST's connectivity-line reference design (RM0008 8.2, 25 MHz crystal):
 *   HSE 25 MHz -> PREDIV2 /5 = 5 MHz -> PLL2 x8 = 40 MHz -> PREDIV1 (source PLL2) /5 = 8 MHz -> PLL x9 = 72 MHz SYSCLK
 *   AHB 72 MHz, APB1 36 MHz, APB2 72 MHz, ADC 12 MHz, OTGFSCLK = PLL VCO 144 MHz / 3 = 48 MHz (OTGFSPRE = 0),
 *   PLL3 x10 = 50 MHz (RMII reference clock on MCO when `mco_pll3` is set), flash 2 wait states + prefetch. */
static inline void clock_72mhz_pll2(int mco_pll3)
{
    RCC_CR |= RCC_CR_HSEON;
    while (!(RCC_CR & RCC_CR_HSERDY))
        ;
    RCC_CFGR2 = RCC_CFGR2_PREDIV2(5) | RCC_CFGR2_PLL2MUL8 | RCC_CFGR2_PLL3MUL10 | RCC_CFGR2_PREDIV1SRC_PLL2
              | RCC_CFGR2_PREDIV1(5);
    RCC_CR |= RCC_CR_PLL2ON;
    while (!(RCC_CR & RCC_CR_PLL2RDY))
        ;
    if (mco_pll3) {
        RCC_CR |= RCC_CR_PLL3ON;
        while (!(RCC_CR & RCC_CR_PLL3RDY))
            ;
    }
    FLASH_ACR = (FLASH_ACR & ~7u) | 2u | (1u << 4);         /* LATENCY = 2 (48-72 MHz), PRFTBE */
    RCC_CFGR = RCC_CFGR_PLLSRC | RCC_CFGR_PLLMUL9 | RCC_CFGR_PPRE1_DIV2 | RCC_CFGR_ADCPRE_DIV6
             | (mco_pll3 ? RCC_CFGR_MCO_PLL3 : 0);
    RCC_CR |= RCC_CR_PLLON;
    while (!(RCC_CR & RCC_CR_PLLRDY))
        ;
    RCC_CFGR |= RCC_CFGR_SW_PLL;
    while ((RCC_CFGR & RCC_CFGR_SWS_MASK) != RCC_CFGR_SWS_PLL)
        ;
}

#endif
