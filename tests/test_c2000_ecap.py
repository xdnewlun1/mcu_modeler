"""C2833x eCAP (chipem/periph/ti_c2000/ecap.py, SPRUFG4A) on the TMS320F28232 with a spinning C28x core: capture
sequencing (absolute / delta, continuous / one-shot, prescaler, polarity), flags and PIE interrupt, counter overflow,
sync, APWM output on the pin, clock gate, reset.  Edges are injected on GPIO24 (MUX1 = ECAP1) cycle-exactly."""
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.core.c28x.asm import assemble

CHIP = Path(__file__).resolve().parents[1] / "chips" / "tms320f28232.yaml"
EALLOW = 1 << 6
PCLKCR0, PCLKCR1, WDCR, GPAMUX1_HI, GPAMUX2_HI = 0x701C, 0x701D, 0x7029, 0x6F87, 0x6F89
C1, C2 = 0x6A00, 0x6A20
TSCTR, CTRPHS, CAP1, CAP2, CAP3, CAP4 = 0x00, 0x02, 0x04, 0x06, 0x08, 0x0A
ECCTL1, ECCTL2, ECEINT, ECFLG, ECCLR, ECFRC = 0x14, 0x15, 0x16, 0x17, 0x18, 0x19
# ECCTL1
FALL1, RST1, FALL2, RST2, FALL3, RST3, FALL4, RST4 = (1 << i for i in range(8))
CAPLDEN = 1 << 8
# ECCTL2
ONESHOT, REARM, RUN, SYNCI_EN, SWSYNC, APWM, APWMPOL = 1, 1 << 3, 1 << 4, 1 << 5, 1 << 8, 1 << 9, 1 << 10
SYNCO_PASS, SYNCO_PRD, SYNCO_OFF = 0 << 6, 1 << 6, 2 << 6
INT, CEVT1, CEVT2, CEVT3, CEVT4, CTROVF, CTR_PRD, CTR_CMP = (1 << i for i in range(8))


def stop_wrap(n):                       # stop / wrap after capture event n (1..4)
    return (n - 1) << 1


@pytest.fixture
def chip():
    ch = Chip(load_chip(CHIP))
    ch.load_firmware(assemble('''
        .sect "boot", 0x33FFF6
        LB start
        .sect "code", 0x8000
start:  SB start,UNC
''').image())
    ch.reset()
    ch.core.write_reg("st1", ch.core.read_reg("st1") | EALLOW)
    w(ch, WDCR, 0x0068)                                              # watchdog off
    w(ch, PCLKCR1, 0x0F00)                                           # ECAP1-4 clocks
    w(ch, GPAMUX2_HI, 1)                                             # GPIO24 = ECAP1 (GPAMUX2 bits 17:16 = 01)
    ch.pins.set_external("GPIO24", "L")
    return ch


def w(ch, a, v):
    ch.bus.write(a, 1, v)


def w32(ch, a, v):
    ch.bus.write(a, 2, v)


def r(ch, a):
    return ch.bus.read(a, 1)


def r32(ch, a):
    return ch.bus.read(a, 2)


def edge_at(ch, cycle, level, pin="GPIO24"):
    """Run to absolute core cycle `cycle`, then drive the pin."""
    ch.run_cycles(cycle - ch.now_cycles())
    assert ch.now_cycles() == cycle
    ch.pins.set_external(pin, level)


def start(ch, ctl1, ctl2, blk=C1):
    w(ch, blk + ECCTL1, ctl1)
    w(ch, blk + ECCTL2, ctl2)
    return ch.now_cycles()


def caps(ch, blk=C1):
    return [r32(ch, blk + o) for o in (CAP1, CAP2, CAP3, CAP4)]


