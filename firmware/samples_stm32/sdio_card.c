/* sdio_card: SDIO host + SD card (run with --sd-card SDIO, or boards/stm32_sdcard.yaml).
 * SYSCLK stays on HSI 16 MHz; the main PLL runs only for PLL48CLK = SDIOCLK (HSI/16 x 336 / Q=7 = 48 MHz).
 * SDIO pins: PC8..PC11 = D0..D3, PC12 = CK, PD2 = CMD (AF12, pull-ups). USART1 PA9/PA10, 115200 8N1.
 * Card identification at 400 kHz (CLKDIV 118): CMD0, CMD8, ACMD41 (HCS) until ready, CMD2, CMD3, CMD9 (CSD),
 * CMD7; then ACMD6 4-bit bus and SDIO_CK = 8 MHz (CLKDIV 4: a 16 MHz CPU polling the FIFO cannot keep up with
 * 24 MHz, which underruns as on silicon). Writes block 2 (CMD24, FIFO polled on TXFIFOHE),
 * waits for the transfer state (CMD13), reads it back (CMD17, FIFO polled on RXFIFOHF), then reads blocks 2..3 with
 * CMD18 + CMD12 through DMA2 stream 3 channel 4. Prints:
 *   "SD v2 SDHC RCA xxxx\r\n" "CSD xxxxxxxx xxxxxxxx xxxxxxxx xxxxxxxx\r\n" "CAP <KiB> KiB\r\n" "WRITE ok\r\n"
 *   "READ ok CHIP_EM SDIO block\r\n" "DMA ok\r\n" "done\r\n"   (or "ERR <where> <STA>\r\n") */
#include "stm32f405.h"

typedef struct {
    volatile uint32_t POWER, CLKCR, ARG, CMD, RESPCMD, RESP1, RESP2, RESP3, RESP4, DTIMER, DLEN, DCTRL, DCOUNT, STA,
        ICR, MASK, RES0[2], FIFOCNT, RES1[13], FIFO;
} SDIO_TypeDef;
#define SDIO ((SDIO_TypeDef *)0x40012C00u)
#define GPIOD ((GPIO_TypeDef *)0x40020C00u)

#define STA_CCRCFAIL (1u << 0)
#define STA_DCRCFAIL (1u << 1)
#define STA_CTIMEOUT (1u << 2)
#define STA_DTIMEOUT (1u << 3)
#define STA_TXUNDERR (1u << 4)
#define STA_RXOVERR  (1u << 5)
#define STA_CMDREND  (1u << 6)
#define STA_CMDSENT  (1u << 7)
#define STA_DATAEND  (1u << 8)
#define STA_DBCKEND  (1u << 10)
#define STA_CMDACT   (1u << 11)
#define STA_TXFIFOHE (1u << 14)
#define STA_RXFIFOHF (1u << 15)
#define STA_RXDAVL   (1u << 21)
#define DATA_ERR (STA_DCRCFAIL | STA_DTIMEOUT | STA_TXUNDERR | STA_RXOVERR)

#define RESP_NONE  0u
#define RESP_SHORT (1u << 6)
#define RESP_LONG  (3u << 6)
#define CPSMEN     (1u << 10)
#define CLKCR_CLKEN (1u << 8)
#define CLKCR_4BIT  (1u << 11)
#define DCTRL_DTEN  (1u << 0)
#define DCTRL_TOCARD 0u
#define DCTRL_FROMCARD (1u << 1)
#define DCTRL_DMAEN (1u << 3)
#define DCTRL_BS512 (9u << 4)

/* DMA2 stream 3 */
#define DMA2_BASE 0x40026400u
#define DMA2_LISR  REG32(DMA2_BASE + 0x00)
#define DMA2_LIFCR REG32(DMA2_BASE + 0x08)
#define DMA2_S3CR   REG32(DMA2_BASE + 0x10 + 0x18 * 3)
#define DMA2_S3NDTR REG32(DMA2_BASE + 0x14 + 0x18 * 3)
#define DMA2_S3PAR  REG32(DMA2_BASE + 0x18 + 0x18 * 3)
#define DMA2_S3M0AR REG32(DMA2_BASE + 0x1C + 0x18 * 3)

static uint32_t wbuf[128], rbuf[128], dbuf[256];
volatile uint32_t csd[4];
volatile uint32_t result;          /* 1 = all steps passed */
static uint32_t rca;
static int sdhc;

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

static void hex(uint32_t v, int digits)
{
    static const char h[] = "0123456789abcdef";
    for (int i = digits - 1; i >= 0; i--)
        putc1(h[(v >> (4 * i)) & 15]);
}

static void dec(uint32_t v)
{
    char b[12];
    int n = 0;
    do {
        b[n++] = (char)('0' + v % 10);
        v /= 10;
    } while (v);
    while (n)
        putc1(b[--n]);
}

