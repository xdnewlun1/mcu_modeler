/* spi_slave: SPI1 slave for the boards/stm32_spi_pair.yaml board (the peer runs spi_master).
 * 16 MHz HSI, SPI1 PA5 SCK / PA6 MISO / PA7 MOSI / PA4 NSS (hardware input, SSM = 0), AF5, mode 0.
 * Answers every 5-byte transaction with "WORLD": the TX buffer is refilled whenever TXE is set (the shift register
 * takes the next byte as soon as a frame starts). After 5 received bytes it prints "S <n> hello\r\n" over USART1
 * (PA9, 115200 8N1). PB0 toggles per transaction. */
#include "stm32f405.h"

typedef struct {
    volatile uint32_t CR1, CR2, SR, DR, CRCPR, RXCRCR, TXCRCR, I2SCFGR, I2SPR;
} SPI_TypeDef;
#define SPI1 ((SPI_TypeDef *)0x40013000u)
#define RCC_APB2ENR_SPI1EN (1u << 12)

volatile uint32_t transactions;

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

int main(void)
{
    static const char reply[] = "WORLD";
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN | RCC_AHB1ENR_GPIOBEN;
    RCC_APB2ENR |= RCC_APB2ENR_USART1EN | RCC_APB2ENR_SPI1EN;
    gpio_af(GPIOA, 9, 7);
    USART1->BRR = USART_BRR(HSI_HZ, 115200u);
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE;
    gpio_mode(GPIOB, 0, GPIO_MODE_OUTPUT);
    for (unsigned p = 4; p <= 7; p++)
        gpio_af(GPIOA, p, 5);
    SPI1->CR1 = 1u << 6;                                   /* slave, mode 0, SPE */

    unsigned ti = 0, ri = 0;
    char rx[6];
    for (;;) {
        uint32_t sr = SPI1->SR;
        if (sr & 2u) {                                     /* TXE: queue the next reply byte */
            *(volatile uint8_t *)&SPI1->DR = (uint8_t)reply[ti];
            ti = (ti + 1) % 5;
        }
        if (sr & 1u) {
            rx[ri++] = (char)*(volatile uint8_t *)&SPI1->DR;
            if (ri == 5) {
                rx[5] = 0;
                ri = 0;
                transactions++;
                GPIOB->ODR ^= 1u;
                puts1("S ");
                putc1((char)('0' + transactions % 10));
                putc1(' ');
                puts1(rx);
                puts1("\r\n");
            }
        }
    }
}
