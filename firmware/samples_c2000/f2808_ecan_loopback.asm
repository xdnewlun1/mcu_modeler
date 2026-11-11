* f2808_ecan_loopback.asm -- TMS320F2808: eCAN-A self-test mode (CANMC.STM = 1: the module acknowledges its own
* frames, nothing reaches the pins) at 500 kbit/s.  The 280x eCAN module clock is SYSCLKOUT (SPRUEU0 3.1.2), so
* CANBTC BRPreg = 19 (5 MHz time quanta at 100 MHz), TSEG1reg = 6, TSEG2reg = 1 -> 10 tq per bit.
* Mailbox 0 (transmit) sends standard ID 0x123 with 4 bytes 0x11223344; mailbox 16 (receive) accepts ID 0x123.
* The frame is sent every 1 ms (CPU timer 0 polled); each time mailbox 16 has a message (CANRMP.16) the data is copied
* to can_mdl_lo/can_mdl_hi, can_rx counts the frames and GPIO34 toggles.  CANTA.0 is acknowledged (can_tx counts).
* Registers are accessed 32 bits wide (eCAN rule, SPRUEU0 2.4).

WDCR      .set 0x7029
PLLSTS    .set 0x7011
PLLCR     .set 0x7021
PCLKCR0   .set 0x701C
GPBDIR    .set 0x6F9A
GPBTOGGLE .set 0x6FCE
TIMER0PRD  .set 0x0C02
TIMER0TCR  .set 0x0C04
CANME     .set 0x6000
CANMD     .set 0x6002
CANTRS    .set 0x6004
CANTA     .set 0x6008
CANRMP    .set 0x600C
CANMC     .set 0x6014
CANBTC    .set 0x6016
CANES     .set 0x6018
MB0_MSGID   .set 0x6100
MB0_MSGCTRL .set 0x6102
MB0_MDL     .set 0x6104
MB16_MSGID  .set 0x6180
MB16_MSGCTRL .set 0x6182
MB16_MDL    .set 0x6184
LED       .set 0x0004

        .sect "ramvars", 0x0100
can_mdl_lo: .word 0
can_mdl_hi: .word 0
can_rx:     .word 0
can_tx:     .word 0

        .sect "codestart", 0x3F7FF6
        LB      _c_int00

        .sect "text", 0x3E8000
_c_int00:
        SETC    INTM
        EALLOW
        MOVW    DP, #DP(WDCR)
        MOV     @WDCR, #0x0068
        AND     @PLLSTS, #0xFFFD
        MOV     @PLLCR, #10
pll:    TBIT    @PLLSTS, #0
        SB      pll, NTC              ; 100 MHz
        OR      @PCLKCR0, #0x4000     ; ECANAENCLK (bit 14)
        MOVW    DP, #DP(GPBDIR)
        OR      @GPBDIR, #LED
* eCAN-A: request configuration mode, self-test, bit timing (EALLOW)
        MOVW    DP, #DP(CANMC)
        MOV     @AH, #0x0000           ; SCB (eCAN mode) | CCR | STM
        MOV     @AL, #0x3040
        MOVL    @CANMC, ACC
cce1:   MOVL    ACC, @CANES
        TBIT    @AL, #4               ; CCE
        SB      cce1, NTC
        MOV     @AH, #0x0013           ; BRPreg 19, TSEG1reg 6, TSEG2reg 1
        MOV     @AL, #0x0031
        MOVL    @CANBTC, ACC
        EDIS
* mailboxes (CANME = 0 while they are written); the mailbox RAM (0x6100) is on other DP pages than the registers
        MOVB    ACC, #0
        MOVL    @CANME, ACC
        MOVW    DP, #DP(MB0_MSGID)
        MOV     @AH, #0x048C          ; standard ID 0x123 << 18
        MOV     @AL, #0x0000
        MOVL    @MB0_MSGID, ACC
        MOVB    ACC, #4               ; DLC 4
        MOVL    @MB0_MSGCTRL, ACC
        MOV     @AH, #0x1122
        MOV     @AL, #0x3344
        MOVL    @MB0_MDL, ACC
        MOVW    DP, #DP(MB16_MSGID)
        MOV     @AH, #0x048C
        MOV     @AL, #0x0000
        MOVL    @MB16_MSGID, ACC
        MOVW    DP, #DP(CANMD)
        MOV     @AH, #0x0001          ; mailbox 16 receives
        MOV     @AL, #0x0000
        MOVL    @CANMD, ACC
        MOV     @AH, #0x0001          ; enable mailboxes 0 and 16
        MOV     @AL, #0x0001
        MOVL    @CANME, ACC
        EALLOW
        MOV     @AH, #0x0000           ; leave configuration mode (CCR = 0), keep SCB | STM
        MOV     @AL, #0x2040
        MOVL    @CANMC, ACC
cce0:   MOVL    ACC, @CANES
        TBIT    @AL, #4
        SB      cce0, TC
        EDIS
* CPU timer 0: 1 ms period, polled through TIF
        MOVW    DP, #DP(TIMER0TCR)
        MOV     @TIMER0TCR, #0x0010
        MOV     @AH, #0x0001
        MOV     @AL, #0x869F
        MOVL    @TIMER0PRD, ACC
        MOV     @TIMER0TCR, #0x8020   ; clear TIF, reload, run
send:   MOVW    DP, #DP(CANTRS)
        MOV     @AH, #0x0000
        MOV     @AL, #0x0001
        MOVL    @CANTRS, ACC           ; transmit request mailbox 0
wait:   MOVL    ACC, @CANRMP
        TBIT    @AH, #0               ; CANRMP.16
        SB      wait, NTC
        MOV     @AH, #0x0001
        MOV     @AL, #0x0000
        MOVL    @CANRMP, ACC           ; clear (write 1)
        MOVW    DP, #DP(MB16_MDL)
        MOVL    ACC, @MB16_MDL
        MOVW    DP, #DP(can_mdl_lo)
        MOV     @can_mdl_lo, AL
        MOV     @can_mdl_hi, AH
        INC     @can_rx
        MOVW    DP, #DP(GPBTOGGLE)
        MOV     @GPBTOGGLE, #LED
        MOVW    DP, #DP(CANTA)
txw:    MOVL    ACC, @CANTA
        TBIT    @AL, #0
        SB      txw, NTC
        MOV     @AH, #0x0000
        MOV     @AL, #0x0001
        MOVL    @CANTA, ACC
        MOVW    DP, #DP(can_tx)
        INC     @can_tx
        MOVW    DP, #DP(TIMER0TCR)
tmr:    TBIT    @TIMER0TCR, #15       ; TIF
        SB      tmr, NTC
        OR      @TIMER0TCR, #0x8000   ; clear TIF (write 1)
        SB      send, UNC
