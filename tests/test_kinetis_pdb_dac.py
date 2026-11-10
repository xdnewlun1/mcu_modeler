"""PDB0 DAC interval trigger into DAC0 (K22P100M120SF5V2RM 37.4.3, chip configuration 3.8.1.4/3.8.1.5) and the
timer trigger sources shared by PDB0 SC.TRGSEL (Table 3-51) and SIM_SOPT7 ADCnTRGSEL (12.2.6): PIT channels, LPTMR0,
RTC alarm/seconds, CMP outputs and the PDB0_EXTRG pin, with exact trigger times."""
from __future__ import annotations

from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from engine_util import build_image

K22 = Path(__file__).resolve().parent.parent / "chips" / "mk22fn1m0vll12.yaml"
SIM = 0x40047000
SOPT7, SCGC2, SCGC4, SCGC5, SCGC6, CLKDIV1 = (SIM + o for o in (0x1018, 0x102C, 0x1034, 0x1038, 0x103C, 0x1044))
WDOG = 0x40052000
PDB = 0x40036000
SC, MOD, CNT, IDLY, DACINTC0, DACINT0 = 0x00, 0x04, 0x08, 0x0C, 0x150, 0x154
LDOK, CONT, PDBEN, SWTRIG = 1, 2, 1 << 7, 1 << 16
MULT10 = 1 << 2                                         # MULT=01: x10
DAC0 = 0x400CC000
ADC0 = 0x4003B000
PIT, LPTMR, RTC = 0x40037000, 0x40040000, 0x4003D000
CMP0 = 0x40073000
PORTC = 0x4004B000
BUS = 60e6
PDB_TICK = 10 * 1e9 / BUS                               # prescaler 1, MULT 10 -> 166.67 ns


@pytest.fixture(scope="module")
def desc():
    return load_chip(K22)


def w(chip, a, v, size=4):
    chip.bus.write(a, size, v)


def r(chip, a, size=4):
    return chip.bus.read(a, size)


@pytest.fixture
def chip(desc):
    c = Chip(desc, clock_hz=120e6)
    img, _ = build_image({"reset": "spin:\n  wfi\n  b spin"})       # sleeps: run_for jumps between events
    c.load_firmware(img)
    c.reset()
    c.peripherals["MCG"].frozen = True
    c.clock.set_hz(120e6)
    w(c, WDOG + 0x0E, 0xC520, 2)                          # unlock, then WDOGEN=0
    w(c, WDOG + 0x0E, 0xD928, 2)
    w(c, WDOG + 0x00, 0x01D2, 2)
    w(c, CLKDIV1, 1 << 24)                                # bus 60 MHz
    w(c, SCGC2, r(c, SCGC2) | (1 << 12))                  # DAC0
    w(c, SCGC4, r(c, SCGC4) | (1 << 19))                  # CMP
    w(c, SCGC5, r(c, SCGC5) | (0x1F << 9) | 1)            # PORTA..E, LPTMR
    w(c, SCGC6, r(c, SCGC6) | (1 << 22) | (1 << 23) | (1 << 27) | (1 << 29))   # PDB, PIT, ADC0, RTC
    return c


def advance_to(chip, t_ns):
    chip.run_for(t_ns - chip.now_ns())


def record(obj, name, chip):
    """Wrap obj.<name> to record the virtual time of every call."""
    times = []
    orig = getattr(obj, name)

    def wrapped(*a, **kw):
        times.append(chip.now_ns())
        return orig(*a, **kw)
    setattr(obj, name, wrapped)
    return times


def dac_hw_buffer(chip, up=15):
    w(chip, DAC0 + 0x23, up, 1)                           # RP 0, UP
    w(chip, DAC0 + 0x22, 0x01, 1)                         # DACBFEN, normal mode
    w(chip, DAC0 + 0x21, 0x80 | 0x40, 1)                  # DACEN, VDDA, DACTRGSEL=0 (hardware trigger)


