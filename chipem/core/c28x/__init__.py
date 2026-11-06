"""Pure-Python TI C28x (+FPU32) core: interpreter (core.py), ISA table loader (isa.py, arch/c28x/isa.yaml),
semantics (sem.py, fpu.py), assembler / disassembler (asm.py).  See docs/C28X_CORE.md."""
from __future__ import annotations

from .core import C28xCore


def create_core(cpu: str = "c28x", *, fpu: bool = True, precise: bool = False) -> C28xCore:
    return C28xCore(cpu, fpu=fpu, precise=precise)


__all__ = ["C28xCore", "create_core"]
