"""STM32F427/F437 system blocks (chipem/periph/stm32/f42x.py): RCC PLLSAI / DCKCFGR / TIMPRE / F42x clock gates
(RM0090 ch. 6), PWR over-drive / under-drive / voltage scaling (RM0090 5.1.4, 5.5; DS9405 Tables 17 and 23), the
dual-bank flash interface and option bytes (RM0090 3.5, 3.9, Tables 6-9), the chip description against the SVD, and
the f42x_clock180_uart7 sample (180 MHz + UART7 + TIM2 with TIMPRE)."""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.firmware import FirmwareImage, load_firmware
from chipem.session import Session

from engine_util import asm

ROOT = Path(__file__).resolve().parent.parent
F427 = ROOT / "chips" / "stm32f427vg.yaml"
F437 = ROOT / "chips" / "stm32f437ai.yaml"
SVD = ROOT / "svd" / "dl-stm32f427" / "STM32F427.svd"
BUILD = ROOT / "firmware" / "samples_stm32_variants" / "build"

FLASH = 0x08000000
RCC, FLASH_IF, PWR = 0x40023800, 0x40023C00, 0x40007000
CR, PLLCFGR, CFGR, PLLI2SCFGR, PLLSAICFGR, DCKCFGR = 0x00, 0x04, 0x08, 0x84, 0x88, 0x8C


@pytest.fixture(scope="module")
def d427():
    return load_chip(F427)


@pytest.fixture(scope="module")
def d437():
    return load_chip(F437)


def spin_chip(desc, **kw) -> Chip:
    code = asm("spin: b spin", FLASH + 0x200)
    img = bytearray(0x200 + len(code))
    img[0:8] = struct.pack("<II", 0x20030000, (FLASH + 0x200) | 1)
    img[0x200:] = code
    chip = Chip(desc, **kw)
    chip.load_firmware(FirmwareImage([(FLASH, bytes(img))]))
    chip.reset()
    return chip


def rd(chip, addr):
    return chip.bus.read(addr, 4)


def wr(chip, addr, v):
    chip.bus.write(addr, 4, v)


def warns(chip) -> list[str]:
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "warn"]


def tool(name: str):
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        return __import__(name)
    finally:
        sys.path.pop(0)


# --------------------------------------------------------------------------------------------- description
def test_description_memory_core_irqs(d427, d437):
    for d, flash in ((d427, 0x100000), (d437, 0x200000)):
        mem = {m.name: (m.base, m.size) for m in d.memory}
        assert mem["FLASH"] == (FLASH, flash)
        assert mem["SRAM"] == (0x20000000, 0x30000)              # SRAM1 112 + SRAM2 16 + SRAM3 64 KB
        assert mem["CCMRAM"] == (0x10000000, 0x10000) and mem["BKPSRAM"] == (0x40024000, 0x1000)
        assert d.core.clock_hz == 16e6 and d.core.fpu and d.core.num_irqs == 91
        # RM0090 Table 63 additions
        assert {n: d.irqs[n] for n in (48, 82, 83, 84, 85, 86, 87, 90)} == {
            48: "FMC", 82: "UART7", 83: "UART8", 84: "SPI4", 85: "SPI5", 86: "SPI6", 87: "SAI1", 90: "DMA2D"}
        assert 88 not in d.irqs and 89 not in d.irqs                # LTDC: F429/F439 only
    assert (d427.name, d427.package, d437.name, d437.package) == ("STM32F427VG", "LQFP100", "STM32F437AI", "UFBGA169")
    assert d437.irqs == d427.irqs
    assert [(p.name, p.base) for p in d437.peripherals] == [(p.name, p.base) for p in d427.peripherals]


def test_svd_check_zero_mismatches():
    if not SVD.exists():
        pytest.skip("STM32F427.svd not downloaded")
    import io
    for f in (F427, F437):
        out = io.StringIO()
        assert tool("svd2chip").check(str(SVD), str(f), out=out) == 0, out.getvalue()
        assert "irq 87: chip YAML 'SAI1'; SVD defines no interrupt 87" in out.getvalue()   # SVD gap (RM0090 Table 63)


