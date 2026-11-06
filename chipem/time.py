"""Virtual time: a cycle counter in the core clock domain plus a heap-based event scheduler.

Time is kept in core cycles (`Clock.cycles`).  The core clock may change at runtime (MCG re-programming);
`Clock.set_hz` re-bases the cycle→ns conversion so nanosecond time stays monotonic and continuous.  The re-base
point is the precise current cycle: the Chip sets `Clock.slice_offset` (core.insns_in_slice), so a frequency change
made by an MMIO write in the middle of a slice takes effect at that instruction, not at the slice start.

Events can be scheduled in the cycle domain (`at_cycles`) or in the wall-clock domain (`in_ns` / `at_ns`).
Wall-clock events remember their nanosecond deadline and are re-converted to cycles whenever the clock
frequency changes, so e.g. a 1 kHz LPO-driven watchdog keeps its real-time period when the core clock moves.
"""
from __future__ import annotations

import heapq
import itertools
import math
from typing import Any, Callable


class Clock:
    __slots__ = ("cycles", "hz", "_base_cycles", "_base_ns", "_listeners", "slice_offset")

    def __init__(self, hz: float = 120_000_000.0):
        self.cycles: int = 0
        self.hz: float = float(hz)
        self._base_cycles = 0        # cycles at the last frequency change
        self._base_ns = 0.0          # ns at the last frequency change
        self._listeners: list[Callable[[float, float], None]] = []
        # instructions already executed in the running slice (set by the Chip: core.insns_in_slice)
        self.slice_offset: Callable[[], int] | None = None

    # -- conversions -----------------------------------------------------
    def now_ns(self) -> float:
        return self._base_ns + (self.cycles - self._base_cycles) * 1e9 / self.hz

    def cycles_to_ns(self, cycles_abs: int) -> float:
        """Absolute cycle count → absolute ns, assuming the current frequency from the last re-base on."""
        return self._base_ns + (cycles_abs - self._base_cycles) * 1e9 / self.hz

    def ns_to_cycles(self, ns: float) -> int:
        """Duration in ns → number of cycles at the current frequency (rounded up, at least 0)."""
        if ns <= 0:
            return 0
        return max(0, math.ceil(ns * self.hz / 1e9 - 1e-9))

    def abs_ns_to_cycles(self, t_ns: float) -> int:
        """Absolute ns → absolute cycle count (rounded up), at the current frequency."""
        return self.cycles + self.ns_to_cycles(t_ns - self.now_ns())

    # -- mutation ----------------------------------------------------------
    def advance(self, cycles: int) -> None:
        self.cycles += cycles

    def set_hz(self, hz: float) -> None:
        hz = float(hz)
        if hz <= 0:
            raise ValueError("clock frequency must be positive")
        if hz == self.hz:
            return
        old = self.hz
        so = self.slice_offset
        at = self.cycles + (so() if so is not None else 0)     # the precise current instruction
        self._base_ns = self.cycles_to_ns(at)
        self._base_cycles = at
        self.hz = hz
        for cb in self._listeners:
            cb(old, hz)

    def on_change(self, cb: Callable[[float, float], None]) -> None:
        """Register cb(old_hz, new_hz) called after every frequency change."""
        self._listeners.append(cb)

    def reset_time(self) -> None:
        self.cycles = 0
        self._base_cycles = 0
        self._base_ns = 0.0


class Event:
    __slots__ = ("cycles", "seq", "cb", "args", "cancelled", "t_ns", "name")

    def __init__(self, cycles: int, seq: int, cb: Callable, args: tuple, t_ns: float | None = None, name: str = ""):
        self.cycles = cycles
        self.seq = seq
        self.cb = cb
        self.args = args
        self.cancelled = False
        self.t_ns = t_ns            # set for wall-clock-domain events
        self.name = name

    def __lt__(self, other: "Event") -> bool:
        return (self.cycles, self.seq) < (other.cycles, other.seq)

    def __repr__(self) -> str:
        return f"<Event {self.name or getattr(self.cb, '__name__', '?')} @{self.cycles}{' X' if self.cancelled else ''}>"


