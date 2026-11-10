/* spi_master: K22 DSPI0 master for boards/k22_stm32_spi.yaml (the STM32 peer runs samples_stm32 spi_slave).
 *
 * SPI0 on PTC4 = SPI0_PCS0, PTC5 = SPI0_SCK, PTC6 = SPI0_SOUT, PTC7 = SPI0_SIN (ALT2), K22 RM ch. 47.
 * Bus clock 60 MHz (120 MHz core, SIM_CLKDIV1 OUTDIV2 = /2). CTAR0: 8-bit frames, mode 0 (CPOL = 0,
 * CPHA = 0), MSB first, PBR = 2 and BR = 256 -> SCK 60 MHz / 512 = 117.2 kHz; tCSC = tASC = 256 bus clocks
 * (4.27 us), tDT = 512 (8.53 us). PCS0 is active low (MCR.PCSIS0 = 1).
 * Every 2 ms (SysTick) it sends "hello" as one transaction: the first four PUSHR commands have CONT = 1, so PCS0
 * stays low between the bytes; the last one has CONT = 0. It reads the slave's answer from POPR and prints
 * "M <n> WORLD\r\n" on UART0 (PTA2, 115200 8N1, core clock). */
#include "k22.h"

#define UART_CLK_HZ CORE_CLK_HZ
#define BAUD        115200u

#define SPI0_BASE   0x4002C000u
#define SPI0_MCR    REG32(SPI0_BASE + 0x00)
#define SPI0_TCR    REG32(SPI0_BASE + 0x08)
#define SPI0_CTAR0  REG32(SPI0_BASE + 0x0C)
#define SPI0_SR     REG32(SPI0_BASE + 0x2C)
#define SPI0_RSER   REG32(SPI0_BASE + 0x30)
#define SPI0_PUSHR  REG32(SPI0_BASE + 0x34)
#define SPI0_POPR   REG32(SPI0_BASE + 0x38)
#define SPI_MCR_MSTR      (1u << 31)
#define SPI_MCR_PCSIS(x)  ((uint32_t)(x) << 16)
#define SPI_MCR_CLR_TXF   (1u << 11)
#define SPI_MCR_CLR_RXF   (1u << 10)
#define SPI_MCR_HALT      (1u << 0)
#define SPI_CTAR_FMSZ(x)  ((uint32_t)(x) << 27)
#define SPI_CTAR_CSSCK(x) ((uint32_t)(x) << 12)
#define SPI_CTAR_ASC(x)   ((uint32_t)(x) << 8)
#define SPI_CTAR_DT(x)    ((uint32_t)(x) << 4)
#define SPI_CTAR_BR(x)    ((uint32_t)(x) << 0)
#define SPI_SR_TCF        (1u << 31)
#define SPI_SR_TFFF       (1u << 25)
#define SPI_SR_RFDF       (1u << 17)
#define SPI_PUSHR_CONT    (1u << 31)
#define SPI_PUSHR_PCS(x)  ((uint32_t)(x) << 16)
#define SIM_SCGC6_SPI0    (1u << 12)

volatile uint32_t transactions;
volatile char last_rx[6];

static void putc0(char c)
{
    while (!(UART0_S1 & UART_S1_TDRE))
        ;
    UART0_D = (uint8_t)c;
}

static void puts0(const char *s)
{
    while (*s)
        putc0(*s++);
}

static uint8_t spi_xfer(uint8_t b, int cont)
{
    while (!(SPI0_SR & SPI_SR_TFFF))
        ;
    SPI0_PUSHR = (cont ? SPI_PUSHR_CONT : 0u) | SPI_PUSHR_PCS(1) | b;
    while (!(SPI0_SR & SPI_SR_RFDF))
        ;
    uint8_t r = (uint8_t)SPI0_POPR;
    SPI0_SR = SPI_SR_RFDF | SPI_SR_TCF | SPI_SR_TFFF;          /* w1c */
    return r;
}

int main(void)
{
    WDOG_Disable();
    SIM_CLKDIV1 = (0u << 28) | (1u << 24) | (4u << 16);         /* core /1, bus /2 = 60 MHz, flash /5 */
    SIM_SCGC5 |= SIM_SCGC5_PORTA | SIM_SCGC5_PORTC;
    SIM_SCGC4 |= SIM_SCGC4_UART0;
    SIM_SCGC6 |= SIM_SCGC6_SPI0;
    PORTA->PCR[2] = PORT_PCR_MUX(2);                            /* UART0_TX */
    for (unsigned p = 4; p <= 7; p++)
        PORTC->PCR[p] = PORT_PCR_MUX(2);                        /* SPI0 PCS0 / SCK / SOUT / SIN */

    uint32_t sbr = UART_CLK_HZ / (16u * BAUD);
    uint32_t brfa = ((UART_CLK_HZ * 4u / BAUD + 1u) / 2u - sbr * 32u) & 0x1Fu;
    UART0_C2 = 0;
    UART0_BDH = (uint8_t)((sbr >> 8) & 0x1F);
    UART0_BDL = (uint8_t)(sbr & 0xFF);
    UART0_C4 = (UART0_C4 & ~0x1Fu) | UART_C4_BRFA(brfa);
    UART0_C1 = 0;
    UART0_C2 = UART_C2_TE;

    SPI0_MCR = SPI_MCR_MSTR | SPI_MCR_PCSIS(1) | SPI_MCR_CLR_TXF | SPI_MCR_CLR_RXF | SPI_MCR_HALT;
    SPI0_CTAR0 = SPI_CTAR_FMSZ(7) | SPI_CTAR_CSSCK(7) | SPI_CTAR_ASC(7) | SPI_CTAR_DT(8) | SPI_CTAR_BR(8);
    SPI0_MCR &= ~SPI_MCR_HALT;                                  /* running */

    SYST_RVR = CORE_CLK_HZ / 500u - 1u;                         /* 2 ms */
    SYST_CVR = 0;
    SYST_CSR = SYST_CSR_CLKSOURCE | SYST_CSR_ENABLE;
    for (;;) {
        while (!(SYST_CSR & SYST_CSR_COUNTFLAG))
            ;
        const char *msg = "hello";
        char rx[6];
        for (unsigned i = 0; i < 5; i++)
            rx[i] = (char)spi_xfer((uint8_t)msg[i], i < 4);
        rx[5] = 0;
        for (unsigned i = 0; i < 6; i++)
            last_rx[i] = rx[i];
        transactions++;
        puts0("M ");
        putc0((char)('0' + transactions % 10));
        putc0(' ');
        puts0(rx);
        puts0("\r\n");
    }
}
