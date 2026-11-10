"""System bus: memory regions (backed by core memory) + MMIO blocks + catch-all stubs for peripheral space.

* `map_memory` maps RAM/flash into the core (Unicorn memory) and fills it.
* `map_mmio` maps an MMIOBlock.  Page-aligned blocks are mapped directly; blocks that are smaller than or not
  aligned to the core's page size (1 KB on Unicorn ARM; e.g. Kinetis GPIOx at 0x400FF0x0, 0x40 bytes) share a
  per-page dispatcher whose unclaimed bytes fall back to an on-demand stub.
* Accesses to unmapped addresses inside the catch-all windows (peripheral space 0x40000000-0x400FFFFF and the
  ARM private peripheral bus 0xE0000000-0xE00FFFFF by default) create a RAM-like `stub` block of 0x1000 bytes on
  first touch (logged once), so firmware never faults on an unmodelled peripheral.  Anything else unmapped is a
  bus fault.
* Optional Cortex-M3/M4 bit-band aliases (0x22000000 → SRAM 0x20000000, 0x42000000 → peripherals 0x40000000).
* Address units (docs/CONTRACTS.md "16-bit address units"): addresses and sizes are in the chip's units
  (`addr_unit_bits`: 8 on ARM, 16 on C28x).  `read`/`write` sizes count units and values are little-endian in unit
  order; `read_bytes`/`write_bytes` work on the little-endian byte image (`unit_bytes` bytes per unit), i.e. the
  core's `read_mem(addr, n_units)` / `write_mem(addr, data)` format.  The catch-all windows come from the chip
  (`extra.auto_stub_ranges`, see Chip); stubs never extend outside their window.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Any

from .periph.base import MMIOBlock
from .periph.registry import StubBlock

DEFAULT_CATCH_ALL = ((0x40000000, 0x40100000), (0xE0000000, 0xE0100000))
BITBAND = ((0x22000000, 0x20000000), (0x42000000, 0x40000000))   # (alias base, target base); 32 MB alias each


class BusError(Exception):
    def __init__(self, addr: int, msg: str = "unmapped address"):
        super().__init__(f"{msg} 0x{addr:08x}")
        self.addr = addr


@dataclass
class MemoryRegion:
    name: str
    base: int
    size: int
    kind: str = "ram"
    writable: bool = True

    @property
    def end(self) -> int:
        return self.base + self.size

    def __repr__(self) -> str:
        return f"<MemoryRegion {self.name} {self.kind} @0x{self.base:08x}+0x{self.size:x}>"


class _PageDispatch:
    """One core page shared by several sub-page MMIO blocks."""
    __slots__ = ("bus", "base", "size", "blocks", "fallback")

    def __init__(self, bus: "Bus", base: int, size: int):
        self.bus = bus
        self.base = base
        self.size = size
        self.blocks: list[MMIOBlock] = []
        self.fallback: MMIOBlock | None = None

    def _route(self, off: int) -> tuple[MMIOBlock, int]:
        addr = self.base + off
        for b in self.blocks:
            if b.base <= addr < b.base + b.size:
                return b, addr - b.base
        if self.fallback is None:
            self.fallback = self.bus._make_stub(self.base, self.size, mapped=False)
        return self.fallback, off

    def read(self, off: int, size: int) -> int:
        b, o = self._route(off)
        return b.read(o, size)

    def write(self, off: int, size: int, value: int) -> None:
        b, o = self._route(off)
        b.write(o, size, value)


class Bus:
    MAX_SHARED_MAPS = 16                                 # map_window: fast mirrored copies, then callbacks

    def __init__(self, core: Any, *, addr_unit_bits: int = 8, log: Any = None,
                 catch_all: tuple | list | None = DEFAULT_CATCH_ALL, bitband: bool = False):
        if addr_unit_bits % 8 or addr_unit_bits <= 0:
            raise ValueError(f"addr_unit_bits must be a multiple of 8, not {addr_unit_bits}")
        self.core = core
        self.addr_unit_bits = addr_unit_bits
        self.unit_bytes = addr_unit_bits // 8
        self.log = log
        self.chip: Any = None
        self.page = getattr(core, "page_size", 0x400)
        self.catch_all = list(catch_all or ())
        self._starts: list[int] = []                     # sorted bases of _entries
        self._entries: list[tuple[int, int, Any]] = []   # (base, end, MemoryRegion | MMIOBlock)
        self._pages: dict[int, _PageDispatch] = {}
        self._direct: list[tuple[int, int]] = []          # core-level mappings not represented in _entries
        self.regions: dict[str, MemoryRegion] = {}
        self.blocks: dict[str, MMIOBlock] = {}
        self.auto_stubs: dict[int, MMIOBlock] = {}
        self._windows: dict[str, tuple] = {}            # map_window: name -> (base, size, obj, keep-alive refs)
        self._bitband: list[tuple[int, int]] = []
        core.set_unmapped_handler(self._on_unmapped)
        if bitband:
            self.enable_bitband()

    # ---------------------------------------------------------------- bookkeeping
    def _entry_overlap(self, base: int, end: int) -> Any:
        j = max(bisect.bisect_right(self._starts, base) - 1, 0)
        ent = self._entries
        while j < len(ent) and ent[j][0] < end:
            b, e, obj = ent[j]
            if base < e:
                return obj
            j += 1
        return None

    def _overlaps(self, base: int, end: int) -> Any:
        obj = self._entry_overlap(base, end)
        if obj is not None:
            return obj
        for b, e in self._direct:
            if b < end and base < e:
                return (b, e)
        for pb, p in self._pages.items():
            if pb < end and base < pb + p.size:
                return p
        return None

    def _add_entry(self, base: int, end: int, obj: Any) -> None:
        i = bisect.bisect_left(self._starts, base)
        self._starts.insert(i, base)
        self._entries.insert(i, (base, end, obj))

    def _emit(self, kind: str, **fields: Any) -> None:
        log = self.log if self.log is not None else getattr(self.chip, "log", None)
        if log is not None:
            t = self.chip.now_ns() if self.chip is not None else 0.0
            log.event(kind, t, **fields)

    # ---------------------------------------------------------------- mapping
    def map_memory(self, name: str, base: int, size: int, kind: str = "ram", data: bytes | None = None,
                   writable: bool = True, fill: int = 0) -> MemoryRegion:
        if base % self.page or size % self.page:
            raise ValueError(f"memory region {name} must be aligned to 0x{self.page:x}")
        other = self._overlaps(base, base + size)
        if other is not None:
            raise ValueError(f"memory region {name} overlaps {other!r}")
        self.core.map_memory(base, size, writable=writable)
        if fill:
            self.core.write_mem(base, bytes([fill & 0xFF]) * (size * self.unit_bytes))
        if data:
            if len(data) > size * self.unit_bytes:
                raise ValueError(f"initial data larger than region {name}")
            self.core.write_mem(base, data)
        r = MemoryRegion(name, base, size, kind, writable)
        self._add_entry(base, base + size, r)
        self.regions[name] = r
        return r

    def map_mmio(self, block: MMIOBlock) -> None:
        base, size, page = block.base, block.size, self.page
        end = base + size
        obj = self._entry_overlap(base, end)
        if obj is not None:
            raise ValueError(f"MMIO block {block.name} overlaps {obj!r}")
        if base % page == 0 and size % page == 0:
            other = self._overlaps(base, end)
            if other is not None:
                raise ValueError(f"MMIO block {block.name} overlaps {other!r}")
            self.core.map_mmio(base, size, block.read, block.write)
        else:
            pb = base - base % page
            while pb < end:
                d = self._pages.get(pb)
                if d is None:
                    for b, e in self._direct:
                        if b < pb + page and pb < e:
                            raise ValueError(f"MMIO block {block.name} overlaps mapping 0x{b:x}")
                    d = _PageDispatch(self, pb, page)
                    self._pages[pb] = d
                    self.core.map_mmio(pb, page, d.read, d.write)
                d.blocks.append(block)
                pb += page
        self._add_entry(base, end, block)
        self.blocks[block.name] = block

    # ---------------------------------------------------------------- dynamic windows (external memory buses)
    def map_window(self, name: str, base: int, size: int, *, buffer: Any = None, period: int | None = None,
                   writable: bool = True, read=None, write=None) -> Any:
        """Map an external-memory window that can come and go at run time (STM32 FSMC banks). With `buffer` (an
        object exposing the buffer protocol, e.g. an array/bytearray/ctypes buffer that is never resized) the window
        is backed by that host memory, mirrored every `period` bytes (default: the buffer size; a multiple of the
        core page): the first MAX_SHARED_MAPS copies are a fast, executable MemoryRegion (kind "ext"), further
        mirrors go through callbacks on the same buffer. Otherwise `read(off, size)` / `write(off, size, value)` are
        MMIO callbacks for the whole window. Also callable from an MMIO callback. `unmap_window(name)` removes it."""
        if name in self._windows:
            self.unmap_window(name)
        end = base + size
        other = self._overlaps(base, end)
        if other is not None:
            raise ValueError(f"window {name} overlaps {other!r}")
        if buffer is not None:
            import ctypes
            mv = memoryview(buffer).cast("B")
            nbytes = mv.nbytes
            period = int(period or nbytes)
            if period % self.page or size % period or period > nbytes:
                raise ValueError(f"window {name}: mirror period 0x{period:x} must be a multiple of the core page "
                                 f"and divide the window")
            cbuf = (ctypes.c_char * nbytes).from_buffer(mv) if not mv.readonly else None
            if cbuf is None:
                raise ValueError(f"window {name}: buffer is read-only")
            ptr = ctypes.addressof(cbuf)
            ncopy = min(size // period, max(1, self.MAX_SHARED_MAPS))
            fast_end = base + ncopy * period
            for a in range(base, fast_end, period):
                self.core.map_shared(a, period, ptr, writable=writable)
            obj: Any = MemoryRegion(name, base, fast_end - base, "ext", writable)
            self.regions[name] = obj
            tail = None
            if fast_end < end:                     # further mirrors: callbacks on the same buffer (not executable)
                tail = MMIOBlock(f"{name}_mirror", fast_end, end - fast_end, ram_like=False)
                off0 = fast_end - base

                def trd(off: int, n: int, _mv=mv, _p=period, _o=off0) -> int:
                    a = (off + _o) % _p
                    return int.from_bytes(bytes(_mv[a:a + n]), "little")

                def twr(off: int, n: int, v: int, _mv=mv, _p=period, _o=off0, _w=writable) -> None:
                    if _w:
                        a = (off + _o) % _p
                        _mv[a:a + n] = (v & ((1 << (8 * n)) - 1)).to_bytes(n, "little")
                tail.read, tail.write = trd, twr
                self.core.map_mmio(fast_end, end - fast_end, trd, twr)
                self._add_entry(fast_end, end, tail)
            keep = (mv, cbuf, tail)
        else:
            blk = MMIOBlock(name, base, size, ram_like=False)
            if read is not None:
                blk.read = read
            if write is not None:
                blk.write = write
            self.core.map_mmio(base, size, blk.read, blk.write)
            obj = blk
            keep = None
        self._add_entry(base, obj.base + obj.size if isinstance(obj, MemoryRegion) else end, obj)
        self._windows[name] = (base, size, obj, keep)
        return obj

    def unmap_window(self, name: str) -> None:
        w = self._windows.pop(name, None)
        if w is None:
            return
        base, size, obj, keep = w
        tail = keep[2] if keep else None
        if tail is not None:
            self.core.unmap(obj.base, obj.size)
            self.core.unmap(tail.base, tail.size)
        else:
            self.core.unmap(base, size)
        for o_rm in (obj, tail):
            for i, (b, e, o) in enumerate(self._entries):
                if o is o_rm and o is not None:
                    del self._entries[i]
                    del self._starts[i]
                    break
        self.regions.pop(name, None)

    def enable_bitband(self) -> None:
        for alias, target in BITBAND:
            if self._overlaps(alias, alias + 0x2000000) is not None:
                continue
            self.core.map_mmio(alias, 0x2000000, self._bb_reader(target), self._bb_writer(target))
            self._direct.append((alias, alias + 0x2000000))
            self._bitband.append((alias, target))

    def _bb_reader(self, target: int):
        def rd(off: int, size: int) -> int:
            return (self.read(target + (off >> 5), 1) >> ((off >> 2) & 7)) & 1
        return rd

    def _bb_writer(self, target: int):
        def wr(off: int, size: int, value: int) -> None:
            a = target + (off >> 5)
            bit = 1 << ((off >> 2) & 7)
            v = self.read(a, 1)
            self.write(a, 1, (v | bit) if value & 1 else (v & ~bit))
        return wr

    # ---------------------------------------------------------------- catch-all
    def _in_catch_all(self, addr: int) -> bool:
        return self._window(addr) is not None

    def _window(self, addr: int) -> tuple[int, int] | None:
        for lo, hi in self.catch_all:
            if lo <= addr < hi:
                return lo, hi
        return None

    def _make_stub(self, base: int, size: int, *, mapped: bool = True) -> MMIOBlock:
        params = {"unit_bits": self.addr_unit_bits} if self.unit_bytes != 1 else None
        blk = StubBlock(f"unmapped_{base:08x}", base, size, params=params)
        if self.chip is not None:
            blk.attach(self.chip)
        elif self.log is not None:
            blk.log = self.log
        if mapped:
            self.core.map_mmio(base, size, blk.read, blk.write)
            self._add_entry(base, base + size, blk)
        self.auto_stubs[base] = blk
        self._emit("warn", addr=base, size=size, block=blk.name,
                   msg=f"access to unmodelled region 0x{base:08x}: created RAM-like stub {blk.name}")
        return blk

    def _on_unmapped(self, addr: int, size: int, access: str) -> bool:
        win = self._window(addr)
        if win is None:
            return False
        lo, hi = win
        base = max(addr & ~0xFFF, lo)
        end = min((addr & ~0xFFF) + 0x1000, hi)
        if self._overlaps(base, end) is not None:
            base = max(addr - addr % self.page, lo)
            end = min(addr - addr % self.page + self.page, hi)
            if self._overlaps(base, end) is not None:
                return False
        self._make_stub(base, end - base)
        return True

    # ---------------------------------------------------------------- lookup / access
    def lookup(self, addr: int) -> MMIOBlock | MemoryRegion | None:
        i = bisect.bisect_right(self._starts, addr) - 1
        if i >= 0:
            b, e, obj = self._entries[i]
            if addr < e:
                return obj
        p = self._pages.get(addr - addr % self.page)
        if p is not None:
            blk, _ = p._route(addr - p.base)
            return blk
        return None

    def _resolve(self, addr: int):
        obj = self.lookup(addr)
        if obj is None:
            for alias, target in self._bitband:
                if alias <= addr < alias + 0x2000000:
                    return ("bb", alias, target)
            if self._on_unmapped(addr, 1, "r"):
                obj = self.lookup(addr)
        if obj is None:
            raise BusError(addr)
        return obj

    def read(self, addr: int, size: int) -> int:
        """Read `size` units at `addr` (little-endian in unit order)."""
        obj = self._resolve(addr)
        if isinstance(obj, MemoryRegion):
            return int.from_bytes(self.core.read_mem(addr, size), "little")
        if isinstance(obj, tuple):
            return self._bb_reader(obj[2])(addr - obj[1], size)
        return obj.read(addr - obj.base, size)

    def write(self, addr: int, size: int, value: int) -> None:
        obj = self._resolve(addr)
        if isinstance(obj, MemoryRegion):
            nb = size * self.unit_bytes
            self.core.write_mem(addr, (value & ((1 << (8 * nb)) - 1)).to_bytes(nb, "little"))
        elif isinstance(obj, tuple):
            self._bb_writer(obj[2])(addr - obj[1], size, value)
        else:
            obj.write(addr - obj.base, size, value)

    def read_bytes(self, addr: int, n: int) -> bytes:
        """`n` bytes of the little-endian byte image starting at unit address `addr`."""
        ub = self.unit_bytes
        if ub != 1:
            return self._read_bytes_units(addr, n)
        obj = self._resolve(addr)
        if isinstance(obj, MemoryRegion) and addr + n <= obj.end:
            return self.core.read_mem(addr, n)
        return bytes(self.read(addr + i, 1) for i in range(n))

    def write_bytes(self, addr: int, data: bytes) -> None:
        """Write the byte image `data` starting at unit address `addr` (a trailing partial unit keeps its other
        bytes)."""
        if self.unit_bytes != 1:
            self._write_bytes_units(addr, bytes(data))
            return
        obj = self._resolve(addr)
        if isinstance(obj, MemoryRegion) and addr + len(data) <= obj.end:
            self.core.write_mem(addr, bytes(data))
            return
        for i, b in enumerate(data):
            self.write(addr + i, 1, b)

    def _read_bytes_units(self, addr: int, n: int) -> bytes:
        ub = self.unit_bytes
        units = -(-n // ub)
        obj = self._resolve(addr)
        if isinstance(obj, MemoryRegion) and addr + units <= obj.end:
            return bytes(self.core.read_mem(addr, units))[:n]
        out = bytearray()
        for i in range(units):
            out += self.read(addr + i, 1).to_bytes(ub, "little")
        return bytes(out[:n])

    def _write_bytes_units(self, addr: int, data: bytes) -> None:
        ub = self.unit_bytes
        rem = len(data) % ub
        if rem:
            last = addr + len(data) // ub
            data = data + self._read_bytes_units(last, ub)[rem:]
        units = len(data) // ub
        obj = self._resolve(addr)
        if isinstance(obj, MemoryRegion) and addr + units <= obj.end:
            self.core.write_mem(addr, data)
            return
        for i in range(units):
            self.write(addr + i, 1, int.from_bytes(data[i * ub:(i + 1) * ub], "little"))

    def read_u32(self, addr: int) -> int:
        return self.read(addr, 4)

    def memory_regions(self) -> list[MemoryRegion]:
        return list(self.regions.values())
