/* STM32F103 high density: SPI2 master to an SPI NOR flash and I2C2 master to a 24C02 EEPROM
 * (boards/stm32f103ze_spi_i2c.yaml, or --spi-flash SPI2:cs=PB12 alone).  HSI 8 MHz (APB1 = 8 MHz).
 * - SPI2 on PB13 SCK / PB14 MISO / PB15 MOSI (AF push-pull, MISO input floating), mode 0, PCLK1 / 8 = 1 MHz,
 *   software NSS; chip select PB12 GPIO output.  Reads the JEDEC ID: "JEDEC xxxxxx\r\n".
 * - I2C2 on PB10 SCL / PB11 SDA (AF open-drain), 100 kHz (FREQ 8, CCR 40, TRISE 9): writes "HD I2C2" at EEPROM
 *   address 0x20 (one page write, acknowledge polling), reads it back: "EEPROM HD I2C2\r\n".
 * - SPI3 (PB3 SCK / PB4 MISO / PB5 MOSI after releasing JTAG, SWJ_CFG = 010) in master mode with MOSI looped back to
 *   MISO on the board: sends 0xA5, 0x3C and prints what came back: "SPI3 a5 3c\r\n".
 * Ends with "done\r\n". */
#include "f1x.h"
#include "console.h"

#define I2C_CR1_PE    (1u << 0)
#define I2C_CR1_START (1u << 8)
#define I2C_CR1_STOP  (1u << 9)
#define I2C_CR1_ACK   (1u << 10)
#define I2C_CR1_SWRST (1u << 15)
#define I2C_SR1_SB    (1u << 0)
#define I2C_SR1_ADDR  (1u << 1)
#define I2C_SR1_BTF   (1u << 2)
#define I2C_SR1_RXNE  (1u << 6)
#define I2C_SR1_TXE   (1u << 7)
#define I2C_SR1_AF    (1u << 10)
#define I2C_SR2_BUSY  (1u << 1)
#define EE 0x50u
#define I2C I2C2_BASE

static const char msg[] = "HD I2C2";
volatile uint32_t jedec, polls, spi3_rx[2];

static uint8_t xfer(uint32_t spi, uint8_t b)
{
    while (!(SPI_SR(spi) & 2u))                      /* TXE */
        ;
    *(volatile uint8_t *)&SPI_DR(spi) = b;
    while (!(SPI_SR(spi) & 1u))                      /* RXNE */
        ;
    return *(volatile uint8_t *)&SPI_DR(spi);
}

static int i2c_start(unsigned addr_rw)
{
    I2C_CR1(I2C) |= I2C_CR1_START;
    while (!(I2C_SR1(I2C) & I2C_SR1_SB))
        ;
    I2C_DR(I2C) = addr_rw;
    for (;;) {
        unsigned s = I2C_SR1(I2C);
        if (s & I2C_SR1_ADDR)
            return 1;
        if (s & I2C_SR1_AF) {
            I2C_SR1(I2C) = ~I2C_SR1_AF;
            return 0;
        }
    }
}

static void i2c_stop(void)
{
    I2C_CR1(I2C) |= I2C_CR1_STOP;
    while (I2C_SR2(I2C) & I2C_SR2_BUSY)
        ;
}

static void clear_addr(void)
{
    (void)I2C_SR1(I2C);
    (void)I2C_SR2(I2C);
}

static void i2c_write(unsigned char b)
{
    while (!(I2C_SR1(I2C) & I2C_SR1_TXE))
        ;
    I2C_DR(I2C) = b;
}

