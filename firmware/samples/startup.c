/* Startup code for the Kinetis K22F samples (clang/lld, no libc).
 * Vector table: initial SP + 15 system exceptions + 82 external IRQs (MK22F12 numbering), Kinetis flash
 * configuration field at 0x400, Reset_Handler that enables the FPU, copies .data, zeroes .bss and calls main. */
#include <stdint.h>
#include "k22.h"

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
void DMA0_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA1_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA2_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA3_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA4_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA5_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA6_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA7_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA8_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA9_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA10_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA11_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA12_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA13_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA14_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA15_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DMA_Error_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void MCM_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void FTFE_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void Read_Collision_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void LVD_LVW_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void LLWU_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void WDOG_EWM_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void RNG_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void I2C0_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void I2C1_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void SPI0_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void SPI1_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void I2S0_Tx_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void I2S0_Rx_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void UART0_LON_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void UART0_RX_TX_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void UART0_ERR_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void UART1_RX_TX_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void UART1_ERR_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void UART2_RX_TX_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void UART2_ERR_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void UART3_RX_TX_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void UART3_ERR_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void ADC0_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CMP0_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CMP1_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void FTM0_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void FTM1_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void FTM2_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CMT_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void RTC_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void RTC_Seconds_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void PIT0_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void PIT1_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void PIT2_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void PIT3_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void PDB0_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void USB0_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void USBDCD_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void Reserved71_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DAC0_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void MCG_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void LPTMR0_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void PORTA_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void PORTB_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void PORTC_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void PORTD_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void PORTE_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void SWI_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void SPI2_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void UART4_RX_TX_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void UART4_ERR_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void UART5_RX_TX_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void UART5_ERR_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CMP2_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void FTM3_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void DAC1_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void ADC1_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void I2C2_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CAN0_ORed_Message_buffer_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CAN0_Bus_Off_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CAN0_Error_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CAN0_Tx_Warning_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CAN0_Rx_Warning_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void CAN0_Wake_Up_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void SDHC_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));

typedef void (*vector_t)(void);

