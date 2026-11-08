"""C2833x ePWM (chipem/periph/ti_c2000/epwm.py) on the TMS320F28232 with a spinning C28x core: time base modes,
action qualifier, shadow loads, dead band, event trigger interrupts and ADC SOC, sync chain, trip zone, TBCLKSYNC."""
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.core.c28x.asm import assemble

CHIP = Path(__file__).resolve().parents[1] / "chips" / "tms320f28232.yaml"
EALLOW = 1 << 6
PCLKCR0, PCLKCR1, WDCR, GPAMUX1 = 0x701C, 0x701D, 0x7029, 0x6F86
ADCTRL1, ADCTRL2, ADCMAXCONV, ADCST = 0x7100, 0x7101, 0x7102, 0x7119
E1, E2 = 0x6800, 0x6840
TBCTL, TBSTS, TBPHS, TBCTR, TBPRD, CMPCTL, CMPA, CMPB, AQCTLA, AQCTLB, AQSFRC, AQCSFRC = \
    0x00, 0x01, 0x03, 0x04, 0x05, 0x07, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E
DBCTL, DBRED, DBFED, TZCTL, TZEINT, TZFLG, TZCLR, TZFRC, ETSEL, ETPS, ETFLG, ETCLR = \
    0x0F, 0x10, 0x11, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1C
UP, DOWN, UPDOWN, FREEZE = 0, 1, 2, 3
ZRO_SET, PRD_CLR, CAU_CLR, CAU_SET, CAD_CLR = 2 << 0, 1 << 2, 1 << 4, 2 << 4, 1 << 6


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
    ch.core.write_reg("st1", ch.core.read_reg("st1") | EALLOW)       # debugger-style writes below need EALLOW
    w(ch, WDCR, 0x0068)                                              # watchdog off
    w(ch, PCLKCR1, 0x3F)                                             # EPWM1-6 clocks
    w(ch, GPAMUX1, 0x5)                                              # GPIO0 = EPWM1A, GPIO1 = EPWM1B
    return ch


def w(ch, a, v):
    ch.bus.write(a, 1, v)


def r(ch, a):
    return ch.bus.read(a, 1)


def tbctl(mode, **kw):
    return mode | kw.get("phsen", 0) << 2 | kw.get("prdld", 0) << 3 | kw.get("synco", 3) << 4   # TBCLK = SYSCLKOUT


def edges(ch, pin):
    out = []
    ch.pins.subscribe(lambda ev: ev.pin == pin and ev.field == "level" and out.append((ev.t_ns, ev.new)))
    return out


def cyc(ch, evs, t0):
    hz = ch.clock.hz
    return [(round((t - t0) * hz / 1e9), lvl) for t, lvl in evs]


def start(ch):
    t0 = ch.now_ns()
    w(ch, PCLKCR0, 1 << 2)                                           # TBCLKSYNC: every time base starts now
    return t0


def test_up_count_pwm_and_tbclksync(chip):
    w(chip, E1 + TBPRD, 99)
    w(chip, E1 + CMPA, 25)
    w(chip, E1 + AQCTLA, ZRO_SET | CAU_CLR)
    w(chip, E1 + TBCTL, tbctl(UP))
    chip.run_cycles(500)
    assert r(chip, E1 + TBCTR) == 0                                  # TBCLKSYNC=0: not counting yet
    e = edges(chip, "GPIO0")
    t0 = start(chip)
    chip.run_cycles(330)
    assert cyc(chip, e, t0) == [(0, "H"), (25, "L"), (100, "H"), (125, "L"), (200, "H"), (225, "L"), (300, "H"),
                                (325, "L")]
    assert 0 <= r(chip, E1 + TBCTR) <= 99


def test_up_down_symmetric_with_active_high_complementary_dead_band(chip):
    w(chip, E1 + TBPRD, 100)
    w(chip, E1 + CMPA, 40)
    w(chip, E1 + AQCTLA, CAU_SET | CAD_CLR)
    w(chip, E1 + DBCTL, 3 | (2 << 2))                                # OUT_MODE both, POLSEL AHC (B inverted)
    w(chip, E1 + DBRED, 5)
    w(chip, E1 + DBFED, 5)
    w(chip, E1 + TBCTL, tbctl(UPDOWN))
    a, b = edges(chip, "GPIO0"), edges(chip, "GPIO1")
    t0 = start(chip)
    chip.run_cycles(420)
    ca, cb = cyc(chip, a, t0), cyc(chip, b, t0)
    assert [x for x in ca if x[0] < 200] == [(45, "H"), (160, "L")]   # CAU at 40 (+RED 5), CAD at 160
    assert [x for x in cb if 0 < x[0] < 200] == [(40, "L"), (165, "H")]   # complement, its rise after FED 5
    assert not (chip.pins.get("GPIO0").level() == "H" and chip.pins.get("GPIO1").level() == "H")