def test_chip_builds_with_f42x_models(d427, d437):
    for d in (d427, d437):
        chip = spin_chip(d)
        p = chip.peripherals
        assert (p["RCC"].model, p["PWR"].model, p["FLASH"].model) == ("stm32.rcc_f42x", "stm32.pwr_f42x",
                                                                       "stm32.flash_f42x")
        assert rd(chip, 0xE0042000) == 0x20036419                   # DEV_ID 0x419 (RM0090 38.6.1)
    from chipem.periph.stm32 import IMPORT_ERRORS
    assert IMPORT_ERRORS == {}


def test_crypto_gated_by_part(d427, d437):
    assert spin_chip(d427).peripherals["CRYP"].__dict__.get("absent") is True
    assert not spin_chip(d437).peripherals["CRYP"].__dict__.get("absent", False)


# --------------------------------------------------------------------------------------------- RCC
def test_rcc_f42x_reset_values_and_new_gates(d427):
    chip = spin_chip(d427)
    assert rd(chip, RCC + PLLSAICFGR) == 0x24003000 and rd(chip, RCC + DCKCFGR) == 0
    assert rd(chip, RCC + PLLI2SCFGR) == 0x24003000
    # RM0090 6.3.10/6.3.13/6.3.14: GPIOJ/K, DMA2D, UART7/8, SPI4-6, SAI1 enables are writable
    wr(chip, RCC + 0x30, (1 << 9) | (1 << 10) | (1 << 23))
    wr(chip, RCC + 0x40, (1 << 30) | (1 << 31))
    wr(chip, RCC + 0x44, (1 << 13) | (1 << 20) | (1 << 21) | (1 << 22))
    assert rd(chip, RCC + 0x30) == (1 << 9) | (1 << 10) | (1 << 23)
    assert rd(chip, RCC + 0x40) == (1 << 30) | (1 << 31)
    assert rd(chip, RCC + 0x44) == (1 << 13) | (1 << 20) | (1 << 21) | (1 << 22)
    r = chip.peripherals["RCC"]
    assert all(r.clock_enabled(g) for g in ("GPIOJ", "GPIOK", "DMA2D", "UART7", "UART8", "SPI4", "SPI5", "SPI6",
                                             "SAI1"))
    wr(chip, RCC + 0x40, 0)
    assert not r.clock_enabled("UART7")


def test_rcc_pllsai_and_sai_clock(d427):
    chip = spin_chip(d427)
    r = chip.peripherals["RCC"]
    calls = []
    r.listeners.append(lambda: calls.append(1))
    # HSE 8 MHz, PLLM 8 -> 1 MHz VCO input; PLLSAIN 344, PLLSAIQ 7 -> 49.142857 MHz; PLLSAIDIVQ /1
    wr(chip, RCC + CR, rd(chip, RCC + CR) | (1 << 16))
    wr(chip, RCC + PLLCFGR, 8 | (360 << 6) | (1 << 22) | (7 << 24))
    wr(chip, RCC + PLLSAICFGR, (344 << 6) | (7 << 24) | (2 << 28))
    assert r.sai_clk_hz("A") == 0.0                               # PLLSAI off
    wr(chip, RCC + CR, rd(chip, RCC + CR) | (1 << 28))
    assert rd(chip, RCC + CR) & (1 << 29)                          # PLLSAIRDY
    assert r.sai_clk_hz("A") == pytest.approx(344e6 / 7)
    assert calls
    wr(chip, RCC + DCKCFGR, 3 << 8)                                # PLLSAIDIVQ = /4
    assert r.sai_clk_hz("B") == pytest.approx(344e6 / 7 / 4)
    # SAI1BSRC = 01: PLLI2S_Q / PLLI2SDIVQ
    wr(chip, RCC + PLLI2SCFGR, (192 << 6) | (4 << 24) | (2 << 28))
    wr(chip, RCC + DCKCFGR, (1 << 22) | 1)
    wr(chip, RCC + CR, rd(chip, RCC + CR) | (1 << 26))
    assert r.sai_clk_hz("B") == pytest.approx(192e6 / 4 / 2)
    assert r.sai_clk_hz("A") == pytest.approx(344e6 / 7)
    wr(chip, RCC + DCKCFGR, 3 << 20)                               # 11 = wrong configuration
    assert r.sai_clk_hz("A") == 0.0 and any("SAI1ASRC" in w or "SAI1A" in w for w in warns(chip))


