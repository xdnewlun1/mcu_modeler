/* sdhc_card: K22 SDHC host + SD card (run with --sd-card SDHC --clock 120e6, or boards/k22_sdcard.yaml).
 * Core clock 120 MHz as the samples assume (SIM_SOPT2[SDHCSRC] = 0: the SDHC clock is the core clock).
 * SDHC pins on port E, ALT4: PTE0 D1, PTE1 D0, PTE2 DCLK, PTE3 CMD, PTE4 D3, PTE5 D2 (pull-ups on CMD/D0-D3).
 * UART0 PTA2 (TX) 115200 8N1 from the 120 MHz core clock.
 * Identification at 375 kHz (SDCLKFS /64, DVS /5): RSTA, INITA (80 clocks), CMD0, CMD8, ACMD41 (HCS), CMD2, CMD3,
 * CMD9 (CSD), CMD7 (R1b: waits for TC); ACMD6 + PROCTL.DTW = 4 bits at 20 MHz (/2 x /3). Writes block 2 with the
 * CPU (DATPORT on BWEN, WML 16 words), reads it back (DATPORT on BREN), then writes blocks 4-5 with simple DMA
 * (DSADDR, CMD25 + auto CMD12) and reads them back with ADMA2 (two descriptors, CMD18 + auto CMD12). Prints:
 *   "SD v2 SDHC RCA xxxx\r\n" "CAP <KiB> KiB\r\n" "WRITE ok\r\n" "READ ok CHIP_EM SDHC block\r\n" "SDMA ok\r\n"
 *   "ADMA ok\r\n" "done\r\n"   (or "ERR <where> <IRQSTAT>\r\n") */
#include "k22.h"

#define SIM_SCGC3 REG32(SIM_BASE + 0x1030)
#define SIM_SCGC3_SDHC (1u << 17)

typedef struct {
    volatile uint32_t DSADDR, BLKATTR, CMDARG, XFERTYP, CMDRSP[4], DATPORT, PRSSTAT, PROCTL, SYSCTL, IRQSTAT,
        IRQSTATEN, IRQSIGEN, AC12ERR, HTCAPBLT, WML, RES0[2], FEVT, ADMAES, ADSADDR;
} SDHC_Type;
#define SDHC ((SDHC_Type *)0x400B1000u)

#define XT_DMAEN  (1u << 0)
#define XT_BCEN   (1u << 1)
#define XT_AC12EN (1u << 2)
#define XT_READ   (1u << 4)
#define XT_MSBSEL (1u << 5)
#define XT_R136   (1u << 16)
#define XT_R48    (2u << 16)
#define XT_R48B   (3u << 16)
#define XT_CCCEN  (1u << 19)
#define XT_CICEN  (1u << 20)
#define XT_DPSEL  (1u << 21)
#define XT_CMD(n) ((uint32_t)(n) << 24)
#define R1  (XT_R48 | XT_CCCEN | XT_CICEN)
#define R1B (XT_R48B | XT_CCCEN | XT_CICEN)

#define PS_CIHB  (1u << 0)
#define PS_CDIHB (1u << 1)
#define PS_SDSTB (1u << 3)
#define PS_BWEN  (1u << 10)
#define PS_BREN  (1u << 11)
#define IS_CC    (1u << 0)
#define IS_TC    (1u << 1)
#define IS_DINT  (1u << 3)
#define IS_ERR   0x117F0000u
#define SC_SDCLKEN (1u << 3)
#define SC_RSTA  (1u << 24)
#define SC_INITA (1u << 27)

static uint32_t wbuf[128], rbuf[128], dbuf[256], abuf[256];
static uint32_t adma[4] __attribute__((aligned(8)));
volatile uint32_t result;
static uint32_t rca;

static void putc0(char c)
{
    while (!(UART0_S1 & UART_S1_TDRE)) { }
    UART0_D = (uint8_t)c;
}

static void puts0(const char *s)
{
    while (*s)
        putc0(*s++);
}

static void hex(uint32_t v, int digits)
{
    static const char h[] = "0123456789abcdef";
    for (int i = digits - 1; i >= 0; i--)
        putc0(h[(v >> (4 * i)) & 15]);
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
        putc0(b[--n]);
}

static void fail(const char *where)
{
    puts0("ERR ");
    puts0(where);
    putc0(' ');
    hex(SDHC->IRQSTAT, 8);
    puts0("\r\n");
    for (;;) { }
}

/* send a command; returns IRQSTAT after CC or an error */
static uint32_t cmd(uint32_t idx, uint32_t arg, uint32_t flags)
{
    while (SDHC->PRSSTAT & PS_CIHB) { }
    if (flags & (XT_DPSEL | XT_R48B))
        while (SDHC->PRSSTAT & PS_CDIHB) { }
    SDHC->IRQSTAT = 0xFFFFFFFFu;
    SDHC->CMDARG = arg;
    SDHC->XFERTYP = XT_CMD(idx) | flags;
    uint32_t s;
    while (!((s = SDHC->IRQSTAT) & (IS_CC | IS_ERR))) { }
    return s;
}