# -------------------------------------------------------------------------------------------- DAC interval
def test_dac_interval_trigger_times_and_buffer_pointer(chip):
    dac = chip.peripherals["DAC0"]
    pdb = chip.peripherals["PDB0"]
    times = record(dac, "hw_trigger", chip)
    dac_hw_buffer(chip)
    base = PDBEN | CONT | MULT10 | (15 << 8)
    w(chip, PDB + SC, base)
    w(chip, PDB + MOD, 999)
    w(chip, PDB + DACINT0, 99)
    w(chip, PDB + DACINTC0, 1)                            # TOE
    w(chip, PDB + SC, base | LDOK)
    assert r(chip, PDB + DACINT0) == 99
    t0 = chip.clock.now_ns()
    w(chip, PDB + SC, base | SWTRIG)
    advance_to(chip, t0 + 2000 * PDB_TICK + 1)            # two full PDB cycles
    # counts 99, 199, ..., 999 in each cycle: every DACINT+1 = 100 PDB clocks, restarting with the counter
    expect = [t0 + (cyc * 1000 + 99 + 100 * k) * PDB_TICK for cyc in range(2) for k in range(10)]
    assert len(times) == 20 and all(abs(a - b) < 1 for a, b in zip(times, expect))
    assert pdb.dac_triggers == 20
    assert r(chip, DAC0 + 0x23, 1) >> 4 == 20 % 16        # normal mode: wrapped once at UP 15
    w(chip, PDB + DACINTC0, 0)                            # TOE=0: no more pulses
    advance_to(chip, t0 + 3000 * PDB_TICK + 1)
    assert len(times) == 20


def test_dac_interval_buffered_by_ldok_and_one_shot(chip):
    dac = chip.peripherals["DAC0"]
    times = record(dac, "hw_trigger", chip)
    dac_hw_buffer(chip)
    base = PDBEN | MULT10 | (15 << 8)                     # one-shot
    w(chip, PDB + SC, base)
    w(chip, PDB + MOD, 499)
    w(chip, PDB + DACINT0, 199)
    w(chip, PDB + DACINTC0, 1)
    w(chip, PDB + SC, base | LDOK)
    w(chip, PDB + DACINT0, 49)                            # not loaded without LDOK
    t0 = chip.clock.now_ns()
    w(chip, PDB + SC, base | SWTRIG)
    advance_to(chip, t0 + 2000 * PDB_TICK)
    assert [round((t - t0) / PDB_TICK) for t in times] == [199, 399]     # 599 > MOD; the counter stops at MOD
    assert r(chip, DAC0 + 0x23, 1) >> 4 == 2
    w(chip, PDB + SC, base | LDOK)                        # now DACINT 49 is effective
    t1 = chip.clock.now_ns()
    w(chip, PDB + SC, base | SWTRIG)
    advance_to(chip, t1 + 1000 * PDB_TICK)
    assert [round((t - t1) / PDB_TICK) for t in times[2:]] == [49, 99, 149, 199, 249, 299, 349, 399, 449, 499]


def test_dac_external_trigger_from_adc0_sc1a(chip):
    """DACINTC0.EXT=1: the interval counter is bypassed; ADC0 SC1A completion (ADC0SC1A_COCO) triggers DAC0."""
    pdb = chip.peripherals["PDB0"]
    dac_hw_buffer(chip)
    w(chip, PDB + SC, PDBEN | (15 << 8))
    w(chip, PDB + DACINTC0, 3)                            # TOE + EXT
    w(chip, ADC0 + 0x00, 14)                              # software conversion of SE14
    advance_to(chip, chip.clock.now_ns() + 10_000)
    assert pdb.dac_triggers == 1 and r(chip, DAC0 + 0x23, 1) >> 4 == 1
    w(chip, ADC0 + 0x04, 14)                              # SC1B is not the external trigger input
    w(chip, PDB + DACINTC0, 2)                            # EXT without TOE: off
    w(chip, ADC0 + 0x00, 14)
    advance_to(chip, chip.clock.now_ns() + 10_000)
    assert pdb.dac_triggers == 1


# -------------------------------------------------------------------------------------------- timer triggers
def adc_hw(chip, sel):
    adc = chip.peripherals["ADC0"]
    done = []
    adc.on_complete.append(lambda a, k: done.append((chip.now_ns(), k)))
    w(chip, ADC0 + 0x20, 0x40)                            # SC2.ADTRG
    w(chip, ADC0 + 0x00, 14)                              # SC1A channel, waits for a trigger
    w(chip, SOPT7, 0x80 | sel)                            # ADC0ALTTRGEN, TRGSEL
    return done


