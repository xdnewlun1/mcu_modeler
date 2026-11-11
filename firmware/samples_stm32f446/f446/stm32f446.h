/* Register helpers for the STM32F446xx samples. Extends ../../samples_stm32/stm32f405.h (same register layout for the
 * F405 blocks) with the F446 additions: RCC PLL_R / PLLSAI / DCKCFGR2 and the new clock enables, PWR over-drive, the
 * QUADSPI registers (stm32f446xx.h QUADSPI_TypeDef), DMA streams, USART2 and the IRQ numbers of stm32f446xx.h.
 * RM0390 is not in the repo: register offsets and bit positions come from the STM32CubeF4 device header. */
#ifndef STM32F446_H
#define STM32F446_H
#include "../../samples_stm32/stm32f405.h"

/* ---- RCC additions ---- */
#define RCC_AHB3ENR     REG32(RCC_BASE + 0x38)
#define RCC_DCKCFGR     REG32(RCC_BASE + 0x8C)
#define RCC_DCKCFGR2    REG32(RCC_BASE + 0x94)
#define RCC_CR_HSEBYP     (1u << 18)
#define RCC_CFGR_SW_PLLR  3u                       /* F446: SW = 11 selects PLL_R */
#define RCC_CFGR_SWS_PLLR (3u << 2)
#define RCC_AHB1ENR_GPIOCEN (1u << 2)
#define RCC_AHB1ENR_GPIODEN (1u << 3)
#define RCC_AHB1ENR_GPIOEEN (1u << 4)
#define RCC_AHB1ENR_GPIOFEN (1u << 5)
#define RCC_AHB1ENR_DMA2EN  (1u << 22)
#define RCC_AHB3ENR_QSPIEN  (1u << 1)
#define RCC_APB1ENR_USART2EN (1u << 17)

/* ---- PWR ---- */
#define PWR_CSR          REG32(0x40007004u)
#define PWR_CR_VOS_SCALE1 (3u << 14)
#define PWR_CR_ODEN      (1u << 16)
#define PWR_CR_ODSWEN    (1u << 17)
#define PWR_CSR_ODRDY    (1u << 16)
#define PWR_CSR_ODSWRDY  (1u << 17)

#define GPIOD ((GPIO_TypeDef *)0x40020C00u)
#define GPIOE ((GPIO_TypeDef *)0x40021000u)
#define GPIOF ((GPIO_TypeDef *)0x40021400u)

/* ---- USART2 (APB1; NUCLEO virtual COM port on PA2/PA3) ---- */
#define USART2 ((USART_TypeDef *)0x40004400u)

/* ---- QUADSPI (registers 0xA0001000, memory-mapped window 0x90000000) ---- */
typedef struct {
    volatile uint32_t CR, DCR, SR, FCR, DLR, CCR, AR, ABR, DR, PSMKR, PSMAR, PIR, LPTR;
} QUADSPI_TypeDef;
#define QUADSPI ((QUADSPI_TypeDef *)0xA0001000u)
#define QSPI_MM ((volatile uint8_t *)0x90000000u)
#define QSPI_CR_EN      (1u << 0)
#define QSPI_CR_ABORT   (1u << 1)
#define QSPI_CR_DMAEN   (1u << 2)
#define QSPI_CR_FTHRES(n) (((n) - 1u) << 8)
#define QSPI_CR_TCIE    (1u << 17)
#define QSPI_CR_SMIE    (1u << 19)
#define QSPI_CR_APMS    (1u << 22)
#define QSPI_CR_PRESCALER(n) ((n) << 24)
#define QSPI_DCR_FSIZE(n) ((n) << 16)
#define QSPI_DCR_CSHT(n)  ((n) << 8)
#define QSPI_SR_TEF  (1u << 0)
#define QSPI_SR_TCF  (1u << 1)
#define QSPI_SR_FTF  (1u << 2)
#define QSPI_SR_SMF  (1u << 3)
#define QSPI_SR_TOF  (1u << 4)
#define QSPI_SR_BUSY (1u << 5)
#define QSPI_FCR_CTCF (1u << 1)
#define QSPI_FCR_CSMF (1u << 3)
/* CCR fields */
#define CCR_INSTR(i)   ((uint32_t)(i))
#define CCR_IMODE(m)   ((m) << 8)               /* 0 none, 1 = 1 line, 2 = 2 lines, 3 = 4 lines */
#define CCR_ADMODE(m)  ((m) << 10)
#define CCR_ADSIZE(s)  ((s) << 12)              /* 0 = 8 .. 3 = 32 bit */
#define CCR_ABMODE(m)  ((m) << 14)
#define CCR_ABSIZE(s)  ((s) << 16)
#define CCR_DCYC(n)    ((n) << 18)
#define CCR_DMODE(m)   ((m) << 24)
#define CCR_FMODE(m)   ((m) << 26)              /* 0 indirect write, 1 indirect read, 2 auto polling, 3 memory-mapped */
#define FMODE_WRITE 0u
#define FMODE_READ  1u
#define FMODE_POLL  2u
#define FMODE_MM    3u

