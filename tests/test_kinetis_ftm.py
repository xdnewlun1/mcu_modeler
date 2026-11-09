"""Kinetis FlexTimer (chipem/periph/kinetis/ftm.py) on the K22F description: counting, PWM / combine / dead time
waveforms on the pins, output compare, flags and their clear sequence, register buffering and synchronization."""
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from engine_util import build_image

K22 = Path(__file__).resolve().parent.parent / "chips" / "mk22fn1m0vll12.yaml"
SIM_SCGC5, SIM_SCGC6, SIM_SCGC3, SIM_CLKDIV1 = 0x40048038, 0x4004803C, 0x40048030, 0x40048044
PORTC = 0x4004B000
FTM0, FTM2, FTM2_ALIAS = 0x40038000, 0x400B8000, 0x4003A000
SC, CNT, MOD, C0SC, C0V, C1SC, C1V = 0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18
CNTIN, STATUS, MODE, SYNC, OUTINIT, OUTMASK, COMBINE, DEADTIME, POL, FMS, CONF, SYNCONF, SWOCTRL = \
    0x4C, 0x50, 0x54, 0x58, 0x5C, 0x60, 0x64, 0x68, 0x70, 0x74, 0x84, 0x8C, 0x94
CLK_SYS, CLK_FF, CPWMS, TOF, TOIE = 1 << 3, 2 << 3, 1 << 5, 1 << 7, 1 << 6
FTMEN, WPDIS, WPEN, SYNCEN0 = 1 << 0, 1 << 2, 1 << 6, 1 << 5
CHF, CHIE, MSB, MSA, ELSB, ELSA = 1 << 7, 1 << 6, 1 << 5, 1 << 4, 1 << 3, 1 << 2
FTM0_IRQ = 42


@pytest.fixture(scope="module")
def desc():
    return load_chip(K22)


def w(chip, a, v):
    chip.bus.write(a, 4, v)


def r(chip, a):
    return chip.bus.read(a, 4)


def freeze_clock(c, hz=120e6):
    """`Chip(clock_hz=120e6)` equals the YAML nominal clock, which leaves the MCG free (FEI, 20.97 MHz after reset).
    Freeze it like `chipem run --clock` does (Session.apply_clock_override): core 120 MHz, bus 60 MHz (OUTDIV2=1)."""
    c.clock_override = float(hz)
    c.peripherals["MCG"].frozen = True
    c.clock.set_hz(float(hz))
    w(c, SIM_CLKDIV1, 1 << 24)


@pytest.fixture
def chip(desc):
    c = Chip(desc, clock_hz=120e6)
    img, _ = build_image({"reset": "spin:\n  b spin"})
    c.load_firmware(img)
    c.reset()
    freeze_clock(c)
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


# ---------------------------------------------------------------- counting rules, flags, re-anchoring (RM chapter 38)
C2SC, C2V = 0x1C, 0x20
PS3 = 3                                                     # /8: 7.5 MHz, a tick is 16 core cycles


def tk(chip):
    return 1e9 / chip.peripherals["FTM0"].counter_hz()


def run_to(chip, t0, n):
    """Run until n FTM ticks after t0 (n + 0.5: in the middle of tick n)."""
    chip.run_for(t0 + n * tk(chip) - chip.now_ns())


def ftm_events(chip, name="FTM0"):
    f = chip.peripherals[name]
    return [e for e in chip.sched.pending() if getattr(e.cb, "__self__", None) is f]


def test_free_running_counter_with_mod_0(chip):
    """FTMEN=0, MOD=0 (reset): 0x0000..0xFFFF, TOF once per 65536 ticks (38.4.3.3); one event per period."""
    ftm = chip.peripherals["FTM0"]
    w(chip, FTM0 + SC, CLK_SYS | PS3)
    t0 = chip.now_ns()
    run_to(chip, t0, 1000.5)
    assert r(chip, FTM0 + CNT) == 1000 and ftm.overflows == 0
    assert len(ftm_events(chip)) == 1
    run_to(chip, t0, 65535.5)
    assert r(chip, FTM0 + CNT) == 0xFFFF and ftm.overflows == 0 and not r(chip, FTM0 + SC) & TOF
    run_to(chip, t0, 65536.5)
    assert r(chip, FTM0 + CNT) == 0 and ftm.overflows == 1 and r(chip, FTM0 + SC) & TOF
    run_to(chip, t0, 2 * 65536 + 0.5)
    assert ftm.overflows == 2


