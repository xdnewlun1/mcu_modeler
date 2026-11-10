"""stm32.can (bxCAN, RM0090 ch. 32) at the register level on the STM32F405ZG description: modes (sleep / init /
normal handshakes), bit timing from PCLK1, transmit mailboxes (priority, abort, NART), receive FIFOs (FMP/FULL/FOVR,
lock mode), the shared filter banks (CAN2SB, mask/list at 16/32 bit, FMI), errors (ESR, bus off, recovery),
interrupts, loop back / silent, pins and clocks, against a host node on the bus (chipem/canbus.py)."""
from __future__ import annotations

import pytest

from chipem.canbus import CanFrame
from test_stm32 import RCC, desc, spin_chip, warns  # noqa: F401  (desc: fixture)

CAN1, CAN2 = 0x40006400, 0x40006800
GPIOB = 0x40020400
MCR, MSR, TSR, RF0R, RF1R, IER, ESR, BTR = 0x0, 0x4, 0x8, 0xC, 0x10, 0x14, 0x18, 0x1C
FMR, FM1R, FS1R, FFA1R, FA1R = 0x200, 0x204, 0x20C, 0x214, 0x21C
INRQ, SLEEP, TXFP, RFLM, NART, AWUM, ABOM, TTCM, RESET = 1, 2, 4, 8, 16, 32, 64, 128, 1 << 15
INAK, SLAK, ERRI, WKUI, SLAKI = 1, 2, 4, 8, 16
BIT = 2000.0                                              # 500 kbit/s
BTR_500K = 1 | (12 << 16) | (1 << 20)                     # 16 MHz / 2 = 8 MHz tq, 1 + 13 + 2 = 16 tq
IRQ = {"CAN1_TX": 19, "CAN1_RX0": 20, "CAN1_RX1": 21, "CAN1_SCE": 22, "CAN2_TX": 63, "CAN2_RX0": 64}


def TI(b, k): return b + 0x180 + 0x10 * k
def TDT(b, k): return b + 0x184 + 0x10 * k
def TDL(b, k): return b + 0x188 + 0x10 * k
def TDH(b, k): return b + 0x18C + 0x10 * k
def RI(b, f): return b + 0x1B0 + 0x10 * f
def RDT(b, f): return b + 0x1B4 + 0x10 * f
def RDL(b, f): return b + 0x1B8 + 0x10 * f
def FR(i, j): return CAN1 + 0x240 + 8 * i + 4 * (j - 1)


def w(c, a, v):
    c.bus.write(a, 4, v & 0xFFFFFFFF)


def r(c, a):
    return c.bus.read(a, 4)


def irq(c, name):
    return 16 + IRQ[name] in c.arch.asserted


def chip(desc, pins=True, can2=False):
    c = spin_chip(desc, clock_hz=16e6)
    w(c, RCC + 0x30, r(c, RCC + 0x30) | 2)                # GPIOB
    w(c, RCC + 0x40, r(c, RCC + 0x40) | (1 << 25) | ((1 << 26) if can2 else 0))
    if pins:
        moder, afrh = r(c, GPIOB), r(c, GPIOB + 0x24)
        moder |= (2 << 16) | (2 << 18)                    # PB8 CAN1_RX, PB9 CAN1_TX
        afrh |= 0x99
        if can2:
            moder |= (2 << 24) | (2 << 26)                # PB12 CAN2_RX, PB13 CAN2_TX
            afrh |= 0x99 << 16
        w(c, GPIOB, moder)
        w(c, GPIOB + 0x24, afrh)
    return c


def init(c, b=CAN1, mcr=0, btr=BTR_500K):
    w(c, b + MCR, INRQ | mcr)                             # from sleep: INRQ takes precedence
    assert r(c, b + MSR) & INAK
    w(c, b + MCR, INRQ | mcr)                             # clear SLEEP
    w(c, b + BTR, btr)


def accept_all(c, bank=0, fifo=0):
    w(c, CAN1 + FMR, r(c, CAN1 + FMR) | 1)
    w(c, CAN1 + FS1R, r(c, CAN1 + FS1R) | (1 << bank))
    w(c, CAN1 + FFA1R, (r(c, CAN1 + FFA1R) & ~(1 << bank)) | (fifo << bank))
    w(c, FR(bank, 1), 0)
    w(c, FR(bank, 2), 0)
    w(c, CAN1 + FA1R, r(c, CAN1 + FA1R) | (1 << bank))
    w(c, CAN1 + FMR, r(c, CAN1 + FMR) & ~1)


def start(c, b=CAN1):
    w(c, b + MCR, r(c, b + MCR) & ~INRQ)


