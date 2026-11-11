"""Scripted board parts: a custom component written in Python (board part type `script`).

Board YAML::

    parts:
      - {id: U5, type: script, file: parts/max31855.py, class: Max31855, params: {temperature: 25.0},
         ports: {CS: A.PTC4, SCK: A.PTC5, SO: A.PTC7}}
      - id: U6
        type: script
        code: |
          from chipem.parts.script import ScriptPart, on_rising
          class Inverter(ScriptPart):
              PORTS = {"A": "in", "Y": "out"}
              def on_change(self, port, level, t_ns):
                  self.drive("Y", None if level is None else 1 - level)
        ports: {A: BTN, Y: A.PTC5}

`file` is resolved relative to the board file (`code:` holds the source inline). The script defines one class derived
from `ScriptPart` (`class:` picks one when the file defines several). Every other key (or the `params:` mapping) sets
the script's PARAMS. See docs/USER_GUIDE.md "Writing your own part in Python" for the user-facing API.

How it fits the board (chipem/board.py): the registered part type is `ScriptHost`, an ordinary `Part`; the user's
object is a separate `ScriptPart` instance that talks to the host only through the small API below, so Part internals
cannot clash with user attribute names. Timing is the board's: input callbacks run inside the board's zero-delay
propagation at the event's virtual time; timers are board-clock events (`Part._at`), cancelled on reset and restarted
by `on_reset`; outputs go through `Part._drive` (propagation, loop detection, `part` events, VCD listeners).

Robustness: every call into user code goes through `ScriptHost._call`. An exception marks the part faulted (one `warn`
event with the script line and the last frames, `state()["fault"]`), cancels its timers and stops calling it until
reset or reload; a callback that takes longer than `slow_ms` of wall time logs one `warn` per callback. A syntax or
load error raises ValueError naming the file and line (board load fails; a failed reload keeps the old code).

Hot reload (`reload()`): the file (or new inline code) is compiled and its class replaces the old one; the part is
reset (fresh object, `on_reset`) and its nets re-resolved. Ports must stay the same (wiring is board structure).

Security: a script is arbitrary Python running with the user's privileges. Loading a board with a script part is the
same as running a program from wherever the board came from. There is no sandbox.

Generic glue: no vendor knowledge (ADC inputs are reached through the `adc_blocks()` capabilities of chip models).
"""
from __future__ import annotations

import linecache
import re
import sys
import time
import traceback
import types
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .base import Part, PortDesc
from .registry import part

__all__ = ["ScriptPart", "ScriptHost", "Param", "Timer", "on_rising", "on_falling", "on_edge", "every", "action",
           "PORT_KINDS", "load_source"]

# port kind -> Part port direction
PORT_KINDS = {"in": "in", "out": "out", "inout": "inout", "od": "inout", "analog_in": "in", "analog_out": "out"}
_KIND_ALIAS = {"input": "in", "output": "out", "io": "inout", "bidir": "inout", "bidirectional": "inout",
               "open_drain": "od", "opendrain": "od", "ain": "analog_in", "aout": "analog_out",
               "analog": "analog_in", "adc": "analog_in", "dac": "analog_out"}
DEFAULT_SLOW_MS = 50.0


# ================================================================================================ user-facing helpers
class Param:
    """A PARAMS entry with UI hints: `PARAMS = {"temperature": Param(25.0, min=-50, max=150, unit="°C")}`."""

    def __init__(self, default: Any, *, min: float | None = None, max: float | None = None,
                 step: float | None = None, unit: str = "", doc: str = "", choices: list | None = None):
        self.default, self.min, self.max, self.step = default, min, max, step
        self.unit, self.doc, self.choices = unit, doc, list(choices) if choices else None

    def meta(self) -> dict:
        d: dict[str, Any] = {}
        for k in ("min", "max", "step", "unit", "doc", "choices"):
            v = getattr(self, k)
            if v not in (None, "", []):
                d[k] = v
        return d


def _mark(fn: Callable, key: str, value: Any) -> Callable:
    lst = fn.__dict__.setdefault(key, [])
    lst.append(value)
    return fn


def on_rising(*ports: str) -> Callable:
    """Decorator: call the method `fn(self)` when an input port goes L -> H."""
    return lambda fn: [_mark(fn, "_chipem_edges", ("rise", p)) for p in ports][-1] if ports else fn


def on_falling(*ports: str) -> Callable:
    """Decorator: call the method `fn(self)` when an input port goes H -> L."""
    return lambda fn: [_mark(fn, "_chipem_edges", ("fall", p)) for p in ports][-1] if ports else fn


def on_edge(*ports: str) -> Callable:
    """Decorator: call the method `fn(self, level)` on every level change of an input port (level 1, 0 or None)."""
    return lambda fn: [_mark(fn, "_chipem_edges", ("edge", p)) for p in ports][-1] if ports else fn


def every(period: Any) -> Callable:
    """Decorator: call the method `fn(self)` every `period` ("1ms", "250us"; a bare number is milliseconds),
    starting one period after reset."""
    return lambda fn: _mark(fn, "_chipem_every", period)


def action(fn: Callable | None = None, *, label: str | None = None) -> Callable:
    """Decorator: the method `fn(self)` becomes a button in the web UI and `part ID action=NAME` in the shell."""
    def deco(f: Callable) -> Callable:
        f._chipem_action = label or f.__name__
        return f
    return deco(fn) if fn is not None else deco


class Timer:
    """Handle of `after()` / `repeat()`: `cancel()`, `active`."""

    def __init__(self, host: "ScriptHost", fn: Callable, args: tuple, period_ns: float | None):
        self._host, self._fn, self._args, self.period_ns = host, fn, args, period_ns
        self._ev = None
        self.t_ns: float | None = None
        self.active = False

    def cancel(self) -> None:
        if self._ev is not None and self._host.board is not None:
            self._host.board.sched_cancel(self._ev)
        self._ev = None
        self.active = False

    def __repr__(self) -> str:
        return f"<Timer {getattr(self._fn, '__name__', '?')} at {self.t_ns} ns{' active' if self.active else ''}>"


def _parse_time(v: Any, what: str) -> float:
    from .std import _time
    t = _time(v, what)
    if t < 0:
        raise ValueError(f"{what}: negative time {v!r}")
    return t


