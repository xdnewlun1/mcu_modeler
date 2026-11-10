/* spi_master: SPI1 master for the boards/stm32_spi_pair.yaml board (the peer runs spi_slave).
 * 16 MHz HSI, SPI1 PA5 SCK / PA6 MISO / PA7 MOSI / PA4 NSS (hardware output, CR2.SSOE), AF5, mode 0, MSB first,
 * PCLK2/128 = 125 kHz. Every 2 ms: SPE = 1 (NSS low), sends "hello" byte by byte while receiving the slave's
 * answer, waits for BSY = 0, SPE = 0 (NSS high) and prints "M <n> WORLD\r\n" over USART1 (PA9, 115200 8N1).
 * PB0 toggles per transaction. */
#include "stm32f405.h"

typedef struct {
    volatile uint32_t CR1, CR2, SR, DR, CRCPR, RXCRCR, TXCRCR, I2SCFGR, I2SPR;
} SPI_TypeDef;
#define SPI1 ((SPI_TypeDef *)0x40013000u)
#define RCC_APB2ENR_SPI1EN (1u << 12)

volatile uint32_t transactions;
volatile char last_rx[6];

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

static void delay_ms(unsigned ms)
{
    SYST_RVR = HSI_HZ / 1000u - 1u;
    SYST_CVR = 0;
    SYST_CSR = SYST_CSR_ENABLE | SYST_CSR_CLKSOURCE;
    while (ms--)
        while (!(SYST_CSR & SYST_CSR_COUNTFLAG)) { }
    SYST_CSR = 0;
}

int main(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN | RCC_AHB1ENR_GPIOBEN;
    RCC_APB2ENR |= RCC_APB2ENR_USART1EN | RCC_APB2ENR_SPI1EN;
    gpio_af(GPIOA, 9, 7);
    USART1->BRR = USART_BRR(HSI_HZ, 115200u);
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE;
    gpio_mode(GPIOB, 0, GPIO_MODE_OUTPUT);
    for (unsigned p = 4; p <= 7; p++)
        gpio_af(GPIOA, p, 5);
    SPI1->CR2 = 1u << 2;                                   /* SSOE: NSS is an output */
    SPI1->CR1 = (1u << 2) | (6u << 3);                     /* MSTR, BR = /128 */

    delay_ms(1);                                           /* the slave configures its SPI */
    for (;;) {
        const char *msg = "hello";
        char rx[6];
        SPI1->CR1 |= 1u << 6;                              /* SPE: NSS low */
        for (unsigned i = 0; i < 5; i++) {
            while (!(SPI1->SR & 2u)) { }
            *(volatile uint8_t *)&SPI1->DR = (uint8_t)msg[i];
            while (!(SPI1->SR & 1u)) { }
            rx[i] = (char)*(volatile uint8_t *)&SPI1->DR;
        }
        while (SPI1->SR & (1u << 7)) { }                   /* BSY */
        SPI1->CR1 &= ~(1u << 6);                           /* NSS high */
        rx[5] = 0;
        for (unsigned i = 0; i < 6; i++)
            last_rx[i] = rx[i];
        transactions++;
        GPIOB->ODR ^= 1u;
        puts1("M ");
        putc1((char)('0' + transactions % 10));
        putc1(' ');
        puts1(rx);
        puts1("\r\n");
        delay_ms(2);
    }
}