def setup(desc, mcr=0, btr=BTR_500K, host=True, filt=True):
    c = chip(desc)
    blk = c.peripherals["CAN1"]
    init(c, CAN1, mcr, btr)
    if filt:
        accept_all(c)
    h = blk.can_bus.add_host() if host else None
    return c, blk, h


def send(c, k, ident, data=b"", ext=False, rtr=False, b=CAN1, tgt=False):
    d = data + bytes(8 - len(data))
    w(c, TDT(b, k), len(data) | ((1 << 8) if tgt else 0))
    w(c, TDL(b, k), int.from_bytes(d[:4], "little"))
    w(c, TDH(b, k), int.from_bytes(d[4:], "little"))
    w(c, TI(b, k), ((ident << 3 | 4) if ext else ident << 21) | (int(rtr) << 1) | 1)


# ------------------------------------------------------------------------------------------------ modes / timing
def test_reset_state_and_init_sleep_handshakes(desc):
    c = chip(desc)
    blk = c.peripherals["CAN1"]
    assert r(c, CAN1 + MCR) == 0x00010002 and r(c, CAN1 + MSR) == 0xC02 and r(c, CAN1 + TSR) == 0x1C000000
    assert r(c, CAN1 + FMR) == 0x2A1C0E01                 # FINIT, CAN2SB = 14
    w(c, CAN1 + BTR, BTR_500K)                            # not in init mode: ignored
    assert r(c, CAN1 + BTR) == 0x01230000 and any("BTR written outside" in m for m in warns(c))   # RM0090 32.9.2
    w(c, CAN1 + MCR, INRQ | SLEEP)                        # HAL: INRQ first, SLEEP still set
    assert r(c, CAN1 + MSR) & (INAK | SLAK) == INAK | SLAK
    w(c, CAN1 + MCR, INRQ)
    assert r(c, CAN1 + MSR) & (INAK | SLAK) == INAK
    w(c, CAN1 + BTR, BTR_500K)
    assert blk.bitrate() == pytest.approx(500e3) and blk.can_bit_ns() == pytest.approx(BIT)
    t0 = c.now_ns()
    w(c, CAN1 + MCR, 0)                                   # leave init: 11 recessive bits
    probe = []
    c.sched.at_ns(t0 + 11 * BIT - 100, lambda: probe.append(r(c, CAN1 + MSR) & INAK))
    c.sched.at_ns(t0 + 11 * BIT + 100, lambda: probe.append(r(c, CAN1 + MSR) & INAK))
    c.run_for(100e3)
    assert probe == [INAK, 0] and blk.mode == "normal"
    w(c, CAN1 + IER, 1 << 17)                             # SLKIE
    w(c, CAN1 + MCR, SLEEP)
    assert r(c, CAN1 + MSR) & (SLAK | SLAKI | INAK) == SLAK | SLAKI and irq(c, "CAN1_SCE")
    w(c, CAN1 + MSR, SLAKI)                               # rc_w1
    assert not r(c, CAN1 + MSR) & SLAKI and not irq(c, "CAN1_SCE")


def test_bit_rate_follows_the_rcc_apb1_clock(desc):
    c, blk, _h = setup(desc)
    assert blk.bitrate() == pytest.approx(500e3)
    w(c, RCC + 0x08, r(c, RCC + 0x08) | (4 << 10))        # PPRE1 /2: PCLK1 = 8 MHz
    assert blk.bitrate() == pytest.approx(250e3)


def test_rx_pin_not_muxed_keeps_inak_until_configured(desc):
    c = chip(desc, pins=False)
    init(c)
    start(c)
    c.run_for(200e3)
    assert r(c, CAN1 + MSR) & INAK and any("CAN1_RX is not muxed" in m for m in warns(c))
    w(c, GPIOB, r(c, GPIOB) | (2 << 16))                  # PB8 AF9 = CAN1_RX
    w(c, GPIOB + 0x24, 0x9)
    r(c, CAN1 + MSR)                                      # the polling read retries the synchronisation
    c.run_for(100e3)
    assert not r(c, CAN1 + MSR) & INAK


def test_clock_gate_and_rcc_reset(desc):
    c, blk, _h = setup(desc)
    w(c, RCC + 0x40, r(c, RCC + 0x40) & ~(1 << 25))
    w(c, CAN1 + IER, 1)
    assert any("CAN1" in m for m in warns(c))
    w(c, RCC + 0x40, r(c, RCC + 0x40) | (1 << 25))
    w(c, RCC + 0x20, 1 << 25)                             # APB1RSTR.CAN1RST
    w(c, RCC + 0x20, 0)
    assert r(c, CAN1 + MCR) == 0x00010002 and r(c, CAN1 + FA1R) == 0 and blk.mode == "sleep"


