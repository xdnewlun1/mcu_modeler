"""DWARF line / function / call-frame information of an ELF image, for the debugger (chipem/debug.py).

    dw = DwarfInfo.load("fw.elf")                 # None: not an ELF or no DWARF (raw .bin, .c28img, TI COFF)
    dw.line_for(0x4ae)      -> LineInfo(addr=0x4ac, file="/abs/blinky.c", line=12, end=0x4b2)  (or None)
    dw.function_for(0x4ae)  -> Function(name="main", lo=0x4ac, hi=0x500, file=..., line=10)
    dw.addresses_for("blinky.c", 12) -> [0x4ac]   # first address of each block of statement rows of that line
    dw.source_lines(path)   -> list of the file's lines (located with find_source) or None
    dw.unwind_rules(pc)     -> CFA rule and saved-register rules from .debug_frame (ARM: register numbers r0..r15)

Addresses are in the image's address units: ARM ELF byte addresses; the TI C2000 ELF (cl2000 EABI) already records
16-bit word addresses in its DWARF, so no conversion is needed. Only pyelftools is used (no third-party deps).

Source files are searched (find_source) as recorded (absolute, or comp_dir/dir/name), then relative to the ELF's
directory and its parent, then in each `source_dirs` entry (the CLI's --source-dir) by relative path and by basename.
"""
from __future__ import annotations

import bisect
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["DwarfInfo", "LineInfo", "Function"]


@dataclass(frozen=True)
class LineInfo:
    addr: int            # start address of this row
    file: str            # path as recorded in DWARF (made absolute with the comp dir when relative)
    line: int
    end: int             # address of the next row (exclusive)
    is_stmt: bool = True

    @property
    def basename(self) -> str:
        return os.path.basename(self.file)


@dataclass(frozen=True)
class Function:
    name: str
    lo: int
    hi: int              # exclusive
    file: str | None = None
    line: int | None = None


def _s(v: Any) -> str:
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return str(v)


