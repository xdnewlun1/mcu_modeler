"""TI C2000 I2C-A (ti_c2000.i2c, SPRUG03) on the TMS320F28232 description and the fake C28x core: bit timing from
IPSC/ICCL/ICCH (d = 7/6/5), START/STOP/NACK on GPIO32/33, EEPROM transfers through the i2c_eeprom part, I2CISRC
priority, FIFOs + I2CINT2A, digital loopback, repeat mode, slave mode against a second F28232, arbitration lost."""
from pathlib import Path

import pytest

from chipem.board import Board
from chipem.chipdesc import load_chip

from c28x_fake import make_fake_chip

CHIP = Path(__file__).resolve().parent.parent / "chips" / "tms320f28232.yaml"
EALLOW = 1 << 6
B = 0x7900
OAR, IER, STR, CLKL, CLKH, CNT, DRR, SAR, DXR, MDR, ISRC, EMDR, PSC, FFTX, FFRX = (
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 0xA, 0xB, 0xC, 0x20, 0x21)
# I2CMDR
IRS, DLB, RM, TRX, MST, STP, STT, NACKMOD = 1 << 5, 1 << 6, 1 << 7, 1 << 9, 1 << 10, 1 << 11, 1 << 13, 1 << 15
# I2CSTR
AL, NACK, ARDY, RRDY, XRDY, SCD, AAS, XSMT, BB, NACKSNT, SDIR = (1, 2, 4, 8, 16, 32, 1 << 9, 1 << 10, 1 << 12,
                                                                 1 << 13, 1 << 14)
PIEIER8 = 0x0CF0
_DESC = None


def _chip():
    global _DESC
    _DESC = _DESC or load_chip(CHIP)
    c = make_fake_chip(_DESC)
    c.reset()
    c.core.regs["st1"] |= EALLOW
    c.bus.write(0x7029, 1, 0x0068)              # watchdog off
    c.bus.write(0x6F80 + 0x16, 2, 0x5)          # GPBMUX1: GPIO32 = SDAA, GPIO33 = SCLA (pull-ups on at reset)
    return c


class Rig:
    def __init__(self, n=1, ee=None):
        self.chips = [_chip() for _ in range(n)]
        self.board = Board({"AB"[i]: c for i, c in enumerate(self.chips)}, quantum_ns=1000)
        if n > 1:
            self.board.add_net("SDA", [("A", "GPIO32"), ("B", "GPIO32")], pull="up")
            self.board.add_net("SCL", [("A", "GPIO33"), ("B", "GPIO33")], pull="up")
        self.ee = None
        if ee is not None and n == 1:
            self.ee = self.board.add_part("EE", "i2c_eeprom", ee, {"sda": "A.GPIO32", "scl": "A.GPIO33"})
        self.c = self.chips[0]

    def w(self, o, v, c=None):
        (c or self.c).bus.write(B + o, 1, v)

    def r(self, o, c=None):
        return (c or self.c).bus.read(B + o, 1)

    def until(self, f, n=20000, step=1000):
        for _ in range(n):
            if f():
                return
            self.board.run_for(step)
        raise AssertionError(f"timeout: STR={self.r(STR):04x}")

    def setup(self, c=None, psc=0, l=5, h=5):
        self.w(PSC, psc, c)
        self.w(CLKL, l, c)
        self.w(CLKH, h, c)
        self.w(MDR, IRS, c)

    def blk(self, c=None):
        return (c or self.c).peripherals["I2CA"]


def _edges(chip, pin):
    out = []
    chip.pins.subscribe(lambda e: out.append((e.t_ns, e.new)) if e.field == "level" and e.pin == pin else None)
    return out


