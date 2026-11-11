* tms320f2806x_bootrom.asm -- stand-in for the TMS320F2806x boot ROM and the TI-reserved OTP routines, after SPRUH18I
* chapter 2 (Boot ROM; section numbers below are SPRUI09A's, the 2806x text is the same) and SPRS698J (Figure 8-1 memory
* map, Table 8-1 boot modes, Table 8-10 PARTID).  The 2806x ROM is 32K x 16 at 0x3F8000 (SPRUH18 Figure 2-1), boot
* loader functions from 0x3FF3B0.
* TI's ROM and OTP contents are not public: this is a functional substitute at TI's documented addresses, assembled by
* tools/c28x_asm.py and seeded into erased ROM/OTP through `boot.rom_words` in chips/tms320f28069.yaml:
*   .venv/bin/python -I tools/gen_piccolo_regs.py --bootrom-words chips/tms320f2806x_bootrom.asm
* (tests/test_c2000_piccolo_chip.py re-assembles this file and compares with the YAML).
*
* InitBoot (bootloader-function area of the ROM, SPRUI09 Figure 2-1; the real routine's address is not documented,
* the RESET vector at 0x3FFFC0 points here):
*   1. dummy read of the CSM password PWL0-7 (0x3F7FF8-0x3F7FFF): an erased password unsecures the CSM (2.2.13)
*   2. Device_cal() with the ADC clock on (Figure 2-3, 2.2.10), then PLLSTS.DIVSEL = 3 (SYSCLKOUT = CLKIN/1, 2.2.3)
*   3. SelectBootMode (2.2.9, Table 2-8, TRST = 0 because CHIP_EM has no JTAG probe): GPIO37/TDO and GPIO34 (pull-ups
*      on at reset), EMU_KEY (0x0D00) = 0x55AA and EMU_BMODE (0x0D01) = the pin mode are written as on silicon:
*        1 1  GetMode: Get_mode() (OTP_KEY/OTP_BMODE, Table 2-6) -> flash 0x3F7FF6 (default), OTP 0x3D7800, or a loader
*        1 0  wait boot      0 1  SCI-A loader      0 0  parallel loader
*      The SCI / SPI / I2C / parallel loaders are not implemented: like the wait mode they disable the watchdog and
*      loop (see docs/LIMITATIONS.md).  A direct branch to flash / OTP leaves the watchdog running (2.2.4).
*   4. ExitBoot (2.2.20): ACC, XAR0-XAR7 = 0, DP = 0, ST1 = 0x0A0B (EALLOW = 0), SP = 0x0400, branch to the entry.
* TI-reserved OTP (not user-programmable, SPRS523 9.2 "0x3D7C80-0x3D7CC0 contain the internal oscillator and ADC
* calibration routines", the same in SPRS698 Figure 8-1):
*   Device_cal 0x3D7C80 (the address firmware calls through `(*Device_cal)()`): writes ADCREFTRIM, ADCOFFTRIM,
*     INTOSC1TRIM and INTOSC2TRIM.  The values are placeholders, not factory trims (the models treat the oscillators and
*     the ADC gain as nominal; ADCOFFTRIM = 0 adds no offset).
*   Get_mode 0x3D7CC0: AL = OTP_BMODE when OTP_KEY (0x3D7BFE) = 0x55AA and OTP_BMODE (0x3D7BFF) is SCI (1), SPI (4),
*     I2C (5) or OTP (6), else FLASH_BOOT (0x000B).
*   getTempSlope 0x3D7E82 / getTempOffset 0x3D7E85 (SPRUH18 8.12.1; PARTID sits at 0x3D7E80): return the data-sheet
*     typical slope 0.18 C/LSB (Q15 5898) and offset 1750 LSB (SPRS698 8.9.2.1.3.3.1), as the ADC model's sensor.
* PARTID (0x3D7E80) is not here: it is the `ti_c2000.partid` block of the chip YAML (one value per part / package).

PLLSTS    .set 0x7011
PCLKCR0   .set 0x701C
WDCR      .set 0x7029
INTOSC1TRIM .set 0x7014
INTOSC2TRIM .set 0x7016
ADCREFTRIM .set 0x7140
ADCOFFTRIM .set 0x7141
GPBDAT    .set 0x6FC8                 ; GPIO32-38 (GPIO34 = bit 2, GPIO37 = bit 5)
EMU_KEY   .set 0x0D00
EMU_BMODE .set 0x0D01
Device_cal .set 0x3D7C80
Get_mode  .set 0x3D7CC0

        .sect "initboot", 0x3FF3B0
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
        EALLOW
        MOVW    DP, #DP(PCLKCR0)
        OR      @PCLKCR0, #0x0008     ; ADCENCLK for Device_cal
        LCR     Device_cal
        EALLOW
        MOVW    DP, #DP(PCLKCR0)
        AND     @PCLKCR0, #0xFFF7
        OR      @PLLSTS, #0x0180      ; DIVSEL = 3: SYSCLKOUT = CLKIN / 1
                                      ; EALLOW stays set: EMU_KEY / EMU_BMODE are PIE vector RAM (EALLOW
                                      ; protected); ExitBoot / the wait loop end with EDIS
        MOVW    DP, #DP(EMU_KEY)
        MOV     @EMU_KEY, #0x55AA
        MOVW    DP, #DP(GPBDAT)
        TBIT    @GPBDAT, #5           ; GPIO37/TDO
        SB      m0x, NTC
        TBIT    @GPBDAT, #2           ; GPIO34
        SB      m10, NTC
        MOVW    DP, #DP(EMU_BMODE)    ; 1 1: GetMode
        MOV     @EMU_BMODE, #0x0003
        LCR     Get_mode
        CMPB    AL, #6
        SB      to_otp, EQ
        CMPB    AL, #11
        SB      to_flash, EQ
        SB      waitb, UNC            ; SCI / SPI / I2C loaders: not implemented
m10:    MOVW    DP, #DP(EMU_BMODE)    ; 1 0: wait boot
        MOV     @EMU_BMODE, #0x0002
        SB      waitb, UNC
m0x:    TBIT    @GPBDAT, #2
        SB      m00, NTC
        MOVW    DP, #DP(EMU_BMODE)    ; 0 1: SCI boot (loader not implemented)
        MOV     @EMU_BMODE, #0x0001
        SB      waitb, UNC
m00:    MOVW    DP, #DP(EMU_BMODE)    ; 0 0: parallel boot (loader not implemented)
        MOV     @EMU_BMODE, #0x0000
        SB      waitb, UNC
to_otp: MOVL    XAR7, #0x3D7800
        SB      ExitBoot, UNC
to_flash:
        MOVL    XAR7, #0x3F7FF6
        SB      ExitBoot, UNC

waitb:  EALLOW                        ; wait boot: watchdog off, loop until a debugger takes over
        MOVW    DP, #DP(WDCR)
        MOV     @WDCR, #0x0068
        EDIS
wloop:  SB      wloop, UNC

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
        EDIS
        LB      *XAR7

ITRAPIsr:
        EALLOW
        MOVW    DP, #DP(WDCR)
        MOV     @WDCR, #0x0028        ; watchdog on, then wait for its reset (2.2.5)
        EDIS
itrap:  SB      itrap, UNC

        .sect "devcal", 0x3D7C80
DevCal: EALLOW
        MOVW    DP, #DP(ADCREFTRIM)
        MOV     @ADCREFTRIM, #0x0000
        MOV     @ADCOFFTRIM, #0x0000
        MOVW    DP, #DP(INTOSC1TRIM)
        MOV     @INTOSC1TRIM, #0x0000
        MOV     @INTOSC2TRIM, #0x0000
        EDIS
        LRETR

        .sect "getmode", 0x3D7CC0
GetMode:
        MOVL    XAR4, #0x3D7BFE
        MOV     AL, *XAR4++           ; OTP_KEY
        CMP     @AL, #0x55AA
        SB      gm_fl, NEQ
        MOV     AL, *XAR4             ; OTP_BMODE
        CMPB    AL, #1
        SB      gm_ok, EQ
        CMPB    AL, #4
        SB      gm_ok, EQ
        CMPB    AL, #5
        SB      gm_ok, EQ
        CMPB    AL, #6
        SB      gm_ok, EQ
gm_fl:  MOVB    AL, #11               ; FLASH_BOOT
gm_ok:  LRETR

        .sect "tempcal", 0x3D7E82
        MOV     ACC, #5898            ; getTempSlope: 0.18 C/LSB in Q15
        LRETR
        MOV     ACC, #1750            ; getTempOffset (0x3D7E85): ADC code at 0 C
        LRETR

        .sect "vectors", 0x3FFFC0
        .long   InitBoot              ; RESET
        .long   0x000042, 0x000044, 0x000046, 0x000048, 0x00004A, 0x00004C, 0x00004E   ; INT1-7
        .long   0x000050, 0x000052, 0x000054, 0x000056, 0x000058, 0x00005A, 0x00005C   ; INT8-14
        .long   0x00005E, 0x000060, 0x000062, 0x000064                                 ; DLOGINT RTOSINT rsvd NMI
        .long   ITRAPIsr              ; ILLEGAL
        .long   0x000068, 0x00006A, 0x00006C, 0x00006E, 0x000070, 0x000072            ; USER1-6
        .long   0x000074, 0x000076, 0x000078, 0x00007A, 0x00007C, 0x00007E            ; USER7-12
