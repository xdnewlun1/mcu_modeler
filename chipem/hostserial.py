"""Host serial bridge: connect PC software on the machine running chipem to a simulated UART.

Vendor-free (no peripheral or family knowledge). Two halves:

1. **Transports** (`HostPort` subclasses) talk to the host operating system on their own I/O threads:

   * `PtyPort`    a pseudo-terminal (Linux/macOS). The emulator keeps the master side; PC software opens the slave
                  path (`/dev/pts/N`, optionally through a stable symlink) like `/dev/ttyUSB0`. Reopen-safe: the
                  host program may open and close it any number of times; "connected" = a program has it open.
   * `SerialPort` an existing host serial port through pyserial (optional dependency, imported lazily): one end of
                  a virtual null-modem pair (com0com on Windows, `socat` on Linux) or a real USB adapter wired back
                  to back with the PC's. Modem lines follow null-modem wiring (see `modem_from_host`).
   * `TcpPort`    a raw TCP listener (`tcp:PORT`, one client at a time). Handy on Windows without com0com.
   * `MemoryPort` in-process; tests and scripts play the host (`host_send`, `host_recv`).

   The byte-stream API (`read`, `write`, `line`, `modem_from_host`, `set_modem_to_host`, `info`, `close`,
   `listeners`) is documented in docs/CONTRACTS.md "Host serial ports" and is all a consumer needs (a future USB
   CDC-ACM device model can reuse it).

2. **Bindings** live on the emulation thread. `UartBridge` wires a port straight to a UART model (TTL level, like a
   USB-TTL cable on the TX/RX pins: `--uart-pty UART0`); the `usb_serial_adapter` board part (chipem/parts/serial.py)
   binds a port to the PC end of a modelled cable chain.

Threading rule: the Session/Chip is not thread-safe. I/O threads never touch it: host bytes go into a lock-protected
buffer that the emulation thread drains (`pump()`, called by Session between run slices, chipem/realtime.py), and
chip bytes go out through a queue that a writer thread empties. Listeners (`HostPort.listeners`, called on the I/O
thread) only set a threading.Event so an idle real-time run wakes within a few ms.

Wire rules (as chipem/periph/uartwire.py): host bytes are paced at the host's line speed (PTY: the baud the host
program set with tcsetattr; serial: the configured one; TCP/memory: the UART's own) and each byte arrives at its
stop bit through the UART's receive path, so it is dropped when the receiver is not listening or overruns like on
silicon. A chip byte reaches the host when its stop bit completes. Frames carry their format both ways:

* host -> chip: a host byte is a `Frame` in the host's line format (serial: the configured bits/parity/stop; PTY:
  the baud the program set, the frame layout of the UART since a Linux pty keeps no CSIZE/PARENB; TCP/memory: the
  UART's own format), decoded by the UART against its own settings: a mismatch gives framing / parity errors and
  garbage characters exactly as the sample points dictate (no longer a plain drop). One `warn` names both settings.
* chip -> host: the chip's frame is decoded with the host's line settings, like the PC's UART would (a matching PTY
  layout is assumed). The host gets what a Linux tty in raw mode reads (termios: IGNPAR, PARMRK and INPCK clear): a
  character with a framing error, or a break, reads as 0x00; parity is not checked. Errors are logged as `uart`
  events {uart: "host:A.UART0", dir: "rx", fe/pe/brk}.
* Breaks: a chip break reaches a pyserial port as `send_break(duration)` (the PC program sees its driver's break);
  on a PTY, TCP or memory port it is the 0x00 byte a raw tty reads. Host -> chip breaks: `MemoryPort.host_break()`.
  A program's tcsendbreak() on a Linux pty is not visible on the master side, pyserial cannot report a received
  break, and raw TCP has no out-of-band break (RFC 2217 is not implemented: use pyserial's rfc2217:// URL with
  --uart-port for a remote serial server), so those transports cannot send breaks to the chip; parity and framing
  errors of the PC's own receiver are not visible to us either.
Bytes the chip sends while no host program has the port open are lost (`dropped_out`), like a PC driver with the
port closed.
"""
from __future__ import annotations

import errno
import os
import queue
import re
import socket
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

__all__ = ["LineSettings", "HostPort", "PtyPort", "SerialPort", "TcpPort", "MemoryPort", "UartBridge", "open_port",
           "parse_format", "parse_bridge_arg", "framing_mismatch", "BAUD_TOLERANCE", "TRANSPORTS"]

BAUD_TOLERANCE = 0.03               # relative baud difference a real UART receiver tolerates
TX_QUEUE_LIMIT = 65536              # bytes queued for a host that does not read: further bytes are lost
TRANSPORTS = ("pty", "serial", "tcp", "memory")