def test_scl_timing_start_stop_and_nack_on_absent_address():
    rig = Rig()
    c = rig.c
    assert c.clock.hz == 10e6 and c.pins.get("GPIO32").owner == "SDAA" and c.pins.get("GPIO32").level() == "H"
    rig.setup(psc=1, l=10, h=3)                  # Tmod = 200 ns, d = 6: low 16 x 200, high 9 x 200
    blk = rig.blk()
    t = blk.timing()
    assert (t.low_ns, t.high_ns) == (3200.0, 1800.0) and blk.module_hz() == 5e6
    assert any("outside 7-12 MHz" in str(e.get("msg")) for e in c.log if e.kind == "warn")
    sda, scl = _edges(c, "GPIO32"), _edges(c, "GPIO33")
    rig.w(IER, 0x02)                             # NACK interrupt
    c.bus.write(PIEIER8, 1, 0x3)
    rig.w(SAR, 0x33)
    rig.w(CNT, 1)
    rig.w(MDR, IRS | MST | TRX | STT | STP)
    rig.until(lambda: not rig.r(MDR) & MST)
    t0 = sda[0][0]
    assert sda[0][1] == "L" and scl[0] == (t0 + 1800, "L")          # START: SCL falls tHD;STA = high time later
    rises = [tt for tt, v in scl if v == "H"]
    falls = [tt for tt, v in scl if v == "L"]
    assert [round(x - t0) for x in rises[:2]] == [1800 + 3200, 1800 + 3200 + 1800 + 3200]
    assert round(falls[1] - rises[0]) == 1800
    assert len(rises) == 10                       # 8 address bits + ACK slot + STOP clock
    assert sda[-1][1] == "H" and round(sda[-1][0] - rises[-1]) == 1800             # STOP after tSU;STO
    st = rig.r(STR)
    assert st & NACK and st & SCD and not st & BB
    assert c.arch.pie.ifr[8] & 1                   # I2CINT1A latched
    assert rig.r(ISRC) == 2 and not rig.r(STR) & NACK          # code 2, read clears NACK
    assert rig.r(ISRC) == 0
    assert blk.transfers == [("a", -1, False)]


def test_eeprom_page_write_ack_polling_and_random_read():
    rig = Rig(ee={"init": "Hi"})
    rig.setup()
    blk = rig.blk()
    assert round(blk.timing().period_ns) == 2400 and blk.baud() == pytest.approx(416666.67, rel=1e-6)
    rig.w(SAR, 0x50)
    rig.w(CNT, 4)
    rig.w(MDR, IRS | MST | TRX | STT | STP)
    for ch in b"\x00OK!":
        rig.until(lambda: rig.r(STR) & XRDY)
        rig.w(DXR, ch)
    rig.until(lambda: rig.r(STR) & SCD)
    assert rig.ee.busy and rig.ee.dump(0, 3) == b"Hi\xff"
    rig.w(STR, SCD)
    # ack polling: the EEPROM NACKs its address during tWR
    rig.w(CNT, 1)
    rig.w(MDR, IRS | MST | TRX | STT | STP)
    rig.until(lambda: rig.r(STR) & SCD)
    assert rig.r(STR) & NACK
    rig.board.run_for(5.1e6)
    assert not rig.ee.busy and rig.ee.dump(0, 4) == b"OK!\xff"
    rig.w(STR, NACK | SCD)
    # random read: dummy write of the word address (no STP: ARDY), repeated START, 3 bytes, STOP
    rig.w(CNT, 1)
    rig.w(MDR, IRS | MST | TRX | STT)
    rig.until(lambda: rig.r(STR) & XRDY)
    rig.w(DXR, 0)
    rig.until(lambda: rig.r(STR) & ARDY)
    rig.w(CNT, 3)
    rig.w(MDR, IRS | MST | STT | STP)
    out = []
    for _ in range(3):
        rig.until(lambda: rig.r(STR) & RRDY)
        out.append(rig.r(DRR))
    rig.until(lambda: rig.r(STR) & SCD)
    assert bytes(out) == b"OK!" and rig.r(STR) & NACKSNT
    assert [x for x in blk.transfers if x[0] == "r"] == [("r", 79, True), ("r", 75, True), ("r", 33, False)]


