* f2808_adc.asm -- TMS320F2808: ePWM1 starts an ADC conversion every 100 us; the SEQ1INT interrupt stores the results.
*   ePWM1: up count, TBPRD = 9999, TBCLK = SYSCLKOUT = 100 MHz -> 10 kHz; ETSEL.SOCAEN with SOCASEL = CTR=0,
*   ETPS.SOCAPRD = 1 (every event).  ADC (SPRU716, SPRS230P 8.15): bandgap/reference and core powered up
*   (ADCTRL3 = 0x00E4: ADCBGRFDN = 11, ADCPWDN = 1, ADCCLKPS = 2 -> ADCCLK = HSPCLK 50 MHz / 4 = 12.5 MHz, the
*   F2808 maximum), then 5 ms settling as in TI's InitAdc (td(BGR)), cascaded sequencer, MAX_CONV1 = 1:
*   CONV00 = ADCINA0, CONV01 = ADCINA1, ACQ_PS = 1; ADCTRL2 = EPWM_SOCA_SEQ1 | INT_ENA_SEQ1.
*   seq1_isr (PIE 1.1, vector 32): copies ADCRESULT0/1 from the right-justified mirror (0x0B00) to adc_a0/adc_a1,
*   counts adc_count, resets SEQ1 (RST_SEQ1), clears INT_SEQ1 (ADCST.INT_SEQ1_CLR), toggles GPIO34, PIEACK group 1.
* Drive the inputs with: chipem run ... --chip tms320f2808 --adc ADCINA0=1234 --adc ADCINA1=4000

WDCR      .set 0x7029
PLLSTS    .set 0x7011
PLLCR     .set 0x7021
PCLKCR0   .set 0x701C
PCLKCR1   .set 0x701D
GPBDIR    .set 0x6F9A
GPBTOGGLE .set 0x6FCE
PIECTRL   .set 0x0CE0
PIEACK    .set 0x0CE1
PIEIER1   .set 0x0CE2
SEQ1VEC   .set 0x0D40                 ; PIE vector 32 = INT1.1 SEQ1INT
ADCRES0M  .set 0x0B00                 ; ADCRESULT0 mirror (right-justified)
ADCRES1M  .set 0x0B01
ADCTRL1   .set 0x7100
ADCTRL2   .set 0x7101
ADCMAXCONV .set 0x7102
ADCCHSELSEQ1 .set 0x7103
ADCTRL3   .set 0x7118
ADCST     .set 0x7119
EPWM1     .set 0x6800
TBCTL     .set EPWM1 + 0x00
TBPRD     .set EPWM1 + 0x05
ETSEL     .set EPWM1 + 0x19
ETPS      .set EPWM1 + 0x1A
LED       .set 0x0004

        .sect "ramvars", 0x0100       ; M0: results (symbols in the .c28img)
adc_a0:    .word 0
adc_a1:    .word 0
adc_count: .word 0

        .sect "codestart", 0x3F7FF6
        LB      _c_int00

        .sect "text", 0x3E8000
_c_int00:
        SETC    INTM
        EALLOW
        MOVW    DP, #DP(WDCR)
        MOV     @WDCR, #0x0068
        AND     @PLLSTS, #0xFFFD
        MOV     @PLLCR, #10
pll:    TBIT    @PLLSTS, #0
        SB      pll, NTC              ; 100 MHz, HSPCLK = 50 MHz (HISPCP reset /2)
        AND     @PCLKCR0, #0xFFFB     ; TBCLKSYNC = 0
        OR      @PCLKCR0, #0x0008     ; ADCENCLK
        OR      @PCLKCR1, #0x0001     ; EPWM1ENCLK
        MOVW    DP, #DP(GPBDIR)
        OR      @GPBDIR, #LED
        MOVW    DP, #DP(SEQ1VEC)
        MOVL    XAR4, #seq1_isr
        MOVL    @SEQ1VEC, XAR4
        EDIS
* ADC power-up and 5 ms settling (5 x 50000 x 2 cycles at 100 MHz)
        MOVW    DP, #DP(ADCTRL3)
        MOV     @ADCTRL3, #0x00E4     ; ADCBGRFDN = 11, ADCPWDN = 1, ADCCLKPS = 2
        MOV     @AR1, #4
outer:  MOV     @AL, #50000
inner:  ADDB    AL, #-1
        SB      inner, NEQ
        BANZ    outer, AR1--
        MOVW    DP, #DP(ADCTRL1)
        MOV     @ADCTRL1, #0x0110     ; ACQ_PS = 1, SEQ_CASC = 1
        MOV     @ADCMAXCONV, #0x0001  ; two conversions
        MOV     @ADCCHSELSEQ1, #0x0010 ; CONV00 = A0, CONV01 = A1
        MOV     @ADCTRL2, #0x0900     ; EPWM_SOCA_SEQ1 | INT_ENA_SEQ1
* ePWM1: 10 kHz SOCA
        MOVW    DP, #DP(TBCTL)
        MOV     @TBPRD, #9999
        MOV     @ETSEL, #0x0900       ; SOCAEN, SOCASEL = 001 (CTR = 0)
        MOV     @ETPS, #0x0100        ; SOCAPRD = 1
        MOV     @TBCTL, #0x0030       ; up count, SYNCOSEL off, TBCLK = SYSCLKOUT
        MOVW    DP, #DP(PIECTRL)
        MOV     @PIECTRL, #1          ; ENPIE
        OR      @PIEIER1, #0x0001     ; INT1.1 SEQ1INT
        MOV     @PIEACK, #0xFFFF
        OR      IER, #0x0001
        EALLOW
        MOVW    DP, #DP(PCLKCR0)
        OR      @PCLKCR0, #0x0004     ; TBCLKSYNC: start ePWM1
        EDIS
        EINT
idle:   IDLE
        SB      idle, UNC

seq1_isr:
        MOVW    DP, #DP(ADCRES0M)
        MOV     AL, @ADCRES0M
        MOV     AH, @ADCRES1M
        MOVW    DP, #DP(adc_a0)
        MOV     @adc_a0, AL
        MOV     @adc_a1, AH
        INC     @adc_count
        MOVW    DP, #DP(ADCTRL2)
        OR      @ADCTRL2, #0x4000     ; RST_SEQ1
        MOV     @ADCST, #0x0010       ; INT_SEQ1_CLR
        MOVW    DP, #DP(GPBTOGGLE)
        MOV     @GPBTOGGLE, #LED
        MOVW    DP, #DP(PIEACK)
        MOV     @PIEACK, #0x0001
        IRET
