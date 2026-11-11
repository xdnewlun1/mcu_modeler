"""STM32F401/F411 system models (chipem/periph/stm32/f4x1.py): RCC (frequency limits per voltage scale, DCKCFGR TIMPRE,
F411 PLLI2SM, bits the SVDs omit, LPENR / AHB1ENR reset values, flash wait-state check), PWR (VOS reset / PLL-on lock /
effective scale, F411 FISSR/FMSSR, LPLVDS/MRLVDS in Stop), FLASH (sector layout per size, SNB beyond the part, RDERR),
DMA request maps "f401" / "f411", DBGMCU IDCODE, I2S on SPI1/4/5 of the F411.  Checked against RM0368 Rev 6 / RM0383
Rev 4 (docs/datasheets/st/); the corrections have their own regression tests in test_stm32f4x1_rm_*.py."""
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
RCC, FLASH_IF, PWR, DBG = 0x40023800, 0x40023C00, 0x40007000, 0xE0042000
CR, PLLCFGR, CFGR, AHB1ENR, APB1ENR, APB2ENR = 0x00, 0x04, 0x08, 0x30, 0x40, 0x44
APB2RSTR, AHB1LPENR, AHB2LPENR, APB1LPENR, APB2LPENR, BDCR = 0x24, 0x50, 0x54, 0x60, 0x64, 0x70
PLLI2SCFGR, DCKCFGR = 0x84, 0x8C
PLLON, PLLRDY, PLLI2SON = 1 << 24, 1 << 25, 1 << 26


def desc(fn):
    return load_chip(ROOT / "chips" / f"{fn}.yaml")


def spin_chip(fn: str, **kw) -> Chip:
    code = asm("spin: b spin", FLASH + 0x200)
    img = bytearray(0x200 + len(code))
    img[0:8] = struct.pack("<II", 0x20010000, (FLASH + 0x200) | 1)
    img[0x200:] = code
    chip = Chip(desc(fn), **kw)
    chip.load_firmware(FirmwareImage([(FLASH, bytes(img))]))
    chip.reset()
    return chip


def rd(chip, a):
    return chip.bus.read(a, 4)


def wr(chip, a, v):
    chip.bus.write(a, 4, v)


def warns(chip) -> list[str]:
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "warn"]


def infos(chip) -> list[str]:
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "info"]


def pll(chip, m, n, p_bits, vos=None, latency=None):
    """HSE 8 MHz -> PLL (m, n, P code), optional VOS while the PLL is off, then switch SYSCLK to the PLL."""
    wr(chip, RCC + APB1ENR, rd(chip, RCC + APB1ENR) | (1 << 28))
    if vos is not None:
        wr(chip, PWR, (rd(chip, PWR) & ~(3 << 14)) | (vos << 14))
    wr(chip, RCC + CR, rd(chip, RCC + CR) | (1 << 16))
    wr(chip, RCC + PLLCFGR, m | (n << 6) | (p_bits << 16) | (1 << 22) | (4 << 24))
    wr(chip, RCC + CR, rd(chip, RCC + CR) | PLLON)
    if latency is not None:
        wr(chip, FLASH_IF, latency)
    wr(chip, RCC + CFGR, (4 << 10) | 2)                 # APB1 /2, SW = PLL


# --------------------------------------------------------------------------------------------- models / IDs
@pytest.mark.parametrize("fn,idcode", [("stm32f401re", 0x10006433), ("stm32f401cc", 0x10016423),
                                       ("stm32f411re", 0x10006431), ("stm32f411ce", 0x10006431)])
def test_models_and_idcode(fn, idcode):
    chip = spin_chip(fn)
    p = chip.peripherals
    assert (p["RCC"].model, p["PWR"].model, p["FLASH"].model) == ("stm32.rcc_f4x1", "stm32.pwr_f4x1",
                                                                   "stm32.flash_f4x1")
    assert rd(chip, DBG) == idcode
    assert not warns(chip)


