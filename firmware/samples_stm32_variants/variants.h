/* Register helpers for the STM32F405xx/STM32F407xx package-variant samples. Extends ../samples_stm32/stm32f405.h
 * (same die, same register map: DS8626 Table 10, RM0090) with GPIOD..GPIOI, USART2, USART6 and the IRQ numbers the
 * samples use. Startup code, vector table and linker script are shared with ../samples_stm32. */
#ifndef VARIANTS_H
#define VARIANTS_H
#include "../samples_stm32/stm32f405.h"

#define GPIOD ((GPIO_TypeDef *)0x40020C00u)
#define GPIOE ((GPIO_TypeDef *)0x40021000u)
#define GPIOF ((GPIO_TypeDef *)0x40021400u)
#define GPIOG ((GPIO_TypeDef *)0x40021800u)
#define GPIOH ((GPIO_TypeDef *)0x40021C00u)
#define GPIOI ((GPIO_TypeDef *)0x40022000u)
#define RCC_AHB1ENR_GPIOCEN (1u << 2)
#define RCC_AHB1ENR_GPIOGEN (1u << 6)
#define RCC_AHB1ENR_GPIOIEN (1u << 8)
#define RCC_AHB1ENR_GPIOALL 0x1FFu                 /* GPIOA..GPIOI clock enables */
#define RCC_APB1ENR_USART2EN (1u << 17)
#define RCC_APB2ENR_USART6EN (1u << 5)

#define USART2 ((USART_TypeDef *)0x40004400u)      /* APB1 */
#define USART6 ((USART_TypeDef *)0x40011400u)      /* APB2 */
#define USART_CR1_RXNEIE (1u << 5)

#define USART2_IRQn 38
#define USART6_IRQn 71

static inline void __disable_irq(void) { __asm volatile ("cpsid i" ::: "memory"); }

#endif
