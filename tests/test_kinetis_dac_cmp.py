"""Kinetis analog extras on the K22F description: VREF (ch. 36), the 12-bit DAC0 (ch. 35), the comparators CMP0-2
with their 6-bit DACs (ch. 34), and the ADC alternate hardware triggers selected by SIM_SOPT7 (12.2.6: FTM, CMP,
PDB0_EXTRG). K22P100M120SF5V2RM."""
from __future__ import annotations

from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.session import Session
from engine_util import build_image

K22 = Path(__file__).resolve().parent.parent / "chips" / "mk22fn1m0vll12.yaml"
SIM = 0x40047000
SOPT7, SCGC2, SCGC4, SCGC5, SCGC6, CLKDIV1 = (SIM + o for o in (0x1018, 0x102C, 0x1034, 0x1038, 0x103C, 0x1044))
VREF_TRM, VREF_SC = 0x40074000, 0x40074001
DAC0 = 0x400CC000
SR, C0, C1, C2 = DAC0 + 0x20, DAC0 + 0x21, DAC0 + 0x22, DAC0 + 0x23
CMP0, CMP1, CMP2 = 0x40073000, 0x40073008, 0x40073010
CR0, CR1, FPR, SCR, DACCR, MUXCR = range(6)
ADC0, ADC1 = 0x4003B000, 0x400BB000
FTM0 = 0x40038000
PORTC, PORTE, PORTB = 0x4004B000, 0x4004D000, 0x4004A000
DAC_IRQ, CMP0_IRQ, CMP1_IRQ = 56, 40, 41


@pytest.fixture(scope="module")
def desc():
    return load_chip(K22)


def w(chip, a, v, size=1):
    chip.bus.write(a, size, v)


def r(chip, a, size=1):
    return chip.bus.read(a, size)


@pytest.fixture
def chip(desc):
    c = Chip(desc, clock_hz=120e6)
    img, _ = build_image({"reset": "spin:\n  b spin"})
    c.load_firmware(img)
    c.reset()
    c.peripherals["MCG"].frozen = True
    c.clock.set_hz(120e6)
    w(c, CLKDIV1, 1 << 24, 4)                                # bus 60 MHz
    w(c, SCGC2, r(c, SCGC2, 4) | (1 << 12), 4)               # DAC0
    w(c, SCGC4, r(c, SCGC4, 4) | (1 << 19) | (1 << 20), 4)   # CMP, VREF
    w(c, SCGC5, r(c, SCGC5, 4) | (0x1F << 9), 4)             # PORTA..E
    w(c, SCGC6, r(c, SCGC6, 4) | (1 << 27) | (1 << 24) | (1 << 22), 4)   # ADC0, FTM0, PDB
    return c


def advance_ns(chip, ns):
    chip.clock.advance(chip.clock.ns_to_cycles(ns))
    chip.sched.run_due()


def infos(chip, needle):
    return [e for e in chip.log if e.kind == "info" and needle in e.fields.get("msg", "")]


# -------------------------------------------------------------------------------------------------- VREF
def test_vref_startup_and_output_modes(chip):
    vref = chip.peripherals["VREF"]
    pin = chip.pins.get(26)
    assert pin.owner == "analog" and vref.voltage() is None
    w(chip, VREF_SC, 0x80 | 0x01)                            # VREFEN, high-power buffer
    assert not r(chip, VREF_SC) & 0x04 and vref.voltage() is None
    advance_ns(chip, 34_000)
    assert not r(chip, VREF_SC) & 0x04
    advance_ns(chip, 2_000)
    assert r(chip, VREF_SC) == 0x85                          # VREFST after ~35 us
    assert vref.voltage() == pytest.approx(1.2)
    assert pin.owner == "VREF_OUT" and pin.direction == "analog"
    w(chip, VREF_SC, 0x80)                                   # MODE_LV=00: bandgap only, no buffered output
    assert r(chip, VREF_SC) == 0x84 and vref.voltage() is None and pin.owner == "analog"
    w(chip, VREF_SC, 0x82)                                   # low-power buffer: VREFST kept (36.4)
    assert r(chip, VREF_SC) == 0x86 and vref.voltage() == pytest.approx(1.2)
    w(chip, VREF_TRM, 0x40 | 4)                              # CHOPEN, TRIM +4 steps of 0.5 mV
    assert vref.voltage() == pytest.approx(1.202)
    w(chip, VREF_SC, 0x00)
    assert r(chip, VREF_SC) == 0 and vref.voltage() is None
    w(chip, VREF_SC, 0x81)
    w(chip, VREF_SC, 0x00)                                   # disabled before stable: the startup event is cancelled
    advance_ns(chip, 100_000)
    assert r(chip, VREF_SC) == 0
    w(chip, VREF_SC, 0xFF)
    assert r(chip, VREF_SC) & 0x18 == 0                      # reserved bits read 0


