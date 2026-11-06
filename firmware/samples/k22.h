/* Minimal register definitions for the NXP/Freescale Kinetis K22F (MK22FN1M0VLL12 / MK22FX512VLL12).
 * Offsets from the K22P100M120SF5 reference manual (and the ARMv7-M architecture manual for the SCS).
 * Only what the CHIP_EM sample programs need. */
#ifndef K22_H
#define K22_H
#include <stdint.h>

#define REG32(a) (*(volatile uint32_t *)(a))
#define REG16(a) (*(volatile uint16_t *)(a))
#define REG8(a)  (*(volatile uint8_t  *)(a))

/* ---- Clocks (assumed by the samples; the samples do not program the MCG) ---- */
#define CORE_CLK_HZ 120000000u
#define BUS_CLK_HZ   60000000u

/* ---- SIM: System Integration Module (0x40047000) ---- */
#define SIM_BASE     0x40047000u
#define SIM_SOPT1    REG32(SIM_BASE + 0x0000)
#define SIM_SOPT2    REG32(SIM_BASE + 0x1004)
#define SIM_SCGC4    REG32(SIM_BASE + 0x1034)
#define SIM_SCGC5    REG32(SIM_BASE + 0x1038)
#define SIM_SCGC6    REG32(SIM_BASE + 0x103C)
#define SIM_SCGC7    REG32(SIM_BASE + 0x1040)
#define SIM_CLKDIV1  REG32(SIM_BASE + 0x1044)
#define SIM_SCGC4_UART0  (1u << 10)
#define SIM_SCGC4_UART1  (1u << 11)
#define SIM_SCGC4_UART2  (1u << 12)
#define SIM_SCGC4_UART3  (1u << 13)
#define SIM_SCGC5_LPTMR  (1u << 0)
#define SIM_SCGC5_PORTA  (1u << 9)
#define SIM_SCGC5_PORTB  (1u << 10)
#define SIM_SCGC5_PORTC  (1u << 11)
#define SIM_SCGC5_PORTD  (1u << 12)
#define SIM_SCGC5_PORTE  (1u << 13)
#define SIM_SCGC6_PIT    (1u << 23)
#define SIM_SCGC6_ADC0   (1u << 27)
#define SIM_SCGC6_RTC    (1u << 29)

/* ---- PORT: pin control (PORTA 0x40049000 .. PORTE 0x4004D000) ---- */
typedef struct {
    volatile uint32_t PCR[32];   /* 0x00 */
    volatile uint32_t GPCLR;     /* 0x80 */
    volatile uint32_t GPCHR;     /* 0x84 */
    uint32_t _r0[6];
    volatile uint32_t ISFR;      /* 0xA0 */
} PORT_Type;
#define PORTA ((PORT_Type *)0x40049000u)
#define PORTB ((PORT_Type *)0x4004A000u)
#define PORTC ((PORT_Type *)0x4004B000u)
#define PORTD ((PORT_Type *)0x4004C000u)
#define PORTE ((PORT_Type *)0x4004D000u)
#define PORT_PCR_PS        (1u << 0)            /* pull select: 1 = pull-up */
#define PORT_PCR_PE        (1u << 1)            /* pull enable */
#define PORT_PCR_SRE       (1u << 2)
#define PORT_PCR_PFE       (1u << 4)
#define PORT_PCR_ODE       (1u << 5)
#define PORT_PCR_DSE       (1u << 6)
#define PORT_PCR_MUX(x)    (((uint32_t)(x) & 7u) << 8)
#define PORT_PCR_IRQC(x)   (((uint32_t)(x) & 0xFu) << 16)
#define PORT_PCR_ISF       (1u << 24)
#define PORT_IRQC_DISABLED 0x0u
#define PORT_IRQC_RISING   0x9u
#define PORT_IRQC_FALLING  0xAu
#define PORT_IRQC_EITHER   0xBu
#define PORT_IRQC_LOW      0x8u

/* ---- GPIO (PTA 0x400FF000, PTB +0x40, ...) ---- */
typedef struct {
    volatile uint32_t PDOR;      /* 0x00 data output */
    volatile uint32_t PSOR;      /* 0x04 set output */
    volatile uint32_t PCOR;      /* 0x08 clear output */
    volatile uint32_t PTOR;      /* 0x0C toggle output */
    volatile uint32_t PDIR;      /* 0x10 data input */
    volatile uint32_t PDDR;      /* 0x14 data direction (1 = output) */
} GPIO_Type;
#define GPIOA ((GPIO_Type *)0x400FF000u)
#define GPIOB ((GPIO_Type *)0x400FF040u)
#define GPIOC ((GPIO_Type *)0x400FF080u)
#define GPIOD ((GPIO_Type *)0x400FF0C0u)
#define GPIOE ((GPIO_Type *)0x400FF100u)