# --------------------------------------------------------------------------------------------- RCC
@pytest.mark.parametrize("fn", ["stm32f401re", "stm32f411re"])
def test_rcc_reset_values(fn):
    """RM0368 / RM0383 6.3.9-6.3.16, 6.3.20 reset values (the SVDs copy F405 values; the RM wins)."""
    chip = spin_chip(fn)
    assert rd(chip, RCC + AHB1ENR) == 0                       # no CCMDATARAMEN (SVD: F405 value 0x00100000)
    assert rd(chip, RCC + AHB1LPENR) == 0x0061900F and rd(chip, RCC + AHB2LPENR) == 0x80
    assert rd(chip, RCC + APB1LPENR) == 0x10E2C80F and rd(chip, RCC + APB2LPENR) == 0x00077930
    assert rd(chip, RCC + PLLCFGR) == 0x24003010 and rd(chip, RCC + DCKCFGR) == 0
    assert rd(chip, RCC + PLLI2SCFGR) == 0x24003000           # both lines; F411 PLLI2SM resets to 0
    assert rd(chip, RCC + CR) & 0x3 == 0x3                    # HSION | HSIRDY


def test_rcc_bits_the_svds_omit():
    f401, f411 = spin_chip("stm32f401re"), spin_chip("stm32f411re")
    wr(f401, RCC + APB2ENR, (1 << 13) | (1 << 20))
    assert rd(f401, RCC + APB2ENR) == 1 << 13                  # SPI4EN; no SPI5 on the F401
    wr(f411, RCC + APB2ENR, (1 << 13) | (1 << 20))
    assert rd(f411, RCC + APB2ENR) == (1 << 13) | (1 << 20)    # SPI4EN, SPI5EN
    assert f411.peripherals["RCC"].clock_enabled("SPI5")
    wr(f411, RCC + APB2RSTR, (1 << 13) | (1 << 20))
    assert rd(f411, RCC + APB2RSTR) == (1 << 13) | (1 << 20)
    wr(f411, RCC + APB2RSTR, 0)
    wr(f411, RCC + PLLI2SCFGR, 0x3F | (200 << 6) | (3 << 28))
    assert rd(f411, RCC + PLLI2SCFGR) == 0x3F | (200 << 6) | (3 << 28) | (1 << 26)   # bit 26: reserved, reads 1
    wr(f401, RCC + PLLI2SCFGR, 0x3F | (200 << 6) | (3 << 28))
    assert rd(f401, RCC + PLLI2SCFGR) == (200 << 6) | (3 << 28) | (1 << 26)   # no PLLI2SM on the F401
    wr(f411, RCC + APB1ENR, 1 << 28)
    wr(f411, PWR, rd(f411, PWR) | (1 << 8))                   # DBP
    wr(f411, RCC + BDCR, 1 << 3)
    assert rd(f411, RCC + BDCR) == 1 << 3                     # LSEMOD


def test_rcc_plli2s_uses_plli2sm_on_f411_only():
    for fn, want in (("stm32f411re", 16e6 / 32 * 256 / 5), ("stm32f401re", 16e6 / 16 * 256 / 5)):
        chip = spin_chip(fn)
        r = chip.peripherals["RCC"]
        wr(chip, RCC + PLLI2SCFGR, 32 | (256 << 6) | (5 << 28))
        assert r.plli2s_hz() == 0.0                           # PLLI2S off
        wr(chip, RCC + CR, rd(chip, RCC + CR) | PLLI2SON)
        assert rd(chip, RCC + CR) & (1 << 27)                 # PLLI2SRDY
        assert r.i2s_clk_hz() == pytest.approx(want)          # F401: PLLM (reset 16) is the PLLI2S input divider


