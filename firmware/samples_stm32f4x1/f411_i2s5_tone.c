/* f411_i2s5_tone: STM32F411 SPI5 in I2S master transmit mode (I2S5; the F411 has I2S on all five SPIs) plays an
 * 8-sample sine to an I2S codec (boards/stm32f411_i2s5_codec.yaml).  Runs on the 16 MHz HSI (no main PLL).
 * Clock: the F411 PLLI2S has its own input divider PLLI2SM (RCC_PLLI2SCFGR[5:0], header RCC_PLLI2SCFGR_PLLI2SM):
 * HSI 16 MHz / PLLI2SM 16 (VCO input 1 MHz, RM0383 6.3.20: 1..2 MHz) x PLLI2SN 128 / PLLI2SR 5 = 25.6 MHz = I2SCLK
 * (on the F401/F405 PLLI2S shares PLLM).  RCC_PLLI2SCFGR reads back 0x54002010: bit 26 is reserved and resets to 1.
 * I2SPR: I2SDIV = 12, ODD = 1, no MCK: Fs = I2SCLK / (32 x 25) = 32 kHz, CK = 32 Fs = 1.024 MHz (16-bit data in 16-bit
 * channels, Philips).  Pins (AF6): PB0 = I2S5_CK, PB1 = I2S5_WS, PB8 = I2S5_SD.  USART2 on PA2 (AF7, 115200 baud from
 * the 16 MHz PCLK1) reports "I2S5 <PLLI2SCFGR hex> tx <n> ok\r\n" after 256 words, then clears I2SE. */
#include "stm32f4x1.h"

#define SR_TXE    (1u << 1)
#define I2SMOD    (1u << 11)
#define I2SE      (1u << 10)
#define I2SCFG_MASTER_TX (2u << 8)
#define I2SPR_ODD (1u << 8)

static const int16_t sine8[8] = {0, 7071, 10000, 7071, 0, -7071, -10000, -7071};
volatile uint32_t tx_words;

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

static void puthex(uint32_t v)
{
    for (int d = 7; d >= 0; d--)
        putc2("0123456789ABCDEF"[(v >> (4 * d)) & 0xF]);
}

static void putdec(uint32_t v)
{
    char b[10];
    int i = 0;
    do {
        b[i++] = (char)('0' + v % 10u);
        v /= 10u;
    } while (v);
    while (i)
        putc2(b[--i]);
}

int main(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN | RCC_AHB1ENR_GPIOBEN;
    RCC_APB1ENR |= RCC_APB1ENR_USART2EN;
    RCC_APB2ENR |= RCC_APB2ENR_SPI5EN;
    gpio_af(GPIOA, 2, 7);
    USART2->BRR = USART_BRR(16000000u, 115200u);
    USART2->CR1 = USART_CR1_UE | USART_CR1_TE;
    gpio_af(GPIOB, 0, 6);                        /* I2S5_CK */
    gpio_af(GPIOB, 1, 6);                        /* I2S5_WS */
    gpio_af(GPIOB, 8, 6);                        /* I2S5_SD */

    RCC_PLLI2SCFGR = 16u | (128u << 6) | (5u << 28);     /* PLLI2SM 16, PLLI2SN 128, PLLI2SR 5 */
    RCC_CR |= RCC_CR_PLLI2SON;
    while (!(RCC_CR & RCC_CR_PLLI2SRDY)) { }

    SPI5->I2SPR = 12u | I2SPR_ODD;
    SPI5->I2SCFGR = I2SMOD | I2SCFG_MASTER_TX;
    SPI5->I2SCFGR |= I2SE;
    while (tx_words < 256u) {
        if (SPI5->SR & SR_TXE) {
            uint32_t n = tx_words++;
            int16_t s = sine8[(n >> 1) & 7];
            SPI5->DR = (uint16_t)((n & 1) ? -s : s);
        }
    }
    while (!(SPI5->SR & SR_TXE)) { }
    SPI5->I2SCFGR &= ~I2SE;
    puts2("I2S5 ");
    puthex(RCC_PLLI2SCFGR);
    puts2(" tx ");
    putdec(tx_words);
    puts2(" ok\r\n");
    for (;;) { }
}