# ------------------------------------------------------------------------------------------------ transmit
def test_transmit_timing_tsr_flags_and_tx_irq(desc):
    c, blk, h = setup(desc)
    w(c, CAN1 + IER, 1)                                   # TMEIE
    start(c)
    t0 = c.now_ns()
    send(c, 0, 0x123, b"\xde\xad\xbe\xef")
    tsr = r(c, CAN1 + TSR)
    assert not tsr & (1 << 26) and (tsr >> 24) & 3 == 1   # TME0 = 0, CODE = next free = 1
    c.run_for(1e6)
    f = CanFrame(0x123, b"\xde\xad\xbe\xef")
    (t_end, got), = h.received
    assert got == f and t_end == pytest.approx(t0 + 11 * BIT + f.bits * BIT, abs=20)
    tsr = r(c, CAN1 + TSR)
    assert tsr & 0x3 == 0x3 and tsr & (7 << 26) == 7 << 26     # RQCP0, TXOK0, all empty
    assert not r(c, TI(CAN1, 0)) & 1 and irq(c, "CAN1_TX")
    w(c, CAN1 + TSR, 1)                                   # RQCP0 w1: clears RQCP/TXOK/ALST/TERR
    assert r(c, CAN1 + TSR) & 0xF == 0 and not irq(c, "CAN1_TX")
    assert blk.can_stats()["tx"] == 1 and r(c, CAN1 + ESR) == 0


def test_mailbox_priority_by_identifier_or_request_order_and_code_low(desc):
    c, blk, h = setup(desc)
    send(c, 0, 0x300, b"\x00")
    send(c, 1, 0x100, b"\x01")
    send(c, 2, 0x200, b"\x02")
    tsr = r(c, CAN1 + TSR)
    assert (tsr >> 24) & 3 == 0 and tsr >> 29 == 1        # all pending: CODE = lowest priority (0), LOW0
    w(c, TDL(CAN1, 0), 0x55)                              # write-protected while pending
    assert r(c, TDL(CAN1, 0)) == 0
    start(c)
    c.run_for(2e6)
    assert [f.id for _t, f in h.received] == [0x100, 0x200, 0x300]
    c2, _b, h2 = setup(desc, mcr=TXFP)
    send(c2, 0, 0x300, b"\x00")
    send(c2, 2, 0x100, b"\x02")
    send(c2, 1, 0x200, b"\x01")
    start(c2)
    c2.run_for(2e6)
    assert [f.id for _t, f in h2.received] == [0x300, 0x100, 0x200]


def test_extended_remote_and_data_byte_order(desc):
    c, blk, h = setup(desc)
    start(c)
    send(c, 0, 0x1ABCDEF0, b"\x01\x02\x03\x04\x05", ext=True)
    c.run_for(1e6)
    send(c, 1, 0x7F, rtr=True)
    w(c, TDT(CAN1, 1), 3)                                 # pending: ignored (DLC 0 stays)
    c.run_for(1e6)
    assert [str(f) for _t, f in h.received] == ["1ABCDEF0#0102030405", "07F#R"]


def test_no_ack_retries_terr_error_passive_and_nart(desc):
    c, blk, _h = setup(desc, host=False)
    w(c, CAN1 + IER, (1 << 15) | (1 << 11) | (1 << 9))   # ERRIE, LECIE, EPVIE
    start(c)
    send(c, 0, 0x10, b"\x01")
    c.run_for(3e6)
    esr = r(c, CAN1 + ESR)
    assert (esr >> 4) & 7 == 3 and esr >> 16 & 0xFF == 128 and esr & 3 == 3     # ACK error, TEC 128, EWGF, EPVF
    tsr = r(c, CAN1 + TSR)
    assert tsr & 0xF == 0x8 and r(c, TI(CAN1, 0)) & 1     # TERR0, still pending (automatic retransmission)
    assert r(c, CAN1 + MSR) & ERRI and irq(c, "CAN1_SCE")
    c2, _b, _h2 = setup(desc, mcr=NART, host=False)
    start(c2)
    send(c2, 0, 0x10, b"\x01")
    c2.run_for(1e6)
    tsr = r(c2, CAN1 + TSR)
    assert tsr & 0xF == RQCP_TERR and tsr & (1 << 26)    # one attempt: RQCP, TXOK = 0, TERR, empty
    assert r(c2, CAN1 + ESR) >> 16 & 0xFF == 8


RQCP_TERR = 0x9


