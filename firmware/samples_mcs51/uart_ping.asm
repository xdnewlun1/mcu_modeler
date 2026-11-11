; uart_ping: chip A of boards/p89cv51_stm32_uart.yaml.  Sends "ping N\r\n" (N = 0..9, then wraps) every 2 ms on the
; UART at 115200 8N1 and counts received bytes in 30h; every received '\n' toggles P1.0.  The STM32 partner
; (firmware/samples_stm32/uart_link_echo.c) echoes everything uppercased, so 31h.. collect "PING N\r\n".
; Baud: Timer 2 baud-rate generator, 11.0592 MHz / (32 x (65536 - FFFDh)) = 115200.  Timer 0 paces the 2 ms.
RXCNT   DATA    30h
RXPTR   DATA    2Fh
N       DATA    2Eh

        ORG     0000h
        LJMP    main
        ORG     0023h
        LJMP    serial_isr

        ORG     0050h
main:   MOV     SP, #6Fh
        MOV     RXCNT, #0
        MOV     RXPTR, #31h
        MOV     N, #'0'
        MOV     RCAP2H, #0FFh
        MOV     RCAP2L, #0FDh
        MOV     TH2, #0FFh
        MOV     TL2, #0FDh
        MOV     T2CON, #34h
        MOV     SCON, #50h
        MOV     TMOD, #01h
        SETB    ES
        SETB    EA
again:  MOV     TH0, #HIGH(65536 - 1843)    ; 2 ms = 1843 machine cycles
        MOV     TL0, #LOW(65536 - 1843)
        CLR     TF0
        SETB    TR0
        JNB     TF0, $
        CLR     TR0
        MOV     DPTR, #ping
        LCALL   puts
        MOV     A, N
        LCALL   putc
        MOV     A, #13
        LCALL   putc
        MOV     A, #10
        LCALL   putc
        INC     N
        MOV     A, N
        CJNE    A, #'9'+1, again
        MOV     N, #'0'
        SJMP    again

putc:   CLR     ES
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
        JNB     RI, s_end
        CLR     RI
        INC     RXCNT
        MOV     A, SBUF
        CJNE    A, #10, s_store
        CPL     P1.0
s_store:
        PUSH    0                       ; R0 of bank 0
        MOV     R0, RXPTR
        CJNE    R0, #60h, s_put
        SJMP    s_skip
s_put:  MOV     @R0, A
        INC     RXPTR
s_skip: POP     0
s_end:  POP     PSW
        POP     ACC
        RETI

ping:   DB      "ping ", 0
        END
