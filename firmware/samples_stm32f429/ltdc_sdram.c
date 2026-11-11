/* STM32F429ZI (LQFP144) wired like ST's 32F429IDISCOVERY (STM32F429I-DISC1): 8 MB SDRAM on FMC SDRAM bank 2
 * (SDNE1 PB6 / SDCKE1 PB5, 0xD0000000) and a 240x320 RGB TFT on the LCD-TFT controller (pins of the board's .ioc:
 * LCD_CLK PG7, HSYNC PC6, VSYNC PA4, DE PF10, R2..R7 PC10 PB0 PA11 PA12 PB1 PG6, G2..G7 PA6 PG10 PB10 PB11 PC7 PD3,
 * B2..B7 PD6 PG11 PG12 PA3 PB8 PB9; PB0/PB1/PG10/PG12 on AF9, the others AF14, DS9405 Table 12).
 *
 * Clocks: 180 MHz over-drive (HSE 8 MHz); LCD_CLK = 1 MHz x PLLSAIN 192 / PLLSAIR 4 / PLLSAIDIVR 8 = 6 MHz (RM0090
 * 6.3.24/6.3.25). Panel timing as ST's BSP for the ILI9341 in RGB mode: HSYNC 10, HBP 20, 240 active, HFP 10
 * (280 clocks per line); VSYNC 2, VBP 2, 320 active, VFP 4 (328 lines): 91840 clocks = 15.307 ms per frame
 * (65.33 Hz). HSYNC/VSYNC/DE active low, pixel clock not inverted.
 *
 * Layer 1: RGB565 frame buffer 240x312 in SDRAM, window lines 8..319 of the active area (lines 0..7 show the
 * background colour BCCR 0x203040), eight vertical 30-pixel colour bars (white, yellow, cyan, green, magenta, red, blue,
 * black). Layer 2: L8 with CLUT (0 = black = colour key -> transparent, 1 = 0xFF8000, 2 = 0x0080FF), 64x32 pixels in
 * SRAM, 8x8 checkerboard of 1/2, columns 0..3 = 0; window at active (88 + s, 144) with constant alpha 0x80 blended
 * over layer 1 (BF1 = pixel alpha x constant alpha, BF2 = 1 - that).
 * LTDC IRQ (88): line interrupt at line 0 (LIPCR = 0) toggles PG13 (the board's green LED) and, in the first 8
 * frames, moves layer 2 one pixel to the right with a vertical-blanking reload (SRCR.VBR); the register reload
 * interrupt (RRIF) counts the reloads. USART1 (PA9, 115200 baud) prints "LTDC ON\r\n" and, after 12 frames,
 * "LTDC frames 12 reloads 8 x 96\r\n". */
#include "stm32f42x.h"

#define FMC_BASE   0xA0000000u
#define FMC_SDCR1  REG32(FMC_BASE + 0x140)
#define FMC_SDCR2  REG32(FMC_BASE + 0x144)
#define FMC_SDTR1  REG32(FMC_BASE + 0x148)
#define FMC_SDTR2  REG32(FMC_BASE + 0x14C)
#define FMC_SDCMR  REG32(FMC_BASE + 0x150)
#define FMC_SDRTR  REG32(FMC_BASE + 0x154)
#define FMC_SDSR   REG32(FMC_BASE + 0x158)
#define SDSR_BUSY  (1u << 5)
#define CMD_CTB2   (1u << 3)

