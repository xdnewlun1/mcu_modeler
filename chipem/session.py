"""Session: one firmware-on-chip emulation shared by `chipem run`, `chipem shell` and `chipem serve`.

Contract: docs/CONTRACTS.md "chipem/session.py".  A Session owns a `Chip` built from a chip description, the
loaded firmware image, the user's stimulus (pin levels now / at a virtual time, UART input, ADC readings) and an
optional VCD writer.  Pin changes reported by the PinBoard are mirrored into the chip's event log as `pin`
events (fields: pin, field, old, new) so every consumer reads one stream.

Helpers: `parse_time("50ms") -> ns` (units ns|us|ms|s, default ms), `parse_level("1") -> "H"`.

UART input is what a host terminal on the RX pin would send: `uart_input()` starts transmitting now, one character
per character time of the receiving UART (chipem/periph/uartwire.py); bytes that arrive while the receiver is not
listening, or that overrun its holding register/FIFO, are lost like on silicon (`uart` events with dir 'drop').
`--uart-in` therefore starts at t=0; use `--at 100ms:UART2=help\r\n` (stimulus with a UART name instead of a pin)
to type later, e.g. once a console is up.
"""
from __future__ import annotations

import re
import sys
import threading
from pathlib import Path
from typing import Any

from .core import StopReason

__all__ = ["Session", "parse_time", "parse_level", "parse_stimulus", "parse_uart_in", "parse_uart_stimulus",
           "decode_escapes"]

_UNITS = {"ns": 1.0, "us": 1e3, "µs": 1e3, "ms": 1e6, "s": 1e9}
_TIME_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*(ns|us|µs|ms|s)?\s*$")


def parse_time(s: str | float | int) -> float:
    """'50ms' -> 5e7 ns. Units ns|us|ms|s; a bare number is milliseconds."""
    if isinstance(s, (int, float)):
        return float(s) * 1e6
    m = _TIME_RE.match(str(s))
    if not m:
        raise ValueError(f"bad time {s!r} (expected e.g. 50ms, 1.5s, 200us, 10ns)")
    return float(m.group(1)) * _UNITS[m.group(2) or "ms"]


def parse_level(s: Any) -> str | None:
    """1/H/high -> 'H', 0/L/low -> 'L', X -> 'X', Z/none/float -> None (disconnect)."""
    if s is None:
        return None
    if isinstance(s, bool) or isinstance(s, int):
        return "H" if s else "L"
    t = str(s).strip().upper()
    if t in ("1", "H", "HIGH", "ON", "TRUE"):
        return "H"
    if t in ("0", "L", "LOW", "OFF", "FALSE"):
        return "L"
    if t == "X":
        return "X"
    if t in ("Z", "NONE", "FLOAT", "FLOATING", "OPEN", "-"):
        return None
    raise ValueError(f"bad pin level {s!r} (expected 1/0/H/L/X/Z)")


def parse_stimulus(spec: str) -> tuple[float | None, str, str | None]:
    """'PTC6=1' -> (None, 'PTC6', 'H'); '50ms:PTC6=0' -> (5e7, 'PTC6', 'L')."""
    s = spec.strip()
    t_ns = None
    if ":" in s:
        t, s = s.split(":", 1)
        t_ns = parse_time(t)
    if "=" not in s:
        raise ValueError(f"bad stimulus {spec!r} (expected [TIME:]PIN=LEVEL, e.g. 50ms:PTC6=0)")
    pin, lvl = s.split("=", 1)
    pin = pin.strip()
    if not pin:
        raise ValueError(f"bad stimulus {spec!r}: missing pin name")
    return t_ns, pin, parse_level(lvl)


def decode_escapes(text: str) -> bytes:
    """C-style escapes (\\n, \\r, \\t, \\xNN, \\\\) -> bytes; other characters are encoded as UTF-8."""
    out = bytearray()
    i = 0
    simple = {"n": 10, "r": 13, "t": 9, "0": 0, "\\": 92, "'": 39, '"': 34, "a": 7, "b": 8, "e": 27}
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            n = text[i + 1]
            if n == "x" and re.match(r"[0-9a-fA-F]{2}", text[i + 2:i + 4]):
                out.append(int(text[i + 2:i + 4], 16))
                i += 4
                continue
            if n in simple:
                out.append(simple[n])
                i += 2
                continue
        out += c.encode("utf-8")
        i += 1
    return bytes(out)


