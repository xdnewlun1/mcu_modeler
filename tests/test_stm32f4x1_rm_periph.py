"""STM32F401/F411 shared peripheral models checked against RM0368 (STM32F401xB/C/D/E, Rev 6) and RM0383 (STM32F411xC/E,
Rev 4): reset values and reserved bits where the SVDs copy the STM32F405 tables.  Each test cites the RM section (the
same section number in both manuals)."""
from __future__ import annotations

import pytest

from test_stm32f4x1_system import rd, spin_chip, wr

CHIPS = ("stm32f401re", "stm32f411re")
RCC = 0x40023800
GPIOA, GPIOB = 0x40020000, 0x40020400
ADC1, ADC_C = 0x40012000, 0x40012300
TIM = {"TIM1": 0x40010000, "TIM2": 0x40000000, "TIM3": 0x40000400, "TIM4": 0x40000800, "TIM5": 0x40000C00,
       "TIM9": 0x40014000, "TIM10": 0x40014400, "TIM11": 0x40014800}
I2C = (0x40005400, 0x40005800, 0x40005C00)
RTC, PWR = 0x40002800, 0x40007000


def clocked(fn: str):
    chip = spin_chip(fn)
    for off in (0x30, 0x40, 0x44):                 # AHB1ENR, APB1ENR, APB2ENR: every clock on
        wr(chip, RCC + off, 0xFFFFFFFF)
    return chip


@pytest.mark.parametrize("fn", CHIPS)
def test_gpio_reset_values_8_4(fn):
    """RM 8.4.1/8.4.3/8.4.4: GPIOA MODER 0xA8000000, OSPEEDR 0x0C000000 (PA13 SWDIO very high speed; the SVD says 0),
    PUPDR 0x64000000; GPIOB MODER 0x00000280, OSPEEDR 0x000000C0, PUPDR 0x00000100."""
    chip = clocked(fn)
    assert [rd(chip, GPIOA + o) for o in (0x00, 0x08, 0x0C)] == [0xA8000000, 0x0C000000, 0x64000000]
    assert [rd(chip, GPIOB + o) for o in (0x00, 0x08, 0x0C)] == [0x00000280, 0x000000C0, 0x00000100]
    assert rd(chip, 0x40020808) == 0                 # other ports: 0


@pytest.mark.parametrize("fn", CHIPS)
def test_adc_common_single_adc_11_12_15(fn):
    """RM 11.12.15/11.12.16 (Table 48 F401 / 47 F411): the common block has only ADC_CCR (offset 0x04) with ADCPRE
    (17:16), VBATE (22), TSVREFE (23); CSR (0x00) / CDR (0x08) and MULTI/DELAY/DDS/DMA are reserved."""
    chip = clocked(fn)
    wr(chip, ADC_C + 4, 0xFFFFFFFF)
    assert rd(chip, ADC_C + 4) == 0x00C30000
    wr(chip, ADC1 + 8, 1)                            # ADON
    assert rd(chip, ADC_C + 0) == 0 and rd(chip, ADC_C + 8) == 0


@pytest.mark.parametrize("fn", CHIPS)
def test_adc_temperature_on_in16_and_in18_vbat_div4_11_9_11_10(fn):
    """RM 11.3.3, 11.9 (Figure 38) and 11.10: the temperature sensor feeds ADC1_IN16 and ADC1_IN18; VBAT / 4 is on
    ADC1_IN18 and wins when VBATE and TSVREFE are both set."""
    chip = clocked(fn)
    adc = chip.peripherals["ADC1"]
    adc.vals["TEMP"], adc.vals["VBAT"] = 25.0, 3.2
    assert adc.volts(16) == adc.volts(18) == 0.0     # sensor off
    wr(chip, ADC_C + 4, 1 << 23)                     # TSVREFE
    assert adc.volts(16) == pytest.approx(0.76) and adc.volts(18) == pytest.approx(0.76)
    assert adc.volts(17) == pytest.approx(1.21)
    wr(chip, ADC_C + 4, (1 << 23) | (1 << 22))       # + VBATE: IN18 converts VBAT / 4
    assert adc.volts(18) == pytest.approx(0.8) and adc.volts(16) == pytest.approx(0.76)


@pytest.mark.parametrize("fn", CHIPS)
def test_adc_tim8_triggers_reserved_11_3(fn):
    """RM 11.3 Tables 43/44: EXTSEL 1101/1110 and JEXTSEL 1100-1110 (TIM8 on the F405) are reserved."""
    chip = clocked(fn)
    wr(chip, ADC1 + 8, 1 | (0xD << 24) | (1 << 28))  # ADON, EXTSEL = 1101, EXTEN rising
    assert any("reserved on this part" in e.fields.get("msg", "") for e in chip.log if e.kind == "warn")


@pytest.mark.parametrize("fn", CHIPS)
def test_timer_arr_reset_and_widths_12_13_14(fn):
    """RM 12.4.12, 13.4.10-13.4.16, 14.4.8-14.4.12, 14.5.7-14.5.10: ARR resets to 0xFFFF (TIM2/TIM5: 0xFFFFFFFF);
    only TIM2/TIM5 have 32-bit CNT/ARR/CCRx, the bits above 15 of the others are reserved."""
    chip = clocked(fn)
    for name, base in TIM.items():
        wide = name in ("TIM2", "TIM5")
        assert rd(chip, base + 0x2C) == (0xFFFFFFFF if wide else 0xFFFF), name
        wr(chip, base + 0x2C, 0x12345678)
        wr(chip, base + 0x34, 0x9ABCDEF0)
        assert rd(chip, base + 0x2C) == (0x12345678 if wide else 0x5678), name
        assert rd(chip, base + 0x34) == (0x9ABCDEF0 if wide else 0xDEF0), name