def test_registers_are_not_eallow_protected_and_reset_values(chip):
    assert r(chip, C1 + ECCTL2) == 0x0006                            # STOP_WRAP = 11 at reset
    chip.core.write_reg("st1", chip.core.read_reg("st1") & ~EALLOW)
    w(chip, C1 + ECCTL1, CAPLDEN | FALL2)
    w32(chip, C1 + CTRPHS, 0x12345678)
    assert r(chip, C1 + ECCTL1) == CAPLDEN | FALL2 and r32(chip, C1 + CTRPHS) == 0x12345678
    assert not [e for e in chip.log if e.kind == "warn" and "EALLOW" in (e.get("msg") or "")]


def test_absolute_capture_rise_fall_continuous(chip):
    t0 = start(chip, CAPLDEN | FALL2 | FALL4, RUN | stop_wrap(4))
    marks = [(t0 + 100, "H"), (t0 + 130, "L"), (t0 + 300, "H"), (t0 + 345, "L")]
    for c, lvl in marks:
        edge_at(chip, c, lvl)
    assert caps(chip) == [100, 130, 300, 345]
    assert r(chip, C1 + ECFLG) == CEVT1 | CEVT2 | CEVT3 | CEVT4      # no INT: nothing enabled
    edge_at(chip, t0 + 500, "H")                                     # wraps: CEVT1 again into CAP1
    assert caps(chip)[0] == 500
    chip.run_cycles(10)
    assert r32(chip, C1 + TSCTR) == 510


def test_wrong_polarity_edges_do_not_advance_the_sequencer(chip):
    t0 = start(chip, CAPLDEN, RUN | stop_wrap(4))                    # every event on rising edges
    edge_at(chip, t0 + 50, "H")
    edge_at(chip, t0 + 60, "L")                                      # falling: ignored
    edge_at(chip, t0 + 80, "H")
    assert caps(chip)[:2] == [50, 80] and r(chip, C1 + ECFLG) == CEVT1 | CEVT2


def test_delta_mode_resets_the_counter_after_each_capture(chip):
    t0 = start(chip, CAPLDEN | FALL2 | FALL4 | RST1 | RST2 | RST3 | RST4, RUN | stop_wrap(4))
    for c, lvl in [(t0 + 100, "H"), (t0 + 175, "L"), (t0 + 300, "H"), (t0 + 320, "L"), (t0 + 1000, "H")]:
        edge_at(chip, c, lvl)
    assert caps(chip) == [680, 75, 125, 20]                          # CAP1 overwritten by the 5th event (t=1000)
    chip.run_cycles(5)
    assert r32(chip, C1 + TSCTR) == 5


def test_one_shot_freezes_after_stop_value_until_rearm(chip):
    t0 = start(chip, CAPLDEN, RUN | ONESHOT | stop_wrap(2))
    for i in range(1, 5):
        edge_at(chip, t0 + 100 * i, "H")
        edge_at(chip, t0 + 100 * i + 10, "L")
    assert caps(chip) == [100, 200, 0, 0]                            # frozen after CEVT2
    assert r(chip, C1 + ECFLG) == CEVT1 | CEVT2
    w(chip, C1 + ECCTL2, RUN | ONESHOT | stop_wrap(2) | REARM)
    assert not r(chip, C1 + ECCTL2) & REARM                          # reads 0
    edge_at(chip, t0 + 1000, "H")
    assert caps(chip) == [1000, 200, 0, 0]                           # Mod4 back at 0: CAP1


def test_continuous_wrap_after_event_two(chip):
    t0 = start(chip, CAPLDEN, RUN | stop_wrap(2))
    for i in range(1, 4):
        edge_at(chip, t0 + 100 * i, "H")
        edge_at(chip, t0 + 100 * i + 10, "L")
    assert caps(chip) == [300, 200, 0, 0]                            # CAP1, CAP2, CAP1 ...


def test_capld_en_clear_sets_flags_without_loading(chip):
    t0 = start(chip, 0, RUN)
    edge_at(chip, t0 + 40, "H")
    assert caps(chip) == [0, 0, 0, 0] and r(chip, C1 + ECFLG) == CEVT1


