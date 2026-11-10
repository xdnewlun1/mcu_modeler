/* i2s_tone: K22 I2S0 (SAI) master plays a 1 kHz tone to an I2S codec and records what the codec sends back
 * (boards/k22_i2s_codec.yaml), K22 RM ch. 51.
 *
 * Clocks: 120 MHz core, bus /2. MCLK = SYSCLK x (FRACT+1)/(DIVIDE+1) = 120 MHz x 32/1875 = 2.048 MHz (MCR.MICS = 0,
 * MOE = 1, on PTC8 = I2S0_MCLK); bit clock = MCLK / ((DIV+1) x 2) = 256 kHz (TCR2.MSEL = MCLK, DIV = 3): 8 kHz
 * stereo, 2 x 16-bit words per frame. I2S format: frame sync = word select, active low (left), one bit early (FSE),
 * 16 bits wide; data MSB first (FBT = 15), driven on the falling bit-clock edge (BCP = 1).
 * Pins: PTB18 = I2S0_TX_BCLK, PTB19 = I2S0_TX_FS (ALT4), PTC1 = I2S0_TXD0 (ALT6), PTC5 = I2S0_RXD0 (ALT4).
 * The receiver runs synchronously to the transmitter (RCR2.SYNC = 1): same bit clock and frame sync.
 * TX: the FIFO is refilled while TCSR.FRF (<= 4 words) with an 8-sample sine table (left) and its negation (right).
 * RX: words are read while RCSR.FRF (> 0 words). After 64 frames it prints the counts and the first received frames
 * on UART0 (PTA2, 115200 8N1), then stops the transmitter (TE = 0: it finishes the current frame). */
#include "k22.h"

#define UART_CLK_HZ CORE_CLK_HZ
#define BAUD        115200u

#define I2S0_BASE   0x4002F000u
#define I2S0_TCSR   REG32(I2S0_BASE + 0x000)
#define I2S0_TCR1   REG32(I2S0_BASE + 0x004)
#define I2S0_TCR2   REG32(I2S0_BASE + 0x008)
#define I2S0_TCR3   REG32(I2S0_BASE + 0x00C)
#define I2S0_TCR4   REG32(I2S0_BASE + 0x010)
#define I2S0_TCR5   REG32(I2S0_BASE + 0x014)
#define I2S0_TDR0   REG32(I2S0_BASE + 0x020)
#define I2S0_RCSR   REG32(I2S0_BASE + 0x080)
#define I2S0_RCR1   REG32(I2S0_BASE + 0x084)
#define I2S0_RCR2   REG32(I2S0_BASE + 0x088)
#define I2S0_RCR3   REG32(I2S0_BASE + 0x08C)
#define I2S0_RCR4   REG32(I2S0_BASE + 0x090)
#define I2S0_RCR5   REG32(I2S0_BASE + 0x094)
#define I2S0_RDR0   REG32(I2S0_BASE + 0x0A0)
#define I2S0_MCR    REG32(I2S0_BASE + 0x100)
#define I2S0_MDR    REG32(I2S0_BASE + 0x104)
#define CSR_TE      (1u << 31)
#define CSR_FEF     (1u << 18)
#define CSR_FRF     (1u << 16)
#define CR2_SYNC_1  (1u << 30)
#define CR2_MSEL_MCLK (1u << 26)
#define CR2_BCP     (1u << 25)
#define CR2_BCD     (1u << 24)
#define CR2_DIV(x)  ((uint32_t)(x))
#define CR3_CE0     (1u << 16)
#define CR4_FRSZ(x) ((uint32_t)(x) << 16)
#define CR4_SYWD(x) ((uint32_t)(x) << 8)
#define CR4_MF      (1u << 4)
#define CR4_FSE     (1u << 3)
#define CR4_FSP     (1u << 1)
#define CR4_FSD     (1u << 0)
#define CR5_WNW(x)  ((uint32_t)(x) << 24)
#define CR5_W0W(x)  ((uint32_t)(x) << 16)
#define CR5_FBT(x)  ((uint32_t)(x) << 8)
#define MCR_MOE     (1u << 30)
#define MCR_DUF     (1u << 31)
#define SIM_SCGC6_I2S (1u << 15)

static const int16_t sine8[8] = {0, 7071, 10000, 7071, 0, -7071, -10000, -7071};
volatile uint32_t tx_words, rx_words, done;
volatile uint16_t rx_buf[16];

static void putc0(char c)
{
    while (!(UART0_S1 & UART_S1_TDRE))
        ;
    UART0_D = (uint8_t)c;
}

static void puts0(const char *s)
{
    while (*s)
        putc0(*s++);
}

static void puthex(uint32_t v, int digits)
{
    while (digits--)
        putc0("0123456789ABCDEF"[(v >> (4 * digits)) & 0xF]);
}

