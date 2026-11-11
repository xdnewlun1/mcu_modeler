; timer_irq: Timer 0 interrupt every 10 ms (mode 1, reload DC00h = 65536 - 9216 at 11.0592 MHz / 12),
; toggles P1.1 in the ISR and counts ticks in 30h:31h (big endian); Timer 0 is given the highest priority level 3
; (IPH.PT0H + IP.PT0).  The main loop toggles P1.0 every 10 ticks.
TICKS_H DATA    30h
TICKS_L DATA    31h

        ORG     0000h
        LJMP    main
        ORG     000Bh
        LJMP    t0_isr

        ORG     0040h
main:   MOV     SP, #5Fh
        MOV     TICKS_H, #0
        MOV     TICKS_L, #0
        MOV     TMOD, #01h
        MOV     TH0, #0DCh
        MOV     TL0, #00h
        ORL     IPH, #02h               ; PT0H
        SETB    PT0
        SETB    ET0
        SETB    EA
        SETB    TR0
        MOV     R7, #10
wait:   MOV     A, TICKS_L
w2:     CJNE    A, TICKS_L, tick        ; spin until the ISR advances the count
        SJMP    w2
tick:   DJNZ    R7, wait
        MOV     R7, #10
        CPL     P1.0
        SJMP    wait

t0_isr: MOV     TH0, #0DCh              ; reload first (mode 1 has no auto-reload)
        MOV     TL0, #00h
        CPL     P1.1
        INC     TICKS_L
        PUSH    ACC
        MOV     A, TICKS_L
        JNZ     t0_done
        INC     TICKS_H
t0_done:
        POP     ACC
        RETI
        END
