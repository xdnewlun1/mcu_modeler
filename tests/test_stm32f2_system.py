"""STM32F2 model differences (chipem/periph/stm32/f2.py and the F2 register table): RCC 120/30/60 MHz limits, PLL
ranges and flash wait states, PWR without VOS, RTC v1 (no sub-seconds / shift / smooth calibration), DBGMCU IDCODE,
reserved bits read 0, CAN reset values, UART5 DMA bits, the F2 DMA request map and the CRYP/HASH gating."""
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
RCC, PWR, RTC, FLASHIF, DBGMCU = 0x40023800, 0x40007000, 0x40002800, 0x40023C00, 0xE0042000
CR, PLLCFGR, CFGR, AHB1ENR, APB1ENR, BDCR, PLLI2SCFGR = 0x00, 0x04, 0x08, 0x30, 0x40, 0x70, 0x84
_D: dict = {}


def desc(fn: str = "stm32f207zg"):
    if fn not in _D:
        _D[fn] = load_chip(ROOT / "chips" / f"{fn}.yaml")
    return _D[fn]


def spin_chip(d, body: str = "", **kw) -> Chip:
    code = asm(f"{body}\nspin: b spin", FLASH + 0x200)
    img = bytearray(0x200 + len(code))
    img[0:8] = struct.pack("<II", 0x20020000, (FLASH + 0x200) | 1)
    img[0x200:] = code
    chip = Chip(d, **kw)
    chip.load_firmware(FirmwareImage([(FLASH, bytes(img))]))
    chip.reset()
    return chip


def rd(chip, a):
    return chip.bus.read(a, 4)


def wr(chip, a, v):
    chip.bus.write(a, 4, v)


def warns(chip) -> list[str]:
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "warn"]


@pytest.fixture
def chip():
    return spin_chip(desc())


def pll(chip, m, n, p_field, q, *, hse=True, sw=True, ws=3, ppre=(5 << 10) | (4 << 13)):
    wr(chip, FLASHIF, ws)
    wr(chip, RCC + CR, rd(chip, RCC + CR) | (1 << 16))
    wr(chip, RCC + PLLCFGR, m | (n << 6) | (p_field << 16) | ((1 << 22) if hse else 0) | (q << 24))
    wr(chip, RCC + CR, rd(chip, RCC + CR) | (1 << 24))
    if sw:
        wr(chip, RCC + CFGR, ppre | 2)


# ------------------------------------------------------------------------------------------------ RCC
def test_rcc_120mhz_tree_without_warnings(chip):
    pll(chip, 8, 240, 0, 5)
    r = chip.peripherals["RCC"]
    assert r.model == "stm32.rcc_f2"
    assert (r.core_hz, r.apb1_hz, r.apb2_hz, r.timer_hz(1), r.timer_hz(2)) == (120e6, 30e6, 60e6, 60e6, 120e6)
    assert r.pll_q_hz() == 48e6 and chip.clock.hz == 120e6
    assert not warns(chip)


def test_rcc_168mhz_exceeds_f2_limits(chip):
    pll(chip, 8, 336, 0, 7, ws=5, ppre=(5 << 10) | (4 << 13))     # the STM32F405 HAL tree
    w = warns(chip)
    assert any("exceeds the 120 MHz maximum" in x for x in w)
    assert any("APB clock above maximum" in x and "STM32F2" in x for x in w)       # APB1 42 > 30, APB2 84 > 60
    assert any("PLL output 168 MHz" in x for x in w)


def test_rcc_pll_ranges(chip):
    pll(chip, 4, 100, 1, 4, sw=False)                             # VCO in 2 MHz OK, PLLN 100 < 192
    chip.peripherals["RCC"].pll_hz()
    assert any("PLLN = 100 (allowed 192..432)" in x for x in warns(chip))
    c2 = spin_chip(desc())
    pll(c2, 2, 240, 0, 5, sw=False)                               # VCO input 4 MHz > 2 MHz
    c2.peripherals["RCC"].pll_hz()
    assert any("VCO input 4 MHz" in x for x in warns(c2))


