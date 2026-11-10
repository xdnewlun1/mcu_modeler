* standby.asm -- TMS320F28232 low-power modes (SPRUI07 1.3.3).  CPU timer 0 toggles GPIO34 every 1 ms while the
* device is awake (main loop in IDLE mode, woken by TINT0).  After 3 timer ticks the main loop enters STANDBY:
* SYSCLKOUT stops, so the timer (and GPIO34) freeze.  Wake-up sources: GPIO12 held low for QUALSTDBY + 2 = 12
* OSCCLK (GPIOLPMSEL bit 12; GPIO12 has its pull-up at reset) and the watchdog interrupt (SCSR.WDENINT +
* LPMCR0.WDINTE; WDCLK = OSCCLK/512, so it fires every 256 x 512 / 20 MHz = 6.55 ms).  Both arrive as WAKEINT
* (PIE 1.8), whose ISR toggles GPIO35.  100 MHz SYSCLKOUT (PLL x10 /2).

WDCR      .set 0x7029
WDKEY     .set 0x7025
SCSR      .set 0x7022
LPMCR0    .set 0x701E
PLLSTS    .set 0x7011
PLLCR     .set 0x7021
GPBDIR    .set 0x6F9A
GPBTOGGLE .set 0x6FCE
GPIOLPMSEL .set 0x6FE8
TIMER0PRD  .set 0x0C02
TIMER0PRDH .set 0x0C03
TIMER0TCR  .set 0x0C04
TIMER0TPR  .set 0x0C06
TIMER0TPRH .set 0x0C07
PIECTRL   .set 0x0CE0
PIEACK    .set 0x0CE1
PIEIER1   .set 0x0CE2
TINT0VEC  .set 0x0D4C                 ; PIE vector 38 = INT1.7
WAKEVEC   .set 0x0D4E                 ; PIE vector 39 = INT1.8 (WAKEINT)
LED       .set 0x0004                 ; GPIO34
WAKELED   .set 0x0008                 ; GPIO35
PERIOD    .set 99999                  ; (PRD + 1) cycles = 1 ms at 100 MHz
LPM_IDLE  .set 0x8028                 ; WDINTE | QUALSTDBY = 10 (12 OSCCLK) | LPM = IDLE
LPM_STBY  .set 0x8029                 ; ... | LPM = STANDBY

        .sect "codestart", 0x33FFF6
        LB      _c_int00

        .sect "text", 0x330000
_c_int00:
        SETC    INTM
        EALLOW
        MOVW    DP, #DP(WDCR)
        MOV     @WDCR, #0x0068        ; watchdog off while the PLL locks
        AND     @PLLSTS, #0xFE7F
        MOV     @PLLCR, #10
pll:    TBIT    @PLLSTS, #0
        SB      pll, NTC
        OR      @PLLSTS, #0x0100
        MOV     @SCSR, #0x0002        ; WDENINT: watchdog interrupt instead of reset (WDOVERRIDE kept)
        MOV     @LPMCR0, #LPM_IDLE
        MOV     @WDKEY, #0x0055
        MOV     @WDKEY, #0x00AA
        MOV     @WDCR, #0x0028        ; WDCHK = 101, enabled, WDCLK = OSCCLK/512
        MOVW    DP, #DP(GPBDIR)
        OR      @GPBDIR, #0x000C            ; GPIO34, GPIO35 outputs
        MOVW    DP, #DP(GPIOLPMSEL)
        MOV     @GPIOLPMSEL, #0x1000  ; GPIO12 wakes from STANDBY
        MOVW    DP, #DP(TINT0VEC)     ; PIE vector table (EALLOW protected)
        MOVL    XAR4, #tint0_isr
        MOVL    @TINT0VEC, XAR4
        MOVL    XAR4, #wake_isr
        MOVL    @WAKEVEC, XAR4
        EDIS
        MOVW    DP, #DP(TIMER0TCR)
        MOV     @TIMER0TCR, #0x0010   ; TSS: stop
        MOV     @TIMER0PRD, #LO16(PERIOD)
        MOV     @TIMER0PRDH, #HI16(PERIOD)
        MOV     @TIMER0TPR, #0
        MOV     @TIMER0TPRH, #0
        MOV     @TIMER0TCR, #0x4020   ; TIE | TRB, run
        MOVW    DP, #DP(PIECTRL)
        MOV     @PIECTRL, #1          ; ENPIE
        OR      @PIEIER1, #0x00C0     ; INT1.7 TINT0, INT1.8 WAKEINT
        MOV     @PIEACK, #0xFFFF
        OR      IER, #0x0001          ; CPU INT1
        EINT
awake:  MOVB    AR6, #2               ; 3 IDLE wake-ups (timer ticks) before STANDBY
tick:   IDLE
        BANZ    tick, AR6--
        EALLOW
        MOVW    DP, #DP(LPMCR0)
        MOV     @LPMCR0, #LPM_STBY
        EDIS
        IDLE                          ; STANDBY until GPIO12 low or the watchdog interrupt
        EALLOW
        MOVW    DP, #DP(LPMCR0)
        MOV     @LPMCR0, #LPM_IDLE
        EDIS
        SB      awake, UNC

tint0_isr:
        MOVW    DP, #DP(GPBTOGGLE)
        MOV     @GPBTOGGLE, #LED
        MOVW    DP, #DP(PIEACK)
        MOV     @PIEACK, #0x0001
        IRET

wake_isr:
        MOVW    DP, #DP(GPBTOGGLE)
        MOV     @GPBTOGGLE, #WAKELED
        MOVW    DP, #DP(PIEACK)
        MOV     @PIEACK, #0x0001
        IRET