def test_ftmen_1_with_mod_and_cntin_0_stops_the_counter(chip):
    ftm = chip.peripherals["FTM0"]
    w(chip, FTM0 + MODE, WPDIS | FTMEN)
    w(chip, FTM0 + SC, CLK_SYS | PS3)
    t0 = chip.now_ns()
    run_to(chip, t0, 500)
    assert r(chip, FTM0 + CNT) == 0 and ftm.overflows == 0 and not r(chip, FTM0 + SC) & TOF
    assert not ftm_events(chip)
    w(chip, FTM0 + MOD, 99)                                  # a non-zero MOD loads at once and the counter runs
    t1 = chip.now_ns()
    run_to(chip, t1, 150.5)
    assert ftm.overflows == 1 and r(chip, FTM0 + CNT) == 50


def test_signed_counting(chip):
    """CNTIN = 0xFFFC (-4), MOD = 4: -4 .. 4, a 9-tick period (Fig. 38-209)."""
    ftm = chip.peripherals["FTM0"]
    w(chip, FTM0 + CNTIN, 0xFFFC)
    w(chip, FTM0 + MOD, 4)
    w(chip, FTM0 + CNT, 0)                                   # load CNTIN into the counter
    w(chip, FTM0 + SC, CLK_SYS | PS3)
    t0 = chip.now_ns()
    seq = []
    for n in range(12):
        run_to(chip, t0, n + 0.5)
        seq.append(r(chip, FTM0 + CNT))
    assert seq == [0xFFFC, 0xFFFD, 0xFFFE, 0xFFFF, 0, 1, 2, 3, 4, 0xFFFC, 0xFFFD, 0xFFFE]
    run_to(chip, t0, 89.5)
    assert ftm.overflows == 9
    run_to(chip, t0, 90.5)
    assert ftm.overflows == 10


def test_signed_epwm(chip):
    """CNTIN = -4, MOD = 4, C0V = 0: high from -4 to -1 (4 ticks of 9)."""
    w(chip, FTM0 + CNTIN, 0xFFFC)
    w(chip, FTM0 + MOD, 4)
    w(chip, FTM0 + C0V, 0)
    w(chip, FTM0 + C0SC, MSB | ELSB)
    w(chip, FTM0 + CNT, 0)
    e = edges(chip, "PTC1")
    t0 = chip.now_ns()
    w(chip, FTM0 + SC, CLK_SYS | PS3)
    run_to(chip, t0, 20)
    assert [x for x in ticks(chip, e, t0) if x[0] > 0] == [(4, "L"), (9, "H"), (13, "L"), (18, "H")]
    assert r(chip, FTM0 + C0SC) & CHF


def test_cntin_above_mod_wraps_through_ffff(chip):
    """CNTIN = 0x15 > MOD = 5: 0x15 .. 0xFFFF, 0 .. 5 (Fig. 38-211), (5 - 0x15 + 1) mod 2^16 = 65521 ticks."""
    ftm = chip.peripherals["FTM0"]
    w(chip, FTM0 + CNTIN, 0x15)
    w(chip, FTM0 + MOD, 5)
    w(chip, FTM0 + CNT, 0)
    w(chip, FTM0 + SC, CLK_SYS | PS3)
    t0 = chip.now_ns()
    run_to(chip, t0, 65515.5)
    assert r(chip, FTM0 + CNT) == 0 and ftm.overflows == 0
    run_to(chip, t0, 65520.5)
    assert r(chip, FTM0 + CNT) == 5 and ftm.overflows == 0
    run_to(chip, t0, 65521.5)
    assert r(chip, FTM0 + CNT) == 0x15 and ftm.overflows == 1


