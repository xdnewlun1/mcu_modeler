"""stm32f1.adc (ADC1/ADC2, RM0008 ch. 11 layout; model not verified against RM0008): power-up (tSTAB) and the second
ADON write start, calibration timing (CAL / RSTCAL), SWSTART / EXTTRIG / EXTSEL, conversion time per sample time and
ADCPRE, alignment, scan / continuous / discontinuous sequences, DMA through DMA1 channel 1, injected group with offsets,
triggered injection, dual modes (regular / injected simultaneous, fast interleaved, alternate trigger), analog watchdog,
the shared ADC1_2 interrupt, temperature sensor / VREFINT, timer triggers, and the adc_dma.elf sample end to end.

Chip default clock: HSI 8 MHz, APB2 /1, ADCPRE /2 -> ADCCLK = 4 MHz = 2 core cycles; tSTAB 1 us = 8 cycles."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.firmware import FirmwareImage, load_firmware

from engine_util import asm

ROOT = Path(__file__).resolve().parent.parent
YAML = ROOT / "chips" / "stm32f103t6.yaml"
ELF = ROOT / "firmware" / "samples_stm32f1" / "build" / "adc_dma.elf"
FLASH = 0x08000000
RCC, DMA1, TIM3 = 0x40021000, 0x40020000, 0x40000400
ADC1, ADC2 = 0x40012400, 0x40012800
CFGR, AHBENR, APB2ENR, APB1ENR = 0x04, 0x14, 0x18, 0x1C
SR, CR1, CR2, SMPR1, SMPR2 = 0x00, 0x04, 0x08, 0x0C, 0x10
JOFR1, JOFR2, HTR, LTR, SQR1, SQR2, SQR3, JSQR = 0x14, 0x18, 0x24, 0x28, 0x2C, 0x30, 0x34, 0x38
JDR1, JDR2, DR = 0x3C, 0x40, 0x4C
AWD, EOC, JEOC, JSTRT, STRT = 1, 2, 4, 8, 16
EOCIE, AWDIE, JEOCIE, SCAN, AWDSGL, JAUTO, DISCEN = 1 << 5, 1 << 6, 1 << 7, 1 << 8, 1 << 9, 1 << 10, 1 << 11
JAWDEN, AWDEN = 1 << 22, 1 << 23
ADON, CONT, CAL, RSTCAL, DMA, ALIGN = 1, 2, 4, 8, 1 << 8, 1 << 11
JEXTTRIG, EXTTRIG, JSWSTART, SWSTART, TSVREFE = 1 << 15, 1 << 20, 1 << 21, 1 << 22, 1 << 23
SW = EXTTRIG | 7 << 17                      # regular trigger = SWSTART
JSW = JEXTTRIG | 7 << 12                    # injected trigger = JSWSTART
ADC_IRQ = 18
K = 2                                       # core cycles per ADCCLK
LAT, JLAT = 2 * K, 3 * K
SMP = (1.5, 7.5, 13.5, 28.5, 41.5, 55.5, 71.5, 239.5)
CONV0 = int((1.5 + 12.5) * K)               # 28 cycles at the reset sample time


def code(v):
    return max(0, min(int(v / 3.3 * 4096), 4095))


@pytest.fixture(scope="module")
def desc():
    return load_chip(YAML)


def rd(chip, addr, n=4):
    return chip.bus.read(addr, n)


def wr(chip, addr, v, n=4):
    chip.bus.write(addr, n, v)


def warns(chip) -> list[str]:
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "warn"]


def irq(chip) -> bool:
    return (16 + ADC_IRQ) in chip.arch.asserted


def spin_chip(desc) -> Chip:
    c = asm("spin: b spin", FLASH + 0x200)
    img = bytearray(0x200 + len(c))
    img[0:8] = struct.pack("<II", 0x20002800, (FLASH + 0x200) | 1)
    img[0x200:] = c
    chip = Chip(desc)
    chip.load_firmware(FirmwareImage([(FLASH, bytes(img))]))
    chip.reset()
    wr(chip, RCC + AHBENR, rd(chip, RCC + AHBENR) | 1)                     # DMA1
    wr(chip, RCC + APB2ENR, 1 << 9 | 1 << 10 | 1 << 2 | 1 << 3)            # ADC1, ADC2, GPIOA, GPIOB
    wr(chip, RCC + APB1ENR, 1 << 1)                                         # TIM3
    return chip


def power(chip, base=ADC1, cr2=0):
    """ADON (first write: power up) and wait past tSTAB."""
    wr(chip, base + CR2, ADON | cr2)
    chip.run_cycles(20)


def probe(chip, times, addr):
    out = []
    for t in times:
        chip.sched.at_cycles(t, lambda: out.append(rd(chip, addr)))
    return out


# ------------------------------------------------------------------------------------------------ power / start
def test_first_adon_powers_up_second_adon_starts(desc):
    chip = spin_chip(desc)
    adc = chip.peripherals["ADC1"]
    adc.set_channel("PA0", 1.65)
    wr(chip, ADC1 + CR1, EOCIE)
    wr(chip, ADC1 + CR2, ADON)
    chip.run_cycles(100)
    assert rd(chip, ADC1 + SR) == 0 and adc.conversions == 0           # first ADON: power-up only
    t0 = chip.now_cycles()
    wr(chip, ADC1 + CR2, ADON)                                         # second write, nothing else changed
    assert rd(chip, ADC1 + SR) == STRT
    end = t0 + LAT + CONV0
    sr = probe(chip, [end - 1, end + 1], ADC1 + SR)
    chip.run_cycles(LAT + CONV0 + 10)
    assert sr == [STRT, STRT | EOC] and irq(chip)
    assert rd(chip, ADC1 + DR) == 2048
    assert rd(chip, ADC1 + SR) == STRT and not irq(chip)              # reading DR clears EOC
    wr(chip, ADC1 + CR2, ADON | ALIGN)                                 # another bit changes too: no conversion
    chip.run_cycles(100)
    assert adc.conversions == 1
    wr(chip, ADC1 + CR2, ADON | ALIGN | EXTTRIG)
    chip.run_cycles(100)
    assert adc.conversions == 1
    wr(chip, ADC1 + CR2, ADON | ALIGN | EXTTRIG)                       # RM0008 11.12.3: the ADON rewrite starts a
    chip.run_cycles(LAT + CONV0 + 10)                                  # conversion whatever EXTTRIG is
    assert adc.conversions == 2


def test_second_adon_before_tstab_waits_for_power_up(desc):
    chip = spin_chip(desc)
    adc = chip.peripherals["ADC1"]
    t0 = chip.now_cycles()
    wr(chip, ADC1 + CR2, ADON)
    wr(chip, ADC1 + CR2, ADON)                                         # at once: starts when ready (tSTAB = 8)
    end = t0 + 8 + LAT + CONV0
    sr = probe(chip, [end - 1, end + 1], ADC1 + SR)
    chip.run_cycles(80)
    assert [s & EOC for s in sr] == [0, EOC] and adc.conversions == 1


def test_swstart_needs_exttrig_and_extsel_111(desc):
    chip = spin_chip(desc)
    adc = chip.peripherals["ADC1"]
    power(chip)
    wr(chip, ADC1 + CR2, ADON | SWSTART)                               # EXTTRIG = 0
    chip.run_cycles(100)
    assert adc.conversions == 0 and any("SWSTART needs EXTTRIG" in w for w in warns(chip))
    wr(chip, ADC1 + CR2, ADON | EXTTRIG | 4 << 17)                     # EXTSEL = TIM3_TRGO
    wr(chip, ADC1 + CR2, ADON | EXTTRIG | 4 << 17 | SWSTART)
    chip.run_cycles(100)
    assert adc.conversions == 0
    wr(chip, ADC1 + CR2, ADON | SW)
    t0 = chip.now_cycles()
    wr(chip, ADC1 + CR2, ADON | SW | SWSTART)
    assert rd(chip, ADC1 + CR2) & SWSTART == 0 and rd(chip, ADC1 + SR) & STRT
    sr = probe(chip, [t0 + LAT + CONV0 - 1, t0 + LAT + CONV0 + 1], ADC1 + SR)
    chip.run_cycles(100)
    assert [s & EOC for s in sr] == [0, EOC] and adc.conversions == 1


@pytest.mark.parametrize("smp", range(8))
def test_conversion_time_per_sample_time(desc, smp):
    chip = spin_chip(desc)
    chip.peripherals["ADC1"].set_channel(3, 1.0)
    wr(chip, ADC1 + SMPR2, smp << 9)                                   # SMP3
    wr(chip, ADC1 + SQR3, 3)
    power(chip, cr2=SW)
    total = int((SMP[smp] + 12.5 + 2) * K)
    t0 = chip.now_cycles()
    wr(chip, ADC1 + CR2, ADON | SW | SWSTART)
    sr = probe(chip, [t0 + total - 1, t0 + total + 1], ADC1 + SR)
    chip.run_cycles(total + 5)
    assert [s & EOC for s in sr] == [0, EOC]
    assert rd(chip, ADC1 + DR) == code(1.0)


@pytest.mark.parametrize("adcpre", range(4))
def test_adcpre_and_left_alignment(desc, adcpre):
    chip = spin_chip(desc)
    chip.peripherals["ADC1"].set_channel("IN0", 1.65)
    wr(chip, RCC + CFGR, rd(chip, RCC + CFGR) | adcpre << 14)          # ADCCLK = PCLK2 / (2, 4, 6, 8)
    k = 2 * (adcpre + 1)
    assert chip.peripherals["ADC1"].adcclk() == k
    power(chip, cr2=SW | ALIGN)
    t0 = chip.now_cycles()
    wr(chip, ADC1 + CR2, ADON | SW | ALIGN | SWSTART)
    total = (2 + 14) * k
    sr = probe(chip, [t0 + total - 1, t0 + total + 1], ADC1 + SR)
    chip.run_cycles(total + 5)
    assert [s & EOC for s in sr] == [0, EOC] and rd(chip, ADC1 + DR) == 2048 << 4


# ------------------------------------------------------------------------------------------------ calibration
def test_calibration_and_reset_calibration_timing(desc):
    chip = spin_chip(desc)
    adc = chip.peripherals["ADC1"]
    power(chip)
    t0 = chip.now_cycles()
    wr(chip, ADC1 + CR2, ADON | RSTCAL)
    cr = probe(chip, [t0 + 2 * K - 1, t0 + 2 * K + 1], ADC1 + CR2)
    chip.run_cycles(10)
    assert [c & RSTCAL for c in cr] == [RSTCAL, 0]
    t1 = chip.now_cycles()
    wr(chip, ADC1 + CR2, ADON | CAL)
    wr(chip, ADC1 + CR2, ADON)                                         # software cannot clear CAL
    assert rd(chip, ADC1 + CR2) & CAL
    wr(chip, ADC1 + CR2, ADON | CAL)                                   # ADON rewrite (nothing changes): the
    cr = probe(chip, [t1 + 83 * K - 1, t1 + 83 * K + 1], ADC1 + CR2)   # start is ignored during calibration
    chip.run_cycles(83 * K + 10)
    assert [c & CAL for c in cr] == [CAL, 0]
    assert adc.calibrations == 1 and adc.conversions == 0 and rd(chip, ADC1 + DR) == 0
    assert any("during calibration" in w for w in warns(chip))
    assert not any("tSTAB" in w for w in warns(chip))


def test_calibration_needs_adon_and_tstab(desc):
    chip = spin_chip(desc)
    adc = chip.peripherals["ADC1"]
    wr(chip, ADC1 + CR2, CAL)                                          # ADON = 0: nothing runs
    chip.run_cycles(400)
    assert rd(chip, ADC1 + CR2) & CAL and adc.calibrations == 0
    assert any("ADON = 0" in w for w in warns(chip))
    wr(chip, ADC1 + CR2, 0)
    chip2 = spin_chip(desc)
    t0 = chip2.now_cycles()
    wr(chip2, ADC1 + CR2, ADON)
    wr(chip2, ADC1 + CR2, ADON | CAL)                                  # right after power-up: RM0008 11.4 asks for
    end = t0 + 2 * K + 83 * K                                          # ADON = 1 for 2 ADCCLK first (not tSTAB)
    cr = probe(chip2, [end - 1, end + 1], ADC1 + CR2)
    chip2.run_cycles(2 * K + 83 * K + 10)
    assert [c & CAL for c in cr] == [CAL, 0]
    assert any("2 ADCCLK" in w for w in warns(chip2))


def test_adon_off_aborts_conversion_and_calibration(desc):
    chip = spin_chip(desc)
    adc = chip.peripherals["ADC1"]
    power(chip, cr2=SW)
    wr(chip, ADC1 + CR2, ADON | SW | CAL)
    wr(chip, ADC1 + CR2, SW)
    assert rd(chip, ADC1 + CR2) & CAL == 0
    chip.run_cycles(300)
    assert adc.calibrations == 0


# ------------------------------------------------------------------------------------------------ sequences / DMA
def test_scan_eoc_at_end_of_sequence_and_internal_channels(desc):
    chip = spin_chip(desc)
    adc = chip.peripherals["ADC1"]
    adc.set_channel("ADC12_IN0", 1.0)
    adc.set_channel("IN1", 1234)
    wr(chip, ADC1 + SQR1, 3 << 20)
    wr(chip, ADC1 + SQR3, 0 | 1 << 5 | 16 << 10 | 17 << 15)
    wr(chip, ADC1 + CR1, SCAN)
    power(chip, cr2=SW | TSVREFE)
    t0 = chip.now_cycles()
    wr(chip, ADC1 + CR2, ADON | SW | TSVREFE | SWSTART)
    ends = [t0 + LAT + CONV0 * (i + 1) for i in range(4)]
    sr = probe(chip, [e + 1 for e in ends], ADC1 + SR)
    dr = [None] * 4
    for i, e in enumerate(ends):
        chip.sched.at_cycles(e + 1, lambda i=i: dr.__setitem__(i, adc.get("DR")))
    chip.run_cycles(200)
    assert dr == [code(1.0), 1234, code(1.43), code(1.20)]
    assert [s & EOC for s in sr] == [0, 0, 0, EOC]


def test_temperature_sensor_and_vrefint(desc):
    chip = spin_chip(desc)
    a1, a2 = chip.peripherals["ADC1"], chip.peripherals["ADC2"]
    assert a1.volts(16) == 0 and a1.volts(17) == 0                     # TSVREFE = 0
    wr(chip, ADC1 + CR2, TSVREFE)
    assert a1.volts(16) == pytest.approx(1.43) and a1.volts(17) == pytest.approx(1.20)
    a1.set_channel("TEMP", 85.0)
    assert a1.volts(16) == pytest.approx(1.43 - 0.0043 * 60)
    assert a1.code12(16) == code(1.43 - 0.258)
    a1.set_channel("TEMP", -40.0)
    assert a1.volts(16) == pytest.approx(1.43 + 0.0043 * 65)
    wr(chip, ADC2 + CR2, TSVREFE)
    assert a2.volts(16) == 0 and a2.volts(17) == 0                     # ADC1 only


def test_channel_naming(desc):
    chip = spin_chip(desc)
    a1, a2 = chip.peripherals["ADC1"], chip.peripherals["ADC2"]
    assert a1.set_channel("PA3", 2.0) and a2.volts(3) == pytest.approx(2.0)     # the pin feeds both ADCs
    assert a1.set_channel("ADC12_IN4", 1.0) and a1.volts(4) == pytest.approx(1.0)
    assert a1.set_channel("ADC2_IN5", 1.0) is False
    assert a1.set_channel(12, 0.5) and a1.volts(12) == pytest.approx(0.5)      # not bonded on the VFQFPN36
    with pytest.raises(ValueError):
        a1.set_channel("VBAT", 3.0)
    with pytest.raises(ValueError):
        a1.set_channel("PA9", 1.0)
    chip.pins.set_external("PB1", "H")                                 # no value: the pin's digital level
    assert a1.volts(9) == pytest.approx(3.3)


def test_continuous_scan_dma_circular(desc):
    """3-channel scan, CONT, DMA1 channel 1 circular 16-bit: every conversion moves one item; TC per sequence."""
    chip = spin_chip(desc)
    adc = chip.peripherals["ADC1"]
    adc.set_channel("PA0", 0.5)
    adc.set_channel("PA1", 2.5)
    adc.set_channel("PB0", 3.3)
    buf = 0x20000400
    wr(chip, DMA1 + 0x0C, 3)                                           # CNDTR1
    wr(chip, DMA1 + 0x10, ADC1 + DR)
    wr(chip, DMA1 + 0x14, buf)
    wr(chip, DMA1 + 0x08, 1 << 7 | 1 << 8 | 1 << 10 | 1 << 5 | 2 | 1)  # MINC, 16/16, CIRC, TCIE, EN
    wr(chip, ADC1 + SQR1, 2 << 20)
    wr(chip, ADC1 + SQR3, 0 | 1 << 5 | 8 << 10)
    wr(chip, ADC1 + CR1, SCAN)
    power(chip, cr2=SW | CONT | DMA)
    t0 = chip.now_cycles()
    wr(chip, ADC1 + CR2, ADON | SW | CONT | DMA | SWSTART)
    seq = 3 * CONV0
    nd = probe(chip, [t0 + LAT + CONV0 + 2, t0 + LAT + 2 * CONV0 + 2, t0 + LAT + seq + 2], DMA1 + 0x0C)
    chip.run_cycles(LAT + seq + 5)
    assert nd == [2, 1, 3]
    assert [rd(chip, buf + 2 * i, 2) for i in range(3)] == [code(0.5), code(2.5), 4095]
    assert rd(chip, DMA1) & 2 and (16 + 11) in chip.arch.asserted
    assert rd(chip, ADC1 + SR) & EOC == 0                              # the DMA read DR
    chip.run_cycles(10 * seq)
    assert adc.conversions == 33


def test_discontinuous_mode(desc):
    chip = spin_chip(desc)
    adc = chip.peripherals["ADC1"]
    wr(chip, ADC1 + SQR1, 4 << 20)                                     # 5 channels
    wr(chip, ADC1 + SQR3, 0 | 1 << 5 | 2 << 10 | 3 << 15 | 4 << 20)
    wr(chip, ADC1 + CR1, DISCEN | 1 << 13)                             # DISCNUM = 1: 2 per trigger
    power(chip, cr2=SW)
    counts, eoc = [], []
    for _ in range(4):
        wr(chip, ADC1 + CR2, ADON | SW | SWSTART)
        chip.run_cycles(LAT + 2 * CONV0 + 10)
        counts.append(adc.conversions)
        eoc.append(rd(chip, ADC1 + SR) & EOC)
        wr(chip, ADC1 + SR, 0)
    assert counts == [2, 4, 5, 7]                                      # 0,1 | 2,3 | 4 | 0,1
    assert eoc == [EOC] * 4                                            # RM0008 11.3.10: EOC at each conversion


def test_sqr_write_restarts_regular_group(desc):
    chip = spin_chip(desc)
    adc = chip.peripherals["ADC1"]
    adc.set_channel(2, 3.0)
    wr(chip, ADC1 + SMPR2, 7)                                          # SMP0 = 239.5
    power(chip, cr2=SW)
    wr(chip, ADC1 + CR2, ADON | SW | SWSTART)
    chip.run_cycles(50)
    t0 = chip.now_cycles()
    wr(chip, ADC1 + SQR3, 2)
    chip.run_cycles(LAT + CONV0 + 2)
    assert adc.conversions == 1 and rd(chip, ADC1 + DR) == code(3.0)
    assert chip.now_cycles() >= t0 + LAT + CONV0


# ------------------------------------------------------------------------------------------------ injected
def test_injected_sequence_offsets_and_alignment(desc):
    chip = spin_chip(desc)
    adc = chip.peripherals["ADC1"]
    adc.set_channel("IN1", 2000)
    adc.set_channel("IN0", 50)
    wr(chip, ADC1 + JSQR, 1 << 20 | 1 << 10 | 0 << 15)                 # JL = 1: JSQ3 = IN1, JSQ4 = IN0
    wr(chip, ADC1 + JOFR1, 100)
    wr(chip, ADC1 + JOFR2, 60)
    wr(chip, ADC1 + CR1, SCAN | JEOCIE)
    power(chip, cr2=JSW)
    t0 = chip.now_cycles()
    wr(chip, ADC1 + CR2, ADON | JSW | JSWSTART)
    assert rd(chip, ADC1 + CR2) & JSWSTART == 0 and rd(chip, ADC1 + SR) == JSTRT
    end = t0 + JLAT + 2 * CONV0
    sr = probe(chip, [end - 1, end + 1], ADC1 + SR)
    chip.run_cycles(JLAT + 2 * CONV0 + 5)
    assert [s & (JEOC | EOC) for s in sr] == [0, JEOC | EOC] and irq(chip)
    assert rd(chip, ADC1 + JDR1) == 1900 and rd(chip, ADC1 + JDR2) == 0xFFF6       # 50 - 60, sign-extended
    wr(chip, ADC1 + SR, ~JEOC & 0xFF)                                  # rc_w0
    assert rd(chip, ADC1 + SR) & JEOC == 0 and irq(chip) is False
    wr(chip, ADC1 + CR2, ADON | JSW | ALIGN)
    wr(chip, ADC1 + CR2, ADON | JSW | ALIGN | JSWSTART)
    chip.run_cycles(JLAT + 2 * CONV0 + 5)
    assert rd(chip, ADC1 + JDR1) == 1900 << 3 and rd(chip, ADC1 + JDR2) == (-10 << 3) & 0xFFFF


def test_triggered_injection_interrupts_regular(desc):
    chip = spin_chip(desc)
    adc = chip.peripherals["ADC1"]
    wr(chip, ADC1 + SMPR2, 7)                                          # SMP0 239.5: (239.5 + 12.5) * 2 = 504
    wr(chip, ADC1 + JSQR, 1 << 15)                                     # JSQ4 = IN1
    power(chip, cr2=SW | JSW)
    t0 = chip.now_cycles()
    wr(chip, ADC1 + CR2, ADON | SW | JSW | SWSTART)
    chip.run_cycles(100)
    t1 = chip.now_cycles()
    wr(chip, ADC1 + CR2, ADON | SW | JSW | JSWSTART)
    jend = t1 + JLAT + CONV0
    s = probe(chip, [jend + 1], ADC1 + SR)
    chip.run_cycles(JLAT + CONV0 + 5)
    assert s[0] & JEOC and adc.conversions == 0 and adc.injected_conversions == 1
    chip.run_cycles(504 + 10)                                          # regular channel restarted after injection
    assert adc.conversions == 1 and chip.now_cycles() >= jend + 504
    assert t0 < t1


def test_jauto_after_regular(desc):
    chip = spin_chip(desc)
    adc = chip.peripherals["ADC1"]
    wr(chip, ADC1 + JSQR, 1 << 15)
    wr(chip, ADC1 + CR1, JAUTO)
    power(chip, cr2=SW)
    wr(chip, ADC1 + CR2, ADON | SW | SWSTART)
    chip.run_cycles(LAT + 2 * CONV0 + 5)
    assert adc.conversions == 1 and adc.injected_conversions == 1
    assert rd(chip, ADC1 + SR) & (EOC | JEOC) == EOC | JEOC


# ------------------------------------------------------------------------------------------------ dual mode
def _dual(chip, mode, cr2=0, cr2_2=None):
    wr(chip, ADC1 + CR1, mode << 16)
    power(chip, ADC1, cr2)
    power(chip, ADC2, cr2 if cr2_2 is None else cr2_2)


def test_dual_regular_simultaneous_dr_upper_half(desc):
    chip = spin_chip(desc)
    a1, a2 = chip.peripherals["ADC1"], chip.peripherals["ADC2"]
    a1.set_channel("PA0", 1.0)
    a1.set_channel("PA1", 2.0)
    wr(chip, ADC2 + SQR3, 1)
    _dual(chip, 6, SW)
    t0 = chip.now_cycles()
    wr(chip, ADC1 + CR2, ADON | SW | SWSTART)
    sr = probe(chip, [t0 + LAT + CONV0 + 1], ADC2 + SR)
    chip.run_cycles(LAT + CONV0 + 5)
    assert a1.conversions == a2.conversions == 1 and sr[0] & EOC
    assert rd(chip, ADC1 + DR) == code(1.0)                            # RM0008 11.9 note: ADC2DATA needs ADC1 DMA = 1
    wr(chip, ADC1 + CR2, ADON | SW | DMA)
    assert rd(chip, ADC1 + DR) == code(2.0) << 16 | code(1.0)
    wr(chip, ADC2 + CR2, ADON | SW | SWSTART)                          # slave's own trigger: ignored
    chip.run_cycles(100)
    assert a2.conversions == 1 and any("started by ADC1" in w for w in warns(chip))


def test_dual_regular_simultaneous_dma_32bit(desc):
    chip = spin_chip(desc)
    a1 = chip.peripherals["ADC1"]
    a1.set_channel("PA0", 1.0)
    a1.set_channel("PA1", 2.0)
    buf = 0x20000600
    wr(chip, DMA1 + 0x0C, 2)
    wr(chip, DMA1 + 0x10, ADC1 + DR)
    wr(chip, DMA1 + 0x14, buf)
    wr(chip, DMA1 + 0x08, 1 << 7 | 2 << 8 | 2 << 10 | 1)               # MINC, 32/32, EN
    wr(chip, ADC2 + SQR3, 1)
    _dual(chip, 6, SW | CONT | DMA, SW | CONT)
    wr(chip, ADC1 + CR2, ADON | SW | CONT | DMA | SWSTART)
    chip.run_cycles(LAT + 2 * CONV0 + 5)
    word = code(2.0) << 16 | code(1.0)
    assert [rd(chip, buf), rd(chip, buf + 4)] == [word, word]


def test_dual_injected_simultaneous_and_alternate(desc):
    chip = spin_chip(desc)
    a1, a2 = chip.peripherals["ADC1"], chip.peripherals["ADC2"]
    a1.set_channel("PA2", 0.8)
    wr(chip, ADC1 + JSQR, 0)                                           # JSQ4 = IN0
    wr(chip, ADC2 + JSQR, 2 << 15)                                     # JSQ4 = IN2
    _dual(chip, 5, JSW)
    wr(chip, ADC1 + CR2, ADON | JSW | JSWSTART)
    chip.run_cycles(JLAT + CONV0 + 5)
    assert a1.injected_conversions == a2.injected_conversions == 1
    assert rd(chip, ADC2 + JDR1) == code(0.8)
    wr(chip, ADC1 + CR1, 9 << 16)                                      # alternate trigger
    got = []
    for _ in range(3):
        wr(chip, ADC1 + CR2, ADON | JSW | JSWSTART)
        chip.run_cycles(JLAT + CONV0 + 5)
        got.append((a1.injected_conversions, a2.injected_conversions))
    assert got == [(2, 1), (2, 2), (3, 2)]


def test_dual_fast_interleaved(desc):
    chip = spin_chip(desc)
    a1, a2 = chip.peripherals["ADC1"], chip.peripherals["ADC2"]
    _dual(chip, 7, SW)
    t0 = chip.now_cycles()
    wr(chip, ADC1 + CR2, ADON | SW | SWSTART)
    e2, e1 = t0 + LAT + CONV0, t0 + 7 * K + LAT + CONV0
    s2 = probe(chip, [e2 - 1, e2 + 1], ADC2 + SR)
    s1 = probe(chip, [e1 - 1, e1 + 1], ADC1 + SR)
    chip.run_cycles(e1 - t0 + 5)
    assert [s & EOC for s in s2] == [0, EOC] and [s & EOC for s in s1] == [0, EOC]
    assert a1.conversions == a2.conversions == 1


# ------------------------------------------------------------------------------------------------ AWD / IRQ / misc
def test_analog_watchdog_regular_single_and_injected(desc):
    chip = spin_chip(desc)
    adc = chip.peripherals["ADC1"]
    adc.set_channel("IN0", 2500)
    adc.set_channel("IN1", 1500)
    wr(chip, ADC1 + HTR, 2000)
    wr(chip, ADC1 + LTR, 1000)
    wr(chip, ADC1 + CR1, AWDEN | AWDIE)
    power(chip, cr2=SW | JSW)
    wr(chip, ADC1 + CR2, ADON | SW | JSW | SWSTART)
    chip.run_cycles(LAT + CONV0 + 5)
    assert rd(chip, ADC1 + SR) & AWD and irq(chip)
    wr(chip, ADC1 + SR, 0)
    assert not irq(chip)
    wr(chip, ADC1 + CR1, AWDEN | AWDSGL | 1)                           # only IN1 (inside the window)
    wr(chip, ADC1 + CR2, ADON | SW | JSW | SWSTART)
    chip.run_cycles(LAT + CONV0 + 5)
    assert rd(chip, ADC1 + SR) & AWD == 0
    wr(chip, ADC1 + CR1, JAWDEN | AWDIE)                               # injected only
    wr(chip, ADC1 + CR2, ADON | SW | JSW | SWSTART)
    chip.run_cycles(LAT + CONV0 + 5)
    assert rd(chip, ADC1 + SR) & AWD == 0                              # regular not watched
    wr(chip, ADC1 + CR2, ADON | SW | JSW | JSWSTART)                   # JSQ4 = IN0 (2500)
    chip.run_cycles(JLAT + CONV0 + 5)
    assert rd(chip, ADC1 + SR) & AWD and irq(chip)


def test_shared_irq_line(desc):
    chip = spin_chip(desc)
    wr(chip, ADC1 + CR1, EOCIE)
    wr(chip, ADC2 + CR1, EOCIE)
    power(chip, ADC1, SW)
    power(chip, ADC2, SW)
    wr(chip, ADC1 + CR2, ADON | SW | SWSTART)
    wr(chip, ADC2 + CR2, ADON | SW | SWSTART)
    chip.run_cycles(LAT + CONV0 + 5)
    assert irq(chip)
    rd(chip, ADC1 + DR)
    assert irq(chip)                                                   # ADC2 still pending
    rd(chip, ADC2 + DR)
    assert not irq(chip)


def test_gate_off_trigger_ignored(desc):
    chip = spin_chip(desc)
    adc = chip.peripherals["ADC2"]
    power(chip, ADC2, SW)
    wr(chip, RCC + APB2ENR, rd(chip, RCC + APB2ENR) & ~(1 << 10))
    wr(chip, ADC2 + CR2, ADON | SW | SWSTART)
    chip.run_cycles(100)
    assert adc.conversions == 0 and any("clock off" in w for w in warns(chip))


def test_tim3_trgo_triggers_regular_conversions(desc):
    """EXTSEL = 100 TIM3_TRGO, TIM3 MMS = update: one conversion per update event (every 200 cycles)."""
    chip = spin_chip(desc)
    adc = chip.peripherals["ADC1"]
    power(chip, cr2=EXTTRIG | 4 << 17)
    wr(chip, TIM3 + 0x04, 2 << 4)                                      # CR2.MMS = update
    wr(chip, TIM3 + 0x2C, 199)                                         # ARR
    wr(chip, TIM3 + 0x00, 1)                                           # CEN
    chip.run_cycles(200 * 10 + 50)
    assert adc.conversions == 10
    wr(chip, ADC1 + CR2, ADON | EXTTRIG | 5 << 17)                     # TIM4_CC4: no TIM4 on the F103T6
    assert any("TIM4_CC4" in w for w in warns(chip))


def test_reset_cancels_everything(desc):
    chip = spin_chip(desc)
    adc = chip.peripherals["ADC1"]
    power(chip, cr2=SW | CONT)
    wr(chip, ADC1 + CR2, ADON | SW | CONT | SWSTART)
    chip.run_cycles(10)
    wr(chip, RCC + 0x0C, 1 << 9)                                       # APB2RSTR.ADC1RST
    wr(chip, RCC + 0x0C, 0)
    chip.run_cycles(500)
    assert adc.conversions == 0 and rd(chip, ADC1 + CR2) == 0


# ------------------------------------------------------------------------------------------------ sample firmware
def _sym(name: str) -> int:
    from elftools.elf.elffile import ELFFile
    with open(ELF, "rb") as f:
        return ELFFile(f).get_section_by_name(".symtab").get_symbol_by_name(name)[0]["st_value"]


def test_sample_adc_dma(desc):
    if not ELF.exists():
        pytest.skip(f"{ELF} not built (firmware/samples_stm32f1/build.sh)")
    chip = Chip(desc)
    chip.load_firmware(load_firmware(ELF))
    chip.reset()
    adc = chip.peripherals["ADC1"]
    adc.set_channel("PA0", 1.0)
    adc.set_channel("PA1", 2.5)
    ev = []
    chip.pins.subscribe(lambda e: ev.append(e.t_ns) if e.pin == "PB5" and e.field == "level" and e.old in ("H", "L")
                        else None)                                       # toggles (not the X -> L configuration)
    chip.run_for(1e6)
    assert chip.clock.hz == 72e6
    assert adc.calibrations == 1 and chip.bus.read(_sym("cal_code"), 4) == 0
    res = lambda: [chip.bus.read(_sym("results") + 2 * i, 2) for i in range(3)]      # noqa: E731
    assert res() == [code(1.0), code(2.5), code(1.43)]
    n = chip.bus.read(_sym("tc_count"), 4)
    assert 25 <= n <= 31 and len(ev) == n
    seq_ns = (2 * (55.5 + 12.5) + 239.5 + 12.5) / 12e6 * 1e9            # 388 ADCCLK at 12 MHz = 32.33 us
    gaps = [b - a for a, b in zip(ev, ev[1:])]
    assert all(g == pytest.approx(seq_ns, abs=20) for g in gaps)
    adc.set_channel("TEMP", 60.0)
    chip.run_for(0.1e6)
    assert res()[2] == code(1.43 - 0.0043 * 35)
    assert not [w for w in warns(chip) if "ADC" in w or "DMA" in w]
