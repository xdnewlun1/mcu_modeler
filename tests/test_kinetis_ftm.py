"""Kinetis FlexTimer (chipem/periph/kinetis/ftm.py) on the K22F description: counting, PWM / combine / dead time
waveforms on the pins, output compare, flags and their clear sequence, register buffering and synchronization."""
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from engine_util import build_image

K22 = Path(__file__).resolve().parent.parent / "chips" / "mk22fn1m0vll12.yaml"
SIM_SCGC5, SIM_SCGC6, SIM_SCGC3 = 0x40048038, 0x4004803C, 0x40048030
PORTC = 0x4004B000
FTM0, FTM2, FTM2_ALIAS = 0x40038000, 0x400B8000, 0x4003A000
SC, CNT, MOD, C0SC, C0V, C1SC, C1V = 0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18
CNTIN, STATUS, MODE, SYNC, OUTINIT, OUTMASK, COMBINE, DEADTIME, POL, SWOCTRL = \
    0x4C, 0x50, 0x54, 0x58, 0x5C, 0x60, 0x64, 0x68, 0x70, 0x94
CLK_SYS, CPWMS, TOF, TOIE = 1 << 3, 1 << 5, 1 << 7, 1 << 6
CHF, CHIE, MSB, MSA, ELSB, ELSA = 1 << 7, 1 << 6, 1 << 5, 1 << 4, 1 << 3, 1 << 2
FTM0_IRQ = 42


@pytest.fixture(scope="module")
def desc():
    return load_chip(K22)


def w(chip, a, v):
    chip.bus.write(a, 4, v)


def r(chip, a):
    return chip.bus.read(a, 4)


@pytest.fixture
def chip(desc):
    c = Chip(desc, clock_hz=120e6)
    img, _ = build_image({"reset": "spin:\n  b spin"})
    c.load_firmware(img)
    c.reset()
    w(c, SIM_SCGC5, r(c, SIM_SCGC5) | (1 << 11))          # PORTC clock
    w(c, SIM_SCGC6, r(c, SIM_SCGC6) | (1 << 24))          # FTM0 clock
    w(c, PORTC + 4 * 1, 4 << 8)                            # PTC1 = ALT4 FTM0_CH0
    w(c, PORTC + 4 * 2, 4 << 8)                            # PTC2 = ALT4 FTM0_CH1
    return c


def edges(chip, pin):
    out = []
    chip.pins.subscribe(lambda ev: ev.pin == pin and ev.field == "level" and out.append((ev.t_ns, ev.new)))
    return out


def ticks(chip, evs, t0):
    tk = 1e9 / chip.peripherals["FTM0"].counter_hz()
    return [(round((t - t0) / tk), lvl) for t, lvl in evs]


def run_ticks(chip, n):
    chip.run_for(n * 1e9 / chip.peripherals["FTM0"].counter_hz())


def test_edge_aligned_pwm(chip):
    w(chip, FTM0 + MOD, 99)
    w(chip, FTM0 + C0V, 25)
    w(chip, FTM0 + C0SC, MSB | ELSB)                       # EPWM high-true
    e = edges(chip, "PTC1")
    t0 = chip.now_ns()
    w(chip, FTM0 + SC, CLK_SYS)
    run_ticks(chip, 420)
    tk = ticks(chip, e, t0)
    assert [x for x in tk if x[0] > 0][:6] == [(25, "L"), (100, "H"), (125, "L"), (200, "H"), (225, "L"), (300, "H")]


def test_center_aligned_pwm(chip):
    w(chip, FTM0 + MOD, 100)
    w(chip, FTM0 + C0V, 30)
    w(chip, FTM0 + C0SC, ELSB)
    e = edges(chip, "PTC1")
    t0 = chip.now_ns()
    w(chip, FTM0 + SC, CPWMS | CLK_SYS)
    run_ticks(chip, 450)
    tk = [x for x in ticks(chip, e, t0) if x[0] > 0]
    assert tk[:4] == [(30, "L"), (170, "H"), (230, "L"), (370, "H")]           # period 2 x MOD = 200
    assert chip.peripherals["FTM0"].overflows >= 2                              # TOF at MOD -> MOD-1


def test_combine_complementary_with_dead_time(chip):
    w(chip, FTM0 + MOD, 99)
    w(chip, FTM0 + C0V, 20)
    w(chip, FTM0 + C1V, 60)
    w(chip, FTM0 + C0SC, ELSB)
    w(chip, FTM0 + C1SC, ELSB)
    w(chip, FTM0 + DEADTIME, 6)                             # 6 system clocks (= FTM clocks with PS=0)
    w(chip, FTM0 + COMBINE, 0x01 | 0x02 | 0x10)              # COMBINE0, COMP0, DTEN0
    a, b = edges(chip, "PTC1"), edges(chip, "PTC2")
    t0 = chip.now_ns()
    w(chip, FTM0 + SC, CLK_SYS)
    run_ticks(chip, 200)
    ta = [x for x in ticks(chip, a, t0) if x[0] > 0]
    tb = [x for x in ticks(chip, b, t0) if x[0] > 0]
    assert ta[:2] == [(26, "H"), (60, "L")]                  # CH0: set at C0V + dead time, cleared at C1V
    # CH1: the complement; every rising edge is delayed, also the first one after the outputs were enabled
    assert tb[:3] == [(6, "H"), (20, "L"), (66, "H")]
    lv = {"PTC1": chip.pins.get("PTC1").level(), "PTC2": chip.pins.get("PTC2").level()}
    assert not (lv["PTC1"] == "H" and lv["PTC2"] == "H")


