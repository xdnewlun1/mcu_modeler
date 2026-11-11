"""Firmware debugger: breakpoints, watchpoints, stepping, source / disassembly, call stack, registers, memory.

Shared by the shell (`break`, `next`, ...), the web UI (Debug tab, /api/debug/*) and the GDB server
(chipem/gdbserver.py). Everything here runs on the emulation thread (the shell's thread, the web Worker, the GDB
executor); nothing is thread-safe.

Layers:
  * The cores implement address breakpoints and data watchpoints (core.set_breakpoints / set_watchpoints,
    StopReason.DEBUG, docs/CONTRACTS.md "Debugger"). They cost nothing while none is set: Unicorn gets one
    UC_HOOK_CODE per breakpoint address and one ranged memory hook per watchpoint; the C28x interpreter wraps the
    decoded closure of a breakpoint PC and sends watched pages down its slow access path.
  * `Debugger` (one per chip) owns the user's breakpoints (ids, enable, hit counts, ignore counts, conditions,
    temporary ones) and the internal ones of step over / out / run to, evaluates conditions in the core's hook, and
    offers inspection: registers, side-effect-free memory (`read_memory` peeks MMIO), disassembly, mixed
    source + assembly listing (DWARF line info, chipem/dwarf.py), call stack (DWARF CFI, exception frames, else a
    stack scan like the shell's `bt`).
  * `SessionDebug` (Session.debug) spans the chips of a session: unique breakpoint ids, the last stop (which chip,
    which breakpoint, board skew), stepping commands that run the whole board (so chips stay in lockstep).

Stepping (`plan_*` return "done" when the chip already moved, "run" when the caller must continue until the next
debugger stop; internal breakpoints are removed at any stop):
  stepi       one instruction (calls entered)                        Session.step
  nexti       one instruction, a call (BL/BLX, LCR/LC/FFC) is run    internal breakpoint after the call, same frame
  next        source line, calls run                                 internal breakpoints on the function's other
                                                                     lines + its return address (same frame or outer)
  step        source line, entering called functions with line info  as next + breakpoints on direct call targets
  finish      run until the current function returns                 internal breakpoint on the return address
  until LOC   run until LOC is reached                                internal breakpoint
Without line info `next`/`step` act as `nexti`/`stepi`.
"""
from __future__ import annotations

import ast
import os
import re
import struct
from dataclasses import dataclass, field
from typing import Any, Callable

from .core import StopReason

__all__ = ["Breakpoint", "Debugger", "SessionDebug", "DebugError"]

ARM_REGS = [f"r{i}" for i in range(13)] + ["sp", "lr", "pc", "xpsr", "msp", "psp", "primask", "basepri",
                                            "faultmask", "control"]
EXC_RETURN_MIN = 0xFFFFFFE0


class DebugError(ValueError):
    pass


@dataclass
class Breakpoint:
    id: int
    kind: str                     # "break" | "watch"
    addr: int                     # break: code address; watch: first address
    end: int = 0                  # watch: exclusive end address
    access: str = "w"             # watch: "r" | "w" | "rw"
    location: str = ""            # what the user typed
    temp: bool = False
    enabled: bool = True
    hits: int = 0
    ignore: int = 0               # hits to ignore before stopping
    cond: str | None = None
    internal: bool = False        # step over / out / run-to (hidden, removed at any stop)
    frame: tuple | None = None    # internal: ("ge"|"le"|"lt"|"gt", sp0): stop only in that frame relation
    chip: str = "A"
    label: str = ""               # resolved description ("main+0x4 blinky.c:12", "USART1.SR")
    _cond_fn: Callable | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        d = {"id": self.id, "kind": self.kind, "addr": self.addr, "location": self.location, "temp": self.temp,
             "enabled": self.enabled, "hits": self.hits, "ignore": self.ignore, "cond": self.cond,
             "chip": self.chip, "label": self.label}
        if self.kind == "watch":
            d.update(end=self.end, size=self.end - self.addr, access=self.access)
        return d


# ------------------------------------------------------------------------------------------------- expressions
_ALLOWED = (ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.Name, ast.Load, ast.Constant,
            ast.Subscript, ast.Call, ast.And, ast.Or, ast.Not, ast.Add, ast.Sub, ast.Mult, ast.FloorDiv, ast.Div,
            ast.Mod, ast.LShift, ast.RShift, ast.BitAnd, ast.BitOr, ast.BitXor, ast.Invert, ast.USub, ast.UAdd,
            ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.IfExp, ast.Slice)


def compile_expr(text: str) -> ast.Expression:
    """Parse a debugger expression: integers, registers (r0, sp, pc, acc, xar4 ...), symbols, `mem8[a]`,
    `mem16[a]`, `mem32[a]`, `addr(sym)`, arithmetic, comparisons, `and`/`or`/`not`, also `&&`, `||`, `!`."""
    t = str(text).strip()
    if not t:
        raise DebugError("empty expression")
    t = t.replace("&&", " and ").replace("||", " or ")
    t = re.sub(r"!(?!=)", " not ", t)
    try:
        tree = ast.parse(t, mode="eval")
    except SyntaxError as e:
        raise DebugError(f"bad expression {text!r}: {e.msg}") from None
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise DebugError(f"bad expression {text!r}: {type(node).__name__} not allowed")
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id == "addr"
                                               and len(node.args) == 1 and not node.keywords):
            raise DebugError(f"bad expression {text!r}: only addr(SYMBOL) may be called")
    return tree


_BIN = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
        ast.FloorDiv: lambda a, b: a // b, ast.Div: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
        ast.LShift: lambda a, b: a << b, ast.RShift: lambda a, b: a >> b, ast.BitAnd: lambda a, b: a & b,
        ast.BitOr: lambda a, b: a | b, ast.BitXor: lambda a, b: a ^ b}
_CMP = {ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b, ast.Lt: lambda a, b: a < b,
        ast.LtE: lambda a, b: a <= b, ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b}


