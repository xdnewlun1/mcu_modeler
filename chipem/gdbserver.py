"""GDB remote serial protocol (RSP) server for one chip of a Session: `chipem run|shell|serve --gdb PORT`.

Clients: gdb / gdb-multiarch / arm-none-eabi-gdb (`target extended-remote :PORT` or `target remote :PORT`), and
therefore Ghidra's debugger (gdb connector), IDA's remote GDB debugger, VS Code cortex-debug ("external" server
type) and lldb's gdb-remote. One client at a time; a new connection is accepted after the previous one closes.

Threading (the rule of chipem: only the emulation thread touches the Session): the socket thread parses packets and
posts closures to an *executor* that runs them on the emulation thread:
  * `QueueExecutor` for `chipem run` / `chipem shell`: the main thread pumps the queue (`pump()` / `serve()`).
  * `WorkerExecutor` for `chipem serve`: closures go through the web Worker (`worker.call`), `c` starts the Worker's
    free run (so the web UI shows it running) and ends at its next stop.
`c` runs the whole board (all chips in lockstep, real-time pacing kept: Session.run_for paces itself) until a
debugger stop (breakpoint / watchpoint on any chip), a fault, or Ctrl-C (0x03 from the client: Session.stop()).

Packets: ?, g/G, p/P, m/M, X, c, s, C/S (signal ignored), vCont (c, s, t), vCont?, Z0-Z4 / z0-z4, qSupported,
qXfer:features:read (target.xml), qXfer:memory-map:read, qAttached, qC, qfThreadInfo/qsThreadInfo, H, T, k, D,
qRcmd (monitor: reset, halt, anything else = a chipem shell command, output as O packets), QStartNoAckMode,
qHostInfo / qProcessInfo (lldb), qOffsets, qSymbol, vMustReplyEmpty.

ARM (Cortex-M) target description: org.gnu.gdb.arm.m-profile (r0-r12, sp, lr, pc, xpsr), org.gnu.gdb.arm.m-system
(msp, psp, primask, basepri, faultmask, control; ARMv6-M: msp, psp, primask, control) and, with an FPU,
org.gnu.gdb.arm.vfp (d0-d15, fpscr). Memory reads never have side effects: MMIO registers are peeked (a register
whose read would pop a FIFO or clear a flag returns its stored value).

C28x: gdb has no C28x architecture. The server sends a custom target.xml (architecture "c28x", feature
org.chipem.c28x, all registers 32 bits) for generic RSP clients; addresses are 16-bit word addresses (as in TI's
tools), `m`/`M` lengths are bytes (two per word, little-endian words).
"""
from __future__ import annotations

import queue
import select
import socket
import struct
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable

from .core import StopReason

__all__ = ["GdbServer", "QueueExecutor", "WorkerExecutor", "target_xml"]

SIGINT, SIGTRAP, SIGSEGV = 2, 5, 11


# ------------------------------------------------------------------------------------------------- executors
class QueueExecutor:
    """Runs posted closures on the thread that calls pump()/serve() (the emulation thread)."""

    def __init__(self, session: Any):
        self.s = session
        self.q: queue.Queue = queue.Queue()
        self._interrupt = threading.Event()
        self._stopped = threading.Event()
        self._stopped.set()
        self.last_reason: Any = None
        self.closed = False

    def call(self, fn: Callable[[], Any], timeout: float | None = None) -> Any:
        fut: Future = Future()
        self.q.put((fn, fut))
        return fut.result(timeout)

    def pump(self, timeout: float = 0.0) -> bool:
        """Run queued closures (waiting up to `timeout` for the first). True if one ran."""
        try:
            fn, fut = self.q.get(timeout=timeout) if timeout else self.q.get_nowait()
        except queue.Empty:
            return False
        while True:
            if fut.set_running_or_notify_cancel():
                try:
                    fut.set_result(fn())
                except BaseException as e:      # noqa: BLE001 - reported to the caller
                    fut.set_exception(e)
            try:
                fn, fut = self.q.get_nowait()
            except queue.Empty:
                return True

    def serve(self, until: Callable[[], bool]) -> None:
        while not until():
            self.pump(0.1)

    def resume(self, kind: str, chip: str | None) -> None:
        """Start a continue (`kind` "c") on the emulation thread; returns at once (wait_stop waits)."""
        self._interrupt.clear()
        self._stopped.clear()

        def run():
            try:
                self.last_reason = self.s.debug.run_until_stop(should_stop=self._interrupt.is_set)
            except Exception as e:              # noqa: BLE001
                self.last_reason = e
            finally:
                self._stopped.set()
        self.q.put((run, Future()))

    def wait_stop(self, timeout: float) -> bool:
        return self._stopped.wait(timeout)

    def interrupt(self) -> None:
        self._interrupt.set()
        try:
            self.s.stop()
        except Exception:
            pass


