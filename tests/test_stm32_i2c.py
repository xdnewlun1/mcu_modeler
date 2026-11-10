"""STM32F4 I2C model (chipem/periph/stm32/i2c.py): bus timing on the pins (SM, FM duty 0/1), START/STOP, NACK -> AF
at the 9th clock, master transmit/receive against the i2c_eeprom part (POS 2-byte, BTF stretching, 3-byte RM
procedure), slave mode against a second I2C module of the same chip, DMA transmit/receive with LAST.

The core runs uart_echo (it only touches PA9/PA10); the test drives the I2C registers from Python between short
board steps, like firmware polling the flags.  PCLK1 = 16 MHz (HSI reset clock): Tpclk1 = 62.5 ns."""
from __future__ import annotations

from pathlib import Path

import pytest

from chipem.session import Session

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "firmware" / "samples_stm32" / "build" / "uart_echo.elf"
pytestmark = pytest.mark.skipif(not FW.exists(), reason="firmware/samples_stm32 not built")

RCC, GPIOB = 0x40023800, 0x40020400
I2C1, I2C2 = 0x40005400, 0x40005800
DMA1 = 0x40026000
CR1, CR2, OAR1, OAR2, DR, SR1, SR2, CCR, TRISE = (4 * i for i in range(9))
PE, START, STOP, ACK, POS, SWRST = 1, 1 << 8, 1 << 9, 1 << 10, 1 << 11, 1 << 15
ITERREN, ITEVTEN, ITBUFEN, DMAEN, LAST = 1 << 8, 1 << 9, 1 << 10, 1 << 11, 1 << 12
SB, ADDR, BTF, STOPF, RXNE, TXE, AF = 1, 2, 4, 16, 64, 128, 1 << 10
MSL, BUSY, TRA = 1, 2, 4
T = 62.5                                  # ns per PCLK1 cycle


class Rig:
    def __init__(self, two: bool = False, eeprom: bool = True):
        self.ses = Session("stm32f405zg", FW)
        self.chip = self.ses.chip
        self.board = self.ses.ensure_board()
        b = self.chip.bus
        b.write(RCC + 0x30, 4, b.read(RCC + 0x30, 4) | 2)                 # GPIOB
        b.write(RCC + 0x40, 4, b.read(RCC + 0x40, 4) | (3 << 21))         # I2C1, I2C2
        pins = (6, 7, 10, 11) if two else (6, 7)
        for p in pins:
            b.write(GPIOB + 0x04, 4, b.read(GPIOB + 0x04, 4) | (1 << p))  # open-drain
            moder = b.read(GPIOB, 4) & ~(3 << 2 * p)
            b.write(GPIOB, 4, moder | (2 << 2 * p))
            afr = GPIOB + 0x20 + 4 * (p >> 3)
            b.write(afr, 4, (b.read(afr, 4) & ~(0xF << 4 * (p & 7))) | (4 << 4 * (p & 7)))
        scl, sda = ["A.PB6"], ["A.PB7"]
        if two:
            scl.append("A.PB10")
            sda.append("A.PB11")
        self.board.add_net("SCL", [tuple(x.split(".")) for x in scl], open_drain=True)
        self.board.add_net("SDA", [tuple(x.split(".")) for x in sda], open_drain=True)
        self.ee = self.board.add_part("EE", "i2c_eeprom", {"init": "ABCDEFGH"}, {"sda": "SDA", "scl": "SCL"}) \
            if eeprom else None
        self.edges: list = []
        self.chip.pins.subscribe(lambda e: self.edges.append((e.t_ns, e.pin, e.new))
                                 if e.field == "level" and e.pin in ("PB6", "PB7") else None)
        self.i2c = self.chip.peripherals["I2C1"]

    def w(self, off, v, base=I2C1):
        self.chip.bus.write(base + off, 4, v)

    def r(self, off, base=I2C1):
        return self.chip.bus.read(base + off, 4)

    def setup(self, ccr=80, base=I2C1, cr2=16):
        self.w(CR1, SWRST, base)
        self.w(CR1, 0, base)
        self.w(CR2, cr2, base)
        self.w(CCR, ccr, base)
        self.w(TRISE, 17, base)
        self.w(CR1, PE | ACK, base)

    def step(self, ns=1000):
        self.board.run_for(ns)

    def wait(self, bits, base=I2C1, reg=SR1, limit=3_000_000):
        t = 0
        while not self.r(reg, base) & bits:
            self.step()
            t += 1000
            assert t < limit, f"timeout waiting for SR1 & {bits:#x} (SR1={self.r(SR1, base):#x})"
        return self.r(reg, base)

    def start(self, addr_rw):
        self.w(CR1, self.r(CR1) | START)
        self.wait(SB)
        self.w(DR, addr_rw)
        s = self.wait(ADDR | AF)
        return bool(s & ADDR)

    def stop(self):
        self.w(CR1, self.r(CR1) | STOP)
        while self.r(SR2) & BUSY:
            self.step()

    def scl(self):
        return [(t, v) for t, p, v in self.edges if p == "PB6"]

    def sda(self):
        return [(t, v) for t, p, v in self.edges if p == "PB7"]


