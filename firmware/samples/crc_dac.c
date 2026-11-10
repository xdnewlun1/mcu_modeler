/* crc_dac: the analog/system extras of the K22 (CRC, DAC0, CMP0, VREF), reported on UART0 (PTA2 TX, ALT2).
 *
 * 1. CRC-32 (IEEE) of "123456789" with the CRC module (TCRC=1, TOT=TOTR=10, FXOR=1, poly 0x04C11DB7, seed
 *    0xFFFFFFFF), fed as two 32-bit words and one byte: prints "CRC32=CBF43926".
 * 2. CRC-16/CCITT-FALSE of the same string (poly 0x1021, seed 0xFFFF, TOT=11: the little-endian 32-bit writes are
 *    byte-transposed so the string's first byte enters the engine first, as KSDK does): prints "CRC16=29B1".
 * 3. VREF on (high-power buffer), wait for VREFST: prints "VREF ok".
 * 4. DAC0: VDDA reference, 4-word buffer (normal mode, upper limit 3) loaded with 0x400/0x800/0xC00/0xFFF, software
 *    trigger 5 times: prints the read pointer after each trigger "DAC rp=1" ... "DAC rp=0" (wraps), then
 *    "DAC sr=..." (SR flags).
 * 5. CMP0: plus input IN0 (pin PTC6, CMP0_IN0), minus input IN7 = the 6-bit DAC at VDD * 32/64 = 1.65 V, output on
 *    PTC5 (ALT6 CMP0_OUT, OPE=1). Prints "COUT=<0|1>" at start and on every change of SCR[COUT] (polled).
 *
 * UART0 baud: as uart_echo.c (60 MHz assumed; the stream is what the tests check). */
#include "k22.h"

#define UART_CLK_HZ BUS_CLK_HZ
#define BAUD        115200u

#define SIM_SCGC2      REG32(SIM_BASE + 0x102C)
#define SIM_SCGC2_DAC0 (1u << 12)
#define SIM_SCGC4_CMP  (1u << 19)
#define SIM_SCGC4_VREF (1u << 20)
#define SIM_SCGC6_CRC  (1u << 18)

#define CRC_BASE   0x40032000u
#define CRC_DATA   REG32(CRC_BASE + 0x0)
#define CRC_DATALL REG8(CRC_BASE + 0x0)
#define CRC_GPOLY  REG32(CRC_BASE + 0x4)
#define CRC_CTRL   REG32(CRC_BASE + 0x8)
#define CRC_TOT(x)  ((uint32_t)(x) << 30)
#define CRC_TOTR(x) ((uint32_t)(x) << 28)
#define CRC_FXOR   (1u << 26)
#define CRC_WAS    (1u << 25)
#define CRC_TCRC   (1u << 24)

#define VREF_SC    REG8(0x40074001u)

#define DAC0_BASE  0x400CC000u
#define DAC0_DATL(n) REG8(DAC0_BASE + 2u * (n))
#define DAC0_DATH(n) REG8(DAC0_BASE + 2u * (n) + 1u)
#define DAC0_SR    REG8(DAC0_BASE + 0x20)
#define DAC0_C0    REG8(DAC0_BASE + 0x21)
#define DAC0_C1    REG8(DAC0_BASE + 0x22)
#define DAC0_C2    REG8(DAC0_BASE + 0x23)

#define CMP0_BASE  0x40073000u
#define CMP0_CR0   REG8(CMP0_BASE + 0)
#define CMP0_CR1   REG8(CMP0_BASE + 1)
#define CMP0_FPR   REG8(CMP0_BASE + 2)
#define CMP0_SCR   REG8(CMP0_BASE + 3)
#define CMP0_DACCR REG8(CMP0_BASE + 4)
#define CMP0_MUXCR REG8(CMP0_BASE + 5)

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

static void puthex(uint32_t v, int digits)
{
    for (int i = digits - 1; i >= 0; i--)
        putc0("0123456789ABCDEF"[(v >> (4 * i)) & 0xF]);
}

static void uart0_init(void)
{
    SIM_SCGC5 |= SIM_SCGC5_PORTA | SIM_SCGC5_PORTC;
    SIM_SCGC4 |= SIM_SCGC4_UART0;
    PORTA->PCR[1] = PORT_PCR_MUX(2);
    PORTA->PCR[2] = PORT_PCR_MUX(2);
    uint32_t sbr = UART_CLK_HZ / (16u * BAUD);
    uint32_t brfa = ((UART_CLK_HZ * 4u / BAUD + 1u) / 2u - sbr * 32u) & 0x1Fu;
    UART0_C2 = 0;
    UART0_BDH = (uint8_t)((sbr >> 8) & 0x1F);
    UART0_BDL = (uint8_t)(sbr & 0xFF);
    UART0_C4 = (UART0_C4 & ~0x1Fu) | UART_C4_BRFA(brfa);
    UART0_C1 = 0;
    UART0_C2 = UART_C2_TE | UART_C2_RE;
}

