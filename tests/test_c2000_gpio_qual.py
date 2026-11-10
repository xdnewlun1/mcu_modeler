"""TI C2000 GPIO input qualification (ti_c2000.gpio, SPRUI07 1.4.4): GPxQSEL 3/6-sample windows with GPxCTRL.QUALPRD
delay GPxDAT and XINT and filter glitches; synchronous inputs pass at once."""
from pathlib import Path

from chipem.board import Board
from chipem.chipdesc import load_chip

from c28x_fake import make_fake_chip

CHIP = Path(__file__).resolve().parent.parent / "chips" / "tms320f28232.yaml"
GPIO = 0x6F80
GPACTRL, GPAQSEL1, GPADAT = GPIO + 0x00, GPIO + 0x02, GPIO + 0x40
GPIOXINT1SEL, XINT1CR = GPIO + 0x60, 0x7070


def _rig():
    c = make_fake_chip(load_chip(CHIP))
    c.reset()
    c.core.regs["st1"] |= 1 << 6
    c.bus.write(0x7029, 1, 0x0068)
    return c, Board({"A": c}, quantum_ns=100)


def _bit12(c):
    return c.bus.read(GPADAT, 1) >> 12 & 1


def _qualify(c, samples, qualprd):
    c.bus.write(GPACTRL, 2, qualprd << 8)                   # QUALPRD1: GPIO8-15
    c.bus.write(GPAQSEL1, 2, (1 if samples == 3 else 2) << 24)   # GPIO12


def test_six_sample_window_delays_gpadat_and_filters_glitches():
    """SYSCLKOUT 10 MHz after reset; QUALPRD = 5 -> sampling period 10 cycles (1 us), 6 samples -> the low level
    shows after the first sample clock edge + 5 periods (5-6 us); a 3 us pulse never shows."""
    c, bd = _rig()
    _qualify(c, 6, 5)
    bd.run_for(10_000)
    assert _bit12(c) == 1                                   # pull-up
    c.pins.set_external("GPIO12", "L")
    t0 = c.now_ns()
    seen = None
    for _ in range(100):
        bd.run_for(100)
        if not _bit12(c):
            seen = c.now_ns() - t0
            break
    assert seen is not None and 5_000 <= seen <= 6_100, seen
    c.pins.set_external("GPIO12", "H")                      # glitch high for 3 us
    bd.run_for(3_000)
    c.pins.set_external("GPIO12", "L")
    bd.run_for(20_000)
    assert _bit12(c) == 0


def test_three_sample_window_and_sync_mode():
    c, bd = _rig()
    _qualify(c, 3, 0)                                       # 3 samples of SYSCLKOUT: ~3 cycles
    c.pins.set_external("GPIO12", "L")
    assert _bit12(c) == 1
    bd.run_for(400)
    assert _bit12(c) == 0
    c.bus.write(GPAQSEL1, 2, 0)                             # synchronous: at once
    c.pins.set_external("GPIO12", "H")
    assert _bit12(c) == 1


def test_xint_sees_the_qualified_edge():
    c, bd = _rig()
    hits = []
    orig = c.arch.pulse_irq
    c.arch.pulse_irq = lambda n: (hits.append((n, c.now_ns())), orig(n))
    _qualify(c, 6, 5)
    c.bus.write(GPIOXINT1SEL, 1, 12)
    c.bus.write(XINT1CR, 1, 1)                              # enable, falling edge
    bd.run_for(5_000)
    c.pins.set_external("GPIO12", "L")
    t0 = c.now_ns()
    bd.run_for(2_000)
    c.pins.set_external("GPIO12", "H")                      # 2 us glitch: filtered
    bd.run_for(10_000)
    assert hits == []
    c.pins.set_external("GPIO12", "L")
    t0 = c.now_ns()
    bd.run_for(10_000)
    assert len(hits) == 1 and 5_000 <= hits[0][1] - t0 <= 6_100
