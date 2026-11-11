/* Minimal register definitions for the STM32F401/STM32F411 samples (chips/stm32f401*.yaml, chips/stm32f411*.yaml).
 * Addresses and bit positions from the STM32CubeF4 device headers stm32f401xe.h / stm32f411xe.h
 * (firmware/vendor/STM32CubeF4); the blocks are the RM0090 F405 ones at the same addresses.
 * Build with -DLINE_F401 or -DLINE_F411 (clock_init() and the IRQ list depend on it). */
#ifndef STM32F4X1_H
#define STM32F4X1_H
#include <stdint.h>

#if !defined(LINE_F401) && !defined(LINE_F411)
#error "define LINE_F401 or LINE_F411"
#endif

#define REG32(a) (*(volatile uint32_t *)(a))
#define HSE_HZ 8000000u                    /* NUCLEO: 8 MHz MCO from the ST-LINK (bypass) */

/* ---- RCC (0x40023800) ---- */
#define RCC_BASE        0x40023800u
#define RCC_CR          REG32(RCC_BASE + 0x00)
#define RCC_PLLCFGR     REG32(RCC_BASE + 0x04)
#define RCC_CFGR        REG32(RCC_BASE + 0x08)
#define RCC_AHB1ENR     REG32(RCC_BASE + 0x30)
#define RCC_APB1ENR     REG32(RCC_BASE + 0x40)
#define RCC_APB2ENR     REG32(RCC_BASE + 0x44)
#define RCC_PLLI2SCFGR  REG32(RCC_BASE + 0x84)
#define RCC_DCKCFGR     REG32(RCC_BASE + 0x8C)
#define RCC_CR_HSEON      (1u << 16)
#define RCC_CR_HSERDY     (1u << 17)
#define RCC_CR_HSEBYP     (1u << 18)
#define RCC_CR_PLLON      (1u << 24)
#define RCC_CR_PLLRDY     (1u << 25)
#define RCC_CR_PLLI2SON   (1u << 26)
#define RCC_CR_PLLI2SRDY  (1u << 27)
#define RCC_PLLCFGR_SRC_HSE (1u << 22)
#define RCC_CFGR_SW_PLL   2u
#define RCC_CFGR_SWS_MASK (3u << 2)
#define RCC_CFGR_SWS_PLL  (2u << 2)
#define RCC_CFGR_PPRE1_DIV2 (4u << 10)
#define RCC_AHB1ENR_GPIOAEN (1u << 0)
#define RCC_AHB1ENR_GPIOBEN (1u << 1)
#define RCC_AHB1ENR_GPIOCEN (1u << 2)
#define RCC_APB1ENR_TIM2EN   (1u << 0)
#define RCC_APB1ENR_USART2EN (1u << 17)
#define RCC_APB1ENR_PWREN    (1u << 28)
#define RCC_APB2ENR_USART1EN (1u << 4)
#define RCC_APB2ENR_USART6EN (1u << 5)
#define RCC_APB2ENR_SPI5EN   (1u << 20)     /* STM32F411 only */
#define RCC_DCKCFGR_TIMPRE   (1u << 24)

/* ---- FLASH / PWR ---- */
#define FLASH_ACR     REG32(0x40023C00u)
#define FLASH_ACR_PRFTEN (1u << 8)
#define FLASH_ACR_ICEN   (1u << 9)
#define FLASH_ACR_DCEN   (1u << 10)
#define PWR_CR        REG32(0x40007000u)
#define PWR_CR_VOS_POS 14u

/* ---- GPIO ---- */
typedef struct {
    volatile uint32_t MODER, OTYPER, OSPEEDR, PUPDR, IDR, ODR, BSRR, LCKR, AFR[2];
} GPIO_TypeDef;
#define GPIOA ((GPIO_TypeDef *)0x40020000u)
#define GPIOB ((GPIO_TypeDef *)0x40020400u)
#define GPIOC ((GPIO_TypeDef *)0x40020800u)
#define GPIO_MODE_OUTPUT 1u
#define GPIO_MODE_AF     2u
#define GPIO_PULL_UP     1u

static inline void gpio_mode(GPIO_TypeDef *g, unsigned pin, unsigned mode)
{
    g->MODER = (g->MODER & ~(3u << (2 * pin))) | (mode << (2 * pin));
}
static inline void gpio_pull(GPIO_TypeDef *g, unsigned pin, unsigned pull)
{
    g->PUPDR = (g->PUPDR & ~(3u << (2 * pin))) | (pull << (2 * pin));
}
static inline void gpio_af(GPIO_TypeDef *g, unsigned pin, unsigned af)
{
    volatile uint32_t *r = &g->AFR[pin >> 3];
    unsigned sh = 4 * (pin & 7);
    *r = (*r & ~(0xFu << sh)) | (af << sh);
    gpio_mode(g, pin, GPIO_MODE_AF);
}

