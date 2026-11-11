"""Kinetis GPIO (PTx): PDOR/PSOR/PCOR/PTOR/PDIR/PDDR.

* PDOR/PSOR/PCOR/PTOR writes update PDOR and push the drive level to the PinBoard for pins that are outputs
  (PDDR bit set) and whose PORT mux is GPIO (ALT1). PSOR/PCOR/PTOR read 0.
* PDDR writes change the direction of GPIO-muxed pins on the PinBoard ("in"/"out"; outputs drive PDOR).
* PDIR read = resolved pin levels of GPIO-muxed pins (H -> 1, L/X -> 0; reading a floating X pin logs one
  `warn` per pin until reset); pins not muxed to GPIO read 0. The value is cached and invalidated by PinBoard
  input listeners and mux changes, so polling loops cost one dict lookup per read.
The matching PORT model (chip.peripherals['PORT'+port]) owns the mux and calls `_on_mux_change`/`pin_state`.

Pin discovery (chipem.pinreport): hot paths only bump counters (`n_pdir`, a per-register {value: count} dict
for PSOR/PCOR/PTOR, `n_pdor_w`); reads are attributed to the GPIO-muxed input pins lazily (`_flush_reads`,
run on mux/direction changes and when a report is built). The PC of the accessing instruction is captured only
on slow paths: PDDR changes, the first write of a new PSOR/PCOR/PTOR value, output level changes and PDIR cache
misses. `pin_stats()` hands the counters to the report.
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ._util import build_regs, find_model


def _zero(blk, reg):
    return 0


_zero.pure_read = True        # write-only register reads 0: the debugger may peek it (periph.base.peek_reg)


@register("kinetis.gpio")
class GPIO(MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.port = str(params.get("port") or name[-1]).upper()
        ov = {
            "PDOR": {"on_write": self._pdor_write},
            "PSOR": {"on_write": self._psor_write, "on_read": _zero},
            "PCOR": {"on_write": self._pcor_write, "on_read": _zero},
            "PTOR": {"on_write": self._ptor_write, "on_read": _zero},
            "PDIR": {"on_read": self._pdir_read, "on_peek": self._pdir_peek},
            "PDDR": {"on_write": self._pddr_write},
        }
        r = build_regs((name, f"GPIO{self.port}"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.pdor = 0
        self.pddr = 0
        self.pins: list[Any] = [None] * 32
        self._port: Any = None
        self._pdir: int | None = None
        self._gpio_pins: list[tuple[int, Any]] | None = None
        self._warned_x: set[int] = set()
        self._pdor_off = self.regs["PDOR"].offset
        # pin discovery counters (see module doc); kept across resets so a report covers the whole run
        self._rep: Any = None
        self.n_pdir = 0
        self._rd_acc = [0] * 32
        self._rd_mark = 0
        self._rd_mask = 0
        self._w_set: dict[int, int] = {}
        self._w_clr: dict[int, int] = {}
        self._w_tog: dict[int, int] = {}
        self._w_fold = [0] * 32
        self.n_pdor_w = 0
        self.changes = [0] * 32
        self.ever_out = 0
        self.x_read_mask = 0

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._port = find_model(chip, "kinetis.port", f"PORT{self.port}")
        if self._port is not None and self._port.port != self.port:
            self._port = None
            for b in chip.peripherals.values():
                if getattr(b, "model", None) == "kinetis.port" and getattr(b, "port", None) == self.port:
                    self._port = b
        if self._port is not None and self._port._gpio is None:
            self._port._gpio = self
        board = getattr(chip, "pins", None)
        if board is not None:
            for i in range(32):
                pin = board.gpio(self.port, i)
                self.pins[i] = pin
                if pin is not None:
                    board.input_listeners[pin.id].append(self._invalidate)
            from ...pinreport import ensure
            self._rep = ensure(chip)
            if self._rep is not None:
                self._rep.hooked = True

    def reset(self) -> None:
        super().reset()
        old_dir = self.pddr
        self.pdor = 0
        self.pddr = 0
        self._pdir = None
        self._gpio_pins = None
        self._warned_x.clear()
        if old_dir:
            self._push_dir(old_dir, quiet=True)
        self._flush_reads()

    # ------------------------------------------------------------ fast paths (polling loops / bit-banging)
    def read(self, offset: int, size: int) -> int:
        if offset == 0x10 and size == 4 and not self.log_all:
            self.n_pdir += 1
            v = self._pdir
            return v if v is not None else self._pdir_compute()
        return MMIOBlock.read(self, offset, size)

    def write(self, offset: int, size: int, value: int) -> None:
        if size == 4 and not self.log_all:
            if offset == 0x04:
                d = self._w_set
                c = d.get(value)
                d[value] = 1 if c is None else c + 1
                if c is None:
                    self._new_write_value(d, value)
                self._set_pdor(self.pdor | value)
                return
            if offset == 0x08:
                d = self._w_clr
                c = d.get(value)
                d[value] = 1 if c is None else c + 1
                if c is None:
                    self._new_write_value(d, value)
                self._set_pdor(self.pdor & ~value)
                return
            if offset == 0x0C:
                d = self._w_tog
                c = d.get(value)
                d[value] = 1 if c is None else c + 1
                if c is None:
                    self._new_write_value(d, value)
                self._set_pdor(self.pdor ^ value)
                return
        MMIOBlock.write(self, offset, size, value)

    # ------------------------------------------------------------ pin discovery (chipem.pinreport)
    def _count_write(self, d: dict, value: int) -> None:
        c = d.get(value)
        d[value] = 1 if c is None else c + 1
        if c is None:
            self._new_write_value(d, value)

    def _new_write_value(self, d: dict, value: int) -> None:
        """First write of this value to PSOR/PCOR/PTOR (or PDOR): note the writing PC for the affected pins."""
        if len(d) > 256:                        # firmware writing many distinct masks: fold into per-pin counts
            fold = self._w_fold
            for v, c in d.items():
                while v:
                    low = v & -v
                    fold[low.bit_length() - 1] += c
                    v ^= low
            d.clear()
        rep = self._rep
        if rep is None:
            return
        pc = rep.pc()
        m = value & 0xFFFFFFFF
        while m:
            low = m & -m
            pin = self.pins[low.bit_length() - 1]
            if pin is not None:
                rep.touch_data(pin, pc)
            m ^= low

    def _flush_reads(self) -> None:
        """Attribute PDIR reads since the last flush to the GPIO-muxed input pins of that interval."""
        d = self.n_pdir - self._rd_mark
        if d:
            m = self._rd_mask
            acc = self._rd_acc
            while m:
                low = m & -m
                acc[low.bit_length() - 1] += d
                m ^= low
        self._rd_mark = self.n_pdir
        p = self._port
        gm = p.gpio_mask if p is not None else 0xFFFFFFFF
        self._rd_mask = gm & ~self.pddr & sum(1 << i for i, x in enumerate(self.pins) if x is not None)

    def pin_stats(self) -> dict[str, dict]:
        self._flush_reads()
        out: dict[str, dict] = {}
        dicts = (self._w_set, self._w_clr, self._w_tog)
        for i, pin in enumerate(self.pins):
            if pin is None:
                continue
            bit = 1 << i
            w = self._w_fold[i] + sum(c for d in dicts for v, c in d.items() if v & bit)
            if self.ever_out & bit:
                w += self.n_pdor_w
            reads = self._rd_acc[i]
            fl = bool(self.x_read_mask & bit)
            if w or reads or self.changes[i] or fl:
                out[pin.id] = {"reads": reads, "writes": w, "changes": self.changes[i], "floating": fl}
        return out

    # ------------------------------------------------------------ PORT interface
    @property
    def gpio_mask(self) -> int:
        p = self._port
        return p.gpio_mask if p is not None else 0xFFFFFFFF

    def pin_state(self, i: int) -> tuple[str, str | None]:
        if self.pddr >> i & 1:
            return "out", ("H" if self.pdor >> i & 1 else "L")
        return "in", None

    def _on_mux_change(self) -> None:
        self._flush_reads()
        self._gpio_pins = None
        self._pdir = None

    def _invalidate(self, pin: Any = None) -> None:
        self._pdir = None

    # ------------------------------------------------------------ output
    def _set_pdor(self, new: int) -> None:
        new &= 0xFFFFFFFF
        old = self.pdor
        self.pdor = new
        o = self._pdor_off
        self.mem[o:o + 4] = new.to_bytes(4, "little")
        ch = (old ^ new) & self.pddr & self.gpio_mask
        if ch:
            mcu = self.chip.pins.mcu_update
            pins = self.pins
            rep = self._rep
            pc = rep.pc() if rep is not None else None
            changes = self.changes
            while ch:
                low = ch & -ch
                i = low.bit_length() - 1
                ch ^= low
                pin = pins[i]
                if pin is not None:
                    changes[i] += 1
                    if rep is not None:
                        rep.touch_data(pin, pc)
                    mcu(pin, drive="H" if new & low else "L")

    def _pdor_write(self, blk, reg, old, new):
        self.n_pdor_w += 1
        if self.n_pdor_w == 1 and self._rep is not None:
            self._new_write_value({}, self.pddr & 0xFFFFFFFF)
        self._set_pdor(new)
        return None

    def _psor_write(self, blk, reg, old, new):
        self._count_write(self._w_set, new)
        self._set_pdor(self.pdor | new)
        return 0

    def _pcor_write(self, blk, reg, old, new):
        self._count_write(self._w_clr, new)
        self._set_pdor(self.pdor & ~new)
        return 0

    def _ptor_write(self, blk, reg, old, new):
        self._count_write(self._w_tog, new)
        self._set_pdor(self.pdor ^ new)
        return 0

    def _pddr_write(self, blk, reg, old, new):
        ch = self.pddr ^ new
        self.pddr = new
        self.ever_out |= new
        if ch:
            rep = self._rep
            if rep is not None:
                pc = rep.pc()
                m = ch
                while m:
                    low = m & -m
                    i = low.bit_length() - 1
                    m ^= low
                    pin = self.pins[i]
                    if pin is not None:
                        rep.config(pin, f"dir out (drive {'H' if self.pdor & low else 'L'})" if new & low
                                   else "dir in", pc)
            self._flush_reads()
            self._push_dir(ch)
        return None

    def _push_dir(self, ch: int, quiet: bool = False) -> None:
        ch &= self.gpio_mask
        if not ch or self.chip is None or self.chip.pins is None:
            return
        for i in range(32):
            if ch >> i & 1:
                pin = self.pins[i]
                if pin is None:
                    continue
                d, drv = self.pin_state(i)
                self.chip.pins.mcu_update(pin, direction=d, drive=drv)
                if not quiet:
                    self.emit("info", block=self.name, pin=pin.id,
                              msg=f"{pin.id} → GPIO {'output' if d == 'out' else 'input'}"
                                  + (f" ({drv})" if drv else ""))
        self._pdir = None

    # ------------------------------------------------------------ input
    def _pdir_read(self, blk, reg):
        self.n_pdir += 1
        v = self._pdir
        if v is not None:
            return v
        return self._pdir_compute()

    def _pdir_peek(self, blk, reg) -> int:
        """PDIR for the debugger: the cached value or the pins' levels, without counting, warning or caching."""
        if self._pdir is not None:
            return self._pdir
        m = self.gpio_mask
        return sum(1 << i for i, p in enumerate(self.pins) if p is not None and m >> i & 1 and p.level() == "H")

    def _pdir_compute(self) -> int:
        """PDIR cache miss (first read, or an input/mux/direction changed since the last read)."""
        gp = self._gpio_pins
        if gp is None:
            m = self.gpio_mask
            gp = self._gpio_pins = [(i, p) for i, p in enumerate(self.pins) if p is not None and m >> i & 1]
        v = 0
        rep = self._rep
        pc = rep.pc() if rep is not None else None
        pddr = self.pddr
        for i, pin in gp:
            lvl = pin.level()
            if rep is not None and not pddr >> i & 1:
                rep.touch_data(pin, pc)
            if lvl == "H":
                v |= 1 << i
            elif lvl == "X":
                self.x_read_mask |= 1 << i
                if i not in self._warned_x:
                    self._warned_x.add(i)
                    self.emit("warn", block=self.name, pin=pin.id,
                              msg=f"reading floating pin {pin.id} (no pull, nothing connected): reads 0")
        self._pdir = v
        return v