def test_rcc_flash_wait_states_warning(chip):
    pll(chip, 8, 240, 0, 5, ws=2)                                 # 120 MHz needs 3 WS at 2.7-3.6 V (RM0033 Table 3)
    assert any("needs FLASH_ACR.LATENCY >= 3" in x for x in warns(chip))
    r = chip.peripherals["RCC"]
    assert [r.min_wait_states(f) for f in (16e6, 30e6, 31e6, 60e6, 90e6, 91e6, 120e6)] == [0, 0, 1, 1, 2, 3, 3]
    wr(chip, FLASHIF, 0xF)
    assert rd(chip, FLASHIF) & 0xF == 7                           # LATENCY[2:0] only


def test_rcc_plli2s_f2_ranges(chip):
    assert rd(chip, RCC + PLLI2SCFGR) == 0x20003000              # no PLLI2SQ
    wr(chip, RCC + PLLI2SCFGR, (2 << 28) | (100 << 6) | (0xF << 24))
    assert rd(chip, RCC + PLLI2SCFGR) == (2 << 28) | (100 << 6)
    wr(chip, RCC + CR, rd(chip, RCC + CR) | (1 << 26))
    chip.peripherals["RCC"].plli2s_hz()
    assert any("PLLI2SN = 100 (allowed 192..432)" in x for x in warns(chip))


def test_rcc_reserved_f4_bits(chip):
    wr(chip, RCC + AHB1ENR, (1 << 20) | 1)                        # CCMDATARAMEN does not exist
    assert rd(chip, RCC + AHB1ENR) == 1
    assert rd(chip, RCC + 0x50) == 0x7E6791FF and rd(chip, RCC + 0x60) == 0x36FEC9FF   # AHB1LPENR, APB1LPENR


# ------------------------------------------------------------------------------------------------ PWR
def test_pwr_has_no_vos(chip):
    assert chip.peripherals["PWR"].model == "stm32.pwr_f2"
    assert rd(chip, PWR) == 0 and rd(chip, PWR + 4) == 0          # F405: CR 0x4000, CSR.VOSRDY
    wr(chip, PWR, (1 << 14) | (1 << 9) | (1 << 8) | (5 << 5) | 1)
    assert rd(chip, PWR) == (1 << 9) | (1 << 8) | (5 << 5) | 1     # VOS reserved; FPDS, DBP, PLS, LPDS kept
    wr(chip, PWR + 4, (1 << 9) | (1 << 14))                       # BRE (DBP set) + VOSRDY (reserved)
    assert rd(chip, PWR + 4) == (1 << 9) | (1 << 3)               # BRE, BRR
    chip.reset()
    assert rd(chip, PWR) == 0 and not rd(chip, PWR + 4) & (1 << 14)


# ------------------------------------------------------------------------------------------------ RTC v1
def rtc_on(chip):
    wr(chip, RCC + APB1ENR, rd(chip, RCC + APB1ENR) | (1 << 28))
    wr(chip, PWR, 1 << 8)                                         # DBP
    wr(chip, RCC + BDCR, 1)                                       # LSEON
    wr(chip, RCC + BDCR, 1 | (1 << 8) | (1 << 15))               # RTCSEL = LSE, RTCEN
    wr(chip, RTC + 0x24, 0xCA)
    wr(chip, RTC + 0x24, 0x53)


def test_rtc_v1_registers(chip):
    assert chip.peripherals["RTC"].model == "stm32.rtc_f2"
    rtc_on(chip)
    wr(chip, RTC + 0x08, (1 << 5) | (1 << 19) | (1 << 6) | (1 << 7))   # BYPSHAD, COSEL reserved; FMT/DCE need INIT
    assert rd(chip, RTC + 0x08) & ((1 << 5) | (1 << 19)) == 0
    for off in (0x28, 0x2C, 0x38, 0x3C, 0x44, 0x48):              # SSR, SHIFTR, TSSSR, CALR, ALRMASSR, ALRMBSSR
        wr(chip, RTC + off, 0xFFFFFFFF)
        assert rd(chip, RTC + off) == 0, hex(off)
    wr(chip, RTC + 0x0C, 1 << 7)                                  # INIT
    chip.run_for(100e3)
    assert rd(chip, RTC + 0x0C) & (1 << 6)                        # INITF
    wr(chip, RTC + 0x10, (0x7F << 16) | 0x7FFF)
    assert rd(chip, RTC + 0x10) == (0x7F << 16) | 0x1FFF          # PREDIV_S is 13 bits
    wr(chip, RTC + 0x10, (0x7F << 16) | 0xFF)
    wr(chip, RTC + 0x00, 0x235958)                                # 23:59:58
    wr(chip, RTC + 0x0C, 0)
    chip.run_for(2.5e9)
    assert rd(chip, RTC + 0x00) == 0x000000 or rd(chip, RTC + 0x00) == 0x000001


