"""Address -> symbol resolution shared by the shell (`dis`/`bt`/`state`) and the pin report.

* ELF images: STT_FUNC symbols from the symbol table (`load_symbols`, `Symbols.lookup` -> "<main+0x1c>").
* Raw images (.bin/.hex/.srec, no symbols): `Symbols.name` falls back to a heuristic Thumb function start,
  shown as `sub_<ADDR>+0x<off>`: the closest address at or below the PC that is either a `bl` target found
  anywhere in the image or a `push {..., lr}` prologue (backward scan). Only if neither exists within 4 KB
  is the end of the previous function (`bx lr` / `pop {..., pc}`) used. It is a guess: leaf functions only
  reached by pointer or tail call, and data that decodes as `bl`, can mislead it.
"""
from __future__ import annotations

import bisect
from typing import Any, Callable


def load_symbols(path: str | None) -> list[tuple[int, int, str]]:
    """Sorted (addr, size, name) of the function symbols of an ELF file ([] if none / not an ELF)."""
    if not path:
        return []
    try:
        from elftools.elf.elffile import ELFFile
        from elftools.elf.sections import SymbolTableSection
        out = []
        with open(path, "rb") as f:
            elf = ELFFile(f)
            for sec in elf.iter_sections():
                if not isinstance(sec, SymbolTableSection):
                    continue
                for s in sec.iter_symbols():
                    if s["st_info"]["type"] == "STT_FUNC" and s.name:
                        out.append((s["st_value"] & ~1, s["st_size"], s.name))
        return sorted(out)
    except Exception:
        return []


def image_symbols(image: Any) -> list[tuple[int, int, str]]:
    """Function symbols of a loaded FirmwareImage: from the ELF file, or the image's own `symbols` dict
    (C28x `.c28img` / COFF: name -> word address, size unknown)."""
    if image is None:
        return []
    if getattr(image, "fmt", "") == "elf" and getattr(image, "unit_bytes", 1) == 1:
        return load_symbols(getattr(image, "source", None))
    syms = getattr(image, "symbols", None) or {}
    try:
        return sorted((int(a), 0, str(n)) for n, a in syms.items())
    except (TypeError, ValueError):
        return []


class Symbols:
    def __init__(self, syms: list[tuple[int, int, str]], read_half: Callable[[int], int | None] | None = None,
                 image: Any = None, *, thumb: bool = True):
        self.syms = syms
        self.amask = ~1 if thumb else ~0    # Thumb: bit 0 is the mode bit; C28x word addresses use every bit
        self.addrs = [s[0] for s in syms]
        self.read_half = read_half          # for the raw-image function-start heuristic
        self.image = image
        self._fn_cache: dict[int, int | None] = {}
        self._bl: list[int] | None = None

    @classmethod
    def for_image(cls, image: Any) -> "Symbols":
        thumb = getattr(image, "thumb", None)               # set by Chip.load_firmware for non-ARM chips (MCS-51)
        if thumb is None:
            thumb = getattr(image, "unit_bytes", 1) == 1
        rh = getattr(image, "read_half", None) if image is not None and thumb else None
        return cls(image_symbols(image), rh, image, thumb=thumb)

    def bl_targets(self) -> list[int]:
        """Sorted targets of every Thumb-2 `bl` encoding in the image (computed once)."""
        if self._bl is None:
            import struct
            out: set[int] = set()
            img = self.image
            for base, data in (getattr(img, "segments", None) or []):
                n = len(data) // 2
                hws = struct.unpack_from(f"<{n}H", data, 0)
                end = base + len(data)
                for k in range(n - 1):
                    h1 = hws[k]
                    if h1 & 0xF800 != 0xF000:
                        continue
                    h2 = hws[k + 1]
                    if h2 & 0xD000 != 0xD000:
                        continue
                    s_ = (h1 >> 10) & 1
                    j1, j2 = (h2 >> 13) & 1, (h2 >> 11) & 1
                    i1, i2 = 1 - (j1 ^ s_), 1 - (j2 ^ s_)
                    off = (s_ << 24) | (i1 << 23) | (i2 << 22) | ((h1 & 0x3FF) << 12) | ((h2 & 0x7FF) << 1)
                    if s_:
                        off -= 1 << 25
                    t = base + 2 * k + 4 + off
                    if base <= t < end:
                        out.add(t)
            self._bl = sorted(out)
        return self._bl

    def find(self, addr: int) -> tuple[str, int] | None:
        """(symbol name, offset) of the function containing addr, or None."""
        i = bisect.bisect_right(self.addrs, addr & self.amask) - 1
        if i < 0:
            return None
        base, size, name = self.syms[i]
        off = (addr & self.amask) - base
        if size and off >= size:
            return None
        return name, off

    def lookup(self, addr: int) -> str:
        """"<name+0x1c>" / "<name>" or "" (shell style)."""
        r = self.find(addr)
        if r is None:
            return ""
        name, off = r
        return f"<{name}+0x{off:x}>" if off else f"<{name}>"

    def guess_function(self, addr: int, max_back: int = 4096) -> int | None:
        """Heuristic start of the Thumb function containing addr (raw images)."""
        rh = self.read_half
        if rh is None:
            return None
        a0 = addr & ~1
        if a0 in self._fn_cache:
            return self._fn_cache[a0]
        lo = max(0, a0 - max_back)
        bl = self.bl_targets()
        i = bisect.bisect_right(bl, a0) - 1
        cand = bl[i] if i >= 0 and bl[i] >= lo else None
        ret_start = None
        a = a0
        stop = cand if cand is not None else lo
        while a >= stop:
            hw = rh(a)
            if hw is None:
                break
            if (hw & 0xFF00) == 0xB500:                                   # push {..., lr}
                cand = a
                break
            if hw == 0xE92D:                                              # push.w {..., lr}
                nx = rh(a + 2)
                if nx is not None and nx & 0x4000:
                    cand = a
                    break
            if ret_start is None and a < a0 and (hw == 0x4770 or (hw & 0xFF00) == 0xBD00):   # bx lr / pop {pc}
                s = a + 2
                while s < a0 and rh(s) in (0xBF00, 0x0000):               # alignment padding
                    s += 2
                ret_start = s
            a -= 2
        start = cand if cand is not None else ret_start
        self._fn_cache[a0] = start
        return start

    def name(self, addr: int | None, *, guess: bool = True) -> str:
        """"main+0x1c" for ELF images, "sub_A1B0+0x18" (heuristic) for raw ones, else "0x0000A1C8"."""
        if addr is None:
            return "?"
        r = self.find(addr)
        if r is not None:
            n, off = r
            return f"{n}+0x{off:x}" if off else n
        if guess and not self.syms:
            s = self.guess_function(addr)
            if s is not None:
                off = (addr & ~1) - s
                return f"sub_{s:X}+0x{off:x}" if off else f"sub_{s:X}"
        return f"0x{addr:08X}"

    def function_of(self, addr: int | None) -> str | None:
        """Name of the containing function (symbol or sub_XXXX guess), without offset."""
        if addr is None:
            return None
        r = self.find(addr)
        if r is not None:
            return r[0]
        if not self.syms:
            s = self.guess_function(addr)
            if s is not None:
                return f"sub_{s:X}"
        return None