def test_abort_request(desc):
    c, blk, _h = setup(desc, host=False)
    start(c)
    c.run_for(30e3)
    send(c, 0, 0x10, b"\x01")
    send(c, 1, 0x20, b"\x02")
    c.run_for(20 * BIT)                                   # mailbox 0 on the bus
    w(c, CAN1 + TSR, 0x80 << 8)                           # ABRQ1: not on the bus, aborted at once
    tsr = r(c, CAN1 + TSR)
    assert (tsr >> 8) & 0xFF == 0x01 and tsr & (1 << 27)  # RQCP1, TXOK1 = 0, TME1
    w(c, CAN1 + TSR, 0x80)                                # ABRQ0: on the bus, decided by the attempt (ACK error)
    assert r(c, CAN1 + TSR) & 0x80 and r(c, TI(CAN1, 0)) & 1
    c.run_for(200e3)
    tsr = r(c, CAN1 + TSR)
    assert tsr & 0xFF == RQCP_TERR and tsr & (1 << 26) and blk.can_bus.error_frames == 1


def test_arbitration_lost_sets_alst_and_nart_gives_up(desc):
    c, blk, h = setup(desc, mcr=NART)
    start(c)
    c.run_for(30e3)
    h.send(CanFrame(0x001, b"\x01"))
    send(c, 0, 0x400, b"\x02")                            # both ready at the same SOF: 0x001 wins
    c.run_for(1e6)
    tsr = r(c, CAN1 + TSR)
    assert tsr & 0xF == 0x5 and h.received == []          # RQCP0, ALST0, TXOK0 = 0: not retried


