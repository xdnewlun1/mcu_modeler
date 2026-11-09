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
ADCTRL1, ADCTRL2, ADCMAXCONV, ADCCHSELSEQ1, ADCCHSELSEQ3, ADCASEQSR, ADCTRL3, ADCST = \
    0x7100, 0x7101, 0x7102, 0x7103, 0x7105, 0x7107, 0x7118, 0x7119
ADCRESULT0 = 0x7108
E1, E2 = 0x6800, 0x6840
TBCTL, TBSTS, TBPHS, TBCTR, TBPRD, CMPCTL, CMPA, CMPB, AQCTLA, AQCTLB, AQSFRC, AQCSFRC = \
    0x00, 0x01, 0x03, 0x04, 0x05, 0x07, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E
DBCTL, DBRED, DBFED, TZCTL, TZEINT, TZFLG, TZCLR, TZFRC, ETSEL, ETPS, ETFLG, ETCLR = \
    0x0F, 0x10, 0x11, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1C
PCCTL = 0x1E
UP, DOWN, UPDOWN, FREEZE = 0, 1, 2, 3
ZRO_SET, PRD_CLR, PRD_SET, CAU_CLR, CAU_SET, CAD_CLR = 2 << 0, 1 << 2, 2 << 2, 1 << 4, 2 << 4, 1 << 6


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
    w(chip, ADCTRL3, 0x00E0)                                         # ADC core powered up
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
    w(chip, E1 + AQSFRC, 3 << 6)                                     # RLDCSF = 11: AQCSFRC loads immediately
    w(chip, E1 + AQCSFRC, 2 | (1 << 2))                               # A forced high, B forced low
    assert chip.pins.get("GPIO0").level() == "H" and chip.pins.get("GPIO1").level() == "L"


def warns(ch):
    return [e.get("msg") or "" for e in ch.log if e.kind == "warn"]


def pwm_up(ch, prd=99, cmpa=25, aq=ZRO_SET | CAU_CLR, mode=UP, **kw):
    w(ch, E1 + TBPRD, prd)
    w(ch, E1 + CMPA, cmpa)
    w(ch, E1 + AQCTLA, aq)
    w(ch, E1 + TBCTL, tbctl(mode, **kw))


# ------------------------------------------------------------------ time base changes while running
def test_tbctl_divider_change_while_running_keeps_the_count(chip):
    w(chip, E1 + TBPRD, 999)
    w(chip, E1 + TBCTL, tbctl(UP))
    start(chip)
    chip.run_cycles(300)
    c0 = r(chip, E1 + TBCTR)
    assert 299 <= c0 <= 301
    w(chip, E1 + TBCTL, tbctl(UP) | (2 << 10))                       # CLKDIV = /4 while running
    assert r(chip, E1 + TBCTR) == c0
    chip.run_cycles(400)
    assert abs(r(chip, E1 + TBCTR) - (c0 + 100)) <= 2                # 400 cycles = 100 TBCLK


@pytest.mark.parametrize("mode,prd,run,exp_ctr,exp_up", [(DOWN, 999, 300, 700, 0), (UPDOWN, 500, 700, 300, 0),
                                                         (UP, 999, 300, 300, 1)])
def test_tbctl_freeze_while_running_keeps_count_and_direction(chip, mode, prd, run, exp_ctr, exp_up):
    w(chip, E1 + TBPRD, prd)
    w(chip, E1 + TBCTL, tbctl(mode))
    start(chip)
    chip.run_cycles(run)
    c0 = r(chip, E1 + TBCTR)
    assert abs(c0 - exp_ctr) <= 2
    w(chip, E1 + TBCTL, tbctl(FREEZE))
    assert r(chip, E1 + TBCTR) == c0 and r(chip, E1 + TBSTS) & 1 == exp_up
    chip.run_cycles(200)
    assert r(chip, E1 + TBCTR) == c0                                 # frozen
    w(chip, E1 + TBCTL, tbctl(mode))
    assert r(chip, E1 + TBCTR) == c0
    chip.run_cycles(50)
    assert abs(r(chip, E1 + TBCTR) - (c0 + (50 if exp_up else -50))) <= 2


