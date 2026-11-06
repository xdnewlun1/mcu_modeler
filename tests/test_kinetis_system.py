"""Unit tests for the Kinetis K22 system models (SIM, PORT, GPIO, WDOG, MCG/OSC, SMC/PMC/RCM/LLWU, VBAT,
FTFE, ADC) on the real chip description, driven through the bus (and keystone-assembled firmware for the
watchdog)."""
from __future__ import annotations

from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from engine_util import build_image

ROOT = Path(__file__).resolve().parent.parent
K22 = ROOT / "chips" / "mk22fn1m0vll12.yaml"

SIM, MCG, OSC = 0x40047000, 0x40064000, 0x40065000
SCGC3, SCGC5, SCGC6, CLKDIV1 = SIM + 0x1030, SIM + 0x1038, SIM + 0x103C, SIM + 0x1044
PORTA, PORTC, PORTD = 0x40049000, 0x4004B000, 0x4004C000
GPIOC = 0x400FF080
WDOG = 0x40052000
ADC0, ADC1 = 0x4003B000, 0x400BB000
FTFE = 0x40020000
SMC, PMC, RCM, LLWU, VBAT = 0x4007E000, 0x4007D000, 0x4007F000, 0x4007C000, 0x4003E000
PORTC_IRQ = 61


@pytest.fixture(scope="module")
def desc():
    return load_chip(K22)


@pytest.fixture
def chip(desc):
    return Chip(desc)


def msgs(chip, kind):
    return [e.fields.get("msg", "") for e in chip.log if e.kind == kind]


def w(chip, addr, value, size=4):
    chip.bus.write(addr, size, value)


def r(chip, addr, size=4):
    return chip.bus.read(addr, size)


def advance_ns(chip, ns):
    chip.clock.advance(chip.clock.ns_to_cycles(ns))
    chip.sched.run_due()


# ---------------------------------------------------------------------------------------------- SIM
def test_sim_reset_values_and_gates(chip):
    sim = chip.peripherals["SIM"]
    assert r(chip, SIM + 0x0) == 0x80000000           # SOPT1
    assert r(chip, SIM + 0x1004) == 0x00001000        # SOPT2
    assert r(chip, SCGC5) == 0x00040182
    assert r(chip, SCGC6) == 0x40000001
    assert r(chip, CLKDIV1) == 0x00010000
    assert r(chip, SIM + 0x1024) == 0x300              # SDID (SVD reset value), read-only
    w(chip, SIM + 0x1024, 0x1234)
    assert r(chip, SIM + 0x1024) == 0x300
    assert not sim.clock_enabled("PORTC") and sim.clock_enabled("FTFE")
    assert not sim.clock_enabled("UART4") and not sim.clock_enabled("ADC1")
    w(chip, SCGC5, r(chip, SCGC5) | (1 << 11))
    assert sim.clock_enabled("PORTC") and not sim.clock_enabled("PORTD")
    w(chip, SCGC3, 1 << 27)
    assert sim.clock_enabled("ADC1")
    assert sim.clock_enabled("NOT_A_GATE")
    assert sim.outdiv(1) == 1 and sim.outdiv(4) == 2


def test_sim_gate_warning_once(chip):
    r(chip, PORTD)
    r(chip, PORTD + 4)
    w(chip, PORTD, 0x100)
    warns = [m for m in msgs(chip, "warn") if "clock gate" in m]
    assert len(warns) == 1 and "PORTD" in warns[0]
    assert not chip.peripherals["SIM"].check_gate("PORTD")      # still off, no second warning
    assert len([m for m in msgs(chip, "warn") if "clock gate" in m]) == 1
    w(chip, SCGC5, r(chip, SCGC5) | (1 << 12))
    assert chip.peripherals["SIM"].check_gate("PORTD")


# ---------------------------------------------------------------------------------------------- MCG/OSC
def test_mcg_reset_fei(chip):
    mcg = chip.peripherals["MCG"]
    assert r(chip, MCG + 6, 1) == 0x10                  # S: IREFST
    assert mcg.mode == "FEI"
    assert mcg.core_hz == pytest.approx(32768 * 640)
    assert chip.clock.hz == pytest.approx(20_971_520)
    assert mcg.bus_hz == pytest.approx(20_971_520)       # OUTDIV2 = 0 after reset


