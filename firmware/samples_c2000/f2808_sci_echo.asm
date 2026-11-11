* f2808_sci_echo.asm -- TMS320F2808 end of boards/f2808_k22_uart.yaml (same protocol as the K22
* firmware/samples/uart_link_echo.c): echoes every byte received on SCI-B back uppercased ('a'..'z' -> 'A'..'Z')
* and toggles GPIO34 per received byte.  SCI-B on GPIO18 (SCITXDB) / GPIO19 (SCIRXDB), GPAMUX2 = 2 (Table 9-17
* peripheral selection 2), 115200 baud 8N1 from LSPCLK = 25 MHz (100 MHz / 4, LOSPCP reset value), BRR 26.
* Feed it with: chipem run firmware/samples_c2000/build/f2808_sci_echo.c28img --chip tms320f2808 --at 7ms:SCIB=hi

WDCR      .set 0x7029
PLLSTS    .set 0x7011
PLLCR     .set 0x7021
PCLKCR0   .set 0x701C
GPAMUX2   .set 0x6F88                 ; low word: GPIO16-23
GPBDIR    .set 0x6F9A
GPBTOGGLE .set 0x6FCE
SCICCRB   .set 0x7750
SCICTL1B  .set 0x7751
SCIHBAUDB .set 0x7752
SCILBAUDB .set 0x7753
SCICTL2B  .set 0x7754
SCIRXSTB  .set 0x7755
SCIRXBUFB .set 0x7757
SCITXBUFB .set 0x7759
LED       .set 0x0004
BRR       .set 26                     ; 25 MHz / ((26 + 1) * 8) = 115741 baud (+0.5 %)

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
        OR      @PCLKCR0, #0x0800     ; SCIBENCLK (bit 11)
        MOVW    DP, #DP(GPAMUX2)
        OR      @GPAMUX2, #0x00A0     ; GPIO18 -> SCITXDB, GPIO19 -> SCIRXDB (mux 2)
        MOVW    DP, #DP(GPBDIR)
        OR      @GPBDIR, #LED
        EDIS
        MOVW    DP, #DP(SCICCRB)
        MOV     @SCICCRB, #0x0007     ; 8 data bits, no parity, 1 stop bit
        MOV     @SCICTL1B, #0x0003    ; TXENA | RXENA, still in software reset
        MOV     @SCICTL2B, #0x0000    ; polling: no interrupts
        MOV     @SCIHBAUDB, #0
        MOV     @SCILBAUDB, #BRR
        MOV     @SCICTL1B, #0x0023    ; release SWRESET
loop:   TBIT    @SCIRXSTB, #6         ; RXRDY
        SB      loop, NTC
        MOV     AL, @SCIRXBUFB
        ANDB    AL, #0xFF
        CMPB    AL, #0x61             ; 'a'
        SB      send, LO
        CMPB    AL, #0x7A             ; 'z'
        SB      send, HI
        ADDB    AL, #-32              ; to upper case
send:   TBIT    @SCICTL2B, #7         ; TXRDY
        SB      send, NTC
        MOV     @SCITXBUFB, AL
        MOVW    DP, #DP(GPBTOGGLE)
        MOV     @GPBTOGGLE, #LED
        MOVW    DP, #DP(SCICCRB)
        SB      loop, UNC