def test_tbctl_mode_change_up_to_updown_keeps_count(chip):
    w(chip, E1 + TBPRD, 999)
    w(chip, E1 + TBCTL, tbctl(UP))
    start(chip)
    chip.run_cycles(300)
    c0 = r(chip, E1 + TBCTR)
    w(chip, E1 + TBCTL, tbctl(UPDOWN))
    assert r(chip, E1 + TBCTR) == c0 and r(chip, E1 + TBSTS) & 1
    chip.run_cycles(100)
    assert abs(r(chip, E1 + TBCTR) - (c0 + 100)) <= 2


def test_start_via_tbctl_with_tbclksync_already_set(chip):
    w(chip, PCLKCR0, 1 << 2)                                         # TBCLKSYNC first (CTRMODE still freeze)
    w(chip, E1 + TBPRD, 99)
    w(chip, E1 + CMPA, 25)
    w(chip, E1 + AQCTLA, ZRO_SET | CAU_CLR)
    w(chip, E1 + ETSEL, (1 << 3) | 1)                                # INT at CTR=ZERO, every event
    w(chip, E1 + ETPS, 1)
    e = edges(chip, "GPIO0")
    t0 = chip.now_ns()
    w(chip, E1 + TBCTL, tbctl(UP))                                   # the counter starts here, at 0
    chip.run_cycles(230)
    assert cyc(chip, e, t0) == [(0, "H"), (25, "L"), (100, "H"), (125, "L"), (200, "H"), (225, "L")]


def test_down_count_waveform(chip):
    pwm_up(chip, prd=99, cmpa=25, aq=PRD_SET | CAD_CLR, mode=DOWN)
    e = edges(chip, "GPIO0")
    t0 = start(chip)
    chip.run_cycles(330)
    # TBCTR starts at 0 (ZRO), reloads TBPRD at the next TBCLK (PRD: set), CAD at 25 (74 TBCLK later): clear
    assert cyc(chip, e, t0) == [(1, "H"), (75, "L"), (101, "H"), (175, "L"), (201, "H"), (275, "L"), (301, "H")]
    assert r(chip, E1 + TBSTS) & 1 == 0


@pytest.mark.parametrize("cmpa,level,n_edges", [(0, "H", 1), (100, "L", 0)])
def test_up_down_compare_at_the_ends(chip, cmpa, level, n_edges):
    """SPRUG04: CMPA = 0 gives CAU at TBCTR = 0 (no CAD): 100 %; CMPA >= TBPRD gives CAU and CAD at TBPRD, CAD
    (down-count priority) wins: 0 %."""
    pwm_up(chip, prd=100, cmpa=cmpa, aq=CAU_SET | CAD_CLR, mode=UPDOWN)
    e = edges(chip, "GPIO0")
    start(chip)
    chip.run_cycles(650)
    assert len(e) == n_edges and chip.pins.get("GPIO0").level() == level


def test_down_count_compare_above_period_fires_at_tbprd(chip):
    pwm_up(chip, prd=99, cmpa=150, aq=CAD_CLR | ZRO_SET, mode=DOWN)
    e = edges(chip, "GPIO0")
    t0 = start(chip)
    chip.run_cycles(250)
    assert cyc(chip, e, t0) == [(0, "H"), (1, "L"), (100, "H"), (101, "L"), (200, "H"), (201, "L")]