def test_rcc_timpre():
    for fn in ("stm32f401re", "stm32f411re"):
        chip = spin_chip(fn)
        r = chip.peripherals["RCC"]
        wr(chip, RCC + CFGR, (5 << 10) | (4 << 13))           # APB1 /4, APB2 /2 at HSI 16 MHz
        assert (r.timer_hz(1), r.timer_hz(2)) == (8e6, 16e6)
        wr(chip, RCC + DCKCFGR, 1 << 24)
        assert (r.timer_hz(1), r.timer_hz(2)) == (16e6, 16e6)
        wr(chip, RCC + CFGR, (6 << 10) | (7 << 13))           # /8, /16 -> 4 x PCLK
        assert (r.timer_hz(1), r.timer_hz(2)) == (8e6, 4e6)
        assert r.effective_timer_hz(2) == 4e6
        wr(chip, RCC + DCKCFGR, 0xFFFFFFFF)
        assert rd(chip, RCC + DCKCFGR) == 1 << 24             # only TIMPRE is implemented


@pytest.mark.parametrize("fn,m,n,p,vos,ws,hz,limit_warn", [
    ("stm32f401re", 8, 336, 1, 2, 2, 84e6, False),            # 84 MHz scale 2: in spec
    ("stm32f401re", 8, 200, 0, 2, 3, 100e6, True),            # 100 MHz on an F401: above 84 MHz
    ("stm32f411re", 4, 100, 0, 3, 3, 100e6, False),           # 100 MHz scale 1
    ("stm32f411re", 4, 100, 0, 2, 3, 100e6, True),            # 100 MHz in scale 2: above 84 MHz
])
def test_rcc_frequency_limits_per_scale(fn, m, n, p, vos, ws, hz, limit_warn):
    chip = spin_chip(fn)
    pll(chip, m, n, p, vos=vos, latency=ws)
    assert chip.clock.hz == hz
    w = [x for x in warns(chip) if "exceeds" in x]
    assert bool(w) == limit_warn, warns(chip)
    if w:
        assert "voltage scale" in w[0]


def test_rcc_apb_limits():
    chip = spin_chip("stm32f401re")
    pll(chip, 8, 336, 1, vos=2, latency=2)
    wr(chip, RCC + CFGR, 2)                                   # APB1 /1 = 84 MHz > 42 MHz
    assert any("APB clock above maximum" in w and "42" in w for w in warns(chip))
    chip = spin_chip("stm32f411re")
    pll(chip, 4, 100, 0, vos=3, latency=3)                    # APB1 /2 = 50 MHz: in spec on the F411
    assert not [w for w in warns(chip) if "APB" in w]


def test_rcc_wait_state_check():
    chip = spin_chip("stm32f411re")
    pll(chip, 4, 100, 0, vos=3, latency=1)                    # 100 MHz needs 3 WS
    assert any("LATENCY" in w and ">= 3" in w for w in warns(chip))
    chip = spin_chip("stm32f401re")
    pll(chip, 8, 336, 1, vos=2, latency=2)
    assert not [w for w in warns(chip) if "LATENCY" in w]


# --------------------------------------------------------------------------------------------- PWR
def test_pwr_reset_values_and_writable_bits():
    f401, f411 = spin_chip("stm32f401re"), spin_chip("stm32f411re")
    for c in (f401, f411):
        wr(c, RCC + APB1ENR, 1 << 28)
    assert rd(f401, PWR) == 0x8000 and rd(f411, PWR) == 0x8000      # RM0368 / RM0383 5.4.1: VOS = 10 (scale 2)
    assert rd(f401, PWR + 4) == 0 and rd(f411, PWR + 4) == 0        # 5.4.2 reset 0 (VOSRDY 0 with the PLL off)
    bits = (1 << 13) | (1 << 11) | (1 << 10) | (1 << 9)       # ADCDC1, MRLVDS, LPLVDS, FPDS
    wr(f401, PWR, rd(f401, PWR) | bits | (3 << 20))
    assert rd(f401, PWR) == 0x8000 | bits                     # no FISSR/FMSSR on the F401
    wr(f411, PWR, rd(f411, PWR) | bits | (1 << 21))
    assert rd(f411, PWR) == 0x8000 | bits | (1 << 21)


