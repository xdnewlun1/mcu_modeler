* uart_chat.asm -- TMS320F28232 side of the two-way UART chat (boards/c2000_stm32_chat.yaml; same protocol as
* firmware/samples_stm32/uart_chat.c and firmware/samples_stm32_hal/):
*   115200 8N1, ASCII lines ending "\r\n".  At boot send "HELLO C2000"; every 5 ms send "PING <n>" (n = 1, 2, ...);
*   a received "PING <n>" is answered with "PONG <n>" (the digits are echoed as received); a received "PONG <n>"
*   toggles GPIO34 and increments PONGS; a received "HELLO <x>" sets PEER and drives GPIO35 high, and the first HELLO
*   received is answered once with our own "HELLO C2000" (only once, so two chips doing it cannot loop; it lets a
*   peer that booted later, or missed our boot HELLO, see us).  Unknown lines are ignored, '\r' is optional (bare "\n" works), lines longer than 31 characters are truncated.
* Clocks: 20 MHz OSCCLK, PLL x10 /2 = 100 MHz SYSCLKOUT, LSPCLK = 25 MHz, SCI-A BRR 26 = 115741 baud (+0.5 %).
* RX: SCI-A RX interrupt (SCIRXINTA = PIE INT9.1, vector 0x0DC0, CPU INT9) pushes each byte into a 64-word ring.
* Tick: CPU timer 0 interrupt (TINT0 = PIE INT1.7, vector 0x0D4C, CPU INT1) every 5 ms increments TICK.
* Main loop: with INTM set, check the ring and TICK; if idle, IDLE (it wakes on IFR & IER even while INTM masks
* the interrupt, so no wakeup is lost), then EINT lets the ISR run.  All transmitting is done by the main loop
* (polled TXRDY), so the ISRs never wait.
* GPIO28/29 are muxed to SCI-A before the PLL relock (~6.6 ms) so a board net sees the SCI pin owners at once.

* ---- system control (EALLOW protected) ----
WDCR      .set 0x7029                 ; watchdog control: 0x0068 = WDDIS
PLLSTS    .set 0x7011                 ; bit 0 PLLLOCKS, bit 7 DIVSEL(0), bit 8 DIVSEL(1)
PLLCR     .set 0x7021                 ; PLL multiplier
PCLKCR0   .set 0x701C                 ; bit 10 SCIAENCLK
* ---- GPIO ----
GPAMUX2H  .set 0x6F89                 ; GPAMUX2 high word (GPIO24-31): GPIO28/29 mux 1 = SCIRXDA/SCITXDA
GPBDIR    .set 0x6F9A                 ; GPIO32-63 direction
GPBSET    .set 0x6FCA                 ; write 1 to set
GPBTOGGLE .set 0x6FCE                 ; write 1 to toggle
LED       .set 0x0004                 ; GPIO34 (GPB bit 2): toggled per PONG
PEERPIN   .set 0x0008                 ; GPIO35 (GPB bit 3): high after a HELLO
* ---- CPU timer 0 ----
TIMER0PRD  .set 0x0C02
TIMER0PRDH .set 0x0C03
TIMER0TCR  .set 0x0C04                ; bit 4 TSS, bit 5 TRB, bit 14 TIE, bit 15 TIF
TIMER0TPR  .set 0x0C06
TIMER0TPRH .set 0x0C07
PERIOD    .set 499999                 ; (PRD + 1) cycles = 5 ms at 100 MHz
* ---- PIE ----
PIECTRL   .set 0x0CE0                 ; bit 0 ENPIE
PIEACK    .set 0x0CE1                 ; write 1 to acknowledge a group
PIEIER1   .set 0x0CE2                 ; group 1 enables (bit 6 = INT1.7 TINT0)
PIEIER9   .set 0x0CF2                 ; group 9 enables (bit 0 = INT9.1 SCIRXINTA)
TINT0VEC  .set 0x0D4C                 ; PIE vector 38 = INT1.7
SCIRXAVEC .set 0x0DC0                 ; PIE vector 96 = INT9.1
* ---- SCI-A ----
SCICCRA   .set 0x7050                 ; 0x0007: 8 data bits, no parity, 1 stop bit
SCICTL1A  .set 0x7051                 ; bit 0 RXENA, bit 1 TXENA, bit 5 SWRESET
SCIHBAUDA .set 0x7052
SCILBAUDA .set 0x7053
SCICTL2A  .set 0x7054                 ; bit 1 RXBKINTENA, bit 7 TXRDY
SCIRXSTA  .set 0x7055                 ; bit 6 RXRDY
SCIRXBUFA .set 0x7057                 ; reading clears RXRDY
SCITXBUFA .set 0x7059
BRR       .set 26                     ; 25 MHz / ((26 + 1) * 8) = 115741 baud
* ---- RAM (M0, one DP page for the variables) ----
VARS      .set 0x0100
PINGN     .set 0x0100                 ; last PING number sent
PONGS     .set 0x0101                 ; PONG lines received
PEER      .set 0x0102                 ; 1 after a HELLO line
TICK      .set 0x0103                 ; 5 ms periods not yet handled (set by the timer ISR)
RXHEAD    .set 0x0104                 ; ring write index (ISR)
RXTAIL    .set 0x0105                 ; ring read index (main loop)
LINELEN   .set 0x0106                 ; characters in LINE
PINGSRX   .set 0x0107                 ; PING lines answered
HELLOANS  .set 0x0108                 ; 1 once the first received HELLO was answered
RING      .set 0x0140                 ; 64-word RX ring (one byte per word)
LINE      .set 0x0180                 ; current line, NUL-terminated when complete (32 words)
LINEMAX   .set 31

        .sect "codestart", 0x33FFF6
        LB      _c_int00

        .sect "text", 0x330000