# ------------------------------------------------------------------------------------------------ misc blocks
def test_dbgmcu_idcode_f2(chip):
    assert rd(chip, DBGMCU) == 0x20036411                        # DEV_ID 0x411, REV_ID 0x2003 (revision X)


def test_can_reset_values(chip):
    wr(chip, RCC + APB1ENR, rd(chip, RCC + APB1ENR) | (1 << 25))
    assert rd(chip, 0x40006400) == 0x00010002                     # MCR (STM32F21x.svd says 0)
    assert rd(chip, 0x40006404) & 0xFFF == 0xC02                  # MSR: sleep acknowledge, RX recessive
    assert rd(chip, 0x40006408) == 0x1C000000                     # TSR: 3 empty mailboxes
    assert rd(chip, 0x40006600) == 0x2A1C0E01                     # FMR: FINIT, CAN2SB = 14
    assert rd(chip, 0x4000641C) == 0x01230000                     # BTR


def test_uart5_dma_bits_writable(chip):
    wr(chip, RCC + APB1ENR, rd(chip, RCC + APB1ENR) | (1 << 20))
    wr(chip, 0x40005014, 0xC0)                                    # UART5_CR3 DMAT | DMAR (omitted by the F21x SVD)
    assert rd(chip, 0x40005014) == 0xC0


def test_dma_request_map_f2(chip):
    from chipem.periph.stm32.dma import REQUEST_TABLES
    assert chip.peripherals["DMA1"].params["requests"] == "f2"
    t = REQUEST_TABLES["f2"]
    assert t[1][7][4] == ("UART5_TX",) and t[2][7][4] == ("USART1_TX",) and t[2][6][2] == ("CRYP_IN",)
    assert t[1][1][5] == () and t[2][1][0] == ()                  # F42x UART7 / SAI1 requests do not exist


def test_spi_i2s_ck_signal_named_sck(chip):
    eng = chip.peripherals["SPI2"]
    sig = getattr(getattr(eng, "i2s", None), "sig", None) or getattr(eng, "_i2s", None)
    if sig is None:
        pytest.skip("SPI model keeps its I2S engine elsewhere")
    assert sig["CK"] == "I2S2_SCK"


def test_crypto_gating():
    f207 = spin_chip(desc("stm32f207zg"))
    f217 = spin_chip(desc("stm32f217zg"))
    for c in (f207, f217):
        wr(c, RCC + 0x34, (1 << 4) | (1 << 5))
    assert f207.peripherals["CRYP"].absent and f207.peripherals["HASH"].absent
    assert not f217.peripherals["CRYP"].absent and not f217.peripherals["Ethernet_MAC"].absent
    wr(f217, 0x50060000, (1 << 19) | (4 << 3))                   # ALGOMODE[3] (GCM/CCM): reserved on the F2
    assert rd(f217, 0x50060000) == 4 << 3
    wr(f217, 0x50060400, (1 << 18) | (1 << 7))                    # HASH ALGO[1] (SHA-2): reserved
    assert not rd(f217, 0x50060400) & (1 << 18)
    assert rd(f207, 0x50060000) == 0 and any("not present on the STM32F207ZG" in w for w in warns(f207))


def test_eth_dcmi_absent_on_f205():
    c = spin_chip(desc("stm32f205zg"))
    assert c.peripherals["Ethernet_MAC"].absent and c.peripherals["DCMI"].absent
    assert c.peripherals["CRYP"].absent and c.peripherals["HASH"].absent
