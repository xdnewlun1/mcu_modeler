"""C28x assembler and disassembler driven by the ISA table (arch/c28x/isa.yaml).

Assembler: a TI-syntax subset for test firmware.
  * labels (column 0, optional ':'; or `name:` anywhere), comments `;` and `*` in column 0
  * sections: `.text`, `.data`, `.sect "name"[, addr]`, `.org expr` (sets the current section's location counter);
    `.bss sym, size`; addresses are 16-bit word addresses
  * data: `.word/.short/.int/.half/.uword` (16 bit), `.long/.ulong` (32 bit, even-aligned, low word first),
    `.float` (IEEE single), `.string/.cstring` (one character per word), `.space/.bes n`, `.align n`
  * symbols: `sym .set expr`, `sym .equ expr`, `.set sym, expr`; `.global/.def/.ref` accepted and ignored;
    `.entry expr` sets the image entry (else `_c_int00`, `start` or `_start` if defined)
  * expressions: + - * / % << >> & | ^ ~, parentheses, `$` (current address), numbers 123 0x1F 1Fh 0b101 101b 'c',
    functions DP(x) = x >> 6 (`MOVW DP,#DP(var)`), LO16(x), HI16(x)
  * every addressing-mode syntax of the table (`@var` takes the low 6 bits, i.e. assumes DP is set), `#` immediates,
    condition names (NEQ EQ GT GEQ LT LEQ HI HIS LO LOS NOV OV NTC TC NBIO UNC, aliases C NC), branch targets as
    labels/addresses (relative offsets computed), 22-bit absolute targets
  * pseudo-instructions: `MOVF32 RaH,#float` and `MOVI32 RaH,#32bit` (MOVIZ[F32] + MOVXI)
Passes repeat until label addresses are stable.

Disassembler: `disasm(words, addr)` decodes with the same table and prints text the assembler accepts.
"""
from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .isa import ISA, AsmError, Entry, KINDS, _split_top, f32_bits, get_isa


# ============================================================================================= expressions
_TOK = re.compile(r"""\s*(?:
    (?P<num>0[xX][0-9A-Fa-f]+|0[bB][01]+|[0-9][0-9A-Fa-f]*[hH](?![\w])|[01]+[bB](?![\w])|\d+)
  | (?P<chr>'(?:\\.|[^'])')
  | (?P<sym>[A-Za-z_.][\w.$]*|\$[\w.$]+)
  | (?P<op><<|>>|[-+*/%&|^~()$,])
)""", re.X)


class Expr:
    """Tiny expression evaluator; undefined symbols make the result None (and set `unresolved`)."""

    FUNCS = {"DP": lambda x: x >> 6, "LO16": lambda x: x & 0xFFFF, "HI16": lambda x: (x >> 16) & 0xFFFF,
             "$LO16": lambda x: x & 0xFFFF, "$HI16": lambda x: (x >> 16) & 0xFFFF}

    def __init__(self, symbols: dict[str, int], here: int):
        self.symbols = symbols
        self.here = here
        self.unresolved = False

    def __call__(self, text: str) -> int | None:
        self.toks = self._lex(text)
        self.i = 0
        v = self._binary(0)
        if self.i != len(self.toks):
            raise AsmError(f"bad expression {text!r}")
        return v

    @staticmethod
    def _lex(text: str) -> list[tuple[str, str]]:
        out, pos, t = [], 0, text.strip()
        while pos < len(t):
            m = _TOK.match(t, pos)
            if not m or m.end() == pos:
                raise AsmError(f"bad expression {text!r}")
            pos = m.end()
            kind = m.lastgroup
            out.append((kind, m.group(kind)))
        return out

    _PREC = [("|",), ("^",), ("&",), ("<<", ">>"), ("+", "-"), ("*", "/", "%")]

    def _binary(self, lvl: int):
        if lvl == len(self._PREC):
            return self._unary()
        v = self._binary(lvl + 1)
        while self.i < len(self.toks) and self.toks[self.i] == ("op", self.toks[self.i][1]) \
                and self.toks[self.i][1] in self._PREC[lvl]:
            op = self.toks[self.i][1]
            self.i += 1
            w = self._binary(lvl + 1)
            if v is None or w is None:
                v = None
                continue
            if op == "|":
                v |= w
            elif op == "^":
                v ^= w
            elif op == "&":
                v &= w
            elif op == "<<":
                v <<= w
            elif op == ">>":
                v >>= w
            elif op == "+":
                v += w
            elif op == "-":
                v -= w
            elif op == "*":
                v *= w
            elif op == "/":
                if w == 0:
                    raise AsmError("division by zero")
                v = int(v / w)
            else:
                v %= w
        return v

    def _unary(self):
        if self.i >= len(self.toks):
            raise AsmError("unexpected end of expression")
        kind, t = self.toks[self.i]
        self.i += 1
        if kind == "op":
            if t == "-":
                v = self._unary()
                return None if v is None else -v
            if t == "+":
                return self._unary()
            if t == "~":
                v = self._unary()
                return None if v is None else ~v
            if t == "(":
                v = self._binary(0)
                self._expect(")")
                return v
            if t == "$":
                return self.here
            raise AsmError(f"unexpected {t!r}")
        if kind == "num":
            return _parse_num(t)
        if kind == "chr":
            s = t[1:-1]
            return ord(s.encode().decode("unicode_escape"))
        # symbol / function
        if self.i < len(self.toks) and self.toks[self.i] == ("op", "(") and t.upper() in self.FUNCS:
            self.i += 1
            v = self._binary(0)
            self._expect(")")
            return None if v is None else self.FUNCS[t.upper()](v)
        if t in self.symbols:
            return self.symbols[t]
        self.unresolved = True
        return None

    def _expect(self, op: str) -> None:
        if self.i >= len(self.toks) or self.toks[self.i] != ("op", op):
            raise AsmError(f"expected {op!r}")
        self.i += 1