_c_int00:
        SETC    INTM
        EALLOW
        MOVW    DP, #DP(WDCR)
        MOV     @WDCR, #0x0068        ; watchdog off
        OR      @PCLKCR0, #0x0400     ; SCI-A clock
        MOVW    DP, #DP(GPAMUX2H)
        OR      @GPAMUX2H, #0x0500    ; GPIO28 -> SCIRXDA, GPIO29 -> SCITXDA
        MOVW    DP, #DP(GPBDIR)
        OR      @GPBDIR, #LED|PEERPIN
        MOVW    DP, #DP(PLLSTS)
        AND     @PLLSTS, #0xFE7F      ; DIVSEL = 0 while relocking
        MOV     @PLLCR, #10
pll:    TBIT    @PLLSTS, #0
        SB      pll, NTC
        OR      @PLLSTS, #0x0100      ; DIVSEL = 2 (/2): 100 MHz
        MOVW    DP, #DP(TINT0VEC)     ; PIE vector table
        MOVL    XAR4, #tint0_isr
        MOVL    @TINT0VEC, XAR4
        MOVW    DP, #DP(SCIRXAVEC)
        MOVL    XAR4, #scirxa_isr
        MOVL    @SCIRXAVEC, XAR4
        EDIS

        MOVW    DP, #DP(VARS)
        MOV     @PINGN, #0
        MOV     @PONGS, #0
        MOV     @PEER, #0
        MOV     @TICK, #0
        MOV     @RXHEAD, #0
        MOV     @RXTAIL, #0
        MOV     @LINELEN, #0
        MOV     @PINGSRX, #0
        MOV     @HELLOANS, #0

        MOVW    DP, #DP(PIECTRL)
        MOV     @PIECTRL, #1          ; ENPIE
        OR      @PIEIER1, #0x0040     ; INT1.7 TINT0
        OR      @PIEIER9, #0x0001     ; INT9.1 SCIRXINTA
        MOV     @PIEACK, #0xFFFF
        OR      IER, #0x0101          ; CPU INT1 | INT9

        MOVW    DP, #DP(SCICCRA)
        MOV     @SCICCRA, #0x0007
        MOV     @SCICTL1A, #0x0003    ; TXENA | RXENA, still in software reset
        MOV     @SCICTL2A, #0x0002    ; RXBKINTENA: RX interrupt
        MOV     @SCIHBAUDA, #0
        MOV     @SCILBAUDA, #BRR
        MOV     @SCICTL1A, #0x0023    ; release SWRESET

        MOVW    DP, #DP(TIMER0TCR)
        MOV     @TIMER0TCR, #0x0010   ; stop
        MOV     @TIMER0PRD, #LO16(PERIOD)
        MOV     @TIMER0PRDH, #HI16(PERIOD)
        MOV     @TIMER0TPR, #0
        MOV     @TIMER0TPRH, #0
        MOV     @TIMER0TCR, #0xC020   ; clear TIF, TIE, reload, run
        EINT

        MOVL    XAR5, #s_hello_me     ; "HELLO C2000\r\n"
        LCR     puts
        LCR     crlf

