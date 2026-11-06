"""Kinetis PORT (pin control and interrupts): PCR0-31, GPCLR/GPCHR, ISFR, PORTD digital filter regs.

PCR fields: PS (0) pull select, PE (1) pull enable, SRE (2), PFE (4), ODE (5), DSE (6), MUX (8-10), LK (15,
locks bits 0-15 until reset), IRQC (16-19), ISF (24, w1c).

Pin board interaction (the PinBoard is generic; this model is the only place that knows Kinetis muxing):
* MUX=1 -> owner "GPIO"; direction/drive come from the matching GPIO model (PDDR/PDOR).
* MUX=0 -> the pin's ALT0 function (analog, e.g. "ADC0_SE14", direction "analog") or "disabled".
* MUX=2..7 -> the function name from the chip description (e.g. "UART1_TX"); direction and idle drive from the
  Kinetis signal table (`signals.py`: UART TX -> output idling H, RX -> input, I2C -> open-drain, ...; unknown
  signals -> "none"). Traffic does not toggle the pin. The JTAG/SWD pins (ALT7 out of reset) are covered too.
* pull = "up"/"down" when PE (digital modes only), else None.
* Pin interrupts: one PinBoard input listener per pin. IRQC 1/2/3 = DMA request on rising/falling/either
  edge (flag only), 8 = logic 0, 9 = rising, 10 = falling, 11 = either edge, 12 = logic 1. A detected event
  sets PCR.ISF and the ISFR bit; the PORTx IRQ line is the OR of the ISF flags of pins in interrupt modes
  (8-12), a level-sensitive NVIC input: it stays asserted while any flag remains set (the NVIC re-pends the
  IRQ on handler return) and clearing the last one deasserts it.
  Level modes re-set ISF immediately after it is cleared if the level still matches. X (floating) counts as 0.
* Pin discovery (chipem.pinreport): every PCR change is reported with the PC of the writing instruction
  (`_note_pcr`, config-rate); ISF sets, PCR writes and floating levels seen by the interrupt logic are counted
  per pin and handed over by `pin_stats()`.
"""
from __future__ import annotations

from functools import partial
from typing import Any

from ..registry import register
from ._util import GatedBlock, build_regs, find_model, irq_number
from .signals import TABLE as SIGNALS

PS, PE, MUX_SHIFT, MUX_MASK, LK = 1 << 0, 1 << 1, 8, 0x700, 1 << 15
IRQC_SHIFT, ISF = 16, 1 << 24
IRQC_NAMES = {0: "disabled", 1: "DMA rising", 2: "DMA falling", 3: "DMA either", 8: "logic 0", 9: "rising edge",
              10: "falling edge", 11: "either edge", 12: "logic 1"}


def _zero(blk, reg):
    return 0


