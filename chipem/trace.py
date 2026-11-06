"""Pin waveform tracing (VCD) and a live, human-readable event printer.

VcdWriter(path, pins, clock, board=None): timescale 1 ns, one 1-bit wire per GPIO-capable pin (H -> 1, L -> 0,
X -> x), plus a string variable per pin in scope `owner` carrying its owner ("GPIO", "UART0_TX", "disabled"), so
GTKWave shows function changes next to the level (on by default; `owners=False` writes levels only).  Feed it PinBoard events via
`on_pin_event` (done automatically when `board` is given).  `close()` writes the file: value changes are spooled
to an anonymous temporary file during the run and the header is written last, so that `add_parts(board)` can trace
a board's parts in a `parts` scope (one wire per part output port, per LED and per probe input) including parts
added at any time later (web, shell); a part is `x` before it existed.

PinEventPrinter(out): subscribe to a chip's EventLog (`printer.attach(log)`), prints lines like
    [    12.345 ms] PTC5  L -> H
    [     0.000 ms] PTC5  owner disabled -> GPIO (out)
    [   100.000 ms] UART0 TX: "hello\\r\\n"
    [  1200.000 ms] RESET watchdog
Consecutive UART bytes of one UART within the same millisecond are coalesced into one line; an owner change
and the direction/pull changes that accompany it (same pin, same instant) are merged too.  Call `flush()` at
the end of a run.
"""
from __future__ import annotations

import sys
from typing import Any, Iterable, TextIO

_VCD_VAL = {"H": "1", "L": "0", "X": "x", None: "x"}


def _vcd_ids():
    chars = [chr(c) for c in range(33, 127)]
    n = 0
    while True:
        s, k = "", n
        while True:
            s += chars[k % len(chars)]
            k //= len(chars)
            if k == 0:
                break
        yield s
        n += 1