def test_pwr_vos_locked_while_pll_on_and_effective_scale():
    chip = spin_chip("stm32f411re")
    wr(chip, RCC + APB1ENR, 1 << 28)
    pwr = chip.peripherals["PWR"]
    assert pwr.vos_scale() == 3                               # PLL off: scale 3 whatever VOS says
    wr(chip, PWR, 0x8000)                                     # scale 2, PLL off: accepted
    assert rd(chip, PWR) == 0x8000
    wr(chip, RCC + CR, rd(chip, RCC + CR) | PLLON)
    assert pwr.vos_scale() == 2
    wr(chip, PWR, 0xC000)
    assert rd(chip, PWR) == 0x8000 and any("VOS" in w for w in warns(chip))
    chip = spin_chip("stm32f401re")
    wr(chip, RCC + APB1ENR, 1 << 28)
    wr(chip, PWR, 0xC000)                                     # F401: 11 = reserved, scale 2 (RM0368 5.4.1)
    wr(chip, RCC + CR, rd(chip, RCC + CR) | PLLON)
    assert chip.peripherals["PWR"].vos_scale() == 2


def test_pwr_f411_flash_stop_bits():
    """RM0383 5.4.1: FMSSR forces the flash to Stop (FPDS = 0) or deep power-down (FPDS = 1); no FISSR dependency."""
    chip = spin_chip("stm32f411re")
    wr(chip, RCC + APB1ENR, 1 << 28)
    wr(chip, PWR, rd(chip, PWR) | (1 << 20))                  # FMSSR (test bus write: no CPU fetch from flash)
    assert rd(chip, PWR) & (1 << 20) and not warns(chip)
    assert any("forced to Stop" in m for m in infos(chip))
    wr(chip, PWR, rd(chip, PWR) & ~(1 << 20))
    wr(chip, PWR, rd(chip, PWR) | (1 << 9) | (1 << 20) | (1 << 21))
    assert any("deep power-down" in m for m in infos(chip))


def test_pwr_stop_low_voltage_regulator_info():
    code = asm("""
        ldr r0, =0xE000ED10
        movs r1, #4
        str r1, [r0]
        wfi
    spin: b spin
    """, FLASH + 0x200)
    img = bytearray(0x200 + len(code))
    img[0:8] = struct.pack("<II", 0x20010000, (FLASH + 0x200) | 1)
    img[0x200:] = code
    chip = Chip(desc("stm32f401re"))
    chip.load_firmware(FirmwareImage([(FLASH, bytes(img))]))
    chip.reset()
    wr(chip, RCC + APB1ENR, 1 << 28)
    wr(chip, PWR, rd(chip, PWR) | 1 | (1 << 10))              # LPDS + LPLVDS
    chip.run_for(50e3)
    assert chip.peripherals["PWR"].mode == "stop"
    assert any("low-voltage" in m for m in infos(chip))


# --------------------------------------------------------------------------------------------- FLASH
def _erase(chip, snb):
    wr(chip, FLASH_IF + 4, 0x45670123)
    wr(chip, FLASH_IF + 4, 0xCDEF89AB)
    wr(chip, FLASH_IF + 0x10, 2 | (snb << 3) | (1 << 24) | (1 << 16))


@pytest.mark.parametrize("fn,last", [("stm32f401cc", 5), ("stm32f401re", 7), ("stm32f411ce", 7)])
def test_flash_sectors_by_size(fn, last):
    chip = spin_chip(fn)
    fl = chip.peripherals["FLASH"]
    assert len(fl.sectors()) == last + 1
    base = FLASH + fl.sectors()[last][0]
    chip.bus.write(base, 4, 0x12345678)
    _erase(chip, last)
    assert chip.bus.read(base, 4) == 0xFFFFFFFF and rd(chip, FLASH_IF + 0xC) & 1        # EOP
    wr(chip, FLASH_IF + 0xC, 1)
    _erase(chip, last + 1)
    assert any("no such sector" in w for w in warns(chip))


