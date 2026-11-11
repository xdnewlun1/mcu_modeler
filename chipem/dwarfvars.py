"""Variables for the debugger: locals / arguments of a frame, globals and statics, and a C-like expression evaluator,
from DWARF (pyelftools) where the ELF has it. Used through `Debugger.frame_vars / globals_list / evaluate`
(chipem/debug.py), the shell (`info locals`, `print`, `display`) and the web API (/api/debug/locals|globals|eval).

Every value is one *item* (the same shape everywhere, nested for structs / arrays):
    {"name", "kind": "arg"|"local"|"global"|"static"|"member"|"element"|"expr"|"register"|"field",
     "type": "uint32_t" (C spelling of the declared type), "value": int|float|None, "text": formatted value,
     "addr": int|None (memory-resident objects), "loc": "r4" | "[sp+8]" | "cfa-12" | "0x20000010" | ...,
     "avail": bool, "reason": str|None (why not available), "children": [items] (struct members, array elements:
     at most 64 per level, 3 levels deep), "count": int (arrays: the full element count)}

Location support (DWARF 2-5, clang / GCC / TI cl2000):
  * DW_OP_addr / DW_OP_addrx (statics), DW_OP_fbreg against DW_AT_frame_base (DW_OP_call_frame_cfa, a register or
    DW_OP_bregN), DW_OP_bregN / bregx, DW_OP_regN / regx (frame 0: the live register; outer frames: only registers
    the CFI unwinder recovered, i.e. saved by a callee or callee-saved and untouched), DW_OP_piece, DW_OP_stack_value,
    DW_OP_implicit_value, literals and simple arithmetic, DW_OP_call_frame_cfa.
  * Location lists (DWARF 4 .debug_loc and 5 .debug_loclists): the entry covering the frame's PC (PC - 1 for outer
    frames, the call instruction); no entry = optimized out at this PC.
  * Anything else is *unavailable* with a reason (DW_OP_entry_value, DW_OP_deref_size on registers, unknown
    opcodes, registers the unwinder could not recover) - never a guessed value.
C28x (TI cl2000): 16-bit address units (sizes, offsets and DW_OP_piece sizes are in words); register numbers per the
C28x EABI (SPRAC71 Table 10-1); a 32-bit value "in" a 16-bit register (AL, PL, ARn, TL) is the register pair
(ACC, P, XARn, XT), as TI emits. No C28x unwinder: outer frames show statics only.

Parsed data are cached per ELF (path, size, mtime) and built lazily: the first call scans the CU top-level DIEs
(functions, globals, named types); a function's scope tree is read when its frame is shown.
"""
from __future__ import annotations

import bisect
import io
import math
import os
import re
import struct
from dataclasses import dataclass, field
from typing import Any

__all__ = ["VarIndex", "index_for", "frame_vars", "globals_list", "evaluate", "MAX_CHILDREN", "MAX_DEPTH"]

MAX_CHILDREN = 64
MAX_DEPTH = 3
_EXC_RETURN_MIN = 0xFFFFFFE0


class Unavailable(ValueError):
    """A value or location that cannot be determined (the message is the reason shown to the user)."""


class EvalError(ValueError):
    pass


def _s(v: Any) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)


# ================================================================================================= types
@dataclass(eq=False)
class CType:
    kind: str                         # base enum ptr struct union array void func typedef const volatile
    name: str = ""                    # base / typedef / struct tag / enum tag
    size: int = 0                     # address units
    target: "CType | None" = None     # ptr / typedef / qualifiers / array element / enum underlying
    enc: int = 0                      # DW_ATE_* (base)
    members: list = field(default_factory=list)   # struct/union: (name, offset_units, CType, bit_size, bit_pos)
    dims: list = field(default_factory=list)      # array: element counts (None = unknown)
    enums: dict = field(default_factory=dict)     # enum: value -> name
    incomplete: bool = False


VOID = CType("void", "void")
_ATE_FLOAT, _ATE_BOOL, _ATE_SIGNED, _ATE_SCHAR, _ATE_UNSIGNED, _ATE_UCHAR = 4, 2, 5, 6, 7, 8


def strip(t: CType | None) -> CType:
    """The concrete type below typedefs and qualifiers."""
    n = 0
    while t is not None and t.kind in ("typedef", "const", "volatile") and n < 32:
        t = t.target
        n += 1
    return t if t is not None else VOID


def tname(t: CType | None) -> str:
    """C spelling: 'uint32_t', 'volatile int', 'struct S *', 'char [16]', 'int [2][3]'."""
    return _tname(t, "").strip()


def _tname(t: CType | None, inner: str) -> str:
    if t is None:
        return ("void " + inner).strip()
    k = t.kind
    if k in ("base", "typedef", "void"):
        return f"{t.name} {inner}".strip() if inner else t.name
    if k in ("struct", "union", "enum"):
        base = f"{k} {t.name}" if t.name else f"{k} {{...}}"
        return f"{base} {inner}".strip()
    if k in ("const", "volatile"):
        tt = t.target
        if tt is not None and tt.kind == "ptr":
            return _tname(tt, f"{k} {inner}".strip())
        return f"{k} {_tname(tt, inner)}".strip()
    if k == "ptr":
        tt = t.target
        if tt is not None and tt.kind in ("array", "func"):
            return _tname(tt, f"(*{inner})")
        return _tname(tt, f"*{inner}")
    if k == "array":
        dims = "".join(f"[{d}]" if d is not None else "[]" for d in t.dims)
        return _tname(t.target, f"{inner}{dims}" if inner.startswith("(") else f"{inner} {dims}".strip()
                      if inner else dims).replace("  ", " ")
    if k == "func":
        return _tname(t.target, f"{inner}()") if inner else (_tname(t.target, "") + " ()")
    return "?"