def _high_low(edges):
    """(high durations, low durations) between consecutive SCL edges of one transfer."""
    hi, lo = [], []
    for (t0, v0), (t1, _v1) in zip(edges, edges[1:]):
        (hi if v0 == "H" else lo).append(round(t1 - t0, 3))
    return hi, lo


@pytest.mark.parametrize("ccr,hi,lo", [(80, 80 * T, 80 * T),                       # SM 100 kHz
                                       ((1 << 15) | 4, 4 * T, 8 * T),              # FM duty 0 (Tlow = 2 Thigh)
                                       ((1 << 15) | (1 << 14) | 1, 9 * T, 16 * T)])  # FM duty 1 (16/9)
def test_scl_timing_and_nack_on_absent_address(ccr, hi, lo):
    rig = Rig(eeprom=False)
    rig.setup(ccr=ccr)
    af_at = []
    orig = rig.i2c._sr1

    def spy(set_bits=0, clear_bits=0):
        if set_bits & AF:
            af_at.append(rig.chip.now_ns())
        orig(set_bits, clear_bits)
    rig.i2c._sr1 = spy
    assert not rig.start(0x33 << 1)                                # nobody answers: AF, not ADDR
    assert rig.r(SR2) & (MSL | BUSY) == MSL | BUSY
    scl, sda = rig.scl(), rig.sda()
    t_start = sda[0][0]
    assert sda[0][1] == "L" and scl[0][1] == "L"
    assert scl[0][0] - t_start == pytest.approx(hi, abs=0.01)          # tHD;STA = Thigh
    his, los = _high_low(scl[:19])
    assert all(h == pytest.approx(hi, abs=0.01) for h in his), his
    assert los[0] >= lo                                                 # SB: SCL held low until DR is written
    assert all(x == pytest.approx(lo, abs=0.01) for x in los[1:]), los
    falls = [t for t, v in scl if v == "L"]
    assert af_at == [pytest.approx(falls[9], abs=0.01)]               # START fall + 9 clocks: the ACK clock's fall
    rig.stop()
    scl, sda = rig.scl(), rig.sda()
    assert sda[-1][1] == "H" and sda[-1][0] - scl[-1][0] == pytest.approx(hi, abs=0.01)   # tSU;STO
    assert rig.r(SR2) & (MSL | BUSY) == 0 and rig.r(CR1) & STOP == 0
    assert rig.i2c.baud() == pytest.approx(1e9 / (hi + lo))


def test_freq_mismatch_warns_once():
    rig = Rig(eeprom=False)
    rig.setup(cr2=42)
    rig.start(0x33 << 1)
    rig.stop()
    msgs = [e.fields.get("msg", "") for e in rig.chip.log if e.kind == "warn" and "FREQ" in e.fields.get("msg", "")]
    assert len(msgs) == 1 and "PCLK1 is 16 MHz" in msgs[0]


def _ee_write(rig, waddr, data):
    assert rig.start(0x50 << 1)
    rig.r(SR1)
    rig.r(SR2)                                                         # clear ADDR
    for b in [waddr, *data]:
        rig.wait(TXE)
        rig.w(DR, b)
    rig.wait(BTF)
    rig.stop()


