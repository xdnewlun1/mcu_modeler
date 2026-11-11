/* Startup code for the STM32F105/F107 connectivity-line samples (clang/lld, no libc).
 * Vector table at 0x08000000 (VTOR): initial SP (end of the 64 KB SRAM) + 15 system exceptions + 68 external IRQs
 * (RM0008 10.1.2 Table 61 "Vector table for connectivity line devices"; CMSIS stm32f107xc.h names, those of
 * chips/stm32f107vc.yaml; 43-49 reserved).  Reset_Handler copies .data, zeroes .bss and calls main.  Generated from the
 * chip YAML irqs: table. */
#include <stdint.h>

extern uint32_t _sidata, _sdata, _edata, _sbss, _ebss, _estack;
int main(void);

void Reset_Handler(void);
void Default_Handler(void);

void NMI_Handler(void) __attribute__((weak, alias("Default_Handler")));
void HardFault_Handler(void) __attribute__((weak, alias("Default_Handler")));
void MemManage_Handler(void) __attribute__((weak, alias("Default_Handler")));
void BusFault_Handler(void) __attribute__((weak, alias("Default_Handler")));
void UsageFault_Handler(void) __attribute__((weak, alias("Default_Handler")));
void SVC_Handler(void) __attribute__((weak, alias("Default_Handler")));
void DebugMon_Handler(void) __attribute__((weak, alias("Default_Handler")));
void PendSV_Handler(void) __attribute__((weak, alias("Default_Handler")));
void SysTick_Handler(void) __attribute__((weak, alias("Default_Handler")));
void WWDG_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void PVD_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void TAMPER_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void RTC_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void FLASH_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void RCC_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void EXTI0_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void EXTI1_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void EXTI2_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void EXTI3_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void EXTI4_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA1_Channel1_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA1_Channel2_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA1_Channel3_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA1_Channel4_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA1_Channel5_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA1_Channel6_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA1_Channel7_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void ADC1_2_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CAN1_TX_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CAN1_RX0_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CAN1_RX1_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CAN1_SCE_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void EXTI9_5_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void TIM1_BRK_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void TIM1_UP_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void TIM1_TRG_COM_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void TIM1_CC_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void TIM2_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void TIM3_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void TIM4_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void I2C1_EV_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void I2C1_ER_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void I2C2_EV_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void I2C2_ER_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void SPI1_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void SPI2_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void USART1_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void USART2_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void USART3_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void EXTI15_10_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void RTC_Alarm_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void OTG_FS_WKUP_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void TIM5_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void SPI3_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void UART4_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void UART5_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void TIM6_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void TIM7_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA2_Channel1_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA2_Channel2_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA2_Channel3_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA2_Channel4_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA2_Channel5_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void ETH_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void ETH_WKUP_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CAN2_TX_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CAN2_RX0_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CAN2_RX1_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CAN2_SCE_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void OTG_FS_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));

__attribute__((section(".isr_vector"), used))
void (*const vectors[16 + 68])(void) = {
    (void (*)(void))&_estack,
    Reset_Handler,
    NMI_Handler,
    HardFault_Handler,
    MemManage_Handler,
    BusFault_Handler,
    UsageFault_Handler,
    0,
    0,
    0,
    0,
    SVC_Handler,
    DebugMon_Handler,
    0,
    PendSV_Handler,
    SysTick_Handler,
    WWDG_IRQHandler,   /* 0 */
    PVD_IRQHandler,   /* 1 */
    TAMPER_IRQHandler,   /* 2 */
    RTC_IRQHandler,   /* 3 */
    FLASH_IRQHandler,   /* 4 */
    RCC_IRQHandler,   /* 5 */
    EXTI0_IRQHandler,   /* 6 */
    EXTI1_IRQHandler,   /* 7 */
    EXTI2_IRQHandler,   /* 8 */
    EXTI3_IRQHandler,   /* 9 */
    EXTI4_IRQHandler,   /* 10 */
    DMA1_Channel1_IRQHandler,   /* 11 */
    DMA1_Channel2_IRQHandler,   /* 12 */
    DMA1_Channel3_IRQHandler,   /* 13 */
    DMA1_Channel4_IRQHandler,   /* 14 */
    DMA1_Channel5_IRQHandler,   /* 15 */
    DMA1_Channel6_IRQHandler,   /* 16 */
    DMA1_Channel7_IRQHandler,   /* 17 */
    ADC1_2_IRQHandler,   /* 18 */
    CAN1_TX_IRQHandler,   /* 19 */
    CAN1_RX0_IRQHandler,   /* 20 */
    CAN1_RX1_IRQHandler,   /* 21 */
    CAN1_SCE_IRQHandler,   /* 22 */
    EXTI9_5_IRQHandler,   /* 23 */
    TIM1_BRK_IRQHandler,   /* 24 */
    TIM1_UP_IRQHandler,   /* 25 */
    TIM1_TRG_COM_IRQHandler,   /* 26 */
    TIM1_CC_IRQHandler,   /* 27 */
    TIM2_IRQHandler,   /* 28 */
    TIM3_IRQHandler,   /* 29 */
    TIM4_IRQHandler,   /* 30 */
    I2C1_EV_IRQHandler,   /* 31 */
    I2C1_ER_IRQHandler,   /* 32 */
    I2C2_EV_IRQHandler,   /* 33 */
    I2C2_ER_IRQHandler,   /* 34 */
    SPI1_IRQHandler,   /* 35 */
    SPI2_IRQHandler,   /* 36 */
    USART1_IRQHandler,   /* 37 */
    USART2_IRQHandler,   /* 38 */
    USART3_IRQHandler,   /* 39 */
    EXTI15_10_IRQHandler,   /* 40 */
    RTC_Alarm_IRQHandler,   /* 41 */
    OTG_FS_WKUP_IRQHandler,   /* 42 */
    0,   /* 43 */
    0,   /* 44 */
    0,   /* 45 */
    0,   /* 46 */
    0,   /* 47 */
    0,   /* 48 */
    0,   /* 49 */
    TIM5_IRQHandler,   /* 50 */
    SPI3_IRQHandler,   /* 51 */
    UART4_IRQHandler,   /* 52 */
    UART5_IRQHandler,   /* 53 */
    TIM6_IRQHandler,   /* 54 */
    TIM7_IRQHandler,   /* 55 */
    DMA2_Channel1_IRQHandler,   /* 56 */
    DMA2_Channel2_IRQHandler,   /* 57 */
    DMA2_Channel3_IRQHandler,   /* 58 */
    DMA2_Channel4_IRQHandler,   /* 59 */
    DMA2_Channel5_IRQHandler,   /* 60 */
    ETH_IRQHandler,   /* 61 */
    ETH_WKUP_IRQHandler,   /* 62 */
    CAN2_TX_IRQHandler,   /* 63 */
    CAN2_RX0_IRQHandler,   /* 64 */
    CAN2_RX1_IRQHandler,   /* 65 */
    CAN2_SCE_IRQHandler,   /* 66 */
    OTG_FS_IRQHandler,   /* 67 */
};

void Reset_Handler(void)
{
    uint32_t *src = &_sidata, *dst = &_sdata;
    while (dst < &_edata)
        *dst++ = *src++;
    for (dst = &_sbss; dst < &_ebss;)
        *dst++ = 0;
    main();
    for (;;)
        __asm__ volatile("wfi");
}

void Default_Handler(void)
{
    for (;;)
        ;
}
