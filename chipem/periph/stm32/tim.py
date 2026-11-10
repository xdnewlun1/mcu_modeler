"""STM32F4 timers: TIM2-5 (general purpose, TIM2/TIM5 32-bit), TIM6/7 (basic), TIM1/8 (advanced), TIM9-14.

One model for all of them; the features follow from the SVD register set of each timer: channels = the CCRx
registers present (TIM9/12: 2, TIM10/11/13/14: 1), BDTR = advanced (complementary outputs, dead time, break), RCR =
repetition counter, CR1 bits the SVD does not define (DIR/CMS on TIM9-14) read 0 and ignore writes (OPM on
TIM10/11/13/14, missing from the SVD, is implemented: RM0090 19.5.1, Rev 22), the encoder interface needs SMCR and 4 channels (TIM1-5, TIM8).  TIM9-14 count up only, 16 bits.

Registers from the SVD; behaviour (RM0090 ch. 17-20):
* Counter clock = timer kernel clock / (PSC + 1); kernel clock = RCC timer clock of the timer's APB bus (APB2 for
  TIM1/8/9/10/11, APB1 otherwise; x2 when the APB prescaler is not 1), consistent with the actual core clock.  The
  kernel clock derives from HCLK only (core / APB prescaler x 1 or 2), so one counter tick is a whole number of core
  cycles and every timer event is scheduled in the core-cycle domain (`sched.at_cycles`): a core clock change
  (SYSCLK switch, HPRE) leaves the timing untouched; an APB prescaler change re-anchors the counter (RCC listener).
* CR1: CEN starts/stops, DIR down-counting, CMS (center-aligned: 0 -> ARR -> 0, period 2 x ARR, DIR is read-only
  and reads the current direction), OPM (CEN clears at the update event, the outputs follow the stopped counter),
  ARPE (ARR preload), UDIS (no update events: shadows keep their value; UG only re-initialises the counter), URS
  (UG / slave reset set no UIF and raise no TIMx_UP DMA request: only over/underflow does, 17.4.1/18.4.1).
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
* Output enables (RM0090 Table 95 on TIM1/8): CCxE drives TIMx_CHn = OCxREF ^ CCxP; general-purpose timers release
  the signal when CCxE=0.  TIM1/8: with MOE=1, CCxNE drives TIMx_CHnN = ~OCxREF ^ CCxNP (OCxREF ^ CCxNP with
  CCxE=0); with OSSR=1 an output whose enable is 0 while the other one is set is driven at its inactive level
  (CCxP / CCxNP).  With MOE=0 and OSSI=1 an enabled channel drives its idle levels CR2.OISx / OISxN; with OSSI=0
  (or both enables 0) the outputs are released.  A released signal is not driven: the STM32 GPIO model asks
  `signal_driven()` and shows a pin muxed to it as an input (level from PUPDR, an external drive or the net).
* IRQ vectors: TIM1/8 have UP (UIF), CC (CCxIF), BRK (BIF) and TRG_COM (TIF/COMIF); the other timers one vector for
  all flags.  Vectors shared between timers (TIM1_UP_TIM10, TIM1_BRK_TIM9, TIM8_UP_TIM13, ...) are the OR of the
  timers' levels.
* Input capture (CCxS != 0, RM0090 17.3.5/18.3.5): the channel releases TIMx_CHn / TIMx_CHnN (an input).  CCxS=01
  captures TIx, 10 the other input of the pair (CH1<->CH2, CH3<->CH4; reserved on 1-channel timers), 11 the trigger
  input TRC (= TRGI of the slave mode controller, SMCR.TS; edges after the TS source polarity, then CCxP/CCxNP like a
  pin).  CCxNP:CCxP select the edge (00 rising, 01 falling,
  11 both; the reserved 10 acts as rising), ICxPSC captures every 1/2/4/8 active edges (the prescaler restarts when
  CCxE, CCxS or ICxPSC change).  With CCxE=1 an active edge latches CCRx = CNT at the edge (chip.now_cycles(),
  instruction-exact, no resynchronisation delay) and sets CCxIF, or CCxOF when CCxIF is still set; CCxIE raises the
  IRQ.  Reading CCRx clears CCxIF (not CCxOF, RM0090 18.4.5); CCRx is read-only.  EGR.CCxG on an input channel
  captures CNT too.  Edges come from the pin muxed to TIMx_CHn (PinBoard input listeners, registered only while an
  input is in use): `--at`, board nets, parts or the timer's own outputs; H is 1, L and floating X are 0 (as IDR).  A
  pin that is not muxed to the timer's signal is not seen.  CR2.TI1S (3+ channel timers): TI1 = CH1 xor CH2 xor CH3
  pins (hall sensor interface).  ICxF (digital filter) is not modelled (no-op).  With the clock gated off (frozen)
  edges are ignored.
* Master mode (CR2.MMS -> TRGO, RM0090 17.4.2/18.4.2): 000 reset (pulse on EGR.UG and on a slave-mode reset), 001
  enable (level CNT_EN = CEN, and the trigger input in gated mode), 010 update (pulse at every update event, also from
  UG), 011 compare pulse (pulse whenever CC1IF is to be set: match, capture, CC1G), 100-111 OC1REF-OC4REF (level).
  Other blocks subscribe with `add_trigger_listener("TRGO" | "CC1".."CC4", cb)`, cb(t_cycles, level) (a pulse is
  cb(t, True), cb(t, False); `trigger_level(sig)` gives the current level): slave timers (ITRx) and the ADC external
  triggers (adc.py).  "CCx" is OCxREF for an output channel (the ADC triggers on its edges) and a pulse per capture
  for an input channel.  A watched channel gets exact match events like one with an enabled output.  TRGO and the
  slave reaction happen at the same core cycle (no resynchronisation delay; MSM has no effect).
* Slave mode controller (SMCR.SMS, trigger input TRGI selected by SMCR.TS): ITR0-3 = another timer's TRGO per
  RM0090 Tables 94/98/101 (`ITR` below; TIM9/12 ITR2/3 = OC1REF of TIM10/11 or TIM13/14), TI1F_ED (a pulse per TI1
  edge), TI1FP1 / TI2FP2 (TIx inverted with CCxP; CCxNP:CCxP = 11 makes both edges active, except in gated mode),
  ETRF (TIMx_ETR pin, including bundled "TIM2_CH1_ETR" functions: ETP inverts, ETPS divides the rising edges by
  1/2/4/8, a divided ETRP is a pulse per Nth edge; ETF filter not modelled).  SMS=100 reset (rising TRGI: captures on
  that edge first, then counter + prescaler re-initialise like UG, TIF), so the RM0090 PWM input mode (IC1 = TI1
  rising, IC2 = TI1 falling, reset on TI1FP1) gives CCR1 = period and CCR2 = high time; 101 gated (the counter runs
  while TRGI is high and CEN=1, holds its value otherwise; TIF on both edges); 110 trigger (rising TRGI sets CEN and
  starts the counter, TIF; with OPM a retriggerable one-pulse); 111 external clock mode 1 (each rising TRGI edge is a
  CK_PSC clock: PSC divides it, the counter counts up or down (DIR) edge-aligned with compare matches, overflow /
  update events, TIF per edge).  SMCR.ECE external clock mode 2: rising ETRF edges clock the counter the same way,
  combinable with reset/gated/trigger mode on another TS.  Center-aligned counting is not applied to external clocks
  (they count edge-aligned).  EGR.TG sets TIF.  DMA requests (`_util.dma_request`, when the DMA model provides it):
  TIMx_UP on update events (UDE), TIMx_CHn when CCxIF is set (CCxDE; on update events with CR2.CCDS), TIMx_TRIG with
  TIF (TDE).  Encoder interface (SMS=001/010/011, TIM1-5/8, RM0090 Tables 93/97): the internal clock stops driving the
  counter; each TI1FP1 (001), TI2FP2 (010) or either (011) edge counts up or down by the level of the other input
  (TIxFPx = TIx inverted with CCxP), between 0 and ARR, DIR reflects the direction (read-only), wrapping is an
  overflow (repetition counter, update event, UIF).  PWM/forced OCxREF follow the count, compare matches set no
  CCxIF in encoder mode.
* Dead time (TIM1/8, RM0090 17.3.11): with MOE=1 and both CCxE and CCxNE set, OCx follows OCxREF and OCxN its
  inverse, each rising edge delayed by BDTR.DTG (0xxxxxxx: DTG x tDTS, 10xxxxxx: (64 + DTG[5:0]) x 2 tDTS, 110xxxxx:
  (32 + DTG[4:0]) x 8 tDTS, 111xxxxx: (32 + DTG[4:0]) x 16 tDTS; tDTS = CK_INT x 1/2/4 by CR1.CKD), then the
  polarity CCxP/CCxNP applies.  A pulse shorter than the dead time is not generated.  The delayed edges are scheduler
  events (cancelled on reset and when the pair leaves the dead-time configuration).  With only one output enabled
  (or OSSR off-state on the other) there is no dead time (RM0090 Table 95).
* Break (TIM1/8): BKE=1 and the pin muxed to TIMx_BKIN at the BKP level (BKP=0: low; floating reads low; no pin muxed:
  inactive), or EGR.BG: MOE is cleared, the outputs take the off-state OSSI selects (CR2.OISx/OISxN idle levels with
  OSSI=1, released with OSSI=0; immediately, without the dead time), BIF is set (BIE: TIMx_BRK vector).  While the
  break input stays active MOE cannot be set and BIF cannot be cleared.  AOE=1 sets MOE again at the next update
  event (counter, UG, slave reset) once the break input is inactive.  BDTR.LOCK (17.4.18) is writable once after
  reset; level 1 freezes BDTR.DTG/BKE/BKP/AOE and CR2.OISx/OISxN, level 2 also OSSR/OSSI and CCxP/CCxNP of output
  channels, level 3 also OCxM/OCxPE of output channels (frozen bits keep their value, the rest of the write
  applies).
* OCxFE (fast enable) is a no-op: trigger reactions are already immediate.  Not modelled: OCxCE (OCxREF clear on
  ETRF), CR2 CCPC/COM preload (COMG), the ETF / ICxF digital filters, TIM2_OR / TIM5_OR / TIM11_OR input remaps, the
  DMA burst registers DCR/DMAR (stored only).
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
ONE_CH_TIMERS = ("TIM10", "TIM11", "TIM13", "TIM14")
OCPE = 1 << 3                    # CCMRx output preload enable (per channel byte)
MOE, AOE, BKP, BKE, OSSR, OSSI = 1 << 15, 1 << 14, 1 << 13, 1 << 12, 1 << 11, 1 << 10      # BDTR (TIM1/8)
TIF, BIF = 1 << 6, 1 << 7        # SR (DIER: TIE, BIE; EGR: TG, BG)
CKD_DIV = (1, 2, 4, 4)           # CR1.CKD -> tDTS / tCK_INT (11 is reserved: treated as /4)
# output source codes (_Chan.o / _Chan.on): None released, 0/1 fixed level, REF follows OCxREF, NREF its inverse
REF, NREF = 2, 3
_PARTNER = {1: 2, 2: 1, 3: 4, 4: 3}      # CCxS=10: the input of the other channel of the pair
# SMCR.TS = 000-011: internal trigger ITR0-ITR3 -> (master timer, its trigger signal).  RM0090 Table 94 (TIM1/8),
# Table 98 (TIM2-5), Table 101 (TIM9/12: ITR2/ITR3 are the OC1 outputs of TIM10/11 and TIM13/14).  TIM2 ITR1 is
# TIM8_TRGO with TIM2_OR.ITR1_RMP = 00 (the OTG FS/HS SOF remaps are not modelled).
ITR: dict[str, tuple[tuple[str, str], ...]] = {
    "TIM1": (("TIM5", "TRGO"), ("TIM2", "TRGO"), ("TIM3", "TRGO"), ("TIM4", "TRGO")),
    "TIM8": (("TIM1", "TRGO"), ("TIM2", "TRGO"), ("TIM4", "TRGO"), ("TIM5", "TRGO")),
    "TIM2": (("TIM1", "TRGO"), ("TIM8", "TRGO"), ("TIM3", "TRGO"), ("TIM4", "TRGO")),
    "TIM3": (("TIM1", "TRGO"), ("TIM2", "TRGO"), ("TIM5", "TRGO"), ("TIM4", "TRGO")),
    "TIM4": (("TIM1", "TRGO"), ("TIM2", "TRGO"), ("TIM3", "TRGO"), ("TIM8", "TRGO")),
    "TIM5": (("TIM2", "TRGO"), ("TIM3", "TRGO"), ("TIM4", "TRGO"), ("TIM8", "TRGO")),
    "TIM9": (("TIM2", "TRGO"), ("TIM3", "TRGO"), ("TIM10", "CC1"), ("TIM11", "CC1")),
    "TIM12": (("TIM4", "TRGO"), ("TIM5", "TRGO"), ("TIM13", "CC1"), ("TIM14", "CC1")),
}
TI1S = 1 << 7                    # CR2: TI1 = CH1 xor CH2 xor CH3
CCDS = 1 << 3                    # CR2: CCx DMA requests on the update event
ECE, ETP = 1 << 14, 1 << 15      # SMCR
UDE, TDE = 1 << 8, 1 << 14       # DIER DMA request enables (CCxDE: bits 9-12)


def _dma_request(chip: Any, name: str, level: bool = True) -> None:
    """Assert (pulse: the DMA acknowledges it by one transfer) or deassert DMA request `name` (RM0090 Table 43/44,
    e.g. "TIM3_UP", "TIM2_CH1", "ADC1") through `_util.dma_request` (stm32.dma); nothing without a DMA model."""
    from . import _util
    fn = getattr(_util, "dma_request", None)
    if fn is None or chip is None:
        return
    try:
        fn(chip, name, level)
    except (ImportError, AttributeError):
        pass


class _Chan:
    __slots__ = ("n", "ccr", "ref", "ev", "out", "outn", "mode", "ccs", "pe", "o", "on", "irq", "active", "lazy_k",
                 "dt", "dt_ref", "dt_ev", "dp", "dn", "ic_src", "ic_edges", "ic_div", "ic_cnt", "ic_key", "watch",
                 "wref")

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
        # OCxREF watched (a CCx trigger listener, TRGO = OCxREF / compare pulse): exact match events, edges reported
        self.watch = False
        self.wref = False


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
            "CR2": {"on_write": self._cr2_write},
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
        if name in ONE_CH_TIMERS:      # RM0090 19.5.1 (Rev 22): CR1.OPM exists; the SVD marks bit 3 reserved
            ov["CR1"]["ro_mask"] = 0xFFFFFC70
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
        # master mode (CR2.MMS -> TRGO) and trigger outputs for other blocks (slave timers, ADC external triggers)
        self.trgo_listeners: list = []               # cb(t_cycles, level); a pulse is cb(t, True), cb(t, False)
        self._cc_lst: dict[int, list] = {}           # channel n -> listeners of its CCx event (OCxREF / capture)
        self._mms = 0
        self._trgo_lvl = False
        self._dma_on = False                         # DIER has a DMA request enable (UDE, CCxDE, TDE)
        # slave mode: trigger input TRGI (level), its source key, ITR subscription, ETR prescaler, external clock
        self._trgi = False
        self._trgi_key: Any = None
        self._itr_sub: tuple | None = None           # (master block, signal, callback)
        self._trc = False                            # a channel captures on TRC (CCxS=11)
        self._etr_cnt = 0
        self._ece = False                            # SMCR.ECE: external clock mode 2 (ETRF)
        self._xpsc = 0                               # external clock: prescaler count
        self._ti1x = False                           # TI1 with CR2.TI1S (CH1 xor CH2 xor CH3)
        self._bdtr_written = False                   # BDTR.LOCK is writable once after reset

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
        if getattr(self, "_itr_sub", None) is not None:
            self._itr_unsub()
        if getattr(self, "_dma_on", False) and self.chip is not None:
            for sfx in ("UP", "TRIG", *(f"CH{c.n}" for c in self.ch)):
                _dma_request(self.chip, f"{self.name}_{sfx}", False)
        if hasattr(self, "trgo_listeners"):
            self._mms = 0
            self._trgo_lvl = self._trgi = self._trc = self._ti1x = self._dma_on = self._ece = False
            self._trgi_key = None
            self._etr_cnt = self._xpsc = 0
        pins = getattr(self.chip, "pins", None)
        for c in getattr(self, "ch", ()):
            if c.dt_ev is not None and self.chip is not None:
                self.chip.sched.cancel(c.dt_ev)
            c.dt_ev = None
            c.dt, c.dt_ref, c.dp, c.dn = 0, False, False, True
            c.ic_src, c.ic_cnt, c.ic_key = 0, 0, None
            c.ref = c.wref = False
            c.ccr = 0
            for sig in (f"{self.name}_CH{c.n}", f"{self.name}_CH{c.n}N"):
                if pins is not None:
                    pins.release_signal(sig)
            c.out = c.outn = None
        super().reset()
        self._bdtr_written = False
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
        self._hold()
        self._frozen = True

    def _hold(self) -> None:
        """Stop the running counter with CEN still set (clock gate off, gated slave mode with TRGI low, entering an
        external clock mode): CNT/DIR hold the counter, the outputs their level."""
        if not self.running:
            return
        c, down = self._cnt_dir()
        self.set("CNT", c)
        if self._cms():
            self.set("CR1", (self.get("CR1") | DIR) if down else (self.get("CR1") & ~DIR))
        self._stop()

    def _ext_mode(self) -> bool:
        """The counter is clocked by input edges (encoder interface, external clock mode 1 or 2), not CK_INT."""
        return (self._enc_ok and 1 <= self._sms <= 3) or self._sms == 7 or self._ece

    def _gate_open(self) -> bool:
        return self._sms != 5 or self._trgi

    def _resume_or_hold(self) -> None:
        """After a slave-mode change: run the internal counter when CEN=1, the clock is on, no external clock mode
        drives it and the gate (SMS=101) is open; hold it otherwise."""
        should = bool(self.get("CR1") & CEN) and not self._frozen and not self._ext_mode() and self._gate_open()
        if self.running and not should:
            self._hold()
        elif not self.running and should:
            self._start(self._restart_pos())

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
        if not self.get("CR1") & CEN or self.chip is None or self._frozen or self._ext_mode() or \
                (self._sms == 5 and not self._trgi):
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
            if self._mms == 1:
                self._trgo_set(False)
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
        if self._mms == 2 and self.trgo_listeners:
            self._trgo_pulse()
        if self._dma_on:
            self._dma_update()
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
        v = self._cr1_apply(blk, reg, old, new)
        if (old ^ new) & CEN:
            self._xpsc = 0
            if self._mms == 1:                            # TRGO = CNT_EN
                self._store(reg, new if v is None else v)
                self._trgo_set(self._cnt_en())
        return v

    def _cnt_en(self) -> bool:
        """CNT_EN (TRGO with MMS=001): CEN, and in gated mode the trigger input level."""
        return bool(self.get("CR1") & CEN) and self._gate_open()

    def _cr1_apply(self, blk, reg, old, new):
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
                if self._ext_mode():
                    self.emit("info", block=self.name, msg=f"{self.name} enabled: external clock mode "
                              f"{'2 (ETRF)' if self._ece else '1 (TRGI)'}, PSC={self.psc}, ARR={self.arr}")
                    return new
                if not self.running and self._sms == 5:
                    self.emit("info", block=self.name, msg=f"{self.name} enabled in gated mode: counts while the "
                              "trigger input is high")
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
            for c in self.ch:
                if new >> c.n & 1:
                    self._cc_event(c)
        if new & TIF:                          # TG: trigger event -> TIF
            self._set_tif()
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
            if not cr1 & URS:                  # URS: only over/underflow raises UIF and the UP DMA request
                self.set("SR", self.get("SR") | UIF)
                self._update_line()
                if self._dma_on:
                    self._dma_update()
            if self._advanced:
                self._aoe()
        self._xpsc = 0
        if self.trgo_listeners and (self._mms == 0 or (self._mms == 2 and not cr1 & UDIS)):
            self._trgo_pulse()                         # MMS=000: UG / slave reset; MMS=010: update event
        start = self.arr if (self._down() and not self._cms()) else 0
        self.set("CNT", start)
        if self._cms():
            self.set("CR1", self.get("CR1") & ~DIR)   # restarts counting up from 0
        if self.running:
            self._start(start, period=True)            # prescaler and counter re-initialised now
        elif self.ch:
            self._oc_resync(kmin=0)                   # stopped counter: OCxREF from the new CNT/CCRx

    def _dier_write(self, blk, reg, old, new):
        self._dma_on = bool(new & (UDE | TDE | 0x1E00))
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
        if self._lock() >= 2:            # LOCK level 2: CCxP / CCxNP of output channels are frozen (17.4.18)
            for c in self.ch:
                if not c.ccs:
                    m = 0xA << (4 * (c.n - 1))
                    new = (new & ~m) | (old & m)
        self._store(reg, new)
        self._oc_config_changed()
        return None

    # ------------------------------------------------------------ output compare / PWM
    def _ccmr_field(self, n: int) -> int:
        name = self._ccmr[(n - 1) // 2]
        return (self.get(name) >> (8 * ((n - 1) % 2))) & 0xFF if name else 0

    def _chan_cfg(self) -> None:
        """Refresh the cached channel configuration from CCMRx, CCER, BDTR, CR2 and DIER (RM0090 Table 95)."""
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
        cc_lst, mms, tl = self._cc_lst, self._mms, bool(self.trgo_listeners)
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
            elif c.ccs == 3 and "SMCR" in self.regs:
                src = "T"                                # TRC: the slave-mode trigger input (SMCR.TS)
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
            watch = c.ccs == 0 and (bool(cc_lst.get(c.n)) or (tl and (mms == 3 + c.n or (mms == 3 and c.n == 1))))
            if watch and not c.watch:
                c.wref = c.ref                           # start reporting from the current level (no false edge)
            c.watch = watch
            c.active = c.ccs == 0 and (follows or c.irq or c.mode in (1, 2, 3) or watch)
        self._trc = any(c.ic_src == "T" for c in self.ch)
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

    def _lock(self) -> int:
        """BDTR.LOCK level (TIM1/8; 0 elsewhere)."""
        return (self.get("BDTR") >> 8) & 3 if self._advanced else 0

    def _oc_cfg_write(self, blk, reg, old, new):
        if self._lock() == 3:            # LOCK level 3: OCxM / OCxPE of output channels (CCxS = 00) are frozen
            for sh in (0, 8):
                if not (old >> sh) & 3:
                    m = 0x78 << sh
                    new = (new & ~m) | (old & m)
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
            if c.watch or self._dma_on:
                self._cc_event(c)
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
        if c.watch and c.ref != c.wref:
            c.wref = c.ref
            self._ref_edge(c)
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

    # ------------------------------------------------------------ inputs: TIx pins, ETR, BKIN
    def _inputs_cfg(self) -> None:
        """Watch the pins of the inputs the configuration uses (capture sources, encoder TI1/TI2, slave-mode trigger
        TI / ETR, external clock mode 2 ETR, CR2.TI1S xor inputs, BKIN with BKE), subscribe to the ITRx master and
        re-sample the levels.  Pin listeners exist only while an input is in use, so PWM outputs cost nothing extra."""
        if self.chip is None or getattr(self.chip, "pins", None) is None:
            return
        want: set = {c.ic_src for c in self.ch if isinstance(c.ic_src, int) and c.ic_src}
        sms, ts = self._sms, self._ts
        slave = sms >= 4 or self._trc                    # TRGI in use
        if self._enc_ok and 1 <= sms <= 3:
            want.update((1, 2))
        elif slave and ts in (4, 5, 6):
            want.add(2 if ts == 6 else 1)
        has_etr = "SMCR" in self.regs and self._enc_ok   # ETR: TIM1-5, TIM8
        if has_etr and (self._ece or (slave and ts == 7)):
            want.add("E")
        if self._advanced and self.get("BDTR") & BKE:
            want.add("B")
        ti1s = bool(self.get("CR2") & TI1S) if "CR2" in self.regs and len(self.ch) >= 3 else False
        if ti1s and 1 in want:
            want.update((1, 2, 3))
        for k in list(self._watch):
            if k not in want:
                self._unwatch(k)
        board = self.chip.pins
        for k in want:
            if k not in self._watch:
                sig = self._input_sig(k)
                pins = self._etr_pins() if k == "E" else board.signal_pins(sig)
                cb = (lambda pin, k=k: self._ti_changed(k, pin))
                for p in pins:
                    board.input_listeners[p.id].append(cb)
                self._watch[k] = (sig, cb, pins)
            self._ti_lvl[k] = self._input_level(k)
        self._ti1x = self._ti1_now()
        # TRGI source: ITRx (another timer's trigger output), TI1F_ED / TI1FP1 / TI2FP2, ETRF
        key = None
        if slave and "SMCR" in self.regs:
            itr = ITR.get(self.name) if ts < 4 else None
            if ts < 4:
                key = ("ITR", ts) if itr else None
            elif ts == 7:
                key = ("E",) if has_etr else None
            else:
                key = ("TI", ts)
        if key != self._trgi_key:
            if self._itr_sub is not None:
                self._itr_unsub()
            self._trgi_key = key
            self._etr_cnt = 0
            if key is not None and key[0] == "ITR":
                self._itr_subscribe(key[1])
        self._trgi = self._trgi_now()                    # (re)sampled silently: source or polarity changed

    def _input_sig(self, k: Any) -> str:
        if k == "B":
            return f"{self.name}_BKIN"
        if k == "E":
            return f"{self.name}_ETR"
        return f"{self.name}_CH{k}"

    def _etr_pins(self) -> list:
        """Pins offering TIMx_ETR, alone or bundled with a channel ("TIM2_CH1_ETR")."""
        return [p for p in self.chip.pins.pins if any(self._etr_owner(f) for f in p.functions.values())]

    def _etr_owner(self, owner: Any) -> bool:
        if not owner:
            return False
        want = f"{self.name}_ETR"
        pre = f"{self.name}_CH"
        for part in str(owner).split("/"):
            part = part.strip()
            if part == want or (part.startswith(pre) and part.endswith("_ETR")):
                return True
        return False

    def _muxed(self, k: Any, owner: Any) -> bool:
        if k == "E":
            return self._etr_owner(owner)
        return self.chip.pins.signal_matches(owner, self._input_sig(k))

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
        """Level of input k (TIx / "B" = BKIN / "E" = ETR) from the pin muxed to it (H = 1; L and floating X read 0,
        as IDR); None when no pin is muxed to the signal."""
        board = getattr(self.chip, "pins", None)
        if board is None:
            return None
        pins = self._watch[k][2] if k in self._watch else (
            self._etr_pins() if k == "E" else board.signal_pins(self._input_sig(k)))
        for p in pins:
            if self._muxed(k, p.owner):
                return p.level() == "H"
        return None

    def _ti1_now(self) -> bool:
        """TI1: the CH1 input, or CH1 xor CH2 xor CH3 with CR2.TI1S."""
        lv = self._ti_lvl
        if "CR2" in self.regs and len(self.ch) >= 3 and self.get("CR2") & TI1S:
            return bool(lv.get(1)) ^ bool(lv.get(2)) ^ bool(lv.get(3))
        return bool(lv.get(1))

    def _ti_changed(self, k: Any, pin: Any) -> None:
        """PinBoard input listener: a pin offering input k changed level."""
        w = self._watch.get(k)
        if w is None or not self._muxed(k, pin.owner):
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
        if self._frozen:                                 # clock gated off: no input sampling
            return
        if k == "E":
            self._etr_edge(new)
            return
        if k <= 3 and len(self.ch) >= 3 and self.get("CR2") & TI1S:
            t1 = self._ti1_now()
            if t1 != self._ti1x:
                self._ti1x = t1
                self._ti_edge(1, t1)
            if k == 1:
                return
        elif k == 1:
            self._ti1x = new
        self._ti_edge(k, new)

    def _ti_edge(self, k: int, rising: bool) -> None:
        """Edge on TIk at chip.now_cycles(): captures (CNT before any slave reset), then encoder / slave trigger."""
        bit = 1 if rising else 2
        for c in self.ch:
            if c.ic_src == k and c.ic_edges & bit:
                c.ic_cnt += 1
                if c.ic_cnt >= c.ic_div:
                    c.ic_cnt = 0
                    self._capture(c)
        sms = self._sms
        if 1 <= sms <= 3:
            if self._enc_ok:
                self._enc_edge(k, rising)
            return
        if (sms >= 4 or self._trc) and k <= 2:
            ts = self._ts
            if ts == 4 and k == 1:
                self._trgi_pulse()                       # TI1F_ED: every edge of TI1
            elif (ts == 5 and k == 1) or (ts == 6 and k == 2):
                e = self._edges_of(k, self.get("CCER"))
                if e == 3 and sms != 5:
                    self._trgi_pulse()                   # CCxNP:CCxP = 11: both edges are active
                else:
                    self._trgi_set(rising ^ (e == 2))    # TIxFPx = TIx inverted with CCxP

    def _etr_edge(self, level: bool) -> None:
        """ETR pin edge: ETRP = ETR xor ETP, divided by ETPS (1, 2, 4, 8 rising edges); ETRF (no filter) feeds TRGI
        (TS=111) and external clock mode 2 (ECE)."""
        smcr = self.get("SMCR")
        lvl = level ^ bool(smcr & ETP)
        div = 1 << (smcr >> 12 & 3)
        if div == 1:
            if self._trgi_key == ("E",):
                self._trgi_set(lvl)
            if self._ece and lvl:
                self._ext_tick()
            return
        if not lvl:
            return
        self._etr_cnt += 1
        if self._etr_cnt < div:
            return
        self._etr_cnt = 0
        if self._trgi_key == ("E",):
            self._trgi_pulse()
        if self._ece:
            self._ext_tick()

    def _capture(self, c: "_Chan") -> None:
        """Input capture: CCRx = CNT now, CCxIF (CCxOF when CCxIF was still set)."""
        v = self.cnt()
        sr = self.get("SR")
        if sr >> c.n & 1:
            sr |= 1 << (8 + c.n)
        self.set(f"CCR{c.n}", v)
        self.set("SR", sr | (1 << c.n))
        self._update_line()
        self._cc_event(c)

    # ------------------------------------------------------------ slave mode controller (TRGI)
    def _itr_subscribe(self, i: int) -> None:
        master_name, sig = ITR[self.name][i]
        m = self.chip.peripherals.get(master_name) if self.chip is not None else None
        if m is None or not hasattr(m, "add_trigger_listener"):
            self._warn_once(f"itr{i}", f"{self.name}: ITR{i} = {master_name}_{sig} is not available on this chip")
            return
        cb = self._itr_level
        m.add_trigger_listener(sig, cb)
        self._itr_sub = (m, sig, cb)

    def _itr_unsub(self) -> None:
        m, sig, cb = self._itr_sub
        self._itr_sub = None
        m.remove_trigger_listener(sig, cb)

    def _itr_level(self, t_cyc: int, level: bool) -> None:
        if not self._frozen:
            self._trgi_set(bool(level))

    def _trgi_now(self) -> bool:
        """Current TRGI level of the selected source (taken silently when the source changes)."""
        key = self._trgi_key
        if key is None:
            return False
        if key[0] == "ITR":
            return bool(self._itr_sub[0].trigger_level(self._itr_sub[1])) if self._itr_sub else False
        if key[0] == "E":
            smcr = self.get("SMCR")
            lv = self._ti_lvl.get("E")
            return bool(lv) ^ bool(smcr & ETP) if lv is not None and not smcr >> 12 & 3 else False
        ts = key[1]
        if ts == 4:
            return False
        k = 1 if ts == 5 else 2
        e = self._edges_of(k, self.get("CCER"))
        lv = self._ti1x if k == 1 else bool(self._ti_lvl.get(2))
        return False if (e == 3 and self._sms != 5) else lv ^ (e == 2)

    def _trgi_pulse(self) -> None:
        self._trgi_set(True)
        self._trgi_set(False)

    def _trgi_set(self, level: bool) -> None:
        """TRGI changed level: TRC captures, then the slave mode (RM0090 17.3.14/18.3.14): reset (100) and trigger
        (110) act on the rising edge, gated (101) runs the counter while TRGI is high, external clock mode 1 (111)
        counts rising edges.  TIF is set on the active edge (both edges in gated mode)."""
        if level == self._trgi:
            return
        self._trgi = level
        bit = 1 if level else 2
        if self._trc:
            for c in self.ch:
                if c.ic_src == "T" and c.ic_edges & bit:
                    c.ic_cnt += 1
                    if c.ic_cnt >= c.ic_div:
                        c.ic_cnt = 0
                        self._capture(c)
        sms = self._sms
        if sms < 4:
            return
        if sms == 5:                                     # gated: counter enable follows TRGI
            if self.get("CR1") & CEN and not self._frozen and not self._ext_mode():
                if level:
                    self._start(self._restart_pos())
                else:
                    self._hold()
            if self._mms == 1:
                self._trgo_set(self._cnt_en())
            self._set_tif()
            return
        if not level:
            return
        if sms == 4:                                     # reset: counter + prescaler re-initialised, update event
            self._reinit()
        elif sms == 6:                                   # trigger: the counter starts (CEN set by hardware)
            cr1 = self.get("CR1")
            if not cr1 & CEN:
                self.set("CR1", cr1 | CEN)
                if not self._ext_mode():
                    self._start(self._restart_pos())
                if self._mms == 1:
                    self._trgo_set(True)
        elif sms == 7 and not self._ece:                 # external clock mode 1: count TRGI rising edges
            self._ext_tick()
        self._set_tif()

    def _set_tif(self) -> None:
        self.set("SR", self.get("SR") | TIF)
        self._update_line()
        if self._dma_on and self.get("DIER") & TDE:
            _dma_request(self.chip, f"{self.name}_TRIG")

    def _ext_tick(self) -> None:
        """One external clock edge (external clock mode 1 on TRGI, mode 2 on ETRF): the prescaler counts it; each
        PSC+1 edges the counter counts (up, or down with DIR; edge-aligned), compare matches and overflows as with
        the internal clock."""
        cr1 = self.get("CR1")
        if not cr1 & CEN or self._frozen or (self._ece and self._sms == 5 and not self._trgi):
            return
        self._xpsc += 1
        if self._xpsc <= self.psc:
            return
        self._xpsc = 0
        cnt, arr = self.get("CNT") & self.cmask, self.arr
        down = bool(cr1 & DIR)
        ovf = False
        if down:
            if cnt == 0:
                cnt, ovf = arr, True
            else:
                cnt -= 1
        elif cnt == arr or cnt == self.cmask:
            cnt, ovf = 0, True
        else:
            cnt += 1
        self.set("CNT", cnt)
        uev = self._overflow() if ovf else False
        sr = self.get("SR")
        for c in self.ch:
            if c.ccs:
                continue
            mode = c.mode
            if mode >= 6:
                c.ref = self._pwm_ref(mode, cnt, down, c.ccr)
            if cnt == c.ccr:
                sr |= 1 << c.n
                if mode == 1:
                    c.ref = True
                elif mode == 2:
                    c.ref = False
                elif mode == 3:
                    c.ref = not c.ref
            self._oc_apply(c)
        if sr != self.get("SR"):
            self.set("SR", sr)
            self._update_line()
            for c in self.ch:
                if not c.ccs and cnt == c.ccr:
                    self._cc_event(c)
        if uev and cr1 & OPM:
            self.set("CR1", self.get("CR1") & ~CEN)
            if self._mms == 1:
                self._trgo_set(False)

    # ------------------------------------------------------------ master mode: TRGO, CCx events, DMA requests
    def add_trigger_listener(self, sig: str, cb) -> None:
        """Subscribe `cb(t_cycles, level)` to trigger signal `sig`: "TRGO" (CR2.MMS) or "CC1".."CC4" (the CCx event:
        OCxREF of an output channel, a pulse at each capture of an input channel).  A pulse is reported as
        cb(t, True) then cb(t, False).  Used by slave timers (ITRx) and the ADC external triggers."""
        sig = sig.upper()
        if sig == "TRGO":
            self.trgo_listeners.append(cb)
        else:
            n = int(sig.lstrip("CO").rstrip("REF") or 0)
            self._cc_lst.setdefault(n, []).append(cb)
        if self.ch and self.chip is not None:
            self._oc_config_changed()                    # watched channels need exact match events

    def remove_trigger_listener(self, sig: str, cb) -> None:
        sig = sig.upper()
        lst = self.trgo_listeners if sig == "TRGO" else self._cc_lst.get(int(sig.lstrip("CO").rstrip("REF") or 0), [])
        if cb in lst:
            lst.remove(cb)
            if self.ch and self.chip is not None:
                self._oc_config_changed()

    def trigger_level(self, sig: str) -> bool:
        """Current level of trigger signal `sig` ("TRGO", "CC1".."CC4")."""
        sig = sig.upper()
        if sig == "TRGO":
            return self._trgo_lvl
        n = int(sig.lstrip("CO").rstrip("REF") or 0)
        c = next((x for x in self.ch if x.n == n), None)
        return bool(c is not None and not c.ccs and c.ref)

    def _trgo_now(self) -> bool:
        mms = self._mms
        if mms == 1:
            return self._cnt_en()
        if mms >= 4:
            c = next((x for x in self.ch if x.n == mms - 3), None)
            return bool(c is not None and c.ref)
        return False

    def _trgo_set(self, level: bool) -> None:
        if level == self._trgo_lvl:
            return
        self._trgo_lvl = level
        if self.trgo_listeners:
            t = self._now()
            for cb in list(self.trgo_listeners):
                cb(t, level)

    def _trgo_pulse(self) -> None:
        t = self._now()
        for cb in list(self.trgo_listeners):
            cb(t, True)
            cb(t, False)

    def _ref_edge(self, c: "_Chan") -> None:
        """OCxREF of a watched channel changed: CCx listeners, TRGO = OCxREF (MMS=100-111)."""
        lst = self._cc_lst.get(c.n)
        if lst:
            t = self._now()
            for cb in list(lst):
                cb(t, c.ref)
        if self._mms == 3 + c.n:
            self._trgo_set(c.ref)

    def _cc_event(self, c: "_Chan") -> None:
        """CCxIF is being set (compare match, capture, EGR.CCxG): compare pulse on TRGO (MMS=011, channel 1), a pulse
        for the CCx listeners of an input channel, DMA request TIMx_CHn (CCxDE, CCDS=0)."""
        if c.n == 1 and self._mms == 3 and self.trgo_listeners:
            self._trgo_pulse()
        if c.ccs:
            lst = self._cc_lst.get(c.n)
            if lst:
                t = self._now()
                for cb in list(lst):
                    cb(t, True)
                    cb(t, False)
        if self._dma_on and self.get("DIER") >> (8 + c.n) & 1 and not self.get("CR2") & CCDS:
            _dma_request(self.chip, f"{self.name}_CH{c.n}")

    def _dma_update(self) -> None:
        """Update event with DMA enables: TIMx_UP (UDE); with CR2.CCDS the CCx requests (CCxDE) too."""
        dier = self.get("DIER")
        if dier & UDE:
            _dma_request(self.chip, f"{self.name}_UP")
        if dier & 0x1E00 and "CR2" in self.regs and self.get("CR2") & CCDS:
            for c in self.ch:
                if dier >> (8 + c.n) & 1:
                    _dma_request(self.chip, f"{self.name}_CH{c.n}")

    def _cr2_write(self, blk, reg, old, new):
        if self._lock() >= 1:            # LOCK level 1: OISx / OISxN are frozen (17.4.18)
            new = (new & ~0x7F00) | (old & 0x7F00)
        self._store(reg, new)
        self._mms = new >> 4 & 7
        if self.ch:
            self._oc_config_changed()                    # watched channels (MMS=011, 1xx), TI1S inputs
        self._trgo_set(self._trgo_now())
        return None

    # ------------------------------------------------------------ slave mode / encoder interface
    def _encoder(self) -> bool:
        return self._enc_ok and 1 <= self._sms <= 3

    def _smcr_write(self, blk, reg, old, new):
        self._store(reg, new)
        self._sms, self._ts = new & 7, new >> 4 & 7
        self._ece = bool(new & ECE) and self._enc_ok
        sms = self._sms
        if sms and sms < 4 and not self._enc_ok:
            self._warn_once(f"sms{sms}", f"{self.name}: SMCR.SMS={sms}: {self.name} has no encoder interface "
                            "(TIM1-5, TIM8 only); ignored")
        if (old ^ new) & (ECE | 7):
            self._xpsc = 0
        if self.ch:
            self._inputs_cfg()
        self._resume_or_hold()
        if self._mms == 1:
            self._trgo_set(self._cnt_en())
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
            up = (rising ^ p2) == (self._ti1x ^ p1)
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
        # LOCK (17.4.18): writable once after reset; level 1 freezes DTG, BKE, BKP, AOE, level 2 also OSSR, OSSI
        req = new
        frozen = 0x300 if self._bdtr_written else 0
        lvl = (old >> 8) & 3
        if lvl >= 1:
            frozen |= 0xFF | BKE | BKP | AOE
        if lvl >= 2:
            frozen |= OSSR | OSSI
        new = (new & ~frozen) | (old & frozen)
        self._bdtr_written = True
        if (req ^ new) & frozen:
            self._warn_once("lock", f"{self.name}: write to BDTR bits frozen by LOCK (level {lvl}) ignored")
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