# ------------------------------------------------------------------ shadow loads
def test_cmpa_load_at_period(chip):
    w(chip, E1 + CMPCTL, 1)                                          # LOADAMODE = CTR=PRD
    pwm_up(chip, prd=100, cmpa=40, aq=CAU_SET | CAD_CLR, mode=UPDOWN)
    e = edges(chip, "GPIO0")
    t0 = start(chip)
    chip.run_cycles(50)                                              # after CAU at 40
    w(chip, E1 + CMPA, 20)
    chip.run_cycles(200)
    assert cyc(chip, e, t0)[:3] == [(40, "H"), (180, "L"), (220, "H")]   # loaded at the peak: CAD at 20, not 40


def test_cmpa_load_at_zero_is_the_default(chip):
    pwm_up(chip, prd=100, cmpa=40, aq=CAU_SET | CAD_CLR, mode=UPDOWN)
    e = edges(chip, "GPIO0")
    t0 = start(chip)
    chip.run_cycles(50)
    w(chip, E1 + CMPA, 20)
    chip.run_cycles(200)
    assert cyc(chip, e, t0)[:3] == [(40, "H"), (160, "L"), (220, "H")]


@pytest.mark.parametrize("prdld,rises", [(0, [0, 100, 150, 200]), (1, [0, 51, 101, 151])])
def test_tbprd_shadow_vs_immediate(chip, prdld, rises):
    pwm_up(chip, prd=99, cmpa=25, prdld=prdld)
    e = edges(chip, "GPIO0")
    t0 = start(chip)
    chip.run_cycles(50)
    w(chip, E1 + TBPRD, 49)          # shadow: active from the next CTR=0; PRDLD: now (TBCTR 50 clamped to 49)
    assert r(chip, E1 + TBPRD) == 49
    chip.run_cycles(160)
    assert [c for c, lvl in cyc(chip, e, t0) if lvl == "H"][:4] == rises


# ------------------------------------------------------------------ trip zone
def test_cbc_trip_flag_latched_action_released_at_zero(chip):
    pwm_up(chip, prd=99, cmpa=50)
    w(chip, E1 + TZCTL, 2)                                           # TZA: force low
    start(chip)
    chip.run_cycles(10)
    w(chip, E1 + TZFRC, 1 << 1)                                      # CBC trip
    assert chip.pins.get("GPIO0").level() == "L" and r(chip, E1 + TZFLG) & 2
    chip.run_cycles(100)                                             # past CTR=0 at 100: action released
    assert chip.pins.get("GPIO0").level() == "H"
    assert r(chip, E1 + TZFLG) & 2                                   # ... but the flag stays until TZCLR
    w(chip, E1 + TZCLR, 1 << 1)
    assert not r(chip, E1 + TZFLG) & 2


def test_tzclr_cbc_mid_period_holds_the_trip_until_zero(chip):
    pwm_up(chip, prd=99, cmpa=50)
    w(chip, E1 + TZCTL, 2)
    e = edges(chip, "GPIO0")
    t0 = start(chip)
    chip.run_cycles(10)
    w(chip, E1 + TZFRC, 1 << 1)
    chip.run_cycles(10)
    w(chip, E1 + TZCLR, 1 << 1)                                      # flag cleared mid-period
    assert r(chip, E1 + TZFLG) & 2 == 0 and chip.pins.get("GPIO0").level() == "L"
    chip.run_cycles(100)
    lv = cyc(chip, e, t0)
    assert lv[0] == (0, "H") and lv[1][1] == "L" and lv[2] == (100, "H")


def test_tzclr_int_with_trip_still_set_pulses_again(chip):
    pwm_up(chip, prd=99, cmpa=50)
    w(chip, E1 + TZEINT, 1 << 2)                                     # OST interrupt
    start(chip)
    w(chip, E1 + TZFRC, 1 << 2)
    assert chip.arch.pie.ifr[2] & 1
    chip.arch.pie.ifr[2] &= ~1                                       # the PIE flag was taken
    w(chip, E1 + TZCLR, 1)                                           # INT only: OST still set
    assert r(chip, E1 + TZFLG) == 5 and chip.arch.pie.ifr[2] & 1     # another TZINT pulse
    chip.arch.pie.ifr[2] &= ~1
    w(chip, E1 + TZCLR, 5)                                           # INT + OST: no more
    assert r(chip, E1 + TZFLG) == 0 and not chip.arch.pie.ifr[2] & 1


