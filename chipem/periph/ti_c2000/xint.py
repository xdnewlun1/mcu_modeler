"""C2833x external interrupts (0x7070): XINT1CR..XINT7CR, XNMICR, XINT1CTR/XINT2CTR/XNMICTR.

XINTnCR: bit 0 ENABLE, bits 3:2 POLARITY (00/10 falling, 01 rising, 11 both).  The source pin is chosen in the GPIO
block (GPIOXINT1SEL/2SEL/XNMISEL: GPIO0-31, GPIOXINT3..7SEL: GPIO32-63).  A qualifying edge of the source pin's level
after GPIO input qualification (`gpio.qual_listeners`, GPxQSEL sampling windows; X counts as low) pulses XINT1 → PIE 1.4, XINT2 → 1.5, XINT3..7 → 12.1..12.5 and resets the 16-bit counter, which
then counts SYSCLKOUT cycles (XINT1CTR/XINT2CTR/XNMICTR, read-only).  XNMI (XNMICR: ENABLE, SELECT, POLARITY) pulses
the NMI, and INT13 when SELECT = 1 (XINT13).  `int_listeners` cb(n) are called with every XINTn / XINT13 (n = 13)
pulse (DMA triggers).
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ._util import C2000Block, build_regs, find_model, irq_line

_DEFAULT = {1: "1.4", 2: "1.5", 3: "12.1", 4: "12.2", 5: "12.3", 6: "12.4", 7: "12.5"}


@register("ti_c2000.xint")
class XInt(C2000Block):
    cycle_anchors = ("_edge_cycle",)

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov = {f"XINT{n}CR": {"on_write": self._cr_write} for n in range(1, 8)}
        ov["XNMICR"] = {"on_write": self._cr_write}
        ov["XINT1CTR"] = {"on_read": self._ctr_read}
        ov["XINT2CTR"] = {"on_read": self._ctr_read}
        ov["XNMICTR"] = {"on_read": self._ctr_read}
        regs = build_regs((name, "XINT"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, regs, params=params, log=log)
        self._gpio: Any = None
        self._src: dict[int, Any] = {}           # n (0 = XNMI) -> pin
        self._level: dict[int, bool] = {}
        self._edge_cycle: dict[int, int] = {}
        self._lines: dict[int, Any] = {}
        self._listeners: list[tuple[str, Any]] = []
        self.int_listeners: list = []            # cb(n) on every XINTn pulse, n = 13 for XINT13 (DMA triggers)
        self.edges = {n: 0 for n in range(8)}

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._gpio = find_model(chip, "ti_c2000.gpio")
        for n in range(1, 8):
            self._lines[n] = irq_line(chip, f"XINT{n}", _DEFAULT[n])
        self.sources_changed()

    def reset(self) -> None:
        super().reset()
        self._edge_cycle.clear()
        self.sources_changed()

    def _cr(self, n: int) -> int:
        return self.get("XNMICR" if n == 0 else f"XINT{n}CR")

    def sources_changed(self) -> None:
        """(Re)subscribe to the source pin of every XINT (called by the GPIO model on GPIOXINTnSEL writes)."""
        board = getattr(self.chip, "pins", None)
        if board is None or self._gpio is None:
            return
        ql = self._gpio.qual_listeners
        for pid, cb in self._listeners:
            lst = ql.get(pid)
            if lst is not None and cb in lst:
                lst.remove(cb)
        self._listeners = []
        for n in range(8):
            pin = self._gpio.xint_pin(n)
            self._src[n] = pin
            if pin is None:
                continue
            self._level[n] = self._gpio.qlevel(pin) == "H"
            cb = self._make_cb(n)
            ql.setdefault(pin.id, []).append(cb)
            self._listeners.append((pin.id, cb))

    def _make_cb(self, n: int):
        def on_level(pin: Any, lvl: str, _n=n) -> None:
            self._on_level(_n, pin, lvl)
        return on_level

    def _on_level(self, n: int, pin: Any, lvl: str) -> None:
        new = lvl == "H"
        old = self._level.get(n, False)
        self._level[n] = new
        if new == old or self.clk_stopped:          # STANDBY/HALT: the SYSCLKOUT-synchronised input is not sampled
            return
        cr = self._cr(n)
        if not cr & 1:
            return
        pol = (cr >> 2) & 3
        rising = new and not old
        if pol == 3 or (pol == 1 and rising) or (pol in (0, 2) and not rising):
            self.edges[n] += 1
            self._edge_cycle[n] = self.chip.now_cycles()
            if n == 0:
                self.pulse_irq("NMI")
                if cr & 2:
                    self.pulse_irq("INT13")
                    for cb in list(self.int_listeners):
                        cb(13)
            else:
                self.pulse_irq(self._lines[n])
                for cb in list(self.int_listeners):
                    cb(n)

    def _cr_write(self, blk, reg, old, new):
        if (old ^ new) & 1:
            n = 0 if reg.name == "XNMICR" else int(reg.name[4])
            pin = self._src.get(n)
            if pin is not None:
                self._level[n] = self._gpio.qlevel(pin) == "H"
            if new & 1:
                self._edge_cycle[n] = self.chip.now_cycles()
        return None

    def _ctr_read(self, blk, reg):
        n = 0 if reg.name == "XNMICTR" else int(reg.name[4])
        if not self._cr(n) & 1 or n not in self._edge_cycle:
            return 0
        return (self.chip.now_cycles() - self._edge_cycle[n]) & 0xFFFF