def _builtin_types(ub: int, ptr_size: int) -> dict[str, CType]:
    """C base types for casts when the DWARF does not name them (sizes in address units)."""
    c28 = ub == 2
    sizes = {"char": 1, "short": 1 if c28 else 2, "int": 1 if c28 else 4, "long": 2 if c28 else 4,
             "long long": 4 if c28 else 8}
    out: dict[str, CType] = {}
    for n, sz in sizes.items():
        enc = _ATE_SCHAR if n == "char" else _ATE_SIGNED
        out[n] = CType("base", n, sz, enc=enc)
        out["signed " + n] = CType("base", "signed " + n, sz, enc=enc)
        out["unsigned " + n] = CType("base", "unsigned " + n, sz, enc=_ATE_UCHAR if n == "char" else _ATE_UNSIGNED)
    out["unsigned"] = out["unsigned int"]
    out["signed"] = out["int"]
    out["float"] = CType("base", "float", 2 if c28 else 4, enc=_ATE_FLOAT)
    out["double"] = CType("base", "double", 4 if c28 else 8, enc=_ATE_FLOAT)
    out["_Bool"] = out["bool"] = CType("base", "_Bool", 1, enc=_ATE_BOOL)
    for bits in (8, 16, 32, 64):
        units = max(1, bits // (8 * ub))
        out[f"uint{bits}_t"] = CType("base", f"uint{bits}_t", units, enc=_ATE_UNSIGNED)
        out[f"int{bits}_t"] = CType("base", f"int{bits}_t", units, enc=_ATE_SIGNED)
    out["void"] = VOID
    return out


# ================================================================================================= the per-ELF index
@dataclass
class GlobalVar:
    name: str
    die: Any
    addr: int
    file: str | None
    func: str | None = None           # function-local static: its function


@dataclass
class SubProg:
    name: str
    ranges: list                      # [(lo, hi)]
    die: Any


_CACHE: dict[tuple, "VarIndex"] = {}


def index_for(path: str) -> "VarIndex | None":
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (os.path.abspath(path), st.st_size, st.st_mtime_ns)
    vi = _CACHE.get(key)
    if vi is None:
        vi = VarIndex(path)
        if len(_CACHE) > 16:
            _CACHE.clear()
        _CACHE[key] = vi
    return vi


class VarIndex:
    """DWARF variable information of one ELF (lazily parsed; see the module docstring)."""

    def __init__(self, path: str):
        self.path = str(path)
        self._dw = None
        self._ok: bool | None = None
        self.subs: list[SubProg] = []
        self._sub_starts: list[int] = []
        self.globals: list[GlobalVar] | None = None        # CU-level (+ function statics after all_globals())
        self._statics_done = False
        self.by_name: dict[str, list[GlobalVar]] = {}
        self.types_by_name: dict[str, Any] = {}           # name -> DIE (typedef / base / struct / union / enum)
        self._tcache: dict[tuple, CType] = {}
        self._expr_cache: dict[tuple, list] = {}
        self._files: dict[int, list[str]] = {}
        self.ptr_size = 4
        self.ub = 1

    # -------------------------------------------------------------------------------------------------- loading
    def ok(self) -> bool:
        if self._ok is None:
            self._ok = self._load()
        return self._ok

    def _load(self) -> bool:
        try:
            from elftools.elf.elffile import ELFFile
            with open(self.path, "rb") as f:
                data = f.read()
            if data[:4] != b"\x7fELF":
                return False
            elf = ELFFile(io.BytesIO(data))
            if not elf.has_dwarf_info():
                return False
            self._dw = elf.get_dwarf_info()
            self.ub = 2 if elf["e_machine"] in ("EM_TI_C2000", 141) else 1      # bytes per address unit
        except Exception:
            return False
        self.globals = []
        subs: list[SubProg] = []
        for cu in self._dw.iter_CUs():
            try:
                top = cu.get_top_DIE()
            except Exception:
                continue
            self.ptr_size = cu.header.get("address_size", 4) or 4
            stack = list(top.iter_children())
            while stack:
                die = stack.pop()
                tag = die.tag
                if tag == "DW_TAG_namespace":
                    stack.extend(die.iter_children())
                elif tag == "DW_TAG_subprogram":
                    rng = self.die_ranges(die)
                    if rng:
                        nm = self.die_name(die) or "?"
                        subs.append(SubProg(nm, rng, die))
                elif tag == "DW_TAG_variable":
                    g = self._global_of(die, None)
                    if g is not None:
                        self.globals.append(g)
                elif tag in ("DW_TAG_typedef", "DW_TAG_base_type", "DW_TAG_structure_type", "DW_TAG_union_type",
                             "DW_TAG_enumeration_type", "DW_TAG_class_type"):
                    nm = self.die_name(die)
                    if nm and "DW_AT_declaration" not in die.attributes:
                        key = {"DW_TAG_structure_type": "struct ", "DW_TAG_class_type": "struct ",
                               "DW_TAG_union_type": "union ", "DW_TAG_enumeration_type": "enum "}.get(tag, "") + nm
                        self.types_by_name.setdefault(key, die)
        subs.sort(key=lambda s: min(lo for lo, _ in s.ranges))
        self.subs = subs
        self._sub_starts = [min(lo for lo, _ in s.ranges) for s in subs]
        for g in self.globals:
            self.by_name.setdefault(g.name, []).append(g)
        return True

    def all_globals(self) -> list[GlobalVar]:
        """CU-level variables plus function-local statics (scanned on first use)."""
        if not self.ok():
            return []
        if not self._statics_done:
            self._statics_done = True
            for sp in self.subs:
                stack = list(sp.die.iter_children())
                while stack:
                    die = stack.pop()
                    if die.tag in ("DW_TAG_lexical_block", "DW_TAG_inlined_subroutine"):
                        stack.extend(die.iter_children())
                    elif die.tag == "DW_TAG_variable":
                        g = self._global_of(die, sp.name)
                        if g is not None:
                            self.globals.append(g)
                            self.by_name.setdefault(f"{sp.name}::{g.name}", []).append(g)
        return self.globals or []

    def _global_of(self, die, func: str | None) -> GlobalVar | None:
        at = die.attributes
        if "DW_AT_location" not in at:
            return None
        addr = self._static_addr(die)
        if addr is None:
            return None
        name = self.die_name(die)
        if not name:
            return None
        return GlobalVar(name, die, addr, self.decl_file(die), func)

    def _static_addr(self, die) -> int | None:
        a = die.attributes["DW_AT_location"]
        if a.form != "DW_FORM_exprloc" and not a.form.startswith("DW_FORM_block"):
            return None
        try:
            ops = self.parse_expr(die.cu, a.value)
        except Exception:
            return None
        if not ops or ops[0].op_name not in ("DW_OP_addr", "DW_OP_addrx", "DW_OP_GNU_addr_index"):
            return None
        addr = self._addr_arg(die.cu, ops[0])
        for op in ops[1:]:
            if op.op_name == "DW_OP_plus_uconst":
                addr += op.args[0]
            else:
                return None
        return addr

    def _addr_arg(self, cu, op) -> int:
        if op.op_name == "DW_OP_addr":
            return int(op.args[0])
        return int(self._dw.get_addr(cu, op.args[0]))

    # -------------------------------------------------------------------------------------------------- DIE helpers
    def parse_expr(self, cu, value) -> list:
        key = (id(cu.structs), tuple(value))
        ops = self._expr_cache.get(key)
        if ops is None:
            from elftools.dwarf.dwarf_expr import DWARFExprParser
            ops = DWARFExprParser(cu.structs).parse_expr(list(value))
            self._expr_cache[key] = ops
        return ops

    @staticmethod
    def _origin(die):
        at = die.attributes
        for k in ("DW_AT_abstract_origin", "DW_AT_specification"):
            if k in at:
                try:
                    return die.get_DIE_from_attribute(k)
                except Exception:
                    return None
        return None

    def die_attr(self, die, name: str):
        n = 0
        while die is not None and n < 8:
            if name in die.attributes:
                return die, die.attributes[name]
            die = self._origin(die)
            n += 1
        return None, None

    def die_name(self, die) -> str | None:
        _, a = self.die_attr(die, "DW_AT_name")
        return _s(a.value) if a is not None else None

    def die_type(self, die) -> CType:
        d2, a = self.die_attr(die, "DW_AT_type")
        if a is None:
            return VOID
        try:
            return self.ctype(d2.get_DIE_from_attribute("DW_AT_type"))
        except Exception:
            return CType("base", "?", 0)

    def decl_file(self, die) -> str | None:
        d2, a = self.die_attr(die, "DW_AT_decl_file")
        if a is None:
            return None
        cu = d2.cu
        names = self._files.get(cu.cu_offset)
        if names is None:
            names = []
            try:
                lp = self._dw.line_program_for_CU(cu)
                ver = lp.header.get("version", 4)
                dirs = [_s(x) for x in lp.header.get("include_directory", [])]
                for fe in lp.header.get("file_entry", []):
                    di = fe.dir_index
                    d = (dirs[di] if 0 <= di < len(dirs) else "") if ver >= 5 else \
                        (dirs[di - 1] if 0 < di <= len(dirs) else "")
                    names.append(os.path.normpath(os.path.join(d, _s(fe.name))) if d else _s(fe.name))
                if ver < 5:
                    names.insert(0, "?")
            except Exception:
                names = []
            self._files[cu.cu_offset] = names
        i = int(a.value)
        return names[i] if 0 <= i < len(names) else None

    def cu_base(self, cu) -> int:
        try:
            top = cu.get_top_DIE()
            if "DW_AT_low_pc" in top.attributes:
                return int(top.attributes["DW_AT_low_pc"].value)
        except Exception:
            pass
        return 0

    def die_ranges(self, die) -> list[tuple[int, int]]:
        at = die.attributes
        if "DW_AT_low_pc" in at and "DW_AT_high_pc" in at:
            lo = int(at["DW_AT_low_pc"].value)
            hv = at["DW_AT_high_pc"]
            hi = int(hv.value) if hv.form == "DW_FORM_addr" else lo + int(hv.value)
            return [(lo, hi)] if hi > lo else []
        if "DW_AT_ranges" in at:
            try:
                rl = self._dw.range_lists()
                ents = rl.get_range_list_at_offset(int(at["DW_AT_ranges"].value), cu=die.cu)
            except Exception:
                return []
            base = self.cu_base(die.cu)
            out = []
            for e in ents:
                if hasattr(e, "base_address"):
                    base = int(e.base_address)
                    continue
                lo, hi = (e.begin_offset, e.end_offset) if e.is_absolute else (base + e.begin_offset,
                                                                                   base + e.end_offset)
                if hi > lo:
                    out.append((int(lo), int(hi)))
            return out
        if "DW_AT_low_pc" in at:
            lo = int(at["DW_AT_low_pc"].value)
            return [(lo, lo + 1)]
        return []

    def covers(self, die, pc: int) -> bool:
        return any(lo <= pc < hi for lo, hi in self.die_ranges(die))

    def sub_for(self, pc: int) -> SubProg | None:
        if not self.ok():
            return None
        i = bisect.bisect_right(self._sub_starts, pc) - 1
        best = None
        while i >= 0:
            s = self.subs[i]
            if any(lo <= pc < hi for lo, hi in s.ranges):
                if best is None or _span(s) < _span(best):
                    best = s
            if pc - self._sub_starts[i] > 0x100000:
                break
            i -= 1
        return best

    def type_named(self, name: str) -> CType | None:
        if not self.ok():
            return None
        die = self.types_by_name.get(name)
        return self.ctype(die) if die is not None else None

    # -------------------------------------------------------------------------------------------------- types
    def ctype(self, die) -> CType:
        key = (type(die.cu).__name__, die.offset)
        t = self._tcache.get(key)
        if t is not None:
            return t
        at = die.attributes
        tag = die.tag
        name = _s(at["DW_AT_name"].value) if "DW_AT_name" in at else ""
        size = int(at["DW_AT_byte_size"].value) if "DW_AT_byte_size" in at and \
            isinstance(at["DW_AT_byte_size"].value, int) else 0
        t = CType("base", name or "?", size)
        self._tcache[key] = t                      # before recursing: self-referential structs

        def target() -> CType:
            if "DW_AT_type" not in at:
                return VOID
            try:
                return self.ctype(die.get_DIE_from_attribute("DW_AT_type"))
            except Exception:
                return CType("base", "?", 0)

        if tag == "DW_TAG_base_type":
            t.enc = int(at["DW_AT_encoding"].value) if "DW_AT_encoding" in at else _ATE_UNSIGNED
        elif tag in ("DW_TAG_typedef", "DW_TAG_const_type", "DW_TAG_volatile_type", "DW_TAG_restrict_type",
                     "DW_TAG_atomic_type"):
            t.kind = {"DW_TAG_typedef": "typedef", "DW_TAG_const_type": "const",
                      "DW_TAG_volatile_type": "volatile"}.get(tag, "typedef")
            t.target = target()
            if t.kind == "typedef" and tag != "DW_TAG_typedef":
                t.name = tname(t.target)                   # restrict / atomic: transparent
            t.size = strip(t).size
        elif tag in ("DW_TAG_pointer_type", "DW_TAG_reference_type", "DW_TAG_rvalue_reference_type"):
            t.kind = "ptr"
            t.target = target()
            t.size = size or max(1, self.ptr_size // self.ub)
        elif tag in ("DW_TAG_structure_type", "DW_TAG_union_type", "DW_TAG_class_type"):
            t.kind = "union" if tag == "DW_TAG_union_type" else "struct"
            t.incomplete = "DW_AT_declaration" in at
            for m in die.iter_children():
                if m.tag != "DW_TAG_member":
                    continue
                ma = m.attributes
                off = 0
                if "DW_AT_data_member_location" in ma:
                    v = ma["DW_AT_data_member_location"]
                    if isinstance(v.value, int):
                        off = v.value
                    else:
                        try:
                            ops = self.parse_expr(m.cu, v.value)
                            off = sum(o.args[0] for o in ops if o.op_name == "DW_OP_plus_uconst")
                        except Exception:
                            off = 0
                mt = self.ctype(m.get_DIE_from_attribute("DW_AT_type")) if "DW_AT_type" in ma else VOID
                bsz = int(ma["DW_AT_bit_size"].value) if "DW_AT_bit_size" in ma else 0
                bpos = None
                if bsz:
                    ub8 = 8 * self.ub
                    if "DW_AT_data_bit_offset" in ma:
                        dbo = int(ma["DW_AT_data_bit_offset"].value)
                        off, bpos = dbo // ub8, dbo % ub8
                    elif "DW_AT_bit_offset" in ma:              # DWARF 2/3: from the MSB of the storage unit
                        ssz = int(ma["DW_AT_byte_size"].value) if "DW_AT_byte_size" in ma else (strip(mt).size or 1)
                        bpos = ssz * ub8 - int(ma["DW_AT_bit_offset"].value) - bsz
                    else:
                        bpos = 0
                t.members.append((_s(ma["DW_AT_name"].value) if "DW_AT_name" in ma else "", off, mt, bsz, bpos))
        elif tag == "DW_TAG_array_type":
            t.kind = "array"
            t.target = target()
            for sr in die.iter_children():
                if sr.tag not in ("DW_TAG_subrange_type", "DW_TAG_enumeration_type"):
                    continue
                sa = sr.attributes
                n = None
                if "DW_AT_count" in sa and isinstance(sa["DW_AT_count"].value, int):
                    n = int(sa["DW_AT_count"].value)
                elif "DW_AT_upper_bound" in sa and isinstance(sa["DW_AT_upper_bound"].value, int):
                    lb = int(sa["DW_AT_lower_bound"].value) if "DW_AT_lower_bound" in sa else 0
                    ubv = int(sa["DW_AT_upper_bound"].value)
                    n = ubv - lb + 1 if ubv < (1 << 31) else 0
                t.dims.append(n)
            if not t.dims:
                t.dims = [None]
            es = strip(t.target).size
            if not size and es and all(d is not None for d in t.dims):
                size = es * math.prod(t.dims)
            elif size and es and t.dims[0] is None and len(t.dims) == 1:
                t.dims = [size // es]
            t.size = size
        elif tag == "DW_TAG_enumeration_type":
            t.kind = "enum"
            t.target = target() if "DW_AT_type" in at else None
            t.incomplete = "DW_AT_declaration" in at
            for e in die.iter_children():
                if e.tag == "DW_TAG_enumerator" and "DW_AT_const_value" in e.attributes:
                    t.enums.setdefault(int(e.attributes["DW_AT_const_value"].value), _s(e.attributes["DW_AT_name"].value))
            if not size and t.target is not None:
                t.size = strip(t.target).size
            t.enc = _ATE_SIGNED if any(v < 0 for v in t.enums) or (
                t.target is not None and strip(t.target).enc in (_ATE_SIGNED, _ATE_SCHAR)) else _ATE_UNSIGNED
        elif tag in ("DW_TAG_subroutine_type", "DW_TAG_subprogram"):
            t.kind = "func"
            t.target = target()
            t.name = ""
        elif tag == "DW_TAG_unspecified_type":
            t.kind = "void"
            t.name = name or "void"
        elif "DW_AT_type" in at and not size:          # vendor wrappers (TI 0x4080 ...): transparent
            t.kind = "typedef"
            t.target = target()
            t.name = tname(t.target)
            t.size = strip(t).size
        return t


def _span(s: SubProg) -> int:
    return sum(hi - lo for lo, hi in s.ranges)


# ================================================================================================= register maps
_ARM_REG = {i: f"r{i}" for i in range(13)}
_ARM_REG.update({13: "sp", 14: "lr", 15: "pc"})
_ARM_CALLEE = {"r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11", "sp"}
_ARM_EXC = {"r0", "r1", "r2", "r3", "r12", "lr"}

_C28_REG = {0: "al", 1: "ah", 2: "pl", 3: "ph", 20: "sp", 21: "tl", 22: "t", 23: "st0", 24: "st1", 25: "pc",
            26: "rpc", 28: "xar2", 29: "dp", 36: "ifr", 37: "ier", 39: "stf", 73: "rb"}
for _k in range(8):
    _C28_REG[4 + 2 * _k] = f"ar{_k}"
    _C28_REG[5 + 2 * _k] = f"xar{_k}"
    _C28_REG[41 + 4 * _k] = f"r{_k}h"
    _C28_REG[43 + 4 * _k] = f"r{_k}h"
_C28_PAIR = {0: "acc", 2: "p", 21: "xt"}
_C28_PAIR.update({4 + 2 * _k: f"xar{_k}" for _k in range(8)})
_C28_32 = {f"xar{_k}" for _k in range(8)} | {"pc", "rpc", "stf", "rb"} | {f"r{_k}h" for _k in range(8)}


# ================================================================================================= frame context
class VarCtx:
    """Register / CFA state of one frame of one Debugger."""

    def __init__(self, d: Any, vi: VarIndex | None, level: int, frame: dict | None):
        self.d = d
        self.vi = vi
        self.level = level
        self.ub = d.ub
        self.c28x = d.c28x
        self.pc = frame["pc"] if frame else d.core.pc
        self.lookup_pc = self.pc if level == 0 else max(self.pc - 1, 0)
        self.func = frame.get("func") if frame else None
        self.regs: dict[str, int] | None = None     # outer ARM frames: recovered registers (name -> value)
        self.no_regs_reason: str | None = None
        self._cfa: int | None | bool = False
        self._fb: Any = False

    # -- registers --------------------------------------------------------------------------------------------
    def reg_name(self, n: int) -> str:
        if self.c28x:
            nm = _C28_REG.get(n)
        else:
            nm = _ARM_REG.get(n)
            if nm is None and 64 <= n < 96:
                nm = f"s{n - 64}"
            elif nm is None and 256 <= n < 272:
                nm = f"d{n - 256}"
        if nm is None:
            raise Unavailable(f"DWARF register {n} not supported")
        return nm

    def reg(self, name: str) -> int:
        if self.level == 0:
            if name.startswith("d") and name[1:].isdigit():
                k = int(name[1:])
                return self.reg(f"s{2 * k}") | (self.reg(f"s{2 * k + 1}") << 32)
            try:
                return int(self.d.core.read_reg(name))
            except Exception:
                raise Unavailable(f"register {name} not readable") from None
        if self.regs is None or name not in self.regs:
            raise Unavailable(self.no_regs_reason or f"register {name} not recovered")
        return self.regs[name]

    def reg_bits(self, name: str) -> int:
        if self.c28x:
            return 32 if name in _C28_32 or name in ("acc", "p", "xt") else 16
        if name.startswith("d") and name[1:].isdigit():
            return 64
        return 32

    def cfa(self) -> int:
        if self._cfa is False:
            self._cfa = None
            dw = self.d.dwarf
            if not self.c28x and dw is not None and getattr(dw, "_cfi", None):
                rules = dw.unwind_rules(self.lookup_pc)
                if rules is not None and rules[0] is not None and getattr(rules[0], "reg", None) is not None:
                    try:
                        self._cfa = (self.reg(_ARM_REG.get(rules[0].reg, "sp")) + rules[0].offset) & 0xFFFFFFFF
                    except Unavailable:
                        self._cfa = None
        if self._cfa is None:
            raise Unavailable("no call frame information (CFA) at this PC")
        return self._cfa

    def frame_base(self, sub_die) -> tuple[int, str]:
        """(address, text prefix) of DW_AT_frame_base of the frame's function."""
        if self._fb is False:
            self._fb = None
            a = sub_die.attributes.get("DW_AT_frame_base") if sub_die is not None else None
            if a is None:
                self._fb = Unavailable("no DW_AT_frame_base")
            else:
                try:
                    loc = eval_location(self, sub_die, a, None)
                    if loc.kind == "reg":
                        self._fb = (self.reg(loc.reg), loc.text)
                    elif loc.kind in ("mem", "value"):
                        self._fb = (loc.addr if loc.kind == "mem" else loc.value, loc.text)
                    else:
                        self._fb = Unavailable("unsupported frame base")
                except Unavailable as e:
                    self._fb = e
        if isinstance(self._fb, Unavailable):
            raise self._fb
        return self._fb


def _frame_ctx(d: Any, vi: VarIndex | None, level: int) -> tuple[VarCtx, dict]:
    frames = d.backtrace(level + 1)
    if level < 0 or level >= len(frames):
        raise EvalError(f"no frame {level} (the stack has {len(frames)})")
    fr = frames[level]
    ctx = VarCtx(d, vi, level, fr)
    if level > 0:
        if d.c28x:
            ctx.no_regs_reason = "register not recovered (no C28x unwinder; outer frames show statics only)"
        elif any(f.get("how") not in ("pc", "cfi", "exception", "lr") for f in frames[1:level + 1]):
            ctx.no_regs_reason = "register not recovered (frame found by a stack scan)"
        else:
            rr = _arm_unwind(d, level, frames)
            if rr is None:
                ctx.no_regs_reason = "register not recovered (unwinding failed)"
            else:
                ctx.regs = rr
    return ctx, fr


def _arm_unwind(d: Any, level: int, frames: list[dict]) -> dict[str, int] | None:
    """Registers of frame `level` that the CFI walk recovered (as backtrace() walks): callee-saved registers
    (r4-r11) keep their value unless a callee saved them, r0-r3/r12/lr are known only from an exception frame or a
    CFI save slot."""
    names = [f"r{i}" for i in range(13)] + ["sp", "lr", "pc"]
    try:
        regs = dict(zip(names, d.core.read_regs(names)))
    except Exception:
        return None
    known = set(names)
    cfi_ok = d.dwarf is not None and getattr(d.dwarf, "_cfi", None)
    for lv in range(1, level + 1):
        if lv == 1 and regs["lr"] >= _EXC_RETURN_MIN and not cfi_ok:
            nxt = d._exc_frame(d._guess_handler_sp(regs, regs["lr"]), regs["lr"])
        elif cfi_ok:
            nxt = d._cfi_step(regs, lv == 1)
        else:
            nxt = None
        if nxt is None:
            return None
        new, how = nxt
        k = {n for n in known if n in _ARM_CALLEE}
        if cfi_ok:
            rules = d.dwarf.unwind_rules(regs["pc"] if lv == 1 else max(regs["pc"] - 1, 0))
            for regno, rule in (rules[1].items() if rules else ()):
                nm = _ARM_REG.get(regno)
                if nm is None:
                    continue
                if rule.type == "OFFSET":
                    k.add(nm)
                elif rule.type == "UNDEFINED":
                    k.discard(nm)
        if how == "exception":
            k |= _ARM_EXC
        k |= {"sp", "pc"}
        regs, known = new, k
        if lv < len(frames) and (regs["pc"] & ~1) != frames[lv]["pc"]:
            return None
    return {n: v for n, v in regs.items() if n in known}


# ================================================================================================= locations
@dataclass
class Loc:
    kind: str                 # mem | reg | value | bytes | pieces
    addr: int = 0
    reg: str = ""
    value: int = 0
    data: bytes = b""
    pieces: list = field(default_factory=list)    # [(Loc | None, size_units)]
    text: str = ""


def _loc_text_off(base: str, off: int) -> str:
    if off == 0:
        return f"[{base}]"
    return f"[{base}{'+' if off > 0 else '-'}{abs(off)}]"


def eval_location(ctx: VarCtx, die, attr, sub_die) -> Loc:
    """Location of a DIE attribute (DW_AT_location / DW_AT_frame_base) in frame `ctx`."""
    vi = ctx.vi
    cu = die.cu
    form = attr.form
    if form == "DW_FORM_exprloc" or form.startswith("DW_FORM_block"):
        return _eval_ops(ctx, cu, vi.parse_expr(cu, attr.value), sub_die)
    if form in ("DW_FORM_sec_offset", "DW_FORM_loclistx") or (
            cu.header.version < 4 and form in ("DW_FORM_data4", "DW_FORM_data8")):
        try:
            ents = vi._dw.location_lists().get_location_list_at_offset(int(attr.value), die)
        except Exception as e:
            raise Unavailable(f"location list not readable ({type(e).__name__})") from None
        base = vi.cu_base(cu)
        pc = ctx.lookup_pc
        default = None
        for e in ents:
            if hasattr(e, "base_address"):
                base = int(e.base_address)
                continue
            if not hasattr(e, "loc_expr"):
                continue
            if e.begin_offset == -1:
                default = e
                continue
            lo, hi = (e.begin_offset, e.end_offset) if e.is_absolute else (base + e.begin_offset, base + e.end_offset)
            if lo <= pc < hi:
                if not e.loc_expr:
                    raise Unavailable("optimized out at this PC")
                return _eval_ops(ctx, cu, vi.parse_expr(cu, e.loc_expr), sub_die)
        if default is not None and default.loc_expr:
            return _eval_ops(ctx, cu, vi.parse_expr(cu, default.loc_expr), sub_die)
        raise Unavailable("optimized out at this PC")
    raise Unavailable(f"unsupported location form {form}")


def _eval_ops(ctx: VarCtx, cu, ops: list, sub_die) -> Loc:
    stack: list[int] = []
    pieces: list = []
    texts: list[str] = []
    cur: Loc | None = None
    stack_value = False
    vi = ctx.vi
    mask = 0xFFFFFFFF
    i = 0
    while i < len(ops):
        op = ops[i]
        n = op.op_name
        a = op.args
        i += 1
        if n in ("DW_OP_addr", "DW_OP_addrx", "DW_OP_GNU_addr_index", "DW_OP_constx"):
            v = vi._addr_arg(cu, op) if n != "DW_OP_constx" else int(vi._dw.get_addr(cu, a[0]))
            stack.append(v)
            texts.append(f"0x{v:X}")
        elif n.startswith("DW_OP_lit"):
            stack.append(int(n[9:]))
        elif n in ("DW_OP_const1u", "DW_OP_const2u", "DW_OP_const4u", "DW_OP_const8u", "DW_OP_constu",
                   "DW_OP_const1s", "DW_OP_const2s", "DW_OP_const4s", "DW_OP_const8s", "DW_OP_consts"):
            stack.append(int(a[0]))
        elif n == "DW_OP_fbreg":
            fb, ftxt = ctx.frame_base(sub_die)
            stack.append((fb + a[0]) & mask)
            texts.append(_fb_text(ftxt, a[0]))
        elif n.startswith("DW_OP_breg") or n == "DW_OP_bregx":
            regno = a[0] if n == "DW_OP_bregx" else int(n[10:])
            off = a[1] if n == "DW_OP_bregx" else a[0]
            nm = ctx.reg_name(regno)
            stack.append((ctx.reg(nm) + off) & mask)
            texts.append(_loc_text_off(nm, off))
        elif n.startswith("DW_OP_reg") or n == "DW_OP_regx":
            regno = a[0] if n == "DW_OP_regx" else int(n[9:])
            nm = ctx.reg_name(regno)
            cur = Loc("reg", reg=nm, text=nm)
        elif n == "DW_OP_call_frame_cfa":
            stack.append(ctx.cfa())
            texts.append("cfa")
        elif n in ("DW_OP_piece",):
            if cur is None and stack:
                cur = Loc("value", value=stack.pop()) if stack_value else Loc("mem", addr=stack.pop())
            pieces.append((cur, int(a[0])))
            cur = None
            stack_value = False
        elif n == "DW_OP_stack_value":
            stack_value = True
        elif n == "DW_OP_implicit_value":
            cur = Loc("bytes", data=bytes(a[1]) if len(a) > 1 else bytes(a[0]), text="const")
        elif n == "DW_OP_plus_uconst":
            stack.append((stack.pop() + a[0]) & mask)
        elif n in ("DW_OP_plus", "DW_OP_minus", "DW_OP_and", "DW_OP_or", "DW_OP_xor", "DW_OP_mul", "DW_OP_shl",
                   "DW_OP_shr", "DW_OP_shra", "DW_OP_div", "DW_OP_mod"):
            b2, a2 = stack.pop(), stack.pop()
            r = {"DW_OP_plus": lambda: a2 + b2, "DW_OP_minus": lambda: a2 - b2, "DW_OP_and": lambda: a2 & b2,
                 "DW_OP_or": lambda: a2 | b2, "DW_OP_xor": lambda: a2 ^ b2, "DW_OP_mul": lambda: a2 * b2,
                 "DW_OP_shl": lambda: a2 << b2, "DW_OP_shr": lambda: a2 >> b2, "DW_OP_shra": lambda: a2 >> b2,
                 "DW_OP_div": lambda: a2 // b2 if b2 else 0, "DW_OP_mod": lambda: a2 % b2 if b2 else 0}[n]()
            stack.append(r & mask)
        elif n == "DW_OP_neg":
            stack.append((-stack.pop()) & mask)
        elif n == "DW_OP_not":
            stack.append((~stack.pop()) & mask)
        elif n == "DW_OP_dup":
            stack.append(stack[-1])
        elif n == "DW_OP_drop":
            stack.pop()
        elif n == "DW_OP_swap":
            stack[-1], stack[-2] = stack[-2], stack[-1]
        elif n == "DW_OP_over":
            stack.append(stack[-2])
        elif n in ("DW_OP_deref", "DW_OP_deref_size"):
            sz = a[0] if n == "DW_OP_deref_size" else 4
            ad = stack.pop()
            try:
                data, _ = ctx.d.read_memory(ad, max(1, (sz + ctx.ub - 1) // ctx.ub))
            except Exception:
                raise Unavailable(f"cannot read {ctx.d.fmt_addr(ad)}") from None
            stack.append(int.from_bytes(data[:sz], "little"))
            texts.append(f"*{texts.pop() if texts else ''}")
        elif n in ("DW_OP_entry_value", "DW_OP_GNU_entry_value"):
            raise Unavailable("optimized out (value at function entry, not recoverable)")
        elif n == "DW_OP_nop":
            pass
        else:
            raise Unavailable(f"unsupported location operation {n}")
    if pieces:
        return Loc("pieces", pieces=pieces, text=":".join(p.text if p is not None and p.text else "?"
                                                           for p, _ in pieces))
    if cur is not None:
        return cur
    if not stack:
        raise Unavailable("optimized out")
    txt = texts[-1] if len(texts) == 1 else (" ".join(texts) if texts else "computed")
    if stack_value:
        return Loc("value", value=stack[-1], text="computed")
    return Loc("mem", addr=stack[-1], text=txt)


def _fb_text(ftxt: str, off: int) -> str:
    if ftxt == "cfa" or ftxt.startswith("cfa"):
        return f"cfa{'+' if off >= 0 else '-'}{abs(off)}"
    if ftxt.startswith("[") and ftxt.endswith("]"):          # frame base = breg: [r7+8] + off
        m = re.fullmatch(r"\[(\w+)([+-]\d+)?\]", ftxt)
        if m:
            return _loc_text_off(m.group(1), int(m.group(2) or 0) + off)
    return _loc_text_off(ftxt, off)


# ================================================================================================= value sources
class _Src:
    """Where the bytes of an object come from: memory (read on demand) or a fixed byte image."""

    def __init__(self, d: Any, addr: int | None = None, data: bytes | None = None):
        self.d = d
        self.addr = addr
        self.data = data

    def read(self, off: int, n: int) -> bytes:
        ub = self.d.ub
        if self.data is not None:
            b = self.data[off * ub:(off + n) * ub]
            if len(b) < n * ub:
                raise Unavailable("value shorter than its type")
            return b
        try:
            data, _ = self.d.read_memory(self.addr + off, n)
        except Exception:
            raise Unavailable(f"cannot read {self.d.fmt_addr(self.addr + off)}: unmapped") from None
        if len(data) < n * ub:
            raise Unavailable(f"cannot read {self.d.fmt_addr(self.addr + off)}: unmapped")
        return data

    def sub(self, off: int) -> "_Src":
        if self.data is not None:
            return _Src(self.d, None, self.data[off * self.d.ub:])
        return _Src(self.d, self.addr + off)

    def at(self, off: int) -> int | None:
        return None if self.addr is None else self.addr + off


def _materialize(ctx: VarCtx, loc: Loc, size: int) -> _Src:
    d = ctx.d
    ub = ctx.ub
    nbytes = size * ub
    if loc.kind == "mem":
        return _Src(d, loc.addr)
    if loc.kind == "value":
        return _Src(d, None, (loc.value & ((1 << (8 * max(nbytes, 1))) - 1)).to_bytes(max(nbytes, 1), "little"))
    if loc.kind == "bytes":
        return _Src(d, None, loc.data)
    if loc.kind == "reg":
        return _Src(d, None, _reg_bytes(ctx, loc.reg, nbytes))
    if loc.kind == "pieces":
        out = b""
        for p, psz in loc.pieces:
            if p is None:
                raise Unavailable("partly optimized out")
            src = _materialize(ctx, p, psz)
            out += src.read(0, psz)
        return _Src(d, None, out)
    raise Unavailable("unsupported location")


def _reg_bytes(ctx: VarCtx, name: str, nbytes: int) -> bytes:
    bits = ctx.reg_bits(name)
    if nbytes * 8 > bits:
        if ctx.c28x:
            rev = {v: k for k, v in _C28_REG.items()}
            regno = rev.get(name)
            pair = _C28_PAIR.get(regno) if regno is not None else None
            if pair is not None and nbytes <= 4:
                name, bits = pair, 32
            else:
                raise Unavailable(f"value of {nbytes * 8} bits does not fit register {name}")
        else:
            raise Unavailable(f"value of {nbytes * 8} bits does not fit register {name}")
    v = ctx.reg(name)
    return (v & ((1 << bits) - 1)).to_bytes(bits // 8, "little")[:max(nbytes, 1)]


# ================================================================================================= items
def _unavail(name: str, kind: str, t: CType | None, reason: str, loc: str = "", addr: int | None = None) -> dict:
    return {"name": name, "kind": kind, "type": tname(t) if t is not None else "?", "value": None,
            "text": f"<{reason}>", "addr": addr, "loc": loc, "avail": False, "reason": reason, "children": []}


def _decode_scalar(t: CType, raw: bytes, ub: int) -> int | float | bool:
    c = strip(t)
    nb = len(raw)
    u = int.from_bytes(raw, "little")
    if c.kind == "base" and c.enc == _ATE_FLOAT:
        if nb == 4:
            return struct.unpack("<f", raw)[0]
        if nb == 8:
            return struct.unpack("<d", raw)[0]
        return u
    if c.kind in ("base", "enum") and c.enc in (_ATE_SIGNED, _ATE_SCHAR) and nb:
        if u >> (8 * nb - 1):
            u -= 1 << (8 * nb)
    return u


_INT8_NAMES = re.compile(r"^(u?int8(_t)?|u?int_(least|fast)8_t|[us]8|byte|BYTE|Uint8|int8|uchar8)$")


def _is_char(t: CType | None, ub: int = 1) -> bool:
    """A character type (shown as 'c' / strings): char, signed / unsigned char, but not a typedef that names an
    8-bit integer (uint8_t, u8: shown as numbers)."""
    n = 0
    while t is not None and t.kind in ("typedef", "const", "volatile") and n < 32:
        if t.kind == "typedef" and _INT8_NAMES.match(t.name):
            return False
        t = t.target
        n += 1
    return t is not None and t.kind == "base" and t.size == 1 and (t.enc in (_ATE_SCHAR, _ATE_UCHAR)
                                                                  or "char" in t.name)


def _char_repr(v: int) -> str:
    v &= 0xFF
    esc = {0: "\\0", 7: "\\a", 8: "\\b", 9: "\\t", 10: "\\n", 11: "\\v", 12: "\\f", 13: "\\r", 39: "\\'",
           92: "\\\\"}
    if v in esc:
        return esc[v]
    return chr(v) if 32 <= v < 127 else f"\\x{v:02x}"


def _fmt_int(v: int, nbytes: int) -> str:
    if -10 < v < 10:
        return str(v)
    return f"{v} (0x{v & ((1 << (8 * max(nbytes, 1))) - 1):X})"


class _Fmt:
    """Formatting helpers bound to one Debugger (pointer symbols, strings)."""

    def __init__(self, ctx: VarCtx):
        self.ctx = ctx
        self.d = ctx.d

    def sym(self, a: int) -> str | None:
        d = self.d
        tab = getattr(d, "_dv_symtab", None)
        if tab is None:
            tab = sorted((ad, sz, nm) for nm, (ad, sz) in d.data_syms.items())
            d._dv_symtab = tab
            d._dv_symstarts = [x[0] for x in tab]
        i = bisect.bisect_right(d._dv_symstarts, a) - 1
        for k in range(i, max(-1, i - 8), -1):          # the nearest symbols at or below a
            ad, sz, nm = tab[k]
            if ad == a:
                return nm
            if ad < a < ad + sz:
                return f"{nm}+{a - ad}"
        try:
            if d.is_code(a):
                return d.describe(a)
        except Exception:
            pass
        return None

    def cstring(self, a: int, n: int = 48) -> str | None:
        try:
            data, _ = self.d.read_memory(a, n)
        except Exception:
            return None
        ub = self.d.ub
        chars = [data[i] for i in range(0, len(data), ub)]
        out = []
        for c in chars:
            if c == 0:
                return '"' + "".join(out) + '"'
            out.append(_char_repr(c).replace("\\'", "'").replace('"', '\\"'))
        return '"' + "".join(out) + '"...'


def make_item(ctx: VarCtx, name: str, kind: str, t: CType, src: _Src | None, loc_text: str,
              depth: int = 0, fmt: _Fmt | None = None) -> dict:
    fmt = fmt or _Fmt(ctx)
    c = strip(t)
    addr = src.at(0) if src is not None else None
    item = {"name": name, "kind": kind, "type": tname(t), "value": None, "text": "", "addr": addr,
            "loc": loc_text, "avail": True, "reason": None, "children": []}
    try:
        if c.kind in ("struct", "union"):
            if c.incomplete:
                item["text"] = "<incomplete type>"
                return item
            kids = []
            for mname, off, mt, bsz, bpos in c.members[:MAX_CHILDREN]:
                if depth + 1 > MAX_DEPTH:
                    break
                if bsz:
                    kids.append(_bitfield_item(ctx, mname, mt, src, off, bsz, bpos, fmt))
                else:
                    kids.append(make_item(ctx, mname or "<anon>", "member", mt, src.sub(off),
                                          _sub_loc(loc_text, f".{mname}"), depth + 1, fmt))
            item["children"] = kids
            item["text"] = _preview_struct(kids) if kids else "{...}"
            return item
        if c.kind == "array":
            et = c.target or VOID
            if len(c.dims) > 1:
                et = CType("array", "", strip(c.target).size * math.prod(x or 0 for x in c.dims[1:]),
                           target=c.target, dims=c.dims[1:])
            esz = strip(et).size
            count = c.dims[0] if c.dims and c.dims[0] is not None else 0
            item["count"] = count
            if _is_char(et) and count:
                raw = src.read(0, min(count, 256))
                chars = [raw[i] for i in range(0, len(raw), ctx.ub)]
                s = "".join(_char_repr(x).replace("\\'", "'").replace('"', '\\"') for x in
                            (chars[:chars.index(0)] if 0 in chars else chars))
                item["text"] = f'"{s}"' + ("" if 0 in chars or count <= 256 else "...")
            if depth + 1 <= MAX_DEPTH and esz:
                kids = [make_item(ctx, f"[{k}]", "element", et, src.sub(k * esz), _sub_loc(loc_text, f"[{k}]"),
                                  depth + 1, fmt) for k in range(min(count, MAX_CHILDREN))]
                item["children"] = kids
                if not item["text"]:
                    item["text"] = _preview_array(kids, count)
            elif not item["text"]:
                item["text"] = f"[{count}]"
            return item
        if c.kind in ("void", "func"):
            item["text"] = "<function>" if c.kind == "func" else "<void>"
            if c.kind == "func" and addr is not None:
                item["value"] = addr
                item["text"] = f"{{{tname(t)}}} 0x{addr:X} <{fmt.sym(addr) or '?'}>"
            return item
        size = c.size or (1 if c.kind == "base" else 0)
        if not size:
            raise Unavailable("type has no size")
        raw = src.read(0, size)[:size * ctx.ub]
        v = _decode_scalar(t, raw, ctx.ub)
        item["value"] = v
        item["text"] = _fmt_scalar(t, v, len(raw), fmt)
    except Unavailable as e:
        item.update(avail=False, reason=str(e), text=f"<{e}>", value=None)
    return item


def _bitfield_item(ctx: VarCtx, name: str, mt: CType, src: _Src, off: int, bsz: int, bpos: int, fmt: _Fmt) -> dict:
    nbits = bpos + bsz
    units = max(1, (nbits + 8 * ctx.ub - 1) // (8 * ctx.ub))
    item = {"name": name, "kind": "member", "type": f"{tname(mt)} : {bsz}", "value": None, "text": "",
            "addr": src.at(off), "loc": "", "avail": True, "reason": None, "children": []}
    try:
        raw = src.read(off, units)
        v = (int.from_bytes(raw, "little") >> bpos) & ((1 << bsz) - 1)
        c = strip(mt)
        if c.enc in (_ATE_SIGNED, _ATE_SCHAR) and v >> (bsz - 1):
            v -= 1 << bsz
        item["value"] = v
        item["text"] = _fmt_scalar(mt, v, (bsz + 7) // 8, fmt)
    except Unavailable as e:
        item.update(avail=False, reason=str(e), text=f"<{e}>")
    return item


def _fmt_scalar(t: CType, v: Any, nbytes: int, fmt: _Fmt) -> str:
    """Text of a scalar of declared type `t` (typedefs kept: uint8_t is a number, char a character)."""
    c = strip(t)
    if isinstance(v, float):
        return repr(v) if nbytes == 8 else f"{v:.9g}"
    if c.kind == "enum":
        m = (1 << (8 * max(nbytes, 1))) - 1
        for ev, en in c.enums.items():
            if ev & m == v & m:
                return en
        return f"{v} (no enumerator)"
    if c.kind == "ptr":
        if v == 0:
            return "0x0 (NULL)"
        s = f"0x{v:X}"
        tc = strip(c.target)
        if tc.kind == "func":
            a = v if fmt.d.c28x else v & ~1
            sym = fmt.d.describe(a) if fmt.d.is_code(a) else None
        else:
            sym = fmt.sym(v)
        if sym:
            s += f" <{sym}>"
        if _is_char(c.target):
            st = fmt.cstring(v)
            if st is not None:
                s += f" {st}"
        return s
    if c.kind == "base" and c.enc == _ATE_BOOL:
        return "true" if v else "false"
    if _is_char(t):
        return f"{v} '{_char_repr(v)}'"
    return _fmt_int(int(v), nbytes)


def _preview_struct(kids: list[dict]) -> str:
    parts = []
    n = 0
    for k in kids:
        t = k["text"].split(" (0x")[0] if k["avail"] else "?"
        p = f"{k['name']} = {t}"
        n += len(p)
        if n > 80:
            parts.append("...")
            break
        parts.append(p)
    return "{" + ", ".join(parts) + "}"


def _preview_array(kids: list[dict], count: int) -> str:
    parts = []
    n = 0
    for k in kids:
        t = (k["text"].split(" (0x")[0] if k["avail"] else "?")
        n += len(t) + 2
        if n > 80:
            parts.append("...")
            break
        parts.append(t)
    else:
        if count > len(kids):
            parts.append("...")
    return "{" + ", ".join(parts) + "}"


def _sub_loc(loc: str, suffix: str) -> str:
    return ""  # members / elements: the address is the location


# ================================================================================================= frame variables
def _scope_vars(vi: VarIndex, sub: SubProg, pc: int) -> list[tuple[Any, str, str | None, int]]:
    """[(die, kind, inlined_function, depth)] of the function's scopes that cover pc: args first, then locals,
    innermost scope first."""
    args: list = []
    scopes: list[tuple[int, list]] = []

    def walk(die, depth: int, inl: str | None) -> None:
        here: list = []
        for ch in die.iter_children():
            tag = ch.tag
            if tag == "DW_TAG_formal_parameter":
                if depth == 0 and inl is None:
                    args.append((ch, "arg", None, 0))
                else:
                    here.append((ch, "local", inl, depth))
            elif tag == "DW_TAG_variable":
                here.append((ch, "local", inl, depth))
            elif tag == "DW_TAG_lexical_block":
                if vi.covers(ch, pc):
                    walk(ch, depth + 1, inl)
            elif tag == "DW_TAG_inlined_subroutine":
                if vi.covers(ch, pc):
                    walk(ch, depth + 1, vi.die_name(ch) or "?")
        scopes.append((depth, here))

    walk(sub.die, 0, None)
    out = list(args)
    for _, vs in sorted(scopes, key=lambda x: -x[0]):
        out += vs
    return out


def _var_item(ctx: VarCtx, vi: VarIndex, die, kind: str, sub_die, name: str | None = None, fmt=None) -> dict:
    name = name or vi.die_name(die) or "?"
    t = vi.die_type(die)
    at = die.attributes
    if "DW_AT_location" not in at:
        _, cv = vi.die_attr(die, "DW_AT_const_value")
        if cv is not None:
            if isinstance(cv.value, int):
                size = strip(t).size or 4
                src = _Src(ctx.d, None, (cv.value & ((1 << (8 * size * ctx.ub)) - 1)).to_bytes(size * ctx.ub, "little"))
            else:
                src = _Src(ctx.d, None, bytes(cv.value))
            return make_item(ctx, name, kind, t, src, "const", fmt=fmt)
        return _unavail(name, kind, t, "optimized out")
    try:
        loc = eval_location(ctx, die, at["DW_AT_location"], sub_die)
        src = _materialize(ctx, loc, strip(t).size or 1)
    except Unavailable as e:
        return _unavail(name, kind, t, str(e))
    except (IndexError, KeyError) as e:
        return _unavail(name, kind, t, f"bad location expression ({type(e).__name__})")
    return make_item(ctx, name, kind, t, src, loc.text if loc.kind != "mem" or loc.text else f"0x{loc.addr:X}",
                     fmt=fmt)


def _index(d: Any) -> VarIndex | None:
    dw = getattr(d, "dwarf", None)
    if dw is None or type(dw).__name__ != "DwarfInfo" or getattr(d, "ad", None) is not None:
        return None
    vi = index_for(dw.path)
    return vi if vi is not None and vi.ok() else None


def _no_dwarf_reason(d: Any) -> str:
    if getattr(d, "ad", None) is not None:
        return "no variable information for this architecture"
    return "no DWARF debug info in this image (globals from the symbol table only)"


def frame_vars(d: Any, level: int = 0) -> dict:
    """{"frame", "func", "pc", "has_dwarf", "reason", "vars": [items]} (see the module docstring)."""
    ad = getattr(d, "ad", None)
    if ad is not None and callable(getattr(ad, "frame_vars", None)):
        return ad.frame_vars(level)
    level = int(level)
    vi = _index(d)
    if vi is None:
        try:
            frames = d.backtrace(level + 1)
            fr = frames[level] if level < len(frames) else {"pc": d.core.pc, "func": None}
        except Exception:
            fr = {"pc": d.core.pc, "func": None}
        return {"frame": level, "func": fr.get("func"), "pc": fr.get("pc"), "has_dwarf": False,
                "reason": _no_dwarf_reason(d), "vars": []}
    ctx, fr = _frame_ctx(d, vi, level)
    sub = vi.sub_for(ctx.lookup_pc)
    res = {"frame": level, "func": fr.get("func"), "pc": ctx.pc, "has_dwarf": True, "reason": None, "vars": []}
    if sub is None:
        res["reason"] = "no debug info for the function at this PC"
        return res
    res["func"] = sub.name
    fmt = _Fmt(ctx)
    for die, kind, inl, _depth in _scope_vars(vi, sub, ctx.lookup_pc):
        it = _var_item(ctx, vi, die, kind, sub.die, fmt=fmt)
        if inl:
            it["inlined"] = inl
        if "DW_AT_location" in die.attributes and kind == "local" and vi._static_addr(die) is not None:
            it["kind"] = "static"
        res["vars"].append(it)
    if ctx.no_regs_reason and level > 0:
        res["note"] = ctx.no_regs_reason
    return res


def globals_list(d: Any, q: str | None = None, limit: int = 500) -> list[dict]:
    """[{name, type, addr, size, file, func?}] of global and static variables (no values: cheap)."""
    ad = getattr(d, "ad", None)
    if ad is not None and callable(getattr(ad, "globals_list", None)):
        return ad.globals_list(q, limit)
    ql = (q or "").strip().lower()
    out: list[dict] = []
    vi = _index(d)
    if vi is not None:
        seen = set()
        for g in vi.all_globals():
            nm = f"{g.func}::{g.name}" if g.func else g.name
            if ql and ql not in nm.lower():
                continue
            if (nm, g.addr) in seen:
                continue
            seen.add((nm, g.addr))
            t = vi.die_type(g.die)
            out.append({"name": nm, "type": tname(t), "addr": g.addr, "size": strip(t).size, "file": g.file,
                        **({"func": g.func} if g.func else {})})
    else:
        for nm, (a, sz) in d.data_syms.items():
            if not sz or (ql and ql not in nm.lower()):
                continue
            try:
                if d.is_code(a):
                    continue
            except Exception:
                pass
            out.append({"name": nm, "type": "?", "addr": a, "size": sz, "file": None})
    out.sort(key=lambda x: (x["name"].lower(), x["addr"]))
    return out[:max(0, int(limit))]


# ================================================================================================= expressions
_TOK = re.compile(r"\s*(?:(0[xX][0-9a-fA-F]+|\d+\.\d*(?:[eE][-+]?\d+)?|\d+)|([A-Za-z_$][\w$]*(?:::[A-Za-z_]\w*)*)"
                  r"|(->|<<|>>|<=|>=|==|!=|&&|\|\||[-+*/%&|^~!<>()\[\].,?:]))")


def _tokens(text: str) -> list[tuple[str, str]]:
    out = []
    pos = 0
    t = text.strip()
    while pos < len(t):
        m = _TOK.match(t, pos)
        if not m or m.end() == pos:
            if t[pos:].strip() == "":
                break
            raise EvalError(f"bad character {t[pos]!r} in {text!r}")
        pos = m.end()
        if m.group(1):
            out.append(("num", m.group(1)))
        elif m.group(2):
            out.append(("id", m.group(2)))
        else:
            out.append(("op", m.group(3)))
    return out


class _Unknown(EvalError):
    """A name the typed evaluator does not know (the caller may fall back to Debugger.eval_expr)."""


@dataclass
class _V:
    """An expression value: a typed object (memory / bytes) or a plain number."""
    t: CType
    src: _Src | None = None              # object storage (None: rvalue number)
    num: int | float | None = None
    loc: str = ""
    name: str = ""
    kind: str = "expr"
    item: dict | None = None             # pre-built item (variables, peripheral registers)
    periph: Any = None                   # (block,) or (block, reg)


_PERIPH_T = {1: "uint8_t", 2: "uint16_t", 4: "uint32_t"}


class _Evaluator:
    def __init__(self, d: Any, level: int):
        self.d = d
        self.level = level
        self.vi = _index(d)
        self._ctx: VarCtx | None = None
        self._fr: dict | None = None
        self.ptr_size = (2 if d.c28x else 4)
        self.builtins = _builtin_types(d.ub, self.ptr_size)

    @property
    def ctx(self) -> VarCtx:
        if self._ctx is None:
            if self.vi is not None:
                self._ctx, self._fr = _frame_ctx(self.d, self.vi, self.level)
            else:
                if self.level:
                    self.d.backtrace(self.level + 1)
                self._ctx = VarCtx(self.d, None, self.level, None)
        return self._ctx

    # -- parsing ----------------------------------------------------------------------------------------------
    def run(self, text: str) -> dict:
        self.toks = _tokens(text)
        if not self.toks:
            raise EvalError("empty expression")
        self.i = 0
        v = self.cond()
        if self.i != len(self.toks):
            raise EvalError(f"unexpected {self.toks[self.i][1]!r} in {text!r}")
        return self.to_item(v, text)

    def peek(self, k: int = 0) -> tuple[str, str] | None:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else None

    def take(self, val: str | None = None) -> tuple[str, str]:
        t = self.peek()
        if t is None or (val is not None and t[1] != val):
            raise EvalError(f"expected {val!r}" if val else "unexpected end of expression")
        self.i += 1
        return t

    def isop(self, *vals: str) -> bool:
        t = self.peek()
        return t is not None and t[0] == "op" and t[1] in vals

    _PREC = [("||",), ("&&",), ("|",), ("^",), ("&",), ("==", "!="), ("<", ">", "<=", ">="), ("<<", ">>"),
             ("+", "-"), ("*", "/", "%")]

    def cond(self) -> _V:
        c = self.binary(0)
        if self.isop("?"):
            self.take()
            a = self.cond()
            self.take(":")
            b = self.cond()
            return a if self.num(c) else b
        return c

    def binary(self, lvl: int) -> _V:
        if lvl >= len(self._PREC):
            return self.unary()
        left = self.binary(lvl + 1)
        while self.isop(*self._PREC[lvl]):
            op = self.take()[1]
            right = self.binary(lvl + 1)
            left = self.arith(op, left, right)
        return left

    def unary(self) -> _V:
        if self.isop("*"):
            self.take()
            return self.deref(self.unary())
        if self.isop("&"):
            self.take()
            v = self.unary()
            if v.src is None or v.src.addr is None:
                raise EvalError(f"cannot take the address of {v.name or 'this value'}: not in memory")
            return _V(CType("ptr", size=self.ptr_size, target=v.t), num=v.src.addr, loc="")
        if self.isop("-", "~", "!", "+"):
            op = self.take()[1]
            v = self.unary()
            n = self.num(v)
            r = {"-": lambda: -n, "~": lambda: ~int(n), "!": lambda: int(not n), "+": lambda: n}[op]()
            return self.number(r, v.t if op != "!" else None)
        if self.isop("(") and self.is_type_at(self.i + 1):
            self.take("(")
            t = self.parse_type()
            self.take(")")
            return self.cast(t, self.unary())
        if self.peek() and self.peek()[1] == "sizeof":
            self.take()
            if self.isop("(") and self.is_type_at(self.i + 1):
                self.take("(")
                t = self.parse_type()
                self.take(")")
            else:
                t = self.unary().t
            return self.number(strip(t).size)
        return self.postfix(self.primary())

    def primary(self) -> _V:
        t = self.take()
        if t[0] == "num":
            s = t[1]
            if "." in s or ("e" in s.lower() and not s.lower().startswith("0x")):
                return _V(self.builtins["double"], num=float(s))
            return self.number(int(s, 0))
        if t[0] == "op" and t[1] == "(":
            v = self.cond()
            self.take(")")
            return v
        if t[0] == "id":
            return self.name(t[1])
        raise EvalError(f"unexpected {t[1]!r}")

    def postfix(self, v: _V) -> _V:
        while True:
            if self.isop("."):
                self.take()
                v = self.member(v, self.take()[1])
            elif self.isop("->"):
                self.take()
                v = self.member(self.deref(v), self.take()[1])
            elif self.isop("["):
                self.take()
                idx = self.num(self.cond())
                self.take("]")
                v = self.index(v, int(idx))
            else:
                return v

    # -- types ------------------------------------------------------------------------------------------------
    _TYPE_WORDS = {"unsigned", "signed", "char", "short", "int", "long", "float", "double", "void", "_Bool", "bool",
                   "struct", "union", "enum", "const", "volatile"}

    def is_type_at(self, j: int) -> bool:
        t = self.toks[j] if j < len(self.toks) else None
        if t is None or t[0] != "id":
            return False
        w = t[1]
        if w in self._TYPE_WORDS:
            return True
        if self.local_names() and w in self.local_names():
            return False
        if self.vi is not None and w in self.vi.by_name:
            return False
        return (self.vi is not None and self.vi.type_named(w) is not None) or w in self.builtins

    def parse_type(self) -> CType:
        words = []
        while self.peek() and self.peek()[0] == "id" and (self.peek()[1] in self._TYPE_WORDS or not words or
                                                           words[-1] in ("struct", "union", "enum")):
            w = self.take()[1]
            if w in ("const", "volatile"):
                continue
            words.append(w)
        if not words:
            raise EvalError("expected a type name")
        if words[0] in ("struct", "union", "enum"):
            name = " ".join(words[:2])
            t = self.vi.type_named(name) if self.vi else None
        else:
            name = " ".join(words)
            t = (self.vi.type_named(name) if self.vi else None) or self.builtins.get(name)
            if t is None and name.endswith(" int"):
                t = self.builtins.get(name[:-4])
        if t is None:
            raise EvalError(f"unknown type {name!r}")
        while self.isop("*"):
            self.take()
            t = CType("ptr", size=self.ptr_size, target=t)
        return t

    def cast(self, t: CType, v: _V) -> _V:
        c = strip(t)
        if c.kind in ("struct", "union", "array"):
            if v.src is None:
                raise EvalError(f"cannot cast a number to {tname(t)}")
            return _V(t, src=v.src, name=v.name)
        n = self.num(v)
        if c.kind == "base" and c.enc == _ATE_FLOAT:
            return _V(t, num=float(n))
        n = int(n)
        bits = 8 * c.size * self.d.ub
        n &= (1 << bits) - 1
        if c.enc in (_ATE_SIGNED, _ATE_SCHAR) and n >> (bits - 1):
            n -= 1 << bits
        return _V(t, num=n)

    # -- names ------------------------------------------------------------------------------------------------
    def local_names(self) -> dict:
        if not hasattr(self, "_locals"):
            self._locals = {}
            if self.vi is not None:
                ctx = self.ctx
                sub = self.vi.sub_for(ctx.lookup_pc)
                self._sub = sub
                if sub is not None:
                    for die, kind, _inl, _dep in _scope_vars(self.vi, sub, ctx.lookup_pc):
                        nm = self.vi.die_name(die)
                        if nm and nm not in self._locals:     # innermost first: the first one wins
                            self._locals[nm] = (die, kind)
        return self._locals

    def name(self, nm: str) -> _V:
        d = self.d
        force_reg = nm.startswith("$")
        bare = nm[1:] if force_reg else nm
        if not force_reg:
            loc = self.local_names().get(nm)
            if loc is not None:
                return self.var_value(loc[0], loc[1], self._sub.die, nm)
            if self.vi is not None:
                self.vi.ok()
                gs = self.vi.by_name.get(nm)
                if gs is None and "::" in nm:
                    self.vi.all_globals()
                    gs = self.vi.by_name.get(nm)
                if gs:
                    g = gs[0]
                    return self.var_value(g.die, "static" if g.func else "global", None, nm)
        low = bare.lower()
        if low in d.reg_names() or low in ("ip", "fp", "acc", "p", "xt") or \
                re.fullmatch(r"(r\d+|s\d+|ar\d|xar\d|r\dh)", low):
            try:
                val = self.ctx.reg(low) if self.level else int(d.core.read_reg(low))
            except Unavailable as e:
                raise EvalError(f"{bare}: {e}") from None
            except Exception:
                val = None
            if val is not None:
                bits = self.ctx.reg_bits(low)
                t = self.builtins["uint32_t" if bits > 16 else "uint16_t"]
                return _V(t, num=val, loc=low, name=bare, kind="register")
        if force_reg:
            raise EvalError(f"unknown register {bare!r}")
        s = d.data_syms.get(nm) or (d.data_syms.get("_" + nm) if not nm.startswith("_") else None)
        if s is not None:
            a, sz = s
            if d.is_code(a):
                return _V(CType("func", target=VOID), num=a, name=nm, loc=f"0x{a:X}")
            ub = d.ub
            if sz and sz * (1 if ub == 1 else 1) in (1, 2, 4, 8) and (sz <= 4 or ub == 1):
                t = self.builtins.get({1: "uint8_t", 2: "uint16_t", 4: "uint32_t", 8: "uint64_t"}[sz] if ub == 1
                                      else {1: "uint16_t", 2: "uint32_t", 4: "uint64_t"}.get(sz, "uint16_t"))
                return _V(t, src=_Src(d, a), name=nm, kind="global", loc=f"0x{a:X}")
            if sz:
                et = self.builtins["uint8_t" if ub == 1 else "uint16_t"]
                return _V(CType("array", size=sz, target=et, dims=[sz]), src=_Src(d, a), name=nm, kind="global",
                          loc=f"0x{a:X}")
            return _V(CType("base", "?", 0), num=a, name=nm, loc=f"0x{a:X}")
        blk = d.chip.peripherals.get(nm) or d.chip.peripherals.get(nm.upper())
        if blk is not None:
            return _V(VOID, name=blk.name, periph=(blk,))
        raise _Unknown(f"no symbol {nm!r} in the current context")

    def var_value(self, die, kind: str, sub_die, nm: str) -> _V:
        vi = self.vi
        ctx = self.ctx
        t = vi.die_type(die)
        item = _var_item(ctx, vi, die, kind, sub_die, nm)
        src = None
        if item["avail"]:
            at = die.attributes
            try:
                if "DW_AT_location" in at:
                    loc = eval_location(ctx, die, at["DW_AT_location"], sub_die)
                    src = _materialize(ctx, loc, strip(t).size or 1)
            except Unavailable:
                src = None
        num = item["value"] if src is None else None
        return _V(t, src=src, num=num, loc=item["loc"], name=nm, kind=kind, item=item)

    # -- operations -------------------------------------------------------------------------------------------
    def number(self, n, t: CType | None = None) -> _V:
        if t is None or strip(t).kind not in ("base", "enum", "ptr"):
            t = self.builtins["int"] if isinstance(n, int) and -(1 << 31) <= n < (1 << 31) and not self.d.c28x \
                else self.builtins["long long"] if isinstance(n, int) else self.builtins["double"]
        return _V(t, num=n)

    def num(self, v: _V):
        if v.periph is not None:
            if len(v.periph) == 1:
                raise EvalError(f"{v.name} is a peripheral, not a value (use {v.name}.REG)")
            return self.periph_value(v)[0]
        if v.num is not None:
            return v.num
        if v.item is not None and not v.item["avail"]:
            raise EvalError(f"{v.name}: {v.item['reason']}")
        if v.src is None:
            raise EvalError(f"{v.name or 'value'} has no value")
        c = strip(v.t)
        if c.kind in ("array",):
            if v.src.addr is None:
                raise EvalError(f"{v.name}: not in memory")
            return v.src.addr                                # arrays decay to pointers
        if c.kind in ("struct", "union"):
            raise EvalError(f"{v.name or 'value'} is a {c.kind}, not a number")
        if c.kind == "func":
            return v.src.addr
        size = c.size or 1
        try:
            raw = v.src.read(0, size)[:size * self.d.ub]
        except Unavailable as e:
            raise EvalError(str(e)) from None
        return _decode_scalar(c, raw, self.d.ub)

    def arith(self, op: str, a: _V, b: _V) -> _V:
        x, y = self.num(a), self.num(b)
        ca, cb = strip(a.t), strip(b.t)
        if op in ("+", "-") and ca.kind in ("ptr", "array") and cb.kind not in ("ptr", "array"):
            el = strip(ca.target).size or 1
            return _V(CType("ptr", size=self.ptr_size, target=ca.target), num=int(x) + (int(y) if op == "+"
                                                                                       else -int(y)) * el)
        if op == "-" and ca.kind in ("ptr", "array") and cb.kind in ("ptr", "array"):
            return self.number((int(x) - int(y)) // (strip(ca.target).size or 1))
        if op in ("/", "%") and y == 0:
            raise EvalError("division by zero")
        if op in ("<<", ">>", "&", "|", "^", "%") and (isinstance(x, float) or isinstance(y, float)):
            raise EvalError(f"operator {op} needs integers")
        r = {"+": lambda: x + y, "-": lambda: x - y, "*": lambda: x * y,
             "/": lambda: (x / y if isinstance(x, float) or isinstance(y, float) else int(x / y)),
             "%": lambda: x - y * int(x / y), "<<": lambda: x << y, ">>": lambda: x >> y, "&": lambda: x & y,
             "|": lambda: x | y, "^": lambda: x ^ y, "==": lambda: int(x == y), "!=": lambda: int(x != y),
             "<": lambda: int(x < y), ">": lambda: int(x > y), "<=": lambda: int(x <= y), ">=": lambda: int(x >= y),
             "&&": lambda: int(bool(x) and bool(y)), "||": lambda: int(bool(x) or bool(y))}[op]()
        return self.number(r)

    def deref(self, v: _V) -> _V:
        c = strip(v.t)
        a = int(self.num(v))
        if c.kind in ("ptr", "array"):
            t = c.target or VOID
            if strip(t).kind == "void":
                raise EvalError("cannot dereference a void pointer (cast it first)")
        else:
            t = self.builtins["uint32_t" if self.d.ub == 1 else "uint16_t"]
        return _V(t, src=_Src(self.d, a), name=f"*{v.name}" if v.name else "", loc=f"0x{a:X}")

    def index(self, v: _V, i: int) -> _V:
        c = strip(v.t)
        if v.periph is not None:
            raise EvalError("peripheral registers cannot be indexed")
        if c.kind == "array":
            et = c.target or VOID
            if len(c.dims) > 1:
                et = CType("array", "", strip(c.target).size * math.prod(x or 0 for x in c.dims[1:]),
                           target=c.target, dims=c.dims[1:])
            if v.src is None:
                raise EvalError(f"{v.name}: {v.item['reason'] if v.item else 'not available'}")
            return _V(et, src=v.src.sub(i * (strip(et).size or 1)), name=f"{v.name}[{i}]")
        if c.kind == "ptr":
            t = c.target or VOID
            a = int(self.num(v)) + i * (strip(t).size or 1)
            return _V(t, src=_Src(self.d, a), name=f"{v.name}[{i}]", loc=f"0x{a:X}")
        raise EvalError(f"{v.name or 'value'} is not an array or pointer")

    def member(self, v: _V, m: str) -> _V:
        if v.periph is not None:
            return self.periph_member(v, m)
        c = strip(v.t)
        if c.kind not in ("struct", "union"):
            raise EvalError(f"{v.name or 'value'} is not a struct or union")
        if v.src is None:
            raise EvalError(f"{v.name}: {v.item['reason'] if v.item else 'not available'}")
        for mname, off, mt, bsz, bpos in c.members:
            if mname == m:
                if bsz:
                    item = _bitfield_item(self.ctx, m, mt, v.src, off, bsz, bpos, _Fmt(self.ctx))
                    return _V(mt, num=item["value"], name=f"{v.name}.{m}", item=item)
                return _V(mt, src=v.src.sub(off), name=f"{v.name}.{m}")
        raise EvalError(f"{tname(v.t)} has no member {m!r} (members: "
                        f"{', '.join(x[0] for x in c.members[:20])})")

    # -- peripheral registers ---------------------------------------------------------------------------------
    def periph_member(self, v: _V, m: str) -> _V:
        blk = v.periph[0]
        if len(v.periph) == 1:
            regs = getattr(blk, "regs", {}) or {}
            r = regs.get(m) or regs.get(m.upper())
            if r is None:
                raise EvalError(f"{blk.name} has no register {m!r}")
            return _V(VOID, name=f"{blk.name}.{r.name}", periph=(blk, r))
        if len(v.periph) == 2:
            blk, r = v.periph
            for f in self.reg_fields(blk, r):
                if f.name.upper() == m.upper():
                    return _V(VOID, name=f"{v.name}.{f.name}", periph=(blk, r, f))
            raise EvalError(f"{v.name} has no field {m!r}")
        raise EvalError(f"{v.name} is a bit field")

    def reg_fields(self, blk, r) -> list:
        try:
            from .regtable import regs_for
            chip = self.d.chip
            rf = chip._regs_file(chip.desc)
            table = regs_for(blk.name, rf) if rf else None
            tr = table[r.name] if table is not None and r.name in table else None
            return list(tr.fields) if tr is not None else []
        except Exception:
            return []

    def periph_value(self, v: _V) -> tuple[int, bool]:
        blk, r = v.periph[0], v.periph[1]
        peek = getattr(blk, "peek_reg", None)
        if peek is not None:
            val, ex = peek(r)
        else:
            val, ex = int.from_bytes(bytes(blk.mem[r.boff:r.boff + r.bw]), "little"), False
        if len(v.periph) == 3:
            f = v.periph[2]
            return (val >> f.lsb) & ((1 << f.width) - 1), bool(ex)
        return int(val), bool(ex)

    def periph_item(self, v: _V, name: str) -> dict:
        blk, r = v.periph[0], v.periph[1]
        val, ex = self.periph_value(v)
        addr = blk.base + r.offset
        bits = r.width * getattr(blk, "unit_bits", 8)
        if len(v.periph) == 3:
            f = v.periph[2]
            en = (f.enum or {}).get(val)
            return {"name": name, "kind": "field", "type": f"bits [{f.lsb + f.width - 1}:{f.lsb}]", "value": val,
                    "text": (f"{en} ({val})" if en else _fmt_int(val, (f.width + 7) // 8)), "addr": addr,
                    "loc": v.name, "avail": True, "reason": None, "children": [], "exact": ex}
        kids = []
        for f in self.reg_fields(blk, r):
            fv = (val >> f.lsb) & ((1 << f.width) - 1)
            en = (f.enum or {}).get(fv)
            kids.append({"name": f.name, "kind": "field", "type": f"bits [{f.lsb + f.width - 1}:{f.lsb}]",
                         "value": fv, "text": f"{en} ({fv})" if en else _fmt_int(fv, (f.width + 7) // 8),
                         "addr": addr, "loc": f"{v.name}.{f.name}", "avail": True, "reason": None, "children": []})
        return {"name": name, "kind": "register", "type": f"uint{bits}_t", "value": val,
                "text": f"{val} (0x{val:0{max(1, bits // 4)}X})" + ("" if ex else " (stored value; reading has "
                                                                     "side effects)"),
                "addr": addr, "loc": v.name, "avail": True, "reason": None, "children": kids, "exact": ex}

    # -- result -----------------------------------------------------------------------------------------------
    def to_item(self, v: _V, text: str) -> dict:
        name = text.strip()
        if v.periph is not None:
            if len(v.periph) == 1:
                blk = v.periph[0]
                regs = sorted((getattr(blk, "regs", {}) or {}).values(), key=lambda r: r.offset)
                kids = [self.periph_item(_V(VOID, name=f"{blk.name}.{r.name}", periph=(blk, r)), r.name)
                        for r in regs[:MAX_CHILDREN]]
                for k in kids:
                    k["children"] = []
                return {"name": name, "kind": "peripheral", "type": getattr(blk, "model", type(blk).__name__),
                        "value": None, "text": f"{blk.name} @ 0x{blk.base:X}", "addr": blk.base, "loc": blk.name,
                        "avail": True, "reason": None, "children": kids}
            return self.periph_item(v, name)
        if v.item is not None and v.src is None:
            return dict(v.item, name=name)
        ctx = self.ctx
        if v.src is not None:
            it = make_item(ctx, name, v.kind, v.t, v.src, v.loc or (f"0x{v.src.addr:X}" if v.src.addr is not None
                                                                   else ""))
            if v.item is not None:
                it["loc"] = v.item["loc"]
            return it
        c = strip(v.t)
        n = v.num
        if c.kind == "func":
            return {"name": name, "kind": v.kind, "type": "function", "value": n,
                    "text": f"0x{n:X} <{self.d.describe(n)}>", "addr": n, "loc": v.loc, "avail": True,
                    "reason": None, "children": []}
        if c.kind == "base" and c.name == "?":
            return {"name": name, "kind": v.kind, "type": "?", "value": n,
                    "text": f"0x{n:X} (address; unknown type)", "addr": n, "loc": v.loc, "avail": True,
                    "reason": None, "children": []}
        nb = (c.size or 4) * self.d.ub
        if isinstance(n, int) and c.kind in ("base", "enum", "ptr"):
            if c.enc not in (_ATE_SIGNED, _ATE_SCHAR) and c.kind != "base" or c.enc in (_ATE_UNSIGNED, _ATE_UCHAR):
                n &= (1 << (8 * nb)) - 1
        return {"name": name, "kind": v.kind, "type": tname(v.t), "value": n,
                "text": _fmt_scalar(v.t, n, nb, _Fmt(ctx)), "addr": None, "loc": v.loc, "avail": True,
                "reason": None, "children": []}


def evaluate(d: Any, expr: str, level: int = 0) -> dict:
    """Evaluate a C-like expression in frame `level` (see the module docstring for the item shape). Names: the
    frame's locals and arguments, then globals / statics (DWARF, `func::name` for function statics), registers
    (`$r0` forces a register), ELF symbols, peripherals (`USART1.SR`, `USART1.SR.RXNE`). Operators: `.` `->` `[]`
    unary `*` `&` `-` `~` `!`, casts `(uint16_t *)0x...`, `sizeof`, arithmetic and comparisons. Anything the typed
    evaluator does not parse falls back to Debugger.eval_expr (mem32[...], addr(sym), and/or)."""
    ad = getattr(d, "ad", None)
    if ad is not None and callable(getattr(ad, "evaluate", None)):
        return ad.evaluate(expr, level)
    text = str(expr or "").strip()
    if not text:
        raise EvalError("empty expression")
    ev = _Evaluator(d, int(level))
    try:
        return ev.run(text)
    except Unavailable as e:
        raise EvalError(str(e)) from None
    except (EvalError, IndexError, KeyError, TypeError, AttributeError, ZeroDivisionError, OverflowError) as e:
        first = e if isinstance(e, EvalError) else EvalError(f"bad expression {text!r} ({type(e).__name__})")
    try:
        n = d.eval_expr(text)
    except Exception:
        raise first from None
    return {"name": text, "kind": "expr", "type": "int", "value": n, "text": _fmt_int(n, 4), "addr": None,
            "loc": "", "avail": True, "reason": None, "children": []}
