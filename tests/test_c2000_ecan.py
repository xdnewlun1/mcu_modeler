"""ti_c2000.ecan (F28232 eCAN-A/B) at the register level on the TMS320F28232 description and the fake C28x core:
init handshake, EALLOW, bit timing, mailboxes (priority, masks, OPC/RML, DBO), remote frames, abort, self-test,
interrupts through the PIE, errors and time-outs, with exact bus timing against a host node."""
from __future__ import annotations

from pathlib import Path

import pytest

from chipem.canbus import CanFrame
from chipem.chipdesc import load_chip

from c28x_fake import make_fake_chip

CHIP = Path(__file__).resolve().parent.parent / "chips" / "tms320f28232.yaml"
EALLOW = 1 << 6
A = 0x6000
ME, MD, TRS, TRR, TA, AA, RMP, RML, RFP, GAM, MC, BTC, ES, TEC, REC, GIF0, GIM, GIF1, MIM, MIL, OPC, TIOC, RIOC, \
    TSC, TOC, TOS = range(0, 0x34, 2)
BIT = 2000.0
BTC_500K = (9 << 16) | (6 << 3) | 1              # SYSCLKOUT 100 MHz / 2 / 10 / 10 tq


def mbox(n, part=0):
    return A + 0x100 + 8 * n + 2 * part


def wr(c, a, v):
    c.bus.write(a, 2, v & 0xFFFFFFFF)


def rd(c, a):
    return c.bus.read(a, 2)


def chip(base=A, init=True, ecan_mode=True, gpio=True):
    c = make_fake_chip(load_chip(CHIP))
    c.reset()
    c.clock.set_hz(100e6)                          # SYSCLKOUT 100 MHz (the samples' PLL setting)
    c.core.regs["st1"] |= EALLOW
    c.bus.write(0x7029, 1, 0x0068)
    c.bus.write(0x701C, 1, c.bus.read(0x701C, 1) | (1 << 14) | (1 << 15))   # ECANA/BENCLK
    if gpio:
        wr(c, 0x6F88, rd(c, 0x6F88) | (1 << 28) | (1 << 30))                  # GPIO30/31 CANRXA/CANTXA
        wr(c, 0x6F86, rd(c, 0x6F86) | (2 << 16) | (2 << 20))                   # GPIO8/10 CANTXB/CANRXB
    wr(c, base + TIOC, 8)
    wr(c, base + RIOC, 8)
    wr(c, base + BTC, BTC_500K)
    if init:
        wr(c, base + MC, 0x2000 if ecan_mode else 0)                           # leave init (SCB = eCAN mode)
        run(c, 11 * BIT + 1000)
    return c


def run(c, ns):
    c.run_cycles(int(ns * c.clock.hz / 1e9) + 1)


def host(c, name="ECANA"):
    return c.peripherals[name].can_bus.add_host()


def tx_mb(c, n, ident, data=b"", tpl=0, ext=False, base=A):
    off = base - A
    wr(c, mbox(n) + off, (1 << 31 | ident) if ext else ident << 18)
    wr(c, mbox(n, 1) + off, (tpl << 8) | len(data))
    d = data + bytes(8 - len(data))
    wr(c, mbox(n, 2) + off, int.from_bytes(d[:4], "big"))
    wr(c, mbox(n, 3) + off, int.from_bytes(d[4:], "big"))
    wr(c, base + ME, rd(c, base + ME) | 1 << n)


def rx_mb(c, n, ident, lam=None, ctrl=None):
    wr(c, A + ME, rd(c, A + ME) & ~(1 << n))       # identifier / control writes need a disabled mailbox
    wr(c, mbox(n), ((1 << 30) if lam is not None else 0) | ident << 18)
    if ctrl is not None:
        wr(c, mbox(n, 1), ctrl)
    if lam is not None:
        wr(c, A + 0x40 + 2 * n, lam)
    wr(c, A + MD, rd(c, A + MD) | 1 << n)
    wr(c, A + ME, rd(c, A + ME) | 1 << n)


