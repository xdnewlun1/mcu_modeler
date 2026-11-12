"""STM32F2 peripherals checked against the official RM0033 Rev 10
(docs/datasheets/st/RM0033_stm32f2_reference_manual.pdf):
one regression test per discrepancy found when the ST PDF replaced the third-party HTML rendition (CRC ch. 3, ADC 10,
DAC 11, TIM 13-16, IWDG 17, WWDG 18, RNG 20, RTC 22, I2C 23, USART 24, SPI/I2S 25), plus a few spot checks of facts the
models took from the HTML copy.  Section numbers are RM0033's."""
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
TIM = {"TIM1": 0x40010000, "TIM2": 0x40000000, "TIM3": 0x40000400, "TIM4": 0x40000800, "TIM5": 0x40000C00,
       "TIM6": 0x40001000, "TIM7": 0x40001400, "TIM8": 0x40010400, "TIM9": 0x40014000, "TIM10": 0x40014400,
       "TIM11": 0x40014800, "TIM12": 0x40001800, "TIM13": 0x40001C00, "TIM14": 0x40002000}
CR2, CCER, CNT, ARR, CCR1, OR = 0x04, 0x20, 0x24, 0x2C, 0x34, 0x50
_D: dict = {}


def chip_of(fn: str = "stm32f207zg") -> Chip:
    if fn not in _D:
        _D[fn] = load_chip(ROOT / "chips" / f"{fn}.yaml")
    code = asm("spin: b spin", FLASH + 0x200)
    img = bytearray(0x200 + len(code))
    img[0:8] = struct.pack("<II", 0x20020000, (FLASH + 0x200) | 1)
    img[0x200:] = code
    chip = Chip(_D[fn])
    chip.load_firmware(FirmwareImage([(FLASH, bytes(img))]))
    chip.reset()
    return chip


@pytest.fixture(scope="module")
def chip():
    return chip_of()


def rd(chip, a):
    return chip.bus.read(a, 4)


def wr(chip, a, v):
    chip.bus.write(a, 4, v)


# ------------------------------------------------------------------------------------------------------------ TIM
@pytest.mark.parametrize("name, ones", [("TIM1", 0xFFFF), ("TIM8", 0xFFFF), ("TIM2", 0xFFFFFFFF), ("TIM3", 0xFFFF),
                                        ("TIM4", 0xFFFF), ("TIM5", 0xFFFFFFFF), ("TIM6", 0xFFFF), ("TIM7", 0xFFFF),
                                        ("TIM9", 0xFFFF), ("TIM12", 0xFFFF), ("TIM10", 0xFFFF), ("TIM14", 0xFFFF)])
def test_tim_arr_resets_to_all_ones(chip, name, ones):
    """13.4.12 (0xFFFF), 14.4.12 (0xFFFF FFFF, 32 bits on TIM2/TIM5 only), 15.4.11, 15.5.9, 16.4.8 (0xFFFF): ARR resets
    to all ones of the counter; STM32F21x.svd says 0."""
    assert rd(chip, TIM[name] + ARR) == ones


@pytest.mark.parametrize("name", ["TIM3", "TIM4"])
def test_tim3_tim4_are_16_bit(chip, name):
    """14.4.10 / 14.4.12 / 14.4.13: CNT[31:16], ARR[31:16], CCRx[31:16] exist on TIM2 and TIM5 only; TIM3/TIM4 read 0
    there (the SVD makes them 32-bit read-write)."""
    b = TIM[name]
    for off in (CNT, ARR, CCR1):
        wr(chip, b + off, 0x12345678)
        assert rd(chip, b + off) == 0x5678
    wr(chip, TIM["TIM2"] + ARR, 0x12345678)
    assert rd(chip, TIM["TIM2"] + ARR) == 0x12345678
    wr(chip, TIM["TIM2"] + ARR, 0xFFFFFFFF)


def test_tim2_or_itr1_rmp_bits(chip):
    """14.4.19: TIM2_OR.ITR1_RMP is bits 11:10 (the SVD copies TIM5_OR's TI4_RMP at 7:6); 14.4.20 TIM5_OR TI4_RMP 7:6,
    15.5.11 TIM11_OR TI1_RMP 1:0."""
    for name, rw in (("TIM2", 0xC00), ("TIM5", 0xC0), ("TIM11", 0x3)):
        wr(chip, TIM[name] + OR, 0xFFFFFFFF)
        assert rd(chip, TIM[name] + OR) == rw, name
        wr(chip, TIM[name] + OR, 0)
    wr(chip, TIM["TIM3"] + OR, 0xFFFFFFFF)                     # 14.4.21: TIM3/TIM4 have no option register
    assert rd(chip, TIM["TIM3"] + OR) == 0


@pytest.mark.parametrize("name", ["TIM1", "TIM8"])
def test_tim1_tim8_ccer_cc4np(chip, name):
    """13.4.9: TIM1/TIM8 CCER bit 15 is CC4NP (rw; CC4NP:CC4P = 11 captures both edges), bit 14 reserved.  The SVD
    marks bit 15 reserved."""
    b = TIM[name]
    wr(chip, b + CCER, 0xFFFF)
    assert rd(chip, b + CCER) == 0xBFFF
    blk = chip.peripherals[name]
    assert blk._edges_of(4, blk.get("CCER")) == 3
    wr(chip, b + CCER, 0)