def parse_uart_in(spec: str, default: str = "UART0") -> tuple[str, bytes]:
    """'[CHIP.][UARTn:]TEXT' with escapes -> (uart name, bytes), e.g. 'B.UART0:hi\\n' -> ('B.UART0', b'hi\\n')."""
    m = re.match(r"^((?:[A-Za-z0-9_]+\.)?(?:(?:LP)?US?ART\d+|SCI[A-D]\d*)):(.*)$", spec, re.S | re.I)
    if m:
        return m.group(1).upper(), decode_escapes(m.group(2))
    return default, decode_escapes(spec)


_UART_NAME = r"(?:[A-Za-z0-9_]+\.)?(?:(?:LP)?US?ART\d+|SCI[A-D]\d*|SCI-[A-D])"
_UART_STIM_RE = re.compile(r"^\s*(?:([0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?\s*(?:ns|us|µs|ms|s)?)\s*:)?\s*(" + _UART_NAME
                           + r")\s*=(.*)$", re.S | re.I)


def parse_uart_stimulus(spec: str) -> tuple[float | None, str, bytes] | None:
    """'[TIME:][CHIP.]UARTn=TEXT' -> (t_ns or None, uart, bytes); None if `spec` does not name a UART.

    '100ms:UART2=help\\r\\n' -> (1e8, 'UART2', b'help\\r\\n'); 'B.USART1=hi' -> (None, 'B.USART1', b'hi')."""
    m = _UART_STIM_RE.match(spec)
    if not m:
        return None
    t = m.group(1)
    return (parse_time(t) if t else None), m.group(2).upper(), decode_escapes(m.group(3))


def _split_adc(spec: str) -> tuple[str, int]:
    if "=" not in spec:
        raise ValueError(f"bad --adc {spec!r} (expected CHANNEL=VALUE, e.g. ADC0_SE14=2048)")
    ch, v = spec.split("=", 1)
    return ch.strip(), int(v.strip(), 0)


class ChipContext:
    """The per-chip half of a Session: the Chip, its firmware image and the user's per-chip state.

    A single-chip Session has one context (id "A"); a board Session has one per board chip."""

    def __init__(self, cid: str, chip: Any, firmware: str | Path | None = None, *, load_addr: int | None = None,
                 fmt: str | None = None, vtor: int | None = None, clock_hz: float | None = None,
                 precise: bool = False, log_mmio: bool = False, vcd_path: str | Path | None = None,
                 quiet: bool = False):
        from .chip import Chip
        from .cli import load_desc, load_image

        self.id = cid
        self.desc = load_desc(str(chip)) if not hasattr(chip, "core") else chip
        self.image = None
        if firmware is not None:
            self.image = load_image(str(firmware), self.desc, load_addr, fmt, quiet=quiet)
        if vtor is None and self.image is not None and self.image.fmt == "bin" and self.image.unit_bytes == 1:
            vtor = self.image.start                     # raw image: vector table at its start
        self.chip = Chip(self.desc, clock_hz=clock_hz, precise=precise, vtor=vtor)
        chip_ = self.chip
        self.vtor = vtor
        self.clock_override = clock_hz
        self.adc_values: dict[str, int] = {}
        self.pending_uart: dict[str, bytearray] = {}
        self.irq_counts: dict[int, int] = {}
        self.devices: list = []                    # board devices (SPI flash, ...)
        self.unsubs: list = []
        self.reset_pins: dict[int, tuple] = {}     # pin num -> (owner, direction, pull) right after the first reset

        # pin changes -> event log (single stream for CLI/VCD/web)
        if chip_.pins is not None and not getattr(chip_, "_pin_log_bridge", False):
            log = chip_.log

            def _bridge(ev, _log=log):
                _log.event("pin", ev.t_ns, pin=ev.pin, field=ev.field, old=ev.old, new=ev.new)
            self.unsubs.append(chip_.pins.subscribe(_bridge))
            chip_._pin_log_bridge = True

        # interrupt statistics (chained onto any existing arch trace hook)
        prev = getattr(chip_.arch, "trace", None)
        counts = self.irq_counts

        def _trace(kind, exc, _prev=prev):
            if kind == "enter":
                counts[exc] = counts.get(exc, 0) + 1
            if _prev is not None:
                _prev(kind, exc)
        chip_.arch.trace = _trace

        if log_mmio:
            chip_.log_mmio = True
            for blk in chip_.peripherals.values():
                blk.log_all = True
        if clock_hz:
            self.apply_clock_override()

        self.vcd = None
        if vcd_path is not None and chip_.pins is not None:
            from .trace import VcdWriter
            self.vcd = VcdWriter(vcd_path, list(chip_.pins.pins), chip_.clock, board=chip_.pins)

        if self.image is not None:
            chip_.load_firmware(self.image)
            if vtor is not None and hasattr(chip_.arch, "reset_vtor"):
                chip_.arch.reset_vtor = vtor
            self.reset()

    def apply_clock_override(self) -> None:
        hz = self.clock_override
        if not hz:
            return
        chip = self.chip
        chip.clock_override = float(hz)
        for name in ("MCG", "SYSCTRL"):              # clock-tree models stop driving the core clock
            blk = chip.peripherals.get(name)
            if blk is not None and hasattr(blk, "frozen"):
                blk.frozen = True
        if chip.clock.hz != float(hz):
            chip.clock.set_hz(float(hz))

    def reset(self) -> None:
        self.chip.reset()
        self.apply_clock_override()
        self.deliver_pending_uart()
        if not self.reset_pins and self.chip.pins is not None:
            self.reset_pins = {p.num: (p.owner, p.direction, p.pull) for p in self.chip.pins.pins}

    def deliver_pending_uart(self) -> None:
        for uart, buf in list(self.pending_uart.items()):
            blk = self.chip.peripherals.get(uart)
            if blk is not None and hasattr(blk, "feed") and buf:
                blk.feed(bytes(buf))
                del self.pending_uart[uart]

    def warn(self, msg: str) -> None:
        self.chip.log.event("warn", self.chip.clock.now_ns(), msg=msg)

    def close(self) -> None:
        if self.vcd is not None:
            self.vcd.close()
            self.vcd = None