def test_prescaler_divide_by_two(chip):
    t0 = start(chip, CAPLDEN | (1 << 9), RUN | stop_wrap(4))                       # PRESCALE = 1: /2
    for i in range(1, 9):                                            # input period 100 cycles
        edge_at(chip, t0 + 100 * i, "H")
        edge_at(chip, t0 + 100 * i + 50, "L")
    # PSout toggles at each input rising edge: rising PSout edges at 100, 300, 500, 700
    assert caps(chip) == [100, 300, 500, 700]


def test_prescaler_divide_by_four(chip):
    t0 = start(chip, CAPLDEN | (2 << 9) | FALL2 | FALL4, RUN | stop_wrap(4))       # /4, rise/fall/rise/fall
    for i in range(1, 17):
        edge_at(chip, t0 + 100 * i, "H")
        edge_at(chip, t0 + 100 * i + 50, "L")
    # PSout toggles every 2nd input rising edge: H at 200, L at 400, H at 600, L at 800
    assert caps(chip) == [1000, 1200, 1400, 1600]


def test_interrupt_flag_pie_and_reassert_on_clear(chip):
    pie = chip.arch.pie
    t0 = start(chip, CAPLDEN | FALL2, RUN | stop_wrap(4))
    w(chip, C1 + ECEINT, CEVT2)                                      # interrupt on the falling edge only
    edge_at(chip, t0 + 10, "H")
    assert r(chip, C1 + ECFLG) == CEVT1 and not pie.ifr[4] & 1
    edge_at(chip, t0 + 30, "L")
    assert r(chip, C1 + ECFLG) == CEVT1 | CEVT2 | INT and pie.ifr[4] & 1   # ECAP1_INT = PIE 4.1
    pie.ifr[4] &= ~1                                                 # the PIE flag was taken
    w(chip, C1 + ECCLR, INT)                                         # INT only: CEVT2 still set and enabled
    assert r(chip, C1 + ECFLG) == CEVT1 | CEVT2 | INT and pie.ifr[4] & 1   # another interrupt
    pie.ifr[4] &= ~1
    w(chip, C1 + ECCLR, INT | CEVT1 | CEVT2)
    assert r(chip, C1 + ECFLG) == 0 and not pie.ifr[4] & 1
    assert r(chip, C1 + ECCLR) == 0


def test_force_and_read_only_flags(chip):
    w(chip, C1 + ECFLG, 0xFF)
    assert r(chip, C1 + ECFLG) == 0                                  # read-only
    w(chip, C1 + ECFRC, CTROVF | CEVT3 | INT)                        # bit 0 is reserved in ECFRC
    assert r(chip, C1 + ECFLG) == CTROVF | CEVT3 and r(chip, C1 + ECFRC) == 0
    w(chip, C1 + ECEINT, CTROVF)                                     # enabling a set flag interrupts
    assert r(chip, C1 + ECFLG) & INT and chip.arch.pie.ifr[4] & 1


def test_counter_overflow(chip):
    w32(chip, C1 + TSCTR, 0xFFFFFFF0)
    w(chip, C1 + ECEINT, CTROVF)
    start(chip, 0, RUN)
    chip.run_cycles(15)
    assert r(chip, C1 + ECFLG) == 0 and r32(chip, C1 + TSCTR) == 0xFFFFFFFF
    chip.run_cycles(5)
    assert r(chip, C1 + ECFLG) == CTROVF | INT and r32(chip, C1 + TSCTR) == 4


def test_counter_stop_and_16bit_access(chip):
    start(chip, 0, RUN)
    chip.run_cycles(1000)
    w(chip, C1 + ECCTL2, 0x0006)                                     # TSCTRSTOP = 0: freeze
    v = r32(chip, C1 + TSCTR)
    chip.run_cycles(500)
    assert v == 1000 and r32(chip, C1 + TSCTR) == 1000
    w(chip, C1 + TSCTR + 1, 0x0002)                                  # high word only
    assert r32(chip, C1 + TSCTR) == 0x000203E8
    w(chip, C1 + ECCTL2, RUN)
    chip.run_cycles(16)
    assert r(chip, C1 + TSCTR) == 0x03F8 and r(chip, C1 + TSCTR + 1) == 2


