* spi_flash.asm -- TMS320F28232: SPI-A master on GPIO16 (SPISIMOA) / GPIO17 (SPISOMIA) / GPIO18 (SPICLKA) /
* GPIO19 (SPISTEA) talks to a SPI NOR flash (boards/c2000_spi_flash.yaml: W25Q32-style JEDEC ID 0xEF4016 with SPISTEA
* as its chip select).  SPI mode 0 (CLKPOLARITY = 0, CLK_PHASE = 1), 8-bit characters, LSPCLK / 5 = 5 MHz, FIFO
* mode: the four words of a command go out back to back, so SPISTEA stays low for the whole command.
*   1. RDID (0x9F + 3 dummy bytes) -> spi_id0..2 = 0xEF, 0x40, 0x16
*   2. READ (0x03, address 0x000000, 4 dummy bytes) -> spi_data0..3 = the first four flash bytes (0xFF erased)
* spi_done = 1 when both commands have completed.  The main loop then sleeps in IDLE.

WDCR      .set 0x7029
PLLSTS    .set 0x7011
PLLCR     .set 0x7021
PCLKCR0   .set 0x701C
GPAMUX2   .set 0x6F88                 ; low word: GPIO16-23
SPICCR    .set 0x7040
SPICTL    .set 0x7041
SPIBRR    .set 0x7044
SPIRXBUF  .set 0x7047
SPITXBUF  .set 0x7048
SPIFFTX   .set 0x704A
SPIFFRX   .set 0x704B
SPIFFCT   .set 0x704C

        .sect "ramvars", 0x0100       ; M0: results (symbols in the .c28img)
spi_id0:   .word 0
spi_id1:   .word 0
spi_id2:   .word 0
spi_data0: .word 0
spi_data1: .word 0
spi_data2: .word 0
spi_data3: .word 0
spi_done:  .word 0

        .sect "codestart", 0x33FFF6
        LB      _c_int00

        .sect "text", 0x330000
_c_int00:
        SETC    INTM
        EALLOW
        MOVW    DP, #DP(WDCR)
        MOV     @WDCR, #0x0068
        AND     @PLLSTS, #0xFE7F
        MOV     @PLLCR, #10
pll:    TBIT    @PLLSTS, #0
        SB      pll, NTC
        OR      @PLLSTS, #0x0100      ; 100 MHz, LSPCLK = 25 MHz
        OR      @PCLKCR0, #0x0100     ; SPIAENCLK
        MOVW    DP, #DP(GPAMUX2)
        OR      @GPAMUX2, #0x0055     ; GPIO16-19 -> SPISIMOA / SPISOMIA / SPICLKA / SPISTEA (mux 1)
        EDIS
        MOVW    DP, #DP(SPICCR)
        MOV     @SPICCR, #0x0007      ; reset, 8-bit characters, CLKPOLARITY = 0
        MOV     @SPICTL, #0x000E      ; CLK_PHASE = 1, master, TALK
        MOV     @SPIBRR, #4           ; 25 MHz / 5 = 5 MHz
        MOV     @SPIFFTX, #0xE040     ; SPIRST, SPIFFENA, TX FIFO released, TXFFINTCLR
        MOV     @SPIFFRX, #0x205F     ; RX FIFO released, RXFFINTCLR, RXFFIL = 31 (no interrupts)
        MOV     @SPIFFCT, #0
        MOV     @SPICCR, #0x0087      ; release SPISWRESET
* RDID
        MOV     @SPITXBUF, #0x9F00    ; left-justified
        MOV     @SPITXBUF, #0
        MOV     @SPITXBUF, #0
        MOV     @SPITXBUF, #0
w1:     AND     AL, @SPIFFRX, #0x1F00
        CMP     @AL, #0x0400           ; RXFFST = 4
        SB      w1, NEQ
        MOV     AL, @SPIRXBUF         ; command echo
        MOVL    XAR4, #spi_id0
        MOV     AL, @SPIRXBUF
        ANDB    AL, #0xFF
        MOV     *XAR4++, AL
        MOV     AL, @SPIRXBUF
        ANDB    AL, #0xFF
        MOV     *XAR4++, AL
        MOV     AL, @SPIRXBUF
        ANDB    AL, #0xFF
        MOV     *XAR4++, AL
* READ 0x000000, 4 bytes
        MOV     @SPITXBUF, #0x0300
        MOV     @SPITXBUF, #0
        MOV     @SPITXBUF, #0
        MOV     @SPITXBUF, #0
        MOV     @SPITXBUF, #0
        MOV     @SPITXBUF, #0
        MOV     @SPITXBUF, #0
        MOV     @SPITXBUF, #0
w2:     AND     AL, @SPIFFRX, #0x1F00
        CMP     @AL, #0x0800           ; RXFFST = 8
        SB      w2, NEQ
        MOV     AL, @SPIRXBUF         ; command + address echoes
        MOV     AL, @SPIRXBUF
        MOV     AL, @SPIRXBUF
        MOV     AL, @SPIRXBUF
        MOVL    XAR4, #spi_data0
        MOV     AL, @SPIRXBUF
        ANDB    AL, #0xFF
        MOV     *XAR4++, AL
        MOV     AL, @SPIRXBUF
        ANDB    AL, #0xFF
        MOV     *XAR4++, AL
        MOV     AL, @SPIRXBUF
        ANDB    AL, #0xFF
        MOV     *XAR4++, AL
        MOV     AL, @SPIRXBUF
        ANDB    AL, #0xFF
        MOV     *XAR4++, AL
        MOV     *XAR4++, #1           ; spi_done
idle:   IDLE
        SB      idle, UNC
