* button_mirror.asm -- TMS320F28232: GPIO34 follows the level of GPIO12 (input with its reset-default pull-up,
* so it reads 1 until something pulls it low).  Polling loop; no PLL setup (SYSCLKOUT = 10 MHz after boot).

WDCR      .set 0x7029
GPADAT    .set 0x6FC0
GPBSET    .set 0x6FCA
GPBCLEAR  .set 0x6FCC
GPBDIR    .set 0x6F9A
LED       .set 0x0004                 ; GPIO34

        .sect "codestart", 0x33FFF6
        LB      _c_int00

        .sect "text", 0x330000
_c_int00:
        EALLOW
        MOVW    DP, #DP(WDCR)
        MOV     @WDCR, #0x0068        ; watchdog off
        MOVW    DP, #DP(GPBDIR)
        OR      @GPBDIR, #LED         ; GPIO34 output (GPIO12 stays an input: mux 0, DIR 0, pull-up on)
        EDIS
        MOVW    DP, #DP(GPADAT)       ; GPADAT, GPBSET and GPBCLEAR share this DP page
loop:   TBIT    @GPADAT, #12          ; TC = GPIO12
        SB      high, TC
        MOV     @GPBCLEAR, #LED
        SB      loop, UNC
high:   MOV     @GPBSET, #LED
        SB      loop, UNC
