"""STM32F401/F411 RCC, PWR and FLASH checked against RM0368 Rev 6 / RM0383 Rev 4 (docs/datasheets/st/): one regression
test per discrepancy found when the manuals arrived (chipem/periph/stm32/f4x1.py).  Section numbers are the same in
both manuals unless stated."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.firmware import FirmwareImage

from engine_util import asm

ROOT = Path(__file__).resolve().parent.parent
FLASH, SRAM = 0x08000000, 0x20000000
RCC, FLASH_IF, PWR = 0x40023800, 0x40023C00, 0x40007000
CR, PLLCFGR, CFGR, AHB2RSTR, AHB2ENR, APB1ENR = 0x00, 0x04, 0x08, 0x14, 0x34, 0x40
PLLI2SCFGR, DCKCFGR = 0x84, 0x8C
ACR, OPTKEYR, OPTCR = 0x00, 0x08, 0x14
PLLON, PLLI2SON = 1 << 24, 1 << 26


def make_chip(fn: str, code: str = "spin: b spin", sram_code: str | None = None) -> Chip:
    body = asm(code, FLASH + 0x200)
    img = bytearray(0x200 + len(body))
    img[0:8] = struct.pack("<II", SRAM + 0x8000, (FLASH + 0x200) | 1)
    img[0x200:] = body
    segs = [(FLASH, bytes(img))]
    if sram_code is not None:
        segs.append((SRAM + 0x100, asm(sram_code, SRAM + 0x100)))
    chip = Chip(load_chip(ROOT / "chips" / f"{fn}.yaml"))
    chip.load_firmware(FirmwareImage(segs))
    chip.reset()
    return chip


def rd(chip, a):
    return chip.bus.read(a, 4)


def wr(chip, a, v):
    chip.bus.write(a, 4, v)


def warns(chip) -> list[str]:
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "warn"]


def pwr_on(chip):
    wr(chip, RCC + APB1ENR, rd(chip, RCC + APB1ENR) | (1 << 28))


# ------------------------------------------------------------------------------------------------------------ RCC
@pytest.mark.parametrize("fn", ["stm32f401re", "stm32f411re"])
def test_rcc_ahb2_crypto_bits_reserved(fn):
    """6.3.6 / 6.3.10: AHB2RSTR / AHB2ENR have only OTGFS (bit 7); bits 4-5 (F415 CRYP/HASH) read 0."""
    chip = make_chip(fn)
    for off in (AHB2ENR, AHB2RSTR):
        wr(chip, RCC + off, 0xFF)
        assert rd(chip, RCC + off) == 0x80
        wr(chip, RCC + off, 0)


@pytest.mark.parametrize("fn", ["stm32f401re", "stm32f411re"])
def test_rcc_lpenr_and_plli2scfgr_reset_values(fn):
    """6.3.13 AHB1LPENR 0x0061900F, 6.3.16 APB2LPENR 0x00077930, 6.3.20 PLLI2SCFGR 0x24003000 (bit 26 reserved)."""
    chip = make_chip(fn)
    assert rd(chip, RCC + 0x50) == 0x0061900F and rd(chip, RCC + 0x64) == 0x00077930
    assert rd(chip, RCC + PLLI2SCFGR) == 0x24003000
    wr(chip, RCC + PLLI2SCFGR, 0)
    assert rd(chip, RCC + PLLI2SCFGR) == 1 << 26                 # reserved bit keeps its reset value
    wr(chip, RCC + 0x50, 0xFFFFFFFF)
    assert rd(chip, RCC + 0x50) == 0x0061909F                    # FLITF, SRAM1, DMA1/2, CRC, GPIOA-E/H only


def test_rcc_f411_plli2sm_resets_to_zero():
    """RM0383 6.3.20: PLLI2SM resets to 0 (wrong configuration): no I2S clock until firmware programs it."""
    chip = make_chip("stm32f411re")
    r = chip.peripherals["RCC"]
    wr(chip, RCC + CR, rd(chip, RCC + CR) | PLLI2SON)
    assert r.plli2s_hz() == 0.0 and any("PLLI2SM = 0" in w for w in warns(chip))


@pytest.mark.parametrize("fn,n,bad", [("stm32f401re", 191, True), ("stm32f401re", 192, False),
                                      ("stm32f411re", 100, False), ("stm32f411re", 49, True)])
def test_rcc_pll_n_and_vco_ranges(fn, n, bad):
    """RM0368 6.3.2: 192 <= PLLN <= 432, VCO 192..432 MHz; RM0383 6.3.2: 50..432, VCO 100..432 MHz."""
    chip = make_chip(fn)
    wr(chip, RCC + PLLCFGR, 16 | (n << 6) | (3 << 16) | (4 << 24))      # HSI 16 MHz / 16 = 1 MHz VCO input
    wr(chip, RCC + CR, rd(chip, RCC + CR) | PLLON)
    w = [x for x in warns(chip) if "PLLN" in x or "PLL VCO" in x]
    assert bool(w) == bad, warns(chip)


def test_rcc_pll_vco_input_range():
    """6.3.2 PLLM caution: the VCO input (PLL input / PLLM) must be 1..2 MHz."""
    chip = make_chip("stm32f411re")
    wr(chip, RCC + PLLCFGR, 32 | (200 << 6) | (4 << 24))              # 16 MHz / 32 = 0.5 MHz
    wr(chip, RCC + CR, rd(chip, RCC + CR) | PLLON)
    assert any("VCO input 0.5 MHz" in w for w in warns(chip))


@pytest.mark.parametrize("fn,n,bad", [("stm32f401re", 150, True), ("stm32f411re", 150, False)])
def test_rcc_plli2s_n_range(fn, n, bad):
    """RM0368 6.3.20: 192 <= PLLI2SN <= 432 (VCO 192..432 MHz); RM0383 6.3.20: 50..432 (100..432 MHz)."""
    chip = make_chip(fn)
    wr(chip, RCC + PLLI2SCFGR, (n << 6) | (3 << 28) | (16 if "411" in fn else 0))
    wr(chip, RCC + CR, rd(chip, RCC + CR) | PLLI2SON)
    assert chip.peripherals["RCC"].plli2s_hz() == pytest.approx(16e6 / 16 * n / 3)
    assert bool([w for w in warns(chip) if "PLLI2SN" in w]) == bad


def test_rcc_pll_fields_locked_while_pll_runs():
    """6.3.2: PLLM/N/P/SRC "can be written only when PLL is disabled"; PLLQ "should" (written, with a warn)."""
    chip = make_chip("stm32f401re")
    rst = rd(chip, RCC + PLLCFGR)
    wr(chip, RCC + CR, rd(chip, RCC + CR) | PLLON)
    wr(chip, RCC + PLLCFGR, (rst & ~0x7FC0) | (300 << 6))
    assert rd(chip, RCC + PLLCFGR) == rst and any("keep their value" in w for w in warns(chip))
    wr(chip, RCC + PLLCFGR, (rst & ~(0xF << 24)) | (7 << 24))
    assert (rd(chip, RCC + PLLCFGR) >> 24) & 0xF == 7 and any("PLLQ" in w for w in warns(chip))
    wr(chip, RCC + CR, rd(chip, RCC + CR) & ~PLLON)
    wr(chip, RCC + PLLCFGR, (rst & ~0x7FC0) | (300 << 6))
    assert (rd(chip, RCC + PLLCFGR) >> 6) & 0x1FF == 300


@pytest.mark.parametrize("fn,m_locked", [("stm32f401re", True), ("stm32f411re", False)])
def test_rcc_plli2s_locks(fn, m_locked):
    """6.3.2: PLLSRC (and on the F401 PLLM, the shared PLLI2S input divider) only while PLL and PLLI2S are off;
    6.3.20: PLLI2SN (F411 PLLI2SM) only while the PLLI2S is off."""
    chip = make_chip(fn)
    wr(chip, RCC + CR, rd(chip, RCC + CR) | PLLI2SON)
    rst = rd(chip, RCC + PLLCFGR)
    wr(chip, RCC + PLLCFGR, (rst & ~0x3F) | 8 | (1 << 22))
    got = rd(chip, RCC + PLLCFGR)
    assert not got & (1 << 22)                                         # PLLSRC kept
    assert (got & 0x3F) == (16 if m_locked else 8)
    i2s = rd(chip, RCC + PLLI2SCFGR)
    wr(chip, RCC + PLLI2SCFGR, (i2s & ~0x7FC0) | (250 << 6))
    assert rd(chip, RCC + PLLI2SCFGR) == i2s


def test_rcc_timpre_ppre_div2():
    """6.3.21 TIMPRE = 1: PPREx /1 or /2 -> TIMxCLK = HCLK, otherwise 4 x PCLKx."""
    chip = make_chip("stm32f411re")
    r = chip.peripherals["RCC"]
    wr(chip, RCC + CFGR, (4 << 10) | (6 << 13))                       # APB1 /2, APB2 /8 at 16 MHz
    wr(chip, RCC + DCKCFGR, 1 << 24)
    assert (r.timer_hz(1), r.timer_hz(2)) == (16e6, 8e6)


@pytest.mark.parametrize("fn,vdd,hz_ws", [("stm32f401re", 2.5, ((24e6, 0), (48e6, 1), (72e6, 2), (84e6, 3))),
                                          ("stm32f401re", 1.8, ((84e6, 5), (80e6, 4))),
                                          ("stm32f411re", 2.2, ((90e6, 4), (100e6, 5))),
                                          ("stm32f411re", 1.8, ((100e6, 6), (96e6, 5))),
                                          ("stm32f411re", 3.3, ((100e6, 3), (90e6, 2), (64e6, 1)))])
def test_flash_wait_states_per_voltage_range(fn, vdd, hz_ws):
    """RM0368 Table 6 / RM0383 Table 5: wait states per HCLK and supply range (PWR model `vdd`)."""
    chip = make_chip(fn)
    chip.peripherals["PWR"].set_vdd(vdd)
    r = chip.peripherals["RCC"]
    for hz, ws in hz_ws:
        assert r.min_wait_states(hz) == ws


def test_flash_latency_is_four_bits():
    """3.8.1: FLASH_ACR.LATENCY[3:0] (RM0090 F405: [2:0])."""
    chip = make_chip("stm32f401re")
    wr(chip, FLASH_IF + ACR, 0xF)
    assert rd(chip, FLASH_IF + ACR) == 0xF


# ------------------------------------------------------------------------------------------------------------ PWR
@pytest.mark.parametrize("fn", ["stm32f401re", "stm32f411re"])
def test_pwr_cr_reset_scale2_and_vosrdy(fn):
    """5.4.1 PWR_CR reset 0x00008000 (F411 too: "10: Scale 2 mode (reset value)"); 5.4.2 CSR reset 0, VOSRDY set once
    the main PLL runs (5.1.3: VOS applies only with the PLL on)."""
    chip = make_chip(fn)
    pwr_on(chip)
    assert rd(chip, PWR) == 0x8000 and rd(chip, PWR + 4) == 0
    wr(chip, RCC + CR, rd(chip, RCC + CR) | PLLON)
    assert rd(chip, PWR + 4) == 1 << 14
    assert chip.peripherals["PWR"].vos_scale() == 2


@pytest.mark.parametrize("fn", ["stm32f401re", "stm32f411re"])
def test_pwr_pvd_levels(fn):
    """5.4.1 PLS: 000 = 2.2 V ... 111 = 2.9 V (RM0090 F405: 2.0 V first)."""
    chip = make_chip(fn)
    pwr_on(chip)
    p = chip.peripherals["PWR"]
    assert [p.pvd_threshold() for _ in [0]] == [2.2]
    wr(chip, PWR, rd(chip, PWR) | (1 << 4))                       # PVDE, PLS = 000
    p.set_vdd(2.15)
    assert rd(chip, PWR + 4) & (1 << 2)                            # PVDO: VDD below 2.2 V
    p.set_vdd(2.25)
    assert not rd(chip, PWR + 4) & (1 << 2)
    wr(chip, PWR, rd(chip, PWR) | (2 << 5))
    assert p.pvd_threshold() == 2.4


def test_pwr_adcdc1_needs_prefetch_off():
    """5.4.1 ADCDC1: "can only be set when operating at supply voltage range 2.7 to 3.6V and when the Prefetch is
    OFF"."""
    chip = make_chip("stm32f401re")
    pwr_on(chip)
    wr(chip, FLASH_IF + ACR, 1 << 8)                               # PRFTEN
    wr(chip, PWR, rd(chip, PWR) | (1 << 13))
    assert not rd(chip, PWR) & (1 << 13) and any("ADCDC1" in w for w in warns(chip))
    wr(chip, FLASH_IF + ACR, 0)
    wr(chip, PWR, rd(chip, PWR) | (1 << 13))
    assert rd(chip, PWR) & (1 << 13)


