"""STM32F4 12-bit ADC (ADC1-3, RM0090 ch. 13) and the ADC common registers (C_ADC: CSR, CCR, CDR).

Regular group (registers from the SVD; behaviour hand-written):
* CR2.ADON powers the ADC (clearing it aborts a conversion).  SWSTART (ADON=1) or an external trigger starts the
  regular group; SWSTART reads 0 again at once (cleared when the conversion starts); SR.STRT is set.  A trigger while
  a conversion runs, while ADON=0, while SR.OVR is set or with the RCC clock (APB2ENR.ADCxEN) off is ignored.
* Sequence: SQR1.L + 1 channels SQ1..SQ16 with CR1.SCAN=1, SQ1 alone with SCAN=0.  CR2.CONT restarts the sequence
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
  it); the DMA reads DR (clearing EOC).  CR2.DDS is stored only: after the DMA's last transfer nobody reads DR any
  more, so with DDS=0 the next conversion ends in OVR, as on silicon.
* Analog watchdog (CR1.AWDEN, regular channels; AWDSGL + AWDCH for a single channel): the 12-bit-scaled code
  outside [LTR, HTR] sets SR.AWD.  JAWDEN (injected) is not modelled.
* Interrupt: one NVIC line "ADC" shared by ADC1-3 (the OR of all three): EOC & EOCIE, AWD & AWDIE, OVR & OVRIE (and
  JEOC & JEOCIE, never set here).  Level, re-evaluated on every flag change.
* External triggers (CR2.EXTEN 01 rising / 10 falling / 11 both, EXTSEL per RM0090 Table 51): TIM1_CC1, TIM1_CC2,
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
Not modelled: injected channels (JSQR, JOFRx, JDRx, JSWSTART, JEXTEN, JAUTO: one `warn`), multi-ADC modes
(CCR.MULTI != 0: one `warn`, each ADC converts on its own; CDR reads 0), CCR.DELAY, the sampling capacitor / source
impedance, ADCCLK phase.

C_ADC (`stm32.adc_common`): CSR mirrors the SR flags of ADC1 (bits 0-5), ADC2 (8-13) and ADC3 (16-21); CCR is
storage read by the ADCs (ADCPRE, TSVREFE, VBATE).
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
AWDEN, OVRIE = 1 << 23, 1 << 26
# CR2
ADON, CONT, DMA, DDS, EOCS, ALIGN, JSWSTART, SWSTART = 1, 2, 1 << 8, 1 << 9, 1 << 10, 1 << 11, 1 << 22, 1 << 30
JEXTEN_MASK = 3 << 20
# CCR (common)
TSVREFE, VBATE = 1 << 23, 1 << 22

SMP_CYCLES = (3, 15, 28, 56, 84, 112, 144, 480)
RES_BITS = (12, 10, 8, 6)
# RM0090 Table 51: EXTSEL -> (block, trigger signal)
TRIGGERS = (("TIM1", "CC1"), ("TIM1", "CC2"), ("TIM1", "CC3"), ("TIM2", "CC2"), ("TIM2", "CC3"), ("TIM2", "CC4"),
            ("TIM2", "TRGO"), ("TIM3", "CC1"), ("TIM3", "TRGO"), ("TIM4", "CC4"), ("TIM5", "CC1"), ("TIM5", "CC2"),
            ("TIM5", "CC3"), ("TIM8", "CC1"), ("TIM8", "TRGO"), ("EXTI", "11"))
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
        if getattr(self, "_sub", None) is not None:
            self._unsubscribe()
        self._sub_src = None
        self._pos, self._burst, self._unread, self._trig_lvl = 0, None, False, False
        if getattr(self, "_dma_req", False):
            _dma_request(self.chip, f"ADC{self.index}", False)
        self._dma_req = False
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
        if cr2 & DMA:
            self._dma_req = True
            _dma_request(chip, f"ADC{self.index}")
        now = chip.now_cycles()
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
        want = TRIGGERS[cr2 >> 24 & 0xF] if (cr2 >> 28 & 3) and cr2 & ADON and self.chip is not None else None
        if want == self._sub_src:
            return
        if self._sub is not None:
            self._unsubscribe()
        self._sub_src = want
        if want is None:
            return
        blk_name, sig = want
        blk = self.chip.peripherals.get(blk_name)
        if blk_name == "EXTI" or not hasattr(blk, "add_trigger_listener"):
            self._warn_once(f"trig{blk_name}{sig}", f"{self.name}: external trigger {blk_name}_{sig} (EXTSEL="
                            f"{cr2 >> 24 & 0xF}) is not modelled: no conversions are triggered")
            return
        cb = self._ext_trig
        blk.add_trigger_listener(sig, cb)
        self._sub = (blk, sig, cb)
        self._trig_lvl = bool(blk.trigger_level(sig))

    def _unsubscribe(self) -> None:
        blk, sig, cb = self._sub
        self._sub = None
        blk.remove_trigger_listener(sig, cb)

    def _ext_trig(self, t: int, level: bool) -> None:
        if level == self._trig_lvl:
            return
        self._trig_lvl = level
        exten = self.get("CR2") >> 28 & 3
        if exten & (1 if level else 2):
            self._trigger(t, external=True)

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
        if new & (JSWSTART | JEXTEN_MASK) or self.get("JSQR"):
            self._warn_once("inj", f"{self.name}: injected channels (JSQR/JSWSTART/JEXTEN) are not modelled")
        v = new & ~(SWSTART | JSWSTART)
        self._store(reg, v)
        if (old ^ new) & ADON and not new & ADON:     # power down: the running conversion is lost
            if self._ev is not None:
                self.chip.sched.cancel(self._ev)
                self._ev = None
            self._pos, self._burst = 0, None
        self._resubscribe()
        if new & SWSTART and new & ADON and self.chip is not None:
            self._trigger(self.chip.now_cycles(), external=False)
        return v

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
    """ADC common registers: CSR (read-only mirror of the ADCx SR flags), CCR, CDR (multi-ADC data: reads 0)."""

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov = {"CSR": {"on_read": self._csr_read, "ro_mask": 0xFFFFFFFF},
              "CCR": {"on_write": self._ccr_write},
              "CDR": {"on_read": lambda b, r: 0, "ro_mask": 0xFFFFFFFF}}
        r = build_regs((name, "C_ADC"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self._warned = False

    def _csr_read(self, blk, reg):
        v = 0
        chip = self.chip
        for i, sh in ((1, 0), (2, 8), (3, 16)):
            a = chip.peripherals.get(f"ADC{i}") if chip is not None else None
            if a is not None and getattr(a, "model", None) == "stm32.adc":
                v |= (a.get("SR") & 0x3F) << sh
        return v

    def _ccr_write(self, blk, reg, old, new):
        if new & 0x1F and not self._warned:
            self._warned = True
            self.emit("warn", block=self.name, msg=f"{self.name}: multi-ADC mode CCR.MULTI={new & 0x1F:#x} is not "
                                                   "modelled: each ADC converts independently")
        return None
