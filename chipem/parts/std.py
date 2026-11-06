"""Standard part types: inputs (switch, button, const/vcc/gnd, pulse, pattern), indicators (led, probe, counter)
and logic (not, buf, and, nand, or, nor, xor, xnor, mux2, dff, latch). See docs/CONTRACTS.md "Board parts".

Three-valued logic: "H", "L", "X" (an unconnected or undriven input reads X). X on an input gives X out except
where the logic dominates (AND with an L input = L, OR with an H input = H, a mux whose inputs agree).
"""
from __future__ import annotations

import re
from collections import deque
from typing import Any

from .base import Part, PortDesc, norm_level
from .registry import part

__all__ = ["Switch", "Button", "Const", "Pulse", "Pattern", "Led", "Probe", "Counter", "Gate", "Mux2", "Dff",
           "Latch"]


def _time(v: Any, what: str) -> float:
    from ..session import parse_time
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v) * 1e6                      # bare numbers are milliseconds, as everywhere in chipem
    try:
        return parse_time(str(v))
    except ValueError:
        raise ValueError(f"{what}: bad time {v!r} (e.g. 1ms, 250us)") from None


def _inv(lvl: str | None) -> str:
    return {"H": "L", "L": "H"}.get(lvl or "X", "X")


def _x(lvl: str | None) -> str:
    return lvl if lvl in ("H", "L") else "X"


def _bool(v: Any, what: str) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    t = str(v).strip().lower()
    if t in ("1", "true", "yes", "on", "pressed", "press", "down", "closed"):
        return True
    if t in ("0", "false", "no", "off", "released", "release", "up", "open"):
        return False
    raise ValueError(f"{what}: expected true/false (got {v!r})")


def _fmt_level(lvl: str | None) -> str:
    return lvl if lvl is not None else "Z"


# ================================================================================================ inputs
@part("switch")
class Switch(Part):
    """Toggle switch: drives `out` with its level (H, L, X, or Z = open). Keeps its position across resets."""
    PARAMS = {"level": "L"}
    PORTS = {"out": "out"}
    ID_PREFIX = "SW"

    def _setup(self) -> None:
        self.level = norm_level(self.params["level"], f"{self.id}.level", allow_none=True)
        self.params["level"] = _fmt_level(self.level)

    def _reset_state(self) -> None:
        self._out["out"] = self.level

    def state(self) -> dict:
        return {"level": _fmt_level(self.level)}

    def set(self, **values: Any) -> dict:
        for k, v in values.items():
            if k not in ("level", "on"):
                raise ValueError(f"part {self.id} (switch): cannot set {k!r} (use level)")
            lvl = norm_level(v, f"{self.id}.level", allow_none=True) if k == "level" else \
                ("H" if _bool(v, f"{self.id}.on") else "L")
            self.level = lvl
            self.params["level"] = _fmt_level(lvl)
            self._drive("out", lvl)
        return self.state()


@part("button")
class Button(Part):
    """Momentary push button: `out` = pressed_level while pressed, released_level otherwise (Z = open)."""
    PARAMS = {"pressed_level": "L", "released_level": "H", "pressed": False}
    PORTS = {"out": "out"}
    ID_PREFIX = "BTN"

    def _param_alias(self, key: str, value: Any) -> str:
        # contract spelling `pressed: L` / `released: H` (a level) for the levels; `pressed: true` is the state
        if key in ("pressed", "released", "active") and isinstance(value, str) and \
                value.strip().upper() in ("H", "L", "X", "Z", "HIGH", "LOW"):
            return "released_level" if key == "released" else "pressed_level"
        return key

    def _setup(self) -> None:
        self.pl = norm_level(self.params["pressed_level"], f"{self.id}.pressed_level", allow_none=True)
        self.rl = norm_level(self.params["released_level"], f"{self.id}.released_level", allow_none=True)
        self.params["pressed_level"], self.params["released_level"] = _fmt_level(self.pl), _fmt_level(self.rl)
        self.params["pressed"] = _bool(self.params["pressed"], f"{self.id}.pressed")
        self.pressed = self.params["pressed"]
        self.presses = 0

    def _level(self) -> str | None:
        return self.pl if self.pressed else self.rl

    def _reset_state(self) -> None:
        self.pressed = self.params["pressed"]
        self.presses = 0
        self._out["out"] = self._level()

    def state(self) -> dict:
        return {"pressed": self.pressed, "level": _fmt_level(self._level()), "presses": self.presses}

    def set(self, **values: Any) -> dict:
        for k, v in values.items():
            if k == "pressed":
                p = _bool(v, f"{self.id}.pressed")
            elif k == "level":
                p = norm_level(v, f"{self.id}.level", allow_none=True) == self.pl
            else:
                raise ValueError(f"part {self.id} (button): cannot set {k!r} (use pressed)")
            if p and not self.pressed:
                self.presses += 1
            if p != self.pressed:
                self.pressed = p
                self.emit(pressed=p)
            self._drive("out", self._level())
        return self.state()

    def stimulus(self, value: str) -> dict:
        t = str(value).strip().upper()
        if t in ("H", "L", "HIGH", "LOW", "X", "Z"):
            return self.set(level=value)
        return self.set(pressed=value)


