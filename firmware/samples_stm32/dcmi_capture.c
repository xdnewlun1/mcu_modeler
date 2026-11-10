/* dcmi_capture (STM32F407 only: the DCMI camera interface is absent on the F405): captures frames from a parallel
 * 8-bit camera with hardware synchronisation and DMA, 16 MHz HSI.
 *   Pins (AF13): PA6 PIXCLK, PA4 HSYNC, PB7 VSYNC, PC6..PC9 D0..D3, PE4 D4, PB6 D5, PE5 D6, PE6 D7.
 *   DMA2 Stream1 Channel 1 (DCMI request, RM0090 Table 43): DCMI_DR -> frame[], 32-bit words, MINC.
 *   Polarities: VSYNC high and HSYNC low during blanking (VSPOL = 1, HSPOL = 0), rising PIXCLK (PCKPOL = 1).
 *   1) snapshot (CM = 1) of a whole 80x60 frame: waits for RIS.FRAME, prints "DCMI frame 80x60 sum=XXXXXXXX".
 *   2) snapshot with CROP: 40x30 window at (20, 15): prints "DCMI crop 40x30 sum=XXXXXXXX".
 *   3) continuous (CM = 0) with FCRC = 01 (every other frame): counts FRAME interrupts (DCMI_IRQHandler) over a few
 *      frames, clears CAPTURE (stops after the current frame) and prints "DCMI continuous frames=N".
 * The checksum is the 32-bit sum of all captured bytes.  USART1 on PA9 TX (AF7), 115200 8N1. */
#include "stm32f405.h"

typedef struct {
    volatile uint32_t CR, NDTR, PAR, M0AR, M1AR, FCR;
} DMA_Stream_TypeDef;
#define DMA2_BASE   0x40026400u
#define DMA2_LISR   REG32(DMA2_BASE + 0x00)
#define DMA2_LIFCR  REG32(DMA2_BASE + 0x08)
#define DMA2_S(n)   ((DMA_Stream_TypeDef *)(DMA2_BASE + 0x10u + 0x18u * (n)))
#define DMA_CR_EN     (1u << 0)
#define DMA_CR_CIRC   (1u << 8)
#define DMA_CR_MINC   (1u << 10)
#define DMA_CR_PSIZE_W (2u << 11)
#define DMA_CR_MSIZE_W (2u << 13)
#define DMA_CR_CHSEL(c) ((uint32_t)(c) << 25)
#define DMA_LISR_S1_ALL (0x3Du << 6)
#define RCC_AHB1ENR_GPIOCEN (1u << 2)
#define RCC_AHB1ENR_GPIOEEN (1u << 4)
#define RCC_AHB1ENR_DMA2EN (1u << 22)
#define RCC_AHB2ENR   REG32(RCC_BASE + 0x34)
#define RCC_AHB2ENR_DCMIEN (1u << 0)
#define GPIOE ((GPIO_TypeDef *)0x40021000u)

typedef struct {
    volatile uint32_t CR, SR, RIS, IER, MIS, ICR, ESCR, ESUR, CWSTRT, CWSIZE, DR;
} DCMI_TypeDef;
#define DCMI ((DCMI_TypeDef *)0x50050000u)
#define DCMI_CR_CAPTURE (1u << 0)
#define DCMI_CR_CM      (1u << 1)
#define DCMI_CR_CROP    (1u << 2)
#define DCMI_CR_PCKPOL  (1u << 5)
#define DCMI_CR_VSPOL   (1u << 7)
#define DCMI_CR_FCRC_2  (1u << 8)
#define DCMI_CR_ENABLE  (1u << 14)
#define DCMI_RIS_FRAME  (1u << 0)
#define DCMI_RIS_OVR    (1u << 1)
#define DCMI_IRQn 78

#define W 80
#define H 60
uint32_t frame[W * H / 4];
volatile uint32_t irq_frames;
volatile uint32_t irq_ovr;

void DCMI_IRQHandler(void)
{
    uint32_t mis = DCMI->MIS;
    DCMI->ICR = mis;
    if (mis & DCMI_RIS_FRAME)
        irq_frames++;
    if (mis & DCMI_RIS_OVR)
        irq_ovr++;
}

static void putc_(char c)
{
    while (!(USART1->SR & USART_SR_TXE)) { }
    USART1->DR = (uint8_t)c;
}

static void puts_(const char *s)
{
    while (*s)
        putc_(*s++);
}

static void puthex(uint32_t v)
{
    for (int i = 7; i >= 0; i--)
        putc_("0123456789ABCDEF"[(v >> (4 * i)) & 0xF]);
}

