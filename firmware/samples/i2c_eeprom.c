/* i2c_eeprom: K22 I2C0 master writes a string into a 24Cxx EEPROM and reads it back (boards/k22_i2c_eeprom.yaml).
 *
 * I2C0: PTB2 = I2C0_SCL, PTB3 = I2C0_SDA (ALT2), open drain (PCR.ODE, required for I2C by K22 RM §3.9.4); the pull-ups
 * are on the board (the EEPROM breakout). Bus clock 60 MHz (SIM_CLKDIV1 OUTDIV2 = /2), F = MULT 0 / ICR 0x2D: SCL
 * divider 640 -> 93.75 kHz. Polled (S.IICIF), K22 RM §48.3: MST 0->1 = START, D write = address/data byte, RSTA =
 * repeated START, D read in receive mode starts the next byte, TXAK = NACK, MST 1->0 = STOP.
 * The write is split at 8-byte page boundaries (24C02); after each page the EEPROM's write cycle (tWR) is polled with
 * address-only transfers until it acknowledges. UART0 (PTA1/PTA2, 115200 8N1, core clock) prints the result.
 * Compile with -DI2C_TARGET=0x42 (i2c_reg_master.c) to talk to any target using the same "first byte = register
 * pointer, auto-increment" convention (the STM32 slave of boards/k22_stm32_i2c.yaml). */
#include "k22.h"

#ifndef I2C_TARGET
#define I2C_TARGET   0x50u
#endif
#ifndef TARGET_NAME
#define TARGET_NAME  "EEPROM"
#endif
#ifndef PAGE_SIZE
#define PAGE_SIZE    8u
#endif
#define WORD_ADDR    0x10u
#define BAUD         115200u

#define I2C0_BASE    0x40066000u
#define I2C0_A1      REG8(I2C0_BASE + 0x0)
#define I2C0_F       REG8(I2C0_BASE + 0x1)
#define I2C0_C1      REG8(I2C0_BASE + 0x2)
#define I2C0_S       REG8(I2C0_BASE + 0x3)
#define I2C0_D       REG8(I2C0_BASE + 0x4)
#define I2C_C1_IICEN (1u << 7)
#define I2C_C1_MST   (1u << 5)
#define I2C_C1_TX    (1u << 4)
#define I2C_C1_TXAK  (1u << 3)
#define I2C_C1_RSTA  (1u << 2)
#define I2C_S_BUSY   (1u << 5)
#define I2C_S_ARBL   (1u << 4)
#define I2C_S_IICIF  (1u << 1)
#define I2C_S_RXAK   (1u << 0)
#define SIM_SCGC4_I2C0 (1u << 6)

static const char message[] = "Hello over I2C!";
static char readback[sizeof message];
volatile uint32_t result;               /* 1 = pass, 2 = fail (read by the tests) */
volatile uint32_t ack_polls;            /* address-only polls until the write cycle ended */

/* ------------------------------------------------------------------ UART0 */
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

static void puthex(uint32_t v, int digits)
{
    while (digits--)
        putc0("0123456789ABCDEF"[(v >> (4 * digits)) & 0xF]);
}

static void putdec(uint32_t v)
{
    char d[10];
    int i = 0;
    do {
        d[i++] = (char)('0' + v % 10u);
        v /= 10u;
    } while (v && i < 10);
    while (i)
        putc0(d[--i]);
}

/* ------------------------------------------------------------------ I2C0 (polled) */
static int i2c_wait(void)                       /* 0 = ACK, 1 = NACK, -1 = timeout / arbitration lost */
{
    uint32_t n = 2000000u;
    while (!(I2C0_S & I2C_S_IICIF))
        if (!--n)
            return -1;
    uint8_t s = I2C0_S;
    I2C0_S = I2C_S_IICIF | I2C_S_ARBL;          /* w1c */
    if (s & I2C_S_ARBL)
        return -1;
    return (s & I2C_S_RXAK) ? 1 : 0;
}

static void i2c_stop(void)
{
    I2C0_C1 &= (uint8_t)~(I2C_C1_MST | I2C_C1_TX | I2C_C1_TXAK);
    uint32_t n = 2000000u;
    while ((I2C0_S & I2C_S_BUSY) && --n)        /* BUSY clears at the STOP condition on the bus */
        ;
}

static int i2c_start(uint8_t addr_rw)           /* START + address byte; 0 = ACK */
{
    uint32_t n = 2000000u;
    while ((I2C0_S & I2C_S_BUSY) && --n)
        ;
    I2C0_C1 |= I2C_C1_TX;
    I2C0_C1 |= I2C_C1_MST;                      /* MST 0 -> 1: START */
    I2C0_D = addr_rw;
    return i2c_wait();
}

static int i2c_restart(uint8_t addr_rw)
{
    I2C0_C1 |= I2C_C1_RSTA | I2C_C1_TX;         /* repeated START */
    I2C0_D = addr_rw;
    return i2c_wait();
}

static int i2c_write_byte(uint8_t b)
{
    I2C0_D = b;
    return i2c_wait();
}

