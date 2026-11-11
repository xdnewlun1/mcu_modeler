/* STM32F437AI (UFBGA169): FMC SDRAM controller + an 8 MB SDR SDRAM (IS42S16400J-like: 4 internal banks x 4096 rows
 * x 256 columns x 16 bit) on SDRAM bank 2 (SDNE1 PB6 / SDCKE1 PB5, window 0xD0000000), wired like ST's
 * STM32F429I-DISCO board (DS9484 Table 12 AF12 pins). 180 MHz with over-drive, SDCLK = HCLK / 2 = 90 MHz.
 *
 * Initialization: RM0090 37.7.3 "SDRAM initialization" steps 1-8: SDCR/SDTR, clock configuration enable, >= 100 us
 * power-up delay (SysTick), PALL, 8 auto-refresh, load mode register (burst length 1, CAS 3), refresh timer
 * COUNT = 64 ms / 4096 rows x 90 MHz - 20 = 1386 (RM0090 37.7.5 FMC_SDRTR formula). Then clears write protection
 * and runs a memory test: data bus walking ones, address lines (every power-of-two offset over 8 MB), and a 1 MB
 * pattern fill/verify. Result on USART1 (PA9, 115200 baud from PCLK2 = 90 MHz):
 *   "SDRAM OK 8192 KB\r\n" or "SDRAM FAIL <test> @<address hex>\r\n". */
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
#define SDRAM      ((volatile uint32_t *)0xD0000000u)
#define SDRAM_SIZE (8u << 20)

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

static void puthex(uint32_t v)
{
    for (int i = 28; i >= 0; i -= 4)
        putc1("0123456789ABCDEF"[(v >> i) & 0xF]);
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

static void sdram_cmd(uint32_t mode, uint32_t nrfs, uint32_t mrd)
{
    while (FMC_SDSR & SDSR_BUSY) { }
    FMC_SDCMR = (mrd << 9) | ((nrfs - 1u) << 5) | CMD_CTB2 | mode;
    while (FMC_SDSR & SDSR_BUSY) { }
}

static void fmc_pin(GPIO_TypeDef *g, unsigned pin)
{
    gpio_speed(g, pin, 3u);
    gpio_af(g, pin, 12u);
}

static void sdram_pins(void)
{
    static const uint8_t pb[] = {5, 6}, pc[] = {0}, pd[] = {0, 1, 8, 9, 10, 14, 15},
        pe[] = {0, 1, 7, 8, 9, 10, 11, 12, 13, 14, 15}, pf[] = {0, 1, 2, 3, 4, 5, 11, 12, 13, 14, 15},
        pg[] = {0, 1, 4, 5, 8, 15};
    for (unsigned i = 0; i < sizeof pb; i++) fmc_pin(GPIOB, pb[i]);
    for (unsigned i = 0; i < sizeof pc; i++) fmc_pin(GPIOC, pc[i]);
    for (unsigned i = 0; i < sizeof pd; i++) fmc_pin(GPIOD, pd[i]);
    for (unsigned i = 0; i < sizeof pe; i++) fmc_pin(GPIOE, pe[i]);
    for (unsigned i = 0; i < sizeof pf; i++) fmc_pin(GPIOF, pf[i]);
    for (unsigned i = 0; i < sizeof pg; i++) fmc_pin(GPIOG, pg[i]);
}

static void sdram_init(void)
{
    RCC_AHB3ENR |= RCC_AHB3ENR_FMCEN;
    /* step 1: SDCR. SDCLK = HCLK/2, RBURST, RPIPE = 1 HCLK go into SDCR1 for both banks (37.7.5) */
    FMC_SDCR1 = (FMC_SDCR1 & ~0x7C00u) | (2u << 10) | (1u << 12) | (1u << 13);
    /* bank 2: NC = 8 bits (00), NR = 12 bits (01), MWID 16 (01), 4 internal banks, CAS 3, WP = 0 */
    FMC_SDCR2 = (0u << 0) | (1u << 2) | (1u << 4) | (1u << 6) | (3u << 7);
    /* step 2: SDTR. TMRD 2, TXSR 7, TRAS 4, TRC 7, TWR 2, TRP 2, TRCD 2 (cycles - 1 in the fields); TRC/TRP from SDTR1 */
    FMC_SDTR1 = (1u << 0) | (6u << 4) | (3u << 8) | (6u << 12) | (1u << 16) | (1u << 20) | (1u << 24);
    FMC_SDTR2 = FMC_SDTR1;
    sdram_cmd(1u, 1u, 0u);                     /* step 3: clock configuration enable */
    delay_us(100u);                            /* step 4: power-up delay */
    sdram_cmd(2u, 1u, 0u);                     /* step 5: PALL */
    sdram_cmd(3u, 8u, 0u);                     /* step 6: 8 auto-refresh */
    sdram_cmd(4u, 1u, 0x230u);                 /* step 7: BL 1, sequential, CAS 3, standard, single write burst */
    FMC_SDRTR = 1386u << 1;                    /* step 8: refresh every 15.4 us */
}

static int sdram_test(void)
{
    volatile uint32_t *m = SDRAM;
    /* data bus: walking ones on one word */
    for (unsigned b = 0; b < 32; b++) {
        m[0] = 1u << b;
        if (m[0] != (1u << b)) { puts1("SDRAM FAIL data @"); puthex((uint32_t)&m[0]); puts1("\r\n"); return 0; }
    }
    /* address lines: one word at every power-of-two offset, check no aliasing */
    for (uint32_t off = 4; off < SDRAM_SIZE; off <<= 1)
        m[off / 4] = 0xAAAA0000u | off;
    m[0] = 0x55555555u;
    for (uint32_t off = 4; off < SDRAM_SIZE; off <<= 1)
        if (m[off / 4] != (0xAAAA0000u | off)) { puts1("SDRAM FAIL addr @"); puthex((uint32_t)&m[off / 4]); puts1("\r\n"); return 0; }
    /* 1 MB pattern */
    for (uint32_t i = 0; i < (1u << 20) / 4; i++)
        m[i] = i * 2654435761u;
    for (uint32_t i = 0; i < (1u << 20) / 4; i++)
        if (m[i] != i * 2654435761u) { puts1("SDRAM FAIL fill @"); puthex((uint32_t)&m[i]); puts1("\r\n"); return 0; }
    return 1;
}

int main(void)
{
    clock_180mhz();
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOALL;
    RCC_APB2ENR |= RCC_APB2ENR_USART1EN;
    gpio_af(GPIOA, 9, 7);                      /* PA9 = USART1_TX */
    USART1->BRR = USART_BRR(90000000u, 115200u);
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE;

    sdram_pins();
    sdram_init();
    if (sdram_test()) {
        puts1("SDRAM OK ");
        putdec(SDRAM_SIZE / 1024u);
        puts1(" KB\r\n");
    }
    for (;;) { }
}
