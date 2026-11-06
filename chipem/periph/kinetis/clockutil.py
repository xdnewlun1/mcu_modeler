"""Small helpers shared by the Kinetis timer/UART models: clock lookup, precise "now", IRQ numbers, regs.

Clock sources (in order of preference):
  core_hz(chip): chip.clock.hz (the MCG model / --clock keep it current).
  bus_hz(chip):  `chip.bus_hz` (attribute or callable) if something sets it, else the MCG model's
                 `bus_hz_effective` (bus clock consistent with the actual core clock, honours --clock), else
                 `SIM.bus_hz()` if the SIM model provides it, else core_hz / 2.
"""
from __future__ import annotations

from typing import Any

from ..base import Reg
from ._util import irq_number  # noqa: F401  (re-exported: one IRQ-name lookup for all Kinetis models)


def core_hz(chip: Any) -> float:
    return float(chip.clock.hz)


def bus_hz(chip: Any) -> float:
    v = getattr(chip, "bus_hz", None)
    if v is not None:
        try:
            v = v() if callable(v) else v
            if v:
                return float(v)
        except Exception:
            pass
    mcg = getattr(chip, "peripherals", {}).get("MCG")
    if mcg is not None and hasattr(mcg, "bus_hz_effective"):
        try:
            v = mcg.bus_hz_effective
            if v:
                return float(v)
        except Exception:
            pass
    sim = getattr(chip, "peripherals", {}).get("SIM")
    fn = getattr(sim, "bus_hz", None)
    if fn is not None:
        try:
            v = fn() if callable(fn) else fn
            if v:
                return float(v)
        except Exception:
            pass
    return core_hz(chip) / 2.0


def now_cycles(chip: Any) -> int:
    """Core cycles including instructions already executed in the running slice (Chip.now_cycles)."""
    f = getattr(chip, "now_cycles", None)
    if f is not None:
        return f()
    return chip.clock.cycles


def now_ns(chip: Any) -> float:
    f = getattr(chip, "now_ns", None)
    if f is not None:
        return f()
    return chip.clock.now_ns()


def regs_from_table(block_name: str, fallback: list[tuple[str, int, int, int]], regs_file: str | None = None,
                    **overrides) -> list[Reg]:
    """Reg descriptors from chips/mk22_regs.yaml (`regs_for`), skipping overlapping alternates; `fallback` is
    [(name, offset, width, reset)] used when the table lacks the block.  overrides: name -> dict of Reg kwargs."""
    specs: list[dict] = []
    try:
        from ...regtable import regs_for
        pr = regs_for(block_name, regs_file) if regs_file else regs_for(block_name)
        if pr is not None:
            specs = pr.reg_specs()
    except Exception:
        specs = []
    if not specs:
        specs = [dict(name=n, offset=o, width=w, reset=r, ro_mask=0, w1c_mask=0) for n, o, w, r in fallback]
    out: list[Reg] = []
    used: set[int] = set()
    for s in specs:
        rng = set(range(s["offset"], s["offset"] + s["width"]))
        if rng & used:
            continue
        used |= rng
        kw = dict(s)
        kw.update(overrides.get(s["name"], {}))
        out.append(Reg(**kw))
    return out