def _ctx_prop(name: str, doc: str):
    def fget(self):
        return getattr(self.ctxs[self.view], name)

    def fset(self, value):
        setattr(self.ctxs[self.view], name, value)
    return property(fget, fset, doc=doc)


class Session:
    """One emulation shared by run/shell/serve: a single chip (classic) or a board of several chips.

    Names of pins, UARTs, SPI blocks and ADC channels may be qualified with a chip id ("B.PTC6", "B.UART0",
    "B.ADC0_SE14"); unqualified names refer to `view` (the first chip unless changed)."""

    def __init__(self, chip: str | Path, firmware: str | Path | None = None, *, load_addr: int | None = None,
                 fmt: str | None = None, vtor: int | None = None, clock_hz: float | None = None,
                 precise: bool = False, log_mmio: bool = False, vcd_path: str | Path | None = None,
                 quiet: bool = False, chip_id: str = "A"):
        ctx = ChipContext(chip_id, chip, firmware, load_addr=load_addr, fmt=fmt, vtor=vtor, clock_hz=clock_hz,
                          precise=precise, log_mmio=log_mmio, vcd_path=vcd_path, quiet=quiet)
        self._setup([ctx], None)

    def _setup(self, ctxs: list[ChipContext], board) -> None:
        self.ctxs: dict[str, ChipContext] = {c.id: c for c in ctxs}
        self.chips: dict[str, Any] = {c.id: c.chip for c in ctxs}
        self.board = board                         # chipem.board.Board or None (single chip)
        self.board_desc = None
        self.view: str = ctxs[0].id                # chip that unqualified names / `session.chip` refer to
        self.pin_scope: str | None = None          # restrict pin_table() to one chip (web UI)
        self.running = False
        self._run_lock = threading.Lock()

    # per-chip attributes of the view chip (backwards compatible single-chip API)
    chip = _ctx_prop("chip", "the view chip (first chip of a board)")
    desc = _ctx_prop("desc", "ChipDescription of the view chip")
    image = _ctx_prop("image", "FirmwareImage of the view chip")
    vtor = _ctx_prop("vtor", "reset VTOR override of the view chip")
    clock_override = _ctx_prop("clock_override", "fixed core clock of the view chip")
    adc_values = _ctx_prop("adc_values", "ADC readings set on the view chip")
    irq_counts = _ctx_prop("irq_counts", "exception entry counts of the view chip")
    devices = _ctx_prop("devices", "devices attached to the view chip")
    reset_pins = _ctx_prop("reset_pins", "pin state of the view chip after the first reset")
    vcd = _ctx_prop("vcd", "VCD writer of the view chip")

    @property
    def log(self):
        """The event stream to subscribe to: the board's merged log, or the chip's log."""
        return self.board.log if self.board is not None else self.chip.log

    @property
    def is_board(self) -> bool:
        return self.board is not None

    # ------------------------------------------------------------------ construction from argparse / board files
    @classmethod
    def from_args(cls, args) -> "Session":
        g = lambda name, d=None: getattr(args, name, d)   # noqa: E731
        board = g("board")
        if board:
            if g("firmware"):
                raise ValueError("a firmware argument cannot be combined with --board "
                                 "(give each chip a `firmware:` in the board file)")
            q = g("quantum")
            s = cls.from_board(board, precise=bool(g("precise", False)), log_mmio=bool(g("log_mmio", False)),
                               vcd_path=g("vcd"), quantum_ns=parse_time(q) if q else None, clock_hz=g("clock"))
        else:
            if hasattr(args, "board") and g("firmware") is None:
                raise ValueError("missing firmware argument (or use --board FILE)")
            s = cls(g("chip"), g("firmware"), load_addr=g("load_addr"), fmt=g("format"), vtor=g("vtor"),
                    clock_hz=g("clock"), precise=bool(g("precise", False)), log_mmio=bool(g("log_mmio", False)),
                    vcd_path=g("vcd"))
        for spec in g("spi_flash", None) or []:
            s.attach_spi_flash(spec)
        for spec in g("part", None) or []:
            s.add_part_spec(spec)
        for spec in g("set", None) or []:
            s.apply_stimulus(spec)
        for spec in g("at", None) or []:
            s.apply_stimulus(spec)
        for spec in g("uart_in", None) or []:
            uart, data = parse_uart_in(spec)
            s.uart_input(data, uart)
        for spec in g("adc", None) or []:
            ch, v = _split_adc(spec)
            s.set_adc(ch, v)
        s._vcd_parts()
        return s

    @classmethod
    def from_board(cls, board, *, precise: bool = False, log_mmio: bool = False,
                   vcd_path: str | Path | None = None, quantum_ns: float | None = None,
                   clock_hz: float | None = None, quiet: bool = False) -> "Session":
        """Build a multi-chip session from a board YAML path or a chipem.board.BoardDesc."""
        from .board import Board, BoardDesc, load_board, spi_flash_spec
        bd = board if isinstance(board, BoardDesc) else load_board(board)
        ctxs = []
        for spec in bd.chips:
            vcd = None
            if vcd_path is not None:
                p = Path(vcd_path)
                vcd = p.with_name(f"{p.stem}.{spec.id}{p.suffix or '.vcd'}")
            ctxs.append(ChipContext(spec.id, spec.chip, spec.firmware, load_addr=spec.load_addr, fmt=spec.format,
                                    vtor=spec.vtor, clock_hz=spec.clock_hz or clock_hz, precise=precise,
                                    log_mmio=log_mmio, vcd_path=vcd, quiet=quiet))
        brd = Board({c.id: c.chip for c in ctxs}, quantum_ns=quantum_ns or bd.quantum_ns, name=bd.name)
        s = cls.__new__(cls)
        s._setup(ctxs, brd)
        s.board_desc = bd
        brd.desc = bd                              # chip specs/devices/stimulus for Board.to_yaml()
        for n in bd.nets:
            brd.add_net(n.name, n.pins, pull=n.pull, drive=n.drive)
        for ln in bd.links:
            brd.add_uart_link(ln.a, ln.b, bidir=ln.bidir)
        for ps in bd.parts:
            brd.add_part(ps.id, ps.type, ps.params, ps.ports)
        for d in bd.devices:
            s.attach_spi_flash(spi_flash_spec(d), chip=d.chip)
        for st in bd.stimulus:
            s.apply_stimulus(st)
        s._vcd_parts()
        return s

    # ------------------------------------------------------------------ parts (switches, LEDs, logic; chipem/parts)
    def ensure_board(self):
        """The Board hosting parts. A single-chip session gets a one-chip Board on first use (transparently: the
        chip runs as before; `log` becomes the board's merged log, `is_board` turns True)."""
        if self.board is not None:
            return self.board
        from .board import Board, BoardChipSpec, BoardDesc
        ctx = self.ctxs[self.view]
        brd = Board({ctx.id: ctx.chip}, name=str(getattr(ctx.desc, "name", "board")).lower())
        src = getattr(ctx.image, "source", None)
        brd.desc = BoardDesc(name=brd.name, path=None, chips=[BoardChipSpec(
            id=ctx.id, chip=str(getattr(ctx.desc, "name", "?")).lower(), firmware=str(src) if src else None,
            clock_hz=ctx.clock_override, vtor=ctx.vtor)])
        self.board = brd
        self._vcd_parts()
        return brd

    @property
    def parts(self) -> dict:
        return self.board.parts if self.board is not None else {}

    def find_part(self, name: str):
        return self.board.find_part(name) if self.board is not None else None

    def add_part(self, part_id: str | None, type_name: str, params: dict | None = None,
                 ports: dict | None = None):
        """Board.add_part on this session's board (created for a single chip if needed)."""
        return self.ensure_board().add_part(part_id, type_name, params, ports)

    def add_part_spec(self, spec: str):
        """CLI `--part ID:TYPE[:KEY=VALUE,...]` (see chipem.board.parse_part_spec)."""
        from .board import parse_part_spec
        ps = parse_part_spec(spec)
        return self.add_part(ps.id, ps.type, ps.params, ps.ports)

    def set_part(self, part_id: str, **values) -> dict:
        if self.board is None:
            raise ValueError(f"no part {part_id!r} (no parts on this session)")
        return self.board.set_part(part_id, **values)

    def _vcd_parts(self) -> None:
        """Let the view chip's VCD trace the board's parts (a `parts` scope; parts added later, from the web or the
        shell, are declared when they appear: the VCD header is written at close)."""
        vcd = self.ctxs[self.view].vcd
        if vcd is not None and self.board is not None and hasattr(vcd, "add_parts"):
            vcd.add_parts(self.board)

    # ------------------------------------------------------------------ name resolution
    def resolve(self, name: str) -> tuple[ChipContext, str]:
        """'B.PTC6' -> (context of chip B, 'PTC6'); unqualified -> (view chip, name)."""
        if len(self.ctxs) == 1 and "." not in str(name):
            return self.ctxs[self.view], str(name).strip()
        from .board import split_qualified
        cid, rest = split_qualified(str(name), list(self.ctxs), self.view)
        return self.ctxs[cid], rest

    def set_view(self, cid: str) -> None:
        for c in self.ctxs:
            if c.upper() == str(cid).strip().upper():
                self.view = c
                return
        raise ValueError(f"unknown chip id {cid!r} (chips: {', '.join(self.ctxs)})")

    def _ctx(self, chip: str | None) -> ChipContext:
        if chip is None:
            return self.ctxs[self.view]
        for c, ctx in self.ctxs.items():
            if c.upper() == str(chip).strip().upper():
                return ctx
        raise ValueError(f"unknown chip id {chip!r} (chips: {', '.join(self.ctxs)})")

    # ------------------------------------------------------------------ board devices
    def attach_spi_flash(self, spec: str, chip: str | None = None):
        """Attach a SPI NOR flash: "[CHIP.]SPI0:cs=PTC4,jedec=0x202015,size=2M,file=img.bin" (see chipem.devices)."""
        from .devices.spi_flash import attach_from_spec
        if chip is not None:
            ctx = self._ctx(chip)
        else:
            ctx, spec = self.resolve(spec)
        dev = attach_from_spec(ctx.chip, spec)
        ctx.devices.append(dev)
        return dev

    # ------------------------------------------------------------------ clock override
    def _apply_clock_override(self) -> None:
        for ctx in self.ctxs.values():
            ctx.apply_clock_override()

    # ------------------------------------------------------------------ control
    def reset(self) -> None:
        for ctx in self.ctxs.values():
            ctx.reset()
        if self.board is not None:
            self.board.reset_parts()
            for net in self.board.nets:
                net.update()

    def _run(self, fn, *a) -> StopReason:
        with self._run_lock:
            self.running = True
            try:
                for ctx in self.ctxs.values():
                    ctx.deliver_pending_uart()
                r = fn(*a)
            finally:
                self.running = False
            for ctx in self.ctxs.values():
                if ctx.clock_override and ctx.chip.clock.hz != ctx.clock_override:
                    ctx.apply_clock_override()
            return r

    def run_for(self, ns: float) -> StopReason:
        if self.board is not None:
            return self._run(self.board.run_for, float(ns))
        return self._run(self.chip.run_for, float(ns))

    def run_cycles(self, n: int) -> StopReason:
        if self.board is not None:          # cycles of the view chip, converted to board time
            return self._run(self.board.run_for, int(n) * 1e9 / self.chip.clock.hz)
        return self._run(self.chip.run_cycles, int(n))

    def step(self) -> StopReason:
        """One instruction on the view chip (other board chips do not move)."""
        return self._run(self.chip.step)

    def stop(self) -> None:
        """Thread-safe: ask a running run_for/run_cycles to return early (StopReason.USER)."""
        if self.running:
            if self.board is not None:
                self.board.stop()
            else:
                self.chip.stop()

    # ------------------------------------------------------------------ stimulus
    def set_pin(self, name: str, level) -> None:
        lvl = level if level in ("H", "L", "X", None) else parse_level(level)
        ctx, pin = self.resolve(name)
        if self.board is not None:
            self.board.set_pin(ctx.id, pin, lvl)
        else:
            ctx.chip.pins.set_external(pin, lvl)

    def schedule_pin(self, t_ns: float, name: str, level) -> None:
        lvl = level if level in ("H", "L", "X", None) else parse_level(level)
        ctx, pin = self.resolve(name)
        ctx.chip.pins.get(pin)                           # validate the name now
        if t_ns <= ctx.chip.clock.now_ns():
            self.set_pin(name, lvl)
            return
        if self.board is not None:
            ctx.chip.sched.at_ns(float(t_ns), self.board.set_pin, ctx.id, pin, lvl)
        else:
            ctx.chip.sched.at_ns(float(t_ns), ctx.chip.pins.set_external, pin, lvl)

    def apply_stimulus(self, spec: str) -> None:
        """'[TIME:]PIN=LEVEL' drives a pin; '[TIME:]UARTn=TEXT' types TEXT into a UART receiver (paced at its baud);
        '[TIME:]PART=VALUE' acts on a board part (switch level, button pressed/released, pulse period, ...) and
        '[TIME:]PART.KEY=VALUE' sets one of its values. Part ids are resolved before pin names."""
        ps = self._part_stimulus(spec)
        if ps is not None:
            t_ns, part, key, value = ps
            if t_ns is None or t_ns <= self.board.part_now_ns():
                self._apply_part(part.id, key, value)
            else:
                self.board.sched_at_ns(float(t_ns), self._apply_part, part.id, key, value)
            return
        u = parse_uart_stimulus(spec)
        if u is not None:
            t_ns, uart, data = u
            if t_ns is None:
                self.uart_input(data, uart)
            else:
                self.schedule_uart(t_ns, uart, data)
            return
        t_ns, pin, lvl = parse_stimulus(spec)
        if t_ns is None:
            self.set_pin(pin, lvl)
        else:
            self.schedule_pin(t_ns, pin, lvl)

    _PART_STIM = re.compile(r"^\s*(?:([0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?\s*(?:ns|us|µs|ms|s)?)\s*:)?\s*"
                            r"([A-Za-z0-9_\-]+)(?:\.([A-Za-z_][A-Za-z0-9_]*))?\s*=(.*)$", re.S)

    def _part_stimulus(self, spec: str):
        """(t_ns|None, Part, key|None, value) if `spec` names a part, else None."""
        if self.board is None or not self.board.parts:
            return None
        m = self._PART_STIM.match(spec)
        if not m:
            return None
        part = self.board.find_part(m.group(2))
        if part is None:
            return None
        if m.group(3) is not None and m.group(3) not in part.params and m.group(3) not in (
                "level", "pressed", "count", "clear", "color", "on"):
            return None                     # "B.PTC6"-style name that happens to start with a part id
        t = m.group(1)
        return (parse_time(t) if t else None), part, m.group(3), m.group(4).strip()

    def _apply_part(self, part_id: str, key: str | None, value: str) -> dict:
        b = self.board
        part = b.part(part_id)
        if key is None:
            st = part.stimulus(value)
            b.part_event(part, action="set", **{k: v for k, v in st.items() if not isinstance(v, (dict, list))})
            return st
        return b.set_part(part.id, **{key: value})

    def _uart_block(self, uart: str):
        ctx, uart = self.resolve(uart)
        uart = ctx.chip.find_uart(uart, fallback=uart.upper() == "UART0") or uart.upper()
        blk = ctx.chip.peripherals.get(uart)
        if blk is None:
            raise ValueError(f"no peripheral named {uart}")
        return ctx, uart, blk

    def uart_input(self, data: bytes | str, uart: str = "UART0") -> None:
        """A host on `uart`'s RX pin starts sending `data` now, one character per character time of that UART.
        Bytes the receiver cannot take when they arrive are dropped (see chipem/periph/uartwire.py)."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        ctx, uart, blk = self._uart_block(uart)
        if hasattr(blk, "feed"):
            blk.feed(bytes(data))
        else:
            ctx.pending_uart.setdefault(uart, bytearray()).extend(data)
            ctx.warn(f"{uart} has no UART model yet; {len(data)} input byte(s) held back")

    def schedule_uart(self, t_ns: float, uart: str, data: bytes | str) -> None:
        """uart_input() at virtual time t_ns (now if t_ns is not in the future)."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        ctx, name, _ = self._uart_block(uart)            # validate the name now
        if t_ns <= ctx.chip.clock.now_ns():
            self.uart_input(data, uart)
            return
        ctx.chip.sched.at_ns(float(t_ns), self.uart_input, bytes(data), uart)

    def _deliver_pending_uart(self) -> None:
        for ctx in self.ctxs.values():
            ctx.deliver_pending_uart()

    def uart_output(self, uart: str = "UART0") -> bytes:
        ctx, uart = self.resolve(uart)
        name = ctx.chip.find_uart(uart, fallback=uart.upper() == "UART0")
        if name is None and hasattr(ctx.chip.peripherals.get(uart.upper()), "output"):
            name = uart.upper()                              # UART-like model without board-link hooks
        if name is None:
            raise ValueError(f"no UART named {uart} (UARTs: {', '.join(ctx.chip.uart_blocks()) or 'none'})")
        return ctx.chip.uart_out(name)

    def set_adc(self, channel: str, value: int) -> None:
        """channel e.g. 'ADC0_SE14' or 'B.ADC0_SE14' (block name prefix; bare 'SE14' means ADC0)."""
        ctx, ch = self.resolve(channel)
        ch = ch.strip().upper()
        if "_" in ch:
            blk_name, sub = ch.split("_", 1)
        elif "ADC0" not in ctx.chip.peripherals and "ADC" in ctx.chip.peripherals:
            blk_name, sub = "ADC", ch                       # C2000: "ADCINA3" / "A3" -> block ADC
            ch = f"ADC_{ch}"
        else:
            blk_name, sub = "ADC0", ch
            ch = f"ADC0_{ch}"
        ctx.adc_values[ch] = int(value)
        blk = ctx.chip.peripherals.get(blk_name)
        fn = getattr(blk, "set_channel", None)
        if fn is None:
            ctx.warn(f"{blk_name} model has no set_channel(); ADC value {ch}={value} stored only")
            return
        try:
            ok = fn(ch, int(value))
        except (KeyError, ValueError, TypeError):
            ok = fn(sub, int(value))
        if ok is False:
            ctx.warn(f"{blk_name} rejected ADC channel {ch}")

    def _warn(self, msg: str) -> None:
        self.ctxs[self.view].warn(msg)

    # ------------------------------------------------------------------ introspection
    def pin_table(self, chip: str | None = None) -> list[dict]:
        """Pin rows of one chip, or of every board chip (scope: `pin_scope`, else all). Each row has a `chip` field;
        on a board, rows of chips other than the view chip have a qualified `short` ("B.PTC5") so names stay
        unique, and pins in a net carry `net`."""
        if chip is not None:
            ctxs = [self._ctx(chip)]
        elif self.pin_scope is not None:
            ctxs = [self._ctx(self.pin_scope)]
        else:
            ctxs = list(self.ctxs.values())
        board = self.board
        out = []
        for ctx in ctxs:
            pins = ctx.chip.pins
            if pins is None:
                continue
            qualify = board is not None and ctx.id != self.view
            for p in pins.pins:
                row = {"num": p.num, "name": p.name, "short": f"{ctx.id}.{p.id}" if qualify else p.id,
                       "kind": p.kind, "owner": p.owner, "direction": p.direction, "level": p.level(),
                       "drive": p.drive, "pull": p.pull, "external": p.external, "functions": dict(p.functions),
                       "chip": ctx.id}
                if board is not None:
                    net = board._net_of.get((ctx.id, p.id))
                    row["net"] = net.name if net is not None else None
                out.append(row)
        return out

    def _chip_state(self, ctx: ChipContext) -> dict:
        chip = ctx.chip
        st = dict(chip.state())
        st["t_ns"] = chip.clock.now_ns()
        st["cycles"] = chip.clock.cycles
        st["insns"] = getattr(chip, "insns", 0)
        st["hz"] = chip.clock.hz
        st["running"] = self.running
        st["chip"] = ctx.id
        return st

    def state(self, chip: str | None = None) -> dict:
        """State of one chip (default: the view chip); on a board also `chips` (per-chip states) and `board`."""
        st = self._chip_state(self._ctx(chip))
        b = self.board
        if b is not None and chip is None:
            st["chips"] = {cid: (dict(st) if cid == st["chip"] else self._chip_state(ctx))
                           for cid, ctx in self.ctxs.items()}
            st["board"] = {"name": b.name, "quantum_ns": b.quantum_ns, "t_ns": b.now_ns(), "skew_ns": b.skew_ns(),
                           "wiring": getattr(b, "wiring", 0),
                           "nets": [{"name": n.name, "level": n.level, "conflict": n.conflict, "pull": n.pull,
                                     "pins": [n.label(i) for i in range(len(n.members))]} for n in b.nets],
                           "links": [{"link": ln.describe(), "a_to_b": ln.bytes_ab, "b_to_a": ln.bytes_ba,
                                      "auto": getattr(ln, "auto", False), "net": getattr(ln, "net", None)}
                                     for ln in b.links],
                           "parts": [{"id": p.id, "type": p.type, "state": p.state(),
                                      "ports": {k: (n.name if (n := b.part_net(p.id, k)) is not None else None)
                                                for k in p.ports}} for p in b.parts.values()]}
            for nd, n in zip(st["board"]["nets"], b.nets):
                nd["ports"] = [f"{p.id}.{port}" for p, port in n.ports]
        return st

    def events(self, since: int = 0, kinds: set[str] | None = None, chip: str | None = None) -> list:
        """Log events with sequence number > since (pass the last seen `ev.seq` to poll incrementally).
        On a board the events come from the merged log (each has a `chip` field, "board" for board-level
        events), ordered by time; `chip` filters on that field."""
        log = self.log
        evs = getattr(log, "_events", None)
        if evs is None:
            evs = list(log)
        want = None if chip is None else (chip if chip == "board" else self._ctx(chip).id)
        out = None
        # walk backwards from the newest event and stop at `since`: incremental polls cost O(new events)
        for _ in range(3):
            res = []
            try:
                for ev in reversed(evs):
                    if getattr(ev, "seq", 0) <= since:
                        break
                    if (kinds is None or ev.kind in kinds) and (want is None or ev.fields.get("chip") == want):
                        res.append(ev)
            except RuntimeError:          # log appended to from another thread meanwhile: retry
                continue
            res.reverse()
            out = res
            break
        if out is None:
            out = [ev for ev in list(evs) if getattr(ev, "seq", 0) > since and (kinds is None or ev.kind in kinds)
                   and (want is None or ev.fields.get("chip") == want)]
        if self.board is not None:
            out.sort(key=lambda e: e.t_ns)
        return out

    def irq_summary(self, chip: str | None = None) -> list[tuple[int, str, int]]:
        """[(exception number, name, entries)] sorted by exception number."""
        ctx = self._ctx(chip)
        arch = ctx.chip.arch
        return [(e, arch.exc_name(e), n) for e, n in sorted(ctx.irq_counts.items())]

    def close(self) -> None:
        for ctx in self.ctxs.values():
            ctx.close()
        if self.board is not None:
            self.board.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
