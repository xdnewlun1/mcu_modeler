* mcbsp.asm -- TMS320F28232: McBSP-A digital loopback (SPCR1.DLB) fed and drained by the DMA (SPRUI07 ch. 8/12).
*   100 MHz SYSCLKOUT, LSPCLK 25 MHz; McBSP-A CLKG = LSPCLK / 5 = 5 MHz, 16-bit single-phase frames, 1-bit data
*   delay, frame sync per DXR-to-XSR copy (FSXM = 1, FSGM = 0, FRST = 1), CLKX/CLKR/FSX/FSR internal.
*   GPIO20-23 are muxed to MDXA / MDRA / MCLKXA / MFSXA, so the transmit waveforms show on the pins (in loopback
*   MDRA is ignored).
*   DMA CH1: PERINTSEL 14 (MXEVTA, XRDY 0 -> 1) moves tx_buf (L4, 0xC000) to DXR1, one word per burst.
*   DMA CH2: PERINTSEL 15 (MREVTA, RRDY 0 -> 1) moves DRR1 to rx_buf (L5, 0xD000).  8 words each.
*   Taking the transmitter out of reset (XRST = 1, XRDY 0 -> 1) fires the first MXEVTA.
* Results (symbols): mcbsp_err = words that differ, mcbsp_done = 1 when CH2 has finished and the buffers were
* compared.  The main loop then sleeps in IDLE.

WDCR      .set 0x7029
PLLSTS    .set 0x7011
PLLCR     .set 0x7021
PCLKCR0   .set 0x701C
PCLKCR3   .set 0x7020
GPAMUX2   .set 0x6F88                 ; low word: GPIO16-23
DRR1      .set 0x5001
DXR1      .set 0x5003
SPCR2     .set 0x5004
SPCR1     .set 0x5005
RCR2      .set 0x5006
RCR1      .set 0x5007
XCR2      .set 0x5008
XCR1      .set 0x5009
SRGR2     .set 0x500A
SRGR1     .set 0x500B
MCR2      .set 0x500C
MCR1      .set 0x500D
PCR       .set 0x5012
MFFINT    .set 0x5023
DMACTRL   .set 0x1000
CH1MODE   .set 0x1020                 ; channel n at 0x1000 + 0x20 n
CH1CTL    .set 0x1021
CH1BSIZE  .set 0x1022
CH1TSIZE  .set 0x1026
CH1STS    .set 0x1028                 ; SRC_TRANSFER_STEP
CH1DTS    .set 0x1029                 ; DST_TRANSFER_STEP
CH1SWRAP  .set 0x102A
CH1DWRAP  .set 0x102D
CH1SBEGS  .set 0x1030                 ; SRC_BEG_ADDR_SHADOW (32-bit)
CH1SADDS  .set 0x1032                 ; SRC_ADDR_SHADOW
CH1DBEGS  .set 0x1038                 ; DST_BEG_ADDR_SHADOW
CH1DADDS  .set 0x103A                 ; DST_ADDR_SHADOW
CH2MODE   .set 0x1040
CH2CTL    .set 0x1041
CH2BSIZE  .set 0x1042
CH2TSIZE  .set 0x1046
CH2STS    .set 0x1048
CH2DTS    .set 0x1049
CH2SWRAP  .set 0x104A
CH2DWRAP  .set 0x104D
CH2SBEGS  .set 0x1050
CH2SADDS  .set 0x1052
CH2DBEGS  .set 0x1058
CH2DADDS  .set 0x105A
NWORDS    .set 8

        .sect "ramvars", 0x0100       ; M0: results
mcbsp_err:  .word 0
mcbsp_done: .word 0

        .sect "txbuf", 0xC000         ; L4 SARAM (on the DMA bus)
tx_buf: .word 0x1234, 0xABCD, 0x0000, 0xFFFF, 0x5A5A, 0x8001, 0x0F0F, 0xC0DE

        .sect "rxbuf", 0xD000         ; L5 SARAM
rx_buf: .word 0, 0, 0, 0, 0, 0, 0, 0

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
        OR      @PLLSTS, #0x0100      ; 20 MHz x 10 / 2 = 100 MHz, LSPCLK = 25 MHz
        OR      @PCLKCR0, #0x1000     ; MCBSPAENCLK
        OR      @PCLKCR3, #0x0800     ; DMAENCLK
        MOVW    DP, #DP(GPAMUX2)
        OR      @GPAMUX2, #0xAA00     ; GPIO20-23 -> MDXA / MDRA / MCLKXA / MFSXA (mux 2)
