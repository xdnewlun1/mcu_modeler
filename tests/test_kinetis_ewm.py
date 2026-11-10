"""Kinetis External Watchdog Monitor (chipem/periph/kinetis/ewm.py) on the K22F description: write-once
configuration, the 0xB4/0x2C service within 15 bus clocks, the CMPL/CMPH window on the 1 kHz LPO, EWM_in, the
sticky EWM_out_b pin and the WDOG_EWM interrupt. K22P100M120SF5V2RM chapter 23."""
from __future__ import annotations

from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from engine_util import build_image

K22 = Path(__file__).resolve().parent.parent / "chips" / "mk22fn1m0vll12.yaml"
SCGC4, SCGC5, CLKDIV1 = 0x40048034, 0x40048038, 0x40048044
PORTE = 0x4004D000
EWM = 0x40061000
CTRL, SERV, CMPL, CMPH = EWM, EWM + 1, EWM + 2, EWM + 3
INTEN, INEN, ASSIN, EWMEN = 0x08, 0x04, 0x02, 0x01
EXC = 16 + 22                                                 # WDOG_EWM


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
    w(c, CLKDIV1, 1 << 24, 4)                                 # bus 60 MHz
    w(c, SCGC4, r(c, SCGC4, 4) | (1 << 1), 4)                  # EWM
    w(c, SCGC5, r(c, SCGC5, 4) | (1 << 13), 4)                 # PORTE
    w(c, PORTE + 4 * 24, 6 << 8, 4)                            # PTE24 ALT6 = EWM_OUT_b
    w(c, PORTE + 4 * 25, 6 << 8, 4)                            # PTE25 ALT6 = EWM_IN
    return c


def at_ms(chip, ms):
    chip.clock.advance(chip.clock.ns_to_cycles(ms * 1e6 - chip.clock.now_ns()))
    chip.sched.run_due()


def service(chip):
    w(chip, SERV, 0xB4)
    w(chip, SERV, 0x2C)


def warns(chip):
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "warn" and "EWM" in e.fields.get("msg", "")]


def test_reset_state_and_out_pin(chip):
    ewm = chip.peripherals["EWM"]
    assert [r(chip, EWM + i) for i in range(4)] == [0, 0, 0, 0xFF]
    assert ewm.asserted and chip.pins.get("PTE24").level() == "L"    # asserted out of reset (23.4.1)
    w(chip, CMPL, 2)
    w(chip, CMPH, 10)
    w(chip, CTRL, EWMEN)
    assert not ewm.asserted and chip.pins.get("PTE24").level() == "H"
    assert r(chip, SERV) == 0


def test_service_window_and_expiry(chip):
    ewm = chip.peripherals["EWM"]
    w(chip, CMPL, 2)
    w(chip, CMPH, 10)
    w(chip, CTRL, EWMEN | INTEN)
    at_ms(chip, 5.5)
    assert ewm.counter() == 5
    service(chip)                                              # 2 <= 5 < 10: legal, counter restarts
    assert ewm.counter() == 0 and not ewm.asserted and ewm.services == 1
    at_ms(chip, 16.4)                                          # counter 10.9: not yet > CMPH
    assert not ewm.asserted
    at_ms(chip, 16.6)                                          # counter 11 > CMPH: EWM_out asserted
    assert ewm.asserted and "CMPH" in ewm.reason
    assert chip.pins.get("PTE24").level() == "L" and EXC in chip.arch.asserted
    service(chip)                                              # sticky: a service does not deassert
    assert ewm.asserted
    w(chip, CTRL, EWMEN)                                       # clearing INTEN drops the request only
    assert EXC not in chip.arch.asserted and ewm.asserted
    assert any("only a reset" in m for m in warns(chip))
    chip.reset()
    assert ewm.asserted and r(chip, CTRL) == 0                 # after reset: asserted until enabled again
    w(chip, CTRL, EWMEN)
    assert not ewm.asserted


def test_service_before_window_asserts(chip):
    ewm = chip.peripherals["EWM"]
    w(chip, CMPL, 4)
    w(chip, CMPH, 20)
    w(chip, CTRL, EWMEN)
    at_ms(chip, 2.5)
    service(chip)                                              # counter 2 < CMPL 4
    assert ewm.asserted and "CMPL" in ewm.reason


