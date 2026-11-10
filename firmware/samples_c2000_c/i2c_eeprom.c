/* i2c_eeprom.c -- TMS320F28232 I2C-A master in C for the TI compiler (cl2000): writes a string into a 24C32-style
 * EEPROM at bus address 0x50 (2-byte word address, 32-byte pages), waits for the write cycle by acknowledge polling,
 * reads it back with a random read (dummy write + repeated START) and prints it on SCI-A:
 *   "I2C EEPROM\r\n" at boot, then "EEPROM: <string read back>\r\n" (or "EEPROM ERROR <code>\r\n").
 * Same register sequence as TI's Example_2833xI2C_eeprom (FIFO mode, I2CCNT + STP, ARDY for the restart).
 * Clocks: 20 MHz OSCCLK, PLLCR = 10, DIVSEL = /2 -> 100 MHz SYSCLKOUT; I2C module clock 100 MHz / (IPSC 9 + 1) =
 * 10 MHz (SPRUG03: 7-12 MHz), SCL = 10 MHz / ((ICCL 45 + 5) + (ICCH 45 + 5)) = 100 kHz.  Pins GPIO32 = SDAA,
 * GPIO33 = SCLA (GPBMUX1 = 1, pull-ups on, asynchronous qualification).  SCI-A 115200 8N1 on GPIO28/29.
 * Run: chipem run --board boards/c2000_i2c_eeprom.yaml --for 20ms */
#include "f28232.h"

#define I2COAR   REG16(0x7900)
#define I2CIER   REG16(0x7901)
#define I2CSTR   REG16(0x7902)
#define I2CCLKL  REG16(0x7903)
#define I2CCLKH  REG16(0x7904)
#define I2CCNT   REG16(0x7905)
#define I2CDRR   REG16(0x7906)
#define I2CSAR   REG16(0x7907)
#define I2CDXR   REG16(0x7908)
#define I2CMDR   REG16(0x7909)
#define I2CPSC   REG16(0x790C)
#define I2CFFTX  REG16(0x7920)
#define I2CFFRX  REG16(0x7921)

#define STR_NACK  0x0002u
#define STR_ARDY  0x0004u
#define STR_SCD   0x0020u
#define STR_BB    0x1000u
#define MDR_IRS   0x0020u
#define MDR_TRX   0x0200u
#define MDR_MST   0x0400u
#define MDR_STP   0x0800u
#define MDR_STT   0x2000u
#define PCLKCR0_I2CAENCLK 0x0010u

#define EE_ADDR   0x50u
#define EE_WORD   0x0040u              /* page aligned: the string fits one 32-byte page */

static const char message[] = "Hello over I2C";
volatile unsigned int poll_count;      /* acknowledge polls until the write cycle ended */
volatile unsigned int done;            /* 1 = ok, 2+ = error */
static char rx[17];

static void putc_(char c)
{
    while (!(SciaRegs.SCICTL2 & SCICTL2_TXRDY)) { }
    SciaRegs.SCITXBUF = (unsigned int)c & 0xFFu;
}

static void puts_(const char *s)
{
    while (*s)
        putc_(*s++);
}

static void init_sysctrl(void)
{
    EALLOW;
    WDCR = WDCR_DISABLE;
    PCLKCR0 |= PCLKCR0_SCIAENCLK | PCLKCR0_I2CAENCLK;
    GpioCtrlRegs.GPAMUX2 |= (1ul << 24) | (1ul << 26);  /* GPIO28 -> SCIRXDA, GPIO29 -> SCITXDA */
    GpioCtrlRegs.GPBPUD &= ~3ul;                        /* GPIO32/33 pull-ups on */
    GpioCtrlRegs.GPBQSEL1 |= 0xFul;                     /* asynchronous input qualification */
    GpioCtrlRegs.GPBMUX1 = (GpioCtrlRegs.GPBMUX1 & ~0xFul) | 0x5ul;   /* GPIO32 -> SDAA, GPIO33 -> SCLA */
    PLLSTS &= ~PLLSTS_DIVSEL_MSK;
    PLLCR = 10;
    while (!(PLLSTS & PLLSTS_PLLLOCKS)) { }
    PLLSTS |= PLLSTS_DIVSEL_2;                          /* 100 MHz */
    EDIS;
}

