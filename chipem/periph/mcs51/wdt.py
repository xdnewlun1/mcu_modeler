"""P89CV51 watchdog timer: WDTRST A6H (data sheet sec. 6.8 pp.43-44).

The WDT is a 14-bit counter, disabled after reset.  Writing 1EH then E1H (in sequence) to WDTRST enables it; once
running the same sequence restarts the count, and only a reset (RST pin, WDT overflow) stops it.  It counts every
machine cycle (12 oscillator periods, or 6 in 6-clock mode with CKCON.WDX2 = 0) while the oscillator runs (it
freezes in power-down and runs in idle).  When the count reaches 3FFFH (16383) the device resets
(`arch.request_reset("wdt")`) and the RST pin outputs a high pulse of 196 (12-clock) / 98 (6-clock) oscillator
periods (logged in the reset event, the pin itself is not toggled).  The counter cannot be read; WDTRST reads 0.
IAP calls with bit 7 of R1 set feed the WDT (mcs51.flash calls `feed()`).
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ._util import SfrBlock, periph_div

OVERFLOW = 0x3FFF


@register("mcs51.wdt")
class Wdt(SfrBlock):
    model = "mcs51.wdt"
    table = ("WDT",)

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, regs, params=params, log=log,
                         overrides={"WDTRST": {"on_write": self._wr, "on_read": lambda b, r: 0,
                                                 "on_peek": lambda b, r: 0}})
        self.enabled = False
        self._seq = False
        self._ev = None
        self._t0 = 0
        self._frozen_at: int | None = None
        self.resets = 0

    def reset(self) -> None:
        self.cancel(self._ev)
        self._ev = None
        super().reset()
        self.enabled = False
        self._seq = False
        self._frozen_at = None

    def _wr(self, blk, reg, old, new):
        if new == 0x1E:
            self._seq = True
        elif new == 0xE1 and self._seq:
            self._seq = False
            if not self.enabled:
                self.enabled = True
                self.emit("info", block=self.name, msg="watchdog enabled")
            self.feed()
        else:
            self._seq = False
        return 0

    def feed(self) -> None:
        if not self.enabled:
            return
        self._t0 = self.now()
        self._arm()

    def _arm(self) -> None:
        self.cancel(self._ev)
        self._ev = self.at(self._t0 + OVERFLOW * periph_div(self.chip, "WDX2"), self._overflow)

    def count(self) -> int:
        if not self.enabled:
            return 0
        now = self._frozen_at if self._frozen_at is not None else self.now()
        return min((now - self._t0) // periph_div(self.chip, "WDX2"), OVERFLOW)

    def _overflow(self) -> None:
        self._ev = None
        self.resets += 1
        pulse = 196 if periph_div(self.chip, "WDX2") == 12 else 98
        self.emit("info", block=self.name, msg=f"watchdog overflow (3FFFH): device reset, RST pin pulse of {pulse} "
                  "oscillator periods")
        self.chip.arch.request_reset("wdt")

    def clock_rebase(self) -> None:
        if self.enabled:
            self._t0 = self.now() - self.count() * periph_div(self.chip, "WDX2")

    def clock_resume(self) -> None:
        if self.enabled:
            self._arm()

    def clock_stop(self, stopped: bool, mode: str) -> None:
        if mode != "pd" or not self.enabled:
            return
        if stopped:
            self._frozen_at = self.now()
            self.cancel(self._ev)
            self._ev = None
        else:
            if self._frozen_at is not None:
                self._t0 += self.now() - self._frozen_at
            self._frozen_at = None
            self._arm()
