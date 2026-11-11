"""STM32F446 FMPI2C1 (chipem/periph/stm32/fmpi2c.py, RM0390 Rev 9 ch. 23 "Fast-mode Plus Inter-integrated circuit
interface"): TIMINGR -> SCL timing on the pins, controller transmit/receive against the i2c_eeprom part (NBYTES,
AUTOEND, software end + TC, RELOAD + TCR), NACKF + automatic STOP on a missing device, target mode addressed by the
chip's own I2C1 as controller, the TXE flush, PE reset, reserved bits, ICR clears, IRQ lines and the DMA requests
(DMA1 stream 5/2 channel 2).

The core spins; the tests drive the registers from Python between short board steps, like firmware polling ISR.
I2CCLK = PCLK1 = 16 MHz (HSI reset clock, DCKCFGR2.FMPI2C1SEL = 00): tI2CCLK = 62.5 ns."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from chipem.board import Board
from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.firmware import FirmwareImage

from engine_util import asm

ROOT = Path(__file__).resolve().parent.parent
FLASH = 0x08000000
RCC, GPIOB, GPIOC = 0x40023800, 0x40020400, 0x40020800
F = 0x40006000
I2C1 = 0x40005400
DMA1 = 0x40026000
CR1, CR2, OAR1, OAR2, TIMINGR, TIMEOUTR, ISR, ICR, PECR, RXDR, TXDR = range(0, 0x2C, 4)
PE, TXIE, RXIE, ADDRIE, NACKIE, STOPIE, TCIE, ERRIE = (1 << i for i in range(8))
TXDMAEN, RXDMAEN, SBC, NOSTRETCH, GCEN = 1 << 14, 1 << 15, 1 << 16, 1 << 17, 1 << 19
RD_WRN, START, STOP, NACK, RELOAD, AUTOEND = 1 << 10, 1 << 13, 1 << 14, 1 << 15, 1 << 24, 1 << 25
TXE, TXIS, RXNE, ADDR, NACKF, STOPF, TC, TCR = (1 << i for i in range(8))
ARLO, OVR, BUSY, DIR = 1 << 9, 1 << 10, 1 << 15, 1 << 16
TK = 62.5
# 100 kHz-ish standard mode at 16 MHz (RM0390 Table 136 style): PRESC 3, SCLL 0x13, SCLH 0x0F, SDADEL 2, SCLDEL 4
TIM_SM = (3 << 28) | (4 << 20) | (2 << 16) | (0x0F << 8) | 0x13


@pytest.fixture(scope="module")
def desc():
    return load_chip(ROOT / "chips" / "stm32f446re.yaml")


class Rig:
    def __init__(self, desc, eeprom=True, i2c1=False):
        code = asm("spin: b spin", FLASH + 0x200)
        img = bytearray(0x200 + len(code))
        img[0:8] = struct.pack("<II", 0x20020000, (FLASH + 0x200) | 1)
        img[0x200:] = code
        c = self.chip = Chip(desc)
        c.load_firmware(FirmwareImage([(FLASH, bytes(img))]))
        c.reset()
        self.board = Board({"A": c}, name="t")
        w, r = c.bus.write, c.bus.read
        w(RCC + 0x30, 4, r(RCC + 0x30, 4) | 0x7 | (1 << 21))        # GPIOA-C, DMA1
        w(RCC + 0x40, 4, r(RCC + 0x40, 4) | (1 << 24) | (1 << 21))  # FMPI2C1EN, I2C1EN
        pins = [(GPIOC, 6, 4), (GPIOC, 7, 4)] + ([(GPIOB, 8, 4), (GPIOB, 9, 4)] if i2c1 else [])
        for base, p, af in pins:
            w(base + 0x04, 4, r(base + 0x04, 4) | (1 << p))          # open drain
            w(base, 4, (r(base, 4) & ~(3 << 2 * p)) | (2 << 2 * p))
            afr = base + 0x20 + 4 * (p >> 3)
            w(afr, 4, (r(afr, 4) & ~(0xF << 4 * (p & 7))) | (af << 4 * (p & 7)))
        scl, sda = [("A", "PC6")], [("A", "PC7")]
        if i2c1:
            scl.append(("A", "PB8"))
            sda.append(("A", "PB9"))
        self.board.add_net("SCL", scl, open_drain=True)
        self.board.add_net("SDA", sda, open_drain=True)
        self.ee = self.board.add_part("EE", "i2c_eeprom", {"init": "ABCDEFGH", "twr": "200us"},
                                      {"sda": "SDA", "scl": "SCL"}) if eeprom else None
        self.edges: list = []
        c.pins.subscribe(lambda e: self.edges.append((e.t_ns, e.pin, e.new))
                         if e.field == "level" and e.pin in ("PC6", "PC7") else None)
        self.f = c.peripherals["FMPI2C1"]

    def w(self, off, v, base=F):
        self.chip.bus.write(base + off, 4, v)

    def r(self, off, base=F):
        return self.chip.bus.read(base + off, 4)

    def setup(self, timing=TIM_SM, cr1=0):
        self.w(CR1, 0)
        self.w(TIMINGR, timing)
        self.w(CR1, PE | cr1)

    def step(self, ns=1000):
        self.board.run_for(ns)

    def wait(self, bits, reg=ISR, base=F, limit=5_000_000):
        t = 0
        while not self.r(reg, base) & bits:
            self.step()
            t += 1000
            assert t < limit, f"timeout waiting for {bits:#x} (ISR={self.r(ISR):#x})"
        return self.r(reg, base)

    def idle(self, limit=5_000_000):
        t = 0
        while self.r(ISR) & BUSY:
            self.step()
            t += 1000
            assert t < limit

    def scl(self):
        return [(t, v) for t, p, v in self.edges if p == "PC6"]


def test_reset_values_reserved_bits_and_icr(desc):
    """RM0390 23.9.12 Table 143: ISR resets to 0x00000001 (TXE); reserved bits read 0; ICR is write-only (reads 0)."""
    rig = Rig(desc, eeprom=False)
    assert rig.r(ISR) == TXE
    for off in (CR1, CR2, OAR1, OAR2, TIMINGR, TIMEOUTR, ICR, PECR, RXDR, TXDR):
        assert rig.r(off) == 0
    rig.w(CR1, 0xFFFFFFFF & ~PE)
    assert rig.r(CR1) == 0x00FBDFFE                            # bits 31:24, 18, 13 reserved (23.9.1)
    rig.w(CR1, 0)
    rig.w(CR2, 0x07FF0FFF)                                     # rs bits START/STOP/NACK need PE: cleared
    assert rig.r(CR2) == 0x03FF0FFF
    rig.w(OAR1, 0xFFFFFFFF)
    assert rig.r(OAR1) == 0x87FF
    rig.w(OAR2, 0xFFFFFFFF)
    assert rig.r(OAR2) == 0x87FE
    rig.w(TIMINGR, 0xFFFFFFFF)
    assert rig.r(TIMINGR) == 0xF0FFFFFF
    rig.w(TXDR, 0x1234)
    assert rig.r(TXDR) == 0x34
    assert rig.r(0x2C) == 0 and rig.r(0x3FC) == 0              # beyond the register map: reserved
    rig.w(ICR, 0xFFFFFFFF)
    assert rig.r(ICR) == 0


def test_oar_fields_locked_while_enabled(desc):
    """RM0390 23.9.3/23.9.4: OA1/OA1MODE and OA2/OA2MSK can be written only while OAxEN = 0."""
    rig = Rig(desc, eeprom=False)
    rig.w(OAR1, (1 << 15) | (0x42 << 1))
    rig.w(OAR1, (1 << 15) | (0x11 << 1))
    assert rig.r(OAR1) == (1 << 15) | (0x42 << 1)
    rig.w(OAR1, 0)
    rig.w(OAR1, 0x11 << 1)
    assert rig.r(OAR1) == 0x11 << 1


def test_timingr_scl_period_and_nack_on_missing_device(desc):
    """RM0390 23.4.9 / 23.9.5: tSCLL = (SCLL+1) tPRESC, tSCLH = (SCLH+1) tPRESC, plus tSYNC1/2 (model: 2 tI2CCLK each);
    the missing device NACKs its address: NACKF, START cleared and an automatic STOP (STOPF)."""
    rig = Rig(desc, eeprom=False)
    rig.setup()
    rig.w(CR2, (0x50 << 1) | (1 << 16) | AUTOEND | START)
    s = rig.wait(STOPF)
    assert s & NACKF and not rig.r(CR2) & START
    edges = rig.scl()
    tp = 4 * TK
    hi = [t1 - t0 for (t0, v0), (t1, _) in zip(edges, edges[1:]) if v0 == "H"]
    lo = [t1 - t0 for (t0, v0), (t1, _) in zip(edges[1:], edges[2:]) if v0 == "L"]
    assert abs(hi[1] - (0x10 * tp + 2 * TK)) < 1
    assert abs(lo[0] - (0x14 * tp + 2 * TK)) < 1
    assert not rig.r(ISR) & BUSY
    rig.w(ICR, NACKF | STOPF)
    assert rig.r(ISR) & (NACKF | STOPF) == 0


def test_fast_mode_plus_timing(desc):
    """Fm+ (RM0390 23.4.10 table style, I2CCLK 16 MHz): PRESC 0, SCLL 4, SCLH 2 -> tLOW 5+2, tHIGH 3+2 tI2CCLK."""
    rig = Rig(desc, eeprom=False)
    rig.setup(timing=(0 << 28) | (1 << 20) | (0 << 16) | (2 << 8) | 4)
    assert abs(rig.f.timing().period_ns - 12 * TK) < 1e-6
    assert rig.f.baud() > 1e6 * 0.99


def test_write_then_read_eeprom_autoend_and_software_end(desc):
    """RM0390 23.4.9 controller transmitter (TXIS per byte, AUTOEND STOP) then the random read: dummy write with
    AUTOEND = 0 -> TC, START with RD_WRN = 1 -> repeated START, RXNE per byte, last byte NACKed, AUTOEND STOP."""
    rig = Rig(desc)
    rig.setup()
    data = b"\x10xyz"
    rig.w(CR2, (0x50 << 1) | (len(data) << 16) | AUTOEND | START)
    for b in data:
        rig.wait(TXIS)
        rig.w(TXDR, b)
    rig.wait(STOPF)
    assert not rig.r(ISR) & NACKF
    rig.w(ICR, STOPF)
    rig.step(300_000)                                          # write cycle (twr 200 us)
    assert rig.ee.dump(0x10, 3) == b"xyz"
    rig.w(CR2, (0x50 << 1) | (1 << 16) | START)                # dummy write, software end
    rig.wait(TXIS)
    rig.w(TXDR, 0x10)
    rig.wait(TC)
    assert rig.r(ISR) & BUSY                                   # SCL held low, bus still owned
    rig.w(CR2, (0x50 << 1) | RD_WRN | (3 << 16) | AUTOEND | START)
    assert not rig.r(ISR) & TC                                 # START clears TC (23.9.7)
    got = bytes(rig.wait(RXNE) and rig.r(RXDR) for _ in range(3))
    rig.wait(STOPF)
    assert got == b"xyz"
    assert [a for k, _b, a in rig.f.transfers if k == "r"] == [True, True, False]   # last byte NACKed


def test_reload_tcr(desc):
    """RM0390 23.4.7 / 23.9.7: RELOAD = 1 -> TCR after NBYTES bytes, SCL stretched; writing NBYTES != 0 clears TCR."""
    rig = Rig(desc)
    rig.setup()
    rig.w(CR2, (0x50 << 1) | (1 << 16) | AUTOEND | RELOAD | START)
    rig.wait(TXIS)
    rig.w(TXDR, 0x00)
    rig.wait(TCR)
    rig.step(50_000)
    assert rig.r(ISR) & TCR and rig.r(ISR) & BUSY
    rig.w(CR2, (rig.r(CR2) & ~(0xFF << 16 | RELOAD)) | (2 << 16))
    assert not rig.r(ISR) & TCR
    for b in b"QR":
        rig.wait(TXIS)
        rig.w(TXDR, b)
    rig.wait(STOPF)
    rig.step(300_000)
    assert rig.ee.dump(0, 2) == b"QR"


def test_txe_flush_and_pe_reset(desc):
    """RM0390 23.9.7: TXE written 1 flushes TXDR; 23.4.6: PE = 0 clears the flags and the CR2 rs bits, TXE = 1."""
    rig = Rig(desc, eeprom=False)
    rig.setup()
    rig.w(TXDR, 0x55)
    assert not rig.r(ISR) & TXE
    rig.w(TXDR, 0x66)                                         # TXE = 0: write ignored
    assert rig.r(TXDR) == 0x55
    rig.w(ISR, TXE)
    assert rig.r(ISR) & TXE
    rig.w(ISR, TXIS)                                           # TXIS writable only with NOSTRETCH = 1
    assert not rig.r(ISR) & TXIS
    rig.w(CR2, (0x50 << 1) | (1 << 16) | START)
    rig.wait(NACKF)
    rig.w(CR1, 0)
    assert rig.r(ISR) == TXE and not rig.r(CR2) & (START | STOP)


def test_txis_count_equals_nbytes_and_txdr_preload(desc):
    """RM0390 23.4.9: the number of TXIS events equals NBYTES; 23.4.7: a byte written to TXDR before START is copied
    to the shift register after the address (TXE = 0 -> 1 without a TXIS for it)."""
    rig = Rig(desc)
    rig.setup()
    rig.w(TXDR, 0x30)                                          # word address, preloaded
    rig.w(CR2, (0x50 << 1) | (3 << 16) | AUTOEND | START)
    n = 0
    while not rig.r(ISR) & STOPF:
        if rig.r(ISR) & TXIS:
            n += 1
            rig.w(TXDR, 0x60 + n)
        rig.step()
    assert n == 2
    rig.step(300_000)
    assert rig.ee.dump(0x30, 2) == b"ab"


def test_cr1_filter_and_nostretch_locked_while_pe(desc):
    """RM0390 23.9.1: NOSTRETCH, ANFOFF and DNF can only be programmed while PE = 0 (one `warn`)."""
    rig = Rig(desc, eeprom=False)
    warns = []
    orig = rig.f.emit
    rig.f.emit = lambda kind, **kw: (warns.append(kw.get("msg", "")) if kind == "warn" else None, orig(kind, **kw))
    rig.setup()
    rig.w(CR1, PE | NOSTRETCH | (1 << 12) | (5 << 8))
    assert rig.r(CR1) == PE
    assert any("PE = 0" in m for m in warns)
    rig.w(CR1, 0)
    rig.w(CR1, NOSTRETCH | (5 << 8))
    assert rig.r(CR1) == NOSTRETCH | (5 << 8)


def test_irq_lines(desc):
    """RM0390 23.6 Table 142: NACKF / STOPF on FMPI2C1_EV (IRQ 95) with NACKIE / STOPIE."""
    rig = Rig(desc, eeprom=False)
    pend = []
    orig = rig.f.irq
    rig.f.irq = lambda n, p=True: (pend.append((n, p)), orig(n, p))
    rig.setup(cr1=NACKIE | STOPIE)
    rig.w(CR2, (0x50 << 1) | (1 << 16) | AUTOEND | START)
    rig.wait(STOPF)
    assert (95, True) in pend
    rig.w(ICR, NACKF | STOPF)
    assert pend[-1] == (95, False)


def test_target_receive_and_transmit_addressed_by_i2c1(desc):
    """RM0390 23.4.8 target mode: I2C1 (controller, F4 I2C v1) addresses OA1 = 0x3A: ADDR + DIR + ADDCODE, SCL
    stretched until ADDRCF, RXNE; then a read: TXIS asks for data, the controller's NACK sets NACKF, STOPF."""
    rig = Rig(desc, eeprom=False, i2c1=True)
    rig.setup()
    rig.w(OAR1, (1 << 15) | (0x3A << 1))
    rig.w(0x00, 1 << 15, I2C1)                                 # I2C1 SWRST
    rig.w(0x00, 0, I2C1)
    rig.w(0x04, 16, I2C1)
    rig.w(0x1C, 80, I2C1)
    rig.w(0x20, 17, I2C1)
    rig.w(0x00, 1 | (1 << 10), I2C1)                           # PE, ACK
    rig.w(0x00, rig.r(0x00, I2C1) | (1 << 8), I2C1)            # START
    rig.wait(1, reg=0x14, base=I2C1)
    rig.w(0x10, 0x3A << 1, I2C1)
    s = rig.wait(ADDR)
    assert not s & DIR and (s >> 17) & 0x7F == 0x3A
    rig.wait(2, reg=0x14, base=I2C1)
    rig.r(0x14, I2C1)
    rig.r(0x18, I2C1)
    rig.w(0x10, 0xA5, I2C1)
    rig.step(100_000)
    assert rig.scl()[-1][1] == "L" and not rig.r(ISR) & RXNE   # SCL stretched by the target until ADDRCF
    rig.w(ICR, ADDR)
    rig.wait(RXNE)
    assert rig.r(RXDR) == 0xA5
    rig.wait(4, reg=0x14, base=I2C1)                          # BTF
    rig.w(0x00, rig.r(0x00, I2C1) | (1 << 9), I2C1)            # STOP
    rig.wait(STOPF)
    rig.w(ICR, STOPF)
    # read: 1 byte (I2C1 v1: ACK = 0 before clearing ADDR, STOP after)
    rig.w(ISR, TXE)
    rig.w(0x00, rig.r(0x00, I2C1) | (1 << 8), I2C1)
    rig.wait(1, reg=0x14, base=I2C1)
    rig.w(0x10, (0x3A << 1) | 1, I2C1)
    s = rig.wait(ADDR)
    assert s & DIR
    rig.w(ICR, ADDR)
    rig.wait(TXIS)
    rig.wait(2, reg=0x14, base=I2C1)
    rig.w(0x00, rig.r(0x00, I2C1) & ~(1 << 10), I2C1)
    rig.r(0x14, I2C1)
    rig.r(0x18, I2C1)
    rig.w(0x00, rig.r(0x00, I2C1) | (1 << 9), I2C1)
    rig.w(TXDR, 0x5C)
    rig.wait(0x40, reg=0x14, base=I2C1)
    assert rig.r(0x10, I2C1) == 0x5C
    rig.wait(STOPF)
    assert rig.r(ISR) & NACKF