/* ---- WDOG (0x40052000), 16-bit registers ---- */
#define WDOG_BASE     0x40052000u
#define WDOG_STCTRLH  REG16(WDOG_BASE + 0x00)
#define WDOG_STCTRLL  REG16(WDOG_BASE + 0x02)
#define WDOG_TOVALH   REG16(WDOG_BASE + 0x04)
#define WDOG_TOVALL   REG16(WDOG_BASE + 0x06)
#define WDOG_WINH     REG16(WDOG_BASE + 0x08)
#define WDOG_WINL     REG16(WDOG_BASE + 0x0A)
#define WDOG_REFRESH  REG16(WDOG_BASE + 0x0C)
#define WDOG_UNLOCK   REG16(WDOG_BASE + 0x0E)
#define WDOG_TMROUTH  REG16(WDOG_BASE + 0x10)
#define WDOG_TMROUTL  REG16(WDOG_BASE + 0x12)
#define WDOG_RSTCNT   REG16(WDOG_BASE + 0x14)
#define WDOG_PRESC    REG16(WDOG_BASE + 0x16)
#define WDOG_STCTRLH_WDOGEN (1u << 0)
#define WDOG_UNLOCK_KEY1 0xC520u
#define WDOG_UNLOCK_KEY2 0xD928u

/* ---- PIT (0x40037000) ---- */
#define PIT_BASE        0x40037000u
#define PIT_MCR         REG32(PIT_BASE + 0x000)
#define PIT_LDVAL(n)    REG32(PIT_BASE + 0x100 + 0x10 * (n))
#define PIT_CVAL(n)     REG32(PIT_BASE + 0x104 + 0x10 * (n))
#define PIT_TCTRL(n)    REG32(PIT_BASE + 0x108 + 0x10 * (n))
#define PIT_TFLG(n)     REG32(PIT_BASE + 0x10C + 0x10 * (n))
#define PIT_MCR_FRZ     (1u << 0)
#define PIT_MCR_MDIS    (1u << 1)
#define PIT_TCTRL_TEN   (1u << 0)
#define PIT_TCTRL_TIE   (1u << 1)
#define PIT_TCTRL_CHN   (1u << 2)
#define PIT_TFLG_TIF    (1u << 0)

/* ---- UART0 (0x4006A000), 8-bit registers. UART1-3 at +0x1000*n, UART4 at 0x400AB000 ---- */
#define UART0_BASE  0x4006A000u
#define UART0_BDH   REG8(UART0_BASE + 0x00)
#define UART0_BDL   REG8(UART0_BASE + 0x01)
#define UART0_C1    REG8(UART0_BASE + 0x02)
#define UART0_C2    REG8(UART0_BASE + 0x03)
#define UART0_S1    REG8(UART0_BASE + 0x04)
#define UART0_S2    REG8(UART0_BASE + 0x05)
#define UART0_C3    REG8(UART0_BASE + 0x06)
#define UART0_D     REG8(UART0_BASE + 0x07)
#define UART0_C4    REG8(UART0_BASE + 0x0A)
#define UART0_C5    REG8(UART0_BASE + 0x0B)
#define UART_C2_RE   (1u << 2)
#define UART_C2_TE   (1u << 3)
#define UART_C2_RIE  (1u << 5)
#define UART_C2_TIE  (1u << 7)
#define UART_S1_RDRF (1u << 5)
#define UART_S1_TC   (1u << 6)
#define UART_S1_TDRE (1u << 7)
#define UART_C4_BRFA(x) ((uint8_t)((x) & 0x1Fu))

/* ---- ARMv7-M System Control Space ---- */
#define SYST_CSR    REG32(0xE000E010u)
#define SYST_RVR    REG32(0xE000E014u)
#define SYST_CVR    REG32(0xE000E018u)
#define SYST_CALIB  REG32(0xE000E01Cu)
#define SYST_CSR_ENABLE    (1u << 0)
#define SYST_CSR_TICKINT   (1u << 1)
#define SYST_CSR_CLKSOURCE (1u << 2)            /* 1 = core clock */
#define SYST_CSR_COUNTFLAG (1u << 16)
#define NVIC_ISER(n) REG32(0xE000E100u + 4u * (n))
#define NVIC_ICER(n) REG32(0xE000E180u + 4u * (n))
#define NVIC_ISPR(n) REG32(0xE000E200u + 4u * (n))
#define NVIC_ICPR(n) REG32(0xE000E280u + 4u * (n))
#define NVIC_IPR8(n) REG8(0xE000E400u + (n))
#define SCB_ICSR    REG32(0xE000ED04u)
#define SCB_VTOR    REG32(0xE000ED08u)
#define SCB_AIRCR   REG32(0xE000ED0Cu)
#define SCB_SHPR3   REG32(0xE000ED20u)
#define SCB_CPACR   REG32(0xE000ED88u)

/* IRQ numbers used by the samples (MK22F12 header) */
#define PIT0_IRQn         48
#define PORTA_IRQn        59
#define PORTC_IRQn        61
#define UART0_RX_TX_IRQn  31

static inline void NVIC_EnableIRQ(int irq)  { NVIC_ISER(irq >> 5) = 1u << (irq & 31); }
static inline void NVIC_DisableIRQ(int irq) { NVIC_ICER(irq >> 5) = 1u << (irq & 31); }
static inline void NVIC_ClearPendingIRQ(int irq) { NVIC_ICPR(irq >> 5) = 1u << (irq & 31); }
static inline void __enable_irq(void)  { __asm volatile ("cpsie i" ::: "memory"); }
static inline void __disable_irq(void) { __asm volatile ("cpsid i" ::: "memory"); }
static inline void __WFI(void)         { __asm volatile ("wfi"); }

/* startup.c */
void WDOG_Disable(void);

#endif /* K22_H */
