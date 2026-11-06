* sci_echo.asm -- TMS320F28232: SCI-A on GPIO28 (SCIRXDA) / GPIO29 (SCITXDA), 115200 baud 8N1 from LSPCLK = 25 MHz
* (100 MHz / 4), polling echo of every received byte.  Feed it with: chipem run ... --uart-in 'SCIA:hi'

WDCR      .set 0x7029
PLLSTS    .set 0x7011
PLLCR     .set 0x7021
PCLKCR0   .set 0x701C
GPAMUX2H  .set 0x6F89                 ; high word of GPAMUX2 (GPIO24-31)
SCICCRA   .set 0x7050
SCICTL1A  .set 0x7051
SCIHBAUDA .set 0x7052
SCILBAUDA .set 0x7053
SCICTL2A  .set 0x7054
SCIRXSTA  .set 0x7055
SCIRXBUFA .set 0x7057
SCITXBUFA .set 0x7059
BRR       .set 26                     ; 25 MHz / ((26 + 1) * 8) = 115741 baud (+0.5 %)

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
        OR      @PLLSTS, #0x0100      ; 100 MHz
        OR      @PCLKCR0, #0x0400     ; SCIAENCLK
        MOVW    DP, #DP(GPAMUX2H)
        OR      @GPAMUX2H, #0x0500    ; GPIO28 -> SCIRXDA, GPIO29 -> SCITXDA (mux 1)
        EDIS
        LCR     sci_init
loop:   TBIT    @SCIRXSTA, #6         ; RXRDY
        SB      loop, NTC
        MOV     AL, @SCIRXBUFA
        ANDB    AL, #0xFF
txw:    TBIT    @SCICTL2A, #7         ; TXRDY
        SB      txw, NTC
        MOV     @SCITXBUFA, AL
        SB      loop, UNC

sci_init:
        MOVW    DP, #DP(SCICCRA)
        MOV     @SCICCRA, #0x0007     ; 8 data bits, no parity, 1 stop bit
        MOV     @SCICTL1A, #0x0003    ; TXENA | RXENA, still in software reset
        MOV     @SCICTL2A, #0x0000    ; polling: no interrupts
        MOV     @SCIHBAUDA, #0
        MOV     @SCILBAUDA, #BRR
        MOV     @SCICTL1A, #0x0023    ; release SWRESET
        LRETR
