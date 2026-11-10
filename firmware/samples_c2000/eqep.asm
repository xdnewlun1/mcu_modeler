* eqep.asm -- TMS320F28232: eQEP1 decodes a quadrature encoder on GPIO20 (EQEP1A), GPIO21 (EQEP1B) and GPIO23
* (EQEP1I, index), GPAMUX2 MUX1.  The PLL is set to 100 MHz (x10 /2 from the 20 MHz crystal) first.
*   QDECCTL = 0: quadrature count mode (x4), no inversion.
*   QPOSMAX = 399: one revolution of a 100-line encoder (400 quadrature edges).
*   QEPCTL  = PCRM 00 (the counter is reset at every index marker: 0 forward / QPOSMAX reverse), QPEN, QCLM = 1
*             (latch QPOSCNT / QCTMR / QCPRD into QPOSLAT / QCTMRLAT / QCPRDLAT at each unit time out), UTE.
*   QUPRD   = 999999: unit time out every 1000000 SYSCLKOUT cycles = 10 ms (100 Hz).
*   QCAPCTL = CEN, CCPS = 7 (capture clock SYSCLKOUT / 128), UPPS = 2 (unit position event every 4 edges).
* The EQEP1_INT ISR (PIE 5.1, on UTO) stores QPOSLAT in eqep_pos (the shaft angle in edges since the index),
* QEPSTS.QDF in eqep_dir (1 = forward / clockwise), QPOSILAT in eqep_ilat (the count at the last index marker),
* QCPRDLAT in eqep_cprd (capture-clock ticks per 4 edges: the speed) and counts eqep_count.  The main loop idles.
* Board: boards/c2000_eqep_encoder.yaml (a quadrature_encoder part, 100 lines, 1500 rpm = 10 kHz edge rate).

WDCR      .set 0x7029
PLLSTS    .set 0x7011
PLLCR     .set 0x7021
PCLKCR1   .set 0x701D
GPAMUX2   .set 0x6F88                 ; low word of GPAMUX2: GPIO16-23
EQEP1     .set 0x6B00
QPOSMAX   .set EQEP1 + 0x04
QPOSMAXH  .set EQEP1 + 0x05
QPOSILAT  .set EQEP1 + 0x08
QPOSLAT   .set EQEP1 + 0x0C
QUPRD     .set EQEP1 + 0x10
QUPRDH    .set EQEP1 + 0x11
QDECCTL   .set EQEP1 + 0x14
QEPCTL    .set EQEP1 + 0x15
QCAPCTL   .set EQEP1 + 0x16
QEINT     .set EQEP1 + 0x18
QCLR      .set EQEP1 + 0x1A
QEPSTS    .set EQEP1 + 0x1C
QCPRDLAT  .set EQEP1 + 0x20
PIECTRL   .set 0x0CE0
PIEACK    .set 0x0CE1
PIEIER5   .set 0x0CEA
EQEP1VEC  .set 0x0D80                 ; PIE vector 64 = INT5.1 (EQEP1_INT)

        .sect "ramvars", 0x0100       ; M0: results (symbols in the .c28img)
eqep_pos:     .long 0                 ; QPOSLAT at the last unit time out
eqep_ilat:    .long 0                 ; QPOSILAT (latched at the index marker)
eqep_dir:     .word 0                 ; QEPSTS.QDF: 1 forward, 0 reverse
eqep_cprd:    .word 0                 ; QCPRDLAT: SYSCLKOUT/128 ticks per unit position (4 edges)
eqep_count:   .word 0                 ; unit time outs seen

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
        OR      @PCLKCR1, #0x4000     ; EQEP1ENCLK
        MOVW    DP, #DP(GPAMUX2)
        OR      @GPAMUX2, #0x4500     ; GPIO20 = EQEP1A, GPIO21 = EQEP1B, GPIO23 = EQEP1I
        MOVW    DP, #DP(EQEP1VEC)     ; PIE vector table (EALLOW protected)
        MOVL    XAR4, #eqep1_isr
        MOVL    @EQEP1VEC, XAR4
        EDIS
        MOVW    DP, #DP(eqep_count)
        MOV     @eqep_count, #0
        MOVW    DP, #DP(QDECCTL)      ; eQEP1 (not EALLOW protected)
        MOV     @QDECCTL, #0x0000     ; quadrature count mode
        MOV     @QUPRD, #0x423F       ; 999999 = 0x000F423F: 100 Hz unit timer
        MOV     @QUPRDH, #0x000F
        MOV     @QPOSMAX, #399
        MOV     @QPOSMAXH, #0
        MOV     @QCAPCTL, #0x0072     ; CCPS /128, UPPS /4 (configured with CEN = 0) ...
        MOV     @QCAPCTL, #0x8072     ; ... then enabled
        MOV     @QCLR, #0x0FFF        ; clear spurious flags
        MOV     @QEINT, #0x0800       ; UTO interrupt
        MOV     @QEPCTL, #0x000E      ; PCRM 00, QPEN, QCLM, UTE
        MOVW    DP, #DP(PIECTRL)
        MOV     @PIECTRL, #1          ; ENPIE
        OR      @PIEIER5, #0x0001     ; INT5.1
        MOV     @PIEACK, #0xFFFF
        OR      IER, #0x0010          ; CPU INT5
        EINT
idle:   IDLE
        SB      idle, UNC

eqep1_isr:                            ; ACC and DP are part of the automatic context save
        MOVW    DP, #DP(QPOSLAT)
        MOVL    ACC, @QPOSLAT
        MOVW    DP, #DP(eqep_pos)
        MOVL    @eqep_pos, ACC
        MOVW    DP, #DP(QPOSILAT)
        MOVL    ACC, @QPOSILAT
        MOVW    DP, #DP(eqep_ilat)
        MOVL    @eqep_ilat, ACC
        MOVW    DP, #DP(QEPSTS)
        MOV     AL, @QEPSTS
        LSR     AL, #5
        ANDB    AL, #1                ; QDF
        MOVW    DP, #DP(eqep_dir)
        MOV     @eqep_dir, AL
        MOVW    DP, #DP(QCPRDLAT)
        MOV     AL, @QCPRDLAT
        MOVW    DP, #DP(eqep_cprd)
        MOV     @eqep_cprd, AL
        INC     @eqep_count
        MOVW    DP, #DP(QCLR)
        MOV     @QCLR, #0x0801        ; UTO | INT
        MOVW    DP, #DP(PIEACK)
        MOV     @PIEACK, #0x0010      ; acknowledge group 5
        IRET