__attribute__((section(".isr_vector"), used))
const vector_t __vector_table[16 + 82] = {
    (vector_t)&_estack,             /* 0x20010000: top of SRAM_U */
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
    DMA0_IRQHandler,                            /* 0 */
    DMA1_IRQHandler,                            /* 1 */
    DMA2_IRQHandler,                            /* 2 */
    DMA3_IRQHandler,                            /* 3 */
    DMA4_IRQHandler,                            /* 4 */
    DMA5_IRQHandler,                            /* 5 */
    DMA6_IRQHandler,                            /* 6 */
    DMA7_IRQHandler,                            /* 7 */
    DMA8_IRQHandler,                            /* 8 */
    DMA9_IRQHandler,                            /* 9 */
    DMA10_IRQHandler,                           /* 10 */
    DMA11_IRQHandler,                           /* 11 */
    DMA12_IRQHandler,                           /* 12 */
    DMA13_IRQHandler,                           /* 13 */
    DMA14_IRQHandler,                           /* 14 */
    DMA15_IRQHandler,                           /* 15 */
    DMA_Error_IRQHandler,                       /* 16 */
    MCM_IRQHandler,                             /* 17 */
    FTFE_IRQHandler,                            /* 18 */
    Read_Collision_IRQHandler,                  /* 19 */
    LVD_LVW_IRQHandler,                         /* 20 */
    LLWU_IRQHandler,                            /* 21 */
    WDOG_EWM_IRQHandler,                        /* 22 */
    RNG_IRQHandler,                             /* 23 */
    I2C0_IRQHandler,                            /* 24 */
    I2C1_IRQHandler,                            /* 25 */
    SPI0_IRQHandler,                            /* 26 */
    SPI1_IRQHandler,                            /* 27 */
    I2S0_Tx_IRQHandler,                         /* 28 */
    I2S0_Rx_IRQHandler,                         /* 29 */
    UART0_LON_IRQHandler,                       /* 30 */
    UART0_RX_TX_IRQHandler,                     /* 31 */
    UART0_ERR_IRQHandler,                       /* 32 */
    UART1_RX_TX_IRQHandler,                     /* 33 */
    UART1_ERR_IRQHandler,                       /* 34 */
    UART2_RX_TX_IRQHandler,                     /* 35 */
    UART2_ERR_IRQHandler,                       /* 36 */
    UART3_RX_TX_IRQHandler,                     /* 37 */
    UART3_ERR_IRQHandler,                       /* 38 */
    ADC0_IRQHandler,                            /* 39 */
    CMP0_IRQHandler,                            /* 40 */
    CMP1_IRQHandler,                            /* 41 */
    FTM0_IRQHandler,                            /* 42 */
    FTM1_IRQHandler,                            /* 43 */
    FTM2_IRQHandler,                            /* 44 */
    CMT_IRQHandler,                             /* 45 */
    RTC_IRQHandler,                             /* 46 */
    RTC_Seconds_IRQHandler,                     /* 47 */
    PIT0_IRQHandler,                            /* 48 */
    PIT1_IRQHandler,                            /* 49 */
    PIT2_IRQHandler,                            /* 50 */
    PIT3_IRQHandler,                            /* 51 */
    PDB0_IRQHandler,                            /* 52 */
    USB0_IRQHandler,                            /* 53 */
    USBDCD_IRQHandler,                          /* 54 */
    Reserved71_IRQHandler,                      /* 55 */
    DAC0_IRQHandler,                            /* 56 */
    MCG_IRQHandler,                             /* 57 */
    LPTMR0_IRQHandler,                          /* 58 */
    PORTA_IRQHandler,                           /* 59 */
    PORTB_IRQHandler,                           /* 60 */
    PORTC_IRQHandler,                           /* 61 */
    PORTD_IRQHandler,                           /* 62 */
    PORTE_IRQHandler,                           /* 63 */
    SWI_IRQHandler,                             /* 64 */
    SPI2_IRQHandler,                            /* 65 */
    UART4_RX_TX_IRQHandler,                     /* 66 */
    UART4_ERR_IRQHandler,                       /* 67 */
    UART5_RX_TX_IRQHandler,                     /* 68 */
    UART5_ERR_IRQHandler,                       /* 69 */
    CMP2_IRQHandler,                            /* 70 */
    FTM3_IRQHandler,                            /* 71 */
    DAC1_IRQHandler,                            /* 72 */
    ADC1_IRQHandler,                            /* 73 */
    I2C2_IRQHandler,                            /* 74 */
    CAN0_ORed_Message_buffer_IRQHandler,        /* 75 */
    CAN0_Bus_Off_IRQHandler,                    /* 76 */
    CAN0_Error_IRQHandler,                      /* 77 */
    CAN0_Tx_Warning_IRQHandler,                 /* 78 */
    CAN0_Rx_Warning_IRQHandler,                 /* 79 */
    CAN0_Wake_Up_IRQHandler,                    /* 80 */
    SDHC_IRQHandler,                            /* 81 */
};

/* Flash configuration field (0x400..0x40F): backdoor key, FPROT, FSEC=0xFE (unsecured), FOPT, FEPROT, FDPROT. */
__attribute__((section(".flash_config"), used))
const uint32_t __flash_config[4] = { 0xFFFFFFFFu, 0xFFFFFFFFu, 0xFFFFFFFFu, 0xFFFFFFFEu };

void Default_Handler(void)
{
    for (;;) { }
}

void WDOG_Disable(void)
{
    /* The unlock sequence must complete within 20 bus clocks, then the WDOG registers are writable
     * for the WCT window. */
    WDOG_UNLOCK = WDOG_UNLOCK_KEY1;
    WDOG_UNLOCK = WDOG_UNLOCK_KEY2;
    __asm volatile ("nop");
    __asm volatile ("nop");
    WDOG_STCTRLH &= (uint16_t)~WDOG_STCTRLH_WDOGEN;
}

__attribute__((noreturn)) void Reset_Handler(void)
{
    /* CP10/CP11 full access: the samples are built with -mfloat-abi=hard. */
    SCB_CPACR |= (0xFu << 20);
    __asm volatile ("dsb\n isb" ::: "memory");

    uint32_t *src = &_sidata;
    for (uint32_t *dst = &_sdata; dst < &_edata; )
        *dst++ = *src++;
    for (uint32_t *dst = &_sbss; dst < &_ebss; )
        *dst++ = 0;

    main();
    for (;;) { }
}
