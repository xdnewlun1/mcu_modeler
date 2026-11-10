"""`chipem shell`: interactive REPL over a Session (see docs/CONTRACTS.md "chipem/session.py").

    chipem shell fw.elf                         interactive (readline history in ~/.chipem_history)
    chipem shell fw.elf --exec "run 10ms; pins gpio; quit"   non-interactive

Commands are listed by `help`.  Several commands may be given on one line separated by ';'.
"""
from __future__ import annotations

import cmd
import os
import re
import shlex
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Iterable

from .symbols import Symbols as _Symbols, load_symbols  # noqa: F401  (ELF symbols for dis/bt; shared with pinreport)

HISTORY = Path.home() / ".chipem_history"
_UNITS = {"ns": 1.0, "us": 1e3, "µs": 1e3, "ms": 1e6, "s": 1e9}


# ---------------------------------------------------------------------------------------------------------------
# helpers shared with chipem.web.server

def parse_time(s: str) -> float:
    """"50ms" -> 5e7 ns. Uses chipem.session.parse_time when available (single source of truth)."""
    try:
        from .session import parse_time as pt
        return float(pt(s))
    except ImportError:
        pass
    m = re.fullmatch(r"\s*([0-9]*\.?[0-9]+(?:[eE][-+]?\d+)?)\s*(ns|us|µs|ms|s)?\s*", str(s))
    if not m:
        raise ValueError(f"bad time {s!r} (use e.g. 10ms, 500us, 1.5s)")
    return float(m.group(1)) * _UNITS[m.group(2) or "ms"]


def parse_level(s: Any) -> str | None:
    """1/0/H/L/X/Z (case-insensitive); Z / none / - mean "nothing connected" (None)."""
    t = str(s).strip().upper()
    if t in ("Z", "NONE", "-", "OPEN", "NC", ""):
        return None
    try:
        from .session import parse_level as pl
        return pl(s)
    except ImportError:
        pass
    if t in ("1", "H", "HIGH"):
        return "H"
    if t in ("0", "L", "LOW"):
        return "L"
    if t in ("X",):
        return "X"
    raise ValueError(f"bad level {s!r} (1/0/H/L/X/Z)")


def unescape(text: str) -> bytes:
    r"""Interpret \n \r \t \\ \xNN \0 escapes; other characters are encoded as latin-1/utf-8."""
    out = bytearray()
    i = 0
    simple = {"n": 10, "r": 13, "t": 9, "\\": 92, "0": 0, "e": 27, '"': 34, "'": 39}
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            n = text[i + 1]
            if n in simple:
                out.append(simple[n])
                i += 2
                continue
            if n == "x" and re.fullmatch(r"[0-9a-fA-F]{2}", text[i + 2:i + 4] or ""):
                out.append(int(text[i + 2:i + 4], 16))
                i += 4
                continue
        out += c.encode("utf-8")
        i += 1
    return bytes(out)


def split_commands(line: str) -> list[str]:
    """Split on ';' outside single/double quotes."""
    parts, cur, q = [], [], None
    i = 0
    while i < len(line):
        c = line[i]
        if q:
            if c == "\\" and i + 1 < len(line):
                cur.append(line[i:i + 2])
                i += 2
                continue
            if c == q:
                q = None
        elif c in "\"'":
            q = c
        elif c == ";":
            parts.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    parts.append("".join(cur))
    return [x.strip() for x in parts if x.strip()]


def fmt_ns(t: float) -> str:
    if t >= 1e9:
        return f"{t / 1e9:.6f} s"
    if t >= 1e6:
        return f"{t / 1e6:.3f} ms"
    if t >= 1e3:
        return f"{t / 1e3:.3f} us"
    return f"{t:.0f} ns"


def fmt_hz(hz: float) -> str:
    return f"{hz / 1e6:g} MHz" if hz >= 1e6 else f"{hz / 1e3:g} kHz"


