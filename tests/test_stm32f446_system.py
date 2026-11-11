"""STM32F446 system blocks (chipem/periph/stm32/f446.py): RCC PLL_R as SYSCLK, PLLSAI/PLLI2S with their own M and P
dividers, DCKCFGR/DCKCFGR2 kernel clock muxes, reset values, F42x-only bits read-only; PWR; the 512 KB single-bank
flash; the "f446" DMA request maps; DBGMCU; SAI2 naming.  Expectations follow RM0390 Rev 9 (the per-discrepancy
regressions are in test_stm32f446_rm0390.py)."""
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
RCC, FLASH_IF, PWR = 0x40023800, 0x40023C00, 0x40007000
CR, PLLCFGR, CFGR, AHB1ENR, AHB3ENR, APB1ENR, APB2ENR = 0x00, 0x04, 0x08, 0x30, 0x38, 0x40, 0x44
PLLI2SCFGR, PLLSAICFGR, DCKCFGR, DCKCFGR2 = 0x84, 0x88, 0x8C, 0x94


@pytest.fixture(scope="module")
def desc():
    return load_chip(ROOT / "chips" / "stm32f446re.yaml")


def spin_chip(desc) -> Chip:
    code = asm("spin: b spin", FLASH + 0x200)
    img = bytearray(0x200 + len(code))
    img[0:8] = struct.pack("<II", 0x20020000, (FLASH + 0x200) | 1)
    img[0x200:] = code
    chip = Chip(desc)
    chip.load_firmware(FirmwareImage([(FLASH, bytes(img))]))
    chip.reset()
    return chip


def rd(chip, a):
    return chip.bus.read(a, 4)


def wr(chip, a, v):
    chip.bus.write(a, 4, v)


def warns(chip) -> list[str]:
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "warn"]


def test_rcc_reset_values(desc):
    c = spin_chip(desc)
    assert rd(c, RCC + PLLCFGR) == 0x24003010            # HAL_RCC_DeInit (STM32F446xx): PLLR = 2
    assert rd(c, RCC + PLLI2SCFGR) == 0x24003010         # the SVD says 0x20003000
    assert rd(c, RCC + PLLSAICFGR) == 0x04003010         # the SVD says 0x24003000
    assert rd(c, RCC + 0x64) & ((1 << 22) | (1 << 23) | (1 << 13)) == (1 << 22) | (1 << 23) | (1 << 13)  # APB2LPENR
    assert rd(c, RCC + 0x60) & (1 << 16 | 1 << 24 | 1 << 27) == (1 << 16 | 1 << 24 | 1 << 27)            # APB1LPENR


def test_f42x_only_bits_read_only_f446_bits_writable(desc):
    c = spin_chip(desc)
    wr(c, RCC + AHB1ENR, (1 << 9) | (1 << 10) | (1 << 23) | 1)      # GPIOJ/K, DMA2D: F42x only
    assert rd(c, RCC + AHB1ENR) & 0x00800601 == 1
    wr(c, RCC + APB1ENR, (1 << 30) | (1 << 16) | (1 << 24) | (1 << 27))
    assert rd(c, RCC + APB1ENR) == (1 << 16) | (1 << 24) | (1 << 27)   # no UART7; SPDIFRX, FMPI2C1, CEC
    wr(c, RCC + APB2ENR, (1 << 20) | (1 << 22) | (1 << 23) | (1 << 13))
    assert rd(c, RCC + APB2ENR) == (1 << 22) | (1 << 23) | (1 << 13)   # no SPI5; SAI1, SAI2, SPI4
    wr(c, RCC + AHB3ENR, 3)
    assert rd(c, RCC + AHB3ENR) == 3                                     # FMC, QSPI


def test_sysclk_pll_r_and_protection(desc):
    c = spin_chip(desc)
    r = c.peripherals["RCC"]
    wr(c, RCC + PLLCFGR, 8 | (180 << 6) | (1 << 16) | (7 << 24) | (3 << 28))   # HSI/8 x 180: VCO 360, P /4, R /3
    wr(c, RCC + CR, rd(c, RCC + CR) | (1 << 24))
    assert r.pll_hz() == 90e6 and r.pll_r_hz() == 120e6
    wr(c, RCC + CFGR, 3)                                                 # SW = 11: PLL_R
    assert (rd(c, RCC + CFGR) >> 2) & 3 == 3 and r.source == "PLL_R"
    assert c.clock.hz == 120e6
    wr(c, RCC + CR, rd(c, RCC + CR) & ~(1 << 24))                        # PLL cannot be stopped while it is SYSCLK
    assert rd(c, RCC + CR) & (1 << 25)
    assert any("PLL_R" in m for m in (e.fields.get("msg", "") for e in c.log if e.kind == "info"))