class VcdWriter:
    """VCD file of a chip's pins (and, with `add_parts`, a board's parts).

    VCD needs every `$var` before `$enddefinitions`, but parts can be added at any time (web, shell, `--part`).
    So the value changes go to an anonymous spool file next to the target while the run lasts, and `close()` writes
    the target: header (every wire ever declared), `$dumpvars`, then the spooled changes.  Memory stays constant;
    the target file is complete only after `close()`.  A part added at time t has value `x` before t and its real
    value from t on; a removed part goes back to `x`."""

    def __init__(self, path, pins: list, clock: Any, *, board: Any = None, owners: bool = True,
                 module: str = "chip"):
        import tempfile
        from pathlib import Path
        self.path = str(path)
        self.clock = clock
        self.f: TextIO | None = open(self.path, "w")          # fail early on a bad path; written at close()
        self._spool: TextIO | None = tempfile.TemporaryFile(
            "w+", dir=str(Path(self.path).resolve().parent), prefix=".chipem-vcd-", suffix=".tmp")
        self.owners = owners
        self._idgen = _vcd_ids()
        ids = self._idgen
        self.ids: dict[str, str] = {}
        self.owner_ids: dict[str, str] = {}
        self.part_ids: dict[tuple[str, str], str] = {}   # (part id, port | "on") -> VCD id
        self.pins = [p for p in pins if getattr(p, "kind", "gpio") == "gpio"]
        self._last_t: int | None = None
        self._unsub = None
        self._unsub_parts = None
        self._parts_board: Any = None
        self._watched: dict[str, Any] = {}               # part id -> Part whose listeners we joined
        hdr = [f"$scope module {module} $end\n"]
        init = []
        for p in self.pins:
            vid = next(ids)
            self.ids[p.id] = vid
            hdr.append(f"$var wire 1 {vid} {p.id} $end\n")
            init.append(f"{_VCD_VAL.get(p.level(), 'x')}{vid}\n")
        if owners:
            hdr.append("$scope module owner $end\n")
            for p in self.pins:
                vid = next(ids)
                self.owner_ids[p.id] = vid
                hdr.append(f"$var string 1 {vid} {p.id} $end\n")
                init.append(f"s{self._vstr(p.owner)} {vid}\n")
            hdr.append("$upscope $end\n")
        hdr.append("$upscope $end\n")
        self._hdr = hdr
        self._part_hdr: list[str] = []
        self._init = init
        self._body_started = False
        self._t0 = int(round(clock.now_ns())) if clock is not None else 0
        self._last_t = self._t0
        if board is not None:
            self._unsub = board.subscribe(self.on_pin_event)

    @staticmethod
    def _vstr(s: Any) -> str:
        return str(s).replace(" ", "_") or "-"

    def _w(self, line: str) -> None:
        self._body_started = True
        self._spool.write(line)

    # ---------------------------------------------------------------- parts
    def add_parts(self, board: Any) -> bool:
        """Trace `board`'s parts in a `parts` scope: one wire per part output port (`SW1.out`), per LED (`LED1`,
        1 = lit) and per probe input (`P1.in`).  Parts added to the board later are declared when they appear.
        Returns False once the writer is closed."""
        if self.f is None:
            return False
        if self._parts_board is not board:
            if self._unsub_parts is not None:
                self._unsub_parts()
            self._parts_board = board
            self._unsub_parts = board.log.subscribe(self.on_part_event, "part")
        t = board.part_now_ns() if hasattr(board, "part_now_ns") else (self.clock.now_ns() if self.clock else 0)
        for part in list(board.parts.values()):
            self._declare_part(part, t)
        return True

    def _part_wires(self, part: Any) -> list[tuple[str, str, str]]:
        """(key, VCD name, current value) per traced signal of `part`."""
        out = []
        for port, d in part.ports.items():
            if d.is_output:
                lvl = part.drive(port)
                out.append((port, f"{part.id}.{port}", "z" if lvl is None else _VCD_VAL.get(lvl, "x")))
        if part.type == "led":
            out.append(("on", part.id, "1" if part.state().get("on") else "0"))
        if part.type == "probe":
            out.append(("in", f"{part.id}.in", _VCD_VAL.get(part.inputs.get("in"), "x")))
        return out

    def _declare_part(self, part: Any, t_ns: float) -> None:
        if self._watched.get(part.id) is part:
            return
        old = self._watched.get(part.id)
        if old is not None and self.on_part_change in old.listeners:
            old.listeners.remove(self.on_part_change)
        self._watched[part.id] = part
        part.listeners.append(self.on_part_change)
        at_start = not self._body_started and int(round(t_ns)) <= self._t0
        for key, name, val in self._part_wires(part):
            vid = self.part_ids.get((part.id, key))
            if vid is None:
                vid = next(self._idgen)
                self.part_ids[(part.id, key)] = vid
                self._part_hdr.append(f"$var wire 1 {vid} {name} $end\n")
                if at_start:
                    self._init.append(f"{val}{vid}\n")
                    continue
                self._init.append(f"x{vid}\n")          # did not exist yet
            self._time(t_ns)
            self._w(f"{val}{vid}\n")

    def _undeclare_part(self, pid: str, t_ns: float) -> None:
        part = self._watched.pop(pid, None)
        if part is not None and self.on_part_change in part.listeners:
            part.listeners.remove(self.on_part_change)
        for (p, _), vid in self.part_ids.items():
            if p == pid:
                self._time(t_ns)
                self._w(f"x{vid}\n")

    def seal(self) -> None:
        """(Compatibility no-op: the header is written by close(), so parts may still be added.)"""

    def _time(self, t_ns: float) -> None:
        t = int(round(t_ns))
        if self._last_t is not None and t < self._last_t:
            t = self._last_t
        if t != self._last_t:
            self._w(f"#{t}\n")
            self._last_t = t

    def on_pin_event(self, ev: Any) -> None:
        if self.f is None:
            return
        if ev.field == "level":
            vid = self.ids.get(ev.pin)
            if vid is None:
                return
            self._time(ev.t_ns)
            self._w(f"{_VCD_VAL.get(ev.new, 'x')}{vid}\n")
        elif ev.field == "owner" and self.owners:
            vid = self.owner_ids.get(ev.pin)
            if vid is None:
                return
            self._time(ev.t_ns)
            self._w(f"s{self._vstr(ev.new)} {vid}\n")

    def on_part_event(self, ev: Any) -> None:
        """Board log `part` events: parts appearing / disappearing (value changes come from part listeners)."""
        if self.f is None or self._parts_board is None:
            return
        f = ev.fields
        act = f.get("action")
        if act == "add":
            part = self._parts_board.parts.get(f.get("part"))
            if part is not None:
                self._declare_part(part, ev.t_ns)
        elif act == "remove":
            self._undeclare_part(f.get("part"), ev.t_ns)

    def on_part_change(self, part: Any, f: dict) -> None:
        """Part listener: every state change of a traced part (also of `quiet` parts, which log nothing)."""
        if self.f is None:
            return
        pid = part.id
        t = part.now()
        if "on" in f and (pid, "on") in self.part_ids:
            self._time(t)
            self._w(f"{'1' if f['on'] else '0'}{self.part_ids[(pid, 'on')]}\n")
        port = f.get("port")
        if port is not None and "level" in f and (pid, port) in self.part_ids:
            self._time(t)
            lvl = f["level"]
            self._w(f"{'z' if lvl == 'Z' else _VCD_VAL.get(lvl, 'x')}{self.part_ids[(pid, port)]}\n")

    def close(self) -> None:
        if self.f is None:
            return
        for u in (self._unsub, self._unsub_parts):
            if u is not None:
                u()
        self._unsub = self._unsub_parts = None
        for part in self._watched.values():
            if self.on_part_change in part.listeners:
                part.listeners.remove(self.on_part_change)
        self._watched = {}
        if self.clock is not None:
            self._time(self.clock.now_ns())       # mark the end of the trace
        f, sp = self.f, self._spool
        self.f = None
        try:
            f.write("$date chipem $end\n$version chipem VCD writer $end\n$timescale 1ns $end\n")
            f.writelines(self._hdr)
            if self._part_hdr:
                f.write("$scope module parts $end\n")
                f.writelines(self._part_hdr)
                f.write("$upscope $end\n")
            f.write("$enddefinitions $end\n")
            f.write(f"#{self._t0}\n$dumpvars\n")
            f.writelines(self._init)
            f.write("$end\n")
            sp.flush()
            sp.seek(0)
            import shutil
            shutil.copyfileobj(sp, f)
        finally:
            f.close()
            sp.close()
            self._spool = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------------------------------------------
