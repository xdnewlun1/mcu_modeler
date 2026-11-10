"""Kinetis 12-bit DAC (DAC0, K22P100M120SF5V2RM ch. 35; chip configuration 3.7.3).

Registers (SVD table): DATnL/DATnH (n = 0..15, the 16-word buffer, 12-bit: DATnH[3:0] are DATA[11:8], DATnH[7:4]
read 0), SR (+0x20), C0 (+0x21), C1 (+0x22), C2 (+0x23).

* Output: with C0.DACEN=1 the DAC converts DAT0 (C1.DACBFEN=0) or the buffer word at the read pointer C2.DACBFRP
  (DACBFEN=1): Vout = Vin * (DATA + 1) / 4096 (35.4.1). Vin per C0.DACRFS: 0 = DACREF_1 = VREF_OUT (the VREF model's
  output, 0 V while it is off), 1 = DACREF_2 = VDDA (`params.vdda`, 3.3 V). `voltage()` returns the output in volts,
  or None while DACEN=0. Every change of the output level is logged as one `info` event ("DAC0_OUT = 1.6504 V",
  fields `signal`, `volts`, `code`) and passed to `listeners` (cb(dac)): CMP1 input IN3 and ADC0_SE23 share the
  DAC0_OUT pin and follow it. The dedicated DAC0_OUT pin is reported with owner "DAC0_OUT" (direction analog)
  while DACEN=1.
* Buffer (35.5.1): a trigger advances the read pointer when DACBFEN=1: software trigger = writing C0.DACSWTRG=1
  with C0.DACTRGSEL=1 (DACSWTRG reads 0); hardware trigger = `hw_trigger()` (the PDB) with DACTRGSEL=0. Modes (C1.DACBFMD):
  00 normal (wraps from C2.DACBFUP to 0), 01 swing (turns at DACBFUP and at 0), 10 one-time scan (stops at DACBFUP),
  11 reserved (no advance). Writing C2.DACBFRP sets the pointer (DACBFRP > DACBFUP is clamped to DACBFUP).
* Flags (SR, write 0 to clear, writing 1 has no effect; set only when the pointer changes): DACBFRPBF when the
  pointer reaches DACBFUP, DACBFRPTF when it reaches 0 (set out of reset), DACBFWMF when it reaches the watermark,
  C1.DACBFWM + 1 words below DACBFUP.
* Interrupt "DAC0" (level): (DACBFRPBF & DACBBIEN) | (DACBFRPTF & DACBTIEN) | (DACBFWMF & DACBWIEN). With
  C1.DMAEN=1 the same condition raises DMAMUX request 45 (Table 3-25) instead of the interrupt; the DMA's
  completion clears the enabled flags (35.5.2).

Hardware trigger source: PDB0 DAC interval trigger 0 (kinetis.pdb, DACINTC0/DACINT0; chip configuration 3.8.1.4).
Not modelled: output settling time and
C0.LPEN (low-power mode only stored), the DAC0 output buffer drive limits, Stop-mode behaviour. 16/32-bit accesses
to SR/C0/C1 (forbidden by the RM) act bytewise. Clock gate: SIM_SCGC2[DAC0].
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ._util import GatedBlock, build_regs, find_model, irq_number
from .dma import dma_request
from .vref import analog_pin, set_analog_owner

BFRPBF, BFRPTF, BFWMF = 0x01, 0x02, 0x04
DACEN, DACRFS, DACTRGSEL, DACSWTRG, LPEN = 0x80, 0x40, 0x20, 0x10, 0x08
BWIEN, BTIEN, BBIEN = 0x04, 0x02, 0x01
DMAEN, DACBFEN = 0x80, 0x01
DMA_SOURCE = 45


@register("kinetis.dac")
class DAC(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.index = int(params.get("index", name[-1] if name[-1:].isdigit() else 0))
        self.gate = params.get("gate", f"DAC{self.index}")
        ov: dict[str, dict] = {f"DAT{n}H": {"ro_mask": 0xF0, "on_write": self._dat_write} for n in range(16)}
        ov.update({f"DAT{n}L": {"on_write": self._dat_write} for n in range(16)})
        ov["SR"] = {"on_write": self._sr_write, "ro_mask": 0xF8}
        ov["C0"] = {"on_write": self._c0_write}
        ov["C1"] = {"on_write": self._c1_write, "ro_mask": 0x60}
        ov["C2"] = {"on_write": self._c2_write}
        r = build_regs((name, f"DAC{self.index}", "DAC0"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.vdda = float(params.get("vdda", 3.3))
        self.dma_source = int(params.get("dma_source", DMA_SOURCE + self.index))
        self.listeners: list = []
        self.triggers = 0
        self._irqn: int | None = None
        self._line = False
        self._down = False                   # swing mode direction
        self._out: tuple | None = None       # last reported (code, volts)
        self._vref: Any = None
        self._pin: Any = None

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, f"DAC{self.index}")
        self._vref = find_model(chip, "kinetis.vref", "VREF")
        if self._vref is not None:
            self._vref.listeners.append(self._ref_changed)
        self._pin = analog_pin(chip, f"DAC{self.index}_OUT")
        for b in chip.peripherals.values():
            if getattr(b, "model", None) == "kinetis.dmamux":
                b.register_source(self.dma_source, self._dma_active)
                if hasattr(b, "register_ack"):
                    b.register_ack(self.dma_source, self._dma_done)

    def reset(self) -> None:
        super().reset()
        self._down = False
        self._line = False
        if self.chip is not None:
            if self._irqn is not None:
                self.irq(self._irqn, False)
            self._update_output()

    # ------------------------------------------------------------ output
    def code(self) -> int:
        """The 12-bit word being converted."""
        n = self.pointer() if self.get("C1") & DACBFEN else 0
        return self.get(f"DAT{n}L") | (self.get(f"DAT{n}H") & 0x0F) << 8

    def reference(self) -> float:
        if self.get("C0") & DACRFS:
            return self.vdda
        v = self._vref.voltage() if self._vref is not None else None
        return v if v is not None else 0.0

    def voltage(self) -> float | None:
        if not self.get("C0") & DACEN:
            return None
        return self.reference() * (self.code() + 1) / 4096.0

    def _update_output(self) -> None:
        v = self.voltage()
        out = None if v is None else (self.code(), round(v, 9))
        if out == self._out:
            return
        self._out = out
        sig = f"DAC{self.index}_OUT"
        set_analog_owner(self.chip, self._pin, sig if v is not None else None)
        if v is None:
            self.emit("info", block=self.name, signal=sig, volts=None, msg=f"{sig} off (DACEN=0)")
        else:
            self.emit("info", block=self.name, signal=sig, volts=v, code=out[0], msg=f"{sig} = {v:.4f} V "
                      f"(code {out[0]}, ref {'VDDA' if self.get('C0') & DACRFS else 'VREF_OUT'})")
        for cb in self.listeners:
            cb(self)

    def _ref_changed(self, vref: Any) -> None:
        if not self.get("C0") & DACRFS:
            self._update_output()

    # ------------------------------------------------------------ buffer
    def pointer(self) -> int:
        return self.get("C2") >> 4

    def upper(self) -> int:
        return self.get("C2") & 0x0F

    def _set_pointer(self, rp: int) -> None:
        c2 = self.get("C2")
        self.set("C2", (c2 & 0x0F) | (rp << 4))
        up = c2 & 0x0F
        sr = self.get("SR")
        if rp == up:
            sr |= BFRPBF
        if rp == 0:
            sr |= BFRPTF
        wm = up - (((self.get("C1") >> 3) & 3) + 1)
        if wm >= 0 and rp == wm:
            sr |= BFWMF
        self.set("SR", sr)
        self._update_irq()
        self._update_output()

    def _advance(self) -> None:
        if not self.get("C1") & DACBFEN:
            return
        self.triggers += 1
        rp, up = self.pointer(), self.upper()
        mode = (self.get("C1") >> 1) & 3
        if mode == 0:                                   # normal: circular
            rp = 0 if rp >= up else rp + 1
        elif mode == 1:                                 # swing
            if rp >= up:
                self._down = True
            elif rp == 0:
                self._down = False
            rp = max(rp - 1, 0) if self._down else rp + 1
            if up == 0:
                rp = 0
        elif mode == 2:                                 # one-time scan
            if rp >= up:
                return
            rp += 1
        else:
            return
        self._set_pointer(rp)

    def software_trigger(self) -> None:
        if self.get("C0") & DACTRGSEL:
            self._advance()

    def hw_trigger(self) -> bool:
        """Hardware trigger input (PDB DAC interval trigger on silicon). Advances the buffer when DACTRGSEL=0."""
        if self.get("C0") & DACTRGSEL:
            return False
        self._advance()
        return True

    # ------------------------------------------------------------ IRQ / DMA
    def _pending(self) -> bool:
        sr, c0 = self.get("SR"), self.get("C0")
        return bool((sr & BFRPBF and c0 & BBIEN) or (sr & BFRPTF and c0 & BTIEN) or (sr & BFWMF and c0 & BWIEN))

    def _dma_active(self) -> bool:
        return bool(self.get("C1") & DMAEN) and self._pending()

    def _dma_done(self) -> None:
        c0 = self.get("C0")
        en = (BFRPBF if c0 & BBIEN else 0) | (BFRPTF if c0 & BTIEN else 0) | (BFWMF if c0 & BWIEN else 0)
        self.set("SR", self.get("SR") & ~en)
        self._update_irq()

    def _update_irq(self) -> None:
        if self.chip is None:
            return
        pend = self._pending()
        dma = bool(self.get("C1") & DMAEN)
        on = pend and not dma
        if self._irqn is not None and on != self._line:
            self._line = on
            self.irq(self._irqn, on)
        if pend and dma:
            dma_request(self.chip, self.dma_source)

    # ------------------------------------------------------------ register hooks
    def _dat_write(self, blk, reg, old, new):
        if old != new:
            self._store(reg, new)
            self._update_output()
        return None

    def _sr_write(self, blk, reg, old, new):
        v = old & new & 0x07                     # write 0 clears, 1 has no effect
        self._store(reg, v)
        self._update_irq()
        return v

    def _c0_write(self, blk, reg, old, new):
        swtrg = bool(new & DACSWTRG)
        new &= ~DACSWTRG
        self._store(reg, new)
        if swtrg:
            self.software_trigger()
        self._update_irq()
        self._update_output()
        return new

    def _c1_write(self, blk, reg, old, new):
        self._store(reg, new)
        self._update_irq()
        self._update_output()
        return None

    def _c2_write(self, blk, reg, old, new):
        up = new & 0x0F
        rp = min(new >> 4, up)
        self._store(reg, (rp << 4) | up)
        if (old ^ new) & 0xF0:
            self._set_pointer(rp)
        else:
            self._update_output()
        return (rp << 4) | up