static void fail(const char *where)
{
    puts1("ERR ");
    puts1(where);
    putc1(' ');
    hex(SDIO->STA, 8);
    puts1("\r\n");
    for (;;) { }
}

/* send a command; returns STA (CMDREND/CMDSENT/CCRCFAIL/CTIMEOUT) */
static uint32_t cmd(uint32_t idx, uint32_t arg, uint32_t resp)
{
    SDIO->ICR = 0x7FFu;
    SDIO->ARG = arg;
    SDIO->CMD = idx | resp | CPSMEN;
    uint32_t done = resp == RESP_NONE ? STA_CMDSENT : (STA_CMDREND | STA_CCRCFAIL | STA_CTIMEOUT);
    uint32_t s;
    while (!((s = SDIO->STA) & done)) { }
    return s;
}

static void cmd_r1(uint32_t idx, uint32_t arg, const char *where)
{
    uint32_t s = cmd(idx, arg, RESP_SHORT);
    if (!(s & STA_CMDREND) || SDIO->RESPCMD != idx || (SDIO->RESP1 & 0xFDF90008u))
        fail(where);
}

static uint32_t card_state(void)
{
    cmd_r1(13, rca << 16, "CMD13");
    return (SDIO->RESP1 >> 9) & 15;
}

static void wait_tran(void)
{
    while (card_state() != 4) { }
}

