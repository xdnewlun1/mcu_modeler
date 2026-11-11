* tms320f2808_bootrom.asm -- stand-in for the TMS320F2808 boot ROM (0x3FF000-0x3FFFFF), after SPRU722C.
* TI's ROM contents are not public; this is a functional substitute at TI's addresses, assembled by tools/c28x_asm.py
* and seeded into erased ROM through `boot.rom_words` in chips/tms320f2808.yaml (tests/test_c2000_f2808_chip.py
* re-assembles this file and compares).  Regenerate the YAML words with:
*   .venv/bin/python -I tools/gen_tms320f2808_regs.py --bootrom-words
* The loaders follow the C listing of SPRU722C section 4.3 (boot ROM V3/V4: SCI_Boot.c, SPI_Boot.c, I2C_Boot.c,
* CAN_Boot.c, Parallel_Boot.c, Shared_Boot.c) register write for register, rewritten in assembler.
*
* InitBoot (0x3FFB50, SPRU722C Table 1: the RESET vector at 0x3FFFC0 points here):
*   1. dummy read of the CSM password PWL0-7 (0x3F7FF8-0x3F7FFF): an erased (all 0xFFFF) password unsecures the CSM
*   2. SelectBootMode (SPRU722C 2.9, SPRS230P Table 9-1): sample GPIO18, GPIO29, GPIO34 (inputs with pull-ups at
*      reset, so an unconnected board boots to flash):
*        1 1 1  jump to flash 0x3F7FF6        0 1 0  jump to M0 SARAM 0x000000     0 0 1  jump to OTP 0x3D7800
*        1 1 0  SCI-A loader   1 0 1  SPI-A loader   1 0 0  I2C loader   0 1 1  eCAN-A loader   0 0 0  parallel loader
*      A loader disables the watchdog first (WDCR = 0x0068, as TI's SelectBootMode); a direct jump leaves it on.
*   3. ExitBoot (SPRU722C 2.20, Table 11): ACC, XAR0-XAR7 = 0, DP = 0, ST0 = 0, ST1 = 0x0A0B (EALLOW = 0),
*      SP = 0x0400, then branch to the entry point.
*
* Data stream (SPRU722C 2.10): key 0x08AA (8-bit stream: each word LSB first) or 0x10AA (16-bit, parallel only),
* 8 reserved words, the 32-bit entry point (MSW first), then blocks {size, 32-bit destination (MSW first), size
* words} until a size of 0.  An invalid key aborts the load and enters flash (0x3F7FF6).  Loaders:
*   SCI-A  GPIO28/29, LSPCLK = SYSCLKOUT/4, 8N1, no FIFO; SCILBAUD primed with 1, autobaud (SCIFFCT.CDC) locks on
*          the host's 'A'/'a', which is echoed; every later byte is echoed too.  8-bit stream only.
*   SPI-A  master, 8-bit characters, SPICTL = 0x0E (CLK_PHASE = 1, master, talk), SPIBRR = 0x7F (slowest), GPIO16-18
*          SPI pins, GPIO19 as a GPIO chip select (low); sends READ (0x03) and the 16-bit address 0x0000 to a serial
*          EEPROM.  Reserved word 1 = LOSPCP (LSB) / SPIBRR (MSB), applied at once; 7 more reserved words.
*   I2C-A  GPIO32/33, EEPROM at slave address 0x50, I2CPSC = 0, I2CCLKL = I2CCLKH = 0x35 (100 kHz at a 12 MHz module
*          clock), FIFOs on; writes the 16-bit word address 0x0000 (NACK there -> flash), then reads 2 bytes per
*          word (START + read + STOP each).  Reserved words 1-3 = I2CPSC, I2CCLKH, I2CCLKL (applied with IRS = 0),
*          5 more reserved words.
*   eCAN-A GPIO30/31, CANBTC BRP = 2, TSEG1 = 6, TSEG2 = 3, SAM (10 TQ: SYSCLKOUT / 20 = 500 kbit/s at 10 MHz),
*          mailbox 1 receives standard ID 0x001; every data frame carries one word, byte 0 = LSB, byte 1 = MSB.
*          Hangs if the missing-clock flag (PLLSTS.MCLKSTS) is set, as TI's CAN_Boot does.
*   Parallel  GPIO0-15 data inputs, GPIO26 = DSP control (output, low = ready / high = read done), GPIO27 = host
*          control (input, low = data valid / high = acknowledge); key 0x10AA = 16-bit words, 0x08AA = 8-bit bytes
*          on GPIO0-7 (LSB first).
* Register use: XAR6 = the stream's GetWord routine; GetWord returns AL and may change ACC, XAR0, XAR1, XAR7, DP;
* XAR2-XAR6 are preserved.  The stack starts at 0x0400 (M1) as after reset.
* ITRAPIsr (vector 0x3FFFE6, boot ROM version 4+): enable the watchdog and loop until it resets the device.
* The CPU vector table at 0x3FFFC0 (VMAP = 1, ENPIE = 0) holds RESET -> InitBoot, ILLEGAL -> ITRAPIsr and, for every
* other vector, the M0 SARAM debug locations 0x000040 + 2n of SPRU722C Table 1.

* system control (EALLOW) and GPIO
PLLSTS    .set 0x7011
LOSPCP    .set 0x701B
PCLKCR0   .set 0x701C
WDCR      .set 0x7029
GPAQSEL2  .set 0x6F84
GPAQSEL2H .set 0x6F85
GPAMUX1   .set 0x6F86
GPAMUX1H  .set 0x6F87
GPAMUX2   .set 0x6F88
GPAMUX2H  .set 0x6F89
GPADIRH   .set 0x6F8B
GPAPUD    .set 0x6F8C
GPAPUDH   .set 0x6F8D
GPBQSEL1  .set 0x6F92
GPBMUX1   .set 0x6F96
GPBPUD    .set 0x6F9C
GPADAT    .set 0x6FC0
GPADATH   .set 0x6FC1                 ; GPIO16-31 (GPIO18 = bit 2, GPIO27 = bit 11, GPIO29 = bit 13)
GPASETH   .set 0x6FC3
GPACLEARH .set 0x6FC5
GPBDAT    .set 0x6FC8                 ; GPIO32-34 (GPIO34 = bit 2)
* SPI-A / SCI-A (one data page)
SPICCR    .set 0x7040
SPICTL    .set 0x7041
SPISTS    .set 0x7042
SPIBRR    .set 0x7044
SPIRXBUF  .set 0x7047
SPITXBUF  .set 0x7048
SPIFFTX   .set 0x704A
SCICCR    .set 0x7050
SCICTL1   .set 0x7051
SCILBAUD  .set 0x7053
SCICTL2   .set 0x7054
SCIRXST   .set 0x7055
SCIRXBUF  .set 0x7057
SCITXBUF  .set 0x7059
SCIFFTX   .set 0x705A
SCIFFCT   .set 0x705C
* I2C-A
I2CSTR    .set 0x7902
I2CCLKL   .set 0x7903
I2CCLKH   .set 0x7904
I2CCNT    .set 0x7905
I2CDRR    .set 0x7906
I2CSAR    .set 0x7907
I2CDXR    .set 0x7908
I2CMDR    .set 0x7909
I2CPSC    .set 0x790C
I2CFFTX   .set 0x7920
I2CFFRX   .set 0x7921
* eCAN-A (32-bit accesses)
CANME     .set 0x6000
CANMD     .set 0x6002
CANRMP    .set 0x600C
CANMC     .set 0x6014
CANBTC    .set 0x6016
CANES     .set 0x6018
CANGIF0   .set 0x601E
CANGIF1   .set 0x6022
CANTIOC   .set 0x602A
CANRIOC   .set 0x602C
MB1MSGID  .set 0x6108
MB1MSGCTL .set 0x610A
MB1MDL    .set 0x610C

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
        B       loader_sci, NTC
        MOVL    XAR7, #0x3F7FF6       ; 1 1 1: boot to flash
        B       ExitBoot, UNC
m10x:   TBIT    @GPBDAT, #2
        B       loader_i2c, NTC
        B       loader_spi, UNC       ; 1 0 1
m0xx:   TBIT    @GPADATH, #13
        SB      m00x, NTC
        TBIT    @GPBDAT, #2
        SB      to_m0, NTC
        B       loader_ecan, UNC      ; 0 1 1
to_m0:  MOVL    XAR7, #0x000000       ; 0 1 0: M0 SARAM
        B       ExitBoot, UNC
m00x:   TBIT    @GPBDAT, #2
        B       loader_par, NTC
        MOVL    XAR7, #0x3D7800       ; 0 0 1: OTP
        B       ExitBoot, UNC

wd_off: EALLOW
        MOVW    DP, #DP(WDCR)
        MOV     @WDCR, #0x0068        ; WDCHK = 101, WDDIS = 1
        EDIS
        LRETR

* ------------------------------------------------------------------ shared stream routines (Shared_Boot.c)
GetWord:                              ; AL = next word of the stream (tail call through XAR6)
        MOVL    XAR7, @XAR6
        LB      *XAR7
GetLong:                              ; ACC = MSW:LSW
        LCR     GetWord
        MOV     *SP++, AL
        LCR     GetWord
        MOV     AH, *--SP
        LRETR
* the key was accepted: `n` reserved words (AR3 = n - 1 on entry), entry point, blocks, exit
load_rest8:
        MOVB    XAR3, #7              ; ReadReservedFn: 8 words
load_rest:
rsvd:   LCR     GetWord
        BANZ    rsvd, AR3--
        LCR     GetLong               ; entry point
        MOVL    XAR4, @ACC
        LCR     CopyData
to_entry:
        MOVL    XAR7, @XAR4
        B       ExitBoot, UNC
to_flash:
        MOVL    XAR7, #0x3F7FF6       ; invalid key / no device: FLASH_ENTRY_POINT
        B       ExitBoot, UNC
CopyData:                             ; blocks {size, address, data} until size 0
        LCR     GetWord
cd_blk: CMPB    AL, #0
        SB      cd_end, EQ
        MOVZ    AR3, @AL
        SUBB    XAR3, #1
        LCR     GetLong
        MOVL    XAR2, @ACC
cd_w:   LCR     GetWord
        MOV     *XAR2++, AL
        BANZ    cd_w, AR3--
        LCR     GetWord
        SB      cd_blk, UNC
cd_end: LRETR

* ------------------------------------------------------------------ SCI-A (SCI_Boot.c)
loader_sci:
        LCR     wd_off
        EALLOW
        MOVW    DP, #DP(PCLKCR0)
        OR      @PCLKCR0, #0x0400     ; SCIAENCLK
        MOV     @LOSPCP, #0x0002      ; LSPCLK = SYSCLKOUT / 4
        MOVW    DP, #DP(SCIFFTX)
        MOV     @SCIFFTX, #0x8000     ; SCIRST = 1, FIFO off
        MOV     @SCICCR, #0x0007      ; 1 stop bit, no parity, 8 bits
        MOV     @SCICTL1, #0x0003     ; TXENA, RXENA
        MOV     @SCICTL2, #0x0000
        MOV     @SCICTL1, #0x0023     ; out of SW RESET
        MOVW    DP, #DP(GPAPUDH)
        AND     @GPAPUDH, #0xCFFF     ; pull-ups on GPIO28/29
        OR      @GPAMUX2H, #0x0500    ; GPIO28 = SCIRXDA, GPIO29 = SCITXDA
        OR      @GPAQSEL2H, #0x0300   ; GPIO28 asynchronous
        EDIS
        MOVW    DP, #DP(SCILBAUD)
        MOV     @SCILBAUD, #0x0001    ; prime the baud register
        MOV     @SCIFFCT, #0x6000     ; CDC = 1, ABDCLR
sci_abd:
        TBIT    @SCIFFCT, #15         ; ABD: 'A' / 'a' received, baud locked
        SB      sci_abd, NTC
        AND     @SCIFFCT, #0xDFFF     ; CDC = 0
sci_a:  TBIT    @SCIRXST, #6          ; RXRDY
        SB      sci_a, NTC
        MOV     AL, @SCIRXBUF
        AND     AL, @AL, #0x00FF
        MOV     @SCITXBUF, AL         ; echo the autobaud character
        MOVL    XAR6, #sci_getw
        LCR     sci_getw
        CMP     @AL, #0x08AA
        B       to_flash, NEQ
        B       load_rest8, UNC
sci_getw:
        MOVW    DP, #DP(SCIRXST)
sci_w1: TBIT    @SCIRXST, #6
        SB      sci_w1, NTC
        MOV     AL, @SCIRXBUF
        AND     AL, @AL, #0x00FF
        MOV     @SCITXBUF, AL         ; echo LSB
        MOVZ    AR1, @AL
sci_w2: TBIT    @SCIRXST, #6
        SB      sci_w2, NTC
        MOV     AL, @SCIRXBUF
        AND     AL, @AL, #0x00FF
        MOV     @SCITXBUF, AL         ; echo MSB
        LSL     AL, #8
        OR      AL, @AR1
        LRETR

* ------------------------------------------------------------------ SPI-A serial EEPROM (SPI_Boot.c)
loader_spi:
        LCR     wd_off
        EALLOW
        MOVW    DP, #DP(PCLKCR0)
        OR      @PCLKCR0, #0x0100     ; SPIAENCLK
        MOV     @LOSPCP, #0x0002
        MOVW    DP, #DP(SPIFFTX)
        MOV     @SPIFFTX, #0x8000     ; FIFO off (SPIRST only)
        MOV     @SPICCR, #0x0007      ; 8-bit characters, in reset
        MOV     @SPICTL, #0x000E      ; CLK_PHASE = 1, master, talk
        MOV     @SPIBRR, #0x007F      ; slowest baud rate
        MOV     @SPICCR, #0x0087      ; out of reset
        MOVW    DP, #DP(GPAPUDH)
        AND     @GPAPUDH, #0xFFF0     ; pull-ups on GPIO16-19
        OR      @GPAMUX2, #0x0015     ; GPIO16-18 = SPISIMOA, SPISOMIA, SPICLKA
        OR      @GPAQSEL2, #0x003F    ; asynchronous
        AND     @GPAMUX2, #0xFF3F     ; GPIO19 = GPIO (chip select)
        OR      @GPADIRH, #0x0008     ; output
        MOVW    DP, #DP(GPACLEARH)
        MOV     @GPACLEARH, #0x0008   ; chip select low
        EDIS
        MOVL    XAR6, #spi_getw
        MOV     @AL, #0x0300           ; READ command (left-justified 8-bit character)
        LCR     spi_xfer
        LCR     spi_getw              ; 16-bit EEPROM address 0x0000
        LCR     spi_getw
        CMP     @AL, #0x08AA
        B       to_flash, NEQ
        MOV     @AL, #0                ; reserved word 1: LOSPCP (first byte), SPIBRR (second byte)
        LCR     spi_xfer
        EALLOW
        MOVW    DP, #DP(LOSPCP)
        MOV     @LOSPCP, AL
        EDIS
        MOV     @AL, #0
        LCR     spi_xfer
        MOV     @SPIBRR, AL           ; (DP still on the SPI/SCI page)
        MOVB    XAR3, #6              ; 7 more reserved words
spi_rs: LCR     spi_getw
        BANZ    spi_rs, AR3--
        LCR     GetLong
        MOVL    XAR4, @ACC
        LCR     CopyData
        MOVW    DP, #DP(GPASETH)
        MOV     @GPASETH, #0x0008     ; chip select high
        B       to_entry, UNC
spi_xfer:                             ; AL = character to send (left-justified); AL = byte received
        MOVW    DP, #DP(SPITXBUF)
        MOV     @SPITXBUF, AL
spi_wt: TBIT    @SPISTS, #6           ; INT_FLAG
        SB      spi_wt, NTC
        MOV     AL, @SPIRXBUF
        AND     AL, @AL, #0x00FF
        LRETR
spi_getw:
        MOV     @AL, #0
        LCR     spi_xfer
        MOVZ    AR1, @AL              ; LSB
        MOV     @AL, #0
        LCR     spi_xfer
        LSL     AL, #8                ; MSB
        OR      AL, @AR1
        LRETR

* ------------------------------------------------------------------ I2C-A EEPROM at 0x50 (I2C_Boot.c)
loader_i2c:
        LCR     wd_off
        EALLOW
        MOVW    DP, #DP(GPBMUX1)
        AND     @GPBMUX1, #0xFFF0
        OR      @GPBMUX1, #0x0005     ; GPIO32 = SDAA, GPIO33 = SCLA
        AND     @GPBPUD, #0xFFFC      ; pull-ups on
        OR      @GPBQSEL1, #0x000F    ; asynchronous
        MOVW    DP, #DP(PCLKCR0)
        OR      @PCLKCR0, #0x0010     ; I2CAENCLK
        EDIS
        MOVW    DP, #DP(I2CSAR)
        MOV     @I2CSAR, #0x0050
        MOV     @I2CPSC, #0x0000
        MOV     @I2CCLKL, #0x0035
        MOV     @I2CCLKH, #0x0035
        MOV     @I2CMDR, #0x0620      ; MST, TRX, IRS
        MOVW    DP, #DP(I2CFFTX)
        MOV     @I2CFFTX, #0x6000     ; FIFO mode, TX FIFO on
        MOV     @I2CFFRX, #0x2000     ; RX FIFO on
        MOVL    XAR6, #i2c_getw
        MOVW    DP, #DP(I2CCNT)
        MOV     @I2CCNT, #0x0002      ; EEPROM address pointer 0x0000
        MOV     @I2CDXR, #0x0000
        MOV     @I2CDXR, #0x0000
        MOV     @I2CMDR, #0x2620      ; START, master transmitter (no STOP)
i2c_ar: TBIT    @I2CSTR, #2           ; ARDY
        SB      i2c_ar, NTC
        TBIT    @I2CSTR, #1           ; NACK: no EEPROM
        SB      i2c_ok, NTC
        OR      @I2CMDR, #0x0800      ; STP
        B       to_flash, UNC
i2c_ok: LCR     i2c_getw
        CMP     @AL, #0x08AA
        B       to_flash, NEQ
        LCR     i2c_getw
        MOVZ    AR2, @AL              ; I2CPSC
        LCR     i2c_getw
        MOVZ    AR3, @AL              ; I2CCLKH
        LCR     i2c_getw
        MOVZ    AR5, @AL              ; I2CCLKL
        MOVW    DP, #DP(I2CMDR)
        AND     @I2CMDR, #0xFFDF      ; IRS = 0
        MOV     @I2CCLKL, AR5
        MOV     @I2CCLKH, AR3
        MOV     @I2CPSC, AR2
        OR      @I2CMDR, #0x0020      ; IRS = 1
        MOVB    XAR3, #4              ; 5 more reserved words
        B       load_rest, UNC
i2c_getw:
        MOVW    DP, #DP(I2CCNT)
        MOV     @I2CCNT, #0x0002
        MOV     @I2CMDR, #0x2C20      ; START, STOP, master receiver, 2 bytes
i2c_w:  TBIT    @I2CMDR, #11          ; STP clears when the STOP is done
        SB      i2c_w, TC
        MOV     AL, @I2CDRR
        AND     AL, @AL, #0x00FF
        MOVZ    AR1, @AL              ; LSB
        MOV     AL, @I2CDRR
        LSL     AL, #8                ; MSB
        OR      AL, @AR1
        LRETR

* ------------------------------------------------------------------ eCAN-A mailbox 1 (CAN_Boot.c)
loader_ecan:
        LCR     wd_off
        MOVW    DP, #DP(PLLSTS)
can_mc: TBIT    @PLLSTS, #3           ; MCLKSTS: limp mode -> hang (the bit rate would be wrong)
        SB      can_mc, TC
        EALLOW
        OR      @PCLKCR0, #0x4000     ; ECANAENCLK
        MOVW    DP, #DP(GPAMUX2H)
        AND     @GPAMUX2H, #0x0FFF
        OR      @GPAMUX2H, #0x5000    ; GPIO30 = CANRXA, GPIO31 = CANTXA
        AND     @GPAPUDH, #0x3FFF     ; pull-ups on
        OR      @GPAQSEL2H, #0x3000   ; GPIO30 asynchronous
        MOVW    DP, #DP(CANTIOC)
        MOVL    ACC, @CANTIOC
        OR       ACC, #0x0008           ; TXFUNC
        MOVL    @CANTIOC, ACC
        MOVL    ACC, @CANRIOC
        OR       ACC, #0x0008           ; RXFUNC
        MOVL    @CANRIOC, ACC
        MOVW    DP, #DP(MB1MSGCTL)
        MOVB    ACC, #0
        MOVL    @MB1MSGCTL, ACC
        MOVW    DP, #DP(CANRMP)
        MOVB    ACC, #0
        SUBB    ACC, #1               ; 0xFFFFFFFF
        MOVL    @CANRMP, ACC
        MOVL    @CANGIF0, ACC
        MOVL    @CANGIF1, ACC
        MOVL    ACC, @CANMC
        OR       ACC, #0x1000           ; CCR = 1
        MOVL    @CANMC, ACC
can_c1: MOVL    ACC, @CANES
        AND     AL, @AL, #0x0010           ; CCE
        SB      can_c1, EQ
        MOV     @AH, #0x0001           ; BRPREG = 1
        MOV     @AL, #0x00AA           ; SAM, TSEG1REG = 5, TSEG2REG = 2
        MOVL    @CANBTC, ACC
        MOVL    ACC, @CANMC
        AND     AL, @AL, #0xEFFF           ; CCR = 0
        MOVL    @CANMC, ACC
can_c0: MOVL    ACC, @CANES
        AND     AL, @AL, #0x0010
        SB      can_c0, NEQ
        MOVB    ACC, #0
        MOVL    @CANME, ACC
        MOVW    DP, #DP(MB1MSGID)
        MOV     @AH, #0x0004           ; standard ID 0x001 (STDMSGID = bits 28:18)
        MOV     @AL, #0x0000
        MOVL    @MB1MSGID, ACC
        MOVW    DP, #DP(CANMD)
        MOVB    ACC, #2
        MOVL    @CANMD, ACC           ; mailbox 1 receives
        MOVL    @CANME, ACC           ; mailbox 1 enabled
        EDIS
        MOVL    XAR6, #can_getw
        LCR     can_getw
        CMP     @AL, #0x08AA
        B       to_flash, NEQ
        B       load_rest8, UNC
can_getw:
        MOVW    DP, #DP(CANRMP)
can_w:  MOVL    ACC, @CANRMP
        TEST    ACC
        SB      can_w, EQ
        MOVW    DP, #DP(MB1MDL)
        MOVL    ACC, @MB1MDL          ; DBO = 0: AH = BYTE0 (bits 31:24) : BYTE1 (bits 23:16)
        MOV     AL, @AH
        LSR     AL, #8
        MOVZ    AR1, @AL              ; LSB = BYTE0
        MOV     AL, @AH
        LSL     AL, #8                ; MSB = BYTE1
        OR      AL, @AR1
        MOVZ    AR1, @AL
        MOVW    DP, #DP(CANRMP)
        MOVB    ACC, #0
        SUBB    ACC, #1
        MOVL    @CANRMP, ACC          ; clear all RMPn
        MOV     AL, @AR1
        LRETR

* ------------------------------------------------------------------ parallel GPIO0-15 (Parallel_Boot.c)
loader_par:
        LCR     wd_off
        EALLOW
        MOVW    DP, #DP(GPAPUD)
        MOV     @GPAPUD, #0x0000      ; pull-ups on GPIO0-15
        AND     @GPAPUDH, #0xF3FF     ; and GPIO26/27
        MOV     @GPAMUX1, #0x0000     ; GPIO0-15 = GPIO
        MOV     @GPAMUX1H, #0x0000
        AND     @GPAMUX2H, #0xFF0F    ; GPIO26/27 = GPIO
        OR      @GPADIRH, #0x0400     ; GPIO26 (DSP control) output
        AND     @GPADIRH, #0xF7FF     ; GPIO27 (host control) input
        EDIS
        LCR     par_get16
        CMP     @AL, #0x10AA
        SB      par_16, EQ
        AND     AL, @AL, #0x00FF
        MOVZ    AR2, @AL
        LCR     par_get16
        LSL     AL, #8
        OR      AL, @AR2
        CMP     @AL, #0x08AA
        B       to_flash, NEQ
        MOVL    XAR6, #par_get8
        B       load_rest8, UNC
par_16: MOVL    XAR6, #par_get16
        B       load_rest8, UNC
par_get16:
        MOVW    DP, #DP(GPACLEARH)
        MOV     @GPACLEARH, #0x0400   ; DSP ready (GPIO26 low)
par_w1: TBIT    @GPADATH, #11         ; wait for the host: GPIO27 low = data valid
        SB      par_w1, TC
        MOV     AL, @GPADAT
        MOV     @GPASETH, #0x0400     ; read done (GPIO26 high)
par_w2: TBIT    @GPADATH, #11         ; wait for the host acknowledge (GPIO27 high)
        SB      par_w2, NTC
        LRETR
par_get8:
        LCR     par_get16
        AND     AL, @AL, #0x00FF
        MOVZ    AR1, @AL              ; LSB
        LCR     par_get16
        LSL     AL, #8                ; MSB from GPIO0-7
        OR      AL, @AR1
        LRETR

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
                                      ; SP is back at 0x0400 (every LCR above returned)
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
