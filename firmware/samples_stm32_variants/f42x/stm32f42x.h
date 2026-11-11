/* Register helpers for the STM32F427xx/STM32F437xx samples (f42x_*.c). Extends ../../samples_stm32/stm32f405.h
 * (same register layout for the F405 blocks, RM0090) with the F42x/F43x additions: RCC PLLSAI/DCKCFGR and the new
 * clock enables (RM0090 ch. 6), PWR over-drive (5.5), UART7/UART8, GPIOD..GPIOK and the IRQ numbers of RM0090
 * Table 63. Each sample adds the block-specific registers it uses (FMC, SAI1, DMA2D, CRYP) itself. */
#ifndef STM32F42X_H
#define STM32F42X_H
#include "../../samples_stm32/stm32f405.h"

/* ---- RCC additions (RM0090 6.3) ---- */
#define RCC_AHB3ENR     REG32(RCC_BASE + 0x38)
#define RCC_AHB2ENR     REG32(RCC_BASE + 0x34)
#define RCC_PLLI2SCFGR  REG32(RCC_BASE + 0x84)
#define RCC_PLLSAICFGR  REG32(RCC_BASE + 0x88)
#define RCC_DCKCFGR     REG32(RCC_BASE + 0x8C)
#define RCC_CR_PLLI2SON   (1u << 26)
#define RCC_CR_PLLI2SRDY  (1u << 27)
#define RCC_CR_PLLSAION   (1u << 28)
#define RCC_CR_PLLSAIRDY  (1u << 29)
#define RCC_DCKCFGR_TIMPRE (1u << 24)
#define RCC_AHB1ENR_GPIOALL 0x7FFu                 /* GPIOA..GPIOK clock enables */
#define RCC_AHB1ENR_DMA2EN  (1u << 22)
#define RCC_AHB1ENR_DMA2DEN (1u << 23)
#define RCC_AHB2ENR_CRYPEN  (1u << 4)
#define RCC_AHB2ENR_HASHEN  (1u << 5)
#define RCC_AHB3ENR_FMCEN   (1u << 0)
#define RCC_APB1ENR_UART7EN (1u << 30)
#define RCC_APB1ENR_UART8EN (1u << 31)
#define RCC_APB2ENR_SPI4EN  (1u << 13)
#define RCC_APB2ENR_SPI5EN  (1u << 20)
#define RCC_APB2ENR_SPI6EN  (1u << 21)
#define RCC_APB2ENR_SAI1EN  (1u << 22)

/* ---- PWR (RM0090 5.5.1 / 5.5.2) ---- */
#define PWR_CSR          REG32(0x40007004u)
#define PWR_CR_VOS_SCALE1 (3u << 14)
#define PWR_CR_ODEN      (1u << 16)
#define PWR_CR_ODSWEN    (1u << 17)
#define PWR_CSR_VOSRDY   (1u << 14)
#define PWR_CSR_ODRDY    (1u << 16)
#define PWR_CSR_ODSWRDY  (1u << 17)

/* ---- GPIO ports D..K ---- */
#define GPIOD ((GPIO_TypeDef *)0x40020C00u)
#define GPIOE ((GPIO_TypeDef *)0x40021000u)
#define GPIOF ((GPIO_TypeDef *)0x40021400u)
#define GPIOG ((GPIO_TypeDef *)0x40021800u)
#define GPIOH ((GPIO_TypeDef *)0x40021C00u)
#define GPIOI ((GPIO_TypeDef *)0x40022000u)
#define GPIOJ ((GPIO_TypeDef *)0x40022400u)
#define GPIOK ((GPIO_TypeDef *)0x40022800u)

static inline void gpio_speed(GPIO_TypeDef *g, unsigned pin, unsigned speed)
{
    g->OSPEEDR = (g->OSPEEDR & ~(3u << (2 * pin))) | (speed << (2 * pin));
}

/* ---- U(S)ARTs ---- */
#define USART2 ((USART_TypeDef *)0x40004400u)      /* APB1 */
#define USART6 ((USART_TypeDef *)0x40011400u)      /* APB2 */
#define UART7  ((USART_TypeDef *)0x40007800u)      /* APB1 */
#define UART8  ((USART_TypeDef *)0x40007C00u)      /* APB1 */
#define USART_CR1_RXNEIE (1u << 5)

/* ---- IRQ numbers (RM0090 Table 63) ---- */
#define DMA2_Stream1_IRQn 57
#define CRYP_IRQn   79
#define UART7_IRQn  82
#define UART8_IRQn  83
#define SPI4_IRQn   84
#define SAI1_IRQn   87
#define DMA2D_IRQn  90

static inline void __disable_irq(void) { __asm volatile ("cpsid i" ::: "memory"); }

/* 180 MHz from HSE 8 MHz with over-drive (RM0090 5.1.4 "Entering Over-drive mode" + 6.3.2):
 * PLLM 8, PLLN 360, PLLP 2 -> 180 MHz, PLLQ 7 (VCO 360 MHz / 7 = 51.4 MHz: no exact 48 MHz at 180 MHz);
 * AHB /1, APB1 /4 = 45 MHz, APB2 /2 = 90 MHz; flash 5 wait states (DS9405 Table 18: 2.7-3.6 V, 150 < HCLK <= 180). */
static inline void clock_180mhz(void)
{
    RCC_APB1ENR |= RCC_APB1ENR_PWREN;
    PWR_CR = (PWR_CR & ~(3u << 14)) | PWR_CR_VOS_SCALE1;      /* scale 1 (reset value; PLL still off) */
    RCC_CR |= RCC_CR_HSEON;
    while (!(RCC_CR & RCC_CR_HSERDY)) { }
    RCC_CFGR = (RCC_CFGR & ~3u) | 1u;                          /* SYSCLK = HSE while switching */
    while ((RCC_CFGR & RCC_CFGR_SWS_MASK) != (1u << 2)) { }
    RCC_PLLCFGR = 8u | (360u << 6) | (0u << 16) | RCC_PLLCFGR_SRC_HSE | (7u << 24);
    RCC_CR |= RCC_CR_PLLON;
    PWR_CR |= PWR_CR_ODEN;                                     /* over-drive during the PLL lock phase */
    while (!(PWR_CSR & PWR_CSR_ODRDY)) { }
    PWR_CR |= PWR_CR_ODSWEN;
    while (!(PWR_CSR & PWR_CSR_ODSWRDY)) { }
    FLASH_ACR = FLASH_ACR_LATENCY_5WS | FLASH_ACR_PRFTEN | FLASH_ACR_ICEN | FLASH_ACR_DCEN;
    RCC_CFGR = (RCC_CFGR & ~((0xFu << 4) | (7u << 10) | (7u << 13))) | RCC_CFGR_PPRE1_DIV4 | RCC_CFGR_PPRE2_DIV2;
    while (!(RCC_CR & RCC_CR_PLLRDY)) { }
    RCC_CFGR = (RCC_CFGR & ~3u) | RCC_CFGR_SW_PLL;
    while ((RCC_CFGR & RCC_CFGR_SWS_MASK) != RCC_CFGR_SWS_PLL) { }
}

#endif
