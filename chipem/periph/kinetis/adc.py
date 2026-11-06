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
* Reading Rn clears SC1n.COCO (and deasserts the IRQ when no COCO with AIEN remains).
* SC2.DMAEN: a completed conversion also raises the DMAMUX request "ADC<n>" (source 40 + n); the DMA reads Rn,
  which clears COCO.  `hw_trigger(k)` starts a hardware-triggered conversion of SC1A (k=0) / SC1B (k=1) only
  when SC2.ADTRG=1 (otherwise it is ignored).
* Input values: `set_channel(name_or_number, value)` (raw counts; clipped to the configured resolution), e.g.
  set_channel(14, 2048), set_channel("ADC0_SE14", 2048) (ignored -> False on ADC1), "SE4b", "DP0" (differential
  pair 0, used when SC1n.DIFF=1). Unset channels read `params.default` (0).
"""
from __future__ import annotations

import re
from typing import Any

from ..registry import register
from ._util import GatedBlock, build_regs, irq_number
from .dma import dma_request

COCO, AIEN, DIFF = 0x80, 0x40, 0x20
CAL, CALF, ADCO = 0x80, 0x40, 0x08
ADTRG, DMAEN = 0x40, 0x04
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
              "RA": {"on_read": self._r_read}, "RB": {"on_read": self._r_read}}
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

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, f"ADC{self.index}")
        self.dma_source = int(self.params.get("dma_source", 40 + self.index))
        for b in chip.peripherals.values():
            if getattr(b, "model", None) == "kinetis.dmamux":
                b.register_source(self.dma_source, self._dma_active)

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
        if diff:
            return self.values.get(f"DP{adch}", self.default)
        ab = "b" if self.get("CFG2") & 0x10 else "a"
        v = self.values.get(f"SE{adch}{ab}")
        if v is None:
            v = self.values.get(f"SE{adch}", self.default)
        return v

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
        if self.conv_ns <= 0:
            self._complete(k)
        else:
            self._ev[k] = self.chip.sched.in_ns(self.conv_ns, self._complete, k)

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

    def hw_trigger(self, k: int = 0) -> bool:
        """Hardware trigger (PDB pre-trigger k -> SC1A/SC1B). Starts a conversion if SC2.ADTRG is set."""
        if not self.get("SC2") & ADTRG or self.chip is None:
            return False
        sc1n = "SC1A" if k == 0 else "SC1B"
        if (self.get(sc1n) & 0x1F) == 0x1F:
            return False
        self._cancel(k)
        self._start(k)
        return True

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
