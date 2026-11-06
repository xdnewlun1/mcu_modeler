"""Peripheral model registry: model name (e.g. "kinetis.port") → MMIOBlock subclass.

Models register with `@register("family.model")`.  `create()` imports `chipem.periph.<family>` /
`chipem.periph.<family>.<model>` on demand, and falls back to the RAM-like `stub` model (with a warning)
for unknown names so chip descriptions that reference not-yet-written models still load.
Models are constructed as `cls(name, base, size, params=params)`.
"""
from __future__ import annotations

import importlib
from typing import Any, Callable

from .base import MMIOBlock

_MODELS: dict[str, type[MMIOBlock]] = {}


def register(name: str) -> Callable[[type[MMIOBlock]], type[MMIOBlock]]:
    def deco(cls: type[MMIOBlock]) -> type[MMIOBlock]:
        _MODELS[name] = cls
        cls.model = name
        return cls
    return deco


def models() -> dict[str, type[MMIOBlock]]:
    return dict(_MODELS)


def _try_import(model: str) -> None:
    if "." not in model:
        return
    family, sub = model.split(".", 1)
    candidates = [f"chipem.periph.{family}.{sub}", f"chipem.periph.{family}"]
    if family.startswith("armv"):
        candidates.insert(0, f"chipem.arch.{family}")
    for modname in candidates:
        try:
            importlib.import_module(modname)
        except ImportError:
            continue
        if model in _MODELS:
            return


def lookup(model: str) -> type[MMIOBlock] | None:
    cls = _MODELS.get(model)
    if cls is None:
        _try_import(model)
        cls = _MODELS.get(model)
    return cls


def create(model: str, name: str, base: int, size: int, params: dict | None = None, chip: Any = None) -> MMIOBlock:
    cls = lookup(model)
    if cls is None:
        log = getattr(chip, "log", None)
        if log is not None:
            t = chip.clock.now_ns() if getattr(chip, "clock", None) is not None else 0.0
            log.event("warn", t, block=name, model=model,
                      msg=f"peripheral model '{model}' not available for {name}; using RAM-like stub")
        cls = _MODELS["stub"]
    return cls(name, base, size, params=dict(params or {}))


@register("stub")
class StubBlock(MMIOBlock):
    """Unmodelled peripheral: behaves like RAM, logs the first touch of every register word."""

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, regs, ram_like=True, log=log, params=params)


@register("ram")
class RamBlock(MMIOBlock):
    """Plain storage (e.g. VBAT register file); silent."""

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, regs, ram_like=True, log=log, params=params)
        self.log_unknown = False