def test_pllsai_pllis_kernel_clocks(desc):
    c = spin_chip(desc)
    r = c.peripherals["RCC"]
    # PLLSAI: HSI / 8 x 96 = 192 MHz VCO, P /4 = 48 MHz, Q /4 = 48 MHz
    wr(c, RCC + PLLSAICFGR, 8 | (96 << 6) | (1 << 16) | (4 << 24))
    # PLLI2S: HSI / 16 x 192 = 192 MHz VCO, P /2, Q /4, R /2
    wr(c, RCC + PLLI2SCFGR, 16 | (192 << 6) | (0 << 16) | (4 << 24) | (2 << 28))
    wr(c, RCC + CR, rd(c, RCC + CR) | (1 << 28) | (1 << 26))
    assert rd(c, RCC + CR) & (1 << 29) and rd(c, RCC + CR) & (1 << 27)
    assert r.pllsai_p_hz() == 48e6 and r.pllsai_q_hz() == 48e6 and r.ltdc_clk_hz() == 0
    assert r.plli2s_p_hz() == 96e6 and r.plli2s_q_hz() == 48e6 and r.plli2s_hz() == 96e6
    assert r.clk48_hz() == 0.0                                           # CK48MSEL = 0: PLL_Q, PLL off
    wr(c, RCC + DCKCFGR2, 1 << 27)
    assert r.clk48_hz() == 48e6 and r.sdio_kernel_hz() == 48e6
    wr(c, RCC + DCKCFGR2, (1 << 27) | (1 << 28))
    assert r.sdio_kernel_hz() == r.sysclk_hz == 16e6
    assert c.peripherals["SDIO"].sdioclk_hz() == 16e6
    # SAI1 from PLLSAI_Q / (PLLSAIDIVQ + 1), SAI2 from PLLI2S_Q / (PLLI2SDIVQ + 1)
    wr(c, RCC + DCKCFGR, (1 << 8) | 3 | (1 << 22))
    assert r.sai_kernel_hz("SAI1") == 24e6 and r.sai_kernel_hz("SAI2") == 12e6
    assert c.peripherals["SAI2"].sai_ck_hz("A") == 12e6
    # I2S: APB1 (I2S2/I2S3) I2S1SRC = 00 PLLI2S_R, APB2 (SPI1/SPI4) I2S2SRC = 11 PLL source (HSI)
    wr(c, RCC + DCKCFGR, 3 << 27)
    assert r.i2s_kernel_hz("SPI2") == 96e6 and r.i2s_kernel_hz("SPI1") == 16e6
    assert r.fmpi2c_clk_hz() == r.apb1_hz and r.cec_clk_hz() == pytest.approx(16e6 / 488)


def test_sai2_signals_and_dma_names(desc):
    c = spin_chip(desc)
    s2 = c.peripherals["SAI2"]
    assert s2.subs["A"].dma_name == "SAI2_A" and s2.subs["B"].sig["SCK"] == "SAI2_SCK_B"
    assert s2.gate == "SAI2" and s2._irqn == 91


def test_dma_f446_table(desc):
    c = spin_chip(desc)
    d1, d2 = c.peripherals["DMA1"], c.peripherals["DMA2"]
    assert d2.table[7][3] == ("QUADSPI",) and d2.table[7][4] == ("USART1_TX",)     # stream 7 ch 3 / ch 4
    assert d1.table[2][2] == ("FMPI2C1_RX",) and d1.table[5][2] == ("FMPI2C1_TX",)
    assert d1.table[5][7] == ("DAC1",) and d1.table[1][3] == ("TIM2_UP", "TIM2_CH3")
    assert "I2S3_EXT_RX" not in d1.known and "SPI5_RX" not in d2.known and "CRYP_IN" not in d2.known
    assert {"SAI2_A", "SAI2_B", "SPDIFRX_DT"} <= d1.known | d2.known


def test_flash_layout_512k(desc):
    c = spin_chip(desc)
    fl = c.peripherals["FLASH"]
    assert sorted(fl._layout(0x80000)) == list(range(8))
    assert fl._layout(0x80000)[7] == (0x60000, 0x20000)
    wr(c, FLASH_IF + 0x04, 0x45670123)
    wr(c, FLASH_IF + 0x04, 0xCDEF89AB)
    c.core.write_mem(FLASH + 0x60000, b"\x00" * 4)
    wr(c, FLASH_IF + 0x10, 2 | (7 << 3) | (1 << 16))                    # SER sector 7
    assert c.core.read_mem(FLASH + 0x60000, 4) == b"\xff" * 4
    wr(c, FLASH_IF + 0x10, 2 | (8 << 3) | (1 << 16))                    # sector 8 does not exist
    assert any("SNB = 8" in m for m in warns(c))
    wr(c, FLASH_IF + 0x10, (1 << 15))                                    # MER1 (F42x bank 2): reserved
    assert not rd(c, FLASH_IF + 0x10) & (1 << 15)
    assert rd(c, FLASH_IF + 0x14) & 0x0FFFAAEC == 0x0FFFAAEC and not c.peripherals["FLASH"].db1m


def test_pwr_overdrive_and_f446_bits(desc):
    c = spin_chip(desc)
    wr(c, RCC + APB1ENR, 1 << 28)
    wr(c, PWR + 0x00, rd(c, PWR) | (1 << 16) | (1 << 20) | (1 << 21))
    assert rd(c, PWR) & (3 << 20) == 3 << 20                            # FMSSR / FISSR stored
    c.run_for(30_000)
    assert rd(c, PWR + 4) & (1 << 16)                                    # ODRDY
    wr(c, PWR + 4, 1 << 7)
    assert rd(c, PWR + 4) & (1 << 7)                                     # EWUP2


def test_dbgmcu_and_storage_models(desc):
    c = spin_chip(desc)
    assert rd(c, 0xE0042000) == 0x10006421
    assert rd(c, 0x40006018) == 1                                        # FMPI2C1_ISR reset: TXE
    wr(c, 0x40006018, 0)
    assert rd(c, 0x40006018) == 1                                        # read-only
    wr(c, 0x40006000, 1)
    assert rd(c, 0x40006000) == 1
    assert not any("FMPI2C1" in m and "storage" in m for m in warns(c))  # real model (RM0390 ch. 23)
    wr(c, 0x40004000, 1)                                                 # SPDIFRX_CR
    assert rd(c, 0x40004000) == 1 and any("SPDIF" in m for m in warns(c))
    assert any("disabled" in m or "clock" in m for m in warns(c))       # APB1ENR gates were off
