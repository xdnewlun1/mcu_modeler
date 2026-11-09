"""systick_irq / pit_irq samples: PTC5 toggles every 1 ms (needs the Kinetis PORT/GPIO models).

The samples assume a 120 MHz core (SysTick RVR 119999) and a 60 MHz bus (PIT LDVAL 59999) but do not program
the MCG/SIM dividers, so the tests pin the clock with --clock: 120 MHz for systick_irq; 60 MHz for pit_irq
(after reset SIM_CLKDIV1 = 0x00010000, so bus = core).
"""
from pathlib import Path

import pytest

from chipem.periph import registry
from chipem.session import Session

BUILD = Path(__file__).resolve().parent.parent / "firmware/samples/build"


def _gpio_models():
    return registry.lookup("kinetis.gpio") is not None and registry.lookup("kinetis.port") is not None


pytestmark = [
    pytest.mark.skipif(not (BUILD / "pit_irq.elf").exists(), reason="sample firmware not built"),
    pytest.mark.skipif(not _gpio_models(), reason="kinetis.gpio/kinetis.port models not available yet"),
]


def _toggles(fw: str, clock_hz: float, run_ns: float = 10.5e6):
    s = Session("mk22fn1m0vll12", BUILD / fw, clock_hz=clock_hz)
    s.run_for(run_ns)
    ev = [e for e in s.events(kinds={"pin"}) if e.pin == "PTC5" and e.field == "level" and e.new in ("H", "L")]
    return s, ev


@pytest.mark.parametrize("fw,clock,irq", [("systick_irq.elf", 120e6, "SysTick"), ("pit_irq.elf", 60e6, "PIT0")])
def test_ptc5_toggles_every_ms(fw, clock, irq):
    s, ev = _toggles(fw, clock)
    toggles = [e for e in ev if e.t_ns > 0.5e6]
    assert len(toggles) == 10, [(e.t_ns, e.new) for e in ev]
    gaps = [b.t_ns - a.t_ns for a, b in zip(toggles, toggles[1:])]
    assert all(abs(g - 1e6) < 5e3 for g in gaps), gaps
    assert all(a.new != b.new for a, b in zip(toggles, toggles[1:]))
    counts = {name: n for _, name, n in s.irq_summary()}
    assert counts.get(irq) == 10


def _period_duty(edges):
    """(periods between rising edges, high times) from [(t_ns, level)] edges."""
    rises = [t for t, v in edges if v == "H"]
    periods = [b - a for a, b in zip(rises, rises[1:])]
    highs = []
    for (t, v), (t2, v2) in zip(edges, edges[1:]):
        if v == "H" and v2 == "L":
            highs.append(t2 - t)
    return periods, highs


@pytest.mark.skipif(not (BUILD / "ftm_pwm.elf").exists(), reason="ftm_pwm sample not built (firmware/samples/build.sh)")
def test_ftm_pwm_sample_1khz_25_percent():
    """ftm_pwm: FTM0 CH0 EPWM on PTC1 from the 60 MHz bus clock, MOD 59999, C0V 15000 -> 1 kHz, 250 us high."""
    s = Session("mk22fn1m0vll12", BUILD / "ftm_pwm.elf", clock_hz=60e6)
    edges = []
    s.chip.pins.subscribe(lambda e: edges.append((e.t_ns, e.new)) if e.pin == "PTC1" and e.field == "level" else None)
    try:
        s.run_for(20.5e6)
    finally:
        s.close()
    pin = s.chip.pins.get("PTC1")
    assert pin.owner == "FTM0_CH0" and pin.direction == "out"
    periods, highs = _period_duty([e for e in edges if e[1] in ("H", "L")])
    assert len(periods) >= 19, edges[:6]
    assert all(abs(p - 1e6) < 50 for p in periods), periods[:5]          # 60000 bus ticks
    assert all(abs(h - 250e3) < 50 for h in highs), highs[:5]            # C0V = 15000 ticks
