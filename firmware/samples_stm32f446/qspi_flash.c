/* STM32F446: 180 MHz (SYSCLK = PLL_R, over-drive), USART2 115200 (PA2 TX / PA3 RX, AF7: the NUCLEO virtual COM port)
 * and a W25Q128-style quad-SPI NOR flash on the QUADSPI (board device, chipem/devices/qspi_flash.py):
 *   JEDEC ID (indirect read 0x9F), WREN + 4 KB sector erase (0x20) with automatic status polling (0x05, WIP = 0, APMS,
 *   SMF interrupt + WFI) timed with TIM2 at 1 MHz, page program, indirect read, the same read through DMA2 stream 7
 *   channel 3, and a memory-mapped read of the 0x90000000 window.
 * QUAD = 0 (qspi_flash.c, STM32F446RE LQFP64): the 64-pin package has no QUADSPI_BK1_IO2 pin (ST open pin data), so
 *   this build uses dual I/O: page program 0x02 (1-1-1), Fast Read Dual I/O 0xBB (1-2-2, M7-0 = 0 on 2 lines = 4
 *   clocks) and memory-mapped Fast Read Dual Output 0x3B (1-1-2, 8 dummy).  Pins: CLK PB2 AF9, BK1_NCS PB6 AF10,
 *   IO0 PC9 AF9, IO1 PC10 AF9.
 * QUAD = 1 (qspi_quad.c, STM32F446ZE LQFP144): QE set with 0x31 (status register 2), Quad Page Program 0x32 (1-1-4),
 *   Fast Read Quad I/O 0xEB (1-4-4, M7-0 on 4 lines = 2 clocks + 4 dummy) and memory-mapped 0x6B (1-1-4, 8 dummy).
 *   Pins: CLK PB2 AF9, BK1_NCS PB6 AF10, IO0 PF8 AF10, IO1 PF9 AF10, IO2 PF7 AF9, IO3 PF6 AF9.
 * Register usage follows the STM32CubeF4 HAL QSPI driver: a command with data starts on the AR write (read) or the
 * first DR write (write), a command without data on the AR or CCR write. */
#include "stm32f446.h"

#ifndef QUAD
#define QUAD 0
#endif

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

static void hex2(unsigned v, int digits)
{
    for (int i = digits - 1; i >= 0; i--)
        putc2("0123456789ABCDEF"[(v >> (4 * i)) & 15u]);
}

static void dec2(unsigned v)
{
    char b[12];
    int n = 0;
    do { b[n++] = (char)('0' + v % 10u); v /= 10u; } while (v);
    while (n)
        putc2(b[--n]);
}

#define DR8 (*(volatile uint8_t *)&QUADSPI->DR)

static volatile unsigned qspi_irqs;
static volatile int status_match;

void QUADSPI_IRQHandler(void)
{
    if (QUADSPI->SR & QSPI_SR_SMF) {
        QUADSPI->FCR = QSPI_FCR_CSMF;
        status_match = 1;
    }
    qspi_irqs++;
}

static void wait_tc(void)
{
    while (!(QUADSPI->SR & QSPI_SR_TCF)) { }
    QUADSPI->FCR = QSPI_FCR_CTCF;
}

static void idle(void)
{
    while (QUADSPI->SR & QSPI_SR_BUSY) { }
}

static void cmd(uint8_t instr)                  /* instruction only: starts on the CCR write */
{
    idle();
    QUADSPI->CCR = CCR_INSTR(instr) | CCR_IMODE(1) | CCR_FMODE(FMODE_WRITE);
    wait_tc();
}

static void wait_ready(void)                    /* automatic polling of RDSR until WIP = 0, SMF interrupt */
{
    idle();
    status_match = 0;
    QUADSPI->DLR = 0;
    QUADSPI->PSMKR = 0x01;
    QUADSPI->PSMAR = 0x00;
    QUADSPI->PIR = 0x10;
    QUADSPI->CR |= QSPI_CR_APMS | QSPI_CR_SMIE;
    QUADSPI->CCR = CCR_INSTR(0x05) | CCR_IMODE(1) | CCR_DMODE(1) | CCR_FMODE(FMODE_POLL);
    while (!status_match)
        __WFI();
    QUADSPI->CR &= ~QSPI_CR_SMIE;
    idle();
}