class Scheduler:
    def __init__(self, clock: Clock):
        self.clock = clock
        self._heap: list[Event] = []
        self._seq = itertools.count()
        # While the core runs a slice ending at `horizon`, scheduling an event before it calls on_early()
        # so the Chip can cut the slice short (set by the Chip's run loop).
        self.horizon: float = math.inf
        self.on_early: Callable[[], None] | None = None
        # Instructions already executed in the running slice (core.insns_in_slice): relative scheduling from an
        # MMIO callback counts from the accessing instruction, not from the slice start.
        self.slice_offset: Callable[[], int] | None = None
        clock.on_change(self._rescale)

    # -- scheduling ---------------------------------------------------------
    def at_cycles(self, cycles_abs: int, cb: Callable, *args: Any) -> Event:
        ev = Event(int(cycles_abs), next(self._seq), cb, args)
        heapq.heappush(self._heap, ev)
        if ev.cycles < self.horizon and self.on_early is not None:
            self.on_early()
        return ev

    def now_cycles(self) -> int:
        so = self.slice_offset
        return self.clock.cycles + (so() if so is not None else 0)

    def now_ns(self) -> float:
        so = self.slice_offset
        if so is None:
            return self.clock.now_ns()
        return self.clock.cycles_to_ns(self.clock.cycles + so())

    def in_cycles(self, cycles: int, cb: Callable, *args: Any) -> Event:
        return self.at_cycles(self.now_cycles() + int(cycles), cb, *args)

    def at_ns(self, t_ns: float, cb: Callable, *args: Any) -> Event:
        """Wall-clock domain: fire at absolute virtual time t_ns, independent of later clock changes."""
        ev = Event(self.clock.abs_ns_to_cycles(t_ns), next(self._seq), cb, args, t_ns=float(t_ns))
        heapq.heappush(self._heap, ev)
        if ev.cycles < self.horizon and self.on_early is not None:
            self.on_early()
        return ev

    def in_ns(self, ns: float, cb: Callable, *args: Any) -> Event:
        return self.at_ns(self.now_ns() + ns, cb, *args)

    def cancel(self, ev: Event | None) -> None:
        if ev is not None:
            ev.cancelled = True

    def clear(self) -> None:
        self._heap.clear()

    # -- queries / dispatch ------------------------------------------------------
    def next_due(self) -> int | None:
        h = self._heap
        while h and h[0].cancelled:
            heapq.heappop(h)
        return h[0].cycles if h else None

    def run_due(self) -> int:
        """Fire every event due at or before clock.cycles (in order). Returns the number fired."""
        h = self._heap
        now = self.clock.cycles
        n = 0
        while h and h[0].cycles <= now:
            ev = heapq.heappop(h)
            if ev.cancelled:
                continue
            ev.cancelled = True        # an event fires once; lets owners test `ev.cancelled` for liveness
            ev.cb(*ev.args)
            n += 1
            now = self.clock.cycles
        return n

    def pending(self) -> list[Event]:
        return sorted(e for e in self._heap if not e.cancelled)

    def __len__(self) -> int:
        return sum(1 for e in self._heap if not e.cancelled)

    # -- clock change -----------------------------------------------------------------
    def _rescale(self, old_hz: float, new_hz: float) -> None:
        changed = False
        for ev in self._heap:
            if ev.t_ns is not None and not ev.cancelled:
                ev.cycles = self.clock.abs_ns_to_cycles(ev.t_ns)
                changed = True
        if changed:
            heapq.heapify(self._heap)
            # a slower clock moves wall-clock events to earlier cycles, possibly inside the running slice
            # (sized for the old clock): ask the Chip to cut it, as for a newly scheduled early event
            if self.on_early is not None and self.next_due() is not None and self._heap[0].cycles < self.horizon:
                self.on_early()