def fmt_t(t_ns: float) -> str:
    return f"[{t_ns / 1e6:10.3f} ms]"


def escape_bytes(data: bytes) -> str:
    out = []
    for b in data:
        if b == 0x0A:
            out.append("\\n")
        elif b == 0x0D:
            out.append("\\r")
        elif b == 0x09:
            out.append("\\t")
        elif b == 0x22:
            out.append('\\"')
        elif b == 0x5C:
            out.append("\\\\")
        elif 0x20 <= b < 0x7F:
            out.append(chr(b))
        else:
            out.append(f"\\x{b:02x}")
    return "".join(out)


def format_part_event(f: dict) -> str:
    """'SW1 level=H', 'LED1 on', 'G1.out L', 'CNT1 count=3', 'SW1 added (switch)'."""
    pid = f.get("part", "?")
    act = f.get("action")
    if act == "add":
        return f"PART {pid} added"
    if act == "remove":
        return f"PART {pid} removed"
    if "on" in f and act is None:
        return f"{pid} {'ON' if f['on'] else 'off'}"
    if f.get("port") is not None and "level" in f and act is None:
        return f"{pid}.{f['port']} {f['level']}"
    rest = " ".join(f"{k}={v}" for k, v in f.items() if k not in ("part", "chip", "port", "action"))
    return f"{pid} {act + ' ' if act else ''}{rest}".rstrip()