# ------------------------------------------------------------------------------------------------ init / EALLOW
def test_reset_values_and_init_handshake():
    c = chip(init=False)
    assert rd(c, A + MC) == 0x1000 and rd(c, A + ES) == 0x00400010        # CCR, CCE + SA1
    e = c.peripherals["ECANA"]
    assert e.bitrate() == pytest.approx(500e3)
    wr(c, A + MC, 0x2000)
    assert rd(c, A + ES) & 0x10                                           # CCE waits for 11 bit times
    run(c, 10 * BIT)
    assert rd(c, A + ES) & 0x10
    run(c, 2 * BIT)
    assert not rd(c, A + ES) & 0x10
    wr(c, A + BTC, 0)                                                     # only in initialization mode
    assert rd(c, A + BTC) == BTC_500K
    wr(c, A + MC, 0x3000)
    assert rd(c, A + ES) & 0x10


def test_invalid_btc_never_leaves_init_and_eallow_protection():
    c = make_fake_chip(load_chip(CHIP))
    c.reset()
    c.core.regs["st1"] |= EALLOW
    wr(c, A + MC, 0)
    run(c, 1e6)
    assert rd(c, A + ES) & 0x10
    c.core.regs["st1"] &= ~EALLOW
    wr(c, A + BTC, BTC_500K)                       # EALLOW-protected: ignored
    wr(c, A + MC, 0x2000 | 0x1F)                   # SCB protected, MBNR not
    assert rd(c, A + BTC) == 0 and rd(c, A + MC) == 0x1F
    assert any("EALLOW" in str(ev.get("msg")) for ev in c.log if ev.kind == "warn")


# ------------------------------------------------------------------------------------------------ transmit
def test_transmit_timing_ta_mots_and_priority():
    c = chip()
    h = host(c)
    tx_mb(c, 1, 0x111, b"\x01", tpl=1)
    tx_mb(c, 2, 0x222, b"\x02", tpl=5)             # higher TPL first despite the higher ID
    tx_mb(c, 3, 0x333, b"\x03", tpl=5)             # same TPL: higher mailbox number first
    t0 = c.now_ns()
    wr(c, A + TRS, 0b1110)
    run(c, 2e6)
    assert [f.id for _t, f in h.received] == [0x333, 0x222, 0x111]
    t_end, f = h.received[0]
    assert t_end == pytest.approx(t0 + f.bits * BIT, abs=20)
    assert rd(c, A + TA) == 0b1110 and rd(c, A + TRS) == 0
    assert rd(c, A + 0x80 + 2 * 3) == pytest.approx((t_end - (c.now_ns() - 0)) / BIT + rd(c, A + TSC), abs=2)


def test_trs_write_zero_has_no_effect_and_ta_clears_by_one():
    c = chip()
    host(c)
    tx_mb(c, 0, 0x10, b"\x00")
    wr(c, A + TRS, 1)
    wr(c, A + TRS, 0)
    run(c, 1e6)
    assert rd(c, A + TA) == 1
    wr(c, A + TA, 0)
    assert rd(c, A + TA) == 1
    wr(c, A + TA, 1)
    assert rd(c, A + TA) == 0


def test_trr_aborts_pending_request_and_sets_aaif():
    c = chip()                                     # no other node: frames are never acknowledged
    tx_mb(c, 4, 0x40, b"\x04")
    tx_mb(c, 5, 0x50, b"\x05")
    wr(c, A + GIM, (1 << 14) | 1)                  # AAIM, I0EN
    wr(c, A + TRS, 0x30)
    run(c, 10 * BIT)                               # mailbox 5 (higher number) is on the bus
    wr(c, A + TRR, 0x30)
    assert rd(c, A + AA) == 0x10 and rd(c, A + TRS) == 0x20             # 4 aborted at once, 5 still on the bus
    run(c, 200e3)
    assert rd(c, A + AA) == 0x30 and rd(c, A + TRS) == 0                # 5 aborted after its ACK error
    assert rd(c, A + GIF0) & (1 << 14) and c.arch.pie.ifr[9] & (1 << 4)  # AAIF0 -> PIE 9.5
    assert rd(c, A + ES) & (1 << 19)               # ACKE


def test_write_denied_and_cdr():
    c = chip()
    tx_mb(c, 0, 0x10, b"\x00")
    wr(c, mbox(0), 0x20 << 18)                     # enabled: identifier write denied
    assert rd(c, mbox(0)) == 0x10 << 18 and rd(c, A + GIF0) & (1 << 13)
    wr(c, A + TRS, 1)
    wr(c, mbox(0, 2), 0x12345678)                  # TRS set: data write denied
    assert rd(c, mbox(0, 2)) == 0
    wr(c, A + MC, 0x2000 | 0x100 | 0)              # CDR with MBNR = 0
    wr(c, mbox(0, 2), 0x12345678)
    assert rd(c, mbox(0, 2)) == 0x12345678


