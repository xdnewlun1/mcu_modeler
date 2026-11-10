"""STM32F4 SYSCFG (EXTICR port selection, MEMRM, PMC, CMPCR) and EXTI (external interrupt/event controller).

SYSCFG (RM0090 ch. 9): EXTICR1-4 select the GPIO port (0 = A ... 8 = I) of EXTI lines 0-15, four bits per line.
CMPCR.READY mirrors CMP_PD (compensation cell ready immediately). MEMRM/PMC are storage.

EXTI (RM0090 ch. 12), lines 0-22:
* Lines 0-15 watch the pin selected by SYSCFG_EXTICRx (PinBoard input listeners on every GPIO pin; X counts as 0).
  A rising (RTSR) / falling (FTSR) edge, or a 0 -> 1 SWIER write, sets PR when the line is unmasked (IMR); the
  event path (EMR) has no visible effect here. PR is write-1-to-clear; clearing PR also clears SWIER.
* Lines 16-22 (PVD, RTC alarm, OTG FS wakeup, Ethernet wakeup, OTG HS wakeup, tamper/timestamp, RTC wakeup) respond
  to SWIER and to `line_event(line, rising)` from internal sources (stm32.rtc: alarm line 17, wakeup line 22, through
  `_util.exti_input`); the other sources are not modelled. A rising RTC alarm (17), tamper/time-stamp (21) or
  wakeup (22) input is also a PWR wake-up event (PWR_CSR.WUF, Standby exit), whatever the EXTI configuration.
* Event mode (EMR): a selected edge (or a SWIER 0 -> 1 write) on a line with its EMR bit set is a wake-up event for
  WFE (stm32.pwr `exti_event`: wakes a core sleeping in WFE, else sets the WFE event register); with SCR.SEVONPEND
  an interrupt line that becomes pending is one too.
* NVIC lines (level, asserted while PR & IMR has a bit of the group): EXTI0..EXTI4 (lines 0-4), EXTI9_5 (5-9),
  EXTI15_10 (10-15), PVD (16), RTC_Alarm (17), OTG_FS_WKUP (18), ETH_WKUP (19), OTG_HS_WKUP (20),
  TAMP_STAMP (21), RTC_WKUP (22); numbers are looked up by name in the chip YAML.
Pin discovery: `pin_stats()` reports the edge mode and the number of PR sets per selected pin.
"""
from __future__ import annotations

from functools import partial
from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ._util import GatedBlock, build_regs, find_model, irq_number

PORTS = "ABCDEFGHIJK"
LINE_IRQS = (["EXTI0", "EXTI1", "EXTI2", "EXTI3", "EXTI4"] + ["EXTI9_5"] * 5 + ["EXTI15_10"] * 6
             + ["PVD", "RTC_Alarm", "OTG_FS_WKUP", "ETH_WKUP", "OTG_HS_WKUP", "TAMP_STAMP", "RTC_WKUP"])
NLINES = 23
LINE_MASK = (1 << NLINES) - 1