def _parse_num(t: str) -> int:
    tl = t.lower()
    if tl.startswith("0x"):
        return int(t[2:], 16)
    if tl.startswith("0b"):
        return int(t[2:], 2)
    if tl.endswith("h"):
        return int(t[:-1], 16)
    if tl.endswith("b") and set(tl[:-1]) <= {"0", "1"}:
        return int(t[:-1], 2)
    return int(t, 10)


# ============================================================================================= operand matching
def _split_shift(text: str) -> tuple[str, str | None]:
    depth = 0
    for i, ch in enumerate(text):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif depth == 0 and text.startswith("<<", i):
            return text[:i].strip(), text[i + 2:].strip()
    return text.strip(), None


def _norm(t: str) -> str:
    return t.replace(" ", "").upper()


class _Matcher:
    def __init__(self, isa: ISA):
        self.isa = isa
        self.last_err: Exception | None = None

    def literal_ok(self, pat: str, src: str, ev: Expr) -> bool:
        p, s = _norm(pat), _norm(src)
        if p == s:
            return True
        if p.startswith("#") and s.startswith("#"):
            try:
                a = float(p[1:])
                try:
                    b = float(s[1:])
                except ValueError:
                    b = ev(s[1:])
                return b is not None and float(b) == a
            except (ValueError, AsmError):
                return False
        return False

    def part(self, pat: str, kind, src: str, pc: int, ev: Expr, fields: dict) -> bool:
        if kind is None:
            return self.literal_ok(pat, src, ev)
        ev.unresolved = False
        try:
            v = kind.parse(src, ev)
        except AsmError as e:
            self.last_err = e
            return False
        if v is None:
            return False
        if kind.name == "flagset":
            fields["F"], fields["V"] = v
            return True
        if ev.unresolved:
            fields[kind.letter] = 0
            return True
        return self._encode(kind, v, pc, fields)

    def _encode(self, kind, v, pc, fields) -> bool:
        e = self._entry
        try:
            fields[kind.letter] = kind.to_field(v, pc, len(e.fields[kind.letter]))
        except AsmError as ex:
            self.last_err = ex
            return False
        return True

    def match(self, e: Entry, srcs: list[str], pc: int, ev: Expr) -> dict | None:
        self._entry = e
        pats = e.operands
        if pats and pats[-1].value_kind is not None and pats[-1].value_kind.rest and pats[-1].shift is None:
            k = len(pats) - 1
            if len(srcs) < len(pats):
                return None
            srcs = srcs[:k] + [", ".join(srcs[k:])]
        if len(srcs) != len(pats):
            return None
        fields: dict[str, int] = {}
        for pat, src in zip(pats, srcs):
            v, s = _split_shift(src) if pat.shift is not None or "<<" in src else (src.strip(), None)
            if pat.shift is None and s is not None:
                return None
            if not self.part(pat.value, pat.value_kind, v, pc, ev, fields):
                return None
            if pat.shift is not None:
                if s is None:
                    if pat.shift_kind is not None:
                        if pat.shift_kind.name != "#sh0_15":
                            return None
                        s = "#0"
                    elif _norm(pat.shift) != "#0":
                        return None
                    else:
                        continue
                if not self.part(pat.shift, pat.shift_kind, s, pc, ev, fields):
                    return None
        return fields