# ------------------------------------------------------------------ action qualifier software force
@pytest.mark.parametrize("rldcsf,at", [(0, 100), (1, 99), (2, 99)])
def test_continuous_software_force_reload_mode(chip, rldcsf, at):
    pwm_up(chip, prd=99, cmpa=50, aq=0)
    w(chip, E1 + AQSFRC, rldcsf << 6)
    e = edges(chip, "GPIO0")
    t0 = start(chip)
    chip.run_cycles(10)
    w(chip, E1 + AQCSFRC, 2)                                         # A: force high, from the shadow
    assert chip.pins.get("GPIO0").level() == "L"
    chip.run_cycles(150)
    assert cyc(chip, e, t0) == [(at, "H")]
    w(chip, E1 + AQSFRC, 3 << 6)
    w(chip, E1 + AQCSFRC, 0)                                         # immediate: AQ output (L) again
    assert chip.pins.get("GPIO0").level() == "L"


# ------------------------------------------------------------------ event trigger
@pytest.mark.parametrize("intprd", [1, 2, 3])
def test_intcnt_saturates_at_intprd(chip, intprd):
    w(chip, E1 + TBPRD, 99)
    w(chip, E1 + TBCTL, tbctl(UP))
    w(chip, E1 + ETSEL, (1 << 3) | 1)
    w(chip, E1 + ETPS, intprd)
    start(chip)
    chip.run_cycles(100 * (2 * intprd + 2) + 50)                     # many events, the flag stays set
    assert r(chip, E1 + ETFLG) & 1
    assert (r(chip, E1 + ETPS) >> 2) & 3 == intprd


def test_socb_starts_seq2_in_dual_mode(chip):
    adc = chip.peripherals["ADC"]
    adc.set_channel("ADCINB2", 0x456)
    w(chip, PCLKCR0, 1 << 3)
    w(chip, ADCTRL3, 0x00E0)
    w(chip, ADCTRL1, 0)                                              # dual sequencers
    w(chip, ADCCHSELSEQ3, 0xA)                                       # CONV08 = B2
    w(chip, ADCTRL2, 1 | (1 << 3))                                   # EPWM_SOCB_SEQ2, INT_ENA_SEQ2
    w(chip, E1 + TBPRD, 999)
    w(chip, E1 + TBCTL, tbctl(UP))
    w(chip, E1 + ETSEL, (1 << 15) | (1 << 12))                        # SOCBEN, SOCBSEL = CTR=ZERO
    w(chip, E1 + ETPS, 1 << 12)
    w(chip, PCLKCR0, (1 << 3) | (1 << 2))
    chip.run_for(2000)
    assert r(chip, E1 + ETFLG) & (1 << 3)
    st = r(chip, ADCST)
    assert st & 2 and not st & 1                                     # INT_SEQ2 only
    assert r(chip, ADCRESULT0 + 8) == 0x456 << 4
    assert chip.arch.pie.ifr[1] & 2                                  # SEQ2INT = PIE 1.2


def test_epwm_soc_ignored_while_adc_powered_down(chip):
    w(chip, PCLKCR0, 1 << 3)
    w(chip, ADCTRL1, 1 << 4)
    w(chip, ADCTRL2, 1 << 8)                                         # EPWM_SOCA_SEQ1, ADCTRL3 still 0
    w(chip, E1 + TBPRD, 99)
    w(chip, E1 + TBCTL, tbctl(UP))
    w(chip, E1 + ETSEL, (1 << 11) | (1 << 8))
    w(chip, E1 + ETPS, 1 << 8)
    w(chip, PCLKCR0, (1 << 3) | (1 << 2))
    chip.run_cycles(350)
    assert r(chip, E1 + ETFLG) & (1 << 2)                            # the ePWM side pulses
    assert chip.peripherals["ADC"].conversions == 0 and not r(chip, ADCST) & 1
    assert sum("SOCA ignored" in m for m in warns(chip)) == 1


