* xintf_sram.asm -- TMS320F28232: external 16-bit SRAM on XINTF zone 6 (boards/c2000_xintf_sram.yaml).
* Init: watchdog off, PLL to 100 MHz, PCLKCR3.XINTFENCLK = 1, XINTF pins muxed as TI's InitXintf16Gpio (XD15-0,
* XA19-0, XZCS6, XZCS0/7, XWE0, XR/W), XTIMING6 = lead 1 / active 2 / trail 1, X2TIMING = 0, 16-bit, XREADY ignored;
* XINTCNF2: XTIMCLK = SYSCLKOUT, XCLKOUT = XTIMCLK/2, write buffer 3 deep.  Then writes 256 words (0x1234 + 3i) to
* 0x100000.., reads them back and counts mismatches into 0x0500 (M1).  GPIO34 goes high when the test passed; 0x0501
* holds a done marker 0xD0NE (0xD00E).

WDCR      .set 0x7029
PLLSTS    .set 0x7011
PLLCR     .set 0x7021
PCLKCR3   .set 0x7020
GPAMUX2   .set 0x6F88
GPBMUX1   .set 0x6F96
GPBDIR    .set 0x6F9A
GPCMUX1   .set 0x6FA6
GPCMUX2   .set 0x6FA8
GPBSET    .set 0x6FCA
GPBCLEAR  .set 0x6FCC
XTIMING6  .set 0x0B2C
XINTCNF2  .set 0x0B34
LED       .set 0x0004                 ; GPIO34 = port B bit 2
ERRS      .set 0x0500
DONE      .set 0x0501

        .sect "codestart", 0x33FFF6
        LB      _c_int00

        .sect "text", 0x330000
_c_int00:
        SETC    INTM
        EALLOW
        MOVW    DP, #DP(WDCR)
        MOV     @WDCR, #0x0068        ; watchdog off
        AND     @PLLSTS, #0xFE7F      ; DIVSEL = /4 before changing PLLCR
        MOV     @PLLCR, #10           ; PLL x10
pll:    TBIT    @PLLSTS, #0           ; wait for PLLLOCKS
        SB      pll, NTC
        OR      @PLLSTS, #0x0100      ; DIVSEL = /2 -> SYSCLKOUT = 100 MHz
        OR      @PCLKCR3, #0x1000     ; XINTFENCLK
        MOVW    DP, #DP(GPAMUX2)
        OR      @GPAMUX2+1, #0xFF00   ; GPIO28-31 = XZCS6, XA19, XA18, XA17
        OR      @GPBMUX1, #0xFFC0     ; GPIO35-39 = XR/W, XZCS0, XZCS7, XWE0, XA16
        MOV     @GPBMUX1+1, #0xFFFF   ; GPIO40-47 = XA0/XWE1, XA1-7
        MOV     @GPCMUX1, #0xFFFF     ; GPIO64-79 = XD15-0
        MOV     @GPCMUX1+1, #0xFFFF
        MOV     @GPCMUX2, #0xFFFF     ; GPIO80-87 = XA8-15
        MOVW    DP, #DP(GPBCLEAR)
        MOV     @GPBCLEAR, #LED
        MOVW    DP, #DP(GPBDIR)
        OR      @GPBDIR, #LED         ; GPIO34 output, low
        MOVW    DP, #DP(XTIMING6)
        MOV     @XTIMING6, #0x14A9    ; RDLEAD 1, RDACTIVE 2, RDTRAIL 1, WRLEAD 1, WRACTIVE 2, WRTRAIL 1, USEREADY 0
        MOV     @XTIMING6+1, #0x0003  ; XSIZE = 3 (16-bit), X2TIMING = 0
        MOV     @XINTCNF2, #0x0007    ; WRBUFF = 3, CLKMODE = 1
        MOV     @XINTCNF2+1, #0x0000  ; XTIMCLK = SYSCLKOUT
        NOP                           ; SPRUI07 Figure 14-2: let the configuration settle (8 cycles)
        NOP
        NOP
        NOP
        NOP
        NOP
        NOP
        NOP
        EDIS
* write 256 words
        MOVL    XAR4, #0x100000
        MOV     @AL, #0x1234
        MOV     @AR1, #255
wr:     MOV     *XAR4++, AL
        ADDB    AL, #3
        BANZ    wr, AR1--
* read back and count mismatches
        MOVL    XAR4, #0x100000
        MOV     @AL, #0x1234
        MOV     @AH, #0
        MOV     @AR1, #255
rd:     CMP     AL, *XAR4++
        SB      ok, EQ
        ADDB    AH, #1
ok:     ADDB    AL, #3
        BANZ    rd, AR1--
        MOVW    DP, #DP(ERRS)
        MOV     @ERRS, AH
        MOV     @DONE, #0xD00E
        CMP     @AH, #0
        SB      halt, NEQ
        MOVW    DP, #DP(GPBSET)
        MOV     @GPBSET, #LED         ; pass: GPIO34 high
halt:   SB      halt, UNC
