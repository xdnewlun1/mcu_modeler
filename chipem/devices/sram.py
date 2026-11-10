"""Board-level asynchronous parallel SRAM (generic, vendor-free): `size` locations of `width` bits (8, 16 or 32).

The memory knows nothing about the controller in front of it.  A chip's external-bus model (any peripheral with an
`attach_memory(dev)` method, e.g. a C2000 XINTF zone `XINTF6`) decides which address lines and data lines reach it,
when it is selected and how long an access takes; it calls `read(loc)` and `write(loc, value, mask)` with a location
index (already reduced to the device's address lines) and the byte-lane / half-word write mask.  Locations wrap
modulo `size` (upper address lines not connected).  `file=` preloads a little-endian image (`width` / 8 bytes per
location); `fill=` sets the power-up content (default 0; real SRAM powers up random).
"""
from __future__ import annotations

from array import array
from pathlib import Path
from typing import Any

from .spi_flash import _parse_size

_TYPECODE = {8: "B", 16: "H", 32: "L"}


class ParallelSram:
    def __init__(self, size: int | str = "64K", width: int = 16, *, image: str | Path | None = None, fill: int = 0,
                 name: str = "sram"):
        self.name = name
        self.width = int(width)
        if self.width not in _TYPECODE:
            raise ValueError(f"sram: width must be 8, 16 or 32, not {width}")
        self.size = _parse_size(size)
        if self.size <= 0:
            raise ValueError("sram: size must be > 0")
        self.mask = (1 << self.width) - 1
        tc = _TYPECODE[self.width]
        if array(tc).itemsize * 8 < self.width:          # "L" may be 64-bit on some platforms; that is fine
            raise ValueError("sram: unsupported width on this platform")
        self.mem = array(tc, [int(fill) & self.mask]) * self.size
        if image is not None:
            self.load(Path(image).read_bytes())
        self.reads = 0
        self.writes = 0
        self.attached: list[tuple[Any, str]] = []

    def load(self, data: bytes, loc: int = 0) -> None:
        """Copy a little-endian byte image into the memory starting at location `loc`."""
        nb = self.width // 8
        for i in range(min(len(data) // nb, self.size - loc)):
            self.mem[loc + i] = int.from_bytes(data[i * nb:(i + 1) * nb], "little")

    def read(self, loc: int) -> int:
        self.reads += 1
        return self.mem[loc % self.size]

    def write(self, loc: int, value: int, mask: int | None = None) -> None:
        self.writes += 1
        i = loc % self.size
        if mask is None:
            self.mem[i] = value & self.mask
        else:
            mask &= self.mask
            self.mem[i] = (self.mem[i] & ~mask) | (value & mask)

    # ------------------------------------------------------------------ attach to a chip
    def attach(self, chip: Any, bus: str) -> "ParallelSram":
        blk = chip.peripherals.get(bus) if chip is not None else None
        if blk is None or not hasattr(blk, "attach_memory"):
            have = [n for n, b in (getattr(chip, "peripherals", {}) or {}).items() if hasattr(b, "attach_memory")]
            raise ValueError(f"sram: {bus!r} is not an external memory bus of this chip "
                             f"(have: {', '.join(have) or 'none'})")
        blk.attach_memory(self)
        self.attached.append((chip, bus))
        return self


def parse_spec(spec: str) -> tuple[str, dict]:
    """"XINTF6:size=256K,width=16,file=img.bin" -> ("XINTF6", {...})."""
    bus, _, rest = spec.partition(":")
    opts: dict[str, Any] = {}
    for part in filter(None, (p.strip() for p in rest.split(","))):
        k, _, v = part.partition("=")
        opts[k.strip().lower()] = v.strip()
    return bus.strip().upper(), opts


def attach_from_spec(chip: Any, spec: str) -> ParallelSram:
    bus, o = parse_spec(spec)
    dev = ParallelSram(o.get("size", "64K"), int(o.get("width", 16)), image=o.get("file"),
                       fill=int(str(o.get("fill", 0)), 0), name=f"{bus}_sram")
    return dev.attach(chip, bus)
