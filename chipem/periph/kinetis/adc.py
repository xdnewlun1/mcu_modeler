"""Kinetis 16-bit SAR ADC (ADC0/ADC1, K22 RM ch. 33).

* SC3.CAL=1 -> calibration completes immediately: SC1A.COCO set, SC3.CAL and SC3.CALF cleared, CLP*/CLM*
  get plausible values (PG/MG are left to software, as on silicon).
* SC1A write (software trigger, SC2.ADTRG=0) with ADCH != 0x1F starts a conversion that completes after
  `conv_ns` (params, default 4 us) via the scheduler: result -> RA, COCO set, ADCn IRQ if AIEN. SC3.ADCO
  (continuous) re-starts it. With SC2.ADTRG=1 an SC1n write only selects the channel; the conversion starts on a
  hardware trigger (`hw_trigger(k)`, called by the PDB model) and completes exactly like a software one (Rn,
  COCO, IRQ if AIEN, DMA request if SC2.DMAEN). SC1B converts only on hardware triggers.
* `on_complete`: list of callbacks cb(adc, k) run after conversion SC1[k] completes (the PDB uses it for
  back-to-back pre-triggers). `busy(k)` is True while conversion k is in progress.
* Hardware average (SC3.AVGE, AVGS 4/8/16/32): a result takes that many conversion times; the average of identical
  samples is the sample (input values have no noise). Compare (SC2.ACFE/ACFGT/ACREN, CV1/CV2, Table 33-112): a result
  that fails the compare sets no COCO and leaves Rn unchanged (continuous conversions go on).
* Reading Rn clears SC1n.COCO (and deasserts the IRQ when no COCO with AIEN remains).
* SC2.DMAEN: a completed conversion also raises the DMAMUX request "ADC<n>" (source 40 + n); the DMA reads Rn,
  which clears COCO.  `hw_trigger(k)` starts a hardware-triggered conversion of SC1A (k=0) / SC1B (k=1) only
  when SC2.ADTRG=1 (otherwise it is ignored).
* Hardware trigger source (SIM_SOPT7, K22P100M120SF5V2RM 12.2.6): with ADCnALTTRGEN=0 the PDB pre-triggers reach the
  ADC (`hw_trigger(k)`); with ADCnALTTRGEN=1 PDB triggers are ignored and the source selected by ADCnTRGSEL starts a
  conversion of SC1A (ADCnPRETRGSEL=0) or SC1B (=1), again only with SC2.ADTRG=1. Modelled sources: 0 the
  PDB0_EXTRG pin (rising edge on a pin muxed to PDB0_EXTRG), 1..3 the CMP0..CMP2 output (rising edge of COUT),
  4..7 PIT channel 0..3 timeout, 8..11 the FTM0..FTM3 trigger output (channel triggers and the initialization
  trigger enabled in FTMx_EXTTRIG), 12/13 RTC alarm / seconds, 14 LPTMR0 compare (kinetis.sim
  `subscribe_trigger_sources`). 15 is reserved (the SIM logs one `info`).
* Internal analog channels (3.7.1.3/3.7.1.4): ADC0_SE23 = DAC0 output (while DAC0 is enabled), ADC1_SE18 = VREF_OUT
  (while the VREF output buffer is on). These are volts, converted with the selected reference: SC2.REFSEL=0 VREFH
  (`params.vrefh`, 3.3 V), REFSEL=1 VALT = VREF_OUT (1.2 V; VREFH while the VREF is off). Values set with
  set_channel() are raw counts and do not depend on REFSEL.
* Input values: `set_channel(name_or_number, value)` (raw counts; clipped to the configured resolution), e.g.
  set_channel(14, 2048), set_channel("ADC0_SE14", 2048) (ignored -> False on ADC1), "SE4b", "DP0" (differential
  pair 0, used when SC1n.DIFF=1). Unset channels read `params.default` (0).
"""
from __future__ import annotations

import re
from typing import Any

