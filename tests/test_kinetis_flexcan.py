"""kinetis.flexcan (K22 CAN0) at the register level on the real MK22FN1M0VLL12 description: modes, bit timing, Tx/Rx
message buffers, masks, lock, remote frames, abort, Rx FIFO, errors and interrupts, with exact bus timing
(chipem/canbus.py) against a host node."""
from __future__ import annotations

from pathlib import Path

import pytest

from chipem.canbus import CanFrame
from chipem.chip import Chip
from chipem.chipdesc import load_chip

from engine_util import build_image

ROOT = Path(__file__).resolve().parents[1]
B = 0x40024000
MCR, CTRL1, TIMER, RXMGMASK, RX14MASK, ECR, ESR1, IMASK1, IFLAG1, CTRL2, ESR2, CRCR, RXFGMASK, RXFIR = \
    0x0, 0x4, 0x8, 0x10, 0x14, 0x1C, 0x20, 0x28, 0x30, 0x34, 0x38, 0x44, 0x48, 0x4C
BIT = 2000.0                                     # 500 kbit/s
CTRL1_500K = (5 << 24) | (1 << 22) | (7 << 19) | (2 << 16) | (1 << 13) | 7     # 60 MHz / 6 / 20 tq


def CS(n): return B + 0x80 + 16 * n
def ID(n): return B + 0x84 + 16 * n
def W0(n): return B + 0x88 + 16 * n
def W1(n): return B + 0x8C + 16 * n
def RXIMR(n): return B + 0x880 + 4 * n


def k22() -> Chip:
    c = Chip(load_chip(ROOT / "chips" / "mk22fn1m0vll12.yaml"))
    img, _ = build_image({"reset": "spin:\n b spin\n"})
    c.load_firmware(img)
    c.reset()
    c.peripherals["MCG"].frozen = True
    c.clock.set_hz(120e6)
    c.bus.write(0x40048044, 4, (1 << 24) | (4 << 16))     # SIM_CLKDIV1: bus = core / 2 = 60 MHz
    return c


def w(c, a, v):
    c.bus.write(a, 4, v & 0xFFFFFFFF)


def r(c, a):
    return c.bus.read(a, 4)


def pins(c, on=True):
    w(c, 0x4004803C, r(c, 0x4004803C) | (1 << 4))        # SIM_SCGC6 FLEXCAN0
    w(c, 0x40048038, r(c, 0x40048038) | (1 << 10))       # PORTB clock
    if on:
        w(c, 0x4004A048, 2 << 8)                          # PTB18 CAN0_TX
        w(c, 0x4004A04C, 2 << 8)                          # PTB19 CAN0_RX


def freeze(c, mcr_extra=0, ctrl1=CTRL1_500K):
    w(c, B + CTRL1, 1 << 13)                              # CLKSRC (disable mode only)
    w(c, B + MCR, 0x5000000F | mcr_extra)                 # enable, FRZ+HALT
    w(c, B + CTRL1, ctrl1)
    for n in range(16):
        w(c, CS(n), 0)


def run(c):
    w(c, B + MCR, r(c, B + MCR) & ~((1 << 30) | (1 << 28)))


def setup(mcr_extra=0, host=True, ctrl1=CTRL1_500K):
    c = k22()
    pins(c)
    freeze(c, mcr_extra, ctrl1)
    blk = c.peripherals["CAN0"]
    h = blk.can_bus.add_host() if host else None
    return c, blk, h


def tx(c, n, ident, data=b"", code=0xC, ext=False, rtr=False, prio=0):
    w(c, CS(n), 0x8 << 24)
    w(c, ID(n), (prio << 29) | (ident if ext else ident << 18))
    d = data + bytes(8 - len(data))
    w(c, W0(n), int.from_bytes(d[:4], "big"))
    w(c, W1(n), int.from_bytes(d[4:], "big"))
    w(c, CS(n), (code << 24) | ((1 << 22 | 1 << 21) if ext else 0) | (int(rtr) << 20) | (len(data) << 16))


def rx(c, n, ident, ext=False):
    w(c, ID(n), ident if ext else ident << 18)
    w(c, CS(n), (0x4 << 24) | ((1 << 21) if ext else 0))


