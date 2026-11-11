"""stm32f1.rcc_cl: the STM32F105/F107 connectivity-line RCC (RM0008 Rev 21 ch. 8).  Each test names the RM0008
section it checks (8.2 clock tree, 8.3.x registers, Table 19 reset values)."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.firmware import FirmwareImage

from engine_util import asm

ROOT = Path(__file__).resolve().parent.parent
FLASH = 0x08000000
RCC = 0x40021000
CR, CFGR, CIR, APB2RSTR, APB1RSTR, AHBENR, APB2ENR, APB1ENR, BDCR, CSR, AHBRSTR, CFGR2 = range(0, 0x30, 4)
PLLON, PLLRDY, PLL2ON, PLL2RDY, PLL3ON, PLL3RDY = (1 << b for b in range(24, 30))
HSEON, HSERDY = 1 << 16, 1 << 17
# the reference design (8.2): PREDIV2 /5, PLL2 x8, PLL3 x10, PREDIV1SRC = PLL2, PREDIV1 /5
REF_CFGR2 = (4 << 4) | (6 << 8) | (8 << 12) | (1 << 16) | 4
PLLSRC, PLLMUL9 = 1 << 16, 7 << 18


@pytest.fixture(scope="module")
def desc():
    return load_chip(ROOT / "chips" / "stm32f107vc.yaml")


def make(desc) -> Chip:
    code = asm("spin: b spin", FLASH + 0x200)
    img = bytearray(0x200 + len(code))
    img[0:8] = struct.pack("<II", 0x20010000, (FLASH + 0x200) | 1)
    img[0x200:] = code
    chip = Chip(desc)
    chip.load_firmware(FirmwareImage([(FLASH, bytes(img))]))
    chip.reset()
    return chip


def rd(chip, off):
    return chip.bus.read(RCC + off, 4)


def wr(chip, off, v):
    chip.bus.write(RCC + off, 4, v)


def warns(chip):
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "warn"]


def rcc(chip):
    return chip.peripherals["RCC"]


def ref_clock(chip, mco: int = 0):
    """8.2: HSE 25 MHz -> PREDIV2 /5 -> PLL2 x8 = 40 MHz -> PREDIV1 /5 -> PLL x9 = 72 MHz."""
    wr(chip, CR, rd(chip, CR) | HSEON)
    wr(chip, CFGR2, REF_CFGR2)
    wr(chip, CR, rd(chip, CR) | PLL2ON | PLL3ON)
    wr(chip, CFGR, PLLSRC | PLLMUL9 | (4 << 8) | (mco << 24))
    wr(chip, CR, rd(chip, CR) | PLLON)
    wr(chip, CFGR, rd(chip, CFGR) | 2)


def test_reset_values(desc):
    """8.3.13 Table 19 / 8.3.1-8.3.12: CR 0x0000XX83, CFGR/CIR/CFGR2/AHBRSTR 0, AHBENR 0x14, CSR flags POR | PIN."""
    c = make(desc)
    assert rd(c, CR) & 0xFFFF00FF == 0x00000083
    assert rd(c, CFGR) == 0 and rd(c, CIR) == 0 and rd(c, CFGR2) == 0 and rd(c, AHBRSTR) == 0
    assert rd(c, AHBENR) == 0x14
    assert rd(c, CSR) == 0x0C000000
    assert rcc(c).core_hz == 8e6


def test_reference_clock_tree_72mhz(desc):
    """8.2 / Figure 11: PLL2 = 40 MHz, PREDIV1 input PLL2 /5 = 8 MHz, PLL x9 = 72 MHz; OTGFSCLK = VCO 144 / 3."""
    c = make(desc)
    ref_clock(c)
    r = rcc(c)
    assert rd(c, CR) & (PLL2RDY | PLL3RDY | PLLRDY) == PLL2RDY | PLL3RDY | PLLRDY
    assert r.pll2_hz() == 40e6 and r.pll3_hz() == 50e6
    assert r.pll_in_hz() == 8e6 and r.pll_hz() == 72e6
    assert (rd(c, CFGR) >> 2) & 3 == 2 and r.core_hz == 72e6 and c.clock.hz == 72e6
    assert r.clk48_hz() == 48e6                                    # OTGFSPRE = 0: VCO / 3
    wr(c, CFGR, rd(c, CFGR) | (1 << 22))
    assert r.clk48_hz() == 72e6                                    # VCO / 2 (needs a 48 MHz PLL)


def test_pllmul_table(desc):
    """8.3.2 PLLMUL: 0010..0111 = x4..x9, 1101 = x6.5; 000x, 10xx, 1100, 111x reserved (the PLL never locks)."""
    c = make(desc)
    r = rcc(c)
    for code, mul in ((2, 4), (7, 9), (13, 6.5)):
        wr(c, CFGR, code << 18)
        assert r.pll_mul() == mul
    wr(c, CFGR, 8 << 18)                                           # 1000: reserved
    wr(c, CR, rd(c, CR) | PLLON)
    assert rd(c, CR) & PLLON and not rd(c, CR) & PLLRDY
    assert any("reserved value" in w for w in warns(c))


def test_pll2_pll3_mul_and_prediv2(desc):
    """8.3.12 PLL2MUL / PLL3MUL (x8..x14, x16, x20) and PREDIV2 (/1../16)."""
    c = make(desc)
    r = rcc(c)
    wr(c, CFGR2, (1 << 4) | (15 << 8) | (14 << 12))                # PREDIV2 /2, PLL2 x20, PLL3 x16
    assert r.predivs()[1] == 2 and r.pll2_hz() == 25e6 / 2 * 20 and r.pll3_hz() == 25e6 / 2 * 16
    wr(c, CFGR2, 13 << 8)                                          # 1101: reserved
    wr(c, CR, rd(c, CR) | PLL2ON)
    assert not rd(c, CR) & PLL2RDY


def test_ready_flags_and_cir(desc):
    """8.3.1 PLL2RDY/PLL3RDY follow PLL2ON/PLL3ON; 8.3.3 PLL2RDYF/PLL3RDYF set only with PLLxRDYIE, cleared by
    PLLxRDYC; the RCC vector follows flag & enable."""
    c = make(desc)
    wr(c, CIR, (1 << 13) | (1 << 14))                              # PLL2RDYIE, PLL3RDYIE
    wr(c, CFGR2, REF_CFGR2)
    wr(c, CR, rd(c, CR) | PLL2ON)
    assert rd(c, CR) & PLL2RDY and rd(c, CIR) & 0x7F == 1 << 5
    assert rcc(c)._line                                            # RCC vector asserted
    wr(c, CR, rd(c, CR) | PLL3ON)
    assert rd(c, CIR) & 0x7F == (1 << 5) | (1 << 6)
    wr(c, CIR, rd(c, CIR) | (1 << 21))                             # PLL2RDYC
    assert rd(c, CIR) & 0x7F == 1 << 6
    wr(c, CIR, rd(c, CIR) | (1 << 22))
    assert rd(c, CIR) & 0x7F == 0
    wr(c, CR, rd(c, CR) & ~PLL3ON)
    assert not rd(c, CR) & PLL3RDY                                  # ready drops with ON


def test_cfgr2_locked_while_plls_run(desc):
    """8.3.12: PREDIV1/PREDIV1SRC only with the PLL off, PLL2MUL with PLL2 off, PLL3MUL with PLL3 off, PREDIV2 with
    PLL2 and PLL3 off; I2S2SRC/I2S3SRC always writable."""
    c = make(desc)
    ref_clock(c)
    wr(c, CFGR2, 0x7FFFF)
    v = rd(c, CFGR2)
    assert v & 0xFFFF == REF_CFGR2 & 0xFFFF and v & (1 << 16)
    assert v & (3 << 17) == 3 << 17                                # I2SxSRC written
    assert any("CFGR2" in w for w in warns(c))


def test_pllxtpre_is_prediv1_bit0(desc):
    """8.3.2 PLLXTPRE / 8.3.12 PREDIV1 note: CFGR bit 17 and CFGR2 bit 0 are the same bit."""
    c = make(desc)
    wr(c, CFGR, 1 << 17)
    assert rd(c, CFGR2) & 1
    wr(c, CFGR2, 0)
    assert not rd(c, CFGR) & (1 << 17)
    wr(c, CFGR2, 3)                                                # PREDIV1 /4
    assert rd(c, CFGR) & (1 << 17) and rcc(c).predivs()[0] == 4


def test_pll2_cannot_stop_while_feeding_sysclk(desc):
    """8.3.1 PLL2ON: cannot be cleared while PLL2 is used indirectly as system clock (and neither can HSEON)."""
    c = make(desc)
    ref_clock(c)
    wr(c, CR, rd(c, CR) & ~(PLL2ON | HSEON))
    assert rd(c, CR) & PLL2ON and rd(c, CR) & HSEON
    wr(c, CR, rd(c, CR) & ~PLL3ON)                                 # PLL3 does not feed SYSCLK
    assert not rd(c, CR) & PLL3ON


def test_otgfspre_and_otg_clock_check(desc):
    """8.3.2 OTGFSPRE cannot be cleared while the OTG FS clock is enabled; 8.2: OTGFSCLK must be 48 MHz (warn)."""
    c = make(desc)
    ref_clock(c)
    wr(c, CFGR, rd(c, CFGR) | (1 << 22))
    wr(c, AHBENR, rd(c, AHBENR) | (1 << 12))                       # OTGFSEN with VCO / 2 = 72 MHz
    assert any("OTGFSCLK" in w for w in warns(c))
    wr(c, CFGR, rd(c, CFGR) & ~(1 << 22))
    assert rd(c, CFGR) & (1 << 22)


def test_mco_sources(desc):
    """8.2.10 / 8.3.2 MCO[3:0]: 1000 PLL2, 1001 PLL3/2, 1010 XT1, 1011 PLL3, 0111 PLL/2."""
    c = make(desc)
    ref_clock(c)
    r = rcc(c)
    expect = {0x7: 36e6, 0x8: 40e6, 0x9: 25e6, 0xA: 25e6, 0xB: 50e6, 0x4: 72e6, 0x0: 0.0, 0xC: 0.0}
    for sel, hz in expect.items():
        wr(c, CFGR, (rd(c, CFGR) & ~(0xF << 24)) | (sel << 24))
        assert r.mco_hz() == hz, hex(sel)
    assert rd(c, CFGR) >> 24 == 0xC                                # 4-bit field (bit 27 writable)


def test_i2s_clock_source(desc):
    """8.3.12 I2S2SRC / I2S3SRC: SYSCLK or the PLL3 VCO (2 x PLL3CLK)."""
    c = make(desc)
    ref_clock(c)
    r = rcc(c)
    assert r.i2s_kernel_hz("SPI2") == 72e6
    wr(c, CFGR2, rd(c, CFGR2) | (1 << 18))
    assert r.i2s_kernel_hz("SPI3") == 100e6 and r.i2s_kernel_hz("SPI2") == 72e6


def test_ahbrstr_and_cl_gates(desc):
    """8.3.11 AHBRSTR OTGFSRST/ETHMACRST reset the blocks; 8.3.6-8.3.8 gate bits (CAN1 25, CAN2 26, ETHMAC 14)."""
    c = make(desc)
    r = rcc(c)
    assert r.GATES["CAN1"] == ("APB1ENR", 25) and r.GATES["CAN2"] == ("APB1ENR", 26)
    assert r.GATES["ETH"] == ("AHBENR", 14) and r.GATES["OTG_FS"] == ("AHBENR", 12)
    assert "USB" not in r.GATES or r.GATES["USB"] == ("AHBENR", 12)
    otg = c.peripherals["OTG_FS"]
    wr(c, AHBENR, rd(c, AHBENR) | (1 << 12))
    c.bus.write(0x50000000 + 0x38, 4, 1 << 16)                    # GCCFG.PWRDWN
    assert c.bus.read(0x50000038, 4) & (1 << 16)
    wr(c, AHBRSTR, 1 << 12)
    wr(c, AHBRSTR, 0)
    assert c.bus.read(0x50000038, 4) == 0 and otg is c.peripherals["OTG_FS"]
    wr(c, AHBRSTR, 0xFFFFFFFF)
    assert rd(c, AHBRSTR) == (1 << 12) | (1 << 14)                 # reserved bits read 0


def test_stop_entry_clears_pll2_pll3(desc):
    """8.3.1: PLL2ON / PLL3ON are cleared by hardware when entering Stop or Standby mode."""
    c = make(desc)
    ref_clock(c)
    rcc(c).enter_stop()
    assert rd(c, CR) & (PLL2ON | PLL3ON | PLLON | HSEON) == 0 and (rd(c, CFGR) >> 2) & 3 == 0