@part("const")
class Const(Part):
    """Constant level source (tie-off): drives `out` with `level`."""
    PARAMS = {"level": "H"}
    PORTS = {"out": "out"}
    ID_PREFIX = "K"
    FIXED: str | None = None

    def _setup(self) -> None:
        if self.FIXED is not None:
            self.level = self.FIXED
            return
        self.level = norm_level(self.params["level"], f"{self.id}.level", allow_none=True)
        self.params["level"] = _fmt_level(self.level)

    def _reset_state(self) -> None:
        self._out["out"] = self.level

    def state(self) -> dict:
        return {"level": _fmt_level(self.level)}

    def set(self, **values: Any) -> dict:
        if self.FIXED is not None:
            return super().set(**values)
        for k, v in values.items():
            if k != "level":
                raise ValueError(f"part {self.id} (const): cannot set {k!r} (use level)")
            self.level = norm_level(v, f"{self.id}.level", allow_none=True)
            self.params["level"] = _fmt_level(self.level)
            self._drive("out", self.level)
        return self.state()


@part("vcc")
class Vcc(Const):
    """Supply rail: drives `out` high."""
    PARAMS: dict[str, Any] = {}
    ID_PREFIX = "VCC"
    FIXED = "H"


@part("gnd")
class Gnd(Const):
    """Ground: drives `out` low."""
    PARAMS: dict[str, Any] = {}
    ID_PREFIX = "GND"
    FIXED = "L"


@part("pulse")
class Pulse(Part):
    """Free-running clock on `out`: `period`, `duty` (high fraction), `start` level, `phase` delay of the first
    edge. Starts at reset/creation time; times are board (wall-clock) time."""
    PARAMS = {"period": "1ms", "duty": 0.5, "start": "L", "phase": "0ns"}
    PORTS = {"out": "out"}
    ID_PREFIX = "CLK"

    def _setup(self) -> None:
        self.period_ns = _time(self.params["period"], f"{self.id}.period")
        if self.period_ns <= 0:
            raise ValueError(f"part {self.id}: period must be > 0")
        self.duty = float(self.params["duty"])
        if not 0.0 <= self.duty <= 1.0:
            raise ValueError(f"part {self.id}: duty must be within 0..1")
        self.start = norm_level(self.params["start"], f"{self.id}.start")
        if self.start not in ("H", "L"):
            raise ValueError(f"part {self.id}: start must be H or L")
        self.params["start"] = self.start
        self.phase_ns = _time(self.params["phase"], f"{self.id}.phase")
        self.edges = 0

    def _seg(self, lvl: str) -> float:
        return self.period_ns * (self.duty if lvl == "H" else 1.0 - self.duty)

    def _reset_state(self) -> None:
        self.edges = 0
        if self.duty <= 0.0 or self.duty >= 1.0:          # degenerate: constant
            self._out["out"] = "H" if self.duty >= 1.0 else "L"
            return
        self._out["out"] = self.start
        self._t_next = self.now() + self.phase_ns + self._seg(self.start)
        self._at(self._t_next, self._edge)

    def _edge(self) -> None:
        new = _inv(self._out["out"])
        self.edges += 1
        self._t_next += self._seg(new)
        self._at(self._t_next, self._edge)              # absolute times: no drift
        self._drive("out", new)

    def state(self) -> dict:
        return {"level": _fmt_level(self._out.get("out")), "period_ns": self.period_ns,
                "freq_hz": 1e9 / self.period_ns, "duty": self.duty, "edges": self.edges}

    def set(self, **values: Any) -> dict:
        for k, v in values.items():
            if k not in self.PARAMS:
                raise ValueError(f"part {self.id} (pulse): cannot set {k!r} (use {', '.join(self.PARAMS)})")
            self.params[k] = v
        self._setup()
        self._cancel_all()
        before = self._out.get("out")
        self._reset_state()
        if self._out.get("out") != before and self.board is not None:
            lvl, self._out["out"] = self._out["out"], before
            self._drive("out", lvl)
        return self.state()

    def stimulus(self, value: str) -> dict:
        return self.set(period=value)