def test_mcg_fei_dmx32_and_status_mirror(chip):
    w(chip, MCG + 0, 0x04, 1)                            # C1: IREFS (Withings boot)
    assert r(chip, MCG + 6, 1) & 0x10
    w(chip, MCG + 3, 0x80 | (r(chip, MCG + 3, 1) & 0x1F), 1)   # C4: DMX32=1, DRS=0
    assert chip.clock.hz == pytest.approx(32768 * 732)   # 23.99 MHz
    w(chip, MCG + 3, 0xA0, 1)                            # DRS=1, DMX32 -> 48 MHz
    assert chip.clock.hz == pytest.approx(32768 * 1464)
    assert any("core clock 47.972 MHz (FEI)" in m for m in msgs(chip, "info"))
    w(chip, MCG + 1, 0x01, 1)                            # C2.IRCS
    assert r(chip, MCG + 6, 1) & 0x01


def test_mcg_pee_120mhz(chip):
    mcg = chip.peripherals["MCG"]
    w(chip, CLKDIV1, 0x01140000)                         # core /1, bus /2, flexbus /2, flash /5
    w(chip, MCG + 1, 0x24, 1)                            # C2: RANGE0=2, EREFS0
    assert r(chip, MCG + 6, 1) & 0x02                    # OSCINIT0
    w(chip, MCG + 0, 0x98, 1)                            # C1: CLKS=2 (ext), FRDIV=3, IREFS=0 -> FBE
    s = r(chip, MCG + 6, 1)
    assert (s >> 2) & 3 == 2 and not s & 0x10
    assert mcg.mode == "FBE" and chip.clock.hz == pytest.approx(8e6)
    w(chip, MCG + 4, 0x01, 1)                            # C5: PRDIV0=1 -> 4 MHz ref
    w(chip, MCG + 5, 0x40 | 6, 1)                        # C6: PLLS, VDIV0=6 -> x30 = 120 MHz (PBE)
    s = r(chip, MCG + 6, 1)
    assert s & 0x20 and s & 0x40                         # PLLST, LOCK0
    assert mcg.mode == "PBE"
    w(chip, MCG + 0, 0x18, 1)                            # CLKS=0 -> PEE
    assert (r(chip, MCG + 6, 1) >> 2) & 3 == 3
    assert mcg.mode == "PEE"
    assert chip.clock.hz == pytest.approx(120e6)
    assert mcg.bus_hz == pytest.approx(60e6) and mcg.bus_hz_effective == pytest.approx(60e6)
    w(chip, MCG + 0, 0x58, 1)                            # CLKS=1: internal reference (slow IRC) -> FBI
    assert mcg.mode == "FBI" and chip.clock.hz == pytest.approx(32768)
    w(chip, MCG + 1, 0x25, 1)                            # fast IRC 4 MHz / 2^FCRDIV (reset 1)
    assert chip.clock.hz == pytest.approx(2e6)


def test_mcg_ext_hz_param_and_frozen_clock(desc):
    chip = Chip(desc, clock_hz=50e6)                     # explicit override -> frozen
    mcg = chip.peripherals["MCG"]
    assert mcg.frozen and chip.clock.hz == 50e6
    w(chip, MCG + 3, 0xA0, 1)
    assert chip.clock.hz == 50e6 and mcg.core_hz == pytest.approx(32768 * 1464)
    mcg.ext_hz = 16e6
    w(chip, MCG + 1, 0x24, 1)
    w(chip, MCG + 0, 0x80, 1)
    assert mcg.core_hz == pytest.approx(16e6)


def test_mcg_respects_chip_clock_override(chip):
    chip.clock_override = 120e6                          # what Session sets for --clock
    chip.clock.set_hz(120e6)
    w(chip, MCG + 3, 0xA0, 1)
    assert chip.peripherals["MCG"].frozen and chip.clock.hz == 120e6
    assert any("frozen" in m for m in msgs(chip, "info"))