class PinEventPrinter:
    DEFAULT_KINDS = frozenset({"pin", "uart", "irq", "reset", "fault", "warn", "info"})

    def __init__(self, out: TextIO | None = None, kinds: Iterable[str] | None = None, *, pin_width: int = 5):
        self.out = out if out is not None else sys.stdout
        self.kinds = frozenset(kinds) if kinds is not None else self.DEFAULT_KINDS
        self.pin_width = pin_width
        self._uart: tuple[str, str, int, float, bytearray] | None = None   # (uart, dir, ms bucket, t0, bytes)
        self._cfg: dict | None = None        # pending pin configuration line
        self._unsub = None
        self.lines = 0

    def attach(self, log: Any) -> "PinEventPrinter":
        self._unsub = log.subscribe(self)
        return self

    def detach(self) -> None:
        self.flush()
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    def _emit(self, line: str) -> None:
        self.out.write(line + "\n")
        self.lines += 1

    def _flush_uart(self) -> None:
        u = self._uart
        if u is not None:
            self._uart = None
            uart, d, _, t0, data = u
            self._emit(f"{fmt_t(t0)} {uart} {d.upper()}: \"{escape_bytes(bytes(data))}\"")

    def _flush_cfg(self) -> None:
        c = self._cfg
        if c is not None:
            self._cfg = None
            parts = []
            if "owner" in c:
                o, n = c["owner"]
                s = f"owner {o} -> {n}"
                if "direction" in c:
                    s += f" ({c['direction'][1]})"
                parts.append(s)
            elif "direction" in c:
                o, n = c["direction"]
                parts.append(f"dir {o} -> {n}")
            if "pull" in c:
                o, n = c["pull"]
                parts.append(f"pull {o or 'none'} -> {n or 'none'}")
            self._emit(f"{fmt_t(c['t'])} {c['pin']:<{self.pin_width}} " + ", ".join(parts))

    def flush(self) -> None:
        self._flush_cfg()
        self._flush_uart()
        try:
            self.out.flush()
        except Exception:
            pass

    def __call__(self, ev: Any) -> None:
        kind = ev.kind
        if kind not in self.kinds:
            return
        f = ev.fields
        cid = f.get("chip")                  # board sessions: events carry the chip id ("board" for the board)
        tag = f"{cid}: " if cid else ""
        if kind == "uart":
            uart = str(f.get("uart") or f.get("name") or "UART")
            if cid:
                uart = f"{cid}.{uart}"
            d = str(f.get("dir", "tx"))
            data = f.get("byte")
            data = bytes([data]) if isinstance(data, int) else bytes(f.get("data") or b"")
            bucket = int(ev.t_ns // 1e6)
            u = self._uart
            self._flush_cfg()
            if u is not None and u[0] == uart and u[1] == d and u[2] == bucket:
                u[4].extend(data)
                return
            self._flush_uart()
            self._uart = (uart, d, bucket, ev.t_ns, bytearray(data))
            return
        if kind == "pin":
            fld = f.get("field", "level")
            pin = f.get("pin")
            if cid:
                pin = f"{cid}.{pin}"
            if fld in ("owner", "direction", "pull"):
                c = self._cfg
                if c is not None and (c["pin"] != pin or c["t"] != ev.t_ns or fld in c):
                    self._flush_cfg()
                    c = None
                self._flush_uart()
                if c is None:
                    c = self._cfg = {"pin": pin, "t": ev.t_ns}
                c[fld] = (f.get("old"), f.get("new"))
                return
            self.flush_pending()
            self._emit(f"{fmt_t(ev.t_ns)} {pin:<{self.pin_width}} {f.get('old')} -> {f.get('new')}")
            return
        self.flush_pending()
        msg = f.get("msg")
        if kind == "part":
            if f.get("action") == "set":
                return                          # the resulting level/on events are printed instead
            self._emit(f"{fmt_t(ev.t_ns)} {format_part_event(f)}")
            return
        if kind == "reset":
            self._emit(f"{fmt_t(ev.t_ns)} {tag}RESET {f.get('reason', '')}".rstrip())
        elif kind == "irq":
            name = f.get("name") or f.get("irq") or ""
            self._emit(f"{fmt_t(ev.t_ns)} {tag}IRQ {name} {msg or ''}".rstrip())
        elif kind == "mmio" and not msg:
            reg = f.get("reg") or f"+0x{f.get('offset', 0):x}"
            acc = "W" if f.get("access") == "w" else "R"
            self._emit(f"{fmt_t(ev.t_ns)} {tag}MMIO {acc} {f.get('block', '')}.{reg} = 0x{int(f.get('value') or 0):x}")
        else:
            self._emit(f"{fmt_t(ev.t_ns)} {tag}{kind.upper()} {msg if msg is not None else f}")

    def flush_pending(self) -> None:
        self._flush_cfg()
        self._flush_uart()