static void write_data(uint32_t ccr, uint32_t addr, const uint8_t *p, unsigned n)
{
    idle();
    QUADSPI->DLR = n - 1u;
    QUADSPI->CCR = ccr | CCR_FMODE(FMODE_WRITE);
    if (ccr & CCR_ADMODE(3))
        QUADSPI->AR = addr;
    for (unsigned i = 0; i < n; i++) {
        while (!(QUADSPI->SR & QSPI_SR_FTF)) { }
        DR8 = p[i];
    }
    wait_tc();
}

#if QUAD
#define READ_CCR (CCR_INSTR(0xEB) | CCR_IMODE(1) | CCR_ADMODE(3) | CCR_ADSIZE(2) | CCR_ABMODE(3) | CCR_ABSIZE(0) | \
                  CCR_DCYC(4) | CCR_DMODE(3))
#define MM_CCR   (CCR_INSTR(0x6B) | CCR_IMODE(1) | CCR_ADMODE(1) | CCR_ADSIZE(2) | CCR_DCYC(8) | CCR_DMODE(3))
#define PROG_CCR (CCR_INSTR(0x32) | CCR_IMODE(1) | CCR_ADMODE(1) | CCR_ADSIZE(2) | CCR_DMODE(3))
#else
#define READ_CCR (CCR_INSTR(0xBB) | CCR_IMODE(1) | CCR_ADMODE(2) | CCR_ADSIZE(2) | CCR_ABMODE(2) | CCR_ABSIZE(0) | \
                  CCR_DMODE(2))
#define MM_CCR   (CCR_INSTR(0x3B) | CCR_IMODE(1) | CCR_ADMODE(1) | CCR_ADSIZE(2) | CCR_DCYC(8) | CCR_DMODE(2))
#define PROG_CCR (CCR_INSTR(0x02) | CCR_IMODE(1) | CCR_ADMODE(1) | CCR_ADSIZE(2) | CCR_DMODE(1))
#endif

static void read_data(uint32_t addr, uint8_t *p, unsigned n)
{
    idle();
    QUADSPI->DLR = n - 1u;
    QUADSPI->ABR = 0;                           /* M7-0 = 0x00: no continuous read mode */
    QUADSPI->CCR = READ_CCR | CCR_FMODE(FMODE_READ);
    QUADSPI->AR = addr;
    for (unsigned i = 0; i < n; i++) {
        while (!(QUADSPI->SR & (QSPI_SR_FTF | QSPI_SR_TCF))) { }
        p[i] = DR8;
    }
    wait_tc();
}

static void read_dma(uint32_t addr, uint8_t *p, unsigned n)
{
    idle();
    DMA2_Stream7->CR = 0;
    while (DMA2_Stream7->CR & DMA_SxCR_EN) { }
    DMA2_HIFCR = 0x0F400000u;                   /* clear stream 7 flags */
    DMA2_Stream7->PAR = (uint32_t)&QUADSPI->DR;
    DMA2_Stream7->M0AR = (uint32_t)p;
    DMA2_Stream7->NDTR = n;
    DMA2_Stream7->CR = DMA_SxCR_CHSEL(3) | DMA_SxCR_MINC;     /* peripheral to memory, bytes */
    DMA2_Stream7->CR |= DMA_SxCR_EN;
    QUADSPI->DLR = n - 1u;
    QUADSPI->ABR = 0;
    QUADSPI->CCR = READ_CCR | CCR_FMODE(FMODE_READ);
    QUADSPI->CR |= QSPI_CR_DMAEN;
    QUADSPI->AR = addr;
    while (!(DMA2_HISR & DMA_HISR_TCIF7)) { }
    wait_tc();
    QUADSPI->CR &= ~QSPI_CR_DMAEN;
}

static const char msg[] = "QSPI on F446 ok!";

