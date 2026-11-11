; iap_flash: In-Application Programming through the boot ROM entry PGM_MTP (FFF0h, data sheet sec. 6.3.8):
; enable the boot block (AUXR1.ENBOOT), erase the 128-byte page at E000h (R1 = 08h), program the 8-byte string
; "IAP OK!\0" there byte by byte (R1 = 02h), read it back with IAP read (R1 = 03h) and with MOVC, and store the
; result in 30h (55h = pass, EEh = fail), the IAP return codes ORed in 31h, the manufacturer ID (R1 = 00h) in 32h.
; Then prints the string from flash on the UART (9600, Timer 1).
PGM_MTP EQU     0FFF0h
ENBOOT  EQU     20h
PAGE    EQU     0E000h

        ORG     0000h
        LJMP    main

        ORG     0030h
main:   MOV     SP, #6Fh
        MOV     TMOD, #20h
        MOV     TH1, #0FDh
        MOV     TL1, #0FDh
        MOV     SCON, #50h
        SETB    TR1
        MOV     31h, #0
        ORL     AUXR1, #ENBOOT
        ; read manufacturer ID
        MOV     R1, #00h
        MOV     DPTR, #0000h
        LCALL   PGM_MTP
        MOV     32h, A
        ; erase the page
        MOV     R1, #08h
        MOV     DPTR, #PAGE
        LCALL   PGM_MTP
        ORL     31h, A
        ; program 8 bytes
        MOV     R2, #0
prog:   MOV     A, R2
        MOV     DPTR, #text
        MOVC    A, @A+DPTR              ; source byte
        MOV     R3, A
        MOV     DPH, #HIGH(PAGE)
        MOV     A, R2
        MOV     DPL, A
        MOV     A, R3
        MOV     R1, #02h
        LCALL   PGM_MTP
        ORL     31h, A
        INC     R2
        CJNE    R2, #8, prog
        ; verify with IAP read and with MOVC
        MOV     30h, #55h
        MOV     R2, #0
verify: MOV     DPH, #HIGH(PAGE)
        MOV     A, R2
        MOV     DPL, A
        MOV     R1, #03h
        LCALL   PGM_MTP
        MOV     R4, A                   ; IAP read
        MOV     DPTR, #PAGE
        MOV     A, R2
        MOVC    A, @A+DPTR              ; MOVC read
        XRL     A, R4
        JNZ     bad
        MOV     DPTR, #text
        MOV     A, R2
        MOVC    A, @A+DPTR
        XRL     A, R4
        JNZ     bad
        INC     R2
        CJNE    R2, #8, verify
        SJMP    report
bad:    MOV     30h, #0EEh
report: ANL     AUXR1, #NOT ENBOOT AND 0FFh
        MOV     DPTR, #PAGE
pr:     CLR     A
        MOVC    A, @A+DPTR
        JZ      done
        CLR     TI
        MOV     SBUF, A
        JNB     TI, $
        INC     DPTR
        SJMP    pr
done:   SJMP    $

text:   DB      "IAP OK!", 0
        END