# ------------------------------------------------------------------------------------------------ receive
def test_receive_masks_highest_mailbox_first_opc_rml_and_dbo():
    c = chip()
    h = host(c)
    rx_mb(c, 3, 0x120, lam=0xF << 18)              # LAM: low 4 ID bits don't care
    rx_mb(c, 7, 0x120, lam=0xF << 18)
    wr(c, A + OPC, 1 << 7)
    for i in range(3):
        h.send(CanFrame(0x12A + i, bytes([i, 0xEE])))
        run(c, 200e3)
    assert rd(c, A + RMP) == (1 << 7) | (1 << 3)    # 7 first; protected -> 3; 3 overwritten
    assert rd(c, A + RML) == 1 << 3
    assert (rd(c, mbox(7)) >> 18) & 0x7FF == 0x12A and rd(c, mbox(7, 2)) == 0x00EE0000
    assert rd(c, mbox(7)) >> 30 == 1                # AME kept, IDE = 0
    assert (rd(c, mbox(3)) >> 18) & 0x7FF == 0x12C and rd(c, mbox(3, 1)) & 0xF == 2
    wr(c, A + RMP, 1 << 3)                         # clears RMP3 and RML3
    assert rd(c, A + RML) == 0 and rd(c, A + RMP) == 1 << 7
    wr(c, A + MC, 0x2000 | (1 << 10))              # DBO: least significant byte first
    h.send(CanFrame(0x120, b"\x01\x02\x03\x04\x05"))
    run(c, 300e3)
    assert rd(c, mbox(3, 2)) == 0x04030201 and rd(c, mbox(3, 3)) & 0xFF == 0x05


def test_scc_mode_uses_gam_and_16_mailboxes():
    c = chip(ecan_mode=False)
    h = host(c)
    wr(c, A + MC, 0x1000)                          # back to init to write CANGAM
    run(c, 1e3)
    wr(c, A + GAM, 0xFF << 18)
    wr(c, A + MC, 0)
    run(c, 30e3)
    rx_mb(c, 10, 0x300, lam=0)                     # AME: SCC mailboxes 6-15 use CANGAM
    h.send(CanFrame(0x3AB, b"\x01"))
    run(c, 300e3)
    assert rd(c, A + RMP) == 1 << 10


def test_mailbox_interrupt_levels_gmif_miv_and_repulse():
    c = chip()
    h = host(c)
    rx_mb(c, 2, 0x10)
    rx_mb(c, 9, 0x10, lam=0x1FFFFFFF)
    wr(c, A + MIM, (1 << 2) | (1 << 9))
    wr(c, A + MIL, 1 << 9)                         # mailbox 9 on line 1
    wr(c, A + GIM, 3)
    h.send(CanFrame(0x10, b"\x01"))
    run(c, 300e3)
    assert rd(c, A + GIF1) == (1 << 15) | 9 and c.arch.pie.ifr[9] & (1 << 5)    # ECAN1INTA = 9.6
    h.send(CanFrame(0x10, b"\x02"))                # 9 is full but not protected: overwritten; then mailbox 2?
    run(c, 300e3)
    assert rd(c, A + RMP) == 1 << 9 and rd(c, A + RML) == 1 << 9
    rx_mb(c, 2, 0x10)
    wr(c, A + OPC, 1 << 9)
    h.send(CanFrame(0x10, b"\x03"))
    run(c, 300e3)
    assert rd(c, A + RMP) == (1 << 9) | (1 << 2)
    assert rd(c, A + GIF0) == (1 << 15) | (1 << 11) | 2        # GMIF0, MIV 2, RMLIF (RML9, GIL = 0)
    wr(c, A + RMP, 1 << 9)
    assert c.peripherals["ECANA"]._lines == [True, False]     # line 1 released, line 0 still active
    wr(c, A + ME, rd(c, A + ME) & ~(1 << 9))       # mailbox 9 (accepts everything) out of the way
    rx_mb(c, 5, 0x11)
    wr(c, A + MIM, rd(c, A + MIM) | 1 << 5)
    h.send(CanFrame(0x11, b"\x04"))
    run(c, 300e3)
    c.arch.pie.ifr[9] = 0
    wr(c, A + RMP, 1 << 5)                         # cleared one flag, mailbox 2 still pending: new edge
    assert c.arch.pie.ifr[9] & (1 << 4)


