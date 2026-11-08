"""Part base class: a board-level component (switch, LED, gate, ...) with named ports attached to nets.

Contract: docs/CONTRACTS.md "Board parts". Levels are "H" | "L" | "X"; an output port drives a level or None
(high impedance). The board calls `on_input(port, level, t_ns)` when the level of the net on an input port
changes (`inputs[port]` is already updated); a part changes an output with `_drive(port, level)`, which logs a
`part` event and asks the board to re-resolve that port's net (`board.part_drive_changed`). Every type takes
`quiet: true`: its per-change `part` events (levels, LED on/off, counts) are then not logged (add/remove/set
actions still are); state, nets and `listeners` (the VCD writer) follow every change regardless. Timed parts schedule
with `_at(t_ns, cb)` / `_in(dt_ns, cb)` (board wall-clock domain); `reset()` cancels those events.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

__all__ = ["Part", "PortDesc", "norm_level", "level_or_none"]


@dataclass(frozen=True)
class PortDesc:
    dir: str                                # "in" | "out" | "inout"

    @property
    def is_input(self) -> bool:
        return self.dir in ("in", "inout")

    @property
    def is_output(self) -> bool:
        return self.dir in ("out", "inout")


def _flag(v: Any, what: str) -> bool:
    if isinstance(v, bool):
        return v
    t = str(v).strip().lower()
    if t in ("1", "true", "yes", "on"):
        return True
    if t in ("0", "false", "no", "off", "", "none"):
        return False
    raise ValueError(f"{what}: expected true/false, got {v!r}")


def level_or_none(v: Any) -> str | None:
    """1/H/high -> 'H', 0/L -> 'L', X -> 'X', Z/none/open -> None."""
    from ..session import parse_level
    return parse_level(v)


def norm_level(v: Any, what: str = "level", *, allow_none: bool = False) -> str | None:
    lvl = level_or_none(v)
    if lvl is None and not allow_none:
        raise ValueError(f"{what}: high impedance (Z) is not allowed here")
    return lvl


class Part:
    """Base class. Subclasses set PARAMS (name -> default) and PORTS (name -> "in"|"out"|"inout") or override
    `port_descs(params)`, and implement `_setup()` (parse params), `_reset_state()`, `on_input()`, `state()`."""

    type = "part"
    PARAMS: dict[str, Any] = {}
    PORTS: dict[str, str] = {}
    ID_PREFIX = "U"

    def __init__(self, part_id: str, params: dict[str, Any] | None = None):
        self.id = str(part_id)
        p = dict(self.default_params())
        params = dict(params or {})
        # `quiet` (every type): per-change `part` events are not logged (state, nets and VCD still follow)
        self.quiet = _flag(params.pop("quiet", False), f"part {self.id}: quiet")
        for k, v in params.items():
            k = self._param_alias(str(k), v)
            if k not in p:
                raise ValueError(f"part {self.id} ({self.type}): unknown parameter {k!r} "
                                 f"(known: {', '.join(p) or 'none'})")
            p[k] = v
        self.params: dict[str, Any] = p
        self._setup()
        if self.quiet:
            self.params["quiet"] = True             # (only when set: exported board YAML stays unchanged)
        # change listeners: cb(part, fields) for every state change, logged or quiet (the VCD writer uses them)
        self.listeners: list[Callable[[Any, dict], None]] = []
        self.ports: dict[str, PortDesc] = self.port_descs(self.params)
        self.board: Any = None
        self.inputs: dict[str, str | None] = {n: None for n, d in self.ports.items() if d.is_input}
        self._out: dict[str, str | None] = {n: None for n, d in self.ports.items() if d.is_output}
        self._events: list = []
        self._reset_state()                 # usable before attach (no board: no timers, no events)

    # ---------------------------------------------------------------- class description
    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return dict(cls.PARAMS)

    @classmethod
    def port_descs(cls, params: dict[str, Any]) -> dict[str, PortDesc]:
        return {n: PortDesc(d) for n, d in cls.PORTS.items()}

    def _param_alias(self, key: str, value: Any) -> str:
        return key

    # ---------------------------------------------------------------- lifecycle
    def _setup(self) -> None:
        """Parse/validate self.params (raise ValueError)."""

    def attach(self, board: Any) -> None:
        self.board = board

    def detach(self) -> None:
        self._cancel_all()
        self.board = None

    def reset(self) -> None:
        """Power-on state: cancel timers, forget input levels (the board re-delivers them), restart. Outputs and
        LED state that the reset changes are reported like any other change (`part` event with action=reset,
        listeners such as the VCD writer); the caller re-resolves the nets."""
        self._cancel_all()
        out0, on0 = dict(self._out), getattr(self, "on", None)
        for k in self.inputs:
            self.inputs[k] = None
        self._reset_state()
        if self.board is None:
            return
        for port, old in out0.items():
            new = self._out.get(port)
            if new != old:
                self.emit(port=port, level=new if new is not None else "Z", action="reset")
        on = getattr(self, "on", None)
        if on0 is not None and on != on0:
            self.emit(port="in", on=on, action="reset")

    def _reset_state(self) -> None:
        for k in self._out:
            self._out[k] = None

    # ---------------------------------------------------------------- board interface
    def on_input(self, port: str, level: str, t_ns: float) -> None:
        """An input port's net changed level (self.inputs[port] already holds it)."""

    def drive(self, port: str) -> str | None:
        return self._out.get(port)

    def state(self) -> dict:
        return {}

    def set(self, **values: Any) -> dict:
        if values:
            raise ValueError(f"part {self.id} ({self.type}) has nothing to set "
                             f"(got {', '.join(values)})")
        return self.state()

    def stimulus(self, value: str) -> dict:
        """`--set ID=VALUE` / `--at T:ID=VALUE`: default sets `level`."""
        return self.set(level=value)

    def to_dict(self) -> dict:
        ports = {}
        for name in self.ports:
            net = self.board.part_net(self.id, name) if self.board is not None else None
            ports[name] = net.name if net is not None else None
        return {"id": self.id, "type": self.type, **self.params, "ports": ports, "state": self.state()}

    # ---------------------------------------------------------------- helpers for subclasses
    def now(self) -> float:
        return self.board.part_now_ns() if self.board is not None else 0.0

    def _drive(self, port: str, level: str | None, *, quiet: bool = False) -> bool:
        """Set an output port; returns True if it changed (then logged and propagated)."""
        if self._out.get(port, "?") == level:
            return False
        self._out[port] = level
        if self.board is not None:
            fields = {"port": port, "level": level if level is not None else "Z"}
            if quiet:
                self._notify(fields)
            else:
                self.emit(**fields)
            self.board.part_drive_changed(self, port)
        return True

    def emit(self, **fields: Any) -> None:
        """Log a `part` event. Per-change events (no `action`) of a `quiet` part only reach the listeners."""
        if self.board is None:
            return
        self._notify(fields)
        if self.quiet and "action" not in fields:
            return
        self.board.part_event(self, **fields)

    def _notify(self, fields: dict) -> None:
        for cb in self.listeners:
            cb(self, fields)

    def _at(self, t_ns: float, cb: Callable, *args: Any):
        if self.board is None:
            return None
        ev = self.board.sched_at_ns(t_ns, self._fire, cb, args)
        self._events.append(ev)
        return ev

    def _in(self, dt_ns: float, cb: Callable, *args: Any):
        return self._at(self.now() + float(dt_ns), cb, *args)

    def _fire(self, cb: Callable, args: tuple) -> None:
        self._events = [e for e in self._events if not getattr(e, "cancelled", False)]
        cb(*args)

    def _cancel_all(self) -> None:
        b = self.board
        for ev in self._events:
            if b is not None:
                b.sched_cancel(ev)
            elif ev is not None:
                ev.cancelled = True
        self._events = []

    def __repr__(self) -> str:
        return f"<{self.type} {self.id} {self.state()}>"