main:   DINT
        MOVW    DP, #DP(VARS)
        MOV     AL, @RXHEAD
        CMP     AL, @RXTAIL
        SB      m_byte, NEQ
        MOV     AL, @TICK
        CMPB    AL, #0
        SB      m_tick, NEQ
        IDLE                          ; wakes on a pending enabled interrupt even with INTM = 1
        EINT                          ; ... which is taken here
        SB      main, UNC
m_tick: MOV     @TICK, #0
        EINT
        LCR     send_ping
        SB      main, UNC
m_byte: MOV     AH, @RXTAIL
        ADD     @AH, #RING
        MOVZ    AR4, @AH
        MOV     AL, *XAR4             ; AL = RING[RXTAIL]
        INC     @RXTAIL
        AND     @RXTAIL, #0x003F
        EINT
        LCR     on_byte
        SB      main, UNC

* on_byte: AL = received byte.  '\r' ignored, '\n' ends the line, others are appended (up to LINEMAX).
on_byte:
        CMPB    AL, #0x0D
        SB      ob_ret, EQ
        CMPB    AL, #0x0A
        SB      ob_eol, EQ
        MOVW    DP, #DP(VARS)
        MOV     AH, @LINELEN
        CMPB    AH, #LINEMAX
        SB      ob_ret, HIS
        ADD     @AH, #LINE
        MOVZ    AR4, @AH
        MOV     *XAR4, AL
        INC     @LINELEN
ob_ret: LRETR
ob_eol: MOVW    DP, #DP(VARS)
        MOV     AH, @LINELEN
        ADD     @AH, #LINE
        MOVZ    AR4, @AH
        MOV     *XAR4, #0             ; NUL-terminate
        MOV     @LINELEN, #0
        MOVL    XAR5, #s_ping
        LCR     prefix
        CMPB    AL, #0
        SB      is_ping, NEQ
        MOVL    XAR5, #s_pong
        LCR     prefix
        CMPB    AL, #0
        SB      is_pong, NEQ
        MOVL    XAR5, #s_hello
        LCR     prefix
        CMPB    AL, #0
        SB      is_hello, NEQ
        LRETR                         ; unknown line

* "PING <n>": reply "PONG <n>" echoing the digit run (at least one digit, else the line is ignored).
is_ping:
        MOVL    XAR6, @XAR4           ; XAR6 -> first digit
        MOV     AL, *XAR4
        ADDB    AL, #-48
        CMPB    AL, #10
        SB      ip_ret, HIS           ; not a digit
        MOVL    XAR5, #s_pong
        LCR     puts
ip_dig: MOV     AL, *XAR6++
        MOV     AH, @AL
        ADDB    AH, #-48
        CMPB    AH, #10
        SB      ip_end, HIS
        LCR     putc
        SB      ip_dig, UNC
ip_end: LCR     crlf
        MOVW    DP, #DP(VARS)
        INC     @PINGSRX
ip_ret: LRETR