def test_osc_register(chip):
    w(chip, OSC, 0x80, 1)
    assert r(chip, OSC, 1) == 0x80


# ---------------------------------------------------------------------------------------------- PORT / GPIO
def enable_portc(chip):
    w(chip, SCGC5, r(chip, SCGC5) | (1 << 11))


def test_port_reset_values_and_owners(chip):
    assert r(chip, PORTA + 0) == 0x742 and r(chip, PORTA + 4) == 0x743 and r(chip, PORTA + 0x14) == 0x41
    pins = chip.pins
    assert pins.get("PTA0").owner == "JTAG_TCLK/SWD_CLK"
    assert pins.get("PTC6").owner == "CMP0_IN0" and pins.get("PTC6").direction == "analog"
    assert pins.get("PTC5").owner == "disabled"


def test_port_pcr_mux_and_pull(chip):
    enable_portc(chip)
    pins = chip.pins
    w(chip, PORTC + 4 * 6, 0x103)                        # MUX=1, PE, PS
    p6 = pins.get("PTC6")
    assert p6.owner == "GPIO" and p6.pull == "up" and p6.direction == "in" and p6.level() == "H"
    w(chip, PORTC + 4 * 6, 0x102)                        # pull-down
    assert p6.pull == "down" and p6.level() == "L"
    w(chip, PORTC + 4 * 6, 0x100)
    assert p6.pull is None and p6.level() == "X"
    w(chip, PORTC + 4 * 6, 0x200)                        # ALT2 = SPI0_SOUT
    assert p6.owner == "SPI0_SOUT"
    w(chip, PORTC + 4 * 6, 0x000)                        # ALT0 = analog CMP0_IN0
    assert p6.owner == "CMP0_IN0" and p6.direction == "analog"
    assert r(chip, PORTC + 4 * 6) == 0
    assert any("PTC6 → GPIO" in m for m in msgs(chip, "info"))
    assert any("PTC6 → SPI0_SOUT" in m for m in msgs(chip, "info"))


def test_port_lock_and_gpclr(chip):
    enable_portc(chip)
    w(chip, PORTC + 0x80, (1 << 21) | (1 << 20) | 0x0103)   # GPCLR: pins 4 and 5 <- 0x103
    assert r(chip, PORTC + 0x10) == 0x103 and r(chip, PORTC + 0x14) == 0x103
    assert r(chip, PORTC + 0x80) == 0                    # write-only
    assert chip.pins.get("PTC5").owner == "GPIO" and chip.pins.get("PTC4").pull == "up"
    w(chip, PORTC + 0x84, (1 << (16 + 0)) | 0x0100)     # GPCHR: pin 16
    assert r(chip, PORTC + 0x40) == 0x100
    w(chip, PORTC + 0x14, 0x8100)                        # LK
    w(chip, PORTC + 0x14, 0x0200)
    assert r(chip, PORTC + 0x14) == 0x8100


def test_gpio_output_and_mux_gating(chip):
    enable_portc(chip)
    pins = chip.pins
    p5 = pins.get("PTC5")
    w(chip, GPIOC + 0x14, 1 << 5)                        # PDDR before mux: nothing on the pin yet
    w(chip, GPIOC + 0x04, 1 << 5)                        # PSOR
    assert p5.owner == "disabled" and p5.direction == "none"
    assert r(chip, GPIOC + 0x00) == 1 << 5               # PDOR updated anyway
    w(chip, PORTC + 0x14, 0x140)                         # MUX=1, DSE -> output high immediately
    assert p5.owner == "GPIO" and p5.direction == "out" and p5.level() == "H"
    w(chip, GPIOC + 0x08, 1 << 5)                        # PCOR
    assert p5.level() == "L"
    w(chip, GPIOC + 0x0C, 1 << 5)                        # PTOR
    assert p5.level() == "H" and r(chip, GPIOC + 0x10) & (1 << 5)    # PDIR reads back the output
    assert r(chip, GPIOC + 0x04) == 0 and r(chip, GPIOC + 0x08) == 0 and r(chip, GPIOC + 0x0C) == 0
    w(chip, GPIOC + 0x00, 0)                             # PDOR direct
    assert p5.level() == "L"
    w(chip, GPIOC + 0x14, 0)                             # input
    assert p5.direction == "in" and p5.drive is None
    w(chip, PORTC + 0x14, 0x200)                         # mux away: GPIO data no longer reaches the pin
    w(chip, GPIOC + 0x14, 1 << 5)
    w(chip, GPIOC + 0x04, 1 << 5)
    assert p5.owner == "SPI0_SCK" and p5.direction == "out" and p5.drive == "L"   # signal table: SCK idles low


