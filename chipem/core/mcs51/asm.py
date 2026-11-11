"""MCS-51 assembler and disassembler (Intel ASM51-style syntax), built on the opcode table in isa.py.

Assembler (two passes, absolute code only):
  * lines:  [label:] [MNEMONIC operand, operand ...] [; comment]     or   NAME EQU|SET|DATA|IDATA|XDATA|BIT|CODE expr
  * directives: ORG expr, DB items (numbers, 'c', "strings"), DW items (big-endian words, as ASM51), DS n, END,
    INCLUDE "file" (relative to the including file), USING n (accepted, no effect)
  * numbers: 123, 0x7B, 7Bh / 0FFH, 1111011b, 173o / 173q, 'A'; `$` = address of the current instruction
  * expressions: ( ) unary - ~ NOT, * / % MOD, + -, << >> SHL SHR, & AND, ^ XOR, | OR; HIGH(x), LOW(x)
  * operands: A C AB DPTR @DPTR @A+DPTR @A+PC R0-R7 @R0 @R1 #imm, direct addresses (numbers, SFR names, DATA
    symbols), bits (BIT symbols, SFR bit names such as TI, BYTE.n with BYTE in 20h-2Fh or a bit-addressable SFR:
    P1.0, ACC.7, 20h.3), /bit (ANL/ORL C), code addresses for jumps (absolute; rel/a11 ranges are checked)
  * JMP / CALL (generic) assemble as LJMP / LCALL
  * SFR and bit names: the standard 8051 set plus the chip's own (`sfrs=`/`bits=`, e.g. from the chip's register
    table with `symbols_from_regs`)
Output: `AsmResult` (segments, symbols, listing); `to_ihex()`, `map_text()` (sidecar symbols for the loader).

Disassembler: `disasm_one(read, pc, names)` -> (size, text) and `disasm(read, pc, n)`; the text assembles back to
the same bytes (tests/test_mcs51_asm.py round-trips every opcode).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .isa import OPCODES, Insn, operands as isa_operands

STD_SFRS = {
    "P0": 0x80, "SP": 0x81, "DPL": 0x82, "DPH": 0x83, "PCON": 0x87, "TCON": 0x88, "TMOD": 0x89, "TL0": 0x8A,
    "TL1": 0x8B, "TH0": 0x8C, "TH1": 0x8D, "P1": 0x90, "SCON": 0x98, "SBUF": 0x99, "P2": 0xA0, "IE": 0xA8,
    "P3": 0xB0, "IP": 0xB8, "PSW": 0xD0, "ACC": 0xE0, "B": 0xF0,
}
STD_BITS = {
    "IT0": 0x88, "IE0": 0x89, "IT1": 0x8A, "IE1": 0x8B, "TR0": 0x8C, "TF0": 0x8D, "TR1": 0x8E, "TF1": 0x8F,
    "RI": 0x98, "TI": 0x99, "RB8": 0x9A, "TB8": 0x9B, "REN": 0x9C, "SM2": 0x9D, "SM1": 0x9E, "SM0": 0x9F,
    "EX0": 0xA8, "ET0": 0xA9, "EX1": 0xAA, "ET1": 0xAB, "ES": 0xAC, "EA": 0xAF,
    "RXD": 0xB0, "TXD": 0xB1, "INT0": 0xB2, "INT1": 0xB3, "T0": 0xB4, "T1": 0xB5, "WR": 0xB6, "RD": 0xB7,
    "PX0": 0xB8, "PT0": 0xB9, "PX1": 0xBA, "PT1": 0xBB, "PS": 0xBC,
    "P": 0xD0, "F1": 0xD1, "OV": 0xD2, "RS0": 0xD3, "RS1": 0xD4, "F0": 0xD5, "AC": 0xD6, "CY": 0xD7,
}
FIXED = {"A", "C", "AB", "DPTR", "@DPTR", "@A+DPTR", "@A+PC", "@R0", "@R1"} | {f"R{i}" for i in range(8)}


class AsmError(Exception):
    def __init__(self, msg: str, file: str = "", line: int = 0):
        super().__init__(f"{file}:{line}: {msg}" if line else msg)
        self.msg, self.file, self.line = msg, file, line


def symbols_from_regs(path: str | Path) -> tuple[dict[str, int], dict[str, int]]:
    """(sfr names, bit names) from a register table whose offsets are SFR addresses (chips/p89cv51_regs.yaml):
    every register, and every 1-bit field of a bit-addressable SFR (address a multiple of 8) as a bit name, except the
    port SFRs P0-P3 (their bits are written P1.2; the alternate-function names stay out of the bit namespace)."""
    from ...regtable import load_regs
    rt = load_regs(path)
    sfrs: dict[str, int] = {}
    bits: dict[str, int] = {}
    for pname in rt.names():
        pr = rt.peripheral(pname)
        for r in pr.registers:
            a = r.offset
            if not 0x80 <= a <= 0xFF:
                continue
            sfrs.setdefault(r.name.upper(), a)
            if a % 8 == 0 and r.name.upper() not in ("P0", "P1", "P2", "P3"):    # port bits stay Px.y
                for f in r.fields:
                    if f.width == 1 and not getattr(f, "access", "") == "reserved":
                        bits.setdefault(f.name.upper(), a + f.lsb)
    return sfrs, bits


@dataclass
class Names:
    """Symbol names used by the disassembler: SFRs, bits and code labels."""
    sfrs: dict[int, str] = field(default_factory=dict)
    bits: dict[int, str] = field(default_factory=dict)
    labels: dict[int, str] = field(default_factory=dict)

    @classmethod
    def standard(cls, sfrs: dict[str, int] | None = None, bits: dict[str, int] | None = None,
                 labels: dict[int, str] | None = None) -> "Names":
        s = {v: k for k, v in STD_SFRS.items()}
        b = {v: k for k, v in STD_BITS.items()}
        for k, v in (sfrs or {}).items():
            s.setdefault(v, k)
        for k, v in (bits or {}).items():
            b.setdefault(v, k)
        return cls(s, b, dict(labels or {}))


# ================================================================================================ disassembler
def _hex(v: int, w: int = 2) -> str:
    return f"0x{v:0{w}X}"


def _bitname(b: int, names: Names) -> str:
    n = names.bits.get(b)
    if n:
        return n
    if b < 0x80:
        return f"{_hex(0x20 + (b >> 3))}.{b & 7}"
    byte = b & 0xF8
    return f"{names.sfrs.get(byte, _hex(byte))}.{b & 7}"


def format_insn(e: Insn, pc: int, raw: bytes | list[int], names: Names | None = None) -> str:
    names = names or Names.standard()
    vals = isa_operands(e, pc, raw)
    out = []
    for k, v in zip(e.ops, vals):
        if k in FIXED:
            out.append(k)
        elif k == "dir":
            out.append(names.sfrs.get(v, _hex(v)) if v >= 0x80 else _hex(v))
        elif k == "#imm":
            out.append(f"#{_hex(v)}")
        elif k == "#imm16":
            out.append(f"#{_hex(v, 4)}")
        elif k == "bit":
            out.append(_bitname(v, names))
        elif k == "/bit":
            out.append("/" + _bitname(v, names))
        else:                                    # rel, a11, a16
            out.append(names.labels.get(v) or _hex(v, 4))
    return f"{e.mnem:<5} " + ", ".join(out) if out else e.mnem


def disasm_one(read: Callable[[int], int], pc: int, names: Names | None = None) -> tuple[int, str]:
    """(size, text) of the instruction at pc; read(addr) returns a code byte."""
    op = read(pc) & 0xFF
    e = OPCODES[op]
    if e is None:
        return 1, f"DB    {_hex(op)}            ; reserved opcode"
    raw = [read((pc + i) & 0xFFFF) & 0xFF for i in range(e.size)]
    return e.size, format_insn(e, pc, raw, names)


def disasm(read: Callable[[int], int], pc: int, n: int, names: Names | None = None) -> list[tuple[int, bytes, str]]:
    """n instructions from pc: [(addr, bytes, text)]."""
    out = []
    for _ in range(n):
        size, text = disasm_one(read, pc, names)
        out.append((pc, bytes(read((pc + i) & 0xFFFF) & 0xFF for i in range(size)), text))
        pc = (pc + size) & 0xFFFF
    return out


# ================================================================================================ assembler
_TOKEN = re.compile(r"""\s*(?:
    (?P<num>0[xX][0-9A-Fa-f]+|[0-9][0-9A-Fa-f]*[hH]\b|[01]+[bB]\b|[0-7]+[oOqQ]\b|[0-9]+\b)
  | (?P<chr>'(?:[^'\\]|\\.)')
  | (?P<name>[A-Za-z_?][A-Za-z0-9_?]*)
  | (?P<op><<|>>|[-+*/%&|^~()$])
)""", re.X)


def _num(t: str) -> int:
    u = t.upper()
    if u.startswith("0X"):
        return int(t[2:], 16)
    if u.endswith("H"):
        return int(t[:-1], 16)
    if u.endswith("B") and re.fullmatch(r"[01]+B", u):
        return int(t[:-1], 2)
    if u[-1] in "OQ":
        return int(t[:-1], 8)
    return int(t, 10)


def _unescape_char(s: str) -> int:
    body = s[1:-1]
    if body.startswith("\\"):
        return {"n": 10, "r": 13, "t": 9, "0": 0, "\\": 92, "'": 39, '"': 34}.get(body[1], ord(body[1]))
    return ord(body)


class _Expr:
    """Recursive-descent evaluator. `lookup(name)` returns an int or raises KeyError (undefined)."""
    WORD_OPS = {"MOD": "%", "SHL": "<<", "SHR": ">>", "AND": "&", "OR": "|", "XOR": "^", "NOT": "~"}

    def __init__(self, text: str, lookup: Callable[[str], int], here: int):
        self.toks: list[tuple[str, str]] = []
        pos = 0
        text = text.strip()
        while pos < len(text):
            m = _TOKEN.match(text, pos)
            if not m or m.end() == pos:
                raise ValueError(f"bad expression {text!r}")
            pos = m.end()
            kind = m.lastgroup
            val = m.group(kind)
            if kind == "name" and val.upper() in self.WORD_OPS:
                kind, val = "op", self.WORD_OPS[val.upper()]
            self.toks.append((kind, val))
            while pos < len(text) and text[pos].isspace():
                pos += 1
        self.i = 0
        self.lookup = lookup
        self.here = here

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def take(self):
        t = self.peek()
        self.i += 1
        return t

    def parse(self) -> int:
        v = self.p_or()
        if self.i != len(self.toks):
            raise ValueError("trailing tokens in expression")
        return v

    def _bin(self, sub, ops):
        v = sub()
        while self.peek()[0] == "op" and self.peek()[1] in ops:
            o = self.take()[1]
            r = sub()
            v = {"|": lambda a, b: a | b, "^": lambda a, b: a ^ b, "&": lambda a, b: a & b,
                 "<<": lambda a, b: a << b, ">>": lambda a, b: a >> b, "+": lambda a, b: a + b,
                 "-": lambda a, b: a - b, "*": lambda a, b: a * b, "/": lambda a, b: a // b,
                 "%": lambda a, b: a % b}[o](v, r)
        return v

    def p_or(self):
        return self._bin(self.p_xor, ("|",))

    def p_xor(self):
        return self._bin(self.p_and, ("^",))

    def p_and(self):
        return self._bin(self.p_shift, ("&",))

    def p_shift(self):
        return self._bin(self.p_add, ("<<", ">>"))

    def p_add(self):
        return self._bin(self.p_mul, ("+", "-"))

    def p_mul(self):
        return self._bin(self.p_unary, ("*", "/", "%"))

    def p_unary(self):
        k, v = self.peek()
        if k == "op" and v in ("-", "~", "+"):
            self.take()
            x = self.p_unary()
            return -x if v == "-" else (~x if v == "~" else x)
        return self.p_atom()

    def p_atom(self):
        k, v = self.take()
        if k == "num":
            return _num(v)
        if k == "chr":
            return _unescape_char(v)
        if k == "op" and v == "$":
            return self.here
        if k == "op" and v == "(":
            x = self.p_or()
            if self.take() != ("op", ")"):
                raise ValueError("missing )")
            return x
        if k == "name":
            u = v.upper()
            if u in ("HIGH", "LOW") and self.peek() == ("op", "("):
                self.take()
                x = self.p_or()
                if self.take() != ("op", ")"):
                    raise ValueError("missing )")
                return (x >> 8) & 0xFF if u == "HIGH" else x & 0xFF
            return self.lookup(v)
        raise ValueError(f"unexpected {v!r} in expression")


@dataclass
class AsmResult:
    segments: list[tuple[int, bytes]]
    symbols: dict[str, tuple[str, int]]          # name (as first written) -> (kind: CODE|DATA|IDATA|XDATA|BIT|NUMBER, value)
    listing: list[str]
    entry: int = 0

    def image_bytes(self) -> dict[int, int]:
        out: dict[int, int] = {}
        for a, d in self.segments:
            for i, b in enumerate(d):
                out[a + i] = b
        return out

    def to_ihex(self) -> str:
        lines = []
        for a, d in self.segments:
            for off in range(0, len(d), 16):
                chunk = d[off:off + 16]
                addr = a + off
                rec = [len(chunk), addr >> 8, addr & 0xFF, 0] + list(chunk)
                lines.append(":" + "".join(f"{x:02X}" for x in rec) + f"{(-sum(rec)) & 0xFF:02X}")
        lines.append(":00000001FF")
        return "\n".join(lines) + "\n"

    def to_bin(self, fill: int = 0xFF) -> bytes:
        if not self.segments:
            return b""
        end = max(a + len(d) for a, d in self.segments)
        out = bytearray([fill]) * end
        for a, d in self.segments:
            out[a:a + len(d)] = d
        return bytes(out)

    def map_text(self) -> str:
        """Sidecar symbol file read by chipem.firmware for .hex images: `C:0x0012 main` (C code, D data, I idata,
        X xdata, B bit, N number)."""
        tag = {"CODE": "C", "DATA": "D", "IDATA": "I", "XDATA": "X", "BIT": "B", "NUMBER": "N"}
        lines = ["; chipem mcs51 symbol map: SPACE:ADDRESS NAME"]
        for name, (kind, v) in sorted(self.symbols.items(), key=lambda kv: (kv[1][0], kv[1][1], kv[0])):
            lines.append(f"{tag.get(kind, 'N')}:0x{v & 0xFFFF:04X} {name}")
        return "\n".join(lines) + "\n"

    def labels(self) -> dict[int, str]:
        out: dict[int, str] = {}
        for n, (k, v) in sorted(self.symbols.items()):
            if k == "CODE":
                out.setdefault(v, n)
        return out


_LINE = re.compile(r"^\s*(?:(?P<label>[A-Za-z_?][A-Za-z0-9_?]*)\s*:)?\s*(?P<rest>.*)$")


def _strip_comment(s: str) -> str:
    out, q = [], None
    for ch in s:
        if q:
            out.append(ch)
            if ch == q:
                q = None
            continue
        if ch in "'\"":
            q = ch
        elif ch == ";":
            break
        out.append(ch)
    return "".join(out).rstrip()


def _split_operands(s: str) -> list[str]:
    out, cur, q, depth = [], [], None, 0
    for ch in s:
        if q:
            cur.append(ch)
            if ch == q:
                q = None
            continue
        if ch in "'\"":
            q = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if cur or out:
        out.append("".join(cur).strip())
    return [o for o in out if o != ""] if not (len(out) == 1 and out[0] == "") else []


class Assembler:
    def __init__(self, *, sfrs: dict[str, int] | None = None, bits: dict[str, int] | None = None,
                 include_dirs: Iterable[str | Path] = ()):
        self.sfrs = {**STD_SFRS, **{k.upper(): v for k, v in (sfrs or {}).items()}}
        self.bits = {**STD_BITS, **{k.upper(): v for k, v in (bits or {}).items()}}
        self.include_dirs = [Path(p) for p in include_dirs]

    # ---------------------------------------------------------------------------------------------- source
    def _lines(self, text: str, fname: str, depth: int = 0):
        for no, raw in enumerate(text.splitlines(), 1):
            s = _strip_comment(raw)
            m = re.match(r"^\s*(?:\$?INCLUDE|\.INCLUDE)\s*\(?\s*[\"']?([^\"')]+)[\"']?\s*\)?\s*$", s, re.I)
            if m:
                if depth > 8:
                    raise AsmError("include nesting too deep", fname, no)
                inc = m.group(1).strip()
                cands = [Path(fname).parent / inc] + [d / inc for d in self.include_dirs]
                for c in cands:
                    if c.exists():
                        yield from self._lines(c.read_text(), str(c), depth + 1)
                        break
                else:
                    raise AsmError(f"include file {inc!r} not found", fname, no)
                continue
            yield fname, no, raw, s

    def assemble(self, text: str, fname: str = "<asm>") -> AsmResult:
        lines = list(self._lines(text, fname))
        symbols: dict[str, tuple[str, int]] = {}             # UPPER name -> (kind, value): lookups ignore case
        self._spelling: dict[str, str] = {}                  # UPPER name -> spelling of its first definition
        for p in (1, 2):
            out: dict[int, int] = {}
            listing: list[str] = []
            pc = 0
            self._pass = p
            self._symbols = symbols
            for fn, no, raw, s in lines:
                try:
                    pc, emitted = self._line(s, pc, symbols, p, fn, no)
                except AsmError:
                    raise
                except (ValueError, KeyError) as e:
                    msg = e.args[0] if e.args else str(e)
                    raise AsmError(str(msg), fn, no) from None
                if emitted is None:
                    break                                    # END
                a, data = emitted
                if p == 2:
                    for i, b in enumerate(data):
                        addr = a + i
                        if addr > 0xFFFF:
                            raise AsmError("code beyond 0xFFFF", fn, no)
                        if addr in out:
                            raise AsmError(f"code overlaps at 0x{addr:04X}", fn, no)
                        out[addr] = b
                    if data or raw.strip():
                        listing.append(f"{a:04X}  {bytes(data).hex(' ').upper():<12} {raw.rstrip()}")
        segs: list[tuple[int, bytes]] = []
        for a in sorted(out):
            if segs and segs[-1][0] + len(segs[-1][1]) == a:
                segs[-1] = (segs[-1][0], segs[-1][1] + bytes([out[a]]))
            else:
                segs.append((a, bytes([out[a]])))
        return AsmResult(segs, {self._spelling.get(k, k): v for k, v in symbols.items()}, listing)

    # ---------------------------------------------------------------------------------------------- lines
    def _define(self, symbols, name: str, kind: str, value: int, p: int, redefine: bool = False):
        u = name.upper()
        if u in FIXED or u in ("A", "C"):
            raise ValueError(f"{name!r} is a reserved word")
        old = symbols.get(u)
        if p == 1 and old is not None and not redefine and old != (kind, value):
            raise ValueError(f"symbol {name!r} redefined")
        symbols[u] = (kind, value)
        self._spelling.setdefault(u, name)

    def _lookup(self, name: str) -> int:
        u = name.upper()
        s = self._symbols.get(u)
        if s is not None:
            return s[1]
        if u in self.sfrs:
            return self.sfrs[u]
        if u in self.bits:
            return self.bits[u]
        if self._pass == 1:
            return 0
        raise KeyError(f"undefined symbol {name!r}")

    def eval(self, text: str, here: int) -> int:
        return _Expr(text, self._lookup, here).parse()

    def _line(self, s: str, pc: int, symbols, p: int, fn: str, no: int):
        if not s.strip():
            return pc, (pc, b"")
        m = re.match(r"^\s*([A-Za-z_?][A-Za-z0-9_?]*)\s+(EQU|SET|DATA|IDATA|XDATA|BIT|CODE)\s+(.+)$", s, re.I)
        if m:
            name, kw, expr = m.group(1), m.group(2).upper(), m.group(3)
            if kw == "BIT":
                v = self._bit_value(expr.strip(), pc)
                self._define(symbols, name, "BIT", v, p)
            else:
                v = self.eval(expr, pc)
                kind = {"EQU": "NUMBER", "SET": "NUMBER"}.get(kw, kw)
                self._define(symbols, name, kind, v, p, redefine=kw == "SET")
            return pc, (pc, b"")
        lm = _LINE.match(s)
        label, rest = lm.group("label"), lm.group("rest").strip()
        if label:
            if label.upper() in ("A", "C") or label.upper() in FIXED:
                raise ValueError(f"bad label {label!r}")
            self._define(symbols, label, "CODE", pc, p)
        if not rest:
            return pc, (pc, b"")
        parts = rest.split(None, 1)
        mnem = parts[0].upper().lstrip(".")
        args = parts[1] if len(parts) > 1 else ""
        if mnem == "END":
            return pc, None
        if mnem == "ORG":
            return self.eval(args, pc) & 0xFFFF, (pc, b"")
        if mnem in ("USING", "CSEG", "NAME", "PUBLIC", "EXTRN"):
            return pc, (pc, b"")
        if mnem == "DS":
            n = self.eval(args, pc)
            return pc + n, (pc, b"")
        if mnem in ("DB", "DW"):
            data = bytearray()
            for item in _split_operands(args):
                if len(item) >= 2 and item[0] == '"' and item[-1] == '"' or \
                        mnem == "DB" and len(item) > 3 and item[0] == "'" and item[-1] == "'":
                    body = bytes(item[1:-1], "latin-1").decode("unicode_escape").encode("latin-1")
                    data += body
                    continue
                v = self.eval(item, pc)
                if mnem == "DB":
                    if not -128 <= v <= 255:
                        raise ValueError(f"DB value {v} out of range")
                    data.append(v & 0xFF)
                else:
                    data += bytes([(v >> 8) & 0xFF, v & 0xFF])
            return pc + len(data), (pc, bytes(data))
        data = self._insn(mnem, _split_operands(args), pc, p)
        return pc + len(data), (pc, data)

    # ---------------------------------------------------------------------------------------------- operands
    def _bit_value(self, t: str, pc: int) -> int:
        t = t.strip()
        u = t.upper()
        if u in self.bits and u not in self._symbols:
            return self.bits[u]
        m = re.match(r"^(.+)\.\s*([0-7])$", t)
        if m and not re.fullmatch(r"[0-9]+\.[0-9]+", t):
            byte = self.eval(m.group(1), pc)
            b = int(m.group(2))
            if 0x20 <= byte <= 0x2F:
                return (byte - 0x20) * 8 + b
            if byte >= 0x80 and byte % 8 == 0 and byte <= 0xF8:
                return byte + b
            if self._pass == 1:
                return 0
            raise ValueError(f"0x{byte:X} is not bit addressable")
        return self.eval(t, pc)

    @staticmethod
    def _classify(t: str) -> str:
        u = re.sub(r"\s+", "", t.upper())
        if u in FIXED:
            return u
        if u.startswith("@"):
            raise ValueError(f"bad indirect operand {t.strip()!r} (only @R0, @R1, @DPTR, @A+DPTR, @A+PC)")
        if u.startswith("#"):
            return "#"
        if u.startswith("/"):
            return "/"
        return "expr"

    def _insn(self, mnem: str, ops: list[str], pc: int, p: int) -> bytes:
        if mnem == "JMP" and not (len(ops) == 1 and self._classify(ops[0]) == "@A+DPTR"):
            mnem = "LJMP"
        elif mnem == "CALL":
            mnem = "LCALL"
        kinds = [self._classify(o) for o in ops]
        cands = [e for e in OPCODES if e is not None and e.mnem == mnem and len(e.ops) == len(ops)]
        if not cands:
            if not any(e is not None and e.mnem == mnem for e in OPCODES):
                raise ValueError(f"unknown instruction {mnem!r}")
            raise ValueError(f"{mnem}: wrong number of operands")
        for e in cands:
            if self._fits(e, kinds):
                return self._encode(e, ops, pc, p)
        raise ValueError(f"{mnem} {', '.join(ops)}: no such addressing mode")

    @staticmethod
    def _fits(e: Insn, kinds: list[str]) -> bool:
        for want, have in zip(e.ops, kinds):
            if want in FIXED:
                if have != want:
                    return False
            elif want in ("#imm", "#imm16"):
                if have != "#":
                    return False
            elif want == "/bit":
                if have != "/":
                    return False
            elif have != "expr":
                return False
        return True

    def _encode(self, e: Insn, ops: list[str], pc: int, p: int) -> bytes:
        npc = pc + e.size
        vals: list[int | None] = [None] * len(ops)
        first = e.op
        for i, (k, t) in enumerate(zip(e.ops, ops)):
            if k in FIXED:
                continue
            if k == "#imm":
                v = self.eval(t.strip()[1:], pc)
                if p == 2 and not -128 <= v <= 255:
                    raise ValueError(f"immediate {v} out of 8-bit range")
                vals[i] = v & 0xFF
            elif k == "#imm16":
                v = self.eval(t.strip()[1:], pc)
                if p == 2 and not -32768 <= v <= 0xFFFF:
                    raise ValueError(f"immediate {v} out of 16-bit range")
                vals[i] = v & 0xFFFF
            elif k == "dir":
                v = self.eval(t, pc)
                if p == 2 and not 0 <= v <= 0xFF:
                    raise ValueError(f"direct address 0x{v:X} out of range")
                vals[i] = v & 0xFF
            elif k in ("bit", "/bit"):
                v = self._bit_value(t.strip().lstrip("/"), pc)
                if p == 2 and not 0 <= v <= 0xFF:
                    raise ValueError(f"bit address 0x{v:X} out of range")
                vals[i] = v & 0xFF
            elif k == "rel":
                v = self.eval(t, pc)
                d = ((v - npc + 0x8000) & 0xFFFF) - 0x8000          # the code space wraps at 64 KB
                if p == 2 and not -128 <= d <= 127:
                    raise ValueError(f"relative jump to 0x{v:04X} out of range ({d})")
                vals[i] = d & 0xFF
            elif k == "a11":
                v = self.eval(t, pc)
                if p == 2 and (v & 0xF800) != (npc & 0xF800):
                    raise ValueError(f"{e.mnem} target 0x{v:04X} not in the 2 KB page of 0x{npc:04X}")
                first = e.op | ((v >> 3) & 0xE0)
                vals[i] = v & 0xFF
            elif k == "a16":
                v = self.eval(t, pc)
                if p == 2 and not 0 <= v <= 0xFFFF:
                    raise ValueError(f"address 0x{v:X} out of range")
                vals[i] = v & 0xFFFF
        out = bytearray([first & 0xFF])
        for idx in e.order:
            k = e.ops[idx]
            v = vals[idx]
            if v is None:
                continue
            if k in ("#imm16", "a16"):
                out += bytes([(v >> 8) & 0xFF, v & 0xFF])
            else:
                out.append(v)
        assert len(out) == e.size, (e, out)
        return bytes(out)


def assemble(text: str, fname: str = "<asm>", **kw) -> AsmResult:
    return Assembler(**kw).assemble(text, fname)


def assemble_file(path: str | Path, **kw) -> AsmResult:
    p = Path(path)
    return Assembler(**kw).assemble(p.read_text(), str(p))
