/* STM32F427/F437 Chrom-ART (DMA2D, RM0090 chapter 11) at the reset clock (HSI 16 MHz, HCLK = TIM2 clock):
 *  1. register-to-memory: fill a 64x48 RGB565 rectangle (OCOLR = 0xF81F) inside an 80-pixel-wide frame (OOR 16);
 *  2. memory-to-memory with PFC: convert a 64x48 ARGB8888 gradient to RGB565 (dst2);
 *  3. memory-to-memory with blending: the gradient at 50 % (FGPFCCR AM = 01, ALPHA 0x80) over the filled frame,
 *     result RGB565 into dst3.
 * Each transfer is started and the core waits in WFI for the transfer-complete IRQ (DMA2D, 90). TIM2 runs at
 * 16 MHz from reset; t_done[k] = TIM2 counts (= HCLK cycles) from the START write to the IRQ. The checks are done in
 * firmware (pixel values computed with the RM0090 11.3.4/11.3.6 formulas); result = 0x600D when all pass, else the
 * number of the failed check. */
#include "stm32f42x.h"

#define DMA2D_BASE 0x4002B000u
#define D2(off) REG32(DMA2D_BASE + (off))
#define DMA2D_CR      D2(0x00)
#define DMA2D_ISR     D2(0x04)
#define DMA2D_IFCR    D2(0x08)
#define DMA2D_FGMAR   D2(0x0C)
#define DMA2D_FGOR    D2(0x10)
#define DMA2D_BGMAR   D2(0x14)
#define DMA2D_BGOR    D2(0x18)
#define DMA2D_FGPFCCR D2(0x1C)
#define DMA2D_BGPFCCR D2(0x24)
#define DMA2D_OPFCCR  D2(0x34)
#define DMA2D_OCOLR   D2(0x38)
#define DMA2D_OMAR    D2(0x3C)
#define DMA2D_OOR     D2(0x40)
#define DMA2D_NLR     D2(0x44)
#define DMA2D_CR_START (1u << 0)
#define DMA2D_CR_TCIE  (1u << 9)
#define DMA2D_ISR_TCIF (1u << 1)
#define MODE_M2M_PFC   (1u << 16)
#define MODE_BLEND     (2u << 16)
#define MODE_R2M       (3u << 16)

#define W 64
#define H 48
#define FRAME_W 80

static uint16_t frame[FRAME_W * H];
static uint32_t grad[W * H];
static uint16_t dst2[W * H];
static uint16_t dst3[W * H];

volatile uint32_t result;
volatile uint32_t tc_irqs;
volatile uint32_t t_done[3];
static volatile uint32_t t0, done;

void DMA2D_IRQHandler(void)
{
    uint32_t t = TIM2->CNT;
    if (DMA2D_ISR & DMA2D_ISR_TCIF) {
        DMA2D_IFCR = 0x3Fu;
        t_done[tc_irqs < 3 ? tc_irqs : 2] = t - t0;
        tc_irqs++;
        done = 1;
    }
}

static void run(uint32_t mode)
{
    done = 0;
    t0 = TIM2->CNT;
    DMA2D_CR = mode | DMA2D_CR_TCIE | DMA2D_CR_START;
    while (!done)
        __WFI();
}

static uint16_t rgb565(uint32_t argb)
{
    return (uint16_t)(((argb >> 19) & 31u) << 11 | ((argb >> 10) & 63u) << 5 | ((argb >> 3) & 31u));
}

static uint32_t expand565(uint16_t v)
{
    uint32_t r = v >> 11, g = (v >> 5) & 63u, b = v & 31u;
    return 0xFF000000u | ((r << 3) | (r >> 2)) << 16 | ((g << 2) | (g >> 4)) << 8 | ((b << 3) | (b >> 2));
}

static uint32_t blend(uint32_t fg, uint32_t bg)                /* RM0090 11.3.6 */
{
    uint32_t fa = fg >> 24, ba = bg >> 24, am = fa * ba / 255u, ao = fa + ba - am, out = ao << 24;
    for (int sh = 0; sh <= 16; sh += 8) {
        uint32_t cf = (fg >> sh) & 255u, cb = (bg >> sh) & 255u;
        out |= ((cf * fa + cb * ba - cb * am) / ao) << sh;
    }
    return out;
}

int main(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_DMA2DEN;
    RCC_APB1ENR |= RCC_APB1ENR_TIM2EN;
    TIM2->PSC = 0;
    TIM2->ARR = 0xFFFFFFFFu;
    TIM2->EGR = TIM_EGR_UG;
    TIM2->CR1 = TIM_CR1_CEN;
    NVIC_EnableIRQ(DMA2D_IRQn);
    __enable_irq();

    for (int i = 0; i < FRAME_W * H; i++)
        frame[i] = 0;
    for (int y = 0; y < H; y++)
        for (int x = 0; x < W; x++)
            grad[y * W + x] = 0xFF000000u | (uint32_t)(x * 4) << 16 | (uint32_t)(y * 5) << 8 | 0x40u;

    /* 1. register-to-memory fill */
    DMA2D_OPFCCR = 2u;                                         /* RGB565 */
    DMA2D_OCOLR = 0xF81Fu;
    DMA2D_OMAR = (uint32_t)frame;
    DMA2D_OOR = FRAME_W - W;
    DMA2D_NLR = (uint32_t)W << 16 | H;
    run(MODE_R2M);

    /* 2. memory-to-memory with PFC: ARGB8888 -> RGB565 */
    DMA2D_FGMAR = (uint32_t)grad;
    DMA2D_FGOR = 0;
    DMA2D_FGPFCCR = 0u;                                        /* ARGB8888, alpha unchanged */
    DMA2D_OMAR = (uint32_t)dst2;
    DMA2D_OOR = 0;
    run(MODE_M2M_PFC);

    /* 3. blending: gradient with alpha replaced by 0x80 over the filled frame */
    DMA2D_FGPFCCR = 0x80u << 24 | 1u << 16;                    /* ALPHA 0x80, AM = 01 replace */
    DMA2D_BGMAR = (uint32_t)frame;
    DMA2D_BGOR = FRAME_W - W;
    DMA2D_BGPFCCR = 2u;                                        /* RGB565 */
    DMA2D_OMAR = (uint32_t)dst3;
    run(MODE_BLEND);

    uint32_t fail = 0;
    for (int y = 0; y < H && !fail; y++) {
        for (int x = 0; x < FRAME_W && !fail; x++)
            if (frame[y * FRAME_W + x] != (x < W ? 0xF81Fu : 0u))
                fail = 1;
        for (int x = 0; x < W && !fail; x++) {
            uint32_t g = grad[y * W + x];
            if (dst2[y * W + x] != rgb565(g))
                fail = 2;
            else if (dst3[y * W + x] != rgb565(blend((g & 0xFFFFFFu) | 0x80000000u, expand565(0xF81Fu))))
                fail = 3;
        }
    }
    result = fail ? fail : 0x600Du;
    for (;;)
        __WFI();
}