def test_rcc_timpre(d427):
    chip = spin_chip(d427)
    r = chip.peripherals["RCC"]
    wr(chip, RCC + CFGR, (5 << 10) | (4 << 13))                    # APB1 /4, APB2 /2 at HSI 16 MHz
    assert (r.timer_hz(1), r.timer_hz(2)) == (8e6, 16e6)           # 2 x PCLK
    wr(chip, RCC + DCKCFGR, 1 << 24)                               # TIMPRE: /1, /2, /4 -> HCLK
    assert (r.timer_hz(1), r.timer_hz(2)) == (16e6, 16e6)
    wr(chip, RCC + CFGR, (6 << 10) | (7 << 13))                    # APB1 /8, APB2 /16 -> 4 x PCLK
    assert (r.timer_hz(1), r.timer_hz(2)) == (8e6, 4e6)
    assert r.effective_timer_hz(1) == 8e6


# --------------------------------------------------------------------------------------------- PWR over-drive
def _to_hse(chip):
    wr(chip, RCC + CR, rd(chip, RCC + CR) | (1 << 16))
    wr(chip, RCC + CFGR, 1)


def test_pwr_reset_and_vos(d427):
    chip = spin_chip(d427)
    wr(chip, RCC + 0x40, 1 << 28)
    assert rd(chip, PWR) == 0xC000 and rd(chip, PWR + 4) & (1 << 14)
    wr(chip, PWR, 0x4000)                                          # scale 3 while the PLL is off: accepted
    assert rd(chip, PWR) == 0x4000
    assert chip.peripherals["PWR"].vos_scale() == 3
    wr(chip, RCC + CR, rd(chip, RCC + CR) | (1 << 24))             # PLL on (HSI): VOS now frozen
    wr(chip, PWR, 0xC000)
    assert rd(chip, PWR) == 0x4000 and any("VOS" in w for w in warns(chip))
    assert chip.peripherals["PWR"].vos_scale() == 3


def test_pwr_overdrive_sequence_timing(d427):
    chip = spin_chip(d427)
    wr(chip, RCC + 0x40, 1 << 28)
    pwr = chip.peripherals["PWR"]
    t0 = chip.now_ns()
    wr(chip, PWR, rd(chip, PWR) | (1 << 17))                       # ODSWEN before ODEN/ODRDY: ignored
    assert not rd(chip, PWR) & (1 << 17)
    wr(chip, PWR, rd(chip, PWR) | (1 << 16))                       # ODEN
    assert not rd(chip, PWR + 4) & (1 << 16)
    chip.run_for(19e3)
    assert not rd(chip, PWR + 4) & (1 << 16)
    chip.run_for(2e3)
    assert rd(chip, PWR + 4) & (1 << 16)                           # ODRDY after 20 us
    wr(chip, PWR, rd(chip, PWR) | (1 << 17))                       # ODSWEN (HSI: Tod_swen = 45 us)
    t1 = chip.now_ns()
    chip.run_for(44e3)
    assert not rd(chip, PWR + 4) & (1 << 17)
    chip.run_for(2e3)
    assert rd(chip, PWR + 4) & (1 << 17) and pwr.od_active()       # ODSWRDY
    assert t1 - t0 >= 20e3
    # exit (sequence 1): clear ODEN and ODSWEN together -> ODSWRDY drops after Tod_swdis (20 us, HSI)
    wr(chip, PWR, rd(chip, PWR) & ~(3 << 16))
    chip.run_for(19e3)
    assert rd(chip, PWR + 4) & (1 << 17)
    chip.run_for(2e3)
    assert not rd(chip, PWR + 4) & (3 << 16)


