"""Speed / fidelity settings and the live speed meter (vendor-free).

`PerfSettings` holds the knobs that trade fidelity for speed; `apply(session)` pushes them into the chips, the board
and the event logs. Presets set them together:

==========  =========  ============  ===========  =======  ==========  ===========
preset      max_slice  auto_quantum  poll_skip    pin_log  drop_kinds  ui_interval
==========  =========  ============  ===========  =======  ==========  ===========
accurate    500        off           off          all      -           30 ms
default     2000       off           off          auto     -           30 ms
fast        20000      on            100 us       auto     mmio        200 ms
==========  =========  ============  ===========  =======  ==========  ===========

No preset changes the board quantum (the board author picks it for the board's links).

What each one costs (see docs/USER_GUIDE.md "Making it faster"):

* ``max_slice`` (instructions per core slice). Slices end at the next scheduled event anyway, also one that a model
  schedules from inside an MMIO callback (read or write: the core lowers the running slice's instruction limit to
  the event, chipem/core/unicorn_cm.py `end_slice_at`), so interrupts, timers and pin timestamps stay exact. A longer
  slice delays only what is not an event: a Session.stop()/pause from another thread (Unicorn stops at the next
  translation block, so in practice at once) and anything that timestamps with `clock.now_ns()` instead of
  `chip.now_ns()` (up to one slice early). Measured with tools/bench.py: 2000 -> 20000 gains about 10-15 % on busy
  loops and about 30 % on k22_sdcard, and no case got slower (k22_i2c_eeprom was three times slower at 50000 while
  read callbacks could not end a slice; it is now flat), so `fast` uses 20000. A core parked in a branch-to-itself
  ignores the limit (nothing can change).
* ``poll_skip_ns``: a polling loop (the same MMIO values, no MMIO write and the identical CPU register state in two
  consecutive iterations) is fast-forwarded by whole iterations up to the next scheduled event, at most this long at
  a time. Exact for models that change status bits in events; a value a model derives from the time on each read is
  seen up to `poll_skip_ns` late. A loop that only changes RAM (no register trace) would be wrongly skipped (rare).
* ``quantum_ns`` (board lockstep): the most one chip runs ahead of another, i.e. the worst-case latency of a pin edge
  or a link byte crossing chips. Synchronous bit-level links between chips (SPI/I2C/McBSP on nets) need it below half
  a bit period; UART/CAN links tolerate up to about one character / frame time.
* ``auto_quantum``: a sleeping chip (WFI/IDLE, nothing pending) holds the others back only up to its next scheduled
  event (exactly: its timer-driven outputs are never late); anything sent to it meanwhile ends the sender's run so the
  lockstep re-plans. No fidelity cost beyond `quantum_ns` for awake chips; idle chips stop costing a slice per
  quantum.
* ``pin_log``: ``all`` stores every pin edge in the event history; ``auto`` decimates dense (PWM) pins into `wave`
  summaries (the default, chipem/log.py); ``off`` keeps pin edges out of the event log entirely (VCD files and the pin
  table still see every edge; the event list, run printer and Discovery tab show no pin events).
* ``drop_kinds``: event kinds not recorded at all (neither stored nor sent to subscribers), e.g. ``mmio`` (only
  logged with --log-mmio anyway) or ``i2s``.
* ``ui_interval_s``: how often the web worker publishes a state snapshot (and the stream sends a frame).

`SpeedMeter` measures what the emulation achieves: virtual time per wall second over the last ~1 s of running, MIPS
per chip and, on a board, the share of the wall time each chip took (the largest share is the bottleneck).
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from typing import Any

__all__ = ["PerfSettings", "PRESETS", "preset", "SpeedMeter", "PIN_LOG_MODES"]

PIN_LOG_MODES = ("all", "auto", "off")


@dataclass
class PerfSettings:
    preset: str = "default"                 # name of the preset these values came from ("custom" once edited)
    max_slice: int = 2000                   # instructions per core slice (Chip.max_slice)
    quantum_ns: float | None = None         # board lockstep quantum; None = the board's own (YAML, --quantum, 10 us)
    auto_quantum: bool = False              # sleeping chips do not hold the others back (Board.set_auto_quantum)
    pin_log: str = "auto"                   # all | auto | off
    drop_kinds: frozenset = field(default_factory=frozenset)   # event kinds not recorded
    ui_interval_s: float = 0.03             # web: state snapshot / stream frame interval
    poll_skip_ns: float = 0.0               # > 0: fast-forward detected polling loops, at most this long at a time

    def to_dict(self) -> dict:
        d = asdict(self)
        d["drop_kinds"] = sorted(self.drop_kinds)
        return d

    def updated(self, **kw: Any) -> "PerfSettings":
        """A copy with the given fields changed (validated); `preset=` loads a preset first. Only pass the keys to
        change: `quantum_ns=None` means "the board's own quantum", None for any other key is ignored."""
        name = kw.pop("preset", None)
        out = preset(name) if name not in (None, "", "custom") else replace(self)
        changed = False
        for k, v in kw.items():
            if v is None and k != "quantum_ns":
                continue
            if k == "max_slice":
                v = int(v)
                if not 1 <= v <= 10_000_000:
                    raise ValueError("max_slice must be 1 .. 10000000 instructions")
            elif k == "quantum_ns":
                if v is not None:
                    v = float(v)
                    if not v > 0:
                        raise ValueError("quantum must be > 0")
            elif k == "auto_quantum":
                v = _bool(v)
            elif k == "pin_log":
                v = str(v).strip().lower()
                if v not in PIN_LOG_MODES:
                    raise ValueError(f"pin_log must be one of {', '.join(PIN_LOG_MODES)}")
            elif k == "drop_kinds":
                v = _kinds(v)
            elif k == "poll_skip_ns":
                v = float(v or 0.0)
                if not 0.0 <= v <= 1e9:
                    raise ValueError("poll_skip must be 0 (off) .. 1 s")
            elif k == "ui_interval_s":
                v = float(v)
                if not 0.01 <= v <= 5.0:
                    raise ValueError("ui_interval must be 10 ms .. 5 s")
            else:
                raise ValueError(f"unknown performance setting {k!r}")
            if getattr(out, k) != v:
                setattr(out, k, v)
                changed = True
        if changed:
            out.preset = "custom"
        return out


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _kinds(v: Any) -> frozenset:
    from .log import KINDS
    if v is None or v == "":
        return frozenset()
    items = v.split(",") if isinstance(v, str) else list(v)
    ks = frozenset(str(k).strip().lower() for k in items if str(k).strip())
    bad = ks - set(KINDS)
    if bad:
        raise ValueError(f"unknown event kind(s): {', '.join(sorted(bad))} (known: {', '.join(KINDS)})")
    if ks & {"fault", "reset"}:
        raise ValueError("fault and reset events cannot be dropped")
    return ks


PRESETS: dict[str, PerfSettings] = {
    "accurate": PerfSettings("accurate", max_slice=500, pin_log="all"),
    "default": PerfSettings("default"),
    "fast": PerfSettings("fast", max_slice=20000, auto_quantum=True, drop_kinds=frozenset({"mmio"}),
                         ui_interval_s=0.2, poll_skip_ns=100e3),
}


def preset(name: str) -> PerfSettings:
    key = str(name).strip().lower()
    if key not in PRESETS:
        raise ValueError(f"unknown preset {name!r} (presets: {', '.join(PRESETS)})")
    return replace(PRESETS[key])


def board_quantum(settings: PerfSettings, session: Any) -> float | None:
    """The quantum to apply to the session's board: an explicit setting, else the board's own value (board YAML
    `quantum:`, --quantum, or 10 us). No preset changes it: it decides cross-chip latency, which the board author
    chose for the board's links (synchronous buses need it short)."""
    b = session.board
    if b is None:
        return None
    if settings.quantum_ns is not None:
        return float(settings.quantum_ns)
    return session.base_quantum_ns