def test_ttcm_timestamps_and_tgt(desc):
    c, blk, h = setup(desc, mcr=TTCM)
    t_sync = c.now_ns() + 11 * BIT                        # the CAN timer starts with Normal mode
    start(c)
    c.run_for(100e3)
    send(c, 0, 0x12, bytes(range(8)), tgt=True)
    c.run_for(1e6)
    t_sof = h.received[0][0] - h.received[0][1].bits * BIT
    tm = r(c, TDT(CAN1, 0)) >> 16
    assert tm == int((t_sof - t_sync) // BIT) and 30 <= tm <= 45
    assert h.received[0][1].data[6:] == bytes((tm & 0xFF, tm >> 8))


# ------------------------------------------------------------------------------------------------ receive
def test_fifo_fmp_full_overrun_release_and_rx_irq(desc):
    c, blk, h = setup(desc)
    w(c, CAN1 + IER, 0xE)                                 # FMPIE0, FFIE0, FOVIE0
    start(c)
    c.run_for(30e3)
    h.send(CanFrame(0x101, b"\x11\x22\x33\x44\x55"))
    c.run_for(400e3)
    assert r(c, CAN1 + RF0R) == 1 and irq(c, "CAN1_RX0")
    assert r(c, RI(CAN1, 0)) == 0x101 << 21 and r(c, RDT(CAN1, 0)) & 0xFFFF == 5   # DLC 5, FMI 0
    assert r(c, RDL(CAN1, 0)) == 0x44332211
    for i in range(3):
        h.send(CanFrame(0x102 + i, bytes([i])))
    c.run_for(2e6)
    assert r(c, CAN1 + RF0R) == 0x3 | 0x8 | 0x10          # FMP 3, FULL, FOVR
    w(c, CAN1 + RF0R, 0x20)                               # RFOM
    assert r(c, CAN1 + RF0R) & 3 == 2 and r(c, RI(CAN1, 0)) >> 21 == 0x102
    w(c, CAN1 + RF0R, 0x20)
    assert r(c, RI(CAN1, 0)) >> 21 == 0x104               # RFLM = 0: the last message was overwritten by 0x104
    w(c, CAN1 + RF0R, 0x38)                               # release + clear FULL, FOVR
    assert r(c, CAN1 + RF0R) == 0 and not irq(c, "CAN1_RX0")
    assert blk.rx_lost == 1


def test_fifo_locked_mode_discards_new_message(desc):
    c, blk, h = setup(desc, mcr=RFLM)
    start(c)
    c.run_for(30e3)
    for i in range(4):
        h.send(CanFrame(0x200 + i, bytes([i])))
    c.run_for(2e6)
    got = []
    while r(c, CAN1 + RF0R) & 3:
        got.append(r(c, RI(CAN1, 0)) >> 21)
        w(c, CAN1 + RF0R, 0x20)
    assert got == [0x200, 0x201, 0x202]


def test_filter_modes_scales_fifo_assignment_and_fmi(desc):
    c, blk, h = setup(desc, filt=False)
    w(c, CAN1 + FMR, r(c, CAN1 + FMR) | 1)
    # bank 0: 16-bit list (FIFO0, FMI 0-3); bank 1: 32-bit mask ext 0x1800xxxx (FIFO1, FMI 0); bank 2: 32-bit list
    # (FIFO0, FMI 4-5); bank 3: 16-bit mask (FIFO1, FMI 1-2), inactive: still numbered; bank 4: 16-bit mask FIFO1
    # (FMI 3-4): std 0x3x0
    w(c, CAN1 + FM1R, 0b00101)
    w(c, CAN1 + FS1R, 0b00110)
    w(c, CAN1 + FFA1R, 0b11010)
    w(c, FR(0, 1), (0x111 << 5 << 16) | (0x110 << 5))
    w(c, FR(0, 2), (0x113 << 5 << 16) | (0x112 << 5))
    w(c, FR(1, 1), (0x18000000 << 3) | 4)
    w(c, FR(1, 2), (0x1FFF0000 << 3) | 4)                 # compare the top 13 ID bits and IDE
    w(c, FR(2, 1), 0x7AA << 21)
    w(c, FR(2, 2), (0x7AB << 21) | 2)                     # remote frame 0x7AB
    w(c, FR(4, 1), 0)                                     # first 16-bit mask: matches nothing useful (mask 0 = all!)
    w(c, FR(4, 2), ((0x70F << 5 | 0x18) << 16) | (0x300 << 5))   # compare ID bits 10:4, RTR, IDE: 0x30x data std
    w(c, CAN1 + FA1R, 0b10111)
    w(c, CAN1 + FMR, r(c, CAN1 + FMR) & ~1)
    w(c, FR(0, 1), 0)                                     # active and FINIT = 0: ignored
    assert r(c, FR(0, 1)) != 0 and any("filter bank 0" in m for m in warns(c))
    start(c)
    c.run_for(30e3)
    frames = [CanFrame(0x112, b"\x01"), CanFrame(0x18001234, b"\x02", ext=True), CanFrame(0x7AB, rtr=True),
              CanFrame(0x7AB, b"\x03"), CanFrame(0x305, b"\x04"), CanFrame(0x7AA, b"\x05")]
    for f in frames:
        h.send(f)
        c.run_for(400e3)
    got = {0: [], 1: []}
    for f in (0, 1):
        while r(c, CAN1 + RF0R + 4 * f) & 3:
            got[f].append((r(c, RI(CAN1, f)), r(c, RDT(CAN1, f)) >> 8 & 0xFF))
            w(c, CAN1 + RF0R + 4 * f, 0x20)
    assert got[0] == [((0x112 << 21), 2), ((0x7AB << 21) | 2, 5), (0x7AA << 21, 4)]
    # 0x305 matches bank 4's FR1 pair (mask 0: everything) before its FR2 pair -> FMI 3; 0x7AB data: bank 4 FMI 3
    assert got[1] == [((0x18001234 << 3) | 4, 0), (0x7AB << 21, 3), (0x305 << 21, 3)]


def test_filter_priority_32bit_before_16bit_and_list_before_mask(desc):
    c, blk, h = setup(desc, filt=False)
    w(c, CAN1 + FMR, r(c, CAN1 + FMR) | 1)
    w(c, CAN1 + FS1R, 0b110)                              # bank 0 16-bit mask (all), bank 1 32-bit mask, 2 32-bit list
    w(c, CAN1 + FM1R, 0b100)
    w(c, FR(0, 1), 0)
    w(c, FR(0, 2), 0)
    w(c, FR(1, 1), 0)
    w(c, FR(1, 2), 0)
    w(c, FR(2, 1), 0x55 << 21)
    w(c, FR(2, 2), 0x56 << 21)
    w(c, CAN1 + FA1R, 0b111)
    w(c, CAN1 + FMR, r(c, CAN1 + FMR) & ~1)
    start(c)
    c.run_for(30e3)
    h.send(CanFrame(0x56, b"\x01"))                       # 32-bit list (FMI 2 + 1 + 1 = 4) wins
    h.send(CanFrame(0x57, b"\x02"))                       # 32-bit mask (FMI 2)
    c.run_for(1e6)
    fmis = []
    while r(c, CAN1 + RF0R) & 3:
        fmis.append(r(c, RDT(CAN1, 0)) >> 8 & 0xFF)
        w(c, CAN1 + RF0R, 0x20)
    assert fmis == [4, 2]


def test_finit_blocks_reception_and_config_guards(desc):
    c, blk, h = setup(desc)
    start(c)
    c.run_for(30e3)
    w(c, CAN1 + FS1R, 0)                                  # FINIT = 0: ignored
    assert r(c, CAN1 + FS1R) == 1
    w(c, CAN1 + FMR, r(c, CAN1 + FMR) | 1)
    h.send(CanFrame(0x10, b"\x01"))
    c.run_for(400e3)
    assert r(c, CAN1 + RF0R) == 0 and blk.rx_lost == 1 and len(h.sent) == 1   # still acknowledged


def test_can2_shares_filters_from_can2sb_and_needs_can1_clock(desc):
    c = chip(desc, can2=True)
    c1, c2 = c.peripherals["CAN1"], c.peripherals["CAN2"]
    init(c, CAN1)
    init(c, CAN2)
    w(c, CAN1 + FMR, (r(c, CAN1 + FMR) & ~0x3F00) | 1 | (2 << 8))     # CAN2SB = 2
    w(c, CAN1 + FS1R, 0b1111)
    w(c, CAN1 + FM1R, 0b1111)                             # 32-bit lists
    w(c, FR(0, 1), 0x100 << 21)                           # CAN1, FMI 0
    w(c, FR(1, 1), 0x101 << 21)                           # CAN1, FMI 2
    w(c, FR(2, 1), 0x200 << 21)                           # CAN2, FMI 0 (numbered from CAN2SB)
    w(c, FR(3, 2), 0x201 << 21)                           # CAN2, FMI 3
    w(c, CAN1 + FA1R, 0b1111)
    w(c, CAN1 + FMR, r(c, CAN1 + FMR) & ~1)
    assert c1.filter_match(CanFrame(0x101, b"")) == (0, 2) and c1.filter_match(CanFrame(0x200, b"")) is None
    assert c1.filter_match(CanFrame(0x201, b""), slave=True) == (0, 3)
    w(c, CAN2 + FMR, 0)                                   # CAN2 has no filter registers
    assert r(c, CAN2 + FMR) == 0 and any("only in CAN1" in m for m in warns(c))
    from chipem.canbus import CanBus
    bus = CanBus("B")
    c1.can_attach(bus)
    c2.can_attach(bus)
    h = bus.add_host()
    start(c, CAN1)
    start(c, CAN2)
    c.run_for(30e3)
    for ident in (0x100, 0x201, 0x101, 0x200):
        h.send(CanFrame(ident, b"\x01"))
    c.run_for(2e6)
    assert r(c, CAN1 + RF0R) & 3 == 2 and r(c, CAN2 + RF0R) & 3 == 2
    assert r(c, RI(CAN2, 0)) >> 21 == 0x200 and r(c, RDT(CAN2, 0)) >> 8 & 0xFF == 0    # host: arbitration order
    w(c, CAN2 + RF0R, 0x20)
    assert r(c, RI(CAN2, 0)) >> 21 == 0x201 and r(c, RDT(CAN2, 0)) >> 8 & 0xFF == 3
    send(c, 0, 0x333, b"\x33", b=CAN2)                    # CAN2 transmits; CAN1 acknowledges (normal bus member)
    c.run_for(1e6)
    assert any(f.id == 0x333 for _t, f in h.received) and r(c, CAN2 + TSR) & 3 == 3
    # CAN1 clock off: CAN2 drops off the bus as well
    w(c, RCC + 0x40, r(c, RCC + 0x40) & ~(1 << 25))
    assert c2.can_rx_state(bus) is None


# ------------------------------------------------------------------------------------------------ errors / modes
def test_tx_pin_unmuxed_bit_errors_bus_off_and_abom_recovery(desc):
    c = chip(desc)
    w(c, GPIOB, r(c, GPIOB) & ~(3 << 18))                 # PB9 back to input: CAN1_TX not muxed
    blk = c.peripherals["CAN1"]
    init(c, mcr=ABOM)
    w(c, CAN1 + IER, (1 << 15) | (1 << 10))               # ERRIE, BOFIE
    start(c)
    send(c, 0, 0x1, b"\x01")
    c.run_for(3e6)
    esr = r(c, CAN1 + ESR)
    assert blk.errors.busoff and esr & 4 and (esr >> 4) & 7 == 5 and irq(c, "CAN1_SCE")
    t_off = [e.t_ns for e in c.log if e.kind == "info" and "bus off (" in e.fields.get("msg", "")][0]
    w(c, GPIOB, r(c, GPIOB) | (2 << 18))                  # fix the pin; ABOM recovers after 128 x 11 bits
    c.run_for(t_off + 128 * 11 * BIT + 1e3 - c.now_ns())
    assert not blk.errors.busoff and r(c, CAN1 + ESR) & 0xFFFF0007 == 0


def test_bus_off_without_abom_needs_init_sequence(desc):
    c = chip(desc)
    w(c, GPIOB, r(c, GPIOB) & ~(3 << 18))
    blk = c.peripherals["CAN1"]
    init(c)
    start(c)
    send(c, 0, 0x1, b"\x01")
    c.run_for(5e6)
    assert blk.errors.busoff
    c.run_for(5e6)
    assert blk.errors.busoff                              # no automatic recovery
    w(c, CAN1 + MCR, INRQ)
    w(c, CAN1 + MCR, 0)
    t0 = c.now_ns()
    c.run_for(128 * 11 * BIT - 2e3)
    assert blk.errors.busoff
    c.run_for(5e3)
    assert not blk.errors.busoff and c.now_ns() - t0 > 128 * 11 * BIT


def test_init_request_waits_for_frame_on_the_bus(desc):
    c, blk, h = setup(desc)
    start(c)
    c.run_for(30e3)
    send(c, 0, 0x50, b"\x01\x02")
    c.run_for(20 * BIT)
    w(c, CAN1 + MCR, INRQ)
    assert not r(c, CAN1 + MSR) & INAK                    # our frame is on the bus
    c.run_for(200e3)
    assert r(c, CAN1 + MSR) & INAK and len(h.received) == 1 and r(c, CAN1 + TSR) & 3 == 3


def test_sleep_wakeup_on_bus_activity_awum(desc):
    c, blk, h = setup(desc, mcr=AWUM)
    w(c, CAN1 + IER, 1 << 16)                             # WKUIE
    start(c)
    c.run_for(30e3)
    w(c, CAN1 + MCR, AWUM | SLEEP)
    assert r(c, CAN1 + MSR) & SLAK
    blk.can_bus.add_host("other")                          # acknowledges while the bxCAN sleeps
    h.send(CanFrame(0x10, b"\x01"))                        # wakes the bxCAN, but is lost for it
    c.run_for(400e3)
    msr = r(c, CAN1 + MSR)
    assert msr & WKUI and irq(c, "CAN1_SCE") and not msr & SLAK and not r(c, CAN1 + MCR) & SLEEP
    assert r(c, CAN1 + RF0R) == 0
    h.send(CanFrame(0x11, b"\x02"))
    c.run_for(400e3)
    assert r(c, CAN1 + RF0R) == 1


def test_loopback_silent_modes(desc):
    c, blk, h = setup(desc, btr=BTR_500K | (1 << 30))     # LBKM: own frames, no ACK needed
    start(c)
    send(c, 0, 0x42, b"\x99")
    c.run_for(1e6)
    assert r(c, CAN1 + TSR) & 3 == 3 and r(c, CAN1 + RF0R) == 1 and r(c, RDL(CAN1, 0)) == 0x99
    assert h.received == []
    c2, b2, h2 = setup(desc, btr=BTR_500K | (1 << 31))    # SILM: receives, never acknowledges or transmits
    start(c2)
    c2.run_for(30e3)
    send(c2, 0, 0x43, b"\x01")
    b2.can_bus.add_host("other")                           # someone else acknowledges the host's frame
    h2.send(CanFrame(0x44, b"\x02"))
    c2.run_for(1e6)
    assert r(c2, CAN1 + RF0R) == 1 and h2.received == [] and r(c2, TI(CAN1, 0)) & 1
    assert b2.can_rx_state(b2.can_bus) == "listen"


def test_master_reset_keeps_filters(desc):
    c, blk, h = setup(desc)
    w(c, CAN1 + IER, 0xFF)
    w(c, CAN1 + MCR, RESET)
    assert r(c, CAN1 + MCR) == 0x00010002 and r(c, CAN1 + IER) == 0 and r(c, CAN1 + FA1R) == 1
    assert blk.mode == "sleep"


def test_can_stats_and_find_can(desc):
    c, blk, _h = setup(desc)
    assert c.find_can("can1") == "CAN1" and set(c.can_blocks()) == {"CAN1", "CAN2"}
    st = blk.can_stats()
    assert st["state"] == "init" and st["bitrate"] == pytest.approx(500e3)


# ------------------------------------------------------------------------------------------------ sample / boards
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STM32_ECHO = ROOT / "firmware/samples_stm32/build/can_echo.elf"
K22_ECHO = ROOT / "firmware/samples/build/can_echo.elf"
C2K_ECHO = ROOT / "firmware/samples_c2000_c/build/can_echo.out"


@pytest.mark.skipif(not STM32_ECHO.exists() or not K22_ECHO.exists(), reason="can_echo samples not built")
def test_k22_stm32_can_board_echo():
    """boards/k22_stm32_can.yaml: the K22 sends 100#<n>4B32 every 10 ms; the STM32's filter bank 0 puts it in FIFO 0,
    the CAN1_RX0 interrupt queues it and the main loop echoes it as 202#<same data>.  Exact frame lengths at 2 us/bit."""
    from chipem.session import Session
    s = Session.from_board(ROOT / "boards" / "k22_stm32_can.yaml", quiet=True)
    try:
        s.run_for(25e6)
        evs = s.events(kinds={"can"})
        k22 = [e for e in evs if e.fields["chip"] == "A"]
        stm = [e for e in evs if e.fields["chip"] == "B"]
        assert [e.fields["data"] for e in k22] == [f"{n:04X}4B32" for n in range(2)]
        assert [(e.fields["id"], e.fields["data"]) for e in stm] == [(0x202, f"{n:04X}4B32") for n in range(2)]
        for e in k22 + stm:
            f = CanFrame(e.fields["id"], bytes.fromhex(e.fields["data"]))
            assert e.t_ns - e.fields["sof_ns"] == pytest.approx(f.bits * BIT) and not e.fields.get("err")
        for a, b in zip(k22, stm):
            assert a.t_ns + 3 * BIT <= b.fields["sof_ns"] < a.t_ns + 300e3       # ISR + echo at 16 MHz
        out = s.chips["B"].uart_out("USART1").decode()
        assert out.startswith("CAN ECHO STM32\r\nRX 100 [4] 00 00 4B 32 F00\r\nTX 202\r\nRX 100 [4] 00 01 4B 32 F00\r\n")
        assert "RX 202 [4] 00 01 4B 32\r\n" in s.chips["A"].uart_out("UART0").decode()
        cl = s.board.can_links[0]
        assert cl.bus.frames == 4 and cl.bus.error_frames == 0
        st = s.chips["B"].peripherals["CAN1"].can_stats()
        assert st == {"bitrate": pytest.approx(500e3), "tx": 2, "rx": 2, "lost": 0, "tec": 0, "rec": 0,
                      "state": "active", "bus": "CANBUS"}
        assert not [e for e in s.events(kinds={"warn"}) if e.fields.get("chip") == "B"]
    finally:
        s.close()


@pytest.mark.skipif(not (STM32_ECHO.exists() and K22_ECHO.exists() and C2K_ECHO.exists()),
                    reason="can_echo samples not built (clang / TI cl2000)")
def test_three_family_can_board():
    """boards/k22_c2000_stm32_can.yaml: both echoes of each K22 frame; 0x201 (eCAN-A) wins the arbitration or is
    simply ready first, 0x202 (bxCAN) follows; the K22 prints both."""
    from chipem.session import Session
    s = Session.from_board(ROOT / "boards" / "k22_c2000_stm32_can.yaml", quiet=True)
    try:
        s.run_for(25e6)
        evs = s.events(kinds={"can"})
        ids = [e.fields["id"] for e in evs]
        assert ids == [0x100, 0x201, 0x202] * 2
        assert all(not e.fields.get("err") for e in evs)
        out = s.chips["A"].uart_out("UART0").decode()
        assert "RX 201 [4] 00 00 4B 32\r\nRX 202 [4] 00 00 4B 32\r\nTX 1\r\n" in out
        assert s.board.can_links[0].bus.error_frames == 0
    finally:
        s.close()


def test_cli_summary_shows_bxcan(capsys):
    if not (STM32_ECHO.exists() and K22_ECHO.exists()):
        pytest.skip("can_echo samples not built")
    from chipem.cli import main
    assert main(["run", "--board", str(ROOT / "boards" / "k22_stm32_can.yaml"), "--for", "15ms", "--summary-only"]) == 0
    out = capsys.readouterr().out
    assert "B|   CAN1: 500 kbit/s, 1 tx, 1 rx, TEC 0 REC 0, active" in out and "CAN2" not in out
    assert "can CANBUS: A.CAN0, B.CAN1: 2 frames" in out


def test_txrq_clears_rqcp_and_status_bits(desc):
    """RM0090 32.9.2 CAN_TSR: RQCPx is cleared by hardware when TXRQ is set again (and with it TXOK/ALST/TERR)."""
    c, blk, h = setup(desc)
    w(c, CAN1 + IER, 1)                                   # TMEIE
    start(c)
    send(c, 0, 0x123, b"\x01")
    c.run_for(1e6)
    assert r(c, CAN1 + TSR) & 3 == 3 and irq(c, "CAN1_TX")
    send(c, 0, 0x124, b"\x02")                            # new request without clearing RQCP0 first
    assert r(c, CAN1 + TSR) & 0xF == 0 and not irq(c, "CAN1_TX")
    c.run_for(1e6)
    assert r(c, CAN1 + TSR) & 3 == 3 and len(h.received) == 2
