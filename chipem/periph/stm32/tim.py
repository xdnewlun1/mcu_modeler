"""STM32F4 timers, counting only: TIM2-5 (general purpose, TIM2/TIM5 32-bit), TIM6/7 (basic), TIM1/8 (advanced).

Registers from the SVD; behaviour (RM0090 ch. 17-20, time base unit only):
* Counter clock = timer kernel clock / (PSC + 1); kernel clock = RCC timer clock of the timer's APB bus (APB2 for
  TIM1/8/9/10/11, APB1 otherwise; x2 when the APB prescaler is not 1), consistent with the actual core clock.
* CR1: CEN starts/stops, DIR down-counting, CMS (center-aligned: 0 -> ARR -> 0, period 2 x ARR, DIR is read-only
  and reads the current direction), OPM (CEN clears at the update event), ARPE (ARR preload), UDIS (no update
  events: shadows keep their value; UG only re-initialises the counter), URS (UG sets no UIF).
* PSC is always preloaded and ARR when ARPE=1: the active values load at each update event (and on EGR.UG).  A
  non-preloaded ARR write takes effect immediately; a counter already above the new ARR runs on to the wrap-around.
* Counter overflow/underflow (up: ARR -> 0, down: 0 -> ARR, center-aligned: at ARR and at 0, so twice per period)
  decrements the repetition counter (RCR, TIM1/8); an update event (UEV) happens only when it is 0 and UDIS=0:
  SR.UIF is set and the shadows load.  The IRQ line (TIM2, TIM3, TIM4, TIM5, TIM6_DAC, TIM7, TIM1_UP_TIM10,
  TIM8_UP_TIM13; `params.irq` overrides) is UIF & DIER.UIE, level.  One scheduler event per overflow (wall-clock).
* CNT reads are live (instruction-exact mid-slice); CNT writes restart the count from the written value.
* SR flags are rc_w0 (write 0 to clear).
* Output compare / PWM (RM0090 17.3.8-17.3.10, 18.3.7-18.3.9), channels 1-4 with CCxS=00: a compare match
  (counter == active CCRx; CCRx preloaded when OCxPE=1, loaded at the update event) sets CCxIF (IRQ with CCxIE: the
  timer's IRQ, or TIMx_CC on TIM1/8) and moves OCxREF per OCxM: frozen, active/inactive/toggle on match, force
  inactive/active, PWM1 (up: REF = CNT < CCR; down: REF = CNT <= CCR), PWM2 (inverse); center-aligned counting
  uses the rule of the current direction.  The output is OCxREF ^ CCxP while CCxE (and MOE on TIM1/8); the
  complementary output OCxN (TIM1/8, CCxNE) is ~OCxREF ^ CCxNP, or OCxREF ^ CCxNP with CCxE=0.  Each edge is a
  scheduler event at its exact counter tick, and pins muxed to TIMx_CHy/TIMx_CHyN follow it (PinBoard.drive_signal).
  Not modelled: dead time (BDTR.DTG), break input, OCxCE/ETR clear, OCxFE, one-pulse delay, input capture (CCxS!=0
  is stored only; one `warn`), DMA requests; a disabled output (or MOE=0) shows L.
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
OCPE = 1 << 3                    # CCMRx output preload enable (per channel byte)
MOE = 1 << 15                    # BDTR main output enable (TIM1/8)


class _Chan:
    __slots__ = ("n", "ccr", "ref", "ev", "out", "outn", "mode", "ccs", "pe", "e", "pol", "ne", "npol", "irq", "active",
                 "lazy_k")

    def __init__(self, n: int):
        self.n = n
        self.ccr = 0             # active compare value (the preload register is CCRn)
        self.ref = False         # OCxREF
        self.ev = None           # next compare-match event
        self.out: str | None = None      # level driven on TIMx_CHn (None: not driving)
        self.outn: str | None = None     # level driven on TIMx_CHnN
        # configuration cached from CCMRx / CCER / BDTR / DIER (refreshed on their writes)
        self.mode = self.ccs = self.pe = 0
        self.e = self.pol = self.ne = self.npol = False
        self.irq = False         # CCxIE or CCxDE: matches must happen at their exact time
        self.active = False      # needs match events (an enabled output or an interrupt/DMA request)
        self.lazy_k = -1         # quiet channel: last match tick (since the anchor) already reflected in CCxIF


@register("stm32.tim")
class TIM(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = name
        self.bits = int(params.get("bits", 32 if name in WIDE else 16))
        self.cmask = (1 << self.bits) - 1
        self.apb = int(params.get("apb", 2 if name in APB2_TIMERS else 1))
        ov: dict[str, dict] = {
            "CR1": {"on_write": self._cr1_write, "on_read": self._cr1_read},
            "PSC": {"on_write": self._psc_write},
            "ARR": {"on_write": self._arr_write},
            "CNT": {"on_read": self._cnt_read, "on_write": self._cnt_write},
            "EGR": {"on_write": self._egr_write, "on_read": lambda b, r: 0},
            "DIER": {"on_write": self._dier_write},
            "SR": {"on_write": self._sr_write, "on_read": self._sr_read},
            "CCER": {"on_write": self._ccer_write},
            "BDTR": {"on_write": self._oc_cfg_write},
            "CCMR1_Output": {"on_write": self._oc_cfg_write}, "CCMR1_Input": {"on_write": self._oc_cfg_write},
            "CCMR2_Output": {"on_write": self._oc_cfg_write}, "CCMR2_Input": {"on_write": self._oc_cfg_write},
        }
        for n in range(1, 5):
            ov[f"CCR{n}"] = {"on_write": self._ccr_write(n)}
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
        self._start_cnt = 0          # counter value at _start_ns; center-aligned: phase in [0, 2*ARR) (>= ARR: down)
        self._tick_ns = 0.0          # counter clock period
        self._warned_out = False
        self.updates = 0
        self.ch = [_Chan(n) for n in range(1, 5) if f"CCR{n}" in self.regs]
        self._ccmr = [next((x for x in (f"CCMR{k}_Output", f"CCMR{k}_Input") if x in self.regs), None) for k in (1, 2)]
        self._advanced = "BDTR" in self.regs
        self._cc_irqn: int | None = None
        self._cc_line = False
        self._quiet: list[_Chan] = []       # output channels without output/IRQ/DMA: CCxIF computed when needed

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, str(self.params.get("irq") or IRQ_NAMES.get(self.name, self.name)))
        self._cc_irqn = irq_number(chip, f"{self.name}_CC")        # TIM1/8: capture/compare has its own vector
        r = rcc(chip)
        if r is not None:
            r.listeners.append(self._clock_changed)

    def reset(self) -> None:
        self._stop()
        if getattr(self, "_line", False) and self.chip is not None and self._irqn is not None:
            self.irq(self._irqn, False)
        if getattr(self, "_cc_line", False) and self.chip is not None and self._cc_irqn is not None:
            self.irq(self._cc_irqn, False)
        self._line = self._cc_line = False
        pins = getattr(self.chip, "pins", None)
        for c in getattr(self, "ch", ()):
            c.ref = False
            c.ccr = 0
            for sig in (f"{self.name}_CH{c.n}", f"{self.name}_CH{c.n}N"):
                if pins is not None:
                    pins.release_signal(sig, "L")
            c.out = c.outn = None
        super().reset()
        self.psc = self.get("PSC")
        self.arr = self.get("ARR") & self.cmask
        self.rep = 0
        self._start_cnt = 0
        self._warned_out = False
        self._chan_cfg()

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
        return (self.mem[0] >> CMS_SHIFT) & 3        # CR1 is at offset 0; CMS and DIR are in its low byte

    def _down(self) -> bool:
        return bool(self.mem[0] & DIR)

    def period_ns(self) -> float:
        """Time between update events (before the repetition counter)."""
        n = 2 * self.arr if self._cms() else self.arr + 1
        return max(n, 1) * self._tick_ns

    def _now(self) -> float:
        return self.chip.now_ns()

    # ------------------------------------------------------------ counting
    def _stop(self) -> None:
        if getattr(self, "_quiet", None) and self.running:
            self._lazy_flags(self._ticks_now())        # quiet channels: matches up to now
        self._quiet = []
        if self._ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None
        self.running = False
        for c in getattr(self, "ch", ()):                  # outputs hold their level while the counter is stopped
            if c.ev is not None and self.chip is not None:
                self.chip.sched.cancel(c.ev)
            c.ev = None

    def _ticks_to_update(self, cnt: int) -> int:
        """Counter clocks from `cnt` (just counted) until the next update event."""
        arr = self.arr
        if self._cms():
            # phase in [0, 2*ARR): up 0..ARR (overflow at ARR), down ARR..0 (underflow at 0)
            p = cnt % max(2 * arr, 1)
            return max((arr - p) if p < arr else (2 * arr - p), 1)
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
        if self.ch:
            self._oc_resync(start=True)

    def _phase(self, cnt: int, down: bool) -> int:
        """Center-aligned phase of counter value `cnt` counting in direction `down` (current ARR)."""
        arr = self.arr
        cnt = min(cnt, arr)
        return (2 * arr - cnt) % max(2 * arr, 1) if down and cnt else cnt

    def _cnt_dir(self) -> tuple[int, bool]:
        """(counter value, counting down) now."""
        cr1 = self.get("CR1")
        if not self.running:
            return self.get("CNT") & self.cmask, bool(cr1 & DIR)
        el = int((self._now() - self._start_ns) / self._tick_ns + 1e-6)
        c = self._start_cnt
        if (cr1 >> CMS_SHIFT) & 3:
            arr = self.arr
            ph = (c + el) % max(2 * arr, 1)
            return (ph, False) if ph < arr else (2 * arr - ph, True)
        if cr1 & DIR:
            return max(c - el, 0), True
        v = c + el
        return (v if c > self.arr else min(v, self.arr)), False

    def cnt(self) -> int:
        return self._cnt_dir()[0]

    def _restart_pos(self) -> int:
        """Start position (counter value, or phase when center-aligned) from the current CNT/direction."""
        c, down = self._cnt_dir()
        return self._phase(c, down) if self._cms() else c

    def _update_event(self) -> None:
        """Counter overflow/underflow; an update event (UEV) only when the repetition counter is 0 and UDIS=0."""
        self._ev = None
        if self._quiet:
            self._lazy_flags(None)          # quiet channels: CCxIF of the matches in the period that ends here
        ticks = self._ticks_to_update(self._start_cnt)
        t = self._start_ns + ticks * self._tick_ns
        cms = self._cms()
        if cms:                         # where the counter is at this overflow (ARR, going down) or underflow (0)
            at_top = (self._start_cnt + ticks) % max(2 * self.arr, 1) == self.arr
            cnt, down = (self.arr, True) if at_top else (0, False)
        uev = False
        if self.rep > 0:
            self.rep -= 1
        elif not self.get("CR1") & UDIS:
            uev = True
            self._load_shadows()
            self.rep = self.get("RCR") & 0xFF if "RCR" in self.regs else 0
            self.updates += 1
            self.set("SR", self.get("SR") | UIF)
            self._update_line()
        if cms:
            restart = self._phase(cnt, down)
        else:
            restart = self.arr if self._down() else 0
        if uev and self.get("CR1") & OPM:
            self.set("CR1", self.get("CR1") & ~CEN)
            self.set("CNT", cnt if cms else restart)
            self._stop()
            return
        self._start(restart, t)

    def _load_shadows(self) -> None:
        self.psc = self.get("PSC") & 0xFFFF
        if self.get("CR1") & ARPE:
            self.arr = self.get("ARR") & self.cmask
        for c in self.ch:
            if c.pe:
                c.ccr = self.get(f"CCR{c.n}") & self.cmask

    def _clock_changed(self) -> None:
        if self.running:
            self._start(self._restart_pos())

    # ------------------------------------------------------------ IRQ
    def _update_line(self) -> None:
        sr, dier = self.get("SR"), self.get("DIER")
        up = bool(sr & UIF and dier & UIE)
        cc = bool(sr & dier & 0x1E)                      # CC1IF..CC4IF & CC1IE..CC4IE
        if self._cc_irqn is None:
            up = up or cc
        elif cc != self._cc_line:
            self._cc_line = cc
            if self.chip is not None:
                self.irq(self._cc_irqn, cc)
        if up != self._line:
            self._line = up
            if self._irqn is not None and self.chip is not None:
                self.irq(self._irqn, up)

    # ------------------------------------------------------------ register hooks
    def _cr1_read(self, blk, reg):
        v = int.from_bytes(self.mem[reg.boff:reg.boff + reg.bw], "little")
        if self.running and (v >> CMS_SHIFT) & 3:          # center-aligned: DIR is read-only status
            v = (v | DIR) if self._cnt_dir()[1] else (v & ~DIR)
        return v

    def _cr1_write(self, blk, reg, old, new):
        if (old >> CMS_SHIFT) & 3 and (new >> CMS_SHIFT) & 3:
            new = (new & ~DIR) | (old & DIR)              # DIR is read-only while center-aligned
        if self.running and (old ^ new) & (CEN | DIR | (3 << CMS_SHIFT)):
            self._store(reg, old)                         # counter and direction in the old mode
            c, down = self._cnt_dir()
        else:
            c, down = self.get("CNT") & self.cmask, bool(old & DIR)
        self._store(reg, new)
        if (old ^ new) & CEN:
            if new & CEN:
                if (new >> CMS_SHIFT) & 3:
                    self._start(self._phase(c, bool(new & DIR)))
                else:
                    self._start(c)
                self.emit("info", block=self.name, msg=f"{self.name} started: update every "
                          f"{self.period_ns() / 1e3:.3f} us ({self.counter_hz() / 1e6:.6g} MHz counter clock, "
                          f"PSC={self.psc}, ARR={self.arr})")
            else:
                self.set("CNT", c)
                if (old >> CMS_SHIFT) & 3:                # keep the direction for the restart
                    new = (new | DIR) if down else (new & ~DIR)
                self._stop()
                return new
        elif self.running and (old ^ new) & (DIR | (3 << CMS_SHIFT)):
            self._start(self._phase(c, down) if (new >> CMS_SHIFT) & 3 else c)
        return new

    def _psc_write(self, blk, reg, old, new):
        return None                      # preloaded: active at the next update event

    def _arr_write(self, blk, reg, old, new):
        if not self.get("CR1") & ARPE:
            c, down = self._cnt_dir()                  # where the counter is under the old ARR
            self.arr = new & self.cmask
            if self.running:
                self._start(self._phase(c, down) if self._cms() else c)
        return None

    def _cnt_read(self, blk, reg):
        return self.cnt()

    def _cnt_write(self, blk, reg, old, new):
        if self.running:
            c = new & self.cmask
            self._start(self._phase(c, self._cnt_dir()[1]) if self._cms() else c)
        return None

    def _egr_write(self, blk, reg, old, new):
        if new & UG:
            cr1 = self.get("CR1")
            if not cr1 & UDIS:                 # UDIS: the counter re-initialises, the shadows keep their value
                self.psc = self.get("PSC") & 0xFFFF
                self.arr = self.get("ARR") & self.cmask
                for c in self.ch:
                    c.ccr = self.get(f"CCR{c.n}") & self.cmask
                self.rep = self.get("RCR") & 0xFF if "RCR" in self.regs else 0
                if not cr1 & URS:
                    self.set("SR", self.get("SR") | UIF)
                    self._update_line()
            start = self.arr if (self._down() and not self._cms()) else 0
            self.set("CNT", start)
            if self._cms():
                self.set("CR1", self.get("CR1") & ~DIR)   # restarts counting up from 0
            if self.running:
                self._start(start)
        if new & 0x1E:                         # CCxG: software compare/capture event -> CCxIF
            self.set("SR", self.get("SR") | (new & 0x1E))
            self._update_line()
        return 0

    def _dier_write(self, blk, reg, old, new):
        self._update_line()
        if (old ^ new) & 0x1E1E and self.ch:          # CCxIE / CCxDE changed: matches may need exact events now
            self._store(reg, new)
            self._oc_config_changed()
        return None

    def _sr_write(self, blk, reg, old, new):
        v = old & new                     # rc_w0
        self._store(reg, v)
        self._update_line()
        return v

    def _ccer_write(self, blk, reg, old, new):
        self._store(reg, new)
        self._oc_config_changed()
        return None


    # ------------------------------------------------------------ output compare / PWM
    def _ccmr_field(self, n: int) -> int:
        name = self._ccmr[(n - 1) // 2]
        return (self.get(name) >> (8 * ((n - 1) % 2))) & 0xFF if name else 0

    def _chan_cfg(self) -> None:
        """Refresh the cached channel configuration from CCMRx, CCER, BDTR and DIER."""
        if not self.ch:                                  # basic timers (TIM6/7) have no channels
            return
        ccer = self.get("CCER")
        dier = self.get("DIER")
        moe = bool(self.get("BDTR") & MOE) if self._advanced else True
        for c in self.ch:
            f = self._ccmr_field(c.n)
            c.ccs, c.pe, c.mode = f & 3, f >> 3 & 1, (f >> 4) & 7
            b = ccer >> (4 * (c.n - 1))
            c.e, c.pol = bool(b & 1 and moe), bool(b & 2)
            c.ne, c.npol = bool(self._advanced and b & 4 and moe), bool(b & 8)
            c.irq = bool(dier >> c.n & 1 or dier >> (8 + c.n) & 1)
            c.active = c.ccs == 0 and (c.e or c.ne or c.irq)

    def _oc_cfg_write(self, blk, reg, old, new):
        self._store(reg, new)
        if reg.name.startswith("CCMR") and not self._warned_out:
            for k in range(2):
                if (new >> (8 * k)) & 3:
                    self._warned_out = True
                    self.emit("warn", block=self.name, msg=f"{self.name}: input capture (CCxS != 0 in {reg.name}=0x"
                              f"{new:04x}) is not modelled; the channel does nothing")
                    break
        self._oc_config_changed()
        return None

    def _ccr_write(self, n: int):
        def wr(blk, reg, old, new):
            c = next((x for x in self.ch if x.n == n), None)
            if c is not None and not c.pe:
                if self._quiet and self.running:
                    self._lazy_flags(self._ticks_now())      # matches against the old value up to now
                c.ccr = new & self.cmask                 # no preload: the comparison uses it at once
                self._oc_config_changed()
            return None
        return wr

    def _oc_config_changed(self) -> None:
        """CCMR/CCER/BDTR/DIER/CCR changed: re-evaluate REF of PWM/forced modes now, reschedule matches, drive pins."""
        if self.ch:
            if self._quiet and self.running:
                self._lazy_flags(self._ticks_now())
            self._chan_cfg()
            self._oc_resync(start=False)

    def _ticks_now(self) -> float:
        if not self.running or self._tick_ns <= 0:
            return 0.0
        return (self._now() - self._start_ns) / self._tick_ns

    def _counter_at(self, pos0: int, k: int) -> tuple[int, bool]:
        """Counter value and direction k ticks after the anchor position pos0 (within the current period)."""
        if self.mem[0] >> CMS_SHIFT & 3:
            arr = self.arr
            ph = (pos0 + k) % max(2 * arr, 1)
            return (ph, False) if ph < arr else (2 * arr - ph, True)
        if self.mem[0] & DIR:
            return max(pos0 - k, 0), True
        return pos0 + k, False

    @staticmethod
    def _pwm_ref(mode: int, cnt: int, down: bool, ccr: int) -> bool:
        ref = (cnt <= ccr) if down else (cnt < ccr)      # PWM1
        return ref if mode == 6 else not ref             # PWM2 is the inverse

    def _match_ticks(self, pos0: int, v: int) -> list[int]:
        """Ticks after the anchor at which the counter equals v, up to the next overflow/underflow event (which
        re-anchors), in increasing order."""
        lim = self._ticks_to_update(pos0)
        cr = self.mem[0]
        if cr >> CMS_SHIFT & 3:
            arr = self.arr
            if not 0 <= v <= arr:
                return []
            ks = {v - pos0, 2 * arr - v - pos0}
            return sorted(k for k in ks if 0 <= k <= lim)
        if cr & DIR:
            k = pos0 - v
        else:
            k = v - pos0
        return [k] if 0 <= k <= lim else []

    def _lazy_flags(self, upto: float | None) -> None:
        """Quiet channels: set CCxIF for the matches of the current period up to tick `upto` (None: the whole
        period, at its overflow event) that are not reflected yet."""
        pos0 = self._start_cnt
        sr = self.get("SR")
        new = sr
        for c in self._quiet:
            for k in self._match_ticks(pos0, c.ccr):
                if k > c.lazy_k and (upto is None or k <= upto + 1e-6):
                    new |= 1 << c.n
                    c.lazy_k = k
        if new != sr:
            self.set("SR", new)
            self._update_line()

    def _sr_read(self, blk, reg):
        if self._quiet and self.running:
            self._lazy_flags(self._ticks_now())
        return None if not self._quiet else self.get("SR")

    def _oc_resync(self, start: bool) -> None:
        el = self._ticks_now()
        pos0 = self._start_cnt
        if start:
            kmin = 0
        else:
            kmin = int(el + 1e-6) + (0 if abs(el - round(el)) < 1e-6 else 1)
        cnt, down = self._counter_at(pos0, max(int(el + 1e-6), 0)) if self.running else \
            (self.get("CNT") & self.cmask, self._down())
        quiet = []
        for c in self.ch:
            if c.ev is not None:
                self.chip.sched.cancel(c.ev)
                c.ev = None
            if c.ccs:                                    # input capture: not modelled
                continue
            mode = c.mode
            if mode >= 6:
                c.ref = self._pwm_ref(mode, cnt, down, c.ccr)
            elif mode == 4:
                c.ref = False
            elif mode == 5:
                c.ref = True
            self._oc_apply(c)
            if not self.running:
                continue
            if c.active:
                self._oc_schedule(c, pos0, kmin)
            else:
                quiet.append(c)
                c.lazy_k = kmin - 1 if not start else -1
        self._quiet = quiet

    def _oc_schedule(self, c: "_Chan", pos0: int, kmin: int) -> None:
        for k in self._match_ticks(pos0, c.ccr):
            if k < kmin:
                continue
            if k == 0 and kmin == 0 and not self._cms():
                self._oc_match(c, pos0, 0, reschedule=False)     # restart exactly on CCR (CCR = 0 at the update)
                continue
            c.ev = self.chip.sched.at_ns(self._start_ns + k * self._tick_ns, self._oc_match, c, pos0, k)
            return

    def _oc_match(self, c: "_Chan", pos0: int, k: int, reschedule: bool = True) -> None:
        c.ev = None
        self.set("SR", self.get("SR") | (1 << c.n))       # CCxIF
        self._update_line()
        mode = c.mode
        if mode >= 6:
            cnt, down = self._counter_at(pos0, k)
            c.ref = self._pwm_ref(mode, cnt, down, c.ccr)
        elif mode == 1:
            c.ref = True
        elif mode == 2:
            c.ref = False
        elif mode == 3:
            c.ref = not c.ref
        self._oc_apply(c)
        if reschedule and self.running and pos0 == self._start_cnt:
            self._oc_schedule(c, pos0, k + 1)

    def _oc_apply(self, c: "_Chan") -> None:
        """Drive TIMx_CHn / TIMx_CHnN from OCxREF and the cached enables/polarities."""
        out = (c.ref ^ c.pol) if c.e else None
        outn = (((not c.ref) if c.e else c.ref) ^ c.npol) if c.ne else None
        if out is None:
            if c.out is not None:
                self.chip.pins.release_signal(f"{self.name}_CH{c.n}", "L")
                c.out = None
        else:
            v = "H" if out else "L"
            if c.out != v:
                c.out = v
                self.chip.pins.drive_signal(f"{self.name}_CH{c.n}", v)
        if outn is None:
            if c.outn is not None:
                self.chip.pins.release_signal(f"{self.name}_CH{c.n}N", "L")
                c.outn = None
        else:
            v = "H" if outn else "L"
            if c.outn != v:
                c.outn = v
                self.chip.pins.drive_signal(f"{self.name}_CH{c.n}N", v)
