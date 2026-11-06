"""Shared helpers for the Kinetis models: SVD-derived register lists, peer lookup, IRQ numbers, gating."""
from __future__ import annotations

from typing import Any, Iterable

from ...regtable import regs_for
from ..base import MMIOBlock, Reg


def svd_regs(names: Iterable[str], regs_file: str | None = None):
    """First register table found among `names` (block name first, then the generic SVD name)."""
    for n in names:
        if not n:
            continue
        pr = regs_for(n, regs_file) if regs_file else regs_for(n)
        if pr is not None:
            return pr
    return None


def build_regs(names: Iterable[str], size: int, overrides: dict[str, dict] | None = None,
               regs_file: str | None = None, skip: Iterable[str] = ()) -> list[Reg]:
    """Reg descriptors from the SVD table for the first of `names`, with per-register keyword overrides
    (on_read, on_write, ro_mask, w1c_mask, reset). Registers outside the block or overlapping an earlier one
    (SVD alternate registers) are dropped."""
    pr = svd_regs(names, regs_file)
    if pr is None:
        raise KeyError(f"no SVD register table for any of {list(names)}")
    overrides = overrides or {}
    skip = set(skip)
    out: list[Reg] = []
    used: set[int] = set()
    for spec in pr.reg_specs():
        if spec["name"] in skip:
            continue
        spec.update(overrides.get(spec["name"], {}))
        off, w = spec["offset"], spec["width"]
        if off + w > size or any(i in used for i in range(off, off + w)):
            continue
        used.update(range(off, off + w))
        out.append(Reg(**spec))
    return out


def find_model(chip: Any, model: str, name: str | None = None) -> MMIOBlock | None:
    """Peer peripheral by name (preferred) or by model name."""
    if chip is None:
        return None
    periphs = getattr(chip, "peripherals", {}) or {}
    if name is not None:
        b = periphs.get(name)
        if b is not None and getattr(b, "model", None) == model:
            return b
    for b in periphs.values():
        if getattr(b, "model", None) == model:
            return b
    return None


def irq_number(chip: Any, *names: str) -> int | None:
    desc = getattr(chip, "desc", None)
    irqs = getattr(desc, "irqs", None) or {}
    for want in names:
        for n, nm in irqs.items():
            if nm == want:
                return n
    return None


def fmt_hz(hz: float) -> str:
    if hz >= 1e6:
        return f"{hz / 1e6:.3f} MHz"
    if hz >= 1e3:
        return f"{hz / 1e3:.3f} kHz"
    return f"{hz:.0f} Hz"


class GatedBlock(MMIOBlock):
    """MMIOBlock whose accesses are checked against a SIM clock gate (`gate` = SIM gate name, e.g. "PORTC").
    A gated-off access logs one `warn` per peripheral (real silicon bus-faults) and then proceeds."""

    gate: str | None = None
    _gate_loc: tuple[int, int] | None = None
    _sim: Any = None

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._sim = find_model(chip, "kinetis.sim", "SIM")
        self._gate_loc = self._sim.gate_location(self.gate) if (self._sim is not None and self.gate) else None

    def _gate_ok(self) -> bool:
        loc = self._gate_loc
        if loc is None:
            return True
        off, mask = loc
        if self._sim.mem[off] & mask:
            return True
        self._sim.check_gate(self.gate, self.name)
        return False

    def read(self, offset: int, size: int) -> int:
        if self._gate_loc is not None:
            self._gate_ok()
        return MMIOBlock.read(self, offset, size)

    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()
        MMIOBlock.write(self, offset, size, value)
