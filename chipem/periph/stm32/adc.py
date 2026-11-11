"""STM32F4 12-bit ADC (ADC1-3, RM0090 ch. 13) and the ADC common registers (C_ADC: CSR, CCR, CDR).

Regular group (registers from the SVD; behaviour hand-written):
* CR2.ADON powers the ADC (clearing it aborts a conversion).  SWSTART (ADON=1) or an external trigger starts the
  regular group; SWSTART reads 0 again at once (cleared when the conversion starts); SR.STRT is set.  A trigger while
  a conversion runs, while ADON=0, while SR.OVR is set or with the RCC clock (APB2ENR.ADCxEN) off is ignored.
* Sequence: SQR1.L + 1 channels SQ1..SQ16 with CR1.SCAN=1, SQ1 alone with SCAN=0.  Writing a different value to
  SQR1-3 during a regular conversion resets it and starts the new group from SQ1 (RM0090 13.3.3; with the regular
  trigger latency; JSQR writes during an injected conversion do not restart it).  CR2.CONT restarts the sequence
  at its end (back to back, no trigger latency).  CR1.DISCEN: each trigger converts DISCNUM + 1 channels of the
  sequence, continuing where the previous subgroup stopped (the last subgroup ends at the end of the sequence).
* Timing (ADCCLK = PCLK2 / CCR.ADCPRE (2, 4, 6, 8), counted in core cycles; one scheduler event per conversion,
  `sched.at_cycles`): a conversion takes SMPx sampling cycles (SMPR1/SMPR2: 3, 15, 28, 56, 84, 112, 144, 480) plus
  12/10/8/6 ADCCLK (CR1.RES 12/10/8/6 bits).  The first conversion after a trigger starts after the regular trigger
  latency tlatr = 2 ADCCLK, plus 1 PCLK2 cycle for an external trigger (DS8626 Table 67 maxima, note 7); the next
  ones follow back to back.  Clock changes during a conversion do not re-time it; tSTAB after ADON is not modelled.
* Result: the channel value is taken at the end of the conversion; code = floor(V / VREF+ x 4096) clipped to
  0..4095 (VREF+ = `params.vref`, 3.3 V), then shifted to RES bits; CR2.ALIGN right (bits RES-1..0) or left (bits
  15..16-RES; 6-bit: bits 7..2).  DR holds it; SR.EOC is set after every conversion with CR2.EOCS=1, at the end of
  the sequence with EOCS=0.  Reading DR clears EOC.  SR bits are rc_w0.
* Overrun (RM0090 13.8.1): only with CR2.DMA=1 or EOCS=1, a conversion that ends while the previous DR value was not
  read sets SR.OVR (DR takes the new value), aborts the sequence (CONT stops) and further regular triggers are ignored
  until OVR is cleared.
* DMA (CR2.DMA): a request "ADCn" after every regular conversion via `_util.dma_request` (when the DMA model provides
  it); the DMA reads DR (clearing EOC).  CR2.DDS (RM0090 13.8.1, 13.13.3): with DDS = 0 the ADC issues no request
  after the DMA stream's last transfer (NDTR reached 0, also in circular mode) until CR2.DMA is written 0 then 1;
  DDS = 1 keeps requesting (circular / double-buffer DMA).  Overrun detection stays on while DMA = 1 (SR.OVR, RM0090
  13.13.1), so with DDS = 0 and CONT the next unread conversion still sets OVR.
* Analog watchdog (CR1.AWDEN, regular channels; AWDSGL + AWDCH for a single channel): the 12-bit-scaled code
  outside [LTR, HTR] sets SR.AWD.  JAWDEN (injected) is not modelled.
* Interrupt: one NVIC line "ADC" shared by ADC1-3 (the OR of all three): EOC & EOCIE, AWD & AWDIE, OVR & OVRIE,
  JEOC & JEOCIE.  Level, re-evaluated on every flag change.
* External triggers (CR2.EXTEN 01 rising / 10 falling / 11 both, EXTSEL per RM0090 Table 68): TIM1_CC1, TIM1_CC2,
  TIM1_CC3, TIM2_CC2, TIM2_CC3, TIM2_CC4, TIM2_TRGO, TIM3_CC1, TIM3_TRGO, TIM4_CC4, TIM5_CC1, TIM5_CC2, TIM5_CC3,
  TIM8_CC1, TIM8_TRGO, EXTI line 11.  The ADC subscribes to the timer's trigger signal (tim.py
  `add_trigger_listener`) while ADON=1 and EXTEN != 0: TIMx_CCy is OCyREF of an output channel (so PWM/toggle modes
  trigger, frozen mode does not) or a pulse per capture; TRGO follows TIMx_CR2.MMS.  EXTI11 is not modelled (one
  `warn`).
* Inputs: `set_channel(name, value)`; value float = volts, int = raw 12-bit code (stored as the matching voltage).
  Names: "IN5" / "5" / "ADC1_IN5" (this ADC's channel 5), the pin table name of a shared input ("ADC123_IN0"), a
  pin ("PA0").  Values belong to the pin: PA0 = ADC123_IN0 feeds channel 0 of all three ADCs.  A channel without a
  value reads its pin's digital level (H = VREF+, L = 0 V; floating = `params.default`, 0 V); a channel whose pin is
  not bonded on the package is stored under "ADCn_INm".  ADC1 internal channels (CCR.TSVREFE / VBATE; 0 V while
  disabled): IN16 temperature sensor V25 + Avg_Slope x (T - 25 degC) = 0.76 V + 2.5 mV/degC (T: "TEMP", degC,
  default 25), IN17 VREFINT 1.21 V, IN18 VBAT / 2 ("VBAT", volts, default 3.3) (DS8626 Tables 69-71).  On ADC2/3
  channels 16-18 read 0.

Injected group (RM0090 §13.3.3, §13.3.9, §13.3.10, §13.13.9-13.13.13):
* JSQR.JL + 1 channels; the sequence is the last JL + 1 of JSQ1..JSQ4 (JL = 0: JSQ4 alone, JL = 3: JSQ1..JSQ4), the
  i-th converted channel goes to JDR(i).  With CR1.SCAN = 0 only the first one is converted (JDR1).
* Triggers: CR2.JSWSTART (ADON = 1; reads 0 again at once) or JEXTEN/JEXTSEL (RM0090 Table 69, same timer hooks as the
  regular triggers): TIM1_CC4, TIM1_TRGO, TIM2_CC1, TIM2_TRGO, TIM3_CC2, TIM3_CC4, TIM4_CC1, TIM4_CC2, TIM4_CC3,
  TIM4_TRGO, TIM5_CC4, TIM5_TRGO, TIM8_CC2, TIM8_CC3, TIM8_CC4, EXTI line 15 (not modelled: one `warn`).  Injection
  trigger latency 3 ADCCLK (DS8626 Table 67 tlatrinj), plus 1 PCLK2 for an external trigger.  SR.JSTRT is set; a
  trigger while an injected sequence runs is ignored.
* Triggered injection (§13.3.9): an injected trigger during a regular conversion aborts that conversion; after the
  injected sequence the regular sequence resumes with the interrupted channel.  A regular trigger arriving during
  an injected sequence is kept and starts the regular group once the injected sequence is over.
* Auto-injection (CR1.JAUTO): the injected group is converted right after the regular group (back to back); with CONT
  the regular group then restarts.  Injected external triggers are ignored with JAUTO (one `warn`).
* CR1.JDISCEN: each trigger converts the next injected channel; JEOC after the last one of the sequence.  JAUTO with
  JDISCEN, or DISCEN with JDISCEN, is not allowed by the RM (one `warn`; JAUTO wins).
* Result: (code at RES bits) - JOFRx, signed: right alignment sign-extends to 16 bits (SEXT in bits 15..RES); left
  alignment shifts one bit less than the regular data (12-bit: SEXT D11..D0 000), bit 15 = sign.  JEOC at the end of
  the injected sequence (no EOC); JDRx reads do not clear it (rc_w0 like the other SR bits).  CR1.JAWDEN applies the
  analog watchdog (AWDSGL/AWDCH as for regular channels) to injected conversions.  No DMA, no overrun.

Multi-ADC mode (C_ADC CCR.MULTI, §13.9): ADC1 is the master; its triggers start the slaves and the slaves' own triggers
(SWSTART, JSWSTART, external) are ignored (one `warn`) for the group the multi mode synchronises.
* Regular simultaneous (MULTI 00110 / 10110, and the combined 00001/00010/10001/10010): a regular trigger of ADC1
  starts the regular groups of ADC1, ADC2 (and ADC3) at the same time.
* Interleaved (00111 / 10111): ADC2 (ADC3) starts its conversion 5 + CCR.DELAY ADCCLK (2 x for ADC3) after ADC1; with
  CONT each ADC restarts at its own end (the phase offset is kept).
* Injected simultaneous (00101 / 10101, and 00001 / 10001): an injected trigger of ADC1 starts the injected groups of
  all participating ADCs.
* Alternate trigger (01001 / 11001, and 00010 / 10010): successive injected triggers of ADC1 convert the injected
  group of ADC1, then ADC2 (then ADC3), then ADC1 again.
* Data (CCR.DMA, §13.9.1): mode 1: each regular conversion result (ADC1, ADC2, ADC3 order) is one half-word item;
  mode 2: two consecutive half-words packed per 32-bit item (dual: ADC2:ADC1; triple: ADC2:ADC1, ADC1:ADC3,
  ADC3:ADC2); mode 3: two bytes packed per half-word (6/8-bit resolution).  Items queue in the common block; while one
  is available the DMA request "ADC1" is asserted and reading CDR returns (and removes) the oldest one (the DMA stream
  of ADC1 reads CDR).  A new result while a whole round of items is still unread (dual mode 2/3: one item; mode 1: one
  item per ADC) sets the ADCs' OVR and stops their regular conversions (as for the single-ADC DMA overrun).  CCR.DMA = 0: CDR = ADC2 DR : ADC1 DR (no request).  With CCR.DMA != 0
  the individual ADCs' CR2.DMA requests are not used.  CCR.DDS = 0: after the DMA's last transfer no item is queued and
  no request issued until CCR.DMA is cleared and set again (RM0090 13.13.16).
* Other MULTI values are reserved: one `warn`, the ADCs convert independently.  The RM's requirement that
  simultaneous groups have equal conversion times is not checked.
Not modelled: CCR.DELAY in modes other than interleaved, the sampling capacitor / source impedance, ADCCLK phase.

C_ADC (`stm32.adc_common`): CSR mirrors the SR flags of ADC1 (bits 0-5), ADC2 (8-13) and ADC3 (16-21); CCR holds
ADCPRE, TSVREFE, VBATE, MULTI, DELAY, DMA, DDS; CDR is the multi-mode data register.
"""
from __future__ import annotations

