"""C28x instruction table: loads `chipem/arch/c28x/isa.yaml`, describes operand kinds (how a field's bits map to an
operand value, how the value prints and parses) and decodes instruction words.

Used by the interpreter (core.py: decode → semantics factory), the assembler and the disassembler (asm.py).
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

ISA_PATH = Path(__file__).resolve().parents[2] / "arch" / "c28x" / "isa.yaml"

COND_NAMES = ("NEQ", "EQ", "GT", "GEQ", "LT", "LEQ", "HI", "HIS", "LO", "LOS", "NOV", "OV", "NTC", "TC", "NBIO", "UNC")
COND_ALIASES = {"C": 7, "NC": 8, "GE": 3, "LE": 5, "NE": 0}
CNDF_NAMES = {0: "NEQ", 1: "EQ", 2: "GT", 3: "GEQ", 4: "LT", 5: "LEQ", 10: "TF", 11: "NTF", 12: "LU", 13: "LV",
              14: "UNC", 15: "UNCF"}
CNDF_VALUES = {v: k for k, v in CNDF_NAMES.items()}
CNDF_VALUES.update({"NE": 0, "GE": 3, "LE": 5})
MODE_BITS = {"SXM": 0x01, "OVM": 0x02, "TC": 0x04, "C": 0x08, "INTM": 0x10, "DBGM": 0x20, "PAGE0": 0x40, "VMAP": 0x80}
FFLAG_BITS = {"LVF": 0x01, "LUF": 0x02, "NF": 0x04, "ZF": 0x08, "NI": 0x10, "ZI": 0x20, "TF": 0x40}   # = STF[6:0]
SETFLG_BITS = dict(FFLAG_BITS, RNDF32=0x200, RND32=0x200)        # 11-bit FLAG/VALUE fields = STF[10:0] (SPRUHS1C)
# MOVST0 FLAG field: not STF order.  From TI's assembler (cl2000 25.11.1: MOVST0 TF = 0xAD80, ZI = 0xAD20,
# ZF = 0xAD10, NI = 0xAD08, NF = 0xAD04, LUF = 0xAD02, LVF = 0xAD01) and dis2000; SPRUHS1C gives no layout.
MOVST0_BITS = {"LVF": 0x01, "LUF": 0x02, "NF": 0x04, "NI": 0x08, "ZF": 0x10, "ZI": 0x20, "TF": 0x80}
# INTR field = vector number - 1 (INT1 = 0 ... INT14 = 13, DLOGINT = 14, RTOSINT = 15): cl2000 25.11.1's assembler.
# (SPRU430F's INTR table lists x -> vector, not the field.)
INTX_NAMES = {14: "DLOGINT", 15: "RTOSINT", **{i - 1: f"INT{i}" for i in range(1, 15)}}
INTX_VALUES = {v: k for k, v in INTX_NAMES.items()}

# SPRU430F Table 5-1 (AMODE = 0)
LOC16_REGS = {0xA8: "AH", 0xA9: "AL", 0xAA: "PH", 0xAB: "PL", 0xAC: "T", 0xAD: "SP"}   # 0xAC: SPRU430F table 5-1 says @TH, examples and dis2000 @T
LOC32_REGS = {0xA9: "ACC", 0xAB: "P", 0xAC: "XT"}
LOC_SPECIAL = {0xBD: "*SP++", 0xBE: "*--SP", 0xBF: "*AR6%++", 0xAE: "*BR0++", 0xAF: "*BR0--", 0xB8: "*",
               0xB9: "*++", 0xBA: "*--", 0xBB: "*0++", 0xBC: "*0--",
               **{0xB0 + n: f"*,ARP{n}" for n in range(8)}}
LOC_SPECIAL_REV = {v: k for k, v in LOC_SPECIAL.items()}


class AsmError(ValueError):
    pass


# ---------------------------------------------------------------------------------------------- loc16 / loc32
def loc_valid(code: int, w32: bool) -> bool:
    if code < 0xA0 or code >= 0xC0:
        return True
    if code < 0xA8:
        return True                                   # @ARn / @XARn
    if code < 0xB0:
        return code in (LOC32_REGS if w32 else LOC16_REGS) or code in LOC_SPECIAL
    return code in LOC_SPECIAL


def loc_fmt(code: int, w32: bool = False) -> str:
    """Canonical text of an addressing byte (C28x mode, AMODE=0)."""
    hi = code >> 6
    if hi == 0:
        return f"@0x{code:X}" if code >= 10 else f"@{code}"
    if hi == 1:
        return f"*-SP[{code & 0x3F}]"
    if hi == 3:
        n, k = code & 7, (code >> 3) & 7
        return f"*+XAR{n}[{k}]"
    sub, n = (code >> 3) & 7, code & 7
    if sub == 0:
        return f"*XAR{n}++"
    if sub == 1:
        return f"*--XAR{n}"
    if sub == 2:
        return f"*+XAR{n}[AR0]"
    if sub == 3:
        return f"*+XAR{n}[AR1]"
    if sub == 4:
        return f"@XAR{n}" if w32 else f"@AR{n}"
    if sub == 5:
        r = (LOC32_REGS if w32 else LOC16_REGS).get(code)
        if r:
            return f"@{r}"
        s = LOC_SPECIAL.get(code)
        return s if s else f"<loc 0x{code:02X}>"
    s = LOC_SPECIAL.get(code)
    return s if s else f"<loc 0x{code:02X}>"


_RE_XAR_POST = re.compile(r"^\*XAR([0-7])\+\+$")
_RE_XAR_PRE = re.compile(r"^\*--XAR([0-7])$")
_RE_XAR_IDX = re.compile(r"^\*\+XAR([0-7])\[(.+)\]$")
_RE_XAR = re.compile(r"^\*XAR([0-7])$")
_RE_SP = re.compile(r"^\*-SP\[(.+)\]$")


def loc_parse(text: str, w32: bool, ev: Callable[[str], int | None]) -> int | None:
    """Addressing byte for operand text, or None if the text is not a loc operand.  `ev` evaluates expressions
    (returns None for a not-yet-defined symbol: the field is then 0 in this pass)."""
    t = text.replace(" ", "")
    u = t.upper()
    if u in LOC_SPECIAL_REV:
        return LOC_SPECIAL_REV[u]
    m = _RE_XAR_POST.match(u)
    if m:
        return 0x80 | int(m.group(1))
    m = _RE_XAR_PRE.match(u)
    if m:
        return 0x88 | int(m.group(1))
    m = _RE_XAR.match(u)
    if m:
        return 0xC0 | int(m.group(1))
    m = _RE_XAR_IDX.match(u)
    if m:
        n, idx = int(m.group(1)), m.group(2)
        if idx == "AR0":
            return 0x90 | n
        if idx == "AR1":
            return 0x98 | n
        k = ev(t[t.index("[") + 1:-1])
        k = 0 if k is None else k
        if not 0 <= k <= 7:
            raise AsmError(f"*+XARn[k] offset must be 0..7, got {k}")
        return 0xC0 | (k << 3) | n
    m = _RE_SP.match(u)
    if m:
        k = ev(t[t.index("[") + 1:-1])
        k = 0 if k is None else k
        if not 0 <= k <= 63:
            raise AsmError(f"*-SP[k] offset must be 0..63, got {k}")
        return 0x40 | k
    if u.startswith("@"):
        r = u[1:]
        regs = LOC32_REGS if w32 else LOC16_REGS
        for c, name in regs.items():
            if r == name:
                return c
        if not w32 and r == "TH":
            return 0xAC                               # @TH (SPRU430F table 5-1 spelling) accepted for @T
        m = re.match(r"^(X?)AR([0-7])$", r)
        if m:
            if bool(m.group(1)) != w32:
                return None
            return 0xA0 | int(m.group(2))
        if r in ("AL", "AH", "PH", "PL", "T", "TH", "SP", "ACC", "P", "XT"):
            return None                               # register not addressable at this width
        v = ev(t[1:])
        return (v or 0) & 0x3F
    return None


# ---------------------------------------------------------------------------------------------- operand kinds
def _sext(v: int, bits: int) -> int:
    v &= (1 << bits) - 1
    return v - (1 << bits) if v >> (bits - 1) else v


def f32_from_bits(b: int) -> float:
    return struct.unpack("<f", struct.pack("<I", b & 0xFFFFFFFF))[0]


def f32_bits(x: float) -> int:
    try:
        return struct.unpack("<I", struct.pack("<f", x))[0]
    except OverflowError:
        return 0x7F800000 if x > 0 else 0xFF800000


@dataclass
class Kind:
    name: str
    letter: str
    role: str
    to_field: Callable[[Any, int, int], int]         # (value, pc, width) -> field
    from_field: Callable[[int, int, int], Any]       # (field, pc, width) -> value
    fmt: Callable[[Any], str]
    parse: Callable[[str, Callable], Any]            # (text, ev) -> value (None: not this kind)
    rest: bool = False                               # consumes all remaining operands


def _imm_parse(text: str, ev) -> Any:
    t = text.strip()
    if not t.startswith("#"):
        return None
    v = ev(t[1:])
    return 0 if v is None else v


def _range_check(lo: int, hi: int, name: str):
    def to_field(v, pc, w):
        if not lo <= v <= hi:
            raise AsmError(f"{name}: value {v} out of range {lo}..{hi}")
        return v & ((1 << w) - 1)
    return to_field


def _num(v: int) -> str:
    return str(v) if -10 < v < 10 else (f"0x{v:X}" if v >= 0 else f"-0x{-v:X}")


def _mk_unsigned(bits: int, slack_neg: bool) -> Kind:
    lo = -(1 << (bits - 1)) if slack_neg else 0
    return Kind(f"#u{bits}", "C", "imm", _range_check(lo, (1 << bits) - 1, f"#u{bits}"),
                lambda f, pc, w: f, lambda v: "#" + _num(v), _imm_parse)


def _mk_signed(bits: int) -> Kind:
    return Kind(f"#s{bits}", "C", "imm", _range_check(-(1 << (bits - 1)), (1 << (bits - 1)) - 1 if bits < 16 else 0xFFFF,
                                                       f"#s{bits}"),
                lambda f, pc, w: _sext(f, w), lambda v: "#" + _num(v), _imm_parse)


def _mk_shift(lo: int, hi: int, bias: int) -> Kind:
    def to_field(v, pc, w):
        if not lo <= v <= hi:
            raise AsmError(f"shift {v} out of range {lo}..{hi}")
        return (v - bias) & ((1 << w) - 1)
    return Kind(f"#sh{lo}_{hi}", "H", "sh", to_field, lambda f, pc, w: f + bias, lambda v: f"#{v}", _imm_parse)


def _name_kind(name: str, letter: str, role: str, names: dict[int, str], values: dict[str, int]) -> Kind:
    def parse(text, ev):
        return values.get(text.strip().upper())

    def to_field(v, pc, w):
        return v
    return Kind(name, letter, role, to_field, lambda f, pc, w: f, lambda v: names.get(v, f"?{v}"), parse)


def _reg_kind(name: str, letter: str, role: str, fmt: str, rx: str) -> Kind:
    r = re.compile(rx)

    def parse(text, ev):
        m = r.match(text.strip().upper().replace(" ", ""))
        return int(m.group(1)) if m else None
    return Kind(name, letter, role, lambda v, pc, w: v, lambda f, pc, w: f, lambda v: fmt % v, parse)


def _target_kind(name: str, bits: int) -> Kind:
    def to_field(v, pc, w):
        off = v - pc
        if not -(1 << (bits - 1)) <= off < (1 << (bits - 1)):
            raise AsmError(f"branch target 0x{v:X} out of {bits}-bit range from 0x{pc:X}")
        return off & ((1 << w) - 1)

    def parse(text, ev):
        t = text.strip()
        if t.startswith("#"):
            t = t[1:]
        if not t or t[0] in "*@":
            return None
        v = ev(t)
        return pc_placeholder if v is None else v
    return Kind(name, "O", "target", to_field, lambda f, pc, w: (pc + _sext(f, w)) & 0x3FFFFF,
                lambda v: f"0x{v:X}", parse)


def _rblock_kind() -> Kind:
    """RPTB block end: the label after the last block instruction.  Field = RSIZE = end - (pc + 2), 7 bits; a block
    must hold >= 9 words when it starts at an even address and >= 8 at an odd one (SPRUHS1C RPTB restrictions)."""
    def to_field(v, pc, w):
        size = v - (pc + 2)
        lo = 9 if (pc + 2) % 2 == 0 else 8
        if not lo <= size <= 127:
            raise AsmError(f"RPTB block 0x{pc + 2:X}..0x{v:X} is {size} words; it must be {lo}..127 "
                           f"({'even' if lo == 9 else 'odd'}-aligned start)")
        return size

    def parse(text, ev):
        t = text.strip()
        if t.startswith("#"):
            t = t[1:]
        if not t or t[0] in "*@":
            return None
        v = ev(t)
        return pc_placeholder if v is None else v
    return Kind("rblock", "S", "end", to_field, lambda f, pc, w: (pc + 2 + f) & 0x3FFFFF,
                lambda v: f"0x{v:X}", parse)


class _PcPlaceholder(int):
    """Value of an unresolved branch target in pass 1 (encodes as offset 0)."""


pc_placeholder = _PcPlaceholder(-1)


def _addr_kind(name: str, hash_: bool) -> Kind:
    def parse(text, ev):
        t = text.strip()
        if t.startswith("#"):
            t = t[1:]
        elif hash_:
            pass                                      # accept LCR label without '#'
        if not t or t[0] in "*@":
            return None
        v = ev(t)
        return 0 if v is None else v
    return Kind(name, "X", "addr", _range_check(0, 0x3FFFFF, name), lambda f, pc, w: f,
                lambda v: (f"#0x{v:X}" if hash_ else f"0x{v:X}"), parse)


def _pma_kind(name: str, style: str) -> Kind:
    """Program/IO 16-bit addresses: style 'pma' → `expr` (TI's syntax for XB/XCALL/XBANZ; `0:expr` is accepted too),
    'star' → `*(expr)`, 'abs' → `*(0:expr)`."""
    def parse(text, ev):
        t = text.strip().replace(" ", "")
        if style == "pma":
            if t[:1] in "*@#":
                return None
            if t.startswith("0:"):
                t = t[2:]
        else:
            if not (t.startswith("*(") and t.endswith(")")):
                return None
            t = t[2:-1]
            if style == "abs":
                if not t.startswith("0:"):
                    return None
                t = t[2:]
            elif t.startswith("0:"):
                t = t[2:]
        v = ev(t)
        return 0 if v is None else v

    def fmt(v):
        if style == "pma":
            return f"0x{v:04X}"
        if style == "abs":
            return f"*(0:0x{v:04X})"
        return f"*(0x{v:04X})"
    return Kind(name, "C", "imm", _range_check(0, 0xFFFF, name), lambda f, pc, w: f, fmt, parse)


def _pm_kind() -> Kind:
    def parse(text, ev):
        t = text.strip().replace("#", "")
        try:
            v = int(t, 0)
        except ValueError:
            return None
        return v if -6 <= v <= 1 else None
    return Kind("pm", "P", "pm", lambda v, pc, w: 1 - v, lambda f, pc, w: 1 - f,
                lambda v: "+1" if v == 1 else str(v), parse)


def _flags_kind(name: str, letter: str, bits: dict[str, int]) -> Kind:
    names = {}
    for k, v in bits.items():
        names.setdefault(v, k)

    def parse(text, ev):
        m = 0
        for part in text.split(","):
            p = part.strip().upper()
            if not p:
                continue
            if p not in bits:
                return None
            m |= bits[p]
        return m

    def fmt(v):
        return ", ".join(names[b] for b in sorted(names) if v & b) or "0"
    return Kind(name, letter, name, lambda v, pc, w: v, lambda f, pc, w: f, fmt, parse, rest=True)


def _setflg_kind() -> Kind:
    names = {}
    for k, v in SETFLG_BITS.items():
        names.setdefault(v, k)

    def parse(text, ev):
        mask = val = 0
        for part in text.split(","):
            p = part.strip().upper().replace(" ", "")
            if not p:
                continue
            if "=" not in p:
                return None
            k, v = p.split("=", 1)
            if k not in SETFLG_BITS or v not in ("0", "1"):
                return None
            mask |= SETFLG_BITS[k]
            if v == "1":
                val |= SETFLG_BITS[k]
        return (mask, val)

    def fmt(mv):
        mask, val = mv
        return ", ".join(f"{names[b]}={1 if val & b else 0}" for b in sorted(names) if mask & b)
    return Kind("flagset", "F", "flagset", None, None, fmt, parse, rest=True)       # special-cased (F and V)


def _f16_kind() -> Kind:
    def parse(text, ev):
        t = text.strip()
        if not t.startswith("#"):
            return None
        t = t[1:].strip()
        if re.fullmatch(r"[-+]?(\d+\.\d*|\.\d+|\d+(\.\d*)?[eE][-+]?\d+)", t):
            b = f32_bits(float(t))
            return b >> 16
        v = ev(t)
        return 0 if v is None else v & 0xFFFF

    def fmt(v):
        x = f32_from_bits(v << 16)
        if x != x or x in (float("inf"), float("-inf")):
            return f"#0x{v:04X}"
        s = repr(x)
        return "#" + (s if ("." in s or "e" in s) else s + ".0")
    return Kind("#f16", "C", "imm", _range_check(0, 0xFFFF, "#f16"), lambda f, pc, w: f, fmt, parse)


def _loc_kind(name: str, w32: bool) -> Kind:
    def to_field(v, pc, w):
        return v

    def parse(text, ev):
        return loc_parse(text, w32, ev)
    return Kind(name, "L", "loc", to_field, lambda f, pc, w: f, lambda v: loc_fmt(v, w32), parse)


REG32_LOCS = frozenset((0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7, 0xA9, 0xAB, 0xAC))   # @XARn @ACC @P @XT


def _reg32_kind() -> Kind:
    """loc32 restricted to the CPU registers @XARn/@ACC/@P/@XT (written with or without '@'): the operand of
    `MOV32 RaH,{ACC|P|XT|XARn}` / `MOV32 {ACC|P|XT|XARn},RaH` (TI encodes those as MOV32 *(0:16bit) forms)."""
    def parse(text, ev):
        t = text.strip()
        if not t.startswith("@"):
            t = "@" + t
        code = loc_parse(t, True, ev)
        return code if code in REG32_LOCS else None
    return Kind("reg32", "L", "loc", lambda v, pc, w: v, lambda f, pc, w: f, lambda v: loc_fmt(v, True), parse)


KINDS: dict[str, Kind] = {}
for _k in (_loc_kind("loc16", False), _loc_kind("loc32", True), _reg32_kind(),
           Kind("AX", "A", "ax", lambda v, pc, w: v, lambda f, pc, w: f, lambda v: "AH" if v else "AL",
                lambda t, ev: {"AL": 0, "AH": 1}.get(t.strip().upper())),
           Kind("AX.LSB", "A", "ax", lambda v, pc, w: v, lambda f, pc, w: f, lambda v: "AH.LSB" if v else "AL.LSB",
                lambda t, ev: {"AL.LSB": 0, "AH.LSB": 1}.get(t.strip().upper().replace(" ", ""))),
           Kind("AX.MSB", "A", "ax", lambda v, pc, w: v, lambda f, pc, w: f, lambda v: "AH.MSB" if v else "AL.MSB",
                lambda t, ev: {"AL.MSB": 0, "AH.MSB": 1}.get(t.strip().upper().replace(" ", ""))),
           _reg_kind("ARn", "n", "n", "AR%d", r"^AR([0-7])$"),
           _reg_kind("ARn--", "n", "n", "AR%d--", r"^AR([0-7])--$"),
           _reg_kind("XARn", "n", "n", "XAR%d", r"^XAR([0-7])$"),
           _reg_kind("XARn++", "n", "n", "XAR%d++", r"^XAR([0-7])\+\+$"),
           _reg_kind("XARn--", "n", "n", "XAR%d--", r"^XAR([0-7])--$"),
           _reg_kind("*XARn", "n", "n", "*XAR%d", r"^\*XAR([0-7])$"),
           _reg_kind("ARm", "m", "m", "AR%d", r"^AR([0-7])$"),
           _reg_kind("ARPn", "n", "n", "ARP%d", r"^ARP([0-7])$"),
           _mk_unsigned(5, False), _mk_unsigned(7, False), _mk_unsigned(8, False), _mk_unsigned(10, False),
           _mk_unsigned(16, True), _mk_signed(8), _mk_signed(16),
           Kind("#bit", "B", "bit", _range_check(0, 15, "#bit"), lambda f, pc, w: f, lambda v: f"#{v}", _imm_parse),
           _mk_shift(0, 15, 0), _mk_shift(1, 15, 0), _mk_shift(1, 16, 1), _mk_shift(1, 8, 1),
           _name_kind("COND", "K", "cond", dict(enumerate(COND_NAMES)),
                      {**{n: i for i, n in enumerate(COND_NAMES)}, **COND_ALIASES}),
           _name_kind("CNDF", "K", "cond", CNDF_NAMES, CNDF_VALUES),
           _name_kind("intx", "I", "int", INTX_NAMES, INTX_VALUES),
           _target_kind("off8", 8), _target_kind("off16", 16),
           _addr_kind("addr22", False), _addr_kind("#addr22", True), _rblock_kind(),
           _pma_kind("pma", "pma"), _pma_kind("*pma", "star"), _pma_kind("pa", "star"), _pma_kind("abs16", "abs"),
           _pm_kind(), _flags_kind("mode", "M", MODE_BITS), _flags_kind("fflags", "F", MOVST0_BITS), _setflg_kind(),
           _f16_kind()):
    KINDS[_k.name] = _k
for _i, _r in enumerate("abcdef"):
    KINDS[f"R{_r}H"] = _reg_kind(f"R{_r}H", _r, "r" + _r, "R%dH", r"^R([0-7])H$")


# ---------------------------------------------------------------------------------------------- table entries
@dataclass
class Operand:
    """One pattern operand: value part (kind or literal) and optional shift part (kind or literal)."""
    value: str
    shift: str | None = None
    value_kind: Kind | None = None
    shift_kind: Kind | None = None


@dataclass
class Entry:
    idx: int
    mn: str
    ops_text: str
    enc: str
    sem: str
    args: dict
    conf: str
    norpt: bool
    nwords: int
    mask: int                  # over the full encoding (16 or 32 bits)
    value: int
    fields: dict[str, list[int]]          # letter -> bit positions (in the full encoding), MSB first
    operands: list[Operand] = field(default_factory=list)
    nfixed: int = 0
    alias: bool = False        # assembler-only spelling of another entry's encoding (never decoded)
    par: int = 0               # parallel instruction "A || B": number of operands that belong to A
    fpu: bool = False          # FPU32 instruction (illegal on a core without the FPU)
    reg32: bool = False        # the loc32 operand is restricted to @XARn/@ACC/@P/@XT (kind reg32)

    @property
    def mask1(self) -> int:
        return self.mask >> 16 if self.nwords == 2 else self.mask

    @property
    def value1(self) -> int:
        return self.value >> 16 if self.nwords == 2 else self.value

    def syntax(self) -> str:
        return f"{self.mn} {self.ops_text}".strip()


def _split_top(text: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip() or out:
        out.append(cur)
    return [o.strip() for o in out]


def _parse_operand_pattern(text: str) -> Operand:
    if "<<" in text:
        v, s = text.split("<<", 1)
        op = Operand(v.strip(), s.strip())
    else:
        op = Operand(text.strip())
    op.value_kind = KINDS.get(op.value)
    if op.shift is not None:
        sk = op.shift
        op.shift_kind = KINDS.get(sk)
    return op


class ISA:
    def __init__(self, path: Path = ISA_PATH):
        data = yaml.safe_load(Path(path).read_text())
        self.entries: list[Entry] = []
        for i, d in enumerate(data["instructions"]):
            enc = d["enc"].replace(" ", "")
            if len(enc) not in (16, 32):
                raise ValueError(f"isa.yaml entry {i} {d['mn']}: encoding must be 16 or 32 bits")
            mask = value = 0
            fields: dict[str, list[int]] = {}
            nb = len(enc)
            for j, ch in enumerate(enc):
                bit = nb - 1 - j
                if ch in "01":
                    mask |= 1 << bit
                    value |= int(ch) << bit
                else:
                    fields.setdefault(ch, []).append(bit)
            e = Entry(i, d["mn"].upper(), d.get("ops", "") or "", enc, d["sem"], dict(d.get("a") or {}),
                      d.get("conf", "low"), bool(d.get("norpt", False)), nb // 16, mask, value, fields)
            e.nfixed = bin(mask).count("1")
            e.alias = bool(d.get("alias", False))
            e.par = int(d.get("par", 0))
            e.fpu = e.sem.startswith("f_") or e.sem == "movabs32"
            e.reg32 = "reg32" in e.ops_text
            e.operands = [_parse_operand_pattern(o) for o in _split_top(e.ops_text)] if e.ops_text else []
            for op in e.operands:
                for k in (op.value_kind, op.shift_kind):
                    if k is not None and k.letter not in fields and k.name != "flagset":
                        raise ValueError(f"isa.yaml {e.syntax()}: field letter {k.letter} for {k.name} missing in enc")
            self.entries.append(e)
        # decode buckets by the first word's high byte; most specific first
        self.buckets: list[list[Entry]] = [[] for _ in range(256)]
        for e in self.entries:
            if e.alias:
                continue
            m1, v1 = e.mask1, e.value1
            for hb in range(256):
                if (hb << 8) & m1 == v1 & m1 & 0xFF00:
                    self.buckets[hb].append(e)
        for b in self.buckets:
            b.sort(key=lambda e: (-e.nfixed, e.idx))
        self.by_mn: dict[str, list[Entry]] = {}
        for e in self.entries:
            self.by_mn.setdefault(e.mn, []).append(e)

    # ------------------------------------------------------------------ decode
    def match(self, w1: int, fetch2: Callable[[], int]) -> tuple[Entry, int] | None:
        """Entry matching the first word w1 (and second word, fetched lazily). Returns (entry, full_word)."""
        w2 = None
        for e in self.buckets[w1 >> 8]:
            if e.nwords == 1:
                if w1 & e.mask == e.value:
                    if "L" in e.fields and not loc_valid(self._field(e, "L", w1), "loc32" in e.ops_text):
                        continue
                    return e, w1
            else:
                if (w1 & e.mask1) != e.value1:
                    continue
                if w2 is None:
                    w2 = fetch2()
                    if w2 is None:
                        return None
                full = (w1 << 16) | w2
                if full & e.mask == e.value:
                    if "L" in e.fields:
                        code = self._field(e, "L", full)
                        if e.reg32 and code not in REG32_LOCS:
                            continue
                        if not loc_valid(code, "loc32" in e.ops_text or e.reg32):
                            continue
                    return e, full
        return None

    @staticmethod
    def _field(e: Entry, letter: str, word: int) -> int:
        v = 0
        for bit in e.fields[letter]:
            v = (v << 1) | ((word >> bit) & 1)
        return v

    def operands(self, e: Entry, word: int, pc: int) -> dict[str, Any]:
        """Decoded operand values keyed by role (loc, ax, n, m, imm, bit, sh, cond, target, addr, pm, mode, int,
        ra..rf, fflags, flagset).  Fixed args from the table (`a:`) are merged in (table args win only when the
        role was not decoded)."""
        out: dict[str, Any] = {}
        for op in e.operands:
            for k in (op.value_kind, op.shift_kind):
                if k is None:
                    continue
                if k.name == "flagset":
                    out["flagset"] = (self._field(e, "F", word), self._field(e, "V", word))
                    continue
                f = self._field(e, k.letter, word)
                out[k.role] = k.from_field(f, pc, len(e.fields[k.letter]))
        for k, v in e.args.items():
            out.setdefault(k, v)
        return out

    def encode_fields(self, e: Entry, vals: dict[str, int]) -> int:
        """Full encoding word from field-letter values."""
        word = e.value
        for letter, bits in e.fields.items():
            v = vals.get(letter, 0)
            n = len(bits)
            if v >> n:
                raise AsmError(f"{e.syntax()}: field {letter} value {v} does not fit {n} bits")
            for i, bit in enumerate(bits):
                if (v >> (n - 1 - i)) & 1:
                    word |= 1 << bit
        return word


_ISA: ISA | None = None


def get_isa() -> ISA:
    global _ISA
    if _ISA is None:
        _ISA = ISA()
    return _ISA