@pytest.mark.parametrize("fn", CHIPS)
def test_tim9_has_no_cr2_14_4(fn):
    """RM 14.4 (TIM9 registers 14.4.1-14.4.12, Table 59/58 register map): offset 0x04 is reserved (no CR2/MMS)."""
    chip = clocked(fn)
    wr(chip, TIM["TIM9"] + 4, 0x70)
    assert rd(chip, TIM["TIM9"] + 4) == 0
    wr(chip, TIM["TIM3"] + 4, 0x70)                  # TIM2-5 do have CR2.MMS (13.4.2)
    assert rd(chip, TIM["TIM3"] + 4) == 0x70


@pytest.mark.parametrize("fn", CHIPS)
def test_timer_option_registers_13_4_19_14_5_11(fn):
    """RM 13.4.19 TIM2_OR.ITR1_RMP[11:10], 13.4.20 TIM5_OR.TI4_RMP[7:6], 14.5.11 TIM11_OR.TI1_RMP[1:0]; other bits
    reserved."""
    chip = clocked(fn)
    for name, mask in (("TIM2", 0xC00), ("TIM5", 0xC0), ("TIM11", 0x3)):
        wr(chip, TIM[name] + 0x50, 0xFFFF)
        assert rd(chip, TIM[name] + 0x50) == mask, name


@pytest.mark.parametrize("fn", CHIPS)
def test_i2c_fltr_present_18_6_10(fn):
    """RM 18.6.10: I2C_FLTR at 0x24 (reset 0): ANOFF (bit 4), DNF[3:0]; written only while PE = 0.  The SVDs omit it."""
    chip = clocked(fn)
    for base in I2C:
        assert rd(chip, base + 0x24) == 0
        wr(chip, base + 0x24, 0xFFFFFFFF)
        assert rd(chip, base + 0x24) == 0x1F
    wr(chip, I2C[0] + 0x24, 0)
    wr(chip, I2C[0], 1)                              # PE = 1
    wr(chip, I2C[0] + 0x24, 0x13)
    assert rd(chip, I2C[0] + 0x24) == 0


def test_spi1_spi4_have_no_i2s_on_f401_2_3():
    """RM0368 2.3 Table 1 (SPI1, SPI4 without I2S; SPI2/I2S2, SPI3/I2S3) and 20.2.2: I2SCFGR/I2SPR of SPI1/SPI4 are
    reserved; SPI2's I2SPR resets to 0x0002 (20.5.9)."""
    chip = clocked("stm32f401re")
    for base in (0x40013000, 0x40013400):
        assert rd(chip, base + 0x20) == 0
        wr(chip, base + 0x1C, 0x0E00)
        wr(chip, base + 0x20, 0x0123)
        assert rd(chip, base + 0x1C) == 0 and rd(chip, base + 0x20) == 0
    assert rd(chip, 0x40003820) == 0x0002


def test_f411_i2s_on_spi1_4_5_2_3():
    """RM0383 2.3 Table 1: SPI1/I2S1, SPI4/I2S4, SPI5/I2S5: the I2S registers exist (I2SPR reset 0x0002, 20.5.9)."""
    chip = clocked("stm32f411re")
    for base in (0x40013000, 0x40013400, 0x40015000):
        assert rd(chip, base + 0x20) == 0x0002
        wr(chip, base + 0x1C, 0x0800)                # I2SMOD
        assert rd(chip, base + 0x1C) == 0x0800


@pytest.mark.parametrize("fn", CHIPS)
def test_rtc_one_tamper_17_6_17(fn):
    """RM 17.6.17: RTC_TAFCR has only TAMP1 (TAMP1E bit 0, TAMP1TRG bit 1, TAMPIE bit 2); bits 6:3 and 31:19 are
    reserved, so TAMP2E/TAMP2TRG (F405 bits 3/4) read 0."""
    chip = clocked(fn)
    wr(chip, PWR, rd(chip, PWR) | (1 << 8))          # DBP: backup domain writable
    wr(chip, RTC + 0x40, 0xFFFFFFFF)
    assert rd(chip, RTC + 0x40) == 0x0007FF87
    assert len([r for r in chip.peripherals["RTC"].regs if r.startswith("BKP")]) == 20     # 17.6.20: BKP0R-BKP19R


@pytest.mark.parametrize("fn", CHIPS)
def test_syscfg_registers_7_2(fn):
    """RM 7.2: MEMRMP.MEM_MODE[1:0], PMC.ADC1DC2 (bit 16) only, EXTICRx 16 bits, CMPCR.CMP_PD with READY."""
    chip = clocked(fn)
    base = 0x40013800
    for off, mask in ((0x00, 0x3), (0x04, 0x10000), (0x08, 0xFFFF), (0x14, 0xFFFF)):
        wr(chip, base + off, 0xFFFFFFFF)
        assert rd(chip, base + off) == mask, hex(off)
        wr(chip, base + off, 0)
    wr(chip, base + 0x20, 1)
    assert rd(chip, base + 0x20) == 0x101