def apply(settings: PerfSettings, session: Any) -> None:
    """Push `settings` into the session's chips, board and logs (emulation thread)."""
    for chip in session.chips.values():
        chip.max_slice = int(settings.max_slice) * int(getattr(chip.core, "slice_scale", 1))
        chip.poll_skip_ns = float(settings.poll_skip_ns)
        watch = getattr(chip.core, "set_poll_watch", None)
        if watch is not None:
            watch(settings.poll_skip_ns > 0)
    logs = [chip.log for chip in session.chips.values()]
    b = session.board
    if b is not None:
        q = board_quantum(settings, session)
        if q:
            b.quantum_ns = q
        b.set_auto_quantum(settings.auto_quantum and len(b.chips) > 1)
        logs.append(b.log)
    for log in logs:
        drop = set(settings.drop_kinds)
        if settings.pin_log == "off":
            drop.add("pin")
        if hasattr(log, "drop"):
            log.drop = frozenset(drop)
        if hasattr(log, "pin_burst"):
            if settings.pin_log == "all":
                log.pin_burst = None
            elif getattr(log, "pin_burst", None) is None:
                log.pin_burst = getattr(log, "default_pin_burst", 1000)


# ------------------------------------------------------------------------------------------------ speed meter
class SpeedMeter:
    """Achieved speed of a Session, over the last `window_s` seconds of wall time spent running.

    `sample(session, wall_s)` is called after each run with the wall time it took; `info()` reports
    {ratio, window_s, chips: {id: {mips, mhz, share, ratio}}, bottleneck}. Time spent paused does not count."""

    def __init__(self, window_s: float = 1.0):
        self.window_s = float(window_s)
        self.run_wall = 0.0                         # wall seconds spent running (cumulative)
        self._hist: deque = deque(maxlen=512)       # (run_wall, t_ns, {cid: insns}, {cid: chip wall}, {cid: t_ns})
        self._last: dict | None = None
        self.last_ts = 0.0                          # time.monotonic() of the last sample

    def reset(self) -> None:
        self._hist.clear()
        self._last = None

    def begin(self, session: Any) -> None:
        """Before a run: the first one (or the first after a reset to t=0) records its starting point."""
        h = self._hist
        if not h or self._now(session) < h[-1][1]:
            self._record(session)

    @staticmethod
    def _now(session: Any) -> float:
        b = session.board
        return b.now_ns() if b is not None else next(iter(session.chips.values())).clock.now_ns()

    def sample(self, session: Any, wall_s: float) -> None:
        self.run_wall += max(0.0, float(wall_s))
        self._record(session)

    def _record(self, session: Any) -> None:
        chips = session.chips
        b = session.board
        cw = dict(getattr(b, "chip_wall", {}) or {}) if b is not None else {}
        if b is None or len(chips) == 1:
            cid = next(iter(chips))
            cw = {cid: self.run_wall}
        t = self._now(session)
        rec = (self.run_wall, t, {c: ch.insns for c, ch in chips.items()}, cw,
               {c: ch.clock.now_ns() for c, ch in chips.items()}, {c: ch.clock.hz for c, ch in chips.items()})
        h = self._hist
        if h and (t < h[-1][1] or set(rec[2]) != set(h[-1][2])):
            h.clear()                               # reset to t=0 / chips changed: start over
        h.append(rec)
        while len(h) > 2 and h[-1][0] - h[1][0] >= self.window_s:
            h.popleft()
        self.last_ts = time.monotonic()

    def info(self) -> dict:
        h = self._hist
        if len(h) < 2:
            return {"ratio": None, "window_s": 0.0, "chips": {}, "bottleneck": None}
        w0, t0, i0, c0, n0, _hz0 = h[0]
        w1, t1, i1, c1, n1, hz1 = h[-1]
        dw = w1 - w0
        if dw <= 0:
            return {"ratio": None, "window_s": 0.0, "chips": {}, "bottleneck": None}
        chips = {}
        tot = sum(max(0.0, c1.get(c, 0.0) - c0.get(c, 0.0)) for c in i1) or 0.0
        for c in i1:
            dc = max(0.0, c1.get(c, 0.0) - c0.get(c, 0.0))
            chips[c] = {"mips": round((i1[c] - i0.get(c, 0)) / dw / 1e6, 3),
                        "mhz": round(hz1[c] / 1e6, 3),
                        "ratio": round((n1[c] - n0.get(c, n1[c])) / 1e9 / dw, 5),
                        "share": round(dc / tot, 3) if tot > 0 else None}
        bott = None
        if len(chips) > 1 and tot > 0:
            bott = max(chips, key=lambda c: chips[c]["share"] or 0.0)
        return {"ratio": round((t1 - t0) / 1e9 / dw, 5), "window_s": round(dw, 3), "chips": chips, "bottleneck": bott}
