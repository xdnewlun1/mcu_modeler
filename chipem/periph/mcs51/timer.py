"""80C51 Timer/counters 0 and 1 with TCON/TMOD and external interrupts INT0/INT1 (P89CV51 data sheet sec. 6.4
pp.26-30, Tables 13-17; sec. 6.11 Table 41).

Counting (exact, event-light): while a timer runs in timer mode (C/T = 0) its count advances once per peripheral
clock (`periph_div(chip, "T0X2")` oscillator periods: 12, or 6 in 6-clock mode).  The registers are brought up to date
lazily (`_fold`: on every TLx/THx/TCON access, on any configuration change and in the overflow event), keeping the
sub-count remainder, so reads are exact to the oscillator period.  An overflow sets TFx from a scheduler event at
the exact overflow cycle, armed only while TFx is clear (or another model listens to the overflows: PCA count
source); with TFx set nothing is scheduled, so Timer 1 used only as the UART baud-rate generator costs one event
(mcs51.uart derives its bit time from `overflow_period(1)`), and a `JNB TF0,$` wait is skipped to that event.

Modes (TMOD M1:M0): 0 = 13-bit (TH 8 bits + TL low 5 bits; TL bits 7..5 keep their value), 1 = 16-bit, 2 = 8-bit
auto-reload (TL counts, TH reloads TL on overflow), 3: Timer 0 splits into TL0 (8 bits, TR0/GATE/C-T/TF0) and TH0
(8-bit timer under TR1, overflow sets TF1); Timer 1 in mode 3 holds its count.  While Timer 0 is in mode 3 Timer 1
keeps running (not gated by TR1, which belongs to TH0) and its overflows set no flag (data sheet sec. 6.4.4).
GATE = 1: the timer runs only while TRx = 1 and the INTx pin is high.  C/T = 1: the count advances on each falling
edge of the T0 (P3.4) / T1 (P3.5) pin (the sampling delay of two machine cycles is not modelled).
External interrupts: ITx = 1 -> a falling edge on INTx (P3.2 / P3.3) sets IEx, cleared by hardware when the CPU
vectors to the interrupt (and by software); ITx = 0 -> IEx follows the inverted pin (level-sensitive, not cleared by
vectoring).  Interrupt lines (arch): "TF0", "TF1", "IE0", "IE1" = the flags.  TF0/TF1 are cleared on vectoring.
Power-down: the timers freeze (`clock_stop`); the external-interrupt logic still sees the pins (level wake-up).
"""
from __future__ import annotations

from typing import Any, Callable

from ..registry import register
from ._util import SfrBlock, listen_pin, periph_div, pin_level

TF1, TR1, TF0, TR0, IE1, IT1, IE0, IT0 = 0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01


class _Unit:
    """One counting unit: Timer n in mode 0/1/2, or TL0 / TH0 halves in mode 3."""
    __slots__ = ("kind", "lo", "hi", "flag", "timer", "t0", "ev")

    def __init__(self, kind: str, lo: str | None, hi: str | None, flag: int, timer: int):
        self.kind, self.lo, self.hi, self.flag, self.timer = kind, lo, hi, flag, timer
        self.t0 = 0          # oscillator cycle the registers are current at (when running in timer mode)
        self.ev = None