import re
from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ._util import GatedBlock, apb_hz, build_regs, find_model, irq_number
from .tim import _dma_request

# SR
AWD, EOC, JEOC, JSTRT, STRT, OVR = 1, 2, 4, 8, 16, 32
# CR1
EOCIE, AWDIE, JEOCIE, SCAN, AWDSGL, JAUTO, DISCEN = 1 << 5, 1 << 6, 1 << 7, 1 << 8, 1 << 9, 1 << 10, 1 << 11
JDISCEN, JAWDEN, AWDEN, OVRIE = 1 << 12, 1 << 22, 1 << 23, 1 << 26
# CR2
ADON, CONT, DMA, DDS, EOCS, ALIGN, JSWSTART, SWSTART = 1, 2, 1 << 8, 1 << 9, 1 << 10, 1 << 11, 1 << 22, 1 << 30
JEXTEN_MASK = 3 << 20
# CCR (common)
TSVREFE, VBATE = 1 << 23, 1 << 22

SMP_CYCLES = (3, 15, 28, 56, 84, 112, 144, 480)
RES_BITS = (12, 10, 8, 6)
# RM0090 Rev 22 Table 68: EXTSEL -> (block, trigger signal)
TRIGGERS = (("TIM1", "CC1"), ("TIM1", "CC2"), ("TIM1", "CC3"), ("TIM2", "CC2"), ("TIM2", "CC3"), ("TIM2", "CC4"),
            ("TIM2", "TRGO"), ("TIM3", "CC1"), ("TIM3", "TRGO"), ("TIM4", "CC4"), ("TIM5", "CC1"), ("TIM5", "CC2"),
            ("TIM5", "CC3"), ("TIM8", "CC1"), ("TIM8", "TRGO"), ("EXTI", "11"))
