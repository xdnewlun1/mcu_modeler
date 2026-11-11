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
import time
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


def parse_speed(s: Any) -> float | None:
    """Emulation rate: "1", "x0.1", "0.5x", "10" -> virtual seconds per wall second; "max" / "inf" / "off" -> None
    (as fast as possible, no pacing)."""
    t = str(s).strip().lower().replace("×", "x")
    if t in ("max", "inf", "unpaced", "off", "none", "fast"):
        return None
    t = t.removeprefix("x").removesuffix("x").strip()
    try:
        v = float(t)
    except ValueError:
        raise ValueError(f"bad speed {s!r} (a factor such as 0.1, 1, 10, or max)") from None
    if not v > 0 or v == float("inf"):
        raise ValueError(f"speed must be > 0 (got {s!r}; use max for unpaced)")
    return v


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


_BREAK_TOKEN = re.compile(rb"<break(?::(\d*))?>", re.I)     # UART input: a break, optionally N bit times long

# one UART-name syntax for --uart-in and --set/--at (Chip.find_uart resolves it): UARTn, USARTn, LPUARTn, UARTA
# (= SCIA on a C2000), SCIA, SCI-A, each optionally chip-qualified ("B.SCI-A")
_UART_NAME = r"(?:[A-Za-z0-9_]+\.)?(?:(?:LP)?US?ART(?:\d+|[A-D])?|SCI-?[A-D]\d*)"   # bare "UART": 80C51 serial port
_UART_IN_RE = re.compile(r"^(" + _UART_NAME + r"):(.*)$", re.S | re.I)


def parse_uart_in(spec: str, default: str = "UART0") -> tuple[str, bytes]:
    """'[CHIP.][UARTn:]TEXT' with escapes -> (uart name, bytes), e.g. 'B.UART0:hi\\n' -> ('B.UART0', b'hi\\n')."""
    m = _UART_IN_RE.match(spec)
    if m:
        return m.group(1).upper(), decode_escapes(m.group(2))
    return default, decode_escapes(spec)


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


def parse_adc_value(text: str) -> int | float:
    """ADC stimulus value: an integer ("2048", "0x800") is a raw conversion code; a number with a decimal point or a
    unit ("1.65", "1.65V", "1650mV", "3V") is a voltage in volts (float), for models that take volts (`adc_volts`)."""
    t = str(text).strip()
    low = t.lower()
    if low.endswith("mv"):
        return float(t[:-2]) / 1000.0
    if low.endswith("v"):
        return float(t[:-1])
    try:
        return int(t, 0)
    except ValueError:
        return float(t)


def _split_adc(spec: str) -> tuple[str, int | float]:
    if "=" not in spec:
        raise ValueError(f"bad --adc {spec!r} (expected CHANNEL=VALUE, e.g. ADC0_SE14=2048 or PA0=1.65V)")
    ch, v = spec.split("=", 1)
    return ch.strip(), parse_adc_value(v)


def _ep_port(ep):
    """The HostPort of a host endpoint (UartBridge.port or a part's host_port), or None."""
    p = getattr(ep, "host_port", None)
    return p if p is not None else getattr(ep, "port", None)


