; pca_pwm: PCA module 0 = 8-bit PWM on CEX0 (P1.3), 25 % high (CCAP0L = C0h: low while CL < C0h);
; module 1 = 16-bit capture of both edges on CEX1 (P1.4) with its interrupt; the ISR stores the first 8 captures
; (CCAP1H, CCAP1L pairs) at 40h-4Fh.  PCA clock = fosc/12 (CPS = 00): PWM frequency 11.0592 MHz / 12 / 256 = 3600 Hz.
; Wire P1.3 to P1.4 (boards/p89cv51_pca_loopback.yaml) and the captures measure the PWM: high 64 counts, period 256.
NCAP    DATA    30h

        ORG     0000h
        LJMP    main
        ORG     0033h                   ; PCA vector (data sheet Table 41)
        LJMP    pca_isr

        ORG     0050h
main:   MOV     SP, #6Fh
        MOV     NCAP, #0
        MOV     R1, #40h
        MOV     CMOD, #00h              ; fosc / 12, no overflow interrupt
        MOV     CCAP0L, #0C0h
        MOV     CCAP0H, #0C0h
        MOV     CCAPM0, #42h            ; ECOM0 + PWM0
        MOV     CCAPM1, #31h            ; CAPP1 + CAPN1 + ECCF1
        SETB    EC
        SETB    EA
        SETB    CR                      ; run the PCA counter
        SJMP    $

pca_isr:
        JNB     CCF1, pca_out
        CLR     CCF1
        MOV     A, NCAP
        CJNE    A, #8, store
        SJMP    pca_out
store:  MOV     @R1, CCAP1H
        INC     R1
        MOV     @R1, CCAP1L
        INC     R1
        INC     NCAP
pca_out:
        RETI
        END
