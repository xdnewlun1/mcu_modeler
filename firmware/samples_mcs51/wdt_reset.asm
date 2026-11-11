; wdt_reset: the watchdog (WDTRST A6h: write 1Eh, E1h) resets the chip when it is not fed.
; Internal RAM survives a reset (data sheet sec. 6.2.3), PCON.POF tells a power-on reset from the others:
;   power-on: clear the reset counter at 7Fh; every reset: increment it and toggle P1.0.
; After enabling the watchdog the program feeds it 4 times (every ~10 ms), then stops feeding: 16383 machine cycles
; (~17.8 ms at 11.0592 MHz) later the WDT resets the chip.  On the third reset it keeps feeding forever and sets
; P1.1 low.
RSTCNT  DATA    7Fh

        ORG     0000h
        LJMP    main

        ORG     0030h
main:   MOV     SP, #5Fh
        MOV     A, PCON
        JNB     ACC.4, warm             ; POF clear: not a power-on reset
        ANL     PCON, #0EFh             ; clear POF
        MOV     RSTCNT, #0
        SJMP    start
warm:   INC     RSTCNT
        CPL     P1.0
start:  MOV     WDTRST, #1Eh
        MOV     WDTRST, #0E1h           ; enable
        MOV     R7, #4
feed:   LCALL   delay10ms
        MOV     WDTRST, #1Eh
        MOV     WDTRST, #0E1h
        DJNZ    R7, feed
        MOV     A, RSTCNT
        CJNE    A, #3, starve
forever:
        CLR     P1.1
        LCALL   delay10ms
        MOV     WDTRST, #1Eh
        MOV     WDTRST, #0E1h
        SJMP    forever
starve: SJMP    $                       ; no more feeding: the WDT resets the chip

delay10ms:                              ; 9216 machine cycles: 2 + 36 * (2 + 128 * 2) - ish
        MOV     R6, #36
d1:     MOV     R5, #127
d2:     DJNZ    R5, d2
        DJNZ    R6, d1
        RET
        END