def test_flags_set_again_between_read_and_write_stay_set(chip):
    """RM SC.TOF / CnSC.CHnF / STATUS: a flag set again after the read is not cleared by the write of 0."""
    w(chip, FTM0 + MOD, 99)
    w(chip, FTM0 + C0V, 20)
    w(chip, FTM0 + C0SC, MSA | CHIE)                         # software compare with interrupt (scheduled)
    w(chip, FTM0 + C1V, 30)
    w(chip, FTM0 + C1SC, MSA)                                # software compare, polled (lazy flag)
    w(chip, FTM0 + SC, CLK_SYS | PS3)
    t0 = chip.now_ns()
    run_to(chip, t0, 150.5)
    assert r(chip, FTM0 + SC) & TOF                          # read 1 (overflow at 100)
    assert r(chip, FTM0 + C0SC) & CHF and r(chip, FTM0 + C1SC) & CHF
    run_to(chip, t0, 250.5)                                  # overflow at 200, matches at 220 / 230
    w(chip, FTM0 + SC, CLK_SYS | PS3)
    w(chip, FTM0 + C0SC, MSA | CHIE)
    w(chip, FTM0 + C1SC, MSA)
    assert r(chip, FTM0 + SC) & TOF and r(chip, FTM0 + C0SC) & CHF and r(chip, FTM0 + C1SC) & CHF
    assert (16 + FTM0_IRQ) in chip.arch.asserted
    w(chip, FTM0 + SC, CLK_SYS | PS3)                        # read above, nothing new: now they clear
    w(chip, FTM0 + C0SC, MSA | CHIE)
    w(chip, FTM0 + C1SC, MSA)
    assert not r(chip, FTM0 + SC) & TOF and not r(chip, FTM0 + C0SC) & CHF and not r(chip, FTM0 + C1SC) & CHF
    assert (16 + FTM0_IRQ) not in chip.arch.asserted
    run_to(chip, t0, 330.5)
    assert r(chip, FTM0 + STATUS) == 3                       # read both
    run_to(chip, t0, 430.5)
    w(chip, FTM0 + STATUS, 0)
    assert r(chip, FTM0 + STATUS) == 3
    w(chip, FTM0 + STATUS, 0)
    assert r(chip, FTM0 + STATUS) == 0


@pytest.mark.parametrize("how", ["clkdiv", "ps", "stop"])
def test_clock_change_does_not_repeat_a_match(chip, how):
    """Re-anchoring after a match (bus clock via SIM_CLKDIV1, SC.PS, CLKS off/on) keeps the counter and the
    fraction of the tick: no second toggle, CHnF not set again, the next match exactly where it belongs."""
    ftm = chip.peripherals["FTM0"]
    w(chip, FTM0 + MOD, 999)
    w(chip, FTM0 + C0V, 300)
    w(chip, FTM0 + C0SC, MSA | ELSA | CHIE)                  # output compare toggle with interrupt
    w(chip, FTM0 + C1V, 300)
    w(chip, FTM0 + C1SC, MSA)                                # quiet software compare at the same count
    e = edges(chip, "PTC1")
    w(chip, FTM0 + SC, CLK_SYS | PS3)
    t0, T = chip.now_ns(), tk(chip)
    run_to(chip, t0, 300.5)
    assert len(e) == 1
    for reg, val in ((C0SC, MSA | ELSA | CHIE), (C1SC, MSA)):
        assert r(chip, FTM0 + reg) & CHF
        w(chip, FTM0 + reg, val)
        assert not r(chip, FTM0 + reg) & CHF
    t1 = chip.now_ns()
    pos = (t1 - t0) / T
    if how == "clkdiv":
        w(chip, SIM_CLKDIV1, 3 << 24)                        # bus 120 / 4 = 30 MHz: the tick doubles
        expect = t1 + (1300 - pos) * 2 * T
    elif how == "ps":
        w(chip, FTM0 + SC, CLK_SYS | (PS3 + 1))
        expect = t1 + (1300 - pos) * 2 * T
    else:
        w(chip, FTM0 + SC, 0)                                # counter stops at 300 ...
        chip.run_for(10 * T)
        assert r(chip, FTM0 + CNT) == 300 and not ftm_events(chip)
        w(chip, FTM0 + SC, CLK_SYS | PS3)                    # ... and continues: 301 one tick later
        t2 = chip.now_ns()
        expect = t2 + 1000 * T
    chip.run_for(0.3 * T)
    assert len(e) == 1 and not r(chip, FTM0 + C0SC) & CHF and not r(chip, FTM0 + C1SC) & CHF
    chip.run_for(expect - chip.now_ns() - 4 * T)
    assert len(e) == 1 and not r(chip, FTM0 + C0SC) & CHF and not r(chip, FTM0 + C1SC) & CHF
    chip.run_for(8 * T)
    assert len(e) == 2 and abs(e[1][0] - expect) < 1.0
    assert r(chip, FTM0 + C0SC) & CHF and r(chip, FTM0 + C1SC) & CHF


