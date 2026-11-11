"""Generic clock-stop freezer for family power controllers (Kinetis SMC, STM32 PWR); no vendor knowledge.

A deep low-power mode stops the clocks of most peripherals.  The family controller picks the blocks whose clock
stops (from its reference manual tables) and calls `Freezer.freeze(blocks, mode)` on entry and `thaw()` on exit:

* Blocks with `clock_stop(stopped, mode)` are told and handle the stop themselves (PIT, WDOG, UART, ...).
* For every other block the freezer pulls the block's pending scheduler events out of the heap ("held") and puts
  them back on exit shifted by the time slept, so they fire with their remaining time.  An event belongs to a block
  when its callback is a bound method of the block, or of a helper object whose `lp_owner` attribute is the block
  (bus engines such as an I2C master or a SPI sequencer living in the model).
* While held, the block's `lp_frozen` is the mode name (truthy; False otherwise).  Models check it in pin listeners:
  an input edge reaching a block whose clock is stopped is not seen, as on silicon (asynchronous wake-up logic such
  as an I2C address match is the exception the model implements).
* On exit `time_shift(dns, dcycles)` is called on every held block that defines it: models whose counters are
  derived from an absolute start time (`(now - t0) / tick`) move their anchors forward by the stopped time, so the
  counters continue from where they stopped instead of jumping by the sleep time.  `shift_event_args()` helps
  models whose pending events carry absolute times as arguments.
"""
from __future__ import annotations

import heapq
from typing import Any, Callable, Iterable


def event_owner(ev: Any) -> Any:
    """The block an event belongs to (see module docstring) or None."""
    owner = getattr(ev.cb, "__self__", None)
    if owner is None:
        return None
    return getattr(owner, "lp_owner", None) or owner


class Freezer:
    """Holds back the scheduler events of blocks whose clock is stopped and resumes them with their remaining time."""

    def __init__(self, chip: Any):
        self.chip = chip
        self.held: list = []
        self.told: list = []
        self.frozen: list = []
        self.t0_ns = 0.0
        self.c0 = 0
        self.mode = ""

    @property
    def active(self) -> bool:
        return bool(self.told or self.frozen)

    def freeze(self, blocks: Iterable, mode: str) -> None:
        chip = self.chip
        sched = chip.sched
        blocks = list(blocks)
        self.mode = mode
        self.t0_ns = sched.now_ns()
        self.c0 = sched.now_cycles()
        told = [b for b in blocks if hasattr(b, "clock_stop")]
        for b in told:
            b.clock_stop(True, mode)
        self.told = told
        rest = [b for b in blocks if not hasattr(b, "clock_stop")]
        ids = {id(b) for b in rest}
        keep, held = [], []
        for ev in sched._heap:
            if not ev.cancelled and id(event_owner(ev)) in ids:
                held.append(ev)
            else:
                keep.append(ev)
        if held:
            heapq.heapify(keep)
            sched._heap[:] = keep
        self.held = held
        for b in rest:
            try:
                b.lp_frozen = mode or True
                b.lp_t0_ns = self.t0_ns              # "now" of a frozen block (counter reads while asleep)
                b.lp_c0 = self.c0
            except AttributeError:
                pass
        self.frozen = rest

    def thaw(self) -> None:
        chip = self.chip
        sched, clock = chip.sched, chip.clock
        dns = sched.now_ns() - self.t0_ns
        dc = sched.now_cycles() - self.c0
        for ev in self.held:
            if ev.cancelled:
                continue
            if ev.t_ns is not None:
                ev.t_ns += dns
                ev.cycles = clock.abs_ns_to_cycles(ev.t_ns)
            else:
                ev.cycles += dc
            heapq.heappush(sched._heap, ev)
            owner = getattr(ev.cb, "__self__", None)
            if getattr(owner, "_st_ev", None) is ev:          # armv7m SysTick keeps its own copy of the wrap time
                owner._st_next = ev.cycles
        self.held = []
        frozen, self.frozen = self.frozen, []
        for b in frozen:
            try:
                b.lp_frozen = False
            except AttributeError:
                pass
            fn = getattr(b, "time_shift", None)
            if fn is not None and (dns or dc):
                fn(dns, dc)
        for b in self.told:
            b.clock_stop(False, self.mode)
        self.told = []

    def drop(self) -> None:
        """Reset while frozen: the held events belong to blocks that reset anyway."""
        for b in self.frozen:
            try:
                b.lp_frozen = False
            except AttributeError:
                pass
        self.held = []
        self.told = []
        self.frozen = []


def own_events(block: Any, *callbacks: Callable) -> list:
    """Pending (not cancelled) scheduler events of `block` (optionally only those whose callback is one of the given
    bound methods)."""
    chip = getattr(block, "chip", None)
    if chip is None:
        return []
    funcs = {getattr(cb, "__func__", cb) for cb in callbacks}
    out = []
    for ev in chip.sched._heap:
        if ev.cancelled or event_owner(ev) is not block:
            continue
        if funcs and getattr(ev.cb, "__func__", ev.cb) not in funcs:
            continue
        out.append(ev)
    return out


def shift_event_args(block: Any, dns: float, *callbacks: Callable, index: int = 0) -> None:
    """Add `dns` to argument `index` (an absolute time in ns) of the block's pending events with these callbacks."""
    for ev in own_events(block, *callbacks):
        a = list(ev.args)
        if len(a) > index and isinstance(a[index], (int, float)):
            a[index] = a[index] + dns
            ev.args = tuple(a)