int main(void)
{
    clock_180mhz_pllr();
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN | RCC_AHB1ENR_GPIOBEN | RCC_AHB1ENR_GPIOCEN | RCC_AHB1ENR_GPIOFEN |
                   RCC_AHB1ENR_DMA2EN;
    RCC_APB1ENR |= RCC_APB1ENR_USART2EN | RCC_APB1ENR_TIM2EN;
    RCC_AHB3ENR |= RCC_AHB3ENR_QSPIEN;
    gpio_mode(GPIOA, 5, GPIO_MODE_OUTPUT);      /* LD2 */
    gpio_af(GPIOA, 2, 7);
    gpio_af(GPIOA, 3, 7);
    gpio_pull(GPIOA, 3, GPIO_PULL_UP);
    USART2->BRR = USART_BRR(45000000u, 115200u);
    USART2->CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;
    puts2("F446 180MHz PLLR\r\n");

    gpio_af(GPIOB, 2, 9);                       /* QUADSPI_CLK */
    gpio_af(GPIOB, 6, 10);                      /* QUADSPI_BK1_NCS */
#if QUAD
    gpio_af(GPIOF, 8, 10);                      /* IO0 */
    gpio_af(GPIOF, 9, 10);                      /* IO1 */
    gpio_af(GPIOF, 7, 9);                       /* IO2 */
    gpio_af(GPIOF, 6, 9);                       /* IO3 */
#else
    gpio_af(GPIOC, 9, 9);                       /* IO0 */
    gpio_af(GPIOC, 10, 9);                      /* IO1 */
#endif
    TIM2->PSC = 90u - 1u;                       /* APB1 timer clock 90 MHz -> 1 MHz */
    TIM2->ARR = 0xFFFFFFFFu;
    TIM2->EGR = TIM_EGR_UG;
    TIM2->CR1 = TIM_CR1_CEN;
    NVIC_EnableIRQ(QUADSPI_IRQn);
    __enable_irq();

    /* QSPI clock = HCLK / 2 = 90 MHz, FIFO threshold 4, 16 MB flash, nCS high >= 2 clocks */
    QUADSPI->CR = QSPI_CR_PRESCALER(1) | QSPI_CR_FTHRES(4) | QSPI_CR_EN;
    QUADSPI->DCR = QSPI_DCR_FSIZE(23) | QSPI_DCR_CSHT(1);

    uint8_t id[3];
    QUADSPI->DLR = 2;
    QUADSPI->CCR = CCR_INSTR(0x9F) | CCR_IMODE(1) | CCR_DMODE(1) | CCR_FMODE(FMODE_READ);
    for (int i = 0; i < 3; i++) {
        while (!(QUADSPI->SR & (QSPI_SR_FTF | QSPI_SR_TCF))) { }
        id[i] = DR8;
    }
    wait_tc();
    puts2("ID ");
    hex2(((unsigned)id[0] << 16) | ((unsigned)id[1] << 8) | id[2], 6);
    puts2("\r\n");

#if QUAD
    cmd(0x06);                                  /* WREN, then status register 2 = QE */
    {
        uint8_t qe = 0x02;
        write_data(CCR_INSTR(0x31) | CCR_IMODE(1) | CCR_DMODE(1), 0, &qe, 1);
    }
    wait_ready();
#endif

    cmd(0x06);
    idle();
    QUADSPI->CCR = CCR_INSTR(0x20) | CCR_IMODE(1) | CCR_ADMODE(1) | CCR_ADSIZE(2) | CCR_FMODE(FMODE_WRITE);
    QUADSPI->AR = 0x001000;                     /* sector 1: starts the command */
    wait_tc();
    TIM2->CNT = 0;
    wait_ready();
    unsigned erase_us = TIM2->CNT;
    puts2("erase ");
    dec2(erase_us);
    puts2("us\r\n");

    cmd(0x06);
    write_data(PROG_CCR, 0x001000, (const uint8_t *)msg, sizeof msg - 1);
    TIM2->CNT = 0;
    wait_ready();
    unsigned prog_us = TIM2->CNT;
    puts2("prog ");
    dec2(prog_us);
    puts2("us\r\n");

    uint8_t buf[sizeof msg];
    read_data(0x001000, buf, sizeof msg - 1);
    buf[sizeof msg - 1] = 0;
    puts2("read: ");
    puts2((const char *)buf);
    puts2("\r\n");

    for (unsigned i = 0; i < sizeof buf; i++)
        buf[i] = 0;
    read_dma(0x001000, buf, sizeof msg - 1);
    puts2("dma: ");
    puts2((const char *)buf);
    puts2("\r\n");

    idle();
    QUADSPI->CCR = MM_CCR | CCR_FMODE(FMODE_MM);
    for (unsigned i = 0; i < sizeof msg - 1; i++)
        buf[i] = QSPI_MM[0x1000 + i];
    puts2("mm: ");
    puts2((const char *)buf);
    puts2("\r\nirqs ");
    dec2(qspi_irqs);
    puts2("\r\ndone\r\n");
    GPIOA->ODR |= 1u << 5;
    for (;;)
        __WFI();
}