# ------------------------------------------------------------------------------------------------ modes / timing
def test_reset_disable_freeze_handshake_and_freeze_only_fields():
    c = k22()
    pins(c)
    assert r(c, B + MCR) == 0xD890000F                   # MDIS, FRZ, HALT, NOTRDY, SUPV, LPMACK, MAXMB 15
    w(c, B + MCR, 0x5000000F)
    assert r(c, B + MCR) & (1 << 24) and r(c, B + MCR) & (1 << 27) and not r(c, B + MCR) & (1 << 20)
    w(c, B + CTRL1, CTRL1_500K)
    assert c.peripherals["CAN0"].bitrate() == pytest.approx(0)   # CLKSRC only writable while disabled
    w(c, B + MCR, 0xD000000F)
    w(c, B + CTRL1, CTRL1_500K)
    w(c, B + MCR, 0x5000000F)
    assert c.peripherals["CAN0"].bitrate() == pytest.approx(500e3)
    w(c, B + MCR, 0x0000000F)                            # leave freeze
    assert not r(c, B + MCR) & ((1 << 24) | (1 << 27))
    w(c, B + CTRL1, 0)                                   # bit timing is freeze-only
    w(c, B + RXMGMASK, 0)
    assert r(c, B + CTRL1) == CTRL1_500K and r(c, B + RXMGMASK) == 0xFFFFFFFF


def test_oscillator_clock_needs_erclken():
    c, blk, _h = setup(ctrl1=(0 << 24) | (7 << 19) | (2 << 16) | 3)   # CLKSRC = 0: OSCERCLK, 16 tq
    w(c, B + MCR, 0xD000000F)
    w(c, B + CTRL1, (0 << 24) | (7 << 19) | (2 << 16) | 3)
    assert blk.bitrate() == 0                           # OSC0_CR.ERCLKEN = 0
    c.bus.write(0x40065000, 1, 0x80)                    # OSC0_CR.ERCLKEN
    assert blk.bitrate() == pytest.approx(blk.clock_hz() / 16)
    assert blk.clock_hz() == pytest.approx(float(c.peripherals["MCG"].ext_hz))


def test_transmit_timing_codes_timestamp_crc_and_iflag():
    c, blk, h = setup()
    w(c, B + IMASK1, 1 << 8)
    run(c)
    t0 = c.now_ns()
    tx(c, 8, 0x123, b"\xde\xad\xbe\xef")
    c.run_for(1e6)
    f = CanFrame(0x123, b"\xde\xad\xbe\xef")
    sof = t0 + 11 * BIT                                  # bus integration after leaving freeze
    (t_end, got), = h.received
    assert got == f and t_end == pytest.approx(sof + 78 * BIT, abs=20)
    cs = r(c, CS(8))
    assert cs >> 24 == 0x8 and (cs >> 16) & 0xF == 4 and cs & 0xFFFF == 13     # INACTIVE, TIMER at SOF+2 bits
    assert r(c, B + IFLAG1) == 1 << 8 and r(c, B + CRCR) == (8 << 16) | f.crc
    from chipem.periph.kinetis._util import irq_number
    assert irq_number(c, "CAN0_ORed_Message_buffer") + 16 in c.arch.pending
    ev = [e for e in c.log if e.kind == "can"]
    assert len(ev) == 1 and ev[0].fields["data"] == "DEADBEEF" and ev[0].fields["sof_ns"] == pytest.approx(sof, abs=20)


def test_internal_arbitration_lowest_id_or_lowest_buffer_or_prio():
    c, blk, h = setup()
    run(c)
    tx(c, 3, 0x300, b"\x03")
    tx(c, 5, 0x100, b"\x05")
    tx(c, 7, 0x200, b"\x07", prio=0)
    c.run_for(2e6)
    assert [f.id for _t, f in h.received] == [0x100, 0x200, 0x300]
    c2, _blk, h2 = setup(mcr_extra=1 << 13)              # LPRIOEN: PRIO in front of the ID
    run(c2)
    tx(c2, 3, 0x300, b"\x03", prio=0)
    tx(c2, 5, 0x100, b"\x05", prio=2)
    c2.run_for(2e6)
    assert [f.id for _t, f in h2.received] == [0x300, 0x100]
    c3, _blk, h3 = setup(ctrl1=CTRL1_500K | (1 << 4))    # LBUF: lowest buffer first
    run(c3)
    tx(c3, 5, 0x100, b"\x05")
    tx(c3, 3, 0x300, b"\x03")
    c3.run_for(2e6)
    assert [f.id for _t, f in h3.received] == [0x300, 0x100]


