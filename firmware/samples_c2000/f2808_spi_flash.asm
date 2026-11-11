* f2808_spi_flash.asm -- TMS320F2808: SPI-B master on GPIO24 (SPISIMOB) / GPIO25 (SPISOMIB) / GPIO26 (SPICLKB) /
* GPIO27 (SPISTEB), GPAMUX2 = 3 (Table 9-17 peripheral selection 3), talks to a SPI NOR flash
* (boards/f2808_spi_flash.yaml: W25Q32-style JEDEC ID 0xEF4016 with SPISTEB as its chip select).  SPI mode 0
* (CLKPOLARITY = 0, CLK_PHASE = 1), 8-bit characters, LSPCLK / 5 = 5 MHz, FIFO mode: the words of a command go out
* back to back, so SPISTEB stays low for the whole command.
*   1. RDID (0x9F + 3 dummy bytes) -> spi_id0..2 = 0xEF, 0x40, 0x16
*   2. READ (0x03, address 0x000000, 4 dummy bytes) -> spi_data0..3 = the first four flash bytes (0xFF erased)
* spi_done = 1 when both commands have completed.  The main loop then sleeps in IDLE.

WDCR      .set 0x7029
PLLSTS    .set 0x7011
PLLCR     .set 0x7021
PCLKCR0   .set 0x701C
GPAMUX2H  .set 0x6F89                 ; high word of GPAMUX2: GPIO24-31
SPICCR    .set 0x7740                 ; SPI-B
SPICTL    .set 0x7741
SPIBRR    .set 0x7744
SPIRXBUF  .set 0x7747
SPITXBUF  .set 0x7748
SPIFFTX   .set 0x774A
SPIFFRX   .set 0x774B
SPIFFCT   .set 0x774C

        .sect "ramvars", 0x0100       ; M0: results (symbols in the .c28img)
spi_id0:   .word 0
spi_id1:   .word 0
spi_id2:   .word 0
spi_data0: .word 0
spi_data1: .word 0
spi_data2: .word 0
spi_data3: .word 0
spi_done:  .word 0

        .sect "codestart", 0x3F7FF6
        LB      _c_int00

        .sect "text", 0x3E8000
_c_int00:
        SETC    INTM
        EALLOW
        MOVW    DP, #DP(WDCR)
        MOV     @WDCR, #0x0068
        AND     @PLLSTS, #0xFFFD
        MOV     @PLLCR, #10
pll:    TBIT    @PLLSTS, #0
        SB      pll, NTC              ; 100 MHz, LSPCLK = 25 MHz
        OR      @PCLKCR0, #0x0200     ; SPIBENCLK (bit 9)
        MOVW    DP, #DP(GPAMUX2H)
        OR      @GPAMUX2H, #0x00FF    ; GPIO24-27 -> SPISIMOB / SPISOMIB / SPICLKB / SPISTEB (mux 3)
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