# -------------------------------------------------------------------------------------------------- DAC
def dat(chip, n, code):
    w(chip, DAC0 + 2 * n, code & 0xFF)
    w(chip, DAC0 + 2 * n + 1, code >> 8)


def test_dac_reset_and_output_voltage(chip):
    dac = chip.peripherals["DAC0"]
    assert (r(chip, SR), r(chip, C0), r(chip, C1), r(chip, C2)) == (0x02, 0, 0, 0x0F)
    assert dac.voltage() is None
    dat(chip, 0, 0xFFF)
    assert r(chip, DAC0 + 1) == 0x0F
    w(chip, DAC0 + 1, 0xFF)
    assert r(chip, DAC0 + 1) == 0x0F                         # DATnH[7:4] reserved
    w(chip, C0, 0x80 | 0x40)                                 # DACEN, DACRFS=1 -> VDDA
    assert dac.voltage() == pytest.approx(3.3)
    dat(chip, 0, 2047)
    assert dac.voltage() == pytest.approx(3.3 * 2048 / 4096)
    assert chip.pins.get(27).owner == "DAC0_OUT"
    ev = [e for e in chip.log if e.kind == "info" and e.fields.get("signal") == "DAC0_OUT"]
    assert ev[-1].fields["volts"] == pytest.approx(1.65) and ev[-1].fields["code"] == 2047
    w(chip, C0, 0x80)                                        # DACRFS=0 -> DACREF_1 = VREF_OUT (off: 0 V)
    assert dac.voltage() == 0.0
    w(chip, VREF_SC, 0x81)
    advance_ns(chip, 40_000)
    assert dac.voltage() == pytest.approx(1.2 * 2048 / 4096)  # follows the VREF when it comes up
    n = len(ev := [e for e in chip.log if e.fields.get("signal") == "DAC0_OUT"])
    dat(chip, 0, 2047)                                       # same code: no new event
    assert len([e for e in chip.log if e.fields.get("signal") == "DAC0_OUT"]) == n
    w(chip, C0, 0)
    assert dac.voltage() is None and chip.pins.get(27).owner == "analog"
    assert chip.peripherals["DAC0_ALIAS"].read(0x21, 1) == 0 and r(chip, 0x4003F000) == 0xFF  # AIPS0 window


def test_dac_buffer_normal_mode_flags_and_irq(chip):
    dac = chip.peripherals["DAC0"]
    for n, code in enumerate((100, 200, 300, 400)):
        dat(chip, n, code)
    w(chip, C2, 0x03)                                        # RP 0, UP 3
    w(chip, C1, 0x01)                                        # DACBFEN, normal, WM 1 word
    w(chip, C0, 0x80 | 0x40 | 0x20 | 0x07)                   # DACEN, VDDA, software trigger, all three IEs
    w(chip, SR, 0)
    assert dac.code() == 100 and DAC_IRQ + 16 not in chip.arch.asserted
    seq = []
    for _ in range(6):
        w(chip, C0, r(chip, C0) | 0x10)                      # DACSWTRG
        seq.append((r(chip, C2) >> 4, r(chip, SR)))
        assert r(chip, C0) & 0x10 == 0                       # reads 0
    assert [p for p, _ in seq] == [1, 2, 3, 0, 1, 2]
    assert seq[0][1] == 0 and seq[1][1] == 0x04              # watermark (UP - 1 = 2)
    assert seq[2][1] == 0x05 and seq[3][1] == 0x07           # bottom (RP == UP), then top (RP == 0)
    assert dac.code() == 300
    assert DAC_IRQ + 16 in chip.arch.asserted
    w(chip, SR, 0x07 & ~0x04)                                # write 0 clears WMF only
    assert r(chip, SR) == 0x03
    w(chip, SR, 0)
    assert r(chip, SR) == 0 and DAC_IRQ + 16 not in chip.arch.asserted
    w(chip, C0, 0x80 | 0x40 | 0x07)                          # DACTRGSEL=0: hardware trigger, software ignored
    w(chip, C0, r(chip, C0) | 0x10)
    assert r(chip, C2) >> 4 == 2
    assert dac.hw_trigger() and r(chip, C2) >> 4 == 3