def jsonable(v: Any) -> Any:
    """Make event fields / state values JSON-serialisable."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("latin-1")
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set, frozenset)):
        return [jsonable(x) for x in v]
    if hasattr(v, "value") and hasattr(v, "name"):      # Enum
        return jsonable(v.value)
    return str(v)


def event_dict(ev: Any) -> dict:
    if hasattr(ev, "to_dict"):
        d = ev.to_dict()
    elif isinstance(ev, dict):
        d = dict(ev)
    else:
        d = {"kind": getattr(ev, "kind", "info"), "t_ns": getattr(ev, "t_ns", 0.0), "msg": str(ev)}
    return jsonable(d)


def format_ev(ev: Any) -> str:
    try:
        from .log import format_event
        return format_event(ev)
    except Exception:
        return str(ev)


def pin_short(p: dict) -> str:
    return p.get("short") or str(p.get("name", "")).split("/")[0]


# ---------------------------------------------------------------------------------------------------------------
# Kinetis PORT/GPIO introspection (best effort; only used for the `pin` detail view)

def _blk_word(blk: Any, off: int) -> int | None:
    try:
        return int.from_bytes(bytes(blk.mem[off:off + 4]), "little")
    except Exception:
        return None


def kinetis_pin_state(chip: Any, port: str | None, bit: int | None) -> list[str]:
    if port is None or bit is None:
        return []
    out = []
    pblk = chip.peripherals.get(f"PORT{port}")
    if pblk is not None:
        pcr = _blk_word(pblk, 4 * bit)
        if pcr is not None:
            irqc = (pcr >> 16) & 0xF
            irqn = {0: "off", 1: "DMA rising", 2: "DMA falling", 3: "DMA either", 8: "IRQ low", 9: "IRQ rising",
                    10: "IRQ falling", 11: "IRQ either", 12: "IRQ high"}.get(irqc, f"0x{irqc:x}")
            out.append(f"PORT{port}_PCR{bit} = 0x{pcr:08X}: MUX=ALT{(pcr >> 8) & 7} "
                       f"PE={pcr >> 1 & 1} PS={pcr & 1} ({'pull-up' if pcr & 3 == 3 else 'pull-down' if pcr & 3 == 2 else 'no pull'}) "
                       f"SRE={pcr >> 2 & 1} PFE={pcr >> 4 & 1} ODE={pcr >> 5 & 1} DSE={pcr >> 6 & 1} "
                       f"IRQC={irqn} ISF={pcr >> 24 & 1}")
    gblk = chip.peripherals.get(f"GPIO{port}")
    if gblk is not None:
        vals = {}
        for name, off in (("PDOR", 0x00), ("PDIR", 0x10), ("PDDR", 0x14)):
            v = None
            try:
                v = gblk.get(name)
            except Exception:
                v = _blk_word(gblk, off)
            vals[name] = v
        bits = " ".join(f"{k}[{bit}]={(v >> bit) & 1}" for k, v in vals.items() if v is not None)
        if bits:
            out.append(f"GPIO{port}: {bits}")
    return out


# ---------------------------------------------------------------------------------------------------------------

def _part_state_str(st: dict) -> str:
    out = []
    for k, v in st.items():
        if isinstance(v, dict):
            v = ",".join(f"{a}={b}" for a, b in v.items())
        elif isinstance(v, float):
            v = f"{v:.6g}"
        out.append(f"{k}={v}")
    return " ".join(out)


class Shell(cmd.Cmd):
    intro = ""
    prompt = "chipem> "
    doc_header = "Commands (help <cmd> for details)"

    def __init__(self, session: Any, stdout=None, *, interactive: bool = False):
        super().__init__(stdout=stdout or sys.stdout)
        self.s = session
        self.interactive = interactive
        self.trace = False
        self._unsub = None
        self._printer = None
        self._baseline = self._snapshot()
        self.symbols = _Symbols.for_image(getattr(session, "image", None))
        self.last_exit = 0

    # ---- plumbing --------------------------------------------------------------------------------------------
    @property
    def chip(self) -> Any:
        return self.s.chip

    def p(self, *a: Any) -> None:
        print(*a, file=self.stdout)

    def _snapshot(self) -> dict[str, tuple]:
        try:
            return {pin_short(p): (p.get("level"), p.get("owner"), p.get("direction"), p.get("pull"))
                    for p in self.s.pin_table()}
        except Exception:
            return {}

    def onecmd(self, line: str) -> bool:
        line = line.strip()
        if ";" in line:
            parts = split_commands(line)
        else:
            parts = [line]
        if len(parts) > 1:
            for part in parts:
                if part.strip() and self.onecmd(part):
                    return True
            return False
        line = parts[0] if parts else ""
        try:
            return bool(super().onecmd(line))
        except (ValueError, KeyError, IndexError) as e:
            self.p(f"error: {e.args[0] if e.args else e}")
        except Exception as e:      # keep the REPL alive
            self.p(f"error: {type(e).__name__}: {e}")
        self.last_exit = 1
        return False

    def emptyline(self) -> bool:
        return False

    def default(self, line: str) -> None:
        self.p(f"unknown command: {line.split()[0]!r} (try 'help')")
        self.last_exit = 1

    def _args(self, arg: str) -> list[str]:
        try:
            return shlex.split(arg)
        except ValueError:
            return arg.split()

    def _int(self, s: str) -> int:
        return int(s, 0)

    def _addr(self, s: str) -> int:
        s = s.strip()
        low = s.lower()
        if low in ("pc", "sp", "lr", "msp", "psp") or re.fullmatch(r"r\d+", low):
            return self.chip.core.read_reg(low)
        try:
            return int(s, 0)
        except ValueError:
            pass
        m = re.fullmatch(r"([A-Za-z_.$][\w.$]*)\s*(?:\+\s*(\w+))?", s)     # symbol or symbol+off
        if m:
            for a, _sz, name in self.symbols.syms:
                if name == m.group(1):
                    return a + (int(m.group(2), 0) if m.group(2) else 0)
        raise ValueError(f"bad address or unknown symbol {s!r}")

    # ---- trace -----------------------------------------------------------------------------------------------
    def _set_trace(self, on: bool, all_edges: bool = False) -> None:
        """Live events through trace.PinEventPrinter (mmio excluded): the same UART coalescing and periodic-waveform
        collapse as `chipem run` (`all_edges` prints every edge, like `chipem run --all-edges`)."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        if self._printer is not None:
            self._printer.flush()
            self._printer = None
        if on:
            log = self.chip.log
            if hasattr(log, "subscribe"):
                from .log import KINDS
                from .trace import PinEventPrinter
                self._printer = PinEventPrinter(self.stdout, set(KINDS) - {"mmio"},
                                                collapse_ns=None if all_edges else 10e6)
                self._unsub = log.subscribe(self._printer)
        self.trace = on

    def _flush_trace(self) -> None:
        if self._printer is not None:
            self._printer.flush()

    # ---- execution -------------------------------------------------------------------------------------------
    def _run_interruptible(self, fn, *a):
        """Run fn with Ctrl-C mapped to session.stop() (only possible from the main thread)."""
        if threading.current_thread() is not threading.main_thread():
            return fn(*a)
        hit = []

        def handler(signum, frame):
            hit.append(1)
            self.s.stop()
        old = signal.signal(signal.SIGINT, handler)
        try:
            return fn(*a)
        finally:
            signal.signal(signal.SIGINT, old)
            if hit:
                self.p("^C stopped")

    def _after_run(self, reason: Any) -> None:
        self._flush_trace()
        st = self.s.state()
        name = getattr(reason, "name", str(reason))
        t = st.get("t_ns", st.get("time_ns", 0.0))
        self.p(f"stopped: {name}  t={fmt_ns(t)}  cycles={st.get('cycles', 0)}  pc=0x{st.get('pc', 0):08X}"
               + (f" {self.symbols.lookup(st.get('pc', 0))}" if self.symbols.syms else ""))
        if name == "FAULT":
            f = getattr(self.chip, "last_fault", None)
            if f:
                self.p(f"fault: {f}")

    def do_run(self, arg: str) -> None:
        """run [TIME]   run for TIME of virtual time (default 10ms; units ns/us/ms/s). Ctrl-C stops."""
        a = self._args(arg)
        ns = parse_time(a[0]) if a else 10e6
        self._after_run(self._run_interruptible(self.s.run_for, ns))

    def do_step(self, arg: str) -> None:
        """step [N]   execute N instructions (default 1) and show the next instruction."""
        a = self._args(arg)
        n = self._int(a[0]) if a else 1
        r = None
        for _ in range(max(n, 1)):
            r = self.s.step()
            if getattr(r, "name", "") == "FAULT":
                break
        self._after_run(r)
        self._dis(self.chip.core.pc, 1)

    def do_reset(self, arg: str) -> None:
        """reset   reset the chip (external pin stimulus is kept)."""
        self.s.reset()
        self._baseline = self._snapshot()
        st = self.s.state()
        self.p(f"reset: pc=0x{st.get('pc', 0):08X} sp=0x{st.get('sp', 0):08X}")

    def do_set(self, arg: str) -> None:
        """set PIN LEVEL   drive an input pin now (LEVEL: 1/0/H/L/X, Z = disconnect); set PART VALUE acts on a
        board part (switch level, button pressed/released, ...)."""
        a = self._args(arg)
        if len(a) == 1 and "=" in a[0]:
            a = a[0].split("=", 1)
        if len(a) != 2:
            raise ValueError("usage: set PIN LEVEL")
        part = self.s.find_part(a[0]) if hasattr(self.s, "find_part") else None
        if part is not None:
            self.s.apply_stimulus(f"{a[0]}={a[1]}")
            self.p(f"{part.id}: {_part_state_str(part.state())}")
            return
        lvl = parse_level(a[1])
        self.s.set_pin(a[0], lvl)
        p = self._pin_row(a[0])
        self.p(f"{pin_short(p) if p else a[0]}: external={lvl or 'none'} level={p.get('level') if p else '?'}")

    def do_at(self, arg: str) -> None:
        """at TIME PIN LEVEL   schedule a pin change (or a part action: at 5ms SW1 H) at absolute virtual TIME."""
        a = self._args(arg)
        if len(a) != 3:
            raise ValueError("usage: at TIME PIN LEVEL")
        if hasattr(self.s, "find_part") and self.s.find_part(a[1]) is not None:
            self.s.apply_stimulus(f"{a[0]}:{a[1]}={a[2]}")
            self.p(f"scheduled {a[1]}={a[2]} at {fmt_ns(parse_time(a[0]))}")
            return
        t = parse_time(a[0])
        lvl = parse_level(a[2])
        self.s.schedule_pin(t, a[1], lvl)
        self.p(f"scheduled {a[1]}={lvl or 'Z'} at {fmt_ns(t)}")

    # ---- parts -----------------------------------------------------------------------------------------------
    def do_parts(self, arg: str) -> None:
        """parts   board parts (switches, LEDs, gates, ...): type, port -> net wiring and state."""
        parts = getattr(self.s, "parts", None) or {}
        if not parts:
            self.p("(no parts; add with --part ID:TYPE:PORT=PIN,... or a board `parts:` list)")
            return
        b = self.s.board
        for p in parts.values():
            ports = " ".join(f"{k}={(n.name if (n := b.part_net(p.id, k)) is not None else '-')}" for k in p.ports)
            self.p(f"{p.id:<8} {p.type:<8} {ports:<34} {_part_state_str(p.state())}")

    def do_part(self, arg: str) -> None:
        """part ID KEY=VALUE ...   set part values now, e.g. part SW1 level=H, part BTN1 pressed=1,
        part CLK1 period=2ms; part ID alone shows its state; part add ID:TYPE:PORT=PIN,... adds one."""
        a = self._args(arg)
        if not a:
            raise ValueError("usage: part ID KEY=VALUE ... | part add ID:TYPE[:KEY=VALUE,...]")
        if a[0] == "add" and len(a) == 2:
            p = self.s.add_part_spec(a[1])
            self.p(f"added {p.id} ({p.type}): {_part_state_str(p.state())}")
            return
        part = self.s.find_part(a[0]) if hasattr(self.s, "find_part") else None
        if part is None:
            raise KeyError(f"unknown part {a[0]!r}")
        values = {}
        for kv in a[1:]:
            if "=" not in kv:
                raise ValueError(f"expected KEY=VALUE, got {kv!r}")
            k, v = kv.split("=", 1)
            values[k] = v
        st = self.s.set_part(part.id, **values) if values else part.state()
        self.p(f"{part.id}: {_part_state_str(st)}")

    # ---- pins ------------------------------------------------------------------------------------------------
    def _pin_row(self, name: str) -> dict | None:
        try:
            pin = self.chip.pins.get(name)
            key = pin.num
        except Exception:
            key = None
        for p in self.s.pin_table():
            if p.get("num") == key or pin_short(p).upper() == str(name).upper():
                return p
        return None

    def do_pins(self, arg: str) -> None:
        """pins [all|changed|gpio|PATTERN]   pin table (default: GPIO-capable pins muxed to a digital function).
        PATTERN is a case-insensitive glob/substring matched against names and functions (e.g. PTC*, UART)."""
        import fnmatch
        a = self._args(arg)
        mode = a[0] if a else ""
        rows = self.s.pin_table()
        if mode == "all":
            sel = rows
        elif mode == "changed":
            sel = [p for p in rows if self._baseline.get(pin_short(p)) !=
                   (p.get("level"), p.get("owner"), p.get("direction"), p.get("pull"))]
        elif mode == "gpio":
            sel = [p for p in rows if p.get("kind") == "gpio"]
        elif mode:
            pat = mode.upper()
            glob = pat if any(c in pat for c in "*?[") else f"*{pat}*"
            sel = [p for p in rows if fnmatch.fnmatch(str(p.get("name", "")).upper(), glob)
                   or fnmatch.fnmatch(pin_short(p).upper(), glob)
                   or any(fnmatch.fnmatch(str(f).upper(), glob) for f in (p.get("functions") or {}).values())]
        else:
            sel = [p for p in rows if p.get("owner") not in ("disabled", None, p.get("kind"))
                   and p.get("kind") == "gpio" and p.get("direction") != "analog"]
        self.p(f"{'num':>3}  {'pin':<8} {'owner/function':<22} {'dir':<6} {'lvl':<3} {'pull':<5} ext")
        for p in sel:
            own = str(p.get('owner') or '-')
            if len(own) > 22:
                own = own[:21] + "~"
            self.p(f"{p.get('num', 0):>3}  {pin_short(p):<8} {own:<22} "
                   f"{str(p.get('direction') or '-'):<6} {str(p.get('level') or '-'):<3} "
                   f"{str(p.get('pull') or '-'):<5} {p.get('external') or '-'}")
        self.p(f"({len(sel)} of {len(rows)} pins)")

    def do_pin(self, arg: str) -> None:
        """pin NAME [all]   details of one pin: names, all ALT functions, owner, direction, PCR/GPIO state, and
        what the firmware did with it (configuration history with the configuring code, counters; 'all' shows
        the whole kept history)."""
        a = self._args(arg)
        if not a:
            raise ValueError("usage: pin NAME [all]")
        p = self._pin_row(a[0])
        if p is None:
            raise KeyError(f"unknown pin {a[0]!r}")
        self.p(f"pin {p.get('num')}  {p.get('name')}  ({pin_short(p)}, kind={p.get('kind')})")
        self.p(f"  owner={p.get('owner')} dir={p.get('direction')} level={p.get('level')} drive={p.get('drive') or '-'} "
               f"pull={p.get('pull') or '-'} external={p.get('external') or 'none'}")
        funcs = p.get("functions") or {}
        if funcs:
            self.p("  functions:")
            for k, v in funcs.items():
                mark = "  <- active" if v == p.get("owner") or (p.get("owner") == "GPIO" and str(v).startswith("PT")) else ""
                self.p(f"    {k:<5} {v}{mark}")
        try:
            pin = self.chip.pins.get(p.get("num"))
            for line in kinetis_pin_state(self.chip, getattr(pin, "port", None), getattr(pin, "bit", None)):
                self.p("  " + line)
        except Exception:
            pass
        try:
            from .pinreport import pin_history
            self.p("  firmware activity:")
            for line in pin_history(self.chip, p.get("num"), full=len(a) > 1 and a[1] == "all").splitlines():
                self.p("  " + line)
        except Exception as e:
            self.p(f"  (pin history unavailable: {e})")

    def do_pinreport(self, arg: str) -> None:
        """pinreport [CHIP] [nostatic]   what the firmware did with each pin so far: final role, configuring
        code (symbol or PC), reads/writes/level changes/interrupts, floating inputs, and a discovery summary
        compared with the static guess of 'chipem info' ('nostatic' skips that comparison). On a board the
        report covers every chip unless a chip id is given."""
        from .pinreport import format_pin_report
        a = self._args(arg)
        static = not any(x.lower() == "nostatic" for x in a)
        sel = [x for x in a if x.lower() != "nostatic"]
        ctxs = getattr(self.s, "ctxs", None) or {}
        if len(ctxs) <= 1 and not sel:
            self.p(format_pin_report(self.chip, static=static))
            return
        if sel:
            want = {x.upper() for x in sel}
            items = [(c, x) for c, x in ctxs.items() if c.upper() in want]
            if not items:
                raise ValueError(f"unknown chip id {sel[0]!r} (chips: {', '.join(ctxs) or '-'})")
        else:
            items = list(ctxs.items())
        for cid, ctx in items:
            self.p(f"=== chip {cid} ===")
            self.p(format_pin_report(ctx.chip, static=static))

    # ---- CPU -------------------------------------------------------------------------------------------------
    def do_regs(self, arg: str) -> None:
        """regs   core registers (r0-r15, xpsr, msp/psp/control/primask/basepri, s0-s3 with an FPU)."""
        core = self.chip.core
        arch_regs = getattr(self.chip.arch, "reg_names", None)       # non-ARM arch (C28x) names its registers
        if arch_regs:
            regs = self.chip.regs()
            items = [f"{n:>6}=0x{v:08X}" for n, v in regs.items()]
            for i in range(0, len(items), 4):
                self.p("  ".join(items[i:i + 4]))
            return
        names = [f"r{i}" for i in range(16)]
        vals = core.read_regs(names) if hasattr(core, "read_regs") else [core.read_reg(n) for n in names]
        alias = {13: "sp", 14: "lr", 15: "pc"}
        for row in range(4):
            cells = []
            for col in range(4):
                i = row * 4 + col
                lab = f"r{i}" + (f"/{alias[i]}" if i in alias else "")
                cells.append(f"{lab:>6}=0x{vals[i]:08X}")
            self.p("  ".join(cells))
        sysregs = ["xpsr", "msp", "psp", "control", "primask", "basepri", "faultmask"]
        sv = []
        for n in sysregs:
            try:
                sv.append(f"{n}=0x{core.read_reg(n):08X}")
            except Exception:
                pass
        self.p("  ".join(sv))
        if getattr(self.s.desc.core, "fpu", False):
            try:
                fs = [core.read_reg(f"s{i}") for i in range(4)]
                import struct
                self.p("  ".join(f"s{i}=0x{v:08X}({struct.unpack('<f', v.to_bytes(4, 'little'))[0]:g})"
                                 for i, v in enumerate(fs)))
            except Exception:
                pass
        x = vals and core.read_reg("xpsr")
        ipsr = x & 0x1FF
        flags = "".join(c if x >> b & 1 else "-" for c, b in (("N", 31), ("Z", 30), ("C", 29), ("V", 28), ("Q", 27)))
        self.p(f"flags={flags} ipsr={ipsr}" + (f" ({self.chip.arch.exc_name(ipsr)})" if ipsr and
                                                 hasattr(self.chip.arch, "exc_name") else " (thread)"))

    def do_mem(self, arg: str) -> None:
        """mem ADDR [N]   hex dump N bytes (default 64) via the bus (MMIO reads may have side effects)."""
        a = self._args(arg)
        if not a:
            raise ValueError("usage: mem ADDR [N]")
        addr = self._addr(a[0])
        n = self._int(a[1]) if len(a) > 1 else 64
        data = self.chip.bus.read_bytes(addr, n)
        for off in range(0, len(data), 16):
            chunk = data[off:off + 16]
            hx = " ".join(f"{b:02x}" for b in chunk)
            asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            self.p(f"{addr + off:08X}  {hx:<47}  {asc}")

    def _dis(self, addr: int, n: int) -> None:
        if getattr(self.chip, "arch_kind", "") == "c28x":
            return self._dis_c28x(addr, n)
        import capstone
        md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
        addr &= ~1
        try:
            code = self.chip.bus.read_bytes(addr, n * 4 + 4)
        except Exception as e:
            self.p(f"cannot read 0x{addr:08X}: {e}")
            return
        pc = self.chip.core.pc
        count = 0
        last_sym = None
        for ins in md.disasm(code, addr):
            sym = self.symbols.lookup(ins.address)
            if sym and sym.endswith(">") and "+" not in sym and sym != last_sym:
                self.p(f"{sym[1:-1]}:")
            last_sym = sym
            mark = "=>" if ins.address == pc else "  "
            self.p(f"{mark} {ins.address:08X}: {ins.bytes.hex():<9} {ins.mnemonic:<8} {ins.op_str}")
            count += 1
            if count >= n:
                break
        if count == 0:
            self.p(f"{addr:08X}: <undecodable>")

    def _dis_c28x(self, addr: int, n: int) -> None:
        """C28x: word addresses, words shown as hex, labels from the image symbols."""
        import struct
        from .core.c28x.asm import disasm_core
        core = self.chip.core
        try:
            lines = disasm_core(core, addr, n)
        except Exception as e:
            self.p(f"cannot read 0x{addr:06X}: {e}")
            return
        if not lines:
            self.p(f"cannot read 0x{addr:06X}")
            return
        pc = core.pc
        for a, ln, text in lines:
            sym = self.symbols.lookup(a)
            if sym and "+" not in sym:
                self.p(f"{sym[1:-1]}:")
            try:
                ws = struct.unpack(f"<{ln}H", core.read_mem(a, ln))
                hx = " ".join(f"{w:04x}" for w in ws)
            except Exception:
                hx = "?"
            mark = "=>" if a == pc else "  "
            self.p(f"{mark} {a:06X}: {hx:<9} {text}")

    def do_dis(self, arg: str) -> None:
        """dis [ADDR|SYMBOL[+OFF]] [N]   disassemble N instructions (default 10) at ADDR (default pc).
        C28x: ADDR is a 16-bit word address."""
        a = self._args(arg)
        addr = self._addr(a[0]) if a else self.chip.core.pc
        n = self._int(a[1]) if len(a) > 1 else 10
        self._dis(addr, n)

    def _flash_ranges(self) -> list[tuple[int, int]]:
        out = []
        for m in self.s.desc.memory:
            if m.kind in ("flash", "rom", "flexnvm"):
                out.append((m.base, m.base + m.size))
        return out

    def _is_call_site(self, ret: int) -> bool:
        a = (ret & ~1)
        try:
            b = self.chip.bus.read_bytes(a - 4, 4)
        except Exception:
            return False
        hw1 = int.from_bytes(b[0:2], "little")
        hw2 = int.from_bytes(b[2:4], "little")
        if (hw1 & 0xF800) == 0xF000 and (hw2 & 0xD000) == 0xD000:     # BL imm
            return True
        return (hw2 & 0xFF87) == 0x4780                                # BLX Rm

    def do_bt(self, arg: str) -> None:
        """bt [WORDS]   best-effort backtrace: pc, lr, then return addresses found on the stack (scan WORDS, default 256)."""
        a = self._args(arg)
        words = self._int(a[0]) if a else 256
        core = self.chip.core
        pc, lr, sp = core.read_reg("pc"), core.read_reg("lr"), core.read_reg("sp")
        sym = self.symbols.lookup
        self.p(f"#0  pc 0x{pc:08X} {sym(pc)}")
        if lr >= 0xFFFFFFE0:
            self.p(f"#1  lr 0x{lr:08X} (EXC_RETURN: in exception handler)")
        else:
            self.p(f"#1  lr 0x{lr:08X} {sym(lr)}")
        flash = self._flash_ranges()
        ram_end = None
        for r in self.chip.bus.memory_regions():
            if r.base <= sp < r.end:
                ram_end = r.end
        if ram_end is None:
            self.p("(sp not in RAM; stop)")
            return
        n = min(words, (ram_end - sp) // 4)
        data = self.chip.bus.read_bytes(sp, n * 4)
        k = 2
        for i in range(n):
            v = int.from_bytes(data[4 * i:4 * i + 4], "little")
            if v >= 0xFFFFFFE0:
                self.p(f"    [sp+0x{4 * i:x}] 0x{v:08X} EXC_RETURN (exception frame follows)")
                continue
            if not v & 1 or not any(b <= v < e for b, e in flash):
                continue
            if not self._is_call_site(v):
                continue
            self.p(f"#{k:<2} [sp+0x{4 * i:x}] 0x{v:08X} {sym(v)}")
            k += 1

    # ---- UART / ADC ------------------------------------------------------------------------------------------
    def _uart_names(self) -> list[str]:
        return list(self.s.chip.uart_blocks())

    def _chip_ids(self) -> list[str]:
        ctxs = getattr(self.s, "ctxs", None)
        return list(ctxs) if ctxs else []

    def _all_uart_names(self) -> list[str]:
        """UART names; chip-qualified ("B.UART0") for every chip when on a board."""
        ids = self._chip_ids()
        if len(ids) <= 1:
            return self._uart_names()
        out = []
        for cid, ctx in self.s.ctxs.items():
            out += [f"{cid}.{n}" for n in ctx.chip.uart_blocks()]
        return out

    @staticmethod
    def _uart_token(tok: str) -> str | None:
        """'UART1' / 'b.uart0' -> normalised name ('UART1' / 'b.UART0'), else None."""
        m = re.fullmatch(r"([A-Za-z0-9_]+\.)?((?:UART|LPUART|USART|SCI)\w*)", tok or "", re.IGNORECASE)
        if not m:
            return None
        return (m.group(1) or "") + m.group(2).upper()

    def do_uart(self, arg: str) -> None:
        r"""uart [NAME] TEXT   send TEXT to a UART receiver (default UART0); escapes \n \r \t \xNN.
        Quote TEXT to keep spaces at the ends: uart UART1 "hello\r\n". The bytes go out from now, one per
        character time of that UART, during the next `run`; bytes it is not ready for are dropped (`events uart`)."""
        rest = arg.strip()
        uart = "UART0"
        names = self._uart_names()
        first, _, tail = rest.partition(" ")
        tok = self._uart_token(first)
        if first.upper() in names or tok is not None:
            uart, rest = (tok or first.upper()), tail.strip()
        if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in "\"'":
            rest = rest[1:-1]
        data = unescape(rest)
        self.s.uart_input(data, uart)
        self.p(f"{uart} <- {data!r} ({len(data)} bytes, sent at the UART's baud while the emulation runs)")

    def do_host(self, arg: str) -> None:
        """host   host serial bridges (PTY / serial / TCP; --uart-pty etc. or usb_serial_adapter parts) and pacing."""
        from .cmd_run import print_host_summary
        st = self.s.host_status()
        if not st["endpoints"] and not st["realtime"].get("enabled"):
            self.p("no host serial endpoints; real time off")
            return
        import io
        buf = io.StringIO()
        print_host_summary(self.s, buf)
        self.p(buf.getvalue().strip("\n"))

    def do_realtime(self, arg: str) -> None:
        """realtime [on|off] [SPEED]   lock virtual time to the wall clock (x SPEED); `run` then takes wall time."""
        a = self._args(arg)
        if not a:
            rt = self.s.host_status()["realtime"]
            self.p(f"real time {'on x' + format(rt['speed'], 'g') if rt.get('enabled') else 'off'}")
            return
        on, speed = True, None
        for tok in a:
            t = tok.lower()
            if t in ("on", "1", "true", "yes"):
                on = True
            elif t in ("off", "0", "false", "no"):
                on = False
            else:
                speed = float(t.lstrip("x"))
        self.s.set_realtime(on, speed)
        self.do_realtime("")

    def do_uartout(self, arg: str) -> None:
        """uartout [NAME]   everything NAME (default: all UARTs with output) has transmitted so far."""
        a = self._args(arg)
        names = [self._uart_token(a[0]) or a[0].upper()] if a else self._all_uart_names()
        shown = False
        for n in names:
            try:
                out = self.s.uart_output(n)
            except Exception as e:
                self.p(f"{n}: {e}")
                continue
            if out or a:
                shown = True
                self.p(f"--- {n} ({len(out)} bytes) ---")
                self.p(bytes(out).decode("utf-8", "replace"))
        if not shown:
            self.p("(no UART output)")

    def do_adc(self, arg: str) -> None:
        """adc CHANNEL VALUE   set an analog input: integer = raw ADC code (adc ADC0_SE14 2048), decimal or V/mV
        suffix = volts (adc PA0 1.65V, adc CMP0_IN0 1200mV; comparator inputs take volts only)."""
        from .session import parse_adc_value
        a = self._args(arg)
        if len(a) == 1 and "=" in a[0]:
            a = a[0].split("=", 1)
        if len(a) != 2:
            raise ValueError("usage: adc CHANNEL VALUE")
        v = parse_adc_value(a[1])
        self.s.set_adc(a[0], v)
        self.p(f"{a[0]} = {v if isinstance(v, int) else f'{v:g} V'}")

    # ---- events / state --------------------------------------------------------------------------------------
    def do_events(self, arg: str) -> None:
        """events [N] [kind,...]   last N events (default 20), optionally filtered (pin,uart,irq,reset,fault,info,warn,mmio)."""
        a = self._args(arg)
        n = 20
        kinds = None
        for x in a:
            if x.isdigit():
                n = int(x)
            else:
                kinds = {k.strip() for k in x.split(",") if k.strip()}
        evs = self.s.events(0, kinds)
        sel = list(evs)[-n:] if n else []
        for ev in sel:
            self.p(format_ev(ev))
        self.p(f"({len(sel)} of {len(evs)} events)")

    def do_trace(self, arg: str) -> None:
        """trace on|off|all   print events live while running (mmio excluded). Periodic signals (PWM) are summarised
        per 10 ms window like `chipem run`; `trace all` prints every edge (like `--all-edges`)."""
        a = self._args(arg)
        if not a:
            self.p(f"trace is {'on' if self.trace else 'off'}")
            return
        w = a[0].lower()
        self._set_trace(w in ("on", "1", "yes", "true", "all"), all_edges=w == "all")
        self.p(f"trace {'on' if self.trace else 'off'}{' (all edges)' if w == 'all' and self.trace else ''}")

    def do_state(self, arg: str) -> None:
        """state   time, cycles, clock, pc/sp, mode, pending exceptions."""
        st = self.s.state()
        t = st.get("t_ns", st.get("time_ns", 0.0))
        self.p(f"time      {fmt_ns(t)}   cycles {st.get('cycles', 0)}   clock {fmt_hz(st.get('hz', 0) or 0)}")
        pc = st.get("pc", 0)
        self.p(f"pc        0x{pc:08X} {self.symbols.lookup(pc)}   sp 0x{st.get('sp', 0):08X}   "
               f"mode {st.get('mode', '?')}" + (f" ({st.get('active_name')})" if st.get("active_name") else ""))
        self.p(f"primask   {st.get('primask', 0)}   basepri {st.get('basepri', 0)}   control {st.get('control', 0)}   "
               f"sleeping {st.get('sleeping', False)}   lockup {st.get('lockup', False)}")
        pend = st.get("pending") or []
        if pend:
            self.p(f"pending   {pend}")
        skip = {"pc", "sp", "msp", "psp", "xpsr", "control", "primask", "basepri", "faultmask", "cycles", "time_ns",
                "t_ns", "hz", "active", "active_name", "mode", "sleeping", "pending", "lockup"}
        extra = {k: v for k, v in st.items() if k not in skip}
        for k, v in extra.items():
            self.p(f"{k:<9} {v}")

    def do_periph(self, arg: str) -> None:
        """periph [NAME]   dump a peripheral's named registers (no read side effects); no NAME lists blocks."""
        a = self._args(arg)
        per = self.chip.peripherals
        if not a:
            for name, blk in per.items():
                self.p(f"{name:<12} 0x{blk.base:08X} +0x{blk.size:<5x} {getattr(blk, 'model', type(blk).__name__)} "
                       f"({len(getattr(blk, 'regs', {}) or {})} regs)")
            return
        name = a[0]
        blk = per.get(name) or per.get(name.upper())
        if blk is None:
            raise KeyError(f"unknown peripheral {name!r} (try 'periph' for a list)")
        self.p(f"{blk.name} @0x{blk.base:08X} model={getattr(blk, 'model', '?')}")
        regs = getattr(blk, "regs", {}) or {}
        if not regs:
            self.p("  (no named registers; stub storage)")
        for r in sorted(regs.values(), key=lambda r: r.offset):
            try:
                v = blk.get(r.name)
            except Exception:
                continue
            w = 2 * r.width
            self.p(f"  +0x{r.offset:03x} {r.name:<14} 0x{v:0{w}X}" + ("" if v == r.reset else "  *"))

    def do_vcd(self, arg: str) -> None:
        """vcd PATH   start writing pin waveforms to a VCD file (closed on quit)."""
        a = self._args(arg)
        if not a:
            raise ValueError("usage: vcd PATH")
        try:
            from . import trace
        except ImportError:
            self.p("VCD support (chipem.trace) is not available")
            return
        W = getattr(trace, "VcdWriter", None)
        if W is None:
            self.p("chipem.trace has no VcdWriter")
            return
        old = getattr(self, "_vcd", None)
        if old is not None:
            old.close()
        w = W(a[0], list(self.chip.pins.pins), self.chip.clock, board=self.chip.pins)
        board = getattr(self.s, "board", None)
        if board is not None and hasattr(w, "add_parts"):
            w.add_parts(board)
        self._vcd = w
        self.p(f"VCD -> {a[0]}")

    def do_quit(self, arg: str) -> bool:
        """quit   leave the shell."""
        return True

    do_exit = do_quit
    do_q = do_quit

    def get_names(self) -> list[str]:
        return [n for n in super().get_names() if n not in ("do_EOF", "do_q", "do_exit")]

    def do_EOF(self, arg: str) -> bool:
        self.p("")
        return True

    def close(self) -> None:
        self._set_trace(False)
        w = getattr(self, "_vcd", None)
        if w is not None and hasattr(w, "close"):
            try:
                w.close()
            except Exception:
                pass

    # completion of pin names for set/pin
    def _complete_pin(self, text: str, *_: Any) -> list[str]:
        t = text.upper()
        return sorted({pin_short(p) for p in self.s.pin_table() if pin_short(p).upper().startswith(t)})

    complete_set = complete_pin = complete_at = _complete_pin

    def complete_periph(self, text: str, *_: Any) -> list[str]:
        return [n for n in self.chip.peripherals if n.upper().startswith(text.upper())]


def banner(session: Any) -> str:
    d = session.desc
    img = getattr(session, "image", None)
    st = session.state()
    fw = Path(getattr(img, "source", "") or "").name or "(none)"
    load = f"0x{img.start:08X}" if img is not None and getattr(img, "segments", None) else "-"
    return (f"chipem shell — {d.name} ({d.package or '?'}, {d.core.cpu}{'+FPU' if d.core.fpu else ''})\n"
            f"firmware {fw} [{getattr(img, 'fmt', '?')}] load {load}   clock {fmt_hz(st.get('hz', d.core.clock_hz))}\n"
            f"pc=0x{st.get('pc', 0):08X} sp=0x{st.get('sp', 0):08X}   type 'help' for commands, Ctrl-C stops 'run'")


def add_args(p) -> None:
    p.add_argument("--exec", dest="exec_cmds", default=None, metavar="CMDS",
                   help="run ';'-separated commands non-interactively and exit")


def main(args) -> int:
    from .session import Session
    session = Session.from_args(args)
    sh = Shell(session, interactive=args.exec_cmds is None)
    try:
        print(banner(session), file=sh.stdout)
        from .hostserial import print_endpoints
        print_endpoints(session, sh.stdout)
        if getattr(args, "exec_cmds", None) is not None:
            sh.last_exit = 0
            for line in str(args.exec_cmds).split("\n"):
                if sh.onecmd(line):
                    break
            return sh.last_exit
        try:
            import readline
            readline.set_completer_delims(" \t;")
            if HISTORY.exists():
                readline.read_history_file(str(HISTORY))
        except (ImportError, OSError):
            readline = None
        while True:
            try:
                sh.cmdloop()
                break
            except KeyboardInterrupt:
                print("^C", file=sh.stdout)
        if readline is not None:
            try:
                readline.set_history_length(2000)
                readline.write_history_file(str(HISTORY))
            except OSError:
                pass
        return 0
    finally:
        sh.close()
        try:
            session.close()
        except Exception:
            pass