def test_flash_rderr_w1c():
    chip = spin_chip("stm32f411re")
    fl = chip.peripherals["FLASH"]
    fl.set("SR", 0x100)
    assert rd(chip, FLASH_IF + 0xC) == 0x100
    wr(chip, FLASH_IF + 0xC, 0x100)
    assert rd(chip, FLASH_IF + 0xC) == 0


# --------------------------------------------------------------------------------------------- DMA maps
def test_dma_request_maps():
    from chipem.periph.stm32.dma import REQUEST_TABLES
    f401, f411, f40x = REQUEST_TABLES["f401"], REQUEST_TABLES["f411"], REQUEST_TABLES["f40x"]
    names = lambda t: {n for c in (1, 2) for row in t[c] for cell in row for n in cell}   # noqa: E731
    n401, n411 = names(f401), names(f411)
    assert not {n for n in n401 if n.split("_")[0] in ("USART3", "UART4", "UART5", "TIM6", "TIM7", "TIM8", "DAC1",
                                                        "DAC2", "ADC2", "ADC3", "SPI5", "SPI6", "SAI1", "DCMI")}
    assert {"SPI5_RX", "SPI5_TX"} <= n411 and n401 | {"SPI5_RX", "SPI5_TX"} == n411
    # same positions as RM0090 (RM0368 Tables 28/29, RM0383 Tables 27/28): USART2_TX DMA1 stream 6 channel 4,
    # USART1_RX DMA2 stream 2/5 channel 4, SPI4_TX DMA2 stream 1 channel 4, SPI5_RX DMA2 stream 3 channel 2 (F411)
    assert f401[1][6][4] == f40x[1][6][4] == ("USART2_TX",)
    assert f401[2][2][4] == ("USART1_RX",) and f401[2][1][4] == ("SPI4_TX",)
    assert f411[2][3][2] == ("SPI5_RX",) and f401[2][3][2] == ()
    assert f401[1][1][4] == () and f40x[1][1][4] == ("USART3_RX",)


def test_dma_usart2_tx_runs_on_f401():
    chip = spin_chip("stm32f401re")
    wr(chip, RCC + AHB1ENR, (1 << 21) | 1)
    wr(chip, RCC + APB1ENR, 1 << 17)
    src = 0x20000100
    chip.bus.write(src, 4, 0x64636261)
    u = 0x40004400
    wr(chip, u + 0x08, 16000000 // 1000000)                   # 1 Mbaud
    wr(chip, u + 0x14, 1 << 7)                                # CR3.DMAT
    wr(chip, u + 0x0C, (1 << 13) | (1 << 3))
    s6 = 0x40026000 + 0x10 + 0x18 * 6
    wr(chip, s6 + 0x08, u + 0x04)                             # PAR = DR
    wr(chip, s6 + 0x0C, src)
    wr(chip, s6 + 0x04, 4)
    wr(chip, s6, (4 << 25) | (1 << 10) | (1 << 6) | 1)        # CHSEL 4, MINC, M2P, EN
    chip.run_for(100e3)
    assert chip.uart_out("USART2") == b"abcd"


# --------------------------------------------------------------------------------------------- I2S on SPI1/4/5 (F411)
def test_f411_i2s_on_spi1_spi4_spi5():
    chip = spin_chip("stm32f411re")
    for n in ("SPI1", "SPI4", "SPI5"):
        assert chip.peripherals[n].i2s is not None and chip.peripherals[n].apb == 2
    chip = spin_chip("stm32f401re")
    assert chip.peripherals["SPI1"].i2s is None and chip.peripherals["SPI4"].i2s is None
    assert chip.peripherals["SPI2"].i2s is not None