# ------------------------------------------------------------------------------------------------- per-chip debugger
class Debugger:
    """Debugger state of one chip (see the module docstring)."""

    def __init__(self, chip: Any, image: Any = None, *, cid: str = "A", source_dirs: list[str] | None = None,
                 ids: Callable[[], int] | None = None):
        from .symbols import Symbols
        self.chip = chip
        self.core = chip.core
        self.cid = cid
        self.image = image
        self.c28x = getattr(chip, "arch_kind", "") == "c28x"
        self.ub = 2 if self.c28x else 1                       # bytes per address unit
        # arch-provided debugger support (MCS-51: chipem/arch/mcs51/debug.py): registers, decode, call stack
        ds = getattr(chip.arch, "debug_support", None)
        self.ad = ds() if callable(ds) else None
        self.symbols = Symbols.for_image(image)
        self.dwarf = None
        self.source_dirs = list(source_dirs or [])
        src = getattr(image, "source", None)
        if image is not None and getattr(image, "fmt", "") == "elf" and src:
            from .dwarf import DwarfInfo
            self.dwarf = DwarfInfo.load(src, self.source_dirs)
        elif image is not None and src:
            # SDCC (mcs51) builds: line table + functions from the .cdb next to the .ihx/.hex (chipem/sdcc_cdb.py)
            from .sdcc_cdb import cdb_for_image, load_cdb
            cdb = cdb_for_image(image)
            if cdb is not None:
                self.dwarf = load_cdb(cdb, self.source_dirs)
        self.data_syms: dict[str, tuple[int, int]] = self._load_data_symbols(image)
        self.bps: dict[int, Breakpoint] = {}
        self._ids = ids or self._own_ids
        self._next = 1
        self._inext = -1                                      # internal breakpoints get negative ids
        self.last_hit: tuple[list[int], dict] | None = None   # (breakpoint ids, core info) of the last stop
        self._cs = None
        self._fpu = bool(getattr(getattr(chip.desc, "core", None), "fpu", False)) if hasattr(chip, "desc") else False
        self.v6m = bool(getattr(chip.arch, "v6m", False))

    def _own_ids(self) -> int:
        n = self._next
        self._next += 1
        return n

    # ---------------------------------------------------------------------------------------------- symbols
    @staticmethod
    def _load_data_symbols(image: Any) -> dict[str, tuple[int, int]]:
        """name -> (addr, size) of every function / object / label symbol (ELF symtab, else the image's dict)."""
        out: dict[str, tuple[int, int]] = {}
        if image is None:
            return out
        src = getattr(image, "source", None)
        thumb = getattr(image, "unit_bytes", 1) == 1
        if getattr(image, "fmt", "") == "elf" and src:
            try:
                from elftools.elf.elffile import ELFFile
                from elftools.elf.sections import SymbolTableSection
                with open(src, "rb") as f:
                    elf = ELFFile(f)
                    for sec in elf.iter_sections():
                        if not isinstance(sec, SymbolTableSection):
                            continue
                        for s in sec.iter_symbols():
                            t = s["st_info"]["type"]
                            if not s.name or t not in ("STT_FUNC", "STT_OBJECT", "STT_NOTYPE"):
                                continue
                            if s["st_shndx"] == "SHN_UNDEF":
                                continue
                            a = int(s["st_value"])
                            if t == "STT_FUNC" and thumb:
                                a &= ~1
                            if s.name.startswith("$"):
                                continue
                            out.setdefault(s.name, (a, int(s["st_size"])))
            except Exception:
                pass
        for n, a in (getattr(image, "symbols", None) or {}).items():
            try:
                out.setdefault(str(n), (int(a), 0))
            except (TypeError, ValueError):
                continue
        return out

    def symbol_addr(self, name: str) -> int | None:
        s = self.data_syms.get(name)
        if s is None and not name.startswith("_"):
            s = self.data_syms.get("_" + name)                # TI COFF / C28x: C names carry a leading underscore
        return None if s is None else s[0]

    def describe(self, addr: int) -> str:
        """"main+0x4 (blinky.c:12)" / "main+0x4" / "0x000004AE"."""
        name = self.symbols.name(addr, guess=False) if self.symbols.syms else None
        if name is None or name.startswith("0x"):
            fn = self.dwarf.function_for(addr) if self.dwarf else None
            name = (f"{fn.name}+0x{addr - fn.lo:x}" if addr != fn.lo else fn.name) if fn else self.fmt_addr(addr)
        li = self.dwarf.line_for(addr) if self.dwarf else None
        return f"{name} ({li.basename}:{li.line})" if li and li.line else name

    def fmt_addr(self, a: int) -> str:
        return f"0x{a:06X}" if self.c28x else f"0x{a:08X}"

    # ---------------------------------------------------------------------------------------------- variables
    # (chipem/dwarfvars.py: DWARF locals / globals / typed expressions; item shape in its docstring)
    def frame_vars(self, level: int = 0) -> dict:
        from .dwarfvars import frame_vars
        return frame_vars(self, level)

    def globals_list(self, q: str | None = None, limit: int = 500) -> list[dict]:
        from .dwarfvars import globals_list
        return globals_list(self, q, limit)

    def evaluate(self, expr: str, level: int = 0) -> dict:
        from .dwarfvars import evaluate
        return evaluate(self, expr, level)

    # ---------------------------------------------------------------------------------------------- locations
    def eval_expr(self, text: str | ast.Expression, *, values: bool = True) -> int:
        """Evaluate a debugger expression (compile_expr). `values`: a data symbol stands for its value (conditions,
        as in gdb); False: for its address (locations, memory view)."""
        tree = compile_expr(text) if isinstance(text, str) else text
        return int(self._ev(tree.body, values))

    def _ev(self, n: ast.AST, values: bool) -> int:
        if isinstance(n, ast.Constant):
            if isinstance(n.value, bool) or not isinstance(n.value, int):
                raise DebugError(f"not an integer: {n.value!r}")
            return n.value
        if isinstance(n, ast.Name):
            return self._name(n.id, values)
        if isinstance(n, ast.BinOp):
            return _BIN[type(n.op)](self._ev(n.left, values), self._ev(n.right, values))
        if isinstance(n, ast.UnaryOp):
            v = self._ev(n.operand, values)
            if isinstance(n.op, ast.Not):
                return int(not v)
            if isinstance(n.op, ast.Invert):
                return ~v
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.BoolOp):
            if isinstance(n.op, ast.And):
                return int(all(self._ev(v, values) for v in n.values))
            return int(any(self._ev(v, values) for v in n.values))
        if isinstance(n, ast.Compare):
            left = self._ev(n.left, values)
            for op, c in zip(n.ops, n.comparators):
                right = self._ev(c, values)
                if not _CMP[type(op)](left, right):
                    return 0
                left = right
            return 1
        if isinstance(n, ast.IfExp):
            return self._ev(n.body if self._ev(n.test, values) else n.orelse, values)
        if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name):
            width = {"mem8": 1, "mem16": 2, "mem32": 4, "mem": 4}.get(n.value.id)
            if width is None:
                raise DebugError(f"unknown memory accessor {n.value.id!r} (mem8/mem16/mem32)")
            a = self._ev(n.slice, True)
            return self.read_uint(a, width)
        if isinstance(n, ast.Call):
            arg = n.args[0]
            if not isinstance(arg, ast.Name):
                raise DebugError("addr() takes a symbol name")
            a = self.symbol_addr(arg.id)
            if a is None:
                raise DebugError(f"unknown symbol {arg.id!r}")
            return a
        raise DebugError(f"unsupported expression element {type(n).__name__}")

    def _name(self, name: str, values: bool) -> int:
        low = name.lower()
        if low in self.reg_names() or low in ("ip", "fp", "ah", "al", "ph", "pl", "t", "tl") \
                or re.fullmatch(r"(r\d+|s\d+|ar\d|xar\d|r\dh)", low):
            try:
                v = self.core.read_reg(low)
            except Exception:
                v = None
            if v is not None:
                # a location (memory view, watch): an arch with several address spaces tags the register's space
                # (MCS-51: sp -> IDATA, dptr -> XDATA); a value (condition) stays the plain register value
                ra = getattr(self.ad, "reg_address", None) if not values else None
                return ra(low, v) if ra is not None else v
        s = self.data_syms.get(name) or (self.data_syms.get("_" + name) if not name.startswith("_") else None)
        if s is None:
            raise DebugError(f"unknown register or symbol {name!r}")
        a, size = s
        if not values or size not in (1, 2, 4) or self.is_code(a):
            return a
        return self.read_uint(a, size if not self.c28x else max(1, size))

    def is_code(self, a: int) -> bool:
        if self.dwarf and self.dwarf.function_for(a) is not None:
            return True
        r = self.symbols.find(a)
        return r is not None and r[1] == 0

    def resolve_location(self, text: str) -> list[tuple[int, str]]:
        """Breakpoint location -> [(addr, label)]: `*EXPR` (exact address), `0x4ac`, `FUNC` (after its prologue when
        line info exists), `FUNC+OFF`, `FILE:LINE`, `LINE` (in the file of the current PC)."""
        t = str(text).strip()
        if not t:
            raise DebugError("missing location")
        if t.startswith("*"):
            a = self.eval_expr(t[1:], values=False)
            return [(self.norm_code(a), self.describe(self.norm_code(a)))]
        m = re.fullmatch(r"(.+?):(\d+)", t)
        if m and not re.fullmatch(r"0x[0-9a-fA-F]+", t):
            if self.dwarf is None:
                raise DebugError(f"{t}: no DWARF line info in this image (use a symbol or an address)")
            addrs = self.dwarf.addresses_for(m.group(1), int(m.group(2)))
            if not addrs:
                if not self.dwarf.match_file(m.group(1)):
                    raise DebugError(f"no source file {m.group(1)!r} in the line info "
                                     f"(files: {', '.join(os.path.basename(f) for f in self.dwarf.files[:12])})")
                raise DebugError(f"no code at {t}")
            return [(a, self.describe(a)) for a in addrs]
        if re.fullmatch(r"\d+", t) and self.dwarf is not None:
            li = self.dwarf.line_for(self.core.pc)
            if li is None:
                raise DebugError(f"line {t}: the PC has no line info (use FILE:LINE)")
            return self.resolve_location(f"{li.file}:{t}")
        m = re.fullmatch(r"([A-Za-z_.$][\w.$]*)", t)
        if m and self.dwarf is not None:
            fn = self.dwarf.function_named(t) or (self.dwarf.function_named("_" + t) if not t.startswith("_") else None)
            if fn is not None:
                a = self.dwarf.prologue_end(fn)
                return [(a, self.describe(a))]
        a = self.eval_expr(t, values=False)
        a = self.norm_code(a)
        return [(a, self.describe(a))]

    def norm_code(self, a: int) -> int:
        if self.ad is not None:
            return a & self.ad.code_mask
        return a & 0x3FFFFF if self.c28x else a & ~1

    def resolve_watch(self, text: str, size: int | None = None) -> tuple[int, int, str]:
        """Watch target -> (lo, hi_exclusive, label): `ADDR[:LEN]`, `SYMBOL` (its size), `PERIPH.REG` (MMIO register
        by name, e.g. USART1.SR, PORTC.PCR6), any expression giving an address."""
        t = str(text).strip()
        m = re.fullmatch(r"(.+?):(\d+|0x[0-9a-fA-F]+)", t)
        if m:
            t, size = m.group(1), int(m.group(2), 0)
        m = re.fullmatch(r"([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)", t)
        if m:
            blk = self.chip.peripherals.get(m.group(1)) or self.chip.peripherals.get(m.group(1).upper())
            if blk is not None:
                regs = getattr(blk, "regs", {}) or {}
                r = regs.get(m.group(2)) or regs.get(m.group(2).upper())
                if r is None:
                    raise DebugError(f"{blk.name} has no register {m.group(2)!r} "
                                     f"(registers: {', '.join(list(regs)[:20])}{' ...' if len(regs) > 20 else ''})")
                lo = blk.base + r.offset
                return lo, lo + (size or r.width), f"{blk.name}.{r.name}"
        s = self.data_syms.get(t) or (self.data_syms.get("_" + t) if not t.startswith("_") else None)
        if s is not None:
            lo, sz = s
            n = size or sz or (1 if self.c28x else 4)
            return lo, lo + n, t
        lo = self.eval_expr(t, values=False)
        n = size or (1 if self.c28x else 4)
        return lo, lo + n, self.fmt_addr(lo)

    # ---------------------------------------------------------------------------------------------- breakpoints
    def add_break(self, location: str, *, temp: bool = False, cond: str | None = None, ignore: int = 0,
                  internal: bool = False, frame: tuple | None = None, addr: int | None = None) -> list[Breakpoint]:
        locs = [(self.norm_code(addr), self.describe(addr))] if addr is not None else self.resolve_location(location)
        fn = compile_expr(cond) if cond else None
        out = []
        for a, label in locs:
            if internal:
                bid, self._inext = self._inext, self._inext - 1
            else:
                bid = self._ids()
            bp = Breakpoint(bid, "break", a, location=str(location or label), temp=temp, cond=cond or None,
                            ignore=int(ignore), internal=internal, frame=frame, chip=self.cid, label=label,
                            _cond_fn=fn)
            self.bps[bp.id] = bp
            out.append(bp)
        self.sync()
        return out

    def add_watch(self, target: str, access: str = "w", *, size: int | None = None, cond: str | None = None,
                  temp: bool = False, ignore: int = 0) -> Breakpoint:
        acc = {"w": "w", "write": "w", "r": "r", "read": "r", "rw": "rw", "a": "rw", "access": "rw",
               "wr": "rw"}.get(str(access).lower())
        if acc is None:
            raise DebugError(f"bad watch access {access!r} (r, w or rw)")
        lo, hi, label = self.resolve_watch(target, size)
        fn = compile_expr(cond) if cond else None
        bp = Breakpoint(self._ids(), "watch", lo, end=hi, access=acc, location=str(target), temp=temp,
                        cond=cond or None, ignore=int(ignore), chip=self.cid, label=label, _cond_fn=fn)
        self.bps[bp.id] = bp
        self.sync()
        return bp

    def delete(self, bid: int) -> Breakpoint:
        bp = self.bps.pop(int(bid), None)
        if bp is None:
            raise DebugError(f"no breakpoint {bid}")
        self.sync()
        return bp

    def enable(self, bid: int, on: bool = True) -> Breakpoint:
        bp = self.bps.get(int(bid))
        if bp is None:
            raise DebugError(f"no breakpoint {bid}")
        bp.enabled = bool(on)
        self.sync()
        return bp

    def modify(self, bid: int, *, cond: str | None | type(...) = ..., ignore: int | None = None) -> Breakpoint:
        bp = self.bps.get(int(bid))
        if bp is None:
            raise DebugError(f"no breakpoint {bid}")
        if cond is not ...:
            bp._cond_fn = compile_expr(cond) if cond else None
            bp.cond = cond or None
        if ignore is not None:
            bp.ignore = int(ignore)
        return bp

    def clear(self, *, internal_only: bool = False) -> None:
        for k in [k for k, b in self.bps.items() if b.internal or not internal_only]:
            del self.bps[k]
        self.sync()

    def user_bps(self) -> list[Breakpoint]:
        return sorted((b for b in self.bps.values() if not b.internal), key=lambda b: b.id)

    def sync(self) -> None:
        """Push the enabled breakpoints / watchpoints to the core (nothing hooked when there are none)."""
        core = self.core
        on = [b for b in self.bps.values() if b.enabled]
        try:
            core.set_breakpoints(sorted({b.addr for b in on if b.kind == "break"}))
            core.set_watchpoints([(b.addr, b.end, b.access) for b in on if b.kind == "watch"])
        except NotImplementedError:
            if on:
                raise DebugError(f"the {type(core).__name__} core has no breakpoint support") from None
        core.debug_hook = self._hook if on else None

    def _hook(self, kind: str, info: dict) -> bool:
        """Called by the core at a breakpoint address / watched access: True = stop."""
        hit: list[int] = []
        if kind == "break":
            a = info["addr"]
            cands = [b for b in self.bps.values() if b.enabled and b.kind == "break" and b.addr == a]
        else:
            a, n, acc = info["addr"], info.get("size", 1), info.get("access", "w")
            cands = [b for b in self.bps.values() if b.enabled and b.kind == "watch" and a < b.end
                     and a + n > b.addr and acc in b.access]
        for b in cands:
            if b.frame is not None and not self._frame_ok(b.frame):
                continue
            if b._cond_fn is not None:
                try:
                    if not self.eval_expr(b._cond_fn):
                        continue
                except Exception:
                    pass                          # a condition that cannot be evaluated stops (as gdb does)
            if not b.internal:
                b.hits += 1
                if b.ignore > 0:
                    b.ignore -= 1
                    continue
            hit.append(b.id)
        if hit:
            self.last_hit = (hit, dict(info, kind=kind))
            return True
        return False

    def _frame_ok(self, frame: tuple) -> bool:
        op, v0 = frame
        if op == "cfa_ge":
            cfa = self.cfa()
            return cfa is None or cfa >= v0
        sp = self.core.read_reg("sp")
        return {"ge": sp >= v0, "le": sp <= v0, "lt": sp < v0, "gt": sp > v0}[op]

    def cfa(self, pc: int | None = None) -> int | None:
        """Canonical frame address of the current frame from DWARF CFI (ARM), else None."""
        if self.c28x or self.dwarf is None or not self.dwarf._cfi:
            return None
        pc = self.core.pc if pc is None else pc
        rules = self.dwarf.unwind_rules(pc)
        if rules is None or rules[0] is None:
            return None
        try:
            return (self.core.read_reg(_ARM_DW.get(rules[0].reg, "sp")) + rules[0].offset) & 0xFFFFFFFF
        except Exception:
            return None

    def after_stop(self, reason: Any) -> list[Breakpoint]:
        """At the end of a run that did not just run out of time: drop internal breakpoints and temporary ones that
        stopped it. Returns the user breakpoints that caused the stop."""
        hit = []
        if reason is StopReason.DEBUG and self.last_hit is not None:
            hit = [self.bps[i] for i in self.last_hit[0] if i in self.bps]
        changed = False
        for b in list(self.bps.values()):
            if b.internal or (b.temp and b in hit):
                del self.bps[b.id]
                changed = True
        if changed:
            self.sync()
        return [b for b in hit if not b.internal]

    def has_internal(self) -> bool:
        return any(b.internal for b in self.bps.values())

    # ---------------------------------------------------------------------------------------------- registers
    def reg_names(self) -> list[str]:
        if self.ad is not None:
            return list(self.ad.reg_names())
        if self.c28x:
            names = list(getattr(self.chip.arch, "reg_names", ()) or ())
            if getattr(self.core, "fpu", False):
                names += [f"r{i}h" for i in range(8)] + ["stf"]
            return names
        names = list(ARM_REGS)
        if self.v6m:
            names = [n for n in names if n not in ("basepri", "faultmask")]
        if self._fpu:
            names += ["fpscr"] + [f"s{i}" for i in range(32)]
        return names

    def registers(self) -> list[tuple[str, int]]:
        names = self.reg_names()
        try:
            vals = self.core.read_regs(names)
        except Exception:
            vals = []
            for n in names:
                try:
                    vals.append(self.core.read_reg(n))
                except Exception:
                    vals.append(0)
        return list(zip(names, vals))

    def set_register(self, name: str, value: int) -> int:
        n = name.strip().lower()
        if n not in self.reg_names() and n not in ("ip", "fp", "al", "ah", "pl", "ph", "t", "tl") \
                and not re.fullmatch(r"(ar\d|r\dh|s\d+)", n):
            raise DebugError(f"unknown register {name!r}")
        if n == "pc":
            self.core.pc = int(value)
            self.core.bp_skip = None
        else:
            self.core.write_reg(n, int(value))
        return self.core.read_reg(n)

    def pc(self) -> int:
        return self.core.pc

    # ---------------------------------------------------------------------------------------------- memory
    def read_memory(self, addr: int, n: int) -> tuple[bytes, list[bool]]:
        """`n` address units at `addr` without side effects: memory from the core, MMIO through the block's peek
        (stored value where a read would have side effects). Returns (bytes, exact per unit); unmapped -> DebugError."""
        bus = self.chip.bus
        from .bus import MemoryRegion
        out = bytearray()
        exact: list[bool] = []
        a, end = addr, addr + n
        ub = self.ub
        while a < end:
            r = bus.lookup(a)
            if isinstance(r, MemoryRegion):
                k = min(end, r.end) - a
                out += self.core.read_mem(a, k)
                exact += [True] * k
                a += k
                continue
            if r is None or not hasattr(r, "peek_bytes"):
                if r is not None and hasattr(r, "mem"):           # a model without peek: its stored bytes
                    o = a - r.base
                    out += bytes(r.mem[o * ub:(o + 1) * ub]) if o * ub < len(r.mem) else b"\0" * ub
                    exact.append(False)
                    a += 1
                    continue
                if a == addr:
                    raise DebugError(f"cannot read {self.fmt_addr(a)}: unmapped")
                break
            k = min(end, r.base + r.size) - a
            data, ex = r.peek_bytes(a - r.base, k)
            out += data
            exact += ex
            a += k
        return bytes(out), exact

    def read_uint(self, addr: int, nbytes: int) -> int:
        units = max(1, (nbytes + self.ub - 1) // self.ub)
        data, _ = self.read_memory(addr, units)
        return int.from_bytes(data[:nbytes], "little")

    def write_memory(self, addr: int, data: bytes) -> None:
        """Debugger write: memory directly; MMIO registers through the bus (their write side effects apply)."""
        from .bus import MemoryRegion
        bus = self.chip.bus
        ub = self.ub
        if len(data) % ub:
            raise DebugError(f"write length must be a multiple of {ub} bytes")
        i = 0
        while i < len(data):
            a = addr + i // ub
            r = bus.lookup(a)
            if isinstance(r, MemoryRegion):
                k = min(len(data) - i, (r.end - a) * ub)
                self.core.write_mem(a, data[i:i + k])
                i += k
                continue
            if r is None:
                raise DebugError(f"cannot write {self.fmt_addr(a)}: unmapped")
            w = 4 if ub == 1 and len(data) - i >= 4 and a % 4 == 0 else (2 if ub == 1 and len(data) - i >= 2
                                                                         and a % 2 == 0 else 1)
            units = w if ub == 1 else 1
            bus.write(a, units, int.from_bytes(data[i:i + units * ub], "little"))
            i += units * ub
        self.core.flush_code_cache()

    # ---------------------------------------------------------------------------------------------- disassembly
    def _capstone(self):
        if self._cs is None:
            import capstone
            self._cs = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
            self._cs.detail = True
        return self._cs

    def decode(self, addr: int) -> dict | None:
        """One instruction: {addr, size (units), bytes (hex), text, call (bool), target (int|None)}."""
        if self.ad is not None:
            return self.ad.decode(addr)
        if self.c28x:
            from .core.c28x.asm import disasm_one
            try:
                ws = list(struct.unpack("<2H", self.core.read_mem(addr, 2)))
            except Exception:
                return None
            n, text, e = disasm_one(ws, addr)
            sem = getattr(e, "sem", None)
            target = None
            m = re.search(r"#?0x([0-9A-Fa-f]+)", text) if sem in ("lc", "lcr", "ffc") else None
            if m:
                target = int(m.group(1), 16)
            elif sem in ("lc", "lcr", "ffc"):
                m = re.search(r"\*XAR(\d)", text)
                if m:
                    target = self.core.read_reg(f"xar{m.group(1)}") & 0x3FFFFF
            return {"addr": addr, "size": n, "bytes": " ".join(f"{w:04x}" for w in ws[:n]), "text": text,
                    "call": sem in ("lc", "lcr", "ffc"), "target": target,
                    "ret": sem in ("lret", "lretr", "iret")}
        try:
            code = self.core.read_mem(addr & ~1, 4)
        except Exception:
            return None
        ins = next(self._capstone().disasm(code, addr & ~1, 1), None)
        if ins is None:
            return {"addr": addr, "size": 2, "bytes": code[:2].hex(), "text": ".short 0x%04x" %
                    int.from_bytes(code[:2], "little"), "call": False, "target": None, "ret": False}
        import capstone.arm as ca
        call = ins.id in (ca.ARM_INS_BL, ca.ARM_INS_BLX)
        target = None
        if call:
            op = ins.operands[0] if ins.operands else None
            if op is not None and op.type == ca.ARM_OP_IMM:
                target = op.imm & ~1
            elif op is not None and op.type == ca.ARM_OP_REG:
                try:
                    target = self.core.read_reg(ins.reg_name(op.reg).lower().replace("sb", "r9").replace(
                        "sl", "r10")) & ~1
                except Exception:
                    target = None
        return {"addr": ins.address, "size": ins.size, "bytes": ins.bytes.hex(),
                "text": f"{ins.mnemonic} {ins.op_str}".strip(), "call": call, "target": target, "ret": False}

    def disassemble(self, addr: int, count: int) -> list[dict]:
        out = []
        a = addr
        for _ in range(max(0, count)):
            d = self.decode(a)
            if d is None:
                break
            out.append(d)
            a += d["size"]
        return out

    def _start_before(self, pc: int, before: int) -> int:
        """An instruction boundary about `before` instructions above `pc` (decoding forward from the function start
        or from a few candidate start points until one lands exactly on pc)."""
        if before <= 0:
            return pc
        fn = None
        if self.dwarf:
            fn = self.dwarf.function_for(pc)
        lo = fn.lo if fn is not None else None
        if lo is None:
            r = self.symbols.find(pc)
            if r is not None:
                lo = pc - r[1]
        step = 1 if self.c28x or self.ad is not None else 2
        span = before * (2 if self.c28x else (3 if self.ad is not None else 4))
        cands = []
        if lo is not None and pc - lo <= 4 * span:
            cands.append(lo)
        cands += [pc - span - k * step for k in range(4)]
        for s in cands:
            if s < 0:
                continue
            seq = []
            a = s
            ok = False
            for _ in range(4 * before + 8):
                if a == pc:
                    ok = True
                    break
                if a > pc:
                    break
                d = self.decode(a)
                if d is None:
                    break
                seq.append(a)
                a += d["size"]
            if ok:
                return seq[-before] if len(seq) >= before else (seq[0] if seq else pc)
        return pc

    def listing(self, addr: int | None = None, before: int = 8, after: int = 24, mode: str = "mixed") -> dict:
        """Rows around `addr` (default PC). mode "asm": instructions; "mixed": source lines interleaved where the
        line changes; "source": the source file around the current line (gutter: has_code, bps). Every asm row:
        {kind: "asm", addr, size, bytes, text, sym (label at a function start), bp: [ids], pc: bool, line}; src
        rows: {kind: "src", file, line, text}. Also: pc, file, line, func, has_dwarf, has_source."""
        pc = self.core.pc
        if addr is None:
            addr = pc
        li = self.dwarf.line_for(addr) if self.dwarf else None
        fn = self.dwarf.function_for(addr) if self.dwarf else None
        res = {"pc": pc, "addr": addr, "file": li.file if li else None, "line": li.line if li else None,
               "func": fn.name if fn else (self.symbols.function_of(addr) if self.symbols.syms else None),
               "has_dwarf": self.dwarf is not None, "mode": mode, "rows": [], "chip": self.cid,
               "has_source": bool(li and self.dwarf.source_lines(li.file) is not None)}
        bpmap: dict[int, list[int]] = {}
        for b in self.bps.values():
            if b.kind == "break" and not b.internal:
                bpmap.setdefault(b.addr, []).append(b.id)
        if mode == "source":
            if li is None:
                mode = res["mode"] = "mixed"
            else:
                lines = self.dwarf.source_lines(li.file)
                if lines is None:
                    mode = res["mode"] = "mixed"
                else:
                    lo = max(1, li.line - max(before, 4) * 2)
                    hi = min(len(lines), li.line + max(after, 8))
                    code_lines = {ln for (f, ln) in self.dwarf.by_line if f == li.file}
                    bp_lines: dict[int, list[int]] = {}
                    for a, ids in bpmap.items():
                        l2 = self.dwarf.line_for(a)
                        if l2 is not None and l2.file == li.file:
                            bp_lines.setdefault(l2.line, []).extend(ids)
                    pcl = self.dwarf.line_for(pc)
                    for n in range(lo, hi + 1):
                        res["rows"].append({"kind": "src", "file": li.file, "line": n, "text": lines[n - 1],
                                            "code": n in code_lines, "bp": bp_lines.get(n, []),
                                            "pc": bool(pcl and pcl.file == li.file and pcl.line == n)})
                    res["path"] = self.dwarf.find_source(li.file)
                    return res
        start = self._start_before(addr, before)
        rows = []
        last_line = None
        src_cache: dict[str, list[str] | None] = {}
        for d in self.disassemble(start, before + after + 1):
            a = d["addr"]
            l2 = self.dwarf.line_for(a) if self.dwarf else None
            if mode == "mixed" and l2 is not None and l2.line and (last_line is None
                                                                    or (l2.file, l2.line) != last_line):
                lines = src_cache.get(l2.file)
                if l2.file not in src_cache:
                    lines = src_cache[l2.file] = self.dwarf.source_lines(l2.file)
                text = lines[l2.line - 1] if lines and 0 < l2.line <= len(lines) else ""
                rows.append({"kind": "src", "file": l2.file, "line": l2.line, "text": text,
                             "base": os.path.basename(l2.file)})
                last_line = (l2.file, l2.line)
            sym = None
            r = self.symbols.find(a) if self.symbols.syms else None
            if r is not None and r[1] == 0:
                sym = r[0]
            elif self.dwarf:
                f2 = self.dwarf.function_for(a)
                if f2 is not None and f2.lo == a:
                    sym = f2.name
            rows.append({"kind": "asm", **d, "sym": sym, "bp": bpmap.get(a, []), "pc": a == pc,
                         "line": l2.line if l2 else None})
        res["rows"] = rows
        return res

    # ---------------------------------------------------------------------------------------------- call stack
    def backtrace(self, max_frames: int = 32) -> list[dict]:
        """[{level, pc, func, file, line, sp, how}] innermost first. ARM: DWARF CFI (.debug_frame) where it exists,
        hardware exception frames (EXC_RETURN in LR), else a stack scan for return addresses after BL/BLX (as the
        shell's `bt`; how = "scan", a guess). C28x: PC, then RPC (LCR return address), then a stack scan. Arch
        support (MCS-51): its own stack walk."""
        if self.ad is not None:
            return [self._frame(i, pc, sp, how) for i, (pc, sp, how) in enumerate(self.ad.backtrace(max_frames))]
        if self.c28x:
            return self._bt_c28x(max_frames)
        core = self.core
        regs = dict(zip([f"r{i}" for i in range(13)] + ["sp", "lr", "pc"],
                        core.read_regs([f"r{i}" for i in range(13)] + ["sp", "lr", "pc"])))
        frames = [self._frame(0, regs["pc"], regs["sp"], "pc")]
        cfi_ok = self.dwarf is not None and self.dwarf._cfi
        if frames[0]["func"] in ("main", "Reset_Handler") and regs["lr"] < EXC_RETURN_MIN:
            return frames
        seen = set()
        level = 1
        while level < max_frames:
            pc, sp, lr = regs["pc"], regs["sp"], regs["lr"]
            key = (pc, sp)
            if key in seen:
                break
            seen.add(key)
            nxt = None
            if level == 1 and lr >= EXC_RETURN_MIN and not cfi_ok:
                nxt = self._exc_frame(self._guess_handler_sp(regs, lr), lr)
            elif cfi_ok:
                nxt = self._cfi_step(regs, level == 1)
            if nxt is None:
                break
            regs, how = nxt
            if regs["pc"] in (0, 0xFFFFFFFE) or regs["pc"] & ~1 == 0:
                break
            frames.append(self._frame(level, regs["pc"], regs["sp"], how))
            level += 1
            if frames[-1]["func"] in ("main", "Reset_Handler", "_c_int00"):
                break                                     # outermost frame (gdb: backtrace past-main off)
        if len(frames) == 1 and not cfi_ok:
            frames += self._scan_frames(regs, max_frames)
        return frames

    def _frame(self, level: int, pc: int, sp: int, how: str) -> dict:
        pc = pc & ~1 if not self.c28x and self.ad is None else pc
        li = self.dwarf.line_for(pc if level == 0 else max(pc - 1, 0)) if self.dwarf else None
        fn = self.dwarf.function_for(pc if level == 0 else max(pc - 1, 0)) if self.dwarf else None
        name = fn.name if fn else (self.symbols.function_of(pc) if self.symbols.syms or not self.c28x else None)
        return {"level": level, "pc": pc, "sp": sp, "func": name or "??", "file": li.file if li else None,
                "base": os.path.basename(li.file) if li else None, "line": li.line if li else None, "how": how,
                "desc": self.describe(pc)}

    def _exc_frame(self, regs: dict, exc_return: int) -> tuple[dict, str] | None:
        """Unwind a hardware exception frame: r0-r3, r12, lr, pc, xpsr (+ FP context when EXC_RETURN bit 4 = 0)."""
        core = self.core
        try:
            use_psp = bool(exc_return & 4)
            sp = core.read_reg("psp") if use_psp else regs["sp"]     # handler mode runs on MSP: the unwound SP
            w = struct.unpack("<8I", self.read_memory(sp, 32)[0])
        except Exception:
            return None
        size = 0x20 if exc_return & 0x10 else 0x68
        if w[7] & 0x200:
            size += 4
        new = dict(regs)
        new.update(r0=w[0], r1=w[1], r2=w[2], r3=w[3], r12=w[4], lr=w[5], pc=w[6] & ~1, sp=sp + size)
        return new, "exception"

    def _guess_handler_sp(self, regs: dict, exc_return: int) -> dict:
        """No CFI: the hardware frame starts right above the EXC_RETURN value the handler pushed (push {.., lr}
        stores LR highest), or at SP for a handler that pushed nothing."""
        out = dict(regs)
        if exc_return & 4:
            return out
        sp = regs["sp"]
        try:
            data = self.read_memory(sp, 64 * 4)[0]
        except Exception:
            return out
        for i in range(len(data) // 4):
            if int.from_bytes(data[4 * i:4 * i + 4], "little") == exc_return:
                out["sp"] = sp + 4 * i + 4
                break
        return out

    def _cfi_step(self, regs: dict, innermost: bool) -> tuple[dict, str] | None:
        pc = regs["pc"]
        lr = regs["lr"]
        rules = self.dwarf.unwind_rules(pc if innermost else max(pc - 1, 0))
        if rules is None:
            if lr >= EXC_RETURN_MIN:
                return self._exc_frame(regs, lr)
            if innermost and lr and self.chip.bus.lookup(lr & ~1) is not None:
                new = dict(regs)
                new["pc"] = lr & ~1
                return new, "lr"
            return None
        cfa_rule, rr = rules
        try:
            base = regs[_ARM_DW.get(cfa_rule.reg, "sp")]
            cfa = (base + cfa_rule.offset) & 0xFFFFFFFF
        except Exception:
            return None
        new = dict(regs)
        restored_lr = False
        for regno, rule in rr.items():
            name = _ARM_DW.get(regno)
            if name is None:
                continue
            if rule.type == "OFFSET":
                try:
                    new[name] = int.from_bytes(self.read_memory(cfa + rule.arg, 4)[0], "little")
                except Exception:
                    return None
                if name == "lr":
                    restored_lr = True
        ret = new["lr"]
        if not restored_lr and not innermost:
            return None
        new["sp"] = cfa
        if ret >= EXC_RETURN_MIN:
            new["lr"] = ret
            regs2 = dict(new)
            ex = self._exc_frame(regs2, ret)
            return ex
        new["pc"] = ret & ~1
        return new, "cfi"

    def _scan_frames(self, regs: dict, max_frames: int) -> list[dict]:
        """Heuristic: words on the stack that are odd, point into code and follow a BL/BLX (shell `bt`)."""
        out = []
        sp = regs["sp"]
        lr = regs["lr"]
        if lr < EXC_RETURN_MIN and lr & 1 and self._is_call_site(lr):
            out.append(self._frame(1, lr & ~1, sp, "lr"))
        bus = self.chip.bus
        from .bus import MemoryRegion
        r = bus.lookup(sp)
        if not isinstance(r, MemoryRegion):
            return out
        n = min(256, (r.end - sp) // 4)
        try:
            data = self.core.read_mem(sp, n * 4)
        except Exception:
            return out
        for i in range(n):
            if len(out) + 1 >= max_frames:
                break
            v = int.from_bytes(data[4 * i:4 * i + 4], "little")
            if not v & 1 or v >= EXC_RETURN_MIN or not self._is_call_site(v):
                continue
            if out and out[-1]["pc"] == v & ~1:
                continue
            out.append(self._frame(len(out) + 1, v & ~1, sp + 4 * i, "scan"))
        return out

    def _is_call_site(self, ret: int) -> bool:
        a = ret & ~1
        rr = self.chip.bus.lookup(a - 4)
        from .bus import MemoryRegion
        if not isinstance(rr, MemoryRegion) or rr.kind not in ("flash", "rom", "flexnvm", "ram"):
            return False
        try:
            b = self.core.read_mem(a - 4, 4)
        except Exception:
            return False
        hw1 = int.from_bytes(b[0:2], "little")
        hw2 = int.from_bytes(b[2:4], "little")
        if (hw1 & 0xF800) == 0xF000 and (hw2 & 0xD000) == 0xD000:
            return True
        return (hw2 & 0xFF87) == 0x4780

    def _bt_c28x(self, max_frames: int) -> list[dict]:
        core = self.core
        pc, sp, rpc = core.pc, core.read_reg("sp"), core.read_reg("rpc")
        frames = [self._frame(0, pc, sp, "pc")]
        if rpc and self._c28x_call_before(rpc):
            frames.append(self._frame(1, rpc, sp, "rpc"))
        try:
            data = self.core.read_mem(max(sp - 128, 0), min(128, sp))
        except Exception:
            return frames
        words = struct.unpack(f"<{len(data) // 2}H", data)
        lo = max(sp - 128, 0)
        for i in range(len(words) - 2, -1, -1):        # the stack grows up: newest below SP
            v = words[i] | (words[i + 1] << 16)
            if 0 < v < 0x400000 and self._c28x_call_before(v) and v != frames[-1]["pc"]:
                frames.append(self._frame(len(frames), v, lo + i, "scan"))
                if len(frames) >= max_frames:
                    break
        return frames

    def _c28x_call_before(self, ret: int) -> bool:
        for back in (2, 1):
            d = self.decode(ret - back)
            if d is not None and d["call"] and d["size"] == back:
                return True
        return False

    # ---------------------------------------------------------------------------------------------- stepping plans
    def _sp_frame(self, outer: bool) -> tuple:
        """Frame check of a breakpoint that must hit in this frame (outer=False) or an outer one (outer=True)."""
        sp = self.core.read_reg("sp")
        if self.c28x or (self.ad is not None and self.ad.stack_grows_up):     # stack grows up
            return ("lt", sp) if outer else ("le", sp)
        return ("ge", sp)

    def plan_nexti(self) -> str:
        d = self.decode(self.core.pc)
        if d is not None and d["call"]:
            self.add_break("", internal=True, addr=d["addr"] + d["size"], frame=self._sp_frame(False))
            return "run"
        return "step"

    def return_address(self) -> int | None:
        fn = self.dwarf.function_for(self.core.pc) if self.dwarf else None
        name = fn.name if fn else (self.symbols.function_of(self.core.pc) if self.symbols.syms else None)
        if name in ("main", "_main", "Reset_Handler", "_c_int00"):
            raise DebugError(f"\"finish\" not meaningful in the outermost frame ({name})")
        if self.ad is not None:
            return self.ad.return_address()
        if self.c28x:
            rpc = self.core.read_reg("rpc")
            return rpc if rpc and self._c28x_call_before(rpc) else None
        bt = self.backtrace(2)
        if len(bt) > 1:
            return bt[1]["pc"]
        return None

    def plan_finish(self) -> str:
        ret = self.return_address()
        if ret is None:
            raise DebugError("cannot determine the return address of the current function")
        self.add_break("", internal=True, addr=ret, frame=self._sp_frame(True) if not self.c28x else None)
        return "run"

    def plan_until(self, location: str) -> str:
        for a, _ in self.resolve_location(location):
            self.add_break("", internal=True, addr=a)
        return "run"

    def plan_next(self, into: bool = False) -> str:
        """Source-line step: internal breakpoints on every other line start of the current function, on the return
        address (outer frame) and, for `step` (into), on the post-prologue address of direct call targets that have
        line info. Without line info at the PC: like nexti / stepi."""
        pc = self.core.pc
        dw = self.dwarf
        li = dw.line_for(pc) if dw else None
        fn = dw.function_for(pc) if dw else None
        if li is None or fn is None:
            return "step" if into else self.plan_nexti()
        if into:
            d = self.decode(pc)
            if d is not None and d["call"] and d["target"] is not None:
                tf = dw.function_for(d["target"])
                if tf is not None:
                    self.add_break("", internal=True, addr=dw.prologue_end(tf))
        cfa0 = self.cfa()
        frame = ("cfa_ge", cfa0) if cfa0 is not None else None
        if frame is None and self.ad is not None and self.ad.stack_grows_up:
            # MCS-51 (no CFI): a line of this frame starts with SP at most one byte above the current SP (SDCC can
            # leave one saved byte on the stack across a call); a deeper call pushed at least its return address
            frame = ("le", self.core.read_reg("sp") + 1)
        cur = (li.file, li.line)
        addrs = set()
        for r in dw.function_lines(fn):
            if r.is_stmt and (r.file, r.line) != cur and r.line != 0:
                addrs.add(r.addr)
        if into:                                            # direct calls made by this line's code
            for lo, hi in dw.line_ranges(li.file, li.line):
                a = lo
                while lo <= a < hi:
                    d = self.decode(a)
                    if d is None:
                        break
                    if d["call"] and d["target"] is not None:
                        tf = dw.function_for(d["target"])
                        if tf is not None:
                            self.add_break("", internal=True, addr=dw.prologue_end(tf))
                    a += d["size"]
        for a in sorted(addrs):
            self.add_break("", internal=True, addr=a, frame=frame)
        try:
            ret = self.return_address()
        except DebugError:
            ret = None                                      # outermost frame (main): no return breakpoint
        if ret is not None:
            self.add_break("", internal=True, addr=ret, frame=self._sp_frame(True) if not self.c28x else None)
        return "run"

    # ---------------------------------------------------------------------------------------------- summary
    def stop_description(self, reason: Any) -> dict:
        core = self.core
        info = dict(core.stop_info) if isinstance(getattr(core, "stop_info", None), dict) else {}
        pc = core.pc
        d = {"chip": self.cid, "reason": getattr(reason, "name", str(reason)), "pc": pc, "where": self.describe(pc)}
        if reason is StopReason.DEBUG:
            d.update(type=info.get("type"), addr=info.get("addr"))
            d["bp"] = [i for i in (self.last_hit[0] if self.last_hit else []) if i > 0]
            if info.get("type") == "watchpoint":
                d.update(access=info.get("access"), size=info.get("size"), value=info.get("value"),
                         insn_pc=info.get("pc"))
                acc = {"r": "read", "w": "write"}.get(info.get("access"), info.get("access"))
                v = info.get("value")
                d["msg"] = (f"watchpoint {','.join(map(str, d['bp'])) or '?'}: {acc} "
                            f"{self.fmt_addr(info.get('addr') or 0)}" + (f" = 0x{v:X}" if v is not None else "")
                            + f" by {self.describe(info.get('pc') or pc)}")
            elif d["bp"]:
                d["msg"] = f"breakpoint {','.join(map(str, d['bp']))} at {self.describe(pc)}"
            else:
                d["msg"] = f"stopped at {self.describe(pc)}"
        return d


# ARM DWARF register numbers -> names (AAPCS DWARF: r0-r15 = 0-15)
_ARM_DW = {i: f"r{i}" for i in range(13)}
_ARM_DW.update({13: "sp", 14: "lr", 15: "pc"})


# ------------------------------------------------------------------------------------------------- session level
class SessionDebug:
    """Debugger state of a Session (Session.debug): one Debugger per chip, unique ids, the last stop."""

    def __init__(self, session: Any, source_dirs: list[str] | None = None):
        self.s = session
        self.source_dirs = list(source_dirs or getattr(session, "source_dirs", None) or [])
        self.dbgs: dict[str, Debugger] = {}
        self._next = 1
        self.last_stop: dict | None = None
        self.stops = 0                                 # increments at every debugger-visible stop (UIs poll it)

    def _id(self) -> int:
        n = self._next
        self._next += 1
        return n

    def chip_id(self, chip: str | None) -> str:
        if chip is None:
            return self.s.view
        return self.s._ctx(chip).id

    def dbg(self, chip: str | None = None) -> Debugger:
        cid = self.chip_id(chip)
        d = self.dbgs.get(cid)
        ctx = self.s.ctxs[cid]
        if d is None or d.chip is not ctx.chip:
            d = Debugger(ctx.chip, ctx.image, cid=cid, source_dirs=self.source_dirs, ids=self._id)
            self.dbgs[cid] = d
        return d

    def split(self, text: str) -> tuple[str | None, str]:
        """'B:main' / 'B.main' (a chip id prefix) -> ('B', 'main'); else (None, text)."""
        t = str(text).strip()
        ids = list(self.s.ctxs)
        if len(ids) > 1:
            m = re.match(r"^([A-Za-z0-9_]+)[:.](.+)$", t)
            if m and any(m.group(1).upper() == c.upper() for c in ids) and not re.fullmatch(r"\d+", m.group(2)):
                return m.group(1), m.group(2)
        return None, t

    def breakpoints(self) -> list[Breakpoint]:
        out = []
        for d in self.dbgs.values():
            out += d.user_bps()
        return sorted(out, key=lambda b: b.id)

    def find(self, bid: int) -> tuple[Debugger, Breakpoint]:
        for d in self.dbgs.values():
            if int(bid) in d.bps:
                return d, d.bps[int(bid)]
        raise DebugError(f"no breakpoint {bid}")

    def delete(self, bid: int) -> Breakpoint:
        d, _ = self.find(bid)
        return d.delete(bid)

    def enable(self, bid: int, on: bool = True) -> Breakpoint:
        d, _ = self.find(bid)
        return d.enable(bid, on)

    def modify(self, bid: int, **kw) -> Breakpoint:
        """Change the condition (`cond=`) or ignore count (`ignore=`) of breakpoint `bid`."""
        d, _ = self.find(bid)
        return d.modify(bid, **kw)

    def delete_all(self) -> int:
        n = len(self.breakpoints())
        for d in self.dbgs.values():
            d.clear()
        return n

    def add_break(self, location: str, *, chip: str | None = None, temp: bool = False, cond: str | None = None,
                  ignore: int = 0) -> list[Breakpoint]:
        c, loc = self.split(location)
        return self.dbg(chip or c).add_break(loc, temp=temp, cond=cond, ignore=ignore)

    def add_watch(self, target: str, access: str = "w", *, chip: str | None = None, size: int | None = None,
                  cond: str | None = None) -> Breakpoint:
        c, t = self.split(target)
        return self.dbg(chip or c).add_watch(t, access, size=size, cond=cond)

    # -- runs ------------------------------------------------------------------------------------------------
    def after_run(self, reason: Any, step: bool = False, chip: str | None = None) -> None:
        """Called by Session after every run / step: at a stop that is not just the end of a time window, drop
        internal breakpoints, record the stop (chip, breakpoint, board skew) and log it."""
        if reason is StopReason.COUNT and not step:
            return
        s = self.s
        cid = None
        if reason is StopReason.DEBUG:
            b = getattr(s, "board", None)
            cid = getattr(b, "last_stop_chip", None) if b is not None and len(s.ctxs) > 1 else None
            if cid is None:
                for k, ctx in s.ctxs.items():
                    if isinstance(getattr(ctx.chip.core, "stop_info", None), dict) and k in self.dbgs \
                            and self.dbgs[k].last_hit is not None:
                        cid = k
                        break
            cid = cid or s.view
        elif step:
            cid = chip
        for k, d in list(self.dbgs.items()):
            hit = d.after_stop(reason if k == cid else StopReason.USER)
            if k == cid:
                desc = d.stop_description(reason)
                desc["bp"] = [b.id for b in hit] or desc.get("bp", [])
                b = getattr(s, "board", None)
                if b is not None and len(s.ctxs) > 1:
                    desc["skew_ns"] = b.skew_ns()
                    desc["t_ns"] = b.now_ns()
                else:
                    desc["t_ns"] = d.chip.now_ns()
                self.last_stop = desc
                self.stops += 1
                d.last_hit = None
                chip = d.chip
                try:
                    chip.log.event("info", chip.clock.now_ns(), block="debug", msg=desc.get("msg", "debug stop"),
                                   pc=desc.get("pc"), bp=desc.get("bp"))
                except Exception:
                    pass
        if reason is not StopReason.DEBUG:
            cid = chip if chip in s.ctxs else s.view
            ctx = s.ctxs[cid]
            d = self.dbgs.get(cid)
            pc = ctx.chip.core.pc
            self.last_stop = {"reason": "STEP" if step else getattr(reason, "name", str(reason)), "chip": cid,
                              "pc": pc, "where": d.describe(pc) if d is not None else f"0x{pc:X}"}
            self.stops += 1

    def plan(self, kind: str, chip: str | None = None, location: str | None = None) -> str:
        """Prepare a step: kind stepi | nexti | next | step | finish | until. Returns "step" (the caller does one
        instruction step of that chip), "run" (continue until the next debugger stop) or "done"."""
        d = self.dbg(chip)
        d.clear(internal_only=True)
        if kind == "stepi":
            return "step"
        if kind == "nexti":
            return d.plan_nexti()
        if kind == "next":
            return d.plan_next(into=False)
        if kind == "step":
            return d.plan_next(into=True)
        if kind == "finish":
            return d.plan_finish()
        if kind == "until":
            if not location:
                return d.plan_next(into=False)
            c, loc = self.split(location)
            return self.dbg(c or chip).plan_until(loc)
        raise DebugError(f"unknown step kind {kind!r}")

    def run_until_stop(self, max_ns: float | None = None, chunk_ns: float = 20e6,
                       should_stop: Callable[[], bool] | None = None) -> Any:
        """Continue (the whole board) until a non-COUNT stop, `max_ns` of virtual time, or should_stop(). Used by the
        shell and the GDB server (the web Worker runs its own chunk loop)."""
        s = self.s
        t0 = self._now()
        while True:
            n = chunk_ns
            if max_ns is not None:
                left = max_ns - (self._now() - t0)
                if left <= 0:
                    return StopReason.COUNT
                n = min(n, left)
            r = s.run_for(n)
            if r is not StopReason.COUNT:
                return r
            if should_stop is not None and should_stop():
                self.after_run(StopReason.USER)
                return StopReason.USER

    def _now(self) -> float:
        b = getattr(self.s, "board", None)
        return b.now_ns() if b is not None else self.s.chip.clock.now_ns()

    def do(self, kind: str, chip: str | None = None, location: str | None = None, *, max_ns: float | None = None,
           should_stop: Callable[[], bool] | None = None, count: int = 1) -> Any:
        """Execute a step command to completion (shell / GDB): plan, then step or run until stop."""
        r = None
        for _ in range(max(1, int(count))):
            p = self.plan(kind, chip, location)
            if p == "step":
                r = self.s.step(self.chip_id(chip) if chip is not None else None)
            else:
                r = self.run_until_stop(max_ns, should_stop=should_stop)
            if r is not StopReason.COUNT and r is not StopReason.DEBUG and p != "step":
                break
            if p == "run" and r is StopReason.DEBUG and self.last_stop and self.last_stop.get("bp"):
                break                                    # a user breakpoint ended it
        return r
