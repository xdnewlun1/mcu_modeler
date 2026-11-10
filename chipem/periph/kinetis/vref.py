"""Kinetis VREF (voltage reference VREFV1, K22P100M120SF5V2RM ch. 36): the 1.2 V VREF_OUT reference.

Registers (SVD table): TRM (+0: CHOPEN 6, TRIM 5-0), SC (+1: VREFEN 7, REGEN 6, ICOMPEN 5, VREFST 2 read-only,
MODE_LV 1-0).

Behaviour:
* SC.VREFEN 0 -> 1 starts the bandgap; SC.VREFST reads 1 `startup_ns` later (params, default 35 us: data sheet
  t_stup) via the scheduler (wall-clock domain, independent of the core clock). VREFEN=0 clears VREFST at once.
  Changing MODE_LV while enabled does not clear VREFST (36.4).
* `voltage()`: the VREF_OUT level in volts, None while the output is off. The buffered output is on when VREFEN=1,
  VREFST=1 and MODE_LV = 01 (high power) or 10 (low power); MODE_LV=00 is bandgap only (no output, 36.3.2.1) and 11
  is reserved (no output). The level is `params.volts` (1.2) + 0.5 mV per TRIM step away from the reset TRIM value
  (the factory trim loaded at reset; the SVD reset value, 0, stands in for it).
* Consumers (K22 chip configuration 3.7): DAC0 DACREF_1 (C0.DACRFS=0), CMPx 6-bit DAC Vin1 (DACCR.VRSEL=0), CMP0/
  CMP1 input IN5, ADC1_SE18 (dedicated pin VREF_OUT/CMP1_IN5/CMP0_IN5/ADC1_SE18). They subscribe to `listeners`
  (cb(vref)) and are told whenever `voltage()` may have changed.
* The dedicated VREF_OUT pin is reported with owner "VREF_OUT" (direction analog) while the output is on.

Not modelled: the chop oscillator (TRM.CHOPEN only stored), the regulator (REGEN) and curvature compensation
(ICOMPEN) accuracy effects, the buffer start-up delay t_stup after a MODE_LV change, VLP-mode behaviour.
Clock gate: SIM_SCGC4[VREF].
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ._util import GatedBlock, build_regs

VREFEN, VREFST = 0x80, 0x04


def analog_pin(chip: Any, signal: str) -> Any:
    """The (dedicated analog) pin whose name lists `signal` among its '/'-parts, or None."""
    pins = getattr(chip, "pins", None)
    if pins is None:
        return None
    for p in pins.pins:
        if signal in str(p.name).split("/"):
            return p
    return None


def set_analog_owner(chip: Any, pin: Any, owner: str | None) -> None:
    """Report a dedicated analog pin as driven by `owner` (e.g. "DAC0_OUT"), or give it back to "analog"."""
    if pin is None or chip.pins is None:
        return
    want = owner or "analog"
    if pin.owner != want:
        chip.pins.mcu_update(pin, owner=want, direction="analog")


@register("kinetis.vref")
class VREF(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = params.get("gate", "VREF")
        ov = {"SC": {"on_write": self._sc_write, "ro_mask": VREFST | 0x18}, "TRM": {"on_write": self._trm_write}}
        r = build_regs((name, "VREF"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.startup_ns = float(params.get("startup_ns", 35_000.0))
        self.volts = float(params.get("volts", 1.2))
        self._trim0 = self.regs["TRM"].reset & 0x3F
        self._ev: Any = None
        self._pin: Any = None
        self.listeners: list = []

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._pin = analog_pin(chip, "VREF_OUT")

    def reset(self) -> None:
        self._cancel()
        super().reset()
        self._changed()

    def _cancel(self) -> None:
        if self._ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None

    # ------------------------------------------------------------ output
    def stable(self) -> bool:
        return bool(self.get("SC") & VREFST)

    def voltage(self) -> float | None:
        sc = self.get("SC")
        if not sc & VREFEN or not sc & VREFST or (sc & 3) not in (1, 2):
            return None
        return self.volts + 0.0005 * ((self.get("TRM") & 0x3F) - self._trim0)

    def _changed(self) -> None:
        if self.chip is None:
            return
        set_analog_owner(self.chip, self._pin, "VREF_OUT" if self.voltage() is not None else None)
        for cb in self.listeners:
            cb(self)

    # ------------------------------------------------------------ registers
    def _sc_write(self, blk, reg, old, new):
        if new & VREFEN and not old & VREFEN:
            self._cancel()
            if self.startup_ns <= 0:
                new |= VREFST
            else:
                self._ev = self.chip.sched.in_ns(self.startup_ns, self._ready)
        elif not new & VREFEN:
            self._cancel()
            new &= ~VREFST
        self._store(reg, new)
        if (old ^ new) & 0xFF:
            self._changed()
        return new

    def _ready(self) -> None:
        self._ev = None
        if self.get("SC") & VREFEN:
            self.set("SC", self.get("SC") | VREFST)
            v = self.voltage()
            self.emit("info", block=self.name, msg=f"{self.name} stable (VREFST=1)"
                      + (f", VREF_OUT = {v:.4f} V" if v is not None else ""))
            self._changed()

    def _trm_write(self, blk, reg, old, new):
        if (old ^ new) & 0x3F:
            self._store(reg, new)
            self._changed()
        return None
