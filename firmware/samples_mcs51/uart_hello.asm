; uart_hello: "Hello" banner and echo on the UART at 9600 8N1, baud rate from Timer 1.
; 11.0592 MHz, 12-clock, SMOD1 = 0: Timer 1 mode 2, TH1 = 256 - 11059200 / (12 * 32 * 9600) = 253 = FDh.
; Every received byte is sent back; P1.0 toggles per byte.
        ORG     0000h
        LJMP    main

        ORG     0030h
main:   MOV     SP, #5Fh
        MOV     TMOD, #20h              ; Timer 1 mode 2 (8-bit auto-reload)
        MOV     TH1, #0FDh
        MOV     TL1, #0FDh
        MOV     SCON, #50h              ; mode 1, REN
        SETB    TR1
        MOV     DPTR, #banner
        LCALL   puts
echo:   JNB     RI, $
        MOV     A, SBUF
        CLR     RI
        CPL     P1.0
        LCALL   putc
        SJMP    echo

putc:   CLR     TI
        MOV     SBUF, A
        JNB     TI, $
        RET

puts:   CLR     A
        MOVC    A, @A+DPTR
        JZ      puts_end
        LCALL   putc
        INC     DPTR
        SJMP    puts
puts_end:
        RET

banner: DB      "Hello from P89CV51RD2 (UART 9600 from Timer 1)", 13, 10, 0
        END
