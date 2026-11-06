"""Register tables generated from CMSIS-SVD by tools/svd2chip.py (schema: chips/README.md).

Dependency-free apart from PyYAML::

    from chipem.regtable import load_regs, regs_for
    rt = load_regs("chips/mk22_regs.yaml")
    sim = rt.peripheral("SIM")
    sim["SCGC5"].reset            # 0x00040182
    sim.at(0x1038).name           # "SCGC5"
    sim["SCGC5"].field("PORTA")   # Field(name='PORTA', lsb=9, width=1, ...)
    regs_for("VBAT", "chips/mk22_regs.yaml")   # aliases: VBAT -> RFVBAT, PTA -> GPIOA, ...

Peripheral models use ``PeripheralRegs.reg_specs()`` to build their ``periph.base.Reg`` descriptors
(name, offset, width, reset, ro_mask, w1c_mask, unit_bits) without this module importing the engine.

Offsets and widths are in the table's address units: ``address_unit_bits`` in the table header (8 for SVD tables,
16 for the C2000 table; ``unit_bits`` or ``cpu: {unit_bits: ..}`` are accepted too).  ``Register.full_mask``,
``ro_mask`` and ``reg_specs()`` use ``width * unit_bits`` bits, so a width-2 register of a 16-bit table is 32 bits.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from functools import lru_cache
from pathlib import Path

import yaml

try:  # libyaml is ~10x faster on the multi-MB generated tables
    _Loader = yaml.CSafeLoader
except AttributeError:  # pragma: no cover
    _Loader = yaml.SafeLoader

# Common names used by chip YAMLs / models that differ from SVD peripheral names.
ALIASES: dict[str, tuple[str, ...]] = {
    "VBAT": ("RFVBAT",), "RFVBAT": ("VBAT",), "OSC0": ("OSC",), "OSC": ("OSC0",),
    "PTA": ("GPIOA",), "PTB": ("GPIOB",), "PTC": ("GPIOC",), "PTD": ("GPIOD",), "PTE": ("GPIOE",),
    "LPTMR": ("LPTMR0",), "USB": ("USB0",), "I2S": ("I2S0",), "PDB": ("PDB0",), "DAC": ("DAC0",),
    "CMP": ("CMP0",), "FTFL": ("FTFE",), "FTFA": ("FTFE",), "FLASH": ("FTFE",), "NV": ("FTFE_FlashConfig",),
}

_WRITABLE = ("read-write", "write-only", "writeOnce", "read-writeOnce")


@dataclass(frozen=True)
class Field:
    name: str
    lsb: int
    width: int
    access: str | None = None          # None = same as the register
    enum: dict[int, str] = dc_field(default_factory=dict, compare=False, hash=False)
    write_effect: str | None = None    # SVD modifiedWriteValues, e.g. "oneToClear"

    @property
    def mask(self) -> int:
        return ((1 << self.width) - 1) << self.lsb

    def get(self, value: int) -> int:
        return (value >> self.lsb) & ((1 << self.width) - 1)

    def put(self, value: int, fv: int) -> int:
        return (value & ~self.mask) | ((fv << self.lsb) & self.mask)


@dataclass
class Register:
    name: str
    offset: int
    width: int                         # address units (bytes for SVD tables, 16-bit words for C2000)
    reset: int
    reset_mask: int
    access: str
    fields: list[Field]
    _by_name: dict[str, Field] = dc_field(default_factory=dict, repr=False, compare=False)
    unit_bits: int = dc_field(default=8, compare=False)    # bits per address unit (width counts units)

    def __post_init__(self):
        self._by_name = {f.name: f for f in self.fields}

    @property
    def full_mask(self) -> int:
        return (1 << (self.unit_bits * self.width)) - 1

    @property
    def end(self) -> int:
        return self.offset + self.width

    def field(self, name: str) -> Field:
        return self._by_name[name]

    def has_field(self, name: str) -> bool:
        return name in self._by_name

    @property
    def readonly(self) -> bool:
        return self.access == "read-only"

    @property
    def writeonly(self) -> bool:
        return self.access == "write-only"

    @property
    def ro_mask(self) -> int:
        """Bits a CPU write cannot change: whole register if read-only; else read-only fields plus bits not
        covered by any field (reserved).  A register without fields is fully writable unless read-only."""
        if self.readonly:
            return self.full_mask
        if not self.fields:
            return 0
        writable = 0
        for f in self.fields:
            if (f.access or self.access) in _WRITABLE:
                writable |= f.mask
        return self.full_mask & ~writable

    @property
    def w1c_mask(self) -> int:
        m = 0
        for f in self.fields:
            if f.write_effect == "oneToClear":
                m |= f.mask
        return m

    def decode(self, value: int) -> dict[str, int]:
        return {f.name: f.get(value) for f in self.fields}


@dataclass
class PeripheralRegs:
    name: str
    base: int
    size: int
    registers: list[Register]
    derived_from: str | None = None
    description: str | None = None
    group: str | None = None
    interrupts: dict[int, str] = dc_field(default_factory=dict)
    unit_bits: int = 8

    def __post_init__(self):
        self.registers.sort(key=lambda r: (r.offset, r.name))
        self._by_name = {r.name: r for r in self.registers}
        self._by_off: dict[int, Register] = {}
        for r in self.registers:
            self._by_off.setdefault(r.offset, r)

    def __getitem__(self, name: str) -> Register:
        return self._by_name[name]

    def __contains__(self, name: str) -> bool:
        return name in self._by_name

    def __iter__(self):
        return iter(self.registers)

    def __len__(self):
        return len(self.registers)

    def reg(self, name: str) -> Register:
        return self._by_name[name]

    def at(self, offset: int) -> Register | None:
        """Register starting exactly at ``offset``."""
        return self._by_off.get(offset)

    def containing(self, offset: int) -> Register | None:
        """Register whose byte range covers ``offset`` (for sub-word accesses)."""
        r = self._by_off.get(offset)
        if r is not None:
            return r
        for r in self.registers:
            if r.offset <= offset < r.end:
                return r
        return None

    def offset_of(self, name: str) -> int:
        return self._by_name[name].offset

    def reset_values(self) -> dict[str, int]:
        return {r.name: r.reset for r in self.registers}

    def names(self) -> list[str]:
        return [r.name for r in self.registers]

    def reg_specs(self, unit_bits: int | None = None) -> list[dict]:
        """kwargs for ``chipem.periph.base.Reg`` (name, offset, width, reset, ro_mask, w1c_mask, unit_bits).
        ``unit_bits`` defaults to the table's address unit (masks span ``width * unit_bits`` bits)."""
        ub = self.unit_bits if unit_bits is None else int(unit_bits)
        out = []
        for r in self.registers:
            if r.unit_bits != ub:
                r = Register(r.name, r.offset, r.width, r.reset, r.reset_mask, r.access, r.fields, unit_bits=ub)
            out.append(dict(name=r.name, offset=r.offset, width=r.width, reset=r.reset & r.reset_mask,
                            ro_mask=r.ro_mask, w1c_mask=r.w1c_mask, unit_bits=ub))
        return out