* "PONG <n>": toggle GPIO34, count it.
is_pong:
        MOVW    DP, #DP(GPBTOGGLE)
        MOV     @GPBTOGGLE, #LED
        MOVW    DP, #DP(VARS)
        INC     @PONGS
        LRETR

* "HELLO <x>": peer seen, GPIO35 high; the first one is answered with our own HELLO.
is_hello:
        MOVW    DP, #DP(GPBSET)
        MOV     @GPBSET, #PEERPIN
        MOVW    DP, #DP(VARS)
        MOV     @PEER, #1
        MOV     AL, @HELLOANS
        CMPB    AL, #0
        SB      ih_ret, NEQ           ; already answered one
        MOV     @HELLOANS, #1
        MOVL    XAR5, #s_hello_me
        LCR     puts
        LCR     crlf
ih_ret: LRETR

* prefix: XAR5 -> NUL-terminated pattern.  AL = 1 if LINE starts with it (XAR4 -> the character after it), else 0.
prefix: MOVL    XAR4, #LINE
pf_lp:  MOV     AL, *XAR5++
        CMPB    AL, #0
        SB      pf_yes, EQ
        MOV     AH, *XAR4++
        CMP     AH, @AL
        SB      pf_lp, EQ
        MOVB    AL, #0
        LRETR
pf_yes: MOVB    AL, #1
        LRETR

* send_ping: PINGN += 1, send "PING <PINGN>\r\n".
send_ping:
        MOVW    DP, #DP(VARS)
        INC     @PINGN
        MOVL    XAR5, #s_ping
        LCR     puts
        MOVW    DP, #DP(VARS)
        MOV     AL, @PINGN
        LCR     put_dec
        LCR     crlf
        LRETR

* puts: send the NUL-terminated string at XAR5 (one character per word, in flash).
puts:   MOV     AL, *XAR5++
        CMPB    AL, #0
        SB      ps_end, EQ
        LCR     putc
        SB      puts, UNC
ps_end: LRETR

crlf:   MOVB    AL, #0x0D
        LCR     putc
        MOVB    AL, #0x0A
        LCR     putc
        LRETR

* putc: send AL on SCI-A (waits for TXRDY).  Clobbers DP.
putc:   MOVW    DP, #DP(SCICTL2A)
pc_w:   TBIT    @SCICTL2A, #7         ; TXRDY
        SB      pc_w, NTC
        MOV     @SCITXBUFA, AL
        LRETR

* put_dec: send AL (0..65535) in decimal without leading zeros.  Uses AR2 (rest), AR3 (divisor), AR4 (digit),
* AR5 (a digit has been sent).
put_dec:
        MOVZ    AR2, @AL
        MOVB    XAR5, #0
        MOV     @AL, #10000
        LCR     pd_digit
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

* ---- interrupt service routines (the CPU saves T, ST0, ACC, P, AR0, AR1, DP, ST1, DBGSTAT, IER, PC) ----
tint0_isr:
        MOVW    DP, #DP(TICK)
        INC     @TICK
        MOVW    DP, #DP(PIEACK)
        MOV     @PIEACK, #0x0001      ; acknowledge group 1
        IRET

scirxa_isr:
        PUSH    XAR4
        MOVW    DP, #DP(SCIRXBUFA)
        MOV     AL, @SCIRXBUFA        ; clears RXRDY (drops the RX interrupt line)
        ANDB    AL, #0xFF
        MOVW    DP, #DP(VARS)
        MOV     AH, @RXHEAD
        ADD     @AH, #RING
        MOVZ    AR4, @AH
        MOV     *XAR4, AL             ; RING[RXHEAD] = byte
        INC     @RXHEAD
        AND     @RXHEAD, #0x003F
        MOVW    DP, #DP(PIEACK)
        MOV     @PIEACK, #0x0100      ; acknowledge group 9
        POP     XAR4
        IRET

* ---- strings (one character per word) ----
s_hello_me: .cstring "HELLO C2000"
s_hello:    .cstring "HELLO "
s_ping:     .cstring "PING "
s_pong:     .cstring "PONG "
