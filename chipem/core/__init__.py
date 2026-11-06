"""CPU core backends behind the `Core` interface."""
from __future__ import annotations

from .base import Core, StopReason

__all__ = ["Core", "StopReason", "create_core"]


def create_core(backend: str, cpu: str, *, fpu: bool = True, precise: bool = False) -> Core:
    if backend == "unicorn":
        from .unicorn_cm import UnicornCortexM
        return UnicornCortexM(cpu, fpu=fpu, precise=precise)
    raise ValueError(f"unknown core backend {backend!r}")