@register("kinetis.port")
class PORT(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.port = str(params.get("port") or name[-1]).upper()
        self.gate = f"PORT{self.port}"
        ov: dict[str, dict] = {f"PCR{i}": {"w1c_mask": ISF, "on_write": self._pcr_write} for i in range(32)}
        ov["GPCLR"] = {"on_write": self._gpc_write, "on_read": _zero}
        ov["GPCHR"] = {"on_write": self._gpc_write, "on_read": _zero}
        ov["ISFR"] = {"w1c_mask": 0xFFFFFFFF, "on_write": self._isfr_write}
        r = build_regs((name, f"PORT{self.port}"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.pins: list[Any] = [None] * 32
        self.gpio_mask = 0                      # bits whose MUX == 1
        self._gpio: Any = None
        self._isf = 0
        self._last = [0] * 32                   # last seen input level (1 = H) for edge detection
        self._line = False
        self._irqn: int | None = None
        self._pcr_off = [self.regs[f"PCR{i}"].offset if f"PCR{i}" in self.regs else None for i in range(32)]
        self._rep: Any = None                   # chipem.pinreport.PinReport (pin discovery)
        self.isf_count = [0] * 32
        self.pcr_writes = [0] * 32
        self.x_irq_mask = 0

    # ------------------------------------------------------------ setup
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        board = getattr(chip, "pins", None)
        if board is not None:
            for i in range(32):
                pin = board.gpio(self.port, i)
                self.pins[i] = pin
                if pin is not None:
                    board.input_listeners[pin.id].append(partial(self._on_level, i))
            from ...pinreport import ensure
            self._rep = ensure(chip)
            if self._rep is not None:
                self._rep.hooked = True
        self._gpio = (find_model(chip, "kinetis.gpio", f"GPIO{self.port}")
                      or find_model(chip, "kinetis.gpio", f"PT{self.port}"))
        if self._gpio is not None and getattr(self._gpio, "port", self.port) != self.port:
            self._gpio = None
            for b in chip.peripherals.values():
                if getattr(b, "model", None) == "kinetis.gpio" and getattr(b, "port", None) == self.port:
                    self._gpio = b
        self._irqn = irq_number(chip, f"PORT{self.port}")

    def reset(self) -> None:
        super().reset()
        self._isf = 0
        if self._line and self._irqn is not None and self.chip is not None:
            self.irq(self._irqn, False)
        self._line = False
        self.gpio_mask = 0
        for i in range(32):
            v = self.pcr(i)
            if (v & MUX_MASK) >> MUX_SHIFT == 1:
                self.gpio_mask |= 1 << i
        for i in range(32):
            self._sync_pin(i, mux_changed=True, quiet=True)
            pin = self.pins[i]
            self._last[i] = 1 if (pin is not None and pin.level() == "H") else 0
        if self._gpio is not None:
            self._gpio._on_mux_change()

    # ------------------------------------------------------------ helpers
    def pcr(self, i: int) -> int:
        o = self._pcr_off[i]
        return 0 if o is None else int.from_bytes(self.mem[o:o + 4], "little")

    def _set_pcr_mem(self, i: int, v: int) -> None:
        o = self._pcr_off[i]
        if o is not None:
            self.mem[o:o + 4] = (v & 0xFFFFFFFF).to_bytes(4, "little")

    def mux(self, i: int) -> int:
        return (self.pcr(i) & MUX_MASK) >> MUX_SHIFT

    def _owner(self, pin: Any, mux: int) -> str:
        if mux == 1:
            return "GPIO"
        f = pin.functions.get(f"ALT{mux}")
        if mux == 0:
            return f if f else "disabled"
        if f is None:
            self.emit("warn", block=self.name, pin=pin.id, msg=f"{pin.id}: PCR MUX={mux} selects no function "
                      f"on this pin")
            return "disabled"
        return f

    def _sync_pin(self, i: int, *, mux_changed: bool, quiet: bool = False) -> None:
        pin = self.pins[i]
        if pin is None or self.chip is None or self.chip.pins is None:
            return
        v = self.pcr(i)
        mux = (v & MUX_MASK) >> MUX_SHIFT
        pull = None if (mux == 0 or not v & PE) else ("up" if v & PS else "down")
        kw: dict[str, Any] = {"pull": pull}
        if mux == 1:
            kw["owner"] = "GPIO"
            d, drv = self._gpio.pin_state(i) if self._gpio is not None else ("in", None)
            kw["direction"] = d
            kw["drive"] = drv
        elif mux_changed:
            kw["owner"] = owner = self._owner(pin, mux)
            if mux == 0:
                kw["direction"] = "analog" if pin.functions.get("ALT0") else "none"
                kw["drive"] = None
            else:
                kw["direction"], kw["drive"] = SIGNALS.pin_state(owner if owner != "disabled" else None)
        self.chip.pins.mcu_update(pin, **kw)
        if mux_changed and not quiet:
            owner = kw["owner"]
            extra = f" {kw['direction'] if kw['direction'] in ('in', 'out') else ''}".rstrip()
            if extra == " in":
                extra = " input"
            elif extra == " out":
                extra = " output"
            if pull:
                extra += f" (pull-{pull})"
            self.emit("info", block=self.name, pin=pin.id, msg=f"{pin.id} → {owner}{extra}")

    # ------------------------------------------------------------ register hooks
    def _pcr_write(self, blk, reg, old, new):
        i = reg.offset >> 2
        if old & LK:
            new = (new & ~0xFFFF) | (old & 0xFFFF)
        self._set_pcr_mem(i, new)
        self._pcr_changed(i, old, new)
        return new

    def _describe_pcr(self, pin: Any, old: int, new: int, full: bool = False) -> list[str]:
        parts = []
        ch = old ^ new
        mux = (new & MUX_MASK) >> MUX_SHIFT
        if full or ch & MUX_MASK:
            if mux == 1:
                parts.append("mux ALT1 (GPIO)")
            else:
                f = pin.functions.get(f"ALT{mux}")
                parts.append(f"mux ALT{mux} ({f or ('disabled' if mux == 0 else '?')})")
        if (full and new & PE) or ch & (PE | PS) and (ch & PE or new & PE):
            parts.append(("pull up" if new & PS else "pull down") if new & PE else "pull off")
        irqc = (new >> IRQC_SHIFT) & 0xF
        if (full and irqc) or ch & (0xF << IRQC_SHIFT):
            parts.append(f"irq {IRQC_NAMES.get(irqc, f'IRQC={irqc}')}" if irqc else "irq off")
        if (full and new & 0x20) or ch & 0x20:
            parts.append("open-drain" if new & 0x20 else "push-pull")
        if ch & LK and new & LK:
            parts.append("locked")
        return parts

    def _note_pcr(self, i: int, old: int, new: int) -> None:
        rep = self._rep
        pin = self.pins[i]
        self.pcr_writes[i] += 1
        if pin is None:
            return
        pc = rep.pc()
        parts = self._describe_pcr(pin, old, new)
        first = self.pcr_writes[i] == 1
        if not parts and first:
            parts = [p + " (unchanged)" for p in self._describe_pcr(pin, old, new, full=True)]
        if old & ISF and not new & ISF:
            rep.touch_isr(pin, pc)
        if parts:
            rep.config(pin, ", ".join(parts), pc)
        else:
            rep.touch_cfg(pin, pc)

    def pin_stats(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i, pin in enumerate(self.pins):
            if pin is None:
                continue
            irqc = (self.pcr(i) >> IRQC_SHIFT) & 0xF
            fl = bool(self.x_irq_mask >> i & 1)
            if self.pcr_writes[i] or self.isf_count[i] or irqc or fl:
                d: dict[str, Any] = {"irqs": self.isf_count[i], "pcr_writes": self.pcr_writes[i]}
                if fl:
                    d["floating"] = True
                if irqc:
                    d["irq_mode"] = IRQC_NAMES.get(irqc, f"IRQC={irqc}")
                out[pin.id] = d
        return out

    def _pcr_changed(self, i: int, old: int, new: int) -> None:
        if self._rep is not None:
            self._note_pcr(i, old, new)
        bit = 1 << i
        if old & ISF and not new & ISF:
            self._isf &= ~bit
            self._sync_isfr()
        mux_changed = (old ^ new) & MUX_MASK != 0
        if mux_changed:
            if (new & MUX_MASK) >> MUX_SHIFT == 1:
                self.gpio_mask |= bit
            else:
                self.gpio_mask &= ~bit
            if self._gpio is not None:
                self._gpio._on_mux_change()
        if mux_changed or (old ^ new) & (PE | PS):
            self._sync_pin(i, mux_changed=mux_changed)
        irqc_old, irqc_new = (old >> IRQC_SHIFT) & 0xF, (new >> IRQC_SHIFT) & 0xF
        pin = self.pins[i]
        if irqc_old != irqc_new:
            if pin is not None:
                self._last[i] = 1 if pin.level() == "H" else 0
                if irqc_new:
                    self.emit("info", block=self.name, pin=pin.id,
                              msg=f"{pin.id} interrupt: {IRQC_NAMES.get(irqc_new, f'reserved IRQC={irqc_new}')}")
        self._recheck_level(i)
        self._update_irq()

    def _gpc_write(self, blk, reg, old, new):
        hi = 16 if reg.name == "GPCHR" else 0
        data, en = new & 0xFFFF, (new >> 16) & 0xFFFF
        for k in range(16):
            if en >> k & 1:
                i = hi + k
                o = self.pcr(i)
                if o & LK:
                    continue
                ro = self.regs[f"PCR{i}"].ro_mask & 0xFFFF
                n = (o & ~0xFFFF) | (data & ~ro) | (o & ro)
                if n != o:
                    self._set_pcr_mem(i, n)
                    self._pcr_changed(i, o, n)
        return 0

    def _isfr_write(self, blk, reg, old, new):
        cleared = self._isf & ~new
        if cleared:
            rep = self._rep
            if rep is not None:
                pc = rep.pc()
                for i in range(32):
                    if cleared >> i & 1 and self.pins[i] is not None:
                        rep.touch_isr(self.pins[i], pc)
            self._isf &= new
            for i in range(32):
                if cleared >> i & 1:
                    self._set_pcr_mem(i, self.pcr(i) & ~ISF)
            for i in range(32):
                if cleared >> i & 1:
                    self._recheck_level(i)
            self._update_irq()
        return self._isf

    def _sync_isfr(self) -> None:
        o = self.regs["ISFR"].offset
        self.mem[o:o + 4] = self._isf.to_bytes(4, "little")

    # ------------------------------------------------------------ interrupts
    def _set_isf(self, i: int) -> None:
        self.isf_count[i] += 1
        self._isf |= 1 << i
        self._set_pcr_mem(i, self.pcr(i) | ISF)
        self._sync_isfr()

    def _recheck_level(self, i: int) -> None:
        if self._isf >> i & 1:
            return
        v = self.pcr(i)
        irqc = (v >> IRQC_SHIFT) & 0xF
        if irqc not in (8, 12) or not v & MUX_MASK:
            return
        pin = self.pins[i]
        if pin is None:
            return
        lvl = pin.level()
        if lvl == "X":
            self.x_irq_mask |= 1 << i
        b = 1 if lvl == "H" else 0
        if (irqc == 12) == (b == 1):
            self._set_isf(i)

    def _on_level(self, i: int, pin: Any) -> None:
        b = 1 if pin.level() == "H" else 0
        prev = self._last[i]
        self._last[i] = b
        v = self.pcr(i)
        irqc = (v >> IRQC_SHIFT) & 0xF
        if not irqc or not v & MUX_MASK:
            return
        if pin.level() == "X":
            self.x_irq_mask |= 1 << i
        if irqc in (1, 9):
            hit = prev == 0 and b == 1
        elif irqc in (2, 10):
            hit = prev == 1 and b == 0
        elif irqc in (3, 11):
            hit = prev != b
        elif irqc == 8:
            hit = b == 0
        elif irqc == 12:
            hit = b == 1
        else:
            hit = False
        if hit:
            self._set_isf(i)
            self._update_irq()

    def irq_line(self) -> bool:
        isf = self._isf
        i = 0
        while isf:
            if isf & 1 and 8 <= (self.pcr(i) >> IRQC_SHIFT) & 0xF <= 12:
                return True
            isf >>= 1
            i += 1
        return False

    def _update_irq(self) -> None:
        line = self.irq_line()
        if self._irqn is None or self.chip is None:
            self._line = line
            return
        if line != self._line:
            self.irq(self._irqn, line)          # level-sensitive: the NVIC re-pends while the line stays high
        self._line = line