static void cmd_ok(uint32_t idx, uint32_t arg, uint32_t flags, const char *where)
{
    uint32_t s = cmd(idx, arg, flags);
    if (s & IS_ERR)
        fail(where);
    if ((flags & XT_R48) && !(flags & XT_R136) && (SDHC->CMDRSP[0] & 0xFDF90008u))
        fail(where);
}

static void wait_tc(const char *where)
{
    uint32_t s;
    while (!((s = SDHC->IRQSTAT) & (IS_TC | IS_ERR))) { }
    if (s & IS_ERR)
        fail(where);
}

static void wait_tran(void)
{
    for (;;) {
        cmd_ok(13, rca << 16, R1, "CMD13");
        if (((SDHC->CMDRSP[0] >> 9) & 15) == 4)
            return;
    }
}

static void set_clock(uint32_t sdclkfs, uint32_t dvs)
{
    SDHC->SYSCTL &= ~SC_SDCLKEN;
    SDHC->SYSCTL = (SDHC->SYSCTL & ~0x000FFFF0u) | (0xEu << 16) | (sdclkfs << 8) | (dvs << 4);
    while (!(SDHC->PRSSTAT & PS_SDSTB)) { }
    SDHC->SYSCTL |= SC_SDCLKEN;
}

int main(void)
{
    WDOG_Disable();
    SIM_SCGC5 |= SIM_SCGC5_PORTA | SIM_SCGC5_PORTE;
    SIM_SCGC4 |= SIM_SCGC4_UART0;
    SIM_SCGC3 |= SIM_SCGC3_SDHC;
    PORTA->PCR[2] = PORT_PCR_MUX(2);                       /* UART0_TX */
    uint32_t sbr = CORE_CLK_HZ / (16u * 115200u);
    uint32_t brfa = ((CORE_CLK_HZ * 4u / 115200u + 1u) / 2u - sbr * 32u) & 0x1Fu;
    UART0_BDH = (uint8_t)((sbr >> 8) & 0x1F);
    UART0_BDL = (uint8_t)(sbr & 0xFF);
    UART0_C4 = (UART0_C4 & ~0x1Fu) | UART_C4_BRFA(brfa);
    UART0_C2 = UART_C2_TE;

    for (unsigned p = 0; p <= 5; p++)                      /* SDHC0 pins, ALT4; pull-ups except DCLK */
        PORTE->PCR[p] = PORT_PCR_MUX(4) | PORT_PCR_DSE | (p == 2 ? 0u : PORT_PCR_PE | PORT_PCR_PS);

    SDHC->SYSCTL = SC_RSTA;
    while (SDHC->SYSCTL & SC_RSTA) { }
    set_clock(0x20u, 4u);                                  /* 120 MHz / (64 x 5) = 375 kHz */
    SDHC->SYSCTL |= SC_INITA;                              /* 80 clocks before CMD0 */
    while (SDHC->SYSCTL & SC_INITA) { }

    cmd_ok(0, 0, 0, "CMD0");
    uint32_t s = cmd(8, 0x1AAu, R1);
    int v2 = !(s & IS_ERR) && (SDHC->CMDRSP[0] & 0xFFFu) == 0x1AAu;
    if (!v2)
        cmd_ok(0, 0, 0, "CMD0");                           /* v1 card: clears ILLEGAL_COMMAND */
    uint32_t ocr = 0;
    for (int tries = 0; tries < 10000; tries++) {
        cmd_ok(55, 0, R1, "CMD55");
        if (cmd(41, 0x00FF8000u | (v2 ? 0x40000000u : 0u), XT_R48) & IS_ERR)    /* R3: no CRC / index check */
            fail("ACMD41");
        ocr = SDHC->CMDRSP[0];
        if (ocr & 0x80000000u)
            break;
    }
    if (!(ocr & 0x80000000u))
        fail("ACMD41 busy");
    int sdhc = (ocr >> 30) & 1;
    cmd_ok(2, 0, XT_R136 | XT_CCCEN, "CMD2");
    if (cmd(3, 0, R1) & IS_ERR)                            /* R6: RCA + compressed status, no R1 check */
        fail("CMD3");
    rca = SDHC->CMDRSP[0] >> 16;
    cmd_ok(9, rca << 16, XT_R136 | XT_CCCEN, "CMD9");
    uint32_t r0 = SDHC->CMDRSP[0], r1 = SDHC->CMDRSP[1], r2 = SDHC->CMDRSP[2], r3 = SDHC->CMDRSP[3];
    puts0(v2 ? "SD v2 " : "SD v1 ");
    puts0(sdhc ? "SDHC RCA " : "SDSC RCA ");
    hex(rca, 4);
    puts0("\r\nCAP ");
    uint32_t kib;
    if (((r3 >> 22) & 3) == 1) {                           /* CSD v2: C_SIZE = CSD[69:48] = R[61:40] */
        kib = (((r1 >> 8) & 0x3FFFFFu) + 1u) * 512u;
    } else {                                               /* CSD v1 */
        uint32_t bl = (r2 >> 8) & 15u;
        uint32_t c_size = ((r2 & 3u) << 10) | (r1 >> 22);
        uint32_t mult = (r1 >> 7) & 7u;
        kib = ((c_size + 1u) << (mult + 2u + bl)) >> 10;
    }
    (void)r0;
    dec(kib);
    puts0(" KiB\r\n");

    cmd_ok(7, rca << 16, R1B, "CMD7");
    wait_tc("CMD7 busy");
    cmd_ok(55, rca << 16, R1, "CMD55");
    cmd_ok(6, 2, R1, "ACMD6");
    SDHC->PROCTL = (SDHC->PROCTL & ~6u) | (1u << 1);       /* DTW = 4 bits */
    set_clock(0x01u, 2u);                                  /* 120 MHz / (2 x 3) = 20 MHz */
    cmd_ok(16, 512, R1, "CMD16");

    const char *text = "CHIP_EM SDHC block";
    uint8_t *wb = (uint8_t *)wbuf;
    for (int i = 0; i < 512; i++)
        wb[i] = (uint8_t)(i * 5 + 1);
    for (int i = 0; text[i]; i++)
        wb[i] = (uint8_t)text[i];
    wb[18] = 0;
    uint32_t blk = 2, addr = sdhc ? blk : blk * 512u;
    SDHC->WML = (16u << 16) | 16u;

    /* ---- single block write, CPU on BWEN ---- */
    SDHC->BLKATTR = (1u << 16) | 512u;
    cmd_ok(24, addr, R1 | XT_DPSEL, "CMD24");
    int n = 0;
    while (n < 128) {
        if (SDHC->PRSSTAT & PS_BWEN)
            for (int k = 0; k < 16; k++)
                SDHC->DATPORT = wbuf[n++];
        if (SDHC->IRQSTAT & IS_ERR)
            fail("WRITE");
    }
    wait_tc("WRITE");
    wait_tran();
    puts0("WRITE ok\r\n");

    /* ---- single block read, CPU on BREN ---- */
    cmd_ok(17, addr, R1 | XT_DPSEL | XT_READ, "CMD17");
    n = 0;
    while (n < 128) {
        if (SDHC->PRSSTAT & PS_BREN)
            for (int k = 0; k < 16; k++)
                rbuf[n++] = SDHC->DATPORT;
        if (SDHC->IRQSTAT & IS_ERR)
            fail("READ");
    }
    wait_tc("READ");
    int same = 1;
    for (int i = 0; i < 128; i++)
        same &= rbuf[i] == wbuf[i];
    if (!same)
        fail("COMPARE");
    puts0("READ ok ");
    puts0((const char *)rbuf);
    puts0("\r\n");

    /* ---- two blocks with simple DMA: CMD25 + auto CMD12 ---- */
    for (int i = 0; i < 256; i++)
        dbuf[i] = 0xA5000000u + (uint32_t)i;
    SDHC->PROCTL &= ~(3u << 8);                            /* DMAS = simple DMA */
    SDHC->DSADDR = (uint32_t)dbuf;
    SDHC->BLKATTR = (2u << 16) | 512u;
    uint32_t a4 = sdhc ? 4u : 4u * 512u;
    cmd_ok(25, a4, R1 | XT_DPSEL | XT_MSBSEL | XT_BCEN | XT_AC12EN | XT_DMAEN, "CMD25");
    wait_tc("SDMA");
    if (!(SDHC->IRQSTAT & IS_DINT) || SDHC->DSADDR != (uint32_t)dbuf + 1024u)
        fail("SDMA DINT");
    wait_tran();
    puts0("SDMA ok\r\n");

    /* ---- the same two blocks back with ADMA2: CMD18 + auto CMD12 ---- */
    adma[0] = (512u << 16) | (2u << 4) | 1u;               /* tran, valid */
    adma[1] = (uint32_t)&abuf[0];
    adma[2] = (512u << 16) | (2u << 4) | 2u | 1u;          /* tran, end, valid */
    adma[3] = (uint32_t)&abuf[128];
    SDHC->PROCTL = (SDHC->PROCTL & ~(3u << 8)) | (2u << 8);    /* DMAS = ADMA2 */
    SDHC->ADSADDR = (uint32_t)adma;
    SDHC->BLKATTR = (2u << 16) | 512u;
    cmd_ok(18, a4, R1 | XT_DPSEL | XT_READ | XT_MSBSEL | XT_BCEN | XT_AC12EN | XT_DMAEN, "CMD18");
    wait_tc("ADMA");
    same = (SDHC->IRQSTAT & IS_DINT) != 0;
    for (int i = 0; i < 256; i++)
        same &= abuf[i] == dbuf[i];
    if (!same)
        fail("ADMA COMPARE");
    wait_tran();
    puts0("ADMA ok\r\n");
    result = 1;
    puts0("done\r\n");
    for (;;) { }
}
