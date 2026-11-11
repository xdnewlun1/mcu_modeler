/* STM32F103T6 spi_flash: SPI1 master to an SPI NOR flash (run with --spi-flash SPI1:cs=PA4).  HSI 8 MHz, SPI1 on
 * PA5 SCK / PA6 MISO / PA7 MOSI (AF push-pull outputs, MISO input floating), mode 0, PCLK2/8 = 1 MHz, software NSS
 * (SSM = SSI = 1); chip select PA4 is a GPIO output (idle high).  USART1 PA9/PA10 115200.
 * Reads the JEDEC ID (0x9F), programs "CHIP_EM" at 0x000100 (WREN, PP, RDSR until WIP = 0), reads 16 bytes back:
 *   "JEDEC xxxxxx\r\n" "SR 00\r\n" "READ 43 48 49 50 5F 45 4D FF ...\r\n" "TEXT CHIP_EM\r\ndone\r\n" */
#include "console.h"

volatile uint32_t jedec_id;
volatile uint8_t page[16];

static uint8_t xfer(uint8_t b)
{
    while (!(SPI_SR(SPI1_BASE) & 2u))                /* TXE */
        ;
    *(volatile uint8_t *)&SPI_DR(SPI1_BASE) = b;
    while (!(SPI_SR(SPI1_BASE) & 1u))                /* RXNE */
        ;
    return *(volatile uint8_t *)&SPI_DR(SPI1_BASE);
}

static void cs(int level)
{
    while (SPI_SR(SPI1_BASE) & (1u << 7))            /* BSY */
        ;
    GPIO_BSRR(GPIOA_BASE) = level ? (1u << 4) : (1u << (4 + 16));
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
    console_init(8000000u, 115200u);
    RCC_APB2ENR |= RCC_APB2ENR_SPI1EN;
    GPIO_BSRR(GPIOA_BASE) = 1u << 4;
    GPIO_CONFIG(GPIOA_BASE, 4, GPIO_OUT_PP_50MHZ);
    GPIO_CONFIG(GPIOA_BASE, 5, GPIO_AF_PP_50MHZ);
    GPIO_CONFIG(GPIOA_BASE, 6, GPIO_IN_FLOATING);
    GPIO_CONFIG(GPIOA_BASE, 7, GPIO_AF_PP_50MHZ);
    SPI_CR1(SPI1_BASE) = (1u << 2) | (2u << 3) | (1u << 9) | (1u << 8);   /* MSTR, BR = /8, SSM, SSI */
    SPI_CR1(SPI1_BASE) |= 1u << 6;                                        /* SPE */

    cs(0);
    xfer(0x9F);
    jedec_id = (uint32_t)xfer(0) << 16;
    jedec_id |= (uint32_t)xfer(0) << 8;
    jedec_id |= xfer(0);
    cs(1);
    puts_("JEDEC ");
    puthex(jedec_id, 6);
    puts_("\r\n");

    cs(0); xfer(0x06); cs(1);
    cs(0);
    cmd_addr(0x02, 0x000100);
    for (const char *s = "CHIP_EM"; *s; s++)
        xfer((uint8_t)*s);
    cs(1);
    uint8_t sr;
    do {
        cs(0); xfer(0x05); sr = xfer(0); cs(1);
    } while (sr & 1);
    puts_("SR ");
    puthex(sr, 2);
    puts_("\r\n");

    cs(0);
    cmd_addr(0x03, 0x000100);
    for (unsigned i = 0; i < sizeof page; i++)
        page[i] = xfer(0);
    cs(1);
    puts_("READ");
    for (unsigned i = 0; i < sizeof page; i++) {
        putc_(' ');
        puthex(page[i], 2);
    }
    puts_("\r\nTEXT ");
    for (unsigned i = 0; i < sizeof page && page[i] != 0xFF; i++)
        putc_((char)page[i]);
    puts_("\r\ndone\r\n");
    for (;;)
        __asm__ volatile("wfi");
}
