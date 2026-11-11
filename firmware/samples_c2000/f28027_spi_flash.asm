* f28027_spi_flash.asm -- TMS320F28027: SPI-A master on GPIO16 (SPISIMOA) / GPIO17 (SPISOMIA) / GPIO18 (SPICLKA)
* talks to a SPI NOR flash (boards/f28027_spi_flash.yaml, JEDEC ID 0xEF4016) whose chip select is GPIO19 driven as a
* GPIO: the Piccolo SPI FIFO is only 4 words deep (SPRUI09A 8.1), so a READ (command, 3 address bytes, 4 data bytes)
* does not fit one FIFO load under the hardware SPISTEA; this sample uses the non-FIFO mode, one character at a time
* (SPITXBUF left-justified, SPISTS.INT_FLAG, SPIRXBUF right-justified).  SPI mode 0 (CLKPOLARITY = 0, CLK_PHASE = 1),
* 8-bit characters, LSPCLK (15 MHz, LOSPCP reset value) / (SPIBRR + 1) = 15 MHz / 4 = 3.75 MHz.
*   1. RDID (0x9F) -> spi_id0..2 = 0xEF, 0x40, 0x16
*   2. READ (0x03, address 0x000000) -> spi_data0..3 = the first four flash bytes (0xFF erased)
* spi_done = 1 when both have completed (L0 0x8000..).  Clock setup as f28027_blinky.asm (60 MHz).

WDCR      .set 0x7029
PLLSTS    .set 0x7011
CLKCTL    .set 0x7012
PCLKCR0   .set 0x701C
PLLCR     .set 0x7021
Device_cal .set 0x3D7C80
GPAMUX2   .set 0x6F88                 ; low word: GPIO16-23
GPADIRH   .set 0x6F8B
GPASETH   .set 0x6FC3
GPACLEARH .set 0x6FC5
CS        .set 0x0008                 ; GPIO19 = bit 3 of the high word
SPICCR    .set 0x7040
SPICTL    .set 0x7041
SPISTS    .set 0x7042
SPIBRR    .set 0x7044
SPIRXBUF  .set 0x7047
SPITXBUF  .set 0x7048

        .sect "ramvars", 0x8000       ; L0: results (symbols in the .c28img)
spi_id0:   .word 0
spi_id1:   .word 0
spi_id2:   .word 0
spi_data0: .word 0
spi_data1: .word 0
spi_data2: .word 0
spi_data3: .word 0
spi_done:  .word 0

        .sect "codestart", 0x3F7FF6
        LB      start

        .sect "text", 0x3F0000
start:  SETC    INTM
        EALLOW
        MOVW    DP, #DP(WDCR)
        MOV     @WDCR, #0x0068
        OR      @PCLKCR0, #0x0008
        LCR     Device_cal
        EALLOW
        MOVW    DP, #DP(PCLKCR0)
        MOV     @CLKCTL, #0x6400
        AND     @PLLSTS, #0xFE7F
        OR      @PLLSTS, #0x0040
        MOV     @PLLCR, #12
pll:    TBIT    @PLLSTS, #0
        SB      pll, NTC
        AND     @PLLSTS, #0xFFBF
        OR      @PLLSTS, #0x0100      ; 60 MHz, LSPCLK = 15 MHz
        OR      @PCLKCR0, #0x0100     ; SPIAENCLK
        MOVW    DP, #DP(GPAMUX2)
        OR      @GPAMUX2, #0x0015     ; GPIO16-18 -> SPISIMOA / SPISOMIA / SPICLKA (mux 1); GPIO19 stays GPIO
        MOVW    DP, #DP(GPASETH)
        MOV     @GPASETH, #CS         ; CS high before the pin becomes an output
        MOVW    DP, #DP(GPADIRH)
        OR      @GPADIRH, #CS
        EDIS
        MOVW    DP, #DP(SPICCR)
        MOV     @SPICCR, #0x0007      ; reset, 8-bit characters, CLKPOLARITY = 0
        MOV     @SPICTL, #0x000E      ; CLK_PHASE = 1, master, TALK
        MOV     @SPIBRR, #3           ; 15 MHz / 4 = 3.75 MHz
        MOV     @SPICCR, #0x0087      ; release SPISWRESET
* RDID: command + 3 bytes into spi_id0..2
        MOVL    XAR2, #spi_id0
        MOV     @AL, #0x9F
        LCR     cs_low
        LCR     xfer
        MOVB    XAR3, #2              ; 3 bytes
rdid:   MOVB    AL, #0
        LCR     xfer
        MOV     *XAR2++, AL
        BANZ    rdid, AR3--
        LCR     cs_high
* READ 0x000000: command, 3 address bytes, 4 data bytes into spi_data0..3 (XAR2 continues there)
        LCR     cs_low
        MOVB    AL, #0x03
        LCR     xfer
        MOVB    XAR3, #2
addr:   MOVB    AL, #0
        LCR     xfer
        BANZ    addr, AR3--
        MOVB    XAR3, #3              ; 4 bytes
rd:     MOVB    AL, #0
        LCR     xfer
        MOV     *XAR2++, AL
        BANZ    rd, AR3--
        LCR     cs_high
        MOV     *XAR2, #1             ; spi_done
done:   IDLE
        SB      done, UNC

xfer:   MOVW    DP, #DP(SPITXBUF)     ; AL = byte out -> AL = byte in
        LSL     AL, #8                ; 8-bit characters are left-justified in SPITXBUF
        MOV     @SPITXBUF, AL
wait:   TBIT    @SPISTS, #6           ; INT_FLAG
        SB      wait, NTC
        MOV     AL, @SPIRXBUF         ; clears INT_FLAG; right-justified
        AND     AL, @AL, #0x00FF
        LRETR
cs_low: MOVW    DP, #DP(GPACLEARH)
        MOV     @GPACLEARH, #CS
        LRETR
cs_high:
        MOVW    DP, #DP(GPASETH)
        MOV     @GPASETH, #CS
        LRETR
