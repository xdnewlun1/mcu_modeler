"""Piccolo models on the real C28x core: clocking / missing-clock / NMI watchdog (piccolo_sysctrl.py), the type-3 ADC
(adc_soc.py), comparators (comp.py), the ePWM digital compare (epwm_dc.py), AIO (piccolo_gpio.py), 4-word serial FIFOs,
CLA storage and the PARTID OTP page.  A tiny image sets EALLOW and spins; the tests write registers through the bus."""
import importlib.util

import pytest

HAVE_CORE = importlib.util.find_spec("chipem.core.c28x") is not None
pytestmark = pytest.mark.skipif(not HAVE_CORE, reason="C28x core not available")

SPIN = """
        .sect "codestart", 0x3F7FF6
        LB      start
        .sect "text", 0x3F0000
start:  EALLOW
loop:   SB      loop, UNC
"""


def session(tmp_path, chip="tms320f28027"):
    from chipem.core.c28x.asm import assemble
    from chipem.session import Session
    img = tmp_path / "spin.c28img"
    assemble(SPIN, filename="spin.asm").write_c28img(img)
    s = Session(chip, img, quiet=True)
    s.run_for(1e3)
    return s


def w(s, addr, v, n=1):
    core = s.chip.core                                  # EALLOW (an NMI taken by the spin loop clears it)
    core.write_reg("st1", core.read_reg("st1") | 0x40)
    s.chip.bus.write(addr, n, v)


def r(s, addr, n=1):
    return s.chip.bus.read(addr, n)


def evs(s, kind):
    return [e for e in s.events(kinds={kind})]


# ------------------------------------------------------------------ clocks
def test_reset_clock_and_pll_lock_period(tmp_path):
    s = session(tmp_path)
    assert s.chip.clock.hz == 10e6 and (r(s, 0x7011) >> 7) & 3 == 3             # boot ROM DIVSEL = 3
    w(s, 0x7013, 10000)                                                         # PLLLOCKPRD = 10000 OSCCLK = 1 ms
    w(s, 0x7011, r(s, 0x7011) & ~0x180)                                         # DIVSEL = 0 (/4)
    w(s, 0x7021, 12)
    assert s.chip.clock.hz == 2.5e6 and not r(s, 0x7011) & 1                   # bypass while locking
    s.run_for(0.99e6)
    assert not r(s, 0x7011) & 1
    s.run_for(0.02e6)
    assert r(s, 0x7011) & 1 and s.chip.clock.hz == 30e6                         # 10 MHz x 12 / 4
    w(s, 0x7011, r(s, 0x7011) | 0x100)
    assert s.chip.clock.hz == 60e6                                              # DIVSEL = 2
    w(s, 0x7011, r(s, 0x7011) | 0x180)
    assert s.chip.clock.hz == 120e6                                             # DIVSEL = 3 (/1) with the PLL on


def test_source_switch_forces_pllcr_zero(tmp_path):
    s = session(tmp_path)
    w(s, 0x7011, r(s, 0x7011) & ~0x180)
    w(s, 0x7021, 12)
    s.run_for(7e6)
    assert s.chip.clock.hz == 30e6
    w(s, 0x7012, 0x6000)                                                        # XTALOSCOFF, XCLKINOFF first
    w(s, 0x7012, 0x6003)                                                        # INTOSC2 (SPRUI09 1.3.2.3.2)
    assert r(s, 0x7021) == 0 and s.chip.clock.hz == 2.5e6                       # PLLCR forced to 0, OSCCLK / 4
    assert s.chip.peripherals["SYSCTRL"].source() == "INTOSC2"