def _level_in(v: Any, what: str) -> str | None:
    """User value -> 'H' | 'L' | 'X' | None (release)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "H" if v else "L"
    if isinstance(v, (int, float)):
        if v in (0, 1):
            return "H" if v else "L"
        raise ValueError(f"{what}: level must be 0, 1, True, False, 'H', 'L', 'X' or None (got {v!r})")
    t = str(v).strip().upper()
    if t in ("H", "HIGH", "1", "TRUE", "ON"):
        return "H"
    if t in ("L", "LOW", "0", "FALSE", "OFF"):
        return "L"
    if t == "X":
        return "X"
    if t in ("Z", "NONE", "OPEN", "RELEASE", ""):
        return None
    raise ValueError(f"{what}: level must be 0, 1, True, False, 'H', 'L', 'X' or None (got {v!r})")


def _level_out(lvl: str | None) -> int | None:
    return 1 if lvl == "H" else 0 if lvl == "L" else None


def _bytes(data: Any, what: str) -> bytes:
    if data is None:
        return b""
    if isinstance(data, int) and not isinstance(data, bool):
        return bytes([data & 0xFF])
    if isinstance(data, str):
        return data.encode("utf-8")
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    try:
        return bytes(int(x) & 0xFF for x in data)
    except (TypeError, ValueError):
        raise ValueError(f"{what}: expected bytes, str, an int or a list of ints (got {type(data).__name__})") \
            from None


# ================================================================================================ the user base class
class ScriptPart:
    """Base class of a scripted part. Subclass it in your script; see docs/USER_GUIDE.md.

    Class attributes: PORTS {name: kind} (kind "in", "out", "inout", "od", "analog_in", "analog_out", optionally with
    " pullup" / " pulldown"), PARAMS {name: default | Param(...)} (user settings, editable at run time).
    Callbacks (all optional): on_reset(), on_change(port, level, t_ns), on_param(name, value), on_analog(port, volts),
    on_spi_start(), on_spi_byte(value), on_spi_end(), on_i2c_write(data), on_i2c_read(n), on_uart_byte(b),
    on_uart_line(text), on_host_data(data)."""

    PORTS: dict[str, Any] = {}
    PARAMS: dict[str, Any] = {}
    ADC_FULL_SCALE = 3.3          # volts -> raw code for ADC models that take raw codes (TI C2000: set 3.0)

    _host: "ScriptHost"
    id: str
    params: dict[str, Any]
    state: dict[str, Any]

    # ---------------------------------------------------------------- time
    @property
    def now_ns(self) -> float:
        """Current virtual time in ns (the time of the event being handled)."""
        return self._host.now()

    @property
    def now(self) -> float:
        """Current virtual time in seconds."""
        return self._host.now() * 1e-9

    @staticmethod
    def ns(t: Any) -> float:
        """A time value ("250us", "1.5ms", "2s"; a bare number is milliseconds) in nanoseconds."""
        return _parse_time(t, "time")

    def after(self, delay: Any, fn: Callable, *args: Any) -> Timer:
        """Call `fn(*args)` once, `delay` from now ("250us", "1ms"; a bare number is milliseconds)."""
        return self._host._timer(_parse_time(delay, "after"), fn, args, None)

    def repeat(self, period: Any, fn: Callable, *args: Any) -> Timer:
        """Call `fn(*args)` every `period`, first one period from now. Returns a Timer (cancel() stops it)."""
        p = _parse_time(period, "repeat")
        if p <= 0:
            raise ValueError("repeat: period must be > 0")
        return self._host._timer(p, fn, args, p)

    # ---------------------------------------------------------------- pins
    def read(self, port: str) -> int | None:
        """Level of the net on `port`: 1, 0, or None (undriven / unknown / not connected)."""
        return _level_out(self._host._level(port))

    def level(self, port: str) -> str:
        """Level of the net on `port` as "H", "L" or "X"."""
        return self._host._level(port) or "X"

    def drive(self, port: str, value: Any) -> None:
        """Drive an output: 1/True/"H", 0/False/"L", "X", or None/"Z" (release). On an "od" port 1 releases."""
        self._host._user_drive(port, value)

    def release(self, port: str) -> None:
        """Stop driving `port` (high impedance; an "od" port is released)."""
        self._host._user_drive(port, None)

    def driving(self, port: str) -> int | None:
        """What this part drives on `port` now (1, 0, None = released)."""
        return _level_out(self._host._out.get(self._host._port(port, out=True)))

    def analog(self, port: str, volts: float | None) -> None:
        """Set the voltage of an "analog_out" port (None = disconnected). Chip ADC inputs on its net see it."""
        self._host._set_analog(port, volts)

    def read_analog(self, port: str) -> float | None:
        """Voltage on the net of `port` from an analog output of a scripted part, or None."""
        return self._host._read_analog(port)

    # ---------------------------------------------------------------- protocol helpers
    def spi_slave(self, *, cs: str = "CS", sck: str = "SCK", mosi: str | None = "MOSI", miso: str | None = "MISO",
                  mode: int = 0, bits: int = 8, lsb_first: bool = False, cs_active: int = 0, fill: int = 0xFF) -> None:
        """Act as an SPI slave on these ports (call it in on_reset). Callbacks: on_spi_start() (CS asserted; may
        return the first byte(s) to send), on_spi_byte(value) (a word received; return the byte(s) to send next),
        on_spi_end() (CS released). `fill` is sent when nothing is queued."""
        self._host._add_engine(_SpiSlave(self._host, cs, sck, mosi, miso, mode, bits, lsb_first, cs_active, fill))

    def spi_send(self, data: Any) -> None:
        """Queue byte(s) for MISO (sent in the following words of this or the next transaction)."""
        eng = self._host._engine(_SpiSlave)
        eng.queue.extend(_bytes(data, "spi_send") if eng.bits <= 8 else ([data] if isinstance(data, int) else data))

    def i2c_slave(self, address: int, *, sda: str = "SDA", scl: str = "SCL") -> None:
        """Act as an I2C target at 7-bit `address` on open-drain ports (call it in on_reset). Callbacks:
        on_i2c_write(data) (the bytes of a write transfer, at its STOP or repeated START), on_i2c_read(n) (the
        master reads; n = bytes already read in this transfer; return the byte(s) to send)."""
        self._host._add_engine(_I2cSlave(self._host, int(address), sda, scl))

    def uart(self, *, tx: str | None = "TX", rx: str | None = "RX", baud: float = 115200, format: str = "8N1",
             host: str | None = None) -> None:
        """A UART on these ports (call it in on_reset). Received bytes call on_uart_byte(b) and complete lines
        on_uart_line(text). `host` ("pty", "pty:/tmp/dev", "tcp:5555", "serial:/dev/ttyUSB0", "memory") talks to
        PC software instead of the board wires: then the PC is the peer."""
        self._host._add_engine(_Uart(self._host, tx, rx, float(baud), str(format), host))

    def uart_send(self, data: Any) -> None:
        """Transmit bytes (str is UTF-8) on the UART, one per character time."""
        self._host._engine(_Uart).send(_bytes(data, "uart_send"))

    # ---------------------------------------------------------------- host side
    def path(self, name: str) -> Path:
        """A file path relative to the script's directory (inline code: the board's directory)."""
        return self._host._data_path(name)

    def open(self, name: str, mode: str = "r", **kw: Any):
        """open() a file relative to the script's directory (inline code: the board's directory)."""
        return open(self._host._data_path(name), mode, **kw)

    def host_open(self, spec: str):
        """Open a host serial endpoint once ("pty", "pty:/tmp/link", "tcp:5555", "serial:/dev/ttyUSB0",
        "memory"); data from the PC calls on_host_data(data). Kept across resets and reloads."""
        return self._host._host_open(spec)

    def host_write(self, data: Any) -> None:
        """Send bytes to the host endpoint opened with host_open (or uart(host=...))."""
        self._host._host_write(_bytes(data, "host_write"))

    # ---------------------------------------------------------------- log / UI
    def log(self, msg: Any) -> None:
        """A `part` event in the event log."""
        self._host.emit(action="log", msg=str(msg))

    def warn(self, msg: Any) -> None:
        """A `warn` event in the event log."""
        self._host._warn(str(msg))

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {getattr(self, 'id', '?')}>"


# ================================================================================================ protocol engines
class _Engine:
    ports: tuple = ()

    def __init__(self, host: "ScriptHost"):
        self.host = host

    def start(self) -> None:
        """After on_reset (outputs may be driven normally)."""

    def on_input(self, port: str, level: str | None, prev: str | None) -> None:
        pass

    def user(self, name: str, *args: Any) -> Any:
        fn = getattr(self.host.obj, name, None)
        if fn is None:
            return None
        return self.host._call(fn, *args, what=name)


class _SpiSlave(_Engine):
    def __init__(self, host, cs, sck, mosi, miso, mode, bits, lsb_first, cs_active, fill):
        super().__init__(host)
        if int(mode) not in (0, 1, 2, 3):
            raise ValueError(f"spi_slave: mode must be 0..3 (got {mode!r})")
        if not 1 <= int(bits) <= 32:
            raise ValueError(f"spi_slave: bits must be 1..32 (got {bits!r})")
        self.cs = host._port(cs, inp=True)
        self.sck = host._port(sck, inp=True)
        self.mosi = host._port(mosi, inp=True) if mosi else None
        self.miso = host._port(miso, out=True) if miso else None
        self.cpol, self.cpha = (int(mode) >> 1) & 1, int(mode) & 1
        self.bits, self.lsb, self.fill = int(bits), bool(lsb_first), int(fill)
        self.cs_level = "H" if cs_active else "L"
        self.ports = (self.cs, self.sck)
        self.queue: deque[int] = deque()
        self.selected = False
        self.words = 0
        self._reset_xfer()

    def _reset_xfer(self) -> None:
        self.rx = 0
        self.nin = 0
        self.tx = self.fill
        self.nout = 0

    def start(self) -> None:
        if self.host.inputs.get(self.cs) == self.cs_level:
            self._select(True)

    def _queue(self, r: Any) -> None:
        if r is None:
            return
        if self.bits > 8 and isinstance(r, int):
            self.queue.append(r)
        elif self.bits > 8 and isinstance(r, (list, tuple)):
            self.queue.extend(int(x) for x in r)
        else:
            self.queue.extend(_bytes(r, "on_spi_byte return"))

    def _next(self) -> int:
        return (self.queue.popleft() if self.queue else self.fill) & ((1 << self.bits) - 1)

    def _bit(self, word: int, i: int) -> int:
        return (word >> (i if self.lsb else self.bits - 1 - i)) & 1

    def _out_bit(self) -> None:
        if self.miso is not None and self.nout < self.bits:
            self.host._eng_drive(self.miso, "H" if self._bit(self.tx, self.nout) else "L")
        self.nout += 1

    def _select(self, on: bool) -> None:
        if on == self.selected:
            return
        self.selected = on
        if on:
            self.queue.clear()
            self._queue(self.user("on_spi_start"))
            self._reset_xfer()
            self.tx = self._next()
            if self.cpha == 0:
                self._out_bit()
        else:
            if self.miso is not None:
                self.host._eng_drive(self.miso, None)
            self.user("on_spi_end")

    def _sample(self) -> None:
        b = 1 if self.mosi is not None and self.host.inputs.get(self.mosi) == "H" else 0
        if self.lsb:
            self.rx |= b << self.nin
        else:
            self.rx = (self.rx << 1) | b
        self.nin += 1
        if self.nin >= self.bits:
            word = self.rx
            self.rx = self.nin = 0
            self.words += 1
            self._queue(self.user("on_spi_byte", word))
            self.tx = self._next()
            self.nout = 0

    def on_input(self, port: str, level: str | None, prev: str | None) -> None:
        if port == self.cs:
            self._select(level == self.cs_level)
            return
        if not self.selected or prev not in ("H", "L") or level not in ("H", "L") or prev == level:
            return
        idle = "H" if self.cpol else "L"
        leading = prev == idle
        if self.cpha == 0:
            if leading:
                self._sample()
            else:
                self._out_bit()
        else:
            if leading:
                self._out_bit()
            else:
                self._sample()


class _I2cSlave(_Engine):
    def __init__(self, host, address, sda, scl):
        super().__init__(host)
        from ..periph.i2cwire import I2CTarget
        if not 0x08 <= address <= 0x77:
            raise ValueError(f"i2c_slave: address must be a 7-bit address 0x08..0x77 (got 0x{address:x})")
        self.address = address
        self.sda = host._port(sda, inp=True, out=True)
        self.scl = host._port(scl, inp=True, out=True)
        for p in (self.sda, self.scl):
            if host.kinds[p] not in ("od", "inout"):
                raise ValueError(f"i2c_slave: port {p} must be an open-drain port (\"od\" or \"od pullup\")")
        self.ports = (self.sda, self.scl)
        self.target = I2CTarget(self, self._sda_low, self._scl_low)
        self.wbuf: bytearray | None = None
        self.rq: deque[int] = deque()
        self.nread = 0
        self.transfers = 0

    def _sda_low(self, low: bool) -> None:
        self.host._eng_drive(self.sda, "L" if low else None)

    def _scl_low(self, low: bool) -> None:
        self.host._eng_drive(self.scl, "L" if low else None)

    def on_input(self, port, level, prev) -> None:
        from ..periph.i2cwire import bit_of
        self.target.bus(bit_of(self.host.inputs.get(self.scl)), bit_of(self.host.inputs.get(self.sda)))

    def _flush(self) -> None:
        if self.wbuf:
            data, self.wbuf = bytes(self.wbuf), None
            self.user("on_i2c_write", data)
        self.wbuf = None

    # I2CTarget handler
    def i2c_start(self, repeated: bool) -> None:
        self._flush()

    def i2c_stop(self) -> None:
        self._flush()

    def i2c_address(self, addr: int, read: bool) -> bool:
        if addr != self.address or self.host.fault is not None:
            return False
        self.transfers += 1
        self.rq.clear()
        self.nread = 0
        self.wbuf = None if read else bytearray()
        return True

    def i2c_rx_byte(self, byte: int) -> bool:
        if self.wbuf is not None:
            self.wbuf.append(byte & 0xFF)
        return self.host.fault is None

    def i2c_tx_byte(self) -> int:
        if not self.rq:
            r = self.user("on_i2c_read", self.nread)
            if r is not None:
                self.rq.extend(_bytes(r, "on_i2c_read return"))
        self.nread += 1
        return self.rq.popleft() if self.rq else 0xFF


class _Uart(_Engine):
    def __init__(self, host, tx, rx, baud, fmt, host_spec):
        super().__init__(host)
        from ..hostserial import LineSettings, parse_format
        bits, par, stop = parse_format(fmt)
        if baud <= 0:
            raise ValueError("uart: baud must be > 0")
        self.line = LineSettings(baud, bits, par, stop)
        self.tx = host._port(tx, out=True) if tx else None
        self.rx = host._port(rx, inp=True) if rx else None
        self.host_spec = host_spec
        self.ports = ()
        self.txq: deque[int] = deque()
        self.rxq: deque[int] = deque()          # bytes from a host endpoint, delivered at character pace
        self._ev = None
        self._rx_ev = None
        self.linebuf = bytearray()
        self.sent = 0
        self.received = 0
        self.dropped = 0
        if host_spec:
            host._host_open(host_spec)

    def start(self) -> None:
        if self.tx is not None and self.host_spec is None:
            self.host._eng_drive(self.tx, "H")                  # idle = mark (TTL high)
        if self.host_spec is None and self.host.board is not None:
            from .serial import serial_router
            serial_router(self.host.board).changed()        # our RX port is a receiver now (path cache)

    def char_ns(self) -> float:
        return self.line.char_ns()

    def send(self, data: bytes) -> None:
        self.txq.extend(data)
        if self._ev is None and self.host.board is not None:
            self._ev = self.host._at(self.host.now() + self.char_ns(), self._tick)

    def _tick(self) -> None:
        self._ev = None
        if not self.txq or self.host.fault is not None:
            return
        b = self.txq.popleft()
        self.sent += 1
        h, board = self.host, self.host.board
        if not h.quiet:
            board.log.event("uart", h.now(), chip="board", uart=f"{h.id}.{self.tx or 'TX'}", dir="tx", byte=b)
        if self.host_spec is not None:
            port = h._hostport
            if port is not None:
                port.write(bytes([b]))
        elif self.tx is not None:
            net = board._port_net.get((h.id, self.tx))
            if net is not None:
                from .serial import serial_router
                r = serial_router(board)
                sinks = r.sinks(("part", h.id, self.tx), net, 0, "ttl", h, (h, self.tx))
                r.send(f"{h.id}.{self.tx}", board.clock_chip, sinks, b, h.now(), src_line=self.line)
        if self.txq:
            self._ev = h._at(h.now() + self.char_ns(), self._tick)

    def receive(self, b: int) -> None:
        h = self.host
        if h.fault is not None:
            return
        self.received += 1
        if not h.quiet and h.board is not None:
            h.board.log.event("uart", h.now(), chip="board", uart=f"{h.id}.{self.rx or 'RX'}", dir="rx", byte=b)
        self.user("on_uart_byte", b)
        if h.fault is not None:
            return
        if b in (0x0A, 0x0D):
            if b == 0x0A or self.linebuf:
                text = bytes(self.linebuf).decode("utf-8", "replace")
                self.linebuf.clear()
                if b == 0x0A or text:
                    if hasattr(h.obj, "on_uart_line"):
                        self.user("on_uart_line", text)
            return
        self.linebuf.append(b)
        if len(self.linebuf) > 4096:
            del self.linebuf[:-4096]

    def host_data(self, data: bytes) -> None:
        self.rxq.extend(data)
        if self._rx_ev is None and self.host.board is not None:
            self._rx_ev = self.host._at(self.host.now() + self.char_ns(), self._rx_tick)

    def _rx_tick(self) -> None:
        self._rx_ev = None
        if not self.rxq:
            return
        self.receive(self.rxq.popleft())
        if self.rxq:
            self._rx_ev = self.host._at(self.host.now() + self.char_ns(), self._rx_tick)


# ================================================================================================ loading
_mod_counter = [0]


def load_source(source: str, label: str, part_id: str, cls_name: str | None) -> type:
    """Compile and run a script, return its ScriptPart subclass. ValueError with file and line on any error."""
    try:
        code = compile(source, label, "exec")
    except SyntaxError as e:
        where = f"{e.filename or label}, line {e.lineno}" + (f", column {e.offset}" if e.offset else "")
        txt = (e.text or "").rstrip()
        raise ValueError(f"part {part_id}: syntax error in {where}: {e.msg}" + (f"\n    {txt}" if txt else "")) \
            from None
    _mod_counter[0] += 1
    modname = f"chipem_script_{re.sub(r'[^A-Za-z0-9_]', '_', part_id)}_{_mod_counter[0]}"
    mod = types.ModuleType(modname)
    mod.__file__ = label
    sys.modules[modname] = mod
    try:
        exec(code, mod.__dict__)
    except Exception as e:
        sys.modules.pop(modname, None)
        raise ValueError(f"part {part_id}: error while loading {label}: {_where(e, label)}") from None
    found = [v for v in mod.__dict__.values()
             if isinstance(v, type) and issubclass(v, ScriptPart) and v is not ScriptPart and v.__module__ == modname]
    if cls_name:
        cls = mod.__dict__.get(cls_name)
        if not (isinstance(cls, type) and issubclass(cls, ScriptPart)):
            raise ValueError(f"part {part_id}: {label} has no ScriptPart class {cls_name!r} "
                             f"(found: {', '.join(c.__name__ for c in found) or 'none'})")
        return cls
    if not found:
        raise ValueError(f"part {part_id}: {label} defines no class derived from chipem.parts.script.ScriptPart")
    if len(found) > 1:
        raise ValueError(f"part {part_id}: {label} defines several parts ({', '.join(c.__name__ for c in found)}); "
                         f"pick one with `class:`")
    return found[0]


def _where(e: BaseException, label: str, frames: int = 0) -> str:
    """'NameError: x at file.py:12 in on_reset: `y = x`' (the innermost frame inside the script)."""
    tb = traceback.extract_tb(e.__traceback__)
    own = [f for f in tb if f.filename == label]
    msg = f"{type(e).__name__}: {e}"
    if own:
        f = own[-1]
        src = (f.line or linecache.getline(label, f.lineno) or "").strip()
        msg += f" at {Path(label).name if not label.startswith('<') else label}:{f.lineno} in {f.name}" + \
            (f": `{src}`" if src else "")
    return msg


def _parse_port(name: str, spec: Any) -> tuple[str, str | None]:
    """PORTS value -> (kind, pull)."""
    if isinstance(spec, dict):
        kind = str(spec.get("kind", spec.get("dir", "in"))).strip().lower()
        pull = spec.get("pull")
        pull = str(pull).strip().lower() if pull else None
    else:
        words = [w for w in re.split(r"[\s,+|]+", str(spec).strip().lower()) if w]
        if not words:
            raise ValueError(f"port {name}: empty kind")
        kind, pull = words[0], None
        for w in words[1:]:
            if w in ("pullup", "pull_up", "up"):
                pull = "up"
            elif w in ("pulldown", "pull_down", "down"):
                pull = "down"
            else:
                raise ValueError(f"port {name}: unknown option {w!r} (pullup | pulldown)")
    kind = _KIND_ALIAS.get(kind, kind)
    if kind not in PORT_KINDS:
        raise ValueError(f"port {name}: unknown kind {kind!r} (in, out, inout, od, analog_in, analog_out)")
    if pull not in (None, "up", "down"):
        raise ValueError(f"port {name}: pull must be up or down")
    return kind, pull


def _class_ports(cls: type, pid: str) -> dict[str, tuple[str, str | None]]:
    raw = getattr(cls, "PORTS", None) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"part {pid}: {cls.__name__}.PORTS must be a mapping name -> kind")
    out = {}
    for n, spec in raw.items():
        n = str(n)
        if not n or any(c in n for c in ".:=, "):
            raise ValueError(f"part {pid}: bad port name {n!r} (no dots, colons, commas, '=' or spaces)")
        try:
            out[n] = _parse_port(n, spec)
        except ValueError as e:
            raise ValueError(f"part {pid}: {e}") from None
    return out


def _class_params(cls: type) -> dict[str, tuple[Any, dict]]:
    out = {}
    for k, v in (getattr(cls, "PARAMS", None) or {}).items():
        out[str(k)] = (v.default, v.meta()) if isinstance(v, Param) else (v, {})
    return out


def _coerce(v: Any, default: Any, what: str) -> Any:
    """A user-entered value with the type of the param's default (bool / int / float / str)."""
    if default is None or v is None:
        return v
    if isinstance(default, bool):
        if isinstance(v, bool):
            return v
        t = str(v).strip().lower()
        if t in ("1", "true", "yes", "on"):
            return True
        if t in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{what}: expected true/false (got {v!r})")
    if isinstance(default, int):
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, (int, float)) and float(v).is_integer():
            return int(v)
        try:
            return int(str(v).strip(), 0)
        except ValueError:
            try:
                f = float(str(v))
                return int(f) if f.is_integer() else f
            except ValueError:
                raise ValueError(f"{what}: expected a number (got {v!r})") from None
    if isinstance(default, float):
        try:
            return float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{what}: expected a number (got {v!r})") from None
    if isinstance(default, str):
        return str(v)
    return v


