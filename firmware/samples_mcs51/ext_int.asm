; ext_int: button on INT0 (P3.2, falling edge, IT0 = 1): each press toggles the LED on P1.0 and counts in 30h.
; INT1 (P3.3) is level-triggered (IT1 = 0): while it is held low its ISR runs again after every RETI (one main-loop
; instruction in between); the ISR counts in 31h and the main loop counts the instructions it gets in 32h/33h.
        ORG     0000h
        LJMP    main
        ORG     0003h
        LJMP    int0_isr
        ORG     0013h
        LJMP    int1_isr

        ORG     0040h
main:   MOV     SP, #5Fh
        MOV     30h, #0
        MOV     31h, #0
        MOV     32h, #0
        MOV     33h, #0
        SETB    IT0                     ; INT0 falling edge
        CLR     IT1                     ; INT1 level
        SETB    EX0
        SETB    EX1
        SETB    EA
idle:   INC     33h
        MOV     A, 33h
        JNZ     idle
        INC     32h
        SJMP    idle

int0_isr:
        CPL     P1.0
        INC     30h
        RETI

int1_isr:
        INC     31h
        RETI
        END
