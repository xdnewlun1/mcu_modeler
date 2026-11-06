* timer_irq.asm -- TMS320F28232: CPU timer 0 interrupts every 1 ms (PIE group 1 channel 7, TINT0); the ISR toggles
* GPIO34 and acknowledges PIE group 1.  The main loop sleeps in IDLE.  100 MHz SYSCLKOUT (PLL x10 /2).

WDCR      .set 0x7029
PLLSTS    .set 0x7011
PLLCR     .set 0x7021
GPBDIR    .set 0x6F9A
GPBTOGGLE .set 0x6FCE
TIMER0PRD  .set 0x0C02
TIMER0PRDH .set 0x0C03
TIMER0TCR  .set 0x0C04
TIMER0TPR  .set 0x0C06
TIMER0TPRH .set 0x0C07
PIECTRL   .set 0x0CE0
PIEACK    .set 0x0CE1
PIEIER1   .set 0x0CE2
TINT0VEC  .set 0x0D4C                 ; PIE vector 38 = INT1.7
LED       .set 0x0004
PERIOD    .set 99999                  ; (PRD + 1) cycles = 1 ms at 100 MHz

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
        MOVW    DP, #DP(GPBDIR)
        OR      @GPBDIR, #LED
        MOVW    DP, #DP(TINT0VEC)     ; PIE vector table (EALLOW protected)
        MOVL    XAR4, #tint0_isr
        MOVL    @TINT0VEC, XAR4
        EDIS
        MOVW    DP, #DP(TIMER0TCR)
        MOV     @TIMER0TCR, #0x0010   ; TSS: stop
        MOV     @TIMER0PRD, #LO16(PERIOD)
        MOV     @TIMER0PRDH, #HI16(PERIOD)
        MOV     @TIMER0TPR, #0        ; prescale /1
        MOV     @TIMER0TPRH, #0
        MOV     @TIMER0TCR, #0x4020   ; TIE | TRB (reload), TSS = 0: run
        MOVW    DP, #DP(PIECTRL)
        MOV     @PIECTRL, #1          ; ENPIE
        OR      @PIEIER1, #0x0040     ; INT1.7
        MOV     @PIEACK, #0xFFFF
        OR      IER, #0x0001          ; CPU INT1
        EINT
idle:   IDLE
        SB      idle, UNC

tint0_isr:
        MOVW    DP, #DP(GPBTOGGLE)    ; DP is part of the automatic context save
        MOV     @GPBTOGGLE, #LED
        MOVW    DP, #DP(PIEACK)
        MOV     @PIEACK, #0x0001      ; acknowledge group 1
        IRET