static int i2c_read(uint8_t *buf, uint32_t n)   /* master receive after the address (R) was acknowledged */
{
    I2C0_C1 &= (uint8_t)~(I2C_C1_TX | I2C_C1_TXAK);
    if (n == 1)
        I2C0_C1 |= I2C_C1_TXAK;                 /* NACK the only byte */
    (void)I2C0_D;                               /* dummy read: starts receiving the first byte */
    for (uint32_t i = 0; i < n; i++) {
        if (i2c_wait() < 0)
            return -1;
        if (i == n - 1)
            I2C0_C1 &= (uint8_t)~(I2C_C1_MST | I2C_C1_TX | I2C_C1_TXAK);   /* STOP before reading D */
        else if (i == n - 2)
            I2C0_C1 |= I2C_C1_TXAK;             /* NACK the last byte */
        buf[i] = I2C0_D;                        /* reading D starts the next byte (not after STOP) */
    }
    uint32_t t = 2000000u;
    while ((I2C0_S & I2C_S_BUSY) && --t)
        ;
    return 0;
}

static int probe(uint8_t addr)                  /* address-only write transfer: 0 = ACK */
{
    int r = i2c_start((uint8_t)(addr << 1));
    i2c_stop();
    return r;
}

static int eeprom_write(uint8_t at, const char *s, uint32_t n)
{
    while (n) {
        uint32_t room = PAGE_SIZE - (at % PAGE_SIZE);
        uint32_t k = n < room ? n : room;
        if (i2c_start((uint8_t)(I2C_TARGET << 1)) != 0 || i2c_write_byte(at) != 0) {
            i2c_stop();
            return -1;
        }
        for (uint32_t i = 0; i < k; i++)
            if (i2c_write_byte((uint8_t)s[i]) != 0) {
                i2c_stop();
                return -1;
            }
        i2c_stop();                             /* the STOP starts the internal write cycle */
        while (probe(I2C_TARGET) != 0)          /* acknowledge polling: NACK while busy */
            ack_polls++;
        at += (uint8_t)k;
        s += k;
        n -= k;
    }
    return 0;
}

static int eeprom_read(uint8_t at, char *buf, uint32_t n)
{
    if (i2c_start((uint8_t)(I2C_TARGET << 1)) != 0 || i2c_write_byte(at) != 0) {   /* dummy write: address */
        i2c_stop();
        return -1;
    }
    if (i2c_restart((uint8_t)((I2C_TARGET << 1) | 1u)) != 0) {
        i2c_stop();
        return -1;
    }
    return i2c_read((uint8_t *)buf, n);
}

int main(void)
{
    WDOG_Disable();
    SIM_CLKDIV1 = (0u << 28) | (1u << 24) | (4u << 16);   /* core /1, bus /2 = 60 MHz, flash /5 */
    SIM_SCGC5 |= SIM_SCGC5_PORTA | SIM_SCGC5_PORTB;
    SIM_SCGC4 |= SIM_SCGC4_UART0 | SIM_SCGC4_I2C0;
    PORTA->PCR[1] = PORT_PCR_MUX(2);                       /* UART0_RX */
    PORTA->PCR[2] = PORT_PCR_MUX(2);                       /* UART0_TX */
    PORTB->PCR[2] = PORT_PCR_MUX(2) | PORT_PCR_ODE;        /* I2C0_SCL, open drain */
    PORTB->PCR[3] = PORT_PCR_MUX(2) | PORT_PCR_ODE;        /* I2C0_SDA, open drain */

    uint32_t sbr = CORE_CLK_HZ / (16u * BAUD);
    uint32_t brfa = ((CORE_CLK_HZ * 4u / BAUD + 1u) / 2u - sbr * 32u) & 0x1Fu;
    UART0_C2 = 0;
    UART0_BDH = (uint8_t)((sbr >> 8) & 0x1F);
    UART0_BDL = (uint8_t)(sbr & 0xFF);
    UART0_C4 = (UART0_C4 & ~0x1Fu) | UART_C4_BRFA(brfa);
    UART0_C1 = 0;
    UART0_C2 = UART_C2_TE | UART_C2_RE;

    I2C0_F = 0x2D;                                         /* MULT 0, ICR 0x2D: 60 MHz / 640 = 93.75 kHz */
    I2C0_C1 = I2C_C1_IICEN;

    puts0("K22 I2C0 -> " TARGET_NAME " @0x");
    puthex(I2C_TARGET, 2);
    puts0("\r\n");
    if (probe(I2C_TARGET + 1u) != 0) {                     /* nobody there: NACK on the address */
        puts0("0x");
        puthex(I2C_TARGET + 1u, 2);
        puts0(": NACK\r\n");
    }
    int ok = eeprom_write(WORD_ADDR, message, sizeof message - 1u) == 0 &&
             eeprom_read(WORD_ADDR, readback, sizeof message - 1u) == 0;
    readback[sizeof message - 1u] = 0;
    if (ok) {
        puts0("wrote \"" );
        puts0(message);
        puts0("\", read \"");
        puts0(readback);
        puts0("\" (");
        putdec(ack_polls);
        puts0(" ack polls)\r\n");
    }
    for (uint32_t i = 0; ok && i < sizeof message - 1u; i++)
        ok = readback[i] == message[i];
    result = ok ? 1u : 2u;
    puts0(ok ? "PASS\r\n" : "FAIL\r\n");
    for (;;)
        __WFI();
}