#define LTDC_BASE   0x40016800u
#define LTDC_SSCR   REG32(LTDC_BASE + 0x08)
#define LTDC_BPCR   REG32(LTDC_BASE + 0x0C)
#define LTDC_AWCR   REG32(LTDC_BASE + 0x10)
#define LTDC_TWCR   REG32(LTDC_BASE + 0x14)
#define LTDC_GCR    REG32(LTDC_BASE + 0x18)
#define LTDC_SRCR   REG32(LTDC_BASE + 0x24)
#define LTDC_BCCR   REG32(LTDC_BASE + 0x2C)
#define LTDC_IER    REG32(LTDC_BASE + 0x34)
#define LTDC_ISR    REG32(LTDC_BASE + 0x38)
#define LTDC_ICR    REG32(LTDC_BASE + 0x3C)
#define LTDC_LIPCR  REG32(LTDC_BASE + 0x40)
#define LX(l, off)  REG32(LTDC_BASE + 0x84 + 0x80 * ((l) - 1) + (off))
#define LCR(l)      LX(l, 0x00)
#define LWHPCR(l)   LX(l, 0x04)
#define LWVPCR(l)   LX(l, 0x08)
#define LCKCR(l)    LX(l, 0x0C)
#define LPFCR(l)    LX(l, 0x10)
#define LCACR(l)    LX(l, 0x14)
#define LDCCR(l)    LX(l, 0x18)
#define LBFCR(l)    LX(l, 0x1C)
#define LCFBAR(l)   LX(l, 0x28)
#define LCFBLR(l)   LX(l, 0x2C)
#define LCFBLNR(l)  LX(l, 0x30)
#define LCLUTWR(l)  LX(l, 0x40)
#define RCC_APB2ENR_LTDCEN (1u << 26)
#define LTDC_IRQn 88

#define HSW 10u
#define HBP 20u
#define AW 240u
#define HFP 10u
#define VSW 2u
#define VBP 2u
#define AH 320u
#define VFP 4u
#define AHBP (HSW + HBP - 1u)          /* 29 */
#define AVBP (VSW + VBP - 1u)          /* 3 */

#define FB1 ((volatile uint16_t *)0xD0000000u)
#define FB1_LINES 312u
#define L2_W 64u
#define L2_H 32u
#define L2_X 88u
#define L2_Y 144u

static uint8_t l2_pixels[L2_W * L2_H];
static volatile uint32_t frames, reloads, shift;

#define GPIOG_ODR REG32(0x40021814u)

static void putc1(char c)
{
    while (!(USART1->SR & USART_SR_TXE)) { }
    USART1->DR = (uint8_t)c;
}

static void puts1(const char *s)
{
    while (*s)
        putc1(*s++);
}

static void putdec(uint32_t v)
{
    char b[11];
    int n = 0;
    do { b[n++] = (char)('0' + v % 10); v /= 10; } while (v);
    while (n)
        putc1(b[--n]);
}

static void delay_us(uint32_t us)          /* SysTick on HCLK = 180 MHz */
{
    SYST_RVR = 180u * us - 1u;
    SYST_CVR = 0;
    SYST_CSR = SYST_CSR_ENABLE | SYST_CSR_CLKSOURCE;
    while (!(SYST_CSR & SYST_CSR_COUNTFLAG)) { }
    SYST_CSR = 0;
}

/* ---------------------------------------------------------------- SDRAM (RM0090 37.7.3, as f42x_sdram.c) */
static void sdram_cmd(uint32_t mode, uint32_t nrfs, uint32_t mrd)
{
    while (FMC_SDSR & SDSR_BUSY) { }
    FMC_SDCMR = (mrd << 9) | ((nrfs - 1u) << 5) | CMD_CTB2 | mode;
    while (FMC_SDSR & SDSR_BUSY) { }
}

static void pin_af(GPIO_TypeDef *g, unsigned pin, unsigned af)
{
    gpio_speed(g, pin, 3u);
    gpio_af(g, pin, af);
}

