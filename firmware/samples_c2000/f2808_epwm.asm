* f2808_epwm.asm -- TMS320F2808: ePWM1 drives a 20 kHz complementary pair with dead band on GPIO0 (EPWM1A) and GPIO1
* (EPWM1B): up-down count, TBPRD = 2500 at TBCLK = SYSCLKOUT = 100 MHz, CMPA = 1750 (CAU set, CAD clear: A high
* 30 %), dead band active high complementary, RED = FED = 50 TBCLK (0.5 us).  Time bases start together with
* PCLKCR0.TBCLKSYNC (bit 2), as in TI's 280x InitEPwm examples.  The main loop sleeps in IDLE.

WDCR      .set 0x7029
PLLSTS    .set 0x7011
PLLCR     .set 0x7021
PCLKCR0   .set 0x701C
PCLKCR1   .set 0x701D
GPAMUX1   .set 0x6F86
EPWM1     .set 0x6800
TBCTL     .set EPWM1 + 0x00
TBPRD     .set EPWM1 + 0x05
CMPCTL    .set EPWM1 + 0x07
CMPA      .set EPWM1 + 0x09
AQCTLA    .set EPWM1 + 0x0B
DBCTL     .set EPWM1 + 0x0F
DBRED     .set EPWM1 + 0x10
DBFED     .set EPWM1 + 0x11

        .sect "codestart", 0x3F7FF6
        LB      _c_int00

        .sect "text", 0x3E8000
_c_int00:
        SETC    INTM
        EALLOW
        MOVW    DP, #DP(WDCR)
        MOV     @WDCR, #0x0068        ; watchdog off
        AND     @PLLSTS, #0xFFFD      ; CLKINDIV = 0
        MOV     @PLLCR, #10
pll:    TBIT    @PLLSTS, #0
        SB      pll, NTC              ; 100 MHz
        AND     @PCLKCR0, #0xFFFB     ; TBCLKSYNC = 0 while the modules are configured
        OR      @PCLKCR1, #0x0001     ; EPWM1ENCLK
        MOVW    DP, #DP(GPAMUX1)
        OR      @GPAMUX1, #0x0005     ; GPIO0 = EPWM1A, GPIO1 = EPWM1B (mux 1)
        EDIS
        MOVW    DP, #DP(TBCTL)
        MOV     @TBPRD, #2500
        MOV     @CMPA, #1750
        MOV     @CMPCTL, #0x0000      ; shadow CMPA/CMPB, load at CTR=0
        MOV     @AQCTLA, #0x0060      ; CAU: set, CAD: clear
        MOV     @DBCTL, #0x000B       ; OUT_MODE = 11, POLSEL = 10 (active high complementary)
        MOV     @DBRED, #50
        MOV     @DBFED, #50
        MOV     @TBCTL, #0x0032       ; up-down, SYNCOSEL off, TBCLK = SYSCLKOUT
        EALLOW
        MOVW    DP, #DP(PCLKCR0)
        OR      @PCLKCR0, #0x0004     ; TBCLKSYNC: start the time base
        EDIS
idle:   IDLE
        SB      idle, UNC
