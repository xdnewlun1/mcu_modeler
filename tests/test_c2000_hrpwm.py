"""HRPWM (micro edge positioner) of the C2833x/2823x ePWM (chipem/periph/ti_c2000/epwm.py; SPRUI07 ch. 4) on the
TMS320F28232: CMPAHR / TBPHSHR / HRCNFG, MEP delay of output A edges, shadow loads (HRLOAD), the 3-cycle inactive
zone, exact edge bookkeeping and the instances without HRPWM (ePWM5/6)."""
import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.core.c28x.asm import assemble
from tests.test_c2000_epwm import (AQCTLA, CAU_CLR, CHIP, CMPA, CMPB, CMPCTL, E1, EALLOW, GPAMUX1, PCLKCR1, TBCTL,
                                   TBPRD, UP, WDCR, ZRO_SET, cyc, edges, r, start, tbctl, w)

TBPHSHR, CMPAHR, HRCNFG = 0x02, 0x08, 0x20
E5 = 0x6900
FE, RE, BE = 2, 1, 3
CTLMODE, HRLOAD = 1 << 2, 1 << 3
SHDWAMODE = 1 << 4


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
    ch.peripherals["SYSCTRL"].frozen = True                     # as --clock: SYSCLKOUT = 100 MHz, 10 ns per cycle
    ch.clock.set_hz(100e6)
    ch.core.write_reg("st1", ch.core.read_reg("st1") | EALLOW)
    w(ch, WDCR, 0x0068)
    w(ch, PCLKCR1, 0x3F)
    w(ch, GPAMUX1, 0x5)                                          # GPIO0 = EPWM1A
    return ch


def setup_pwm(ch, cmpa=40, prd=99, hr=0, mode=FE, shadow=True):
    w(ch, E1 + TBPRD, prd)
    w(ch, E1 + CMPCTL, 0 if shadow else SHDWAMODE)
    w(ch, E1 + CMPA, cmpa)
    w(ch, E1 + CMPAHR, hr << 8)
    w(ch, E1 + AQCTLA, ZRO_SET | CAU_CLR)
    w(ch, E1 + HRCNFG, mode)
    w(ch, E1 + TBCTL, tbctl(UP))


def test_registers_and_reset(chip):
    w(chip, E1 + CMPAHR, 0x16FF)
    assert r(chip, E1 + CMPAHR) == 0x1600                    # bits 7:0 reserved, read 0
    w(chip, E1 + TBPHSHR, 0x80AB)
    assert r(chip, E1 + TBPHSHR) == 0x8000
    w(chip, E1 + HRCNFG, 0xFFFF)
    assert r(chip, E1 + HRCNFG) == 0x000F
    chip.reset()
    assert r(chip, E1 + HRCNFG) == 0 and r(chip, E1 + CMPAHR) == 0
    assert chip.peripherals["EPWM1"].hr_state()["edgmode"] == 0


def test_hrcnfg_is_eallow_protected(chip):
    chip.core.write_reg("st1", chip.core.read_reg("st1") & ~(1 << 6))
    w(chip, E1 + HRCNFG, FE)
    assert r(chip, E1 + HRCNFG) == 0
    w(chip, E1 + CMPAHR, 0x1200)                             # CMPAHR is not protected
    assert r(chip, E1 + CMPAHR) == 0x1200


def test_falling_edge_mep_delay_and_exact_duty(chip):
    # TI's example (SPRUI07 Table 4-4 with 150 ps): CMPA = 40, CMPAHR = 40 steps -> fall at 400 + 6 ns
    ep = chip.peripherals["EPWM1"]
    setup_pwm(chip, cmpa=40, hr=40)
    e = edges(chip, "GPIO0")
    t0 = start(chip)
    chip.run_cycles(450)
    st = ep.hr_state()
    assert st["steps"] == 40 and st["offset_ns"] == pytest.approx(6.0)
    # 6 ns rounds to the nearest SYSCLKOUT edge: one cycle later than the coarse edge at count 40
    assert cyc(chip, e, t0)[:4] == [(0, "H"), (41, "L"), (100, "H"), (141, "L")]
    assert ep.hr_period_ns == pytest.approx(1000.0)
    assert ep.hr_high_ns == pytest.approx(406.0)
    assert ep.hr_duty == pytest.approx(0.406)
    assert any(ev.kind == "info" and "HRPWM falling" in ev.get("msg", "") for ev in chip.log)


def test_small_delay_stays_on_coarse_edge(chip):
    ep = chip.peripherals["EPWM1"]
    setup_pwm(chip, cmpa=40, hr=20)                          # 3 ns: nearest cycle edge is the coarse one
    e = edges(chip, "GPIO0")
    t0 = start(chip)
    chip.run_cycles(250)
    assert cyc(chip, e, t0)[:2] == [(0, "H"), (40, "L")]
    assert ep.hr_high_ns == pytest.approx(403.0)


def test_rising_edge_mode_leaves_falling_edge(chip):
    ep = chip.peripherals["EPWM1"]
    w(chip, E1 + TBPRD, 99)
    w(chip, E1 + CMPA, 50)
    w(chip, E1 + CMPAHR, 100 << 8)                           # 15 ns
    w(chip, E1 + AQCTLA, 2 << 4 | 1 << 0)                    # CAU set, ZRO clear
    w(chip, E1 + HRCNFG, RE)
    w(chip, E1 + TBCTL, tbctl(UP))
    e = edges(chip, "GPIO0")
    t0 = start(chip)
    chip.run_cycles(260)
    assert cyc(chip, e, t0)[:4] == [(52, "H"), (100, "L"), (152, "H"), (200, "L")]
    assert ep.hr_high_ns == pytest.approx(485.0)
    assert any(ev.kind == "warn" and "exceeds one SYSCLKOUT" in ev.get("msg", "") for ev in chip.log)


