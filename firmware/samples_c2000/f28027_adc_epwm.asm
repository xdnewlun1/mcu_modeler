* f28027_adc_epwm.asm -- TMS320F28027: ePWM1 (10 kHz, 25 % duty on EPWM1A = GPIO0) starts the type-3 ADC through
* SOCA at CTR = PRD; SOC0 converts ADCINA1, SOC1 ADCINB1 (TRIGSEL 5 = ePWM1 SOCA, ACQPS 6 = 7 ADCCLK, SPRUI09A
* 6.2); ADCINT1 at EOC1 (INTSEL1N2 = 0x0021, late pulse: ADCCTL1.INTPULSEPOS = 1) enters adc_isr through PIE 1.1
* (vector 32 at 0x0D40), which copies ADCRESULT0/1 (0x0B00, right-justified) to adc_a1 / adc_b1 (0x8000 / 0x8001),
* counts adc_count (0x8002), toggles GPIO1, clears ADCINTFLG.ADCINT1 and acknowledges PIE group 1.
* ADC power-up as SPRUI09 6.8: ADCPWDN | ADCBGPWD | ADCREFPWD | ADCENABLE, then 1 ms before the first conversion.
* Clock setup as f28027_blinky.asm (60 MHz after the 6.55 ms PLL lock).
* chipem run firmware/samples_c2000/build/f28027_adc_epwm.c28img --chip tms320f28027 --adc ADCINA1=1.0V --adc ADCINB1=2.0V
WDCR      .set 0x7029
PLLSTS    .set 0x7011
CLKCTL    .set 0x7012
PCLKCR0   .set 0x701C
PCLKCR1   .set 0x701D
PCLKCR3   .set 0x7020
PLLCR     .set 0x7021
Device_cal .set 0x3D7C80
GPAMUX1   .set 0x6F86
GPADIR    .set 0x6F8A
GPATOGGLE .set 0x6FC6
PIECTRL   .set 0x0CE0
PIEACK    .set 0x0CE1
PIEIER1   .set 0x0CE2
PIEIER2   .set 0x0CE4
ADCCTL1   .set 0x7100
ADCINTFLGCLR .set 0x7105
INTSEL1N2 .set 0x7108
ADCSOC0CTL .set 0x7120
ADCSOC1CTL .set 0x7121
ADCRESULT0 .set 0x0B00
ADCRESULT1 .set 0x0B01
TBCTL     .set 0x6800                 ; ePWM1
TBPRD     .set 0x6805
CMPCTL    .set 0x6807
CMPA      .set 0x6809
AQCTLA    .set 0x680B
TZSEL     .set 0x6812
TZDCSEL   .set 0x6813
TZCTL     .set 0x6814
TZEINT    .set 0x6815
TZFLG     .set 0x6816
TZCLR     .set 0x6817
ETSEL     .set 0x6819
ETPS      .set 0x681A
DCTRIPSEL .set 0x6830
COMPCTL   .set 0x6400                 ; COMP1
COMPSTS   .set 0x6402
DACVAL    .set 0x6406
adc_a1    .set 0x8000
adc_b1    .set 0x8001
adc_count .set 0x8002
ADCVEC    .set 0x0D40                 ; PIE vector 32 = INT1.1 ADCINT1

        .sect "codestart", 0x3F7FF6
        LB      start

        .sect "text", 0x3F0000
start:  SETC    INTM
        EALLOW
        MOVW    DP, #DP(WDCR)
        MOV     @WDCR, #0x0068        ; watchdog off
        OR      @PCLKCR0, #0x0008     ; ADC clock on for Device_cal
        LCR     Device_cal
        EALLOW
        MOVW    DP, #DP(PCLKCR0)
        MOV     @CLKCTL, #0x6400      ; INTOSC1, other sources off
        AND     @PLLSTS, #0xFE7F      ; DIVSEL = 0
        OR      @PLLSTS, #0x0040      ; MCLKOFF while locking
        MOV     @PLLCR, #12
pll:    TBIT    @PLLSTS, #0
        SB      pll, NTC
        AND     @PLLSTS, #0xFFBF
        OR      @PLLSTS, #0x0100      ; DIVSEL = 2: 60 MHz
        AND     @PCLKCR0, #0xFFFB     ; TBCLKSYNC = 0 while configuring ePWM1
        OR      @PCLKCR1, #0x0001     ; EPWM1ENCLK
        MOVW    DP, #DP(GPAMUX1)
        OR      @GPAMUX1, #0x0001     ; GPIO0 = EPWM1A
        OR      @GPADIR, #0x0002      ; GPIO1 output (LED)
        MOVW    DP, #DP(ADCVEC)
        MOVL    XAR4, #adc_isr
        MOVL    @ADCVEC, XAR4
        MOVW    DP, #DP(ADCCTL1)
        MOV     @ADCCTL1, #0x40E4     ; ADCENABLE | ADCPWDN | ADCBGPWD | ADCREFPWD | INTPULSEPOS
        MOV     @ADCSOC0CTL, #0x2846  ; TRIGSEL 5 (ePWM1 SOCA), CHSEL 1 (A1), ACQPS 6
        MOV     @ADCSOC1CTL, #0x2A46  ; TRIGSEL 5, CHSEL 9 (B1), ACQPS 6
        MOV     @INTSEL1N2, #0x0021   ; ADCINT1 = EOC1, enabled
        EDIS
        MOV     @AL, #30000           ; 1 ms ADC power-up delay (2 x 30000 cycles at 60 MHz)
pwr:    ADDB    AL, #-1
        SB      pwr, NEQ
        MOVW    DP, #DP(TBCTL)
        MOV     @TBPRD, #5999         ; 60 MHz / 6000 = 10 kHz, up count
        MOV     @CMPA, #1500          ; 25 %
        MOV     @AQCTLA, #0x0012      ; ZRO: set, CAU: clear
        MOV     @ETSEL, #0x0A00       ; SOCAEN, SOCASEL = 010 (CTR = PRD)
        MOV     @ETPS, #0x0100        ; SOCAPRD = 1
        MOV     @TBCTL, #0x0030       ; up count, SYNCOSEL off, TBCLK = SYSCLKOUT
        MOVW    DP, #DP(PIECTRL)
        MOV     @PIECTRL, #1          ; ENPIE
        OR      @PIEIER1, #0x0001     ; INT1.1 ADCINT1
        MOV     @PIEACK, #0xFFFF
        OR      IER, #0x0001
        EALLOW
        MOVW    DP, #DP(PCLKCR0)
        OR      @PCLKCR0, #0x0004     ; TBCLKSYNC: start ePWM1
        EDIS
        EINT
idle:   IDLE
        SB      idle, UNC

adc_isr:
        MOVW    DP, #DP(ADCRESULT0)
        MOV     AL, @ADCRESULT0
        MOV     AH, @ADCRESULT1
        MOVW    DP, #DP(adc_a1)
        MOV     @adc_a1, AL
        MOV     @adc_b1, AH
        INC     @adc_count
        MOVW    DP, #DP(GPATOGGLE)
        MOV     @GPATOGGLE, #0x0002
        MOVW    DP, #DP(ADCINTFLGCLR)
        MOV     @ADCINTFLGCLR, #0x0001
        MOVW    DP, #DP(PIEACK)
        MOV     @PIEACK, #0x0001
        IRET
