"""Kinetis LPTMR (low-power timer), time-counter and pulse-counter modes.

CSR: TEN bit 0, TMS bit 1 (1 = pulse counter), TFC bit 2 (1 = free-running),
TPP bit 3, TPS bits 4-5, TIE bit 6, TCF bit 7 (write 1 to clear).  PSR: PCS bits 0-1 (0 MCGIRCLK, modelled as
32.768 kHz; 1 LPO 1 kHz; 2 ERCLK32K 32.768 kHz; 3 OSCERCLK, modelled as 8 MHz), PBYP bit 2, PRESCALE bits 3-6
(divide by 2^(PRESCALE+1) unless bypassed).  CMR (16 bit), CNR (16 bit, live).
With TEN=1 the counter increments per prescaled tick; when CNR == CMR and increments, TCF is set (IRQ "LPTMR0"
if TIE) and, with TFC=0, CNR restarts from 0 (period CMR+1 ticks); with TFC=1 it free-runs and wraps at 2^16.
Writing TEN=0 resets the counter and clears TCF.  Ticks are scheduled in the wall-clock domain (these clocks do
not depend on the core clock).  Like on silicon, only the first (power-on) reset clears it; warm resets
(SYSRESETREQ, watchdog) leave it running.
Trigger output: each compare match (TCF set) calls `trigger_listeners` cb(lptmr): PDB TRGSEL 14 and the ADC
alternate trigger (SIM_SOPT7 ADCnTRGSEL 14). `flag_listeners` cb(lptmr) run when TCF/TIE may have changed (LLWU M0).
Pulse-counter mode (TMS=1, RM 40.4.2, 3.8.4.2): CNR counts active edges of input TPS (0 = CMP0 output, 1 = the pin
muxed to LPTMR0_ALT1, 2 = LPTMR0_ALT2, 3 reserved); TPP=0 rising, 1 falling edges. With PBYP=0 the glitch filter
passes a change only after the input stayed at its new level for 2^PRESCALE prescaler clocks (PCS clock; PRESCALE=0
is not supported by the filter and behaves as bypassed, one `warn`); the filter's sampling phase is not modelled.
Compare match, TFC and TCF work as in time-counter mode.
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock
from ..registry import register
from .clockutil import irq_number, now_ns, regs_from_table

TEN, TMS, TFC, TIE, TCF = 1, 2, 4, 0x40, 0x80
PBYP = 4
SOURCE_HZ = {0: 32768.0, 1: 1000.0, 2: 32768.0, 3: 8_000_000.0}


@register("kinetis.lptmr")
class LPTMR(MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, ram_like=True, log=log, params=params)
        ov = {"CSR": dict(ro_mask=0xFFFFFF00, w1c_mask=TCF, on_write=self._csr_write),
              "PSR": dict(ro_mask=0xFFFFFF80, on_write=self._psr_write),
              "CMR": dict(ro_mask=0xFFFF0000, on_write=self._cmr_write),
              "CNR": dict(ro_mask=0xFFFFFFFF, on_read=lambda b, r: self.cnr(),
                         on_peek=lambda b, r: self.cnr())}
        fb = [("CSR", 0, 4, 0), ("PSR", 4, 4, 0), ("CMR", 8, 4, 0), ("CNR", 12, 4, 0)]
        for r in regs_from_table("LPTMR0", fb, self.params.get("regs_file"), **ov):
            self.add_reg(r)
        self.source_hz = dict(params.get("source_hz", {}) if params else {}) or dict(SOURCE_HZ)
        self._ev = None
        self._start_ns = 0.0      # time at which CNR was 0
        self._tick_ns = 0.0
        self._line = False
        self._irq: int | None = None
        self._warned = False
        self._powered = False
        self.trigger_listeners: list = []          # cb(lptmr) at each compare match
        self.flag_listeners: list = []             # cb(lptmr) when TCF/TIE may have changed
        self._pulses = 0                           # pulse-counter CNR
        self._in_level: dict[int, int] = {}        # pulse input -> last raw level
        self._filt_ev = None
        self._filt_from = None
        self._filt_warned = False

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irq = irq_number(chip, self.name) or irq_number(chip, "LPTMR0")
        pins = getattr(chip, "pins", None)
        base = self.name if self.name.startswith("LPTMR") else "LPTMR0"
        if pins is not None:
            for k in (1, 2):
                sig = f"{base}_ALT{k}"
                for p in pins.signal_pins(sig):
                    pins.input_listeners[p.id].append(self._pin_cb(k, sig))
        for b in chip.peripherals.values():
            if getattr(b, "model", None) == "kinetis.cmp" and b.name == "CMP0":
                b.cout_listeners.append(lambda _c, lvl: self._input(0, 1 if lvl else 0))

    def reset(self) -> None:
        # LPTMR is reset only by POR/LVD (RM): it keeps counting (and its IRQ line) through warm resets
        if self._powered:
            return
        self._powered = True
        self._cancel()
        self._pulses = 0
        if self._line:
            self._set_line(False)
        super().reset()

    # ------------------------------------------------------------------ helpers
    def tick_hz(self) -> float:
        psr = self.get("PSR")
        f = float(self.source_hz.get(psr & 3, 32768.0))
        if not psr & PBYP:
            f /= 2 ** (((psr >> 3) & 0xF) + 1)
        return f

    def _cancel(self) -> None:
        if self._ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None

    def _running(self) -> bool:
        csr = self.get("CSR")
        return bool(csr & TEN) and not csr & TMS

    def _counting_pulses(self) -> bool:
        csr = self.get("CSR")
        return bool(csr & TEN) and bool(csr & TMS)

    # ------------------------------------------------------------------ pulse counter
    def _pin_cb(self, k: int, sig: str):
        def cb(pin):
            if pin.owner != sig and sig not in str(pin.owner).split("/"):
                return
            lvl = pin.level()
            if lvl in ("H", "L"):
                self._input(k, 1 if lvl == "H" else 0)
        return cb

    def _input(self, k: int, level: int) -> None:
        old = self._in_level.get(k)
        self._in_level[k] = level
        if old == level or not self._counting_pulses() or (self.get("CSR") >> 4) & 3 != k:
            return
        psr = self.get("PSR")
        if psr & PBYP or not (psr >> 3) & 0xF:
            if not psr & PBYP and not self._filt_warned:
                self._filt_warned = True
                self.emit("warn", block=self.name, msg=f"{self.name}: glitch filter with PRESCALE=0 is not supported "
                          "(RM 40.3.2): input taken unfiltered")
            self._filtered(k, level)
            return
        if self._filt_ev is not None:
            self.chip.sched.cancel(self._filt_ev)        # the input changed back before the filter passed it
            self._filt_ev = None
            if old is not None and level == self._filt_from:
                return
        self._filt_from = old
        clk = float(self.source_hz.get(psr & 3, 32768.0))
        self._filt_ev = self.chip.sched.in_ns((1 << ((psr >> 3) & 0xF)) * 1e9 / clk, self._filter_pass, k, level)

    def _filter_pass(self, k: int, level: int) -> None:
        self._filt_ev = None
        self._filtered(k, level)

    def _filtered(self, k: int, level: int) -> None:
        active_low = bool(self.get("CSR") & 0x08)          # TPP
        if level != (0 if active_low else 1):
            return
        cmr = self.get("CMR") & 0xFFFF
        hit = self._pulses == cmr
        self._pulses = (self._pulses + 1) & 0xFFFF
        if hit:
            self.set("CSR", self.get("CSR") | TCF)
            if not self.get("CSR") & TFC:
                self._pulses = 0
            self._update_line()
            for cb in self.trigger_listeners:
                cb(self)

    def _set_line(self, level: bool) -> None:
        if level != self._line:
            self._line = level
            if self._irq is not None and self.chip is not None:
                self.irq(self._irq, level)

    def _update_line(self) -> None:
        csr = self.get("CSR")
        self._set_line(bool(csr & TCF) and bool(csr & TIE))
        for cb in self.flag_listeners:
            cb(self)

    def _ticks(self) -> int:
        return int((now_ns(self.chip) - self._start_ns) / self._tick_ns + 1e-6)

    def cnr(self) -> int:
        if self._counting_pulses():
            return self._pulses
        if not self._running() or self._tick_ns <= 0:
            return 0
        return self._ticks() & 0xFFFF

    def _schedule(self, after: int | None = None) -> None:
        """Schedule the next compare match (CNR == CMR then increments)."""
        self._cancel()
        if not self._running():
            return
        cmr = self.get("CMR") & 0xFFFF
        el = self._ticks() if after is None else after
        # next tick index k > el (k = ticks since start) where (k-1) mod 2^16 == cmr  (TFC=1); k = cmr+1 (TFC=0)
        k = cmr + 1
        if k <= el:
            k += ((el - k) // 0x10000 + 1) * 0x10000
        self._ev = self.chip.sched.at_ns(self._start_ns + k * self._tick_ns, self._match, k)

    def _match(self, k: int) -> None:
        self._ev = None
        self.set("CSR", self.get("CSR") | TCF)
        self._update_line()
        for cb in self.trigger_listeners:
            cb(self)
        if not self.get("CSR") & TFC:
            self._start_ns += k * self._tick_ns       # counter restarts from 0
            self._schedule(0)
        else:
            self._schedule(k)

    def _restart(self) -> None:
        self._tick_ns = 1e9 / self.tick_hz()
        self._start_ns = now_ns(self.chip)
        self._schedule()

    # ------------------------------------------------------------------ hooks
    def _csr_write(self, blk, reg, old, new):
        if (old ^ new) & TEN or ((old ^ new) & TMS and new & TEN):
            self._pulses = 0
            if self._filt_ev is not None:
                self.chip.sched.cancel(self._filt_ev)
                self._filt_ev = None
        if (old ^ new) & TEN:
            if new & TEN:
                self.set("CSR", new)
                self._restart()
            else:
                self._cancel()
                new &= ~TCF
                self.set("CSR", new)
        elif (old ^ new) & (TFC | TMS) and new & TEN:
            self.set("CSR", new)
            self._schedule()
        self.set("CSR", new)
        self._update_line()
        return new

    def _psr_write(self, blk, reg, old, new):
        if old != new and self._running():
            self.set("PSR", new)
            self._restart()
        return None

    def _cmr_write(self, blk, reg, old, new):
        if self._running():
            self._schedule()
        return None
