; codestart.asm -- boot-to-flash entry for the F28232 C samples (same idea as TI's DSP2833x_CodeStartBranch.asm).
; The boot ROM's "jump to flash" mode branches to 0x33FFF6 (section `codestart`, placed at BEGIN by f28232.cmd).
; CHIP_EM has no boot ROM: it starts at the image entry, which build.sh sets to `code_start` (--entry_point), so
; both paths run the same code.  The watchdog is disabled before the RTS's _c_int00 (its .cinit/.ebss
; initialisation could otherwise outlast the ~6.5 ms watchdog period at the reset clock).

        .ref    _c_int00
        .def    code_start

        .sect   "codestart"
code_start:
        LB      wd_disable              ; 2 words: fits BEGIN (0x33FFF6, length 2)

        .text
wd_disable:
        SETC    OBJMODE                 ; C28x object mode (the boot ROM sets it too)
        EALLOW
        MOVZ    DP, #7029h >> 6         ; WDCR page
        MOV     @7029h, #0068h          ; WDDIS = 1, WDCHK = 101b
        EDIS
        LB      _c_int00                ; RTS boot: SP = __stack, auto-init (.cinit), main()