def _mk_register(d: dict, unit_bits: int = 8) -> Register:
    fields = [Field(name=str(f["name"]), lsb=int(f["lsb"]), width=int(f["width"]), access=f.get("access"),
                    enum={int(k): str(v) for k, v in (f.get("enum") or {}).items()},
                    write_effect=f.get("write_effect"))
              for f in d.get("fields") or []]
    width = int(d.get("width", 4))
    full = (1 << (unit_bits * width)) - 1
    rm = d.get("reset_mask")
    return Register(name=str(d["name"]), offset=int(d["offset"]), width=width, reset=int(d.get("reset", 0)),
                    reset_mask=full if rm is None else int(rm), access=d.get("access") or "read-write",
                    fields=fields, unit_bits=unit_bits)


class RegTable:
    def __init__(self, doc: dict, path: str | None = None):
        self.path = path
        self.device: str = doc.get("device")
        self.cpu: dict = doc.get("cpu") or {}
        self.address_unit_bits: int = int(doc.get("address_unit_bits") or doc.get("unit_bits")
                                          or self.cpu.get("unit_bits") or 8)
        self.unit_bits = self.address_unit_bits
        self.interrupts: dict[int, str] = {int(k): v for k, v in (doc.get("interrupts") or {}).items()}
        self._raw: dict[str, dict] = doc.get("peripherals") or {}
        self._cache: dict[str, PeripheralRegs] = {}

    def __contains__(self, name: str) -> bool:
        return self.resolve_name(name) is not None

    def names(self) -> list[str]:
        return list(self._raw)

    def resolve_name(self, name: str) -> str | None:
        if name in self._raw:
            return name
        for a in ALIASES.get(name, ()):
            if a in self._raw:
                return a
        return None

    def _reg_dicts(self, name: str, seen=()) -> list[dict]:
        p = self._raw[name]
        own = list(p.get("registers") or [])
        base = p.get("derived_from")
        if not base:
            return own
        if base in seen or base not in self._raw:
            raise ValueError(f"{name}: bad derived_from {base!r}")
        merged = {r["name"]: r for r in self._reg_dicts(base, seen + (name,))}
        for r in own:                          # derived peripheral's own registers add/override by name
            merged[r["name"]] = r
        return list(merged.values())

    def peripheral(self, name: str) -> PeripheralRegs:
        real = self.resolve_name(name)
        if real is None:
            raise KeyError(f"peripheral {name!r} not in register table {self.path or self.device}")
        if real not in self._cache:
            p = self._raw[real]
            self._cache[real] = PeripheralRegs(
                name=real, base=int(p["base"]), size=int(p["size"]),
                registers=[_mk_register(r, self.unit_bits) for r in self._reg_dicts(real)],
                derived_from=p.get("derived_from"), description=p.get("description"), group=p.get("group"),
                interrupts={int(k): v for k, v in (p.get("interrupts") or {}).items()}, unit_bits=self.unit_bits)
        return self._cache[real]

    def get(self, name: str) -> PeripheralRegs | None:
        try:
            return self.peripheral(name)
        except KeyError:
            return None

    def regs_for(self, block_name: str) -> PeripheralRegs | None:
        return self.get(block_name)

    def by_address(self, addr: int) -> tuple[PeripheralRegs, Register | None] | None:
        for n in self._raw:
            p = self._raw[n]
            if int(p["base"]) <= addr < int(p["base"]) + int(p["size"]):
                pr = self.peripheral(n)
                return pr, pr.containing(addr - pr.base)
        return None


@lru_cache(maxsize=8)
def _load_cached(path: str, mtime: float) -> RegTable:
    with open(path, "rb") as f:
        doc = yaml.load(f, Loader=_Loader)
    return RegTable(doc or {}, path)


def load_regs(path: str | Path) -> RegTable:
    """Load (and cache) a generated *_regs.yaml."""
    p = Path(path).resolve()
    return _load_cached(str(p), p.stat().st_mtime)


DEFAULT_REGS = Path(__file__).resolve().parent.parent / "chips" / "mk22_regs.yaml"


def regs_for(block_name: str, regs: RegTable | str | Path | None = None) -> PeripheralRegs | None:
    """Register descriptions for an MMIO block (by chip-YAML block name; SVD aliases resolved).
    ``regs`` is a RegTable or a path (default chips/mk22_regs.yaml). Returns None when unknown/absent."""
    if regs is None:
        regs = DEFAULT_REGS
    if not isinstance(regs, RegTable):
        if not Path(regs).exists():
            return None
        regs = load_regs(regs)
    return regs.get(block_name)