static void init_scia(void)
{
    SciaRegs.SCICCR = 0x0007;
    SciaRegs.SCICTL1 = SCICTL1_RXENA | SCICTL1_TXENA;
    SciaRegs.SCIHBAUD = 0;
    SciaRegs.SCILBAUD = 26;                             /* LSPCLK 25 MHz: 115741 baud */
    SciaRegs.SCICTL1 = SCICTL1_RXENA | SCICTL1_TXENA | SCICTL1_SWRESET;
}

static void init_i2c(void)
{
    I2CMDR = 0;                                         /* reset while configuring */
    I2CPSC = 9;                                         /* 10 MHz module clock */
    I2CCLKL = 45;
    I2CCLKH = 45;
    I2CSAR = EE_ADDR;
    I2CIER = 0;
    I2CMDR = MDR_IRS;
    I2CFFTX = 0x6000;                                   /* I2CFFEN | TXFFRST */
    I2CFFRX = 0x2040;                                   /* RXFFRST | RXFFINTCLR */
}

static void wait_idle(void)
{
    while (I2CMDR & MDR_STP) { }
    while (I2CSTR & STR_BB) { }
}

/* START, address + W, the word address, n data bytes, STOP. Returns 1 if the address was acknowledged. */
static int write_block(unsigned int word, const char *data, unsigned int n)
{
    unsigned int i;
    wait_idle();
    I2CSTR = STR_SCD | STR_NACK | STR_ARDY;
    I2CCNT = 2u + n;
    I2CDXR = (word >> 8) & 0xFFu;
    I2CDXR = word & 0xFFu;
    for (i = 0; i < n; i++)
        I2CDXR = (unsigned int)data[i] & 0xFFu;
    I2CMDR = MDR_STT | MDR_STP | MDR_MST | MDR_TRX | MDR_IRS;
    while (!(I2CSTR & STR_SCD)) { }
    return !(I2CSTR & STR_NACK);
}

static int read_block(unsigned int word, char *out, unsigned int n)
{
    unsigned int i;
    wait_idle();
    I2CSTR = STR_SCD | STR_NACK | STR_ARDY;
    I2CCNT = 2;                                         /* dummy write of the word address, no STOP */
    I2CDXR = (word >> 8) & 0xFFu;
    I2CDXR = word & 0xFFu;
    I2CMDR = MDR_STT | MDR_MST | MDR_TRX | MDR_IRS;
    while (!(I2CSTR & (STR_ARDY | STR_NACK))) { }
    if (I2CSTR & STR_NACK) {
        I2CMDR |= MDR_STP;
        return 0;
    }
    I2CCNT = n;                                         /* repeated START, address + R, n bytes, STOP */
    I2CMDR = MDR_STT | MDR_STP | MDR_MST | MDR_IRS;
    while (!(I2CSTR & STR_SCD)) { }
    for (i = 0; i < n; i++)
        out[i] = (char)(I2CDRR & 0xFFu);
    out[n] = 0;
    return 1;
}

void main(void)
{
    unsigned int n = sizeof(message) - 1u;
    DINT;
    poll_count = done = 0;
    init_sysctrl();
    init_scia();
    init_i2c();
    puts_("I2C EEPROM\r\n");
    if (!write_block(EE_WORD, message, n)) {
        done = 2;
    } else {
        while (!write_block(EE_WORD, message, 0))      /* acknowledge polling: NACK while it programs */
            poll_count++;
        if (!read_block(EE_WORD, rx, n))
            done = 3;
        else
            done = 1;
    }
    if (done == 1) {
        puts_("EEPROM: ");
        puts_(rx);
        puts_("\r\n");
    } else {
        puts_("EEPROM ERROR ");
        putc_((char)('0' + done));
        puts_("\r\n");
    }
    for (;;) { }
}