def test_dac_buffer_swing_and_one_time_scan(chip):
    w(chip, C2, 0x02)                                        # UP 2
    w(chip, C0, 0x80 | 0x40 | 0x20)
    w(chip, C1, 0x01 | (1 << 1))                             # swing
    rp = []
    for _ in range(7):
        w(chip, C0, r(chip, C0) | 0x10)
        rp.append(r(chip, C2) >> 4)
    assert rp == [1, 2, 1, 0, 1, 2, 1]
    w(chip, C1, 0x01 | (2 << 1))                             # one-time scan: stops at UP
    rp = []
    for _ in range(3):
        w(chip, C0, r(chip, C0) | 0x10)
        rp.append(r(chip, C2) >> 4)
    assert rp == [2, 2, 2]
    w(chip, C2, 0x02)                                        # pointer written back to 0 (and UP 2)
    assert r(chip, C2) == 0x02 and r(chip, SR) & 0x02
    w(chip, C2, 0xF2)                                        # RP > UP is clamped to UP
    assert r(chip, C2) == 0x22
    w(chip, C1, 0)                                           # buffer disabled: triggers do not move the pointer
    w(chip, C0, r(chip, C0) | 0x10)
    assert r(chip, C2) == 0x22


def test_dac_dma_request_instead_of_irq(chip):
    hits = []
    mux = chip.peripherals["DMAMUX"]
    orig = mux.request
    mux.request = lambda src: hits.append(src) or orig(src)
    w(chip, C2, 0x01)
    w(chip, C1, 0x81)                                        # DMAEN, DACBFEN
    w(chip, C0, 0x80 | 0x40 | 0x20 | 0x01)                   # bottom-flag interrupt enable
    w(chip, SR, 0)
    w(chip, C0, r(chip, C0) | 0x10)                          # RP 1 == UP -> BF
    assert 45 in hits and DAC_IRQ + 16 not in chip.arch.asserted
    chip.peripherals["DAC0"]._dma_done()                     # DMA completion clears the enabled flag
    assert r(chip, SR) & 0x01 == 0


# -------------------------------------------------------------------------------------------------- CMP
def test_cmp_reset_registers_and_internal_6bit_dac(chip):
    cmp0 = chip.peripherals["CMP0"]
    assert [r(chip, CMP0 + i) for i in range(6)] == [0] * 6
    w(chip, CMP0 + DACCR, 0x80 | 0x40 | 31)                  # DACEN, Vin2 = VDD, VOSEL 31
    assert cmp0.dac6_voltage() == pytest.approx(3.3 * 32 / 64)
    w(chip, CMP0 + DACCR, 0x80 | 63)                         # Vin1 = VREF_OUT (off)
    assert cmp0.dac6_voltage() == 0.0
    w(chip, VREF_SC, 0x81)
    advance_ns(chip, 40_000)
    assert cmp0.dac6_voltage() == pytest.approx(1.2)
    w(chip, CMP0 + DACCR, 63)                                # DACEN=0: analog ground
    assert cmp0.dac6_voltage() == 0.0
    w(chip, CMP0 + CR0, 0xFF)
    w(chip, CMP0 + MUXCR, 0xFF)
    assert r(chip, CMP0 + CR0) == 0x73 and r(chip, CMP0 + MUXCR) == 0xBF   # reserved bits
    w(chip, CMP0 + CR1, 0xC0)                                # SE and WE both: SE wins, WE cleared
    assert r(chip, CMP0 + CR1) == 0x80


def setup_cmp0(chip, vin0, *, inv=False, ope=True):
    w(chip, PORTC + 4 * 5, 6 << 8, 4)                         # PTC5 ALT6 = CMP0_OUT
    w(chip, CMP0 + DACCR, 0x80 | 0x40 | 31)                   # 1.65 V
    w(chip, CMP0 + MUXCR, (0 << 3) | 7)                      # + IN0, - IN7
    chip.peripherals["CMP0"].set_input(0, vin0)
    w(chip, CMP0 + CR1, (0x08 if inv else 0) | (0x02 if ope else 0) | 0x01)


