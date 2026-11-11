"""Shared helpers for the MCS-51 family models (chipem/periph/mcs51, NXP P89CV51RB2/RC2/RD2).

* SFR blocks are sparse (docs/CONTRACTS.md "MCS-51 address spaces"): base = the SFR tag 0x30000, size 0x100, a
  register's offset is its SFR address, and `ranges` lists the SFRs the Bus maps.  Registers come from the chip's
  hand-written table (chips/p89cv51_regs.yaml) through `build_regs`; models only add behaviour.
* Time: the Chip clock runs at the oscillator frequency (one scheduler cycle = one oscillator period), so a
  peripheral clocked every machine cycle counts every `periph_div(chip, "T0X2")` cycles: 12 in 12-clock mode, 6 when
  the CPU runs in 6-clock mode (CKCON.X2 or the FX2 flash bit) and the peripheral's CKCON bit is 0 (data sheet
  Table 52 p.58).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable

from ...regtable import regs_for
from ..base import MMIOBlock, Reg

SFR_BASE = 0x30000
DEFAULT_REGS = Path(__file__).resolve().parents[3] / "chips" / "p89cv51_regs.yaml"


def build_regs(names: Iterable[str], overrides: dict[str, dict] | None = None,
               regs_file: str | None = None) -> list[Reg]:
    """Reg descriptors (offset = SFR address) of the first register-table peripheral found among `names`, with
    per-register keyword overrides (on_read, on_write, ro_mask, w1c_mask, reset)."""
    pr = None
    for n in names:
        if n:
            pr = regs_for(n, regs_file or DEFAULT_REGS)
            if pr is not None:
                break
    if pr is None:
        raise KeyError(f"no register table entry for any of {list(names)}")
    overrides = overrides or {}
    out = []
    for spec in pr.reg_specs(8):
        spec.update(overrides.get(spec["name"], {}))
        out.append(Reg(**spec))
    return out


def sfr_ranges(regs: Iterable[Reg]) -> list[tuple[int, int]]:
    offs = sorted(o for r in regs for o in range(r.offset, r.offset + r.width))
    rng: list[list[int]] = []
    for o in offs:
        if rng and rng[-1][0] + rng[-1][1] == o:
            rng[-1][1] += 1
        else:
            rng.append([o, 1])
    return [(SFR_BASE + o, n) for o, n in rng]


class SfrBlock(MMIOBlock):
    """Base of the MCS-51 SFR models: a sparse block whose offsets are SFR addresses."""

    table: tuple[str, ...] = ()          # register-table peripheral name(s)

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None,
                 overrides: dict[str, dict] | None = None):
        params = dict(params or {})
        if not regs:
            regs = build_regs((params.get("table"), name, *self.table), overrides, params.get("regs_file"))
        super().__init__(name, SFR_BASE, 0x100, regs, ram_like=False, log=log, params=params)
        self.ranges = sfr_ranges(self.regs.values())

    # ---- helpers ------------------------------------------------------------------------------------------------
    _at: int | None = None              # due cycle of the event being processed (see `at`)

    def now(self) -> int:
        """Current time in oscillator periods (instruction-precise inside a slice).  Inside one of this block's own
        scheduler events it is the event's due cycle: a slice can end up to one instruction after an event is due
        (the core finishes the instruction it started), and counters must change at their exact count."""
        at = self._at
        return at if at is not None else self.chip.now_cycles()

    def irq_line(self, line: str, level: bool) -> None:
        self.chip.arch.set_irq(line, level)

    def at(self, cycles_abs: int, cb: Callable, *args):
        """Schedule cb(*args) at an absolute cycle; while it runs, `now()` returns that cycle."""
        due = int(cycles_abs)
        return self.chip.sched.at_cycles(due, self._fire, due, cb, args)

    def _fire(self, due: int, cb: Callable, args: tuple) -> None:
        """Run one of our events at its due cycle: `now()`, our log events and the pin edges it causes are stamped
        with the due time, not with the end of the instruction the slice finished on."""
        prev, self._at = self._at, due
        arch = self.chip.arch
        prev_due, arch.event_due = getattr(arch, "event_due", None), due    # flag-set time for the polling cycle
        pb = self.chip.pins
        tf = pb.time_fn if pb is not None else None
        if pb is not None:
            clock = self.chip.clock
            pb.time_fn = lambda: clock.cycles_to_ns(due)
        try:
            cb(*args)
        finally:
            self._at = prev
            arch.event_due = prev_due
            if pb is not None:
                pb.time_fn = tf

    def now_ns(self) -> float:
        at = self._at
        if at is not None:
            return self.chip.clock.cycles_to_ns(at)
        return super().now_ns()

    def cancel(self, ev) -> None:
        if ev is not None and self.chip is not None:
            self.chip.sched.cancel(ev)


def find_model(chip: Any, model: str) -> Any:
    """The chip's (first) block of `model`, cached per chip (blocks never change after construction)."""
    cache = getattr(chip, "_mcs51_models", None)
    if cache is None:
        cache = {}
        try:
            chip._mcs51_models = cache
        except AttributeError:
            pass
    b = cache.get(model, cache)
    if b is not cache:
        return b
    b = None
    for blk in (getattr(chip, "peripherals", None) or {}).values():
        if getattr(blk, "model", None) == model:
            b = blk
            break
    if b is not None or len(getattr(chip, "peripherals", None) or {}) > 0:
        cache[model] = b
    return b


def sysctrl(chip: Any):
    return find_model(chip, "mcs51.sysctrl")


def periph_div(chip: Any, bit: str | None = None) -> int:
    """Oscillator periods per peripheral clock (machine) cycle for the CKCON bit `bit` ("T0X2", "SIX2", ...)."""
    sc = sysctrl(chip)
    if sc is None:
        return int(getattr(chip.core, "clocks_per_mc", 12))
    return sc.periph_div(bit)


def ports(chip: Any):
    return find_model(chip, "mcs51.port")


def listen_pin(chip: Any, pin: str, cb: Callable[[str], None]) -> bool:
    """cb(level "H"|"L"|"X") on every level change of pin `pin` (external stimulus, nets, and the chip's own port
    drive: clearing P3.2 pulls INT0 low on silicon too).  False when the chip has no such pin."""
    pb = getattr(chip, "pins", None)
    if pb is None:
        return False
    try:
        p = pb.get(pin)
    except KeyError:
        return False
    pb.input_listeners[p.id].append(lambda _p: cb(_p.level()))
    return True


def pin_level(chip: Any, pin: str) -> str:
    pb = getattr(chip, "pins", None)
    if pb is None:
        return "H"
    try:
        return pb.get(pin).level()
    except KeyError:
        return "H"


def fmt_hz(hz: float) -> str:
    if hz >= 1e6:
        return f"{hz / 1e6:.4f} MHz"
    if hz >= 1e3:
        return f"{hz / 1e3:.3f} kHz"
    return f"{hz:.1f} Hz"
