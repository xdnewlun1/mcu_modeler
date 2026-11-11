; blinky: P1.0 toggles every 50 ms, timed by Timer 0 (mode 1, polled TF0).
; P89CV51RD2 at 11.0592 MHz, 12-clock mode: one machine cycle = 1.0851 us; 50 ms = 46080 counts
; -> reload TH0:TL0 = 65536 - 46080 = 4C00h.
        ORG     0000h
        LJMP    main

        ORG     0030h
main:   MOV     SP, #5Fh
        MOV     TMOD, #01h              ; Timer 0: mode 1 (16-bit), timer
        MOV     TH0, #4Ch
        MOV     TL0, #00h
        SETB    TR0
loop:   JNB     TF0, $                  ; wait for the overflow
        CLR     TR0
        MOV     TH0, #4Ch               ; reload (the few cycles spent here are not compensated)
        MOV     TL0, #00h
        CLR     TF0
        SETB    TR0
        CPL     P1.0
        INC     30h                     ; toggle counter (tests)
        SJMP    loop
        END