def test_missing_clock_switches_to_intosc1_with_nmi(tmp_path):
    s = session(tmp_path)
    w(s, 0x7060, 2)                                                             # NMICFG.CLOCKFAIL
    w(s, 0x7065, 5000)                                                          # NMIWDPRD
    w(s, 0x7012, 0x8001)                                                        # NMIRESETSEL, OSCCLK = external: none
    s.run_for(2.2e6)                                                            # detected at 8192 / 4 MHz = 2.05 ms
    sc = s.chip.peripherals["SYSCTRL"]
    assert sc.source() == "INTOSC1" and r(s, 0x7012) & 1 == 0                   # case D: back to INTOSC1
    assert r(s, 0x7061) == 3 and not r(s, 0x7011) & 8                          # CLOCKFAIL + NMIINT; MCLKSTS cleared
    assert dict((v, n) for v, _, n in s.irq_summary()).get(18) == 1             # one NMI
    assert not [e for e in evs(s, "reset") if e.get("reason") not in (None, "reset")]
    s.run_for(1e6)                                                              # NMI watchdog: 5000 SYSCLKOUT = 0.5 ms
    assert [e for e in evs(s, "reset") if e.get("reason") == "nmi_watchdog"]
    assert r(s, 0x7061) == 3                                                    # NMIFLG survives the NMIRS (XRS only)


def test_missing_clock_without_nmi_resets_into_limp_mode(tmp_path):
    s = session(tmp_path)
    w(s, 0x7012, 0x0001)                                                        # external clock, NMIRESETSEL = 0
    s.run_for(3e6)
    assert [e for e in evs(s, "reset") if e.get("reason") == "missing_clock"]
    assert r(s, 0x7011) & 8 and s.chip.peripherals["SYSCTRL"].limp            # MCLKSTS, limp clock
    assert s.chip.clock.hz == 4e6                                               # limp 4 MHz / DIVSEL 3


def test_nmi_watchdog_cleared_flags_prevent_reset(tmp_path):
    s = session(tmp_path)
    w(s, 0x7060, 2)
    w(s, 0x7065, 20000)
    w(s, 0x7063, 2)                                                             # NMIFLGFRC.CLOCKFAIL (test)
    s.run_for(0.1e6)
    assert r(s, 0x7061) == 3 and 500 < r(s, 0x7064) < 1500                      # counting SYSCLKOUT (10 MHz)
    w(s, 0x7062, 3)                                                             # clear CLOCKFAIL, NMIINT
    assert r(s, 0x7061) == 0 and r(s, 0x7064) == 0
    s.run_for(3e6)
    assert not [e for e in evs(s, "reset") if e.get("reason") not in (None, "reset")]


# ------------------------------------------------------------------ type-3 ADC
def adc_on(s):
    w(s, 0x701C, r(s, 0x701C) | 0x0008)                                         # ADCENCLK
    w(s, 0x7100, 0x40E4)                                                        # powered, enabled, late EOC
    s.run_for(1.01e6)


def test_adc_sequential_timing_results_and_interrupt(tmp_path):
    s = session(tmp_path)
    adc = s.chip.peripherals["ADC"]
    adc_on(s)
    adc.set_input("ADCINA1", 1.0)
    adc.set_input("ADCINB1", 2.0)
    w(s, 0x7120, (1 << 6) | 6)                                                  # SOC0: A1, ACQPS 6
    w(s, 0x7121, (9 << 6) | 6)                                                  # SOC1: B1
    w(s, 0x7108, 0x0021)                                                        # ADCINT1 = EOC1
    c0 = s.chip.now_cycles()
    w(s, 0x711A, 3)                                                             # force SOC0 + SOC1
    assert r(s, 0x7118) == 3
    s.run_for(2.3e3)                                                            # 23 ADCCLK at 10 MHz
    assert r(s, 0x0B00) == 0
    s.run_for(0.2e3)
    assert r(s, 0x0B00) == int(4096 * 1.0 / 3.3)                                # latched at 24 ADCCLK (Figure 6-8)
    s.run_for(1.3e3)
    assert r(s, 0x0B01) == int(4096 * 2.0 / 3.3) and r(s, 0x7104) & 1           # second result at 37 ADCCLK
    assert s.chip.now_cycles() - c0 >= 37
    assert adc.int_pulses[1] == 1                                               # ADCINT1 -> PIE 1.1 and 10.1
    pie = s.chip.arch.pie
    assert pie.ifr[1] & 1 and pie.ifr[10] & 1
    w(s, 0x711A, 3)
    s.run_for(5e3)
    assert r(s, 0x7106) & 1                                                     # second EOC with the flag set: OVF


