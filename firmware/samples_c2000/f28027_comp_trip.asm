* f28027_comp_trip.asm -- TMS320F28027: comparator COMP1 trips ePWM1 through the digital compare submodule.
* ePWM1: 20 kHz, 50 % on EPWM1A (GPIO0), up count.  COMP1: COMP1A = the ADCINA2/COMP1A/AIO2 pin, inverting input =
* the internal 10-bit DAC at DACVAL = 512 (512 x 3.3 V / 1023 = 1.652 V, SPRUI09A 7.3), asynchronous output, also
* on GPIO1 (MUX3 = COMP1OUT).  The ADC bandgap is powered (ADCCTL1.ADCBGPWD, required by 7.5).
* Digital compare (SPRUI09 3.2.9): DCTRIPSEL.DCAHCOMPSEL = 8 (COMP1OUT), TZDCSEL.DCAEVT1 = 2 (DCAH high),
* TZSEL.DCAEVT1 = one-shot trip source, TZCTL.TZA = 2 (EPWM1A forced low), TZEINT.OST -> EPWM1_TZINT (PIE 2.1,
* vector 40) -> tz_isr counts trip_count (0x8000) and toggles GPIO2 (LED), disables the OST interrupt (SPRUI09
* Table 3-47: clearing TZFLG.INT while OST is set and enabled pulses again) and clears TZFLG.INT; the trip stays
* latched (TZFLG.OST).
* When COMP1A rises above 1.652 V EPWM1A stops at low.  Board: boards/f28027_comp_adc.yaml (a script part ramps
* the voltage).  Clock setup as f28027_blinky.asm.
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
trip_count .set 0x8000
TZVEC     .set 0x0D50                 ; PIE vector 40 = INT2.1 EPWM1_TZINT

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
        AND     @PCLKCR0, #0xFFFB     ; TBCLKSYNC = 0
        OR      @PCLKCR1, #0x0001     ; EPWM1ENCLK
        OR      @PCLKCR3, #0x0001     ; COMP1ENCLK
        MOVW    DP, #DP(GPAMUX1)
        OR      @GPAMUX1, #0x000D     ; GPIO0 = EPWM1A (mux 1), GPIO1 = COMP1OUT (mux 3)
        OR      @GPADIR, #0x0004      ; GPIO2 output (LED)
        MOVW    DP, #DP(TZVEC)
        MOVL    XAR4, #tz_isr
        MOVL    @TZVEC, XAR4
        MOVW    DP, #DP(ADCCTL1)
        MOV     @ADCCTL1, #0x00E0     ; bandgap, reference and ADC core powered (comparator needs the bandgap)
        MOVW    DP, #DP(COMPCTL)
        MOV     @DACVAL, #512
        MOV     @COMPCTL, #0x0001     ; COMPDACEN, inverting input = DAC, async output, not inverted
        MOVW    DP, #DP(TBCTL)
        MOV     @DCTRIPSEL, #0x0008   ; DCAH = COMP1OUT
        MOV     @TZDCSEL, #0x0002     ; DCAEVT1 = DCAH high
        MOV     @TZSEL, #0x4000       ; DCAEVT1 = one-shot trip
        MOV     @TZCTL, #0x000A       ; TZA = TZB = force low
        MOV     @TZEINT, #0x0004      ; OST interrupt
        EDIS
        MOV     @TBPRD, #2999         ; 60 MHz / 3000 = 20 kHz
        MOV     @CMPA, #1500          ; 50 %
        MOV     @AQCTLA, #0x0012      ; ZRO: set, CAU: clear
        MOV     @TBCTL, #0x0030
        MOVW    DP, #DP(PIECTRL)
        MOV     @PIECTRL, #1
        OR      @PIEIER2, #0x0001     ; INT2.1 EPWM1_TZINT
        MOV     @PIEACK, #0xFFFF
        OR      IER, #0x0002
        EALLOW
        MOVW    DP, #DP(PCLKCR0)
        OR      @PCLKCR0, #0x0004     ; TBCLKSYNC
        EDIS
        EINT
idle:   IDLE
        SB      idle, UNC

tz_isr: MOVW    DP, #DP(trip_count)
        INC     @trip_count
        MOVW    DP, #DP(GPATOGGLE)
        MOV     @GPATOGGLE, #0x0004
        EALLOW
        MOVW    DP, #DP(TZCLR)
        MOV     @TZEINT, #0x0000      ; OST stays latched: clearing INT with OST set and enabled would pulse again
        MOV     @TZCLR, #0x0001       ; clear TZFLG.INT (TZFLG.OST stays: EPWM1A held low)
        EDIS
        MOVW    DP, #DP(PIEACK)
        MOV     @PIEACK, #0x0002
        IRET
