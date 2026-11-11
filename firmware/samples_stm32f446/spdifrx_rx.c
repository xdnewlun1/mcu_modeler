/* SPDIFRX receiver (STM32F446RE; board boards/stm32f446re_spdifrx.yaml: an IEC 60958 source part, 48 kHz "counter"
 * signal, on PB7 = SPDIFRX_IN0, AF8).  RM0390 ch. 27 programming procedure (27.4):
 *   SPDIFRX_CLK = PLLI2S_P = 100 MHz (HSI 16 MHz / PLLI2SM 16 x PLLI2SN 200 / 2; DCKCFGR2.SPDIFRXSEL = 1): at least
 *   704 x 96 kHz (Table 171);  CR: INSEL 0, WFA, NBTR 15 re-tries, CUMSK + PTMSK, DRFMT 01 (left aligned), RXSTEO;
 *   SPDIFRXEN = 01 (STATE_SYNC) -> wait for SYNCD, estimate the sample rate from WIDTH5 (27.5.3);
 *   read 4 channel status words (CSR: SOB, CS[7:0] from channel A) and print IEC 60958 channel status bytes 0..3;
 *   SPDIFRXEN = 11 (STATE_RCV) with RXDMAEN: DMA1 stream 1 channel 0 (SPDIFRX_DT, RM0390 Table 28) moves 16 words of
 *   DR; check that they are A/B pairs of the counter (B = ~A, A increments by one per frame).
 * Console: USART2 (PA2, 115200). */
#include "stm32f446.h"

typedef struct { volatile uint32_t CR, NDTR, PAR, M0AR, M1AR, FCR; } DMA_Stream1_TypeDef;
#define DMA1_LISR   REG32(0x40026000u)
#define DMA1_LIFCR  REG32(0x40026008u)
#define DMA1_Stream1 ((DMA_Stream1_TypeDef *)(0x40026010u + 0x18u * 1u))
#define RCC_AHB1ENR_DMA1EN (1u << 21)
#define DMA_LISR_TCIF1 (1u << 11)

static void putc2(char c)
{
    while (!(USART2->SR & USART_SR_TXE)) { }
    USART2->DR = (uint8_t)c;
}

static void puts2(const char *s)
{
    while (*s)
        putc2(*s++);
}

static void hex(unsigned v, int digits)
{
    for (int i = digits - 1; i >= 0; i--)
        putc2("0123456789ABCDEF"[(v >> (4 * i)) & 15u]);
}

static void dec(unsigned v)
{
    char b[12];
    int n = 0;
    do { b[n++] = (char)('0' + v % 10u); v /= 10u; } while (v);
    while (n)
        putc2(b[--n]);
}

static uint32_t samples[16];