def test_gpio_pdir(chip):
    enable_portc(chip)
    pins = chip.pins
    assert r(chip, GPIOC + 0x10) == 0                    # nothing muxed to GPIO
    pins.set_external("PTC7", "H")
    assert r(chip, GPIOC + 0x10) == 0                    # PTC7 not GPIO-muxed -> 0
    w(chip, PORTC + 4 * 7, 0x100)
    assert r(chip, GPIOC + 0x10) == 1 << 7
    pins.set_external("PTC7", "L")
    assert r(chip, GPIOC + 0x10) == 0
    pins.set_external("PTC7", None)                      # floating
    assert r(chip, GPIOC + 0x10) == 0
    assert r(chip, GPIOC + 0x10) == 0
    floating = [m for m in msgs(chip, "warn") if "floating" in m]
    assert len(floating) == 1 and "PTC7" in floating[0]
    w(chip, PORTC + 4 * 7, 0x103)
    assert r(chip, GPIOC + 0x10) == 1 << 7               # pull-up


def test_port_edge_interrupt(chip):
    enable_portc(chip)
    pins = chip.pins
    port = chip.peripherals["PORTC"]
    w(chip, PORTC + 4 * 6, 0x103 | (10 << 16) | (1 << 24))   # falling edge, pull-up
    exc = 16 + PORTC_IRQ
    assert r(chip, PORTC + 0xA0) == 0 and exc not in chip.arch.pending
    pins.set_external("PTC6", "H")                       # no edge (already H through the pull-up)
    assert r(chip, PORTC + 0xA0) == 0
    pins.set_external("PTC6", "L")
    assert r(chip, PORTC + 0xA0) == 1 << 6 and r(chip, PORTC + 4 * 6) & (1 << 24)
    assert exc in chip.arch.pending
    pins.set_external("PTC6", "H")                       # rising: no new flag
    w(chip, PORTC + 0xA0, 1 << 6)                        # w1c via ISFR
    assert r(chip, PORTC + 0xA0) == 0 and not r(chip, PORTC + 4 * 6) & (1 << 24)
    assert exc not in chip.arch.asserted and not port.irq_line()
    assert exc in chip.arch.pending                      # NVIC pending stays latched until taken / ICPR
    chip.arch.pending.discard(exc)
    pins.set_external("PTC6", "L")
    assert r(chip, PORTC + 0xA0) == 1 << 6
    w(chip, PORTC + 4 * 6, r(chip, PORTC + 4 * 6))      # w1c via PCR.ISF
    assert r(chip, PORTC + 0xA0) == 0


def test_port_edge_modes_and_dma_flags(chip):
    enable_portc(chip)
    pins = chip.pins
    exc = 16 + PORTC_IRQ
    w(chip, PORTC + 4 * 8, 0x100 | (11 << 16))           # either edge
    pins.set_external("PTC8", "H")                       # X -> H counts as rising
    assert r(chip, PORTC + 0xA0) == 1 << 8
    w(chip, PORTC + 0xA0, 0xFFFFFFFF)
    pins.set_external("PTC8", "L")
    assert r(chip, PORTC + 0xA0) == 1 << 8
    w(chip, PORTC + 0xA0, 0xFFFFFFFF)
    chip.arch.pending.discard(exc)
    w(chip, PORTC + 4 * 9, 0x100 | (1 << 16))            # DMA on rising: flag only, no IRQ
    pins.set_external("PTC9", "L")
    pins.set_external("PTC9", "H")
    assert r(chip, PORTC + 0xA0) == 1 << 9 and exc not in chip.arch.pending
    w(chip, PORTC + 0xA0, 1 << 9)
    w(chip, PORTC + 4 * 10, 0x000 | (9 << 16))           # MUX=0 (analog): no interrupts
    pins.set_external("PTC10", "L")
    pins.set_external("PTC10", "H")
    assert r(chip, PORTC + 0xA0) == 0


