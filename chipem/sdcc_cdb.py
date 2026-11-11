"""SDCC debug information (`<name>.cdb`) as a line table + function list for the debugger (chipem/debug.py).

SDCC 4.5 has no ELF/DWARF output for the mcs51 port (`--out-fmt-elf` is accepted by the hc08/s08/stm8 ports only),
so `sdcc --debug` builds are debugged from the CDB file the linker writes next to the `.ihx`:

    L:C$blinky.c$12$1_0$3:4A     C source line 12 of blinky.c starts at code address 004Ah
    L:G$main$0$0:40              global function main starts at 0040h  (L:F<module>$name...: static function)
    L:XG$main$0$0:6B             ... its last instruction (the RET) is at 006Bh   (L:XF...: static function)
    L:A$blinky$452:4A            line 452 of the generated blinky.asm (used only for modules without C lines)
    S:G$cnt$0_0$0({1}SC:U),E,0,0 symbol record: type, address space (E/G/H/B/R internal RAM, F/A/P external RAM,
    L:G$cnt$0_0$0:8              C/D code, I SFR, J SFR bit) -> chipem/firmware.py `sidecar_symbols` tags it

`load_cdb(path)` returns a `CdbInfo`, a `chipem.dwarf.DwarfInfo` built from these rows (no CFI: the MCS-51 call
stack is walked by `chipem/arch/mcs51/debug.py`), so `break main`, `break file.c:line`, `list`, mixed
source/assembly and `step`/`next` work as they do for ELF/DWARF.  Source files are found like DWARF's (next to the
CDB, its parent directory, `--source-dir`).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .dwarf import DwarfInfo, Function, LineInfo

__all__ = ["CdbInfo", "load_cdb", "cdb_for_image", "CDB_SPACES"]

_LINE_C = re.compile(r"^L:C\$(.+?)\$(\d+)\$[^:]*:([0-9A-Fa-f]+)\s*$")
_LINE_A = re.compile(r"^L:A\$(.+?)\$(\d+):([0-9A-Fa-f]+)\s*$")
_FUNC = re.compile(r"^L:(X?)(G|F[^$]*|L[^$]*)\$([A-Za-z_]\w*)\$[^:]*:([0-9A-Fa-f]+)\s*$")
_SYM = re.compile(r"^S:(G|F[^$]*|L[^$]*)\$([A-Za-z_]\w*)\$([^(]*)\((.*)\),([A-Z]),(\d+),(\d+)")
_FDEF = re.compile(r"^F:(G|F[^$]*)\$([A-Za-z_]\w*)\$")

# CDB address-space letters (SDCC "cdbfileformat": A external stack, B internal stack, C code, D code (static
# segment), E internal RAM (lower 128), F external RAM, G internal RAM, H bit-addressable, I SFR, J SFR bit,
# R register, Z none) -> MCS-51 tagged address base (docs/MCS51_CORE.md), None for spaces without a byte address
CDB_SPACES = {"A": 0x10000, "B": 0x20000, "C": 0x00000, "D": 0x00000, "E": 0x20000, "F": 0x10000, "G": 0x20000,
              "H": 0x20000, "I": 0x30000, "P": 0x10000, "R": None, "J": None, "Z": None}


class CdbInfo(DwarfInfo):
    """DwarfInfo built from an SDCC CDB file (see the module docstring)."""

    def prologue_end(self, fn: Function) -> int:
        """SDCC functions need no frame setup unless reentrant; the first row of a line after the function's
        opening line is where `break func` stops (gdb's "second line" rule would skip the first statement when it
        shares the address of the `{` line)."""
        rows = [r for r in self.function_lines(fn) if r.is_stmt]
        for r in rows:
            if fn.line is not None and r.line > fn.line:
                return r.addr
        return rows[0].addr if rows else fn.lo


def _modules_asm(cdb: Path, module: str) -> str:
    return str(cdb.parent / f"{module}.asm")


def load_cdb(path: str | os.PathLike, source_dirs: list[str] | None = None) -> CdbInfo | None:
    """Parse an SDCC .cdb file; None if it has no line or function records."""
    p = Path(path)
    try:
        text = p.read_text(errors="replace")
    except OSError:
        return None
    base = p.resolve().parent
    c_rows: list[tuple[int, str, int]] = []            # (addr, file, line)
    a_rows: dict[str, list[tuple[int, int]]] = {}      # module -> [(addr, asm line)]
    starts: dict[str, int] = {}
    ends: dict[str, int] = {}
    for line in text.splitlines():
        if not line.startswith("L:"):
            continue
        m = _LINE_C.match(line)
        if m:
            f = m.group(1)
            if not os.path.isabs(f):
                f = os.path.normpath(str(base / f))
            c_rows.append((int(m.group(3), 16), f, int(m.group(2))))
            continue
        m = _LINE_A.match(line)
        if m:
            a_rows.setdefault(m.group(1), []).append((int(m.group(3), 16), int(m.group(2))))
            continue
        m = _FUNC.match(line)
        if m and m.group(2)[:1] in ("G", "F"):
            (ends if m.group(1) else starts)[m.group(3)] = int(m.group(4), 16)
    # function list: a function has both a start label and an end (XG/XF) label
    funcs: list[Function] = []
    first_line: dict[str, tuple[str, int]] = {}
    for name, lo in starts.items():
        hi = ends.get(name)
        if hi is None or hi < lo:
            continue
        lines = sorted((a, ln, f) for a, f, ln in c_rows if lo <= a <= hi)
        if lines:
            first_line[name] = (lines[0][2], min(ln for a, ln, f in lines if a == lines[0][0]))
        fl, ln = first_line.get(name, (None, None))
        funcs.append(Function(name, lo, hi + 1, fl, ln))       # XG/XF: address of the last instruction (RET/RETI)
    # assembler-only modules (no C rows at all): their .asm lines
    c_files = {os.path.splitext(os.path.basename(f))[0] for _a, f, _l in c_rows}
    rows_in: list[tuple[int, str, int]] = list(c_rows)
    for mod, recs in a_rows.items():
        if mod in c_files:
            continue
        rows_in += [(a, _modules_asm(p, mod), ln) for a, ln in recs]
    if not rows_in and not funcs:
        return None
    rows_in.sort(key=lambda t: (t[0], t[2]))
    addrs = sorted({a for a, _f, _l in rows_in})
    nxt = {a: b for a, b in zip(addrs, addrs[1:])}
    fend = sorted((f.lo, f.hi) for f in funcs)
    rows: list[LineInfo] = []
    by_addr: dict[int, list[tuple[str, int]]] = {}
    for a, f, ln in rows_in:
        by_addr.setdefault(a, [])
        if (f, ln) not in by_addr[a]:
            by_addr[a].append((f, ln))
    for a, recs in by_addr.items():
        end = nxt.get(a)
        hi = next((h for lo, h in fend if lo <= a < h), None)
        if end is None or (hi is not None and end > hi):
            end = hi if hi is not None else a + 1
        # several lines at one address (e.g. `void main(void) {` and the first statement): the earlier ones get an
        # empty range (they stay breakpoint locations), the last one owns the code
        for f, ln in recs[:-1]:
            rows.append(LineInfo(a, f, ln, a, True))
        f, ln = recs[-1]
        rows.append(LineInfo(a, f, ln, max(end, a + 1), True))
    return CdbInfo(str(p), rows, funcs, None, source_dirs)


def cdb_for_image(image) -> Path | None:
    """The `<stem>.cdb` next to an Intel HEX image (SDCC writes it beside the .ihx), if present."""
    src = getattr(image, "source", None)
    if not src or getattr(image, "fmt", "") not in ("hex", "ihex"):
        return None
    p = Path(src).with_suffix(".cdb")
    return p if p.exists() else None


def cdb_symbols(path: str | os.PathLike) -> dict[str, int]:
    """name -> tagged address of every addressable CDB symbol (functions and variables).  Globals keep their name;
    file-static and function-local statics are named `module.name` / `function.name` only when no global has the
    plain name."""
    p = Path(path)
    try:
        text = p.read_text(errors="replace")
    except OSError:
        return {}
    space: dict[tuple[str, str], str] = {}
    for line in text.splitlines():
        m = _SYM.match(line)
        if m:
            space[(m.group(1), m.group(2))] = m.group(5)
    out: dict[str, int] = {}
    for line in text.splitlines():
        m = _FUNC.match(line)
        if not m or m.group(1):
            continue
        scope, name, addr = m.group(2), m.group(3), int(m.group(4), 16)
        sp = space.get((scope, name), "C")
        tag = CDB_SPACES.get(sp)
        if tag is None:
            continue
        key = name if scope == "G" else f"{scope[1:]}.{name}" if len(scope) > 1 else name
        if scope == "G" or name not in out:
            out.setdefault(key if scope != "G" else name, tag + addr)
    return out