def test_cmph_ff_never_expires_and_equal_compare_values(chip, desc):
    ewm = chip.peripherals["EWM"]
    w(chip, CTRL, EWMEN)                                       # CMPL 0, CMPH 0xFF (reset values)
    at_ms(chip, 300)                                           # (before the WDOG's 0.42 s reset-default timeout)
    assert not ewm.asserted and ewm.counter() == 255
    service(chip)                                              # no minimum time with CMPL = 0
    assert not ewm.asserted
    c = chip
    c.reset()
    w(c, CMPL, 3)
    w(c, CMPH, 3)                                              # illegal equal values: expires at CMPL + 1
    t0 = c.clock.now_ns() / 1e6
    w(c, CTRL, EWMEN)
    at_ms(c, t0 + 3.9)
    assert not ewm.asserted
    at_ms(c, t0 + 4.1)
    assert ewm.asserted


def test_illegal_service_sequences_have_no_effect(chip):
    ewm = chip.peripherals["EWM"]
    w(chip, CMPL, 0)
    w(chip, CMPH, 10)
    w(chip, CTRL, EWMEN)
    at_ms(chip, 5.2)
    w(chip, SERV, 0x2C)                                        # second byte without the first
    w(chip, SERV, 0xB4)
    w(chip, SERV, 0x55)                                        # wrong second byte
    assert ewm.services == 0 and ewm.counter() == 5 and not ewm.asserted
    w(chip, SERV, 0xB4)
    chip.clock.advance(chip.clock.ns_to_cycles(1000))          # 1 us = 60 bus clocks > 15
    w(chip, SERV, 0x2C)
    assert ewm.services == 0 and not ewm.asserted
    service(chip)
    assert ewm.services == 1 and ewm.counter() == 0


def test_write_once_registers(chip):
    w(chip, CMPL, 1)
    w(chip, CMPH, 50)
    w(chip, CMPH, 5)                                           # ignored
    assert r(chip, CMPH) == 50
    w(chip, CTRL, EWMEN)
    w(chip, CTRL, EWMEN | INTEN)                               # INTEN stays writable, no warning
    assert r(chip, CTRL) == EWMEN | INTEN
    assert not any("CTRL" in m for m in warns(chip))
    w(chip, CTRL, INTEN)                                       # clearing EWMEN a second time: ignored
    assert r(chip, CTRL) == EWMEN | INTEN
    ws = warns(chip)
    assert any("EWM_CMPH is write-once" in m for m in ws) and any("EWM_CTRL is write-once" in m for m in ws)
    w(chip, CTRL, 0xFF)
    assert r(chip, CTRL) & 0xF0 == 0


def test_disabled_first_write_cannot_be_enabled_later(chip):
    ewm = chip.peripherals["EWM"]
    w(chip, CTRL, 0)                                           # write-once with EWMEN=0
    w(chip, CTRL, EWMEN)
    assert r(chip, CTRL) == 0 and ewm.asserted


@pytest.mark.parametrize("assin, active", [(0, "L"), (ASSIN, "H")])
def test_ewm_in_assertion(chip, assin, active):
    ewm = chip.peripherals["EWM"]
    idle = "H" if active == "L" else "L"
    chip.pins.set_external("PTE25", idle)
    w(chip, CMPH, 100)
    w(chip, CTRL, EWMEN | INEN | assin | INTEN)
    at_ms(chip, 3)
    assert not ewm.asserted
    chip.pins.set_external("PTE25", active)
    assert ewm.asserted and "EWM_in" in ewm.reason and EXC in chip.arch.asserted
    assert chip.pins.get("PTE24").level() == "L"


def test_ewm_in_ignored_without_inen_and_service_while_asserted(chip):
    ewm = chip.peripherals["EWM"]
    chip.pins.set_external("PTE25", "L")                       # asserted level (ASSIN=0), but INEN=0
    w(chip, CMPH, 100)
    w(chip, CTRL, EWMEN)
    at_ms(chip, 1)
    assert not ewm.asserted
    service(chip)
    assert not ewm.asserted
