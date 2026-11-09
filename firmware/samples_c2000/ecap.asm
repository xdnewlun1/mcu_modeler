* ecap.asm -- TMS320F28232: eCAP1 measures the ePWM1A signal.  ePWM1 drives a 20 kHz, 30 % PWM on GPIO0 (EPWM1A):
* up-count, TBPRD = 4999 at TBCLK = SYSCLKOUT = 100 MHz (PLL x10 /2), set at CTR=ZERO, clear at CTR=CMPA = 1500.
* eCAP1 on GPIO24 (GPAMUX2 MUX1) captures in continuous mode: event 1 on the rising edge with a counter reset
* (CTRRST1), event 2 on the falling edge, wrap after event 2.  So CAP1 = the period (rising to rising) and CAP2 =
* the high time, in SYSCLKOUT cycles.  The ECAP1_INT ISR (PIE 4.1, on CEVT2) copies them to ecap_period /
* ecap_high in M0 RAM and counts ecap_count.  Wire GPIO0 to GPIO24 (boards/c2000_ecap_loopback.yaml); expected
* ecap_period = 5000 (50 us), ecap_high = 1500 (15 us).  The main loop sleeps in IDLE.

WDCR      .set 0x7029
PLLSTS    .set 0x7011
PLLCR     .set 0x7021
PCLKCR0   .set 0x701C
PCLKCR1   .set 0x701D
GPAMUX1   .set 0x6F86
GPAMUX2H  .set 0x6F89                 ; high word of GPAMUX2: GPIO24-31
EPWM1     .set 0x6800
TBCTL     .set EPWM1 + 0x00
TBPRD     .set EPWM1 + 0x05
CMPCTL    .set EPWM1 + 0x07
CMPA      .set EPWM1 + 0x09
AQCTLA    .set EPWM1 + 0x0B
ECAP1     .set 0x6A00
CAP1      .set ECAP1 + 0x04
CAP2      .set ECAP1 + 0x06
ECCTL1    .set ECAP1 + 0x14
ECCTL2    .set ECAP1 + 0x15
ECEINT    .set ECAP1 + 0x16
ECCLR     .set ECAP1 + 0x18
PIECTRL   .set 0x0CE0
PIEACK    .set 0x0CE1
PIEIER4   .set 0x0CE8
ECAP1VEC  .set 0x0D70                 ; PIE vector 56 = INT4.1 (ECAP1_INT)

        .sect "ramvars", 0x0100       ; M0: results (symbols in the .c28img)
ecap_period:  .long 0                 ; CAP1: rising -> rising, SYSCLKOUT cycles
ecap_high:    .long 0                 ; CAP2: rising -> falling
ecap_count:   .word 0                 ; ECAP1_INT count

        .sect "codestart", 0x33FFF6
        LB      _c_int00

        .sect "text", 0x330000
_c_int00:
        SETC    INTM
        EALLOW
        MOVW    DP, #DP(WDCR)
        MOV     @WDCR, #0x0068        ; watchdog off
        AND     @PLLSTS, #0xFE7F
        MOV     @PLLCR, #10
pll:    TBIT    @PLLSTS, #0
        SB      pll, NTC
        OR      @PLLSTS, #0x0100      ; DIVSEL /2: 100 MHz
        AND     @PCLKCR0, #0xFFFB     ; TBCLKSYNC = 0 while the modules are configured
        OR      @PCLKCR1, #0x0101     ; EPWM1ENCLK, ECAP1ENCLK
        MOVW    DP, #DP(GPAMUX1)
        OR      @GPAMUX1, #0x0001     ; GPIO0 = EPWM1A
        OR      @GPAMUX2H, #0x0001    ; GPIO24 = ECAP1
        MOVW    DP, #DP(ECAP1VEC)     ; PIE vector table (EALLOW protected)
        MOVL    XAR4, #ecap1_isr
        MOVL    @ECAP1VEC, XAR4
        EDIS
        MOVW    DP, #DP(ecap_count)
        MOV     @ecap_count, #0
        MOVW    DP, #DP(ECCTL1)       ; eCAP1 (not EALLOW protected), the order TI recommends:
        MOV     @ECEINT, #0x0000      ; interrupts off
        MOV     @ECCTL2, #0x0082      ; counter stopped, continuous, wrap after CEVT2, SYNCO off
        MOV     @ECCTL1, #0x0106      ; CAPLDEN, CAP1 rising + CTRRST1, CAP2 falling
        MOV     @ECCLR, #0x00FF       ; clear spurious flags
        MOV     @ECEINT, #0x0004      ; CEVT2 interrupt
        OR      @ECCTL2, #0x0010      ; TSCTRSTOP = 1: run
        MOVW    DP, #DP(TBCTL)
        MOV     @TBPRD, #4999
        MOV     @CMPA, #1500
        MOV     @CMPCTL, #0x0000      ; shadow CMPA, load at CTR=0
        MOV     @AQCTLA, #0x0012      ; ZRO: set, CAU: clear
        MOV     @TBCTL, #0x0030       ; up-count, SYNCOSEL off, TBCLK = SYSCLKOUT
        MOVW    DP, #DP(PIECTRL)
        MOV     @PIECTRL, #1          ; ENPIE
        OR      @PIEIER4, #0x0001     ; INT4.1
        MOV     @PIEACK, #0xFFFF
        OR      IER, #0x0008          ; CPU INT4
        EINT
        EALLOW
        MOVW    DP, #DP(PCLKCR0)
        OR      @PCLKCR0, #0x0004     ; TBCLKSYNC: start the ePWM time base
        EDIS
idle:   IDLE
        SB      idle, UNC

ecap1_isr:                            ; ACC and DP are part of the automatic context save
        MOVW    DP, #DP(CAP1)
        MOVL    ACC, @CAP1
        MOVW    DP, #DP(ecap_period)
        MOVL    @ecap_period, ACC
        MOVW    DP, #DP(CAP2)
        MOVL    ACC, @CAP2
        MOVW    DP, #DP(ecap_high)
        MOVL    @ecap_high, ACC
        INC     @ecap_count
        MOVW    DP, #DP(ECCLR)
        MOV     @ECCLR, #0x0005       ; CEVT2 | INT
        MOVW    DP, #DP(PIEACK)
        MOV     @PIEACK, #0x0008      ; acknowledge group 4
        IRET