@pytest.mark.parametrize("name", ["TIM9", "TIM12"])
def test_tim9_tim12_have_cr2_mms(chip, name):
    """15.4.2: TIM9/12_CR2 exists with MMS[6:4] (unlike the STM32F401/F411 TIM9); other bits reserved."""
    wr(chip, TIM[name] + CR2, 0xFFFF)
    assert rd(chip, TIM[name] + CR2) == 0x70
    wr(chip, TIM[name] + CR2, 0)


# ------------------------------------------------------------------------------------- spot checks (already right)
def test_i2c_has_no_fltr(chip):
    """23.6.10 Table 83: the I2C register map ends at TRISE (0x20); there is no I2C_FLTR at 0x24."""
    assert "FLTR" not in chip.peripherals["I2C1"].regs
    wr(chip, 0x40005400 + 0x24, 0x1F)
    assert rd(chip, 0x40005400 + 0x24) == 0


def test_usart_over8_and_onebit_present(chip):
    """24.6.4 CR1.OVER8 (bit 15) and 24.6.6 CR3.ONEBIT (bit 11) exist on the F2 USARTs (and UART4/5)."""
    for base in (0x40011000, 0x40004C00):
        wr(chip, base + 0x0C, 1 << 15)
        wr(chip, base + 0x14, 1 << 11)
        assert rd(chip, base + 0x0C) & (1 << 15) and rd(chip, base + 0x14) & (1 << 11)
        wr(chip, base + 0x0C, 0)
        wr(chip, base + 0x14, 0)


def test_adc_internal_channels():
    """10.10 / 10.11: temperature sensor on ADC1_IN16, VREFINT on IN17, VBAT / 2 on IN18 (bridge divider by 2)."""
    c = chip_of()
    adc = c.peripherals["ADC1"]
    wr(c, 0x40012304, (1 << 23) | (1 << 22))          # CCR.TSVREFE | VBATE
    assert adc.volts(16) == pytest.approx(0.76)
    assert adc.volts(17) == pytest.approx(1.21)
    assert adc.volts(18) == pytest.approx(3.3 / 2)


def test_rtc_v1_register_set(chip):
    """22.6.15 Table 80: TR..WPR, TSTR 0x30, TSDR 0x34, TAFCR 0x40, BKP0R-BKP19R 0x50-0x9C; CR bits 19 and 5 reserved;
    PRER.PREDIV_S 13 bits; TAFCR TAMP1E/TAMP1TRG/TAMPIE (2:0), TAMP1INSEL/TSINSEL/ALARMOUTTYPE (18:16)."""
    rtc = chip.peripherals["RTC"]
    regs = {r.offset: r for r in rtc.regs.values()}
    assert {o for o, r in regs.items() if not r.name.startswith("RESERVED")} == \
        {0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18, 0x1C, 0x20, 0x24, 0x30, 0x34, 0x40} | set(range(0x50, 0xA0, 4))
    assert ~regs[0x08].ro_mask & 0xFFFFFFFF == 0x00F7FFDF
    assert ~regs[0x10].ro_mask & 0xFFFFFFFF == 0x007F1FFF and regs[0x10].reset == 0x007F00FF
    assert ~regs[0x40].ro_mask & 0xFFFFFFFF == 0x00070007


# ------------------------------------------------------------------------------------------------------------ ADC
def _pll120(chip, ppre2):
    rcc, cr = 0x40023800, 0x40023800
    wr(chip, 0x40023C00, 3)                                   # FLASH_ACR 3 WS
    wr(chip, cr, rd(chip, cr) | (1 << 16))                    # HSEON
    wr(chip, rcc + 0x04, 8 | (240 << 6) | (1 << 22) | (5 << 24))
    wr(chip, cr, rd(chip, cr) | (1 << 24))                    # PLLON
    wr(chip, rcc + 0x08, (5 << 10) | (ppre2 << 13) | 2)       # APB1 /4, APB2 ppre2, SW = PLL


def test_adcclk_limit_30mhz():
    """10.2 ("ADCCLK maximum value is 30 MHz") / 10.3.5, DS15818 fADC <= 30 MHz at VDDA 2.4-3.6 V: ADCCLK = PCLK2 / 2
    with PCLK2 = 120 MHz (60 MHz ADCCLK) logs one `warn`; PCLK2 60 MHz / 2 = 30 MHz does not."""
    c = chip_of()
    _pll120(c, 4)                                             # APB2 /2 = 60 MHz
    adc = c.peripherals["ADC1"]
    adc.adcclk_cycles()
    assert not [e for e in c.log if e.kind == "warn" and "ADCCLK" in e.fields.get("msg", "")]
    _pll120(c, 0)                                             # APB2 /1 = 120 MHz (itself out of spec)
    adc.adcclk_cycles()
    assert [e for e in c.log if e.kind == "warn" and "ADCCLK 60 MHz exceeds 30 MHz" in e.fields.get("msg", "")]
