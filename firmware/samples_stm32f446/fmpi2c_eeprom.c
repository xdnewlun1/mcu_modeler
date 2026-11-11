/* STM32F446RE FMPI2C1 (RM0390 ch. 23) as controller in Fast-mode Plus against a 24C02 EEPROM at 0x50
 * (boards/stm32f446re_fmpi2c_eeprom.yaml): PC6 FMPI2C1_SCL / PC7 FMPI2C1_SDA (AF4, open drain).
 * Clock: HSI 16 MHz after reset; the FMPI2C1 kernel clock is selected as HSI (RCC_DCKCFGR2.FMPI2C1SEL = 10) and
 * TIMINGR = 0x00200204 is the RM0390 Table 135 Fm+ (1 MHz) setting for fI2CCLK = 16 MHz (PRESC 0, SCLDEL 2,
 * SDADEL 0, SCLH 2, SCLL 4). SYSCFG_CFGR FMPI2C1_SCL/SDA select the Fm+ 20 mA drive (RM0390 8.2.8).
 * Flow (RM0390 23.4.9): page writes (NBYTES = 1 + data, AUTOEND, TXIS per byte), acknowledge polling of the write
 * cycle (NBYTES = 0 + AUTOEND: the EEPROM NACKs its address while busy -> NACKF + automatic STOP), then a random
 * read: dummy write of the word address in software end mode (TC), repeated START with RD_WRN = 1, NBYTES = n,
 * AUTOEND (the last byte is NACKed by hardware). Prints "EEPROM: Hello FMPI2C!" and the number of polls on USART2
 * (PA2, 115200 8N1). */
#include "stm32f446.h"

#define EE 0x50u
static const char msg[] = "Hello FMPI2C!";
volatile unsigned polls;

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

static void dec2(unsigned v)
{
    char b[12];
    int n = 0;
    do { b[n++] = (char)('0' + v % 10u); v /= 10u; } while (v);
    while (n)
        putc2(b[--n]);
}

/* START + address + NBYTES (write), AUTOEND; returns 0 if the address or a byte was NACKed */
static int write_bytes(const uint8_t *p, unsigned n)
{
    FMPI2C1->CR2 = FMPI2C_CR2_SADD7(EE) | FMPI2C_CR2_NBYTES(n) | FMPI2C_CR2_AUTOEND | FMPI2C_CR2_START;
    for (;;) {
        uint32_t s = FMPI2C1->ISR;
        if (s & FMPI2C_ISR_NACKF) {
            while (!(FMPI2C1->ISR & FMPI2C_ISR_STOPF)) { }      /* automatic STOP after the NACK */
            FMPI2C1->ICR = FMPI2C_ICR_NACKCF | FMPI2C_ICR_STOPCF;
            FMPI2C1->ISR = FMPI2C_ISR_TXE;                       /* flush a byte loaded for the next TXIS */
            return 0;
        }
        if (s & FMPI2C_ISR_STOPF) {
            FMPI2C1->ICR = FMPI2C_ICR_STOPCF;
            return 1;
        }
        if (s & FMPI2C_ISR_TXIS)
            FMPI2C1->TXDR = *p++;
    }
}

int main(void)
{
    char buf[sizeof msg];
    unsigned n = sizeof msg - 1, i;

    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN | RCC_AHB1ENR_GPIOCEN;
    RCC_APB1ENR |= RCC_APB1ENR_USART2EN | RCC_APB1ENR_FMPI2C1EN;
    RCC_APB2ENR |= RCC_APB2ENR_SYSCFGEN;
    gpio_af(GPIOA, 2, 7);
    gpio_af(GPIOA, 3, 7);
    USART2->BRR = USART_BRR(HSI_HZ, 115200u);
    USART2->CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;

    GPIOC->OTYPER |= (1u << 6) | (1u << 7);                     /* open drain */
    gpio_af(GPIOC, 6, 4);
    gpio_af(GPIOC, 7, 4);
    SYSCFG_CFGR |= 3u;                                          /* Fm+ drive on FMPI2C1_SCL / SDA */
    RCC_DCKCFGR2 = (RCC_DCKCFGR2 & ~(3u << 22)) | RCC_DCKCFGR2_FMPI2C1SEL_HSI;

    FMPI2C1->CR1 = 0;                                           /* RM0390 Figure 244: configure with PE = 0 */
    FMPI2C1->TIMINGR = 0x00200204u;
    FMPI2C1->CR1 = FMPI2C_CR1_PE;

    polls = 0;
    for (i = 0; i < n; ) {
        uint8_t frame[9];
        unsigned a = 0x10 + i, k = 8 - (a & 7), j;
        if (k > n - i)
            k = n - i;
        frame[0] = (uint8_t)a;
        for (j = 0; j < k; j++)
            frame[1 + j] = (uint8_t)msg[i + j];
        if (!write_bytes(frame, 1 + k)) {
            puts2("EEPROM: no ACK\r\n");
            for (;;) { }
        }
        i += k;
        while (!write_bytes(0, 0))                              /* acknowledge polling */
            polls++;
    }

    /* random read: dummy write (software end -> TC), repeated START for reading */
    FMPI2C1->CR2 = FMPI2C_CR2_SADD7(EE) | FMPI2C_CR2_NBYTES(1) | FMPI2C_CR2_START;
    while (!(FMPI2C1->ISR & FMPI2C_ISR_TXIS)) { }
    FMPI2C1->TXDR = 0x10;
    while (!(FMPI2C1->ISR & FMPI2C_ISR_TC)) { }
    FMPI2C1->CR2 = FMPI2C_CR2_SADD7(EE) | FMPI2C_CR2_RD_WRN | FMPI2C_CR2_NBYTES(n) | FMPI2C_CR2_AUTOEND |
                   FMPI2C_CR2_START;
    for (i = 0; i < n; i++) {
        while (!(FMPI2C1->ISR & FMPI2C_ISR_RXNE)) { }
        buf[i] = (char)FMPI2C1->RXDR;
    }
    buf[n] = 0;
    while (!(FMPI2C1->ISR & FMPI2C_ISR_STOPF)) { }
    FMPI2C1->ICR = FMPI2C_ICR_STOPCF;

    puts2("EEPROM: ");
    puts2(buf);
    puts2("\r\npolls: ");
    dec2(polls);
    puts2("\r\n");
    for (;;) { }
}
