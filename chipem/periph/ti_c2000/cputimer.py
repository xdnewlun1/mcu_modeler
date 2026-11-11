"""C28x 32-bit CPU timers 0/1/2 (0x0C00 + 8n): TIM, PRD, TCR, TPR, TPRH.

The 16-bit prescale counter PSCH:PSC counts SYSCLKOUT cycles down; when it passes 0 it reloads from TDDRH:TDDR and
TIM decrements.  When TIM is 0 the next decrement reloads it from PRD, sets TCR.TIF and, with TCR.TIE, pulses the
timer interrupt (TINT0 → PIE 1.7, TINT1 → CPU INT13, TINT2 → CPU INT14; `params.irq` overrides).  The period is
therefore (PRD + 1) × (TDDR + 1) cycles.  TCR: TSS (bit 4) stops/starts, TRB (bit 5, reads 0) reloads TIM from PRD
and PSC from TDDR, TIF (bit 15) is cleared by writing 1, TIE (bit 14) enables the interrupt.  Timers run after reset
(TIM = PRD = 0xFFFFFFFF).  The state is kept as an anchor (cycle, TIM, PSC) plus one scheduler event at the next
reload, so polling TIM costs no events.  `tint_listeners` cb(index) are called with every interrupt pulse (DMA TINTn
triggers).
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ._util import C2000Block, build_regs

TSS, TRB, TIE, TIF = 1 << 4, 1 << 5, 1 << 14, 1 << 15
_DEFAULT_IRQ = {0: "TINT0", 1: "INT13", 2: "INT14"}
_DEFAULT_LINE = {0: "1.7", 1: "INT13", 2: "INT14"}


@register("ti_c2000.cputimer")
class CpuTimer(C2000Block):
    cycle_anchors = ("_c0",)

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.index = int(params.get("index", name[-1] if name[-1].isdigit() else 0))
        ov = {
            "TIM": {"on_read": self._tim_read, "on_peek": self._tim_read, "on_write": self._tim_write},
            "PRD": {"on_write": self._prd_write},
            "TCR": {"on_write": self._tcr_write, "w1c_mask": TIF, "ro_mask": 0x33CF},
            "TPR": {"on_read": self._tpr_read, "on_peek": self._tpr_read, "on_write": self._tpr_write},
            "TPRH": {"on_read": self._tpr_read, "on_peek": self._tpr_read, "on_write": self._tpr_write},
        }
        regs = build_regs((name, f"CPUTIMER{self.index}", "CPUTIMER0"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, regs, params=params, log=log)
        self.gate = f"CPUTIMER{self.index}ENCLK"
        self.line = params.get("irq")
        self._ev = None
        self._c0 = 0               # anchor cycle
        self._tim0 = 0xFFFFFFFF    # TIM at the anchor
        self._psc0 = 0             # prescale counter at the anchor
        self.expiries = 0
        self.tint_listeners: list = []   # cb(index) on every TINTn pulse (DMA triggers)

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        if self.line is None:
            from ._util import irq_line
            self.line = irq_line(chip, _DEFAULT_IRQ[self.index % 3], _DEFAULT_LINE[self.index % 3])

    # ------------------------------------------------------------------ counting
    def _now(self) -> int:
        c = self.chip
        return c.now_cycles() if c is not None else 0

    def tddr(self) -> int:
        return ((self.get("TPRH") & 0xFF) << 8) | (self.get("TPR") & 0xFF)

    def running(self) -> bool:
        return not self.get("TCR") & TSS

    def _state(self, cyc: int | None = None) -> tuple[int, int]:
        """(TIM, PSC) at cycle `cyc` (default now)."""
        if not self.running() or self.chip is None:
            return self._tim0, self._psc0
        e = (self._now() if cyc is None else cyc) - self._c0
        d = self.tddr() + 1
        p0 = self._psc0
        if e <= p0:
            return self._tim0, p0 - e
        k = 1 + (e - p0 - 1) // d                         # decrements so far
        psc = d - 1 - ((e - p0 - 1) % d)
        t0 = self._tim0
        if k <= t0:
            return t0 - k, psc
        prd = self.get("PRD")
        j = k - t0 - 1
        return prd - (j % (prd + 1)), psc

    def _anchor(self, tim: int, psc: int) -> None:
        self._c0 = self._now()
        self._tim0 = tim & 0xFFFFFFFF
        self._psc0 = psc & 0xFFFF
        self._schedule()

    def _schedule(self) -> None:
        if self.chip is None:
            return
        sched = self.chip.sched
        if self._ev is not None:
            sched.cancel(self._ev)
            self._ev = None
        if not self.running():
            return
        d = self.tddr() + 1
        at = self._c0 + self._psc0 + 1 + self._tim0 * d     # cycle of the decrement that reloads from PRD
        self._ev = sched.at_cycles(at, self._expire)

    def _expire(self) -> None:
        self._ev = None
        self.expiries += 1
        self._c0 = self.chip.clock.cycles if not getattr(self.chip.core, "running", False) else self._now()
        self._tim0 = self.get("PRD")
        self._psc0 = self.tddr()
        tcr = self.get("TCR") | TIF
        self.set("TCR", tcr)
        if tcr & TIE:
            self.pulse_irq(self.line)
            for cb in list(self.tint_listeners):
                cb(self.index)
        self._schedule()

    def period_cycles(self) -> int:
        return (self.get("PRD") + 1) * (self.tddr() + 1)

    # ------------------------------------------------------------------ lifecycle
    def reset(self) -> None:
        if self._ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None
        super().reset()
        self._c0 = self.chip.clock.cycles if self.chip is not None else 0
        self._tim0 = 0xFFFFFFFF
        self._psc0 = 0
        self._schedule()

    # ------------------------------------------------------------------ registers
    def _tim_read(self, blk, reg):
        return self._state()[0]

    def _tim_write(self, blk, reg, old, new):
        # a 16-bit write merges into the stored (possibly stale) other half; firmware normally writes TIM whole
        self.check_gate()
        _, psc = self._state()
        self._anchor(new, psc)
        return new

    def _prd_write(self, blk, reg, old, new):
        return None

    def _tpr_read(self, blk, reg):
        _, psc = self._state()
        v = self.get(reg.name) & 0xFF
        return v | (((psc >> 8) if reg.name == "TPRH" else psc) & 0xFF) << 8

    def _tpr_write(self, blk, reg, old, new):
        tim, psc = self._state()
        self.set(reg.name, new & 0xFF)
        if self.running():
            self._anchor(tim, min(psc, self.tddr()))
        return new & 0xFF

    def _tcr_write(self, blk, reg, old, new):
        tim, psc = self._state()
        was_running = not old & TSS
        if new & TRB:
            tim, psc = self.get("PRD"), self.tddr()
            new &= ~TRB
        self.set("TCR", new)
        if not (new & TSS) or was_running:
            if was_running and new & TSS:                    # stopping: freeze the current count
                if self._ev is not None:
                    self.chip.sched.cancel(self._ev)
                    self._ev = None
                self._tim0, self._psc0 = tim, psc
            else:
                self._anchor(tim, psc)
        else:
            self._tim0, self._psc0 = tim, psc
        return new