def _host_serial_entry(h: dict, base) -> tuple[str, str]:
    """Board YAML host_serial entry -> (uart, transport spec): {uart: A.UART0, pty: /tmp/ttyK22 | true} |
    {uart, port: /dev/ttyUSB0, baud: 115200, format: 8N1} | {uart, tcp: 5555}."""
    uart = str(h["uart"])
    if "pty" in h:
        v = h["pty"]
        return uart, "pty" if v in (None, True, "", "auto") else f"pty:{v}"
    if "tcp" in h:
        return uart, f"tcp:{h['tcp']}"
    if "port" in h:
        spec = f"serial:{h['port']}"
        if h.get("baud") is not None:
            spec += f":{int(float(h['baud']))}"
            if h.get("format"):
                spec += f":{h['format']}"
        return uart, spec
    if "memory" in h:
        return uart, "memory"
    raise ValueError(f"board: host_serial entry {h!r} needs one of pty, port, tcp")


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
        self.adc_values: dict[str, int | float] = {}
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
            scs = getattr(chip_.arch, "scs", None)
            if self.image.fmt == "bin" and self.image.unit_bytes == 1 and scs is not None and hasattr(scs, "cpacr_reset"):
                scs.cpacr_reset = 0x00F00000    # raw image started by a bootloader that enabled the FPU (CP10/CP11)
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
        # host serial bridges (chipem/hostserial.py) and real-time pacing (chipem/realtime.py)
        self.host_bridges: list = []               # UartBridge: a host port wired to a UART (--uart-pty/port/tcp)
        self.pacer = None                          # chipem.realtime.RealTime while real-time pacing is on
        self._wake = threading.Event()             # set by host I/O threads and stop(): wakes a waiting paced run
        self._pace_stop = False
        # speed / fidelity settings and the achieved-speed meter (chipem/perf.py)
        from .perf import PerfSettings, SpeedMeter
        self.perf = PerfSettings()
        self.speed = SpeedMeter()
        self.base_quantum_ns: float | None = board.quantum_ns if board is not None else None

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
        for spec in g("sd_card", None) or []:
            s.attach_sd_card(spec)
        if g("eth_pcap", None) or g("eth_host", None):
            s.eth_link(pcap=g("eth_pcap", None), host=g("eth_host", None))
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
        from .hostserial import parse_bridge_arg
        for kind, opt in (("pty", "uart_pty"), ("serial", "uart_port"), ("tcp", "uart_tcp")):
            for spec in g(opt, None) or []:
                s.attach_host_serial(*parse_bridge_arg(kind, spec))
        for spec in g("usb_host", None) or []:                 # chipem/usbbus.py: plug a USB port into a host
            s.attach_usb_host(spec)
        for spec in g("usb_cdc", None) or []:                  # the CDC-ACM port to a PC endpoint (PTY/TCP/serial)
            s.attach_usb_cdc(spec)
        for link in g("usb_cdc_pty", None) or []:
            s.attach_usb_cdc(f"pty:{link}" if link else "pty")
        rt = g("realtime", None)
        speed = g("speed", None)
        if isinstance(speed, str):
            speed = parse_speed(speed)
            if speed is None:                      # --speed max: as fast as possible (no pacing)
                rt = False if rt is None else rt
        if rt is not None or speed is not None:
            s.set_realtime(rt if rt is not None else True, speed)
        elif s.pacer is None and s.host_endpoints(transport_only=True):
            s.set_realtime(True)                   # interactive host bridge: lock virtual time to the wall clock
        perf = {k: v for k, v in (("preset", g("preset", None)), ("max_slice", g("max_slice", None)),
                                  ("auto_quantum", g("auto_quantum", None)), ("pin_log", g("pin_log", None)),
                                  ("drop_kinds", g("drop_events", None))) if v is not None}
        if g("ui_interval", None) is not None:
            perf["ui_interval_s"] = parse_time(g("ui_interval")) / 1e9
        if g("poll_skip", None) is not None:
            ps = str(g("poll_skip")).strip().lower()
            perf["poll_skip_ns"] = 0.0 if ps in ("0", "off", "") else parse_time(ps)
        if perf:
            s.set_perf(**perf)                     # speed / fidelity settings (chipem/perf.py)
        s._vcd_parts()
        s.source_dirs = list(g("source_dir", None) or []) if "g" in locals() else []
        return s

    @classmethod
    def from_board(cls, board, *, precise: bool = False, log_mmio: bool = False,
                   vcd_path: str | Path | None = None, quantum_ns: float | None = None,
                   clock_hz: float | None = None, quiet: bool = False) -> "Session":
        """Build a multi-chip session from a board YAML path or a chipem.board.BoardDesc."""
        from .board import Board, BoardDesc, device_spec, load_board, spi_flash_spec
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
            brd.add_net(n.name, n.pins, pull=n.pull, drive=n.drive, open_drain=getattr(n, "open_drain", False))
        for ln in bd.links:
            if ln.type == "can":
                brd.add_can_link(ln.nodes, name=ln.name, host=ln.host)
            elif ln.type == "eth":
                o = ln.options
                brd.add_eth_link([(c, p, ph) for (c, p), ph in zip(ln.nodes, o.get("phy") or [{}] * len(ln.nodes))],
                                 name=ln.name, host=o.get("host"), pcap=o.get("pcap"), caps=o.get("caps"),
                                 inject=o.get("inject"))
            elif ln.type == "usb":
                ul = brd.add_usb_link(ln.a, name=ln.name, options=ln.options)
                for key, kind in (("cdc_pty", "pty"), ("cdc_tcp", "tcp"), ("cdc_serial", "serial")):
                    v = ln.options.get(key)
                    if v not in (None, False, "", "false", "no", "off"):
                        s.attach_usb_cdc(kind if v is True or v in ("true", "yes", "on") else f"{kind}:{v}",
                                         port=ul.name)
            else:
                brd.add_uart_link(ln.a, ln.b, bidir=ln.bidir)
        for ps in bd.parts:
            brd.add_part(ps.id, ps.type, ps.params, ps.ports)
        for d in bd.devices:
            if d.type == "sram":
                s.attach_sram(device_spec(d, "bus", "XINTF6"), chip=d.chip)
            elif d.type == "sd_card":
                s.attach_sd_card(device_spec(d, "host", ""), chip=d.chip)
            elif d.type == "sdram":
                s.attach_sdram(device_spec(d, "bus", "FMC"), chip=d.chip)
            else:
                s.attach_spi_flash(spi_flash_spec(d), chip=d.chip)
        for st in bd.stimulus:
            s.apply_stimulus(st)
        for h in getattr(bd, "host_serial", None) or []:
            s.attach_host_serial(*_host_serial_entry(h, bd.path))
        if getattr(bd, "perf", None):
            s.set_perf(**bd.perf)                  # board YAML `performance:` / `auto_quantum:` / `max_slice:`
        rt = getattr(bd, "realtime", None)
        if rt is not None and rt is not False:
            s.set_realtime(True, None if rt is True else float(rt))
        elif rt is None and s.host_endpoints(transport_only=True):
            s.set_realtime(True)                   # PC software on a host port: lock to the wall clock
        s._vcd_parts()
        s.source_dirs = list(g("source_dir", None) or []) if "g" in locals() else []
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
        self.base_quantum_ns = brd.quantum_ns
        from .perf import apply
        apply(self.perf, self)
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

    def attach_sd_card(self, spec: str, chip: str | None = None):
        """Insert an SD card into an SD host controller: "[CHIP.]SDIO:size=64M,kind=sdhc,image=card.img,save=out.img"
        (chipem.devices.sdcard; the host is any peripheral with `attach_card`, the first one when the name is empty).
        The card is written back to `save` (or to `image` with writeback=1) when the session closes."""
        from .devices.sdcard import attach_from_spec
        if chip is not None:
            ctx = self._ctx(chip)
        else:
            ctx, spec = self.resolve(spec)
        dev = attach_from_spec(ctx.chip, spec)
        ctx.devices.append(dev)
        return dev

    def eth_link(self, *, pcap: str | None = None, host: str | dict | None = None):
        """Ethernet segment for --eth-pcap / --eth-host (chipem.ethernet): the board's first `eth` link, or a new
        segment joining every Ethernet MAC of the session's chips (a single chip gets a one-chip board).  `pcap` starts
        a capture file; `host` ("ip=..,ping=..,count=..,interval=..,start=..,mac=..") adds a host node."""
        from .ethernet import eth_blocks
        if isinstance(host, str):
            host = dict(kv.split("=", 1) for kv in host.split(",") if kv.strip())
            for k in ("count", "size"):
                if k in host:
                    host[k] = int(host[k], 0)
        phy = {}
        for k, k2 in (("phy", "model"), ("phy_addr", "addr"), ("aneg", "aneg")):
            if host and k in host:
                phy[k2] = host.pop(k)
        brd = self.ensure_board()
        for ctx in self.ctxs.values():
            for m in (eth_blocks(ctx.chip).values() if phy else ()):
                m.phy.configure(**phy)
        if brd.eth_links:
            link = brd.eth_links[0]
            if pcap:
                link.segment.set_pcap(pcap)
                link.pcap_path = pcap
            if host:
                h = dict(host)
                link.host = link.segment.add_host(str(h.pop("name", "HOST")), **h)
                link.host.start_pending()
                link.host_opts = dict(host)
            return link
        nodes = [(cid, n, {}) for cid, ctx in self.ctxs.items() for n in eth_blocks(ctx.chip)]
        if not nodes:
            raise ValueError("no Ethernet MAC on this chip (STM32F407/F417 have one)")
        return brd.add_eth_link(nodes, pcap=pcap, host=host)

    def attach_sdram(self, spec: str, chip: str | None = None):
        """Attach an SDR SDRAM to an SDRAM controller: "[CHIP.]FMC:bank=2,size=8M,width=16" (chipem.devices.sdram;
        the controller is any peripheral with `attach_sdram`, e.g. stm32.fmc)."""
        from .devices.sdram import attach_from_spec
        if chip is not None:
            ctx = self._ctx(chip)
        else:
            ctx, spec = self.resolve(spec)
        dev = attach_from_spec(ctx.chip, spec)
        ctx.devices.append(dev)
        return dev

    def attach_sram(self, spec: str, chip: str | None = None):
        """Attach a parallel SRAM to an external bus: "[CHIP.]XINTF6:size=256K,width=16,file=img.bin"
        (chipem.devices.sram; the bus is any peripheral with `attach_memory`)."""
        from .devices.sram import attach_from_spec
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

    def _run(self, fn, *a, step_chip: str | None = None) -> StopReason:
        with self._run_lock:
            self.running = True
            if step_chip is None:
                self.speed.begin(self)
            w0 = time.perf_counter()
            try:
                for ctx in self.ctxs.values():
                    ctx.deliver_pending_uart()
                r = fn(*a)
            finally:
                self.running = False
                if step_chip is None:
                    self.speed.sample(self, time.perf_counter() - w0)
            for ctx in self.ctxs.values():
                if ctx.clock_override and ctx.chip.clock.hz != ctx.clock_override:
                    ctx.apply_clock_override()
            self._flush_waves(stopped_only=True)
            dbg = self.__dict__.get("_debug")
            if dbg is not None and (r is not StopReason.COUNT or step_chip is not None):
                dbg.after_run(r, step=step_chip is not None, chip=step_chip)   # chipem/debug.py: stop bookkeeping
            return r

    @property
    def debug(self):
        """The debugger of this session (chipem.debug.SessionDebug: breakpoints, watchpoints, stepping, last stop),
        created on first use."""
        d = self.__dict__.get("_debug")
        if d is None:
            from .debug import SessionDebug
            d = self._debug = SessionDebug(self, getattr(self, "source_dirs", None))
        return d

    def _flush_waves(self, stopped_only: bool) -> None:
        """Store the open `wave` summaries of decimated pins (log.EventLog): after a run only those of pins that
        stopped toggling, on close all of them."""
        logs = [ctx.chip.log for ctx in self.ctxs.values()]
        if self.board is not None:
            logs.append(self.board.log)
        for log in logs:
            fw = getattr(log, "flush_waves", None)
            if fw is None:
                continue
            if stopped_only:
                now = self.board.now_ns() if self.board is not None else self.chip.now_ns()
                fw(now)
            else:
                fw()

    def run_for(self, ns: float) -> StopReason:
        fn = self.board.run_for if self.board is not None else self.chip.run_for
        if self.pacer is not None or self.host_endpoints():
            return self._run(self._run_paced, fn, float(ns))
        return self._run(fn, float(ns))

    def _run_paced(self, fn, ns: float) -> StopReason:
        """Run in slices, pumping host serial endpoints between them; with real-time pacing on, lock virtual time
        to the wall clock (chipem/realtime.py)."""
        from .realtime import run_paced
        self._pace_stop = False
        rt = self.pacer
        if rt is not None:
            rt.wake = self._wake

        def stopped() -> bool:
            if self._pace_stop:
                self._pace_stop = False
                return True
            return False
        return run_paced(self, ns, rt, fn, self._pump_hosts, stopped)

    # ------------------------------------------------------------------ host serial bridges / real time
    def host_endpoints(self, transport_only: bool = False) -> list:
        """Host serial endpoints: direct UART bridges and board parts bound to a host port (usb_serial_adapter).
        `transport_only`: only those with an OS transport (PTY, serial, TCP), not in-memory ones."""
        eps = list(self.host_bridges)
        if self.board is not None:
            eps += [p for p in self.board.parts.values() if hasattr(p, "pump") and hasattr(p, "host_port")]
        if transport_only:
            eps = [e for e in eps if getattr(_ep_port(e), "kind", "memory") != "memory"]
        return eps

    def _pump_hosts(self) -> None:
        for ep in self.host_endpoints():
            port = _ep_port(ep)
            if port is not None and self._wake.set not in port.listeners:
                port.listeners.append(self._wake.set)
            ep.pump()

    def attach_host_serial(self, uart: str, transport: str):
        """Wire a host port straight to a UART (TTL level): `transport` "pty[:LINK]", "tcp:[HOST:]PORT",
        "serial:DEVICE[:BAUD[:8N1]]", "memory". Returns the UartBridge (its `port` is the HostPort)."""
        from .hostserial import UartBridge, open_port
        ctx, name, blk = self._uart_block(uart)
        if not hasattr(blk, "tx_listeners"):
            raise ValueError(f"{name} is not a UART model with link hooks")
        for b in self.host_bridges:
            if b.blk is blk:
                raise ValueError(f"{ctx.id}.{name} already has a host serial bridge ({b.port.describe()})")
        port = open_port(transport)
        if port is None:
            raise ValueError(f"host serial {uart}: no transport given")
        br = UartBridge(ctx.chip, blk, port, label=f"{ctx.id}.{name}" if self.board is not None else name)
        port.listeners.append(self._wake.set)
        self.host_bridges.append(br)
        ctx.chip.log.event("info", ctx.chip.clock.now_ns(), block=name,
                           msg=f"host serial: {br.label} <-> {port.kind} {port.describe()}")
        return br

    # ------------------------------------------------------------------ USB host (chipem/usbbus.py)
    def usb_links(self) -> list:
        return list(getattr(self.board, "usb_links", []) if self.board is not None else [])

    def attach_usb_host(self, spec: str | None = None, chip: str | None = None):
        """Plug a USB device port into an emulated PC host: "[CHIP.]PORT[:key=value,...]" (PORT e.g. OTG_FS, USB0,
        empty = the chip's first port; keys as the board `usb` link `host:` options, e.g. debounce=10ms,
        cdc_send=hello\\r\\n, cdc_send_at=200ms).  Returns the UsbLink (`.host` is the UsbHost)."""
        from .usbbus import host_options
        spec = str(spec or "")
        port, _, opts = spec.partition(":")
        if chip is not None:
            ctx = self._ctx(chip)
        elif port:
            ctx, port = self.resolve(port)
        else:
            ctx = self.ctxs[self.view]
        o: dict[str, str] = {}
        for kv in filter(None, opts.split(",")):
            k, eq, v = kv.partition("=")
            if not eq:
                raise ValueError(f"bad --usb-host option {kv!r} (expected KEY=VALUE)")
            o[k.strip()] = v.strip()
        name = ctx.chip.find_usb(port or None) if hasattr(ctx.chip, "find_usb") else None
        if name is None:
            raise ValueError(f"no USB device port {port or ''!r} on {ctx.id} "
                             f"(ports: {', '.join(ctx.chip.usb_blocks()) or 'none'})")
        brd = self.ensure_board()
        return brd.add_usb_link((ctx.id, name), options=host_options(o, "--usb-host"))

    def attach_usb_cdc(self, spec: str, port: str | None = None):
        """Bind the CDC-ACM function of a USB link to a host PC endpoint: "[PORT=]TRANSPORT" with TRANSPORT "pty",
        "pty:/tmp/link", "tcp:5555", "serial:/dev/ttyUSB1" or "memory" (chipem.hostserial).  The link is created with
        default host options when the port has none yet.  Returns the UsbCdcBridge (its `port` is the HostPort)."""
        from .hostserial import open_port
        from .usbbus import UsbCdcBridge
        spec = str(spec).strip()
        if "=" in spec and not spec.split("=", 1)[0].lower().startswith(("pty", "tcp", "serial", "memory")):
            port, spec = spec.split("=", 1)
        if spec.isdigit():
            spec = f"tcp:{spec}"
        link = None
        for ln in self.usb_links():
            if port is None or port.upper() in (ln.port.upper(), f"{ln.chip_id}.{ln.port}".upper(), ln.name.upper()):
                link = ln
                break
        if link is None:
            link = self.attach_usb_host(port)
        hp = open_port(spec or "pty")
        if hp is None:
            raise ValueError("usb cdc: no transport given")
        label = f"{link.chip_id}.{link.port} CDC" if len(self.ctxs) > 1 else f"{link.port} CDC"
        br = UsbCdcBridge(link.host, hp, label=label, blk=link.block)
        link.endpoint = br
        hp.listeners.append(self._wake.set)
        self.host_bridges.append(br)
        chip = link.block.chip
        chip.log.event("info", chip.clock.now_ns(), block=link.port,
                       msg=f"usb cdc: {br.label} <-> {hp.kind} {hp.describe()}")
        return br

    def set_realtime(self, on: bool = True, speed: float | None = None) -> None:
        """Real-time pacing on/off; `speed` = virtual seconds per wall second (1.0 = real time)."""
        if not on:
            self.pacer = None
            self._wake.set()
            return
        from .realtime import RealTime
        if self.pacer is None:
            self.pacer = RealTime(speed if speed is not None else 1.0)
        elif speed is not None:
            self.pacer.set_speed(speed)
        self._wake.set()

    # ------------------------------------------------------------------ speed / fidelity (chipem/perf.py)
    def set_perf(self, **kw) -> Any:
        """Change speed/fidelity settings (PerfSettings fields, or `preset=` accurate|default|fast first) and apply
        them to the chips, board and logs. Emulation thread (or while not running). Returns the new settings."""
        from .perf import apply
        new = self.perf.updated(**kw)
        self.perf = new
        apply(new, self)
        return new

    def speed_info(self) -> dict:
        """Achieved speed (SpeedMeter.info: ratio, MIPS and wall-time share per chip, bottleneck), the requested rate
        (real-time pacing speed, None = as fast as possible), whether pacing falls behind, and the settings."""
        d = self.speed.info()
        rt = self.pacer
        d["requested"] = rt.speed if rt is not None else None
        req, got = d["requested"], d["ratio"]
        d["behind"] = bool(rt is not None and (rt.lag_ns > rt.max_lag_ns / 2 or
                                               (got is not None and got < 0.9 * req)))
        d["settings"] = self.perf.to_dict()
        b = self.board
        d["quantum_ns"] = b.quantum_ns if b is not None else None
        d["base_quantum_ns"] = self.base_quantum_ns
        d["auto_quantum"] = bool(getattr(b, "auto_quantum", False)) if b is not None else False
        return d

    def host_status(self) -> dict:
        """{realtime: {...} | {enabled: False}, endpoints: [info, ...]} for UIs (emulation thread)."""
        rt = self.pacer
        return {"realtime": rt.info() if rt is not None else {"enabled": False},
                "endpoints": [ep.info() for ep in self.host_endpoints()]}

    def close_hosts(self) -> None:
        for ep in self.host_endpoints():
            try:
                (ep.close_host if hasattr(ep, "close_host") else ep.close)()
            except Exception:
                pass

    def run_cycles(self, n: int) -> StopReason:
        if self.board is not None:          # cycles of the view chip, converted to board time
            return self._run(self.board.run_for, int(n) * 1e9 / self.chip.clock.hz)
        return self._run(self.chip.run_cycles, int(n))

    def step(self, chip: str | None = None) -> StopReason:
        """One instruction on chip `chip` (default: the view chip); other board chips do not move."""
        ctx = self._ctx(chip)
        return self._run(ctx.chip.step, step_chip=ctx.id)

    def stop(self) -> None:
        """Thread-safe: ask a running run_for/run_cycles to return early (StopReason.USER)."""
        if self.running:
            self._pace_stop = True                 # a paced run waiting for the wall clock
            self._wake.set()
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
            # `<break>` / `<break:N>` in the text: the host sends a break (N low bit times; default two character
            # times) at that point (chipem/periph/uartwire.py feed_break)
            parts = _BREAK_TOKEN.split(bytes(data)) if hasattr(blk, "feed_break") else [bytes(data)]
            for i, part in enumerate(parts):
                if i % 2:
                    blk.feed_break(int(part) if part else None)
                elif part:
                    blk.feed(part)
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

    def set_adc(self, channel: str, value: int | float) -> None:
        """channel e.g. 'ADC0_SE14' or 'B.ADC0_SE14' (block name prefix; bare 'SE14' means ADC0).  A name that is not
        "<ADC block>_<channel>" (a pin "PA0", a shared pin-table name "ADC123_IN0") goes to the first ADC model with
        the `set_input(name, value)` capability that accepts it.  `value`: int = raw code; float = volts, only for
        models that declare `adc_volts` (Chip.adc_blocks()); a model with `adc_volts_only` (no raw codes: Kinetis CMP inputs)
        rejects an int with a ValueError that suggests the V / mV form."""
        ctx, ch = self.resolve(channel)
        ch = ch.strip().upper()
        head = ch.split("_", 1)[0]
        if not hasattr(ctx.chip.peripherals.get(head), "set_channel"):
            for bname, b in ctx.chip.adc_blocks().items():
                fn = getattr(b, "set_input", None)
                if fn is None or (isinstance(value, float) and not getattr(b, "adc_volts", False)) \
                        or (not isinstance(value, float) and getattr(b, "adc_volts_only", False)):
                    continue
                if fn(ch, value):
                    ctx.adc_values[ch] = value
                    return
        if "_" in ch:
            blk_name, sub = ch.split("_", 1)
        elif "ADC0" not in ctx.chip.peripherals and "ADC" in ctx.chip.peripherals:
            blk_name, sub = "ADC", ch                       # C2000: "ADCINA3" / "A3" -> block ADC
            ch = f"ADC_{ch}"
        else:
            blk_name, sub = "ADC0", ch
            ch = f"ADC0_{ch}"
        blk = ctx.chip.peripherals.get(blk_name)
        if isinstance(value, float) and not getattr(blk, "adc_volts", False):
            raise ValueError(f"{blk_name} takes raw ADC codes, not volts ({ch}={value})")
        if not isinstance(value, float) and getattr(blk, "adc_volts_only", False):
            raise ValueError(f"{blk_name} takes volts, not raw codes: write {ch}={value}mV or e.g. {ch}=1.2V "
                             f"(a bare integer is a raw ADC code)")
        ctx.adc_values[ch] = value
        fn = getattr(blk, "set_channel", None)
        if fn is None:
            ctx.warn(f"{blk_name} model has no set_channel(); ADC value {ch}={value} stored only")
            return
        try:
            ok = fn(ch, value)
        except (KeyError, ValueError, TypeError):
            ok = fn(sub, value)
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
        try:
            self._flush_waves(stopped_only=False)
        except Exception:
            pass
        self.close_hosts()                          # host serial ports (PTY, serial, TCP)
        for ctx in self.ctxs.values():
            for dev in ctx.devices:
                save = getattr(dev, "save_image", None)      # SD card write-back (chipem.devices.sdcard)
                if save is not None:
                    save()
        for ctx in self.ctxs.values():
            ctx.close()
        if self.board is not None:
            self.board.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