def test_port_level_interrupt(chip):
    w(chip, SCGC5, r(chip, SCGC5) | (1 << 9))
    pins = chip.pins
    exc = 16 + 59
    pins.set_external("PTA13", "H")
    w(chip, PORTA + 4 * 13, 0x100 | (8 << 16))          # interrupt when logic 0 (Withings PTA13)
    assert r(chip, PORTA + 0xA0) == 0
    pins.set_external("PTA13", "L")
    assert r(chip, PORTA + 0xA0) == 1 << 13 and exc in chip.arch.pending
    w(chip, PORTA + 0xA0, 1 << 13)                       # still low -> flag comes straight back
    assert r(chip, PORTA + 0xA0) == 1 << 13
    pins.set_external("PTA13", "H")
    w(chip, PORTA + 0xA0, 1 << 13)
    assert r(chip, PORTA + 0xA0) == 0 and exc not in chip.arch.asserted
    assert exc in chip.arch.pending                      # NVIC pending stays latched until taken / ICPR
    w(chip, PORTA + 4 * 13, 0x100 | (12 << 16))          # logic 1 while high: immediately flagged
    assert r(chip, PORTA + 0xA0) == 1 << 13


def test_port_gpio_reset_resyncs_pins(chip):
    enable_portc(chip)
    w(chip, PORTC + 0x14, 0x140)
    w(chip, GPIOC + 0x14, 1 << 5)
    w(chip, GPIOC + 0x04, 1 << 5)
    assert chip.pins.get("PTC5").level() == "H"
    for blk in chip.peripherals.values():
        blk.reset()
    p5 = chip.pins.get("PTC5")
    assert p5.owner == "disabled" and p5.direction == "none" and p5.drive is None
    assert r(chip, GPIOC + 0x14) == 0 and r(chip, PORTC + 0x14) == 0


# ---------------------------------------------------------------------------------------------- WDOG
def test_wdog_reset_values_and_timeout_math(chip):
    wd = chip.peripherals["WDOG"]
    assert r(chip, WDOG + 0, 2) == 0x01D3 and r(chip, WDOG + 4, 2) == 0x004C and r(chip, WDOG + 6, 2) == 0x4B4C
    assert r(chip, WDOG + 0x16, 2) == 0x0400
    assert wd.enabled and wd.toval == 0x004C4B4C == 5_000_012 and wd.prescaler == 5
    # CLKSRC=1: bus clock = FEI 20.97152 MHz / (OUTDIV2+1 = 1)
    assert wd.timeout_s == pytest.approx(5_000_012 * 5 / 20_971_520)
    assert wd.timeout_s == pytest.approx(1.192, abs=1e-3)


def test_wdog_locked_writes_ignored(chip):
    w(chip, WDOG + 0, 0x01D2, 2)
    assert r(chip, WDOG + 0, 2) == 0x01D3
    assert any("ignored" in m and "STCTRLH" in m for m in msgs(chip, "warn"))
    w(chip, WDOG + 0xE, 0xC520, 2)
    w(chip, WDOG + 0xE, 0xD928, 2)
    w(chip, WDOG + 0, 0x01D2, 2)
    assert r(chip, WDOG + 0, 2) == 0x01D2
    assert not chip.peripherals["WDOG"].enabled
    assert "WDOG disabled" in msgs(chip, "info")


