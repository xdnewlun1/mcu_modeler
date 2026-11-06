"""Kinetis SMC, PMC, RCM, LLWU and the VBAT register file: register tables from the SVD with reset values.

* SMC: PMSTAT reads RUN (0x01); PMPROT/PMCTRL/VLLSCTRL are stored (STOPM changes are logged once).
* PMC: REGSC.ACKISO (bit 3) is write-1-to-clear; LVDSC1.LVDACK / LVDSC2.LVWACK (bit 6) read 0.
* RCM: SRS0/SRS1 report the cause of the last reset. The model watches the chip event log for `reset`
  events: reason "watchdog" -> SRS0=0x20 (WDOG), "lockup" -> SRS1=0x02, SYSRESETREQ/"sw" -> SRS1=0x04,
  anything else -> power-on SRS0=0x82 (POR | LVD).
* LLWU: PE1-4, ME, F1/F2 (w1c), F3 (read-only), FILT1/2 (FILTF w1c), RST; wake-up flags read 0.
* VBAT: battery-backed register file = plain RAM that survives chip.reset().
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ._util import build_regs


@register("kinetis.smc")
class SMC(MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov = {"PMSTAT": {"reset": 0x01, "ro_mask": 0xFF}, "PMCTRL": {"on_write": self._pmctrl}}
        super().__init__(name, base, size, build_regs((name, "SMC"), size, ov, params.get("regs_file")),
                         log=log, params=params)

    def _pmctrl(self, blk, reg, old, new):
        if (old ^ new) & 0x67:
            runm, stopm = (new >> 5) & 3, new & 7
            self.emit("info", block=self.name, msg=f"SMC_PMCTRL=0x{new:02x} (RUNM={runm}, STOPM={stopm}); "
                      "power modes are not modelled (PMSTAT stays RUN, WFI = sleep)")
        return None


@register("kinetis.pmc")
class PMC(MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ack = {"on_write": lambda b, r, o, n: n & ~0x40}
        ov = {"REGSC": {"w1c_mask": 0x08}, "LVDSC1": dict(ack), "LVDSC2": dict(ack)}
        super().__init__(name, base, size, build_regs((name, "PMC"), size, ov, params.get("regs_file")),
                         log=log, params=params)


@register("kinetis.rcm")
class RCM(MMIOBlock):
    POR = (0x82, 0x00)

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        super().__init__(name, base, size, build_regs((name, "RCM"), size, None, params.get("regs_file")),
                         log=log, params=params)
        self._cause = self.POR
        self.last_reason = "por"

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        log = getattr(chip, "log", None)
        if log is not None and hasattr(log, "subscribe"):
            log.subscribe(self._on_reset_event, kinds=("reset",))

    def _on_reset_event(self, ev: Any) -> None:
        reason = str(ev.get("reason", "") if hasattr(ev, "get") else "").lower()
        self.last_reason = reason
        if "watchdog" in reason or "wdog" in reason:
            self._cause = (0x20, 0x00)
        elif "lockup" in reason:
            self._cause = (0x00, 0x02)
        elif "sysresetreq" in reason or "vectreset" in reason or reason in ("sw", "software", "aircr"):
            self._cause = (0x00, 0x04)
        elif "pin" in reason:
            self._cause = (0x40, 0x00)
        else:
            self._cause = self.POR

    def reset(self) -> None:
        super().reset()
        s0, s1 = self._cause
        self.set("SRS0", s0)
        self.set("SRS1", s1)
        self._cause = self.POR


@register("kinetis.llwu")
class LLWU(MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov = {"F1": {"w1c_mask": 0xFF}, "F2": {"w1c_mask": 0xFF}, "F3": {"ro_mask": 0xFF},
              "FILT1": {"w1c_mask": 0x80}, "FILT2": {"w1c_mask": 0x80}}
        super().__init__(name, base, size, build_regs((name, "LLWU"), size, ov, params.get("regs_file")),
                         log=log, params=params)


@register("kinetis.vbat")
class VBAT(MMIOBlock):
    """32-byte VBAT register file (the whole block is plain RAM). Battery-backed: chip.reset() keeps it."""

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, (), ram_like=True, log=log, params=params)
        self.log_unknown = False

    def reset(self) -> None:
        self._seen.clear()          # contents survive every reset (cleared only by power-on = new Chip)

    def power_on_reset(self) -> None:
        self.mem[:] = bytes(self.size)