def test_dma_transmit_and_receive(desc):
    """RM0390 23.7: TXDMAEN -> FMPI2C1_TX on TXIS (DMA1 stream 5 channel 2), RXDMAEN -> FMPI2C1_RX on RXNE (DMA1
    stream 2 channel 2); the end of transfer is NBYTES + AUTOEND."""
    rig = Rig(desc)
    rig.setup(cr1=TXDMAEN | RXDMAEN)
    src, dst = 0x20001000, 0x20002000
    for i, b in enumerate(b"\x20DMA!"):
        rig.chip.bus.write(src + i, 1, b)

    def stream(n, ch, mem, cnt, to_periph, par):
        sb = DMA1 + 0x10 + 0x18 * n
        rig.w(0, 0, sb)
        rig.w(0x04, cnt, sb)
        rig.w(0x08, par, sb)
        rig.w(0x0C, mem, sb)
        rig.w(0, (ch << 25) | (1 << 10) | ((1 << 6) if to_periph else 0) | 1, sb)

    stream(5, 2, src, 5, True, F + TXDR)
    rig.w(CR2, (0x50 << 1) | (5 << 16) | AUTOEND | START)
    rig.wait(STOPF)
    rig.w(ICR, STOPF)
    rig.step(300_000)
    assert rig.ee.dump(0x20, 4) == b"DMA!"
    rig.w(CR2, (0x50 << 1) | (1 << 16) | START)
    rig.wait(TXIS)
    rig.w(CR1, PE)                                             # DMA off for the address byte
    rig.w(TXDR, 0x20)
    rig.wait(TC)
    rig.w(CR1, PE | RXDMAEN)
    stream(2, 2, dst, 4, False, F + RXDR)
    rig.w(CR2, (0x50 << 1) | RD_WRN | (4 << 16) | AUTOEND | START)
    rig.wait(STOPF)
    assert bytes(rig.chip.bus.read(dst + i, 1) for i in range(4)) == b"DMA!"