def test_eeprom_write_ack_polling_and_reads():
    rig = Rig()
    rig.setup()
    _ee_write(rig, 0x00, b"xyz")
    assert rig.ee.busy
    assert not rig.start(0x50 << 1)                                    # write cycle: the EEPROM NACKs
    rig.w(SR1, ~AF & 0xFFFF)
    assert rig.r(SR1) & AF == 0
    rig.stop()
    rig.step(5_100_000)
    assert not rig.ee.busy and rig.ee.dump(0, 4) == b"xyzD"
    # 2-byte reception with POS (HAL): ACK=0, POS=1 before clearing ADDR; wait BTF; STOP; read DR twice
    _ee_write(rig, 0x01, b"")
    rig.w(CR1, rig.r(CR1) | ACK)
    assert rig.start((0x50 << 1) | 1)
    rig.w(CR1, (rig.r(CR1) & ~ACK) | POS)
    rig.r(SR1)
    assert rig.r(SR2) & (MSL | TRA) == MSL
    s = rig.wait(BTF)
    assert s & RXNE
    t_btf = rig.chip.now_ns()
    rig.step(30_000)                                                   # BTF: SCL held low by the master
    assert rig.chip.pins.get("PB6").level() == "L" and not [t for t, v in rig.scl() if t > t_btf]
    rig.w(CR1, rig.r(CR1) | STOP)
    assert bytes([rig.r(DR), rig.r(DR)]) == b"yz"
    rig.w(CR1, rig.r(CR1) & ~POS)
    while rig.r(SR2) & BUSY:
        rig.step()
    acks = [a for d, _b, a in list(rig.i2c.transfers)[-2:]]
    assert acks == [True, False]                                       # second byte NACKed (POS)
    # 4-byte reception, RM0090 N > 2 procedure
    _ee_write(rig, 0x00, b"")
    rig.w(CR1, rig.r(CR1) | ACK)
    assert rig.start((0x50 << 1) | 1)
    rig.r(SR1)
    rig.r(SR2)
    out = []
    rig.wait(RXNE)
    out.append(rig.r(DR))
    rig.wait(BTF)                                                      # bytes 2 (DR) and 3 (shift register)
    rig.w(CR1, rig.r(CR1) & ~ACK)
    out.append(rig.r(DR))
    rig.wait(BTF)
    rig.w(CR1, rig.r(CR1) | STOP)
    out += [rig.r(DR), rig.r(DR)]
    while rig.r(SR2) & BUSY:
        rig.step()
    assert bytes(out) == b"xyzD"
    assert [a for _d, _b, a in list(rig.i2c.transfers)[-4:]] == [True, True, True, False]


def test_slave_mode_answers_another_master_on_the_same_bus():
    rig = Rig(two=True, eeprom=False)
    rig.setup()                                                        # I2C1 = master
    rig.setup(base=I2C2)                                               # I2C2 = slave at 0x42
    rig.w(OAR1, (1 << 14) | (0x42 << 1), I2C2)
    regs = bytearray(range(0xA0, 0xB0))
    state = {"ptr": None, "rx": []}

    def service():
        s = rig.r(SR1, I2C2)
        if s & ADDR:
            s2 = rig.r(SR2, I2C2)
            state["tra"] = bool(s2 & TRA)
            state["first"] = not s2 & TRA
            s = rig.r(SR1, I2C2)
        if s & RXNE:
            b = rig.r(DR, I2C2)
            if state.get("first"):
                state["ptr"], state["first"] = b, False
            else:
                regs[state["ptr"]] = b
                state["rx"].append(b)
                state["ptr"] += 1
        if s & TXE and state.get("tra"):
            rig.w(DR, regs[state["ptr"]], I2C2)
            state["ptr"] += 1
        if s & STOPF:
            rig.w(CR1, rig.r(CR1, I2C2), I2C2)
            state["stopf"] = True
        if s & AF:
            rig.w(SR1, ~AF & 0xFFFF, I2C2)
            state["af"] = True

    def wait(bits):
        for _ in range(3000):
            service()
            if rig.r(SR1) & bits:
                return rig.r(SR1)
            rig.step()
        raise AssertionError("timeout")

    rig.w(CR1, rig.r(CR1) | START)
    wait(SB)
    rig.w(DR, 0x42 << 1)
    assert wait(ADDR | AF) & ADDR
    rig.r(SR1)
    rig.r(SR2)
    for b in (0x04, 0x5A, 0x5B):
        wait(TXE)
        rig.w(DR, b)
    wait(BTF)
    rig.w(CR1, rig.r(CR1) | STOP)
    for _ in range(200):
        service()
        rig.step()
    assert state["rx"] == [0x5A, 0x5B] and regs[4:6] == b"\x5a\x5b" and state.get("stopf")
    # read back 3 registers from 0x04: write pointer, repeated START, read
    rig.w(CR1, rig.r(CR1) | START | ACK)
    wait(SB)
    rig.w(DR, 0x42 << 1)
    wait(ADDR)
    rig.r(SR1)
    rig.r(SR2)
    wait(TXE)
    rig.w(DR, 0x04)
    wait(TXE)
    rig.w(CR1, rig.r(CR1) | START)
    wait(SB)
    rig.w(DR, (0x42 << 1) | 1)
    wait(ADDR)
    rig.r(SR1)
    rig.r(SR2)
    out = []
    wait(RXNE)
    out.append(rig.r(DR))
    wait(BTF)
    rig.w(CR1, rig.r(CR1) & ~ACK)
    out.append(rig.r(DR))
    wait(BTF)
    rig.w(CR1, rig.r(CR1) | STOP)
    out += [rig.r(DR), rig.r(DR)]
    for _ in range(200):
        service()
        rig.step()
    assert bytes(out[:3]) == b"\x5a\x5b\xa6" and state.get("af")   # the master NACKed the last byte: slave AF
    slave = rig.chip.peripherals["I2C2"]
    assert ("sw", 0x5A, True) in slave.transfers and rig.r(SR2, I2C2) & BUSY == 0


