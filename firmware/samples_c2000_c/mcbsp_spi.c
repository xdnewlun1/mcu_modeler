/* mcbsp_spi.c -- TMS320F28232 McBSP-A as an SPI master (clock-stop mode, SPRUI07 12.7) in C for the TI compiler
 * (cl2000), talking to a SPI NOR flash (boards/c2000_mcbsp_flash.yaml: W25Q32-style JEDEC ID 0xEF4016):
 *   RDID, WREN + PAGE PROGRAM of "MCBS" at 0x000100, READ of those 4 bytes, then prints on SCI-A
 *   "MCBSP SPI\r\n" at boot and "ID EF4016 DATA MCBS RINT <n>\r\n" (hex ID, the bytes read back, MRINTA count).
 * McBSP-A (Table 12-16, master): CLKSTP = 11b (data half a cycle ahead of the clock: SPI mode 0 with CLKXP = 0,
 * CLKRP = 1), CLKXM = 1, SCLKME = 0 / CLKSM = 1, CLKGDV = 24 -> MCLKXA = LSPCLK 25 MHz / 25 = 1 MHz, FSXM = 1,
 * FSGM = 0, FSXP = 1 (MFSXA active low per byte), XDATDLY = RDATDLY = 1, 8-bit words.  Pins: GPIO20 MDXA (SIMO),
 * GPIO21 MDRA (SOMI), GPIO22 MCLKXA (SPICLK), GPIO23 MFSXA (SPISTE per byte, not used by the flash); the flash
 * chip select is GPIO24 driven as a GPIO, so a command spans several McBSP packets.  MRINTA (PIE 6.5,
 * MFFINT.RINTENA, RINTM = 0) counts received words in `rint_count`; the data is polled from DRR1.
 * Clocks: 20 MHz OSCCLK, PLLCR = 10, DIVSEL = /2 -> 100 MHz; LSPCLK = 25 MHz.  SCI-A 115200 8N1 on GPIO28/29.
 * Run: chipem run --board boards/c2000_mcbsp_flash.yaml --for 15ms */
#include "f28232.h"

/* ---- McBSP-A, 0x5000 (Peripheral Frame 3) ---- */
#define DRR1     REG16(0x5001)
#define DXR1     REG16(0x5003)
#define SPCR2    REG16(0x5004)
#define SPCR1    REG16(0x5005)
#define RCR2     REG16(0x5006)
#define RCR1     REG16(0x5007)
#define XCR2     REG16(0x5008)
#define XCR1     REG16(0x5009)
#define SRGR2    REG16(0x500A)
#define SRGR1    REG16(0x500B)
#define MCR2     REG16(0x500C)
#define MCR1     REG16(0x500D)
#define PCR      REG16(0x5012)
#define MFFINT   REG16(0x5023)
#define SPCR1_RRST    0x0001u
#define SPCR1_RRDY    0x0002u
#define SPCR1_CLKSTP3 0x1800u
#define SPCR2_XRST    0x0001u
#define SPCR2_XRDY    0x0002u
#define SPCR2_GRST    0x0040u
#define SPCR2_FRST    0x0080u
#define PCR_CLKRP     0x0001u
#define PCR_FSXP      0x0008u
#define PCR_CLKXM     0x0200u
#define PCR_FSXM      0x0800u
#define SRGR2_CLKSM   0x2000u
#define MFFINT_RINTENA 0x0004u
#define PCLKCR0_MCBSPAENCLK 0x1000u
#define VEC_MRINTA PIE_VECNUM(6, 5)
#define M_INT6 0x0020u

#define CS_BIT (1ul << 24)                 /* GPIO24: flash chip select (active low) */

volatile unsigned long rint_count;
volatile unsigned int done;
volatile unsigned int flash_id[3];
volatile unsigned int flash_data[4];

__interrupt void mrinta_isr(void)
{
    rint_count++;
    PieCtrlRegs.PIEACK = PIEACK_GROUP(6);
}

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

static void put_hex(unsigned long v, unsigned int digits)
{
    while (digits--)
        putc_("0123456789ABCDEF"[(v >> (4u * digits)) & 0xFu]);
}

static void put_dec(unsigned long v)
{
    char buf[11];
    unsigned int n = 0;
    do {
        buf[n++] = (char)('0' + v % 10u);
        v /= 10u;
    } while (v);
    while (n)
        putc_(buf[--n]);
}