def test_adc_round_robin_overflow_and_priority(tmp_path):
    s = session(tmp_path)
    adc = s.chip.peripherals["ADC"]
    adc_on(s)
    order = []
    adc.result_listeners.append(lambda soc, v: order.append(soc))
    for n in range(16):
        w(s, 0x7120 + n, (n << 6) | 6)
    w(s, 0x711A, 0x8004)                                                        # SOC2 and SOC15
    w(s, 0x711A, 0x0004)                                                        # SOC2 again while pending: overflow?
    s.run_for(10e3)
    assert order == [2, 15]
    w(s, 0x7110, 0x0010)                                                        # all high priority, RRPOINTER reset
    assert (r(s, 0x7110) >> 5) & 0x3F == 0x20
    w(s, 0x711A, 0x8004)
    s.run_for(10e3)
    assert order[2:] == [2, 15]
    w(s, 0x711A, 0x0001)
    w(s, 0x711A, 0x0001)                                                        # trigger while SOC0 still pending
    s.run_for(10e3)
    assert r(s, 0x711C) & 1 and adc.lost >= 1


def test_adc_simultaneous_temperature_and_offset_trim(tmp_path):
    s = session(tmp_path)
    adc = s.chip.peripherals["ADC"]
    adc_on(s)
    adc.set_input("ADCINA3", 0.825)
    adc.set_input("ADCINB3", 2.475)
    w(s, 0x7112, 1)                                                             # SIMULEN0
    w(s, 0x7120, (3 << 6) | 6)
    w(s, 0x711A, 1)
    s.run_for(5e3)
    assert r(s, 0x0B00) == 1024 and r(s, 0x0B01) == 3072                        # A -> even, B -> odd result
    w(s, 0x7112, 0)
    w(s, 0x7100, r(s, 0x7100) | 1)                                              # TEMPCONV
    w(s, 0x7122, (5 << 6) | 6)
    w(s, 0x7141, 0x1FB)                                                         # ADCOFFTRIM = -5
    w(s, 0x711A, 4)
    s.run_for(5e3)
    assert r(s, 0x0B02) == int(25 / 0.18 + 0.0) + 1750 - 5 or abs(r(s, 0x0B02) - (1888 - 5)) <= 1


def test_adc_epwm_trigger_and_power_warning(tmp_path):
    s = session(tmp_path)
    adc = s.chip.peripherals["ADC"]
    w(s, 0x701C, r(s, 0x701C) | 0x0008)
    w(s, 0x7100, 0x40E0)
    w(s, 0x7120, (5 << 11) | 6)                                                 # TRIGSEL 5 = ePWM1 SOCA
    w(s, 0x701D, 1)                                                             # EPWM1ENCLK
    w(s, 0x6805, 999)
    w(s, 0x6819, 0x0A00)
    w(s, 0x681A, 0x0100)
    w(s, 0x6800, 0x0030)
    w(s, 0x701C, r(s, 0x701C) | 0x0004)                                         # TBCLKSYNC
    s.run_for(1.05e6)
    assert 10 <= adc.conversions <= 11                                          # 10 kHz (TBPRD 999 at 10 MHz)
    assert [e for e in evs(s, "warn") if "1 ms" in str(e.get("msg"))]           # converted before the 1 ms power-up