_cls_cache: dict[tuple, type] = {}


# ================================================================================================ the board part
class _HostOnly:
    """Descriptor: an attribute that exists only while the part has a host serial endpoint (Session.host_endpoints
    selects parts with `pump` and `host_port`; a script without one must not make every run a paced run)."""

    def __init__(self, name: str):
        self.name = name

    def __get__(self, obj: Any, cls: Any = None) -> Any:
        if obj is None or obj._hostport is None:
            raise AttributeError(self.name)
        return obj._hostport if self.name == "host_port" else getattr(obj, "_" + self.name)


@part("script")
class ScriptHost(Part):
    """Custom part written in Python: `file` (relative to the board) or inline `code`, `class` (when the file has
    several), `params` {name: value} for the script's PARAMS; ports as the script's PORTS declare.
    Runs arbitrary Python with your privileges: only load boards you trust."""
    PARAMS = {"file": None, "code": None, "class": None, "params": None, "slow_ms": DEFAULT_SLOW_MS}
    PATH_PARAMS = ("file",)                  # resolved relative to the board file (chipem.board.parse_board)
    ID_PREFIX = "U"
    _BASE_KEYS = frozenset(PARAMS) | {"quiet"}

    host_port = _HostOnly("host_port")
    pump = _HostOnly("pump")

    def __init__(self, part_id: str, params: dict[str, Any] | None = None):
        params = dict(params or {})
        self.board_dir: str | None = params.pop("_board_dir", None)
        if isinstance(params.get("params"), str):        # typed in a UI field: "{temperature: 30}" (YAML / JSON)
            import yaml
            txt = params["params"].strip()
            try:
                params["params"] = yaml.safe_load(txt) if txt else None
            except yaml.YAMLError as e:
                raise ValueError(f"part {part_id}: params: not a mapping ({e})") from None
        extra = {k: params.pop(k) for k in list(params) if k not in self._BASE_KEYS}
        if extra:                            # `--part U5:script:file=x.py,temperature=30`: script params
            nested = dict(params.get("params") or {})
            nested.update(extra)
            params["params"] = nested
        self.obj: Any = None
        self.fault: dict | None = None
        self._hostport = None
        self._engines: list[_Engine] = []
        self._by_port: dict[str, list[_Engine]] = {}
        self._analog: dict[str, float | None] = {}
        self._prev: dict[str, str | None] = {}
        self._slow_warned: set = set()
        self._in_reset = False
        self.reloads = 0
        super().__init__(part_id, params)

    # ---------------------------------------------------------------- description
    @classmethod
    def _cls_port_descs(cls, params: dict[str, Any]) -> dict[str, PortDesc]:
        """Class-level (catalog, CLI --part key split): the ports of the script named in `params`, if loadable."""
        f, code = params.get("file"), params.get("code")
        if not f and not code:
            return {}
        try:
            src, label = (Path(f).read_text(), str(Path(f).resolve())) if f else (str(code), "<inline>")
            key = (label, hash(src), params.get("class"))
            ucls = _cls_cache.get(key) or load_source(src, label, "?", params.get("class"))
            _cls_cache[key] = ucls
            return {n: PortDesc(PORT_KINDS[k]) for n, (k, _p) in _class_ports(ucls, "?").items()}
        except Exception:
            return {}

    class _PortDescs:
        def __get__(self, obj: Any, cls: Any = None) -> Callable:
            if obj is None or not hasattr(obj, "kinds"):
                return cls._cls_port_descs
            return lambda params: {n: PortDesc(PORT_KINDS[k]) for n, k in obj.kinds.items()}

    port_descs = _PortDescs()

    # ---------------------------------------------------------------- setup / load
    def _setup(self) -> None:
        f, code = self.params.get("file"), self.params.get("code")
        if bool(f) == bool(code):
            raise ValueError(f"part {self.id} (script): give exactly one of `file` (a .py path) or `code`")
        if f:
            p = Path(str(f)).expanduser()
            self.params["file"] = str(p.resolve())
        try:
            self.params["slow_ms"] = float(self.params["slow_ms"])
        except (TypeError, ValueError):
            raise ValueError(f"part {self.id}: slow_ms must be a number") from None
        if self.params["params"] is not None and not isinstance(self.params["params"], dict):
            raise ValueError(f"part {self.id}: params must be a mapping name -> value")
        self.user_cls = self._compile()
        self._ports = _class_ports(self.user_cls, self.id)
        if not self._ports:
            raise ValueError(f"part {self.id}: {self.user_cls.__name__} declares no PORTS")
        self.kinds = {n: k for n, (k, _p) in self._ports.items()}
        self._scan = self._scan_class(self.user_cls)
        self.uparams: dict[str, Any] = {}
        self._apply_params(dict(self.params["params"] or {}))

    def _source(self) -> tuple[str, str]:
        f = self.params.get("file")
        if f:
            p = Path(f)
            if not p.is_file():
                raise ValueError(f"part {self.id}: script not found: {p}")
            return p.read_text(encoding="utf-8"), str(p)
        label = f"<part {self.id} code>"
        src = str(self.params.get("code"))
        linecache.cache[label] = (len(src), None, src.splitlines(True), label)     # tracebacks show the source
        return src, label

    def _compile(self) -> type:
        src, label = self._source()
        self.label = label
        return load_source(src, label, self.id, self.params.get("class"))

    def _apply_params(self, given: dict, keep: dict | None = None) -> None:
        decl = _class_params(self.user_cls)
        unknown = [k for k in given if k not in decl]
        if unknown:
            raise ValueError(f"part {self.id}: unknown parameter(s) {', '.join(map(str, unknown))} "
                             f"({self.user_cls.__name__} PARAMS: {', '.join(decl) or 'none'})")
        vals = {}
        for k, (dflt, _m) in decl.items():
            if k in given:
                vals[k] = _coerce(given[k], dflt, f"part {self.id}: {k}")
            elif keep is not None and k in keep:
                vals[k] = keep[k]
            else:
                vals[k] = dflt
        self.uparams.clear()
        self.uparams.update(vals)
        self.param_meta = {k: m for k, (_d, m) in decl.items()}
        self._sync_param_export()

    def _sync_param_export(self) -> None:
        decl = _class_params(self.user_cls)
        changed = {k: v for k, v in self.uparams.items() if k in decl and v != decl[k][0]}
        self.params["params"] = changed or None

    # ---------------------------------------------------------------- lifecycle
    def _reset_state(self) -> None:
        for k in self._out:
            self._out[k] = None
        for k in self._prev:
            self._prev[k] = None
        self.fault = None
        self._engines = []
        self._by_port = {}
        self._analog = {n: None for n, k in self.kinds.items() if k == "analog_out"}
        obj = self.user_cls.__new__(self.user_cls)
        obj._host = self
        obj.id = self.id
        obj.params = self.uparams
        obj.state = {}
        self.obj = obj
        self._edges = {port: [(kind, getattr(obj, name)) for kind, name in hs] for port, hs in self._scan[0].items()}
        self._has_on_change = callable(getattr(obj, "on_change", None))
        if self.board is None:
            return                           # not on a board yet: no time, no events (reset() follows attach)
        self._in_reset = True
        try:
            init = getattr(type(obj), "__init__", object.__init__)
            if init is not object.__init__:
                self._call(obj.__init__, what="__init__")
            if self.fault is None and hasattr(obj, "on_reset"):
                self._call(obj.on_reset, what="on_reset")
            for name, p in self._scan[1]:
                if self.fault is not None:
                    break
                self._timer(p, getattr(obj, name), (), p)
            if self.fault is None:
                for eng in list(self._engines):
                    self._guard_engine(eng.start)
        finally:
            self._in_reset = False
        for port in self._analog:
            self._apply_adc(port)

    def _scan_class(self, cls: type) -> tuple[dict, list, dict]:
        """Decorated methods of a user class: ({port: [(kind, method name)]}, [(method name, period ns)],
        {action label: method name}). ValueError for a decorator naming an unknown port or a bad period."""
        edges: dict[str, list[tuple[str, str]]] = {}
        every_: list[tuple[str, float]] = []
        actions: dict[str, str] = {}
        seen = set()
        for klass in cls.__mro__:
            for name, fn in vars(klass).items():
                if name in seen or not callable(fn):
                    continue
                seen.add(name)
                for kind, port in getattr(fn, "_chipem_edges", ()):
                    if port not in self.kinds:
                        raise ValueError(f"part {self.id}: @on_{'edge' if kind == 'edge' else kind + 'ing'}"
                                         f"({port!r}) on {name}: no such port (ports: {', '.join(self.kinds)})")
                    edges.setdefault(port, []).append((kind, name))
                for period in getattr(fn, "_chipem_every", ()):
                    p = _parse_time(period, f"part {self.id}: @every on {name}")
                    if p <= 0:
                        raise ValueError(f"part {self.id}: @every on {name}: period must be > 0")
                    every_.append((name, p))
                a = getattr(fn, "_chipem_action", None)
                if a:
                    actions[a] = name
        return edges, every_, actions

    @property
    def actions(self) -> dict[str, str]:
        return self._scan[2]

    def attach(self, board: Any) -> None:
        super().attach(board)

    def detach(self) -> None:
        self.close_host()
        super().detach()

    def close_host(self) -> None:
        if self._hostport is not None:
            try:
                self._hostport.close()
            except Exception:
                pass

    def part_added(self, other: Any) -> None:
        if other is self:                    # our ports are wired now: analog outputs reach the chips' ADCs
            for port in self._analog:
                self._apply_adc(port)

    def pull(self, port: str) -> str | None:
        p = self._ports.get(port)
        return p[1] if p else None

    # ---------------------------------------------------------------- guarded calls into user code
    def _call(self, fn: Callable, *args: Any, what: str | None = None) -> Any:
        if self.fault is not None:
            return None
        t0 = time.perf_counter()
        try:
            return fn(*args)
        except (Exception, SystemExit) as e:
            self._fault_exc(e, what or getattr(fn, "__name__", "?"))
            return None
        finally:
            dt = (time.perf_counter() - t0) * 1e3
            if dt > self.params["slow_ms"]:
                name = what or getattr(fn, "__name__", "?")
                if name not in self._slow_warned:
                    self._slow_warned.add(name)
                    self._warn(f"{name} took {dt:.0f} ms of wall time (limit slow_ms={self.params['slow_ms']:g}); "
                               f"the emulation waits for every callback")

    def _guard_engine(self, fn: Callable, *args: Any) -> None:
        try:
            fn(*args)
        except Exception as e:               # our engine or a bad value returned by a callback
            self._fault_exc(e, getattr(fn, "__qualname__", "engine"))

    def _fault_exc(self, e: BaseException, what: str) -> None:
        msg = _where(e, self.label)
        tb = [ln for ln in traceback.format_exception(type(e), e, e.__traceback__)]
        frames = traceback.extract_tb(e.__traceback__)
        own = [f for f in frames if f.filename == self.label]
        line = own[-1].lineno if own else None
        last = "".join(traceback.format_list(own[-3:] if own else frames[-3:])).rstrip()
        self._set_fault(f"{what}: {msg}", line, last or "".join(tb[-3:]).rstrip())

    def _fault_msg(self, msg: str) -> None:
        self._set_fault(msg, None, "")

    def _set_fault(self, msg: str, line: int | None, tb: str) -> None:
        if self.fault is not None:
            return
        t = self.now() if self.board is not None else 0.0
        self.fault = {"msg": msg, "line": line, "t_ns": t, "traceback": tb}
        self._cancel_all()
        if self.board is not None:
            self.board.log.event("warn", t, chip="board", part=self.id,
                                 msg=f"part {self.id} faulted (calls stopped until reset or reload): {msg}",
                                 traceback=tb)

    def _warn(self, msg: str) -> None:
        if self.board is not None:
            self.board.log.event("warn", self.now(), chip="board", part=self.id, msg=f"part {self.id}: {msg}")

    # ---------------------------------------------------------------- inputs
    def on_input(self, port: str, level: str, t_ns: float) -> None:
        prev = self._prev.get(port)
        self._prev[port] = level
        if self.fault is not None or self._in_reset or self.obj is None:
            return
        for eng in self._by_port.get(port, ()):
            self._guard_engine(eng.on_input, port, level, prev)
            if self.fault is not None:
                return
        if self._has_on_change:
            self._call(self.obj.on_change, port, _level_out(level), t_ns, what="on_change")
        hs = self._edges.get(port)
        if hs and prev in ("H", "L") and level != prev:
            for kind, fn in hs:
                if self.fault is not None:
                    return
                if kind == "edge":
                    self._call(fn, _level_out(level), what=fn.__name__)
                elif (kind == "rise" and prev == "L" and level == "H") or (kind == "fall" and prev == "H" and level == "L"):
                    self._call(fn, what=fn.__name__)

    # ---------------------------------------------------------------- API backends
    def _port(self, name: Any, *, inp: bool = False, out: bool = False) -> str:
        n = str(name)
        if n not in self.kinds:
            for k in self.kinds:
                if k.upper() == n.upper():
                    n = k
                    break
            else:
                raise ValueError(f"no port {name!r} (ports: {', '.join(self.kinds)})")
        d = PORT_KINDS[self.kinds[n]]
        if out and d == "in":
            raise ValueError(f"port {n} is an input ({self.kinds[n]}): it cannot be driven")
        if inp and d == "out" and self.kinds[n] != "analog_out":
            raise ValueError(f"port {n} is an output ({self.kinds[n]}): declare it \"in\" or \"inout\" to read it")
        return n

    def _level(self, port: str) -> str | None:
        n = self._port(port)
        if n in self.inputs and self.inputs[n] is not None:
            return self.inputs[n]
        net = self.board._port_net.get((self.id, n)) if self.board is not None else None
        return net.level if net is not None else None

    def _user_drive(self, port: str, value: Any) -> None:
        n = self._port(port, out=True)
        kind = self.kinds[n]
        if kind == "analog_out":
            raise ValueError(f"port {n} is an analog output: use analog({n!r}, volts)")
        lvl = _level_in(value, f"drive({n!r})")
        if kind == "od" and lvl == "H":
            lvl = None                       # open drain: 1 = released
        if self._in_reset:                   # power-on outputs: the board resolves the nets after the reset
            self._out[n] = lvl
            return
        self._drive(n, lvl)

    def _eng_drive(self, port: str, lvl: str | None) -> None:
        if self._in_reset:
            self._out[port] = lvl
        else:
            self._drive(port, lvl, quiet=True)

    def _set_analog(self, port: str, volts: Any) -> None:
        n = self._port(port, out=True)
        if self.kinds[n] != "analog_out":
            raise ValueError(f"port {n} is not an analog output (declare it \"analog_out\")")
        v = None if volts is None else float(volts)
        if self._analog.get(n) == v:
            return
        self._analog[n] = v
        if self.board is None or self._in_reset:
            return
        self._notify({"port": n, "volts": v})
        self._apply_adc(n)
        net = self.board._port_net.get((self.id, n))
        for p, pt in list(net.ports) if net is not None else ():
            if p is not self and isinstance(p, ScriptHost) and p.kinds.get(pt) == "analog_in":
                p._analog_input(pt, v)

    def _analog_input(self, port: str, v: float | None) -> None:
        if self.fault is None and self.obj is not None and hasattr(self.obj, "on_analog"):
            self._call(self.obj.on_analog, port, v, what="on_analog")

    def _read_analog(self, port: str) -> float | None:
        n = self._port(port)
        if self.kinds[n] == "analog_out":
            return self._analog.get(n)
        net = self.board._port_net.get((self.id, n)) if self.board is not None else None
        for p, pt in (net.ports if net is not None else ()):
            if isinstance(p, ScriptHost) and p.kinds.get(pt) == "analog_out" and p._analog.get(pt) is not None:
                return p._analog[pt]
        return None

    def _apply_adc(self, port: str) -> None:
        """Put the analog output's voltage on every chip ADC input of its net (model capabilities, no vendor code):
        `analog_sources` (callable volts per channel), `adc_volts` models' `set_input(pin, volts)`, else a raw code
        from ADC_FULL_SCALE through `set_channel(name, code)`."""
        b = self.board
        net = b._port_net.get((self.id, port)) if b is not None else None
        if net is None:
            return
        v = self._analog.get(port)
        fs = float(getattr(self.user_cls, "ADC_FULL_SCALE", 3.3) or 3.3)
        for _cid, chip, pin in net.members:
            names = []
            for fn in (pin.functions or {}).values():
                for s in str(fn).split("/"):
                    s = s.strip()
                    if "ADC" in s.upper() and s not in names:
                        names.append(s)
            blocks = chip.adc_blocks() if hasattr(chip, "adc_blocks") else {}
            for bname, blk in blocks.items():
                try:
                    src = getattr(blk, "analog_sources", None)
                    if isinstance(src, dict):
                        for nm in names:
                            m = re.fullmatch(rf"{re.escape(bname)}_SE(\d+)[AB]?", nm.upper())
                            if m:
                                src[f"SE{int(m.group(1))}"] = (lambda _p=port: self._analog.get(_p))
                        continue
                    if v is None:
                        continue
                    if getattr(blk, "adc_volts", False):
                        fn = getattr(blk, "set_input", None)
                        if fn is not None and fn(pin.id, float(v)):
                            continue
                        for nm in names:
                            try:
                                if blk.set_channel(nm, float(v)):
                                    break
                            except (KeyError, ValueError, TypeError):
                                pass
                        continue
                    code = max(0, min(4095, int(round(v / fs * 4095))))
                    for nm in names:
                        try:
                            if blk.set_channel(nm, code):
                                break
                        except (KeyError, ValueError, TypeError):
                            pass
                except Exception:
                    continue

    def _timer(self, dt_ns: float, fn: Callable, args: tuple, period_ns: float | None) -> Timer:
        if not callable(fn):
            raise ValueError(f"after/repeat: {fn!r} is not callable")
        t = Timer(self, fn, args, period_ns)
        if self.board is None:
            return t
        t.t_ns = self.now() + dt_ns
        t.active = True
        t._ev = self._at(t.t_ns, self._timer_fire, t)
        return t

    def _timer_fire(self, t: Timer) -> None:
        t._ev = None
        if not t.active or self.fault is not None:
            t.active = False
            return
        if t.period_ns is not None:
            t.t_ns += t.period_ns                # absolute times: no drift
            t._ev = self._at(t.t_ns, self._timer_fire, t)
        else:
            t.active = False
        self._call(t._fn, *t._args, what=getattr(t._fn, "__name__", "timer"))

    def _add_engine(self, eng: _Engine) -> None:
        if any(type(e) is type(eng) for e in self._engines):
            raise ValueError(f"part {self.id}: only one {type(eng).__name__.strip('_')} per script part")
        self._engines.append(eng)
        for p in eng.ports:
            self._by_port.setdefault(p, []).append(eng)
        if not self._in_reset and self.board is not None:
            self._guard_engine(eng.start)

    def _engine(self, cls: type) -> Any:
        for e in self._engines:
            if isinstance(e, cls):
                return e
        what = {"_SpiSlave": "spi_slave()", "_Uart": "uart()", "_I2cSlave": "i2c_slave()"}.get(cls.__name__, "")
        raise ValueError(f"call {what} first (in on_reset)")

    def _data_path(self, name: str) -> Path:
        p = Path(str(name)).expanduser()
        if p.is_absolute():
            return p
        f = self.params.get("file")
        base = Path(f).parent if f else Path(self.board_dir or ".")
        return base / p

    # ---- host endpoint (chipem/hostserial.py; I/O threads never call in here: Session pumps on the emulation thread)
    def _host_open(self, spec: str):
        if self._hostport is not None:
            return self._hostport
        from ..hostserial import open_port
        uart = next((e for e in self._engines if isinstance(e, _Uart)), None)
        self._hostport = open_port(spec, line=uart.line if uart is not None else None)
        if self._hostport is None:
            raise ValueError(f"host_open: no transport in {spec!r}")
        if self.board is not None:
            self.board.info(f"part {self.id}: host endpoint {self._hostport.kind} {self._hostport.describe()}")
        return self._hostport

    def _host_write(self, data: bytes) -> None:
        if self._hostport is None:
            raise ValueError("host_write: no host endpoint (call host_open first)")
        self._hostport.write(data)

    def _pump(self) -> None:
        h = self._hostport
        if h is None:
            return
        data = h.read()
        if not data or self.fault is not None or self.obj is None:
            return
        uart = next((e for e in self._engines if isinstance(e, _Uart) and e.host_spec), None)
        if uart is not None:
            uart.host_data(data)
        elif hasattr(self.obj, "on_host_data"):
            self._call(self.obj.on_host_data, data, what="on_host_data")

    def info(self) -> dict:
        h = self._hostport
        base = h.info() if h is not None else {"transport": None, "path": None, "connected": False}
        uart = next((e for e in self._engines if isinstance(e, _Uart)), None)
        return {"name": self.id, "kind": "part", **base,
                "line": str(uart.line) if uart is not None else base.get("line")}

    # ---- serial router hooks (chipem/parts/serial.py): the UART helper's RX port is a receiver
    def _uart(self) -> _Uart | None:
        return next((e for e in self._engines if isinstance(e, _Uart)), None)

    def serial_sink(self, port: str) -> dict | None:
        u = self._uart()
        if u is None or u.host_spec is not None or port != u.rx:
            return None
        return {"inv": 0, "domain": "ttl"}

    def serial_receive(self, b: int, *_a: Any, **_k: Any) -> None:
        u = self._uart()
        if u is not None:
            u.receive(int(b) & 0xFF)

    def serial_drop(self, b: int, reason: str = "", *_a: Any, **_k: Any) -> None:
        u = self._uart()
        if u is not None:
            u.dropped += 1
        if self.board is not None:
            self.board.log.event("uart", self.now(), chip="board", uart=f"{self.id}.{u.rx if u else 'RX'}",
                                 dir="drop", byte=b, dropped=1, reason=reason)

    def line(self):
        u = self._uart()
        if u is not None:
            return u.line
        from ..hostserial import LineSettings
        return LineSettings(115200.0)

    # ---------------------------------------------------------------- hot reload
    def reload(self, code: str | None = None) -> dict:
        """Re-read the script (or take new inline `code`), keep the board running: fresh object, on_reset, nets
        re-resolved. Raises ValueError (old code keeps running) on a load error or when the ports changed."""
        old = (self.user_cls, self.label, self.params.get("code"))
        if code is not None:
            if self.params.get("file"):
                raise ValueError(f"part {self.id}: the script is a file; save the file instead")
            self.params["code"] = str(code)
        try:
            ucls = self._compile()
            ports = _class_ports(ucls, self.id)
            if ports != self._ports:
                def fmt(d):
                    return ", ".join(f"{n}:{k}{' ' + p + 'pull' if p else ''}" for n, (k, p) in d.items())
                raise ValueError(f"part {self.id}: the ports changed ({fmt(self._ports)} -> {fmt(ports)}); "
                                 f"a reload keeps the board's wiring, so reload the board to apply new ports")
            scan = self._scan_class(ucls)
            keep = dict(self.uparams)
            prev = (self.user_cls, self._scan)
            self.user_cls, self._scan = ucls, scan
            try:
                self._apply_params({}, keep=keep)
            except ValueError:
                self.user_cls, self._scan = prev
                raise
        except ValueError:
            self.user_cls, self.label, self.params["code"] = old[0], old[1], old[2]
            raise
        self.reloads += 1
        self._slow_warned.clear()
        self.reset()
        b = self.board
        if b is not None:
            for port in self.ports:
                net = b._port_net.get((self.id, port))
                if net is not None:
                    net.update(b.clock_chip)
            self.emit(action="reload", file=self.params.get("file") or "<inline>", reloads=self.reloads)
        return self.state()

    def source(self) -> dict:
        """{file, code, inline} for an editor."""
        f = self.params.get("file")
        if f:
            p = Path(f)
            return {"file": f, "inline": False, "code": p.read_text(encoding="utf-8") if p.is_file() else ""}
        return {"file": None, "inline": True, "code": str(self.params.get("code") or "")}

    def save_source(self, code: str) -> dict:
        """Write the script (file or inline code) and reload it. The file is written only after the new code
        compiles and loads (ports unchanged); otherwise ValueError and nothing changes."""
        code = str(code)
        f = self.params.get("file")
        if not f:
            return self.reload(code)
        load_source(code, f, self.id, self.params.get("class"))            # validate first
        p = Path(f)
        old = p.read_text(encoding="utf-8") if p.is_file() else None
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(code, encoding="utf-8")
        tmp.replace(p)
        try:
            return self.reload()
        except ValueError:
            if old is not None:
                p.write_text(old, encoding="utf-8")
            raise

    # ---------------------------------------------------------------- state / UI
    def state(self) -> dict:
        d: dict[str, Any] = {}
        st = getattr(self.obj, "state", None)
        if isinstance(st, dict):
            for k, v in st.items():
                d[str(k)] = v
        if self.fault is not None:
            d["fault"] = self.fault["msg"]
        d["params"] = dict(self.uparams)
        return d

    def ui_info(self) -> dict:
        """Static-ish description for UIs: file, class, port kinds, controls, actions, fault."""
        decl = _class_params(self.user_cls)
        return {"file": self.params.get("file"), "inline": not self.params.get("file"),
                "class": self.user_cls.__name__, "doc": " ".join((self.user_cls.__doc__ or "").split()),
                "ports": dict(self.kinds),
                "controls": [{"name": k, "default": decl[k][0], **self.param_meta.get(k, {})} for k in decl],
                "actions": list(self.actions), "fault": self.fault, "reloads": self.reloads}

    def set(self, **values: Any) -> dict:
        for k, v in values.items():
            if k == "action":
                if self.fault is not None:
                    raise ValueError(f"part {self.id} is faulted: {self.fault['msg']} (reset or reload it)")
                name = self.actions.get(str(v))
                if name is None:
                    raise ValueError(f"part {self.id}: no action {v!r} (actions: {', '.join(self.actions) or 'none'})")
                self._call(getattr(self.obj, name), what=name)
            elif k == "reload":
                self.reload()
            elif k in self.uparams:
                dflt = _class_params(self.user_cls)[k][0]
                nv = _coerce(v, dflt, f"part {self.id}: {k}")
                meta = self.param_meta.get(k, {})
                if isinstance(nv, (int, float)) and not isinstance(nv, bool):
                    if meta.get("min") is not None and nv < meta["min"] or \
                            meta.get("max") is not None and nv > meta["max"]:
                        raise ValueError(f"part {self.id}: {k}={nv} outside {meta.get('min')}..{meta.get('max')}")
                if meta.get("choices") and nv not in meta["choices"]:
                    raise ValueError(f"part {self.id}: {k} must be one of {meta['choices']}")
                self.uparams[k] = nv
                self._sync_param_export()
                if self.obj is not None and hasattr(self.obj, "on_param") and self.board is not None:
                    self._call(self.obj.on_param, k, nv, what="on_param")
            else:
                raise ValueError(f"part {self.id} ({self.user_cls.__name__}): cannot set {k!r} (params: "
                                 f"{', '.join(self.uparams) or 'none'}; action=NAME; reload=1)")
        return self.state()

    def stimulus(self, value: str) -> dict:
        """`--at 5ms:U5=temperature=30` / `U5=calibrate` (an action) / `U5=reload`."""
        v = str(value).strip()
        if "=" in v:
            k, _, val = v.partition("=")
            return self.set(**{k.strip(): val.strip()})
        if v.lower() == "reload":
            return self.set(reload=1)
        return self.set(action=v)

    def to_dict(self) -> dict:
        d = super().to_dict()
        for k in ("code", "class", "params"):
            if d.get(k) is None:
                d.pop(k, None)
        if d.get("slow_ms") == DEFAULT_SLOW_MS:
            d.pop("slow_ms")
        if d.get("file") is None:
            d.pop("file", None)
        return d