def test_pwr_overdrive_needs_hsi_or_hse_sysclk_and_hse_timing(d427):
    chip = spin_chip(d427)
    wr(chip, RCC + 0x40, 1 << 28)
    _to_hse(chip)                                                  # 8 MHz crystal: Tod_swen = 100 - 55 x 4/22 us
    wr(chip, PWR, rd(chip, PWR) | (1 << 16))
    chip.run_for(25e3)
    wr(chip, PWR, rd(chip, PWR) | (1 << 17))
    want = 100e3 - 55e3 * 4 / 22
    chip.run_for(want - 1e3)
    assert not rd(chip, PWR + 4) & (1 << 17)
    chip.run_for(2e3)
    assert rd(chip, PWR + 4) & (1 << 17)
    # SYSCLK = PLL: ODEN cannot be changed
    wr(chip, RCC + PLLCFGR, 8 | (180 << 6) | (1 << 22) | (7 << 24))
    wr(chip, RCC + CR, rd(chip, RCC + CR) | (1 << 24))
    wr(chip, RCC + CFGR, 2)
    wr(chip, PWR, rd(chip, PWR) & ~(3 << 16))
    assert rd(chip, PWR) & (3 << 16) == 3 << 16
    assert any("HSI or HSE must be the system clock" in w for w in warns(chip))


def test_clock_limits_depend_on_overdrive(d427):
    chip = spin_chip(d427)
    wr(chip, RCC + 0x40, 1 << 28)
    _to_hse(chip)
    wr(chip, RCC + PLLCFGR, 8 | (360 << 6) | (1 << 22) | (7 << 24))   # 180 MHz
    wr(chip, RCC + CR, rd(chip, RCC + CR) | (1 << 24))
    wr(chip, RCC + CFGR, (5 << 10) | (4 << 13) | 1)
    wr(chip, RCC + CFGR, (5 << 10) | (4 << 13) | 2)                # to PLL without over-drive
    assert chip.peripherals["RCC"].core_hz == 180e6
    assert any("exceeds the 168 MHz maximum" in w and "over-drive off" in w for w in warns(chip))


def test_clock_180_with_overdrive_no_warning(d427):
    chip = spin_chip(d427)
    wr(chip, RCC + 0x40, 1 << 28)
    _to_hse(chip)
    wr(chip, RCC + PLLCFGR, 8 | (360 << 6) | (1 << 22) | (7 << 24))
    wr(chip, RCC + CR, rd(chip, RCC + CR) | (1 << 24))
    wr(chip, PWR, rd(chip, PWR) | (1 << 16))
    chip.run_for(30e3)
    wr(chip, PWR, rd(chip, PWR) | (1 << 17))
    chip.run_for(120e3)
    wr(chip, RCC + CFGR, (5 << 10) | (4 << 13) | 1)
    wr(chip, RCC + CFGR, (5 << 10) | (4 << 13) | 2)
    r = chip.peripherals["RCC"]
    assert (r.core_hz, r.apb1_hz, r.apb2_hz, chip.clock.hz) == (180e6, 45e6, 90e6, 180e6)
    assert not [w for w in warns(chip) if "maximum" in w]


def test_stop_clears_overdrive_pllsai_and_sets_udrdy(d427):
    chip = spin_chip(d427)
    wr(chip, RCC + 0x40, 1 << 28)
    wr(chip, PWR, rd(chip, PWR) | (1 << 16))
    chip.run_for(30e3)
    wr(chip, PWR, rd(chip, PWR) | (1 << 17))
    chip.run_for(60e3)
    wr(chip, RCC + PLLSAICFGR, (192 << 6) | (4 << 24) | (2 << 28))
    wr(chip, RCC + CR, rd(chip, RCC + CR) | (1 << 28))
    wr(chip, PWR, rd(chip, PWR) | (3 << 18) | (1 << 11))           # UDEN = 11, MRUDS (main regulator in Stop)
    pwr = chip.peripherals["PWR"]
    wr(chip, 0xE000ED10, 1 << 2)                                   # SCB_SCR.SLEEPDEEP
    pwr._enter_stop(chip)
    assert pwr.mode == "stop"
    assert rd(chip, PWR) & (3 << 16) == 0
    assert rd(chip, PWR + 4) & (3 << 16) == 0 and rd(chip, PWR + 4) & (3 << 18) == 3 << 18
    assert rd(chip, RCC + CR) & (3 << 28) == 0
    wr(chip, PWR + 4, 3 << 18)                                     # UDRDY: write 1 to clear
    assert rd(chip, PWR + 4) & (3 << 18) == 0