static void putdec(uint32_t v)
{
    char b[11];
    int n = 0;
    do { b[n++] = (char)('0' + v % 10); v /= 10; } while (v);
    while (n)
        putc_(b[--n]);
}

static void dma_arm(uint32_t words, int circ)
{
    DMA_Stream_TypeDef *s = DMA2_S(1);
    s->CR = 0;
    while (s->CR & DMA_CR_EN) { }
    DMA2_LIFCR = DMA_LISR_S1_ALL;
    s->PAR = (uint32_t)&DCMI->DR;
    s->M0AR = (uint32_t)frame;
    s->NDTR = words;
    s->FCR = 0;                                   /* direct mode */
    s->CR = DMA_CR_CHSEL(1) | DMA_CR_MINC | DMA_CR_PSIZE_W | DMA_CR_MSIZE_W | (circ ? DMA_CR_CIRC : 0) | DMA_CR_EN;
}

static uint32_t sum_bytes(uint32_t nbytes)
{
    const uint8_t *p = (const uint8_t *)frame;
    uint32_t s = 0;
    for (uint32_t i = 0; i < nbytes; i++)
        s += p[i];
    return s;
}

static void snapshot(uint32_t cr_extra, uint32_t nbytes)
{
    DCMI->CR = 0;
    DCMI->ICR = 0x1F;
    dma_arm(nbytes / 4, 0);
    DCMI->CR = DCMI_CR_PCKPOL | DCMI_CR_VSPOL | DCMI_CR_CM | cr_extra;   /* EDM = 00: 8-bit */
    DCMI->CR |= DCMI_CR_ENABLE;
    DCMI->CR |= DCMI_CR_CAPTURE;
    while (!(DCMI->RIS & DCMI_RIS_FRAME)) { }
    DCMI->ICR = DCMI_RIS_FRAME;
}

int main(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN | RCC_AHB1ENR_GPIOBEN | RCC_AHB1ENR_GPIOCEN | RCC_AHB1ENR_GPIOEEN
                 | RCC_AHB1ENR_DMA2EN;
    RCC_AHB2ENR |= RCC_AHB2ENR_DCMIEN;
    RCC_APB2ENR |= RCC_APB2ENR_USART1EN;

    gpio_af(GPIOA, 9, 7);                         /* USART1_TX */
    USART1->BRR = USART_BRR(HSI_HZ, 115200u);
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE;

    gpio_af(GPIOA, 6, 13);                        /* DCMI_PIXCLK */
    gpio_af(GPIOA, 4, 13);                        /* DCMI_HSYNC */
    gpio_af(GPIOB, 7, 13);                        /* DCMI_VSYNC */
    for (unsigned p = 6; p <= 9; p++)
        gpio_af(GPIOC, p, 13);                    /* D0..D3 */
    gpio_af(GPIOE, 4, 13);                        /* D4 */
    gpio_af(GPIOB, 6, 13);                        /* D5 */
    gpio_af(GPIOE, 5, 13);                        /* D6 */
    gpio_af(GPIOE, 6, 13);                        /* D7 */

    puts_("DCMI ready\r\n");

    snapshot(0, W * H);
    puts_("DCMI frame 80x60 sum=");
    puthex(sum_bytes(W * H));
    puts_("\r\n");

    DCMI->CWSTRT = (15u << 16) | 20u;             /* VST = line 15, HOFFCNT = pixel clock 20 */
    DCMI->CWSIZE = ((30u - 1u) << 16) | (40u - 1u);
    snapshot(DCMI_CR_CROP, 40 * 30);
    puts_("DCMI crop 40x30 sum=");
    puthex(sum_bytes(40 * 30));
    puts_("\r\n");

    /* continuous, every other frame, FRAME interrupt */
    DCMI->CR = 0;
    DCMI->ICR = 0x1F;
    irq_frames = 0;
    dma_arm(W * H / 4, 1);
    DCMI->IER = DCMI_RIS_FRAME | DCMI_RIS_OVR;
    NVIC_EnableIRQ(DCMI_IRQn);
    __enable_irq();
    DCMI->CR = DCMI_CR_PCKPOL | DCMI_CR_VSPOL | DCMI_CR_FCRC_2 | DCMI_CR_ENABLE;
    DCMI->CR |= DCMI_CR_CAPTURE;
    while (irq_frames < 3) { }
    DCMI->CR &= ~DCMI_CR_CAPTURE;                 /* continuous: stops after the current frame */
    while (DCMI->CR & DCMI_CR_CAPTURE) { }
    puts_("DCMI continuous frames=");
    putdec(irq_frames);
    puts_(" ovr=");
    putdec(irq_ovr);
    puts_("\r\ndone\r\n");
    for (;;)
        __WFI();
}
