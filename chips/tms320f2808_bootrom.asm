* tms320f2808_bootrom.asm -- stand-in for the TMS320F2808 boot ROM (0x3FF000-0x3FFFFF), after SPRU722C.
* TI's ROM contents are not public; this is a functional substitute at TI's addresses, assembled by tools/c28x_asm.py
* and seeded into erased ROM through `boot.rom_words` in chips/tms320f2808.yaml (tests/test_c2000_f2808_chip.py
* re-assembles this file and compares).  Regenerate the YAML words with:
*   .venv/bin/python -I tools/gen_tms320f2808_regs.py --bootrom-words
*
* InitBoot (0x3FFB50, SPRU722C Table 1: the RESET vector at 0x3FFFC0 points here):
*   1. dummy read of the CSM password PWL0-7 (0x3F7FF8-0x3F7FFF): an erased (all 0xFFFF) password unsecures the CSM
*   2. SelectBootMode (SPRU722C 2.9, SPRS230P Table 9-1): sample GPIO18, GPIO29, GPIO34 (inputs with pull-ups at
*      reset, so an unconnected board boots to flash):
*        1 1 1  jump to flash 0x3F7FF6        0 1 0  jump to M0 SARAM 0x000000     0 0 1  jump to OTP 0x3D7800
*        1 1 0  SCI-A loader   1 0 1  SPI-A loader   1 0 0  I2C loader   0 1 1  eCAN-A loader   0 0 0  parallel loader
*      The loaders are NOT implemented: like TI's SelectBootMode the watchdog is disabled first (the loaders assume
*      it is off), then the ROM waits in a per-mode loop (`loader_*`, visible as the PC) as a loader waiting for a
*      host that never answers.  A direct jump leaves the watchdog untouched (SPRU722C 2.4).
*   3. ExitBoot (SPRU722C 2.20, Table 11): ACC, XAR0-XAR7 = 0, DP = 0, ST0 = 0, ST1 = 0x0A0B (EALLOW = 0),
*      SP = 0x0400, then branch to the entry point.
* ITRAPIsr (vector 0x3FFFE6, boot ROM version 4+): enable the watchdog and loop until it resets the device.
* The CPU vector table at 0x3FFFC0 (VMAP = 1, ENPIE = 0) holds RESET -> InitBoot, ILLEGAL -> ITRAPIsr and, for every
* other vector, the M0 SARAM debug locations 0x000040 + 2n of SPRU722C Table 1.

WDCR      .set 0x7029
GPADATH   .set 0x6FC1                 ; GPIO16-31 (GPIO18 = bit 2, GPIO29 = bit 13)
GPBDAT    .set 0x6FC8                 ; GPIO32-34 (GPIO34 = bit 2)

        .sect "initboot", 0x3FFB50
InitBoot:
        MOVL    XAR1, #0x3F7FF8       ; CSM password dummy read
        MOV     AL, *XAR1++
        MOV     AL, *XAR1++
        MOV     AL, *XAR1++
        MOV     AL, *XAR1++
        MOV     AL, *XAR1++
        MOV     AL, *XAR1++
        MOV     AL, *XAR1++
        MOV     AL, *XAR1++
        MOVW    DP, #DP(GPADATH)
        TBIT    @GPADATH, #2          ; GPIO18
        SB      m0xx, NTC
        TBIT    @GPADATH, #13         ; GPIO29
        SB      m10x, NTC
        TBIT    @GPBDAT, #2           ; GPIO34
        SB      loader_sci, NTC
        MOVL    XAR7, #0x3F7FF6       ; 1 1 1: boot to flash
        SB      ExitBoot, UNC
m10x:   TBIT    @GPBDAT, #2
        SB      loader_i2c, NTC
        SB      loader_spi, UNC       ; 1 0 1
m0xx:   TBIT    @GPADATH, #13
        SB      m00x, NTC
        TBIT    @GPBDAT, #2
        SB      to_m0, NTC
        SB      loader_ecan, UNC      ; 0 1 1
to_m0:  MOVL    XAR7, #0x000000       ; 0 1 0: M0 SARAM
        SB      ExitBoot, UNC
m00x:   TBIT    @GPBDAT, #2
        SB      loader_par, NTC
        MOVL    XAR7, #0x3D7800       ; 0 0 1: OTP
        SB      ExitBoot, UNC

wd_off: EALLOW
        MOVW    DP, #DP(WDCR)
        MOV     @WDCR, #0x0068        ; WDCHK = 101, WDDIS = 1
        EDIS
        LRETR
loader_sci:
        LCR     wd_off
lsci:   SB      lsci, UNC
loader_spi:
        LCR     wd_off
lspi:   SB      lspi, UNC
loader_i2c:
        LCR     wd_off
li2c:   SB      li2c, UNC
loader_ecan:
        LCR     wd_off
lecan:  SB      lecan, UNC
loader_par:
        LCR     wd_off
lpar:   SB      lpar, UNC

ExitBoot:
        MOVB    ACC, #0
        MOVL    XAR0, #0
        MOVL    XAR1, #0
        MOVL    XAR2, #0
        MOVL    XAR3, #0
        MOVL    XAR4, #0
        MOVL    XAR5, #0
        MOVL    XAR6, #0
        MOVW    DP, #0
                                      ; SP is still 0x0400 (reset value; every LCR above returned)
        EDIS
        LB      *XAR7

ITRAPIsr:
        EALLOW
        MOVW    DP, #DP(WDCR)
        MOV     @WDCR, #0x0028        ; WDCHK = 101, WDDIS = 0: watchdog on
        EDIS
itrap:  SB      itrap, UNC

        .sect "vectors", 0x3FFFC0
        .long   InitBoot              ; RESET
        .long   0x000042, 0x000044, 0x000046, 0x000048, 0x00004A, 0x00004C, 0x00004E   ; INT1-7
        .long   0x000050, 0x000052, 0x000054, 0x000056, 0x000058, 0x00005A, 0x00005C   ; INT8-14
        .long   0x00005E, 0x000060, 0x000062, 0x000064                                 ; DLOGINT RTOSINT rsvd NMI
        .long   ITRAPIsr              ; ILLEGAL
        .long   0x000068, 0x00006A, 0x00006C, 0x00006E, 0x000070, 0x000072            ; USER1-6
        .long   0x000074, 0x000076, 0x000078, 0x00007A, 0x00007C, 0x00007E            ; USER7-12