# ------------------------------------------------------------------------------------------- sample firmware
FW = ROOT / "firmware" / "samples_stm32f446" / "build" / "fmpi2c_eeprom.elf"
HAL = ROOT / "firmware" / "samples_stm32_hal_f446" / "build" / "fmpi2c_hal.elf"


@pytest.mark.skipif(not FW.exists(), reason="firmware/samples_stm32f446 not built")
def test_sample_fmpi2c_eeprom_board():
    """Bare-metal sample on boards/stm32f446re_fmpi2c_eeprom.yaml (RM0390 23.4.9 controller flows): Fm+ page writes
    with acknowledge polling (NACKF + automatic STOP while the EEPROM is busy), dummy write + TC, repeated START read
    with AUTOEND; the SCL period follows RM0390 Table 135 (1 MHz Fm+ at fI2CCLK = HSI 16 MHz: 12 tI2CCLK incl. tSYNC)."""
    from chipem.session import Session
    s = Session.from_board(ROOT / "boards" / "stm32f446re_fmpi2c_eeprom.yaml", quiet=True)
    s.run_for(20e6)
    out = s.uart_output("A.USART2")
    assert out.startswith(b"EEPROM: Hello FMPI2C!\r\npolls: ")
    assert int(out.split(b"polls: ")[1].split(b"\r")[0]) > 100       # 5 ms write cycles at ~1 MHz
    f = s.chips["A"].peripherals["FMPI2C1"]
    assert abs(f.timing().period_ns - 12 * TK) < 1e-6
    assert not [e for e in s.events(kinds={"warn", "fault"}) if "FMPI2C" in str(getattr(e, "msg", ""))]
    s.close()


@pytest.mark.skipif(not HAL.exists(), reason="firmware/samples_stm32_hal_f446 not built")
def test_sample_fmpi2c_hal():
    """STM32CubeF4 HAL_FMPI2C polling API (Init with Timing, Mem_Write, IsDeviceReady, Mem_Read) against the
    i2c_eeprom part; HAL_RCCEx_PeriphCLKConfig selects HSI as kernel clock (RM0390 6.3.27 DCKCFGR2.FMPI2C1SEL)."""
    from chipem.session import Session
    s = Session("stm32f446re", str(HAL))
    b = s.ensure_board()
    b.add_net("SCL", [("A", "PC6")], open_drain=True)
    b.add_net("SDA", [("A", "PC7")], open_drain=True)
    ee = b.add_part("EE", "i2c_eeprom", {}, {"sda": "SDA", "scl": "SCL"})
    s.run_for(30e6)
    assert s.uart_output("A.USART2") == b"HAL EEPROM: Hello HAL FMPI2C!\r\n"
    assert ee.dump(0x40, 17) == b"Hello HAL FMPI2C!"
    assert (s.chip.peripherals["RCC"].get("DCKCFGR2") >> 22) & 3 == 2
    s.close()