def test_cmp_compare_flags_irq_and_pin(chip):
    cmp0 = chip.peripherals["CMP0"]
    setup_cmp0(chip, 1.0)
    pin = chip.pins.get("PTC5")
    assert r(chip, CMP0 + SCR) & 1 == 0 and pin.owner == "CMP0_OUT" and pin.level() == "L"
    w(chip, CMP0 + SCR, 0x10 | 0x08)                          # IER, IEF
    cmp0.set_input(0, 2.0)
    assert r(chip, CMP0 + SCR) == 0x10 | 0x08 | 0x04 | 0x01   # CFR, COUT
    assert pin.level() == "H" and CMP0_IRQ + 16 in chip.arch.asserted
    cmp0.set_input(0, 1.0)
    assert r(chip, CMP0 + SCR) == 0x10 | 0x08 | 0x04 | 0x02   # CFF too, COUT 0
    assert pin.level() == "L"
    w(chip, CMP0 + SCR, 0x10 | 0x08 | 0x04)                   # w1c CFR
    assert r(chip, CMP0 + SCR) == 0x10 | 0x08 | 0x02 and CMP0_IRQ + 16 in chip.arch.asserted
    w(chip, CMP0 + SCR, 0x10 | 0x08 | 0x02)
    assert r(chip, CMP0 + SCR) == 0x18 and CMP0_IRQ + 16 not in chip.arch.asserted
    w(chip, CMP0 + SCR, 0x01)                                 # COUT is read-only; IER/IEF written 0
    assert r(chip, CMP0 + SCR) == 0
    cmp0.set_input(0, 2.0)
    assert r(chip, CMP0 + SCR) == 0x05 and CMP0_IRQ + 16 not in chip.arch.asserted   # flag without IER: no IRQ
    w(chip, CMP0 + CR1, 0x02 | 0x01 | 0x08)                   # INV: output inverted
    assert r(chip, CMP0 + SCR) & 1 == 0 and pin.level() == "L"
    w(chip, CMP0 + CR1, 0x02 | 0x08)                          # disabled: COUT reads INV, CMPO = 0
    assert r(chip, CMP0 + SCR) & 1 == 1 and pin.level() == "L"
    w(chip, CMP0 + CR1, 0x01)                                 # OPE=0: the pin is released
    assert chip.pins.signal_levels.get("CMP0_OUT") is None


def test_cmp_hysteresis_and_same_input_shutdown(chip):
    cmp0 = chip.peripherals["CMP0"]
    setup_cmp0(chip, 1.0)
    w(chip, CMP0 + CR0, 3)                                    # level 3: 30 mV
    cmp0.set_input(0, 1.66)                                   # +10 mV: inside the band, stays 0
    assert r(chip, CMP0 + SCR) & 1 == 0
    cmp0.set_input(0, 1.67)                                   # +20 mV > 15 mV: switches
    assert r(chip, CMP0 + SCR) & 1 == 1
    cmp0.set_input(0, 1.64)                                   # -10 mV: stays 1
    assert r(chip, CMP0 + SCR) & 1 == 1
    cmp0.set_input(0, 1.63)
    assert r(chip, CMP0 + SCR) & 1 == 0
    w(chip, CMP0 + MUXCR, (7 << 3) | 7)                       # PSEL == MSEL: comparator shuts itself down
    assert not cmp0.enabled()


def test_cmp_filter_delay(chip):
    """Sampled/filtered mode (SE=0, FILTER_CNT=4, FILT_PER=10): a change reaches COUT after 4 * 10 bus clocks
    (60 MHz bus -> 666.7 ns); a glitch shorter than that is filtered out."""
    cmp0 = chip.peripherals["CMP0"]
    setup_cmp0(chip, 1.0)
    w(chip, CMP0 + CR0, 4 << 4)
    w(chip, CMP0 + FPR, 10)
    cmp0.set_input(0, 2.0)
    assert r(chip, CMP0 + SCR) & 1 == 0
    advance_ns(chip, 600)
    assert r(chip, CMP0 + SCR) & 1 == 0
    advance_ns(chip, 100)
    assert r(chip, CMP0 + SCR) & 1 == 1 and r(chip, CMP0 + SCR) & 0x04
    cmp0.set_input(0, 1.0)
    advance_ns(chip, 300)
    cmp0.set_input(0, 2.0)                                    # back before the filter accepted it
    advance_ns(chip, 2000)
    assert r(chip, CMP0 + SCR) & 0x07 == 0x05                 # no falling edge was seen
    w(chip, CMP0 + CR1, 0x80 | 0x01)                          # SE=1: PDB SAMPLE not modelled -> holds, warns
    assert any("SAMPLE/WINDOW" in e.fields.get("msg", "") for e in chip.log if e.kind == "warn")


