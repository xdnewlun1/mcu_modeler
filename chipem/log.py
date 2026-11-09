"""EventLog: the single stream of runtime observations (pin, mmio, irq, uart, reset, fault, info, warn).

`log.event(kind, t_ns, **fields)` appends and notifies subscribers; CLI/VCD/web subscribe. See docs/CONTRACTS.md.

Bounded pin history.  Subscribers see every event, but the *stored* history (iteration, `filter`, Session.events,
the web event list) keeps a fast periodic pin (PWM) bounded: a `pin` level edge counts as dense when at least
`pin_dense_edges` (20) edges of that pin (per `chip` field) fall within `pin_dense_ns` (10 ms), i.e. a square wave
of about 1 kHz or more.  The first `pin_burst` (1000) dense edges are stored as usual; after that the pin's edges are
counted, and one `pin` event with `field="wave"` is stored per `pin_wave_ns` (100 ms) of virtual time:
`edges` (edges in the window), `since_ns` (first of them; `t_ns` is the last), `freq_hz` and `duty` (% high over
whole periods, None if under two rising edges) and `new` (level after the last edge).  When the pin slows down the
open window is stored and edges are stored one by one again (a new burst).  `flush_waves()` stores open windows (the
Session does so after each run for pins that stopped, and on close).  `EventLog(pin_burst=None)` stores every edge.
The VCD writer and the run printer subscribe to the PinBoard / log and still receive every edge.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator

KINDS = ("pin", "mmio", "irq", "uart", "reset", "fault", "info", "warn", "part")


@dataclass
class LogEvent:
    kind: str
    t_ns: float
    fields: dict[str, Any] = field(default_factory=dict)
    seq: int = 0

    def __getattr__(self, name: str) -> Any:     # ev.pin, ev.old, ... for convenience
        f = self.__dict__.get("fields")
        if f is not None and name in f:
            return f[name]
        raise AttributeError(name)

    def get(self, name: str, default: Any = None) -> Any:
        return self.fields.get(name, default)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "t_ns": self.t_ns, "seq": self.seq, **self.fields}


class _Wave:
    """Edge statistics of one pin while its stored history is decimated (see the module doc)."""
    __slots__ = ("t0", "t_last", "edges", "rise0", "rise_last", "rises", "high", "t_up", "high_at_last_rise",
                 "level", "fields")

    def __init__(self, t: float, fields: dict):
        self.t0 = self.t_last = t
        self.edges = 0
        self.rise0 = self.rise_last = None
        self.rises = 0
        self.high = 0.0
        self.t_up = None
        self.high_at_last_rise = 0.0
        self.level = None
        self.fields = fields

    def add(self, t: float, new: Any) -> None:
        self.edges += 1
        self.t_last = t
        self.level = new
        if new == "H":
            if self.rise0 is None:
                self.rise0 = t
            self.rise_last = t
            self.rises += 1
            self.t_up = t
            self.high_at_last_rise = self.high
        elif self.t_up is not None:
            if self.rise0 is not None:
                self.high += t - self.t_up
            self.t_up = None

    def summary(self) -> dict:
        freq = duty = None
        if self.rises >= 2 and self.rise_last > self.rise0:
            span = self.rise_last - self.rise0
            freq = (self.rises - 1) * 1e9 / span
            duty = round(100.0 * self.high_at_last_rise / span, 3)
        return {**self.fields, "field": "wave", "old": None, "new": self.level, "edges": self.edges,
                "since_ns": self.t0, "freq_hz": freq, "duty": duty}


class EventLog:
    def __init__(self, maxlen: int | None = 1_000_000, *, pin_burst: int | None = 1000, pin_dense_edges: int = 20,
                 pin_dense_ns: float = 10e6, pin_wave_ns: float = 100e6):
        self._events: deque[LogEvent] = deque(maxlen=maxlen)
        self._subs: list[tuple[Callable[[LogEvent], None], frozenset[str] | None]] = []
        self._seq = 0
        self.pin_burst = pin_burst
        self.pin_dense_edges = max(2, int(pin_dense_edges))
        self.pin_dense_ns = float(pin_dense_ns)
        self.pin_wave_ns = float(pin_wave_ns)
        # (chip, pin) -> [recent edge times, dense edges stored in this burst, open _Wave | None]
        self._pins: dict[tuple, list] = {}
        self.pin_edges_dropped = 0          # edges kept out of the stored history (counted in `wave` events)

    def event(self, kind: str, t_ns: float, **fields: Any) -> LogEvent:
        self._seq += 1
        ev = LogEvent(kind, float(t_ns), fields, self._seq)
        if kind != "pin" or self.pin_burst is None or fields.get("field") != "level" or self._store_edge(ev):
            self._events.append(ev)
        for cb, kinds in list(self._subs):
            if kinds is None or kind in kinds:
                cb(ev)
        return ev

    def _store_edge(self, ev: LogEvent) -> bool:
        f = ev.fields
        key = (f.get("chip"), f.get("pin"))
        st = self._pins.get(key)
        if st is None:
            st = self._pins[key] = [deque(maxlen=self.pin_dense_edges - 1), 0, None]
        t = ev.t_ns
        rec = st[0]
        dense = len(rec) == rec.maxlen and t - rec[0] < self.pin_dense_ns
        rec.append(t)
        w = st[2]
        if not dense:
            if w is not None:
                self._store_wave(st)
            st[1] = 0
            return True
        if w is None:
            if st[1] < self.pin_burst:
                st[1] += 1
                return True
            w = st[2] = _Wave(t, {k: v for k, v in f.items() if k in ("chip", "pin")})
        elif t - w.t0 >= self.pin_wave_ns:
            self._store_wave(st)
            w = st[2] = _Wave(t, w.fields)
        w.add(t, f.get("new"))
        self.pin_edges_dropped += 1
        return False

    def _store_wave(self, st: list) -> None:
        w = st[2]
        st[2] = None
        if w is not None and w.edges:
            self._seq += 1
            self._events.append(LogEvent("pin", w.t_last, w.summary(), self._seq))

    def flush_waves(self, now_ns: float | None = None) -> None:
        """Store open `wave` windows: all of them (`now_ns` None), or those of pins with no edge for
        `pin_dense_ns` before `now_ns` (stopped; a running PWM keeps its window open)."""
        for st in self._pins.values():
            w = st[2]
            if w is not None and (now_ns is None or now_ns - w.t_last >= self.pin_dense_ns):
                self._store_wave(st)

    # convenience wrappers
    def info(self, t_ns: float, msg: str, **fields: Any) -> LogEvent:
        return self.event("info", t_ns, msg=msg, **fields)

    def warn(self, t_ns: float, msg: str, **fields: Any) -> LogEvent:
        return self.event("warn", t_ns, msg=msg, **fields)

    def subscribe(self, cb: Callable[[LogEvent], None], kinds: Iterable[str] | str | None = None) -> Callable[[], None]:
        """Register cb(ev); optional kinds filter. Returns an unsubscribe function."""
        if isinstance(kinds, str):
            kinds = (kinds,)
        entry = (cb, frozenset(kinds) if kinds is not None else None)
        self._subs.append(entry)
        return lambda: self._subs.remove(entry) if entry in self._subs else None

    def filter(self, kind: str | Iterable[str] | None = None, *, since_ns: float | None = None,
               until_ns: float | None = None, **match: Any) -> list[LogEvent]:
        kinds = None if kind is None else ({kind} if isinstance(kind, str) else set(kind))
        out = []
        for ev in self._events:
            if kinds is not None and ev.kind not in kinds:
                continue
            if since_ns is not None and ev.t_ns < since_ns:
                continue
            if until_ns is not None and ev.t_ns > until_ns:
                continue
            if any(ev.fields.get(k) != v for k, v in match.items()):
                continue
            out.append(ev)
        return out

    def of_kind(self, kind: str) -> list[LogEvent]:
        return self.filter(kind)

    def clear(self):
        self._events.clear()
        self._pins.clear()

    def __iter__(self) -> Iterator[LogEvent]:
        return iter(list(self._events))

    def __len__(self) -> int:
        return len(self._events)

    def __getitem__(self, i):
        return list(self._events)[i]


def format_time(t_ns: float) -> str:
    return f"[{t_ns / 1e6:10.3f} ms]"


def format_event(ev: LogEvent) -> str:
    f = dict(ev.fields)
    head = f"{format_time(ev.t_ns)} {ev.kind}"
    if ev.kind == "pin" and "pin" in f:
        fld = f.pop("field", "level")
        old, new = f.pop("old", None), f.pop("new", None)
        if fld == "wave":
            n, fr, du = f.pop("edges", 0), f.pop("freq_hz", None), f.pop("duty", None)
            t0 = f.pop("since_ns", ev.t_ns)
            s = (f"{head} {f.pop('pin')} wave {n} edges since {t0 / 1e6:.3f} ms"
                 + (f" {fr:.6g} Hz" if fr else "") + (f" duty {du:.1f}%" if du is not None else "") + f" now {new}")
        else:
            s = f"{head} {f.pop('pin')} {fld} {old}->{new}"
    elif ev.kind == "uart" and ("data" in f or "text" in f):
        name = f.pop("name", f.pop("uart", ""))
        data = f.pop("data", None)
        if data is None:
            data = f.pop("text")
        if isinstance(data, (bytes, bytearray)):
            data = data.decode("latin-1")
        s = " ".join(x for x in (head, str(name), str(f.pop("dir", "tx")), repr(data)) if x)
    elif "msg" in f:
        s = f"{head} {f.pop('msg')}"
    else:
        s = head
    for k, v in f.items():
        if isinstance(v, int) and not isinstance(v, bool) and k in ("addr", "pc", "value", "base", "offset"):
            v = f"0x{v:X}"
        s += f" {k}={v}"
    return s