def test_interrupt_lines_follow_flags():
    rig = Rig()
    rig.setup()
    rig.w(CR2, 16 | ITEVTEN | ITERREN)
    arch = rig.chip.arch
    ev, er = rig.i2c._ev_irq, rig.i2c._er_irq
    assert (ev, er) == (31, 32)
    rig.w(CR1, rig.r(CR1) | START)
    rig.wait(SB)
    assert rig.i2c._lines == (True, False)
    rig.w(DR, 0x33 << 1)                                               # SB cleared
    assert rig.i2c._lines == (False, False)
    rig.wait(AF)
    assert rig.i2c._lines == (False, True)
    rig.w(SR1, ~AF & 0xFFFF)
    assert rig.i2c._lines == (False, False)
    rig.stop()
    assert arch is not None


def _dma_stream(rig, s, ch, direction, mem, n):
    b = rig.chip.bus
    base = DMA1 + 0x10 + 0x18 * s
    b.write(base + 0x4, 4, n)
    b.write(base + 0x8, 4, I2C1 + DR)
    b.write(base + 0xC, 4, mem)
    b.write(base, 4, (ch << 25) | (1 << 10) | (direction << 6) | 1)   # CHSEL, MINC, DIR, EN


def test_dma_transmit_and_receive_with_last():
    rig = Rig()
    rig.setup()
    b = rig.chip.bus
    b.write(RCC + 0x30, 4, b.read(RCC + 0x30, 4) | (1 << 21))        # DMA1
    mem = 0x20001000
    for i, c in enumerate(b"\x00dmaX"):
        b.write(mem + i, 1, c)
    rig.w(CR2, 16 | DMAEN)
    assert rig.start(0x50 << 1)
    _dma_stream(rig, 6, 1, 1, mem, 5)                                   # DMA1 stream 6 ch 1 = I2C1_TX, M2P
    rig.r(SR1)
    rig.r(SR2)
    rig.wait(BTF)
    rig.stop()
    rig.step(5_100_000)
    assert rig.ee.dump(0, 5) == b"dmaXE"
    # receive 4 bytes from 0 with DMA + LAST: the 4th byte is NACKed, then STOP
    _ee_write(rig, 0x00, b"")
    rig.w(CR2, 16 | DMAEN | LAST)
    rig.w(CR1, rig.r(CR1) | ACK)
    assert rig.start((0x50 << 1) | 1)
    _dma_stream(rig, 0, 1, 0, mem + 0x40, 4)                            # DMA1 stream 0 ch 1 = I2C1_RX, P2M
    rig.r(SR1)
    rig.r(SR2)
    for _ in range(400):
        if b.read(DMA1 + 0x10, 4) & 1 == 0:
            break
        rig.step()
    rig.stop()
    assert bytes(b.read(mem + 0x40 + i, 1) for i in range(4)) == b"dmaX"
    assert [a for _d, _b, a in list(rig.i2c.transfers)[-4:]] == [True, True, True, False]


def test_nostretch_slave_transmitter_underrun_resends_dr():
    """RM0090 27.3.4: a slave transmitter with NOSTRETCH whose DR was not refilled sends the DR byte again (OVR)."""
    rig = Rig(two=True, eeprom=False)
    rig.setup()                                                        # I2C1 = master
    rig.setup(base=I2C2)
    NOSTRETCH, OVR = 1 << 7, 1 << 11
    rig.w(OAR1, (1 << 14) | (0x42 << 1), I2C2)
    rig.w(CR1, PE | ACK | NOSTRETCH, I2C2)
    rig.w(DR, 0x77, I2C2)                                              # DR holds 0x77, never refilled
    rig.w(CR1, rig.r(CR1) | START | ACK)
    rig.wait(SB)
    rig.w(DR, (0x42 << 1) | 1)
    rig.wait(ADDR)
    rig.r(SR1)
    rig.r(SR2)
    out = [None]
    rig.wait(RXNE)
    rig.wait(BTF)
    rig.w(CR1, rig.r(CR1) & ~ACK)
    out[0] = rig.r(DR)
    rig.wait(BTF)
    rig.w(CR1, rig.r(CR1) | STOP)
    out += [rig.r(DR), rig.r(DR)]
    for _ in range(200):
        rig.step()
    assert out == [0x77, 0x77, 0x77] and rig.r(SR1, I2C2) & OVR