def test_mcgffclk_counter_ignores_bus_clock_changes(chip):
    """CLKS=10: the fixed frequency clock (slow IRC 32.768 kHz in FEI); a SIM_CLKDIV1 write does not move it."""
    ftm = chip.peripherals["FTM0"]
    w(chip, FTM0 + MOD, 99)
    w(chip, FTM0 + SC, CLK_FF)
    t0 = chip.now_ns()
    assert ftm.counter_hz() == 32768.0
    run_to(chip, t0, 100.5)
    assert ftm.overflows == 1
    anchor = ftm._start_ns
    w(chip, SIM_CLKDIV1, 3 << 24)
    assert ftm._start_ns == anchor
    run_to(chip, t0, 199.5)
    assert ftm.overflows == 1
    run_to(chip, t0, 200.5)
    assert ftm.overflows == 2


def test_ftmen_1_output_compare_cnv_takes_effect_at_the_next_tick(chip):
    """Table 38-305: FTMEN=1, SYNCEN=0, output compare: CnV is updated at the next counter tick."""
    w(chip, FTM0 + MODE, WPDIS | FTMEN)
    w(chip, FTM0 + MOD, 999)
    w(chip, FTM0 + C0V, 100)
    w(chip, FTM0 + C0SC, MSA | ELSA)
    e = edges(chip, "PTC1")
    w(chip, FTM0 + SC, CLK_SYS | PS3)
    t0 = chip.now_ns()
    run_to(chip, t0, 10.5)
    w(chip, FTM0 + C0V, 11)                                  # counter at 10: 11 is the next count
    run_to(chip, t0, 1020)
    assert ticks(chip, e, t0) == [(11, "H"), (1011, "L")]
    w(chip, FTM0 + COMBINE, SYNCEN0)                         # SYNCEN0: now only by synchronization
    w(chip, FTM0 + C0V, 1500 - 1000)
    run_to(chip, t0, 2020)
    assert ticks(chip, e, t0)[2:] == [(2011, "H")]


def test_cntin_updates_at_once_with_ftmen_0(chip):
    """Table 38-303: CNTIN written while counting (FTMEN=0) applies at once; the counter goes on to MOD."""
    ftm = chip.peripherals["FTM0"]
    w(chip, FTM0 + MOD, 999)
    w(chip, FTM0 + SC, CLK_SYS | PS3)
    t0 = chip.now_ns()
    run_to(chip, t0, 10.5)
    w(chip, FTM0 + CNTIN, 500)
    assert ftm.cntin == 500 and r(chip, FTM0 + CNT) == 10
    run_to(chip, t0, 999.5)
    assert ftm.overflows == 0 and r(chip, FTM0 + CNT) == 999
    run_to(chip, t0, 1000.5)
    assert ftm.overflows == 1 and r(chip, FTM0 + CNT) == 500
    run_to(chip, t0, 1499.5)
    assert ftm.overflows == 1
    run_to(chip, t0, 1500.5)
    assert ftm.overflows == 2


def test_cntin_waits_for_sync_with_ftmen_1_and_cntinc(chip):
    ftm = chip.peripherals["FTM0"]
    w(chip, FTM0 + MODE, WPDIS | FTMEN)
    w(chip, FTM0 + SYNCONF, (1 << 7) | (1 << 2))             # SYNCMODE, CNTINC
    w(chip, FTM0 + MOD, 999)
    w(chip, FTM0 + SC, CLK_SYS | PS3)
    t0 = chip.now_ns()
    run_to(chip, t0, 10.5)
    w(chip, FTM0 + CNTIN, 500)
    run_to(chip, t0, 1200.5)
    assert ftm.cntin == 0 and r(chip, FTM0 + CNT) == 200