* McBSP-A in reset, configured (SPRUI07 12.10.2.3)
        MOVW    DP, #DP(SPCR2)
        MOV     @SPCR2, #0            ; XRST = GRST = FRST = 0
        MOV     @SPCR1, #0x8000       ; RRST = 0, DLB = 1, RJUST = 0, CLKSTP = 0
        MOV     @RCR2, #0x0001        ; single phase, RDATDLY = 1
        MOV     @RCR1, #0x0040        ; 1 word of 16 bits
        MOV     @XCR2, #0x0001
        MOV     @XCR1, #0x0040
        MOV     @SRGR2, #0x2000       ; CLKSM = 1 (LSPCLK), FSGM = 0
        MOV     @SRGR1, #0x0004       ; FWID = 0, CLKGDV = 4 -> 5 MHz
        MOV     @MCR2, #0
        MOV     @MCR1, #0
        MOV     @PCR, #0x0F00         ; FSXM FSRM CLKXM CLKRM: everything from the sample rate generator
        MOV     @MFFINT, #0           ; no CPU interrupts (the DMA takes the events)
        OR      @SPCR2, #0x0040       ; GRST = 1
        RPT     #20                   ; > 2 CLKG cycles
     || NOP
        OR      @SPCR1, #0x0001       ; RRST = 1
* DMA channels
        MOVW    DP, #DP(DMACTRL)
        MOV     @DMACTRL, #1          ; HARDRESET
        NOP
        MOV     @CH1BSIZE, #0         ; 1 word per burst
        MOV     @CH1TSIZE, #NWORDS-1
        MOV     @CH1STS, #1
        MOV     @CH1DTS, #0
        MOV     @CH1SWRAP, #0xFFFF
        MOV     @CH1DWRAP, #0xFFFF
        MOVL    XAR4, #tx_buf
        MOVL    @CH1SBEGS, XAR4
        MOVL    @CH1SADDS, XAR4
        MOVL    XAR4, #DXR1
        MOVL    @CH1DBEGS, XAR4
        MOVL    @CH1DADDS, XAR4
        MOV     @CH1MODE, #0x010E     ; PERINTE, PERINTSEL 14 = MXEVTA
        MOV     @CH1CTL, #0x0090      ; PERINTCLR, ERRCLR
        MOV     @CH1CTL, #0x0001      ; RUN
        MOVW    DP, #DP(CH2MODE)
        MOV     @CH2BSIZE, #0
        MOV     @CH2TSIZE, #NWORDS-1
        MOV     @CH2STS, #0
        MOV     @CH2DTS, #1
        MOV     @CH2SWRAP, #0xFFFF
        MOV     @CH2DWRAP, #0xFFFF
        MOVL    XAR4, #DRR1
        MOVL    @CH2SBEGS, XAR4
        MOVL    @CH2SADDS, XAR4
        MOVL    XAR4, #rx_buf
        MOVL    @CH2DBEGS, XAR4
        MOVL    @CH2DADDS, XAR4
        MOV     @CH2MODE, #0x010F     ; PERINTE, PERINTSEL 15 = MREVTA
        MOV     @CH2CTL, #0x0090
        MOV     @CH2CTL, #0x0001
        EDIS
* start: transmitter out of reset (XRDY 0 -> 1 = first MXEVTA), frame-sync logic on
        MOVW    DP, #DP(SPCR2)
        OR      @SPCR2, #0x0081       ; XRST = 1, FRST = 1
* wait for CH2 to finish (RUNSTS = CONTROL bit 13 clears after the last burst of a one-shot transfer)
        MOVW    DP, #DP(CH2CTL)
wait:   TBIT    @CH2CTL, #13
        SB      wait, TC
* compare
        MOVL    XAR4, #tx_buf
        MOVL    XAR5, #rx_buf
        MOVB    XAR0, #NWORDS-1
        MOVB    AH, #0
cmp:    MOV     AL, *XAR4++
        CMP     AL, *XAR5++
        SB      same, EQ
        ADDB    AH, #1
same:   BANZ    cmp, AR0--
        MOVW    DP, #DP(mcbsp_err)
        MOV     @mcbsp_err, AH
        MOV     @mcbsp_done, #1
idle:   IDLE
        SB      idle, UNC
