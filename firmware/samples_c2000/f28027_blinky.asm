* f28027_blinky.asm -- TMS320F28027 (LAUNCHXL-F28027): the four LaunchPad LEDs (GPIO0-3) count in binary, one step
* every 1 ms, after a clock setup in the order of TI's InitSysCtrl (SPRUI09A 1.3.2, Figure 1-23):
*   watchdog off; Device_cal() through its fixed pointer 0x3D7C80 with the ADC clock on; INTOSC1 as OSCCLK with
*   INTOSC2 / crystal / XCLKIN off (CLKCTL = 0x6400); PLLSTS.DIVSEL = 0 before PLLCR; MCLKOFF = 1 while locking;
*   PLLCR = 12; wait for PLLLOCKS (PLLLOCKPRD = 65535 OSCCLK = 6.55 ms at reset); MCLKOFF = 0; DIVSEL = 2:
*   SYSCLKOUT = 10 MHz x 12 / 2 = 60 MHz.
* Boot: GetMode (GPIO34/37 pulled up) -> 0x3F7FF6 holds LB start.

WDCR      .set 0x7029
PLLSTS    .set 0x7011
CLKCTL    .set 0x7012
PCLKCR0   .set 0x701C
PLLCR     .set 0x7021
GPAMUX1   .set 0x6F86
GPADIR    .set 0x6F8A
GPADAT    .set 0x6FC0
GPACLEAR  .set 0x6FC4
Device_cal .set 0x3D7C80

        .sect "codestart", 0x3F7FF6
        LB      start

        .sect "text", 0x3F0000
start:  SETC    INTM
        EALLOW
        MOVW    DP, #DP(WDCR)
        MOV     @WDCR, #0x0068        ; WDCHK = 101, WDDIS = 1
        OR      @PCLKCR0, #0x0008     ; ADCENCLK on for Device_cal
        LCR     Device_cal
        EALLOW
        MOVW    DP, #DP(PCLKCR0)
        AND     @PCLKCR0, #0xFFF7
        MOV     @CLKCTL, #0x6400      ; INTOSC1 = OSCCLK, INTOSC2/XTAL/XCLKIN off
        AND     @PLLSTS, #0xFE7F      ; DIVSEL = 0 (/4) before PLLCR
        OR      @PLLSTS, #0x0040      ; MCLKOFF = 1 while the PLL locks
        MOV     @PLLCR, #12
pll:    TBIT    @PLLSTS, #0           ; PLLLOCKS
        SB      pll, NTC
        AND     @PLLSTS, #0xFFBF      ; MCLKOFF = 0
        OR      @PLLSTS, #0x0100      ; DIVSEL = 2: 60 MHz
        MOVW    DP, #DP(GPAMUX1)
        AND     @GPAMUX1, #0xFF00     ; GPIO0-3 = GPIO
        MOVW    DP, #DP(GPACLEAR)
        MOV     @GPACLEAR, #0x000F
        MOVW    DP, #DP(GPADIR)
        OR      @GPADIR, #0x000F      ; GPIO0-3 outputs
        EDIS
        MOVB    XAR2, #0
loop:   MOVW    DP, #DP(GPADAT)
        MOV     AL, @AR2
        AND     AL, @AL, #0x000F
        MOV     @GPADAT, AL
        ADDB    XAR2, #1
        MOV     @AL, #29997           ; 2 x 29997 + 6 = 60000 cycles = 1 ms at 60 MHz
dly:    ADDB    AL, #-1
        SB      dly, NEQ
        SB      loop, UNC