def test_cmp1_in3_is_dac0_and_in5_is_vref(chip):
    cmp1 = chip.peripherals["CMP1"]
    cmp1.set_input(3, 0.5)                                    # external value on the shared pin
    assert cmp1.input_voltage(3) == 0.5
    dat(chip, 0, 4095)
    w(chip, C0, 0xC0)                                         # DAC0 on: 3.3 V drives the pin
    assert cmp1.input_voltage(3) == pytest.approx(3.3)
    w(chip, CMP1 + DACCR, 0x80 | 0x40 | 31)
    w(chip, CMP1 + MUXCR, (3 << 3) | 7)
    w(chip, CMP1 + SCR, 0x10)
    w(chip, CMP1 + CR1, 0x01)
    assert r(chip, CMP1 + SCR) & 1 == 1 and CMP1_IRQ + 16 in chip.arch.asserted
    dat(chip, 0, 1000)                                        # DAC drops below 1.65 V: falling edge
    assert r(chip, CMP1 + SCR) & 0x03 == 0x02
    assert cmp1.input_voltage(5) == 0.0                       # VREF off, nothing set
    w(chip, VREF_SC, 0x81)
    advance_ns(chip, 40_000)
    assert cmp1.input_voltage(5) == pytest.approx(1.2) and cmp1.input_voltage(6) == pytest.approx(1.0)


def test_cmp_inputs_via_session_set_adc_in_volts(desc):
    """CMP inputs take volts only (`adc_volts_only`): "1.2V" / "1200mV" / 1.2 work, a bare integer is rejected with a
    message naming the V / mV form (an integer is a raw ADC code everywhere else)."""
    from chipem.session import parse_adc_value
    s = Session("mk22fn1m0vll12")
    s.set_adc("CMP0_IN0", parse_adc_value("1.2V"))
    s.set_adc("CMP2_IN1", parse_adc_value("3000mV"))
    cmp0, cmp2 = s.chip.peripherals["CMP0"], s.chip.peripherals["CMP2"]
    assert cmp0.input_voltage(0) == pytest.approx(1.2) and cmp2.input_voltage(1) == pytest.approx(3.0)
    with pytest.raises(ValueError, match="volts"):
        s.set_adc("CMP0_IN0", 1200)
    with pytest.raises(ValueError, match="volts"):
        cmp0.set_channel("CMP0_IN0", 1200)
    assert cmp0.input_voltage(0) == pytest.approx(1.2)        # unchanged by the rejected value
    assert cmp0.set_channel("CMP1_IN0", 5.0) is False        # another instance's input
    assert cmp0.set_input("PTC6", 1.0) is False              # not a CMP input name (Session fallback)
    assert cmp0.set_input("CMP0_IN2", 0.5) and cmp0.input_voltage(2) == pytest.approx(0.5)


def test_cmp_dma_request(chip):
    hits = []
    mux = chip.peripherals["DMAMUX"]
    orig = mux.request
    mux.request = lambda src: hits.append(src) or orig(src)
    setup_cmp0(chip, 1.0)
    w(chip, CMP0 + SCR, 0x40 | 0x10)                          # DMAEN, IER
    chip.peripherals["CMP0"].set_input(0, 2.0)
    assert 42 in hits and CMP0_IRQ + 16 not in chip.arch.asserted
    chip.peripherals["CMP0"]._dma_done()
    assert r(chip, CMP0 + SCR) & 0x04 == 0