static void sdram_init(void)
{
    static const uint8_t pb[] = {5, 6}, pc[] = {0}, pd[] = {0, 1, 8, 9, 10, 14, 15},
        pe[] = {0, 1, 7, 8, 9, 10, 11, 12, 13, 14, 15}, pf[] = {0, 1, 2, 3, 4, 5, 11, 12, 13, 14, 15},
        pg[] = {0, 1, 4, 5, 8, 15};
    for (unsigned i = 0; i < sizeof pb; i++) pin_af(GPIOB, pb[i], 12u);
    for (unsigned i = 0; i < sizeof pc; i++) pin_af(GPIOC, pc[i], 12u);
    for (unsigned i = 0; i < sizeof pd; i++) pin_af(GPIOD, pd[i], 12u);
    for (unsigned i = 0; i < sizeof pe; i++) pin_af(GPIOE, pe[i], 12u);
    for (unsigned i = 0; i < sizeof pf; i++) pin_af(GPIOF, pf[i], 12u);
    for (unsigned i = 0; i < sizeof pg; i++) pin_af(GPIOG, pg[i], 12u);
    RCC_AHB3ENR |= RCC_AHB3ENR_FMCEN;
    FMC_SDCR1 = (FMC_SDCR1 & ~0x7C00u) | (2u << 10) | (1u << 12) | (1u << 13);   /* SDCLK HCLK/2, RBURST, RPIPE 1 */
    FMC_SDCR2 = (0u << 0) | (1u << 2) | (1u << 4) | (1u << 6) | (3u << 7);       /* 8 col, 12 row, 16 bit, 4 banks, CAS 3 */
    FMC_SDTR1 = (1u << 0) | (6u << 4) | (3u << 8) | (6u << 12) | (1u << 16) | (1u << 20) | (1u << 24);
    FMC_SDTR2 = FMC_SDTR1;
    sdram_cmd(1u, 1u, 0u);                     /* clock configuration enable */
    delay_us(100u);
    sdram_cmd(2u, 1u, 0u);                     /* PALL */
    sdram_cmd(3u, 8u, 0u);                     /* 8 auto-refresh */
    sdram_cmd(4u, 1u, 0x230u);                 /* load mode register: BL 1, CAS 3, single write burst */
    FMC_SDRTR = 1386u << 1;
}

/* ---------------------------------------------------------------- LTDC */
static void lcd_pins(void)
{
    static const struct { GPIO_TypeDef *g; uint8_t pin, af; } p[] = {
        {GPIOG, 7, 14}, {GPIOC, 6, 14}, {GPIOA, 4, 14}, {GPIOF, 10, 14},                               /* CLK HS VS DE */
        {GPIOC, 10, 14}, {GPIOB, 0, 9}, {GPIOA, 11, 14}, {GPIOA, 12, 14}, {GPIOB, 1, 9}, {GPIOG, 6, 14},  /* R2..R7 */
        {GPIOA, 6, 14}, {GPIOG, 10, 9}, {GPIOB, 10, 14}, {GPIOB, 11, 14}, {GPIOC, 7, 14}, {GPIOD, 3, 14}, /* G2..G7 */
        {GPIOD, 6, 14}, {GPIOG, 11, 14}, {GPIOG, 12, 9}, {GPIOA, 3, 14}, {GPIOB, 8, 14}, {GPIOB, 9, 14},  /* B2..B7 */
    };
    for (unsigned i = 0; i < sizeof p / sizeof p[0]; i++)
        pin_af(p[i].g, p[i].pin, p[i].af);
}

static void lcd_clock(void)
{
    RCC_CR &= ~RCC_CR_PLLSAION;
    while (RCC_CR & RCC_CR_PLLSAIRDY) { }
    RCC_PLLSAICFGR = (192u << 6) | (7u << 24) | (4u << 28);        /* N 192, Q 7, R 4: PLLSAI_R = 48 MHz */
    RCC_DCKCFGR = (RCC_DCKCFGR & ~(3u << 16)) | (2u << 16);         /* PLLSAIDIVR /8 -> LCD_CLK 6 MHz */
    RCC_CR |= RCC_CR_PLLSAION;
    while (!(RCC_CR & RCC_CR_PLLSAIRDY)) { }
}

static void fill_layers(void)
{
    static const uint16_t bars[8] = {0xFFFF, 0xFFE0, 0x07FF, 0x07E0, 0xF81F, 0xF800, 0x001F, 0x0000};
    for (uint32_t y = 0; y < FB1_LINES; y++)
        for (uint32_t x = 0; x < AW; x++)
            FB1[y * AW + x] = bars[x / 30u];
    for (uint32_t y = 0; y < L2_H; y++)
        for (uint32_t x = 0; x < L2_W; x++)
            l2_pixels[y * L2_W + x] = x < 4u ? 0u : ((((x / 8u) + (y / 8u)) & 1u) ? 1u : 2u);
}

static uint32_t l2_whpcr(uint32_t s)
{
    uint32_t x0 = AHBP + 1u + L2_X + s;
    return ((x0 + L2_W - 1u) << 16) | x0;
}

