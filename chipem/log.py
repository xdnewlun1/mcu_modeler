"""EventLog: the single stream of runtime observations (pin, mmio, irq, uart, reset, fault, info, warn).

`log.event(kind, t_ns, **fields)` appends and notifies subscribers; CLI/VCD/web subscribe. See docs/CONTRACTS.md.
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


class EventLog:
    def __init__(self, maxlen: int | None = 1_000_000):
        self._events: deque[LogEvent] = deque(maxlen=maxlen)
        self._subs: list[tuple[Callable[[LogEvent], None], frozenset[str] | None]] = []
        self._seq = 0

    def event(self, kind: str, t_ns: float, **fields: Any) -> LogEvent:
        self._seq += 1
        ev = LogEvent(kind, float(t_ns), fields, self._seq)
        self._events.append(ev)
        for cb, kinds in list(self._subs):
            if kinds is None or kind in kinds:
                cb(ev)
        return ev

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