def test_wdog_allowupdate_clear_locks_forever(chip):
    w(chip, WDOG + 0xE, 0xC520, 2)
    w(chip, WDOG + 0xE, 0xD928, 2)
    w(chip, WDOG + 0, 0x01C1, 2)                         # ALLOWUPDATE=0, LPO, enabled
    w(chip, WDOG + 0xE, 0xC520, 2)
    w(chip, WDOG + 0xE, 0xD928, 2)
    w(chip, WDOG + 0, 0x01C0, 2)
    assert r(chip, WDOG + 0, 2) == 0x01C1


def test_wdog_bus_clock_change_rebases_count(chip):
    wd = chip.peripherals["WDOG"]
    t_full = wd.timeout_s * 1e9
    advance_ns(chip, t_full / 2)
    assert wd.counter() == pytest.approx(wd.toval / 2, rel=1e-3)
    w(chip, MCG + 3, 0x20, 1)                            # DRS=1: bus 20.97 -> 41.94 MHz
    ev = wd._ev
    assert ev.t_ns == pytest.approx(t_full / 2 + t_full / 4, rel=1e-4)


WDOG_IDLE = {"reset": "loop:\n wfi\n b loop\n"}


def _wdog_chip(desc, code, vectors=None):
    chip = Chip(desc)
    img, _ = build_image(code, vectors=vectors)
    chip.load_firmware(img)
    chip.reset()
    return chip


def test_wdog_timeout_resets_chip(desc):
    chip = _wdog_chip(desc, WDOG_IDLE)
    chip.run_for(3e9)
    resets = [e for e in chip.log if e.kind == "reset" and e.fields.get("reason") == "watchdog"]
    assert len(resets) == 2
    t0 = resets[0].t_ns
    assert t0 == pytest.approx(5_000_012 * 5 / 20_971_520 * 1e9, rel=1e-4)       # 1.192 s
    assert resets[1].t_ns - t0 == pytest.approx(t0, rel=1e-4)
    assert r(chip, WDOG + 0x14, 2) == 2                  # RSTCNT survives watchdog resets
    assert r(chip, RCM, 1) == 0x20 and r(chip, RCM + 1, 1) == 0     # SRS0.WDOG
    assert any("WDOG timeout" in m for m in msgs(chip, "warn"))


WITHINGS = {
    "reset": """
  ldr r0, =0x40052000
  movw r1, #0xC520
  strh r1, [r0, #0xE]
  movw r1, #0xD928
  strh r1, [r0, #0xE]
  movw r1, #0x01D1
  strh r1, [r0, #0]
  movs r1, #0
  strh r1, [r0, #4]
  movw r1, #0x1388
  strh r1, [r0, #6]
  movs r1, #0
  strh r1, [r0, #0x16]
  ldr r2, =0xE000E010
  ldr r1, =0x00FFFFFF
  str r1, [r2, #4]
  movs r1, #0
  str r1, [r2, #8]
  movs r1, #7
  str r1, [r2]
  cpsie i
loop:
  wfi
  b loop
""",
    "systick": """
  ldr r0, =0x40052000
  movw r1, #0xA602
  strh r1, [r0, #0xC]
  movw r1, #0xB480
  strh r1, [r0, #0xC]
  ldr r0, =0x20000100
  ldr r1, [r0]
  adds r1, #1
  str r1, [r0]
  bx lr
""",
}


def test_wdog_withings_sequence_refresh_keeps_alive(desc):
    chip = _wdog_chip(desc, WITHINGS, vectors={15: "systick"})
    wd = chip.peripherals["WDOG"]
    chip.run_for(1e6)
    assert wd.enabled and wd.toval == 5000 and wd.prescaler == 1
    assert wd.timeout_s == pytest.approx(5.0)
    assert any("WDOG timeout 5.000 s" in m for m in msgs(chip, "info"))
    chip.run_for(12e9)                                   # SysTick (0.8 s) refreshes keep it alive
    assert not [e for e in chip.log if e.kind == "reset" and e.fields.get("reason") == "watchdog"]
    assert chip.bus.read(0x20000100, 4) >= 14
    w(chip, 0xE000E010, 0)                               # stop SysTick: no more refreshes
    t_stop = chip.clock.now_ns()
    chip.run_for(6e9)
    resets = [e for e in chip.log if e.kind == "reset" and e.fields.get("reason") == "watchdog"]
    assert len(resets) == 1
    assert t_stop < resets[0].t_ns <= t_stop + 5e9 + 1e6


