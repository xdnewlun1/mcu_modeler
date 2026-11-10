"""Kinetis RTC (VBAT domain), driven by virtual time (32.768 kHz oscillator, independent of the core clock).

TSR seconds counter, TPR 15-bit prescaler (live: 32768 Hz since the last second), TAR alarm, TCR (stored),
CR (SWR bit 0 = software reset of all RTC registers except CR.SWR; OSCE bit 8 stored), SR (TIF bit 0 = time
invalid (reset 1), TOF bit 1, TAF bit 2, TCE bit 4), LR / WAR / RAR (lock bits: once cleared stay cleared until a
software/VBAT reset), IER (TIIE 0, TOIE 1, TAIE 2, TSIE 4; reset 0x7).
The time counter runs while SR.TCE=1 and TIF=TOF=0; TSR then increments once per second (scheduler event).
Writing TSR (only while TCE=0) clears TIF/TOF; writing TAR clears TAF.  As on silicon, TAF is set when TSR equals
TAR and increments (i.e. on the transition TAR -> TAR+1).  IRQ "RTC" = (TIF&TIIE)|(TOF&TOIE)|(TAF&TAIE) (level);
IRQ "RTC_Seconds" is pulsed every second while IER.TSIE=1.
Trigger outputs: `trigger_listeners` cb(rtc, kind) with kind "alarm" when TAF gets set and "seconds" at every
TSR increment (independent of IER.TSIE): PDB TRGSEL 12/13 and the ADC alternate trigger (SOPT7 ADCnTRGSEL 12/13).
Like the real VBAT domain, the RTC keeps its state across chip (system) resets: only the first reset (power-on)
and CR.SWR clear it.
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock
from ..registry import register
from .clockutil import irq_number, now_ns, regs_from_table

SR_TIF, SR_TOF, SR_TAF, SR_TCE = 1, 2, 4, 0x10
IER_TIIE, IER_TOIE, IER_TAIE, IER_TSIE = 1, 2, 4, 0x10
CR_SWR = 1
SEC_NS = 1e9


def _fallback():
    return [("TSR", 0, 4, 0), ("TPR", 4, 4, 0), ("TAR", 8, 4, 0), ("TCR", 0xC, 4, 0), ("CR", 0x10, 4, 0),
            ("SR", 0x14, 4, SR_TIF), ("LR", 0x18, 4, 0xFF), ("IER", 0x1C, 4, 7), ("WAR", 0x800, 4, 0xFF),
            ("RAR", 0x804, 4, 0xFF)]


@register("kinetis.rtc")
class RTC(MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, ram_like=True, log=log, params=params)
        once = dict(on_write=lambda b, r, o, n: o & n)
        ov = {"TSR": dict(on_write=self._tsr_write, on_read=lambda b, r: self.get("TSR")),
              "TPR": dict(on_write=self._tpr_write, on_read=lambda b, r: self.tpr()),
              "TAR": dict(on_write=self._tar_write),
              "CR": dict(on_write=self._cr_write),
              "SR": dict(ro_mask=SR_TIF | SR_TOF | SR_TAF, on_write=self._sr_write),
              "IER": dict(on_write=self._ier_write),
              "LR": once, "WAR": once, "RAR": once}
        for r in regs_from_table("RTC", _fallback(), self.params.get("regs_file"), **ov):
            self.add_reg(r)
        self._ev = None
        self._sec_start_ns = 0.0     # time at which TPR was 0 (start of the current second)
        self.trigger_listeners: list = []      # cb(rtc, "alarm" | "seconds")
        self._line = False
        self._irq: int | None = None
        self._irq_sec: int | None = None
        self._powered = False
        self.seconds_irqs = 0

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irq = irq_number(chip, "RTC")
        self._irq_sec = irq_number(chip, "RTC_Seconds")

    def reset(self) -> None:
        if self._powered:            # system reset: VBAT-domain state is retained
            return
        self._powered = True
        self._full_reset()

    def _full_reset(self) -> None:
        self._cancel()
        if self._line:
            self._set_line(False)
        super().reset()
        self._update_line()                  # TIF=1 with TIIE=1 at power-on: RTC IRQ asserted (RM)

    # ------------------------------------------------------------------ helpers
    def _cancel(self) -> None:
        if self._ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None

    def counting(self) -> bool:
        sr = self.get("SR")
        return bool(sr & SR_TCE) and not sr & (SR_TIF | SR_TOF)

    def tpr(self) -> int:
        if not self.counting() or self.chip is None:
            return self.get("TPR") & 0xFFFF
        return int((now_ns(self.chip) - self._sec_start_ns) * 32768 / SEC_NS) & 0x7FFF

    def _set_line(self, level: bool) -> None:
        if level != self._line:
            self._line = level
            if self._irq is not None and self.chip is not None:
                self.irq(self._irq, level)

    def _update_line(self) -> None:
        sr, ier = self.get("SR"), self.get("IER")
        self._set_line(bool((sr & SR_TIF and ier & IER_TIIE) or (sr & SR_TOF and ier & IER_TOIE)
                            or (sr & SR_TAF and ier & IER_TAIE)))

    def _start(self) -> None:
        """Start counting with the current TPR as the phase within the second."""
        self._cancel()
        if not self.counting():
            return
        tpr = self.get("TPR") & 0x7FFF
        self._sec_start_ns = now_ns(self.chip) - tpr * SEC_NS / 32768
        self._ev = self.chip.sched.at_ns(self._sec_start_ns + SEC_NS, self._second)

    def _stop(self) -> None:
        if self._ev is not None:
            self.set("TPR", self.tpr())
        self._cancel()

    def _second(self) -> None:
        self._ev = None
        tsr = self.get("TSR")
        sr = self.get("SR")
        alarm = tsr == self.get("TAR") and not sr & SR_TAF
        if tsr == self.get("TAR"):
            sr |= SR_TAF
        tsr = (tsr + 1) & 0xFFFFFFFF
        if tsr == 0:
            sr |= SR_TOF                     # overflow sets TOF only (TIF: POR / software reset)
        self.set("TSR", tsr)
        self.set("SR", sr)
        self.set("TPR", 0)
        self._sec_start_ns += SEC_NS
        self._update_line()
        if self.get("IER") & IER_TSIE and self._irq_sec is not None:
            self.seconds_irqs += 1
            self.pulse_irq(self._irq_sec)
        for cb in self.trigger_listeners:
            if alarm:
                cb(self, "alarm")
            cb(self, "seconds")
        if self.counting():
            self._ev = self.chip.sched.at_ns(self._sec_start_ns + SEC_NS, self._second)

    # ------------------------------------------------------------------ hooks
    def _tsr_write(self, blk, reg, old, new):
        if self.get("SR") & SR_TCE:
            return old                                   # write ignored while the counter is enabled
        self.set("SR", self.get("SR") & ~(SR_TIF | SR_TOF))
        self._update_line()
        return None

    def _tpr_write(self, blk, reg, old, new):
        if self.get("SR") & SR_TCE:
            return old
        return new & 0xFFFF

    def _tar_write(self, blk, reg, old, new):
        self.set("SR", self.get("SR") & ~SR_TAF)
        self._update_line()
        return None

    def _sr_write(self, blk, reg, old, new):
        was = bool(old & SR_TCE) and not old & (SR_TIF | SR_TOF)
        if was and not new & SR_TCE:
            self.set("SR", old)
            self._stop()
            self.set("SR", new)
        elif not was:
            self.set("SR", new)
            if self.counting():
                self._start()
        return None

    def _cr_write(self, blk, reg, old, new):
        if new & CR_SWR:
            self._full_reset()
            return CR_SWR
        return None

    def _ier_write(self, blk, reg, old, new):
        self.set("IER", new)
        self._update_line()
        return None
