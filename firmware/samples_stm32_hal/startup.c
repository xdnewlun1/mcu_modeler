/* Startup code for samples_stm32_hal (STM32F405ZG, ST STM32CubeF4 HAL; clang/lld, no libc).
 * Same layout as firmware/samples_stm32/startup.c: vector table at 0x08000000 (initial SP 0x20020000, 15 system
 * exceptions, 82 external IRQs in RM0090 Table 61 order), but the Reset_Handler follows ST's
 * startup_stm32f405xx.s: SystemInit() (CMSIS; FPU access + VTOR) first, then .data copy, .bss clear, main().
 * memset/memcpy are provided because clang may emit calls to them for struct initialisers (no libc here). */
#include <stdint.h>
#include <stddef.h>
#include "stm32f4xx.h"

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
void TAMP_STAMP_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void RTC_WKUP_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void FLASH_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void RCC_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void EXTI0_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void EXTI1_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void EXTI2_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void EXTI3_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void EXTI4_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA1_Stream0_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA1_Stream1_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA1_Stream2_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA1_Stream3_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA1_Stream4_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA1_Stream5_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA1_Stream6_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void ADC_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CAN1_TX_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CAN1_RX0_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CAN1_RX1_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CAN1_SCE_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void EXTI9_5_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void TIM1_BRK_TIM9_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void TIM1_UP_TIM10_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void TIM1_TRG_COM_TIM11_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
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
void TIM8_BRK_TIM12_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void TIM8_UP_TIM13_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void TIM8_TRG_COM_TIM14_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void TIM8_CC_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA1_Stream7_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void FSMC_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void SDIO_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void TIM5_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void SPI3_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void UART4_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void UART5_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void TIM6_DAC_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void TIM7_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA2_Stream0_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA2_Stream1_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA2_Stream2_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA2_Stream3_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA2_Stream4_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void ETH_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void ETH_WKUP_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CAN2_TX_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CAN2_RX0_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CAN2_RX1_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CAN2_SCE_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void OTG_FS_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA2_Stream5_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA2_Stream6_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA2_Stream7_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void USART6_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void I2C3_EV_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void I2C3_ER_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void OTG_HS_EP1_OUT_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void OTG_HS_EP1_IN_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void OTG_HS_WKUP_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void OTG_HS_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DCMI_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CRYP_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void HASH_RNG_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void FPU_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));

typedef void (*vector_t)(void);

