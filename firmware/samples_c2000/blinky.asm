* blinky.asm -- TMS320F28232: GPIO34 (LED on TI's controlCARD) toggles every ~1 ms from a delay loop.
* Boot: the boot ROM's "jump to flash" lands on 0x33FFF6, which holds LB _c_int00.
* Init: watchdog off, PLL to 100 MHz (20 MHz OSCCLK x 10 / 2), GPIO34 = output via GPBMUX1/GPBDIR (EALLOW).

WDCR      .set 0x7029
PLLSTS    .set 0x7011
PLLCR     .set 0x7021
GPBMUX1   .set 0x6F96
GPBDIR    .set 0x6F9A
GPBCLEAR  .set 0x6FCC
GPBTOGGLE .set 0x6FCE
LED       .set 0x0004                 ; GPIO34 = port B bit 2

        .sect "codestart", 0x33FFF6
        LB      _c_int00

        .sect "text", 0x330000
_c_int00:
        SETC    INTM
        EALLOW
        MOVW    DP, #DP(WDCR)
        MOV     @WDCR, #0x0068        ; WDCHK = 101, WDDIS = 1: watchdog off
        AND     @PLLSTS, #0xFE7F      ; DIVSEL = /4 before changing PLLCR
        MOV     @PLLCR, #10           ; PLL x10
pll:    TBIT    @PLLSTS, #0           ; wait for PLLLOCKS
        SB      pll, NTC
        OR      @PLLSTS, #0x0100      ; DIVSEL = /2 -> SYSCLKOUT = 100 MHz
        MOVW    DP, #DP(GPBMUX1)
        AND     @GPBMUX1, #0xFFCF     ; GPIO34 mux = 0 (GPIO)
        MOVW    DP, #DP(GPBCLEAR)
        MOV     @GPBCLEAR, #LED       ; latch low before enabling the output
        MOVW    DP, #DP(GPBDIR)
        OR      @GPBDIR, #LED         ; GPIO34 output
        EDIS
        MOVW    DP, #DP(GPBTOGGLE)
loop:   MOV     @GPBTOGGLE, #LED
        MOV     @AL, #50000           ; 50000 x 2 instructions = 100000 cycles = 1 ms at 100 MHz
dly:    ADDB    AL, #-1
        SB      dly, NEQ
        SB      loop, UNC