# --------------------------------------------------------------------------------------------- FLASH
def _unlock(chip):
    wr(chip, FLASH_IF + 4, 0x45670123)
    wr(chip, FLASH_IF + 4, 0xCDEF89AB)
    assert not rd(chip, FLASH_IF + 0x10) & (1 << 31)


def _fill(chip, base, size, val=0):
    chip.core.write_mem(base, bytes([val]) * size)


def _erased(chip, addr, n=16):
    return chip.core.read_mem(addr, n) == b"\xFF" * n


def test_flash_2mb_sector_23_and_bank_erases(d437):
    chip = spin_chip(d437)
    _fill(chip, 0x081E0000, 0x20000)
    _fill(chip, 0x08100000, 0x4000)
    _unlock(chip)
    wr(chip, FLASH_IF + 0x10, (27 << 3) | 2)                       # SER, SNB = 0b11011 = sector 23
    wr(chip, FLASH_IF + 0x10, (27 << 3) | 2 | (1 << 16))
    assert _erased(chip, 0x081E0000) and _erased(chip, 0x081FFFF0)
    assert not _erased(chip, 0x08100000)
    wr(chip, FLASH_IF + 0x10, (16 << 3) | 2 | (1 << 16))           # SNB 16 = sector 12 at 0x08100000
    assert _erased(chip, 0x08100000) and _erased(chip, 0x08103FF0)
    _fill(chip, 0x08100000, 0x100)
    _fill(chip, 0x08080000, 0x100)
    wr(chip, FLASH_IF + 0x10, (1 << 15) | (1 << 16))               # MER1: bank 2 only
    assert _erased(chip, 0x08100000) and not _erased(chip, 0x08080000)
    wr(chip, FLASH_IF + 0x10, (12 << 3) | 2 | (1 << 16))           # 0b01100: not allowed
    assert any("no such sector" in w for w in warns(chip))


def test_flash_1mb_db1m_option_takes_effect_after_reset(d427):
    chip = spin_chip(d427)
    fl = chip.peripherals["FLASH"]
    assert rd(chip, FLASH_IF + 0x14) == 0x0FFFAAED and rd(chip, FLASH_IF + 0x18) == 0x0FFF0000
    _fill(chip, 0x08080000, 0x100)
    _unlock(chip)
    wr(chip, FLASH_IF + 0x10, (8 << 3) | 2 | (1 << 16))            # DB1M = 0: sector 8 = 0x08080000, 128 KB
    assert _erased(chip, 0x08080000)
    wr(chip, FLASH_IF + 0x14, 0x4FFFAAEC)                          # locked: ignored
    assert not fl.db1m
    wr(chip, FLASH_IF + 8, 0x08192A3B)
    wr(chip, FLASH_IF + 8, 0x4C5D6E7F)
    assert not rd(chip, FLASH_IF + 0x14) & 1
    wr(chip, FLASH_IF + 0x14, 0x4FFFAAEC)
    wr(chip, FLASH_IF + 0x14, 0x4FFFAAEE)                          # OPTSTRT
    assert fl.option_bytes["OPTCR"] & (1 << 30)
    chip.reset()
    assert fl.db1m and rd(chip, FLASH_IF + 0x14) == 0x4FFFAAED
    _fill(chip, 0x08080000, 0x8000)
    _unlock(chip)
    wr(chip, FLASH_IF + 0x10, (8 << 3) | 2 | (1 << 16))            # DB1M = 1: sector 8 does not exist
    assert not _erased(chip, 0x08080000)
    wr(chip, FLASH_IF + 0x10, (16 << 3) | 2 | (1 << 16))           # sector 12 = 0x08080000, 16 KB
    assert _erased(chip, 0x08080000) and _erased(chip, 0x08083FF0) and not _erased(chip, 0x08084000)