# ---------------------------------------------------------------------------------------------- SMC/PMC/RCM/LLWU/VBAT
def test_power_blocks(chip):
    assert r(chip, SMC + 3, 1) == 0x01
    w(chip, SMC + 3, 0x04, 1)
    assert r(chip, SMC + 3, 1) == 0x01
    w(chip, SMC + 0, 0x22, 1)                            # PMPROT (Withings)
    assert r(chip, SMC + 0, 1) == 0x22
    assert r(chip, PMC + 2, 1) == 0x04
    chip.peripherals["PMC"].set("REGSC", 0x0C)           # ACKISO set by a (not modelled) VLLS wake-up
    w(chip, PMC + 2, 0x08, 1)                            # PMC_REGSC |= 8 clears ACKISO (w1c)
    assert r(chip, PMC + 2, 1) == 0x04
    assert r(chip, RCM, 1) == 0x82 and r(chip, RCM + 1, 1) == 0
    assert r(chip, LLWU + 5, 1) == 0 and r(chip, LLWU + 6, 1) == 0 and r(chip, LLWU + 0xA, 1) == 2
    chip.peripherals["LLWU"].set("F1", 0x11)
    w(chip, LLWU + 5, 0x01, 1)
    assert r(chip, LLWU + 5, 1) == 0x10
    w(chip, LLWU + 0, 0x55, 1)
    assert r(chip, LLWU + 0, 1) == 0x55


def test_vbat_survives_reset(chip):
    w(chip, VBAT + 0, 0xDEADBEEF)
    w(chip, VBAT + 0x1C, 0x12345678)
    img, _ = build_image(WDOG_IDLE)
    chip.load_firmware(img)
    chip.reset()
    assert r(chip, VBAT) == 0xDEADBEEF and r(chip, VBAT + 0x1C) == 0x12345678
    assert r(chip, RCM, 1) == 0x82


def test_rcm_software_reset_cause(chip):
    img, _ = build_image(WDOG_IDLE)
    chip.load_firmware(img)
    chip.reset("SYSRESETREQ")
    assert r(chip, RCM, 1) == 0 and r(chip, RCM + 1, 1) == 0x04


# ---------------------------------------------------------------------------------------------- FTFE
def ftfe_cmd(chip, cmd, addr, data: bytes = b""):
    w(chip, FTFE + 7, cmd, 1)                            # FCCOB0
    w(chip, FTFE + 6, (addr >> 16) & 0xFF, 1)            # FCCOB1
    w(chip, FTFE + 5, (addr >> 8) & 0xFF, 1)             # FCCOB2
    w(chip, FTFE + 4, addr & 0xFF, 1)                    # FCCOB3
    for k, b in enumerate(data):
        w(chip, FTFE + 8 + k, b, 1)
    w(chip, FTFE, 0x80, 1)


def test_ftfe_status_and_config(chip):
    assert r(chip, FTFE, 1) == 0x80                      # CCIF idle
    assert r(chip, FTFE + 2, 1) == 0xFE and r(chip, FTFE + 3, 1) == 0xFF
    img, _ = build_image(WDOG_IDLE)
    data = bytearray(img.segments[0][1])
    data[0x40C] = 0xFE
    data[0x40D] = 0xF9
    from chipem.firmware import FirmwareImage
    chip.load_firmware(FirmwareImage([(0, bytes(data))]))
    chip.reset()
    assert r(chip, FTFE + 2, 1) == 0xFE and r(chip, FTFE + 3, 1) == 0xF9