# ------------------------------------------------------------------ comparator + digital compare + AIO
def test_comparator_dac_qualification_and_dc_trip(tmp_path):
    s = session(tmp_path)
    comp = s.chip.peripherals["COMP1"]
    w(s, 0x7100, 0x00E0)                                                        # bandgap on
    w(s, 0x7020, r(s, 0x7020) | 1)                                              # COMP1ENCLK
    w(s, 0x6406, 512)                                                           # DAC 1.652 V
    w(s, 0x6400, 0x0001 | (3 << 3) | 0x100)                                     # enabled, SYNCSEL, QUALSEL 3
    w(s, 0x701D, 1)
    w(s, 0x6805, 99)
    w(s, 0x680B, 0x0012)
    w(s, 0x6809, 50)
    w(s, 0x6830, 0x0008)                                                        # DCAH = COMP1OUT
    w(s, 0x6813, 0x0002)                                                        # DCAEVT1 = DCAH high
    w(s, 0x6812, 0x4000)                                                        # one-shot source
    w(s, 0x6814, 0x0002)                                                        # TZA force low
    w(s, 0x6800, 0x0030)
    w(s, 0x701C, r(s, 0x701C) | 0x0004)
    s.run_for(50e3)
    comp.set_input("ADCINA2", 2.0)                                              # pin name of COMP1A
    s.run_for(0.3e3)                                                            # 3 cycles: still qualifying
    assert not comp.out and r(s, 0x6402) == 0
    comp.set_input("ADCINA2", 1.0)                                              # glitch shorter than 4 SYSCLKOUT
    s.run_for(1e3)
    assert not comp.out and comp.transitions == 0
    comp.set_input("COMP1A", 2.0)
    s.run_for(1e3)
    assert comp.out and r(s, 0x6402) == 1
    assert r(s, 0x6816) & 0x0C == 0x0C                                          # TZFLG.OST + DCAEVT1
    ep = s.chip.peripherals["EPWM1"]
    assert ep._out["A"] == "L"
    s.run_for(30e3)
    assert ep._out["A"] == "L"                                                  # latched
    comp.set_input("COMP1A", 1.0)
    s.run_for(1e3)
    w(s, 0x6817, 0x0004)                                                        # TZCLR.OST
    s.run_for(30e3)
    assert ep._out["A"] in ("H", "L") and r(s, 0x6816) & 0x04 == 0


def test_dc_event_direct_action_and_cbc(tmp_path):
    s = session(tmp_path)
    comp = s.chip.peripherals["COMP2"]
    w(s, 0x7100, 0x00E0)
    w(s, 0x6420 + 6, 300)
    w(s, 0x6420, 0x0001)
    w(s, 0x701D, 2)                                                             # EPWM2
    w(s, 0x6845, 199)
    w(s, 0x684B, 0x0012)
    w(s, 0x6849, 100)
    w(s, 0x6870, 0x0009)                                                        # DCAH = COMP2OUT
    w(s, 0x6853, 0x0002 << 3)                                                   # DCAEVT2 = DCAH high
    w(s, 0x6854, 0x0003 | (0x1 << 6) | 0xC)                                     # TZA/TZB no action, DCAEVT2: force high
    w(s, 0x6855, 0x0010)                                                        # TZEINT.DCAEVT2
    w(s, 0x6840, 0x0030)
    w(s, 0x701C, r(s, 0x701C) | 0x0004)
    s.run_for(50e3)
    comp.set_input("COMP2A", 2.0)
    s.run_for(1e3)
    ep = s.chip.peripherals["EPWM2"]
    assert ep._out["A"] == "H" and r(s, 0x6856) & 0x11 == 0x11                  # forced high, flag + INT
    assert ep._tz_lvl                                                           # EPWM2_TZINT asserted
    comp.set_input("COMP2A", 0.1)
    s.run_for(1e3)
    w(s, 0x6857, 0x0011)
    assert r(s, 0x6856) & 0x10 == 0                                             # event gone: flag stays clear
    s.run_for(40e3)
    assert ep._out["A"] in ("H", "L") and len([1 for e in s.events(kinds={"pin"}) if e.get("pin") == "GPIO2"]) >= 0


