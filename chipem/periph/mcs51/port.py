"""80C51 ports P0-P3 (P89CV51 data sheet Table 3 pp.5-8, Table 4 p.11): quasi-bidirectional I/O.

Electrical model per pin (one SFR latch bit each):
  latch 0                  -> strong LOW driver: PinBoard direction "out", drive "L" (also P0)
  latch 1, P1-P3           -> weak pull-up: direction "in", pull "up" (reads H unless something drives it low)
  latch 1, P0              -> open drain released: direction "in", no pull (floats unless pulled externally)
  latch 1 + an alternate output claimed by a peripheral (UART TXD, PCA CEXn, SPI master SCK/MOSI, T2 clock-out)
                           -> owner = the signal, direction "out", drive = the signal's level (pin = latch AND alt)
  latch 1 + an alternate input claimed (UART RXD while REN = 1, SPI slave inputs, PCA capture) -> owner = signal
The pin read of `MOV A,P1` (and of every non read-modify-write instruction) returns the pin levels: L -> 0, H -> 1,
floating (X) -> 1.  Read-modify-write instructions (ANL/ORL/XRL/INC/DEC/DJNZ/JBC/CPL/CLR/SETB/MOV bit,C on a port)
read the latch instead: the model registers a latch reader with the core (`core.set_latch_reader`).
Pin reads are cached per port and invalidated by PinBoard level changes, so polling loops stay cheap.
Pin ids follow the chip YAML `gpio_name_format` ("P{port}.{bit}" -> "P1.0").
Not modelled: the two-cycle strong pull-up on a 0->1 latch transition, port drive strength, P0/P2 activity during
MOVX (mcs51.xbus logs the bus cycle instead).
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ._util import SfrBlock

PORT_SFRS = {"P0": 0x80, "P1": 0x90, "P2": 0xA0, "P3": 0xB0}


@register("mcs51.port")
class Ports(SfrBlock):
    model = "mcs51.port"
    table = ("PORTS",)

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        ov = {p: {"on_read": self._rd, "on_write": self._wr} for p in PORT_SFRS}
        super().__init__(name, base, size, regs, params=params, log=log, overrides=ov)
        self.latch = {p: 0xFF for p in range(4)}
        self._val: list[int | None] = [None] * 4
        self._pins: dict[int, list[Any]] = {}        # port -> [Pin or None] * 8
        self.claims: dict[str, tuple[str, str | None]] = {}   # pin id -> (signal, out level | None = input role)
        self.reads = 0

    # ------------------------------------------------------------------ wiring
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        pb = chip.pins
        for p in range(4):
            row = []
            for b in range(8):
                pin = pb.gpio(str(p), b) if pb is not None else None
                row.append(pin)
                if pin is not None:
                    pb.input_listeners[pin.id].append(self._make_inval(p))
            self._pins[p] = row
        core = chip.core
        setl = getattr(core, "set_latch_reader", None)
        if setl is not None:
            for p, (nm, a) in enumerate(PORT_SFRS.items()):
                setl(a, lambda _p=p: self.latch[_p])

    def _make_inval(self, p: int):
        val = self._val

        def inval(_pin, _p=p):
            val[_p] = None
        return inval

    def reset(self) -> None:
        super().reset()
        for p in range(4):
            self.latch[p] = 0xFF
            self._val[p] = None
        if self.chip is not None:
            for p in range(4):
                self._sync(p, 0xFF)

    # ------------------------------------------------------------------ SFR access
    def _rd(self, blk, reg):
        p = (reg.offset >> 4) - 8
        v = self._val[p]
        if v is None:
            v = 0
            for b, pin in enumerate(self._pins.get(p) or ()):
                if pin is None or pin.level() != "L":
                    v |= 1 << b
            self._val[p] = v
        self.reads += 1
        return v

    def _wr(self, blk, reg, old, new):
        p = (reg.offset >> 4) - 8
        prev = self.latch[p]
        self.latch[p] = new & 0xFF
        ch = prev ^ new
        if ch:
            self._sync(p, ch)
        return None

    def read_latch(self, port: int) -> int:
        return self.latch[port]

    def write_latch(self, port: int, value: int) -> None:
        """Programmatic latch write (tests, shell): same effect as a CPU write."""
        old = self.latch[port]
        self.latch[port] = value & 0xFF
        self.set(f"P{port}", value & 0xFF)
        self._sync(port, old ^ value)

    # ------------------------------------------------------------------ pins
    def _sync(self, p: int, mask: int) -> None:
        row = self._pins.get(p) or ()
        lat = self.latch[p]
        pb = self.chip.pins
        for b, pin in enumerate(row):
            if pin is None or not mask >> b & 1:
                continue
            self._apply(pb, pin, p, lat >> b & 1)
        self._val[p] = None

    def _apply(self, pb, pin, port: int, bit: int) -> None:
        claim = self.claims.get(pin.id)
        if not bit:
            pb.mcu_update(pin, owner="GPIO", direction="out", drive="L", pull=None)
            return
        if claim is not None and claim[1] is not None:
            sig, lvl = claim
            pb.mcu_update(pin, owner=sig, direction="out", drive=lvl, pull=None)
            return
        owner = claim[0] if claim is not None else "GPIO"
        pb.mcu_update(pin, owner=owner, direction="in", drive=None, pull=None if port == 0 else "up")

    def _pin_port(self, pin_id: str):
        pb = self.chip.pins
        try:
            pin = pb.get(pin_id)
        except KeyError:
            return None, None
        if pin.port is None:
            return pin, None
        return pin, int(pin.port)

    def claim(self, pin_id: str, signal: str, out: str | None = None) -> None:
        """A peripheral takes pin `pin_id` for `signal`: `out` = the level its alternate output drives (output role)
        or None (input role).  The pin shows it while the latch bit is 1."""
        if self.chip is None:
            return
        if self.claims.get(pin_id) == (signal, out):
            return
        self.claims[pin_id] = (signal, out)
        pin, port = self._pin_port(pin_id)
        if pin is not None and port is not None:
            self._apply(self.chip.pins, pin, port, self.latch[port] >> pin.bit & 1)
            self._val[port] = None

    def release(self, pin_id: str, signal: str | None = None) -> None:
        c = self.claims.get(pin_id)
        if c is None or (signal is not None and c[0] != signal):
            return
        del self.claims[pin_id]
        if self.chip is None:
            return
        pin, port = self._pin_port(pin_id)
        if pin is not None and port is not None:
            self._apply(self.chip.pins, pin, port, self.latch[port] >> pin.bit & 1)
            self._val[port] = None

    def drive_alt(self, pin_id: str, level: str) -> None:
        """Change the level of a claimed alternate output (no-op unless claimed as an output)."""
        c = self.claims.get(pin_id)
        if c is None or c[1] is None or c[1] == level:
            return
        self.claims[pin_id] = (c[0], level)
        pin, port = self._pin_port(pin_id)
        if pin is not None and port is not None and self.latch[port] >> pin.bit & 1:
            self.chip.pins.mcu_update(pin, drive=level)
            self._val[port] = None

    def pin_for(self, signal: str) -> str | None:
        """Id of the pin that offers `signal` in the chip description (e.g. "UART_TXD" -> "P3.1")."""
        pb = getattr(self.chip, "pins", None)
        if pb is None:
            return None
        for p in pb.pins:
            if any(signal in str(f).split("/") for f in (p.functions or {}).values()):
                return p.id
        return None
