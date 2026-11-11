/* STM32F103 high density hd_sdio: SDIO host + SD card (boards/stm32f103ze_sdcard.yaml, or --sd-card SDIO alone).
 * Port of firmware/samples_stm32/sdio_card.c to RM0008 ch. 22: HSE 8 MHz x 9 = 72 MHz; SDIOCLK = HCLK = 72 MHz
 * (RM0008 22.2).  SDIO pins PC8..PC11 = D0..D3, PC12 = CK, PD2 = CMD as AF push-pull (no AF numbers on the F1).
 * USART1 PA9/PA10, 115200 8N1.  Card identification at 400 kHz (CLKDIV 178), then ACMD6 4-bit bus and SDIO_CK =
 * 12 MHz (CLKDIV 4).  Writes block 2 (CMD24, FIFO polled on TXFIFOHE), reads it back (CMD17, RXFIFOHF), then blocks 2..3
 * with CMD18 + CMD12 through DMA2 channel 4 (request SDIO, RM0008 13.3.7 Table 79; no flow control on the F1 DMA:
 * CNDTR = 256 words).  Prints:
 *   "SD v2 SDHC RCA xxxx\r\n" "CSD xxxxxxxx xxxxxxxx xxxxxxxx xxxxxxxx\r\n" "CAP <KiB> KiB\r\n" "WRITE ok\r\n"
 *   "READ ok CHIP_EM SDIO block\r\n" "DMA ok\r\n" "done\r\n"   (or "ERR <where> <STA>\r\n") */
#include "f1x.h"
#include "console.h"

typedef struct {
    volatile uint32_t POWER, CLKCR, ARG, CMD, RESPCMD, RESP1, RESP2, RESP3, RESP4, DTIMER, DLEN, DCTRL, DCOUNT, STA,
        ICR, MASK, RES0[2], FIFOCNT, RES1[13], FIFO;
} SDIO_TypeDef;
#define SDIO ((SDIO_TypeDef *)SDIO_BASE)

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

static uint32_t wbuf[128], rbuf[128], dbuf[256];
volatile uint32_t csd[4];
volatile uint32_t result;          /* 1 = all steps passed */
static uint32_t rca;
static int sdhc;

static void putc1(char c)
{
    putc_(c);
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
    clock_72mhz();
    console_init(72000000u, 115200u);
    RCC_APB2ENR |= RCC_APB2ENR_IOPCEN | RCC_APB2ENR_IOPDEN;
    RCC_AHBENR |= RCC_AHBENR_SDIOEN | RCC_AHBENR_DMA2EN;
    for (unsigned p = 8; p <= 12; p++)
        GPIO_CONFIG(GPIOC_BASE, p, GPIO_AF_PP_50MHZ);
    GPIO_CONFIG(GPIOD_BASE, 2, GPIO_AF_PP_50MHZ);

    SDIO->POWER = 3u;
    SDIO->CLKCR = CLKCR_CLKEN | 178u;                 /* 72 MHz / 180 = 400 kHz */
    for (volatile int i = 0; i < 1000; i++) { }       /* >= 74 clocks before CMD0 */

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
    SDIO->CLKCR = CLKCR_CLKEN | CLKCR_4BIT | 4u;      /* 72 MHz / 6 = 12 MHz */
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

    /* ---- two blocks with CMD18 + CMD12 through DMA2 channel 4 ---- */
    SDIO->ICR = 0x7FFu;
    DMA2_IFCR = 0xFu << 12;                           /* channel 4 flags */
    DMA2_CPAR(4) = (uint32_t)&SDIO->FIFO;
    DMA2_CMAR(4) = (uint32_t)dbuf;
    DMA2_CNDTR(4) = 256;
    DMA2_CCR(4) = DMA_CCR_MINC | DMA_CCR_PSIZE_32 | DMA_CCR_MSIZE_32 | (2u << 12);   /* P2M, word, priority high */
    DMA2_CCR(4) |= DMA_CCR_EN;
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
    while (!(DMA2_ISR & (1u << 13))) { }              /* TCIF4 */
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
