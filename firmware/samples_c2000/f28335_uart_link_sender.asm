* f28335_uart_link_sender.asm -- TMS320F28335 version of uart_link_sender.asm for boards/f28335_stm32_uart.yaml (same protocol as the K22
* firmware/samples/uart_link_sender.c): every 2 ms (CPU timer 0 TIF, polled) it sends "ping N\r\n" (N = 1, 2, ...)
* on SCI-A, and every received '\n' (end of a reply line) toggles GPIO34.  RX is polled while waiting to transmit.
* 150 MHz SYSCLKOUT (30 MHz crystal x 10 / 2), SCI-A on GPIO28/29 at 115200 8N1 (LSPCLK 37.5 MHz, BRR 40: 114329
* baud, -0.8 %), CPU timer 0 period 300000 cycles = 2 ms.

WDCR      .set 0x7029
PLLSTS    .set 0x7011
PLLCR     .set 0x7021
PCLKCR0   .set 0x701C
GPAMUX2H  .set 0x6F89
GPBDIR    .set 0x6F9A
GPBTOGGLE .set 0x6FCE
TIMER0PRD  .set 0x0C02
TIMER0PRDH .set 0x0C03
TIMER0TCR  .set 0x0C04
TIMER0TPR  .set 0x0C06
TIMER0TPRH .set 0x0C07
SCICCRA   .set 0x7050
SCICTL1A  .set 0x7051
SCIHBAUDA .set 0x7052
SCILBAUDA .set 0x7053
SCICTL2A  .set 0x7054
SCIRXSTA  .set 0x7055
SCIRXBUFA .set 0x7057
SCITXBUFA .set 0x7059
LED       .set 0x0004
BRR       .set 40
PERIOD    .set 299999                 ; 2 ms at 150 MHz
COUNT     .set 0x0100                 ; ping counter (M0 RAM)
REPLIES   .set 0x0101                 ; reply lines received

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
        OR      @PLLSTS, #0x0100      ; 150 MHz
        OR      @PCLKCR0, #0x0400     ; SCI-A clock
        MOVW    DP, #DP(GPAMUX2H)
        OR      @GPAMUX2H, #0x0500    ; GPIO28/29 -> SCIRXDA/SCITXDA
        MOVW    DP, #DP(GPBDIR)
        OR      @GPBDIR, #LED
        EDIS
        MOVW    DP, #DP(COUNT)
        MOV     @COUNT, #0
        MOV     @REPLIES, #0
        MOVW    DP, #DP(SCICCRA)
        MOV     @SCICCRA, #0x0007
        MOV     @SCICTL1A, #0x0003
        MOV     @SCICTL2A, #0x0000
        MOV     @SCIHBAUDA, #0
        MOV     @SCILBAUDA, #BRR
        MOV     @SCICTL1A, #0x0023
        MOVW    DP, #DP(TIMER0TCR)
        MOV     @TIMER0TCR, #0x0010
        MOV     @TIMER0PRD, #LO16(PERIOD)
        MOV     @TIMER0PRDH, #HI16(PERIOD)
        MOV     @TIMER0TPR, #0
        MOV     @TIMER0TPRH, #0
        MOV     @TIMER0TCR, #0x8020   ; clear TIF, reload, run (no interrupt: polled)

main:   LCR     poll_rx
        MOVW    DP, #DP(TIMER0TCR)
        TBIT    @TIMER0TCR, #15       ; TIF: 2 ms elapsed
        SB      main, NTC
        MOV     @TIMER0TCR, #0x8000   ; write 1 to clear TIF
        MOVW    DP, #DP(COUNT)
        INC     @COUNT
        MOVB    AL, #0x70             ; 'p'
        LCR     putc
        MOVB    AL, #0x69             ; 'i'
        LCR     putc
        MOVB    AL, #0x6E             ; 'n'
        LCR     putc
        MOVB    AL, #0x67             ; 'g'
        LCR     putc
        MOVB    AL, #0x20             ; ' '
        LCR     putc
        MOVW    DP, #DP(COUNT)
        MOV     AL, @COUNT
        LCR     put_dec
        MOVB    AL, #0x0D
        LCR     putc
        MOVB    AL, #0x0A
        LCR     putc
        SB      main, UNC

* poll_rx: if a byte is waiting, read it; '\n' toggles GPIO34.  Clobbers AH, DP.
poll_rx:
        MOVW    DP, #DP(SCIRXSTA)
        TBIT    @SCIRXSTA, #6         ; RXRDY
        SB      prx_done, NTC
        MOV     AH, @SCIRXBUFA
        ANDB    AH, #0xFF
        CMPB    AH, #0x0A
        SB      prx_done, NEQ
        MOVW    DP, #DP(GPBTOGGLE)
        MOV     @GPBTOGGLE, #LED
        MOVW    DP, #DP(REPLIES)
        INC     @REPLIES
prx_done:
        LRETR

* putc: send AL on SCI-A, polling RX while the transmitter is busy.  Clobbers AH, DP.
putc:
        MOVW    DP, #DP(SCICTL2A)
        TBIT    @SCICTL2A, #7         ; TXRDY
        SB      pc_send, TC
        LCR     poll_rx
        SB      putc, UNC
pc_send:
        MOV     @SCITXBUFA, AL
        LRETR

* put_dec: send AL (0..9999) in decimal without leading zeros.  Uses AR2 (rest), AR3 (divisor), AR4 (digit),
* AR5 (a digit has been sent).
put_dec:
        MOVZ    AR2, @AL
        MOVB    XAR5, #0
        MOV     @AL, #1000
        LCR     pd_digit
        MOV     @AL, #100
        LCR     pd_digit
        MOV     @AL, #10
        LCR     pd_digit
        MOV     @AL, AR2              ; units digit, always sent
        ADDB    AL, #0x30
        LCR     putc
        LRETR
pd_digit:                             ; AL = divisor: AR4 = AR2 / AL, AR2 = AR2 % AL, send the digit if needed
        MOVZ    AR3, @AL
        MOVB    XAR4, #0
        MOV     @AL, AR2
pd_loop:
        CMP     AL, @AR3
        SB      pd_have, LO
        SUB     AL, @AR3
        ADDB    XAR4, #1
        SB      pd_loop, UNC
pd_have:
        MOVZ    AR2, @AL
        MOV     @AL, AR4
        CMPB    AL, #0
        SB      pd_send, NEQ
        MOV     @AH, AR5
        CMPB    AH, #0
        SB      pd_skip, EQ
pd_send:
        MOVB    XAR5, #1
        ADDB    AL, #0x30
        LCR     putc
pd_skip:
        LRETR