__attribute__((section(".isr_vector"), used))
const vector_t __vector_table[16 + 82] = {
    (vector_t)&_estack,             /* 0x20020000: top of SRAM */
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
    WWDG_IRQHandler,                        /* 0 */
    PVD_IRQHandler,                         /* 1 */
    TAMP_STAMP_IRQHandler,                  /* 2 */
    RTC_WKUP_IRQHandler,                    /* 3 */
    FLASH_IRQHandler,                       /* 4 */
    RCC_IRQHandler,                         /* 5 */
    EXTI0_IRQHandler,                       /* 6 */
    EXTI1_IRQHandler,                       /* 7 */
    EXTI2_IRQHandler,                       /* 8 */
    EXTI3_IRQHandler,                       /* 9 */
    EXTI4_IRQHandler,                       /* 10 */
    DMA1_Stream0_IRQHandler,                /* 11 */
    DMA1_Stream1_IRQHandler,                /* 12 */
    DMA1_Stream2_IRQHandler,                /* 13 */
    DMA1_Stream3_IRQHandler,                /* 14 */
    DMA1_Stream4_IRQHandler,                /* 15 */
    DMA1_Stream5_IRQHandler,                /* 16 */
    DMA1_Stream6_IRQHandler,                /* 17 */
    ADC_IRQHandler,                         /* 18 */
    CAN1_TX_IRQHandler,                     /* 19 */
    CAN1_RX0_IRQHandler,                    /* 20 */
    CAN1_RX1_IRQHandler,                    /* 21 */
    CAN1_SCE_IRQHandler,                    /* 22 */
    EXTI9_5_IRQHandler,                     /* 23 */
    TIM1_BRK_TIM9_IRQHandler,               /* 24 */
    TIM1_UP_TIM10_IRQHandler,               /* 25 */
    TIM1_TRG_COM_TIM11_IRQHandler,          /* 26 */
    TIM1_CC_IRQHandler,                     /* 27 */
    TIM2_IRQHandler,                        /* 28 */
    TIM3_IRQHandler,                        /* 29 */
    TIM4_IRQHandler,                        /* 30 */
    I2C1_EV_IRQHandler,                     /* 31 */
    I2C1_ER_IRQHandler,                     /* 32 */
    I2C2_EV_IRQHandler,                     /* 33 */
    I2C2_ER_IRQHandler,                     /* 34 */
    SPI1_IRQHandler,                        /* 35 */
    SPI2_IRQHandler,                        /* 36 */
    USART1_IRQHandler,                      /* 37 */
    USART2_IRQHandler,                      /* 38 */
    USART3_IRQHandler,                      /* 39 */
    EXTI15_10_IRQHandler,                   /* 40 */
    RTC_Alarm_IRQHandler,                   /* 41 */
    OTG_FS_WKUP_IRQHandler,                 /* 42 */
    TIM8_BRK_TIM12_IRQHandler,              /* 43 */
    TIM8_UP_TIM13_IRQHandler,               /* 44 */
    TIM8_TRG_COM_TIM14_IRQHandler,          /* 45 */
    TIM8_CC_IRQHandler,                     /* 46 */
    DMA1_Stream7_IRQHandler,                /* 47 */
    FSMC_IRQHandler,                        /* 48 */
    SDIO_IRQHandler,                        /* 49 */
    TIM5_IRQHandler,                        /* 50 */
    SPI3_IRQHandler,                        /* 51 */
    UART4_IRQHandler,                       /* 52 */
    UART5_IRQHandler,                       /* 53 */
    TIM6_DAC_IRQHandler,                    /* 54 */
    TIM7_IRQHandler,                        /* 55 */
    DMA2_Stream0_IRQHandler,                /* 56 */
    DMA2_Stream1_IRQHandler,                /* 57 */
    DMA2_Stream2_IRQHandler,                /* 58 */
    DMA2_Stream3_IRQHandler,                /* 59 */
    DMA2_Stream4_IRQHandler,                /* 60 */
    ETH_IRQHandler,                         /* 61 */
    ETH_WKUP_IRQHandler,                    /* 62 */
    CAN2_TX_IRQHandler,                     /* 63 */
    CAN2_RX0_IRQHandler,                    /* 64 */
    CAN2_RX1_IRQHandler,                    /* 65 */
    CAN2_SCE_IRQHandler,                    /* 66 */
    OTG_FS_IRQHandler,                      /* 67 */
    DMA2_Stream5_IRQHandler,                /* 68 */
    DMA2_Stream6_IRQHandler,                /* 69 */
    DMA2_Stream7_IRQHandler,                /* 70 */
    USART6_IRQHandler,                      /* 71 */
    I2C3_EV_IRQHandler,                     /* 72 */
    I2C3_ER_IRQHandler,                     /* 73 */
    OTG_HS_EP1_OUT_IRQHandler,              /* 74 */
    OTG_HS_EP1_IN_IRQHandler,               /* 75 */
    OTG_HS_WKUP_IRQHandler,                 /* 76 */
    OTG_HS_IRQHandler,                      /* 77 */
    DCMI_IRQHandler,                        /* 78 */
    CRYP_IRQHandler,                        /* 79 */
    HASH_RNG_IRQHandler,                    /* 80 */
    FPU_IRQHandler,                         /* 81 */
};

void Default_Handler(void)
{
    for (;;) { }
}

void *memset(void *d, int c, size_t n)
{
    uint8_t *p = d;
    while (n--)
        *p++ = (uint8_t)c;
    return d;
}

void *memcpy(void *d, const void *s, size_t n)
{
    uint8_t *p = d;
    const uint8_t *q = s;
    while (n--)
        *p++ = *q++;
    return d;
}

__attribute__((noreturn)) void Reset_Handler(void)
{
    SystemInit();                  /* CMSIS system_stm32f4xx.c: CPACR CP10/CP11 full access, VTOR = 0x08000000 */

    uint32_t *src = &_sidata;
    for (uint32_t *dst = &_sdata; dst < &_edata; )
        *dst++ = *src++;
    for (uint32_t *dst = &_sbss; dst < &_ebss; )
        *dst++ = 0;

    main();
    for (;;) { }
}