_SET_FISSR = """
    ldr r0, =0x40007000
    ldr r1, [r0]
    ldr r2, =0x00300000
    orrs r1, r2
    str r1, [r0]
spin: b spin
"""


@pytest.mark.parametrize("from_sram", [False, True])
def test_pwr_f411_fissr_fmssr_not_settable_from_flash(from_sram):
    """RM0383 5.4.1 FISSR / FMSSR: "This bit could not be set while executing with the Flash itself. It should be done
    with specific routine executed from RAM"."""
    if from_sram:
        chip = make_chip("stm32f411re", "ldr r0, =0x20000101\nbx r0", sram_code=_SET_FISSR)
    else:
        chip = make_chip("stm32f411re", _SET_FISSR)
    pwr_on(chip)
    chip.run_for(20e3)
    got = rd(chip, PWR) & (3 << 20)
    if from_sram:
        assert got == 3 << 20
    else:
        assert got == 0 and any("FISSR/FMSSR write ignored" in w for w in warns(chip))


# ------------------------------------------------------------------------------------------------------------ FLASH
def test_flash_optcr_reset_lock_and_programming():
    """3.8.6 OPTCR reset 0x0FFFAAED, OPTLOCK set; 3.8.3 OPTKEYR sequence; OPTSTRT programs the option bytes, which the
    next reset loads ("loaded with values from flash memory at reset release")."""
    chip = make_chip("stm32f411re")
    assert rd(chip, FLASH_IF + OPTCR) == 0x0FFFAAED
    wr(chip, FLASH_IF + OPTCR, 0x0FFFAAEC & ~(1 << 5))
    assert rd(chip, FLASH_IF + OPTCR) == 0x0FFFAAED                    # locked: ignored
    wr(chip, FLASH_IF + OPTKEYR, 0x08192A3B)
    wr(chip, FLASH_IF + OPTKEYR, 0x4C5D6E7F)
    assert rd(chip, FLASH_IF + OPTCR) == 0x0FFFAAEC
    wr(chip, FLASH_IF + OPTCR, 0xF0FEAACC)            # SPRMOD, nWRP0 = 0, WDG_SW = 0; bits 30:24 reserved
    assert rd(chip, FLASH_IF + OPTCR) == 0x8FFEAACC
    wr(chip, FLASH_IF + OPTCR, rd(chip, FLASH_IF + OPTCR) | 2)          # OPTSTRT
    assert not rd(chip, FLASH_IF + OPTCR) & 2
    chip.reset()
    assert rd(chip, FLASH_IF + OPTCR) == 0x8FFEAACD                     # programmed values, OPTLOCK set again
    wr(chip, FLASH_IF + OPTKEYR, 0x12345678)                           # wrong key: locked until reset
    wr(chip, FLASH_IF + OPTKEYR, 0x08192A3B)
    wr(chip, FLASH_IF + OPTKEYR, 0x4C5D6E7F)
    assert rd(chip, FLASH_IF + OPTCR) & 1


def test_flash_optcr_f401xbc_nwrp76_reserved():
    """RM0368 3.8.6: nWRP[7:6] "For STM32F401xB/C: Reserved" (sectors 6-7 exist on the 384/512 KB parts only)."""
    for fn, want in (("stm32f401cc", 0x0FC0AAEC), ("stm32f401re", 0x0F00AAEC)):
        chip = make_chip(fn)
        wr(chip, FLASH_IF + OPTKEYR, 0x08192A3B)
        wr(chip, FLASH_IF + OPTKEYR, 0x4C5D6E7F)
        wr(chip, FLASH_IF + OPTCR, 0x0000AAEC)
        assert rd(chip, FLASH_IF + OPTCR) == want
