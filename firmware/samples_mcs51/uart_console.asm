; uart_console: a tiny command console on the UART at 9600 8N1 (Timer 1), for boards/p89cv51_rs232_console.yaml
; (MAX232-style transceiver, DB9, PC terminal).  Commands (one letter + Enter): 'l' toggles the LED (P1.0, active
; low), 'v' prints the version, 'h' or '?' help.  Characters are echoed; prompt "> ".
        ORG     0000h
        LJMP    main

        ORG     0030h
main:   MOV     SP, #6Fh
        MOV     TMOD, #20h
        MOV     TH1, #0FDh
        MOV     TL1, #0FDh
        MOV     SCON, #50h
        SETB    TR1
        MOV     DPTR, #s_hello
        LCALL   puts
prompt: MOV     DPTR, #s_prompt
        LCALL   puts
        MOV     R7, #0                  ; command letter
getc:   JNB     RI, $
        MOV     A, SBUF
        CLR     RI
        CJNE    A, #13, not_cr
        LCALL   crlf
        MOV     A, R7
        CJNE    A, #'l', c2
        CPL     P1.0
        MOV     DPTR, #s_led
        LCALL   puts
        SJMP    prompt
c2:     CJNE    A, #'v', c3
        MOV     DPTR, #s_ver
        LCALL   puts
        SJMP    prompt
c3:     JZ      prompt
        MOV     DPTR, #s_help
        LCALL   puts
        SJMP    prompt
not_cr: LCALL   putc                    ; echo
        MOV     R7, A
        SJMP    getc

crlf:   MOV     A, #13
        LCALL   putc
        MOV     A, #10
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

s_hello: DB     13, 10, "P89CV51RD2 console (9600 8N1)", 13, 10, 0
s_prompt: DB    "> ", 0
s_led:  DB      "LED toggled", 13, 10, 0
s_ver:  DB      "chipem mcs51 sample 1.0", 13, 10, 0
s_help: DB      "commands: l (LED), v (version), h (help)", 13, 10, 0
        END