_STEP_SPLIT = re.compile(r"[;,\s]+")


def _parse_steps(v: Any, pid: str) -> list[tuple[float, str | None, Any]]:
    """[[time, level], ...] | [{t|time, level}, ...] | ["1ms:H", ...] | "0:L 1ms:H; 2ms:L" -> [(t_ns, lvl, raw_t)]."""
    if v is None:
        return []
    items = _STEP_SPLIT.split(v.strip()) if isinstance(v, str) else list(v)
    out = []
    for it in items:
        if it in ("", None):
            continue
        if isinstance(it, dict):
            t, lvl = it.get("t", it.get("time")), it.get("level")
        elif isinstance(it, (list, tuple)) and len(it) == 2:
            t, lvl = it
        elif isinstance(it, str) and (":" in it or "=" in it):
            t, lvl = re.split(r"[:=]", it, maxsplit=1)
        else:
            raise ValueError(f"part {pid}: bad pattern step {it!r} (expected [TIME, LEVEL] or 'TIME:LEVEL')")
        out.append((_time(t, f"{pid}.steps"), norm_level(lvl, f"{pid}.steps", allow_none=True), t))
    out.sort(key=lambda s: s[0])
    return out


@part("pattern")
class Pattern(Part):
    """Level sequence on `out`: `steps` [[TIME, LEVEL], ...] relative to reset; `repeat` with `period` loops it.
    Before the first step the output is undriven (Z)."""
    PARAMS = {"steps": [], "repeat": False, "period": None}
    PORTS = {"out": "out"}
    ID_PREFIX = "PAT"

    def _setup(self) -> None:
        self.steps = _parse_steps(self.params["steps"], self.id)
        self.params["steps"] = [[raw if isinstance(raw, str) else f"{t / 1e6:g}ms", _fmt_level(l)]
                                for t, l, raw in self.steps]
        self.repeat = _bool(self.params["repeat"], f"{self.id}.repeat")
        self.params["repeat"] = self.repeat
        p = self.params["period"]
        self.period_ns = _time(p, f"{self.id}.period") if p not in (None, "") else None
        if self.repeat:
            if not self.period_ns or self.period_ns <= 0:
                raise ValueError(f"part {self.id}: repeat needs a period > 0")
            if self.steps and self.steps[-1][0] >= self.period_ns:
                raise ValueError(f"part {self.id}: every step time must be < period")
        self.idx = 0
        self.cycles = 0

    def _reset_state(self) -> None:
        self._out["out"] = None
        self.idx = 0
        self.cycles = 0
        self._t0 = self.now()
        while self.idx < len(self.steps) and self.steps[self.idx][0] <= 0:     # steps at t=0 apply at once
            self._out["out"] = self.steps[self.idx][1]
            self.idx += 1
        self._sched()

    def _sched(self) -> None:
        if self.idx >= len(self.steps):
            if not self.repeat or not self.steps:
                return
            self.idx = 0
            self.cycles += 1
            self._t0 += self.period_ns
        self._at(self._t0 + self.steps[self.idx][0], self._step)

    def _step(self) -> None:
        lvl = self.steps[self.idx][1]
        self.idx += 1
        self._sched()
        self._drive("out", lvl)

    def state(self) -> dict:
        return {"level": _fmt_level(self._out.get("out")), "step": self.idx, "cycles": self.cycles}

    def set(self, **values: Any) -> dict:
        for k, v in values.items():
            if k not in self.PARAMS:
                raise ValueError(f"part {self.id} (pattern): cannot set {k!r} (use {', '.join(self.PARAMS)})")
            self.params[k] = v
        self._setup()
        self._cancel_all()
        before = self._out.get("out")
        self._reset_state()
        if self._out.get("out") != before and self.board is not None:
            lvl, self._out["out"] = self._out["out"], before
            self._drive("out", lvl)
        return self.state()

    def stimulus(self, value: str) -> dict:
        if str(value).strip().lower() in ("restart", "reset", "start"):
            return self.set()
        return self.set(steps=value)


