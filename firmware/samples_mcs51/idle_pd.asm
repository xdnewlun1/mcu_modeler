; idle_pd: power-saving modes (data sheet sec. 6.12).
; 1. Idle: Timer 0 interrupts every 10 ms wake the CPU from Idle (ORL PCON,#01h); the ISR toggles P1.1 and counts
;    in 30h; after 5 wake-ups the main loop goes on.
; 2. Power-down: INT0 is set level-sensitive (IT0 = 0) and enabled, Timer 0 stopped, PCON.PD set; the oscillator
;    stops until INT0 (P3.2) is held low; the INT0 ISR clears EX0 (the pin is still low), toggles P1.2 and sets 31h.
;    The main loop then sets 32h = A5h and toggles P1.0 forever.
        ORG     0000h
        LJMP    main
        ORG     0003h
        LJMP    int0_isr
        ORG     000Bh
        LJMP    t0_isr

        ORG     0040h
main:   MOV     SP, #5Fh
        MOV     30h, #0
        MOV     31h, #0
        MOV     32h, #0
        MOV     TMOD, #01h
        MOV     TH0, #0DCh
        MOV     TL0, #00h
        SETB    ET0
        SETB    EA
        SETB    TR0
idle_lp:
        ORL     PCON, #01h              ; Idle until the next Timer 0 interrupt
        MOV     A, 30h
        CJNE    A, #5, idle_lp
        CLR     TR0
        CLR     ET0
        CLR     IT0                     ; INT0 level-sensitive (power-down wake-up source)
        SETB    EX0
        ORL     PCON, #02h              ; Power-down
        MOV     32h, #0A5h
blink:  CPL     P1.0
        SJMP    blink

t0_isr: MOV     TH0, #0DCh
        MOV     TL0, #00h
        CPL     P1.1
        INC     30h
        RETI

int0_isr:
        CLR     EX0
        CPL     P1.2
        MOV     31h, #1
        RETI
        END