# ------------------------------------------------------------------------------------------------ line settings
_FMT = re.compile(r"^([5-9])([NEOMS])(1|1\.5|2)$", re.I)


def parse_format(s: str) -> tuple[int, str, float]:
    """'8N1' -> (8, 'N', 1.0); '7E2' -> (7, 'E', 2.0)."""
    m = _FMT.match(str(s).strip())
    if not m:
        raise ValueError(f"bad serial format {s!r} (expected e.g. 8N1, 7E1, 8N2)")
    return int(m.group(1)), m.group(2).upper(), float(m.group(3))


@dataclass(frozen=True)
class LineSettings:
    """Host line settings. `framed=False`: only the baud is known (a Linux PTY keeps the speed but not CSIZE/PARENB),
    so only the baud is compared."""
    baud: float
    bits: int = 8
    parity: str = "N"
    stop: float = 1.0
    framed: bool = True

    def char_bits(self) -> float:
        return 1 + self.bits + (0 if self.parity == "N" else 1) + self.stop

    def char_ns(self) -> float:
        return self.char_bits() * 1e9 / self.baud if self.baud > 0 else 0.0

    def frame(self, b: int):
        """The host transmitting byte `b` in this format (chipem.periph.uartwire.Frame)."""
        from .periph.uartwire import Frame
        return Frame(b, self.bits, self.parity, self.stop, self.baud)

    def rx_format(self):
        """How the host's UART receives (16x sampling, majority vote)."""
        from .periph.uartwire import RxFormat
        return RxFormat(self.baud, self.bits, self.parity, self.stop)

    def __str__(self) -> str:
        base = f"{self.baud:g}"
        return f"{base} {self.bits}{self.parity}{self.stop:g}" if self.framed else f"{base} baud"


def framing_mismatch(baud: float, bits: float | None, line: LineSettings | None) -> str | None:
    """Why a UART at (`baud`, `bits` per character incl. start/stop) cannot talk to a host at `line`, or None."""
    if line is None or not baud or baud <= 0 or line.baud <= 0:
        return None                 # nothing to compare: a side without a baud generator / unknown host speed
    if abs(baud - line.baud) / max(baud, line.baud) > BAUD_TOLERANCE:
        return f"baud {line.baud:g} vs {baud:.0f}"
    if line.framed and bits is not None and abs(float(bits) - line.char_bits()) > 0.01:
        return f"frame {line.char_bits():g} vs {float(bits):g} bits/char"
    return None


def _termios_speeds() -> dict[int, int]:
    try:
        import termios
    except ImportError:
        return {}
    out = {}
    for k in dir(termios):
        m = re.fullmatch(r"B(\d+)", k)
        if m:
            out[int(getattr(termios, k))] = int(m.group(1))
    return out