static uint32_t crc_run(uint32_t ctrl, uint32_t poly, uint32_t seed, const char *s, uint32_t n)
{
    CRC_CTRL = ctrl;                       /* TCRC first: GPOLY[HIGH] is writable only in 32-bit mode */
    CRC_GPOLY = poly;
    CRC_CTRL = ctrl | CRC_WAS;
    CRC_DATA = seed;
    CRC_CTRL = ctrl;
    uint32_t i = 0;
    for (; i + 4 <= n; i += 4) {
        uint32_t w = (uint32_t)(uint8_t)s[i] | (uint32_t)(uint8_t)s[i + 1] << 8 |
                     (uint32_t)(uint8_t)s[i + 2] << 16 | (uint32_t)(uint8_t)s[i + 3] << 24;
        CRC_DATA = w;
    }
    for (; i < n; i++)
        CRC_DATALL = (uint8_t)s[i];
    return CRC_DATA;
}

int main(void)
{
    WDOG_Disable();
    uart0_init();
    SIM_SCGC6 |= SIM_SCGC6_CRC;
    SIM_SCGC4 |= SIM_SCGC4_CMP | SIM_SCGC4_VREF;
    SIM_SCGC2 |= SIM_SCGC2_DAC0;

    static const char msg[] = "123456789";
    uint32_t c32 = crc_run(CRC_TCRC | CRC_TOT(2) | CRC_TOTR(2) | CRC_FXOR, 0x04C11DB7u, 0xFFFFFFFFu, msg, 9);
    puts0("CRC32=");
    puthex(c32, 8);
    puts0("\r\n");
    uint32_t c16 = crc_run(CRC_TOT(3), 0x1021u, 0xFFFFu, msg, 9);
    puts0("CRC16=");
    puthex(c16 & 0xFFFF, 4);
    puts0("\r\n");

    VREF_SC = 0x80u | 0x40u | 0x20u | 0x01u;        /* VREFEN, REGEN, ICOMPEN, MODE_LV=01 (high-power buffer) */
    while (!(VREF_SC & 0x04u)) { }
    puts0("VREF ok\r\n");

    static const uint16_t words[4] = {0x400, 0x800, 0xC00, 0xFFF};
    for (int i = 0; i < 4; i++) {
        DAC0_DATL(i) = (uint8_t)words[i];
        DAC0_DATH(i) = (uint8_t)(words[i] >> 8);
    }
    DAC0_C2 = 0x03;                                  /* read pointer 0, upper limit 3 */
    DAC0_C1 = 0x01;                                  /* DACBFEN, normal mode, watermark 1 word */
    DAC0_C0 = 0x80u | 0x40u | 0x20u;                 /* DACEN, DACRFS=1 (VDDA), DACTRGSEL=1 (software) */
    DAC0_SR = 0;
    for (int k = 0; k < 5; k++) {
        DAC0_C0 |= 0x10u;                            /* DACSWTRG */
        puts0("DAC rp=");
        puthex(DAC0_C2 >> 4, 1);
        puts0("\r\n");
    }
    puts0("DAC sr=");
    puthex(DAC0_SR, 1);
    puts0("\r\n");

    PORTC->PCR[5] = PORT_PCR_MUX(6);                 /* CMP0_OUT */
    CMP0_CR0 = 0;                                    /* no filter, hysteresis level 0 */
    CMP0_FPR = 0;
    CMP0_DACCR = 0x80u | 0x40u | 31u;                /* 6-bit DAC on, Vin2 = VDD, (31+1)/64 * 3.3 V = 1.65 V */
    CMP0_MUXCR = (0u << 3) | 7u;                     /* + = IN0 (CMP0_IN0 pin), - = IN7 (6-bit DAC) */
    CMP0_CR1 = 0x10u | 0x02u | 0x01u;                /* PMODE high speed, OPE, EN */
    int last = -1;
    for (;;) {
        int cout = CMP0_SCR & 1;
        if (cout != last) {
            last = cout;
            puts0("COUT=");
            putc0(cout ? '1' : '0');
            puts0("\r\n");
        }
    }
}
