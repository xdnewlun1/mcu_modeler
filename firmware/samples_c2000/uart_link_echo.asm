* uart_link_echo.asm -- TMS320F28232 end of boards/c2000_k22_uart.yaml (same protocol as the K22
* firmware/samples/uart_link_echo.c): echoes every byte received on SCI-A back uppercased ('a'..'z' -> 'A'..'Z')
* and toggles GPIO34 per received byte.  SCI-A on GPIO28/29, 115200 baud 8N1, 100 MHz SYSCLKOUT, LSPCLK 25 MHz.

WDCR      .set 0x7029
PLLSTS    .set 0x7011
PLLCR     .set 0x7021
PCLKCR0   .set 0x701C
GPAMUX2H  .set 0x6F89
GPBDIR    .set 0x6F9A
GPBTOGGLE .set 0x6FCE
SCICCRA   .set 0x7050
SCICTL1A  .set 0x7051
SCIHBAUDA .set 0x7052
SCILBAUDA .set 0x7053
SCICTL2A  .set 0x7054
SCIRXSTA  .set 0x7055
SCIRXBUFA .set 0x7057
SCITXBUFA .set 0x7059
LED       .set 0x0004
BRR       .set 26

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
        OR      @PLLSTS, #0x0100
        OR      @PCLKCR0, #0x0400
        MOVW    DP, #DP(GPAMUX2H)
        OR      @GPAMUX2H, #0x0500
        MOVW    DP, #DP(GPBDIR)
        OR      @GPBDIR, #LED
        EDIS
        MOVW    DP, #DP(SCICCRA)
        MOV     @SCICCRA, #0x0007
        MOV     @SCICTL1A, #0x0003
        MOV     @SCICTL2A, #0x0000
        MOV     @SCIHBAUDA, #0
        MOV     @SCILBAUDA, #BRR
        MOV     @SCICTL1A, #0x0023
loop:   TBIT    @SCIRXSTA, #6         ; RXRDY
        SB      loop, NTC
        MOV     AL, @SCIRXBUFA
        ANDB    AL, #0xFF
        CMPB    AL, #0x61             ; 'a'
        SB      send, LO
        CMPB    AL, #0x7A             ; 'z'
        SB      send, HI
        ADDB    AL, #-32              ; to upper case
send:   TBIT    @SCICTL2A, #7         ; TXRDY
        SB      send, NTC
        MOV     @SCITXBUFA, AL
        MOVW    DP, #DP(GPBTOGGLE)
        MOV     @GPBTOGGLE, #LED
        MOVW    DP, #DP(SCICCRA)
        SB      loop, UNC
