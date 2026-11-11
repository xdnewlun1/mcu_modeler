"""Debugger commands of `chipem shell` (mixed into cmd_shell.Shell): break, tbreak, watch, rwatch, awatch, delete,
enable, disable, condition, ignore, info, continue, next, nexti, stepi, finish, until, advance, list, frame.

They use Session.debug (chipem/debug.py). Locations: `main`, `*0x4ac`, `main+0x10`, `blinky.c:12`, `12` (current
file); on a board a chip prefix `B:main`. Watch targets: `0x20000000[:LEN]`, a data symbol, `USART1.SR`.
"""
from __future__ import annotations

import os
import re
from typing import Any

# continue/next/finish in an --exec script: give up after this much wall time when nothing stops the run
EXEC_RUN_LIMIT_S = 10.0


class DebugCommands:
    s: Any

    # ---- helpers -----------------------------------------------------------------------------------------------
    @property
    def dbg(self):
        return self.s.debug.dbg()

    def _split_cond(self, arg: str) -> tuple[str, str | None]:
        m = re.match(r"^(.*?)\s+if\s+(.+)$", arg.strip())
        return (m.group(1).strip(), m.group(2).strip()) if m else (arg.strip(), None)

    def _bp_line(self, b) -> str:
        what = b.label or b.location
        if b.kind == "watch":
            acc = {"w": "watch", "r": "rwatch", "rw": "awatch"}[b.access]
            typ = f"{acc:<10}"
            where = f"{what} [{b.end - b.addr}]"
            addr = self.dbg.fmt_addr(b.addr)
        else:
            typ = "tbreak    " if b.temp else "breakpoint"
            where = what
            addr = self.dbg.fmt_addr(b.addr)
        chip = f"{b.chip}:" if len(getattr(self.s, "ctxs", {}) or {}) > 1 else ""
        line = f"{b.id:<4} {typ} {'y' if b.enabled else 'n'}   {chip}{addr}  {where}"
        if b.cond:
            line += f"  if {b.cond}"
        if b.hits:
            line += f"  (hit {b.hits})"
        if b.ignore:
            line += f"  (ignore next {b.ignore})"
        return line

    def _src_line(self, d, pc: int) -> str | None:
        dw = d.dwarf
        li = dw.line_for(pc) if dw else None
        if li is None or not li.line:
            return None
        lines = dw.source_lines(li.file)
        text = lines[li.line - 1] if lines and 0 < li.line <= len(lines) else ""
        return f"{li.line}\t{text}"

    def _report_stop(self, r: Any) -> None:
        """gdb style: the breakpoint / watchpoint message, then the source line (or the instruction) at the PC."""
        D = self.s.debug
        last = D.last_stop or {}
        name = getattr(r, "name", str(r))
        if name == "DEBUG" and last.get("msg") and (last.get("bp") or last.get("type") == "watchpoint"):
            self.p(last["msg"] + (f"  [board skew {last['skew_ns']:.0f} ns]" if last.get("skew_ns") else ""))
        cid = last.get("chip")
        try:
            d = D.dbg(cid)
        except Exception:
            d = self.dbg
        pc = d.core.pc
        src = self._src_line(d, pc)
        if src is not None:
            self.p(src)
        else:
            ins = d.decode(pc)
            if ins is not None:
                self.p(f"=> {d.fmt_addr(pc)}: {ins['text']}   {d.describe(pc)}")

    def _deadline(self):
        """should_stop callback: none interactively (Ctrl-C stops), a wall-clock limit for --exec scripts."""
        if getattr(self, "interactive", False):
            return None
        import time
        end = time.monotonic() + EXEC_RUN_LIMIT_S

        def late() -> bool:
            if time.monotonic() >= end:
                self.p(f"(no stop within {EXEC_RUN_LIMIT_S:g} s of wall time: interrupted)")
                return True
            return False
        return late

    def _go(self, kind: str, location: str | None = None, count: int = 1, chip: str | None = None) -> None:
        D = self.s.debug
        stop = self._deadline()
        r = self._run_interruptible(lambda: D.do(kind, chip, location, should_stop=stop, count=count))
        self._after_run(r)

    # ---- breakpoints ---------------------------------------------------------------------------------------------
    def do_break(self, arg: str) -> None:
        """break LOCATION [if COND]   breakpoint at FUNC (after its prologue with line info), *ADDR, FUNC+OFF,
        FILE:LINE or LINE; COND e.g. r0 == 3, counter > 10, mem32[0x20000000] & 1. Board: B:main.
        No argument: at the current PC."""
        loc, cond = self._split_cond(arg)
        if not loc:
            loc = f"*0x{self.dbg.core.pc:x}"
        for b in self.s.debug.add_break(loc, cond=cond):
            self.p(f"Breakpoint {b.id} at {self.dbg.fmt_addr(b.addr)}: {b.label}" + (f" if {cond}" if cond else ""))

    do_b = do_break

    def do_tbreak(self, arg: str) -> None:
        """tbreak LOCATION [if COND]   temporary breakpoint (deleted when it stops the run)."""
        loc, cond = self._split_cond(arg)
        for b in self.s.debug.add_break(loc, cond=cond, temp=True):
            self.p(f"Temporary breakpoint {b.id} at {self.dbg.fmt_addr(b.addr)}: {b.label}")

    def _watch(self, arg: str, access: str) -> None:
        a = arg.strip()
        m = re.match(r"^-(r|w|a|rw)\s+(.*)$", a)
        if m:
            access = {"a": "rw"}.get(m.group(1), m.group(1))
            a = m.group(2)
        target, cond = self._split_cond(a)
        if not target:
            raise ValueError("usage: watch [-r|-w|-a] ADDR[:LEN] | SYMBOL | PERIPH.REG [if COND]")
        b = self.s.debug.add_watch(target, access, cond=cond)
        kind = {"w": "Watchpoint", "r": "Read watchpoint", "rw": "Access watchpoint"}[b.access]
        self.p(f"{kind} {b.id}: {b.label} ({self.dbg.fmt_addr(b.addr)}, {b.end - b.addr} "
               f"{'words' if self.dbg.c28x else 'bytes'})")

    def do_watch(self, arg: str) -> None:
        """watch [-r|-a] TARGET [if COND]   stop after an instruction writes (-r reads, -a reads or writes) TARGET:
        ADDR[:LEN], a data symbol, or a peripheral register PERIPH.REG (e.g. USART1.SR, GPIOC.PDOR)."""
        self._watch(arg, "w")

    def do_rwatch(self, arg: str) -> None:
        """rwatch TARGET   read watchpoint (see watch)."""
        self._watch(arg, "r")

    def do_awatch(self, arg: str) -> None:
        """awatch TARGET   access (read or write) watchpoint (see watch)."""
        self._watch(arg, "rw")

    def do_delete(self, arg: str) -> None:
        """delete [N ...]   delete breakpoints / watchpoints N (none: all)."""
        a = self._args(arg)
        if a and a[0] in ("breakpoints", "break", "b"):
            a = a[1:]
        if not a:
            self.p(f"deleted {self.s.debug.delete_all()} breakpoint(s)")
            return
        for x in a:
            self.s.debug.delete(int(x))
            self.p(f"deleted {x}")

    def do_disable(self, arg: str) -> None:
        """disable N ...   disable breakpoints / watchpoints (kept, not hooked)."""
        for x in self._args(arg):
            self.s.debug.enable(int(x), False)

    def do_enable(self, arg: str) -> None:
        """enable N ...   enable breakpoints / watchpoints."""
        for x in self._args(arg):
            self.s.debug.enable(int(x), True)

    def do_condition(self, arg: str) -> None:
        """condition N [EXPR]   set (or with no EXPR remove) the condition of breakpoint N."""
        a = arg.strip().split(None, 1)
        if not a:
            raise ValueError("usage: condition N [EXPR]")
        d, _ = self.s.debug.find(int(a[0]))
        d.modify(int(a[0]), cond=a[1] if len(a) > 1 else None)

    def do_ignore(self, arg: str) -> None:
        """ignore N COUNT   do not stop at breakpoint N for its next COUNT hits."""
        a = self._args(arg)
        if len(a) != 2:
            raise ValueError("usage: ignore N COUNT")
        d, _ = self.s.debug.find(int(a[0]))
        d.modify(int(a[0]), ignore=int(a[1]))

    def do_info(self, arg: str) -> None:
        """info break | regs | frame | line [LOC] | source   debugger information."""
        a = self._args(arg)
        what = a[0] if a else "break"
        if what in ("break", "breakpoints", "b", "watch", "watchpoints"):
            bps = self.s.debug.breakpoints()
            if not bps:
                self.p("No breakpoints or watchpoints.")
                return
            self.p("Num  Type       Enb  Address     What")
            for b in bps:
                self.p(self._bp_line(b))
            return
        if what in ("regs", "registers", "r"):
            return self.do_regs("")
        if what in ("frame", "f"):
            d = self.dbg
            pc = d.core.pc
            self.p(f"pc {d.fmt_addr(pc)} in {d.describe(pc)}; sp 0x{d.core.read_reg('sp'):X}")
            cfa = d.cfa()
            if cfa is not None:
                self.p(f"frame at 0x{cfa:X} (CFA)")
            return
        if what == "line":
            d = self.dbg
            addr = d.resolve_location(a[1])[0][0] if len(a) > 1 else d.core.pc
            li = d.dwarf.line_for(addr) if d.dwarf else None
            if li is None:
                self.p(f"no line info for {d.fmt_addr(addr)}")
            else:
                self.p(f"Line {li.line} of {li.file} is at {d.fmt_addr(li.addr)}..{d.fmt_addr(li.end)}")
            return
        if what == "source":
            d = self.dbg
            if d.dwarf is None:
                self.p("no DWARF debug info (symbols + assembly only)")
                return
            for f in d.dwarf.files:
                self.p(f"{f}  ->  {d.dwarf.find_source(f) or 'not found (--source-dir DIR)'}")
            return
        raise ValueError(f"unknown 'info {what}' (break, regs, frame, line, source)")

    # ---- execution ---------------------------------------------------------------------------------------------
    def do_continue(self, arg: str) -> None:
        """continue [TIME]   run until a breakpoint / watchpoint / fault (or TIME of virtual time; Ctrl-C stops).
        Non-interactive (--exec): gives up after 10 s of wall time."""
        a = self._args(arg)
        D = self.s.debug
        lim = parse_time_arg(a[0]) if a else None
        stop = self._deadline()
        r = self._run_interruptible(lambda: D.run_until_stop(lim, should_stop=stop))
        self._after_run(r)

    do_c = do_continue

    def do_next(self, arg: str) -> None:
        """next [N]   next source line, running called functions (no line info: one instruction, calls run)."""
        a = self._args(arg)
        self._go("next", count=int(a[0], 0) if a else 1)

    do_n = do_next

    def do_nexti(self, arg: str) -> None:
        """nexti [N]   one instruction; a call (BL/BLX, LCR/LC/FFC) runs until it returns."""
        a = self._args(arg)
        self._go("nexti", count=int(a[0], 0) if a else 1)

    do_ni = do_nexti

    def do_stepi(self, arg: str) -> None:
        """stepi [N]   one instruction (into calls): same as step."""
        self.do_step(arg)

    do_si = do_stepi

    def do_s(self, arg: str) -> None:
        """s [N]   source-line step into called functions with line info (gdb `step`; the shell's `step` is one
        instruction)."""
        a = self._args(arg)
        self._go("step", count=int(a[0], 0) if a else 1)

    def do_finish(self, arg: str) -> None:
        """finish   run until the current function returns (to its caller's frame)."""
        self._go("finish")

    def do_until(self, arg: str) -> None:
        """until [LOCATION]   run until LOCATION is reached (no argument: next line, not into calls)."""
        self._go("until", arg.strip() or None)

    do_advance = do_until

    # ---- source --------------------------------------------------------------------------------------------------
    def do_list(self, arg: str) -> None:
        """list [LOCATION] [N]   source lines around LOCATION (default: the PC), N lines (default 10); breakpoint
        lines are marked B, the PC line =>. Images without line info: disassembly instead."""
        a = self._args(arg)
        n = 10
        if a and re.fullmatch(r"\d+", a[-1]) and len(a) > 1:
            n = int(a.pop())
        d = self.dbg
        if d.dwarf is None:
            self.p("(no line info in this image: disassembly)")
            return self.do_dis(" ".join(a))
        addr = d.resolve_location(a[0])[0][0] if a else d.core.pc
        li = d.dwarf.line_for(addr)
        if li is None:
            self.p(f"no line info for {d.fmt_addr(addr)}")
            return
        lines = d.dwarf.source_lines(li.file)
        if lines is None:
            self.p(f"{li.file}: source not found (use --source-dir DIR); line {li.line}")
            return
        bp_lines = set()
        for b in d.user_bps():
            if b.kind == "break":
                l2 = d.dwarf.line_for(b.addr)
                if l2 is not None and l2.file == li.file:
                    bp_lines.add(l2.line)
        pcl = d.dwarf.line_for(d.core.pc)
        lo = max(1, li.line - n // 2)
        self.p(f"{os.path.basename(li.file)}:")
        for k in range(lo, min(len(lines), lo + n - 1) + 1):
            mark = ("=>" if pcl is not None and pcl.file == li.file and pcl.line == k else "  ")
            mark += "B" if k in bp_lines else " "
            self.p(f"{mark}{k:>5}  {lines[k - 1]}")

    do_l = do_list


def parse_time_arg(t: str) -> float:
    from .session import parse_time
    return parse_time(t)
