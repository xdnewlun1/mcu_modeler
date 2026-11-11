/* spi_flash.c - SPI master (mode 0, MSB first, SPICLK = fosc/16) reads a serial NOR flash
 * (boards/p89cv51_spi_flash_c.yaml): JEDEC ID (9Fh) into `id`, READ (03h) of 16 bytes at 000000h into `data`,
 * then prints "ID EF4016" and the first bytes on the UART.  Chip select on P2.0 (GPIO). */
#include <stdio.h>
#include "uart.h"

#define CS P2_0
unsigned char id[3];
__xdata unsigned char data[16];

static unsigned char spi_xfer(unsigned char b)
{
    SPDAT = b;
    while (!(SPSR & SPSR_SPIF))
        ;
    SPSR = 0;                            /* clear SPIF (and WCOL) */
    return SPDAT;
}

void main(void)
{
    unsigned char i;
    uart_init();
    CS = 1;
    SPCR = SPCR_SPEN | SPCR_MSTR | SPCR_SPR0;
    CS = 0;
    spi_xfer(0x9F);
    for (i = 0; i < 3; i++)
        id[i] = spi_xfer(0xFF);
    CS = 1;
    CS = 0;
    spi_xfer(0x03);
    spi_xfer(0);
    spi_xfer(0);
    spi_xfer(0);
    for (i = 0; i < 16; i++)
        data[i] = spi_xfer(0xFF);
    CS = 1;
    printf("ID %02X%02X%02X data %02X %02X\r\n", (unsigned int)id[0], (unsigned int)id[1],
           (unsigned int)id[2], (unsigned int)data[0], (unsigned int)data[15]);
    uart_flush();
    for (;;)
        PCON |= PCON_IDL;
}