def test_isrc_priority_and_clear_on_read():
    rig = Rig()
    rig.setup()
    blk = rig.blk()
    blk.set("I2CSTR", blk.get("I2CSTR") | ARDY | SCD | NACK | AL)
    rig.w(IER, 0x7F)
    assert rig.r(ISRC) == 1 and rig.r(ISRC) == 2       # AL then NACK, both cleared by the read
    assert rig.r(ISRC) == 3 and rig.r(ISRC) == 3       # ARDY is not cleared by the read
    rig.w(STR, ARDY)
    assert rig.r(ISRC) == 5                            # XRDY (reset value 1)
    rig.w(IER, 0x20)
    assert rig.r(ISRC) == 6 and rig.r(ISRC) == 0       # SCD cleared by the read
    rig.w(STR, BB)                                     # w1c bits do not touch read-only XSMT/XRDY
    assert rig.r(STR) & (XSMT | XRDY) == XSMT | XRDY


def test_fifo_mode_levels_and_i2cint2a():
    rig = Rig(ee={})
    c = rig.c
    rig.setup()
    c.bus.write(PIEIER8, 1, 0x3)
    rig.w(FFTX, (1 << 14) | (1 << 13) | (1 << 5) | 0)      # I2CFFEN, TXFFRST, TXFFIENA, TXFFIL = 0
    assert rig.r(FFTX) & 0x80 and c.arch.pie.ifr[8] & 2  # empty FIFO <= 0: TXFFINT at once
    rig.w(FFTX, rig.r(FFTX) | 0x40)                      # TXFFINTCLR: re-set while the condition holds
    for ch in b"\x10ABC":
        rig.w(DXR, ch)
    assert (rig.r(FFTX) >> 8) & 0x1F == 4
    rig.w(FFTX, rig.r(FFTX) | 0x40)
    assert not rig.r(FFTX) & 0x80
    rig.w(SAR, 0x50)
    rig.w(CNT, 4)
    rig.w(MDR, IRS | MST | TRX | STT | STP)
    rig.until(lambda: rig.r(STR) & SCD)
    assert (rig.r(FFTX) >> 8) & 0x1F == 0 and rig.r(FFTX) & 0x80
    rig.board.run_for(5.1e6)
    assert rig.ee.dump(0x10, 3) == b"ABC"
    rig.w(STR, SCD)
    rig.w(FFRX, (1 << 13) | 3)                            # RXFFRST, RXFFIL = 3
    rig.w(CNT, 1)
    rig.w(MDR, IRS | MST | TRX | STT)
    rig.w(DXR, 0x10)
    rig.until(lambda: rig.r(STR) & ARDY)
    rig.w(CNT, 3)
    rig.w(MDR, IRS | MST | STT | STP)
    rig.until(lambda: rig.r(STR) & SCD)
    ffrx = rig.r(FFRX)
    assert (ffrx >> 8) & 0x1F == 3 and ffrx & 0x80
    assert bytes(rig.r(DRR) for _ in range(3)) == b"ABC"
    assert (rig.r(FFRX) >> 8) & 0x1F == 0


def test_digital_loopback_no_pin_activity():
    rig = Rig()
    c = rig.c
    rig.setup()
    sda, scl = _edges(c, "GPIO32"), _edges(c, "GPIO33")
    rig.w(OAR, 0x21)
    rig.w(CNT, 2)
    rig.w(DXR, 0x5A)
    rig.w(MDR, IRS | MST | TRX | DLB | STT | STP)
    rig.until(lambda: rig.r(STR) & RRDY)
    t = c.now_ns()
    assert rig.r(DRR) == 0x5A
    rig.w(DXR, 0xA5)
    rig.until(lambda: rig.r(STR) & SCD)
    assert rig.r(DRR) == 0xA5 and not rig.r(MDR) & MST
    assert t >= 9 * 2400 and not sda and not scl