def test_output_compare_toggle_flag_and_irq(chip):
    w(chip, FTM0 + MOD, 999)
    w(chip, FTM0 + C0V, 300)
    w(chip, FTM0 + C0SC, MSA | ELSA | CHIE)                 # output compare, toggle, interrupt
    e = edges(chip, "PTC1")
    t0 = chip.now_ns()
    w(chip, FTM0 + SC, CLK_SYS | TOIE)
    run_ticks(chip, 310)
    assert r(chip, FTM0 + C0SC) & CHF and (16 + FTM0_IRQ) in chip.arch.asserted
    w(chip, FTM0 + C0SC, MSA | ELSA | CHIE)                 # read (above) then write 0: clears CHF
    assert not r(chip, FTM0 + C0SC) & CHF
    run_ticks(chip, 700)
    assert r(chip, FTM0 + SC) & TOF and (16 + FTM0_IRQ) in chip.arch.asserted
    w(chip, FTM0 + SC, CLK_SYS | TOIE)
    assert not r(chip, FTM0 + SC) & TOF and (16 + FTM0_IRQ) not in chip.arch.asserted
    run_ticks(chip, 1000)
    tk = [x for x in ticks(chip, e, t0) if x[0] > 0]
    assert tk[:2] == [(300, "H"), (1300, "L")]               # one toggle per period


def test_flag_not_cleared_without_reading_it_first(chip):
    w(chip, FTM0 + MOD, 99)
    w(chip, FTM0 + SC, CLK_SYS)
    run_ticks(chip, 150)
    w(chip, FTM0 + SC, CLK_SYS)                              # write 0 without a read of TOF=1 first
    assert r(chip, FTM0 + SC) & TOF


def test_cnv_buffered_until_overflow_with_ftmen_0(chip):
    w(chip, FTM0 + MOD, 99)
    w(chip, FTM0 + C0V, 25)
    w(chip, FTM0 + C0SC, MSB | ELSB)
    e = edges(chip, "PTC1")
    t0 = chip.now_ns()
    w(chip, FTM0 + SC, CLK_SYS)
    run_ticks(chip, 110)
    w(chip, FTM0 + C0V, 75)                                  # mid period: active at the next MOD -> CNTIN
    run_ticks(chip, 200)
    tk = [x for x in ticks(chip, e, t0) if x[0] > 0]
    assert tk[:4] == [(25, "L"), (100, "H"), (125, "L"), (200, "H")] and (275, "L") in tk


def test_cnv_waits_for_software_sync_with_ftmen_1(chip):
    w(chip, FTM0 + MODE, 0x04 | 0x01)                        # WPDIS, FTMEN
    w(chip, FTM0 + SYNC, 0x02)                               # loading point: CNTMAX
    w(chip, FTM0 + MOD, 99)
    w(chip, FTM0 + C0V, 25)
    w(chip, FTM0 + C0SC, MSB | ELSB)
    e = edges(chip, "PTC1")
    t0 = chip.now_ns()
    w(chip, FTM0 + SC, CLK_SYS)
    run_ticks(chip, 110)
    w(chip, FTM0 + C0V, 75)
    run_ticks(chip, 200)                                     # no sync yet: still 25
    w(chip, FTM0 + SYNC, 0x02 | 0x80)                        # SWSYNC
    run_ticks(chip, 200)
    tk = [x for x in ticks(chip, e, t0) if x[0] > 0]
    falls = [t for t, lvl in tk if lvl == "L"]
    # SWSYNC at tick 310: the period from 300 still uses 25; loaded at CNTMAX (399), so 75 from the period at 400
    assert {225, 325, 475} <= set(falls) and 425 not in falls
    assert not r(chip, FTM0 + SYNC) & 0x80                   # SWSYNC clears once loaded


def test_polarity_mask_and_software_control(chip):
    w(chip, FTM0 + MOD, 99)
    w(chip, FTM0 + C0V, 50)
    w(chip, FTM0 + C0SC, MSB | ELSB)
    w(chip, FTM0 + SC, CLK_SYS)
    run_ticks(chip, 10)                                       # CNT < C0V: high
    assert chip.pins.get("PTC1").level() == "H"
    w(chip, FTM0 + POL, 1)
    assert chip.pins.get("PTC1").level() == "L"
    w(chip, FTM0 + OUTMASK, 1)                               # masked: inactive state = POL0 = 1
    assert chip.pins.get("PTC1").level() == "H"
    w(chip, FTM0 + OUTMASK, 0)
    w(chip, FTM0 + POL, 0)
    w(chip, FTM0 + SWOCTRL, 0x0001)                           # CH0OC, CH0OCV=0: forced low
    assert chip.pins.get("PTC1").level() == "L"
    w(chip, FTM0 + SWOCTRL, 0x0101)
    assert chip.pins.get("PTC1").level() == "H"


def test_ftm2_alias_window_is_the_same_timer(chip):
    w(chip, SIM_SCGC3, r(chip, SIM_SCGC3) | (1 << 24))       # FTM2 clock
    w(chip, FTM2_ALIAS + MOD, 1234)
    assert r(chip, FTM2 + MOD) == 1234 and chip.peripherals["FTM2"].mod == 1234


def test_polled_flag_of_a_software_compare_channel(chip):
    """MSnB:MSnA=01, ELS=00 (software compare): no pin, no interrupt; CHnF still sets at the match."""
    ftm = chip.peripherals["FTM0"]
    w(chip, FTM0 + MOD, 999)
    w(chip, FTM0 + C1V, 400)
    w(chip, FTM0 + C1SC, MSA)
    w(chip, FTM0 + SC, CLK_SYS)
    run_ticks(chip, 390)
    assert not r(chip, FTM0 + C1SC) & CHF and ftm.ch[1].ev is None
    run_ticks(chip, 20)
    assert r(chip, FTM0 + C1SC) & CHF and r(chip, FTM0 + STATUS) & 2
