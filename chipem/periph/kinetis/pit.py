"""Kinetis PIT (Periodic Interrupt Timer): 4 down-counting 32-bit channels clocked by the bus clock.

MCR (MDIS bit 1, reset 1 = module disabled; FRZ bit 0 stored only), per channel n at 0x100 + 0x10*n:
LDVAL, CVAL (live), TCTRL (TEN bit 0, TIE bit 1, CHN bit 2), TFLG (TIF bit 0, write 1 to clear).
A running channel loads LDVAL and counts down once per bus clock; reaching 0 → next clock sets TIF and reloads,
so the period is LDVAL+1 bus clocks.  One scheduler event per expiry (wall-clock domain, period computed from
the bus clock at (re)load time).  A new LDVAL written while running takes effect at the next reload.
Chained channels (CHN=1, n>0) count expirations of channel n-1 instead of clocks.
IRQ line of channel n (IRQ "PIT<n>") is level = TIF & TIE; clearing TIF/TIE deasserts it.
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock, Reg
from ..registry import register
from .clockutil import bus_hz, irq_number, now_ns, regs_from_table

MCR_FRZ, MCR_MDIS = 1, 2
TEN, TIE, CHN = 1, 2, 4
TIF = 1
NCH = 4


def _fallback():
    out = [("MCR", 0, 4, MCR_MDIS)]
    for n in range(NCH):
        b = 0x100 + 0x10 * n
        out += [(f"LDVAL{n}", b, 4, 0), (f"CVAL{n}", b + 4, 4, 0), (f"TCTRL{n}", b + 8, 4, 0), (f"TFLG{n}", b + 12, 4, 0)]
    return out


class _Chan:
    __slots__ = ("n", "running", "start_ns", "period_ns", "load", "ev", "chain_cnt", "irq", "line", "hz")

    def __init__(self, n: int):
        self.n = n
        self.running = False
        self.start_ns = 0.0
        self.period_ns = 0.0
        self.load = 0
        self.ev = None
        self.chain_cnt = 0
        self.irq: int | None = None
        self.line = False
        self.hz = 0.0


@register("kinetis.pit")
class PIT(MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, ram_like=True, log=log, params=params)
        ov = {"MCR": dict(on_write=self._mcr_write)}
        for n in range(NCH):
            ov[f"LDVAL{n}"] = dict(on_write=self._ldval_write(n))
            ov[f"CVAL{n}"] = dict(ro_mask=0xFFFFFFFF, on_read=self._cval_read(n))
            ov[f"TCTRL{n}"] = dict(on_write=self._tctrl_write(n))
            ov[f"TFLG{n}"] = dict(ro_mask=0xFFFFFFFE, w1c_mask=TIF, on_write=self._tflg_write(n))
        tbl = name if name.startswith("PIT") else "PIT"
        for r in regs_from_table(tbl, _fallback(), self.params.get("regs_file"), **ov):
            if r.name in ov or r.name == "MCR":
                self.add_reg(r)
        self.ch = [_Chan(n) for n in range(NCH)]

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        for c in self.ch:
            c.irq = irq_number(chip, f"PIT{c.n}")

    def reset(self) -> None:
        for c in getattr(self, "ch", ()):
            self._stop(c)
            c.chain_cnt = 0
            if c.line:
                self._set_line(c, False)
        super().reset()

    # ------------------------------------------------------------------ helpers
    def _enabled(self) -> bool:
        return not (self.get("MCR") & MCR_MDIS)

    def _chained(self, c: _Chan) -> bool:
        return c.n > 0 and bool(self.get(f"TCTRL{c.n}") & CHN)

    def _set_line(self, c: _Chan, level: bool) -> None:
        if level == c.line:
            return
        c.line = level
        if c.irq is not None and self.chip is not None:
            self.irq(c.irq, level)

    def _update_line(self, c: _Chan) -> None:
        lvl = bool(self.get(f"TFLG{c.n}") & TIF) and bool(self.get(f"TCTRL{c.n}") & TIE)
        self._set_line(c, lvl)

    def _stop(self, c: _Chan) -> None:
        if c.ev is not None and self.chip is not None:
            self.chip.sched.cancel(c.ev)
        c.ev = None
        c.running = False

    def _start(self, c: _Chan, t_ns: float | None = None) -> None:
        """(Re)load LDVAL and start counting at t_ns (default: now)."""
        self._stop(c)
        if not self._enabled() or not (self.get(f"TCTRL{c.n}") & TEN):
            return
        c.running = True
        c.load = self.get(f"LDVAL{c.n}")
        if self._chained(c):
            c.chain_cnt = c.load
            return
        c.hz = bus_hz(self.chip)
        c.start_ns = now_ns(self.chip) if t_ns is None else t_ns
        c.period_ns = (c.load + 1) * 1e9 / c.hz
        c.ev = self.chip.sched.at_ns(c.start_ns + c.period_ns, self._expire, c)

    def _expire(self, c: _Chan) -> None:
        c.ev = None
        t = c.start_ns + c.period_ns
        self._hit(c)
        if c.running and not self._chained(c):
            self._start(c, t)

    def _hit(self, c: _Chan) -> None:
        """Channel c reached zero: set TIF, notify a chained successor."""
        self.set(f"TFLG{c.n}", self.get(f"TFLG{c.n}") | TIF)
        self._update_line(c)
        if c.n + 1 < NCH:
            nxt = self.ch[c.n + 1]
            if nxt.running and self._chained(nxt):
                if nxt.chain_cnt == 0:
                    nxt.load = self.get(f"LDVAL{nxt.n}")
                    nxt.chain_cnt = nxt.load
                    self._hit(nxt)
                else:
                    nxt.chain_cnt -= 1

    def cval(self, n: int) -> int:
        c = self.ch[n]
        if not c.running:
            return self.get(f"CVAL{n}")
        if self._chained(c):
            return c.chain_cnt
        el = int((now_ns(self.chip) - c.start_ns) * c.hz / 1e9 + 1e-6)
        return max(c.load - el, 0) & 0xFFFFFFFF

    # ------------------------------------------------------------------ register hooks
    def _mcr_write(self, blk, reg, old, new):
        if (old ^ new) & MCR_MDIS:
            self.set("MCR", new)
            for c in self.ch:
                if new & MCR_MDIS:
                    if c.running:
                        self.set(f"CVAL{c.n}", self.cval(c.n))
                    self._stop(c)
                else:
                    self._start(c)
        return None

    def _ldval_write(self, n: int):
        def wr(blk, reg, old, new):
            c = self.ch[n]
            if not c.running:
                self.set(f"CVAL{n}", new)
            return None
        return wr

    def _cval_read(self, n: int):
        return lambda blk, reg: self.cval(n)

    def _tctrl_write(self, n: int):
        def wr(blk, reg, old, new):
            c = self.ch[n]
            if n == 0:
                new &= ~CHN
                self.set("TCTRL0", new)
            if (old ^ new) & (TEN | CHN):
                if new & TEN:
                    self._start(c)
                else:
                    if c.running:
                        self.set(f"CVAL{n}", self.cval(n))
                    self._stop(c)
            self._update_line(c)
            return new
        return wr

    def _tflg_write(self, n: int):
        def wr(blk, reg, old, new):
            self.set(f"TFLG{n}", new)
            self._update_line(self.ch[n])
            return None
        return wr
