"""Incremental quadrature encoder part (rotary encoder / motor shaft encoder) for eQEP / FTM / TIM decoder inputs.

Generic glue (no vendor knowledge): it only drives three nets.  See docs/CONTRACTS.md "Board parts".
"""
from __future__ import annotations

import re
from typing import Any

from .base import Part
from .registry import part
from .std import _bool, _fmt_level

__all__ = ["QuadratureEncoder"]

_FWD = {"cw": True, "fwd": True, "forward": True, "+": True, "up": True,
        "ccw": False, "rev": False, "reverse": False, "backward": False, "-": False, "down": False}
_QUAD = (("L", "L"), ("H", "L"), ("H", "H"), ("L", "H"))      # (A, B) at position p mod 4, forward = A leads B
_NUM_UNIT = re.compile(r"^\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*([a-zA-Z]*)\s*$")


def _num(v: Any, what: str, *, allow_none: bool = False) -> float | None:
    if v is None or (isinstance(v, str) and v.strip().lower() in ("", "none")):
        if allow_none:
            return None
        raise ValueError(f"{what}: a number is required")
    if isinstance(v, bool):
        raise ValueError(f"{what}: expected a number, got {v!r}")
    if isinstance(v, (int, float)):
        x = float(v)
    else:
        m = _NUM_UNIT.match(str(v))
        if not m:
            raise ValueError(f"{what}: expected a number, got {v!r}")
        mult = {"": 1.0, "rpm": 1.0, "hz": 1.0, "khz": 1e3, "mhz": 1e6}.get(m.group(2).lower())
        if mult is None:
            raise ValueError(f"{what}: unknown unit in {v!r}")
        x = float(m.group(1)) * mult
    if x < 0:
        raise ValueError(f"{what}: must be >= 0")
    return x


@part("quadrature_encoder")
class QuadratureEncoder(Part):
    """Incremental encoder: square waves on `a` and `b` 90 degrees apart (cw / forward: A leads B), `ppr` pulses
    (lines) per revolution, so 4 * ppr quadrature edges per revolution at `rpm` (or `hz` = quadrature edge rate,
    which overrides rpm); `rpm: 0` stops it.  `index: true` drives `i` high for one A period (4 edges) once per
    revolution (position 0..3 of 4 * ppr); it starts half a revolution away from the index.  Edges are not logged
    (a VCD still records them); `set` / `--at T:ENC=ccw|cw|stop|600rpm|40khz|restart` or `ENC.rpm=...` change it
    at run time without losing the position."""
    PARAMS = {"ppr": 100, "rpm": 60.0, "hz": None, "direction": "cw", "index": True}
    PORTS = {"a": "out", "b": "out", "i": "out"}
    ID_PREFIX = "ENC"

    def _setup(self) -> None:
        try:
            ppr = int(self.params["ppr"])
        except (TypeError, ValueError):
            raise ValueError(f"part {self.id}: ppr must be an integer") from None
        if ppr <= 0:
            raise ValueError(f"part {self.id}: ppr must be > 0")
        self.params["ppr"] = self.ppr = ppr
        self.rpm = _num(self.params["rpm"], f"{self.id}.rpm")
        self.params["rpm"] = self.rpm
        self.hz = _num(self.params["hz"], f"{self.id}.hz", allow_none=True)
        self.params["hz"] = self.hz
        d = str(self.params["direction"]).strip().lower()
        if d not in _FWD:
            raise ValueError(f"part {self.id}: direction must be cw or ccw (got {self.params['direction']!r})")
        self.forward = _FWD[d]
        self.params["direction"] = "cw" if self.forward else "ccw"
        self.index = _bool(self.params["index"], f"{self.id}.index")
        self.params["index"] = self.index

    # ---------------------------------------------------------------- timing
    @property
    def counts(self) -> int:
        """Quadrature edges per revolution."""
        return 4 * self.ppr

    def edge_hz(self) -> float:
        return self.hz if self.hz is not None else self.counts * self.rpm / 60.0

    def _levels(self) -> dict[str, str | None]:
        a, b = _QUAD[self.pos % 4]
        i = ("H" if self.pos % self.counts < 4 else "L") if self.index else None
        return {"a": a, "b": b, "i": i}

    def _reset_state(self) -> None:
        self.pos = self.counts // 2                     # half a revolution before the index
        self.edges = 0
        self._out.update(self._levels())
        self._restart_timer()

    def _restart_timer(self) -> None:
        """Next edge one edge period after now; absolute times from here on (no drift)."""
        self._cancel_all()
        hz = self.edge_hz()
        self._dt = 1e9 / hz if hz > 0 else None
        self._t0 = self.now()
        self._k = 0
        self._sched()

    def _sched(self) -> None:
        if self._dt is not None and self.board is not None:
            self._at(self._t0 + (self._k + 1) * self._dt, self._step)

    def _step(self) -> None:
        self._k += 1
        self.pos += 1 if self.forward else -1
        self.edges += 1
        self._sched()
        new = self._levels()
        for port in ("a", "b", "i"):
            self._drive(port, new[port], quiet=True)    # per-edge changes reach listeners (VCD), not the log

    # ---------------------------------------------------------------- user interface
    def state(self) -> dict:
        return {"position": self.pos, "revolutions": self.pos / self.counts, "direction": self.params["direction"],
                "rpm": (self.edge_hz() * 60.0 / self.counts), "edge_hz": self.edge_hz(), "edges": self.edges,
                "a": _fmt_level(self._out.get("a")), "b": _fmt_level(self._out.get("b")),
                "i": _fmt_level(self._out.get("i"))}

    def set(self, **values: Any) -> dict:
        restart = False
        for k, v in values.items():
            if k == "restart":
                restart = _bool(v, f"{self.id}.restart")
                continue
            if k not in self.PARAMS:
                raise ValueError(f"part {self.id} (quadrature_encoder): cannot set {k!r} "
                                 f"(use {', '.join(self.PARAMS)}, restart)")
            if k == "rpm":
                self.params["hz"] = None                # an explicit speed in rpm replaces an edge rate
            self.params[k] = v
        self._setup()
        if restart:
            self.pos = self.counts // 2
            self.edges = 0
            new = self._levels()
            for port in ("a", "b", "i"):
                self._drive(port, new[port])
        elif "index" in values or "ppr" in values:
            new = self._levels()
            for port in ("a", "b", "i"):
                self._drive(port, new[port])
        if self.board is not None:
            self._restart_timer()
        return self.state()

    def stimulus(self, value: str) -> dict:
        """`cw` / `ccw` (direction), `stop`, `600rpm` or a bare number (rpm), `40khz` / `500hz` (edge rate),
        `restart` (back to the start position); several separated by spaces or commas."""
        kw: dict[str, Any] = {}
        for tok in re.split(r"[\s,;]+", str(value).strip()):
            t = tok.lower()
            if not t:
                continue
            if t in _FWD:
                kw["direction"] = t
            elif t in ("stop", "stopped", "off"):
                kw["rpm"], kw["hz"] = 0, None
            elif t in ("restart", "reset"):
                kw["restart"] = True
            elif t.endswith("hz"):
                kw["hz"] = t
                kw.pop("rpm", None)
            else:
                kw["rpm"] = t
        if not kw:
            raise ValueError(f"part {self.id}: expected cw, ccw, stop, restart, <n>rpm or <n>hz (got {value!r})")
        hz = kw.pop("hz", "__keep__")
        st = self.set(**kw)
        if hz != "__keep__":
            st = self.set(hz=hz)
        return st