def test_cmpa_shadow_loads_at_zero(chip):
    w(chip, E1 + TBPRD, 99)
    w(chip, E1 + CMPA, 25)
    w(chip, E1 + AQCTLA, ZRO_SET | CAU_CLR)
    w(chip, E1 + TBCTL, tbctl(UP))
    e = edges(chip, "GPIO0")
    t0 = start(chip)
    chip.run_cycles(110)
    w(chip, E1 + CMPA, 75)                                           # shadow: active from the next CTR=0
    assert r(chip, E1 + CMPCTL) & (1 << 8)                           # SHDWAFULL
    chip.run_cycles(200)
    falls = [c for c, lvl in cyc(chip, e, t0) if lvl == "L"]
    assert 125 in falls and 275 in falls and 175 not in falls
    assert not r(chip, E1 + CMPCTL) & (1 << 8)


def test_event_trigger_interrupt_every_second_zero(chip):
    w(chip, E1 + TBPRD, 99)
    w(chip, E1 + TBCTL, tbctl(UP))
    w(chip, E1 + ETSEL, (1 << 3) | 1)                                # INTEN, INTSEL = CTR=ZERO
    w(chip, E1 + ETPS, 2)                                            # every 2nd event
    start(chip)
    chip.run_cycles(50)                                              # ZRO #1 at 0
    assert not r(chip, E1 + ETFLG) & 1 and (r(chip, E1 + ETPS) >> 2) & 3 == 1
    chip.run_cycles(100)                                             # ZRO #2 at 100
    assert r(chip, E1 + ETFLG) & 1 and chip.arch.pie.ifr[3] & 1      # EPWM1_INT = PIE 3.1
    w(chip, E1 + ETCLR, 1)
    assert not r(chip, E1 + ETFLG) & 1


def test_soca_starts_the_adc(chip):
    w(chip, PCLKCR0, 1 << 3)                                         # ADC clock (TBCLKSYNC still 0)
    w(chip, ADCTRL1, 1 << 4)                                         # cascaded
    w(chip, ADCTRL2, 1 << 8)                                         # EPWM_SOCA_SEQ1
    w(chip, E1 + TBPRD, 999)
    w(chip, E1 + TBCTL, tbctl(UP))
    w(chip, E1 + ETSEL, (1 << 11) | (2 << 8))                         # SOCAEN, SOCASEL = CTR=PRD
    w(chip, E1 + ETPS, 1 << 8)
    w(chip, PCLKCR0, (1 << 3) | (1 << 2))
    chip.run_cycles(1005)
    assert r(chip, E1 + ETFLG) & (1 << 2)                            # SOCA flag
    chip.run_for(2000)
    assert r(chip, ADCST) & 1                                        # INT_SEQ1: the sequence ran


def test_sync_chain_loads_phase(chip):
    w(chip, E1 + TBPRD, 199)
    w(chip, E1 + TBCTL, tbctl(UP, synco=1))                           # SYNCO at CTR=ZERO
    w(chip, E2 + TBPRD, 199)
    w(chip, E2 + TBPHS, 50)
    w(chip, E2 + TBCTL, tbctl(UP, phsen=1, synco=3))
    start(chip)
    chip.run_cycles(330)
    c1, c2 = r(chip, E1 + TBCTR), r(chip, E2 + TBCTR)
    assert (c2 - c1) % 200 == 50                                     # ePWM2 runs 50 counts ahead of ePWM1


def test_software_trip_forces_output_until_cleared(chip):
    w(chip, E1 + TBPRD, 99)
    w(chip, E1 + CMPA, 50)
    w(chip, E1 + AQCTLA, ZRO_SET | CAU_CLR)
    w(chip, E1 + TBCTL, tbctl(UP))
    w(chip, E1 + TZCTL, 2)                                           # TZA: force low
    w(chip, E1 + TZEINT, 1 << 2)                                     # OST interrupt
    start(chip)
    chip.run_cycles(10)
    assert chip.pins.get("GPIO0").level() == "H"
    w(chip, E1 + TZFRC, 1 << 2)                                      # one-shot trip
    assert chip.pins.get("GPIO0").level() == "L" and r(chip, E1 + TZFLG) & 5 == 5
    assert chip.arch.pie.ifr[2] & 1                                  # EPWM1_TZINT = PIE 2.1
    chip.run_cycles(300)
    assert chip.pins.get("GPIO0").level() == "L"                     # stays tripped across periods
    w(chip, E1 + TZCLR, 5)
    chip.run_cycles(100)
    assert r(chip, E1 + TZFLG) == 0


def test_trip_registers_are_eallow_protected(chip):
    chip.core.write_reg("st1", chip.core.read_reg("st1") & ~EALLOW)
    w(chip, E1 + TZCTL, 2)
    assert r(chip, E1 + TZCTL) == 0


def test_continuous_software_force(chip):
    w(chip, E1 + AQCSFRC, 2 | (1 << 2))                               # A forced high, B forced low
    assert chip.pins.get("GPIO0").level() == "H" and chip.pins.get("GPIO1").level() == "L"
