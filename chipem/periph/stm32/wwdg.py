"""STM32F4 window watchdog (WWDG, RM0090 ch. 22).

Registers from the SVD: CR (+0: T[6:0], WDGA bit 7; reset 0x7F), CFR (+4: W[6:0], WDGTB[1:0] bits 7-8, EWI bit 9;
reset 0x7F), SR (+8: EWIF bit 0, rc_w0).
* Counter: the 7-bit down-counter T[6:0] decrements once every 4096 x 2^WDGTB PCLK1 cycles (22.4: t_WWDG =
  t_PCLK1 x 4096 x 2^WDGTB), counted in core cycles (PPRE1 x 4096 x 2^WDGTB, `sched.at_cycles`). Reading CR returns
  the live counter. The prescaler phase starts at the last CR write (silicon's prescaler runs freely: up to one tick
  of difference). It counts while the APB1 clock (RCC_APB1ENR.WWDGEN) is on, also before WDGA is set (free-running,
  wrapping 0x00 -> 0x7F while the watchdog is off).
* WDGA (set by software, cleared only by a reset) activates the watchdog. A system reset ("wwdg": RCC_CSR.WWDGRSTF
  and PINRSTF are set on the next reset) is generated when the counter rolls over from 0x40 to 0x3F (T6 becomes 0),
  when CR is written with T6 = 0 while active, and when CR is written while the counter is still above the window
  value W (refresh too early, 22.3, Figure 215).
* Early wakeup (22.3): EWIF is set when the counter reaches 0x40; with CFR.EWI (set by software, cleared only by a
  reset) the level IRQ "WWDG" follows EWIF. EWIF is cleared by writing 0.
WWDG is reset by a system reset and by RCC_APB1RSTR.WWDGRST. Debug freeze (DBGMCU_APB1_FZ) is not modelled.
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock, pure_read
from ..registry import register
from ._util import GatedBlock, build_regs, irq_number, rcc

WDGA, EWI, EWIF = 0x80, 0x200, 1
WWDGRSTF, PINRSTF = 1 << 30, 1 << 26


@register("stm32.wwdg")
class WWDG(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = "WWDG"
        ov = {"CR": {"on_write": self._cr_write, "on_read": self._cr_read},
              "CFR": {"on_write": self._cfr_write},
              "SR": {"ro_mask": 0xFFFFFFFE, "on_write": self._sr_write}}
        r = build_regs((name, "WWDG"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self._t0 = 0                 # core cycle of the counter base
        self._c0 = 0x7F              # counter value at _t0
        self._ev = None
        self._irqn: int | None = None
        self._line = False
        self._tick = 4096
        self._running = False
        self.resets = 0

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, "WWDG")
        if self._rcc is not None:
            self._rcc.listeners.append(self._rebase)
            self._rcc.gate_listeners.append(lambda enr, old, new: self._rebase() if enr == "APB1ENR" else None)

    def reset(self) -> None:
        if getattr(self, "_ev", None) is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None
        if getattr(self, "_line", False) and self.chip is not None:
            self.irq(self._irqn, False)
        self._line = False
        super().reset()
        self._c0 = 0x7F
        if self.chip is not None:
            self._t0 = self.chip.now_cycles()
            self._rebase()

    # ------------------------------------------------------------------ counter
    def tick_cycles(self) -> int:
        """Core cycles per counter decrement: PPRE1 x 4096 x 2^WDGTB."""
        r = self._rcc
        ppre = r.ppre(1) if r is not None else 1
        return ppre * 4096 << ((self.get("CFR") >> 7) & 3)

    def _gate_on(self) -> bool:
        loc = self._gate_loc
        return loc is None or bool(self._rcc.mem[loc[0]] & loc[1])

    lp_frozen: Any = False              # PWR freezer: PCLK1 off in Stop (RM0090 5.3.4) / gated Sleep

    def time_shift(self, dns: float, dcycles: int) -> None:
        """The counter held while PCLK1 was off: move its anchor by the time slept."""
        self._t0 += dcycles

    def counter(self) -> int:
        if not self._running or self.chip is None:
            return self._c0
        now = self.lp_c0 if self.lp_frozen else self.chip.now_cycles()
        n = (now - self._t0) // self._tick
        c = self._c0 - n
        if self.get("CR") & WDGA:
            return max(c, 0x3F)
        return c & 0x7F

    def _rebase(self, c: int | None = None) -> None:
        """Re-anchor the counter at the current cycle (after a write, prescaler or clock-gate change)."""
        if self.chip is None:
            return
        self._c0 = self.counter() if c is None else c & 0x7F
        self._t0 = self.chip.now_cycles()
        self._tick = self.tick_cycles()
        self._running = self._gate_on()
        self._schedule()

    def _schedule(self) -> None:
        if self._ev is not None:
            self.chip.sched.cancel(self._ev)
            self._ev = None
        if not self._running:
            return
        c = self._c0
        active = bool(self.get("CR") & WDGA)
        # next interesting value: 0x40 (EWIF) or 0x3F (reset when active); free-running wraps through 0x7F
        if c > 0x40:
            steps = c - 0x40
        elif c == 0x40:
            steps = 1 if active else 0x41               # to 0x3F (reset) / around to 0x40 again
        else:
            steps = c + 1 + (0x7F - 0x40) if not active else 0
        if steps <= 0:
            return
        self._ev = self.chip.sched.at_cycles(self._t0 + steps * self._tick, self._reached, (c - steps) & 0x7F)

    def _reached(self, value: int) -> None:
        self._ev = None
        self._c0 = value
        self._t0 = self.chip.now_cycles()
        if value == 0x40:
            self.set("SR", self.get("SR") | EWIF)
            self._update()
        elif value == 0x3F and self.get("CR") & WDGA:
            self._wwdg_reset("counter reached 0x3F (no refresh)")
            return
        self._schedule()

    def _wwdg_reset(self, why: str) -> None:
        self.resets += 1
        self.emit("warn", block=self.name, msg=f"WWDG reset: {why}")
        r = rcc(self.chip)
        if r is not None:
            r.pending_reset_flags |= WWDGRSTF | PINRSTF
        if self.chip.core.running:
            self.chip.arch.request_reset("wwdg")
        else:
            self.chip.arch.reset_requested = "wwdg"

    def _update(self) -> None:
        on = bool(self.get("SR") & EWIF and self.get("CFR") & EWI)
        if on != self._line and self._irqn is not None:
            self._line = on
            self.irq(self._irqn, on)

    # ------------------------------------------------------------------ register hooks
    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()
        MMIOBlock.write(self, offset, size, value)

    @pure_read
    def _cr_read(self, blk, reg):
        return (self.get("CR") & WDGA) | self.counter()

    def _cr_write(self, blk, reg, old, new):
        active = bool(old & WDGA)
        cur = self.counter()
        new_t = new & 0x7F
        wdga = (old | new) & WDGA                       # WDGA cannot be cleared by software
        self._store(reg, wdga | new_t)
        if active or new & WDGA:
            if active and cur > (self.get("CFR") & 0x7F):
                self._wwdg_reset(f"refresh outside the window (counter 0x{cur:02X} > W 0x{self.get('CFR') & 0x7F:02X})")
                return None
            if not new_t & 0x40:
                self._wwdg_reset(f"CR written with T6 = 0 (T = 0x{new_t:02X})")
                return None
            if not active:
                self.emit("info", block=self.name, msg=f"WWDG enabled: T = 0x{new_t:02X}, W = 0x"
                          f"{self.get('CFR') & 0x7F:02X}, tick {self.tick_cycles()} cycles")
        self._rebase(new_t)
        return None

    def _cfr_write(self, blk, reg, old, new):
        new = (new & ~EWI) | ((old | new) & EWI)        # EWI cannot be cleared by software
        self._store(reg, old)
        c = self.counter()
        self._store(reg, new)
        self._rebase(c)
        self._update()
        return new

    def _sr_write(self, blk, reg, old, new):
        v = old & new & EWIF                           # rc_w0
        self._store(reg, v)
        self._update()
        return v
