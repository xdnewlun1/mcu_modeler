"""Small C2000 models: register storage with reset values (`ti_c2000.storage`, e.g. device emulation and blocks of
other parts that have no model yet), flash configuration (`ti_c2000.flash`), code security module (`ti_c2000.csm`,
always reported unlocked) and the L0-L3 dual mapping (`ti_c2000.alias`).  SPI-A is `ti_c2000.spi` (spi.py)."""
from __future__ import annotations

from typing import Any

from ..registry import register
from ._util import C2000Block, build_regs


@register("ti_c2000.storage")
class Storage(C2000Block):
    """Register file with the reset values from the register table; no behaviour.  `params.regs` names the table
    entry (default: the block name).  Accesses are not logged as unmodelled when the register is declared."""

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        try:
            regs = build_regs((params.get("regs") or name, name), size, {}, params.get("regs_file"))
        except KeyError:
            regs = []
        super().__init__(name, base, size, regs, params=params, log=log)
        self.gate = params.get("gate")


@register("ti_c2000.flash")
class FlashCtl(C2000Block):
    """Flash/OTP wait-state and power registers (0x0A80): plain storage with reset values, EALLOW protected.
    FSTATUS reads PWRS = FPWR (the flash is always ready); wait states do not affect emulated timing."""
    eallow_regs = "*"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        regs = build_regs((name, "FLASH"), size, {"FSTATUS": {"on_read": self._fstatus}}, params.get("regs_file"))
        super().__init__(name, base, size, regs, params=params, log=log)

    def _fstatus(self, blk, reg):
        return (self.get("FSTATUS") & ~3) | (self.get("FPWR") & 3)


@register("ti_c2000.csm")
class CSM(C2000Block):
    """Code security module (0x0AE0): KEY0-7 storage; CSMSCR.SECURE always reads 0 (unsecured: the emulator does
    not hold passwords, as with an erased password field)."""

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        regs = build_regs((name, "CSM"), size, {"CSMSCR": {"on_read": lambda b, r: b.get("CSMSCR") & ~1}},
                          params.get("regs_file"))
        super().__init__(name, base, size, regs, params=params, log=log)


@register("ti_c2000.alias")
class Alias(C2000Block):
    """A second view of memory mapped elsewhere (L0-L3 SARAM appear at 0x8000 and 0x3F8000): every access is
    forwarded to `params.target` + offset through the bus."""

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        super().__init__(name, base, 0, (), params=params, log=log)
        self.size = size                    # no backing store of its own
        t = params.get("target", 0)
        self.target = int(t, 0) if isinstance(t, str) else int(t)
        self.log_unknown = False

    def read(self, offset: int, size: int) -> int:
        return self.chip.bus.read(self.target + offset, size)

    def write(self, offset: int, size: int, value: int) -> None:
        self.chip.bus.write(self.target + offset, size, value)

    def reset(self) -> None:
        pass
