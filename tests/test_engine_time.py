import math

import pytest

from chipem.time import Clock, Scheduler


def test_clock_basic_and_set_hz_monotonic():
    c = Clock(1_000_000)
    c.advance(1000)
    assert c.now_ns() == pytest.approx(1_000_000)
    c.set_hz(2_000_000)
    assert c.now_ns() == pytest.approx(1_000_000)        # unchanged by the frequency switch
    c.advance(2000)
    assert c.now_ns() == pytest.approx(2_000_000)
    assert c.ns_to_cycles(1000) == 2
    assert c.ns_to_cycles(0) == 0
    assert c.ns_to_cycles(1) == 1                         # rounds up


def test_scheduler_order_cancel_and_run_due():
    c = Clock(1e6)
    s = Scheduler(c)
    fired = []
    s.at_cycles(30, fired.append, "c")
    s.at_cycles(10, fired.append, "a")
    e = s.at_cycles(20, fired.append, "x")
    s.at_cycles(10, fired.append, "b")                   # same time: FIFO
    s.cancel(e)
    assert s.next_due() == 10
    c.advance(25)
    assert s.run_due() == 2
    assert fired == ["a", "b"]
    assert s.next_due() == 30
    c.advance(10)
    s.run_due()
    assert fired == ["a", "b", "c"]
    assert s.next_due() is None


def test_scheduler_callback_reschedules_and_catches_up():
    c = Clock(1e6)
    s = Scheduler(c)
    ticks = []

    def tick(t):
        ticks.append(t)
        s.at_cycles(t + 100, tick, t + 100)
    s.at_cycles(100, tick, 100)
    c.advance(350)
    s.run_due()
    assert ticks == [100, 200, 300]


def test_in_ns_is_wall_clock_domain():
    c = Clock(1_000_000)          # 1 cycle = 1 µs
    s = Scheduler(c)
    fired = []
    s.in_ns(100_000, fired.append, 1)       # 100 µs = 100 cycles now
    assert s.next_due() == 100
    c.advance(50)                           # 50 µs elapsed
    c.set_hz(4_000_000)                     # remaining 50 µs = 200 cycles at the new rate
    assert s.next_due() == 250
    c.advance(199)
    s.run_due()
    assert fired == []
    c.advance(1)
    s.run_due()
    assert fired == [1]
    assert c.now_ns() == pytest.approx(100_000)


def test_early_hook_inside_horizon():
    c = Clock(1e6)
    s = Scheduler(c)
    calls = []
    s.on_early = lambda: calls.append(1)
    s.horizon = 100
    s.at_cycles(150, lambda: None)
    s.at_cycles(50, lambda: None)
    assert calls == [1]


def test_set_hz_mid_slice_rebases_at_current_instruction():
    """A clock change made by an MMIO write in the middle of a slice takes effect at that instruction: later
    instructions in the same slice must not get earlier timestamps than the ones before the change."""
    c = Clock(1_000_000)
    off = [0]
    c.slice_offset = lambda: off[0]
    c.advance(1000)                                      # slice starts at 1000 cycles = 1 ms
    off[0] = 500
    before = c.cycles_to_ns(c.cycles + off[0])           # instruction 500 of the slice: 1.5 ms
    c.set_hz(2_000_000)                                  # PLL switch at that instruction
    assert c.cycles_to_ns(c.cycles + off[0]) == pytest.approx(before)
    after = c.cycles_to_ns(c.cycles + 600)               # 100 instructions later at 2 MHz: +50 us
    assert after == pytest.approx(before + 50_000)
    c.advance(600)                                       # slice end
    assert c.now_ns() == pytest.approx(after)


def test_time_monotonic_across_firmware_pll_switch():
    """STM32 tim_irq switches SYSCLK to the 168 MHz PLL from inside a slice; event timestamps stay monotonic."""
    from pathlib import Path
    fw = Path(__file__).resolve().parent.parent / "firmware" / "samples_stm32" / "build" / "tim_irq.elf"
    if not fw.exists():
        pytest.skip("STM32 samples not built")
    from chipem.session import Session
    s = Session("stm32f405zg", fw, quiet=True)
    s.run_for(5e6)
    assert s.chip.clock.hz == 168e6
    ts = [e.t_ns for e in s.events()]
    assert len(ts) > 5
    assert all(b >= a - 1e-6 for a, b in zip(ts, ts[1:]))
    s.close()


def test_rescaled_event_inside_running_slice_calls_early_hook():
    """A clock slow-down moves a wall-clock event to an earlier cycle; if that falls inside the running slice the
    scheduler must ask for the slice to be cut (on_early), as it does for a newly scheduled early event."""
    c = Clock(10_000_000)
    s = Scheduler(c)
    off = [0]
    c.slice_offset = s.slice_offset = lambda: off[0]
    calls = []
    s.on_early = lambda: calls.append(off[0])
    s.at_ns(86_800, lambda: None)                        # 868 cycles at 10 MHz
    s.horizon = 868                                      # slice sized for the old clock
    calls.clear()
    off[0] = 5
    c.set_hz(5_000_000)                                  # DIVSEL /2 -> /4 at instruction 5
    assert s.next_due() == 5 + math.ceil((86_800 - 500) * 5e6 / 1e9)
    assert calls == [5]
    s.horizon = math.inf


C28_HALVE = """
WDCR    .set 0x7029
PLLSTS  .set 0x7011
        .sect "codestart", 0x33FFF6
        LB      _c_int00
        .sect "text", 0x330000
_c_int00:
        EALLOW
        MOVW    DP, #DP(WDCR)
        MOV     @WDCR, #0x0068
        AND     @PLLSTS, #0xFE7F      ; DIVSEL 2 -> 0: SYSCLKOUT 10 -> 5 MHz
loop:   SB      loop, UNC
"""


def test_event_before_mid_slice_clock_change_fires_on_time_c28x(tmp_path):
    """Engine regression: an event due at 86.8 us, scheduled before a mid-slice DIVSEL change 10 -> 5 MHz, fired a
    whole slice late (173 us) because the running slice was not cut short."""
    from chipem.core.c28x.asm import Assembler
    from chipem.session import Session
    img = tmp_path / "halve.c28img"
    Assembler().assemble(C28_HALVE, filename="halve.asm").write_c28img(str(img))
    s = Session("tms320f28232", img, quiet=True)
    chip = s.chip
    assert chip.clock.hz == 10e6
    fired = []
    chip.sched.at_ns(86_800, lambda: fired.append(chip.now_ns()))
    s.run_for(300_000)
    assert chip.clock.hz == 5e6
    assert len(fired) == 1
    assert fired[0] == pytest.approx(86_800, abs=200)    # one instruction at 5 MHz
    s.close()