def test_ftfe_program_erase(chip):
    a = 0x80000
    assert chip.bus.read_bytes(a, 8) == b"\xff" * 8
    ftfe_cmd(chip, 0x07, a, bytes(range(1, 9)))
    assert r(chip, FTFE, 1) == 0x80
    assert chip.bus.read_bytes(a, 8) == bytes(range(1, 9))
    ftfe_cmd(chip, 0x01, a, b"\x00\x01")                 # read-1s section: not blank
    assert r(chip, FTFE, 1) == 0x81
    ftfe_cmd(chip, 0x09, a + 0x10)                       # erase sector containing a
    assert r(chip, FTFE, 1) == 0x80
    assert chip.bus.read_bytes(a, 8) == b"\xff" * 8
    ftfe_cmd(chip, 0x07, a + 4)                          # misaligned -> ACCERR
    assert r(chip, FTFE, 1) == 0xA0
    ftfe_cmd(chip, 0x07, a)                              # launch ignored while ACCERR set
    w(chip, FTFE, 0x20, 1)                               # clear ACCERR (w1c)
    assert r(chip, FTFE, 1) == 0x80
    ftfe_cmd(chip, 0x03, 0)
    assert r(chip, FTFE + 0xB, 1) == 0xFF
    ftfe_cmd(chip, 0x81, 0)
    ftfe_cmd(chip, 0x77, 0)
    assert r(chip, FTFE, 1) & 0x20
    infos = [m for m in msgs(chip, "info") if m.startswith("FTFE")]
    assert any("PGM8 0x00080000" in m for m in infos) and any("ERSSCR 0x00080000" in m for m in infos)


# ---------------------------------------------------------------------------------------------- ADC
def test_adc_calibration(chip):
    w(chip, SCGC6, r(chip, SCGC6) | (1 << 27))
    assert r(chip, ADC0) == 0x1F
    w(chip, ADC0 + 0x24, 0x80 | 0x07)                    # SC3: CAL + AVGE/AVGS
    assert r(chip, ADC0) & 0x80                          # COCO
    sc3 = r(chip, ADC0 + 0x24)
    assert not sc3 & 0xC0 and sc3 & 0x07 == 0x07
    clp = [r(chip, ADC0 + o) for o in (0x4C, 0x48, 0x44, 0x40, 0x3C, 0x38)]
    assert all(v > 0 for v in clp)
    assert not [m for m in msgs(chip, "warn") if "ADC0" in m]


def test_adc_conversion_and_irq(chip):
    w(chip, SCGC6, r(chip, SCGC6) | (1 << 27))
    adc = chip.peripherals["ADC0"]
    assert adc.set_channel("ADC0_SE14", 2048) is True
    assert adc.set_channel("ADC1_SE14", 5) is False
    w(chip, ADC0 + 0x08, 0x04)                           # CFG1 MODE=1: 12-bit
    w(chip, ADC0, 14)                                    # SC1A: start channel 14
    assert not r(chip, ADC0) & 0x80
    advance_ns(chip, 5000)
    assert r(chip, ADC0) & 0x80
    assert r(chip, ADC0 + 0x10) == 2048                  # RA
    assert not r(chip, ADC0) & 0x80                      # reading RA cleared COCO
    adc.set_channel(3, 70000)
    w(chip, ADC0, 0x40 | 3)                              # AIEN
    advance_ns(chip, 5000)
    assert r(chip, ADC0 + 0x10) == 4095                  # clipped to 12 bits
    assert 16 + 39 not in chip.arch.asserted             # RA read cleared COCO -> IRQ deasserted
    assert 16 + 39 in chip.arch.pending                  # ... but the NVIC keeps the latched pending bit
    chip.arch.pending.discard(16 + 39)
    w(chip, ADC0, 0x40 | 3)
    advance_ns(chip, 5000)
    assert 16 + 39 in chip.arch.pending
    w(chip, ADC0, 0x1F)                                  # disable
    assert 16 + 39 not in chip.arch.asserted


def test_adc1_and_gate_warning(chip):
    adc1 = chip.peripherals["ADC1"]
    assert adc1.index == 1 and adc1.set_channel("ADC1_DP0", 100)
    w(chip, ADC1, 0x20)                                  # gated off: warns, still works
    assert any("ADC1" in m and "clock gate" in m for m in msgs(chip, "warn"))
    advance_ns(chip, 5000)
    assert r(chip, ADC1 + 0x10) == 100