static void ltdc_init(void)
{
    RCC_APB2ENR |= RCC_APB2ENR_LTDCEN;
    LTDC_SSCR = ((HSW - 1u) << 16) | (VSW - 1u);
    LTDC_BPCR = (AHBP << 16) | AVBP;
    LTDC_AWCR = ((AHBP + AW) << 16) | (AVBP + AH);
    LTDC_TWCR = ((AHBP + AW + HFP) << 16) | (AVBP + AH + VFP);
    LTDC_GCR = 0;                                                   /* HS/VS/DE active low, IPC */
    LTDC_BCCR = 0x203040u;
    /* layer 1: RGB565, lines 8..319 */
    LWHPCR(1) = ((AHBP + AW) << 16) | (AHBP + 1u);
    LWVPCR(1) = ((AVBP + AH) << 16) | (AVBP + 1u + 8u);
    LPFCR(1) = 2u;
    LCACR(1) = 0xFFu;
    LDCCR(1) = 0;
    LBFCR(1) = (6u << 8) | 7u;
    LCFBAR(1) = (uint32_t)FB1;
    LCFBLR(1) = ((AW * 2u) << 16) | (AW * 2u + 3u);
    LCFBLNR(1) = FB1_LINES;
    LCR(1) = 1u;
    /* layer 2: L8 + CLUT + colour key, constant alpha 0x80 */
    LWHPCR(2) = l2_whpcr(0);
    LWVPCR(2) = ((AVBP + 1u + L2_Y + L2_H - 1u) << 16) | (AVBP + 1u + L2_Y);
    LPFCR(2) = 5u;
    LCACR(2) = 0x80u;
    LDCCR(2) = 0;
    LBFCR(2) = (6u << 8) | 7u;
    LCKCR(2) = 0x000000u;
    LCFBAR(2) = (uint32_t)l2_pixels;
    LCFBLR(2) = (L2_W << 16) | (L2_W + 3u);
    LCFBLNR(2) = L2_H;
    LCLUTWR(2) = (0u << 24) | 0x000000u;
    LCLUTWR(2) = (1u << 24) | 0xFF8000u;
    LCLUTWR(2) = (2u << 24) | 0x0080FFu;
    LCR(2) = (1u << 4) | (1u << 1) | 1u;                            /* CLUTEN, COLKEN, LEN */
    LTDC_SRCR = 1u;                                                 /* immediate reload */
    LTDC_LIPCR = 0;                                                 /* line interrupt at line 0 */
    LTDC_IER = (1u << 3) | 1u;                                      /* RRIE, LIE */
    NVIC_EnableIRQ(LTDC_IRQn);
    LTDC_GCR |= 1u;                                                 /* LTDCEN */
}

void LTDC_IRQHandler(void)
{
    uint32_t isr = LTDC_ISR;
    if (isr & 1u) {
        LTDC_ICR = 1u;
        GPIOG_ODR ^= 1u << 13;
        frames++;
        if (shift < 8u) {
            shift++;
            LWHPCR(2) = l2_whpcr(shift);
            LTDC_SRCR = 2u;                                         /* reload in the next vertical blanking */
        }
    }
    if (isr & 8u) {
        LTDC_ICR = 8u;
        reloads++;
    }
}

int main(void)
{
    clock_180mhz();
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOALL;
    RCC_APB2ENR |= RCC_APB2ENR_USART1EN;
    gpio_af(GPIOA, 9, 7);                      /* PA9 = USART1_TX */
    USART1->BRR = USART_BRR(90000000u, 115200u);
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE;
    gpio_mode(GPIOG, 13, GPIO_MODE_OUTPUT);    /* LD3 (green) */

    sdram_init();
    fill_layers();
    lcd_pins();
    lcd_clock();
    ltdc_init();
    puts1("LTDC ON\r\n");
    while (frames < 12u)
        __asm volatile ("wfi");
    uint32_t f = frames, r = reloads, s = shift;
    puts1("LTDC frames ");
    putdec(f);
    puts1(" reloads ");
    putdec(r);
    puts1(" x ");
    putdec(L2_X + s);
    puts1("\r\n");
    for (;;)
        __asm volatile ("wfi");
}
