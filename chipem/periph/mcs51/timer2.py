"""P89CV51 Timer/counter 2: T2CON C8H, T2MOD C9H, RCAP2L/H CAH/CBH, TL2/TH2 CCH/CDH (data sheet sec. 6.5 pp.30-36,
Tables 18-23).

Modes (Table 18): TR2 = 0 off; RCLK | TCLK = 1: baud-rate generator; else CP/RL2 = 1: 16-bit capture; T2MOD.T2OE =
1 (with C/T2 = 0): programmable clock-out on T2 (P1.0); else 16-bit auto-reload, counting down as well when
T2MOD.DCEN = 1 (direction = T2EX pin, 1 = up).
Count rate: timer mode counts once per peripheral clock (fosc/12, or fosc/6 in 6-clock mode with CKCON.T2X2 = 0);
baud-rate generator and clock-out modes count at twice that rate (fosc/2 in 12-clock mode: baud = fosc / (32 x
(65536 - RCAP2)); in 6-clock mode fosc / (16 x (65536 - RCAP2)) as the data sheet's equation 3 and Table 23 give);
C/T2 = 1 counts falling edges on T2 (not in clock-out).
* Up counting: overflow FFFFH -> reload RCAP2H:RCAP2L and set TF2 (not in baud-rate / clock-out mode).
* DCEN = 1, T2EX = 0: count down; when the count equals RCAP2 the next count reloads FFFFH and sets TF2 (underflow);
  EXF2 toggles on every overflow / underflow (17th bit).
* EXEN2 = 1: a falling edge on T2EX sets EXF2 and captures TL2/TH2 into RCAP2 (capture mode), reloads RCAP2 into
  the count (auto-reload, DCEN = 0), or only sets EXF2 (baud-rate mode, "an additional external interrupt").
* Clock-out: T2 toggles on every overflow (50 % duty, frequency = count rate / (2 x (65536 - RCAP2))); edges are
  scheduled through PinBoard.drive_signal("T2") while the pin is claimed; above fosc/200 the pin is not toggled
  (one `info`), the frequency is still reported by `clock_out_hz()`.
The count is computed lazily like Timer 0/1 (exact to the oscillator period); TF2 is set by an overflow event,
scheduled while TF2 is clear.  Interrupt line "TF2" = TF2 | EXF2 (vector of Table 41).
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ._util import SfrBlock, find_model, listen_pin, periph_div, pin_level

TF2, EXF2, RCLK, TCLK, EXEN2, TR2, C_T2, CP_RL2 = 0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01
T2OE, DCEN = 0x02, 0x01


@register("mcs51.timer2")
class Timer2(SfrBlock):
    model = "mcs51.timer2"
    table = ("TIMER2",)

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        ov = {"T2CON": {"on_read": self._fold_r, "on_write": self._cfg_w, "on_peek": self._fold_peek},
              "T2MOD": {"on_write": self._cfg_w, "ro_mask": 0xFC},
              "TL2": {"on_read": self._fold_r, "on_write": self._cnt_w, "on_peek": self._fold_peek},
              "TH2": {"on_read": self._fold_r, "on_write": self._cnt_w, "on_peek": self._fold_peek},
              "RCAP2L": {"on_write": self._cfg_w}, "RCAP2H": {"on_write": self._cfg_w}}
        super().__init__(name, base, size, regs, params=params, log=log, overrides=ov)
        self.t0 = 0
        self._ev = None
        self._co_ev = None
        self._co_level = "L"
        self._stopped = False
        self._t2ex = "H"
        self._warned_fast = False
        self.pin_t2, self.pin_t2ex = "P1.0", "P1.1"

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        listen_pin(chip, self.pin_t2ex, self._t2ex_changed)
        listen_pin(chip, self.pin_t2, self._t2_changed)
        getattr(chip.arch, "ie_listeners", []).append(self._resched)

    def reset(self) -> None:
        self.cancel(self._ev)
        self.cancel(self._co_ev)
        self._ev = self._co_ev = None
        super().reset()
        self._stopped = False
        if self.chip is not None:
            self.t0 = self.now()
            self._t2ex = pin_level(self.chip, self.pin_t2ex)
            self._t2_in = pin_level(self.chip, self.pin_t2)
            self._clock_out_pin(False)
            self._line()

    # ------------------------------------------------------------------ configuration
    def _con(self) -> int:
        return self.get("T2CON")

    def brg(self) -> bool:
        return bool(self._con() & (RCLK | TCLK))

    def clock_out(self) -> bool:
        return bool(self.get("T2MOD") & T2OE) and not self._con() & C_T2 and not self.brg()

    def running(self) -> bool:
        return bool(self._con() & TR2) and not self._stopped

    def counter_mode(self) -> bool:
        return bool(self._con() & C_T2) and not self.clock_out()

    def rcap(self) -> int:
        return (self.get("RCAP2H") << 8) | self.get("RCAP2L")

    def count(self) -> int:
        return (self.get("TH2") << 8) | self.get("TL2")

    def _set_count(self, v: int) -> None:
        self.set("TL2", v & 0xFF)
        self.set("TH2", (v >> 8) & 0xFF)

    def period(self) -> int:
        """Oscillator periods per count (timer mode, baud-rate generator / clock-out)."""
        d = periph_div(self.chip, "T2X2")
        return max(d // 6, 1) if (self.brg() or self.clock_out()) else d

    def brg_period(self) -> int | None:
        """Oscillator periods per Timer 2 overflow in baud-rate generator mode (None if not running as one)."""
        if not self.running() or not self.brg() or self._con() & C_T2:
            return None
        return (0x10000 - self.rcap()) * self.period()

    def baud_source(self, rx: bool) -> bool:
        return bool(self._con() & (RCLK if rx else TCLK))

    def clock_out_hz(self) -> float:
        if not self.running() or not self.clock_out():
            return 0.0
        return self.chip.clock.hz / (2.0 * (0x10000 - self.rcap()) * self.period())

    def _down(self) -> bool:
        return bool(self.get("T2MOD") & DCEN) and not self.brg() and not self._con() & CP_RL2 \
            and self._t2ex == "L"

    # ------------------------------------------------------------------ counting
    def _advance(self, k: int) -> int:
        """k counts; returns the number of overflows/underflows."""
        if k <= 0:
            return 0
        v, r = self.count(), self.rcap()
        reload = not (self._con() & CP_RL2) or self.brg()
        if self._down():
            per = 0x10000 - r                       # from FFFF down to r inclusive, then reload FFFF
            first = v - r + 1
            if k < first:
                self._set_count(v - k)
                return 0
            k -= first
            self._set_count(0xFFFF - k % per)
            return 1 + k // per
        if not reload:                              # capture mode: free 16-bit
            t = v + k
            self._set_count(t & 0xFFFF)
            return t >> 16
        first = 0x10000 - v
        if k < first:
            self._set_count(v + k)
            return 0
        per = 0x10000 - r
        k -= first
        self._set_count(r + k % per)
        return 1 + k // per

    def _fold(self) -> None:
        if self.chip is None:
            return
        now = self.now()
        if not self.running() or self.counter_mode():
            self.t0 = now
            return
        p = self.period()
        k = (now - self.t0) // p
        if k <= 0:
            return
        self.t0 += k * p
        n = self._advance(k)
        if n:
            self._overflowed(n)

    def _overflowed(self, n: int) -> None:
        con = self._con()
        if self.brg() or self.clock_out():
            return
        new = con | TF2
        if self.get("T2MOD") & DCEN and not con & CP_RL2 and n & 1:
            new ^= EXF2
        if new != con:
            self.set("T2CON", new)
            self._line()

    def _line(self) -> None:
        if self.chip is not None:
            self.chip.arch.set_irq("TF2", bool(self._con() & (TF2 | EXF2)))

    def _resched(self) -> None:
        if self.chip is None:
            return
        self.cancel(self._ev)
        self._ev = None
        if not self.running() or self.counter_mode() or self.brg() or self.clock_out():
            return
        if self._con() & TF2:
            return
        v, r = self.count(), self.rcap()
        if self._down():
            d = v - r + 1
        else:
            d = 0x10000 - v
        self._ev = self.at(self.t0 + d * self.period(), self._ovf_event)

    def _ovf_event(self) -> None:
        self._ev = None
        self._fold()
        self._resched()

    # ------------------------------------------------------------------ clock-out
    def _clock_out_pin(self, on: bool) -> None:
        ports = find_model(self.chip, "mcs51.port")
        self.cancel(self._co_ev)
        self._co_ev = None
        if ports is None:
            return
        if not on:
            ports.release(self.pin_t2, "T2")
            if self.chip.pins is not None:
                self.chip.pins.release_signal("T2")
            return
        ports.claim(self.pin_t2, "T2", self._co_level)
        half = (0x10000 - self.rcap()) * self.period()
        if half < 100:
            if not self._warned_fast:
                self._warned_fast = True
                self.emit("info", block=self.name, msg=f"{self.name}: clock-out at {self.clock_out_hz():.0f} Hz is "
                          "too fast to toggle edge by edge; the T2 pin is not toggled")
            return
        self._co_ev = self.at(self.now() + half, self._co_toggle, half)

    def _co_toggle(self, half: int) -> None:
        self._co_level = "H" if self._co_level == "L" else "L"
        self.chip.pins.drive_signal("T2", self._co_level)
        self._co_ev = self.at(self.now() + half, self._co_toggle, half)

    # ------------------------------------------------------------------ pins
    def _t2ex_changed(self, lvl: str) -> None:
        old, self._t2ex = self._t2ex, lvl
        if self._stopped:
            return
        if self.get("T2MOD") & DCEN:
            self._fold()                       # direction change: count up to now in the old direction
        if old != "L" and lvl == "L" and self._con() & EXEN2:
            self._fold()
            con = self._con() | EXF2
            if not self.brg():
                if con & CP_RL2:
                    self.set("RCAP2L", self.get("TL2"))
                    self.set("RCAP2H", self.get("TH2"))
                elif not self.get("T2MOD") & DCEN:
                    self._set_count(self.rcap())
                    self.t0 = self.now()
            self.set("T2CON", con)
            self._line()
        self._resched()

    def _t2_changed(self, lvl: str) -> None:
        old, self._t2_in = getattr(self, "_t2_in", "H"), lvl
        if old != "L" and lvl == "L" and self.running() and self.counter_mode():
            n = self._advance(1)
            if n:
                self._overflowed(n)

    # ------------------------------------------------------------------ SFR hooks
    def _fold_peek(self, blk, reg) -> int:
        """Debugger peek: the value a read (which folds the counter up to now) returns, computed on the register
        storage only and restored afterwards (no t0 / IRQ line change)."""
        if self.chip is None or not self.running() or self.counter_mode():
            return self.get(reg.name)
        saved = bytes(self.mem)
        try:
            k = (self.now() - self.t0) // self.period()
            n = self._advance(k) if k > 0 else 0
            if n and not (self.brg() or self.clock_out()):
                con = self._con()
                new = con | TF2
                if self.get("T2MOD") & DCEN and not con & CP_RL2 and n & 1:
                    new ^= EXF2
                self.set("T2CON", new)
            return self.get(reg.name)
        finally:
            self.mem[:] = saved

    def _fold_r(self, blk, reg):
        self._fold()
        return None

    def _cfg_w(self, blk, reg, old, new):
        self.set(reg.name, old)
        was_co = self.clock_out() and self.running()
        self._fold()
        self.set(reg.name, new)
        self.t0 = self.now()
        self._line()
        co = self.clock_out() and self.running()
        if co != was_co or (co and reg.name.startswith("RCAP")):
            self._clock_out_pin(co)
        self._resched()
        if reg.name == "T2CON" and (old ^ new) & (RCLK | TCLK | TR2):
            u = find_model(self.chip, "mcs51.uart")
            if u is not None:
                u.config_changed()
        return None

    def _cnt_w(self, blk, reg, old, new):
        self.set(reg.name, old)
        self._fold()
        self.set(reg.name, new)
        self.t0 = self.now()
        self._resched()
        return None

    # ------------------------------------------------------------------ clocks
    def clock_rebase(self) -> None:
        self._fold()

    def clock_resume(self) -> None:
        self.t0 = self.now()
        self._resched()

    def clock_stop(self, stopped: bool, mode: str) -> None:
        if mode != "pd":
            return
        self._fold()
        self._stopped = stopped
        self.t0 = self.now()
        self._resched()
