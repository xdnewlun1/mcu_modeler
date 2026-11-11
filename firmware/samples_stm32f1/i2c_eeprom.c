/* STM32F103T6 i2c_eeprom: I2C1 master on PB6 SCL / PB7 SDA (AF open-drain; default AFIO mapping), 100 kHz standard mode
 * from PCLK1 = 8 MHz (HSI): FREQ = 8, CCR = 40, TRISE = 9.  Writes "Hello F103!" to a 24C02 at 0x50 (address 0x10,
 * 8-byte page writes + acknowledge polling), reads it back (dummy write, repeated START, N > 2 byte read sequence) and
 * prints "EEPROM: Hello F103!" and the poll count on USART1 (PA9, 115200).  Board: boards/stm32f103_i2c_eeprom.yaml. */
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
static const char msg[] = "Hello F103!";
volatile unsigned polls, done;

static int i2c_start(unsigned addr_rw)
{
    I2C_CR1(I2C1_BASE) |= I2C_CR1_START;
    while (!(I2C_SR1(I2C1_BASE) & I2C_SR1_SB))
        ;
    I2C_DR(I2C1_BASE) = addr_rw;
    for (;;) {
        unsigned s = I2C_SR1(I2C1_BASE);
        if (s & I2C_SR1_ADDR)
            return 1;
        if (s & I2C_SR1_AF) {
            I2C_SR1(I2C1_BASE) = ~I2C_SR1_AF;
            return 0;
        }
    }
}

static void i2c_stop(void)
{
    I2C_CR1(I2C1_BASE) |= I2C_CR1_STOP;
    while (I2C_SR2(I2C1_BASE) & I2C_SR2_BUSY)
        ;
}

static void clear_addr(void)
{
    (void)I2C_SR1(I2C1_BASE);
    (void)I2C_SR2(I2C1_BASE);
}

static void i2c_write(unsigned char b)
{
    while (!(I2C_SR1(I2C1_BASE) & I2C_SR1_TXE))
        ;
    I2C_DR(I2C1_BASE) = b;
}

int main(void)
{
    char buf[sizeof msg];
    unsigned n = sizeof msg - 1, i;

    console_init(8000000u, 115200u);
    RCC_APB2ENR |= RCC_APB2ENR_IOPBEN;
    RCC_APB1ENR |= RCC_APB1ENR_I2C1EN;
    GPIO_CONFIG(GPIOB_BASE, 6, GPIO_AF_OD_50MHZ);
    GPIO_CONFIG(GPIOB_BASE, 7, GPIO_AF_OD_50MHZ);
    I2C_CR1(I2C1_BASE) = I2C_CR1_SWRST;
    I2C_CR1(I2C1_BASE) = 0;
    I2C_CR2(I2C1_BASE) = 8;
    I2C_CCR(I2C1_BASE) = 40;
    I2C_TRISE(I2C1_BASE) = 9;
    I2C_CR1(I2C1_BASE) = I2C_CR1_PE;

    for (i = 0; i < n;) {
        unsigned a = 0x10 + i, k = 8 - (a & 7);
        if (k > n - i)
            k = n - i;
        if (!i2c_start(EE << 1)) {
            i2c_stop();
            puts_("EEPROM: no ACK\r\n");
            for (;;)
                ;
        }
        clear_addr();
        i2c_write((unsigned char)a);
        while (k--)
            i2c_write((unsigned char)msg[i++]);
        while (!(I2C_SR1(I2C1_BASE) & I2C_SR1_BTF))
            ;
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
    i2c_write(0x10);
    while (!(I2C_SR1(I2C1_BASE) & I2C_SR1_TXE))
        ;
    if (!i2c_start((EE << 1) | 1)) {
        i2c_stop();
        puts_("EEPROM: read NACK\r\n");
        for (;;)
            ;
    }
    I2C_CR1(I2C1_BASE) |= I2C_CR1_ACK;
    clear_addr();
    for (i = 0; i < n; i++) {
        if (n - i == 3) {
            while (!(I2C_SR1(I2C1_BASE) & I2C_SR1_BTF))
                ;
            I2C_CR1(I2C1_BASE) &= ~I2C_CR1_ACK;
            buf[i++] = (char)I2C_DR(I2C1_BASE);
            while (!(I2C_SR1(I2C1_BASE) & I2C_SR1_BTF))
                ;
            I2C_CR1(I2C1_BASE) |= I2C_CR1_STOP;
            buf[i++] = (char)I2C_DR(I2C1_BASE);
            buf[i] = (char)I2C_DR(I2C1_BASE);
            break;
        }
        while (!(I2C_SR1(I2C1_BASE) & I2C_SR1_RXNE))
            ;
        buf[i] = (char)I2C_DR(I2C1_BASE);
    }
    buf[n] = 0;
    while (I2C_SR2(I2C1_BASE) & I2C_SR2_BUSY)
        ;
    puts_("EEPROM: ");
    puts_(buf);
    puts_("\r\npolls: ");
    putdec(polls);
    puts_("\r\n");
    done = 1;
    for (;;)
        __asm__ volatile("wfi");
}