# -------------------------------------------------------------------------------------------------- ADC triggers
def adc_hw_mode(chip, base=ADC0, ch=14, sc1b=False):
    w(chip, base + 0x08, 1 << 2, 4)                           # CFG1 MODE=01 12-bit
    w(chip, base + 0x20, 0x40, 4)                             # SC2.ADTRG
    w(chip, base + (0x04 if sc1b else 0x00), ch, 4)           # SC1A/SC1B channel (no conversion yet)


def test_adc_ftm_alternate_trigger(chip):
    adc = chip.peripherals["ADC0"]
    adc.set_channel(14, 1234)
    adc.set_channel("SE15", 777)
    adc_hw_mode(chip)
    adc_hw_mode(chip, ch=15, sc1b=True)
    w(chip, FTM0 + 0x6C, 1 << 6, 4)                           # EXTTRIG INITTRIGEN
    w(chip, FTM0 + 0x08, 99, 4)                               # MOD
    w(chip, FTM0 + 0x00, (1 << 3) | 4, 4)                     # system clock /16: period 100 ticks = 26.7 us
    chip.run_for(200_000)
    assert adc.conversions == 0                               # ALTTRGEN=0: PDB trigger path, PDB idle
    w(chip, SOPT7, 0x80 | 8, 4)                               # ADC0ALTTRGEN, TRGSEL FTM0, pre-trigger A
    chip.run_for(200_000)
    n = adc.conversions
    assert n >= 6 and r(chip, ADC0 + 0x10, 4) == 1234         # RA
    w(chip, SOPT7, 0x80 | 0x10 | 8, 4)                        # pre-trigger B -> SC1B
    chip.run_for(200_000)
    assert adc.conversions > n and r(chip, ADC0 + 0x14, 4) == 777
    w(chip, SOPT7, 0x80 | 9, 4)                               # FTM1 selected: FTM0 no longer triggers
    m = adc.conversions
    chip.run_for(200_000)
    assert adc.conversions == m
    assert adc.hw_trigger(0) is False                         # PDB pre-trigger ignored while ALTTRGEN=1
    w(chip, SOPT7, 0, 4)
    assert adc.hw_trigger(0) is True


def test_adc_cmp_and_extrg_alternate_triggers(chip):
    adc = chip.peripherals["ADC0"]
    adc_hw_mode(chip)
    setup_cmp0(chip, 1.0, ope=False)
    w(chip, SOPT7, 0x80 | 1, 4)                               # CMP0 output
    chip.peripherals["CMP0"].set_input(0, 2.0)                # rising COUT edge
    advance_ns(chip, 10_000)
    assert adc.conversions == 1
    chip.peripherals["CMP0"].set_input(0, 1.0)                # falling edge: no trigger
    advance_ns(chip, 10_000)
    assert adc.conversions == 1
    w(chip, PORTC + 4 * 0, 3 << 8, 4)                         # PTC0 ALT3 = PDB0_EXTRG
    w(chip, SOPT7, 0x80 | 0, 4)
    chip.pins.set_external("PTC0", "L")
    chip.pins.set_external("PTC0", "H")
    advance_ns(chip, 10_000)
    assert adc.conversions == 2
    chip.pins.set_external("PTC0", "L")
    advance_ns(chip, 10_000)
    assert adc.conversions == 2


def test_adc_unmodelled_trigger_logged_and_internal_channels(chip):
    w(chip, SOPT7, (0x80 | 15) << 8, 4)                       # ADC1 <- reserved TRGSEL 15
    assert infos(chip, "ADC1 alternate trigger 15 (reserved) is not modelled")
    adc0 = chip.peripherals["ADC0"]
    dat(chip, 0, 2047)
    w(chip, C0, 0xC0)                                         # DAC0 = 1.65 V on ADC0_SE23
    w(chip, ADC0 + 0x08, 1 << 2, 4)                           # 12-bit
    w(chip, ADC0 + 0x00, 23, 4)
    advance_ns(chip, 10_000)
    assert r(chip, ADC0 + 0x10, 4) == 2048
    w(chip, 0x400BB008, 1 << 2, 4)                            # ADC1 12-bit: SE18 = VREF_OUT (1.2 V of 3.3 V)
    w(chip, VREF_SC, 0x81)
    advance_ns(chip, 40_000)
    w(chip, 0x400BB000, 18, 4)
    advance_ns(chip, 10_000)
    assert r(chip, 0x400BB010, 4) == round(1.2 / 3.3 * 4096)