static void putdec(uint32_t v)
{
    char d[10];
    int i = 0;
    do {
        d[i++] = (char)('0' + v % 10u);
        v /= 10u;
    } while (v && i < 10);
    while (i)
        putc0(d[--i]);
}

static uint32_t next_word(void)
{
    uint32_t n = tx_words++;
    int16_t s = sine8[(n >> 1) & 7];
    return (uint16_t)((n & 1) ? -s : s);             /* left = sine, right = -sine */
}

int main(void)
{
    WDOG_Disable();
    SIM_CLKDIV1 = (0u << 28) | (1u << 24) | (4u << 16);          /* core /1, bus /2 = 60 MHz, flash /5 */
    SIM_SCGC5 |= SIM_SCGC5_PORTA | SIM_SCGC5_PORTB | SIM_SCGC5_PORTC;
    SIM_SCGC4 |= SIM_SCGC4_UART0;
    SIM_SCGC6 |= SIM_SCGC6_I2S;
    PORTA->PCR[2] = PORT_PCR_MUX(2);                             /* UART0_TX */
    PORTB->PCR[18] = PORT_PCR_MUX(4);                            /* I2S0_TX_BCLK */
    PORTB->PCR[19] = PORT_PCR_MUX(4);                            /* I2S0_TX_FS */
    PORTC->PCR[1] = PORT_PCR_MUX(6);                             /* I2S0_TXD0 */
    PORTC->PCR[5] = PORT_PCR_MUX(4);                             /* I2S0_RXD0 */
    PORTC->PCR[8] = PORT_PCR_MUX(4);                             /* I2S0_MCLK */

    uint32_t sbr = UART_CLK_HZ / (16u * BAUD);
    uint32_t brfa = ((UART_CLK_HZ * 4u / BAUD + 1u) / 2u - sbr * 32u) & 0x1Fu;
    UART0_C2 = 0;
    UART0_BDH = (uint8_t)((sbr >> 8) & 0x1F);
    UART0_BDL = (uint8_t)(sbr & 0xFF);
    UART0_C4 = (UART0_C4 & ~0x1Fu) | UART_C4_BRFA(brfa);
    UART0_C1 = 0;
    UART0_C2 = UART_C2_TE;

    /* audio master clock: 2.048 MHz from the 120 MHz system clock */
    I2S0_MCR = MCR_MOE;                                          /* MICS = 0: SYSCLK */
    I2S0_MDR = (31u << 12) | 1874u;
    while (I2S0_MCR & MCR_DUF)
        ;

    const uint32_t cr4 = CR4_FRSZ(1) | CR4_SYWD(15) | CR4_MF | CR4_FSE | CR4_FSP | CR4_FSD;
    const uint32_t cr5 = CR5_WNW(15) | CR5_W0W(15) | CR5_FBT(15);
    I2S0_TCR1 = 4;                                               /* FRF while <= 4 words */
    I2S0_TCR2 = CR2_MSEL_MCLK | CR2_BCP | CR2_BCD | CR2_DIV(3);
    I2S0_TCR3 = CR3_CE0;
    I2S0_TCR4 = cr4;
    I2S0_TCR5 = cr5;
    I2S0_RCR1 = 0;                                               /* FRF while > 0 words */
    I2S0_RCR2 = CR2_SYNC_1 | CR2_MSEL_MCLK | CR2_BCP | CR2_BCD | CR2_DIV(3);
    I2S0_RCR3 = CR3_CE0;
    I2S0_RCR4 = cr4;
    I2S0_RCR5 = cr5;
    for (int i = 0; i < 8; i++)
        I2S0_TDR0 = next_word();
    I2S0_RCSR = CSR_TE;                                          /* RE (same bit position) */
    I2S0_TCSR = CSR_TE;                                          /* transmitter last: both start */

    while (rx_words < 2u * 64u) {
        if (I2S0_TCSR & CSR_FRF)
            I2S0_TDR0 = next_word();
        if (I2S0_RCSR & CSR_FRF) {
            uint32_t w = I2S0_RDR0;
            if (rx_words < 16u)
                rx_buf[rx_words] = (uint16_t)w;
            rx_words++;
        }
    }
    I2S0_TCSR = 0;                                               /* stops at the end of the frame */
    I2S0_RCSR = 0;
    puts0("I2S 8000 Hz: tx ");
    putdec(tx_words);
    puts0(" rx ");
    putdec(rx_words);
    puts0(I2S0_TCSR & CSR_FEF ? " underrun" : " ok");
    puts0("\r\nRX");
    for (int i = 0; i < 8; i++) {
        putc0(' ');
        puthex(rx_buf[i], 4);
    }
    puts0("\r\n");
    done = 1;
    for (;;)
        __WFI();
}