# ================================================================================================ indicators
@part("led")
class Led(Part):
    """LED on `in`: lit while the net is at the `active` level (X is dark). Counts on/off changes."""
    PARAMS = {"active": "H", "color": "red"}
    PORTS = {"in": "in"}
    ID_PREFIX = "LED"

    def _setup(self) -> None:
        self.active = norm_level(self.params["active"], f"{self.id}.active")
        if self.active not in ("H", "L"):
            raise ValueError(f"part {self.id}: active must be H or L")
        self.params["active"] = self.active
        self.params["color"] = str(self.params["color"])
        self.on = False
        self.changes = 0

    def _reset_state(self) -> None:
        self.on = False
        self.changes = 0
        self._seen = False

    def on_input(self, port: str, level: str, t_ns: float) -> None:
        on = level == self.active
        if on != self.on:
            if self._seen:
                self.changes += 1
            self.on = on
            self.emit(port="in", on=on)
        if level in ("H", "L"):
            self._seen = True                   # changes count from the first defined level on

    def state(self) -> dict:
        return {"on": self.on, "changes": self.changes, "level": _x(self.inputs.get("in")),
                "color": self.params["color"]}

    def set(self, **values: Any) -> dict:
        for k, v in values.items():
            if k == "color":
                self.params["color"] = str(v)
            elif k == "active":
                self.params["active"] = v
                on, ch, seen = self.on, self.changes, self._seen
                self._setup()
                self.on, self.changes, self._seen = on, ch, seen
                self.on_input("in", _x(self.inputs.get("in")), self.now())
            else:
                raise ValueError(f"part {self.id} (led): cannot set {k!r} (use color, active)")
        return self.state()


@part("probe")
class Probe(Part):
    """Logic probe on `in`: level, rising/falling edge counts, time of the last change and the frequency of
    the rising edges (None while fewer than two rises, or once the signal has stopped for 4 periods)."""
    PARAMS: dict[str, Any] = {}
    PORTS = {"in": "in"}
    ID_PREFIX = "P"
    WINDOW = 32

    def _reset_state(self) -> None:
        self.rises = 0
        self.falls = 0
        self.last_change_ns: float | None = None
        self._rise_t: deque = deque(maxlen=self.WINDOW + 1)
        self.high_ns = 0.0
        self._t_high: float | None = None

    def on_input(self, port: str, level: str, t_ns: float) -> None:
        prev = getattr(self, "_prev", None)
        self._prev = level
        if prev is None:
            return
        self.last_change_ns = t_ns
        if prev == "L" and level == "H":
            self.rises += 1
            rt = self._rise_t
            if len(rt) >= 2:
                avg = (rt[-1] - rt[0]) / (len(rt) - 1)
                if t_ns - rt[-1] > 1.5 * avg:          # signal paused or slowed down: new measurement
                    rt.clear()
            rt.append(t_ns)
        elif prev == "H" and level == "L":
            self.falls += 1
        self.emit(port="in", level=level)

    def reset(self) -> None:
        self._prev = None
        super().reset()

    def freq_hz(self) -> float | None:
        rt = self._rise_t
        if len(rt) < 2:
            return None
        avg = (rt[-1] - rt[0]) / (len(rt) - 1)
        if avg <= 0:
            return None
        if self.board is not None and self.now() - rt[-1] > 4 * avg:
            return None
        return 1e9 / avg

    def state(self) -> dict:
        return {"level": _x(self.inputs.get("in")), "rises": self.rises, "falls": self.falls,
                "last_change_ns": self.last_change_ns, "freq_hz": self.freq_hz()}

    def set(self, **values: Any) -> dict:
        for k, v in values.items():
            if k != "clear":
                raise ValueError(f"part {self.id} (probe): cannot set {k!r} (use clear=1)")
            if _bool(v, f"{self.id}.clear"):
                prev = getattr(self, "_prev", None)
                self._reset_state()
                self._prev = prev
        return self.state()

    def stimulus(self, value: str) -> dict:
        return self.set(clear=value)


