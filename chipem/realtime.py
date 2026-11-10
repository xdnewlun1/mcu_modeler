"""Real-time pacing: lock virtual time to wall-clock time (times a speed factor) for interactive use with host
serial bridges (chipem/hostserial.py). Vendor-free.

`RealTime.run(session, ns)` replaces a plain `run_for(ns)` when pacing is on or a host endpoint is attached. It runs
the board/chip in slices of at most `slice_ns` (2 ms) of virtual time and, between slices, drains the host endpoints
(`pump()`, on the emulation thread: the only place bytes cross from I/O threads into the emulation). With pacing on:

* the emulation may run at most one slice ahead of the wall clock; when it is ahead it waits on `wake`, a
  threading.Event that host input, a connection change or `Session.stop()` sets, so host bytes are taken within a few
  ms of wall time;
* when every chip sleeps (WFI/WFE with nothing pending) it does not run ahead at all: it waits until the wall clock
  reaches the next scheduled event (at most 100 ms per wait) or host input arrives, then jumps there. An idle
  firmware therefore costs no CPU;
* when the emulation cannot keep up, `lag_ns` grows; past `max_lag_ns` (200 ms) the anchor is moved (no burst of
  catch-up) and one `warn` reports it with the achieved speed (not in the first 2 s: start-up work). `ratio` is virtual/wall speed over the last second.

Pacing off (but endpoints attached): the same slicing and pumping, no waiting.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any

__all__ = ["RealTime", "SLICE_NS"]

SLICE_NS = 2e6                       # virtual time per slice between host pumps (2 ms)
MAX_WAIT_S = 0.1
STARTUP_GRACE_S = 2.0                # falling behind while loading / printing a banner is not worth a warn


class RealTime:
    def __init__(self, speed: float = 1.0, *, slice_ns: float = SLICE_NS, max_lag_ns: float = 200e6):
        speed = float(speed)
        if not speed > 0 or math.isinf(speed):
            raise ValueError(f"real-time speed must be > 0 (got {speed})")
        self.speed = speed
        self.slice_ns = float(slice_ns)
        self.max_lag_ns = float(max_lag_ns)
        self.wake = threading.Event()
        self.lag_ns = 0.0                    # how far virtual time is behind the wall clock (now)
        self.max_lag_seen_ns = 0.0
        self.rebases = 0                     # times the emulation fell more than max_lag_ns behind
        self.warned = False
        self.ratio: float | None = None      # virtual / wall speed over the last ~1 s of running
        self.waited_s = 0.0                  # total time spent waiting for the wall clock
        self._anchor: tuple[float, float] | None = None     # (wall s, virtual ns)
        self._last_wall = 0.0
        self._win: tuple[float, float] | None = None        # (wall, virtual) at the start of the ratio window
        self._started: float | None = None                   # wall time of the first run (start-up grace for the warn)
        self.grace_s = STARTUP_GRACE_S

    # ---- state ------------------------------------------------------------------------------------------------
    def set_speed(self, speed: float) -> None:
        speed = float(speed)
        if not speed > 0 or math.isinf(speed):
            raise ValueError(f"real-time speed must be > 0 (got {speed})")
        self.speed = speed
        self._anchor = None
        self.wake.set()

    def rebase(self, virt_ns: float, *, keep_window: bool = False) -> None:
        self._anchor = (time.monotonic(), float(virt_ns))
        if not keep_window:
            self._win = None

    def target_ns(self) -> float:
        w0, v0 = self._anchor
        return v0 + (time.monotonic() - w0) * self.speed * 1e9

    def info(self) -> dict:
        return {"enabled": True, "speed": self.speed, "lag_ms": round(self.lag_ns / 1e6, 3),
                "max_lag_ms": round(self.max_lag_seen_ns / 1e6, 3), "behind": self.lag_ns > self.max_lag_ns / 2,
                "rebases": self.rebases, "ratio": None if self.ratio is None else round(self.ratio, 4),
                "waited_s": round(self.waited_s, 3)}

    # ---- the loop -----------------------------------------------------------------------------------------------
    def _measure(self, now_ns: float) -> None:
        wall = time.monotonic()
        if self._win is None:
            self._win = (wall, now_ns)
            return
        w0, v0 = self._win
        if wall - w0 >= 1.0:
            self.ratio = (now_ns - v0) / 1e9 / (wall - w0)
            self._win = (wall, now_ns)


def _now(session: Any) -> float:
    b = session.board
    return b.now_ns() if b is not None else session.chip.clock.now_ns()


def _chips_idle(session: Any) -> float | None:
    """If every chip sleeps: the virtual time (ns) of the earliest scheduled event (inf if none); else None."""
    nxt = math.inf
    for chip in session.chips.values():
        if not getattr(chip, "sleeping", False):
            return None
        due = chip.sched.next_due()
        if due is not None:
            nxt = min(nxt, chip.clock.cycles_to_ns(due))
    return nxt


def run_paced(session: Any, ns: float, rt: RealTime | None, run_fn: Any, pump: Any, stop_flag: Any) -> Any:
    """Run `run_fn(dt)` (board/chip run_for) for `ns` virtual ns in slices, pumping host endpoints between slices
    and, with `rt`, locking virtual time to the wall clock. `stop_flag()` -> True ends early (StopReason.USER)."""
    from .core import StopReason
    start = _now(session)
    end = start + float(ns)
    eps = 1e-6
    slice_ns = rt.slice_ns if rt is not None else SLICE_NS
    if rt is not None:
        wall = time.monotonic()
        if rt._started is None:
            rt._started = wall
        if rt._anchor is None or wall - rt._last_wall > 0.05 or start < rt._anchor[1]:
            rt.rebase(start)                 # paused / reset / first run: start from here, no catch-up
    try:
        while True:
            if stop_flag():
                return StopReason.USER
            pump()
            now = _now(session)
            if now >= end - eps:
                return StopReason.COUNT
            limit = min(end, now + slice_ns)
            if rt is not None:
                target = rt.target_ns()
                lag = target - now
                rt.lag_ns = max(lag, 0.0)
                if lag > rt.max_lag_seen_ns:
                    rt.max_lag_seen_ns = lag
                if lag > rt.max_lag_ns:
                    rt.rebases += 1
                    if not rt.warned and time.monotonic() - (rt._started or 0.0) >= rt.grace_s:
                        rt.warned = True
                        _warn_behind(session, rt, lag, now)
                    rt.rebase(now, keep_window=True)
                    target = now
                idle = _chips_idle(session)
                if idle is not None and idle > now + eps:
                    # every chip sleeps until `idle`: wait for the wall clock to get there (host input or stop()
                    # wakes us earlier), then jump virtual time to the wall clock (no instructions run)
                    nxt = min(idle, end)
                    if target < nxt - eps:
                        _wait(rt, (nxt - target) / (rt.speed * 1e9))
                    t = min(rt.target_ns(), nxt)
                    if t > now + eps:
                        r = run_fn(t - now)
                        if r is not StopReason.COUNT:
                            return r
                    continue
                if now > target + eps:
                    # ahead of the wall clock (by at most one slice): wait until it catches up
                    _wait(rt, (now - target) / (rt.speed * 1e9))
                    continue
            r = run_fn(limit - now)
            if rt is not None:
                rt._measure(_now(session))
            if r is not StopReason.COUNT:
                return r
    finally:
        if rt is not None:
            rt._last_wall = time.monotonic()


def _wait(rt: RealTime, seconds: float) -> None:
    seconds = min(seconds, MAX_WAIT_S)
    if seconds > 0:
        t0 = time.monotonic()
        rt.wake.wait(seconds)
        rt.waited_s += time.monotonic() - t0
    rt.wake.clear()


def _warn_behind(session: Any, rt: RealTime, lag: float, now: float) -> None:
    ratio = f"{rt.ratio * 100:.0f} %" if rt.ratio is not None else "below 100 %"
    msg = (f"real-time: the emulation cannot keep up with the wall clock (x{rt.speed:g}; {lag / 1e6:.0f} ms behind, "
           f"running at about {ratio} of the requested speed); virtual time is re-anchored, host bytes arrive late "
           f"but none are lost")
    log = session.log
    log.event("warn", now, msg=msg, **({"chip": "board"} if session.board is not None else {}))