@register("mcs51.timer01")
class Timer01(SfrBlock):
    model = "mcs51.timer01"
    table = ("TIMER01",)

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        ov = {"TCON": {"on_read": self._tcon_r, "on_write": self._tcon_w, "on_peek": self._fold_peek},
              "TMOD": {"on_write": self._tmod_w}}
        for r in ("TL0", "TH0", "TL1", "TH1"):
            ov[r] = {"on_read": self._cnt_r, "on_write": self._cnt_w, "on_peek": self._fold_peek}
        super().__init__(name, base, size, regs, params=params, log=log, overrides=ov)
        self.units: list[_Unit] = []
        self.ovf_listeners: dict[int, list[Callable[[], None]]] = {0: [], 1: []}
        self.overflows = [0, 0]                   # overflow counts (statistics / tests)
        self._run: dict[int, bool] = {}
        self._stopped = False                     # power-down: clocks off
        self._idle_pins = {"INT0": "H", "INT1": "H", "T0": "H", "T1": "H"}
        self.pins = {"INT0": "P3.2", "INT1": "P3.3", "T0": "P3.4", "T1": "P3.5"}
        self.pins.update({k: v for k, v in (self.params.get("pins") or {}).items()})

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        for sig, pin in self.pins.items():
            listen_pin(chip, pin, lambda lvl, _s=sig: self._pin_changed(_s, lvl))
        arch = chip.arch
        arch.on_vector["TF0"] = lambda: self._clear_flag(TF0)
        arch.on_vector["TF1"] = lambda: self._clear_flag(TF1)
        arch.on_vector["IE0"] = lambda: self._ack_ext(0)
        arch.on_vector["IE1"] = lambda: self._ack_ext(1)
        getattr(arch, "ie_listeners", []).append(self._resched_all)

    def reset(self) -> None:
        for u in self.units:
            self.cancel(u.ev)
        super().reset()
        self.units = []
        self._stopped = False
        self.overflows = [0, 0]
        if self.chip is not None:
            for s, p in self.pins.items():
                self._idle_pins[s] = pin_level(self.chip, p)
            self._rebuild()
            self._ext_sync(0)
            self._ext_sync(1)
            self._lines()

    # ------------------------------------------------------------------ configuration
    def _mode(self, n: int) -> int:
        return (self.get("TMOD") >> (4 * n)) & 3

    def _ct(self, n: int) -> bool:
        return bool(self.get("TMOD") >> (4 * n) & 4)

    def _gate(self, n: int) -> bool:
        return bool(self.get("TMOD") >> (4 * n) & 8)

    def _rebuild(self) -> None:
        for u in self.units:
            self.cancel(u.ev)
        units = []
        m0, m1 = self._mode(0), self._mode(1)
        if m0 == 3:
            units.append(_Unit("8", "TL0", None, TF0, 0))
            units.append(_Unit("8h", "TH0", None, TF1, 2))      # timer 2 = "TH0 under TR1"
        else:
            units.append(_Unit(str(m0), "TL0", "TH0", TF0, 0))
        if m1 != 3:
            units.append(_Unit(str(m1), "TL1", "TH1", 0 if m0 == 3 else TF1, 1))
        now = self.now()
        for u in units:
            u.t0 = now
        self.units = units
        self._run = {id(u): self._running(u) for u in units}
        self._resched_all()

    def _running(self, u: _Unit) -> bool:
        if self._stopped:
            return False
        tcon = self.get("TCON")
        if u.timer == 2:
            return bool(tcon & TR1)
        if u.timer == 1 and self._mode(0) == 3:
            return True                              # TR1 belongs to TH0 now
        tr = tcon & (TR0 if u.timer == 0 else TR1)
        if not tr:
            return False
        if self._gate(u.timer):
            return self._idle_pins["INT0" if u.timer == 0 else "INT1"] != "L"
        return True

    def _timer_mode(self, u: _Unit) -> bool:
        return u.timer == 2 or not self._ct(u.timer)

    def _div(self, u: _Unit) -> int:
        return periph_div(self.chip, "T1X2" if u.timer == 1 else "T0X2")

    # ------------------------------------------------------------------ counting
    def _value(self, u: _Unit) -> int:
        lo = self.get(u.lo)
        if u.kind == "1":
            return (self.get(u.hi) << 8) | lo
        if u.kind == "0":
            return (self.get(u.hi) << 5) | (lo & 0x1F)
        return lo                                    # "2", "8", "8h"

    def _store(self, u: _Unit, v: int) -> None:
        if u.kind == "1":
            self.set(u.lo, v & 0xFF)
            self.set(u.hi, (v >> 8) & 0xFF)
        elif u.kind == "0":
            self.set(u.lo, (self.get(u.lo) & 0xE0) | (v & 0x1F))
            self.set(u.hi, (v >> 5) & 0xFF)
        else:
            self.set(u.lo, v & 0xFF)

    def _to_ovf(self, u: _Unit) -> int:
        """Counts until the next overflow."""
        v = self._value(u)
        if u.kind == "1":
            return 0x10000 - v
        if u.kind == "0":
            return 0x2000 - v
        return 0x100 - v

    def _advance(self, u: _Unit, k: int) -> int:
        """Add k counts to unit u; returns the number of overflows."""
        if k <= 0:
            return 0
        v = self._value(u)
        if u.kind == "2":
            first = 0x100 - v
            if k < first:
                self._store(u, v + k)
                return 0
            th = self.get(u.hi)
            per = 0x100 - th
            k -= first
            self._store(u, th + k % per)
            return 1 + k // per
        mod = 0x10000 if u.kind == "1" else (0x2000 if u.kind == "0" else 0x100)
        t = v + k
        self._store(u, t % mod)
        return t // mod

    def _fold(self) -> None:
        """Bring every running timer-mode unit up to now; set the flags of the units that overflowed."""
        if self.chip is None:
            return
        now = self.now()
        for u in self.units:
            if not self._run.get(id(u)) or not self._timer_mode(u):
                u.t0 = now
                continue
            div = self._div(u)
            k = (now - u.t0) // div
            if k <= 0:
                continue
            u.t0 += k * div
            n = self._advance(u, k)
            if n:
                self._overflowed(u, n)

    def _fold_peek(self, blk, reg) -> int:
        """Debugger peek: the register as a read (which folds the running timers up to now) would return it. The fold
        runs on the register storage only (counts and TFx), which is restored afterwards: no flags/IRQ lines/t0."""
        if self.chip is None:
            return self.get(reg.name)
        saved = bytes(self.mem)
        try:
            now = self.now()
            for u in self.units:
                if not self._run.get(id(u)) or not self._timer_mode(u):
                    continue
                k = (now - u.t0) // self._div(u)
                if k > 0 and self._advance(u, k) and u.flag:
                    self.set("TCON", self.get("TCON") | u.flag)
            return self.get(reg.name)
        finally:
            self.mem[:] = saved

    def _overflowed(self, u: _Unit, n: int) -> None:
        if u.timer in (0, 1):
            self.overflows[u.timer] += n
        if u.flag:
            tcon = self.get("TCON")
            if not tcon & u.flag:
                self.set("TCON", tcon | u.flag)
                self._lines()
        tl = 0 if u.timer in (0, 2) and u.flag == TF0 else (1 if u.timer == 1 else None)
        if tl is not None:
            for cb in list(self.ovf_listeners.get(tl, ())):
                for _ in range(min(n, 1024)):
                    cb()

    def overflow_period(self, n: int) -> int | None:
        """Oscillator periods between overflows of timer n (the UART baud clock for n = 1); None if it does not run
        periodically in timer mode."""
        for u in self.units:
            if u.timer == n:
                if not self._run.get(id(u)) or not self._timer_mode(u):
                    return None
                div = self._div(u)
                if u.kind == "2":
                    return (0x100 - self.get(u.hi)) * div
                if u.kind == "1":
                    return 0x10000 * div
                if u.kind == "0":
                    return 0x2000 * div
        return None

    # ------------------------------------------------------------------ scheduling
    def _resched_all(self) -> None:
        if self.chip is None:
            return
        for u in self.units:
            self._resched(u)

    def _resched(self, u: _Unit) -> None:
        self.cancel(u.ev)
        u.ev = None
        if not self._run.get(id(u)) or not self._timer_mode(u):
            return
        # flags are set from events (never only lazily on a read) so a `JNB TF0,$` wait can be skipped to the next
        # event; once TFx is set nothing is scheduled until it is cleared (Timer 1 as a baud clock: one event)
        need = bool(u.flag) and not self.get("TCON") & u.flag
        need = need or (u.flag == TF0 and bool(self.ovf_listeners.get(0))) or \
            (u.timer == 1 and bool(self.ovf_listeners.get(1)))
        if not need:
            return
        div = self._div(u)
        due = u.t0 + self._to_ovf(u) * div
        u.ev = self.at(due, self._ovf_event, u)

    def _ovf_event(self, u: _Unit) -> None:
        u.ev = None
        self._fold()
        self._resched(u)

    # ------------------------------------------------------------------ flags / lines
    def _lines(self) -> None:
        if self.chip is None:
            return
        tcon = self.get("TCON")
        arch = self.chip.arch
        arch.set_irq("TF0", bool(tcon & TF0))
        arch.set_irq("TF1", bool(tcon & TF1))
        arch.set_irq("IE0", bool(tcon & IE0))
        arch.set_irq("IE1", bool(tcon & IE1))

    def _clear_flag(self, bit: int) -> None:
        self._fold()
        self.set("TCON", self.get("TCON") & ~bit)
        self._lines()
        self._resched_all()

    def _ack_ext(self, n: int) -> None:
        tcon = self.get("TCON")
        if tcon & (IT0 if n == 0 else IT1):                       # edge mode: cleared on vectoring
            self.set("TCON", tcon & ~(IE0 if n == 0 else IE1))
            self._lines()

    def _ext_sync(self, n: int) -> None:
        """Level mode: IEx = NOT INTx pin."""
        tcon = self.get("TCON")
        if tcon & (IT0 if n == 0 else IT1):
            return
        low = self._idle_pins["INT0" if n == 0 else "INT1"] == "L"
        bit = IE0 if n == 0 else IE1
        new = (tcon | bit) if low else (tcon & ~bit)
        if new != tcon:
            self.set("TCON", new)

    def pd_wakeup(self) -> bool:
        """Power-down exit condition: an enabled level-sensitive external interrupt is requesting."""
        tcon = self.get("TCON")
        arch = self.chip.arch
        for n, (bit, it, line) in enumerate(((IE0, IT0, "IE0"), (IE1, IT1, "IE1"))):
            if tcon & bit and not tcon & it and arch.enabled(line):
                return True
        return False

    # ------------------------------------------------------------------ pins
    def _pin_changed(self, sig: str, lvl: str) -> None:
        old = self._idle_pins.get(sig, "H")
        self._idle_pins[sig] = lvl
        fall = old != "L" and lvl == "L"
        if sig in ("INT0", "INT1"):
            n = 0 if sig == "INT0" else 1
            tcon = self.get("TCON")
            if tcon & (IT0 if n == 0 else IT1):
                if fall:
                    self.set("TCON", tcon | (IE0 if n == 0 else IE1))
            else:
                self._ext_sync(n)
            if self._gate(n):
                self._reconfigure()
            self._lines()
            return
        if fall and not self._stopped:                           # counter inputs T0 / T1
            n = 0 if sig == "T0" else 1
            for u in self.units:
                if u.timer == n and self._ct(n) and self._run.get(id(u)):
                    k = self._advance(u, 1)
                    if k:
                        self._overflowed(u, k)

    def _reconfigure(self) -> None:
        """Run states may have changed: fold with the old states, then re-evaluate and reschedule."""
        self._fold()
        now = self.now()
        for u in self.units:
            r = self._running(u)
            if r != self._run.get(id(u)):
                self._run[id(u)] = r
                u.t0 = now
        self._resched_all()

    # ------------------------------------------------------------------ SFR hooks
    def _tcon_r(self, blk, reg):
        self._fold()
        return None

    def _tcon_w(self, blk, reg, old, new):
        # fold with the old TCON (the store already happened: restore it while folding)
        self.set("TCON", old)
        self._fold()
        cur = self.get("TCON")
        # flags that overflowed during the fold and were not written as 1 stay as the CPU wrote them (RMW semantics)
        self.set("TCON", new)
        self._ext_sync(0)
        self._ext_sync(1)
        if (old ^ new) & (TR0 | TR1) or cur != old:
            self._reconfigure()
        else:
            self._resched_all()
        self._lines()
        return None

    def _tmod_w(self, blk, reg, old, new):
        self.set("TMOD", old)
        self._fold()
        self.set("TMOD", new)
        if (old ^ new) & 0x33:
            self._rebuild()
        else:
            self._reconfigure()
        return None

    def _cnt_r(self, blk, reg):
        self._fold()
        return None

    def _cnt_w(self, blk, reg, old, new):
        self.set(reg.name, old)
        self._fold()
        self.set(reg.name, new)
        now = self.now()
        for u in self.units:
            if reg.name in (u.lo, u.hi):
                u.t0 = now
        self._resched_all()
        return None

    # ------------------------------------------------------------------ clocks
    def clock_rebase(self) -> None:
        self._fold()

    def clock_resume(self) -> None:
        now = self.now()
        for u in self.units:
            u.t0 = now
        self._resched_all()

    def clock_stop(self, stopped: bool, mode: str) -> None:
        if mode != "pd":
            return                                   # Idle: the timers keep running
        self._fold()
        self._stopped = stopped
        self._reconfigure()
