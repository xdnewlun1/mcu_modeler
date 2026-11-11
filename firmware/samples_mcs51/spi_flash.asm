; spi_flash: SPI master reads a serial NOR flash (boards/p89cv51_spi_flash.yaml): JEDEC ID (9Fh) to 30h-32h, then
; READ (03h) of 16 bytes from address 000000h to 40h-4Fh, then prints "ID xxxxxx" on the UART (9600, Timer 1).
; SPI: master, CPOL = 0, CPHA = 0, MSB first, SPICLK = fosc / 16 (SPR = 01); chip select on P2.0 (a GPIO: the
; SS pin P1.4 is not used by a master).  SPCR = 51h: SPEN, MSTR, SPR0.
CS      BIT     P2.0

        ORG     0000h
        LJMP    main

        ORG     0030h
main:   MOV     SP, #6Fh
        MOV     TMOD, #20h
        MOV     TH1, #0FDh
        MOV     TL1, #0FDh
        MOV     SCON, #50h
        SETB    TR1
        SETB    CS
        MOV     SPCR, #51h
        ; JEDEC ID
        CLR     CS
        MOV     A, #9Fh
        LCALL   spi_xfer
        MOV     R0, #30h
        MOV     R7, #3
id_lp:  LCALL   spi_xfer
        MOV     @R0, A
        INC     R0
        DJNZ    R7, id_lp
        SETB    CS
        ; READ 16 bytes from 000000h
        CLR     CS
        MOV     A, #03h
        LCALL   spi_xfer
        CLR     A
        LCALL   spi_xfer
        CLR     A
        LCALL   spi_xfer
        CLR     A
        LCALL   spi_xfer
        MOV     R0, #40h
        MOV     R7, #16
rd_lp:  LCALL   spi_xfer
        MOV     @R0, A
        INC     R0
        DJNZ    R7, rd_lp
        SETB    CS
        ; report
        MOV     A, #'I'
        LCALL   putc
        MOV     A, #'D'
        LCALL   putc
        MOV     A, #' '
        LCALL   putc
        MOV     A, 30h
        LCALL   puthex
        MOV     A, 31h
        LCALL   puthex
        MOV     A, 32h
        LCALL   puthex
        MOV     A, #13
        LCALL   putc
        MOV     A, #10
        LCALL   putc
        SJMP    $

spi_xfer:                               ; A out, A in
        MOV     SPDAT, A
        MOV     A, SPSR
        JNB     ACC.7, $-2              ; wait for SPIF
        ANL     SPSR, #7Fh              ; clear SPIF
        MOV     A, SPDAT
        RET

puthex: PUSH    ACC
        SWAP    A
        LCALL   nib
        POP     ACC
nib:    ANL     A, #0Fh
        ADD     A, #'0'
        CJNE    A, #'9'+1, $+3
        JC      nib_out
        ADD     A, #'A'-'9'-1
nib_out:
putc:   CLR     TI
        MOV     SBUF, A
        JNB     TI, $
        RET
        END
