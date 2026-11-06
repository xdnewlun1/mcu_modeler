/* Minimal register definitions for the STM32F405ZG (STM32F4, Cortex-M4F).
 * Offsets from RM0090 (STM32F405/415/407/417 reference manual) and the ARMv7-M architecture manual for the SCS.
 * Only what the CHIP_EM sample programs need. */
#ifndef STM32F405_H
#define STM32F405_H
#include <stdint.h>

#define REG32(a) (*(volatile uint32_t *)(a))

/* ---- Clocks: HSI after reset (the samples run there unless they program the PLL) ---- */
#define HSI_HZ 16000000u
#define HSE_HZ  8000000u

/* ---- RCC (0x40023800) ---- */
#define RCC_BASE      0x40023800u
#define RCC_CR        REG32(RCC_BASE + 0x00)
#define RCC_PLLCFGR   REG32(RCC_BASE + 0x04)
#define RCC_CFGR      REG32(RCC_BASE + 0x08)
#define RCC_AHB1ENR   REG32(RCC_BASE + 0x30)
#define RCC_APB1ENR   REG32(RCC_BASE + 0x40)
#define RCC_APB2ENR   REG32(RCC_BASE + 0x44)
#define RCC_CSR       REG32(RCC_BASE + 0x74)
#define RCC_CR_HSION      (1u << 0)
#define RCC_CR_HSIRDY     (1u << 1)
#define RCC_CR_HSEON      (1u << 16)
#define RCC_CR_HSERDY     (1u << 17)
#define RCC_CR_PLLON      (1u << 24)
#define RCC_CR_PLLRDY     (1u << 25)
#define RCC_PLLCFGR_SRC_HSE (1u << 22)
#define RCC_CFGR_SW_PLL   2u
#define RCC_CFGR_SWS_MASK (3u << 2)
#define RCC_CFGR_SWS_PLL  (2u << 2)
#define RCC_CFGR_PPRE1_DIV4 (5u << 10)
#define RCC_CFGR_PPRE2_DIV2 (4u << 13)
#define RCC_AHB1ENR_GPIOAEN (1u << 0)
#define RCC_AHB1ENR_GPIOBEN (1u << 1)
#define RCC_APB1ENR_TIM2EN  (1u << 0)
#define RCC_APB1ENR_PWREN   (1u << 28)
#define RCC_APB2ENR_USART1EN (1u << 4)
#define RCC_APB2ENR_SYSCFGEN (1u << 14)

/* ---- FLASH interface (0x40023C00) and PWR (0x40007000) ---- */
#define FLASH_ACR     REG32(0x40023C00u)
#define FLASH_ACR_LATENCY_5WS 5u
#define FLASH_ACR_PRFTEN (1u << 8)
#define FLASH_ACR_ICEN   (1u << 9)
#define FLASH_ACR_DCEN   (1u << 10)
#define PWR_CR        REG32(0x40007000u)
#define PWR_CR_VOS    (1u << 14)

/* ---- GPIO (GPIOA 0x40020000, GPIOB 0x40020400, ...) ---- */
typedef struct {
    volatile uint32_t MODER, OTYPER, OSPEEDR, PUPDR, IDR, ODR, BSRR, LCKR, AFR[2];
} GPIO_TypeDef;
#define GPIOA ((GPIO_TypeDef *)0x40020000u)
#define GPIOB ((GPIO_TypeDef *)0x40020400u)
#define GPIOC ((GPIO_TypeDef *)0x40020800u)
#define GPIO_MODE_INPUT  0u
#define GPIO_MODE_OUTPUT 1u
#define GPIO_MODE_AF     2u
#define GPIO_MODE_ANALOG 3u
#define GPIO_PULL_UP     1u
#define GPIO_PULL_DOWN   2u

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
    unsigned r = pin >> 3, s = 4 * (pin & 7);
    g->AFR[r] = (g->AFR[r] & ~(0xFu << s)) | (af << s);
    gpio_mode(g, pin, GPIO_MODE_AF);
}

/* ---- USART1 (0x40011000, APB2) ---- */
typedef struct {
    volatile uint32_t SR, DR, BRR, CR1, CR2, CR3, GTPR;
} USART_TypeDef;
#define USART1 ((USART_TypeDef *)0x40011000u)
#define USART_SR_RXNE (1u << 5)
#define USART_SR_TC   (1u << 6)
#define USART_SR_TXE  (1u << 7)
#define USART_CR1_RE  (1u << 2)
#define USART_CR1_TE  (1u << 3)
#define USART_CR1_UE  (1u << 13)
/* BRR for oversampling by 16: USARTDIV = clk / (16 * baud) as 12.4 fixed point, rounded */
#define USART_BRR(clk, baud) (((clk) + (baud) / 2u) / (baud))

/* ---- SYSCFG (0x40013800) / EXTI (0x40013C00) ---- */
#define SYSCFG_EXTICR1 REG32(0x40013808u)
#define EXTI_IMR      REG32(0x40013C00u)
#define EXTI_RTSR     REG32(0x40013C08u)
#define EXTI_FTSR     REG32(0x40013C0Cu)
#define EXTI_PR       REG32(0x40013C14u)

/* ---- TIM2 (0x40000000, APB1, 32-bit) ---- */
typedef struct {
    volatile uint32_t CR1, CR2, SMCR, DIER, SR, EGR, CCMR1, CCMR2, CCER, CNT, PSC, ARR, RCR, CCR[4], BDTR, DCR, DMAR;
} TIM_TypeDef;
#define TIM2 ((TIM_TypeDef *)0x40000000u)
#define TIM_CR1_CEN  (1u << 0)
#define TIM_DIER_UIE (1u << 0)
#define TIM_SR_UIF   (1u << 0)
#define TIM_EGR_UG   (1u << 0)

/* ---- IRQ numbers (RM0090 Table 61) ---- */
#define EXTI0_IRQn   6
#define TIM2_IRQn    28
#define USART1_IRQn  37

/* ---- System Control Space ---- */
#define SCB_CPACR    REG32(0xE000ED88u)
#define NVIC_ISER(n) REG32(0xE000E100u + 4u * (n))
#define NVIC_ICPR(n) REG32(0xE000E280u + 4u * (n))
#define SYST_CSR     REG32(0xE000E010u)
#define SYST_RVR     REG32(0xE000E014u)
#define SYST_CVR     REG32(0xE000E018u)
#define SYST_CSR_ENABLE    (1u << 0)
#define SYST_CSR_CLKSOURCE (1u << 2)
#define SYST_CSR_COUNTFLAG (1u << 16)

static inline void NVIC_EnableIRQ(unsigned n)       { NVIC_ISER(n >> 5) = 1u << (n & 31); }
static inline void NVIC_ClearPendingIRQ(unsigned n) { NVIC_ICPR(n >> 5) = 1u << (n & 31); }
static inline void __enable_irq(void) { __asm volatile ("cpsie i" ::: "memory"); }
static inline void __WFI(void)        { __asm volatile ("wfi"); }

#endif