def test_epwm_cnv_0_and_cnv_above_mod(chip):
    """38.4.6: CnV = 0 -> 0 %, CnV > MOD -> 100 %; neither sets CHnF."""
    w(chip, FTM0 + MOD, 99)
    w(chip, FTM0 + C0V, 0)
    w(chip, FTM0 + C0SC, MSB | ELSB)
    w(chip, FTM0 + C1V, 200)
    w(chip, FTM0 + C1SC, MSB | ELSB)
    a, b = edges(chip, "PTC1"), edges(chip, "PTC2")
    t0 = chip.now_ns()
    w(chip, FTM0 + SC, CLK_SYS)
    run_ticks(chip, 250)
    assert chip.pins.get("PTC1").level() == "L" and chip.pins.get("PTC2").level() == "H"
    assert not [x for x in ticks(chip, a, t0) + ticks(chip, b, t0) if x[0] > 0]
    assert not r(chip, FTM0 + C0SC) & CHF and not r(chip, FTM0 + C1SC) & CHF


def test_epwm_cntin_above_cnv_is_100_percent(chip):
    """38.4.6 note: CNTIN > CnV -> 100 %; CnV = CNTIN -> 0 %."""
    w(chip, FTM0 + CNTIN, 50)
    w(chip, FTM0 + MOD, 99)
    w(chip, FTM0 + C0V, 10)
    w(chip, FTM0 + C0SC, MSB | ELSB)
    w(chip, FTM0 + CNT, 0)
    e = edges(chip, "PTC1")
    t0 = chip.now_ns()
    w(chip, FTM0 + SC, CLK_SYS | PS3)
    run_to(chip, t0, 120)
    assert chip.pins.get("PTC1").level() == "H" and not [x for x in ticks(chip, e, t0) if x[0] > 0]
    assert not r(chip, FTM0 + C0SC) & CHF
    w(chip, FTM0 + C0V, 50)                                  # buffered: from the next period (tick 150)
    run_to(chip, t0, 220)
    assert [x for x in ticks(chip, e, t0) if x[0] > 0] == [(150, "L")]
    assert not r(chip, FTM0 + C0SC) & CHF


def test_cpwm_cnv_boundary_rules(chip):
    """38.4.7: CnV = 0 or CnV[15] = 1 -> 0 %, CnV >= MOD -> 100 %; no CHnF."""
    w(chip, FTM0 + MOD, 100)
    w(chip, FTM0 + C0V, 0)
    w(chip, FTM0 + C0SC, ELSB)
    w(chip, FTM0 + C1V, 100)
    w(chip, FTM0 + C1SC, ELSB)
    w(chip, FTM0 + C2V, 0x8005)
    w(chip, FTM0 + C2SC, CHIE)                               # ELS = 00: flag only
    a, b = edges(chip, "PTC1"), edges(chip, "PTC2")
    t0 = chip.now_ns()
    w(chip, FTM0 + SC, CPWMS | CLK_SYS)
    run_ticks(chip, 450)
    assert chip.pins.get("PTC1").level() == "L" and chip.pins.get("PTC2").level() == "H"
    assert not [x for x in ticks(chip, a, t0) + ticks(chip, b, t0) if x[0] > 0]
    assert r(chip, FTM0 + STATUS) == 0


def test_numtof(chip):
    """CONF.NUMTOF = 2: TOF at the first overflow, then not for the next 2 (38.3.21 / 38.4.3.5)."""
    w(chip, FTM0 + CONF, 2)
    w(chip, FTM0 + MOD, 99)
    w(chip, FTM0 + CNT, 0)
    w(chip, FTM0 + SC, CLK_SYS | PS3)
    t0 = chip.now_ns()
    seen = []
    for n in range(1, 8):
        run_to(chip, t0, 100 * n + 0.5)
        v = r(chip, FTM0 + SC)
        seen.append(bool(v & TOF))
        if v & TOF:
            w(chip, FTM0 + SC, CLK_SYS | PS3)
    assert seen == [True, False, False, True, False, False, True]