def test_repeat_mode_ardy_per_byte_and_stp():
    rig = Rig(ee={})
    rig.setup()
    rig.w(SAR, 0x50)
    rig.w(DXR, 0x20)
    rig.w(MDR, IRS | MST | TRX | RM | STT)
    for ch in b"xy":
        rig.until(lambda: rig.r(STR) & ARDY)
        rig.w(STR, ARDY)
        rig.w(DXR, ch)
    rig.until(lambda: rig.r(STR) & ARDY)
    assert rig.r(MDR) & MST and rig.r(STR) & BB
    rig.w(MDR, IRS | MST | TRX | RM | STP)
    rig.until(lambda: rig.r(STR) & SCD)
    rig.board.run_for(5.1e6)
    assert rig.ee.dump(0x20, 2) == b"xy" and not rig.r(MDR) & (MST | STP)


def test_slave_mode_against_second_f28232_and_arbitration_lost():
    rig = Rig(n=2)
    a, b = rig.chips
    rig.setup(a)
    rig.setup(b)
    rig.w(OAR, 0x29, b)
    blk_b = rig.blk(b)
    # A writes two bytes to B
    rig.w(SAR, 0x29, a)
    rig.w(CNT, 2, a)
    rig.w(DXR, 0x11, a)
    rig.w(MDR, IRS | MST | TRX | STT | STP, a)
    rig.until(lambda: rig.r(STR, b) & AAS)
    assert not rig.r(STR, b) & SDIR
    rig.until(lambda: rig.r(STR, a) & XRDY)
    rig.w(DXR, 0x22, a)
    rig.until(lambda: rig.r(STR, b) & RRDY)
    assert rig.r(DRR, b) == 0x11
    rig.until(lambda: rig.r(STR, b) & SCD)
    assert rig.r(DRR, b) == 0x22 and not rig.r(STR, a) & NACK
    # A reads one byte from B: B stretches SCL (XSMT = 0) until its I2CDXR is written
    for c in (a, b):
        rig.w(STR, SCD, c)
    rig.w(CNT, 1, a)
    rig.w(MDR, IRS | MST | STT | STP, a)
    rig.until(lambda: rig.r(STR, b) & SDIR and not rig.r(STR, b) & XSMT)
    rig.board.run_for(20_000)
    assert a.pins.get("GPIO33").level() == "L"                      # stretched
    rig.w(DXR, 0x77, b)
    rig.until(lambda: rig.r(STR, a) & RRDY)
    assert rig.r(DRR, a) == 0x77
    rig.until(lambda: rig.r(STR, a) & SCD)
    assert ("sr", 0x77, True) in blk_b.transfers
    # B starts a transfer; A tries STT while the bus is busy: AL, A becomes a slave
    rig.w(SAR, 0x50, b)
    rig.w(CNT, 1, b)
    rig.w(MDR, IRS | MST | TRX | STT | STP, b)
    rig.until(lambda: rig.r(STR, a) & BB)
    rig.w(MDR, IRS | MST | TRX | STT, a)
    assert rig.r(STR, a) & AL and not rig.r(MDR, a) & MST


def test_irs_reset_keeps_bb_and_releases():
    rig = Rig()
    rig.setup()
    rig.w(SAR, 0x33)
    rig.w(CNT, 1)
    rig.w(MDR, IRS | MST | TRX | STT)
    rig.until(lambda: rig.r(STR) & BB)
    rig.w(MDR, 0)                                  # IRS = 0 mid-transfer
    st = rig.r(STR)
    assert st & BB and st & (XSMT | XRDY) == XSMT | XRDY and not st & (NACK | ARDY)
    assert rig.c.pins.get("GPIO32").level() == "H" and rig.c.pins.get("GPIO33").level() == "H"
    rig.w(MDR, STT | MST)                          # STT not writable while IRS = 0
    assert not rig.r(MDR) & STT