int main(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN | (1u << 2) | (1u << 3) | (1u << 22);   /* GPIOA, GPIOC, GPIOD, DMA2 */
    RCC_APB2ENR |= RCC_APB2ENR_USART1EN | (1u << 11);                          /* USART1, SDIO */
    gpio_af(GPIOA, 9, 7);
    gpio_af(GPIOA, 10, 7);
    USART1->BRR = USART_BRR(HSI_HZ, 115200u);
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;

    /* PLL48CLK from HSI: M=16 N=336 P=2 Q=7 -> 48 MHz (SYSCLK stays HSI) */
    RCC_PLLCFGR = 16u | (336u << 6) | (0u << 16) | (7u << 24);
    RCC_CR |= RCC_CR_PLLON;
    while (!(RCC_CR & RCC_CR_PLLRDY)) { }

    for (unsigned p = 8; p <= 12; p++) {
        gpio_af(GPIOC, p, 12);
        if (p != 12)
            gpio_pull(GPIOC, p, GPIO_PULL_UP);
    }
    gpio_af(GPIOD, 2, 12);
    gpio_pull(GPIOD, 2, GPIO_PULL_UP);

    SDIO->POWER = 3u;
    SDIO->CLKCR = CLKCR_CLKEN | 118u;                 /* 48 MHz / 120 = 400 kHz */
    for (volatile int i = 0; i < 200; i++) { }        /* >= 74 clocks before CMD0 */

    cmd(0, 0, RESP_NONE);
    uint32_t s = cmd(8, 0x1AAu, RESP_SHORT);
    int v2 = (s & STA_CMDREND) && (SDIO->RESP1 & 0xFFFu) == 0x1AAu;
    if (!v2)
        cmd(0, 0, RESP_NONE);       /* v1 card: CMD8 was illegal; CMD0 clears ILLEGAL_COMMAND (as the ST HAL does) */
    uint32_t ocr = 0;
    for (int tries = 0; tries < 10000; tries++) {
        cmd_r1(55, 0, "CMD55");
        s = cmd(41, 0x80FF8000u | (v2 ? 0x40000000u : 0u), RESP_SHORT);   /* R3: CCRCFAIL expected */
        if (!(s & STA_CCRCFAIL))
            fail("ACMD41");
        ocr = SDIO->RESP1;
        if (ocr & 0x80000000u)
            break;
    }
    if (!(ocr & 0x80000000u))
        fail("ACMD41 busy");
    sdhc = (ocr >> 30) & 1;
    if (!(cmd(2, 0, RESP_LONG) & STA_CMDREND))
        fail("CMD2");
    if (!(cmd(3, 0, RESP_SHORT) & STA_CMDREND))
        fail("CMD3");
    rca = SDIO->RESP1 >> 16;
    if (!(cmd(9, rca << 16, RESP_LONG) & STA_CMDREND))
        fail("CMD9");
    csd[0] = SDIO->RESP1;
    csd[1] = SDIO->RESP2;
    csd[2] = SDIO->RESP3;
    csd[3] = SDIO->RESP4;
    puts1(v2 ? "SD v2 " : "SD v1 ");
    puts1(sdhc ? "SDHC RCA " : "SDSC RCA ");
    hex(rca, 4);
    puts1("\r\nCSD");
    for (int i = 0; i < 4; i++) {
        putc1(' ');
        hex(csd[i], 8);
    }
    puts1("\r\nCAP ");
    uint32_t kib;
    if ((csd[0] >> 30) == 1) {                          /* CSD v2: (C_SIZE + 1) x 512 KiB */
        uint32_t c_size = ((csd[1] & 0x3Fu) << 16) | (csd[2] >> 16);
        kib = (c_size + 1u) * 512u;
    } else {                                          /* CSD v1 */
        uint32_t bl = (csd[1] >> 16) & 15u;
        uint32_t c_size = ((csd[1] & 0x3FFu) << 2) | (csd[2] >> 30);
        uint32_t mult = (csd[2] >> 15) & 7u;
        kib = ((c_size + 1u) << (mult + 2u + bl)) >> 10;
    }
    dec(kib);
    puts1(" KiB\r\n");

    cmd_r1(7, rca << 16, "CMD7");
    cmd_r1(55, rca << 16, "CMD55");
    cmd_r1(6, 2, "ACMD6");                            /* 4-bit bus */
    SDIO->CLKCR = CLKCR_CLKEN | CLKCR_4BIT | 4u;      /* 48 MHz / 6 = 8 MHz */
    cmd_r1(16, 512, "CMD16");

    const char *text = "CHIP_EM SDIO block";
    uint8_t *wb = (uint8_t *)wbuf;
    for (int i = 0; i < 512; i++)
        wb[i] = (uint8_t)(i * 7 + 3);
    for (int i = 0; text[i]; i++)
        wb[i] = (uint8_t)text[i];
    wb[18] = 0;
    uint32_t blk = 2, addr = sdhc ? blk : blk * 512u;

    /* ---- single block write, FIFO polled ---- */
    cmd_r1(24, addr, "CMD24");
    SDIO->DTIMER = 0xFFFFFFu;
    SDIO->DLEN = 512;
    SDIO->DCTRL = DCTRL_DTEN | DCTRL_TOCARD | DCTRL_BS512;
    int n = 0;
    while (!((s = SDIO->STA) & (DATA_ERR | STA_DATAEND))) {
        if ((s & STA_TXFIFOHE) && n < 128)
            for (int k = 0; k < 8; k++)
                SDIO->FIFO = wbuf[n++];
    }
    if (s & DATA_ERR)
        fail("WRITE");
    wait_tran();
    puts1("WRITE ok\r\n");

    /* ---- single block read, FIFO polled ---- */
    SDIO->ICR = 0x7FFu;
    SDIO->DTIMER = 0xFFFFFFu;
    SDIO->DLEN = 512;
    SDIO->DCTRL = DCTRL_DTEN | DCTRL_FROMCARD | DCTRL_BS512;
    cmd_r1(17, addr, "CMD17");
    n = 0;
    while (!((s = SDIO->STA) & (DATA_ERR | STA_DATAEND))) {
        if ((s & STA_RXFIFOHF) && n < 128)
            for (int k = 0; k < 8; k++)
                rbuf[n++] = SDIO->FIFO;
    }
    if (s & DATA_ERR)
        fail("READ");
    while ((SDIO->STA & STA_RXDAVL) && n < 128)
        rbuf[n++] = SDIO->FIFO;
    int same = n == 128;
    for (int i = 0; i < 128; i++)
        same &= rbuf[i] == wbuf[i];
    if (!same)
        fail("COMPARE");
    puts1("READ ok ");
    puts1((const char *)rbuf);
    puts1("\r\n");

    /* ---- two blocks with CMD18 + CMD12 through DMA2 stream 3 channel 4 ---- */
    SDIO->ICR = 0x7FFu;
    DMA2_LIFCR = 0x0F7D0F7Du;
    DMA2_S3PAR = (uint32_t)&SDIO->FIFO;
    DMA2_S3M0AR = (uint32_t)dbuf;
    DMA2_S3NDTR = 256;
    DMA2_S3CR = (4u << 25) | (2u << 13) | (2u << 11) | (1u << 10) | (0u << 6);   /* ch4, word, MINC, P2M */
    DMA2_S3CR |= 1u;
    SDIO->DTIMER = 0xFFFFFFu;
    SDIO->DLEN = 1024;
    SDIO->DCTRL = DCTRL_DTEN | DCTRL_FROMCARD | DCTRL_BS512 | DCTRL_DMAEN;
    cmd_r1(18, addr, "CMD18");
    while (!((s = SDIO->STA) & (DATA_ERR | STA_DATAEND))) { }
    if (s & DATA_ERR)
        fail("DMA READ");
    s = cmd(12, 0, RESP_SHORT);
    if (!(s & STA_CMDREND))
        fail("CMD12");
    while (!(DMA2_LISR & (1u << 27))) { }             /* TCIF3 */
    same = 1;
    for (int i = 0; i < 128; i++)
        same &= dbuf[i] == wbuf[i];
    if (!same)
        fail("DMA COMPARE");
    wait_tran();
    puts1("DMA ok\r\n");
    result = 1;
    puts1("done\r\n");
    for (;;) { }
}