# RM0090 Rev 22 Table 69: JEXTSEL -> (block, trigger signal)
JTRIGGERS = (("TIM1", "CC4"), ("TIM1", "TRGO"), ("TIM2", "CC1"), ("TIM2", "TRGO"), ("TIM3", "CC2"), ("TIM3", "CC4"),
             ("TIM4", "CC1"), ("TIM4", "CC2"), ("TIM4", "CC3"), ("TIM4", "TRGO"), ("TIM5", "CC4"), ("TIM5", "TRGO"),
             ("TIM8", "CC2"), ("TIM8", "CC3"), ("TIM8", "CC4"), ("EXTI", "15"))
TLATINJ_ADCCLK = 3                   # DS8626 Table 67: injection trigger conversion latency
# CCR.MULTI -> (ADCs, regular mode, injected mode); regular "sim" | "int" | None, injected "sim" | "alt" | None
MULTI_MODES = {0b00001: (2, "sim", "sim"), 0b00010: (2, "sim", "alt"), 0b00101: (2, None, "sim"),
               0b00110: (2, "sim", None), 0b00111: (2, "int", None), 0b01001: (2, None, "alt"),
               0b10001: (3, "sim", "sim"), 0b10010: (3, "sim", "alt"), 0b10101: (3, None, "sim"),
               0b10110: (3, "sim", None), 0b10111: (3, "int", None), 0b11001: (3, None, "alt")}
TLATR_ADCCLK = 2                     # DS8626 Table 67: regular trigger conversion latency (max), + 1/fPCLK2 external
VREFINT_V, V25, AVG_SLOPE, VBAT_DIV = 1.21, 0.76, 0.0025, 2
INTERNAL = {16: "TEMP", 17: "VREFINT", 18: "VBAT"}
_PIN_FN = re.compile(r"ADC(\d+)_IN(\d+)$")
_CH_NAME = re.compile(r"^(?:ADC(\d+)_)?(?:IN)?(\d+)$")
_INT_NAME = re.compile(r"^(?:ADC\d*_)?(TEMP|TEMPSENSOR|TS|VREFINT|VBAT)$")


def _store(chip: Any) -> dict:
    """Per-chip analog input values (pin id / "ADCn_INm" -> volts, "TEMP" -> degC, "VBAT" -> volts)."""
    d = getattr(chip, "_stm32_analog", None)
    if d is None:
        d = {}
        try:
            chip._stm32_analog = d
        except AttributeError:
            pass
    return d


