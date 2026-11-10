/* uart_dma: USART1 (PA9 TX / PA10 RX, AF7, 115200 8N1 from PCLK2 = 16 MHz HSI) driven by DMA2 (RM0090 Table 43):
 *   TX: DMA2 Stream7 Channel 4 (USART1_TX), memory -> USART1_DR, byte, MINC; completion by the DMA2_Stream7
 *       interrupt (TCIF7), then USART TC before the next transmission.
 *   RX: DMA2 Stream5 Channel 4 (USART1_RX), USART1_DR -> rx_buf, 8 bytes, MINC; completion by polling HISR.TCIF5.
 * Arms RX first, prints "DMA ready\r\n", then forever: wait for 8 bytes, copy them, re-arm RX at once, echo them as
 * "<8 bytes>\r\n" by DMA.  The echo of a block (10 characters) is longer than the next block (8), and the CPU only
 * re-arms RX after the previous echo, so back-to-back input overruns the single-buffered USART (ORE, bytes lost) just
 * like on silicon: send one block at a time. */
#include "stm32f405.h"

/* ---- DMA2 (0x40026400; streams at 0x10 + 0x18 * n) ---- */
typedef struct {
    volatile uint32_t CR, NDTR, PAR, M0AR, M1AR, FCR;
} DMA_Stream_TypeDef;
#define DMA2_BASE   0x40026400u
#define DMA2_LISR   REG32(DMA2_BASE + 0x00)
#define DMA2_HISR   REG32(DMA2_BASE + 0x04)
#define DMA2_LIFCR  REG32(DMA2_BASE + 0x08)
#define DMA2_HIFCR  REG32(DMA2_BASE + 0x0C)
#define DMA2_S(n)   ((DMA_Stream_TypeDef *)(DMA2_BASE + 0x10u + 0x18u * (n)))
#define DMA_CR_EN     (1u << 0)
#define DMA_CR_TCIE   (1u << 4)
#define DMA_CR_DIR_M2P (1u << 6)
#define DMA_CR_MINC   (1u << 10)
#define DMA_CR_CHSEL(c) ((uint32_t)(c) << 25)
#define DMA_HISR_TCIF5 (1u << 11)
#define DMA_HISR_TCIF7 (1u << 27)
#define DMA_HISR_S5_ALL (0x3Du << 6)
#define DMA_HISR_S7_ALL (0x3Du << 22)
#define RCC_AHB1ENR_DMA2EN (1u << 22)
#define USART_CR3_DMAR (1u << 6)
#define USART_CR3_DMAT (1u << 7)
#define DMA2_Stream7_IRQn 70

static const char banner[] = "DMA ready\r\n";
uint8_t rx_buf[8];
uint8_t tx_buf[10];
volatile uint32_t tx_done;       /* DMA2 Stream7 transfer-complete interrupts */
volatile uint32_t rx_blocks;     /* 8-byte blocks received by DMA */

void DMA2_Stream7_IRQHandler(void)
{
    if (DMA2_HISR & DMA_HISR_TCIF7) {
        DMA2_HIFCR = DMA_HISR_TCIF7;
        tx_done++;
    }
}

static void tx_dma(const void *buf, uint32_t n)
{
    DMA_Stream_TypeDef *s = DMA2_S(7);
    uint32_t done = tx_done;
    while (!(USART1->SR & USART_SR_TC)) { }      /* previous frame fully shifted out */
    s->CR = 0;
    while (s->CR & DMA_CR_EN) { }
    DMA2_HIFCR = DMA_HISR_S7_ALL;
    s->PAR = (uint32_t)&USART1->DR;
    s->M0AR = (uint32_t)buf;
    s->NDTR = n;
    s->FCR = 0;                                  /* direct mode */
    s->CR = DMA_CR_CHSEL(4) | DMA_CR_MINC | DMA_CR_DIR_M2P | DMA_CR_TCIE;
    USART1->SR = ~USART_SR_TC;                   /* rc_w0, as ST's HAL does before DMAT */
    s->CR |= DMA_CR_EN;
    while (tx_done == done) { }                  /* DMA2_Stream7 IRQ */
}

static void rx_start(void *buf, uint32_t n)
{
    DMA_Stream_TypeDef *s = DMA2_S(5);
    s->CR = 0;
    while (s->CR & DMA_CR_EN) { }
    DMA2_HIFCR = DMA_HISR_S5_ALL;
    s->PAR = (uint32_t)&USART1->DR;
    s->M0AR = (uint32_t)buf;
    s->NDTR = n;
    s->FCR = 0;
    s->CR = DMA_CR_CHSEL(4) | DMA_CR_MINC | DMA_CR_EN;   /* peripheral -> memory */
}

int main(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN | RCC_AHB1ENR_DMA2EN;
    RCC_APB2ENR |= RCC_APB2ENR_USART1EN;
    gpio_af(GPIOA, 9, 7);                          /* USART1_TX */
    gpio_af(GPIOA, 10, 7);                         /* USART1_RX */
    gpio_pull(GPIOA, 10, GPIO_PULL_UP);

    USART1->BRR = USART_BRR(HSI_HZ, 115200u);
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;
    USART1->CR3 = USART_CR3_DMAT | USART_CR3_DMAR;
    NVIC_EnableIRQ(DMA2_Stream7_IRQn);
    __enable_irq();

    rx_start(rx_buf, sizeof rx_buf);
    tx_dma(banner, sizeof banner - 1);
    for (;;) {
        while (!(DMA2_HISR & DMA_HISR_TCIF5)) { }
        for (uint32_t i = 0; i < 8; i++)
            tx_buf[i] = rx_buf[i];
        rx_start(rx_buf, sizeof rx_buf);           /* clears the stream 5 flags */
        rx_blocks++;
        tx_buf[8] = '\r';
        tx_buf[9] = '\n';
        tx_dma(tx_buf, sizeof tx_buf);
    }
}