def test_receive_global_mask_movein_and_overrun():
    c, blk, h = setup()
    w(c, B + RXMGMASK, 0x1FFC0000 & ~(0xF << 18))        # ignore the low 4 ID bits
    rx(c, 2, 0x120)
    run(c)
    c.run_for(100e3)
    h.send(CanFrame(0x12A, b"\x11\x22\x33"))
    c.run_for(400e3)
    assert (r(c, ID(2)) >> 18) == 0x12A and r(c, W0(2)) == 0x11223300     # ID/data reads do not lock
    assert r(c, B + IFLAG1) & (1 << 2)
    h.send(CanFrame(0x125, b"\x44"))                    # not serviced (C/S not read): OVERRUN
    c.run_for(400e3)
    cs = r(c, CS(2))
    assert cs >> 24 == 0x6 and (cs >> 16) & 0xF == 1 and r(c, W0(2)) >> 24 == 0x44
    r(c, B + TIMER)                                      # unlock (serviced)
    h.send(CanFrame(0x121, b"\x45"))
    c.run_for(400e3)
    assert r(c, CS(2)) >> 24 == 0x2                      # serviced OVERRUN -> FULL
    h.send(CanFrame(0x320, b"\x55"))                    # no match: not stored
    c.run_for(400e3)
    assert blk.rx_lost == 1


def test_individual_masks_queue_and_lock():
    c, blk, h = setup(mcr_extra=1 << 16)                 # IRMQ
    for n in (4, 5):
        w(c, RXIMR(n), 0x1FFC0000)
        rx(c, n, 0x0AA)
    run(c)
    c.run_for(100e3)
    for v in (1, 2):
        h.send(CanFrame(0x0AA, bytes([v])))
        c.run_for(400e3)
    assert r(c, W0(4)) >> 24 == 1 and r(c, W0(5)) >> 24 == 2     # queue: first free MB, then the next
    r(c, CS(5))                                          # lock MB5 (FULL)
    h.send(CanFrame(0x0AA, b"\x03"))                    # nothing free: the last match (MB5) but it is locked
    c.run_for(400e3)
    assert r(c, W0(5)) >> 24 == 2
    r(c, B + TIMER)                                      # unlock: the pending frame moves in
    assert r(c, W0(5)) >> 24 == 3 and r(c, CS(5)) >> 24 == 0x2   # it had been serviced (read + unlocked)


def test_remote_request_answered_by_ranswer_mb():
    c, blk, h = setup()
    tx(c, 6, 0x55, b"\x01\x02", code=0xA)                # RANSWER
    run(c)
    c.run_for(100e3)
    h.send(CanFrame(0x55, rtr=True, dlc=2))
    c.run_for(1e6)
    assert [str(f) for _t, f in h.received] == ["055#0102"]
    assert r(c, CS(6)) >> 24 == 0xA and r(c, B + IFLAG1) & (1 << 6)


def test_remote_frame_tx_turns_mb_into_rx():
    c, blk, h = setup()
    run(c)
    tx(c, 1, 0x77, b"", rtr=True)
    w(c, CS(1), (0xC << 24) | (1 << 20) | (2 << 16))
    c.run_for(1e6)
    assert str(h.received[0][1]) == "077#R2" and r(c, CS(1)) >> 24 == 0x4
    r(c, B + TIMER)                                      # IRMQ = 0: reading an EMPTY C/S word locked it
    h.send(CanFrame(0x77, b"\x09\x08"))
    c.run_for(1e6)
    assert r(c, CS(1)) >> 24 == 0x2 and r(c, W0(1)) >> 16 == 0x0908


def test_abort_with_aen():
    c, blk, _h = setup(mcr_extra=1 << 12, host=False)    # AEN, nobody acknowledges
    run(c)
    tx(c, 2, 0x10, b"\x01")
    tx(c, 3, 0x20, b"\x02")
    c.run_for(30e3 + 11 * BIT)                          # MB2 is on the bus now
    w(c, CS(3), 0x9 << 24)                              # not transmitting: inactivated at once, IFLAG
    assert r(c, CS(3)) >> 24 == 0x9 and r(c, B + IFLAG1) & (1 << 3)
    w(c, CS(2), 0x9 << 24)                              # on the bus: blocked, pending until the ACK error
    assert r(c, CS(2)) >> 24 == 0xC
    c.run_for(200e3)
    assert r(c, CS(2)) >> 24 == 0x9 and r(c, B + IFLAG1) & (1 << 2)
    assert r(c, B + ESR1) & (1 << 13)                   # ACKERR