/* ---- DMA2 (0x40026400) ---- */
typedef struct { volatile uint32_t CR, NDTR, PAR, M0AR, M1AR, FCR; } DMA_Stream_TypeDef;
#define DMA2_HISR   REG32(0x40026404u)
#define DMA2_HIFCR  REG32(0x4002640Cu)
#define DMA2_Stream7 ((DMA_Stream_TypeDef *)(0x40026410u + 0x18u * 7u))
#define DMA_SxCR_EN   (1u << 0)
#define DMA_SxCR_TCIE (1u << 4)
#define DMA_SxCR_MINC (1u << 10)
#define DMA_SxCR_CHSEL(n) ((n) << 25)
#define DMA_HISR_TCIF7 (1u << 27)

/* ---- FMPI2C1 (0x40006000, APB1; RM0390 23.9 register map) ---- */
typedef struct {
    volatile uint32_t CR1, CR2, OAR1, OAR2, TIMINGR, TIMEOUTR, ISR, ICR, PECR, RXDR, TXDR;
} FMPI2C_TypeDef;
#define FMPI2C1 ((FMPI2C_TypeDef *)0x40006000u)
#define RCC_APB1ENR_FMPI2C1EN (1u << 24)
#define RCC_DCKCFGR2_FMPI2C1SEL_HSI (2u << 22)
#define SYSCFG_CFGR    REG32(0x4001382Cu)        /* RM0390 8.2.8 (offset 0x2C); FMPI2C1_SCL / FMPI2C1_SDA: Fm+ drive (bits 0 / 1) */
#define FMPI2C_CR1_PE      (1u << 0)
#define FMPI2C_CR1_TXIE    (1u << 1)
#define FMPI2C_CR1_RXIE    (1u << 2)
#define FMPI2C_CR1_NACKIE  (1u << 4)
#define FMPI2C_CR1_STOPIE  (1u << 5)
#define FMPI2C_CR1_TCIE    (1u << 6)
#define FMPI2C_CR2_SADD7(a) ((uint32_t)(a) << 1)
#define FMPI2C_CR2_RD_WRN  (1u << 10)
#define FMPI2C_CR2_START   (1u << 13)
#define FMPI2C_CR2_STOP    (1u << 14)
#define FMPI2C_CR2_NBYTES(n) ((uint32_t)(n) << 16)
#define FMPI2C_CR2_RELOAD  (1u << 24)
#define FMPI2C_CR2_AUTOEND (1u << 25)
#define FMPI2C_ISR_TXE   (1u << 0)
#define FMPI2C_ISR_TXIS  (1u << 1)
#define FMPI2C_ISR_RXNE  (1u << 2)
#define FMPI2C_ISR_NACKF (1u << 4)
#define FMPI2C_ISR_STOPF (1u << 5)
#define FMPI2C_ISR_TC    (1u << 6)
#define FMPI2C_ISR_BUSY  (1u << 15)
#define FMPI2C_ICR_NACKCF (1u << 4)
#define FMPI2C_ICR_STOPCF (1u << 5)

/* ---- IRQ numbers (stm32f446xx.h) ---- */
#define USART2_IRQn  38
#define FMPI2C1_EV_IRQn 95
#define QUADSPI_IRQn 92
#define CEC_IRQn      93
#define SPDIF_RX_IRQn 94

/* ---- HDMI-CEC (0x40006C00, RM0390 32.7) ---- */
typedef struct { volatile uint32_t CR, CFGR, TXDR, RXDR, ISR, IER; } CEC_TypeDef;
#define CEC ((CEC_TypeDef *)0x40006C00u)
#define RCC_APB1ENR_CECEN (1u << 27)
#define CEC_CR_CECEN  (1u << 0)
#define CEC_CR_TXSOM  (1u << 1)
#define CEC_CR_TXEOM  (1u << 2)
#define CEC_CFGR_SFT(n)  ((n) & 7u)
#define CEC_CFGR_SFTOPT  (1u << 8)
#define CEC_CFGR_OAR(a)  (1u << (16u + (a)))     /* own logical address a (0..14) */
#define CEC_CFGR_LSTN    (1u << 31)
#define CEC_ISR_RXBR   (1u << 0)
#define CEC_ISR_RXEND  (1u << 1)
#define CEC_ISR_RXOVR  (1u << 2)
#define CEC_ISR_BRE    (1u << 3)
#define CEC_ISR_SBPE   (1u << 4)
#define CEC_ISR_LBPE   (1u << 5)
#define CEC_ISR_RXACKE (1u << 6)
#define CEC_ISR_ARBLST (1u << 7)
#define CEC_ISR_TXBR   (1u << 8)
#define CEC_ISR_TXEND  (1u << 9)
#define CEC_ISR_TXUDR  (1u << 10)
#define CEC_ISR_TXERR  (1u << 11)
#define CEC_ISR_TXACKE (1u << 12)

