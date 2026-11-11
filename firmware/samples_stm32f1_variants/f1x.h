/* STM32F103 medium/high-density additions to ../samples_stm32f1/stm32f103.h (bases from RM0008 Rev 21 3.3 Table 3,
 * offsets and bits from the RM0008 register maps: 7.3 RCC, 9.4 AFIO, 12.5 DAC, 13.4 DMA, 21.5/21.6 FSMC, 22.9 SDIO,
 * 31.6 DBGMCU; IRQ numbers from 10.1.2 Table 63). */
#ifndef F1X_H
#define F1X_H
#include "stm32f103.h"

#define TIM4_BASE    0x40000800u
#define TIM5_BASE    0x40000C00u
#define TIM6_BASE    0x40001000u
#define TIM7_BASE    0x40001400u
#define SPI2_BASE    0x40003800u
#define SPI3_BASE    0x40003C00u
#define USART3_BASE  0x40004800u
#define UART4_BASE   0x40004C00u
#define UART5_BASE   0x40005000u
#define I2C2_BASE    0x40005800u
#define DAC_BASE     0x40007400u
#define GPIOE_BASE   0x40011800u
#define GPIOF_BASE   0x40011C00u
#define GPIOG_BASE   0x40012000u
#define TIM8_BASE    0x40013400u
#define ADC3_BASE    0x40013C00u
#define SDIO_BASE    0x40018000u
#define DMA2_BASE    0x40020400u
#define FSMC_BASE    0xA0000000u

#define RCC_AHBENR_DMA2EN (1u << 1)
#define RCC_AHBENR_FSMCEN (1u << 8)
#define RCC_AHBENR_SDIOEN (1u << 10)
#define RCC_APB2ENR_IOPEEN (1u << 6)
#define RCC_APB2ENR_IOPFEN (1u << 7)
#define RCC_APB2ENR_IOPGEN (1u << 8)
#define RCC_APB2ENR_TIM8EN (1u << 13)
#define RCC_APB2ENR_ADC3EN (1u << 15)
#define RCC_APB1ENR_TIM4EN (1u << 2)
#define RCC_APB1ENR_TIM5EN (1u << 3)
#define RCC_APB1ENR_TIM6EN (1u << 4)
#define RCC_APB1ENR_TIM7EN (1u << 5)
#define RCC_APB1ENR_SPI2EN (1u << 14)
#define RCC_APB1ENR_SPI3EN (1u << 15)
#define RCC_APB1ENR_USART3EN (1u << 18)
#define RCC_APB1ENR_UART4EN (1u << 19)
#define RCC_APB1ENR_UART5EN (1u << 20)
#define RCC_APB1ENR_I2C2EN (1u << 22)
#define RCC_APB1ENR_DACEN (1u << 29)

/* DMA2 channel n = 1..5 (same layout as DMA1) */
#define DMA2_ISR     REG32(DMA2_BASE + 0x00)
#define DMA2_IFCR    REG32(DMA2_BASE + 0x04)
#define DMA2_CCR(n)  REG32(DMA2_BASE + 0x08 + 20 * ((n) - 1))
#define DMA2_CNDTR(n) REG32(DMA2_BASE + 0x0C + 20 * ((n) - 1))
#define DMA2_CPAR(n) REG32(DMA2_BASE + 0x10 + 20 * ((n) - 1))
#define DMA2_CMAR(n) REG32(DMA2_BASE + 0x14 + 20 * ((n) - 1))

/* DAC (RM0008 12.5: no DAC_SR on the F1) */
#define DAC_CR       REG32(DAC_BASE + 0x00)
#define DAC_SWTRIGR  REG32(DAC_BASE + 0x04)
#define DAC_DHR12R1  REG32(DAC_BASE + 0x08)
#define DAC_DHR12R2  REG32(DAC_BASE + 0x14)
#define DAC_DOR1     REG32(DAC_BASE + 0x2C)
#define DAC_DOR2     REG32(DAC_BASE + 0x30)

/* ADC extras */
#define ADC_JSQR(b)  REG32((b) + 0x38)

/* SDIO */
#define SDIO_POWER   REG32(SDIO_BASE + 0x00)
#define SDIO_CLKCR   REG32(SDIO_BASE + 0x04)
#define SDIO_ARG     REG32(SDIO_BASE + 0x08)
#define SDIO_CMD     REG32(SDIO_BASE + 0x0C)
#define SDIO_RESPCMD REG32(SDIO_BASE + 0x10)
#define SDIO_RESP1   REG32(SDIO_BASE + 0x14)
#define SDIO_RESP2   REG32(SDIO_BASE + 0x18)
#define SDIO_RESP3   REG32(SDIO_BASE + 0x1C)
#define SDIO_RESP4   REG32(SDIO_BASE + 0x20)
#define SDIO_DTIMER  REG32(SDIO_BASE + 0x24)
#define SDIO_DLEN    REG32(SDIO_BASE + 0x28)
#define SDIO_DCTRL   REG32(SDIO_BASE + 0x2C)
#define SDIO_DCOUNT  REG32(SDIO_BASE + 0x30)
#define SDIO_STA     REG32(SDIO_BASE + 0x34)
#define SDIO_ICR     REG32(SDIO_BASE + 0x38)
#define SDIO_MASK    REG32(SDIO_BASE + 0x3C)
#define SDIO_FIFOCNT REG32(SDIO_BASE + 0x48)
#define SDIO_FIFO    REG32(SDIO_BASE + 0x80)

/* FSMC NOR/PSRAM bank 1 */
#define FSMC_BCR(x)  REG32(FSMC_BASE + 8 * ((x) - 1))          /* x = 1..4 */
#define FSMC_BTR(x)  REG32(FSMC_BASE + 4 + 8 * ((x) - 1))

/* DBGMCU, device electronic signature (RM0008 31.6.1, 30.1.1) */
#define DBGMCU_IDCODE REG32(0xE0042000u)
#define F_SIZE       REG16(0x1FFFF7E0u)

/* USART CR3 */
#define USART_CR3_DMAT (1u << 7)
#define USART_CR3_DMAR (1u << 6)
#define USART_SR_ORE  (1u << 3)

/* IRQ numbers (RM0008 10.1.2 Table 63, chips/stm32f103ze.yaml) */
#define TIM4_IRQn 30
#define I2C2_EV_IRQn 33
#define I2C2_ER_IRQn 34
#define SPI2_IRQn 36
#define USART3_IRQn 39
#define TIM8_UP_IRQn 44
#define TIM8_CC_IRQn 46
#define ADC3_IRQn 47
#define SDIO_IRQn 49
#define TIM5_IRQn 50
#define SPI3_IRQn 51
#define UART4_IRQn 52
#define UART5_IRQn 53
#define TIM6_IRQn 54
#define TIM7_IRQn 55
#define DMA2_Channel1_IRQn 56
#define DMA2_Channel2_IRQn 57
#define DMA2_Channel3_IRQn 58
#define DMA2_Channel4_5_IRQn 59

/* minimal formatted output on any USART (polling) */
static inline void uputc(uint32_t b, char c)
{
    while (!(USART_SR(b) & USART_SR_TXE))
        ;
    USART_DR(b) = (uint8_t)c;
}

static inline void uputs(uint32_t b, const char *s)
{
    while (*s)
        uputc(b, *s++);
}

static inline void uflush(uint32_t b)
{
    while (!(USART_SR(b) & USART_SR_TC))
        ;
}
#endif