# ============================================================================================= assembler
@dataclass
class AsmResult:
    segments: list[tuple[int, bytes]]                  # (word address, little-endian bytes)
    symbols: dict[str, int]
    entry: int | None
    listing: list[tuple[int, list[int], str]] = field(default_factory=list)

    def words(self) -> dict[int, int]:
        out = {}
        for a, d in self.segments:
            for i in range(len(d) // 2):
                out[a + i] = d[2 * i] | (d[2 * i + 1] << 8)
        return out

    def to_c28img(self) -> dict:
        return {"format": "c28img", "unit_bits": 16, "entry": self.entry,
                "segments": [{"addr": a, "data_hex": d.hex()} for a, d in self.segments],
                "symbols": dict(sorted(self.symbols.items(), key=lambda kv: kv[1]))}

    def write_c28img(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_c28img(), indent=1) + "\n")

    def write_bin(self, path: str | Path) -> None:
        if len(self.segments) != 1:
            raise AsmError(f"plain binary output needs exactly one segment, have {len(self.segments)}")
        Path(path).write_bytes(self.segments[0][1])

    def map_text(self) -> str:
        return "".join(f"{v:06X}  {k}\n" for k, v in sorted(self.symbols.items(), key=lambda kv: (kv[1], kv[0])))

    def image(self):
        from ...firmware import FirmwareImage
        return FirmwareImage(list(self.segments), self.entry, "asm", "c28img", 2, dict(self.symbols))


_LABEL0 = re.compile(r"^([A-Za-z_.$][\w.$]*):?")
_LABELC = re.compile(r"^\s*([A-Za-z_.$][\w.$]*):")