def test_rx_fifo_format_a_flags_and_overflow():
    c, blk, h = setup(mcr_extra=(1 << 29))              # RFEN, IDAM format A, RFFN 0: 8 filters at 0xE0
    w(c, B + RXFGMASK, 0xFFFFFFFF)
    w(c, B + 0xE0, 0x300 << 19)                         # element 0: std 0x300
    w(c, B + 0xE4, 0x301 << 19)                         # element 1: std 0x301
    for k in range(2, 8):
        w(c, B + 0xE0 + 4 * k, 0x7FF << 19)
    run(c)
    c.run_for(100e3)
    for i in range(7):
        h.send(CanFrame(0x300 + (i & 1), bytes([i])))
    c.run_for(3e6)
    fl = r(c, B + IFLAG1)
    assert fl & (1 << 5) and fl & (1 << 6) and fl & (1 << 7)   # available, warning (5th), overflow (7th)
    got = []
    while r(c, B + IFLAG1) & (1 << 5):
        got.append((r(c, B + 0x84) >> 18, r(c, B + 0x88) >> 24, r(c, B + RXFIR)))
        w(c, B + IFLAG1, 1 << 5)
    # all seven were ready at once: the host sends them in arbitration order (0x300 x 4, then 0x301), the 7th is lost
    assert got == [(0x300, 0, 0), (0x300, 2, 0), (0x300, 4, 0), (0x300, 6, 0), (0x301, 1, 1), (0x301, 3, 1)]


def test_no_ack_errors_irq_and_error_passive():
    c, blk, _h = setup(mcr_extra=1 << 21, host=False)  # WRNEN
    w(c, B + CTRL1, CTRL1_500K | (1 << 14) | (1 << 11))    # ERRMSK, TWRNMSK
    run(c)
    tx(c, 0, 0x1, b"\x01")
    c.run_for(3e6)
    assert r(c, B + ECR) & 0xFF == 128
    esr = r(c, B + ESR1)
    assert (esr >> 4) & 3 == 1 and esr & (1 << 13) and esr & (1 << 9) and esr & (1 << 17) and esr & 2
    assert not r(c, B + ESR1) & (1 << 13)               # error bits cleared by the read
    from chipem.periph.kinetis._util import irq_number
    for name in ("CAN0_Error", "CAN0_Tx_Warning"):
        assert irq_number(c, name) + 16 in c.arch.pending, name


def test_unmuxed_pins_give_bit0_errors_and_bus_off():
    c = k22()
    pins(c, on=False)
    freeze(c, ctrl1=CTRL1_500K | (1 << 6))              # BOFFREC: stay bus off
    run(c)
    tx(c, 0, 0x1, b"\x01")
    c.run_for(3e6)
    esr = r(c, B + ESR1)
    assert esr & (1 << 14) and esr & (1 << 2) and (esr >> 4) & 3 >= 2   # BIT0ERR, BOFFINT, bus off
    blk = c.peripherals["CAN0"]
    assert blk.errors.busoff and blk.can_bus.frames == 0


def test_loopback_self_reception():
    c, blk, h = setup(ctrl1=CTRL1_500K | (1 << 12), host=True)     # LPB
    rx(c, 1, 0x42)
    run(c)
    tx(c, 0, 0x42, b"\x99")
    c.run_for(1e6)
    assert r(c, CS(0)) >> 24 == 0x8 and r(c, CS(1)) >> 24 == 0x2 and r(c, W0(1)) >> 24 == 0x99
    assert h.received == []                             # nothing on the external bus


def test_soft_reset():
    c, blk, h = setup()
    w(c, B + IMASK1, 0xFF)
    w(c, B + MCR, r(c, B + MCR) | (1 << 25))
    assert not r(c, B + MCR) & (1 << 25) and r(c, B + IMASK1) == 0 and r(c, B + CTRL1) == CTRL1_500K
