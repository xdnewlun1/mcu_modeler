; uart_t2: interrupt-driven UART at 9600 8N1 with Timer 2 as the baud-rate generator.
; 11.0592 MHz, 12-clock: baud = fosc / (32 * (65536 - RCAP2)) -> RCAP2 = 65536 - 36 = FFDCh.
; The serial ISR (vector 0023h) stores received bytes in a 16-byte ring at 40h; the main loop echoes them
; uppercased.  T2CON = 34h: RCLK, TCLK, TR2.
RXHEAD  DATA    30h
RXTAIL  DATA    31h
RXCOUNT DATA    32h
RING    EQU     40h

        ORG     0000h
        LJMP    main
        ORG     0023h
        LJMP    serial_isr

        ORG     0050h
main:   MOV     SP, #6Fh
        MOV     RXHEAD, #0
        MOV     RXTAIL, #0
        MOV     RXCOUNT, #0
        MOV     RCAP2H, #0FFh
        MOV     RCAP2L, #0DCh
        MOV     TH2, #0FFh
        MOV     TL2, #0DCh
        MOV     T2CON, #34h             ; RCLK = TCLK = 1, TR2 = 1
        MOV     SCON, #50h              ; mode 1, REN
        SETB    ES
        SETB    EA
        MOV     DPTR, #banner
        LCALL   puts
loop:   MOV     A, RXHEAD
        CJNE    A, RXTAIL, have
        SJMP    loop
have:   ADD     A, #RING
        MOV     R0, A
        MOV     A, @R0
        INC     RXHEAD
        ANL     RXHEAD, #0Fh
        CJNE    A, #'a', $+3
        JC      send                    ; < 'a'
        CJNE    A, #'z'+1, $+3
        JNC     send                    ; > 'z'
        ADD     A, #'A'-'a'
send:   LCALL   putc
        SJMP    loop

putc:   CLR     ES                      ; TI is polled: keep the ISR from seeing it
        CLR     TI
        MOV     SBUF, A
        JNB     TI, $
        CLR     TI
        SETB    ES
        RET

puts:   CLR     A
        MOVC    A, @A+DPTR
        JZ      puts_end
        LCALL   putc
        INC     DPTR
        SJMP    puts
puts_end:
        RET

serial_isr:
        PUSH    ACC
        PUSH    PSW
        MOV     PSW, #08h               ; register bank 1
        JNB     RI, isr_end
        CLR     RI
        MOV     A, RXTAIL
        ADD     A, #RING
        MOV     R0, A
        MOV     @R0, SBUF
        INC     RXTAIL
        ANL     RXTAIL, #0Fh
        INC     RXCOUNT
isr_end:
        POP     PSW
        POP     ACC
        RETI

banner: DB      "P89CV51 T2 9600 echo", 13, 10, 0
        END