@part("counter")
class Counter(Part):
    """Edge counter on `in`: counts L->H (edge=rise), H->L (fall) or both transitions."""
    PARAMS = {"edge": "rise"}
    PORTS = {"in": "in"}
    ID_PREFIX = "CNT"

    def _setup(self) -> None:
        e = str(self.params["edge"]).lower()
        if e not in ("rise", "fall", "both"):
            raise ValueError(f"part {self.id}: edge must be rise|fall|both")
        self.params["edge"] = e
        self.count = 0

    def _reset_state(self) -> None:
        self.count = 0
        self._prev = None

    def on_input(self, port: str, level: str, t_ns: float) -> None:
        prev, self._prev = self._prev, level
        e = self.params["edge"]
        if (prev == "L" and level == "H" and e in ("rise", "both")) or \
                (prev == "H" and level == "L" and e in ("fall", "both")):
            self.count += 1
            self.emit(port="in", count=self.count)

    def state(self) -> dict:
        return {"count": self.count, "level": _x(self.inputs.get("in"))}

    def set(self, **values: Any) -> dict:
        for k, v in values.items():
            if k != "count":
                raise ValueError(f"part {self.id} (counter): cannot set {k!r} (use count)")
            self.count = int(v)
            self.emit(count=self.count)
        return self.state()

    def stimulus(self, value: str) -> dict:
        return self.set(count=int(str(value), 0))


# ================================================================================================ logic
def _and(vals: list[str]) -> str:
    if "L" in vals:
        return "L"
    return "H" if all(v == "H" for v in vals) else "X"


def _or(vals: list[str]) -> str:
    if "H" in vals:
        return "H"
    return "L" if all(v == "L" for v in vals) else "X"


def _xor(vals: list[str]) -> str:
    if any(v not in ("H", "L") for v in vals):
        return "X"
    return "H" if sum(v == "H" for v in vals) % 2 else "L"


class Gate(Part):
    """Combinational gate with inputs in0..in{n-1} and output out (zero delay)."""
    PARAMS = {"inputs": 2}
    ID_PREFIX = "U"
    FN = staticmethod(_and)
    INVERT = False

    @classmethod
    def port_descs(cls, params: dict[str, Any]) -> dict[str, PortDesc]:
        n = int(params.get("inputs", 2))
        if not 1 <= n <= 16:
            raise ValueError("gate inputs must be within 1..16")
        d = {f"in{i}": PortDesc("in") for i in range(n)}
        d["out"] = PortDesc("out")
        return d

    def _setup(self) -> None:
        try:
            self.params["inputs"] = int(self.params["inputs"])
        except (TypeError, ValueError):
            raise ValueError(f"part {self.id}: inputs must be an integer") from None
        if not 1 <= self.params["inputs"] <= 16:
            raise ValueError(f"part {self.id}: inputs must be within 1..16")

    def _eval(self) -> str:
        r = self.FN([_x(v) for k, v in sorted(self.inputs.items())])
        return _inv(r) if self.INVERT else r

    def _reset_state(self) -> None:
        self._out["out"] = self._eval()

    def on_input(self, port: str, level: str, t_ns: float) -> None:
        self._drive("out", self._eval())

    def state(self) -> dict:
        return {"out": _fmt_level(self._out.get("out")),
                "inputs": {k: _x(v) for k, v in sorted(self.inputs.items(), key=lambda kv: int(kv[0][2:]))}}


def _gate(name: str, fn, invert: bool, doc: str) -> type:
    cls = type(name.capitalize() + "Gate", (Gate,), {"FN": staticmethod(fn), "INVERT": invert, "__doc__": doc})
    return part(name)(cls)


_gate("and", _and, False, "AND gate: out = in0 & in1 & ... (any L -> L).")
_gate("nand", _and, True, "NAND gate: out = !(in0 & in1 & ...).")
_gate("or", _or, False, "OR gate: out = in0 | in1 | ... (any H -> H).")
_gate("nor", _or, True, "NOR gate: out = !(in0 | in1 | ...).")
_gate("xor", _xor, False, "XOR gate: out = odd parity of the inputs.")
_gate("xnor", _xor, True, "XNOR gate: out = even parity of the inputs.")


