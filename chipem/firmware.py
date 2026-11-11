"""Firmware images: loaders (.bin/.hex/.elf/.srec, TI COFF2 `.out`, the C28x assembler's `.c28img`) and static
analysis against a ChipDescription.

C28x images (16-bit address units): segment addresses are word addresses and segment data is the little-endian word
stream (FirmwareImage.unit_bytes = 2).  `.out` is sniffed: ELF magic → ELF (a TI C2000 ELF, e_machine 141, is
word-addressed too), COFF version 0x00C2/0x00C1 → COFF.  `.c28img` is JSON:
  {"format": "c28img", "unit_bits": 16, "entry": <word addr>|null,
   "segments": [{"addr": <word addr>, "data_hex": "<little-endian bytes>"} | {"addr": ..., "words": [...]}],
   "symbols": {"name": <word addr>, ...}}

    image = load_firmware("fw.bin", load_addr=0x4000)
    report = analyze(image, desc)
    print(format_report(report))
"""
from __future__ import annotations

import bisect
import functools
import re
import struct
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------------------------------------------
# Image


@dataclass
class FirmwareImage:
    """Loaded image.  Segment addresses are in the chip's address units; segment data is the little-endian byte
    image (`unit_bytes` bytes per unit: 1 on ARM, 2 for C28x word streams).  `start/end/size` and the `n` of
    `contains/read` count units; `read` returns `n * unit_bytes` bytes."""
    segments: list[tuple[int, bytes]]          # (load address, data), sorted, non-overlapping
    entry: int | None = None
    source: str = ""
    fmt: str = ""
    unit_bytes: int = 1
    symbols: dict = field(default_factory=dict)  # name -> address (formats that carry them: .c28img, COFF)

    def __post_init__(self):
        self.segments = _merge_segments(self.segments, self.unit_bytes)

    @property
    def start(self) -> int:
        return self.segments[0][0] if self.segments else 0

    @property
    def end(self) -> int:
        ub = self.unit_bytes
        return max((a + len(d) // ub for a, d in self.segments), default=0)

    @property
    def size(self) -> int:
        return sum(len(d) for _, d in self.segments) // self.unit_bytes

    def contains(self, addr: int, n: int = 1) -> bool:
        ub = self.unit_bytes
        return any(a <= addr and addr + n <= a + len(d) // ub for a, d in self.segments)

    def read(self, addr: int, n: int) -> bytes | None:
        ub = self.unit_bytes
        for a, d in self.segments:
            if a <= addr and addr + n <= a + len(d) // ub:
                return d[(addr - a) * ub: (addr - a + n) * ub]
        return None

    def read_word(self, addr: int) -> int | None:
        b = self.read(addr, 4)
        return None if b is None else struct.unpack("<I", b)[0]

    def read_half(self, addr: int) -> int | None:
        b = self.read(addr, 2)
        return None if b is None else struct.unpack("<H", b)[0]

    def rebased(self, load_addr: int) -> FirmwareImage:
        """Shift every segment so the image starts at load_addr (for raw .bin images)."""
        delta = load_addr - self.start
        return FirmwareImage([(a + delta, d) for a, d in self.segments],
                             None if self.entry is None else self.entry + delta, self.source, self.fmt,
                             self.unit_bytes, {k: v + delta for k, v in self.symbols.items()})


def _merge_segments(segs: list[tuple[int, bytes]], ub: int = 1) -> list[tuple[int, bytes]]:
    segs = sorted(((int(a), bytes(d)) for a, d in segs if len(d)), key=lambda s: s[0])
    out: list[tuple[int, bytearray]] = []
    for a, d in segs:
        if ub != 1 and len(d) % ub:
            d = d + b"\0" * (ub - len(d) % ub)
        if out and a <= out[-1][0] + len(out[-1][1]) // ub:
            pa, pd = out[-1]
            off = (a - pa) * ub
            if off + len(d) > len(pd):
                pd.extend(b"\0" * (off + len(d) - len(pd)))
            pd[off:off + len(d)] = d            # later data wins on overlap
        else:
            out.append((a, bytearray(d)))
    return [(a, bytes(d)) for a, d in out]


# ---------------------------------------------------------------------------------------------------------------
# Loaders

class FirmwareError(ValueError):
    pass


COFF_MAGICS = (0x00C2, 0x00C1, 0x00C0)       # TI COFF2 / COFF1 / COFF0 version ids
EM_TI_C2000 = 141


def detect_format(path: str | Path, data: bytes | None = None) -> str:
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".c28img":
        return "c28img"
    if ext == ".out":
        data = data if data is not None else p.read_bytes()[:16]
        if data[:4] == b"\x7fELF":
            return "elf"
        if len(data) >= 2 and struct.unpack_from("<H", data)[0] in COFF_MAGICS:
            return "coff"
        return "elf"
    if ext in (".elf", ".axf"):
        return "elf"
    if ext in (".hex", ".ihex", ".ihx"):
        return "hex"
    if ext in (".srec", ".s19", ".s28", ".s37", ".mot", ".s"):
        return "srec"
    if ext == ".bin":
        return "bin"
    data = data if data is not None else p.read_bytes()[:16]
    if data[:4] == b"\x7fELF":
        return "elf"
    if data[:1] == b"{":
        head = p.read_bytes()[:512] if p.exists() else data
        if b"c28img" in head or b'"segments"' in head:
            return "c28img"
    if data[:1] == b":":
        return "hex"
    if data[:1] == b"S" and data[1:2].isdigit():
        return "srec"
    return "bin"


def load_firmware(path: str | Path, *, load_addr: int | None = None, fmt: str | None = None,
                  unit_bytes: int = 1) -> FirmwareImage:
    """Load an image.  `unit_bytes` (2 for C28x chips) only matters for raw `.bin` images, whose load address is
    then a word address; the COFF/ELF/c28img loaders know their own unit size."""
    path = Path(path)
    data = path.read_bytes()
    fmt = (fmt or detect_format(path, data[:16])).lower()
    if fmt in ("bin", "raw"):
        img = FirmwareImage([(load_addr or 0, data)], None, str(path), "bin", unit_bytes)
    elif fmt in ("coff", "out"):
        img = parse_coff(data, source=str(path))
    elif fmt == "c28img":
        img = parse_c28img(data.decode("utf-8"), source=str(path))
    elif fmt in ("hex", "ihex"):
        img = parse_ihex(data.decode("ascii", "replace"), source=str(path))
        img.symbols = sidecar_symbols(path)
    elif fmt in ("srec", "s19", "s28", "s37"):
        img = parse_srec(data.decode("ascii", "replace"), source=str(path))
    elif fmt == "elf":
        img = parse_elf(path)
    else:
        raise FirmwareError(f"unknown firmware format {fmt!r}")
    if load_addr is not None and img.fmt != "bin":
        img = img.rebased(load_addr)
    return img


_MAP_LINE = re.compile(r"^\s*([CDIXB]):\s*(?:0[xX])?([0-9A-Fa-f]{4,8})\s+([A-Za-z_?$.][\w?$.]*)\s*$")
_CDB_LINE = re.compile(r"^L:(?:G|F[^$]*)\$([A-Za-z_]\w*)\$[^:]*:([0-9A-Fa-f]+)\s*$")
_NOI_LINE = re.compile(r"^DEF\s+([A-Za-z_?$.][\w?$.]*)\s+(?:0[xX])?([0-9A-Fa-f]+)\s*$")
_SPACE_TAG = {"C": 0x00000, "X": 0x10000, "D": 0x20000, "I": 0x20000}      # MCS-51 tagged address spaces


def sidecar_symbols(path: str | Path) -> dict[str, int]:
    """Symbols of an Intel HEX image from files next to it (8051 toolchains keep them apart from the .hex):
    `<stem>.map` (tools/mcs51_asm.py `C:0x0030 main` lines, or an SDCC/aslink linker map `C:   00000030  _main`),
    SDCC `<stem>.cdb` (`L:G$main$0$0:30` function/global labels) and `<stem>.noi` (`DEF _main 0x30`).  Code labels
    (C) keep their address; data (D/I) and XDATA (X) symbols are given the MCS-51 tagged addresses (IDATA 0x20000,
    XDATA 0x10000; docs/MCS51_CORE.md).  Missing or unreadable files give no symbols."""
    p = Path(path)
    out: dict[str, int] = {}
    for ext in (".map", ".cdb", ".noi"):
        f = p.with_suffix(ext)
        if not f.exists():
            continue
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = _MAP_LINE.match(line)
            if m:
                if m.group(1) in _SPACE_TAG:
                    out.setdefault(m.group(3), _SPACE_TAG[m.group(1)] + int(m.group(2), 16))
                continue
            m = _CDB_LINE.match(line) or _NOI_LINE.match(line)
            if m:
                out.setdefault(m.group(1), int(m.group(2), 16))
    return out


def parse_ihex(text: str, *, source: str = "") -> FirmwareImage:
    segs: list[tuple[int, bytes]] = []
    base = 0
    entry = None
    cur_addr, cur = None, bytearray()
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise FirmwareError(f"{source}:{lineno}: not an Intel HEX record")
        try:
            rec = bytes.fromhex(line[1:])
        except ValueError:
            raise FirmwareError(f"{source}:{lineno}: bad hex digits") from None
        if len(rec) < 5 or len(rec) != rec[0] + 5:
            raise FirmwareError(f"{source}:{lineno}: bad record length")
        if sum(rec) & 0xFF:
            raise FirmwareError(f"{source}:{lineno}: checksum mismatch")
        n, addr, typ, payload = rec[0], (rec[1] << 8) | rec[2], rec[3], rec[4:4 + rec[0]]
        if typ == 0x00:
            a = base + addr
            if cur_addr is not None and a == cur_addr + len(cur):
                cur.extend(payload)
            else:
                if cur_addr is not None:
                    segs.append((cur_addr, bytes(cur)))
                cur_addr, cur = a, bytearray(payload)
        elif typ == 0x01:
            break
        elif typ == 0x02:
            base = int.from_bytes(payload, "big") << 4
        elif typ == 0x04:
            base = int.from_bytes(payload, "big") << 16
        elif typ == 0x03:
            cs, ip = int.from_bytes(payload[:2], "big"), int.from_bytes(payload[2:4], "big")
            entry = (cs << 4) + ip
        elif typ == 0x05:
            entry = int.from_bytes(payload, "big")
        else:
            raise FirmwareError(f"{source}:{lineno}: unknown record type {typ:#x}")
        del n
    if cur_addr is not None:
        segs.append((cur_addr, bytes(cur)))
    return FirmwareImage(segs, entry, source, "hex")


def parse_srec(text: str, *, source: str = "") -> FirmwareImage:
    segs: list[tuple[int, bytes]] = []
    entry = None
    cur_addr, cur = None, bytearray()
    alen = {"1": 2, "2": 3, "3": 4, "5": 2, "6": 3, "7": 4, "8": 3, "9": 2, "0": 2}
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if len(line) < 4 or line[0] != "S" or line[1] not in alen:
            raise FirmwareError(f"{source}:{lineno}: not an S-record")
        t = line[1]
        try:
            rec = bytes.fromhex(line[2:])
        except ValueError:
            raise FirmwareError(f"{source}:{lineno}: bad hex digits") from None
        if len(rec) != rec[0] + 1:
            raise FirmwareError(f"{source}:{lineno}: bad record length")
        if (sum(rec[:-1]) & 0xFF) ^ 0xFF != rec[-1]:
            raise FirmwareError(f"{source}:{lineno}: checksum mismatch")
        al = alen[t]
        addr = int.from_bytes(rec[1:1 + al], "big")
        payload = rec[1 + al:-1]
        if t in "123":
            if cur_addr is not None and addr == cur_addr + len(cur):
                cur.extend(payload)
            else:
                if cur_addr is not None:
                    segs.append((cur_addr, bytes(cur)))
                cur_addr, cur = addr, bytearray(payload)
        elif t in "789":
            entry = addr
    if cur_addr is not None:
        segs.append((cur_addr, bytes(cur)))
    return FirmwareImage(segs, entry, source, "srec")


def parse_elf(path: str | Path) -> FirmwareImage:
    """ELF executable: PT_LOAD segments at their load address (p_paddr).  A TI C2000 ELF (EABI, e_machine
    EM_TI_C2000 = 141, SPRAC71 §11.2) is word-addressed: addresses (p_paddr, e_entry, st_value) count 16-bit words
    while sizes (p_filesz) count bytes (SPRAC71 §11), so the segment data is the little-endian word stream as is.
    For C2000 the function and object symbols are kept in `symbols` (ARM ELF symbols are read by symbols.py)."""
    from elftools.elf.elffile import ELFFile
    from elftools.elf.sections import SymbolTableSection
    segs = []
    syms: dict[str, int] = {}
    with open(path, "rb") as f:
        elf = ELFFile(f)
        mach = elf.header["e_machine"]
        ub = 2 if mach in (EM_TI_C2000, "EM_TI_C2000") else 1
        for seg in elf.iter_segments():
            if seg["p_type"] != "PT_LOAD" or seg["p_filesz"] == 0:
                continue
            addr = seg["p_paddr"]
            if ub == 2:
                addr &= 0x3FFFFF
            segs.append((addr, seg.data()[:seg["p_filesz"]]))   # LMA: .data initialisers land in flash
        entry = elf.header["e_entry"]
        if ub == 2:
            for sec in elf.iter_sections():
                if not isinstance(sec, SymbolTableSection):
                    continue
                for sym in sec.iter_symbols():
                    st = sym["st_info"]["type"]
                    if (sym.name and st in ("STT_FUNC", "STT_OBJECT", "STT_NOTYPE") and sym["st_shndx"] != "SHN_UNDEF"
                            and sym["st_shndx"] != "SHN_ABS" and not sym.name.startswith(("$", "."))):
                        syms.setdefault(sym.name, sym["st_value"] & 0x3FFFFF)
            entry &= 0x3FFFFF
            if not entry and "_c_int00" in syms:
                entry = syms["_c_int00"]
    return FirmwareImage(segs, entry, str(path), "elf", ub, syms)


# TI COFF (SPRAAO8 "Common Object File Format", cached in svd/c28x-docs/dl3): file header 22 bytes, optional header
# 28, section headers 48 (COFF2: ... 40-43 flags, 44-45 reserved, 46-47 memory page) or 40 (COFF0/1), symbols 18
# bytes.  For C2800 (target id 0x009D) section sizes, addresses and symbol values count 16-bit words; raw data is
# the little-endian word stream.  The memory page (PAGE 0 program / PAGE 1 data in the linker command file) is
# ignored: the C28x has one unified 22-bit space.
STYP_DSECT, STYP_NOLOAD, STYP_COPY, STYP_BSS = 0x01, 0x02, 0x10, 0x80
C_EXT, C_STAT, C_LABEL = 2, 3, 6
COFF_TARGET_C2800 = 0x009D


def _coff_cinit(raw: bytes) -> list[tuple[int, bytes]] | None:
    """Records of a COFF `.cinit` table (SPRU514 7.10.3.1: size in words (stored negative), 32-bit destination
    address (two words, low first), data; a zero size ends the table).  Used for `--ram_model` links, where the
    linker marks .cinit STYP_COPY and the loader performs the initialisation.  None if the table does not parse
    exactly (then nothing is applied)."""
    n = len(raw) // 2
    w = struct.unpack_from(f"<{n}H", raw, 0)
    out, i = [], 0
    while i < n:
        size = w[i]
        if size == 0:
            break
        size = abs(size - 0x10000 if size & 0x8000 else size)
        if i + 3 + size > n:
            return None
        addr = w[i + 1] | (w[i + 2] << 16)
        if addr > 0x3FFFFF:
            return None
        out.append((addr, raw[2 * (i + 3):2 * (i + 3 + size)]))
        i += 3 + size
    return out


def parse_coff(data: bytes, *, source: str = "") -> FirmwareImage:
    """TI COFF executable (cl2000 --abi=coffabi).  Loads every initialised, allocated section at its load (physical)
    address; skips .ebss/.bss/.stack/.esysmem (STYP_BSS), NOLOAD/DSECT and COPY sections (debug info), except a
    COPY `.cinit` (--ram_model), whose records are applied as load-time initialisation.  With --rom_model (the usual
    choice) `.cinit` is an ordinary section and `_c_int00` copies it at run time.  Symbols: external and static
    symbols of real sections, by their COFF name (C names carry a leading underscore: `_main`, `_c_int00`)."""
    if len(data) < 22:
        raise FirmwareError(f"{source}: too short for a COFF file")
    ver, nscns, _ts, symptr, nsyms, opthdr, flags, target = struct.unpack_from("<HHiiiHHH", data, 0)
    if ver not in COFF_MAGICS:
        raise FirmwareError(f"{source}: not a TI COFF file (version id 0x{ver:04X})")
    little = not flags & 0x0200                      # F_BIG
    if not little:
        raise FirmwareError(f"{source}: big-endian COFF is not supported")
    if target != COFF_TARGET_C2800:
        raise FirmwareError(f"{source}: COFF target id 0x{target:04X} is not TMS320C2800 (0x009D)")
    entry = None
    if opthdr >= 28:
        _magic, _vs, _ts2, _ds, _bs, ent, _tst, _dst = struct.unpack_from("<hhiiiiii", data, 22)
        entry = ent & 0x3FFFFF
    ub = 2                                            # target 0x009D = TMS320C2800 (16-bit addressable units)
    shsz = 48 if ver == 0x00C2 else 40
    strtab = symptr + 18 * nsyms if symptr else None

    def name_at(raw: bytes) -> str:
        if raw[:4] == b"\0\0\0\0" and strtab is not None:
            off = struct.unpack_from("<I", raw, 4)[0]
            end = data.find(b"\0", strtab + off)
            return data[strtab + off:end].decode("ascii", "replace")
        return raw.split(b"\0", 1)[0].decode("ascii", "replace")

    segs = []
    pos = 22 + opthdr
    for _ in range(nscns):
        h = data[pos:pos + shsz]
        if len(h) < shsz:
            raise FirmwareError(f"{source}: truncated section header table")
        if ver == 0x00C2:
            paddr, _vaddr, size, scnptr, _rel, _res, _nrel, _res2, sflags = struct.unpack_from("<iiiiiiIII", h, 8)
        else:
            paddr, _vaddr, size, scnptr, _rel, _lnno, _nrel, _nlnno, sflags = struct.unpack_from("<iiiiiiHHH", h, 8)
        pos += shsz
        if size <= 0 or scnptr <= 0 or sflags & (STYP_BSS | STYP_NOLOAD | STYP_DSECT):
            continue
        is_cinit = name_at(h[:8]) == ".cinit"
        if sflags & STYP_COPY and not is_cinit:
            continue        # debug info / build attributes: not loaded, and cl2000 gives their size in bytes
        raw = data[scnptr:scnptr + size * ub]
        if len(raw) < size * ub:
            raise FirmwareError(f"{source}: section {name_at(h[:8])} data runs past the end of the file")
        if sflags & STYP_COPY:          # --ram_model .cinit (size in words like every target section)
            segs.extend(_coff_cinit(raw) or [])
            continue
        segs.append((paddr & 0x3FFFFF, raw))
    syms: dict[str, int] = {}
    if symptr and nsyms:
        i = 0
        while i < nsyms:
            o = symptr + 18 * i
            if o + 18 > len(data):
                break
            raw = data[o:o + 8]
            value, scn, _typ, sclass, naux = struct.unpack_from("<ihHBB", data, o + 8)
            if scn > 0 and sclass in (C_EXT, C_STAT, C_LABEL) and value >= 0:
                nm = name_at(raw)
                if nm and not nm.startswith((".", "$")):
                    syms.setdefault(nm, value & 0x3FFFFF)
            i += 1 + naux
    if not segs:
        raise FirmwareError(f"{source}: COFF file has no loadable sections")
    if entry is None:
        entry = syms.get("_c_int00")
    return FirmwareImage(segs, entry, source, "coff", ub, syms)


def parse_c28img(text: str, *, source: str = "") -> FirmwareImage:
    import json
    try:
        d = json.loads(text)
    except ValueError as e:
        raise FirmwareError(f"{source}: bad .c28img JSON: {e}") from None
    ub = int(d.get("unit_bits", 16)) // 8
    segs = []
    for sg in d.get("segments") or []:
        addr = int(sg["addr"], 0) if isinstance(sg["addr"], str) else int(sg["addr"])
        if "data_hex" in sg:
            raw = bytes.fromhex(sg["data_hex"])
        else:
            raw = b"".join((int(w) & 0xFFFF).to_bytes(2, "little") for w in sg.get("words") or [])
        segs.append((addr, raw))
    entry = d.get("entry")
    if isinstance(entry, str):
        entry = int(entry, 0)
    syms = {str(k): (int(v, 0) if isinstance(v, str) else int(v)) for k, v in (d.get("symbols") or {}).items()}
    return FirmwareImage(segs, entry, source, "c28img", ub, syms)


# ---------------------------------------------------------------------------------------------------------------
# Static analysis

SYSTEM_EXCEPTIONS = ["Initial_SP", "Reset", "NMI", "HardFault", "MemManage", "BusFault", "UsageFault",
                     "Reserved7", "Reserved8", "Reserved9", "Reserved10", "SVCall", "DebugMon", "Reserved13",
                     "PendSV", "SysTick"]

# ARMv7-M private peripheral bus blocks (not in the chip YAML: the arch layer owns them)
ARM_PPB = [("ITM", 0xE0000000, 0x1000), ("DWT", 0xE0001000, 0x1000), ("FPB", 0xE0002000, 0x1000),
           ("SCS", 0xE000E000, 0x1000), ("TPIU", 0xE0040000, 0x1000), ("ETM", 0xE0041000, 0x1000)]



def exception_name(idx: int, desc: Any = None) -> str:
    if idx < 16:
        return SYSTEM_EXCEPTIONS[idx]
    n = idx - 16
    if desc is not None and n in desc.irqs:
        return desc.irqs[n]
    return f"IRQ{n}"


@dataclass
class PeriphRef:
    name: str
    count: int = 0
    samples: list[int] = field(default_factory=list)      # distinct referenced addresses (first few)


@dataclass
class PinRole:
    pin: str                  # e.g. "PTC5"
    reg: str                  # e.g. "PORTC_PCR5" / "GPIOC_PDDR"
    pc: int                   # address of the store instruction
    value: int | None = None  # stored value if it could be determined
    note: str = ""


@dataclass
class AnalysisReport:
    chip: str
    source: str
    fmt: str
    segments: list[tuple[int, int, str]]                 # (addr, size, region name)
    load_addr: int
    vtor: int
    initial_sp: int | None
    reset_pc: int | None
    bootable: bool
    boot_problems: list[str]
    num_vectors: int
    default_handler: int | None
    handlers: list[tuple[int, str, int]]                 # (vector index, name, address) for non-default handlers
    periph_refs: dict[str, PeriphRef]
    memory_refs: dict[str, int]
    store_sites: dict[str, int]                          # capstone: stores per peripheral
    code_refs: dict[str, int]                            # capstone: ldr [pc] / movw+movt address loads per peripheral
    pin_roles: list[PinRole]
    pins_configured: dict[str, list[str]]                # pin -> list of notes (summary of pin_roles)
    disasm_insns: int = 0
    disasm_seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)
    arch: str = "armv7m"                                 # "c28x": vtor = boot entry, reset_pc = its LB target
    unit_bytes: int = 1
    symbols: dict = field(default_factory=dict)          # C28x: the image's symbols (COFF / C2000 ELF / c28img)


class _RangeMap:
    """addr -> (name, base, obj) via bisect over sorted, non-overlapping ranges."""

    def __init__(self, ranges: list[tuple[int, int, str, Any]]):
        self.r = sorted(ranges)
        self.starts = [r[0] for r in self.r]

    def find(self, addr: int):
        i = bisect.bisect_right(self.starts, addr) - 1
        if i >= 0:
            base, size, name, obj = self.r[i]
            if addr < base + size:
                return name, base, obj
        return None


def _periph_map(desc: Any) -> _RangeMap:
    rs = [(p.base, p.size, p.name, p) for p in desc.peripherals]
    taken = {p.base for p in desc.peripherals}
    rs += [(b, s, n, None) for n, b, s in ARM_PPB if b not in taken]
    return _RangeMap(rs)


def _is_prologue(hw0: int, hw1: int | None) -> bool:
    """Heuristic: does this Thumb code start like a function?"""
    if (hw0 & 0xFF00) == 0xB500:                 # push {..., lr}
        return True
    if (hw0 & 0xFE00) == 0xB400 and hw0 & 0xFF:  # push {r..} (no lr)
        return True
    if hw0 == 0xE92D and hw1 is not None and (hw1 & 0x4000):   # stmdb sp!, {..., lr}  (push.w)
        return True
    if (hw0 & 0xFF80) == 0xB080:                 # sub sp, #imm
        return True
    if hw0 == 0xE7FE:                            # b .  (default handler spin)
        return True
    if hw0 == 0xED2D:                            # vpush
        return True
    if hw0 == 0x4770 or hw0 == 0x4685 or hw0 == 0x4668:   # bx lr / mov sp,r0 / mov r0,sp
        return True
    if (hw0 & 0xF800) == 0x4800:                 # ldr rX, [pc, #imm]  (common first insn of leaf handlers)
        return True
    if hw0 in (0xB672, 0xB662, 0xF3EF):          # cpsid i / cpsie i / mrs
        return True
    return False


def score_load_addr(data: bytes, base: int, num_vectors: int = 112) -> int:
    """How many distinct vector entries point at plausible function starts if `data` is placed at `base`."""
    n = min(num_vectors, len(data) // 4)
    if n < 2:
        return 0
    words = struct.unpack_from(f"<{n}I", data, 0)
    seen = set()
    score = 0
    for i, v in enumerate(words[1:], 1):
        if v == 0 or not (v & 1) or v in seen:
            continue
        seen.add(v)
        off = (v & ~1) - base
        if off < 0 or off + 4 > len(data):
            continue
        hw0, hw1 = struct.unpack_from("<HH", data, off)
        if _is_prologue(hw0, hw1):
            score += 1
    return score


LOAD_ADDR_CANDIDATES = (0x0, 0x1000, 0x2000, 0x4000, 0x8000, 0x10000)


def guess_load_addr_scores(image: FirmwareImage, desc: Any = None,
                           candidates: tuple[int, ...] = LOAD_ADDR_CANDIDATES) -> dict[int, int]:
    data = image.segments[0][1] if image.segments else b""
    nv = 16 + (desc.core.num_irqs if desc is not None else 96)
    return {base: score_load_addr(data, base, nv) for base in candidates}


def guess_load_addr(image: FirmwareImage, desc: Any = None,
                    candidates: tuple[int, ...] = LOAD_ADDR_CANDIDATES) -> int:
    """Best load address for a raw image: the base where most vector entries hit function prologues
    (ties resolve to the lowest base)."""
    scores = guess_load_addr_scores(image, desc, candidates)
    best = max(scores.values(), default=0)
    if best == 0:
        return candidates[0]
    return min(b for b, s in scores.items() if s == best)


def _region_name(desc: Any, addr: int) -> str:
    m = desc.memory_at(addr)
    return m.name if m else "?"


_RE_LDR_PC = re.compile(r"^(r\d+|sb|sl|fp|ip), \[pc, #(-?0x[0-9a-f]+|-?\d+)\]$")
_RE_MEM = re.compile(r"^(r\d+|sb|sl|fp|ip|lr), \[(r\d+|sb|sl|fp|ip)(?:, #(-?0x[0-9a-f]+|-?\d+))?\]$")
_NO_DEST = {"cmp", "cmn", "tst", "teq", "push", "it", "ite", "itt", "itet", "itte", "ittt", "itee", "nop", "vstr",
            "stm", "stmia", "stmdb", "dsb", "isb", "dmb", "cpsid", "cpsie", "wfi", "wfe", "svc", "bkpt", "vpush",
            "strd", "vstmia", "vstmdb", "msr", "sev"}
_RE_MOVI = re.compile(r"^(r\d+|sb|sl|fp|ip), #(-?0x[0-9a-f]+|-?\d+)$")


def _flash_ranges(image: FirmwareImage, desc: Any) -> list[tuple[int, bytes]]:
    out = []
    for a, d in image.segments:
        m = desc.memory_at(a)
        if m is None or m.kind in ("flash", "flexnvm", "rom"):
            out.append((a, d))
    return out


def _add_sample(samples: dict[str, list[int]], name: str, v: int):
    lst = samples.setdefault(name, [])
    if v not in lst and len(lst) < 6:
        lst.append(v)


class _PinCtx:
    """Per-analysis helpers for pin-role detection: the chip's register table (names any block's registers by
    offset) and (port, bit) / GPIO-name -> pin lookups."""

    def __init__(self, desc: Any):
        self.desc = desc
        self.rt = _chip_regtable(desc)
        self._pr: dict[str, Any] = {}
        self.by_pb: dict[tuple[str, int], Any] = {}
        self.by_gpio: dict[str, Any] = {}
        for p in getattr(desc, "pins", None) or []:
            if p.port is not None and p.bit is not None:
                self.by_pb.setdefault((str(p.port), int(p.bit)), p)
                g = p.gpio_name
                if g:
                    self.by_gpio.setdefault(g, p)
        self.unit_bits = int(getattr(desc.core, "addr_unit_bits", 8) or 8)

    def periph_regs(self, pname: str, pdesc: Any):
        if pname not in self._pr:
            pr = None
            if self.rt is not None:
                pr = self.rt.get(pname)
                if pr is None and pdesc is not None and (pdesc.params or {}).get("svd_name"):
                    pr = self.rt.get(pdesc.params["svd_name"])
            self._pr[pname] = pr
        return self._pr[pname]

    def pin(self, port: str, bit: int):
        return self.by_pb.get((str(port), int(bit)))


def _chip_regtable(desc: Any):
    """The chip's register table: `regs_file` (relative to the chip YAML), else chips/mk22_regs.yaml for Kinetis."""
    from .regtable import DEFAULT_REGS, load_regs
    rf = getattr(desc, "regs_file", None)
    path = None
    if rf:
        path = Path(rf)
        src = getattr(desc, "source_path", None)
        if not path.is_absolute() and src is not None:
            path = Path(src).parent / path
        if not path.exists():
            path = DEFAULT_REGS.parent / Path(rf).name
    elif str(getattr(desc, "family", "")).lower() == "kinetis":
        path = DEFAULT_REGS
    if path is None or not Path(path).exists():
        return None
    try:
        return load_regs(path)
    except Exception:                                    # pragma: no cover - best effort
        return None


_STM32_MODE = {0: "input", 1: "output", 2: "AF", 3: "analog"}
_STM32_PULL = {0: "no pull", 1: "pull-up", 2: "pull-down", 3: "pull (reserved 3)"}


def _pin_roles(pc_: _PinCtx, pdesc: Any, pname: str, off: int, pc: int, val: int | None,
               kmask: int = 0) -> list[PinRole]:
    """Pin roles implied by a store of `val` to `pname`+`off` (in address units).  `kmask`: which bits of the stored
    value are known (val holds them; full mask when the value is an immediate); unknown bits are assumed to keep
    their reset value.  Registers are named from the chip's register table; decoding is per family:
    Kinetis PORTx_PCRn (mux/pull/IRQC) and GPIOx_PDDR/PSOR/...; STM32 GPIOx MODER/PUPDR/OTYPER/AFRL/AFRH/BSRR/ODR;
    C2000 GPxMUX1/2, GPxDIR, GPxPUD, GPxSET/CLEAR/TOGGLE/DAT."""
    if pdesc is None:
        return []
    model = str(pdesc.model)
    fam = model.split(".", 1)[0]
    if not (model.endswith(".port") or model.endswith(".gpio")):
        return []
    if val is not None and kmask == 0:
        kmask = -1
    pr = pc_.periph_regs(pname, pdesc)
    reg = pr.containing(off) if pr is not None else None
    if reg is None:
        return []
    wbits = reg.width * pc_.unit_bits
    full = (1 << wbits) - 1
    shift = (off - reg.offset) * pc_.unit_bits         # sub-register store (C28x 16-bit half of a 32-bit reg)
    km = ((kmask & ((1 << (wbits - shift)) - 1)) << shift) & full if val is not None else 0
    kv = ((val or 0) << shift) & km
    eff = kv | (reg.reset & ~km & full)                 # unknown bits: assume the reset value
    rname = f"{pname}_{reg.name}"
    stored = (val << shift) & full if val is not None and km == full else (eff if km else None)
    port = (pdesc.params or {}).get("port")
    out: list[PinRole] = []

    def add(pin_name: str, note: str, value: int | None = stored):
        out.append(PinRole(pin_name, rname, pc, value, note))

    def gname(port_: str, bit: int) -> str:
        p = pc_.pin(port_, bit)
        return p.gpio_name if p is not None else f"P{port_}{bit}"

    def fields_touched():
        """(field, value) of the fields this store sets: known bits, else (full write) fields off their reset."""
        res = []
        for f in reg.fields:
            if km & f.mask == 0:
                continue
            if km == full and f.get(eff) == f.get(reg.reset):
                continue
            res.append((f, f.get(eff)))
        return res

    if fam == "kinetis":
        if model.endswith(".port") and reg.name.startswith("PCR") and port is not None:
            bit = int(reg.name[3:])
            note = "configured"
            if km:
                parts = []
                if km & 0x700:
                    mux = (eff >> 8) & 7
                    p = pc_.pin(port, bit)
                    fn = p.functions.get(f"ALT{mux}") if p is not None else None
                    parts.append(f"MUX=ALT{mux}" + (f" ({'GPIO' if mux == 1 else fn})" if fn else ""))
                if km & 0x3 and eff & 0x2:
                    parts.append("pull-" + ("up" if eff & 1 else "down"))
                irqc = (eff >> 16) & 0xF
                if km & 0xF0000 and irqc:
                    parts.append(f"IRQC={irqc}")
                note = " ".join(parts) or note
            add(gname(port, bit), note)
            return out
        if model.endswith(".gpio") and port is not None:
            short = reg.name
            note = {"PDDR": "direction", "PDOR": "output", "PSOR": "set", "PCOR": "clear",
                    "PTOR": "toggle"}.get(short, "")
            pin = f"PT{port}"
            if short == "PDIR":
                return []
            if km:
                bits = [b for b in range(32) if km >> b & 1 and eff >> b & 1]      # bits known to be written 1
                if short == "PDDR":
                    note = ("outputs: " + ",".join(gname(port, b) for b in bits) if bits else
                            "all inputs" if km == full else note)
                elif bits and len(bits) <= 4:
                    pin = ",".join(gname(port, b) for b in bits)
            add(pin, note)
            return out
        return out

    if fam == "stm32" and port is not None:
        n = reg.name
        if n in ("IDR", "LCKR"):
            return out
        if not km:
            if n in ("MODER", "PUPDR", "AFRL", "AFRH", "OTYPER"):
                add(f"P{port}", f"{n} (value unknown)")
            return out
        for f, v in fields_touched():
            m = re.search(r"(\d+)$", f.name)
            if not m:
                continue
            i = int(m.group(1))
            if n == "MODER":
                add(gname(port, i), f"mode {_STM32_MODE[v]}", v)
            elif n == "PUPDR":
                add(gname(port, i), _STM32_PULL[v], v)
            elif n == "OTYPER":
                add(gname(port, i), "open-drain" if v else "push-pull", v)
            elif n == "OSPEEDR":
                add(gname(port, i), f"speed {v}", v)
            elif n in ("AFRL", "AFRH"):
                p = pc_.pin(port, i)
                fn = p.functions.get(f"AF{v}") if p is not None else None
                add(gname(port, i), f"AF{v}" + (f" ({fn})" if fn else ""), v)
            elif n == "BSRR" and v:
                add(gname(port, i), "set" if f.name.startswith("BS") else "reset", None)
            elif n == "ODR":
                add(gname(port, i), f"output {'high' if v else 'low'}", v)
        return out

    if fam == "ti_c2000":
        n = reg.name
        mm = re.fullmatch(r"GP([A-C])(MUX[12]|DIR|PUD|SET|CLEAR|TOGGLE|DAT)", n)
        if not mm:
            return out
        prt, kind = mm.groups()
        if not km:
            add(f"GP{prt}", f"{kind} (value unknown)")
            return out
        if kind.startswith("MUX"):
            for f, v in fields_touched():
                p = pc_.by_gpio.get(f.name)
                fn = None if p is None else p.functions.get("GPIO" if v == 0 else f"MUX{v}")
                add(f.name, f"mux {v}" + (f" ({fn})" if fn else ""), v)
            return out
        word = {"DIR": "output", "PUD": "pull-up disabled", "SET": "set", "CLEAR": "clear", "TOGGLE": "toggle",
                "DAT": "output high"}[kind]
        anti = {"DIR": "input", "PUD": "pull-up enabled", "DAT": "output low"}
        for b in range(wbits):
            if not km >> b & 1:
                continue
            v = eff >> b & 1
            if km == full and v == (reg.reset >> b & 1) and kind in ("DIR", "PUD", "DAT"):
                continue
            if not v and kind not in anti:
                continue
            add(gname(prt, b), word if v else anti[kind], v)
        return out
    return out


# ---------------------------------------------------------------------------------------------------------------
# Static store scan: a bounded forward dataflow over linearly decoded code (both ISAs)
#
# Compiled code reaches peripheral registers through a base held in a register plus a displacement (ARM `ldr r3,=base`
# / `movw+movt` / `add r7, r8, #0x19400`, then `str r2, [r3, #off]`; C28x `MOVL XAR4,#addr` then `*+XAR4[k]`, or
# `MOVW DP,#page` then `@off`).  The scan decodes every code segment once, then runs one forward pass that keeps, per
# program point, what is known about each register: a constant, a stack address (sp + k, for argument structs built
# on the stack), or the known bits of a read-modify-write value.  Join points get the intersection of the states
# reaching them (fall-through + recorded forward branches); a loop head forgets every register the loop body may
# redefine (computed from a pre-pass over the body); function entries (symbols, call targets, vectors) and code after
# an unconditional jump that no recorded branch reaches start empty.  No path enumeration and no symbolic values:
# work is linear in the code size (loop bodies longer than _MAX_LOOP instructions just clear the state).

_ALL = "*"              # defs sentinel: the instruction may change anything
_MAX_LOOP = 4000        # instructions; longer backward branches clear the whole state at their target
_SP = "sp"              # defs marker: the stack pointer moved (stack-slot and sp-relative facts are dropped)
_SLOTS = "slots"        # defs marker: stack memory may have been written (stack-slot facts are dropped)


@dataclass(slots=True)
class _Ins:
    pc: int
    end: int
    flow: str           # seq | cbr (conditional branch) | br (unconditional) | call | ret (no fall-through)
    target: int | None
    defs: Any           # frozenset of state keys this instruction may write, or _ALL
    data: Any           # decoder-specific


def _meet(states: list[dict]) -> dict:
    if len(states) == 1:
        return dict(states[0])
    first, rest = states[0], states[1:]
    return {k: v for k, v in first.items() if all(s.get(k, _ALL) == v for s in rest)}


def _kill(state: dict, defs: Any) -> None:
    if defs is _ALL:
        state.clear()
        return
    for d in defs:
        if d == _SP:
            for k in [k for k, v in state.items() if isinstance(k, tuple) or (isinstance(v, tuple) and v[0] == "sp")]:
                del state[k]
        elif d == _SLOTS:
            for k in [k for k in state if isinstance(k, tuple)]:
                del state[k]
        else:
            state.pop(d, None)


def _reachable(insns: list[_Ins], index: dict[int, int], entries: set[int]) -> list[bool]:
    """Instructions reachable from `entries` by fall-through and direct branches (calls fall through)."""
    seen = [False] * len(insns)
    work = [index[e] for e in entries if e in index]
    while work:
        i = work.pop()
        while 0 <= i < len(insns) and not seen[i]:
            seen[i] = True
            ins = insns[i]
            t = ins.target
            if ins.flow in ("cbr", "br") and t is not None and t in index:
                work.append(index[t])
            if ins.flow in ("br", "ret") or i + 1 >= len(insns) or insns[i + 1].pc != ins.end:
                break
            i += 1
    return seen


def _dataflow(insns: list[_Ins], entries: set[int], transfer, call_clobber: frozenset, deadline: float,
              trusted_entries: bool = False) -> bool:
    """Run `transfer(ins, state)` over `insns` (address order) with the joined incoming state.  Returns False when
    the time budget ran out.  `trusted_entries` (a symbol table): branches in code not reachable from an entry
    (data decoded as instructions) are ignored."""
    import time
    index = {ins.pc: i for i, ins in enumerate(insns)}
    ent = sorted(entries)
    live = _reachable(insns, index, entries) if trusted_entries else None

    def same_function(a: int, b: int) -> bool:          # no function entry in (min, max]: a jump, not a tail call
        lo, hi = min(a, b), max(a, b)
        k = bisect.bisect_right(ent, lo)
        return k >= len(ent) or ent[k] > hi
    loop_kill: dict[int, Any] = {}
    for i, ins in enumerate(insns):           # loop heads: forget whatever the body redefines
        t = ins.target
        if live is not None and not live[i]:
            continue
        if ins.flow in ("cbr", "br") and t is not None and t <= ins.pc and t in index and same_function(t, ins.pc):
            j = index[t]
            if i - j > _MAX_LOOP:
                loop_kill[t] = _ALL
                continue
            acc = loop_kill.get(t, frozenset())
            if acc is _ALL:
                continue
            ks = set(acc)
            for k in range(j, i + 1):
                d = insns[k].defs
                if d is _ALL:
                    ks = None
                    break
                ks.update(d)
                if insns[k].flow == "call":
                    ks.update(call_clobber)
            loop_kill[t] = _ALL if ks is None else frozenset(ks)
    pending: dict[int, list[dict]] = {}
    state: dict = {}
    fall = False
    prev_end = None
    for n, ins in enumerate(insns):
        if (n & 0x3FF) == 0 and time.perf_counter() > deadline:
            return False
        pc = ins.pc
        if pc != prev_end:
            fall = False                       # gap (data / undecodable bytes)
        inc = pending.pop(pc, None)
        if pc in entries:
            state = {}
        else:
            ins_states = ([state] if fall else []) + (inc or [])
            state = _meet(ins_states) if ins_states else {}
            lk = loop_kill.get(pc)
            if lk is not None:
                _kill(state, lk)
        transfer(ins, state)
        f = ins.flow
        if f == "call":
            _kill(state, call_clobber)
        t = ins.target
        if f in ("cbr", "br") and t is not None and t > pc and len(pending) < 100000 and same_function(pc, t) \
                and (live is None or live[n]):
            pending.setdefault(t, []).append(dict(state))
        fall = f not in ("br", "ret")
        prev_end = ins.end
    return True


# ---------------------------------------------------------------------------------------------------------------
# ARM (Thumb-2) decoder + transfer

_ARM_CALLER = frozenset(("r0", "r1", "r2", "r3", "ip", "lr", _SLOTS))     # the callee may write arg structs
_ARM_REGN = {"sb": "r9", "sl": "r10", "fp": "r11", "ip": "r12"}
_CONDS = ("eq", "ne", "cs", "hs", "cc", "lo", "mi", "pl", "vs", "vc", "hi", "ls", "ge", "lt", "gt", "le")
_RE_IMM = re.compile(r"#(-?0x[0-9a-f]+|-?\d+)")
_RE_REG = re.compile(r"\b(r\d+|sb|sl|fp|ip|lr|sp|pc)\b")
_RE_MEMOP = re.compile(r"^(?P<rt>r\d+|sb|sl|fp|ip|lr)(?:, (?P<rt2>r\d+|sb|sl|fp|ip|lr))?, "
                       r"\[(?P<rb>r\d+|sb|sl|fp|ip|sp)(?:, (?:#(?P<off>-?0x[0-9a-f]+|-?\d+)|(?P<ri>r\d+|sb|sl|fp|ip)))?\]"
                       r"(?P<wb>!)?(?:, #(?P<post>-?0x[0-9a-f]+|-?\d+))?$")
_NOREG = {"cmp", "cmn", "tst", "teq", "nop", "dsb", "isb", "dmb", "cpsid", "cpsie", "wfi", "wfe", "svc", "bkpt",
          "msr", "sev", "yield", "vpush", "vstr", "vstmia", "vstmdb", "pld", "pli"}


def _arm_base(mnem: str) -> str:
    return mnem.split(".", 1)[0]


@functools.lru_cache(maxsize=65536)
def _arm_flow(m: str, ops: str) -> tuple[str, int | None]:
    def tgt():
        mm = re.search(r"#(0x[0-9a-f]+)", ops)
        return int(mm.group(1), 16) if mm else None
    if m == "bl":
        return "call", tgt()
    if m == "blx":
        return "call", None
    if m == "bx":
        return "ret", None
    if m == "b":
        return "br", tgt()
    if m in ("cbz", "cbnz") or (m[0] == "b" and len(m) == 3 and m[1:] in _CONDS):
        return "cbr", tgt()
    if m in ("tbb", "tbh"):
        return "ret", None
    if m in ("pop", "ldm", "ldmia", "ldmdb") and "pc" in ops:
        return "ret", None
    if m.startswith("ldr") and ops.startswith("pc"):
        return "ret", None
    if m == "mov" and ops.startswith("pc"):
        return "ret", None
    return "seq", None


@functools.lru_cache(maxsize=65536)
def _arm_defs(m: str, ops: str, flow: str):
    regs = [_ARM_REGN.get(r, r) for r in _RE_REG.findall(ops)]
    if flow == "call":
        return frozenset()                      # call_clobber is applied by the driver
    if flow in ("cbr", "br", "ret") or m in _NOREG or m.startswith("it"):
        return frozenset()
    if m == "push" or m.startswith("stmdb") and ops.startswith("sp"):
        return frozenset((_SP,))
    if m == "pop":
        return frozenset(regs) | {_SP}
    if m.startswith("st"):
        wb = "!" in ops or re.search(r"\], #", ops)
        if m.startswith("stm"):
            return frozenset((regs[0],)) if "!" in ops else frozenset()
        mm = _RE_MEMOP.match(ops)
        if wb and mm:
            b = _ARM_REGN.get(mm.group("rb"), mm.group("rb"))
            return frozenset((b, _SP) if b == "sp" else (b,))
        return frozenset()
    if m.startswith("ldm"):
        return frozenset(regs[1:] + ([regs[0]] if "!" in ops else []))
    if not regs:
        return frozenset()
    d = {regs[0]}
    if m in ("ldrd", "strd", "umull", "smull", "umlal", "smlal") and len(regs) > 1:
        d.add(regs[1])
    if m.startswith("ld") and ("!" in ops or re.search(r"\], #", ops)):
        mm = _RE_MEMOP.match(ops)
        if mm:
            d.add(_ARM_REGN.get(mm.group("rb"), mm.group("rb")))
    if "sp" in d:
        d.discard("sp")
        d.add(_SP)
    return frozenset(d)


def _decode_thumb(image: FirmwareImage, desc: Any, deadline: float) -> tuple[list[_Ins], set[int], bool]:
    import time

    import capstone
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
    insns: list[_Ins] = []
    calls: set[int] = set()
    for seg_addr, data in _flash_ranges(image, desc):
        mv = memoryview(data)
        pos = 0
        n = len(data)
        while pos < n - 1:
            if time.perf_counter() > deadline:
                return insns, calls, False
            last_end = pos
            it_left = 0
            for addr, size, mnem, ops in md.disasm_lite(mv[pos:], seg_addr + pos):
                last_end = addr + size - seg_addr
                m = _arm_base(mnem)
                flow, tgt = _arm_flow(m, ops)
                if flow == "call" and tgt is not None:
                    calls.add(tgt)
                cond = it_left > 0
                if it_left:
                    it_left -= 1
                if m.startswith("it") and len(m) <= 4 and set(m[1:]) <= {"t", "e"}:
                    it_left = len(m) - 1
                defs = _arm_defs(m, ops, flow)
                insns.append(_Ins(addr, addr + size, flow, tgt, defs, (m, ops, cond)))
                if len(insns) & 0x3FFF == 0 and time.perf_counter() > deadline:
                    return insns, calls, False
            pos = last_end + 2 if last_end > pos else pos + 2
    return insns, calls, True


def _imm(s: str | None) -> int:
    return int(s, 0) if s else 0


class _ArmScan:
    """Transfer function of the Thumb store scan; collects pin roles, store counts and address loads."""

    M32 = 0xFFFFFFFF

    def __init__(self, image: FirmwareImage, desc: Any, pmap: _RangeMap, api: dict[int, str]):
        self.image, self.pmap, self.api = image, pmap, api
        self.pctx = _PinCtx(desc)
        self.roles: list[PinRole] = []
        self.stores: Counter = Counter()
        self.lit_loads: Counter = Counter()
        self.samples: dict[str, list[int]] = {}
        self.seen_stores: set[tuple[int, int]] = set()

    # -- helpers
    @staticmethod
    def R(r: str | None) -> str | None:
        return _ARM_REGN.get(r, r) if r else r

    def const(self, st: dict, r: str | None) -> int | None:
        v = st.get(self.R(r)) if r else None
        return v[1] if v is not None and v[0] == "c" else None

    def addr_ref(self, v: int) -> None:
        hit = self.pmap.find(v)
        if hit is not None:
            self.lit_loads[hit[0]] += 1
            _add_sample(self.samples, hit[0], v)

    def set_const(self, st: dict, rd: str, v: int, ref: bool = False) -> None:
        v &= self.M32
        st[rd] = ("c", v)
        if ref:
            self.addr_ref(v)

    def store(self, st: dict, pc: int, ea: int, rt: str | None, width: int) -> None:
        hit = self.pmap.find(ea)
        if hit is None:
            return
        pname, pbase, pdesc = hit
        if (pc, ea) in self.seen_stores:
            return
        self.seen_stores.add((pc, ea))
        self.stores[pname] += 1
        v = st.get(self.R(rt)) if rt else None
        val, km = None, 0
        wm = (1 << (8 * width)) - 1
        if v is not None and v[0] == "c":
            val, km = v[1] & wm, wm
        elif v is not None and v[0] == "p" and v[1]:
            val, km = v[2] & wm, v[1] & wm
        self.roles.extend(_pin_roles(self.pctx, pdesc, pname, ea - pbase, pc, val, km))

    # -- the transfer function
    def __call__(self, ins: _Ins, st: dict) -> None:
        m, ops, cond = ins.data
        R = self.R
        if ins.flow == "call":
            name = self.api.get(ins.target) if ins.target is not None else None
            if name is not None:
                self.roles.extend(_api_roles(self, name, st, ins.pc))
            return
        if m in ("ldr", "ldrb", "ldrh", "ldrsb", "ldrsh") and not cond:
            mm = _RE_LDR_PC.match(ops)
            if mm:
                rd = R(mm.group(1))
                v = self.image.read_word(((ins.pc + 4) & ~3) + int(mm.group(2), 0)) if m == "ldr" else None
                if v is None:
                    st.pop(rd, None)
                else:
                    self.set_const(st, rd, v, ref=True)
                return
            mm = _RE_MEMOP.match(ops)
            if mm and not mm.group("rt2"):
                rd, rb = R(mm.group("rt")), R(mm.group("rb"))
                bv = st.get(rb)
                ea_off = _imm(mm.group("off"))
                if mm.group("ri"):
                    ri = self.const(st, mm.group("ri"))
                    ea_off = ri if ri is not None else None
                new = None
                if bv is not None and ea_off is not None:
                    if bv[0] == "c" and self.pmap.find(bv[1] + ea_off) is not None:
                        new = ("p", 0, 0)                  # peripheral read: start of a read-modify-write
                    elif bv[0] == "sp" and m == "ldr":
                        new = st.get(("slot", bv[1] + ea_off))
                elif rb == "sp" and ea_off is not None and m == "ldr":
                    new = st.get(("slot", ea_off))
                self._writeback(st, mm, rb)
                if new is None:
                    st.pop(rd, None)
                else:
                    st[rd] = new
                return
        if m in ("str", "strb", "strh", "strd"):
            mm = _RE_MEMOP.match(ops)
            if mm:
                rb = R(mm.group("rb"))
                bv = st.get(rb) if rb != "sp" else ("sp", 0)
                off = _imm(mm.group("off")) if not mm.group("post") else 0
                if mm.group("ri"):
                    ri = self.const(st, mm.group("ri"))
                    off = ri if ri is not None else None
                width = {"str": 4, "strb": 1, "strh": 2, "strd": 4}[m]
                rts = [mm.group("rt")] + ([mm.group("rt2")] if mm.group("rt2") else [])
                if bv is not None and off is not None:
                    for i, rt in enumerate(rts):
                        ea = bv[1] + off + 4 * i
                        if bv[0] == "c":
                            self.store(st, ins.pc, ea & self.M32, rt, width)
                        elif bv[0] == "sp":
                            v = st.get(R(rt))
                            if width == 4 and v is not None and v[0] == "c":
                                st[("slot", ea)] = v
                            else:
                                st.pop(("slot", ea), None)
                elif bv is None and rb != "sp":
                    pass
                self._writeback(st, mm, rb)
                return
        if m.startswith("stm") and not m.startswith("stmdb"):
            regs = [R(r) for r in _RE_REG.findall(ops)]
            bv = st.get(regs[0]) if regs else None
            if bv is not None and bv[0] == "c":
                for i, rt in enumerate(regs[1:]):
                    self.store(st, ins.pc, (bv[1] + 4 * i) & self.M32, rt, 4)
            _kill(st, ins.defs)
            return
        if cond:                                           # IT-conditional: the result is not known
            _kill(st, ins.defs)
            return
        if m in ("mov", "movs", "movw", "mvn", "mvns"):
            rd, _, src = ops.partition(", ")
            rd = R(rd)
            if src.startswith("#"):
                v = _imm(src[1:])
                self.set_const(st, rd, ~v if m.startswith("mvn") else v)
            elif m.startswith("mov") and _RE_REG.fullmatch(src):
                s = R(src)
                v = ("sp", 0) if s == "sp" else st.get(s)
                if v is None:
                    st.pop(rd, None)
                else:
                    st[rd] = v
            else:
                st.pop(rd, None)
            return
        if m == "movt":
            rd, _, src = ops.partition(", ")
            rd = R(rd)
            c = self.const(st, rd)
            if c is not None and src.startswith("#"):
                self.set_const(st, rd, (c & 0xFFFF) | ((_imm(src[1:]) & 0xFFFF) << 16), ref=True)
            else:
                st.pop(rd, None)
            return
        if m in ("add", "adds", "addw", "sub", "subs", "subw"):
            parts = [p.strip() for p in ops.split(",")]
            if len(parts) == 2:
                parts = [parts[0], parts[0], parts[1]]
            if len(parts) == 3:
                rd, rn, src = R(parts[0]), R(parts[1]), parts[2]
                sign = -1 if m.startswith("sub") else 1
                bv = ("sp", 0) if rn == "sp" else st.get(rn)
                if src.startswith("#"):
                    k = _imm(src[1:])
                else:
                    k = self.const(st, src) if _RE_REG.fullmatch(src) else None
                    if k is None and sign == 1 and _RE_REG.fullmatch(src):   # add rd, rn, rm with rn unknown
                        k2 = self.const(st, rn)
                        bv2 = st.get(R(src))
                        if k2 is not None and bv2 is not None:
                            bv, k = bv2, k2
                if rd == "sp":
                    _kill(st, ins.defs)
                    return
                if bv is not None and k is not None and bv[0] in ("c", "sp"):
                    if bv[0] == "c":
                        self.set_const(st, rd, bv[1] + sign * k, ref=True)
                    else:
                        st[rd] = ("sp", bv[1] + sign * k)
                    return
            _kill(st, ins.defs)
            return
        if m in ("lsl", "lsls", "lsr", "lsrs") and ops.count("#") == 1:
            parts = [p.strip() for p in ops.split(",")]
            if len(parts) == 2:
                parts = [parts[0], parts[0], parts[1]]
            c = self.const(st, parts[1])
            if c is not None and len(parts) == 3:
                sh = _imm(parts[2][1:])
                self.set_const(st, R(parts[0]), c << sh if m.startswith("lsl") else c >> sh)
                return
            _kill(st, ins.defs)
            return
        if m in ("orr", "orrs", "bic", "bics", "and", "ands", "bfi", "bfc") and _rmw2(m.rstrip("s") if m != "bics"
                                                                                       else "bic", ops, st):
            return
        _kill(st, ins.defs)

    def _writeback(self, st: dict, mm, rb: str) -> None:
        if not (mm.group("wb") or mm.group("post")):
            return
        delta = _imm(mm.group("post")) if mm.group("post") else _imm(mm.group("off"))
        if rb == "sp":
            _kill(st, frozenset((_SP,)))
            return
        bv = st.get(rb)
        if bv is not None and bv[0] in ("c", "sp"):
            st[rb] = (bv[0], (bv[1] + delta) & self.M32 if bv[0] == "c" else bv[1] + delta)
        else:
            st.pop(rb, None)


_RE_RMW = re.compile(r"^(r\d+|sb|sl|fp|ip), (?:(r\d+|sb|sl|fp|ip), )?(?:#(-?0x[0-9a-f]+|-?\d+)|(r\d+|sb|sl|fp|ip))"
                     r"(?:, #(0x[0-9a-f]+|\d+), #(0x[0-9a-f]+|\d+))?$")


def _rmw2(op: str, ops: str, st: dict) -> bool:
    """Known-bits tracking for read-modify-write sequences (ldr from a peripheral; bic/orr/and #imm; bfi rX, rConst,
    #lsb, #w; bfc) and constant folding of the same operations.  False if the instruction does not fit."""
    M = 0xFFFFFFFF
    R = _ArmScan.R
    if op == "bfc":
        mm = re.match(r"^(r\d+|sb|sl|fp|ip), #(0x[0-9a-f]+|\d+), #(0x[0-9a-f]+|\d+)$", ops)
        if not mm:
            return False
        rd = R(mm.group(1))
        v = st.get(rd)
        if v is None or v[0] not in ("c", "p"):
            return False
        km, kv = (M, v[1]) if v[0] == "c" else (v[1], v[2])
        fm = ((1 << int(mm.group(3), 0)) - 1) << int(mm.group(2), 0)
        st[rd] = ("p", (km | fm) & M, kv & ~fm & M)
        return True
    mm = _RE_RMW.match(ops)
    if not mm:
        return False
    rd, rn, imm, rm, lsb, wid = mm.groups()
    rd = R(rd)
    src = st.get(R(rn or rd))
    if src is None or src[0] not in ("c", "p"):
        return False
    km, kv = (M, src[1]) if src[0] == "c" else (src[1], src[2])
    if op == "bfi":
        c = st.get(R(rm)) if rm else None
        if c is None or c[0] != "c" or lsb is None:
            return False
        fm = ((1 << int(wid, 0)) - 1) << int(lsb, 0)
        km, kv = km | fm, (kv & ~fm) | ((c[1] << int(lsb, 0)) & fm)
    else:
        if imm is not None:
            v = int(imm, 0) & M
        else:
            c = st.get(R(rm))
            if c is None or c[0] != "c":
                return False
            v = c[1]
        if op == "orr":
            km, kv = km | v, kv | v
        elif op == "bic":
            km, kv = km | v, kv & ~v
        else:                                           # and
            km, kv = km | (~v & M), kv & v
    km &= M
    st[rd] = ("c", kv & M) if km == M else ("p", km, kv & M)
    return True


# Known driver calls (by symbol name) whose register effect is computed inside a loop over pin bits, which no
# bounded scan can follow: the pin roles are taken from the call's arguments instead.
#   STM32 HAL: HAL_GPIO_Init(GPIOx, &GPIO_InitTypeDef{Pin, Mode, Pull, Speed, Alternate}),
#              HAL_GPIO_WritePin(GPIOx, pins, state), HAL_GPIO_TogglePin(GPIOx, pins), HAL_GPIO_DeInit(GPIOx, pins)
_API_CALLS = ("HAL_GPIO_Init", "HAL_GPIO_WritePin", "HAL_GPIO_TogglePin", "HAL_GPIO_DeInit")
_HAL_MODE = {0: "input", 1: "output", 2: "AF", 3: "analog"}


def _api_roles(sc: "_ArmScan", name: str, st: dict, pc: int) -> list[PinRole]:
    base = sc.const(st, "r0")
    if base is None:
        return []
    hit = sc.pmap.find(base)
    if hit is None or hit[1] != base or hit[2] is None:
        return []
    pname, _, pdesc = hit
    port = (pdesc.params or {}).get("port")
    if port is None or not str(pdesc.model).endswith(".gpio"):
        return []
    pc_ = sc.pctx

    def gname(bit: int) -> str:
        p = pc_.pin(port, bit)
        return p.gpio_name if p is not None else f"P{port}{bit}"

    out: list[PinRole] = []
    if name == "HAL_GPIO_Init":
        a = st.get("r1")
        if a is None or a[0] != "sp":
            return []
        fld = [st.get(("slot", a[1] + 4 * i)) for i in range(5)]
        if fld[0] is None or fld[0][0] != "c":
            return []
        pins = fld[0][1] & 0xFFFF
        mode = fld[1][1] if fld[1] is not None and fld[1][0] == "c" else None
        pull = fld[2][1] if fld[2] is not None and fld[2][0] == "c" else None
        alt = fld[4][1] if fld[4] is not None and fld[4][0] == "c" else None
        for b in range(16):
            if not pins >> b & 1:
                continue
            if mode is None:
                out.append(PinRole(gname(b), f"{pname}_MODER", pc, None, f"{name} (mode unknown)"))
                continue
            md = mode & 3
            note = f"mode {_HAL_MODE[md]}" + (" open-drain" if md in (1, 2) and mode & 0x10 else "")
            if mode & 0x30000:
                note += " EXTI"
            out.append(PinRole(gname(b), f"{pname}_MODER", pc, md, f"{note} ({name})"))
            if pull:
                out.append(PinRole(gname(b), f"{pname}_PUPDR", pc, pull,
                                   f"{_STM32_PULL.get(pull & 3, 'pull?')} ({name})"))
            if md == 2 and alt is not None:
                p = pc_.pin(port, b)
                fn = p.functions.get(f"AF{alt}") if p is not None else None
                out.append(PinRole(gname(b), f"{pname}_AFR{'L' if b < 8 else 'H'}", pc, alt,
                                   f"AF{alt}" + (f" ({fn})" if fn else "") + f" ({name})"))
        return out
    pins = sc.const(st, "r1")
    if pins is None:
        return []
    word = {"HAL_GPIO_WritePin": None, "HAL_GPIO_TogglePin": "toggle", "HAL_GPIO_DeInit": "deinit"}[name]
    if word is None:
        s = sc.const(st, "r2")
        word = "write" if s is None else ("set" if s else "reset")
    reg = {"set": "BSRR", "reset": "BSRR", "write": "BSRR", "toggle": "ODR", "deinit": "MODER"}[word]
    for b in range(16):
        if pins >> b & 1:
            out.append(PinRole(gname(b), f"{pname}_{reg}", pc, None, f"{word} ({name})"))
    return out


def _scan_code(image: FirmwareImage, desc: Any, pmap: _RangeMap, time_budget: float = 4.0,
               vectors: list[int] | None = None):
    """Thumb store scan (see the section comment above): peripheral stores through literal / movw+movt / derived
    bases, with read-modify-write known bits, plus the pin roles of known driver calls."""
    import time
    from .symbols import image_symbols
    deadline = time.perf_counter() + time_budget
    insns, calls, done = _decode_thumb(image, desc, deadline)
    syms = image_symbols(image)
    # function entries: the symbol table when there is one (a `bl` decoded from data must not cut a function in
    # two), else every call target
    entries = ({a & ~1 for a, _, _ in syms} if syms else set(calls)) | {v & ~1 for v in vectors or () if v & 1}
    api = {a & ~1: n for a, _, n in syms if n in _API_CALLS}
    sc = _ArmScan(image, desc, pmap, api)
    if done:
        done = _dataflow(insns, entries, sc, _ARM_CALLER, deadline, trusted_entries=bool(syms))
    return sc.roles, sc.stores, (sc.lit_loads, sc.samples), len(insns), not done


# ---------------------------------------------------------------------------------------------------------------
# C28x decoder + transfer.  State keys: "dp" (page), "x0".."x7" (XARn), "acc" ((known mask, known bits), 32 bits).

_C28_CALLER = frozenset(("acc", "dp", "x0", "x4", "x5", "x6", "x7"))    # XAR1-XAR3 are save-on-entry (TI C ABI)
_C28_AL, _C28_AH = 0xA9, 0xA8
_RE_AR = re.compile(r"\bX?AR([0-7n])\b")


def _c28_loc_xar(loc: int | None) -> int | None:
    """XARn that an addressing byte post-increments / pre-decrements or names directly (@ARn / @XARn)."""
    if loc is None or not 0x80 <= loc < 0xA8:
        return None
    sub = (loc >> 3) & 7
    return loc & 7 if sub in (0, 1, 4) else None


def _c28_defs(e: Any, ops: dict) -> Any:
    a = e.args
    d = a.get("d")
    out = set()
    first = e.ops_text.split(",", 1)[0].strip() if e.ops_text else ""
    if d == "dp" or first == "DP":
        out.add("dp")
    if d in ("acc", "ax") or first in ("ACC", "AX", "AL", "AH", "ACC:P", "P:ACC") or first.startswith("ACC"):
        out.add("acc")
    for mm in _RE_AR.finditer(first):
        n = ops.get("n") if mm.group(1) == "n" else int(mm.group(1))
        if n is not None:
            out.add(f"x{n}")
    if d == "xar":
        n = a.get("n", ops.get("n"))
        if n is not None:
            out.add(f"x{n}")
    loc = ops.get("loc")
    if loc is not None:
        n = _c28_loc_xar(loc)
        if n is not None:
            out.add(f"x{n}")
        if d in ("loc16", "loc32") and loc in (_C28_AL, _C28_AH):
            out.add("acc")
    if "POP" in e.mn and not out:
        return _ALL
    return frozenset(out)


def _decode_c28x(image: FirmwareImage, desc: Any, deadline: float) -> tuple[list[_Ins], set[int], bool]:
    import time

    from .core.c28x.isa import get_isa
    isa = get_isa()
    insns: list[_Ins] = []
    calls: set[int] = set()
    for a, ws in _c28x_words(image):
        m = desc.memory_at(a)
        if m is not None and m.kind not in ("flash", "rom", "otp", "ram"):
            continue
        n = len(ws)
        i = 0
        while i < n:
            if (i & 0x3FF) == 0 and time.perf_counter() > deadline:
                return insns, calls, False
            j = i
            r = isa.match(ws[i], lambda: ws[j + 1] if j + 1 < n else None)
            if r is None:
                i += 1
                continue
            e, word = r
            i += e.nwords
            ops = isa.operands(e, word, a + j)
            flow, tgt = "seq", None
            if e.norpt:
                mn = e.mn
                tgt = ops.get("target", ops.get("addr"))
                cond = ops.get("cond", 15)
                if mn in ("LCR", "LC", "FFC"):
                    flow = "call"
                elif mn in ("LRETR", "LRET", "IRET", "RET", "RETE") or (mn == "LB" and tgt is None):
                    flow = "ret"
                elif mn in ("LB", "B", "SB", "BF", "LBF") or mn.startswith("BAR") or mn == "BANZ":
                    flow = "br" if cond == 15 and mn not in ("BANZ", "BAR") else "cbr"
                if flow == "call" and isinstance(tgt, int):
                    calls.add(tgt)
            insns.append(_Ins(a + j, a + i, flow, tgt if isinstance(tgt, int) else None, _c28_defs(e, ops),
                              (e, ops)))
    return insns, calls, True


class _C28Scan:
    def __init__(self, desc: Any, pmap: _RangeMap):
        self.pmap = pmap
        self.pctx = _PinCtx(desc)
        self.roles: list[PinRole] = []
        self.stores: Counter = Counter()

    @staticmethod
    def half(acc: tuple[int, int] | None, hi: bool) -> tuple[int, int]:
        km, kv = acc if acc is not None else (0, 0)
        s = 16 if hi else 0
        return (km >> s) & 0xFFFF, (kv >> s) & 0xFFFF

    @staticmethod
    def set_half(st: dict, hi: bool, km: int, kv: int) -> None:
        okm, okv = st.get("acc", (0, 0))
        s = 16 if hi else 0
        m = 0xFFFF << s
        st["acc"] = ((okm & ~m) | ((km & 0xFFFF) << s), (okv & ~m) | ((kv & km & 0xFFFF) << s))

    def ea(self, st: dict, loc: int, w32: bool, update: bool = True) -> int | None:
        if loc < 0x40:
            dp = st.get("dp")
            return None if dp is None else dp * 64 + loc
        if loc >= 0xC0:
            x = st.get(f"x{loc & 7}")
            return None if x is None else (x + ((loc >> 3) & 7)) & 0x3FFFFF
        sub, n = (loc >> 3) & 7, loc & 7
        if sub in (0, 1):
            x = st.get(f"x{n}")
            if x is None:
                return None
            sz = 2 if w32 else 1
            if sub == 0:
                if update:
                    st[f"x{n}"] = (x + sz) & 0x3FFFFF
                return x
            x = (x - sz) & 0x3FFFFF
            if update:
                st[f"x{n}"] = x
            return x
        return None

    def __call__(self, ins: _Ins, st: dict) -> None:
        e, ops = ins.data
        a = e.args
        d, s, op = a.get("d"), a.get("s"), a.get("op")
        loc = ops.get("loc")
        imm = ops.get("imm")
        imm = imm if isinstance(imm, int) else None
        sh = ops.get("shift")
        if e.sem == "movabs":                               # MOV *(0:abs16), loc16 / MOV loc16, *(0:abs16)
            if a.get("ld") == 0 and imm is not None:
                if loc in (_C28_AL, _C28_AH):
                    km, kv = self.half(st.get("acc"), loc == _C28_AH)
                    self.store(ins.pc, imm, kv if km else None, km)
                else:
                    if loc is not None and loc >= 0x80:
                        self.ea(st, loc, False)
                    self.store(ins.pc, imm, None, 0)
                return
            if loc in (_C28_AL, _C28_AH):
                self.set_half(st, loc == _C28_AH, 0, 0)
                return
            _kill(st, ins.defs)
            return
        if d == "dp":
            if s == "imm" and imm is not None:
                st["dp"] = imm
            else:
                st.pop("dp", None)
            return
        if d == "xar":
            n = a.get("n", ops.get("n"))
            k = f"x{n}"
            if s == "addr" and isinstance(ops.get("addr"), int):
                st[k] = ops["addr"] & 0x3FFFFF
                return
            if s == "imm" and imm is not None and op is None:
                st[k] = imm & 0x3FFFFF
                return
            _kill(st, ins.defs)
            return
        if e.sem == "xaradd" and imm is not None:
            n = ops.get("n")
            x = st.get(f"x{n}")
            if x is not None:
                st[f"x{n}"] = (x + a.get("sign", 1) * imm) & 0x3FFFFF
            return
        if d == "ax":
            hi = bool(ops.get("ax"))
            if s == "imm" and imm is not None and op is None and not isinstance(sh, int):
                self.set_half(st, hi, 0xFFFF, imm & 0xFFFF)
                return
            if s == "imm" and imm is not None and op in ("or", "and"):
                km, kv = self.half(st.get("acc"), hi)
                if op == "or":
                    km, kv = km | imm, kv | imm
                else:
                    km, kv = km | (~imm & 0xFFFF), kv & imm
                self.set_half(st, hi, km, kv)
                return
            if s == "loc16" and op is None and loc is not None:
                if loc >= 0x80:
                    self.ea(st, loc, False)
                self.set_half(st, hi, 0, 0)
                return
            _kill(st, ins.defs)
            return
        if d is None and s == "imm" and imm is not None and op in ("or", "and", "mov") \
                and (e.ops_text or "").startswith("ACC,"):   # OR/AND/MOV ACC,#u16<<#sh
            sh = ops.get("sh", a.get("sh", 0))
            if not isinstance(sh, int) or (op == "mov" and imm & 0x8000):
                _kill(st, ins.defs)                           # (MOV sign-extends with SXM: value not known)
                return
            v = (imm << sh) & 0xFFFFFFFF
            km, kv = st.get("acc", (0, 0))
            if op == "or":
                km, kv = km | v, kv | v
            elif op == "and":
                km, kv = km | (~v & 0xFFFFFFFF), kv & v
            else:
                km, kv = 0xFFFFFFFF, v
            st["acc"] = (km, kv)
            return
        if d == "acc":
            if s == "imm" and imm is not None and op is None and sh in (None, 0):
                st["acc"] = (0xFFFFFFFF, imm & 0xFFFFFFFF)
                return
            if s == "loc32" and op is None and loc is not None:
                if loc >= 0x80:
                    self.ea(st, loc, True)
                st["acc"] = (0, 0)                          # tracked, nothing known (read-modify-write start)
                return
            _kill(st, ins.defs)
            return
        if d in ("loc16", "loc32") and loc is not None:
            w32 = d == "loc32"
            if loc in (_C28_AL, _C28_AH) and not w32:     # register destination: OR AH,#imm / AND AL,#imm / MOV AL,#imm
                hi = loc == _C28_AH
                if s == "imm" and imm is not None and op in ("or", "and"):
                    km, kv = self.half(st.get("acc"), hi)
                    if op == "or":
                        km, kv = km | imm, kv | imm
                    else:
                        km, kv = km | (~imm & 0xFFFF), kv & imm
                    self.set_half(st, hi, km, kv)
                    return
                if s == "imm" and imm is not None and op is None and e.mn in ("MOV", "MOVW", "MOVB"):
                    self.set_half(st, hi, 0xFFFF, imm)
                    return
                if s == "zero":
                    self.set_half(st, hi, 0xFFFF, 0)
                    return
                _kill(st, ins.defs)
                return
            if w32 and loc == _C28_AL:                      # loc32 = @ACC
                _kill(st, ins.defs)
                return
            ea = self.ea(st, loc, w32)
            if ea is not None:
                val, km = None, 0
                if s == "imm" and imm is not None and op is None and e.mn in ("MOV", "MOVW", "MOVB"):
                    val, km = imm & 0xFFFF, 0xFFFF
                elif s == "imm" and imm is not None and op == "or":
                    val, km = imm & 0xFFFF, imm & 0xFFFF
                elif s == "imm" and imm is not None and op == "and":
                    val, km = 0, ~imm & 0xFFFF
                elif s == "zero":
                    val, km = 0, 0xFFFF if not w32 else 0xFFFFFFFF
                elif s == "acc" and op is None:
                    akm, akv = st.get("acc", (0, 0))
                    if not w32:
                        akm, akv = akm & 0xFFFF, akv & 0xFFFF
                    val, km = (akv, akm) if akm else (None, 0)
                elif s == "ax" and op is None:
                    hkm, hkv = self.half(st.get("acc"), bool(ops.get("ax")))
                    val, km = (hkv, hkm) if hkm else (None, 0)
                elif s == "xar" and op is None:
                    x = st.get(f"x{a.get('n', ops.get('n'))}")
                    val, km = (x, 0xFFFFFFFF) if x is not None else (None, 0)
                self.store(ins.pc, ea, val, km)
            for k in ins.defs:
                if not (k.startswith("x") and loc >= 0x80):
                    st.pop(k, None)
            return
        if loc is not None and 0x80 <= loc < 0xA0 and (loc >> 3) & 7 in (0, 1):
            self.ea(st, loc, bool(s == "loc32" or "loc32" in (e.ops_text or "")))
            _kill(st, frozenset(k for k in ins.defs if not k.startswith("x")) if ins.defs is not _ALL else _ALL)
            return
        _kill(st, ins.defs)

    def store(self, pc: int, ea: int, val: int | None, km: int) -> None:
        hit = self.pmap.find(ea)
        if hit is None:
            return
        pname, pbase, pdesc = hit
        self.stores[pname] += 1
        self.roles.extend(_pin_roles(self.pctx, pdesc, pname, ea - pbase, pc, val, km))


def _scan_c28x_stores(image: FirmwareImage, desc: Any, pmap: _RangeMap, time_budget: float = 4.0):
    """C28x store scan (see the section comment above): DP-direct (@6bit), XARn-indirect (*+XARn[k], *XARn++,
    *--XARn) and absolute (*(0:16bit)) stores to peripheral registers.  Values: MOV #imm (16 bits known), OR/AND #imm
    on the register itself, and ACC/AL/AH built by MOVB/MOV #imm, ORB/ANDB, OR/AND AL|AH,#imm after a load."""
    import time
    deadline = time.perf_counter() + time_budget
    insns, calls, done = _decode_c28x(image, desc, deadline)
    # function entries: call targets, the image entry and (compiler output: COFF / ELF) the symbol table; a
    # `.c28img` from our assembler lists every label, so its symbols are not function starts
    funcs = image.fmt != "c28img" and bool(image.symbols)
    entries = set(calls) | ({int(v) for v in image.symbols.values()} if funcs else set())
    if image.entry is not None:
        entries.add(int(image.entry))
    sc = _C28Scan(desc, pmap)
    if done:
        done = _dataflow(insns, entries, sc, _C28_CALLER, deadline, trusted_entries=funcs)
    return sc.roles, sc.stores, not done


def analyze(image: FirmwareImage, desc: Any, *, vtor: int | None = None, disasm: bool = True,
            time_budget: float = 4.0) -> AnalysisReport:
    import time
    warnings: list[str] = []
    if not image.segments:
        raise FirmwareError("empty firmware image")
    if str(desc.core.arch).lower() == "c28x" or getattr(desc.core, "addr_unit_bits", 8) == 16:
        return _analyze_c28x(image, desc, entry=vtor, disasm=disasm, time_budget=time_budget)
    if str(desc.core.arch).lower() in ("mcs51", "8051"):
        from .firmware_mcs51 import analyze_mcs51
        return analyze_mcs51(image, desc, entry=vtor, disasm=disasm, time_budget=time_budget)
    # --- vector table ------------------------------------------------------------------------------------------
    if vtor is None:
        flash_segs = [a for a, _ in image.segments if (m := desc.memory_at(a)) is not None and m.kind == "flash"]
        vtor = min(flash_segs) if flash_segs else image.start
    nv_max = 16 + desc.core.num_irqs
    vectors: list[int] = []
    for i in range(nv_max):
        w = image.read_word(vtor + 4 * i)
        if w is None:
            break
        vectors.append(w)
    while vectors and vectors[-1] == 0:
        vectors.pop()
    sp = vectors[0] if vectors else None
    pc = vectors[1] if len(vectors) > 1 else None
    problems = []
    if sp is None:
        problems.append("no vector table")
    else:
        if sp & 3:
            problems.append(f"initial SP 0x{sp:08X} not word aligned")
        if not any(m.kind in ("ram", "flexram") and m.base < sp <= m.end for m in desc.memory):
            problems.append(f"initial SP 0x{sp:08X} is not inside a RAM region")
    if pc is None:
        problems.append("no reset vector")
    else:
        if not pc & 1:
            problems.append(f"reset PC 0x{pc:08X} is not a Thumb address (bit 0 clear)")
        m = desc.memory_at(pc & ~1)
        if m is None or m.kind not in ("flash", "flexnvm", "rom"):
            problems.append(f"reset PC 0x{pc:08X} is not inside a flash region")
        elif not image.contains(pc & ~1, 2):
            problems.append(f"reset PC 0x{pc:08X} is outside the loaded image")
    handler_slots = [(i, v) for i, v in enumerate(vectors) if i >= 2 and v not in (0, 0xFFFFFFFF)]
    default = Counter(v for _, v in handler_slots).most_common(1)[0][0] if handler_slots else None
    handlers = [(i, exception_name(i, desc), v) for i, v in handler_slots if v != default]
    # --- segments ------------------------------------------------------------------------------------------------
    segs = []
    for a, d in image.segments:
        m = desc.memory_at(a)
        segs.append((a, len(d), m.name if m else "?"))
        if m is None:
            warnings.append(f"segment 0x{a:08X}+0x{len(d):X} is outside every memory region")
        elif a + len(d) > m.end:
            warnings.append(f"segment 0x{a:08X}+0x{len(d):X} overflows {m.name} (ends 0x{m.end:08X})")
    # --- literal-pool scan ---------------------------------------------------------------------------------------
    pmap = _periph_map(desc)
    prefs: dict[str, PeriphRef] = {}
    mrefs: Counter = Counter()
    mem_sorted = _RangeMap([(m.base, m.size, m.name, m) for m in desc.memory])
    for a, d in image.segments:
        start = (-a) & 3
        cnt = (len(d) - start) // 4
        if cnt <= 0:
            continue
        for w in struct.unpack_from(f"<{cnt}I", d, start):
            if w >= 0x40000000:
                # (bit-band aliases 0x42000000.. are not counted: Thumb code words alias them too often)
                hit = pmap.find(w)
                if hit:
                    name = hit[0]
                    r = prefs.get(name) or prefs.setdefault(name, PeriphRef(name))
                    r.count += 1
                    if w not in r.samples and len(r.samples) < 6:
                        r.samples.append(w)
                continue
            if w >= 0x100:
                mh = mem_sorted.find(w)
                if mh:
                    mrefs[mh[0]] += 1
    # --- capstone pass ------------------------------------------------------------------------------------------
    roles: list[PinRole] = []
    stores: Counter = Counter()
    lit_loads: Counter = Counter()
    code_samples: dict[str, list[int]] = {}
    ninsn, secs = 0, 0.0
    if disasm:
        t0 = time.perf_counter()
        try:
            roles, stores, (lit_loads, code_samples), ninsn, timed_out = _scan_code(
                image, desc, pmap, time_budget, vectors[1:])
            if timed_out:
                warnings.append(f"disassembly pass stopped after {time_budget:.0f} s budget (partial results)")
        except ImportError:
            warnings.append("capstone not available: skipped pin role detection")
        secs = time.perf_counter() - t0
    for name, n in lit_loads.items():      # movw/movt-built addresses never appear in a literal pool
        if name not in prefs:
            prefs[name] = PeriphRef(name, n, code_samples.get(name, []))
    return AnalysisReport(chip=desc.name, source=image.source, fmt=image.fmt, segments=segs, load_addr=image.start,
                          vtor=vtor, initial_sp=sp, reset_pc=pc, bootable=not problems, boot_problems=problems,
                          num_vectors=len(vectors), default_handler=default, handlers=handlers,
                          periph_refs=dict(sorted(prefs.items(), key=lambda kv: -kv[1].count)),
                          memory_refs=dict(mrefs.most_common()), store_sites=dict(stores.most_common()),
                          code_refs=dict(lit_loads.most_common()),
                          pin_roles=roles, pins_configured=_summarise_roles(roles),
                          disasm_insns=ninsn, disasm_seconds=secs, warnings=warnings)


# ---------------------------------------------------------------------------------------------------------------
# C28x (16-bit units): boot entry, PIE/CPU vectors present in the image, peripheral references from DP loads


def _c28x_words(image: FirmwareImage):
    for a, d in image.segments:
        n = len(d) // 2
        yield a, struct.unpack_from(f"<{n}H", d, 0) if n else ()


def _analyze_c28x(image: FirmwareImage, desc: Any, *, entry: int | None = None, disasm: bool = True,
                  time_budget: float = 4.0) -> AnalysisReport:
    from .arch.c28x.arch import vector_name
    warnings: list[str] = []
    boot = (desc.extra.get("boot") or {}) if hasattr(desc, "extra") else {}
    be = boot.get("entry", 0x33FFF6)
    boot_entry = entry if entry is not None else (int(be, 0) if isinstance(be, str) else int(be))
    problems: list[str] = []
    reset_pc = None
    w = image.read(boot_entry, 2)
    if w is not None:
        w0, w1 = struct.unpack("<HH", w)
        if w0 & 0xFFC0 == 0x0040:                          # LB #addr22
            reset_pc = ((w0 & 0x3F) << 16) | w1
        else:
            problems.append(f"no LB instruction at the boot entry 0x{boot_entry:06X} (found 0x{w0:04X} 0x{w1:04X})")
    elif image.entry is not None:
        reset_pc = image.entry
        warnings.append(f"boot entry 0x{boot_entry:06X} not in the image; using the image entry")
    else:
        problems.append(f"boot entry 0x{boot_entry:06X} is not in the image and the image has no entry point")
    if image.entry is not None and reset_pc is not None and image.entry not in (reset_pc, boot_entry):
        # (entry == the boot-to-flash location itself is TI's CodeStartBranch scheme: cl2000 --entry_point=code_start)
        warnings.append(f"image entry 0x{image.entry:06X} differs from the boot-entry branch target 0x{reset_pc:06X}")
    if reset_pc is not None:
        m = desc.memory_at(reset_pc)
        if m is None:
            problems.append(f"reset target 0x{reset_pc:06X} is outside every memory region")
        elif not image.contains(reset_pc):
            problems.append(f"reset target 0x{reset_pc:06X} is outside the loaded image")
    # vectors present in the image (PIE table at 0x0D00 or CPU tables at 0x000000 / 0x3FFFC0)
    handlers = []
    nvec = 0
    for base, count, off in ((0x0D00, 128, 0), (0x3FFFC0, 32, 0), (0x000000, 32, 0)):
        for n in range(count):
            b = image.read(base + 2 * n, 2)
            if b is None:
                continue
            v = struct.unpack("<I", b)[0] & 0x3FFFFF
            nvec += 1
            if v not in (0, 0x3FFFFF):
                handlers.append((n, vector_name(n, desc.irqs), v))
        if handlers:
            break
    default = Counter(v for _, _, v in handlers).most_common(1)[0][0] if len(handlers) > 3 else None
    if default is not None:
        handlers = [h for h in handlers if h[2] != default]
    segs = []
    for a, d in image.segments:
        m = desc.memory_at(a)
        n = len(d) // 2
        segs.append((a, n, m.name if m else (desc.peripheral_at(a).name if desc.peripheral_at(a) else "?")))
        if m is None and desc.peripheral_at(a) is None:
            warnings.append(f"segment 0x{a:06X}+0x{n:X} is outside every memory region")
        elif m is not None and a + n > m.end:
            warnings.append(f"segment 0x{a:06X}+0x{n:X} overflows {m.name} (ends 0x{m.end:06X})")
    # peripheral references: MOVW DP,#16 (761F imm), MOVZ DP,#10 (B8xx-BBxx), MOVL XARn,#22
    pmap = _periph_map(desc)
    prefs: dict[str, PeriphRef] = {}
    mrefs: Counter = Counter()

    def note(addr: int, page: bool) -> None:
        hit = None
        for k in range(64 if page else 1):
            hit = pmap.find(addr + k)
            if hit:
                addr = addr + k
                break
        if hit:
            name = hit[0]
            r = prefs.get(name) or prefs.setdefault(name, PeriphRef(name))
            r.count += 1
            if addr not in r.samples and len(r.samples) < 6:
                r.samples.append(addr)
        elif not page:
            mh = desc.memory_at(addr)
            if mh is not None:
                mrefs[mh.name] += 1
    ninsn = 0
    for a, ws in _c28x_words(image):
        n = len(ws)
        i = 0
        while i < n:
            w0 = ws[i]
            if w0 == 0x761F and i + 1 < n:
                note(ws[i + 1] << 6, True)
                ninsn += 1
                i += 2
                continue
            if w0 & 0xFC00 == 0xB800:
                note((w0 & 0x3FF) << 6, True)
                ninsn += 1
            elif i + 1 < n and ((w0 >> 8) in (0x8D, 0x8F) or (w0 >> 8 == 0x76 and w0 & 0x80)) \
                    and not (w0 >> 8 == 0x8F and w0 & 0x80):
                note(((w0 & 0x3F) << 16) | ws[i + 1], False)
                ninsn += 1
                i += 2
                continue
            i += 1
    roles: list[PinRole] = []
    stores: Counter = Counter()
    secs = 0.0
    if disasm:
        import time
        t0 = time.perf_counter()
        try:
            roles, stores, timed_out = _scan_c28x_stores(image, desc, pmap, time_budget)
            if timed_out:
                warnings.append(f"store scan stopped after {time_budget:.0f} s budget (partial results)")
        except Exception as e:                           # ISA table unavailable / mid-edit: best effort
            warnings.append(f"C28x store scan skipped: {e}")
        secs = time.perf_counter() - t0
    return AnalysisReport(chip=desc.name, source=image.source, fmt=image.fmt, segments=segs, load_addr=image.start,
                          vtor=boot_entry, initial_sp=int(boot.get("sp", 0x400)), reset_pc=reset_pc,
                          bootable=not problems, boot_problems=problems, num_vectors=nvec, default_handler=default,
                          handlers=handlers, periph_refs=dict(sorted(prefs.items(), key=lambda kv: -kv[1].count)),
                          memory_refs=dict(mrefs.most_common()), store_sites=dict(stores.most_common()),
                          code_refs={}, pin_roles=roles, pins_configured=_summarise_roles(roles),
                          disasm_insns=ninsn, disasm_seconds=secs, warnings=warnings, arch="c28x", unit_bytes=2,
                          symbols=dict(image.symbols or {}))


def _summarise_roles(roles: list[PinRole]) -> dict[str, list[str]]:
    pins_cfg: dict[str, list[str]] = {}
    for r in roles:
        if "," in r.pin or not r.pin[-1].isdigit():
            continue
        notes = pins_cfg.setdefault(r.pin, [])
        s = f"{r.reg}{'=0x%X' % r.value if r.value is not None else ''} {r.note}".strip()
        if s not in notes:
            notes.append(s)
    return dict(sorted(pins_cfg.items(), key=_pin_sort))


def _format_c28x(r: AnalysisReport) -> str:
    L = []
    A = L.append
    A(f"Firmware: {r.source} ({r.fmt}), chip {r.chip} (C28x, 16-bit words)")
    for a, n, reg in r.segments:
        A(f"  segment 0x{a:06X}..0x{a + n - 1:06X} ({n} words) in {reg}")
    A("")
    tgt = "-" if r.reset_pc is None else f"0x{r.reset_pc:06X}"
    A(f"Boot entry 0x{r.vtor:06X} -> {tgt}   SP after reset 0x{r.initial_sp or 0:04X}   "
      f"{'bootable' if r.bootable else 'NOT bootable'}")
    for p in r.boot_problems:
        A(f"  ! {p}")
    if r.symbols:
        by_addr = {v: k for k, v in sorted(r.symbols.items(), reverse=True)}
        tgt_name = by_addr.get(r.reset_pc) if r.reset_pc is not None else None
        keys = [k for k in ("_c_int00", "c_int00", "_main", "main") if k in r.symbols]
        A(f"Symbols: {len(r.symbols)}" + (f"   reset target = {tgt_name}" if tgt_name else "")
          + ("   " + ", ".join(f"{k} 0x{r.symbols[k]:06X}" for k in keys) if keys else ""))
    if r.num_vectors:
        A(f"Vectors in the image: {r.num_vectors} slots, {len(r.handlers)} handlers"
          + (f" (default 0x{r.default_handler:06X})" if r.default_handler is not None else ""))
        for i, name, v in r.handlers:
            A(f"    [{i:3d}] {name:<14} 0x{v:06X}")
    else:
        A("No vector table in the image (PIE vectors are normally copied to 0x0D00 at run time)")
    A("")
    A(f"Peripheral references (MOVW/MOVZ DP + MOVL XARn,#22 scan, {r.disasm_insns} hits, "
      f"{len(r.periph_refs)} blocks):")
    for name, ref in r.periph_refs.items():
        A(f"  {name:<10} {ref.count:5d}  e.g. " + ", ".join(f"0x{s:04X}" for s in ref.samples[:4]))
    if r.memory_refs:
        A("Memory references: " + ", ".join(f"{k} {v}" for k, v in r.memory_refs.items()))
    if r.store_sites:
        A(f"Peripheral stores (DP @off, *+XARn[k], *XARn++, *(0:abs)): " + ", ".join(f"{k} {v}" for k, v in r.store_sites.items())
          + f"   ({r.disasm_seconds:.2f} s)")
    _format_pin_roles(r, A)
    for w in r.warnings:
        A(f"warning: {w}")
    return "\n".join(L)


def _format_pin_roles(r: AnalysisReport, A, max_pin_roles: int = 60) -> None:
    if r.pins_configured:
        A(f"Provisional pin roles ({len(r.pins_configured)} pins written directly):")
        for pin, notes in list(r.pins_configured.items())[:max_pin_roles]:
            A(f"  {pin:<6} " + "; ".join(notes[:3]) + (f" (+{len(notes) - 3} more)" if len(notes) > 3 else ""))


def _pin_sort(kv):
    m = re.fullmatch(r"(\D*)(\d+)", kv[0])       # PTC5, PA10, GPIO34 -> (prefix, number)
    return (m.group(1), int(m.group(2))) if m else (kv[0], 0)


def _h(v: int | None) -> str:
    return "-" if v is None else f"0x{v:08X}"


def format_report(r: AnalysisReport, *, max_pin_roles: int = 60) -> str:
    if r.arch == "c28x":
        return _format_c28x(r)
    if r.arch == "mcs51":
        from .firmware_mcs51 import format_mcs51
        return format_mcs51(r)
    L = []
    A = L.append
    A(f"Firmware: {r.source} ({r.fmt}), chip {r.chip}")
    for a, n, reg in r.segments:
        A(f"  segment 0x{a:08X}..0x{a + n - 1:08X} ({n} bytes) in {reg}")
    A("")
    A(f"Vector table @ 0x{r.vtor:08X} ({r.num_vectors} entries)")
    A(f"  initial SP {_h(r.initial_sp)}   reset PC {_h(r.reset_pc)}   "
      f"{'bootable' if r.bootable else 'NOT bootable'}")
    for p in r.boot_problems:
        A(f"  ! {p}")
    if r.default_handler is not None:
        A(f"  default handler {_h(r.default_handler)}")
    A(f"  {len(r.handlers)} non-default handlers:")
    for i, name, v in r.handlers:
        kind = "exc" if i < 16 else f"IRQ{i - 16:<3d}"
        A(f"    [{i:3d}] {kind:<6} {name:<26} {_h(v)}")
    A("")
    A(f"Peripheral references (literal-pool + code scan, {len(r.periph_refs)} blocks):")
    for name, ref in r.periph_refs.items():
        A(f"  {name:<8} {ref.count:5d}  e.g. " + ", ".join(f"0x{s:08X}" for s in ref.samples[:4]))
    if r.memory_refs:
        A("Memory references: " + ", ".join(f"{k} {v}" for k, v in r.memory_refs.items()))
    A("")
    A(f"Code scan: {r.disasm_insns} Thumb instructions in {r.disasm_seconds:.2f} s")
    if r.code_refs:
        A("  address loads (ldr [pc] / movw+movt): " + ", ".join(f"{k} {v}" for k, v in r.code_refs.items()))
    if r.store_sites:
        A("  stores via a known base (literal, movw+movt, base+offset, HAL GPIO calls): " + ", ".join(f"{k} {v}" for k, v in r.store_sites.items()))
    _format_pin_roles(r, A, max_pin_roles)
    gp = [x for x in r.pin_roles if x.reg.split("_")[-1] in ("PDDR",)]
    if gp:
        A("GPIO direction writes:")
        seen = set()
        for x in gp:
            k = (x.reg, x.value)
            if k in seen:
                continue
            seen.add(k)
            A(f"  {x.reg:<12} @0x{x.pc:08X} {'=0x%X' % x.value if x.value is not None else '(value unknown)'} {x.note}")
    for w in r.warnings:
        A(f"warning: {w}")
    return "\n".join(L)