@register("stm32.syscfg")
class SYSCFG(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = "SYSCFG"
        ov: dict[str, dict] = {f"EXTICR{k}": {"on_write": self._exticr_write} for k in range(1, 5)}
        ov["CMPCR"] = {"ro_mask": 0xFFFFFFFE, "on_write": self._cmpcr_write}
        r = build_regs((name, "SYSCFG"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self._exti: Any = None

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._exti = find_model(chip, "stm32.exti", "EXTI")

    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()
        MMIOBlock.write(self, offset, size, value)

    def port_index(self, line: int) -> int:
        """Port index (0 = A) selected for EXTI line 0..15."""
        v = self.get(f"EXTICR{line // 4 + 1}")
        return (v >> (4 * (line % 4))) & 0xF

    def _exticr_write(self, blk, reg, old, new):
        if old != new and self._exti is not None:
            k = int(reg.name[-1]) - 1
            for j in range(4):
                if ((old ^ new) >> (4 * j)) & 0xF:
                    self._exti.selection_changed(4 * k + j)
        return None

    def _cmpcr_write(self, blk, reg, old, new):
        return (new & ~0x100) | (0x100 if new & 1 else 0)


@register("stm32.exti")
class EXTI(MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov = {"IMR": {"on_write": self._imr_write},
              "SWIER": {"on_write": self._swier_write},
              "PR": {"w1c_mask": LINE_MASK, "on_write": self._pr_write}}
        r = build_regs((name, "EXTI"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self._syscfg: Any = None
        self._pins: dict[tuple[int, int], Any] = {}         # (port index, line) -> Pin
        self._last: dict[tuple[int, int], int] = {}
        self._irqn: list[int | None] = [None] * NLINES
        self._lines: dict[int, bool] = {}                   # NVIC line -> asserted
        self.pr_count = [0] * NLINES
        self.x_mask = 0
        self._pwr: Any = None

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._syscfg = find_model(chip, "stm32.syscfg", "SYSCFG")
        self._pwr = find_model(chip, "stm32.pwr", "PWR")
        for ln in range(NLINES):
            self._irqn[ln] = irq_number(chip, LINE_IRQS[ln])
        board = getattr(chip, "pins", None)
        if board is not None:
            for pi, port in enumerate(PORTS):
                for ln in range(16):
                    pin = board.gpio(port, ln)
                    if pin is not None:
                        self._pins[(pi, ln)] = pin
                        board.input_listeners[pin.id].append(partial(self._on_level, pi, ln))

    def reset(self) -> None:
        super().reset()
        for n, on in list(self._lines.items()):
            if on and self.chip is not None:
                self.irq(n, False)
        self._lines.clear()
        for key, pin in self._pins.items():
            self._last[key] = 1 if pin.level() == "H" else 0

    # ------------------------------------------------------------ helpers
    def port_of(self, line: int) -> int:
        return self._syscfg.port_index(line) if self._syscfg is not None else 0

    def pin_of(self, line: int) -> Any:
        return self._pins.get((self.port_of(line), line)) if line < 16 else None

    def selection_changed(self, line: int) -> None:
        pin = self.pin_of(line)
        if pin is not None:
            self._last[(self.port_of(line), line)] = 1 if pin.level() == "H" else 0

    def line_event(self, line: int, rising: bool) -> bool:
        """Edge on an internal EXTI input (16 PVD, 17 RTC alarm, 21 tamper/timestamp, 22 RTC wakeup, ...): sets PR
        when the edge is selected (RTSR for rising, FTSR for falling) and IMR enables the line. Returns True if PR was
        set."""
        if rising and line in (17, 21, 22) and self._pwr is not None:
            self._pwr.wakeup_event(LINE_IRQS[line])
        sel = self.get("RTSR") if rising else self.get("FTSR")
        if sel >> line & 1 and self.get("EMR") >> line & 1:
            self._event()
        if not (sel >> line & 1 and self.get("IMR") >> line & 1):
            return False
        self._set_pending(line)
        return True

    def _event(self) -> None:
        if self._pwr is not None:
            self._pwr.exti_event()

    def _set_pending(self, line: int) -> None:
        if not self.get("IMR") >> line & 1:
            return
        self.pr_count[line] += 1
        pr = self.get("PR")
        self.set("PR", pr | (1 << line))
        self._update_irqs()
        if not pr >> line & 1 and self._pwr is not None and getattr(self.chip.arch, "sevonpend", False):
            self._pwr.exti_event()

    def _update_irqs(self) -> None:
        act = self.get("PR") & self.get("IMR")
        want: dict[int, bool] = {}
        for ln in range(NLINES):
            n = self._irqn[ln]
            if n is None:
                continue
            want[n] = want.get(n, False) or bool(act >> ln & 1)
        for n, lvl in want.items():
            if self._lines.get(n, False) != lvl:
                self._lines[n] = lvl
                if self.chip is not None:
                    self.irq(n, lvl)

    def _on_level(self, pi: int, line: int, pin: Any) -> None:
        lvl = pin.level()
        b = 1 if lvl == "H" else 0
        key = (pi, line)
        prev = self._last.get(key, 0)
        self._last[key] = b
        if self.port_of(line) != pi or prev == b:
            return
        if lvl == "X":
            self.x_mask |= 1 << line
        if (b and self.get("RTSR") >> line & 1) or (not b and self.get("FTSR") >> line & 1):
            if self.get("EMR") >> line & 1:
                self._event()
            self._set_pending(line)

    # ------------------------------------------------------------ register hooks
    def _imr_write(self, blk, reg, old, new):
        self._update_irqs()
        return None

    def _swier_write(self, blk, reg, old, new):
        rising = new & ~old & LINE_MASK
        for ln in range(NLINES):
            if rising >> ln & 1:
                if self.get("EMR") >> ln & 1:
                    self._event()
                self._set_pending(ln)
        return None

    def _pr_write(self, blk, reg, old, new):
        cleared = old & ~new
        if cleared:
            self.set("SWIER", self.get("SWIER") & ~cleared)
            rep = getattr(self.chip, "pin_report", None)
            if rep is not None:
                pc = rep.pc()
                for ln in range(16):
                    if cleared >> ln & 1 and self.pin_of(ln) is not None:
                        rep.touch_isr(self.pin_of(ln), pc)
        self._update_irqs()
        return None

    # ------------------------------------------------------------ pin discovery
    def pin_stats(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        imr, rt, ft = self.get("IMR"), self.get("RTSR"), self.get("FTSR")
        for ln in range(16):
            pin = self.pin_of(ln)
            if pin is None:
                continue
            r, f = rt >> ln & 1, ft >> ln & 1
            if not (imr >> ln & 1 and (r or f)) and not self.pr_count[ln]:
                continue
            mode = "either edge" if r and f else ("rising edge" if r else ("falling edge" if f else ""))
            d: dict[str, Any] = {"irqs": self.pr_count[ln]}
            if mode and imr >> ln & 1:
                d["irq_mode"] = mode
            if self.x_mask >> ln & 1:
                d["floating"] = True
            out[pin.id] = d
        return out