def test_cpwms_switch_while_running_uses_the_live_counter(chip):
    ftm = chip.peripherals["FTM0"]
    w(chip, FTM0 + MOD, 99)
    w(chip, FTM0 + SC, CLK_SYS | PS3)
    t0 = chip.now_ns()
    run_to(chip, t0, 50.5)
    w(chip, FTM0 + SC, CPWMS | CLK_SYS | PS3)
    assert r(chip, FTM0 + CNT) == 50
    run_to(chip, t0, 99.5)
    assert r(chip, FTM0 + CNT) == 99 and ftm.overflows == 0
    run_to(chip, t0, 100.5)
    assert r(chip, FTM0 + CNT) == 98 and ftm.overflows == 1              # TOF at MOD -> MOD-1


def test_chip_reset_cancels_ftm_events(chip):
    ftm = chip.peripherals["FTM0"]
    w(chip, FTM0 + MOD, 99)
    w(chip, FTM0 + C0V, 25)
    w(chip, FTM0 + C0SC, MSB | ELSB | CHIE)
    w(chip, FTM0 + SC, CLK_SYS | TOIE)
    run_ticks(chip, 150)
    assert ftm_events(chip) and (16 + FTM0_IRQ) in chip.arch.asserted
    chip.reset()
    assert not ftm_events(chip) and not ftm.running and (16 + FTM0_IRQ) not in chip.arch.asserted
    assert r(chip, FTM0 + SC) == 0 and ftm.overflows == 1


def test_ftm2_clock_gate_in_scgc6(chip):
    """RM 12.2.13: SIM_SCGC6[FTM2] (bit 26) also gates FTM2 (besides SIM_SCGC3 bit 24)."""
    warns = []
    chip.log.subscribe(lambda ev: ev.kind == "warn" and warns.append(ev))
    w(chip, SIM_SCGC6, r(chip, SIM_SCGC6) | (1 << 26))
    w(chip, FTM2 + MOD, 5)
    assert r(chip, FTM2 + MOD) == 5 and not warns
    w(chip, SIM_SCGC6, r(chip, SIM_SCGC6) & ~(1 << 26))
    w(chip, FTM2 + MOD, 6)
    assert len(warns) == 1


def test_unmuxed_pwm_channel_is_quiet_until_muxed(chip):
    """EPWM on FTM0_CH2 with PTC3 not muxed: no match events; once PORTC_PCR3 selects ALT4 the pin follows."""
    ftm = chip.peripherals["FTM0"]
    w(chip, FTM0 + MOD, 99)
    w(chip, FTM0 + C2V, 25)
    w(chip, FTM0 + C2SC, MSB | ELSB)
    w(chip, FTM0 + SC, CLK_SYS | PS3)
    t0 = chip.now_ns()
    run_to(chip, t0, 150.5)
    assert ftm.ch[2].ev is None and ftm.ch[2] in ftm._quiet
    e = edges(chip, "PTC3")
    w(chip, PORTC + 4 * 3, 4 << 8)                           # PTC3 = ALT4 FTM0_CH2
    assert chip.pins.get("PTC3").level() == "L" and ftm.ch[2].active and ftm.ch[2] not in ftm._quiet
    run_to(chip, t0, 330)
    assert [x for x in ticks(chip, e, t0) if x[0] > 151] == [(200, "H"), (225, "L"), (300, "H"), (325, "L")]
    assert r(chip, FTM0 + C2SC) & CHF


def test_write_protection_bits(chip):
    """MODE.WPDIS is the complement of FMS.WPEN; WPDIS is set by reading WPEN = 1 and then writing WPDIS = 1."""
    assert r(chip, FTM0 + MODE) & WPDIS and not r(chip, FTM0 + FMS) & WPEN
    w(chip, FTM0 + FMS, WPEN)
    assert not r(chip, FTM0 + MODE) & WPDIS
    w(chip, FTM0 + MODE, WPDIS)                              # FMS not read since the write: no effect
    assert not r(chip, FTM0 + MODE) & WPDIS
    assert r(chip, FTM0 + FMS) & WPEN
    w(chip, FTM0 + MODE, WPDIS)
    assert r(chip, FTM0 + MODE) & WPDIS and not r(chip, FTM0 + FMS) & WPEN