def test_clock_gate_stops_counter_and_captures(chip):
    t0 = start(chip, CAPLDEN, RUN)
    chip.run_cycles(100)
    w(chip, PCLKCR1, 0x0E00)                                         # ECAP1ENCLK off
    edge_at(chip, t0 + 300, "H")
    assert r(chip, C1 + ECFLG) == 0
    assert r32(chip, C1 + TSCTR) == 100
    w(chip, PCLKCR1, 0x0F00)
    chip.run_cycles(50)
    assert r32(chip, C1 + TSCTR) == 150


def test_software_sync_loads_phase_down_the_chain(chip):
    w32(chip, C1 + CTRPHS, 1000)
    w32(chip, C2 + CTRPHS, 5000)
    start(chip, 0, RUN | SYNCO_PASS | SYNCI_EN, blk=C2)
    start(chip, 0, RUN | SYNCO_PASS | SYNCI_EN)
    chip.run_cycles(200)
    w(chip, C1 + ECCTL2, RUN | SYNCO_PASS | SYNCI_EN | SWSYNC)       # eCAP1 SWSYNC, passed on to eCAP2
    assert not r(chip, C1 + ECCTL2) & SWSYNC
    chip.run_cycles(10)
    assert r32(chip, C1 + TSCTR) == 1010 and r32(chip, C2 + TSCTR) == 5010


def test_sync_without_synci_en_does_not_load(chip):
    w32(chip, C1 + CTRPHS, 1000)
    start(chip, 0, RUN | SYNCO_OFF)
    chip.run_cycles(200)
    w(chip, C1 + ECCTL2, RUN | SYNCO_OFF | SWSYNC)
    chip.run_cycles(10)
    assert r32(chip, C1 + TSCTR) == 210


def test_epwm1_syncout_reaches_ecap1(chip):
    e1 = chip.peripherals["EPWM1"]
    if not isinstance(getattr(e1, "sync_listeners", None), list):
        pytest.skip("ePWM model has no sync_listeners yet")
    w32(chip, C1 + CTRPHS, 7)
    start(chip, 0, RUN | SYNCI_EN)
    w(chip, PCLKCR1, 0x0F01)                                         # + EPWM1ENCLK
    w(chip, 0x6800 + 0x05, 99)                                       # TBPRD
    w(chip, 0x6800 + 0x00, 0x0010)                                   # up-count, SYNCOSEL = CTR=ZERO, /1
    chip.run_cycles(50)
    w(chip, PCLKCR0, 1 << 2)                                         # TBCLKSYNC: CTR=ZERO now -> SYNCO
    t_sync = chip.now_cycles()
    chip.run_cycles(130)                                             # second CTR=ZERO at +100
    assert r32(chip, C1 + TSCTR) == 7 + chip.now_cycles() - (t_sync + 100)


def pin_edges(ch, pin="GPIO24"):
    out = []
    ch.pins.subscribe(lambda ev: ev.pin == pin and ev.field == "level" and out.append((ch.now_cycles(), ev.new)))
    return out


def test_apwm_output_period_compare_and_flags(chip):
    chip.pins.set_external("GPIO24", None)
    w32(chip, C1 + CAP1, 99)                                         # written in CAP mode: plain storage
    w(chip, C1 + ECCTL2, APWM | SYNCO_OFF)
    assert chip.pins.get("GPIO24").direction == "out"
    w32(chip, C1 + CAP1, 99)                                         # APWM: active + shadow
    w32(chip, C1 + CAP2, 25)
    assert r32(chip, C1 + CAP3) == 99 and r32(chip, C1 + CAP4) == 25
    assert r(chip, C1 + ECFLG) == CEVT1                              # GPIO24 released to its pull-up: a rise
    w(chip, C1 + ECCLR, 0xFF)
    ev = pin_edges(chip)
    t0 = start(chip, 0, APWM | SYNCO_OFF | RUN)
    chip.run_cycles(320)
    rel = [(c - t0, lvl) for c, lvl in ev]
    assert rel == [(0, "H"), (25, "L"), (100, "H"), (125, "L"), (200, "H"), (225, "L"), (300, "H")]
    assert r(chip, C1 + ECFLG) == CTR_PRD | CTR_CMP
    assert r32(chip, C1 + TSCTR) == 20