int main(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN | RCC_AHB1ENR_GPIOBEN | RCC_AHB1ENR_DMA1EN;
    RCC_APB1ENR |= RCC_APB1ENR_USART2EN | RCC_APB1ENR_SPDIFRXEN;
    gpio_af(GPIOA, 2, 7);
    USART2->BRR = USART_BRR(HSI_HZ, 115200u);
    USART2->CR1 = USART_CR1_TE | USART_CR1_UE;
    gpio_af(GPIOB, 7, 8);                                     /* SPDIFRX_IN0 */

    /* SPDIFRX_CLK: PLLI2S_P = 16 MHz / 16 x 200 / 2 = 100 MHz */
    RCC_PLLI2SCFGR = 16u | (200u << 6) | (0u << 16) | (2u << 24) | (2u << 28);
    RCC_CR |= RCC_CR_PLLI2SON;
    while (!(RCC_CR & RCC_CR_PLLI2SRDY)) { }
    RCC_DCKCFGR2 |= 1u << 29;                                 /* SPDIFRXSEL = PLLI2S_P */

    SPDIFRX->CR = SPDIFRX_CR_INSEL(0) | SPDIFRX_CR_WFA | SPDIFRX_CR_NBTR(2) | SPDIFRX_CR_CUMSK | SPDIFRX_CR_PTMSK |
                  SPDIFRX_CR_DRFMT(1) | SPDIFRX_CR_RXSTEO;
    SPDIFRX->CR |= SPDIFRX_CR_SPDIFEN(1);                     /* STATE_SYNC */
    puts2("SPDIFRX wait sync\r\n");
    uint32_t sr;
    while (!((sr = SPDIFRX->SR) & (SPDIFRX_SR_SYNCD | SPDIFRX_SR_TERR | SPDIFRX_SR_SERR | SPDIFRX_SR_FERR))) { }
    if (!(sr & SPDIFRX_SR_SYNCD)) {
        puts2("sync error ");
        hex(sr, 3);
        puts2("\r\n");
        for (;;) { }
    }
    SPDIFRX->IFCR = SPDIFRX_IFCR_SYNCDCF;
    unsigned w5 = (sr >> 16) & 0x7FFFu;
    unsigned fs = (unsigned)((5ull * 100000000u + w5 * 32u) / (w5 * 64u));
    puts2("SYNCD WIDTH5 ");
    dec(w5);
    puts2(" Fs ~");
    dec(fs);
    puts2(" Hz\r\n");

    /* channel status: wait for the start of a block (SOB), then 3 more words = CS bytes 0..3 of channel A */
    uint8_t cs[4];
    unsigned got = 0;
    while (got < 4) {
        while (!(SPDIFRX->SR & SPDIFRX_SR_CSRNE)) { }
        uint32_t w = SPDIFRX->CSR;
        if (got == 0 && !(w & (1u << 24)))
            continue;
        cs[got++] = (uint8_t)(w >> 16);
    }
    puts2("CS");
    for (unsigned i = 0; i < 4; i++) {
        putc2(' ');
        hex(cs[i], 2);
    }
    static const char *const fsname[16] = {"44.1", "?", "48", "32", "?", "?", "?", "?", "88.2", "?", "96", "?",
                                           "176.4", "?", "192", "?"};
    puts2(cs[0] & 2 ? " non-audio " : " PCM ");
    puts2(fsname[cs[3] & 15u]);
    puts2(" kHz\r\n");

    /* audio: DMA1 stream 1 channel 0 (SPDIFRX_DT), 16 words */
    DMA1_Stream1->CR = 0;
    DMA1_LIFCR = 0x0F40u;
    DMA1_Stream1->PAR = (uint32_t)&SPDIFRX->DR;
    DMA1_Stream1->M0AR = (uint32_t)samples;
    DMA1_Stream1->NDTR = 16;
    DMA1_Stream1->CR = (0u << 25) | (2u << 13) | (2u << 11) | DMA_SxCR_MINC;   /* ch 0, 32-bit, P2M */
    DMA1_Stream1->CR |= DMA_SxCR_EN;
    SPDIFRX->CR |= SPDIFRX_CR_RXDMAEN | SPDIFRX_CR_SPDIFEN(3);  /* STATE_RCV from the next B preamble */
    while (!(DMA1_LISR & DMA_LISR_TCIF1)) { }
    SPDIFRX->CR &= ~SPDIFRX_CR_RXDMAEN;
    unsigned ok = 0;
    for (unsigned i = 0; i < 16; i += 2) {
        uint32_t a = samples[i] >> 8, b = samples[i + 1] >> 8;
        if (b == (~a & 0xFFFFFFu) && (i == 0 || a == ((samples[i - 2] >> 8) + 1u)))
            ok++;
    }
    puts2("A0 ");
    hex(samples[0] >> 8, 6);
    puts2(" B0 ");
    hex(samples[1] >> 8, 6);
    puts2(" pairs ok ");
    dec(ok);
    puts2("/8\r\nSR ");
    hex(SPDIFRX->SR & 0x1FFu, 3);
    SPDIFRX->CR &= ~3u;                                        /* STATE_IDLE */
    puts2("\r\ndone\r\n");
    for (;;)
        __asm volatile("wfi");
}
