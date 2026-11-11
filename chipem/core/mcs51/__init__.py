"""Pure-Python MCS-51 (8051) core: interpreter (core.py), instruction table (isa.py), assembler / disassembler
(asm.py).  See docs/MCS51_CORE.md."""
from __future__ import annotations

from .core import CODE_BASE, IDATA_BASE, SFR_BASE, XDATA_BASE, XRAM_BASE, MCS51Core, space_of


def create_core(cpu: str = "80c51", *, fpu: bool = False, precise: bool = False) -> MCS51Core:
    return MCS51Core(cpu, fpu=fpu, precise=precise)


__all__ = ["MCS51Core", "create_core", "CODE_BASE", "XDATA_BASE", "IDATA_BASE", "SFR_BASE", "XRAM_BASE", "space_of"]