class Assembler:
    def __init__(self, isa: ISA | None = None):
        self.isa = isa or get_isa()
        self.matcher = _Matcher(self.isa)

    # ------------------------------------------------------------------ public
    def assemble(self, text: str, *, filename: str = "<asm>", sections: dict[str, int] | None = None) -> AsmResult:
        self.filename = filename
        self.sect_base = dict(sections or {})
        lines = text.splitlines()
        self.symbols: dict[str, int] = {}
        self.choice: dict[int, Entry] = {}
        prev = None
        for _ in range(12):
            self._pass(lines, final=False)
            snap = dict(self.symbols)
            if snap == prev:
                break
            prev = snap
        else:
            raise AsmError("label addresses did not converge")
        self._pass(lines, final=True)
        entry = self.entry
        if entry is None:
            for k in ("_c_int00", "start", "_start"):
                if k in self.symbols:
                    entry = self.symbols[k]
                    break
        return AsmResult(self._segments(), dict(self.labels_out), entry, self.listing)

    # ------------------------------------------------------------------ passes
    def _pass(self, lines: list[str], final: bool) -> None:
        self.final = final
        self.out: dict[int, int] = {}
        self.listing: list[tuple[int, list[int], str]] = []
        self.locs: dict[str, int] = {}
        self.sect = ".text"
        self.locs[self.sect] = self.sect_base.get(".text", 0)
        self.entry = None
        self.labels_out: dict[str, int] = {}
        lines = _merge_parallel(lines)
        for ln, raw in enumerate(lines, 1):
            self.lineno = ln
            try:
                if self._line(raw, ln) == "end":
                    break
            except AsmError as e:
                if final or not getattr(e, "soft", False):
                    raise AsmError(f"{self.filename}:{ln}: {e}\n    {raw.strip()}") from None

    @property
    def here(self) -> int:
        return self.locs[self.sect]

    def _adv(self, n: int) -> None:
        self.locs[self.sect] = self.here + n

    def _emit(self, words: list[int], src: str = "") -> None:
        a = self.here
        if self.final:
            for i, w in enumerate(words):
                if a + i in self.out:
                    raise AsmError(f"overlapping output at 0x{a + i:06X}")
                self.out[a + i] = w & 0xFFFF
            self.listing.append((a, list(words), src))
        self._adv(len(words))

    def _define(self, name: str, value: int, label: bool = True) -> None:
        self.symbols[name] = value
        self.labels_out[name] = value

    def _ev(self, text: str, need: bool = False) -> int | None:
        e = Expr(self.symbols, self.here)
        v = e(text)
        if v is None and (need or self.final):
            raise AsmError(f"undefined symbol in {text!r}")
        return v

    def _line(self, raw: str, ln: int):
        line = _strip_comment(raw)
        if not line.strip() or raw.startswith("*"):
            return None
        label = None
        if not line[0].isspace():
            m = _LABEL0.match(line)
            if m and not line.startswith("."):
                label = m.group(1)
                line = line[m.end():]
        else:
            m = _LABELC.match(line)
            if m:
                label = m.group(1)
                line = line[m.end():]
        parts = line.strip().split(None, 1)
        op = parts[0] if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        opl = op.lower()
        if opl in (".set", ".equ") and label is not None:
            self._define(label, self._ev(rest, need=self.final) or 0)
            return None
        if label is not None:
            self._define(label, self.here)
        if not op:
            return None
        if op.startswith("."):
            return self._directive(opl, rest)
        self._instruction(op.upper(), rest, raw.strip())
        return None

    # ------------------------------------------------------------------ directives
    def _directive(self, d: str, rest: str):
        args = _split_top(rest) if rest else []
        if d in (".text", ".data"):
            self._switch(d)
        elif d == ".sect":
            name = args[0].strip().strip('"')
            self._switch(name)
            if len(args) > 1:
                self.locs[name] = self._ev(args[1], need=True)
        elif d == ".org":
            self.locs[self.sect] = self._ev(rest, need=True)
        elif d in (".word", ".short", ".int", ".half", ".uword", ".uhalf", ".ushort"):
            self._emit([(self._ev(a) or 0) & 0xFFFF for a in args], rest)
        elif d in (".long", ".ulong", ".xlong"):
            if self.here & 1:
                self._emit([0])
            ws = []
            for a in args:
                v = (self._ev(a) or 0) & 0xFFFFFFFF
                ws += [v & 0xFFFF, v >> 16]
            self._emit(ws, rest)
        elif d in (".float", ".xfloat"):
            if self.here & 1:
                self._emit([0])
            ws = []
            for a in args:
                b = f32_bits(float(a))
                ws += [b & 0xFFFF, b >> 16]
            self._emit(ws, rest)
        elif d in (".string", ".cstring", ".pstring"):
            ws = []
            for a in args:
                a = a.strip()
                if a.startswith('"'):
                    ws += [ord(ch) for ch in a[1:-1].encode().decode("unicode_escape")]
                else:
                    ws.append((self._ev(a) or 0) & 0xFFFF)
            if d == ".cstring":
                ws.append(0)
            self._emit(ws, rest)
        elif d in (".space", ".bes", ".zero"):
            self._emit([0] * self._ev(rest, need=True))
        elif d == ".align":
            n = self._ev(rest, need=True) if rest else 2
            pad = (-self.here) % n
            self._emit([0] * pad)
        elif d == ".bss":
            name = args[0].strip()
            size = self._ev(args[1], need=True)
            saved = self.sect
            self._switch(".bss")
            self._define(name, self.here)
            self._adv(size)
            self._switch(saved)
        elif d in (".set", ".equ"):
            self._define(args[0].strip(), self._ev(args[1], need=self.final) or 0)
        elif d == ".entry":
            self.entry = self._ev(rest)
        elif d == ".end":
            return "end"
        elif d in (".global", ".globl", ".def", ".ref", ".title", ".width", ".length", ".option", ".newblock",
                   ".clink", ".retain", ".asg", ".cdecls"):
            pass
        else:
            raise AsmError(f"unknown directive {d}")
        return None

    def _switch(self, name: str) -> None:
        self.sect = name
        if name not in self.locs:
            self.locs[name] = self.sect_base.get(name, 0)

    # ------------------------------------------------------------------ instructions
    def _instruction(self, mn: str, rest: str, src: str) -> None:
        if "||" in rest:
            left, right = rest.split("||", 1)
            if mn == "RPT":                          # RPT #n || insn  (one-line repeat syntax)
                self._instruction(mn, left.strip(), src)
                parts = right.strip().split(None, 1)
                if parts:
                    self._instruction(parts[0].upper(), parts[1].strip() if len(parts) > 1 else "", src)
                return
            parts = right.strip().split(None, 1)       # FPU parallel pair: "MPYF32 a,b,c || ADDF32 d,e,f"
            mn = f"{mn}||{parts[0].upper()}"
            srcs = (_split_top(left) if left.strip() else []) + (_split_top(parts[1]) if len(parts) > 1 else [])
            rest = ""
        else:
            srcs = _split_top(rest) if rest else []
        if mn in ("MOVF32", "MOVI32"):
            return self._movf32(mn, srcs, src)
        cands = self.isa.by_mn.get(mn)
        if not cands:
            raise AsmError(f"unknown mnemonic {mn}")
        pc = self.here
        key = (self.lineno, self.sect, mn)
        try:
            e, fields = self._select(cands, srcs, pc, key)
        except AsmError:
            merged = _merge_arp(srcs)                # loc operand "*,ARPn" contains a comma
            if merged == srcs:
                raise
            e, fields = self._select(cands, merged, pc, key)
        self._emit(self._words(e, fields), src)

    def _select(self, cands, srcs, pc, key):
        m = self.matcher
        m.last_err = None
        ev = Expr(self.symbols, pc)
        chosen = self.choice.get(key)
        if chosen is not None and self.final:
            f = m.match(chosen, srcs, pc, ev)
            if f is not None and not ev.unresolved:
                return chosen, f
        for e in cands:
            ev = Expr(self.symbols, pc)
            f = m.match(e, srcs, pc, ev)
            if f is not None:
                if self.final and ev.unresolved:
                    raise AsmError("undefined symbol")
                self.choice[key] = e
                return e, f
        if m.last_err is not None:
            raise AsmError(str(m.last_err))
        forms = "; ".join(e.syntax() for e in cands)
        raise AsmError(f"no form of {cands[0].mn} matches operands {', '.join(srcs)!r} (forms: {forms})")

    def _words(self, e: Entry, fields: dict) -> list[int]:
        w = self.isa.encode_fields(e, fields)
        return [w >> 16, w & 0xFFFF] if e.nwords == 2 else [w]

    def _movf32(self, mn: str, srcs: list[str], src: str) -> None:
        if len(srcs) != 2 or not srcs[1].strip().startswith("#"):
            raise AsmError(f"{mn} RaH, #value")
        reg, val = srcs[0].strip(), srcs[1].strip()[1:]
        if mn == "MOVF32" and re.fullmatch(r"[-+]?(\d+\.\d*|\.\d+|\d+(\.\d*)?[eE][-+]?\d+|\d+)", val.strip()):
            bits = f32_bits(float(val))
        else:
            bits = (self._ev(val) or 0) & 0xFFFFFFFF
        hi, lo = bits >> 16, bits & 0xFFFF
        if mn == "MOVF32" and lo == 0:
            self._instruction("MOVIZ", f"{reg}, #0x{hi:X}", src)
            return
        self._instruction("MOVIZ", f"{reg}, #0x{hi:X}", src)
        self._instruction("MOVXI", f"{reg}, #0x{lo:X}", src)

    # ------------------------------------------------------------------ output
    def _segments(self) -> list[tuple[int, bytes]]:
        segs: list[tuple[int, bytes]] = []
        cur_a, cur = None, []
        for a in sorted(self.out):
            if cur_a is not None and a == cur_a + len(cur):
                cur.append(self.out[a])
                continue
            if cur:
                segs.append((cur_a, struct.pack(f"<{len(cur)}H", *cur)))
            cur_a, cur = a, [self.out[a]]
        if cur:
            segs.append((cur_a, struct.pack(f"<{len(cur)}H", *cur)))
        return segs