/* ---- USART1/2/6 ---- */
typedef struct {
    volatile uint32_t SR, DR, BRR, CR1, CR2, CR3, GTPR;
} USART_TypeDef;
#define USART1 ((USART_TypeDef *)0x40011000u)
#define USART2 ((USART_TypeDef *)0x40004400u)
#define USART6 ((USART_TypeDef *)0x40011400u)
#define USART_SR_RXNE (1u << 5)
#define USART_SR_TXE  (1u << 7)
#define USART_CR1_RE  (1u << 2)
#define USART_CR1_TE  (1u << 3)
#define USART_CR1_RXNEIE (1u << 5)
#define USART_CR1_UE  (1u << 13)
#define USART_BRR(clk, baud) (((clk) + (baud) / 2u) / (baud))

/* ---- TIM2 (32-bit, APB1) ---- */
typedef struct {
    volatile uint32_t CR1, CR2, SMCR, DIER, SR, EGR, CCMR1, CCMR2, CCER, CNT, PSC, ARR, RCR, CCR[4], BDTR, DCR, DMAR;
} TIM_TypeDef;
#define TIM2 ((TIM_TypeDef *)0x40000000u)
#define TIM_CR1_CEN  (1u << 0)
#define TIM_DIER_UIE (1u << 0)
#define TIM_SR_UIF   (1u << 0)
#define TIM_EGR_UG   (1u << 0)

/* ---- SPI/I2S ---- */
typedef struct {
    volatile uint32_t CR1, CR2, SR, DR, CRCPR, RXCRCR, TXCRCR, I2SCFGR, I2SPR;
} SPI_TypeDef;
#define SPI5 ((SPI_TypeDef *)0x40015000u)    /* STM32F411 only */

/* ---- IRQ numbers (stm32f401xe.h / stm32f411xe.h IRQn_Type) ---- */
#define TIM2_IRQn    28
#define USART1_IRQn  37
#define USART2_IRQn  38
#define USART6_IRQn  71

/* ---- System Control Space ---- */
#define SCB_CPACR    REG32(0xE000ED88u)
#define NVIC_ISER(n) REG32(0xE000E100u + 4u * (n))
static inline void NVIC_EnableIRQ(unsigned n) { NVIC_ISER(n >> 5) = 1u << (n & 31); }
static inline void __enable_irq(void) { __asm volatile ("cpsie i" ::: "memory"); }

/* Clock tree: HSE 8 MHz (bypass) -> PLL.  F401: M = 8, N = 336, P = 4 -> 84 MHz (VCO 336 MHz), Q = 7 -> 48 MHz,
 * regulator scale 2 (VOS = 10), 2 flash wait states; F411: M = 4, N = 100, P = 2 -> 100 MHz (VCO 200 MHz), Q = 4,
 * scale 1 (VOS = 11), 3 wait states.  APB1 = HCLK / 2 (42 / 50 MHz), APB2 = HCLK.  Returns HCLK. */
#if defined(LINE_F401)
#define SYSCLK_HZ 84000000u
#define PLL_M 8u
#define PLL_N 336u
#define PLL_P_BITS 1u                      /* /4 */
#define PLL_Q 7u
#define VOS 2u
#define FLASH_WS 2u
#else
#define SYSCLK_HZ 100000000u
#define PLL_M 4u
#define PLL_N 100u
#define PLL_P_BITS 0u                      /* /2 */
#define PLL_Q 4u
#define VOS 3u
#define FLASH_WS 3u
#endif
#define PCLK1_HZ (SYSCLK_HZ / 2u)
#define PCLK2_HZ SYSCLK_HZ

static inline void clock_init(void)
{
    RCC_APB1ENR |= RCC_APB1ENR_PWREN;
    PWR_CR = (PWR_CR & ~(3u << PWR_CR_VOS_POS)) | (VOS << PWR_CR_VOS_POS);   /* while the PLL is off */
    RCC_CR |= RCC_CR_HSEBYP | RCC_CR_HSEON;
    while (!(RCC_CR & RCC_CR_HSERDY)) { }
    RCC_PLLCFGR = PLL_M | (PLL_N << 6) | (PLL_P_BITS << 16) | RCC_PLLCFGR_SRC_HSE | (PLL_Q << 24);
    RCC_CR |= RCC_CR_PLLON;
    while (!(RCC_CR & RCC_CR_PLLRDY)) { }
    FLASH_ACR = FLASH_WS | FLASH_ACR_PRFTEN | FLASH_ACR_ICEN | FLASH_ACR_DCEN;
    RCC_CFGR = RCC_CFGR_PPRE1_DIV2 | RCC_CFGR_SW_PLL;
    while ((RCC_CFGR & RCC_CFGR_SWS_MASK) != RCC_CFGR_SWS_PLL) { }
}

#endif
