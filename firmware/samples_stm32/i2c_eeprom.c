/* i2c_eeprom: I2C1 master (PB6 SCL / PB7 SDA, AF4 open-drain, 100 kHz standard mode from PCLK1 = 16 MHz HSI:
 * FREQ = 16, CCR = 80, TRISE = 17) writes "Hello I2C!" to a 24C02 EEPROM at 0x50 (word address 0x10, one page
 * write), polls for the end of the write cycle (the EEPROM NACKs its address meanwhile), reads the string back
 * with a dummy write + repeated START + sequential read (ACK/NACK + STOP handled like RM0090 for N > 2 bytes) and
 * prints "EEPROM: Hello I2C!" on USART1 (PA9, 115200 8N1), plus the number of acknowledge polls. */
#include "i2c_regs.h"

#define EE 0x50u
static const char msg[] = "Hello I2C!";
volatile unsigned polls;

static int i2c_start(unsigned addr_rw)
{
    I2C1->CR1 |= I2C_CR1_START;
    while (!(I2C1->SR1 & I2C_SR1_SB)) { }
    I2C1->DR = addr_rw;                               /* SR1 read above + DR write clears SB */
    for (;;) {
        unsigned s = I2C1->SR1;
        if (s & I2C_SR1_ADDR)
            return 1;
        if (s & I2C_SR1_AF) {
            I2C1->SR1 = ~I2C_SR1_AF;
            return 0;
        }
    }
}

static void i2c_stop(void)
{
    I2C1->CR1 |= I2C_CR1_STOP;
    while (I2C1->SR2 & I2C_SR2_BUSY) { }
}

static void clear_addr(void)
{
    (void)I2C1->SR1;
    (void)I2C1->SR2;
}

static void i2c_write(unsigned char b)
{
    while (!(I2C1->SR1 & I2C_SR1_TXE)) { }
    I2C1->DR = b;
}

int main(void)
{
    char buf[sizeof msg];
    unsigned n = sizeof msg - 1, i;

    uart_init();
    i2c1_pins();
    RCC_APB1ENR |= RCC_APB1ENR_I2C1EN;
    I2C1->CR1 = I2C_CR1_SWRST;
    I2C1->CR1 = 0;
    I2C1->CR2 = 16;                                   /* FREQ = PCLK1 in MHz */
    I2C1->CCR = 80;                                   /* SM: Thigh = Tlow = 80 / 16 MHz = 5 us -> 100 kHz */
    I2C1->TRISE = 17;
    I2C1->CR1 = I2C_CR1_PE;

    /* page writes (8-byte pages: a write must not cross a page boundary, the address rolls over inside the page),
     * each followed by acknowledge polling: the EEPROM ignores its address during the write cycle */
    polls = 0;
    for (i = 0; i < n; ) {
        unsigned a = 0x10 + i, k = 8 - (a & 7);
        if (k > n - i)
            k = n - i;
        if (!i2c_start(EE << 1)) {
            i2c_stop();
            puts1("EEPROM: no ACK\r\n");
            for (;;) { }
        }
        clear_addr();
        i2c_write((unsigned char)a);
        while (k--)
            i2c_write((unsigned char)msg[i++]);
        while (!(I2C1->SR1 & I2C_SR1_BTF)) { }
        i2c_stop();
        while (!i2c_start(EE << 1)) {
            i2c_stop();
            polls++;
        }
        if (i < n) {
            clear_addr();
            i2c_stop();
        }
    }
    clear_addr();
    i2c_write(0x10);                                  /* dummy write: set the address pointer */
    while (!(I2C1->SR1 & I2C_SR1_TXE)) { }
    if (!i2c_start((EE << 1) | 1)) {                  /* repeated START, read */
        i2c_stop();
        puts1("EEPROM: read NACK\r\n");
        for (;;) { }
    }
    I2C1->CR1 |= I2C_CR1_ACK;
    clear_addr();
    for (i = 0; i < n; i++) {
        if (n - i == 3) {                             /* RM0090 27.3.3: N > 2 bytes, the last three */
            while (!(I2C1->SR1 & I2C_SR1_BTF)) { }
            I2C1->CR1 &= ~I2C_CR1_ACK;
            buf[i++] = (char)I2C1->DR;
            while (!(I2C1->SR1 & I2C_SR1_BTF)) { }
            I2C1->CR1 |= I2C_CR1_STOP;
            buf[i++] = (char)I2C1->DR;
            buf[i] = (char)I2C1->DR;
            break;
        }
        while (!(I2C1->SR1 & I2C_SR1_RXNE)) { }
        buf[i] = (char)I2C1->DR;
    }
    buf[n] = 0;
    while (I2C1->SR2 & I2C_SR2_BUSY) { }

    puts1("EEPROM: ");
    puts1(buf);
    puts1("\r\npolls: ");
    puthex(polls);
    puts1("\r\n");
    for (;;) { }
}
