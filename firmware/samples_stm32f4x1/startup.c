/* Startup code for the STM32F401/STM32F411 samples (clang/lld, no libc).
 * Vector table at 0x08000000 (VTOR): initial SP + 15 system exceptions + 86 external IRQs (stm32f411xe.h IRQn_Type;
 * the F401 has 85, vector 85 SPI5 is unused there).  Handlers a sample does not define fall back to Default_Handler
 * (an endless loop).  Reset_Handler enables the FPU, copies .data, zeroes .bss and calls main. */
#include <stdint.h>

extern uint32_t _sidata, _sdata, _edata, _sbss, _ebss, _estack;
int main(void);

void Reset_Handler(void);
void Default_Handler(void) { for (;;) { } }

void TIM2_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void USART1_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void USART2_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void USART6_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void SysTick_Handler(void) __attribute__((weak, alias("Default_Handler")));

#define NIRQ 86
__attribute__((section(".isr_vector"), used))
void (*const vectors[16 + NIRQ])(void) = {
    [0] = (void (*)(void))&_estack,
    [1] = Reset_Handler,
    [2 ... 15] = Default_Handler,
    [16 ... 16 + NIRQ - 1] = Default_Handler,
    [15] = SysTick_Handler,
    [16 + 28] = TIM2_IRQHandler,
    [16 + 37] = USART1_IRQHandler,
    [16 + 38] = USART2_IRQHandler,
    [16 + 71] = USART6_IRQHandler,
};

void Reset_Handler(void)
{
    *(volatile uint32_t *)0xE000ED88u |= 0xFu << 20;      /* CPACR: CP10/CP11 full access */
    uint32_t *src = &_sidata, *dst = &_sdata;
    while (dst < &_edata)
        *dst++ = *src++;
    for (dst = &_sbss; dst < &_ebss;)
        *dst++ = 0;
    main();
    for (;;) { }
}