def test_remote_frames_auto_answer_and_request():
    c = chip()
    h = host(c)
    tx_mb(c, 6, 0x66, b"\xAB\xCD")
    wr(c, mbox(6), (1 << 29) | 0x66 << 18)         # AAM (written while disabled)
    wr(c, A + ME, 0)
    wr(c, mbox(6), (1 << 29) | 0x66 << 18)
    wr(c, A + ME, 1 << 6)
    h.send(CanFrame(0x66, rtr=True, dlc=2))
    run(c, 600e3)
    assert [str(f) for _t, f in h.received] == ["066#ABCD"]
    assert rd(c, A + RFP) == 1 << 6 and rd(c, A + TA) == 1 << 6
    # remote request from a receive mailbox: TRS cleared without TA, the answer lands in the same mailbox
    rx_mb(c, 1, 0x77, ctrl=0x10 | 2)               # RTR, DLC 2
    wr(c, A + TRS, 1 << 1)
    run(c, 300e3)
    assert str(h.received[-1][1]) == "077#R2" and rd(c, A + TRS) == 0 and not rd(c, A + TA) & 2
    h.send(CanFrame(0x77, b"\x01\x02"))
    run(c, 300e3)
    assert rd(c, A + RMP) & 2 and rd(c, mbox(1, 2)) == 0x01020000


def test_self_test_mode_stores_own_frame():
    c = chip(gpio=False)                           # pins not needed in STM
    wr(c, A + MC, 0x2000 | (1 << 6))               # STM
    tx_mb(c, 0, 0x55, b"\x99")
    rx_mb(c, 4, 0x55)
    wr(c, A + TRS, 1)
    run(c, 300e3)
    assert rd(c, A + TA) == 1 and rd(c, A + RMP) == 1 << 4 and rd(c, mbox(4, 2)) >> 24 == 0x99


# ------------------------------------------------------------------------------------------------ errors
def test_no_ack_error_passive_and_epif():
    c = chip()
    wr(c, A + GIM, (1 << 9) | (1 << 8) | 1)        # EPIM, WLIM, I0EN
    tx_mb(c, 0, 0x1, b"\x01")
    wr(c, A + TRS, 1)
    run(c, 3e6)
    assert rd(c, A + TEC) == 128
    es = rd(c, A + ES)
    assert es & (1 << 17) and es & (1 << 16) and es & (1 << 19)       # EP, EW, ACKE
    assert rd(c, A + GIF0) & ((1 << 9) | (1 << 8))
    wr(c, A + ES, 1 << 19)
    assert not rd(c, A + ES) & (1 << 19)


def test_unmuxed_pins_bus_off_and_abo():
    c = chip(gpio=False)
    wr(c, A + MC, 0x2000 | (1 << 7))               # ABO
    tx_mb(c, 0, 0x1, b"\x01")
    wr(c, A + TRS, 1)
    run(c, 2e6)
    e = c.peripherals["ECANA"]
    assert e.errors.busoff and rd(c, A + ES) & (1 << 18) and rd(c, A + ES) & (1 << 23)   # BO, BE
    run(c, 128 * 11 * BIT + 1e5)
    assert not e.errors.busoff or e.errors.tec > 0  # auto bus-on happened (and errors start again)


def test_timeout_sets_tos_and_mtof():
    c = chip()
    tx_mb(c, 0, 0x1, b"\x01")
    tsc = rd(c, A + TSC)
    wr(c, A + 0xC0, tsc + 100)                     # MOTO0: 100 bit times from now
    wr(c, A + TOC, 1)
    wr(c, A + TRS, 1)                              # never acknowledged: times out
    run(c, 99 * BIT)
    assert rd(c, A + TOS) == 0
    run(c, 3 * BIT)
    assert rd(c, A + TOS) == 1 and rd(c, A + GIF0) & (1 << 17)
    wr(c, A + TOC, 0)
    assert rd(c, A + TOS) == 0


def test_ecanb_uses_its_own_pins_and_lines():
    c = chip(base=0x6200)
    h = host(c, "ECANB")
    wr(c, 0x6200 + MIM, 1)
    wr(c, 0x6200 + GIM, 1)
    tx_mb(c, 0, 0x2B, b"\x0B", base=0x6200)
    wr(c, 0x6200 + TRS, 1)
    run(c, 300e3)
    assert str(h.received[0][1]) == "02B#0B" and c.arch.pie.ifr[9] & (1 << 6)     # ECAN0INTB = 9.7