/* ---- SPDIFRX (0x40004000, RM0390 27.5) ---- */
typedef struct { volatile uint32_t CR, IMR, SR, IFCR, DR, CSR, DIR; } SPDIFRX_TypeDef;
#define SPDIFRX ((SPDIFRX_TypeDef *)0x40004000u)
#define RCC_APB1ENR_SPDIFRXEN (1u << 16)
#define RCC_PLLI2SCFGR REG32(RCC_BASE + 0x84)
#define RCC_CR_PLLI2SON  (1u << 26)
#define RCC_CR_PLLI2SRDY (1u << 27)
#define SPDIFRX_CR_SPDIFEN(n) ((n) & 3u)        /* 0 idle, 1 sync only, 3 receive */
#define SPDIFRX_CR_RXDMAEN (1u << 2)
#define SPDIFRX_CR_RXSTEO  (1u << 3)
#define SPDIFRX_CR_DRFMT(n) ((n) << 4)
#define SPDIFRX_CR_PMSK    (1u << 6)
#define SPDIFRX_CR_VMSK    (1u << 7)
#define SPDIFRX_CR_CUMSK   (1u << 8)
#define SPDIFRX_CR_PTMSK   (1u << 9)
#define SPDIFRX_CR_CBDMAEN (1u << 10)
#define SPDIFRX_CR_CHSEL   (1u << 11)
#define SPDIFRX_CR_NBTR(n) ((n) << 12)
#define SPDIFRX_CR_WFA     (1u << 14)
#define SPDIFRX_CR_INSEL(n) ((n) << 16)
#define SPDIFRX_SR_RXNE  (1u << 0)
#define SPDIFRX_SR_CSRNE (1u << 1)
#define SPDIFRX_SR_PERR  (1u << 2)
#define SPDIFRX_SR_OVR   (1u << 3)
#define SPDIFRX_SR_SBD   (1u << 4)
#define SPDIFRX_SR_SYNCD (1u << 5)
#define SPDIFRX_SR_FERR  (1u << 6)
#define SPDIFRX_SR_SERR  (1u << 7)
#define SPDIFRX_SR_TERR  (1u << 8)
#define SPDIFRX_IFCR_PERRCF  (1u << 2)
#define SPDIFRX_IFCR_OVRCF   (1u << 3)
#define SPDIFRX_IFCR_SBDCF   (1u << 4)
#define SPDIFRX_IFCR_SYNCDCF (1u << 5)

/* 180 MHz from the 8 MHz HSE (NUCLEO: ST-LINK MCO, HSE bypass) with over-drive, SYSCLK = PLL_R:
 * PLLM 4, PLLN 180 (VCO 360 MHz), PLLP /2 = 180 MHz, PLLR /2 = 180 MHz (SW = 11), PLLQ /7;
 * AHB /1, APB1 /4 = 45 MHz, APB2 /2 = 90 MHz; flash 5 wait states. */
static inline void clock_180mhz_pllr(void)
{
    RCC_APB1ENR |= RCC_APB1ENR_PWREN;
    PWR_CR = (PWR_CR & ~(3u << 14)) | PWR_CR_VOS_SCALE1;
    RCC_CR |= RCC_CR_HSEON | RCC_CR_HSEBYP;
    while (!(RCC_CR & RCC_CR_HSERDY)) { }
    RCC_PLLCFGR = 4u | (180u << 6) | (0u << 16) | RCC_PLLCFGR_SRC_HSE | (7u << 24) | (2u << 28);
    RCC_CR |= RCC_CR_PLLON;
    PWR_CR |= PWR_CR_ODEN;
    while (!(PWR_CSR & PWR_CSR_ODRDY)) { }
    PWR_CR |= PWR_CR_ODSWEN;
    while (!(PWR_CSR & PWR_CSR_ODSWRDY)) { }
    FLASH_ACR = FLASH_ACR_LATENCY_5WS | FLASH_ACR_PRFTEN | FLASH_ACR_ICEN | FLASH_ACR_DCEN;
    RCC_CFGR = (RCC_CFGR & ~((0xFu << 4) | (7u << 10) | (7u << 13))) | RCC_CFGR_PPRE1_DIV4 | RCC_CFGR_PPRE2_DIV2;
    while (!(RCC_CR & RCC_CR_PLLRDY)) { }
    RCC_CFGR = (RCC_CFGR & ~3u) | RCC_CFGR_SW_PLLR;
    while ((RCC_CFGR & RCC_CFGR_SWS_MASK) != RCC_CFGR_SWS_PLLR) { }
}

#endif
