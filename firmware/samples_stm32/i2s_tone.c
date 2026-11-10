/* i2s_tone: STM32F405 SPI2 in I2S master transmit mode plays a 1 kHz tone to an I2S codec and I2S2ext (full-duplex
 * extension, slave receive on the same CK/WS) records what the codec sends back (boards/stm32_i2s_codec.yaml),
 * RM0090 28.4.
 *
 * Clocks: 16 MHz HSI core (no PLL). PLLI2S = HSI / PLLM (16, reset value) x PLLI2SN 256 / PLLI2SR 5 = 51.2 MHz;
 * I2SPR: I2SDIV = 12, ODD = 1, MCKOE = 1: MCK = 51.2 MHz / 25 = 2.048 MHz = 256 Fs on PC6 (I2S2_MCK), Fs = 8 kHz,
 * CK = 32 Fs = 256 kHz (16-bit data in 16-bit channels). Philips standard, CKPOL = 0.
 * Pins (AF5): PB12 = I2S2_WS, PB13 = I2S2_CK, PB15 = I2S2_SD (to the codec), PC6 = I2S2_MCK; PB14 = I2S2ext_SD (AF6,
 * from the codec). USART1 TX on PA9 (AF7, 115200 8N1).
 * TX: polled on SPI2 TXE, an 8-sample sine table (left) and its negation (right); RX: polled on I2S2ext RXNE. After 64
 * received frames it prints the counts and the first received words, then clears I2SE (the master finishes the
 * current channel slot). */
#include "stm32f405.h"

typedef struct {
    volatile uint32_t CR1, CR2, SR, DR, CRCPR, RXCRCR, TXCRCR, I2SCFGR, I2SPR;
} SPI_TypeDef;
#define SPI2      ((SPI_TypeDef *)0x40003800u)
#define I2S2EXT   ((SPI_TypeDef *)0x40003400u)
#define RCC_PLLI2SCFGR REG32(RCC_BASE + 0x84)
#define RCC_CR_PLLI2SON  (1u << 26)
#define RCC_CR_PLLI2SRDY (1u << 27)
#define RCC_AHB1ENR_GPIOCEN (1u << 2)
#define RCC_APB1ENR_SPI2EN  (1u << 14)
#define SR_RXNE   (1u << 0)
#define SR_TXE    (1u << 1)
#define SR_UDR    (1u << 3)
#define SR_OVR    (1u << 6)
#define I2SMOD    (1u << 11)
#define I2SE      (1u << 10)
#define I2SCFG_SLAVE_RX  (1u << 8)
#define I2SCFG_MASTER_TX (2u << 8)
#define I2SPR_MCKOE (1u << 9)
#define I2SPR_ODD   (1u << 8)

static const int16_t sine8[8] = {0, 7071, 10000, 7071, 0, -7071, -10000, -7071};
volatile uint32_t tx_words, rx_words, done;
volatile uint16_t rx_buf[16];

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

static void puthex(uint32_t v, int digits)
{
    while (digits--)
        putc1("0123456789ABCDEF"[(v >> (4 * digits)) & 0xF]);
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
        putc1(d[--i]);
}

static uint16_t next_word(void)
{
    uint32_t n = tx_words++;
    int16_t s = sine8[(n >> 1) & 7];
    return (uint16_t)((n & 1) ? -s : s);             /* left = sine, right = -sine */
}

int main(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN | RCC_AHB1ENR_GPIOBEN | RCC_AHB1ENR_GPIOCEN;
    RCC_APB2ENR |= RCC_APB2ENR_USART1EN;
    RCC_APB1ENR |= RCC_APB1ENR_SPI2EN;
    gpio_af(GPIOA, 9, 7);
    USART1->BRR = USART_BRR(HSI_HZ, 115200u);
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE;
    gpio_af(GPIOB, 12, 5);
    gpio_af(GPIOB, 13, 5);
    gpio_af(GPIOB, 15, 5);
    gpio_af(GPIOB, 14, 6);
    gpio_af(GPIOC, 6, 5);

    RCC_PLLI2SCFGR = (5u << 28) | (256u << 6);       /* R = 5, N = 256 (PLLM = 16 from PLLCFGR) */
    RCC_CR |= RCC_CR_PLLI2SON;
    while (!(RCC_CR & RCC_CR_PLLI2SRDY)) { }

    SPI2->I2SPR = I2SPR_MCKOE | I2SPR_ODD | 12u;     /* MCK 2.048 MHz, Fs 8 kHz */
    SPI2->I2SCFGR = I2SMOD | I2SCFG_MASTER_TX;       /* Philips, 16-bit data, 16-bit channel, CKPOL = 0 */
    I2S2EXT->I2SCFGR = I2SMOD | I2SCFG_SLAVE_RX;     /* same format, receives on I2S2ext_SD */
    I2S2EXT->I2SCFGR |= I2SE;                        /* the extension first: it starts with the master */
    SPI2->I2SCFGR |= I2SE;

    while (rx_words < 2u * 64u) {
        if (SPI2->SR & SR_TXE)
            SPI2->DR = next_word();
        if (I2S2EXT->SR & SR_RXNE) {
            uint16_t w = (uint16_t)I2S2EXT->DR;
            if (rx_words < 16u)
                rx_buf[rx_words] = w;
            rx_words++;
        }
    }
    uint32_t ovr = I2S2EXT->SR & SR_OVR;
    SPI2->I2SCFGR &= ~I2SE;                          /* stops at the end of the current channel slot */
    I2S2EXT->I2SCFGR &= ~I2SE;
    puts1("I2S 8000 Hz: tx ");
    putdec(tx_words);
    puts1(" rx ");
    putdec(rx_words);
    puts1(ovr ? " overrun" : " ok");
    puts1("\r\nRX");
    for (int i = 0; i < 8; i++) {
        putc1(' ');
        puthex(rx_buf[i], 4);
    }
    puts1("\r\n");
    done = 1;
    for (;;)
        __WFI();
}
