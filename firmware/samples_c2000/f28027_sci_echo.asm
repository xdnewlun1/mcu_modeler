* f28027_sci_echo.asm -- TMS320F28027 (LAUNCHXL-F28027) end of boards/f28027_k22_uart.yaml (the protocol of the K22
* firmware/samples/uart_link_echo.c): echoes every byte received on SCI-A back uppercased ('a'..'z' -> 'A'..'Z') and
* toggles the LaunchPad LED GPIO0 per byte.  SCI-A on the LaunchPad backchannel pins GPIO28 (SCIRXDA) / GPIO29
* (SCITXDA), GPAMUX2 = 1 (SPRS523 Table 9-30), 115200 baud 8N1 from LSPCLK = SYSCLKOUT = 60 MHz (LOSPCP = 0), BRR 64
* (115385 baud, +0.16 %).  The SCI FIFO is 4 words deep on the Piccolo (SPRUI09 9.1); this sample polls RXRDY.
* Clock setup as f28027_blinky.asm (InitSysCtrl order, 60 MHz after the 6.55 ms PLL lock).
* chipem run firmware/samples_c2000/build/f28027_sci_echo.c28img --chip tms320f28027 --at 8ms:SCIA=hi

WDCR      .set 0x7029
PLLSTS    .set 0x7011
CLKCTL    .set 0x7012
LOSPCP    .set 0x701B
PCLKCR0   .set 0x701C
PLLCR     .set 0x7021
GPAMUX1   .set 0x6F86
GPAMUX2H  .set 0x6F89                 ; GPIO24-31
GPAPUDH   .set 0x6F8D
GPADIR    .set 0x6F8A
GPACLEAR  .set 0x6FC4
GPATOGGLE .set 0x6FC6
SCICCR    .set 0x7050
SCICTL1   .set 0x7051
SCIHBAUD  .set 0x7052
SCILBAUD  .set 0x7053
SCICTL2   .set 0x7054
SCIRXST   .set 0x7055
SCIRXBUF  .set 0x7057
SCITXBUF  .set 0x7059
Device_cal .set 0x3D7C80
BRR       .set 64                     ; 60 MHz / ((64 + 1) * 8) = 115385 baud

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
        AND     @PCLKCR0, #0xFFF7
        MOV     @CLKCTL, #0x6400
        AND     @PLLSTS, #0xFE7F
        OR      @PLLSTS, #0x0040
        MOV     @PLLCR, #12
pll:    TBIT    @PLLSTS, #0
        SB      pll, NTC
        AND     @PLLSTS, #0xFFBF
        OR      @PLLSTS, #0x0100      ; 60 MHz
        MOV     @LOSPCP, #0           ; LSPCLK = SYSCLKOUT
        OR      @PCLKCR0, #0x0400     ; SCIAENCLK
        MOVW    DP, #DP(GPAMUX2H)
        OR      @GPAMUX2H, #0x0500    ; GPIO28 -> SCIRXDA, GPIO29 -> SCITXDA (mux 1)
        AND     @GPAPUDH, #0xCFFF     ; pull-ups on GPIO28/29
        AND     @GPAMUX1, #0xFFFC     ; GPIO0 = GPIO
        OR      @GPADIR, #0x0001
        EDIS
        MOVW    DP, #DP(SCICCR)
        MOV     @SCICCR, #0x0007      ; 8N1
        MOV     @SCICTL1, #0x0003     ; TXENA | RXENA, in software reset
        MOV     @SCICTL2, #0x0000
        MOV     @SCIHBAUD, #0
        MOV     @SCILBAUD, #BRR
        MOV     @SCICTL1, #0x0023     ; release SWRESET
loop:   TBIT    @SCIRXST, #6          ; RXRDY
        SB      loop, NTC
        MOV     AL, @SCIRXBUF
        ANDB    AL, #0xFF
        CMPB    AL, #0x61
        SB      send, LO
        CMPB    AL, #0x7A
        SB      send, HI
        ADDB    AL, #-32
send:   TBIT    @SCICTL2, #7          ; TXRDY
        SB      send, NTC
        MOV     @SCITXBUF, AL
        MOVW    DP, #DP(GPATOGGLE)
        MOV     @GPATOGGLE, #0x0001
        MOVW    DP, #DP(SCICCR)
        SB      loop, UNC