@register("stm32.adc")
class ADC(GatedBlock):
    adc_volts = True                 # set_channel takes volts (float); Session passes "1.65" / "1.65V" as float

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        m = re.search(r"(\d+)$", name)
        self.index = int(params.get("index", m.group(1) if m else 1))
        self.gate = f"ADC{self.index}"
        ov: dict[str, dict] = {
            "SR": {"on_write": self._sr_write},
            "CR1": {"on_write": self._cr1_write},
            "CR2": {"on_write": self._cr2_write},
            "SQR1": {"on_write": self._sqr_write}, "SQR2": {"on_write": self._sqr_write},
            "SQR3": {"on_write": self._sqr_write},
            "DR": {"on_read": self._dr_read, "ro_mask": 0xFFFFFFFF},
        }
        for k in range(1, 5):
            ov[f"JDR{k}"] = {"ro_mask": 0xFFFFFFFF}
        r = build_regs((name, f"ADC{self.index}", "ADC1"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.vref = float(params.get("vref", 3.3))
        self.default = float(params.get("default", 0.0))
        self._ev = None
        self._pos = 0                # next position in the regular sequence
        self._burst: int | None = None   # discontinuous mode: conversions left in this subgroup
        self._sub: tuple | None = None   # external trigger subscription (block, signal, callback)
        self._sub_src: tuple | None = None
        self._trig_lvl = False
        self._unread = False         # DR written by a conversion and not read yet (overrun detection)
        self._irqn: int | None = None
        self._line = False
        self._common: Any = None
        self._ch_key: dict[int, str] = {}    # channel 0-15 -> pin id
        self._ch_pin: dict[int, Any] = {}
        self._warned: set[str] = set()
        self.conversions = 0
        self.vals: dict = {}
        self._dma_req = False
        self._dds_stop = False       # DDS = 0 and the DMA did its last transfer: no more requests (RM0090 13.8.1)
        self._jev = None             # injected conversion in progress
        self._jpos = 0               # next position in the injected sequence (JDISCEN)
        self._jsub: tuple | None = None
        self._jsub_src: tuple | None = None
        self._jtrig_lvl = False
        self._reg_resume = False     # a regular conversion was interrupted by the injected group
        self._reg_pending: bool | None = None   # regular trigger (external?) waiting for the injected group
        self._auto_inj = False       # the running injected group was started by JAUTO
        self.injected_conversions = 0

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, str(self.params.get("irq", "ADC")))
        self.vals = _store(chip)
        board = getattr(chip, "pins", None)
        for p in (board.pins if board is not None else ()):
            fn = (p.functions or {}).get("ADC")
            mm = _PIN_FN.match(str(fn or ""))
            if mm and str(self.index) in mm.group(1):
                n = int(mm.group(2))
                self._ch_key[n] = p.id
                self._ch_pin[n] = p

    def reset(self) -> None:
        if getattr(self, "_ev", None) is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None
        if getattr(self, "_jev", None) is not None and self.chip is not None:
            self.chip.sched.cancel(self._jev)
        self._jev = None
        if getattr(self, "_sub", None) is not None:
            self._unsubscribe()
        self._sub_src = None
        if getattr(self, "_jsub", None) is not None:
            self._unsubscribe(inj=True)
        self._jsub_src = None
        self._jpos, self._reg_resume, self._reg_pending, self._auto_inj, self._jtrig_lvl = 0, False, None, False, False
        self._pos, self._burst, self._unread, self._trig_lvl = 0, None, False, False
        if getattr(self, "_dma_req", False):
            _dma_request(self.chip, f"ADC{self.index}", False)
        self._dma_req = False
        self._dds_stop = False
        super().reset()
        if self.chip is not None:
            self._update_irq()

    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()                          # one `warn` when accessed with the clock off
        MMIOBlock.write(self, offset, size, value)

    # ------------------------------------------------------------ stimulus
    def set_channel(self, ch: int | str, value: int | float) -> bool:
        """Set an analog input: float = volts, int = raw 12-bit code ("TEMP": degC, "VBAT": volts).  False if the
        name is another ADC's channel ("ADC2_IN3" on ADC1); ValueError for an unknown name."""
        key = self._key(ch)
        if key is None:
            return False
        if key in ("TEMP", "VBAT"):
            self.vals[key] = float(value)
        elif isinstance(value, float):
            self.vals[key] = value
        else:
            self.vals[key] = (max(0, min(int(value), 4095)) + 0.5) * self.vref / 4096
        return True

    def set_input(self, name: str, value: int | float) -> bool:
        """Session fallback for names that are not "<block>_<channel>": a pin ("PA0") or a shared pin-table
        name ("ADC123_IN0").  True when handled."""
        try:
            return self.set_channel(name, value)
        except ValueError:
            return False

    def _key(self, ch: int | str) -> str | None:
        if isinstance(ch, int):
            return self._chan_key(ch)
        s = str(ch).strip().upper()
        m = _CH_NAME.match(s)
        if m:
            if m.group(1) is not None and str(self.index) not in m.group(1):
                return None
            return self._chan_key(int(m.group(2)))
        m = _INT_NAME.match(s)
        if m:
            k = m.group(1)
            return "TEMP" if k in ("TEMPSENSOR", "TS") else k
        board = getattr(self.chip, "pins", None)
        if board is not None:
            for p in board.pins:
                if s == p.id.upper() or s in (a.upper() for a in p.aliases):
                    if _PIN_FN.match(str((p.functions or {}).get("ADC") or "")):
                        return p.id
                    raise ValueError(f"pin {p.id} has no ADC input")
        raise ValueError(f"bad ADC channel name {ch!r}")

    def _chan_key(self, n: int) -> str:
        if n in INTERNAL:
            return INTERNAL[n]
        if not 0 <= n <= 15:
            raise ValueError(f"ADC channel {n} out of range (0-18)")
        return self._ch_key.get(n) or f"ADC{self.index}_IN{n}"

    def volts(self, ch: int) -> float:
        """Input voltage of channel `ch` now."""
        if ch >= 16:
            if self.index != 1:
                return 0.0
            ccr = self._ccr()
            if ch == 16:
                return V25 + AVG_SLOPE * (self.vals.get("TEMP", 25.0) - 25.0) if ccr & TSVREFE else 0.0
            if ch == 17:
                return self.vals.get("VREFINT", VREFINT_V) if ccr & TSVREFE else 0.0
            return self.vals.get("VBAT", 3.3) / VBAT_DIV if ccr & VBATE else 0.0
        key = self._ch_key.get(ch) or f"ADC{self.index}_IN{ch}"
        v = self.vals.get(key)
        if v is not None:
            return v
        p = self._ch_pin.get(ch)
        if p is not None:
            lvl = p.level()
            if lvl == "H":
                return self.vref
            if lvl == "L":
                return 0.0
        return self.default

    def code12(self, ch: int) -> int:
        return max(0, min(int(self.volts(ch) / self.vref * 4096), 4095))

    # ------------------------------------------------------------ configuration
    def _ccr(self) -> int:
        c = self._common
        if c is None:
            c = self._common = find_model(self.chip, "stm32.adc_common")
            if c is None:
                return 0
        return c.get("CCR")

    def adcclk_cycles(self) -> int:
        """Core cycles per ADCCLK: PCLK2 / (2, 4, 6, 8) by CCR.ADCPRE."""
        pre = 2 * ((self._ccr() >> 16 & 3) + 1)
        return max(1, round(self.chip.clock.hz / apb_hz(self.chip, 2) * pre))

    def pclk2_cycles(self) -> int:
        return max(1, round(self.chip.clock.hz / apb_hz(self.chip, 2)))

    def resolution(self) -> int:
        return RES_BITS[self.get("CR1") >> 24 & 3]

    def sample_cycles(self, ch: int) -> int:
        if ch <= 9:
            f = self.get("SMPR2") >> (3 * ch) & 7
        else:
            f = self.get("SMPR1") >> (3 * (ch - 10)) & 7
        return SMP_CYCLES[f]

    def conv_cycles(self, ch: int) -> int:
        """Conversion time of channel `ch` in core cycles: (sampling + RES) ADCCLK."""
        return (self.sample_cycles(ch) + self.resolution()) * self.adcclk_cycles()

    def sequence(self) -> list[int]:
        """Channels of the regular sequence (SQ1..SQ(L+1) with SCAN or DISCEN, SQ1 alone otherwise)."""
        cr1 = self.get("CR1")
        n = (self.get("SQR1") >> 20 & 0xF) + 1 if cr1 & (SCAN | DISCEN) else 1
        regs = (self.get("SQR3"), self.get("SQR2"), self.get("SQR1"))
        return [regs[i // 6] >> (5 * (i % 6)) & 0x1F for i in range(n)]

    def jsequence(self) -> list[int]:
        """Channels of the injected sequence: the last JL + 1 of JSQ1..JSQ4 (only the first one without SCAN, JAUTO or
        JDISCEN)."""
        jsqr = self.get("JSQR")
        jl = jsqr >> 20 & 3
        seq = [jsqr >> (5 * k) & 0x1F for k in range(3 - jl, 4)]
        return seq if self.get("CR1") & (SCAN | JAUTO | JDISCEN) else seq[:1]

    def _clk_on(self) -> bool:
        loc = self._gate_loc
        if loc is None or self._rcc is None:
            return True
        return bool(self._rcc.mem[loc[0]] & loc[1])

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    # ------------------------------------------------------------ conversions
    def _trigger(self, t: int, external: bool) -> bool:
        """Start the regular group at core cycle `t` (the trigger)."""
        if self.chip is None or not self.get("CR2") & ADON or self._ev is not None or self.get("SR") & OVR:
            return False
        if not self._clk_on():
            self._warn_once("gate", f"{self.name}: conversion trigger with the clock off (RCC APB2ENR.ADC"
                                    f"{self.index}EN=0): ignored")
            return False
        self.set("SR", self.get("SR") | STRT)
        if self._jev is not None:                    # injected sequence running: the regular group waits for it
            self._reg_pending = external
            return True
        cr1 = self.get("CR1")
        if cr1 & DISCEN:
            self._burst = (cr1 >> 13 & 7) + 1
        else:
            self._burst = None
            self._pos = 0
        seq = self.sequence()
        if self._pos >= len(seq):
            self._pos = 0
        lat = TLATR_ADCCLK * self.adcclk_cycles() + (self.pclk2_cycles() if external else 0)
        self._ev = self.chip.sched.at_cycles(t + lat + self.conv_cycles(seq[self._pos]), self._done)
        return True

    def _done(self) -> None:
        self._ev = None
        chip = self.chip
        seq = self.sequence()
        pos = self._pos if self._pos < len(seq) else 0
        ch = seq[pos]
        last = pos + 1 >= len(seq)
        cr1, cr2 = self.get("CR1"), self.get("CR2")
        bits = RES_BITS[cr1 >> 24 & 3]
        c12 = self.code12(ch)
        val = c12 >> (12 - bits)
        if cr2 & ALIGN:
            val = val << 2 if bits == 6 else val << (16 - bits)
        sr = self.get("SR")
        if self._unread and cr2 & (DMA | EOCS):
            self.set("DR", val)
            self.set("SR", sr | OVR)
            self._pos, self._burst = 0, None
            self.emit("info", block=self.name, msg=f"{self.name}: overrun (DR not read), regular conversions stopped")
            self._update_irq()
            return
        self.set("DR", val)
        self._unread = True
        self.conversions += 1
        if cr1 & AWDEN and (not cr1 & AWDSGL or (cr1 & 0x1F) == ch):
            c = (c12 >> (12 - bits)) << (12 - bits)
            if c > (self.get("HTR") & 0xFFF) or c < (self.get("LTR") & 0xFFF):
                sr |= AWD
        if cr2 & EOCS or last:
            sr |= EOC
        self.set("SR", sr)
        self._pos = 0 if last else pos + 1
        self._update_irq()
        com = self._com()
        if com is not None and com.multi_dma(self):
            com.push(self, val)
        elif cr2 & DMA and not self._dds_stop:
            self._dma_req = True
            _dma_request(chip, f"ADC{self.index}")
        if self.get("SR") & OVR:                     # multi-mode DMA overrun (set by the common block)
            return
        now = chip.now_cycles()
        if last and cr1 & JAUTO and not (self._burst is not None and self._burst > 1):
            self._burst = None
            self._start_inj(now, auto=True)          # auto-injection right after the regular group
            return
        if self._burst is not None:
            self._burst -= 1
            if self._burst > 0 and not last:
                self._ev = chip.sched.at_cycles(now + self.conv_cycles(seq[self._pos]), self._done)
            else:
                self._burst = None
            return
        if not last:
            self._ev = chip.sched.at_cycles(now + self.conv_cycles(seq[self._pos]), self._done)
        elif cr2 & CONT:
            self._ev = chip.sched.at_cycles(now + self.conv_cycles(seq[0]), self._done)

    # ------------------------------------------------------------ injected group
    def _com(self) -> Any:
        if self._common is None and self.chip is not None:
            self._common = find_model(self.chip, "stm32.adc_common")
        return self._common

    def _jtrigger(self, t: int, external: bool) -> bool:
        """Start the injected group (or its next channel with JDISCEN) at core cycle `t` (the trigger)."""
        if self.chip is None or not self.get("CR2") & ADON or self._jev is not None:
            return False
        if not self._clk_on():
            self._warn_once("gate", f"{self.name}: conversion trigger with the clock off (RCC APB2ENR.ADC"
                                    f"{self.index}EN=0): ignored")
            return False
        lat = TLATINJ_ADCCLK * self.adcclk_cycles() + (self.pclk2_cycles() if external else 0)
        return self._start_inj(t + lat, auto=False)

    def _start_inj(self, t: int, auto: bool) -> bool:
        cr1 = self.get("CR1")
        if cr1 & JDISCEN and (cr1 & (JAUTO | DISCEN)):
            self._warn_once("jdisc", f"{self.name}: JDISCEN together with JAUTO or DISCEN is not allowed (RM0090 "
                                     "13.3.10); JDISCEN ignored with JAUTO")
        if self._ev is not None:                     # triggered injection: the regular conversion is reset
            self.chip.sched.cancel(self._ev)
            self._ev = None
            self._reg_resume = True
        self._auto_inj = auto
        self.set("SR", self.get("SR") | JSTRT)
        seq = self.jsequence()
        if self._jpos >= len(seq) or not (cr1 & JDISCEN and not cr1 & JAUTO):
            self._jpos = 0
        self._jev = self.chip.sched.at_cycles(t + self.conv_cycles(seq[self._jpos]), self._jdone)
        return True

    def _jdone(self) -> None:
        self._jev = None
        chip = self.chip
        seq = self.jsequence()
        pos = self._jpos if self._jpos < len(seq) else 0
        ch = seq[pos]
        cr1, cr2 = self.get("CR1"), self.get("CR2")
        bits = RES_BITS[cr1 >> 24 & 3]
        c12 = self.code12(ch)
        d = (c12 >> (12 - bits)) - (self.get(f"JOFR{pos + 1}") & 0xFFF)
        if cr2 & ALIGN:
            val = (d << (1 if bits == 6 else 15 - bits)) & 0xFFFF
        else:
            val = d & 0xFFFF
        self.set(f"JDR{pos + 1}", val)
        self.injected_conversions += 1
        sr = self.get("SR")
        if cr1 & JAWDEN and (not cr1 & AWDSGL or (cr1 & 0x1F) == ch):
            c = (c12 >> (12 - bits)) << (12 - bits)
            if c > (self.get("HTR") & 0xFFF) or c < (self.get("LTR") & 0xFFF):
                sr |= AWD
        last = pos + 1 >= len(seq)
        self._jpos = 0 if last else pos + 1
        if last:
            sr |= JEOC
        self.set("SR", sr)
        self._update_irq()
        now = chip.now_cycles()
        if not last and not (cr1 & JDISCEN and not cr1 & JAUTO):
            self._jev = chip.sched.at_cycles(now + self.conv_cycles(seq[self._jpos]), self._jdone)
            return
        # injected burst over: give the converter back to the regular group
        auto, self._auto_inj = self._auto_inj, False
        if auto:
            if cr2 & CONT and cr2 & ADON:
                self._pos = 0
                self._ev = chip.sched.at_cycles(now + self.conv_cycles(self.sequence()[0]), self._done)
            return
        if self._reg_resume:
            self._reg_resume = False
            rseq = self.sequence()
            if self._pos >= len(rseq):
                self._pos = 0
            self._ev = chip.sched.at_cycles(now + self.conv_cycles(rseq[self._pos]), self._done)
            if self._reg_pending is None:
                return
        if self._reg_pending is not None and self._ev is None:
            ext, self._reg_pending = self._reg_pending, None
            self._trigger(now, ext)
        self._reg_pending = None

    def _start_regular(self, t: int, external: bool) -> None:
        com = self._com()
        group = com.regular_group(self) if com is not None else None
        if group is None:
            self._trigger(t, external)
            return
        if not group:
            self._warn_once("slave_reg", f"{self.name}: regular trigger ignored: multi-ADC mode (CCR.MULTI) - the "
                                         "regular conversions of the slaves are started by ADC1")
            return
        step = com.interleave_delay() if com.regular_mode() == "int" else 0
        for k, adc in enumerate(group):
            adc._trigger(t + k * step * adc.adcclk_cycles(), external)

    def _start_injected(self, t: int, external: bool) -> None:
        cr1 = self.get("CR1")
        if cr1 & JAUTO:
            self._warn_once("jauto_trig", f"{self.name}: injected trigger ignored with CR1.JAUTO = 1 (auto-injection "
                                          "follows the regular group)")
            return
        com = self._com()
        group = com.injected_group(self) if com is not None else None
        if group is None:
            self._jtrigger(t, external)
            return
        if not group:
            self._warn_once("slave_inj", f"{self.name}: injected trigger ignored: multi-ADC mode (CCR.MULTI) - the "
                                         "injected conversions of the slaves are started by ADC1")
            return
        for adc in group:
            adc._jtrigger(t, external)

    def _update_irq(self) -> None:
        if self.chip is None or self._irqn is None:
            return
        sr, cr1 = self.get("SR"), self.get("CR1")
        on = bool((sr & EOC and cr1 & EOCIE) or (sr & AWD and cr1 & AWDIE) or (sr & JEOC and cr1 & JEOCIE)
                  or (sr & OVR and cr1 & OVRIE))
        if on == self._line:
            return
        self._line = on
        shared = getattr(self.chip, "_stm32_adc_lines", None)
        if shared is None:
            shared = set()
            try:
                self.chip._stm32_adc_lines = shared
            except AttributeError:
                pass
        if on:
            shared.add(self.name)
        else:
            shared.discard(self.name)
        self.irq(self._irqn, bool(shared))

    # ------------------------------------------------------------ external triggers
    def _resubscribe(self) -> None:
        cr2 = self.get("CR2")
        on = cr2 & ADON and self.chip is not None
        self._subscribe(TRIGGERS[cr2 >> 24 & 0xF] if on and (cr2 >> 28 & 3) else None, inj=False)
        self._subscribe(JTRIGGERS[cr2 >> 16 & 0xF] if on and (cr2 >> 20 & 3) else None, inj=True)

    def _subscribe(self, want: tuple | None, inj: bool) -> None:
        if want == (self._jsub_src if inj else self._sub_src):
            return
        if (self._jsub if inj else self._sub) is not None:
            self._unsubscribe(inj)
        if inj:
            self._jsub_src = want
        else:
            self._sub_src = want
        if want is None:
            return
        blk_name, sig = want
        blk = self.chip.peripherals.get(blk_name)
        if blk_name == "EXTI" or not hasattr(blk, "add_trigger_listener"):
            sel = self.get("CR2") >> (16 if inj else 24) & 0xF
            self._warn_once(f"trig{blk_name}{sig}", f"{self.name}: external {'injected ' if inj else ''}trigger "
                            f"{blk_name}_{sig} ({'J' if inj else ''}EXTSEL={sel}) is not modelled: no conversions are "
                            "triggered")
            return
        cb = self._ext_jtrig if inj else self._ext_trig
        blk.add_trigger_listener(sig, cb)
        if inj:
            self._jsub = (blk, sig, cb)
            self._jtrig_lvl = bool(blk.trigger_level(sig))
        else:
            self._sub = (blk, sig, cb)
            self._trig_lvl = bool(blk.trigger_level(sig))

    def _unsubscribe(self, inj: bool = False) -> None:
        blk, sig, cb = self._jsub if inj else self._sub
        if inj:
            self._jsub = None
        else:
            self._sub = None
        blk.remove_trigger_listener(sig, cb)

    def _ext_trig(self, t: int, level: bool) -> None:
        if level == self._trig_lvl:
            return
        self._trig_lvl = level
        exten = self.get("CR2") >> 28 & 3
        if exten & (1 if level else 2) and not self.lp_frozen:     # Stop: no ADC clock, the trigger is missed
            self._start_regular(t, external=True)

    def _ext_jtrig(self, t: int, level: bool) -> None:
        if level == self._jtrig_lvl:
            return
        self._jtrig_lvl = level
        jexten = self.get("CR2") >> 20 & 3
        if jexten & (1 if level else 2) and not self.lp_frozen:
            self._start_injected(t, external=True)

    lp_frozen: Any = False              # PWR freezer: Stop / gated Sleep (a conversion in progress is held)

    # ------------------------------------------------------------ register hooks
    def _sr_write(self, blk, reg, old, new):
        v = (old & new & 0x3F) | (old & ~0x3F)        # rc_w0
        self._store(reg, v)
        self._update_irq()
        return v

    def _cr1_write(self, blk, reg, old, new):
        self._store(reg, new)
        self._update_irq()
        return None

    def _cr2_write(self, blk, reg, old, new):
        v = new & ~(SWSTART | JSWSTART)
        self._store(reg, v)
        if not new & DMA:
            self._dds_stop = False                   # DMA written to 0 (then 1): requests re-armed (RM0090 13.8.1)
        if (old ^ new) & ADON and not new & ADON:     # power down: the running conversions are lost
            for attr in ("_ev", "_jev"):
                ev = getattr(self, attr)
                if ev is not None:
                    self.chip.sched.cancel(ev)
                    setattr(self, attr, None)
            self._pos, self._burst, self._jpos = 0, None, 0
            self._reg_resume, self._reg_pending, self._auto_inj = False, None, False
        self._resubscribe()
        if new & JSWSTART and new & ADON and self.chip is not None:
            self._start_injected(self.chip.now_cycles(), external=False)
        if new & SWSTART and new & ADON and self.chip is not None:
            self._start_regular(self.chip.now_cycles(), external=False)
        return v

    def _sqr_write(self, blk, reg, old, new):
        """RM0090 13.3.3: modifying ADC_SQRx during a (regular) conversion resets it and restarts the new group."""
        self._store(reg, new)
        if new != old and self._ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
            self._ev = None
            self._pos, self._burst = 0, None
            self._trigger(self.chip.now_cycles(), external=False)
        return None

    def dma_transfer_complete(self, names: tuple) -> None:
        """Called by stm32.dma when a stream serving `names` reaches NDTR = 0 (its last transfer).  CR2.DDS = 0: the ADC
        issues no further DMA request until CR2.DMA is written to 0 and back to 1 (RM0090 13.8.1, 13.13.3)."""
        cr2 = self.get("CR2")
        if f"ADC{self.index}" in names and cr2 & DMA and not cr2 & DDS:
            self._dds_stop = True
            if self._dma_req:
                self._dma_req = False
                _dma_request(self.chip, f"ADC{self.index}", False)

    def _dr_read(self, blk, reg):
        self._unread = False
        if self._dma_req:                            # read (by the DMA or the CPU): the request is served
            self._dma_req = False
            _dma_request(self.chip, f"ADC{self.index}", False)
        sr = self.get("SR")
        if sr & EOC:
            self.set("SR", sr & ~EOC)
            self._update_irq()
        return None


@register("stm32.adc_common")
class ADCCommon(MMIOBlock):
    """ADC common registers: CSR (read-only mirror of the ADCx SR flags), CCR (prescaler, internal channels, multi-ADC
    mode, DMA mode), CDR (multi-mode data)."""

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov = {"CSR": {"on_read": self._csr_read, "ro_mask": 0xFFFFFFFF},
              "CCR": {"on_write": self._ccr_write},
              "CDR": {"on_read": self._cdr_read, "ro_mask": 0xFFFFFFFF}}
        r = build_regs((name, "C_ADC"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self._warned: set[int] = set()
        self._half: list[tuple[int, int]] = []       # (ADC index, data) not yet packed into an item
        self._items: list[int] = []                  # CDR items waiting for the DMA
        self._req = False
        self._dds_stop = False                       # CCR.DDS = 0 after the DMA's last transfer (RM0090 13.13.16)
        self._alt = 0                                # alternate trigger: next ADC (0-based)

    def reset(self) -> None:
        super().reset()
        self._half, self._items, self._alt = [], [], 0
        if getattr(self, "_req", False) and self.chip is not None:
            _dma_request(self.chip, "ADC1", False)
        self._req = False
        self._dds_stop = False

    def _adc(self, i: int) -> Any:
        a = self.chip.peripherals.get(f"ADC{i}") if self.chip is not None else None
        return a if a is not None and getattr(a, "model", None) == "stm32.adc" else None

    def _csr_read(self, blk, reg):
        v = 0
        for i, sh in ((1, 0), (2, 8), (3, 16)):
            a = self._adc(i)
            if a is not None:
                v |= (a.get("SR") & 0x3F) << sh
        return v

    # ------------------------------------------------------------ multi-ADC mode
    def mode(self) -> tuple[int, str | None, str | None] | None:
        """(number of ADCs, regular mode, injected mode) of CCR.MULTI, None in independent (or reserved) mode."""
        return MULTI_MODES.get(self.get("CCR") & 0x1F)

    def regular_mode(self) -> str | None:
        m = self.mode()
        return m[1] if m else None

    def _members(self, n: int) -> list:
        return [a for a in (self._adc(i) for i in range(1, n + 1)) if a is not None]

    def regular_group(self, adc: Any) -> list | None:
        """ADCs a regular trigger of `adc` starts: None = only itself (independent), [] = none (a slave)."""
        m = self.mode()
        if m is None or m[1] is None or adc.index > m[0]:
            return None
        return self._members(m[0]) if adc.index == 1 else []

    def injected_group(self, adc: Any) -> list | None:
        m = self.mode()
        if m is None or m[2] is None or adc.index > m[0]:
            return None
        if adc.index != 1:
            return []
        members = self._members(m[0])
        if m[2] == "sim":
            return members
        k = self._alt % len(members)                 # alternate trigger: one ADC per trigger, in turn
        self._alt = k + 1
        return [members[k]]

    def interleave_delay(self) -> int:
        """ADCCLK cycles between the sampling phases of consecutive ADCs in interleaved mode (5 + CCR.DELAY)."""
        return 5 + (self.get("CCR") >> 8 & 0xF)

    def multi_dma(self, adc: Any) -> bool:
        """True when `adc`'s regular results go to CDR (multi mode with a regular group and CCR.DMA != 0)."""
        m = self.mode()
        return bool(m and m[1] and adc.index <= m[0] and self.get("CCR") >> 14 & 3)

    def push(self, adc: Any, val: int) -> None:
        """A regular conversion result of `adc` for the multi-mode DMA (CCR.DMA modes 1-3)."""
        m = self.mode()
        dma = self.get("CCR") >> 14 & 3
        if self._dds_stop:                           # no DMA request after the last transfer: nothing is queued
            return
        if len(self._items) >= -(-m[0] // (1 if dma == 1 else 2)):     # a whole round still unread: overrun
            for a in self._members(m[0]):
                if not a.get("SR") & OVR:
                    a.set("SR", a.get("SR") | OVR)
                    a._pos, a._burst = 0, None
                    if a._ev is not None and a is not adc:
                        a.chip.sched.cancel(a._ev)
                        a._ev = None
                    a._update_irq()
            self.emit("info", block=self.name, msg=f"{self.name}: multi-ADC DMA overrun (CDR not read), regular "
                                                   "conversions stopped")
            return
        if dma == 1:
            self._items.append(val & 0xFFFF)
        else:
            self._half.append(val)
            if len(self._half) == 2:
                (a0, a1), self._half = self._half, []
                self._items.append((a1 & 0xFFFF) << 16 | (a0 & 0xFFFF) if dma == 2 else (a1 & 0xFF) << 8 | (a0 & 0xFF))
        if self._items and not self._req:
            self._req = True
            _dma_request(self.chip, "ADC1")

    def dma_transfer_complete(self, names: tuple) -> None:
        """stm32.dma: the stream serving "ADC1" did its last transfer.  CCR.DDS = 0 (bit 13): no new request until
        CCR.DMA is cleared and set again (RM0090 13.13.16)."""
        ccr = self.get("CCR")
        if "ADC1" in names and ccr >> 14 & 3 and not ccr & 1 << 13 and self.mode():
            self._dds_stop = True
            self._half, self._items = [], []
            if self._req:
                self._req = False
                _dma_request(self.chip, "ADC1", False)

    def _cdr_read(self, blk, reg):
        if self._items:
            v = self._items.pop(0)
            if not self._items and self._req:
                self._req = False
                _dma_request(self.chip, "ADC1", False)
            return v
        a1, a2 = self._adc(1), self._adc(2)
        return ((a2.get("DR") & 0xFFFF) << 16 if a2 is not None else 0) | (a1.get("DR") & 0xFFFF if a1 else 0)

    def _ccr_write(self, blk, reg, old, new):
        multi = new & 0x1F
        if multi and multi not in MULTI_MODES and multi not in self._warned:
            self._warned.add(multi)
            self.emit("warn", block=self.name, msg=f"{self.name}: CCR.MULTI={multi:#07b} is a reserved multi-ADC mode: "
                                                   "each ADC converts independently")
        if not new >> 14 & 3:
            self._dds_stop = False
        if (old ^ new) & 0x1F:
            self._half, self._items, self._alt = [], [], 0
        if (old ^ new) & 0x1F or (old ^ new) & (3 << 14):
            if self._req and not (new >> 14 & 3):
                self._req = False
                _dma_request(self.chip, "ADC1", False)
        return None
