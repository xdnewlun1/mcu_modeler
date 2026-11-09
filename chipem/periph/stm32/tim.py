"""STM32F4 timers: TIM2-5 (general purpose, TIM2/TIM5 32-bit), TIM6/7 (basic), TIM1/8 (advanced), TIM9-14.

One model for all of them; the features follow from the SVD register set of each timer: channels = the CCRx
registers present (TIM9/12: 2, TIM10/11/13/14: 1), BDTR = advanced (complementary outputs, dead time, break), RCR =
repetition counter, CR1 bits the SVD does not define (DIR/CMS on TIM9-14, OPM on TIM10/11/13/14) read 0 and ignore
writes, the encoder interface needs SMCR and 4 channels (TIM1-5, TIM8).  TIM9-14 count up only, 16 bits.

Registers from the SVD; behaviour (RM0090 ch. 17-20):
* Counter clock = timer kernel clock / (PSC + 1); kernel clock = RCC timer clock of the timer's APB bus (APB2 for
  TIM1/8/9/10/11, APB1 otherwise; x2 when the APB prescaler is not 1), consistent with the actual core clock.  The
  kernel clock derives from HCLK only (core / APB prescaler x 1 or 2), so one counter tick is a whole number of core
  cycles and every timer event is scheduled in the core-cycle domain (`sched.at_cycles`): a core clock change
  (SYSCLK switch, HPRE) leaves the timing untouched; an APB prescaler change re-anchors the counter (RCC listener).
* CR1: CEN starts/stops, DIR down-counting, CMS (center-aligned: 0 -> ARR -> 0, period 2 x ARR, DIR is read-only
  and reads the current direction), OPM (CEN clears at the update event, the outputs follow the stopped counter),
  ARPE (ARR preload), UDIS (no update events: shadows keep their value; UG only re-initialises the counter), URS
  (UG sets no UIF).
* PSC is always preloaded and ARR when ARPE=1: the active values load at each update event (and on EGR.UG).  A
  non-preloaded ARR write takes effect immediately; a counter already above the new ARR runs on to the wrap-around.
  Writes that re-anchor the running counter (ARR without preload, CNT, DIR/CMS) keep the prescaler phase: the counter
  value changes at the same core cycles as before.  EGR.UG resets the prescaler (re-anchors now) and, with the
  counter stopped, re-evaluates OCxREF and the pins from the re-initialised counter.
* Counter overflow/underflow (up: ARR -> 0, down: 0 -> ARR, center-aligned: at ARR and at 0, so twice per period)
  decrements the repetition counter (RCR, TIM1/8); an update event (UEV) happens only when it is 0 and UDIS=0:
  SR.UIF is set and the shadows load.  The IRQ line (TIM2, TIM3, TIM4, TIM5, TIM6_DAC, TIM7, TIM1_UP_TIM10,
  TIM8_UP_TIM13; `params.irq` overrides) is UIF & DIER.UIE, level.  One scheduler event per overflow.
* CNT reads are live (instruction-exact mid-slice); CNT writes restart the count from the written value.
* SR flags are rc_w0 (write 0 to clear).
* RCC clock gate (APBxENR) turned off while the counter runs: the counter and its outputs freeze (no events, the
  pins hold their level, CEN stays set) until the gate is turned on again, which resumes from the frozen counter.
  Register accesses while gated still proceed with one `warn` (project convention), so a timer whose gate was never
  turned on still counts.
* Output compare / PWM (RM0090 17.3.8-17.3.10, 18.3.7-18.3.9), channels 1-4 with CCxS=00: a compare match
  (counter == active CCRx; CCRx preloaded when OCxPE=1, loaded at the update event) sets CCxIF (IRQ with CCxIE: the
  timer's IRQ, or TIMx_CC on TIM1/8) and moves OCxREF per OCxM: frozen, active/inactive/toggle on match, force
  inactive/active, PWM1 (up: REF = CNT < CCR; down: REF = CNT <= CCR), PWM2 (inverse); center-aligned counting
  uses the rule of the current direction, and CCxIF follows CR1.CMS: 01 sets it only on matches while counting
  down, 10 only while counting up, 11 on both (OCxREF changes on both).  CCR > ARR (edge-aligned) never matches: PWM
  is 100 % / 0 % without CCxIF; CCR = 0 matches at CNT = 0 (CCxIF set, PWM1 0 %).  A match happens when the counter
  takes the value of CCRx: a restart or a CCRx write while the counter already equals CCRx does not match again
  (toggle mode toggles once per period, CCxIF is set once).  Each edge is a scheduler event at its exact counter
  tick, and pins muxed to TIMx_CHy/TIMx_CHyN follow it (PinBoard.drive_signal).
* Output enables (RM0090 table 81 on TIM1/8): CCxE drives TIMx_CHn = OCxREF ^ CCxP; general-purpose timers release
  the signal when CCxE=0.  TIM1/8: with MOE=1, CCxNE drives TIMx_CHnN = ~OCxREF ^ CCxNP (OCxREF ^ CCxNP with
  CCxE=0); with OSSR=1 an output whose enable is 0 while the other one is set is driven at its inactive level
  (CCxP / CCxNP).  With MOE=0 and OSSI=1 an enabled channel drives its idle levels CR2.OISx / OISxN; with OSSI=0
  (or both enables 0) the outputs are released.  A released signal is not driven: the STM32 GPIO model asks
  `signal_driven()` and shows a pin muxed to it as an input (level from PUPDR, an external drive or the net).
* IRQ vectors: TIM1/8 have UP (UIF), CC (CCxIF), BRK (BIF) and TRG_COM (TIF/COMIF); the other timers one vector for
  all flags.  Vectors shared between timers (TIM1_UP_TIM10, TIM1_BRK_TIM9, TIM8_UP_TIM13, ...) are the OR of the
  timers' levels.
* Input capture (CCxS != 0, RM0090 17.3.5/18.3.5): the channel releases TIMx_CHn / TIMx_CHnN (an input).  CCxS=01
  captures TIx, 10 the other input of the pair (CH1<->CH2, CH3<->CH4; reserved on 1-channel timers), 11 (TRC) is not
  modelled (one `warn`: there are no internal trigger inputs).  CCxNP:CCxP select the edge (00 rising, 01 falling,
  11 both; the reserved 10 acts as rising), ICxPSC captures every 1/2/4/8 active edges (the prescaler restarts when
  CCxE, CCxS or ICxPSC change).  With CCxE=1 an active edge latches CCRx = CNT at the edge (chip.now_cycles(),
  instruction-exact, no resynchronisation delay) and sets CCxIF, or CCxOF when CCxIF is still set; CCxIE raises the
  IRQ.  Reading CCRx clears CCxIF (not CCxOF, RM0090 18.4.5); CCRx is read-only.  EGR.CCxG on an input channel
  captures CNT too.  Edges come from the pin muxed to TIMx_CHn (PinBoard input listeners, registered only while an
  input is in use): `--at`, board nets, parts or the timer's own outputs; H is 1, L and floating X are 0 (as IDR).  A
  pin that is not muxed to the timer's signal is not seen.  ICxF (digital filter) and CR2.TI1S (XOR of CH1-3) are
  not modelled (the filter is a no-op).  With the clock gated off (frozen) edges are ignored.
* Slave mode (SMCR): reset mode (SMS=100) with TS = TI1F_ED (both edges of TI1), TI1FP1 or TI2FP2 (edge from CC1P/
  CC1NP or CC2P/CC2NP): captures on that edge first, then counter + prescaler re-initialise like UG and TIF is set,
  so the RM0090 PWM input mode (IC1 = TI1 rising, IC2 = TI1 falling, reset on TI1FP1) gives CCR1 = period and CCR2 =
  high time.  Encoder interface (SMS=001/010/011, TIM1-5/8, RM0090 table 95): the internal clock stops driving the
  counter; each TI1FP1 (001), TI2FP2 (010) or either (011) edge counts up or down by the level of the other input
  (TIxFPx = TIx inverted with CCxP), between 0 and ARR, DIR reflects the direction (read-only), wrapping is an
  overflow (repetition counter, update event, UIF).  PWM/forced OCxREF follow the count, compare matches set no
  CCxIF in encoder mode.  Gated (101), trigger (110) and external clock (111, SMCR.ECE) modes, ITRx/ETRF triggers,
  MSM: not modelled (one `warn`).  EGR.TG sets TIF.
* Dead time (TIM1/8, RM0090 17.3.11): with MOE=1 and both CCxE and CCxNE set, OCx follows OCxREF and OCxN its
  inverse, each rising edge delayed by BDTR.DTG (0xxxxxxx: DTG x tDTS, 10xxxxxx: (64 + DTG[5:0]) x 2 tDTS, 110xxxxx:
  (32 + DTG[4:0]) x 8 tDTS, 111xxxxx: (32 + DTG[4:0]) x 16 tDTS; tDTS = CK_INT x 1/2/4 by CR1.CKD), then the
  polarity CCxP/CCxNP applies.  A pulse shorter than the dead time is not generated.  The delayed edges are scheduler
  events (cancelled on reset and when the pair leaves the dead-time configuration).  With only one output enabled
  (or OSSR off-state on the other) there is no dead time (RM0090 table 81).
* Break (TIM1/8): BKE=1 and the pin muxed to TIMx_BKIN at the BKP level (BKP=0: low; floating reads low; no pin muxed:
  inactive), or EGR.BG: MOE is cleared, the outputs take the off-state OSSI selects (CR2.OISx/OISxN idle levels with
  OSSI=1, released with OSSI=0; immediately, without the dead time), BIF is set (BIE: TIMx_BRK vector).  While the
  break input stays active MOE cannot be set and BIF cannot be cleared.  AOE=1 sets MOE again at the next update
  event (counter, UG, slave reset) once the break input is inactive.  BDTR.LOCK is stored only (no write
  protection).
* OCxFE (fast enable) is a no-op: it only shortens the trigger-input-to-output delay, and there are no trigger inputs
  in the model.  OCxCE (OCxREF clear on ETRF), ETR, CR2 CCPC/COM preload, MMS/TRGO (no timer chaining), one-pulse
  trigger start and DMA requests are not modelled.
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
BRK_IRQ = {"TIM1": "TIM1_BRK_TIM9", "TIM8": "TIM8_BRK_TIM12"}
TRG_IRQ = {"TIM1": "TIM1_TRG_COM_TIM11", "TIM8": "TIM8_TRG_COM_TIM14"}
WIDE = ("TIM2", "TIM5")
OCPE = 1 << 3                    # CCMRx output preload enable (per channel byte)
MOE, AOE, BKP, BKE, OSSR, OSSI = 1 << 15, 1 << 14, 1 << 13, 1 << 12, 1 << 11, 1 << 10      # BDTR (TIM1/8)
TIF, BIF = 1 << 6, 1 << 7        # SR (DIER: TIE, BIE; EGR: TG, BG)
CKD_DIV = (1, 2, 4, 4)           # CR1.CKD -> tDTS / tCK_INT (11 is reserved: treated as /4)
# output source codes (_Chan.o / _Chan.on): None released, 0/1 fixed level, REF follows OCxREF, NREF its inverse
REF, NREF = 2, 3
_PARTNER = {1: 2, 2: 1, 3: 4, 4: 3}      # CCxS=10: the input of the other channel of the pair


class _Chan:
    __slots__ = ("n", "ccr", "ref", "ev", "out", "outn", "mode", "ccs", "pe", "o", "on", "irq", "active", "lazy_k",
                 "dt", "dt_ref", "dt_ev", "dp", "dn", "ic_src", "ic_edges", "ic_div", "ic_cnt", "ic_key")

    def __init__(self, n: int):
        self.n = n
        self.ccr = 0             # active compare value (the preload register is CCRn)
        self.ref = False         # OCxREF
        self.ev = None           # next compare-match event
        self.out: str | None = None      # level driven on TIMx_CHn (None: not driving)
        self.outn: str | None = None     # level driven on TIMx_CHnN
        # configuration cached from CCMRx / CCER / BDTR / CR2 / DIER (refreshed on their writes)
        self.mode = self.ccs = self.pe = 0
        self.o: int | None = None        # TIMx_CHn source: None, 0, 1, REF, NREF
        self.on: int | None = None       # TIMx_CHnN source
        self.irq = False         # CCxIE or CCxDE: matches must happen at their exact time
        self.active = False      # needs match events (an output following REF, a REF-changing mode or an IRQ/DMA)
        self.lazy_k = -1         # quiet channel: last match tick (since the anchor) already reflected in CCxIF
        # dead time (TIM1/8 complementary pair with both outputs enabled): delay in core cycles (0: off), OCxREF the
        # delayed signals follow, pending delayed rising edge, delayed OCx (dp) and OCxN (dn) before polarity
        self.dt = 0
        self.dt_ref = False
        self.dt_ev = None
        self.dp = False
        self.dn = True
        # input capture: source TI (1-4, 0 none/disabled), edge mask (1 rising, 2 falling), prescaler (capture every
        # ic_div edges), edges counted so far, configuration key (a change resets the prescaler)
        self.ic_src = 0
        self.ic_edges = 1
        self.ic_div = 1
        self.ic_cnt = 0
        self.ic_key: tuple | None = None


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
            "CR2": {"on_write": self._oc_cfg_write},
            "PSC": {"on_write": self._psc_write},
            "ARR": {"on_write": self._arr_write},
            "CNT": {"on_read": self._cnt_read, "on_write": self._cnt_write},
            "EGR": {"on_write": self._egr_write, "on_read": lambda b, r: 0},
            "DIER": {"on_write": self._dier_write},
            "SR": {"on_write": self._sr_write, "on_read": self._sr_read},
            "CCER": {"on_write": self._ccer_write},
            "BDTR": {"on_write": self._bdtr_write},
            "SMCR": {"on_write": self._smcr_write},
            "CCMR1_Output": {"on_write": self._oc_cfg_write}, "CCMR1_Input": {"on_write": self._oc_cfg_write},
            "CCMR2_Output": {"on_write": self._oc_cfg_write}, "CCMR2_Input": {"on_write": self._oc_cfg_write},
        }
        for n in range(1, 5):
            ov[f"CCR{n}"] = {"on_write": self._ccr_write(n), "on_read": self._ccr_read(n)}
        r = build_regs((name, "TIM2" if name in WIDE else "TIM3"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self._ev = None
        self._irqn: int | None = None
        self._line = False
        self.running = False
        self.psc = 0                 # active (shadow) prescaler
        self.arr = 0                 # active (shadow) auto-reload
        self.rep = 0                 # repetition down-counter
        self._start_cyc = 0          # core cycle of the anchor (a counter tick boundary)
        self._start_cnt = 0          # counter value at the anchor; center-aligned: phase in [0, 2*ARR) (>= ARR: down)
        self._tick_cyc = 1           # counter clock period in core cycles (whole: kernel clock = HCLK x 1, /2, /4...)
        self.updates = 0
        self.ch = [_Chan(n) for n in range(1, 5) if f"CCR{n}" in self.regs]
        self._ccmr = [next((x for x in (f"CCMR{k}_Output", f"CCMR{k}_Input") if x in self.regs), None) for k in (1, 2)]
        self._advanced = "BDTR" in self.regs
        self._cc_irqn: int | None = None
        self._cc_line = False
        self._quiet: list[_Chan] = []       # output channels without output/IRQ/DMA: CCxIF computed when needed
        self._gate_hooked = False
        self._frozen = False             # clock gated off while counting: held until the gate is on again
        self._gpios: list | None = None  # STM32 GPIO ports (re-synced when a channel starts/stops driving)
        # inputs (input capture, slave mode, encoder, break)
        self._brk_irqn: int | None = None
        self._trg_irqn: int | None = None
        self._lines: dict[int, bool] = {}            # IRQ number -> level this timer drives on it
        self._sms = 0                                # SMCR.SMS / TS (cached)
        self._ts = 0
        self._enc_ok = "SMCR" in self.regs and "CCR3" in self.regs     # encoder interface: TIM1-5, TIM8
        self._watch: dict[Any, tuple] = {}           # watched input -> (signal, callback, pins)
        self._ti_lvl: dict[Any, bool] = {}           # last level of each watched input (1-4 = TIx, "B" = BKIN)
        self._dt_cyc = 0                             # dead time (BDTR.DTG, CR1.CKD) in core cycles
        self._warned: set[str] = set()

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, str(self.params.get("irq") or IRQ_NAMES.get(self.name, self.name)))
        self._cc_irqn = irq_number(chip, f"{self.name}_CC")        # TIM1/8: capture/compare has its own vector
        if self._cc_irqn is not None:                               # TIM1/8: break and trigger/commutation vectors
            self._brk_irqn = irq_number(chip, BRK_IRQ.get(self.name, f"{self.name}_BRK"))
            self._trg_irqn = irq_number(chip, TRG_IRQ.get(self.name, f"{self.name}_TRG_COM"))
        r = rcc(chip)
        if r is not None:
            r.listeners.append(self._clock_changed)
        self._watch_gate()

    def reset(self) -> None:
        self._stop()
        self._frozen = False
        if self.chip is not None:
            for n in list(getattr(self, "_lines", {})):
                self._set_line(n, False)
        self._line = self._cc_line = False
        for k in list(getattr(self, "_watch", {})):
            self._unwatch(k)
        if hasattr(self, "_ti_lvl"):
            self._ti_lvl.clear()
            self._sms = self._ts = 0
        pins = getattr(self.chip, "pins", None)
        for c in getattr(self, "ch", ()):
            if c.dt_ev is not None and self.chip is not None:
                self.chip.sched.cancel(c.dt_ev)
            c.dt_ev = None
            c.dt, c.dt_ref, c.dp, c.dn = 0, False, False, True
            c.ic_src, c.ic_cnt, c.ic_key = 0, 0, None
            c.ref = False
            c.ccr = 0
            for sig in (f"{self.name}_CH{c.n}", f"{self.name}_CH{c.n}N"):
                if pins is not None:
                    pins.release_signal(sig)
            c.out = c.outn = None
        super().reset()
        self.psc = self.get("PSC")
        self.arr = self.get("ARR") & self.cmask
        self.rep = 0
        self._start_cnt = 0
        self._chan_cfg()
        if pins is not None:
            for c in self.ch:
                self._repin(f"{self.name}_CH{c.n}")
                self._repin(f"{self.name}_CH{c.n}N")

    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()
        MMIOBlock.write(self, offset, size, value)

    # ------------------------------------------------------------ clock gate
    def _clk_on(self) -> bool:
        loc = self._gate_loc
        if loc is None or self._rcc is None:
            return True
        return bool(self._rcc.mem[loc[0]] & loc[1])

    def _watch_gate(self) -> None:
        """Get told about writes to our RCC clock-enable bit (rcc.gate_listeners)."""
        r = self._rcc
        if self._gate_hooked or r is None or self._gate_loc is None:
            return
        from .rcc import GATES
        gl = getattr(r, "gate_listeners", None)
        if gl is not None and self.gate in GATES:
            self._gate_enr, self._gate_bit = GATES[self.gate]
            gl.append(self._gate_write)
            self._gate_hooked = True

    def _gate_write(self, enr: str, old: int, new: int) -> None:
        if enr == self._gate_enr and (old ^ new) >> self._gate_bit & 1:
            self._gate_changed()

    def _gate_changed(self) -> None:
        on = self._clk_on()
        if not on and self.running:
            self._freeze()
        elif on and self._frozen:
            self._frozen = False
            if self.get("CR1") & CEN and not self.running:
                c = self.get("CNT") & self.cmask
                self._start(self._phase(c, self._down()) if self._cms() else c)

    def _freeze(self) -> None:
        """Stop counting (clock gated off) with CEN still set: CNT/DIR hold the counter, the outputs their level."""
        c, down = self._cnt_dir()
        self.set("CNT", c)
        if self._cms():
            self.set("CR1", (self.get("CR1") | DIR) if down else (self.get("CR1") & ~DIR))
        self._stop()
        self._frozen = True

    # ------------------------------------------------------------ derived
    def kernel_hz(self) -> float:
        return timer_hz(self.chip, self.apb) if self.chip is not None else 0.0

    def counter_hz(self) -> float:
        return self.kernel_hz() / (self.psc + 1)

    def _calc_tick(self) -> int:
        """Core cycles per counter tick (0: no clock)."""
        kh = self.kernel_hz()
        if kh <= 0 or self.chip is None:
            return 0
        return max(1, round(self.chip.clock.hz / kh * (self.psc + 1)))

    def _cms(self) -> int:
        return (self.mem[0] >> CMS_SHIFT) & 3        # CR1 is at offset 0; CMS and DIR are in its low byte

    def _down(self) -> bool:
        return bool(self.mem[0] & DIR)

    def period_ns(self) -> float:
        """Time between update events (before the repetition counter)."""
        n = 2 * self.arr if self._cms() else self.arr + 1
        hz = self.counter_hz()
        return max(n, 1) * 1e9 / hz if hz > 0 else 0.0

    def _now(self) -> int:
        return self.chip.now_cycles()

    def _elapsed(self) -> tuple[int, bool]:
        """(whole counter ticks since the anchor, now exactly on a tick boundary)."""
        q, r = divmod(self._now() - self._start_cyc, self._tick_cyc)
        return q, r == 0

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

    def _start(self, cnt: int, t_cyc: int | None = None, period: bool = False) -> None:
        """(Re)start counting from `cnt` (center-aligned: phase) at core cycle `t_cyc` (default now).  `period`: the
        anchor is a fresh counter value (update event, UG), so a compare match at the anchor tick happens; otherwise
        the counter already had this value and its match (if any) is not repeated."""
        self._stop()
        if not self.get("CR1") & CEN or self.chip is None or self._frozen or self._encoder():
            return
        tick = self._calc_tick()
        if tick <= 0:
            return
        self.running = True
        self._tick_cyc = tick
        self._start_cyc = self._now() if t_cyc is None else int(t_cyc)
        self._start_cnt = cnt
        self._ev = self.chip.sched.at_cycles(self._start_cyc + self._ticks_to_update(cnt) * tick, self._update_event)
        if self.ch:
            self._oc_resync(kmin=0 if period else 1)

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
        return self._counter_at(self._start_cnt, self._elapsed()[0])

    def cnt(self) -> int:
        return self._cnt_dir()[0]

    def _reanchor(self, cnt: int | None = None) -> None:
        """Restart the running counter at the last tick boundary (prescaler phase kept) from its current value, or
        from `cnt` (CNT write), in the current mode."""
        el, _exact = self._elapsed()
        t = self._start_cyc + el * self._tick_cyc
        c, down = self._cnt_dir()
        if cnt is not None:
            c = cnt
        self._start(self._phase(c, down) if self._cms() else c, t)

    def _update_event(self) -> None:
        """Counter overflow/underflow; an update event (UEV) only when the repetition counter is 0 and UDIS=0."""
        self._ev = None
        if self._quiet:
            self._lazy_flags(None)          # quiet channels: CCxIF of the matches in the period that ends here
        ticks = self._ticks_to_update(self._start_cnt)
        t = self._start_cyc + ticks * self._tick_cyc
        cms = self._cms()
        if cms:                         # where the counter is at this overflow (ARR, going down) or underflow (0)
            at_top = (self._start_cnt + ticks) % max(2 * self.arr, 1) == self.arr
            cnt, down = (self.arr, True) if at_top else (0, False)
        uev = self._overflow()
        if cms:
            restart = self._phase(cnt, down)
        else:
            restart = self.arr if self._down() else 0
        if uev and self.get("CR1") & OPM:
            self.set("CR1", self.get("CR1") & ~CEN)
            self.set("CNT", cnt if cms else restart)
            if cms:
                self.set("CR1", (self.get("CR1") | DIR) if down else (self.get("CR1") & ~DIR))
            self._stop()
            if self.ch:
                self._oc_resync(kmin=0)        # OCxREF of PWM modes follows the stopped counter (ends the pulse)
            return
        self._start(restart, t, period=True)

    def _overflow(self) -> bool:
        """Counter overflow/underflow: decrement the repetition counter, or (at 0, UDIS=0) an update event: shadows
        load, UIF, AOE.  True if an update event happened."""
        if self.rep > 0:
            self.rep -= 1
            return False
        if self.get("CR1") & UDIS:
            return False
        self._load_shadows()
        self.rep = self.get("RCR") & 0xFF if "RCR" in self.regs else 0
        self.updates += 1
        self.set("SR", self.get("SR") | UIF)
        self._update_line()
        if self._advanced:
            self._aoe()
        return True

    def _load_shadows(self) -> None:
        self.psc = self.get("PSC") & 0xFFFF
        if self.get("CR1") & ARPE:
            self.arr = self.get("ARR") & self.cmask
        for c in self.ch:
            if c.pe and not c.ccs:
                c.ccr = self.get(f"CCR{c.n}") & self.cmask

    def _clock_changed(self) -> None:
        if self.running and self._calc_tick() != self._tick_cyc:
            self._start(self._restart_pos())             # APB prescaler changed: new tick length from now
        if self._advanced and self.chip is not None:
            self._chan_cfg()                             # dead time in core cycles

    def _restart_pos(self) -> int:
        """Start position (counter value, or phase when center-aligned) from the current CNT/direction."""
        c, down = self._cnt_dir()
        return self._phase(c, down) if self._cms() else c

    # ------------------------------------------------------------ IRQ
    def _update_line(self) -> None:
        act = self.get("SR") & self.get("DIER")
        if self._cc_irqn is None:                        # one vector: update, CCx, trigger
            up = bool(act & 0xFF)
        else:                                            # TIM1/8: UP, CC, BRK, TRG_COM vectors
            up = bool(act & UIF)
            cc = bool(act & 0x1E)                        # CC1IF..CC4IF & CC1IE..CC4IE
            if cc != self._cc_line:
                self._cc_line = cc
                self._set_line(self._cc_irqn, cc)
            if self._brk_irqn is not None:
                self._set_line(self._brk_irqn, bool(act & BIF))
            if self._trg_irqn is not None:
                self._set_line(self._trg_irqn, bool(act & 0x60))     # TIF, COMIF
        if up != self._line:
            self._line = up
            self._set_line(self._irqn, up)

    def _set_line(self, n: int | None, on: bool) -> None:
        """Drive IRQ line n for this timer.  Several timers share vectors (TIM1_UP_TIM10, TIM1_BRK_TIM9, ...): the
        line is the OR of the timers driving it."""
        if n is None or self.chip is None or self._lines.get(n, False) == on:
            return
        self._lines[n] = on
        shared = getattr(self.chip, "_stm32_tim_lines", None)
        if shared is None:
            shared = {}
            try:
                self.chip._stm32_tim_lines = shared
            except AttributeError:
                pass
        who = shared.setdefault(n, set())
        if on:
            who.add(self.name)
        else:
            who.discard(self.name)
        self.irq(n, bool(who))

    # ------------------------------------------------------------ register hooks
    def _cr1_read(self, blk, reg):
        v = int.from_bytes(self.mem[reg.boff:reg.boff + reg.bw], "little")
        if self.running and (v >> CMS_SHIFT) & 3:          # center-aligned: DIR is read-only status
            v = (v | DIR) if self._cnt_dir()[1] else (v & ~DIR)
        return v

    def _cr1_write(self, blk, reg, old, new):
        if ((old >> CMS_SHIFT) & 3 and (new >> CMS_SHIFT) & 3) or self._encoder():
            new = (new & ~DIR) | (old & DIR)              # DIR is read-only while center-aligned / in encoder mode
        if self._advanced and (old ^ new) & 0x300:        # CKD: dead-time clock
            self._store(reg, new)
            self._chan_cfg()
        if self.running and (old ^ new) & (CEN | DIR | (3 << CMS_SHIFT)):
            self._store(reg, old)                         # counter and direction in the old mode
            c, down = self._cnt_dir()
            el, _exact = self._elapsed()
            t = self._start_cyc + el * self._tick_cyc     # the counter took value c at this tick boundary
        else:
            c, down = self.get("CNT") & self.cmask, bool(old & DIR)
            t = None
        self._store(reg, new)
        if (old ^ new) & CEN:
            if new & CEN:
                if (new >> CMS_SHIFT) & 3:
                    self._start(self._phase(c, bool(new & DIR)))
                else:
                    self._start(c)
                if self._encoder():
                    self.emit("info", block=self.name, msg=f"{self.name} started in encoder mode (SMS={self._sms})")
                    return new
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
            self._start(self._phase(c, down) if (new >> CMS_SHIFT) & 3 else c, t)
        return new

    def _psc_write(self, blk, reg, old, new):
        return None                      # preloaded: active at the next update event

    def _arr_write(self, blk, reg, old, new):
        if not self.get("CR1") & ARPE:
            if self.running:
                el, _exact = self._elapsed()
                t = self._start_cyc + el * self._tick_cyc
                c, down = self._cnt_dir()                  # where the counter is under the old ARR
                self.arr = new & self.cmask
                self._start(self._phase(c, down) if self._cms() else c, t)
            else:
                self.arr = new & self.cmask
        return None

    def _cnt_read(self, blk, reg):
        return self.cnt()

    def _cnt_write(self, blk, reg, old, new):
        if self.running:
            self._reanchor(new & self.cmask)
        return None

    def _egr_write(self, blk, reg, old, new):
        if new & UG:
            self._reinit()
        if new & 0x1E:                         # CCxG: software compare/capture event -> CCxIF
            sr = self.get("SR")
            for c in self.ch:
                if new >> c.n & 1 and c.ccs:           # input channel: CNT captured into CCRx (CCxOF if pending)
                    self.set(f"CCR{c.n}", self.cnt())
                    if sr >> c.n & 1:
                        sr |= 1 << (8 + c.n)
            self.set("SR", sr | (new & 0x1E))
            self._update_line()
        if new & TIF:                          # TG: trigger event -> TIF
            self.set("SR", self.get("SR") | TIF)
            self._update_line()
        if new & BIF and self._advanced:       # BG: software break
            self._break()
        return 0

    def _reinit(self) -> None:
        """Update generation (EGR.UG, slave reset mode): re-initialise the counter and the prescaler; an update
        event (shadows, repetition counter; UIF unless URS) unless UDIS."""
        cr1 = self.get("CR1")
        if not cr1 & UDIS:                     # UDIS: the counter re-initialises, the shadows keep their value
            self.psc = self.get("PSC") & 0xFFFF
            self.arr = self.get("ARR") & self.cmask
            for c in self.ch:
                if not c.ccs:
                    c.ccr = self.get(f"CCR{c.n}") & self.cmask
            self.rep = self.get("RCR") & 0xFF if "RCR" in self.regs else 0
            if not cr1 & URS:
                self.set("SR", self.get("SR") | UIF)
                self._update_line()
            if self._advanced:
                self._aoe()
        start = self.arr if (self._down() and not self._cms()) else 0
        self.set("CNT", start)
        if self._cms():
            self.set("CR1", self.get("CR1") & ~DIR)   # restarts counting up from 0
        if self.running:
            self._start(start, period=True)            # prescaler and counter re-initialised now
        elif self.ch:
            self._oc_resync(kmin=0)                   # stopped counter: OCxREF from the new CNT/CCRx

    def _dier_write(self, blk, reg, old, new):
        self._update_line()
        if (old ^ new) & 0x1E1E and self.ch:          # CCxIE / CCxDE changed: matches may need exact events now
            self._store(reg, new)
            self._oc_config_changed()
        return None

    def _sr_write(self, blk, reg, old, new):
        v = old & new                     # rc_w0
        if old & BIF and self._advanced and self._brk_active():
            v |= BIF                      # BIF cannot be cleared while the break input is active
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
        """Refresh the cached channel configuration from CCMRx, CCER, BDTR, CR2 and DIER (RM0090 table 81)."""
        if not self.ch:                                  # basic timers (TIM6/7) have no channels
            return
        ccer = self.get("CCER")
        dier = self.get("DIER")
        adv = self._advanced
        bdtr = self.get("BDTR") if adv else MOE
        cr2 = self.get("CR2") if adv and "CR2" in self.regs else 0
        moe, ossr, ossi = bdtr & MOE, bdtr & OSSR, bdtr & OSSI
        dtc = self._dt_cyc = self._calc_dt(bdtr) if adv else 0
        nch = len(self.ch)
        for c in self.ch:
            f = self._ccmr_field(c.n)
            c.ccs, c.pe, c.mode = f & 3, f >> 3 & 1, (f >> 4) & 7
            b = ccer >> (4 * (c.n - 1))
            e, pol = bool(b & 1), bool(b & 2)
            ne, npol = bool(adv and c.n <= 3 and b & 4), bool(b & 8)
            c.irq = bool(dier >> c.n & 1 or dier >> (8 + c.n) & 1)
            # input capture: CCxS 01 = TIx, 10 = the pair partner's input, 11 = TRC (no slave inputs: not modelled)
            src = 0
            if c.ccs == 1:
                src = c.n
            elif c.ccs == 2 and _PARTNER[c.n] <= nch:
                src = _PARTNER[c.n]
            if c.ccs == 3:
                self._warn_once("trc", f"{self.name}: CC{c.n}S=11 (IC{c.n} on TRC) is not modelled (no internal "
                                       "trigger inputs): the channel captures nothing")
            if not e:
                src = 0
            key = (src, f >> 2 & 3)
            if key != c.ic_key:                          # new source / prescaler or CCxE=0: prescaler restarts
                c.ic_key, c.ic_cnt = key, 0
            c.ic_src, c.ic_div = src, 1 << (f >> 2 & 3)
            c.ic_edges = self._edges_of(c.n, ccer)
            dt = dtc if (moe and e and ne and not c.ccs) else 0
            if dt != c.dt:
                if not dt or not c.dt:                   # dead time on / off: the delayed signals settle at once
                    if c.dt_ev is not None:
                        self.chip.sched.cancel(c.dt_ev)
                        c.dt_ev = None
                    c.dt_ref, c.dp, c.dn = c.ref, c.ref, not c.ref
                c.dt = dt
            if c.ccs:                                    # input capture: the channel drives nothing
                o = on = None
            elif not adv:
                o, on = ((NREF if pol else REF) if e else None), None
            elif moe:
                o = (NREF if pol else REF) if e else (int(pol) if ossr and ne else None)
                if ne:
                    on = ((REF if npol else NREF) if e else (NREF if npol else REF))
                else:
                    on = int(npol) if ossr and e else None
            elif ossi and (e or ne):                     # MOE=0, OSSI=1: idle levels
                o = cr2 >> (8 + 2 * (c.n - 1)) & 1
                on = (cr2 >> (9 + 2 * (c.n - 1)) & 1) if c.n <= 3 else None
            else:
                o = on = None
            c.o, c.on = o, on
            follows = (o is not None and o >= REF) or (on is not None and on >= REF)
            c.active = c.ccs == 0 and (follows or c.irq or c.mode in (1, 2, 3))
        self._inputs_cfg()

    @staticmethod
    def _edges_of(n: int, ccer: int) -> int:
        """Active edges of channel n's input (CCxNP:CCxP 00 rising, 01 falling, 11 both; 10 is reserved: rising)."""
        b = ccer >> (4 * (n - 1))
        return {0: 1, 2: 2, 8: 1, 10: 3}[b & 0xA]

    def _calc_dt(self, bdtr: int) -> int:
        """BDTR.DTG dead time in core cycles (RM0090 17.4.18): DTG[7:5] = 0xx: DTG x tDTS, 10x: (64 + DTG[5:0]) x
        2 tDTS, 110: (32 + DTG[4:0]) x 8 tDTS, 111: (32 + DTG[4:0]) x 16 tDTS; tDTS = CK_INT x 1/2/4 (CR1.CKD)."""
        g = bdtr & 0xFF
        if not g or self.chip is None:
            return 0
        if g < 0x80:
            n = g
        elif g < 0xC0:
            n = (64 + (g & 0x3F)) * 2
        elif g < 0xE0:
            n = (32 + (g & 0x1F)) * 8
        else:
            n = (32 + (g & 0x1F)) * 16
        kh = self.kernel_hz()
        if kh <= 0:
            return 0
        kc = max(1, round(self.chip.clock.hz / kh))        # core cycles per CK_INT cycle (whole, see _calc_tick)
        return n * CKD_DIV[self.get("CR1") >> 8 & 3] * kc

    def _oc_cfg_write(self, blk, reg, old, new):
        self._store(reg, new)
        self._oc_config_changed()
        return None

    def _ccr_read(self, n: int):
        def rd(blk, reg):
            c = self.ch[n - 1] if n <= len(self.ch) else None
            if c is not None and c.ccs:                  # input: reading CCRx clears CCxIF (RM0090 18.4.5), not CCxOF
                sr = self.get("SR")
                if sr >> n & 1:
                    self.set("SR", sr & ~(1 << n))
                    self._update_line()
            return None
        return rd

    def _ccr_write(self, n: int):
        def wr(blk, reg, old, new):
            c = next((x for x in self.ch if x.n == n), None)
            if c is not None and c.ccs:
                return old                               # input capture: CCRx is read-only
            if c is not None and not c.pe:
                if self._quiet and self.running:
                    self._lazy_flags(self._ticks_now())      # matches against the old value up to now
                c.ccr = new & self.cmask                 # no preload: the comparison uses it at once
                self._oc_config_changed()
            return None
        return wr

    def _oc_config_changed(self) -> None:
        """CCMR/CCER/BDTR/CR2/DIER/CCR changed: re-evaluate REF of PWM/forced modes now, reschedule matches, drive
        pins.  The counter already has its current value: a match at the current tick is not (re)generated."""
        if self.ch:
            if self._quiet and self.running:
                self._lazy_flags(self._ticks_now())
            self._chan_cfg()
            self._oc_resync(kmin=self._elapsed()[0] + 1 if self.running else 0)

    def _ticks_now(self) -> float:
        if not self.running or self._tick_cyc <= 0:
            return 0.0
        return (self._now() - self._start_cyc) / self._tick_cyc

    def _counter_at(self, pos0: int, k: int) -> tuple[int, bool]:
        """Counter value and direction k ticks after the anchor position pos0 (within the current period)."""
        if self.mem[0] >> CMS_SHIFT & 3:
            arr = self.arr
            ph = (pos0 + k) % max(2 * arr, 1)
            return (ph, False) if ph < arr else (2 * arr - ph, True)
        if self.mem[0] & DIR:
            return max(pos0 - k, 0), True
        v = pos0 + k
        if v > self.cmask:                               # above ARR: ran on through the wrap-around
            v -= self.cmask + 1
        return v, False

    @staticmethod
    def _pwm_ref(mode: int, cnt: int, down: bool, ccr: int) -> bool:
        ref = (cnt <= ccr) if down else (cnt < ccr)      # PWM1
        return ref if mode == 6 else not ref             # PWM2 is the inverse

    def _match_ticks(self, pos0: int, v: int) -> list[int]:
        """Ticks after the anchor at which the counter equals v, before the next overflow/underflow event (which
        re-anchors, its tick being k=0 of the next anchor), in increasing order."""
        lim = self._ticks_to_update(pos0)
        cr = self.mem[0]
        if cr >> CMS_SHIFT & 3:
            arr = self.arr
            if not 0 <= v <= arr:
                return []
            ks = {v - pos0, 2 * arr - v - pos0}
            return sorted(k for k in ks if 0 <= k < lim)
        if cr & DIR:
            k = pos0 - v
        elif pos0 > self.arr and v < pos0:               # running on above ARR: v is reached after the wrap
            k = (self.cmask - pos0 + 1) + v if v <= self.arr else -1
        else:
            k = v - pos0
        return [k] if 0 <= k < lim else []

    def _flag_dir_ok(self, pos0: int, k: int) -> bool:
        """CR1.CMS: compare flags only while counting down (01), up (10) or both (11, edge-aligned)."""
        cms = self.mem[0] >> CMS_SHIFT & 3
        if cms == 0 or cms == 3:
            return True
        return self._counter_at(pos0, k)[1] == (cms == 1)

    def _lazy_flags(self, upto: float | None) -> None:
        """Quiet channels: set CCxIF for the matches of the current period up to tick `upto` (None: the whole
        period, at its overflow event) that are not reflected yet."""
        pos0 = self._start_cnt
        sr = self.get("SR")
        new = sr
        for c in self._quiet:
            for k in self._match_ticks(pos0, c.ccr):
                if k > c.lazy_k and (upto is None or k <= upto + 1e-9):
                    if self._flag_dir_ok(pos0, k):
                        new |= 1 << c.n
                    c.lazy_k = k
        if new != sr:
            self.set("SR", new)
            self._update_line()

    def _sr_read(self, blk, reg):
        if self._quiet and self.running:
            self._lazy_flags(self._ticks_now())
        return None if not self._quiet else self.get("SR")

    def _oc_resync(self, kmin: int) -> None:
        """Re-evaluate OCxREF of PWM/forced modes from the counter now, drive the pins and (when running) schedule
        the matches from tick `kmin` after the anchor (earlier ones already happened)."""
        pos0 = self._start_cnt
        if self.running:
            cnt, down = self._counter_at(pos0, self._elapsed()[0])
        else:
            cnt, down = self.get("CNT") & self.cmask, self._down()
        quiet = []
        for c in self.ch:
            if c.ev is not None:
                self.chip.sched.cancel(c.ev)
                c.ev = None
            if not c.ccs:
                mode = c.mode
                if mode >= 6:
                    c.ref = self._pwm_ref(mode, cnt, down, c.ccr)
                elif mode == 4:
                    c.ref = False
                elif mode == 5:
                    c.ref = True
            self._oc_apply(c)                            # input capture: releases the outputs
            if not self.running or c.ccs:
                continue
            if c.active:
                self._oc_schedule(c, pos0, kmin)
            else:
                quiet.append(c)
                c.lazy_k = kmin - 1
        self._quiet = quiet

    def _oc_schedule(self, c: "_Chan", pos0: int, kmin: int) -> None:
        for k in self._match_ticks(pos0, c.ccr):
            if k < kmin:
                continue
            if k == 0:                                   # fresh counter value at the anchor (update event / UG)
                self._oc_match(c, pos0, 0, reschedule=False)
                continue
            c.ev = self.chip.sched.at_cycles(self._start_cyc + k * self._tick_cyc, self._oc_match, c, pos0, k)
            return

    def _oc_match(self, c: "_Chan", pos0: int, k: int, reschedule: bool = True) -> None:
        c.ev = None
        if self._flag_dir_ok(pos0, k):
            self.set("SR", self.get("SR") | (1 << c.n))   # CCxIF
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
        """Drive TIMx_CHn / TIMx_CHnN from OCxREF and the cached output sources.  Starting or stopping to drive a
        signal also re-syncs the GPIO pins muxed to it (a released output is an undriven pin).  Dead time: OCx
        follows OCxREF and OCxN its inverse, each with its rising edge delayed (c.dp / c.dn, polarity after)."""
        o, on = c.o, c.on
        pins = self.chip.pins
        if c.dt:
            ref = c.ref
            if ref != c.dt_ref:
                c.dt_ref = ref
                if c.dt_ev is not None:                  # a pulse shorter than the dead time is not generated
                    self.chip.sched.cancel(c.dt_ev)
                if ref:
                    c.dn = False
                else:
                    c.dp = False
                c.dt_ev = self.chip.sched.at_cycles(self._now() + c.dt, self._dt_fire, c)
            if o is not None and o >= REF:
                v = "H" if c.dp ^ (o == NREF) else "L"
                if c.out != v:
                    first = c.out is None
                    c.out = v
                    sig = f"{self.name}_CH{c.n}"
                    pins.drive_signal(sig, v)
                    if first:
                        self._repin(sig)
                o = -1
            if on is not None and on >= REF:
                v = "H" if c.dn ^ (on == REF) else "L"
                if c.outn != v:
                    first = c.outn is None
                    c.outn = v
                    sig = f"{self.name}_CH{c.n}N"
                    pins.drive_signal(sig, v)
                    if first:
                        self._repin(sig)
                on = -1
        if o == -1:
            pass
        elif o is None:
            if c.out is not None:
                sig = f"{self.name}_CH{c.n}"
                c.out = None
                self._repin(sig)                         # the GPIO pin becomes an input first (no idle blip)
                pins.release_signal(sig)
        else:
            v = "H" if (o == 1 if o < REF else c.ref ^ (o == NREF)) else "L"
            if c.out != v:
                first = c.out is None
                c.out = v
                sig = f"{self.name}_CH{c.n}"
                pins.drive_signal(sig, v)
                if first:
                    self._repin(sig)
        if on == -1:
            pass
        elif on is None:
            if c.outn is not None:
                sig = f"{self.name}_CH{c.n}N"
                c.outn = None
                self._repin(sig)                         # the GPIO pin becomes an input first (no idle blip)
                pins.release_signal(sig)
        else:
            v = "H" if (on == 1 if on < REF else c.ref ^ (on == NREF)) else "L"
            if c.outn != v:
                first = c.outn is None
                c.outn = v
                sig = f"{self.name}_CH{c.n}N"
                pins.drive_signal(sig, v)
                if first:
                    self._repin(sig)

    def _dt_fire(self, c: "_Chan") -> None:
        """The dead time after an OCxREF edge elapsed: the delayed output rises."""
        c.dt_ev = None
        if c.dt_ref:
            c.dp = True
        else:
            c.dn = True
        self._oc_apply(c)

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    # ------------------------------------------------------------ inputs: TIx pins, BKIN
    def _inputs_cfg(self) -> None:
        """Watch the pins of the inputs the configuration uses (capture sources, encoder TI1/TI2, slave-mode trigger
        TI, BKIN with BKE) and re-sample their levels.  Pin listeners exist only while an input is in use, so PWM
        outputs cost nothing extra."""
        if self.chip is None or getattr(self.chip, "pins", None) is None:
            return
        want: set = {c.ic_src for c in self.ch if c.ic_src}
        sms, ts = self._sms, self._ts
        if self._enc_ok and 1 <= sms <= 3:
            want.update((1, 2))
        elif sms == 4 and ts in (4, 5, 6):
            want.add(2 if ts == 6 else 1)
        if self._advanced and self.get("BDTR") & BKE:
            want.add("B")
        for k in list(self._watch):
            if k not in want:
                self._unwatch(k)
        board = self.chip.pins
        for k in want:
            if k not in self._watch:
                sig = f"{self.name}_BKIN" if k == "B" else f"{self.name}_CH{k}"
                pins = board.signal_pins(sig)
                cb = (lambda pin, k=k: self._ti_changed(k, pin))
                for p in pins:
                    board.input_listeners[p.id].append(cb)
                self._watch[k] = (sig, cb, pins)
            self._ti_lvl[k] = self._input_level(k)

    def _unwatch(self, k: Any) -> None:
        sig, cb, pins = self._watch.pop(k)
        self._ti_lvl.pop(k, None)
        board = getattr(self.chip, "pins", None)
        if board is None:
            return
        for p in pins:
            lst = board.input_listeners.get(p.id)
            if lst and cb in lst:
                lst.remove(cb)

    def _input_level(self, k: Any) -> bool | None:
        """Level of input k (TIx / "B" = BKIN) from the pin muxed to it (H = 1; L and floating X read 0, as IDR);
        None when no pin is muxed to the signal."""
        board = getattr(self.chip, "pins", None)
        if board is None:
            return None
        sig = f"{self.name}_BKIN" if k == "B" else f"{self.name}_CH{k}"
        for p in board.signal_pins(sig):
            if board.signal_matches(p.owner, sig):
                return p.level() == "H"
        return None

    def _ti_changed(self, k: Any, pin: Any) -> None:
        """PinBoard input listener: a pin offering input k changed level."""
        w = self._watch.get(k)
        if w is None or not self.chip.pins.signal_matches(pin.owner, w[0]):
            return                                       # pin not muxed to the timer
        new = pin.level() == "H"
        old = self._ti_lvl.get(k)
        self._ti_lvl[k] = new
        if old is None or old == new:
            return
        if k == "B":
            if self._brk_active():
                self._break()
            return
        if not self._frozen:                             # clock gated off: no input sampling
            self._ti_edge(k, new)

    def _ti_edge(self, k: int, rising: bool) -> None:
        """Edge on TIk at chip.now_cycles(): captures (CNT before any slave reset), then slave mode."""
        bit = 1 if rising else 2
        for c in self.ch:
            if c.ic_src == k and c.ic_edges & bit:
                c.ic_cnt += 1
                if c.ic_cnt >= c.ic_div:
                    c.ic_cnt = 0
                    self._capture(c)
        sms = self._sms
        if not sms:
            return
        if 1 <= sms <= 3:
            if self._enc_ok:
                self._enc_edge(k, rising)
        elif sms == 4:
            ts = self._ts
            if (ts == 4 and k == 1) or ((ts == 5 and k == 1) or (ts == 6 and k == 2)) and \
                    self._edges_of(k, self.get("CCER")) & bit:
                self._reinit()                           # reset mode: counter + prescaler re-initialised, UEV
                self.set("SR", self.get("SR") | TIF)
                self._update_line()

    def _capture(self, c: "_Chan") -> None:
        """Input capture: CCRx = CNT now, CCxIF (CCxOF when CCxIF was still set)."""
        v = self.cnt()
        sr = self.get("SR")
        if sr >> c.n & 1:
            sr |= 1 << (8 + c.n)
        self.set(f"CCR{c.n}", v)
        self.set("SR", sr | (1 << c.n))
        self._update_line()

    # ------------------------------------------------------------ slave mode / encoder interface
    def _encoder(self) -> bool:
        return self._enc_ok and 1 <= self._sms <= 3

    def _smcr_write(self, blk, reg, old, new):
        enc_new = self._enc_ok and 1 <= (new & 7) <= 3
        if enc_new and not self._encoder() and self.running:
            c, down = self._cnt_dir()                    # entering encoder mode: the counter holds its value
            self.set("CNT", c)
            self._stop()
        was = self._encoder()
        self._store(reg, new)
        self._sms, self._ts = new & 7, new >> 4 & 7
        sms, ts = self._sms, self._ts
        if sms in (5, 6, 7) or (sms and sms < 4 and not self._enc_ok) or (sms == 4 and ts not in (4, 5, 6)) \
                or new & (1 << 14):
            self._warn_once(f"sms{sms}.{ts}.{new >> 14 & 1}", f"{self.name}: SMCR=0x{new:04x}: only encoder mode "
                            "(SMS=001-011) and reset mode (SMS=100) on TI1F_ED/TI1FP1/TI2FP2 are modelled; gated/"
                            "trigger/external clock modes, ITRx and ETR are not")
        if was and not self._encoder() and self.get("CR1") & CEN:
            self._start(self._restart_pos())
        if self.ch:
            self._inputs_cfg()
        return None

    def _enc_edge(self, k: int, rising: bool) -> None:
        """Encoder interface (RM0090 table 95): SMS=001 counts TI1FP1 edges, 010 TI2FP2 edges, 011 both; the
        direction comes from the level of the other input.  TIxFPx = TIx inverted when CCxP=1."""
        sms = self._sms
        if (sms != 3 and sms != k) or not self.get("CR1") & CEN or k > 2:
            return
        ccer = self.get("CCER")
        p1, p2 = bool(ccer & 2), bool(ccer & 0x20)
        if k == 1:
            up = (rising ^ p1) != (bool(self._ti_lvl.get(2)) ^ p2)
        else:
            up = (rising ^ p2) == (bool(self._ti_lvl.get(1)) ^ p1)
        cnt, arr = self.get("CNT") & self.cmask, self.arr
        ovf = False
        if up:
            if cnt == arr:
                cnt, ovf = 0, True
            else:
                cnt = (cnt + 1) & self.cmask
        elif cnt == 0:
            cnt, ovf = arr, True
        else:
            cnt -= 1
        self.set("CNT", cnt)
        cr1 = self.get("CR1")
        self.set("CR1", (cr1 & ~DIR) if up else (cr1 | DIR))
        if ovf:
            self._overflow()
        if self.ch:
            self._oc_resync(kmin=0)                      # PWM/forced OCxREF follow the counter (no CCxIF)

    # ------------------------------------------------------------ break (TIM1/8)
    def _brk_active(self, bdtr: int | None = None) -> bool:
        """Break input active: BKE=1 and the pin muxed to TIMx_BKIN at the BKP level (no pin muxed: inactive)."""
        bd = self.get("BDTR") if bdtr is None else bdtr
        if not bd & BKE:
            return False
        lvl = self._input_level("B")
        return lvl is not None and lvl == bool(bd & BKP)

    def _break(self) -> None:
        """Break event (BKIN at its active level with BKE, or EGR.BG): MOE cleared, outputs to the off-state
        selected by OSSI (CR2.OISx idle levels or released), BIF (BIE: the TIMx_BRK vector)."""
        bd = self.get("BDTR")
        self.set("BDTR", bd & ~MOE)
        self.set("SR", self.get("SR") | BIF)
        self._update_line()
        self.emit("info", block=self.name, msg=f"{self.name}: break (MOE cleared)")
        self._oc_config_changed()

    def _bdtr_write(self, blk, reg, old, new):
        was = self._brk_active(old)
        if self._brk_active(new):
            new &= ~MOE                                  # MOE cannot be set while the break input is active
            self._store(reg, new)
            if not was:                                  # BKE / BKP written so that the input is now active
                self._break()
                return None
        else:
            self._store(reg, new)
        self._oc_config_changed()
        return None

    def _aoe(self) -> None:
        """Update event: AOE=1 sets MOE again unless the break input is active."""
        bd = self.get("BDTR")
        if bd & AOE and not bd & MOE and not self._brk_active(bd):
            self.set("BDTR", bd | MOE)
            self._chan_cfg()

    # ------------------------------------------------------------ pin direction (asked by the STM32 GPIO model)
    def signal_driven(self, n: int, neg: bool = False) -> bool | None:
        """Does the timer drive TIMx_CHn (TIMx_CHnN with `neg`) now?  None: no such channel.  The GPIO model shows a
        pin muxed to a channel that is not driven (output disabled / released, input capture) as an input."""
        c = next((x for x in self.ch if x.n == n), None)
        if c is None:
            return None
        return (c.on if neg else c.o) is not None

    def _repin(self, sig: str) -> None:
        chip = self.chip
        if chip is None:
            return
        gp = self._gpios
        if not gp:
            gp = self._gpios = [b for b in getattr(chip, "peripherals", {}).values()
                                if getattr(b, "model", None) == "stm32.gpio" and hasattr(b, "resync_signal")]
        for b in gp:
            b.resync_signal(sig)
