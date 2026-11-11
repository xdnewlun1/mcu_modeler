/* dma2_m2m: DMA2 of the connectivity line (RM0008 ch. 13, 5 channels, vectors DMA2_Channel1..5 = 56..60, Table 61):
 * channel 1 copies 256 words from flash to SRAM in memory-to-memory mode (CCR.MEM2MEM, 32-bit, both pointers
 * incremented) and raises DMA2_Channel1 on transfer complete; then channel 2 fills a buffer from one SRAM word
 * (PINC = 0).  USART1 prints "DMA2 ok 256" (or "DMA2 bad n"). */
#include "console.h"

static const uint32_t src[256] = {
#define R4(x) (x), (x) + 1u, (x) + 2u, (x) + 3u
#define R16(x) R4(x), R4((x) + 4u), R4((x) + 8u), R4((x) + 12u)
#define R64(x) R16(x), R16((x) + 16u), R16((x) + 32u), R16((x) + 48u)
    R64(0x1000u), R64(0x1040u), R64(0x1080u), R64(0x10C0u)};
static uint32_t dst[256], fill[16];
static uint32_t pattern = 0xA5A5F00Du;
volatile uint32_t tc1, tc2;

void DMA2_Channel1_IRQHandler(void)
{
    if (DMA2_ISR & (1u << 1)) {                           /* TCIF1 */
        DMA2_IFCR = 1u << 0;                              /* CGIF1 */
        tc1++;
    }
}

void DMA2_Channel2_IRQHandler(void)
{
    if (DMA2_ISR & (1u << 5)) {
        DMA2_IFCR = 1u << 4;
        tc2++;
    }
}

int main(void)
{
    clock_72mhz_pll2(0);
    console_init(72000000u, 115200u);
    RCC_AHBENR |= RCC_AHBENR_DMA2EN;
    NVIC_ENABLE(DMA2_Channel1_IRQn);
    NVIC_ENABLE(DMA2_Channel2_IRQn);
    enable_irq();
    DMA2_CPAR(1) = (uint32_t)src;                         /* DIR = 0: "peripheral" (here flash) -> memory */
    DMA2_CMAR(1) = (uint32_t)dst;
    DMA2_CNDTR(1) = 256;
    DMA2_CCR(1) = DMA_CCR_MEM2MEM | DMA_CCR_PSIZE_32 | DMA_CCR_MSIZE_32 | DMA_CCR_PINC | DMA_CCR_MINC | DMA_CCR_TCIE
                | DMA_CCR_EN;
    while (!tc1)
        __asm__ volatile("wfi");
    DMA2_CCR(1) &= ~DMA_CCR_EN;                          /* disable first (configuration bits stay) */
    DMA2_CPAR(2) = (uint32_t)&pattern;
    DMA2_CMAR(2) = (uint32_t)fill;
    DMA2_CNDTR(2) = 16;
    DMA2_CCR(2) = DMA_CCR_MEM2MEM | DMA_CCR_PSIZE_32 | DMA_CCR_MSIZE_32 | DMA_CCR_MINC | DMA_CCR_TCIE | DMA_CCR_EN;
    while (!tc2)
        __asm__ volatile("wfi");
    uint32_t good = 0;
    for (unsigned i = 0; i < 256; i++)
        good += dst[i] == 0x1000u + i;
    for (unsigned i = 0; i < 16; i++)
        good += fill[i] == pattern;
    puts_(good == 272 ? "DMA2 ok " : "DMA2 bad ");
    putdec(good == 272 ? 256 : good);
    puts_("\r\n");
    for (;;)
        __asm__ volatile("wfi");
}