def test_adc_start_stop_continues_and_holds_one_pending_soc(chip):
    adc = chip.peripherals["ADC"]
    for i in range(4):
        adc.set_channel(f"A{i}", 0x100 * (i + 1))
    w(chip, PCLKCR0, 1 << 3)
    w(chip, ADCTRL1, 1 << 4)                                         # cascaded, start/stop mode
    w(chip, ADCMAXCONV, 0)                                           # one conversion per SOC
    w(chip, ADCCHSELSEQ1, 0x3210)                                    # CONV00-03 = A0-A3
    w(chip, ADCTRL2, 1 << 13)                                        # SOC 1: CONV00
    w(chip, ADCTRL2, 1 << 13)                                        # busy: held pending
    assert r(chip, ADCTRL2) & (1 << 13)
    w(chip, ADCTRL2, 1 << 13)                                        # busy and pending: lost
    chip.run_for(1000)
    assert adc.conversions == 2 and adc.soc_lost == 1 and not r(chip, ADCTRL2) & (1 << 13)
    assert [r(chip, ADCRESULT0 + k) >> 4 for k in range(3)] == [0x100, 0x200, 0]   # continued at CONV01
    assert r(chip, ADCASEQSR) & 0xF == 2
    w(chip, ADCTRL2, 1 << 14)                                        # RST_SEQ1: back to CONV00
    assert r(chip, ADCASEQSR) & 0xF == 0
    adc.set_channel("A0", 0x7)
    w(chip, ADCTRL2, 1 << 13)
    chip.run_for(1000)
    assert r(chip, ADCRESULT0) >> 4 == 0x7


# ------------------------------------------------------------------ reset, pins, warnings
def test_chip_reset_while_running_cancels_events(chip):
    pwm_up(chip, prd=99, cmpa=25)
    w(chip, E1 + DBCTL, 3)
    w(chip, E1 + DBRED, 5)
    e = edges(chip, "GPIO0")
    start(chip)
    chip.run_cycles(150)
    assert len(e) >= 3
    ep = chip.peripherals["EPWM1"]
    chip.reset()
    assert ep._ev is None and ep._red_ev is None and ep._fed_ev is None and not ep.running
    n = len(e)
    chip.run_cycles(500)
    assert len(e) == n and r(chip, E1 + TBCTR) == 0


def test_pin_mux_applied_after_the_pwm_runs(chip):
    w(chip, GPAMUX1, 0)                                              # GPIO0/1 plain GPIO
    pwm_up(chip, prd=99, cmpa=50)
    start(chip)
    chip.run_cycles(10)                                              # EPWM1A high (ZRO set) but not on a pin
    assert chip.pins.get("GPIO0").level() != "H"
    w(chip, GPAMUX1, 0x5)
    assert chip.pins.get("GPIO0").level() == "H"                     # the current output level
    e = edges(chip, "GPIO0")
    chip.run_cycles(100)
    assert [lvl for _, lvl in e] == ["L", "H"]


def test_chopper_warns_once(chip):
    w(chip, E1 + PCCTL, 1)
    w(chip, E1 + PCCTL, 1)
    assert sum("chopper" in m for m in warns(chip)) == 1


def test_tbprd_zero_with_events_warns_once(chip):
    w(chip, E1 + TBPRD, 0)
    w(chip, E1 + AQCTLA, ZRO_SET | PRD_CLR)
    w(chip, E1 + TBCTL, tbctl(UP))
    start(chip)
    chip.run_cycles(200)
    assert sum("TBPRD = 0" in m for m in warns(chip)) == 1
