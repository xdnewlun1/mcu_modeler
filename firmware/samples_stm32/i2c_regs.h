/* I2C1 register map (RM0090 ch. 27) for the STM32 I2C samples. */
#ifndef I2C_REGS_H
#define I2C_REGS_H
#include "stm32f405.h"

typedef struct {
    volatile uint32_t CR1, CR2, OAR1, OAR2, DR, SR1, SR2, CCR, TRISE;
} I2C_TypeDef;
#define I2C1 ((I2C_TypeDef *)0x40005400u)
#define RCC_APB1ENR_I2C1EN (1u << 21)
#define I2C_CR1_PE    (1u << 0)
#define I2C_CR1_START (1u << 8)
#define I2C_CR1_STOP  (1u << 9)
#define I2C_CR1_ACK   (1u << 10)
#define I2C_CR1_POS   (1u << 11)
#define I2C_CR1_SWRST (1u << 15)
#define I2C_CR2_ITERREN (1u << 8)
#define I2C_CR2_ITEVTEN (1u << 9)
#define I2C_CR2_ITBUFEN (1u << 10)
#define I2C_SR1_SB    (1u << 0)
#define I2C_SR1_ADDR  (1u << 1)
#define I2C_SR1_BTF   (1u << 2)
#define I2C_SR1_STOPF (1u << 4)
#define I2C_SR1_RXNE  (1u << 6)
#define I2C_SR1_TXE   (1u << 7)
#define I2C_SR1_AF    (1u << 10)
#define I2C_SR2_BUSY  (1u << 1)
#define I2C_SR2_TRA   (1u << 2)
#define I2C1_EV_IRQn  31

/* PB6 = I2C1_SCL, PB7 = I2C1_SDA: AF4, open-drain, pull-up */
static inline void i2c1_pins(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOBEN;
    GPIOB->OTYPER |= (1u << 6) | (1u << 7);
    gpio_pull(GPIOB, 6, GPIO_PULL_UP);
    gpio_pull(GPIOB, 7, GPIO_PULL_UP);
    gpio_af(GPIOB, 6, 4);
    gpio_af(GPIOB, 7, 4);
}

static void uart_init(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN;
    RCC_APB2ENR |= RCC_APB2ENR_USART1EN;
    gpio_af(GPIOA, 9, 7);
    gpio_af(GPIOA, 10, 7);
    USART1->BRR = USART_BRR(HSI_HZ, 115200u);
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;
}

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

static void puthex(unsigned v)
{
    static const char h[] = "0123456789ABCDEF";
    putc1(h[(v >> 4) & 15]);
    putc1(h[v & 15]);
}
#endif
