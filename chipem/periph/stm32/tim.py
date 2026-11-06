"""STM32F4 timers, counting only: TIM2-5 (general purpose, TIM2/TIM5 32-bit), TIM6/7 (basic), TIM1/8 (advanced).

Registers from the SVD; behaviour (RM0090 ch. 17-20, time base unit only):
* Counter clock = timer kernel clock / (PSC + 1); kernel clock = RCC timer clock of the timer's APB bus (APB2 for
  TIM1/8/9/10/11, APB1 otherwise; x2 when the APB prescaler is not 1), consistent with the actual core clock.
* CR1: CEN starts/stops, DIR down-counting, CMS (center-aligned: counts up and down, period 2 x ARR), OPM (CEN
  clears at the update event), ARPE (ARR preload), UDIS (no update events), URS (UG sets no UIF).
* PSC is always preloaded and ARR when ARPE=1: the active values load at each update event (and on EGR.UG).  A
  non-preloaded ARR write takes effect immediately.
* Update event when the counter wraps (up: ARR -> 0, down: 0 -> ARR): SR.UIF is set; the IRQ line (TIM2, TIM3,
  TIM4, TIM5, TIM6_DAC, TIM7, TIM1_UP_TIM10, TIM8_UP_TIM13; `params.irq` overrides) is UIF & DIER.UIE, level.
  The repetition counter (RCR, TIM1/8) divides update events.  One scheduler event per update (wall-clock).
* CNT reads are live (instruction-exact mid-slice); CNT writes restart the count from the written value.
* SR flags are rc_w0 (write 0 to clear).  CCRx/CCMRx/CCER/BDTR are storage: no compare flags, no input capture and
  no PWM/output-compare on pins (a `warn` is logged once when firmware enables a CCxE output).
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ._util import GatedBlock, build_regs, irq_number, timer_hz, rcc

CEN, UDIS, URS, OPM, DIR, ARPE = 1 << 0, 1 << 1, 1 << 2, 1 << 3, 1 << 4, 1 << 7
CMS_SHIFT = 5
UIF, UIE, UG = 1, 1, 1
APB2_TIMERS = ("TIM1", "TIM8", "TIM9", "TIM10", "TIM11")
IRQ_NAMES = {"TIM1": "TIM1_UP_TIM10", "TIM8": "TIM8_UP_TIM13", "TIM6": "TIM6_DAC", "TIM9": "TIM1_BRK_TIM9",
             "TIM10": "TIM1_UP_TIM10", "TIM11": "TIM1_TRG_COM_TIM11", "TIM12": "TIM8_BRK_TIM12",
             "TIM13": "TIM8_UP_TIM13", "TIM14": "TIM8_TRG_COM_TIM14"}
WIDE = ("TIM2", "TIM5")


@register("stm32.tim")
class TIM(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = name
        self.bits = int(params.get("bits", 32 if name in WIDE else 16))
        self.cmask = (1 << self.bits) - 1
        self.apb = int(params.get("apb", 2 if name in APB2_TIMERS else 1))
        ov: dict[str, dict] = {
            "CR1": {"on_write": self._cr1_write},
            "PSC": {"on_write": self._psc_write},
            "ARR": {"on_write": self._arr_write},
            "CNT": {"on_read": self._cnt_read, "on_write": self._cnt_write},
            "EGR": {"on_write": self._egr_write, "on_read": lambda b, r: 0},
            "DIER": {"on_write": self._dier_write},
            "SR": {"on_write": self._sr_write},
            "CCER": {"on_write": self._ccer_write},
        }
        r = build_regs((name, "TIM2" if name in WIDE else "TIM3"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self._ev = None
        self._irqn: int | None = None
        self._line = False
        self.running = False
        self.psc = 0                 # active (shadow) prescaler
        self.arr = 0                 # active (shadow) auto-reload
        self.rep = 0                 # repetition down-counter
        self._start_ns = 0.0         # time of the last (re)start from _start_cnt
        self._start_cnt = 0
        self._tick_ns = 0.0          # counter clock period
        self._warned_out = False
        self.updates = 0

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, str(self.params.get("irq") or IRQ_NAMES.get(self.name, self.name)))
        r = rcc(chip)
        if r is not None:
            r.listeners.append(self._clock_changed)

    def reset(self) -> None:
        self._stop()
        if getattr(self, "_line", False) and self.chip is not None and self._irqn is not None:
            self.irq(self._irqn, False)
        self._line = False
        super().reset()
        self.psc = self.get("PSC")
        self.arr = self.get("ARR") & self.cmask
        self.rep = 0
        self._start_cnt = 0
        self._warned_out = False

    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()
        MMIOBlock.write(self, offset, size, value)

    # ------------------------------------------------------------ derived
    def kernel_hz(self) -> float:
        return timer_hz(self.chip, self.apb) if self.chip is not None else 0.0

    def counter_hz(self) -> float:
        return self.kernel_hz() / (self.psc + 1)

    def _cms(self) -> int:
        return (self.get("CR1") >> CMS_SHIFT) & 3

    def _down(self) -> bool:
        return bool(self.get("CR1") & DIR)

    def period_ns(self) -> float:
        """Time between update events (before the repetition counter)."""
        n = 2 * self.arr if self._cms() else self.arr + 1
        return max(n, 1) * self._tick_ns

    def _now(self) -> float:
        return self.chip.now_ns()

    # ------------------------------------------------------------ counting
    def _stop(self) -> None:
        if self._ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None
        self.running = False

    def _ticks_to_update(self, cnt: int) -> int:
        """Counter clocks from `cnt` (just counted) until the next update event."""
        arr = self.arr
        if self._cms():
            # count up 0..ARR then down ARR..0; model as a phase in [0, 2*ARR)
            return max(2 * arr - (cnt % max(2 * arr, 1)), 1)
        if self._down():
            return cnt + 1
        if cnt > arr:                    # counter above ARR (ARR lowered without preload): runs to wrap-around
            return (self.cmask - cnt) + 1 + arr + 1
        return arr - cnt + 1

    def _start(self, cnt: int, t_ns: float | None = None) -> None:
        self._stop()
        if not self.get("CR1") & CEN or self.chip is None:
            return
        hz = self.counter_hz()
        if hz <= 0:
            return
        self.running = True
        self._tick_ns = 1e9 / hz
        self._start_ns = self._now() if t_ns is None else t_ns
        self._start_cnt = cnt
        dt = self._ticks_to_update(cnt) * self._tick_ns
        self._ev = self.chip.sched.at_ns(self._start_ns + dt, self._update_event)

    def cnt(self) -> int:
        if not self.running:
            return self.get("CNT") & self.cmask
        el = int((self._now() - self._start_ns) / self._tick_ns + 1e-6)
        c = self._start_cnt
        if self._cms():
            ph = (c + el) % max(2 * self.arr, 1)
            return ph if ph <= self.arr else 2 * self.arr - ph
        if self._down():
            return max(c - el, 0)
        v = c + el
        return v if c > self.arr else min(v, self.arr)

    def _update_event(self) -> None:
        self._ev = None
        t = self._start_ns + self._ticks_to_update(self._start_cnt) * self._tick_ns
        self._load_shadows()
        restart = 0 if not self._down() or self._cms() else self.arr
        if self.rep > 0:
            self.rep -= 1
        else:
            self.rep = self.get("RCR") & 0xFF if "RCR" in self.regs else 0
            if not self.get("CR1") & UDIS:
                self.updates += 1
                self.set("SR", self.get("SR") | UIF)
                self._update_line()
            if self.get("CR1") & OPM:
                self.set("CR1", self.get("CR1") & ~CEN)
                self.set("CNT", restart)
                self._stop()
                return
        self._start(restart, t)

    def _load_shadows(self) -> None:
        self.psc = self.get("PSC") & 0xFFFF
        if self.get("CR1") & ARPE:
            self.arr = self.get("ARR") & self.cmask

    def _clock_changed(self) -> None:
        if self.running:
            c = self.cnt()
            self._start(c)

    # ------------------------------------------------------------ IRQ
    def _update_line(self) -> None:
        lvl = bool(self.get("SR") & UIF and self.get("DIER") & UIE)
        if lvl != self._line:
            self._line = lvl
            if self._irqn is not None and self.chip is not None:
                self.irq(self._irqn, lvl)

    # ------------------------------------------------------------ register hooks
    def _cr1_write(self, blk, reg, old, new):
        self._store(reg, new)
        if (old ^ new) & CEN:
            if new & CEN:
                self._start(self.get("CNT") & self.cmask)
                self.emit("info", block=self.name, msg=f"{self.name} started: update every "
                          f"{self.period_ns() / 1e3:.3f} us ({self.counter_hz() / 1e6:.6g} MHz counter clock, "
                          f"PSC={self.psc}, ARR={self.arr})")
            else:
                self.set("CNT", self.cnt())
                self._stop()
        elif self.running and (old ^ new) & (DIR | (3 << CMS_SHIFT)):
            self._start(self.cnt())
        return None

    def _psc_write(self, blk, reg, old, new):
        return None                      # preloaded: active at the next update event

    def _arr_write(self, blk, reg, old, new):
        if not self.get("CR1") & ARPE:
            self.arr = new & self.cmask
            if self.running:
                self._start(self.cnt())
        return None

    def _cnt_read(self, blk, reg):
        return self.cnt()

    def _cnt_write(self, blk, reg, old, new):
        if self.running:
            self._start(new & self.cmask)
        return None

    def _egr_write(self, blk, reg, old, new):
        if new & UG:
            self.psc = self.get("PSC") & 0xFFFF
            self.arr = self.get("ARR") & self.cmask
            self.rep = self.get("RCR") & 0xFF if "RCR" in self.regs else 0
            start = self.arr if (self._down() and not self._cms()) else 0
            self.set("CNT", start)
            if not self.get("CR1") & (URS | UDIS):
                self.set("SR", self.get("SR") | UIF)
                self._update_line()
            if self.running:
                self._start(start)
        return 0

    def _dier_write(self, blk, reg, old, new):
        self._update_line()
        return None

    def _sr_write(self, blk, reg, old, new):
        v = old & new                     # rc_w0
        self._store(reg, v)
        self._update_line()
        return v

    def _ccer_write(self, blk, reg, old, new):
        if new & 0x1111 and not self._warned_out:
            self._warned_out = True
            self.emit("warn", block=self.name, msg=f"{self.name}: capture/compare outputs (CCER=0x{new:04x}) are not "
                      f"modelled: no PWM/compare levels on pins, no CCxIF flags")
        return None