# --------------------------------------------------------------------------------------------- sample
def _sample(name):
    elf = BUILD / f"{name}.elf"
    if not elf.exists():
        pytest.skip(f"{elf} not built (firmware/samples_stm32_variants/build.sh)")
    return elf


def test_clock180_uart7_sample(d427):
    elf = _sample("f42x_clock180_uart7")
    chip = Chip(d427)
    chip.load_firmware(load_firmware(elf))
    chip.reset()
    ev: list = []
    chip.pins.subscribe(lambda e: ev.append(e) if e.pin == "PB0" and e.field == "level" else None)
    chip.run_for(20e6)
    r = chip.peripherals["RCC"]
    assert (r.core_hz, r.apb1_hz, r.apb2_hz, chip.clock.hz) == (180e6, 45e6, 90e6, 180e6)
    assert chip.peripherals["PWR"].od_active()
    assert chip.peripherals["FLASH"].latency == 5
    assert bytes(chip.peripherals["UART7"].output) == b"hello 180\r\n"
    assert chip.peripherals["UART7"].baud() == pytest.approx(45e6 / round(45e6 / 115200), rel=1e-6)
    assert not [w for w in warns(chip) if "maximum" in w or "PE8" in w or "PE7" in w]
    t = [e.t_ns for e in ev[1:]]
    gaps = [b - a for a, b in zip(t, t[1:])]
    # TIM2 at 90 MHz / 90 = 1 MHz: 1 ms toggles; after 10 ticks TIMPRE makes TIMxCLK = HCLK = 180 MHz: 0.5 ms
    assert gaps[:8] and all(g == pytest.approx(1e6, rel=1e-3) for g in gaps[:8])
    assert all(g == pytest.approx(0.5e6, rel=1e-3) for g in gaps[11:])
    assert len(gaps[11:]) >= 15


def test_clock180_uart7_echo_session():
    elf = _sample("f42x_clock180_uart7")
    s = Session(F427, elf)
    s.schedule_uart(2e6, "UART7", b"abc")
    s.run_for(4e6)
    assert s.uart_output("UART7") == b"hello 180\r\nABC"
    s.close()


# --------------------------------------------------------------------------------------------- I2C FLTR
def test_i2c_fltr_only_with_pe_clear(d427):
    chip = spin_chip(d427)
    i2c1 = 0x40005400
    wr(chip, RCC + 0x40, 1 << 21)
    assert rd(chip, i2c1 + 0x24) == 0
    wr(chip, i2c1 + 0x24, 0xFFFF)
    assert rd(chip, i2c1 + 0x24) == 0x1F                           # ANOFF + DNF[3:0]; 15:5 reserved
    wr(chip, i2c1 + 0x04, 16)                                      # FREQ = 16 MHz
    wr(chip, i2c1 + 0x00, 1)                                       # PE
    wr(chip, i2c1 + 0x24, 0x03)
    assert rd(chip, i2c1 + 0x24) == 0x1F and any("FLTR write ignored" in w for w in warns(chip))
    wr(chip, i2c1 + 0x00, 0)
    wr(chip, i2c1 + 0x24, 0x03)
    assert rd(chip, i2c1 + 0x24) == 0x03


# --------------------------------------------------------------------------------------------- HAL + board
HAL_ELF = ROOT / "firmware" / "samples_stm32_hal_f427" / "build" / "clock_uart7_hal.elf"
BOARD = ROOT / "boards" / "stm32f427vg_f405rg_uart7.yaml"