def test_apwm_shadow_loads_at_period_and_polarity(chip):
    chip.pins.set_external("GPIO24", None)
    w(chip, C1 + ECCTL2, APWM | SYNCO_OFF)
    w32(chip, C1 + CAP1, 99)
    w32(chip, C1 + CAP2, 50)
    ev = pin_edges(chip)
    t0 = start(chip, 0, APWM | SYNCO_OFF | RUN)
    chip.run_cycles(60)
    w32(chip, C1 + CAP4, 10)                                         # shadow: active from the next period
    assert r32(chip, C1 + CAP2) == 50
    chip.run_cycles(100)
    assert r32(chip, C1 + CAP2) == 10
    w(chip, C1 + ECCTL2, APWM | SYNCO_OFF | RUN | APWMPOL)           # active low from now on
    chip.run_cycles(100)
    rel = [(c - t0, lvl) for c, lvl in ev]
    assert rel[:3] == [(0, "H"), (50, "L"), (100, "H")] and rel[3] == (110, "L")
    assert rel[4] == (160, "H")                                      # polarity flip at the write (inactive -> H)
    assert rel[5:] == [(200, "L"), (210, "H")]


def test_apwm_zero_and_full_duty(chip):
    w(chip, C1 + ECCTL2, APWM | SYNCO_OFF)
    w32(chip, C1 + CAP1, 99)
    w32(chip, C1 + CAP2, 0)
    start(chip, 0, APWM | SYNCO_OFF | RUN)
    chip.run_cycles(450)
    assert chip.pins.get("GPIO24").level() == "L"
    w32(chip, C1 + CAP2, 100)                                        # CMP = PRD + 1: 100 %
    chip.run_cycles(250)
    ev = pin_edges(chip)
    chip.run_cycles(300)
    assert chip.pins.get("GPIO24").level() == "H" and ev == []


def test_mux_after_apwm_start_shows_the_live_level(chip):
    w(chip, GPAMUX2_HI, 0)                                           # GPIO24 back to GPIO
    w(chip, C1 + ECCTL2, APWM | SYNCO_OFF)
    w32(chip, C1 + CAP1, 99)
    w32(chip, C1 + CAP2, 60)
    start(chip, 0, APWM | SYNCO_OFF | RUN)
    chip.run_cycles(30)
    w(chip, GPAMUX2_HI, 1)                                           # now muxed: an output at the live level
    p = chip.pins.get("GPIO24")
    assert p.owner == "ECAP1" and p.direction == "out" and p.level() == "H"
    chip.run_cycles(40)
    assert p.level() == "L"
    w(chip, C1 + ECCTL2, RUN)                                        # back to capture: an input again
    assert p.direction == "in"


def test_chip_reset_cancels_events(chip):
    w(chip, C1 + ECCTL2, APWM | SYNCO_OFF)
    w32(chip, C1 + CAP1, 99)
    w32(chip, C1 + CAP2, 10)
    start(chip, 0, APWM | SYNCO_OFF | RUN)
    chip.run_cycles(150)
    blk = chip.peripherals["ECAP1"]
    chip.reset()
    assert not [e for e in chip.sched.pending() if getattr(e.cb, "__self__", None) is blk]
    assert r(chip, C1 + ECCTL2) == 0x0006 and r32(chip, C1 + TSCTR) == 0
    assert chip.pins.get("GPIO24").owner == "GPIO"