def _merge_arp(srcs: list[str]) -> list[str]:
    out: list[str] = []
    for s_ in srcs:
        if out and out[-1].strip() == "*" and re.fullmatch(r"ARP[0-7]\s*(<<.*)?", s_.strip().upper()):
            out[-1] = f"*,{s_.strip()}"
        else:
            out.append(s_)
    return out


def _merge_parallel(lines: list[str]) -> list[str]:
    """TI two-line syntax: a line starting with `||` continues the previous instruction.  After RPT the bars are
    only a reminder and are dropped; otherwise the line is appended (`A ... || B ...`).  Line count is kept."""
    out = list(lines)
    prev = None
    for i, raw in enumerate(out):
        body = _strip_comment(raw)
        t = body.strip()
        if not t or raw.startswith("*"):
            continue
        if t.startswith("||") and prev is not None:
            pbody = _strip_comment(out[prev])
            toks = pbody.split()
            mns = [x.upper() for x in toks[:2]]
            if "RPT" in mns:
                out[i] = body.replace("||", "  ", 1)
            else:
                out[prev] = pbody + " " + t
                out[i] = ""
                continue
        prev = i
    return out


def _strip_comment(line: str) -> str:
    out, q = [], None
    for ch in line:
        if q:
            out.append(ch)
            if ch == q:
                q = None
            continue
        if ch in "\"'":
            q = ch
        elif ch == ";":
            break
        out.append(ch)
    return "".join(out).rstrip()