def test_pit_channel_triggers_adc_at_each_timeout(chip):
    done = adc_hw(chip, 4 + 1)                            # PIT trigger 1
    w(chip, PIT + 0x000, 0)                               # MCR: module enabled
    w(chip, PIT + 0x100, 59_999)                          # channel 0, 1 ms: must not trigger
    w(chip, PIT + 0x108, 1)
    w(chip, PIT + 0x110, 2_999)                           # channel 1: 3000 bus clocks = 50 us
    t0 = chip.clock.now_ns()
    w(chip, PIT + 0x118, 1)                               # TEN (no TIE: the trigger does not need the IRQ)
    advance_to(chip, t0 + 220_000)
    assert [round(t - t0) for t, _ in done] == [54_000, 104_000, 154_000, 204_000]   # timeout + 4 us conversion
    assert all(k == 0 for _, k in done)
    w(chip, SOPT7, 0x80 | 0x10 | 5)                       # pre-trigger B -> SC1B
    w(chip, ADC0 + 0x04, 15)
    advance_to(chip, t0 + 260_000)
    assert done[-1][1] == 1 and round(done[-1][0] - t0) == 254_000


def test_lptmr_compare_triggers_adc(chip):
    done = adc_hw(chip, 14)
    w(chip, LPTMR + 0x04, 1 | 4)                          # PSR: LPO 1 kHz, prescaler bypassed
    w(chip, LPTMR + 0x08, 4)                              # CMR 4 -> TCF every 5 ms
    t0 = chip.clock.now_ns()
    w(chip, LPTMR + 0x00, 1)                              # TEN
    advance_to(chip, t0 + 16e6)
    assert [round(t - t0) for t, _ in done] == [5_004_000, 10_004_000, 15_004_000]


def test_rtc_alarm_and_seconds_trigger_pdb_and_adc(chip):
    pdb = chip.peripherals["PDB0"]
    starts = record(pdb, "_start_cycle", chip)
    done = adc_hw(chip, 12)                               # ADC0 <- RTC alarm
    w(chip, PDB + SC, PDBEN | (13 << 8))                  # PDB0 <- RTC seconds
    w(chip, RTC + 0x00, 0)                                # TSR (clears TIF)
    w(chip, RTC + 0x08, 2)                                # TAR: TAF on the 2 -> 3 transition
    t0 = chip.clock.now_ns()
    w(chip, RTC + 0x14, 0x10)                             # SR.TCE
    advance_to(chip, t0 + 4.5e9)
    assert [round((t - t0) / 1e6) for t in starts] == [1000, 2000, 3000, 4000]
    assert [round(t - t0) for t, _ in done] == [3_000_004_000]


def test_pdb_input_triggers_pit_lptmr_cmp_and_extrg(chip):
    pdb = chip.peripherals["PDB0"]
    starts = record(pdb, "_start_cycle", chip)
    w(chip, PDB + SC, PDBEN | (6 << 8))                   # PIT channel 2
    w(chip, PIT + 0x000, 0)
    w(chip, PIT + 0x120, 599)                             # 10 us
    t0 = chip.clock.now_ns()
    w(chip, PIT + 0x128, 1)
    advance_to(chip, t0 + 35_000)
    assert [round(t - t0) for t in starts] == [10_000, 20_000, 30_000]
    w(chip, PIT + 0x128, 0)
    starts.clear()
    w(chip, PDB + SC, PDBEN | (1 << 8))                   # CMP0 output (rising edges only)
    cmp0 = chip.peripherals["CMP0"]
    w(chip, CMP0 + 4, 0x80 | 0x40 | 31, 1)                # 6-bit DAC 1.65 V on IN7
    w(chip, CMP0 + 5, 7, 1)                               # + IN0, - IN7
    w(chip, CMP0 + 1, 1, 1)                               # EN
    cmp0.set_input(0, 3.0)
    cmp0.set_input(0, 0.0)
    assert len(starts) == 1
    starts.clear()
    w(chip, PORTC + 4 * 6, 3 << 8)                        # PTC6 ALT3 = PDB0_EXTRG
    w(chip, PDB + SC, PDBEN | (0 << 8))
    chip.pins.set_external("PTC6", "L")
    chip.pins.set_external("PTC6", "H")
    chip.pins.set_external("PTC6", "L")
    assert len(starts) == 1
    starts.clear()
    w(chip, PDB + SC, PDBEN | (14 << 8))                  # LPTMR0
    w(chip, LPTMR + 0x04, 1 | 4)
    w(chip, LPTMR + 0x08, 1)                              # every 2 ms
    t1 = chip.clock.now_ns()
    w(chip, LPTMR + 0x00, 1)
    advance_to(chip, t1 + 5e6)
    assert [round(t - t1) for t in starts] == [2_000_000, 4_000_000]
    assert not [e for e in chip.log if e.kind == "info" and "TRGSEL" in e.fields.get("msg", "")]