static void init_sysctrl(void)
{
    EALLOW;
    WDCR = WDCR_DISABLE;
    PCLKCR0 |= PCLKCR0_SCIAENCLK | PCLKCR0_MCBSPAENCLK;
    GpioDataRegs.GPASET = CS_BIT;                       /* chip select high before it becomes an output */
    GpioCtrlRegs.GPADIR |= CS_BIT;
    GpioCtrlRegs.GPAMUX2 = (GpioCtrlRegs.GPAMUX2 & ~0x0003FF00ul)
                         | 0x0000AA00ul                 /* GPIO20-23 -> MDXA / MDRA / MCLKXA / MFSXA (mux 2) */
                         | (1ul << 24) | (1ul << 26);   /* GPIO28 -> SCIRXDA, GPIO29 -> SCITXDA; GPIO24 stays GPIO */
    PLLSTS &= ~PLLSTS_DIVSEL_MSK;
    PLLCR = 10;
    while (!(PLLSTS & PLLSTS_PLLLOCKS)) { }
    PLLSTS |= PLLSTS_DIVSEL_2;                          /* 100 MHz */
    PIE_VECT[VEC_MRINTA] = (unsigned long)mrinta_isr;
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

static void delay_cycles(unsigned int n)
{
    while (n--)
        asm(" NOP");
}

static void init_mcbsp(void)
{
    SPCR2 = 0;                                          /* XRST = GRST = FRST = 0 */
    SPCR1 = SPCR1_CLKSTP3;                              /* RRST = 0, clock stop with half-cycle delay */
    RCR2 = 0x0001;                                      /* single phase, RDATDLY = 1 */
    RCR1 = 0x0000;                                      /* 1 word of 8 bits */
    XCR2 = 0x0001;
    XCR1 = 0x0000;
    SRGR2 = SRGR2_CLKSM;                                /* LSPCLK, FSGM = 0 */
    SRGR1 = 24;                                         /* CLKGDV = 24: 1 MHz */
    MCR1 = 0;
    MCR2 = 0;
    PCR = PCR_CLKXM | PCR_FSXM | PCR_FSXP | PCR_CLKRP;
    MFFINT = MFFINT_RINTENA;
    SPCR2 |= SPCR2_GRST;
    delay_cycles(100);                                  /* two CLKG cycles */
    SPCR1 |= SPCR1_RRST;
    SPCR2 |= SPCR2_XRST;
    delay_cycles(100);
    SPCR2 |= SPCR2_FRST;                                /* frame syncs from the DXR copies */
}

static unsigned int xfer(unsigned int b)
{
    while (!(SPCR2 & SPCR2_XRDY)) { }
    DXR1 = b & 0xFFu;
    while (!(SPCR1 & SPCR1_RRDY)) { }
    return DRR1 & 0xFFu;
}

static void cs(int low)
{
    if (low)
        GpioDataRegs.GPACLEAR = CS_BIT;
    else
        GpioDataRegs.GPASET = CS_BIT;
}

static void cmd_addr(unsigned int op, unsigned long addr)
{
    xfer(op);
    xfer((unsigned int)(addr >> 16));
    xfer((unsigned int)(addr >> 8));
    xfer((unsigned int)addr);
}

void main(void)
{
    static const char msg[4] = {'M', 'C', 'B', 'S'};
    unsigned int i;
    DINT;
    rint_count = 0;
    done = 0;
    init_sysctrl();
    init_scia();
    PieCtrlRegs.PIECTRL = 1;                            /* ENPIE */
    PIEIER(6) |= 1u << 4;                               /* INT6.5 MRINTA */
    PieCtrlRegs.PIEACK = 0xFFFF;
    IER |= M_INT6;
    EINT;
    puts_("MCBSP SPI\r\n");
    init_mcbsp();

    cs(1);                                              /* RDID */
    xfer(0x9F);
    for (i = 0; i < 3u; i++)
        flash_id[i] = xfer(0);
    cs(0);
    cs(1);                                              /* WREN */
    xfer(0x06);
    cs(0);
    cs(1);                                              /* PAGE PROGRAM 4 bytes at 0x100 */
    cmd_addr(0x02, 0x000100ul);
    for (i = 0; i < 4u; i++)
        xfer((unsigned int)msg[i]);
    cs(0);
    cs(1);                                              /* READ them back */
    cmd_addr(0x03, 0x000100ul);
    for (i = 0; i < 4u; i++)
        flash_data[i] = xfer(0);
    cs(0);

    puts_("ID ");
    for (i = 0; i < 3u; i++)
        put_hex(flash_id[i], 2);
    puts_(" DATA ");
    for (i = 0; i < 4u; i++)
        putc_((char)flash_data[i]);
    puts_(" RINT ");
    put_dec(rint_count);
    puts_("\r\n");
    done = 1;
    for (;;) { }
}