def assemble(text: str, **kw) -> AsmResult:
    return Assembler().assemble(text, **kw)


# ============================================================================================= disassembler
def format_insn(e: Entry, o: dict) -> str:
    parts = []
    for op in e.operands:
        if op.value_kind is not None:
            k = op.value_kind
            v = o["flagset"] if k.name == "flagset" else o[k.role]
            t = k.fmt(v)
        else:
            t = op.value
        if op.shift is not None:
            t += "<<" + (op.shift_kind.fmt(o[op.shift_kind.role]) if op.shift_kind is not None else op.shift)
        parts.append(t)
    if e.par:
        m1, m2 = e.mn.split("||")
        return f"{m1} {','.join(parts[:e.par])} || {m2} {','.join(parts[e.par:])}"
    return f"{e.mn} {','.join(parts)}".rstrip()


def disasm_one(words, addr: int = 0, isa: ISA | None = None) -> tuple[int, str, Entry | None]:
    """(length, text, entry) for the instruction at words[0] (address `addr`)."""
    isa = isa or get_isa()
    w1 = words[0] & 0xFFFF
    m = isa.match(w1, lambda: (words[1] & 0xFFFF) if len(words) > 1 else None)
    if m is None:
        return 1, f".word 0x{w1:04X}", None
    e, word = m
    return e.nwords, format_insn(e, isa.operands(e, word, addr)), e


def disasm(words, addr: int = 0) -> list[tuple[int, int, str]]:
    """[(address, length, text)] for a word sequence starting at `addr`."""
    isa = get_isa()
    out, i = [], 0
    words = list(words)
    while i < len(words):
        n, t, _ = disasm_one(words[i:i + 2], addr + i, isa)
        out.append((addr + i, n, t))
        i += n
    return out


def disasm_core(core, addr: int, count: int = 10) -> list[tuple[int, int, str]]:
    """Disassemble `count` instructions from a core's memory (for the shell)."""
    out = []
    for _ in range(count):
        try:
            ws = list(struct.unpack("<2H", core.read_mem(addr, 2)))
        except Exception:
            break
        n, t, _ = disasm_one(ws, addr)
        out.append((addr, n, t))
        addr += n
    return out


def encode(e: Entry, values: dict[str, Any], pc: int = 0) -> list[int]:
    """Words for entry `e` with operand values keyed by kind name (tests / tools)."""
    isa = get_isa()
    fields = {}
    for op in e.operands:
        for k in (op.value_kind, op.shift_kind):
            if k is None:
                continue
            v = values[k.name]
            if k.name == "flagset":
                fields["F"], fields["V"] = v
            else:
                fields[k.letter] = k.to_field(v, pc, len(e.fields[k.letter]))
    w = isa.encode_fields(e, fields)
    return [w >> 16, w & 0xFFFF] if e.nwords == 2 else [w]