int main(void)
{
    console_init(8000000u, 115200u);
    RCC_APB2ENR |= RCC_APB2ENR_IOPBEN | RCC_APB2ENR_AFIOEN;
    RCC_APB1ENR |= RCC_APB1ENR_SPI2EN | RCC_APB1ENR_I2C2EN | RCC_APB1ENR_SPI3EN;

    /* ---- SPI2 JEDEC ID ---- */
    GPIO_BSRR(GPIOB_BASE) = 1u << 12;
    GPIO_CONFIG(GPIOB_BASE, 12, GPIO_OUT_PP_50MHZ);
    GPIO_CONFIG(GPIOB_BASE, 13, GPIO_AF_PP_50MHZ);
    GPIO_CONFIG(GPIOB_BASE, 14, GPIO_IN_FLOATING);
    GPIO_CONFIG(GPIOB_BASE, 15, GPIO_AF_PP_50MHZ);
    SPI_CR1(SPI2_BASE) = (1u << 2) | (2u << 3) | (1u << 8) | (1u << 9);   /* MSTR, BR = /8, SSI, SSM */
    SPI_CR1(SPI2_BASE) |= 1u << 6;                                        /* SPE */
    GPIO_BSRR(GPIOB_BASE) = 1u << (12 + 16);
    xfer(SPI2_BASE, 0x9F);
    jedec = (uint32_t)xfer(SPI2_BASE, 0) << 16;
    jedec |= (uint32_t)xfer(SPI2_BASE, 0) << 8;
    jedec |= xfer(SPI2_BASE, 0);
    while (SPI_SR(SPI2_BASE) & (1u << 7))            /* BSY */
        ;
    GPIO_BSRR(GPIOB_BASE) = 1u << 12;
    puts_("JEDEC ");
    puthex(jedec, 6);
    puts_("\r\n");

    /* ---- I2C2 EEPROM ---- */
    GPIO_CONFIG(GPIOB_BASE, 10, GPIO_AF_OD_50MHZ);
    GPIO_CONFIG(GPIOB_BASE, 11, GPIO_AF_OD_50MHZ);
    I2C_CR1(I2C) = I2C_CR1_SWRST;
    I2C_CR1(I2C) = 0;
    I2C_CR2(I2C) = 8;
    I2C_CCR(I2C) = 40;
    I2C_TRISE(I2C) = 9;
    I2C_CR1(I2C) = I2C_CR1_PE;
    unsigned n = sizeof msg - 1, i;
    if (!i2c_start(EE << 1)) {
        i2c_stop();
        puts_("EEPROM no ACK\r\n");
        for (;;)
            ;
    }
    clear_addr();
    i2c_write(0x20);
    for (i = 0; i < n; i++)
        i2c_write((unsigned char)msg[i]);
    while (!(I2C_SR1(I2C) & I2C_SR1_BTF))
        ;
    i2c_stop();
    while (!i2c_start(EE << 1)) {                   /* acknowledge polling while the page is programmed */
        i2c_stop();
        polls++;
    }
    clear_addr();
    i2c_write(0x20);
    while (!(I2C_SR1(I2C) & I2C_SR1_TXE))
        ;
    i2c_start((EE << 1) | 1);
    char buf[sizeof msg];
    I2C_CR1(I2C) |= I2C_CR1_ACK;
    clear_addr();
    for (i = 0; i < n; i++) {
        if (n - i == 3) {
            while (!(I2C_SR1(I2C) & I2C_SR1_BTF))
                ;
            I2C_CR1(I2C) &= ~I2C_CR1_ACK;
            buf[i++] = (char)I2C_DR(I2C);
            while (!(I2C_SR1(I2C) & I2C_SR1_BTF))
                ;
            I2C_CR1(I2C) |= I2C_CR1_STOP;
            buf[i++] = (char)I2C_DR(I2C);
            buf[i] = (char)I2C_DR(I2C);
            break;
        }
        while (!(I2C_SR1(I2C) & I2C_SR1_RXNE))
            ;
        buf[i] = (char)I2C_DR(I2C);
    }
    buf[n] = 0;
    while (I2C_SR2(I2C) & I2C_SR2_BUSY)
        ;
    puts_("EEPROM ");
    puts_(buf);
    puts_("\r\n");

    /* ---- SPI3 loopback on PB3/PB4/PB5 (JTAG-DP off, SW-DP on: RM0008 9.3.5) ---- */
    AFIO_MAPR = (AFIO_MAPR & ~(7u << 24)) | AFIO_MAPR_SWJ_JTAGDISABLE;
    GPIO_CONFIG(GPIOB_BASE, 3, GPIO_AF_PP_50MHZ);
    GPIO_CONFIG(GPIOB_BASE, 4, GPIO_IN_FLOATING);
    GPIO_CONFIG(GPIOB_BASE, 5, GPIO_AF_PP_50MHZ);
    SPI_CR1(SPI3_BASE) = (1u << 2) | (2u << 3) | (1u << 8) | (1u << 9) | (1u << 6);
    spi3_rx[0] = xfer(SPI3_BASE, 0xA5);
    spi3_rx[1] = xfer(SPI3_BASE, 0x3C);
    puts_("SPI3 ");
    puthex(spi3_rx[0], 2);
    puts_(" ");
    puthex(spi3_rx[1], 2);
    puts_("\r\ndone\r\n");
    for (;;)
        __asm__ volatile("wfi");
}
