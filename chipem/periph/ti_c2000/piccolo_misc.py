"""Piccolo blocks kept as documented storage: the control law accelerator registers (`ti_c2000.cla`).

CLA (TMS320F28069/F28065 only, SPRUH18I chapter 10; registers at 0x1400, message RAMs 0x1480-0x157F): the CLA is a
second processor with its own instruction set.  CHIP_EM does not execute it: the register block keeps the documented
reset values, and the first time firmware enables a task (MIER), forces one (MIFRC) or starts the CLA clock
(PCLKCR3.CLA1ENCLK, seen through the register writes here) a `warn` says that the tasks never run, so MIRUN/MIFR stay 0
and no CLA1_INTn interrupt is raised.  See docs/LIMITATIONS.md.
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ._util import C2000Block, build_regs


@register("ti_c2000.cla")
class Cla(C2000Block):
    eallow_regs = "*"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov: dict[str, dict] = {}
        for n in ("MIER", "MIFRC", "MCTL", "MVECT1", "MMEMCFG"):
            ov[n] = {"on_write": self._write}
        try:
            regs = build_regs((params.get("regs") or name, "CLA1"), size, ov, params.get("regs_file"))
        except KeyError:
            regs = []
        super().__init__(name, base, size, regs, params=params, log=log)
        self.gate = params.get("gate", "CLA1ENCLK")
        self._warned = False

    def _write(self, blk, reg, old, new):
        if reg.name in ("MIER", "MIFRC") and new and not self._warned:
            self._warned = True
            self.emit("warn", block=self.name, msg=f"{self.name}.{reg.name} = 0x{new:04x}: the CLA (control law "
                      "accelerator) is not emulated; its tasks never run (registers are storage only)")
        if reg.name == "MIFRC":
            return 0                                   # force bits read 0
        return None


@register("ti_c2000.partid")
class PartId(C2000Block):
    """Read-only window of TI-reserved OTP holding the part identification: PARTID (2802x 0x3D7FFF, SPRS523 Table 9-11;
    2806x 0x3D7E80, SPRS698 Table 8-10).  The bus maps memory in 0x40-word pages, so this block covers the page around
    PARTID: `params.value` is the PARTID word at `params.partid_offset`, `params.words` {offset: value} other
    programmed words of the page (e.g. the 2806x temperature-sensor getter routines at 0x3D7E82, assembled from
    chips/tms320f2806x_bootrom.asm); everything else reads erased (0xFFFF).  Writes are ignored (OTP)."""

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, [], params=params, log=log)
        p = self.params
        self.words = {int(k): int(v) & 0xFFFF for k, v in (p.get("words") or {}).items()}
        self.words[int(p.get("partid_offset", 0))] = int(p.get("value", 0xFFFF)) & 0xFFFF
        self._warned = False

    def read(self, offset: int, size: int) -> int:
        lo = self.words.get(offset, 0xFFFF)
        if size == 1:
            return lo
        return lo | (self.words.get(offset + 1, 0xFFFF) << 16)

    def write(self, offset: int, size: int, value: int) -> None:
        if not self._warned:
            self._warned = True
            self.emit("warn", block=self.name, msg=f"{self.name}: write to TI-reserved OTP ignored")
