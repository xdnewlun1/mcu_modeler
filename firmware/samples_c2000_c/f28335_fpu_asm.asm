; f28335_fpu_asm.asm -- hand-written FPU32 helpers for f28335_fpu.c (things C cannot express: STF.RND32, the STF
; flags, MOVST0, RPTB with an immediate count).  FPU32 calling convention (SPRU514): float arguments in R0H, R1H,
; pointers in XAR4, a float result in R0H, a 32-bit integer result in ACC, a 16-bit one in AL.  R4H-R7H are
; save-on-entry (not touched here).  EABI symbols have no leading underscore; COFF ones do (the .asg below).

        .if __TI_EABI__
        .asg    fpu_mul_trunc, _fpu_mul_trunc
        .asg    fpu_stf_after_mul, _fpu_stf_after_mul
        .asg    fpu_st0_after_mul, _fpu_st0_after_mul
        .asg    fpu_poly_rptb, _fpu_poly_rptb
        .endif
        .def    _fpu_mul_trunc, _fpu_stf_after_mul, _fpu_st0_after_mul, _fpu_poly_rptb

        .text
; float fpu_mul_trunc(float a, float b): a * b with STF.RND32 = 0 (round toward zero), then RND32 = 1 again
; (TI's _c_int00 sets RNDF32 = 1, round to nearest even, for C code).
_fpu_mul_trunc:
        SETFLG  RNDF32=0
        MPYF32  R0H, R0H, R1H
        NOP                             ; MPYF32 has one delay slot (2p)
        SETFLG  RNDF32=1
        LRETR

; unsigned long fpu_stf_after_mul(float a, float b): the STF register after a * b, LVF/LUF cleared before.
_fpu_stf_after_mul:
        SETFLG  LVF=0, LUF=0
        MPYF32  R0H, R0H, R1H
        NOP
        MOV32   *SP++, STF
        MOVL    ACC, *--SP
        LRETR

; unsigned long fpu_st0_after_mul(float a, float b): after a * b, MOVST0 LVF, LUF copies the flags into ST0.V (and
; clears them in STF); returns ST0 in the low word and the STF low word after MOVST0 in the high word.
_fpu_st0_after_mul:
        SETFLG  LVF=0, LUF=0
        MPYF32  R0H, R0H, R1H
        NOP
        MOVST0  LVF, LUF
        PUSH    ST0
        POP     AL
        MOV32   *SP++, STF
        MOV     AH, *-SP[2]
        SUBB    SP, #2
        LRETR

; float fpu_poly_rptb(const float *c, float x): Horner c[0]*x^5 + ... + c[5] with RPTB #4 (5 passes).
_fpu_poly_rptb:
        MOV32   R1H, R0H                ; x
        MOV32   R0H, *XAR4++            ; acc = c[0]
        .align  2
        RPTB    poly_end, #4            ; block repeated RC + 1 = 5 times
        MPYF32  R0H, R0H, R1H
        NOP
        MOV32   R2H, *XAR4++
        ADDF32  R0H, R0H, R2H
        NOP
        NOP
poly_end:
        LRETR