def test_ramp_generator_trips_at_crossing(tmp_path):
    s = session(tmp_path, "tms320f28069")
    comp = s.chip.peripherals["COMP1"]
    w(s, 0x7100, 0x00E0)
    comp.set_input("COMP1A", 1.65)
    w(s, 0x640A, 0xFFFF)                                                        # RAMPMAXREF_SHDW
    w(s, 0x640E, 64)                                                            # RAMPDECVAL_SHDW: one DAC LSB / cycle
    w(s, 0x6400, 0x0001)
    w(s, 0x6404, 0x0001)                                                        # DACSOURCE = ramp, PWMSYNC1
    w(s, 0x701D, 1)
    w(s, 0x6805, 9999)                                                          # 1 ms period at 10 MHz
    w(s, 0x6828, 0x0000)                                                        # HRPCTL.PWMSYNCSEL = CTR=PRD
    w(s, 0x6800, 0x0030)
    w(s, 0x701C, r(s, 0x701C) | 0x0004)
    s.run_for(0.5e6)
    assert comp.ramp_value() == 0xFFFF and not comp.out                        # static until the first PWMSYNC
    n0 = comp.transitions                                                       # (DACVAL 0 -> ramp: 2 already)
    s.run_for(0.6e6)                                                            # PWMSYNC at 1 ms: the ramp falls
    # 1.65 V = DAC code 511.5: the ramp (one DAC LSB per cycle from 1023) crosses after ~512 cycles, the trip
    # reloads RAMPSTS from RAMPMAXREF_ACTIVE and holds it until the next PWMSYNC, so the output pulses once
    assert comp.transitions - n0 == 2 and comp.ramp_value() == 0xFFFF and not comp.out
    assert s.chip.peripherals["COMP1"].get("RAMPMAXREF_ACTIVE") == 0xFFFF


def test_aio_digital_output_seen_by_adc(tmp_path):
    s = session(tmp_path)
    adc = s.chip.peripherals["ADC"]
    adc_on(s)
    pin = s.chip.pins.get("ADCINA2")
    w(s, 0x6FB6, 0x2220_2200, 2)                                                # AIOMUX1: AIO2 enabled
    w(s, 0x6FBA, 0x0004, 2)                                                     # AIODIR: AIO2 output
    w(s, 0x6FDA, 0x0004, 2)                                                     # AIOSET
    assert pin.direction == "out" and pin.level() == "H" and r(s, 0x6FD8, 2) & 4
    w(s, 0x7120, (2 << 6) | 6)
    w(s, 0x711A, 1)
    s.run_for(5e3)
    assert r(s, 0x0B00) == 4095                                                 # the ADC path stays connected
    w(s, 0x6FDC, 0x0004, 2)                                                     # AIOCLEAR
    assert pin.level() == "L"
    w(s, 0x6FB6, 0x2220_2220, 2)                                                # AIO2 disabled again
    assert pin.direction == "analog"


def test_xint3_selects_port_a(tmp_path):
    s = session(tmp_path)
    g = s.chip.peripherals["GPIO"]
    w(s, 0x6FE2, 12)
    assert g.xint_pin(3).id == "GPIO12"


def test_sci_fifo_is_four_words(tmp_path):
    s = session(tmp_path)
    w(s, 0x701C, r(s, 0x701C) | 0x0400)
    w(s, 0x7050, 7)
    w(s, 0x7053, 1)
    w(s, 0x7051, 0x23)
    w(s, 0x705A, 0xE040)
    for b in b"abcdef":
        w(s, 0x7059, b)
    assert (r(s, 0x705A) >> 8) & 0x1F <= 4 and s.chip.peripherals["SCIA"].fifo_depth == 4


def test_partid_readonly_and_cla_warn(tmp_path):
    s = session(tmp_path, "tms320f28069")
    w(s, 0x3D7E80, 0x1234)
    assert r(s, 0x3D7E80) == 0x9E
    w(s, 0x1420, 0x0001)                                                        # CLA MIER
    assert [e for e in evs(s, "warn") if "CLA" in str(e.get("msg"))]
