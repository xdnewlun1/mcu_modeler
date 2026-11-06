"""MMIO peripheral framework: `Reg` descriptors and the `MMIOBlock` base class.

A block keeps its whole register file in a little-endian `bytearray`.  Accesses of any width (1/2/4 units)
are merged into / extracted from the containing register; register hooks always see the full register
value.  Offsets that belong to no declared register behave as plain RAM (`ram_like=True`, the default) and
are reported once per word through the chip's event log.

Address units (docs/CONTRACTS.md "16-bit address units"): offsets, sizes and register widths count the
chip's address units (`unit_bits`, 8 on ARM, 16 on C28x).  The backing store holds `unit_bytes` bytes per
unit and multi-unit values are little-endian in unit order (a 32-bit C28x register keeps its low word at the
lower address).  A block's `unit_bits` comes from the constructor argument, else `params["unit_bits"]` (the
Chip injects the chip's `addr_unit_bits`), else the class attribute `unit_bits` (8).
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

ReadHook = Callable[["MMIOBlock", "Reg"], "int | None"]
WriteHook = Callable[["MMIOBlock", "Reg", int, int], "int | None"]


class Reg:
    """Register descriptor. `width` in address units (1/2/4 bytes on ARM; 1/2 words on C28x, `unit_bits=16`);
    masks span the register width.

    on_read(block, reg) -> int | None : a non-None return value is what the CPU reads (storage unchanged).
    on_write(block, reg, old, new) -> int | None : called after `new` (masks applied) has been stored;
        a non-None return value is stored instead.
    """
    __slots__ = ("name", "offset", "width", "reset", "ro_mask", "w1c_mask", "on_read", "on_write", "mask", "doc",
                 "unit_bits", "boff", "bw", "_raw")

    def __init__(self, name: str, offset: int, width: int = 4, reset: int = 0, ro_mask: int = 0, w1c_mask: int = 0,
                 on_read: ReadHook | None = None, on_write: WriteHook | None = None, doc: str = "",
                 unit_bits: int = 8):
        if width not in (1, 2, 4, 8):
            raise ValueError(f"register {name}: width must be 1, 2, 4 or 8 units")
        self.name = name
        self.offset = offset
        self.width = width
        self.on_read = on_read
        self.on_write = on_write
        self.doc = doc
        self._raw = (reset, ro_mask, w1c_mask)
        self.set_unit_bits(unit_bits)

    def set_unit_bits(self, unit_bits: int) -> None:
        """(Re)compute masks and byte geometry for `unit_bits`-bit address units (called by MMIOBlock.add_reg)."""
        self.unit_bits = unit_bits
        ub = unit_bits // 8
        self.boff = self.offset * ub          # byte offset / byte width in the block's backing store
        self.bw = self.width * ub
        self.mask = (1 << (unit_bits * self.width)) - 1
        reset, ro_mask, w1c_mask = self._raw
        self.reset = reset & self.mask
        self.ro_mask = ro_mask & self.mask
        self.w1c_mask = w1c_mask & self.mask

    def __repr__(self) -> str:
        return f"<Reg {self.name} +0x{self.offset:x} w{self.width}>"


class MMIOBlock:
    """Base class of every memory-mapped peripheral model."""

    model = "base"
    unit_bits = 8                 # address-unit size; subclasses for 16-bit chips set 16

    def __init__(self, name: str, base: int, size: int, regs: Iterable[Reg] = (), *, ram_like: bool = True,
                 log: Any = None, params: dict | None = None, unit_bits: int | None = None):
        self.name = name
        self.base = base
        self.size = size
        self.params: dict = dict(params or {})
        ubits = int(unit_bits or self.params.get("unit_bits") or type(self).unit_bits)
        if ubits % 8 or ubits <= 0:
            raise ValueError(f"{name}: unit_bits must be a multiple of 8")
        self.unit_bits = ubits
        self.unit_bytes = ubits // 8
        self.ram_like = ram_like
        self.log = log
        self.chip: Any = None
        self.log_all = False          # log every access (--log-mmio)
        self.log_unknown = True       # log first touch of each unknown word
        self.mem = bytearray(size * self.unit_bytes)
        self.regs: dict[str, Reg] = {}
        self._by_off: dict[int, Reg] = {}
        self._seen: set[int] = set()
        for r in regs:
            self.add_reg(r)

    # ------------------------------------------------------------------ setup
    def add_reg(self, reg: Reg) -> Reg:
        if reg.offset < 0 or reg.offset + reg.width > self.size:
            raise ValueError(f"{self.name}: register {reg.name} outside block")
        if reg.unit_bits != self.unit_bits:
            reg.set_unit_bits(self.unit_bits)
        for i in range(reg.offset, reg.offset + reg.width):
            other = self._by_off.get(i)
            if other is not None:
                raise ValueError(f"{self.name}: register {reg.name} overlaps {other.name}")
            self._by_off[i] = reg
        self.regs[reg.name] = reg
        self.mem[reg.boff:reg.boff + reg.bw] = reg.reset.to_bytes(reg.bw, "little")
        return reg

    def attach(self, chip: Any) -> None:
        """Called once by the Chip after construction; gives access to clock/sched/pins/arch/log."""
        self.chip = chip
        if self.log is None:
            self.log = getattr(chip, "log", None)

    def reset(self) -> None:
        mem = self.mem
        mem[:] = bytes(len(mem))
        for r in self.regs.values():
            mem[r.boff:r.boff + r.bw] = r.reset.to_bytes(r.bw, "little")
        self._seen.clear()

    # ------------------------------------------------------------- helpers
    def now_ns(self) -> float:
        c = self.chip
        if c is None:
            return 0.0
        f = getattr(c, "now_ns", None)
        return f() if f is not None else c.clock.now_ns()

    def emit(self, kind: str, **fields: Any) -> None:
        if self.log is not None:
            self.log.event(kind, self.now_ns(), **fields)

    def irq(self, n: int, pending: bool = True) -> None:
        """Drive IRQ line n (level): True while the interrupt condition holds, False once it is cleared."""
        self.chip.arch.set_irq(n, pending)

    def pulse_irq(self, n: int) -> None:
        """Pend IRQ n once (pulse source with no status flag, e.g. the RTC seconds interrupt)."""
        arch = self.chip.arch
        f = getattr(arch, "pulse_irq", None)
        if f is not None:
            f(n)
        else:
            arch.set_irq(n, True)

    def get(self, regname: str) -> int:
        r = self.regs[regname]
        return int.from_bytes(self.mem[r.boff:r.boff + r.bw], "little")

    def set(self, regname: str, value: int) -> None:
        r = self.regs[regname]
        self.mem[r.boff:r.boff + r.bw] = (value & r.mask).to_bytes(r.bw, "little")

    def _store(self, r: Reg, value: int) -> None:
        self.mem[r.boff:r.boff + r.bw] = (value & r.mask).to_bytes(r.bw, "little")

    def _unit(self, o: int) -> int:
        """Raw stored value of unit `o` (no hooks)."""
        ub = self.unit_bytes
        if ub == 1:
            return self.mem[o]
        return int.from_bytes(self.mem[o * ub:(o + 1) * ub], "little")

    def reg_at(self, offset: int) -> Reg | None:
        return self._by_off.get(offset)

    def _unknown(self, off: int, size: int, access: str, value: int) -> None:
        if self.log is None:
            return
        key = off & ~(max(4 // self.unit_bytes, 1) - 1)     # one report per 32-bit word
        if key in self._seen:
            return
        self._seen.add(key)
        if self.log_unknown:
            self.log.event("mmio", self.now_ns(), block=self.name, addr=self.base + off, offset=off, size=size,
                           access=access, value=value, msg=f"{self.name}+0x{off:x}: unmodelled register "
                           f"({'read' if access == 'r' else 'write'} 0x{value:x})")

    # --------------------------------------------------------------- read
    def read(self, offset: int, size: int) -> int:
        r = self._by_off.get(offset)
        if r is not None and offset + size <= r.offset + r.width:
            bo = r.boff
            v = int.from_bytes(self.mem[bo:bo + r.bw], "little")
            if r.on_read is not None:
                x = r.on_read(self, r)
                if x is not None:
                    v = x & r.mask
            if size != r.width:
                ub = self.unit_bits
                v = (v >> ((offset - r.offset) * ub)) & ((1 << (size * ub)) - 1)
            if self.log_all:
                self.emit("mmio", block=self.name, addr=self.base + offset, reg=r.name, size=size, access="r", value=v)
            return v
        return self._read_slow(offset, size)

    def _read_slow(self, offset: int, size: int) -> int:
        regs_seen: dict[str, int] = {}
        out = 0
        any_reg = False
        ub = self.unit_bits
        umask = (1 << ub) - 1
        for i in range(size):
            o = offset + i
            r = self._by_off.get(o)
            if r is None:
                b = self._unit(o) if (self.ram_like and o < self.size) else 0
            else:
                any_reg = True
                if r.name not in regs_seen:
                    regs_seen[r.name] = self.read(r.offset, r.width)
                b = (regs_seen[r.name] >> ((o - r.offset) * ub)) & umask
            out |= b << (ub * i)
        if not any_reg:
            self._unknown(offset, size, "r", out)
            if self.log_all:
                self.emit("mmio", block=self.name, addr=self.base + offset, reg=None, size=size, access="r", value=out)
        return out

    # -------------------------------------------------------------- write
    def write(self, offset: int, size: int, value: int) -> None:
        r = self._by_off.get(offset)
        if r is not None and offset + size <= r.offset + r.width:
            ro = r.boff
            w = r.bw
            mem = self.mem
            old = int.from_bytes(mem[ro:ro + w], "little")
            if size == r.width:
                lane = r.mask
                val = value & lane
            else:
                ub = self.unit_bits
                sh = (offset - r.offset) * ub
                lane = ((1 << (size * ub)) - 1) << sh
                val = (value << sh) & lane
            new = (old & ~lane) | val
            if r.ro_mask:
                new = (new & ~r.ro_mask) | (old & r.ro_mask)
            if r.w1c_mask:
                new = (new & ~r.w1c_mask) | (old & r.w1c_mask & ~val)
            mem[ro:ro + w] = new.to_bytes(w, "little")
            if self.log_all:
                self.emit("mmio", block=self.name, addr=self.base + offset, reg=r.name, size=size, access="w",
                          value=value)
            if r.on_write is not None:
                x = r.on_write(self, r, old, new)
                if x is not None:
                    mem[ro:ro + w] = (x & r.mask).to_bytes(w, "little")
            return
        self._write_slow(offset, size, value)

    def _write_slow(self, offset: int, size: int, value: int) -> None:
        # group the accessed bytes by owning register (None = unknown/RAM-like)
        i = 0
        any_reg = False
        ub = self.unit_bits
        nb = self.unit_bytes
        umask = (1 << ub) - 1
        while i < size:
            o = offset + i
            r = self._by_off.get(o)
            if r is None:
                if self.ram_like and o < self.size:
                    if nb == 1:
                        self.mem[o] = (value >> (8 * i)) & 0xFF
                    else:
                        self.mem[o * nb:(o + 1) * nb] = ((value >> (ub * i)) & umask).to_bytes(nb, "little")
                i += 1
                continue
            any_reg = True
            n = 1
            while i + n < size and self._by_off.get(o + n) is r:
                n += 1
            # sub-write of n units into r; may not be 1/2/4 aligned in odd cases → do it unitwise via lanes
            sub = (value >> (ub * i)) & ((1 << (ub * n)) - 1)
            if n in (1, 2, 4, 8):
                self.write(o, n, sub)
            else:
                for k in range(n):
                    self.write(o + k, 1, (sub >> (ub * k)) & umask)
            i += n
        if not any_reg:
            self._unknown(offset, size, "w", value)
            if self.log_all:
                self.emit("mmio", block=self.name, addr=self.base + offset, reg=None, size=size, access="w",
                          value=value)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name} @0x{self.base:08x}+0x{self.size:x}>"
