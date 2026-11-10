/* spi_flash: SPI1 master talking to an SPI NOR flash (run with --spi-flash SPI1:cs=PA4).
 * 16 MHz HSI. SPI1 on PA5 (SCK) / PA6 (MISO) / PA7 (MOSI), AF5, mode 0, PCLK2/8 = 2 MHz, software NSS (SSM=SSI=1);
 * chip select PA4 is a GPIO output (idle high). USART1 PA9/PA10, 115200 8N1.
 * Reads the JEDEC ID (0x9F), programs "CHIP_EM" into page 0x000100 (WREN 0x06, PP 0x02, RDSR 0x05 until WIP = 0),
 * reads 16 bytes from 0x000100 (READ 0x03) and prints everything:
 *   "JEDEC 202015\r\n" "SR 00\r\n" "READ 43 48 49 50 5F 45 4D FF ...\r\n" "TEXT CHIP_EM\r\ndone\r\n" */
#include "stm32f405.h"

typedef struct {
    volatile uint32_t CR1, CR2, SR, DR, CRCPR, RXCRCR, TXCRCR, I2SCFGR, I2SPR;
} SPI_TypeDef;
#define SPI1 ((SPI_TypeDef *)0x40013000u)
#define SPI_CR1_MSTR (1u << 2)
#define SPI_CR1_BR_DIV8 (2u << 3)
#define SPI_CR1_SPE  (1u << 6)
#define SPI_CR1_SSI  (1u << 8)
#define SPI_CR1_SSM  (1u << 9)
#define SPI_SR_RXNE  (1u << 0)
#define SPI_SR_TXE   (1u << 1)
#define SPI_SR_BSY   (1u << 7)
#define RCC_APB2ENR_SPI1EN (1u << 12)

volatile uint32_t jedec_id;
volatile uint8_t page[16];

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

static void hex8(unsigned v)
{
    static const char h[] = "0123456789ABCDEF";
    putc1(h[(v >> 4) & 15]);
    putc1(h[v & 15]);
}

static uint8_t xfer(uint8_t b)
{
    while (!(SPI1->SR & SPI_SR_TXE)) { }
    *(volatile uint8_t *)&SPI1->DR = b;
    while (!(SPI1->SR & SPI_SR_RXNE)) { }
    return *(volatile uint8_t *)&SPI1->DR;
}

static void cs(int level)
{
    while (SPI1->SR & SPI_SR_BSY) { }          /* last frame fully shifted out before CS changes */
    GPIOA->BSRR = level ? (1u << 4) : (1u << (4 + 16));
}

static void cmd_addr(uint8_t cmd, uint32_t a)
{
    xfer(cmd);
    xfer((uint8_t)(a >> 16));
    xfer((uint8_t)(a >> 8));
    xfer((uint8_t)a);
}

int main(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN;
    RCC_APB2ENR |= RCC_APB2ENR_USART1EN | RCC_APB2ENR_SPI1EN;
    gpio_af(GPIOA, 9, 7);
    gpio_af(GPIOA, 10, 7);
    USART1->BRR = USART_BRR(HSI_HZ, 115200u);
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;

    GPIOA->BSRR = 1u << 4;                     /* CS high before it becomes an output */
    gpio_mode(GPIOA, 4, GPIO_MODE_OUTPUT);
    gpio_af(GPIOA, 5, 5);
    gpio_af(GPIOA, 6, 5);
    gpio_af(GPIOA, 7, 5);
    SPI1->CR1 = SPI_CR1_MSTR | SPI_CR1_BR_DIV8 | SPI_CR1_SSM | SPI_CR1_SSI;
    SPI1->CR1 |= SPI_CR1_SPE;

    cs(0);
    xfer(0x9F);
    jedec_id = (uint32_t)xfer(0) << 16;
    jedec_id |= (uint32_t)xfer(0) << 8;
    jedec_id |= xfer(0);
    cs(1);
    puts1("JEDEC ");
    hex8(jedec_id >> 16); hex8(jedec_id >> 8); hex8(jedec_id);
    puts1("\r\n");

    cs(0); xfer(0x06); cs(1);                  /* WREN */
    cs(0);
    cmd_addr(0x02, 0x000100);                  /* page program */
    for (const char *s = "CHIP_EM"; *s; s++)
        xfer((uint8_t)*s);
    cs(1);
    uint8_t sr;
    do {
        cs(0); xfer(0x05); sr = xfer(0); cs(1);
    } while (sr & 1);
    puts1("SR ");
    hex8(sr);
    puts1("\r\n");

    cs(0);
    cmd_addr(0x03, 0x000100);
    for (unsigned i = 0; i < sizeof page; i++)
        page[i] = xfer(0);
    cs(1);
    puts1("READ");
    for (unsigned i = 0; i < sizeof page; i++) {
        putc1(' ');
        hex8(page[i]);
    }
    puts1("\r\nTEXT ");
    for (unsigned i = 0; i < sizeof page && page[i] != 0xFF; i++)
        putc1((char)page[i]);
    puts1("\r\ndone\r\n");
    for (;;) { }
}