class DwarfInfo:
    def __init__(self, path: str, rows: list[LineInfo], funcs: list[Function], cfi: Any = None,
                 source_dirs: list[str] | None = None):
        self.path = str(path)
        self.rows = sorted(rows, key=lambda r: (r.addr, -r.end))
        self.starts = [r.addr for r in self.rows]
        self.funcs = sorted(funcs, key=lambda f: f.lo)
        self.fstarts = [f.lo for f in self.funcs]
        self.source_dirs = [str(d) for d in (source_dirs or [])]
        self._cfi = cfi                       # list of pyelftools CFI entries (FDEs) or None
        self._fde_index: list[tuple[int, int, Any]] | None = None
        self._src_cache: dict[str, list[str] | None] = {}
        self._where: dict[str, str | None] = {}
        by_line: dict[tuple[str, int], list[int]] = {}
        prev = None
        for r in self.rows:
            if not r.is_stmt:
                continue
            key = (r.file, r.line)
            # first address of each run of rows of this line (a line split by other lines gets several)
            if prev is None or prev.file != r.file or prev.line != r.line or prev.end != r.addr:
                by_line.setdefault(key, []).append(r.addr)
            prev = r
        self.by_line = by_line
        self.files = sorted({r.file for r in self.rows})

    # ---------------------------------------------------------------------------------------------- loading
    @classmethod
    def load(cls, path: str | os.PathLike | None, source_dirs: list[str] | None = None) -> "DwarfInfo | None":
        if not path:
            return None
        try:
            from elftools.elf.elffile import ELFFile
            with open(path, "rb") as f:
                if f.read(4) != b"\x7fELF":
                    return None
                f.seek(0)
                elf = ELFFile(f)
                if not elf.has_dwarf_info():
                    return None
                dw = elf.get_dwarf_info()
                rows, funcs = cls._read_lines_funcs(dw)
                cfi = None
                try:
                    if dw.has_CFI():
                        cfi = [e for e in dw.CFI_entries() if hasattr(e, "header") and "initial_location" in e.header]
                except Exception:
                    cfi = None
        except Exception:
            return None
        if not rows and not funcs:
            return None
        return cls(str(path), rows, funcs, cfi, source_dirs)

    @staticmethod
    def _read_lines_funcs(dw) -> tuple[list[LineInfo], list[Function]]:
        rows: list[LineInfo] = []
        funcs: list[Function] = []
        for cu in dw.iter_CUs():
            try:
                top = cu.get_top_DIE()
            except Exception:
                continue
            comp_dir = _s(top.attributes["DW_AT_comp_dir"].value) if "DW_AT_comp_dir" in top.attributes else ""
            lp = None
            try:
                lp = dw.line_program_for_CU(cu)
            except Exception:
                lp = None
            names: list[str] = []
            if lp is not None:
                ver = lp.header.get("version", 4)
                dirs = [_s(d) for d in lp.header.get("include_directory", [])]
                files = lp.header.get("file_entry", [])

                def fpath(idx: int) -> str:
                    # DWARF 5 numbers files from 0, earlier versions from 1 (0 = unknown)
                    i = idx if ver >= 5 else idx - 1
                    if not 0 <= i < len(files):
                        return "?"
                    fe = files[i]
                    name = _s(fe.name)
                    di = fe.dir_index
                    d = ""
                    if ver >= 5:
                        d = dirs[di] if 0 <= di < len(dirs) else ""
                    elif di > 0 and di - 1 < len(dirs):
                        d = dirs[di - 1]
                    p = os.path.join(d, name) if d else name
                    if not os.path.isabs(p) and comp_dir:
                        p = os.path.join(comp_dir, p)
                    return os.path.normpath(p)
                names = [fpath(i) for i in range(len(files) + 1)]
                seq: list = []
                try:
                    for e in lp.get_entries():
                        st = e.state
                        if st is None:
                            continue
                        seq.append(st)
                        if st.end_sequence:
                            for a, b in zip(seq, seq[1:]):
                                if b.address > a.address or (b is seq[-1] and b.address >= a.address):
                                    fi = a.file
                                    fn = names[fi] if 0 <= fi < len(names) else "?"
                                    if b.address > a.address:
                                        rows.append(LineInfo(a.address, fn, a.line, b.address, bool(a.is_stmt)))
                            seq = []
                except Exception:
                    pass
            # functions (subprograms with code), nested ones included
            try:
                stack = [top]
                while stack:
                    die = stack.pop()
                    for ch in die.iter_children():
                        stack.append(ch)
                        if ch.tag != "DW_TAG_subprogram":
                            continue
                        at = ch.attributes
                        if "DW_AT_low_pc" not in at or "DW_AT_high_pc" not in at:
                            continue
                        lo = int(at["DW_AT_low_pc"].value)
                        hv = at["DW_AT_high_pc"]
                        hi = int(hv.value) if hv.form == "DW_FORM_addr" else lo + int(hv.value)
                        name = _function_name(ch)
                        if not name:
                            continue
                        fl, ln = None, None
                        if "DW_AT_decl_line" in at:
                            ln = int(at["DW_AT_decl_line"].value)
                        if "DW_AT_decl_file" in at and names:
                            fi = int(at["DW_AT_decl_file"].value)
                            fl = names[fi] if 0 <= fi < len(names) else None
                        funcs.append(Function(name, lo, hi, fl, ln))
            except Exception:
                pass
        return rows, funcs

    # ---------------------------------------------------------------------------------------------- queries
    def line_for(self, addr: int) -> LineInfo | None:
        """The line-table row covering `addr` (the innermost: the latest row starting at or below it)."""
        i = bisect.bisect_right(self.starts, addr) - 1
        while i >= 0:
            r = self.rows[i]
            if r.addr <= addr < r.end:
                if r.line == 0:
                    # line 0 = "no source line" (compiler-generated code): like gdb, use the line before it
                    for k in range(i - 1, max(-1, i - 64), -1):
                        p = self.rows[k]
                        if p.end == r.addr and p.line:
                            return LineInfo(r.addr, p.file, p.line, r.end, False)
                        if p.end < r.addr:
                            break
                return r
            if addr - r.addr > 0x10000:
                break
            i -= 1
        return None

    def function_for(self, addr: int) -> Function | None:
        i = bisect.bisect_right(self.fstarts, addr) - 1
        best = None
        while i >= 0:
            f = self.funcs[i]
            if f.lo <= addr < f.hi:
                if best is None or f.hi - f.lo < best.hi - best.lo:
                    best = f
            if addr - f.lo > 0x100000:
                break
            i -= 1
        return best

    def function_named(self, name: str) -> Function | None:
        for f in self.funcs:
            if f.name == name:
                return f
        return None

    def match_file(self, name: str) -> list[str]:
        """Recorded file paths matching `name` (exact path, path suffix, or basename)."""
        n = os.path.normpath(name)
        exact = [f for f in self.files if f == n]
        if exact:
            return exact
        suf = [f for f in self.files if f.endswith(os.sep + n)]
        if suf:
            return suf
        return [f for f in self.files if os.path.basename(f) == os.path.basename(n)]

    def addresses_for(self, file: str, line: int) -> list[int]:
        """Breakpoint addresses for `file:line`: the first address of each run of statement rows of that line. If
        the line has no code, the next line with code (up to 50 lines further) is used, as gdb does."""
        for f in self.match_file(file):
            for ln in range(int(line), int(line) + 50):
                addrs = self.by_line.get((f, ln))
                if addrs:
                    return sorted(addrs)
        return []

    def line_ranges(self, file: str, line: int) -> list[tuple[int, int]]:
        """Address ranges [lo, hi) of every row of `file:line` (statement or not)."""
        return [(r.addr, r.end) for r in self.rows if r.line == line and r.file == file]

    def function_lines(self, fn: Function) -> list[LineInfo]:
        i = bisect.bisect_left(self.starts, fn.lo)
        out = []
        while i < len(self.rows) and self.rows[i].addr < fn.hi:
            out.append(self.rows[i])
            i += 1
        return out

    def prologue_end(self, fn: Function) -> int:
        """Address after the prologue: the second distinct line row of the function (gdb's heuristic), else lo."""
        rows = [r for r in self.function_lines(fn) if r.is_stmt]
        if not rows:
            return fn.lo
        first = rows[0].line
        for r in rows[1:]:
            if r.line != first and r.addr > fn.lo:
                return r.addr
        return rows[1].addr if len(rows) > 1 else fn.lo

    # ---------------------------------------------------------------------------------------------- sources
    def find_source(self, file: str) -> str | None:
        """Locate a source file on this machine (see the module docstring for the search order)."""
        if file in self._where:
            return self._where[file]
        cands = [file]
        elf_dir = Path(self.path).resolve().parent
        base = os.path.basename(file)
        rel = file.lstrip("/\\") if os.path.isabs(file) else file
        parts = Path(rel).parts
        for root in [elf_dir, elf_dir.parent] + [Path(d) for d in self.source_dirs]:
            cands.append(str(root / rel))
            for k in range(1, len(parts)):              # trailing components: src/main.c, main.c
                cands.append(str(root.joinpath(*parts[k:])))
            cands.append(str(root / base))
        found = None
        for c in cands:
            try:
                if os.path.isfile(c):
                    found = c
                    break
            except OSError:
                continue
        self._where[file] = found
        return found

    def source_lines(self, file: str) -> list[str] | None:
        if file in self._src_cache:
            return self._src_cache[file]
        p = self.find_source(file)
        lines = None
        if p is not None:
            try:
                with open(p, "rb") as f:
                    data = f.read(4 << 20)
                lines = data.decode("utf-8", "replace").splitlines()
            except OSError:
                lines = None
        self._src_cache[file] = lines
        return lines

    # ---------------------------------------------------------------------------------------------- unwinding
    def unwind_rules(self, pc: int) -> tuple[Any, dict] | None:
        """(cfa_rule, {regnum: rule}) of .debug_frame at `pc`, or None. Rules are pyelftools CFARule / RegisterRule."""
        if not self._cfi:
            return None
        if self._fde_index is None:
            idx = []
            for e in self._cfi:
                try:
                    lo = int(e.header["initial_location"])
                    idx.append((lo, lo + int(e.header["address_range"]), e))
                except Exception:
                    continue
            idx.sort(key=lambda t: t[0])
            self._fde_index = idx
        starts = [t[0] for t in self._fde_index]
        i = bisect.bisect_right(starts, pc) - 1
        if i < 0:
            return None
        lo, hi, fde = self._fde_index[i]
        if not lo <= pc < hi:
            return None
        try:
            table = fde.get_decoded().table
        except Exception:
            return None
        row = None
        for r in table:
            if r["pc"] <= pc:
                row = r
            else:
                break
        if row is None:
            return None
        regs = {k: v for k, v in row.items() if isinstance(k, int)}
        return row.get("cfa"), regs


def _function_name(die) -> str | None:
    at = die.attributes
    for key in ("DW_AT_name", "DW_AT_linkage_name", "DW_AT_MIPS_linkage_name"):
        if key in at:
            return _s(at[key].value)
    if "DW_AT_specification" in at or "DW_AT_abstract_origin" in at:
        try:
            ref = die.get_DIE_from_attribute("DW_AT_specification" if "DW_AT_specification" in at
                                              else "DW_AT_abstract_origin")
            return _function_name(ref)
        except Exception:
            return None
    return None