def test_hal_f427_overdrive_pllsai_uart7():
    """ST's HAL built for STM32F427xx: HAL_PWREx_EnableOverDrive, HAL_RCCEx_PeriphCLKConfig(SAI_PLLSAI), HAL_Delay."""
    if not HAL_ELF.exists():
        pytest.skip("firmware/samples_stm32_hal_f427/build.sh not run")
    from elftools.elf.elffile import ELFFile
    with open(HAL_ELF, "rb") as f:
        st = ELFFile(f).get_section_by_name(".symtab")
        sym = {n: st.get_symbol_by_name(n)[0]["st_value"] for n in ("hal_status", "delay_ticks")}
    s = Session(F427, HAL_ELF)
    s.schedule_uart(12e6, "UART7", b"ok")
    s.run_for(14e6)
    chip = s.chip
    assert chip.bus.read(sym["hal_status"], 4) == 0
    assert chip.bus.read(sym["delay_ticks"], 4) in (5, 6)           # HAL_Delay(5) on a 1 kHz SysTick at 180 MHz
    assert s.uart_output("UART7") == b"HAL F427 sys 180000000 p1 45000000 p2 90000000\r\nOK"
    r = chip.peripherals["RCC"]
    assert chip.clock.hz == 180e6 and chip.peripherals["PWR"].od_active()
    assert r.sai_clk_hz("A") == pytest.approx(344e6 / 7)            # PLLSAI_Q / PLLSAIDIVQ (6.3.25)
    assert not [w for w in warns(chip) if "maximum" in w or "ignored" in w]
    s.close()


def test_board_f427vg_uart7_f405rg():
    if not (BUILD / "f42x_link_responder.elf").exists() or \
            not (BUILD / "link_requester.elf").exists():
        pytest.skip("firmware/samples_stm32_variants/build.sh not run")
    s = Session.from_board(BOARD, quiet=True)
    a, b = s.chips["A"], s.chips["B"]
    s.run_for(21e6)
    assert a.clock.hz == 168e6 and b.clock.hz == 180e6
    ub = b.peripherals[b.find_uart("UART7")]
    assert ub.baud() == pytest.approx(45e6 / 195)
    assert s.uart_output("A.USART2").startswith(b"REQ 1\r\nREQ 2\r\n")
    out_b = s.uart_output("B.UART7")
    assert out_b.startswith(b"ACK 1 1\r\nACK 2 4\r\n") and b"ACK 10 100\r\n" in out_b
    assert b.pins.get("PE8").owner == "UART7_TX" and b.pins.get("PE7").owner == "UART7_RX"
    s.close()


# --------------------------------------------------------------------------------------------- F405 samples on the F42x die
STM32_BUILD = ROOT / "firmware" / "samples_stm32" / "build"


def _boot405(desc, name):
    elf = STM32_BUILD / f"{name}.elf"
    if not elf.exists():
        pytest.skip(f"{elf} not built (firmware/samples_stm32/build.sh)")
    chip = Chip(desc)
    chip.load_firmware(load_firmware(elf))
    chip.reset()
    return chip


@pytest.mark.parametrize("fn", [F427, F437], ids=["f427vg", "f437ai"])
def test_f405_blinky_button_exti_samples_run_unchanged(fn):
    """The register-compatible F405 samples (blinky PB0/PB7, button_mirror PA0 -> PB0, exti_irq PA0 rising edges)
    run on the F42x die: GPIO, EXTI/SYSCFG and NVIC lines 0-81 are the same (RM0090 Table 63)."""
    desc = load_chip(fn)
    chip = _boot405(desc, "blinky")
    ev: list = []
    chip.pins.subscribe(lambda e: ev.append(e) if e.pin == "PB0" and e.field == "level" else None)
    chip.run_for(3e6)
    assert len(ev) > 10 and chip.pins.get("PB0").owner == "GPIO" and not warns(chip)
    chip = _boot405(desc, "button_mirror")
    chip.run_for(0.2e6)
    chip.pins.set_external("PA0", "H")
    chip.run_for(0.1e6)
    assert chip.pins.get("PB0").level() == "H"
    chip = _boot405(desc, "exti_irq")
    chip.run_for(0.1e6)
    for _ in range(3):
        chip.pins.set_external("PA0", "H")
        chip.run_for(0.2e6)
        chip.pins.set_external("PA0", "L")
        chip.run_for(0.2e6)
    assert chip.peripherals["EXTI"].pr_count[0] == 3
