/* i2c_slave: I2C1 slave at address 0x42 (PB6 SCL / PB7 SDA, AF4 open-drain) exposing a 16-byte register map with
 * the usual pointer convention: the first byte of a write sets the register pointer, the following bytes are
 * stored at the pointer (auto-increment, wrapping at 16); a read returns registers from the pointer.  Interrupt
 * driven (I2C1_EV, ITEVTEN | ITBUFEN, clock stretching while software services ADDR/RXNE/TXE).  Registers start as
 * 0xA0..0xAF.  After each write transfer (on STOPF) it prints "SLAVE RX <ptr>:<hex bytes>\r\n" on USART1 (PA9,
 * 115200), e.g. "SLAVE RX 04:4B32", and after a read (ended by the master's NACK: AF) "SLAVE TX <n>\r\n" (n = bytes
 * written to DR, hex: the ISR fills DR one byte ahead, so n = bytes sent + 1). */
#include "i2c_regs.h"

#define OWN 0x42u
static volatile unsigned char regs[16];
static volatile unsigned char ptr, first, rxn, txn, start_ptr, report;
static volatile unsigned char rxbuf[16];

void I2C1_EV_IRQHandler(void)
{
    unsigned s = I2C1->SR1;
    if (s & I2C_SR1_ADDR) {
        unsigned s2 = I2C1->SR2;                      /* SR1 + SR2 read clears ADDR */
        if (s2 & I2C_SR2_TRA) {
            txn = 0;
        } else {
            first = 1;
            rxn = 0;
        }
        s = I2C1->SR1;
    }
    if (s & I2C_SR1_RXNE) {
        unsigned char b = (unsigned char)I2C1->DR;
        if (first) {
            ptr = b & 15;
            start_ptr = ptr;
            first = 0;
        } else {
            regs[ptr] = b;
            if (rxn < 16)
                rxbuf[rxn++] = b;
            ptr = (ptr + 1) & 15;
        }
    }
    if (s & I2C_SR1_TXE && I2C1->SR2 & I2C_SR2_TRA) {
        I2C1->DR = regs[ptr];
        ptr = (ptr + 1) & 15;
        txn++;
    }
    if (s & I2C_SR1_STOPF) {
        I2C1->CR1 |= I2C_CR1_PE;                      /* SR1 read + CR1 write clears STOPF */
        report = 1;
    }
}

void I2C1_ER_IRQHandler(void)
{
    if (I2C1->SR1 & I2C_SR1_AF) {                     /* master NACKed our last byte: end of a read */
        I2C1->SR1 = ~I2C_SR1_AF;
        report = 2;
    }
}

int main(void)
{
    unsigned i;
    for (i = 0; i < 16; i++)
        regs[i] = (unsigned char)(0xA0 + i);
    uart_init();
    i2c1_pins();
    RCC_APB1ENR |= RCC_APB1ENR_I2C1EN;
    I2C1->CR1 = I2C_CR1_SWRST;
    I2C1->CR1 = 0;
    I2C1->CR2 = 16 | I2C_CR2_ITEVTEN | I2C_CR2_ITBUFEN | I2C_CR2_ITERREN;
    I2C1->CCR = 80;
    I2C1->TRISE = 17;
    I2C1->OAR1 = (1u << 14) | (OWN << 1);
    I2C1->CR1 = I2C_CR1_PE | I2C_CR1_ACK;
    NVIC_EnableIRQ(I2C1_EV_IRQn);
    NVIC_EnableIRQ(I2C1_EV_IRQn + 1);
    __enable_irq();
    puts1("SLAVE READY\r\n");
    for (;;) {
        unsigned char r = report;                     /* read once: the ISR may set it at any time */
        if (r == 1 && rxn) {
            unsigned char n = rxn, p0 = start_ptr, copy[16];
            for (i = 0; i < n; i++)                   /* snapshot: the next transfer may start while we print */
                copy[i] = rxbuf[i];
            rxn = 0;
            report = 0;
            puts1("SLAVE RX ");
            puthex(p0);
            putc1(':');
            for (i = 0; i < n; i++)
                puthex(copy[i]);
            puts1("\r\n");
        } else if (r == 2) {
            report = 0;
            puts1("SLAVE TX ");
            puthex(txn);
            puts1("\r\n");
        } else if (r == 1) {
            report = 0;
        }
    }
}