def test_cmpahr_shadow_load_at_zero_and_immediate_mode(chip):
    ep = chip.peripherals["EPWM1"]
    setup_pwm(chip, cmpa=40, hr=10)
    start(chip)
    chip.run_cycles(150)                                     # mid period 2
    w(chip, E1 + CMPAHR, 50 << 8)
    assert ep.hr_state()["steps"] == 10 and ep.hr_state()["cmpahr_pending"]
    assert r(chip, E1 + CMPAHR) == 50 << 8                   # reads return the shadow
    chip.run_cycles(60)                                      # past CTR=ZERO at 200
    assert ep.hr_state()["steps"] == 50 and not ep.hr_state()["cmpahr_pending"]
    w(chip, E1 + CMPCTL, SHDWAMODE)
    w(chip, E1 + CMPAHR, 7 << 8)
    assert ep.hr_state()["steps"] == 7


def test_hrload_ctr_prd(chip):
    ep = chip.peripherals["EPWM1"]
    setup_pwm(chip, cmpa=40, hr=10, mode=FE | HRLOAD)
    start(chip)
    chip.run_cycles(20)
    w(chip, E1 + CMPAHR, 30 << 8)
    chip.run_cycles(60)                                      # count ~80: no load yet
    assert ep.hr_state()["steps"] == 10
    chip.run_cycles(25)                                      # CTR=PRD at 99
    assert ep.hr_state()["steps"] == 30


def test_inactive_zone_first_three_cycles(chip):
    ep = chip.peripherals["EPWM1"]
    setup_pwm(chip, cmpa=2, hr=60)                           # falling edge at count 2: MEP inactive
    e = edges(chip, "GPIO0")
    t0 = start(chip)
    chip.run_cycles(150)
    assert cyc(chip, e, t0)[:2] == [(0, "H"), (2, "L")]
    assert ep.hr_high_ns == pytest.approx(20.0)
    assert any(ev.kind == "warn" and "inactive" in ev.get("msg", "") for ev in chip.log)


def test_phase_control_mode_delays_both_edges(chip):
    ep = chip.peripherals["EPWM1"]
    w(chip, E1 + TBPHSHR, 67 << 8)                           # 10.05 ns -> one cycle
    setup_pwm(chip, cmpa=40, hr=0, mode=BE | CTLMODE)
    e = edges(chip, "GPIO0")
    t0 = start(chip)
    chip.run_cycles(250)
    # phase control: the 3-cycle inactive zone is a duty-control (CMPAHR) limit, the rise at CTR=ZERO moves too
    assert cyc(chip, e, t0)[:4] == [(1, "H"), (41, "L"), (101, "H"), (141, "L")]
    assert ep.hr_high_ns == pytest.approx(400.0)
    assert ep.hr_period_ns == pytest.approx(1000.0)


def test_disabled_mep_and_cmpahr_zero(chip):
    setup_pwm(chip, cmpa=40, hr=0)                           # EDGMODE on but CMPAHR = 0: no MEP
    e = edges(chip, "GPIO0")
    t0 = start(chip)
    chip.run_cycles(150)
    assert cyc(chip, e, t0)[:2] == [(0, "H"), (40, "L")]
    w(chip, E1 + HRCNFG, 0)
    w(chip, E1 + CMPAHR, 90 << 8)
    chip.run_cycles(200)
    assert all(c % 100 in (0, 40) for c, _ in cyc(chip, e, t0))


def test_pulse_shorter_than_delay_vanishes(chip):
    w(chip, E1 + TBPRD, 99)
    w(chip, E1 + CMPA, 50)
    w(chip, E1 + CMPAHR, 200 << 8)                           # 30 ns rising-edge delay
    # set at CAU (50), clear at CBU (51): a one-cycle pulse, its delayed rising edge is overtaken
    w(chip, E1 + CMPB, 51)
    w(chip, E1 + AQCTLA, 2 << 4 | 1 << 8)
    w(chip, E1 + HRCNFG, RE)
    w(chip, E1 + TBCTL, tbctl(UP))
    e = edges(chip, "GPIO0")
    start(chip)
    chip.run_cycles(250)
    assert e == []


def test_epwm5_has_no_hrpwm(chip):
    ep5 = chip.peripherals["EPWM5"]
    assert not ep5.hr
    w(chip, E5 + HRCNFG, FE)
    w(chip, E5 + CMPAHR, 0x40FF)
    assert r(chip, E5 + CMPAHR) == 0x40FF                    # reserved location: plain storage
    assert ep5.hr_state()["hrpwm"] is False and ep5.hr_offset_ns() == 0.0


def test_mep_step_param(chip):
    ep = chip.peripherals["EPWM1"]
    assert ep.mep_step_ps == 150.0                           # chip YAML params.mep_step_ps
    ep.mep_step_ps = 310.0                                   # SPRS439 maximum
    setup_pwm(chip, cmpa=40, hr=20)
    assert ep.hr_offset_ns() == pytest.approx(6.2)


def test_32bit_cmpa_cmpahr_write(chip):
    # TI's run-time code: MOVL *XAR3,ACC writes [CMPA:CMPAHR] in one access (SPRUI07 Example 4-7)
    ep = chip.peripherals["EPWM1"]
    w(chip, E1 + CMPCTL, SHDWAMODE)
    chip.bus.write(E1 + CMPAHR, 2, (32 << 16) | (23 << 8) | 0x80)
    assert r(chip, E1 + CMPA) == 32 and r(chip, E1 + CMPAHR) == 23 << 8
    assert ep.cmpa == 32 and ep.hr_state()["steps"] == 23