from ..base import peek_stored as _stored
from ..registry import register
from ._util import GatedBlock, build_regs, find_model, irq_number
from .dma import dma_request
from .sim import subscribe_trigger_sources


COCO, AIEN, DIFF = 0x80, 0x40, 0x20
CAL, CALF, ADCO = 0x80, 0x40, 0x08
ADTRG, DMAEN = 0x40, 0x04
ACFE, ACFGT, ACREN = 0x20, 0x10, 0x08
AVGE = 0x04
CAL_VALUES = {"CLPD": 0x0A, "CLPS": 0x24, "CLP4": 0x220, "CLP3": 0x110, "CLP2": 0x88, "CLP1": 0x44, "CLP0": 0x22,
              "CLMD": 0x0A, "CLMS": 0x24, "CLM4": 0x220, "CLM3": 0x110, "CLM2": 0x88, "CLM1": 0x44, "CLM0": 0x22}
_NAME = re.compile(r"^(?:ADC(\d+)_)?(SE|DP|DM|AD)?(\d+)([AB])?$", re.I)


@register("kinetis.adc")
class ADC(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        m = re.search(r"(\d+)$", name)
        self.index = int(params.get("index", m.group(1) if m else 0))
        self.gate = f"ADC{self.index}"
        ov = {"SC1A": {"on_write": self._sc1_write}, "SC1B": {"on_write": self._sc1_write},
              "SC3": {"on_write": self._sc3_write, "ro_mask": 0xFFFFFF30 & ~CALF, "w1c_mask": CALF},
              "SC2": {"on_write": self._sc2_write},
              "RA": {"on_read": self._r_read, "on_peek": _stored}, "RB": {"on_read": self._r_read, "on_peek": _stored}}
        r = build_regs((name, f"ADC{self.index}", "ADC0"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.conv_ns = float(params.get("conv_ns", 4000.0))
        self.default = int(params.get("default", 0))
        self.values: dict[str, int] = {}
        self._ev: list[Any] = [None, None]
        self._irqn: int | None = None
        self._in_dma = False
        self.on_complete: list = []
        self.dma_source = -1
        self.conversions = 0
        self.vrefh = float(params.get("vrefh", 3.3))
        self.analog_sources: dict[str, Any] = {}     # "SE23" -> callable() -> volts or None (internal channels)
        self._sim: Any = None
        self._vref: Any = None

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, f"ADC{self.index}")
        self.dma_source = int(self.params.get("dma_source", 40 + self.index))
        for b in chip.peripherals.values():
            if getattr(b, "model", None) == "kinetis.dmamux":
                b.register_source(self.dma_source, self._dma_active)
        self._sim = find_model(chip, "kinetis.sim", "SIM")
        self._vref = find_model(chip, "kinetis.vref", "VREF")
        # alternate hardware triggers (SIM_SOPT7): every source of the shared trigger encoding
        subscribe_trigger_sources(chip, self.alt_trigger)
        # internal analog channels (K22 chip configuration 3.7.1.3/3.7.1.4)
        if self.index == 0:
            dac = find_model(chip, "kinetis.dac", "DAC0")
            if dac is not None:
                self.analog_sources.setdefault("SE23", dac.voltage)
        elif self.index == 1 and self._vref is not None:
            self.analog_sources.setdefault("SE18", self._vref.voltage)

    def reset(self) -> None:
        for k in (0, 1):
            self._cancel(k)
        super().reset()
        if self.chip is not None and self._irqn is not None:
            self.irq(self._irqn, False)          # COCO cleared by the reset: deassert the line

    # ------------------------------------------------------------ stimulus
    def set_channel(self, ch: int | str, value: int) -> bool:
        """Set the value a channel converts to. Returns False if the name belongs to another ADC instance."""
        key = self._key(ch)
        if key is None:
            return False
        self.values[key] = int(value)
        return True

    def _key(self, ch: int | str) -> str | None:
        if isinstance(ch, int):
            return f"SE{ch}"
        s = str(ch).strip().upper()
        m = _NAME.match(s)
        if not m:
            raise ValueError(f"bad ADC channel name {ch!r}")
        inst, kind, num, ab = m.groups()
        if inst is not None and int(inst) != self.index:
            return None
        kind = (kind or "SE").upper()
        if kind == "DM":
            kind = "DP"
        if kind == "AD":
            kind = "SE"
        return f"{kind}{int(num)}{(ab or '').lower()}"

    def channel_value(self, adch: int, diff: bool) -> int:
        if not diff and self.analog_sources:
            src = self.analog_sources.get(f"SE{adch}")
            v = src() if src is not None else None
            if v is not None:
                return self._volts_to_counts(v)
        if diff:
            return self.values.get(f"DP{adch}", self.default)
        ab = "b" if self.get("CFG2") & 0x10 else "a"
        v = self.values.get(f"SE{adch}{ab}")
        if v is None:
            v = self.values.get(f"SE{adch}", self.default)
        return v

    def reference_volts(self) -> float:
        if self.get("SC2") & 3 == 1:                     # REFSEL=01: VALT = VREF_OUT
            v = self._vref.voltage() if self._vref is not None else None
            if v is not None:
                return v
        return self.vrefh

    def _volts_to_counts(self, v: float) -> int:
        lo, hi = self.resolution(False)
        return max(lo, min(hi, int(round(v / self.reference_volts() * (hi + 1)))))

    def resolution(self, diff: bool) -> tuple[int, int]:
        mode = (self.get("CFG1") >> 2) & 3
        bits = {0: 8, 1: 12, 2: 10, 3: 16}[mode]
        if diff:                     # MODE 0/1/2 differential = 9/13/11-bit two's complement; MODE 3 = 16-bit
            if bits < 16:
                return -(1 << bits), (1 << bits) - 1
            return -32768, 32767
        return 0, (1 << bits) - 1

    # ------------------------------------------------------------ conversions
    def _cancel(self, k: int) -> None:
        ev = self._ev[k]
        if ev is not None and self.chip is not None:
            self.chip.sched.cancel(ev)
        self._ev[k] = None

    def _sc1_write(self, blk, reg, old, new):
        k = 0 if reg.name == "SC1A" else 1
        self._cancel(k)
        new &= ~COCO
        self._store(reg, new)
        self._update_irq()
        if k == 1:
            return new
        if self.get("SC2") & ADTRG:          # hardware-trigger mode: wait for hw_trigger()
            return new
        if (new & 0x1F) != 0x1F:
            self._start(k)
        return new

    def _start(self, k: int) -> None:
        if self.chip is None:
            return
        sc3 = self.get("SC3")
        n = (4 << (sc3 & 3)) if sc3 & AVGE else 1          # hardware average: 4/8/16/32 conversions (33.4.4)
        if self.conv_ns <= 0:
            self._complete(k)
        else:
            self._ev[k] = self.chip.sched.in_ns(self.conv_ns * n, self._complete, k)

    def _compare_ok(self, v: int, diff: bool) -> bool:
        """33.4.5 Table 33-112 (CV1/CV2 in the result format; signed for differential results)."""
        sc2 = self.get("SC2")
        cv1, cv2 = self.get("CV1") & 0xFFFF, self.get("CV2") & 0xFFFF
        if diff:
            cv1 = cv1 - 0x10000 if cv1 & 0x8000 else cv1
            cv2 = cv2 - 0x10000 if cv2 & 0x8000 else cv2
        gt, ren = bool(sc2 & ACFGT), bool(sc2 & ACREN)
        if not ren:
            return v >= cv1 if gt else v < cv1
        if cv1 <= cv2:
            return (cv1 <= v <= cv2) if gt else (v < cv1 or v > cv2)
        return (v >= cv1 or v <= cv2) if gt else (cv2 < v < cv1)

    def _complete(self, k: int) -> None:
        self._ev[k] = None
        sc1n = "SC1A" if k == 0 else "SC1B"
        sc1 = self.get(sc1n)
        adch = sc1 & 0x1F
        if adch == 0x1F:
            return
        diff = bool(sc1 & DIFF)
        lo, hi = self.resolution(diff)
        v = max(lo, min(hi, self.channel_value(adch, diff)))
        if self.get("SC2") & ACFE and not self._compare_ok(v, diff):
            # compare false: no COCO, Rn unchanged; continuous conversions go on (33.4.5)
            if self.get("SC3") & ADCO:
                self._start(k)
            return
        self.set("RA" if k == 0 else "RB", v & 0xFFFF)
        self.set(sc1n, sc1 | COCO)
        self.conversions += 1
        self._update_irq()
        if self.get("SC3") & ADCO:
            self._start(k)
        for cb in self.on_complete:
            cb(self, k)

    def busy(self, k: int) -> bool:
        return self._ev[k] is not None

    def _r_read(self, blk, reg):
        sc1n = "SC1A" if reg.name == "RA" else "SC1B"
        sc1 = self.get(sc1n)
        if sc1 & COCO:
            self.set(sc1n, sc1 & ~COCO)
            self._update_irq()
        return None

    def _update_irq(self) -> None:
        if self.chip is None:
            return
        if self._irqn is not None:
            on = any(self.get(n) & (COCO | AIEN) == (COCO | AIEN) for n in ("SC1A", "SC1B"))
            self.irq(self._irqn, on)
        if self._dma_active() and not self._in_dma:
            self._in_dma = True
            try:
                dma_request(self.chip, self.dma_source)
            finally:
                self._in_dma = False

    def _dma_active(self) -> bool:
        return bool(self.get("SC2") & DMAEN) and any(self.get(n) & COCO for n in ("SC1A", "SC1B"))

    def _sopt7(self) -> int:
        """This ADC's SIM_SOPT7 byte: TRGSEL 3-0, PRETRGSEL 4, ALTTRGEN 7."""
        sim = self._sim
        if sim is None or "SOPT7" not in sim.regs:
            return 0
        return (sim.get("SOPT7") >> (8 * self.index)) & 0xFF if self.index < 2 else 0

    def hw_trigger(self, k: int = 0, source: str = "pdb") -> bool:
        """Hardware trigger (PDB pre-trigger k -> SC1A/SC1B). Starts a conversion if SC2.ADTRG is set. PDB
        triggers (`source="pdb"`) are ignored while SIM_SOPT7.ADCnALTTRGEN selects the alternate trigger."""
        if not self.get("SC2") & ADTRG or self.chip is None:
            return False
        if source == "pdb" and self._sopt7() & 0x80:
            return False
        sc1n = "SC1A" if k == 0 else "SC1B"
        if (self.get(sc1n) & 0x1F) == 0x1F:
            return False
        self._cancel(k)
        self._start(k)
        return True

    def alt_trigger(self, sel: int) -> bool:
        """Alternate trigger source `sel` (SIM_SOPT7 ADCnTRGSEL encoding) fired: converts SC1A/SC1B per
        ADCnPRETRGSEL when ADCnALTTRGEN=1 and ADCnTRGSEL == sel."""
        s = self._sopt7()
        if not s & 0x80 or (s & 0x0F) != sel:
            return False
        return self.hw_trigger(1 if s & 0x10 else 0, source="alt")

    def _sc2_write(self, blk, reg, old, new):
        self._store(reg, new)
        self._update_irq()
        return None

    def _sc3_write(self, blk, reg, old, new):
        if new & CAL:
            for n, v in CAL_VALUES.items():
                if n in self.regs:
                    self.set(n, v)
            self.set("SC1A", self.get("SC1A") | COCO)
            self.emit("info", block=self.name, msg=f"{self.name} calibration complete (CALF=0)")
            self._store(reg, new & ~(CAL | CALF))
            self._update_irq()
            return new & ~(CAL | CALF)
        return None