class WorkerExecutor:
    """Executor over the web Worker (chipem serve): `c` = the Worker's free run, stopped at the next stop."""

    def __init__(self, worker: Any):
        self.w = worker
        self.last_reason: Any = None
        self.closed = False

    @property
    def s(self):
        return self.w.s

    def call(self, fn: Callable[[], Any], timeout: float | None = 30.0) -> Any:
        return self.w.call(fn, timeout=timeout or 30.0)

    def resume(self, kind: str, chip: str | None) -> None:
        self.last_reason = None
        self.w.cmd_run(None)

    def wait_stop(self, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while self.w.running:
            if time.monotonic() >= end:
                return False
            time.sleep(0.005)
        ls = self.w.last_stop
        self.last_reason = getattr(StopReason, ls, None) if isinstance(ls, str) else ls
        return True

    def interrupt(self) -> None:
        self.w.pause()


# ------------------------------------------------------------------------------------------------- target layout
class _Layout:
    """Register list for `g`/`p` in target.xml order: [(name, bits, core_name or None)]."""

    def __init__(self, chip: Any):
        self.c28x = getattr(chip, "arch_kind", "") == "c28x"
        regs: list[tuple[str, int, str | None, str]] = []      # (gdb name, bits, core reg, feature)
        if self.c28x:
            names = list(getattr(chip.arch, "reg_names", ()) or ())
            for n in names:
                regs.append((n, 32, n, "org.chipem.c28x"))
            if getattr(chip.core, "fpu", False):
                for i in range(8):
                    regs.append((f"r{i}h", 32, f"r{i}h", "org.chipem.c28x.fpu"))
                regs.append(("stf", 32, "stf", "org.chipem.c28x.fpu"))
        else:
            for i in range(13):
                regs.append((f"r{i}", 32, f"r{i}", "org.gnu.gdb.arm.m-profile"))
            regs += [("sp", 32, "sp", "org.gnu.gdb.arm.m-profile"), ("lr", 32, "lr", "org.gnu.gdb.arm.m-profile"),
                     ("pc", 32, "pc", "org.gnu.gdb.arm.m-profile"), ("xpsr", 32, "xpsr", "org.gnu.gdb.arm.m-profile")]
            sysregs = ["msp", "psp", "primask", "control"] if getattr(chip.arch, "v6m", False) else \
                ["msp", "psp", "primask", "basepri", "faultmask", "control"]
            for n in sysregs:
                regs.append((n, 32, n, "org.gnu.gdb.arm.m-system"))
            if getattr(chip.desc.core, "fpu", False):
                for i in range(16):
                    regs.append((f"d{i}", 64, f"d{i}", "org.gnu.gdb.arm.vfp"))
                regs.append(("fpscr", 32, "fpscr", "org.gnu.gdb.arm.vfp"))
        self.regs = regs
        self.pc_index = next(i for i, r in enumerate(regs) if r[0] == "pc")

    def read(self, core: Any, i: int) -> int:
        name, bits, cn, _ = self.regs[i]
        if cn.startswith("d") and cn[1:].isdigit():
            k = int(cn[1:])
            lo, hi = core.read_reg(f"s{2 * k}"), core.read_reg(f"s{2 * k + 1}")
            return lo | (hi << 32)
        if cn in ("msp", "psp") and not self.c28x:
            return core.read_reg(cn)
        return core.read_reg(cn) & ((1 << bits) - 1)

    def write(self, core: Any, i: int, v: int) -> None:
        name, bits, cn, _ = self.regs[i]
        if cn.startswith("d") and cn[1:].isdigit():
            k = int(cn[1:])
            core.write_reg(f"s{2 * k}", v & 0xFFFFFFFF)
            core.write_reg(f"s{2 * k + 1}", (v >> 32) & 0xFFFFFFFF)
            return
        if cn == "pc":
            core.pc = v
            core.bp_skip = None
            return
        core.write_reg(cn, v)


def target_xml(chip: Any) -> str:
    lay = _Layout(chip)
    if lay.c28x:
        arch = "c28x"
    else:
        arch = "arm"
    out = ['<?xml version="1.0"?>', '<!DOCTYPE target SYSTEM "gdb-target.dtd">', "<target>",
           f"  <architecture>{arch}</architecture>"]
    feat = None
    for i, (name, bits, _cn, f) in enumerate(lay.regs):
        if f != feat:
            if feat is not None:
                out.append("  </feature>")
            out.append(f'  <feature name="{f}">')
            feat = f
        typ = ""
        if name == "pc":
            typ = ' type="code_ptr"'
        elif name in ("sp", "msp", "psp"):
            typ = ' type="data_ptr"'
        elif name.startswith("d") and name[1:].isdigit():
            typ = ' type="ieee_double"'
        out.append(f'    <reg name="{name}" bitsize="{bits}" regnum="{i}"{typ}/>')
    if feat is not None:
        out.append("  </feature>")
    out.append("</target>")
    return "\n".join(out) + "\n"


def memory_map_xml(chip: Any) -> str:
    out = ['<?xml version="1.0"?>',
           '<!DOCTYPE memory-map PUBLIC "+//IDN gnu.org//DTD GDB Memory Map V1.0//EN" '
           '"http://sourceware.org/gdb/gdb-memory-map.dtd">', "<memory-map>"]
    spans: list[tuple[int, int, str]] = []
    for r in chip.bus.memory_regions():
        spans.append((r.base, r.end, "rom" if r.kind in ("flash", "rom", "flexnvm") else "ram"))
    # peripheral space (gdb refuses addresses outside the map): every MMIO block and the auto-stub windows, merged
    io = sorted([(b.base, b.base + b.size) for b in chip.peripherals.values()]
                + [tuple(w) for w in getattr(chip.bus, "catch_all", ()) or ()])
    merged: list[list[int]] = []
    for lo, hi in io:
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    for lo, hi in merged:
        if not any(a < hi and lo < b for a, b, _ in spans):
            spans.append((lo, hi, "ram"))
    for lo, hi, kind in sorted(spans):
        out.append(f'  <memory type="{kind}" start="0x{lo:x}" length="0x{hi - lo:x}"/>')
    out.append("</memory-map>")
    return "\n".join(out) + "\n"


def _escape(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b in (0x23, 0x24, 0x7D, 0x2A):
            out += bytes((0x7D, b ^ 0x20))
        else:
            out.append(b)
    return bytes(out)


def _unescape(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        if b == 0x7D and i + 1 < len(data):
            out.append(data[i + 1] ^ 0x20)
            i += 2
            continue
        out.append(b)
        i += 1
    return bytes(out)


# ------------------------------------------------------------------------------------------------- server
class GdbServer:
    """RSP server; `start()` listens on a background thread. `chip`: board chip id (default: the view chip)."""

    def __init__(self, executor: Any, port: int = 3333, *, host: str = "127.0.0.1", chip: str | None = None,
                 log: Callable[[str], None] | None = None, shell_factory: Callable[[], Any] | None = None):
        self.ex = executor
        self.host = host
        self.chip = chip
        self.log = log or (lambda m: None)
        self.shell_factory = shell_factory
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, int(port)))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.conn: socket.socket | None = None
        self.noack = False
        self.done = False                    # set when a client killed the target (`k`) or the server closed
        self.clients = 0
        self.killed = False
        self._thread: threading.Thread | None = None
        self._buf = b""
        self._layout: _Layout | None = None
        self._ids: dict[tuple, int] = {}     # (type, addr, kind) -> breakpoint id
        self._pending_int = False
        self._end = False

    # ---- lifecycle -------------------------------------------------------------------------------------------
    def start(self) -> "GdbServer":
        self._thread = threading.Thread(target=self._accept_loop, name="chipem-gdb", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        self.done = True
        for s in (self.conn, self.sock):
            try:
                if s is not None:
                    s.close()
            except OSError:
                pass

    def _accept_loop(self) -> None:
        while not self.done:
            try:
                conn, addr = self.sock.accept()
            except OSError:
                break
            self.conn = conn
            self.clients += 1
            self.log(f"gdb: client connected from {addr[0]}:{addr[1]}")
            self.noack = False
            self._buf = b""
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._session_loop(conn)
            except (OSError, ConnectionError):
                pass
            except Exception as e:           # noqa: BLE001 - keep serving
                self.log(f"gdb: error: {type(e).__name__}: {e}")
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
                self._cleanup()                  # before conn = None: `chipem run --gdb` stops pumping after that
                self.conn = None
                self.log("gdb: client disconnected")

    def _cleanup(self) -> None:
        """Client gone: remove the breakpoints it set (the emulation stays where it is)."""
        ids = list(self._ids.values())
        self._ids.clear()
        if ids:
            def rm():
                for i in ids:
                    try:
                        self.ex.s.debug.delete(i)
                    except Exception:
                        pass
            try:
                self.ex.call(rm, 10)
            except Exception:
                pass

    # ---- packet I/O --------------------------------------------------------------------------------------------
    def _recv(self, conn: socket.socket, timeout: float | None = None) -> bytes:
        if timeout is not None:
            r, _, _ = select.select([conn], [], [], timeout)
            if not r:
                return b""
        data = conn.recv(65536)
        if not data:
            raise ConnectionError("closed")
        return data

    def _read_packet(self, conn: socket.socket) -> bytes | None:
        """Next packet payload; None for a Ctrl-C (0x03) seen outside a packet."""
        while True:
            buf = self._buf
            i = 0
            while i < len(buf) and buf[i] in (0x2B, 0x2D):       # '+' / '-' acks
                i += 1
            if i < len(buf) and buf[i] == 0x03:
                self._buf = buf[i + 1:]
                return None
            j = buf.find(b"$", i)
            if j >= 0:
                k = buf.find(b"#", j)
                if k >= 0 and len(buf) >= k + 3:
                    payload = buf[j + 1:k]
                    self._buf = buf[k + 3:]
                    if not self.noack:
                        conn.sendall(b"+")
                    return payload
                self._buf = buf[j:]
            else:
                self._buf = b""
            self._buf += self._recv(conn)

    def _send(self, conn: socket.socket, payload: str | bytes) -> None:
        if isinstance(payload, str):
            payload = payload.encode("latin-1")
        cs = sum(payload) & 0xFF
        conn.sendall(b"$" + payload + b"#" + f"{cs:02x}".encode())
        if not self.noack:
            # wait for the ack ('+', '-' = resend); a client that sends something else meanwhile is not waited for
            end = time.monotonic() + 2.0
            while time.monotonic() < end:
                head = self._buf[:1]
                if head == b"+":
                    self._buf = self._buf[1:]
                    return
                if head == b"-":
                    self._buf = self._buf[1:]
                    conn.sendall(b"$" + payload + b"#" + f"{cs:02x}".encode())
                    continue
                if head:
                    return
                self._buf += self._recv(conn, 0.5)

    # ---- the protocol ------------------------------------------------------------------------------------------
    def _session_loop(self, conn: socket.socket) -> None:
        self._layout = self.ex.call(lambda: _Layout(self._chip()), 30)
        self._end = False
        while not self.done and not self._end:
            pkt = self._read_packet(conn)
            if pkt is None:                                # Ctrl-C while halted: report a stop
                self._send(conn, self._stop_reply(SIGINT))
                continue
            reply = self._handle(conn, pkt)
            if reply is not None:
                self._send(conn, reply)

    def _chip(self):
        s = self.ex.s
        return s.ctxs[s.debug.chip_id(self.chip)].chip

    def _dbg(self):
        return self.ex.s.debug.dbg(self.chip)

    def _call(self, fn: Callable[[], Any]) -> Any:
        return self.ex.call(fn, 30)

    def _stop_reply(self, sig: int | None = None) -> str:
        def fn():
            dbg = self._dbg()
            last = self.ex.s.debug.last_stop or {}
            out = ""
            s = sig
            if s is None:
                reason = last.get("reason")
                s = SIGTRAP
                if reason == "FAULT":
                    s = SIGSEGV
                elif reason in ("USER", "PAUSED"):
                    s = SIGINT
            out = f"T{s:02x}"
            if last.get("type") == "watchpoint" and sig is None and last.get("chip") == dbg.cid:
                key = {"r": "rwatch", "w": "watch"}.get(last.get("access"), "awatch")
                # the gdb breakpoint kind decides the key (an access watchpoint reports awatch)
                for (t, a, _k), _i in self._ids.items():
                    if t == "4" and a <= (last.get("addr") or 0) < a + max(_k, 1):
                        key = "awatch"
                out += f"{key}:{last.get('addr', 0):x};"
            elif last.get("type") == "breakpoint" and sig is None:
                hw = any(t == "1" and a == last.get("addr") for (t, a, _k) in self._ids)
                out += "hwbreak:;" if hw else "swbreak:;"
            lay = self._layout
            pc = lay.read(dbg.core, lay.pc_index)
            out += f"{lay.pc_index:02x}:{self._hex_le(pc, 32)};thread:1;"
            return out
        return self._call(fn)

    @staticmethod
    def _hex_le(v: int, bits: int) -> str:
        return (v & ((1 << bits) - 1)).to_bytes(bits // 8, "little").hex()

    def _handle(self, conn: socket.socket, pkt: bytes) -> str | bytes | None:
        p = pkt.decode("latin-1")
        if not p:
            return ""
        c = p[0]
        try:
            if p.startswith("qSupported"):
                return ("PacketSize=4000;qXfer:features:read+;qXfer:memory-map:read+;QStartNoAckMode+;"
                        "swbreak+;hwbreak+;vContSupported+")
            if p == "QStartNoAckMode":
                self._send(conn, "OK")
                self.noack = True
                return None
            if c == "?":
                return self._stop_reply()
            if p.startswith("qXfer:features:read:"):
                name, rng = p[len("qXfer:features:read:"):].split(":", 1)
                if name != "target.xml":
                    return "E00"
                return self._xfer(self._call(lambda: target_xml(self._chip())), rng)
            if p.startswith("qXfer:memory-map:read:"):
                rng = p.split(":", 4)[-1]
                return self._xfer(self._call(lambda: memory_map_xml(self._chip())), rng)
            if p == "qAttached" or p.startswith("qAttached:"):
                return "1"
            if p == "qC":
                return "QC1"
            if p == "qfThreadInfo":
                return "m1"
            if p == "qsThreadInfo":
                return "l"
            if p.startswith("qThreadExtraInfo"):
                return self._call(lambda: f"chip {self._dbg().cid}".encode().hex())
            if c == "H" or c == "T":
                return "OK"
            if p == "qOffsets":
                return "Text=0;Data=0;Bss=0"
            if p.startswith("qSymbol"):
                return "OK"
            if p == "qHostInfo":
                lay = self._layout
                triple = ("c28x-unknown-none" if lay.c28x else "thumbv7m-none-eabi").encode().hex()
                return f"triple:{triple};endian:little;ptrsize:4;"
            if p == "qProcessInfo":
                return "pid:1;endian:little;ptrsize:4;"
            if p.startswith("qRcmd,"):
                return self._monitor(conn, bytes.fromhex(p[6:]).decode("latin-1", "replace"))
            if c == "g":
                return self._call(self._read_all)
            if c == "G":
                data = bytes.fromhex(p[1:])
                self._call(lambda: self._write_all(data))
                return "OK"
            if c == "p":
                i = int(p[1:], 16)
                lay = self._layout
                if not 0 <= i < len(lay.regs):
                    return "E00"
                return self._call(lambda: self._hex_le(lay.read(self._dbg().core, i), lay.regs[i][1]))
            if c == "P":
                k, v = p[1:].split("=", 1)
                i = int(k, 16)
                lay = self._layout
                if not 0 <= i < len(lay.regs):
                    return "E00"
                val = int.from_bytes(bytes.fromhex(v), "little")
                self._call(lambda: lay.write(self._dbg().core, i, val))
                return "OK"
            if c == "m":
                a, n = (int(x, 16) for x in p[1:].split(","))
                return self._call(lambda: self._read_mem(a, n))
            if c == "M":
                head, data = p[1:].split(":", 1)
                a, n = (int(x, 16) for x in head.split(","))
                raw = bytes.fromhex(data)[:n]
                return self._call(lambda: self._write_mem(a, raw))
            if c == "X":
                i = pkt.index(b":")
                a, n = (int(x, 16) for x in pkt[1:i].decode().split(","))
                raw = _unescape(pkt[i + 1:])[:n]
                if n == 0:
                    return "OK"
                return self._call(lambda: self._write_mem(a, raw))
            if c in "Zz" and len(p) > 1 and p[1] in "01234":
                return self._call(lambda: self._breakpoint(p))
            if p == "vCont?":
                return "vCont;c;C;s;S;t"
            if p.startswith("vCont;"):
                acts = p[6:].split(";")
                a0 = acts[0].split(":")[0]
                if a0 in ("s", "S") or any(x.split(":")[0] in ("s", "S") and (":" not in x or x.endswith(":1"))
                                           for x in acts):
                    return self._step(None)
                if a0 == "t":
                    return self._stop_reply(SIGINT)
                return self._continue(conn, None)
            if c in "cC":
                arg = p[1:].split(";")
                addr = arg[1] if c == "C" and len(arg) > 1 else (arg[0] if c == "c" and arg[0] else None)
                return self._continue(conn, int(addr, 16) if addr else None)
            if c in "sS":
                arg = p[1:].split(";")
                addr = arg[1] if c == "S" and len(arg) > 1 else (arg[0] if c == "s" and arg[0] else None)
                return self._step(int(addr, 16) if addr else None)
            if c == "k" or p.startswith("vKill"):
                self.killed = True                     # `chipem run --gdb` ends; serve / shell keep listening
                self._end = True
                return "OK" if p.startswith("vKill") else None
            if c == "D":
                self._send(conn, "OK")
                self._end = True                       # end this client (the server keeps listening)
                return None
            if p == "!":
                return "OK"
            if p.startswith("vMustReplyEmpty"):
                return ""
        except Exception as e:                          # noqa: BLE001 - protocol errors are E replies
            self.log(f"gdb: {p[:40]!r}: {type(e).__name__}: {e}")
            return "E01"
        return ""                                       # unsupported packet

    def _xfer(self, text: str, rng: str) -> bytes:
        off, ln = (int(x, 16) for x in rng.split(","))
        data = text.encode()
        chunk = data[off:off + ln]
        return (b"l" if off + ln >= len(data) else b"m") + _escape(chunk)

    # ---- emulation-thread helpers ------------------------------------------------------------------------------
    def _read_all(self) -> str:
        core = self._dbg().core
        lay = self._layout
        return "".join(self._hex_le(lay.read(core, i), r[1]) for i, r in enumerate(lay.regs))

    def _write_all(self, data: bytes) -> None:
        core = self._dbg().core
        lay = self._layout
        pos = 0
        for i, r in enumerate(lay.regs):
            n = r[1] // 8
            if pos + n > len(data):
                break
            lay.write(core, i, int.from_bytes(data[pos:pos + n], "little"))
            pos += n

    def _read_mem(self, a: int, n: int) -> str:
        d = self._dbg()
        units = (n + d.ub - 1) // d.ub
        try:
            data, _ = d.read_memory(a, units)
        except Exception:
            return "E14"
        if not data:
            return "E14"
        return data[:n].hex()

    def _write_mem(self, a: int, raw: bytes) -> str:
        d = self._dbg()
        if d.ub == 2 and len(raw) % 2:
            raw = raw + d.read_memory(a + len(raw) // 2, 1)[0][1:2]
        try:
            d.write_memory(a, raw)
        except Exception:
            return "E14"
        return "OK"

    def _breakpoint(self, p: str) -> str:
        t = p[1]
        a_s, k_s = p[3:].split(";")[0].split(",")[:2]
        a, kind = int(a_s, 16), int(k_s, 16)
        key = (t, a, kind)
        D = self.ex.s.debug
        cid = D.chip_id(self.chip)
        if p[0] == "Z":
            if key in self._ids:
                return "OK"
            if t in "01":
                bp = D.dbg(cid).add_break("", addr=a)[0]
                bp.location = f"gdb Z{t} 0x{a:x}"
            else:
                d = D.dbg(cid)
                size = max(1, (kind + d.ub - 1) // d.ub) if d.ub > 1 else max(1, kind)
                bp = d.add_watch(f"0x{a:x}", {"2": "w", "3": "r", "4": "rw"}[t], size=size)
                bp.location = f"gdb Z{t} 0x{a:x},{kind}"
            self._ids[key] = bp.id
            return "OK"
        bid = self._ids.pop(key, None)
        if bid is not None:
            try:
                D.delete(bid)
            except Exception:
                pass
        return "OK"

    def _step(self, addr: int | None) -> str:
        def fn():
            d = self._dbg()
            if addr is not None:
                d.core.pc = addr
            r = self.ex.s.step(d.cid)
            return r
        r = self._call(fn)
        if r is StopReason.FAULT:
            return self._stop_reply(SIGSEGV)
        return self._stop_reply(SIGTRAP)

    def _continue(self, conn: socket.socket, addr: int | None) -> str:
        if addr is not None:
            self._call(lambda: setattr(self._dbg().core, "pc", addr))
        self.ex.resume("c", self.chip)
        interrupted = False
        while not self.ex.wait_stop(0.02):
            try:
                data = self._recv(conn, 0.0)
            except ConnectionError:
                self.ex.interrupt()
                self.ex.wait_stop(5.0)
                raise
            if data:
                if b"\x03" in data:
                    interrupted = True
                    self.ex.interrupt()
                    data = data.replace(b"\x03", b"")
                self._buf += data
        r = self.ex.last_reason
        if isinstance(r, Exception):
            self.log(f"gdb: run failed: {r}")
            return self._stop_reply(SIGSEGV)
        if interrupted or r is StopReason.USER:
            return self._stop_reply(SIGINT)
        if r is StopReason.FAULT:
            return self._stop_reply(SIGSEGV)
        return self._stop_reply()

    def _monitor(self, conn: socket.socket, cmd: str) -> str:
        cmd = cmd.strip()
        words = cmd.split()
        if not words:
            return "OK"
        if words[0] in ("reset", "system_reset"):
            out = self._call(lambda: (self.ex.s.reset(), f"reset: pc=0x{self._dbg().core.pc:X}\n")[1])
        elif words[0] in ("halt", "stop"):
            out = "halted\n"
        else:
            def run_shell():
                import io
                from .cmd_shell import Shell
                buf = io.StringIO()
                sh = Shell(self.ex.s, stdout=buf)
                try:
                    sh.onecmd(cmd)
                finally:
                    sh._set_trace(False)
                return buf.getvalue()
            out = self._call(run_shell)
        for i in range(0, len(out), 512):
            self._send(conn, "O" + out[i:i + 512].encode().hex())
        return "OK"


def start_for_session(session: Any, port: int, *, chip: str | None = None, host: str = "127.0.0.1",
                      log: Callable[[str], None] | None = None) -> tuple[GdbServer, QueueExecutor]:
    """`chipem run/shell --gdb`: a QueueExecutor (pump it on the emulation thread) and the listening server."""
    ex = QueueExecutor(session)
    srv = GdbServer(ex, port, host=host, chip=chip, log=log).start()
    return srv, ex