# ------------------------------------------------------------------------------------------------ transports
class HostPort:
    """Base class: a byte stream to host software, thread-safe on the emulation side."""

    kind = "?"

    def __init__(self) -> None:
        self._rx = bytearray()                  # host -> emulation, filled by the I/O thread
        self._rx_brk: list[tuple[int, float]] = []    # host breaks: (offset in _rx, duration s)
        self.breaks_in = 0
        self.breaks_out = 0
        self._rx_lock = threading.Lock()
        self._txq: queue.Queue = queue.Queue()  # emulation -> host: bytes | ("modem", dict) | None (stop)
        self._tx_pending = 0
        self._tx_lock = threading.Lock()
        self.connected = False
        self.connects = 0                       # host programs that opened the port so far
        self.bytes_in = 0                       # host -> emulation
        self.bytes_out = 0                      # emulation -> host (written)
        self.dropped_out = 0                    # emulation -> host lost: nobody connected / host not reading
        self.error: str | None = None
        self.listeners: list[Callable[[], None]] = []   # called on the I/O thread on input/connection changes
        self._closed = threading.Event()
        self._threads: list[threading.Thread] = []
        self._modem_host: dict[str, bool | None] = {"rts": None, "dtr": None}
        self._modem_dev: dict[str, bool] = {"cts": False, "dsr": False, "dcd": False, "ri": False}

    # ---- emulation side (any thread) -------------------------------------------------------------------------
    def read(self) -> bytes:
        """Drain what the host sent so far."""
        if not self._rx:
            return b""
        with self._rx_lock:
            data = bytes(self._rx)
            self._rx.clear()
        return data

    def read_stream(self) -> list:
        """Like read(), with the host's breaks in order: [bytes, ("break", seconds), bytes, ...]."""
        if not self._rx and not self._rx_brk:
            return []
        with self._rx_lock:
            data = bytes(self._rx)
            marks = list(self._rx_brk)
            self._rx.clear()
            self._rx_brk.clear()
        out: list = []
        pos = 0
        for off, dur in marks:
            if off > pos:
                out.append(data[pos:off])
                pos = off
            out.append(("break", dur))
        if pos < len(data):
            out.append(data[pos:])
        return out

    def send_break(self, seconds: float) -> None:
        """Device -> host break. Default (PTY, TCP, memory): the 0x00 a raw-mode tty reads for a break."""
        self.breaks_out += 1
        self.write(b"\x00")

    def pending(self) -> int:
        return len(self._rx)

    def write(self, data: bytes) -> int:
        """Queue bytes for the host. Returns the bytes accepted; bytes are lost when no host program is connected
        or the host stopped reading (TX_QUEUE_LIMIT)."""
        data = bytes(data)
        if not data:
            return 0
        with self._tx_lock:
            if not self.connected or self._closed.is_set() or self._tx_pending + len(data) > TX_QUEUE_LIMIT:
                self.dropped_out += len(data)
                return 0
            self._tx_pending += len(data)
        self._txq.put(data)
        return len(data)

    def line(self) -> LineSettings | None:
        """Line settings the host uses, if the transport knows them."""
        return None

    def modem_from_host(self) -> dict[str, bool | None]:
        """Host outputs as seen by the device: {"rts": bool|None, "dtr": bool|None} (None = unknown)."""
        return dict(self._modem_host)

    def set_modem_to_host(self, **lines: bool) -> None:
        """Device outputs towards the host's inputs: cts, dsr, dcd, ri."""
        changed = False
        for k, v in lines.items():
            if k in self._modem_dev and self._modem_dev[k] != bool(v):
                self._modem_dev[k] = bool(v)
                changed = True
        if changed:
            self._apply_modem()

    def _apply_modem(self) -> None:
        """Transport hook: push `_modem_dev` to the host (default: not supported, only recorded)."""

    def describe(self) -> str:
        return self.kind

    def info(self) -> dict:
        ln = self.line()
        return {"transport": self.kind, "path": self.describe(), "connected": self.connected,
                "connects": self.connects, "line": str(ln) if ln is not None else None, "bytes_in": self.bytes_in,
                "bytes_out": self.bytes_out, "dropped_out": self.dropped_out, "error": self.error,
                "modem_in": self.modem_from_host(), "modem_out": dict(self._modem_dev)}

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._txq.put(None)
        self._close_os()
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(1.0)

    def _close_os(self) -> None:
        pass

    # ---- I/O thread helpers ----------------------------------------------------------------------------------
    def _notify(self) -> None:
        for cb in list(self.listeners):
            try:
                cb()
            except Exception:
                pass

    def _host_bytes(self, data: bytes) -> None:
        with self._rx_lock:
            self._rx += data
        self.bytes_in += len(data)
        self._notify()

    def _set_connected(self, on: bool) -> None:
        if on == self.connected:
            return
        self.connected = on
        if on:
            self.connects += 1
        else:
            self._discard_tx()
        self._notify()

    def _discard_tx(self) -> None:
        while True:
            try:
                item = self._txq.get_nowait()
            except queue.Empty:
                break
            if item is None:
                self._txq.put(None)
                break
            if isinstance(item, (bytes, bytearray)):
                with self._tx_lock:
                    self._tx_pending -= len(item)
                    self.dropped_out += len(item)

    def _start(self, *targets: Callable[[], None]) -> None:
        for fn in targets:
            t = threading.Thread(target=self._guard, args=(fn,), name=f"chipem-{self.kind}-{fn.__name__}",
                                 daemon=True)
            self._threads.append(t)
            t.start()

    def _guard(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - surface in info(), never kill the process
            if not self._closed.is_set():
                self.error = f"{type(e).__name__}: {e}"
                self._set_connected(False)
                self._notify()

    def _writer(self, write: Callable[[bytes], None], modem: Callable[[dict], None] | None = None) -> None:
        while True:
            item = self._txq.get()
            if item is None:
                return
            if isinstance(item, tuple):
                if modem is not None:
                    modem(item[1])
                continue
            try:
                if not self.connected:
                    raise OSError("not connected")
                write(item)
                self.bytes_out += len(item)
            except OSError:
                self.dropped_out += len(item)
            finally:
                with self._tx_lock:
                    self._tx_pending -= len(item)


class MemoryPort(HostPort):
    """In-process host: `host_send(data)` plays a host program typing, `host_recv()` returns what the device sent.
    `line` / `connected` are settable."""

    kind = "memory"

    def __init__(self, line: LineSettings | None = None, *, connected: bool = True):
        super().__init__()
        self._line = line
        self.received = bytearray()
        self.connected = connected
        self.connects = int(connected)
        self._start(self._run)

    def _run(self) -> None:
        self._writer(self.received.extend, lambda d: None)

    def host_send(self, data: bytes | str) -> None:
        if isinstance(data, str):
            data = data.encode("utf-8")
        if self.connected:
            self._host_bytes(bytes(data))

    def host_break(self, seconds: float = 0.0) -> None:
        """The host sends a break (length in seconds; 0 = the receiving UART's default)."""
        if self.connected:
            with self._rx_lock:
                self._rx_brk.append((len(self._rx), float(seconds)))
            self.breaks_in += 1
            self._notify()

    def host_recv(self, timeout: float = 0.0) -> bytes:
        end = time.monotonic() + timeout
        while self._tx_pending and time.monotonic() < end:
            time.sleep(0.001)
        out = bytes(self.received)
        self.received.clear()
        return out

    def set_line(self, line: LineSettings | None) -> None:
        self._line = line

    def set_host_modem(self, **lines: bool | None) -> None:
        self._modem_host.update({k: v for k, v in lines.items() if k in ("rts", "dtr")})
        self._notify()

    def set_connected(self, on: bool) -> None:
        self._set_connected(bool(on))

    def line(self) -> LineSettings | None:
        return self._line


class PtyPort(HostPort):
    """Pseudo-terminal. `link`: optional symlink to the slave path (a stable name for the PC software)."""

    kind = "pty"

    def __init__(self, link: str | None = None):
        super().__init__()
        if not hasattr(os, "openpty"):
            raise ValueError("pseudo-terminals are not available on this platform; use a TCP port (--uart-tcp) "
                             "or a virtual COM pair such as com0com with --uart-port")
        import termios
        import tty
        self._termios = termios
        self.master, slave = os.openpty()
        try:
            self.path = os.ttyname(slave)
            tty.setraw(slave)               # no echo/line editing in the pty itself: bytes pass unchanged
        finally:
            os.close(slave)                 # only the host keeps the slave open: EIO on read = nobody connected
        os.set_blocking(self.master, False)
        self.link = None
        if link:
            self._make_link(str(link))
        self._speeds = _termios_speeds()
        self._line_cache: tuple[float, LineSettings | None] = (0.0, None)
        self._start(self._reader, self._run_writer)

    def _make_link(self, link: str) -> None:
        if os.path.lexists(link):
            if not os.path.islink(link):
                os.close(self.master)
                raise ValueError(f"--uart-pty link {link!r} exists and is not a symlink; not replacing it")
            os.unlink(link)
        os.symlink(self.path, link)
        self.link = link

    def describe(self) -> str:
        return f"{self.path} (link {self.link})" if self.link else self.path

    def _reader(self) -> None:
        import select
        m = self.master
        poll = select.poll()
        poll.register(m, select.POLLIN | select.POLLHUP | select.POLLERR)
        while not self._closed.is_set():
            ev = poll.poll(50)
            if not ev:
                self._set_connected(True)   # quiet but open: a closed slave would report POLLHUP at once
                continue
            try:
                data = os.read(m, 4096)
            except BlockingIOError:
                continue
            except OSError as e:
                if e.errno == errno.EIO:    # no program has the slave open
                    if self.connected:
                        self._flush()
                    self._set_connected(False)
                    self._closed.wait(0.02)
                    continue
                raise
            if data:
                self._set_connected(True)
                self._host_bytes(data)

    def _flush(self) -> None:
        try:
            self._termios.tcflush(self.master, self._termios.TCIOFLUSH)
        except (OSError, self._termios.error):
            pass

    def _run_writer(self) -> None:
        def write(data: bytes) -> None:
            view = memoryview(data)
            while view and not self._closed.is_set():
                try:
                    n = os.write(self.master, view)
                    view = view[n:]
                except BlockingIOError:
                    self._closed.wait(0.002)
        self._writer(write)

    def line(self) -> LineSettings | None:
        """Baud the host program set (Linux ptys keep the speed but drop CSIZE/PARENB, so the frame is unknown);
        None while no program has the port open. Re-read at most every 50 ms."""
        if not self.connected:
            return None                     # no program has it open: no settings to disagree with
        now = time.monotonic()
        t, ln = self._line_cache
        if now - t < 0.05:
            return ln
        try:
            attrs = self._termios.tcgetattr(self.master)
            sp = attrs[5] or attrs[4]
            baud = self._speeds.get(int(sp), int(sp) if int(sp) > 50 else 0)
            ln = LineSettings(float(baud), framed=False) if baud else None
        except (OSError, self._termios.error):
            ln = None
        self._line_cache = (now, ln)
        return ln

    def modem_from_host(self) -> dict[str, bool | None]:
        # a pty has no modem lines (TIOCMGET fails): a host program that opens the port raises DTR and RTS, so
        # both read asserted while a program has it open
        return {"rts": self.connected, "dtr": self.connected}

    def _close_os(self) -> None:
        try:
            os.close(self.master)
        except OSError:
            pass
        if self.link:
            try:
                if os.path.islink(self.link):
                    os.unlink(self.link)
            except OSError:
                pass


class SerialPort(HostPort):
    """Host serial port through pyserial (`pip install pyserial`, extra `chipem[serial]`). `path` may be a device
    (/dev/ttyUSB0, COM5) or a pyserial URL (loop://, socket://host:port, rfc2217://host:port)."""

    kind = "serial"

    def __init__(self, path: str, line: LineSettings | None = None):
        super().__init__()
        try:
            import serial  # noqa: F401
        except ImportError:
            raise ValueError("--uart-port needs pyserial: pip install pyserial (or the chipem[serial] extra)") from None
        import serial
        self._line = line or LineSettings(115200.0)
        ln = self._line
        par = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD,
               "M": serial.PARITY_MARK, "S": serial.PARITY_SPACE}[ln.parity]
        stop = {1.0: serial.STOPBITS_ONE, 1.5: serial.STOPBITS_ONE_POINT_FIVE, 2.0: serial.STOPBITS_TWO}[ln.stop]
        self.path = path
        try:
            self.ser = serial.serial_for_url(path, baudrate=int(ln.baud), bytesize=ln.bits, parity=par,
                                             stopbits=stop, timeout=0.05)
        except (serial.SerialException, ValueError, OSError) as e:
            raise ValueError(f"cannot open serial port {path}: {e}") from None
        self.connected = True
        self.connects = 1
        self._start(self._reader, self._run_writer)

    def describe(self) -> str:
        return self.path

    def line(self) -> LineSettings | None:
        return self._line

    def _poll_modem(self) -> None:
        # null-modem wiring: the host's RTS arrives on our CTS, its DTR on our DSR (and DCD)
        try:
            new = {"rts": bool(self.ser.cts), "dtr": bool(self.ser.dsr)}
        except Exception:
            return
        if new != self._modem_host:
            self._modem_host = new
            self._notify()

    def _reader(self) -> None:
        while not self._closed.is_set():
            n = self.ser.in_waiting if hasattr(self.ser, "in_waiting") else 1
            data = self.ser.read(n or 1)
            if data:
                self._host_bytes(bytes(data))
            self._poll_modem()

    def _run_writer(self) -> None:
        def modem(d: dict) -> None:
            try:
                if "seconds" in d:
                    self.ser.send_break(duration=max(d["seconds"], 0.001))
                    return
                self.ser.rts = bool(d.get("cts"))                      # our RTS -> host CTS
                self.ser.dtr = bool(d.get("dsr") or d.get("dcd"))      # our DTR -> host DSR + DCD
            except Exception:
                pass
        self._writer(self.ser.write, modem)

    def _apply_modem(self) -> None:
        self._txq.put(("modem", dict(self._modem_dev)))

    def send_break(self, seconds: float) -> None:
        """A real break on the port (pyserial send_break, run on the writer thread)."""
        self.breaks_out += 1
        self._txq.put(("break", {"seconds": max(float(seconds), 0.0)}))

    def _close_os(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass


class TcpPort(HostPort):
    """Raw TCP listener: one client at a time (a second one is refused while the first is connected). Use a raw
    client (nc, PuTTY "Raw", socat); telnet negotiation bytes would reach the UART as data."""

    kind = "tcp"

    def __init__(self, port: int, host: str = "127.0.0.1"):
        super().__init__()
        self.host = host
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind((host, int(port)))
        except OSError as e:
            self.sock.close()
            raise ValueError(f"cannot listen on tcp {host}:{port}: {e}") from None
        self.sock.listen(1)
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.client: socket.socket | None = None
        self._start(self._acceptor, self._run_writer)

    def describe(self) -> str:
        return f"tcp {self.host}:{self.port}"

    def _acceptor(self) -> None:
        while not self._closed.is_set():
            try:
                c, _addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if self.client is not None:
                c.close()                   # busy: one host program at a time, like a COM port
                continue
            c.settimeout(0.1)
            self.client = c
            self._set_connected(True)
            t = threading.Thread(target=self._guard, args=(lambda: self._serve(c),), name="chipem-tcp-client",
                                 daemon=True)
            t.start()

    def _serve(self, c: socket.socket) -> None:
        try:
            while not self._closed.is_set():
                try:
                    data = c.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                self._host_bytes(data)
        finally:
            self.client = None
            self._set_connected(False)
            try:
                c.close()
            except OSError:
                pass

    def _run_writer(self) -> None:
        def write(data: bytes) -> None:
            c = self.client
            if c is None:
                raise OSError("no client")
            c.sendall(data)
        self._writer(write)

    def modem_from_host(self) -> dict[str, bool | None]:
        return {"rts": self.connected, "dtr": self.connected}

    def _close_os(self) -> None:
        for s in (self.client, self.sock):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


# ------------------------------------------------------------------------------------------------ specs
_SER_TAIL = re.compile(r"^(.*?)(?::(\d+))?(?::([5-9][NEOMSneoms](?:1|1\.5|2)))?$")


def open_port(spec: str | None, *, line: LineSettings | None = None) -> HostPort | None:
    """Transport spec -> open HostPort (None for "none"/empty):

    "pty" | "pty:/path/link" | "tcp:PORT" | "tcp:HOST:PORT" | "serial:/dev/ttyUSB0[:BAUD[:8N1]]" | "memory"
    A bare device path or COMn means serial."""
    if spec is None:
        return None
    s = str(spec).strip()
    if s.lower() in ("", "none", "null", "-", "off"):
        return None
    kind, _, rest = s.partition(":")
    k = kind.lower()
    if k == "pty":
        return PtyPort(rest or None)
    if k == "tcp":
        host, _, port = rest.rpartition(":")
        if not port.isdigit():
            raise ValueError(f"bad tcp port spec {spec!r} (expected tcp:PORT or tcp:HOST:PORT)")
        return TcpPort(int(port), host or "127.0.0.1")
    if k == "memory":
        return MemoryPort(line)
    if k == "serial":
        path_spec = rest
    else:
        path_spec = s                       # bare path / COMn / pyserial URL
    m = _SER_TAIL.match(path_spec)
    path, baud, fmt = m.group(1), m.group(2), m.group(3)
    if "://" in path_spec and baud is None and fmt is None:
        path = path_spec
    if not path:
        raise ValueError(f"bad serial port spec {spec!r}")
    ln = line or LineSettings(115200.0)
    if baud:
        ln = LineSettings(float(baud), ln.bits, ln.parity, ln.stop)
    if fmt:
        b, p, st = parse_format(fmt)
        ln = LineSettings(ln.baud, b, p, st)
    return SerialPort(path, ln)


def parse_bridge_arg(kind: str, arg: str) -> tuple[str, str]:
    """CLI argument -> (uart name, transport spec).

    --uart-pty  UART0 | UART0=/tmp/link            -> ("UART0", "pty[:/tmp/link]")
    --uart-port UART0=/dev/ttyUSB0[:115200[:8N1]]   -> ("UART0", "serial:/dev/ttyUSB0:115200:8N1")
    --uart-tcp  UART0=5555 | UART0=0.0.0.0:5555     -> ("UART0", "tcp:5555")
    The UART name may be chip-qualified ("B.USART1")."""
    a = str(arg).strip()
    name, eq, val = a.partition("=")
    name = name.strip()
    if not eq:
        if kind == "pty" and (name.startswith("/") or name.startswith(".")):
            return "UART0", f"pty:{name}"
        if kind == "pty":
            return name.upper() or "UART0", "pty"
        raise ValueError(f"bad --uart-{'port' if kind == 'serial' else kind} {arg!r} (expected UART=VALUE)")
    if not name:
        raise ValueError(f"bad host serial argument {arg!r}: missing UART name")
    val = val.strip()
    if kind == "pty":
        return name.upper(), f"pty:{val}" if val else "pty"
    if kind == "tcp":
        if not val.rpartition(":")[2].isdigit():
            raise ValueError(f"bad --uart-tcp {arg!r} (expected UART=PORT or UART=HOST:PORT)")
        return name.upper(), f"tcp:{val}"
    if kind == "serial":
        if not val:
            raise ValueError(f"bad --uart-port {arg!r} (expected UART=DEVICE[:BAUD[:8N1]])")
        return name.upper(), f"serial:{val}"
    raise ValueError(f"unknown host serial transport {kind!r}")


# ------------------------------------------------------------------------------------------------ binding
def host_rx_format(line: LineSettings | None, fr: Any):
    """The host UART's receive format for chip frame `fr`: the host's line, or (PTY: frame unknown) the frame's own
    layout at the host's baud; None when the host has no line settings (TCP / memory: bytes pass as they are)."""
    from .periph.uartwire import RxFormat
    if line is None or line.baud <= 0:
        return None
    if line.framed:
        return line.rx_format()
    return RxFormat(line.baud, fr.bits, fr.parity if fr.parity in "NEO" else "N", fr.stop, addr=fr.addr is not None,
                    msbf=fr.msbf, inv=fr.inv)


def host_bytes_for(fr: Any, rx: Any) -> list:
    """What a raw-mode tty reads for chip frame `fr` (see the module docstring): [(byte | "break", RxChar | None)]."""
    from .periph.uartwire import decode_frame
    if rx is None:
        return [("break", None)] if fr.brk else [(fr.data & 0xFF, None)]
    out = []
    for ch in decode_frame(fr, rx):
        if ch.brk:
            out.append(("break", ch))
        else:
            out.append((0 if ch.fe else ch.data & 0xFF, ch))
    return out


def host_frame_for(blk: Any, line: LineSettings | None, b: int):
    """The frame a host byte `b` becomes on the line towards UART model `blk`: the host's format when known (PTY:
    the UART's layout at the host's baud), else the UART's own format."""
    fmt = getattr(blk, "rx_format", None)
    own = fmt() if fmt is not None else None
    if line is not None and line.baud > 0:
        if line.framed:
            return line.frame(b)
        if own is not None:
            from .periph.uartwire import Frame
            return Frame(b, own.bits, own.parity, own.stop, line.baud, addr=0 if own.addr else None,
                         msbf=own.msbf, inv=own.inv)
    return own.frame(b) if own is not None else None


class UartBridge:
    """A host port wired straight to a UART model's TX/RX pins (TTL level: a USB-TTL cable). Emulation thread only.

    Host -> chip: `pump()` drains the port; the bytes are the host's transmit buffer, sent one per host character
    time as frames in the host's format; each arrives at its stop bit through the UART's receive path
    (`_rx_frame(frame, "host")`: decoded at the UART's own settings; dropped when not listening / RX pin not muxed,
    overrun as on silicon). Chip -> host: a `frame_listeners` hook decodes each frame with the host's line settings
    when its stop bit has gone out and writes the result (module docstring)."""

    def __init__(self, chip: Any, blk: Any, port: HostPort, *, label: str):
        self.chip = chip
        self.blk = blk
        self.port = port
        self.label = label                  # "A.UART0"
        self.to_host = 0
        self.from_host = 0
        self.dropped = 0                    # kept for compatibility (no longer used: mismatches are decoded)
        self.errors = 0                     # characters the receiving side decoded with errors, both directions
        self._q: deque = deque()
        self._ev = None
        self._warned: set = set()
        self._closed = False
        fl = getattr(blk, "frame_listeners", None)
        self._hook = (fl, self._on_frame) if fl is not None else (blk.tx_listeners, self._on_tx)
        self._hook[0].append(self._hook[1])

    # ---- emulation thread ------------------------------------------------------------------------------------
    def pump(self) -> None:
        stream = self.port.read_stream()
        if not stream:
            return
        for item in stream:
            if isinstance(item, tuple):
                self._q.append(item)
            else:
                self._q.extend(item)
        if self._ev is None:
            self._ev = self.chip.sched.in_ns(self._item_ns(self._q[0]), self._tick)

    def _char_ns(self) -> float:
        ln = self.port.line()
        if ln is not None and ln.baud > 0:
            bits = ln.char_bits() if ln.framed else (self.blk.char_bits() if hasattr(self.blk, "char_bits") else 10)
            return float(bits) * 1e9 / ln.baud
        fn = getattr(self.blk, "_host_char_ns", None)
        return fn() if fn is not None else 10e9 / 115200.0

    def _item_ns(self, item: Any) -> float:
        if isinstance(item, tuple):
            return self._brk_bits(item[1]) * self._char_ns() / 10.0 + self._char_ns() / 10.0
        return self._char_ns()

    def _brk_bits(self, seconds: float) -> int:
        bit = self._char_ns() / 10.0
        if seconds and bit > 0:
            return max(int(seconds * 1e9 / bit), 1)
        try:
            return max(13, int(round(2 * float(self.blk.char_bits()))))
        except Exception:
            return 20

    def _mismatch(self) -> str | None:
        try:
            return framing_mismatch(float(self.blk.baud()), float(self.blk.char_bits()), self.port.line())
        except Exception:
            return None

    def _warn(self, direction: str, why: str) -> None:
        key = (direction, why)
        if key in self._warned:
            return
        self._warned.add(key)
        ln = self.port.line()
        self.chip.log.event("warn", self.chip.now_ns(), block=self.blk.name, uart=self.blk.name,
                            msg=f"host serial {self.label} ({self.port.kind} {self.port.describe()}): host line "
                                f"{ln} does not match the UART ({why}); the receiver decodes at its own settings: "
                                f"framing / parity errors and garbage {direction}")

    def _tick(self) -> None:
        self._ev = None
        if self._closed or not self._q:
            return
        item = self._q.popleft()
        why = self._mismatch()
        if why is not None:
            self._warn("host→chip", why)
        ln = self.port.line()
        if isinstance(item, tuple):
            from .periph.uartwire import Frame
            own = self.blk.rx_format() if hasattr(self.blk, "rx_format") else None
            baud = ln.baud if ln is not None and ln.baud > 0 else (own.baud if own is not None else 0.0)
            fr = Frame.brk_frame(self._brk_bits(item[1]), 1.0, baud)
        else:
            self.from_host += 1
            fr = host_frame_for(self.blk, ln, item)
        rf = getattr(self.blk, "_rx_frame", None)
        if rf is not None and fr is not None:
            rf(fr, "host")
        elif not isinstance(item, tuple):
            arrive = getattr(self.blk, "_rx_arrive", None)
            if arrive is not None:
                arrive(item, "host")
            else:
                self.blk.feed(bytes([item]))
        if self._q:
            self._ev = self.chip.sched.in_ns(self._item_ns(self._q[0]), self._tick)

    def _on_tx(self, blk: Any, byte: int) -> None:
        if self._closed:
            return
        t = self.chip.now_ns() + (blk.char_ns() if hasattr(blk, "char_ns") else 0.0)
        self.chip.sched.at_ns(t, self._deliver, byte & 0xFF)

    def _on_frame(self, blk: Any, fr: Any) -> None:
        if self._closed:
            return
        self.chip.sched.at_ns(self.chip.now_ns() + fr.duration_ns(), self._deliver_frame, fr)

    def _deliver(self, b: int) -> None:
        if self._closed:
            return
        self.to_host += 1
        self.port.write(bytes([b]))

    def _deliver_frame(self, fr: Any) -> None:
        if self._closed:
            return
        ln = self.port.line()
        why = self._mismatch()
        if why is not None:
            self._warn("chip→host", why)
        for b, ch in host_bytes_for(fr, host_rx_format(ln, fr)):
            if ch is not None and ch.flags():
                self.errors += 1
                self.chip.log.event("uart", self.chip.now_ns(), uart=f"host:{self.label}", dir="rx",
                                    byte=0 if b == "break" else b, **ch.flags())
            if b == "break":
                bit = 1e9 / ln.baud if ln is not None and ln.baud > 0 else 0.0
                self.port.send_break((ch.brk if ch is not None else fr.brk) * bit / 1e9 if bit else 0.0)
            else:
                self.to_host += 1
                self.port.write(bytes([b]))

    def pending(self) -> int:
        return len(self._q)

    def info(self) -> dict:
        d = {"name": self.label, "kind": "uart", **self.port.info(), "to_host": self.to_host,
             "from_host": self.from_host, "dropped": self.dropped, "errors": self.errors, "queued": len(self._q)}
        try:
            b = float(self.blk.baud())
            fmt = self.blk.frame() if hasattr(self.blk, "frame") else f"{self.blk.char_bits():g} bits/char"
            d["uart_line"] = f"{b:.0f} baud, {fmt}" if b > 0 else "no baud set"
        except Exception:
            pass
        return d

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        lst, cb = self._hook
        if cb in lst:
            lst.remove(cb)
        if self._ev is not None:
            self.chip.sched.cancel(self._ev)
            self._ev = None
        self.port.close()


def print_endpoints(session: Any, out: Any = None) -> None:
    """One line per host serial endpoint (what the user opens in the PC software)."""
    out = out or sys.stderr
    for ep in session.host_endpoints():
        d = ep.info()
        if d.get("transport") in (None, "memory"):
            continue
        print(f"host serial: {d['name']} <-> {d['transport']} {d['path']}", file=out)
    rt = getattr(session, "pacer", None)
    if rt is not None and session.host_endpoints():
        print(f"real-time: on (speed x{rt.speed:g}); Ctrl-C stops", file=out)