class _Unary(Gate):
    PARAMS: dict[str, Any] = {}
    PORTS = {"in": "in", "out": "out"}

    @classmethod
    def port_descs(cls, params: dict[str, Any]) -> dict[str, PortDesc]:
        return {"in": PortDesc("in"), "out": PortDesc("out")}

    def _setup(self) -> None:
        pass

    def _eval(self) -> str:
        v = _x(self.inputs.get("in"))
        return _inv(v) if self.INVERT else v

    def state(self) -> dict:
        return {"out": _fmt_level(self._out.get("out")), "in": _x(self.inputs.get("in"))}


@part("not")
class Not(_Unary):
    """Inverter: out = !in."""
    INVERT = True


@part("buf")
class Buf(_Unary):
    """Buffer: out = in."""
    INVERT = False


@part("mux2")
class Mux2(Part):
    """2:1 multiplexer: out = a while sel is L, b while sel is H (X sel: a if a == b, else X)."""
    PORTS = {"a": "in", "b": "in", "sel": "in", "out": "out"}
    ID_PREFIX = "U"

    def _eval(self) -> str:
        a, b, s = (_x(self.inputs.get(k)) for k in ("a", "b", "sel"))
        if s == "L":
            return a
        if s == "H":
            return b
        return a if a == b else "X"

    def _reset_state(self) -> None:
        self._out["out"] = self._eval()

    def on_input(self, port: str, level: str, t_ns: float) -> None:
        self._drive("out", self._eval())

    def state(self) -> dict:
        return {"out": _fmt_level(self._out.get("out")),
                **{k: _x(self.inputs.get(k)) for k in ("a", "b", "sel")}}


@part("dff")
class Dff(Part):
    """D flip-flop: q takes d on the clk edge (`edge` rise|fall); qn = !q; `rst` at `rst_active` forces q to
    `init` asynchronously (unconnected/X rst is inactive). q starts at `init`."""
    PARAMS = {"edge": "rise", "init": "L", "rst_active": "H"}
    PORTS = {"d": "in", "clk": "in", "rst": "in", "q": "out", "qn": "out"}
    ID_PREFIX = "FF"

    def _setup(self) -> None:
        e = str(self.params["edge"]).lower()
        if e not in ("rise", "fall"):
            raise ValueError(f"part {self.id}: edge must be rise|fall")
        self.params["edge"] = e
        self.init = norm_level(self.params["init"], f"{self.id}.init")
        self.ra = norm_level(self.params["rst_active"], f"{self.id}.rst_active")
        self.params["init"], self.params["rst_active"] = self.init, self.ra
        self.clocks = 0

    def _reset_state(self) -> None:
        self.clocks = 0
        self._clk = None
        self._out["q"] = self.init
        self._out["qn"] = _inv(self.init)

    def _set_q(self, q: str) -> None:
        self._drive("q", q)
        self._drive("qn", _inv(q))

    def on_input(self, port: str, level: str, t_ns: float) -> None:
        if self.inputs.get("rst") == self.ra:
            self._clk = self.inputs.get("clk")
            self._set_q(self.init)
            return
        if port != "clk":
            return
        prev, self._clk = self._clk, level
        want = ("L", "H") if self.params["edge"] == "rise" else ("H", "L")
        if (prev, level) == want:
            self.clocks += 1
            self._set_q(_x(self.inputs.get("d")))

    def state(self) -> dict:
        return {"q": _fmt_level(self._out.get("q")), "clocks": self.clocks}


@part("latch")
class Latch(Part):
    """Transparent D latch: q follows d while en is H, holds while en is L (X en with d != q gives X)."""
    PORTS = {"d": "in", "en": "in", "q": "out"}
    PARAMS = {"init": "L"}
    ID_PREFIX = "FF"

    def _setup(self) -> None:
        self.init = norm_level(self.params["init"], f"{self.id}.init")
        self.params["init"] = self.init

    def _reset_state(self) -> None:
        self._out["q"] = self.init

    def on_input(self, port: str, level: str, t_ns: float) -> None:
        en, d = _x(self.inputs.get("en")), _x(self.inputs.get("d"))
        if en == "H":
            self._drive("q", d)
        elif en == "X" and d != self._out.get("q"):
            self._drive("q", "X")

    def state(self) -> dict:
        return {"q": _fmt_level(self._out.get("q"))}
