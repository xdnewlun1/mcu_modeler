"""GDB remote serial protocol server (chipem/gdbserver.py): a small Python RSP client against blinky (ARM) and the
C28x sample, plus an end-to-end run of the system gdb when it supports ARM (skipped otherwise)."""
from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from chipem.gdbserver import GdbServer, QueueExecutor, WorkerExecutor
from chipem.session import Session

ROOT = Path(__file__).resolve().parent.parent
BLINKY = ROOT / "firmware" / "samples" / "build" / "blinky.elf"
C28_BLINKY = ROOT / "firmware" / "samples_c2000" / "build" / "blinky.c28img"
pytestmark = pytest.mark.skipif(not BLINKY.exists(), reason="sample firmware not built")


class Rsp:
    """Minimal RSP client (ack mode, then no-ack after QStartNoAckMode)."""

    def __init__(self, port: int):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=20)
        self.buf = b""
        self.ack = True

    def _read(self) -> bytes:
        while True:
            i = self.buf.find(b"$")
            if i >= 0:
                j = self.buf.find(b"#", i)
                if j >= 0 and len(self.buf) >= j + 3:
                    p = self.buf[i + 1:j]
                    assert int(self.buf[j + 1:j + 3], 16) == sum(p) & 0xFF
                    self.buf = self.buf[j + 3:]
                    if self.ack:
                        self.s.sendall(b"+")
                    return p
            self.buf += self.s.recv(65536)

    def cmd(self, payload: str | bytes, *, wait=True) -> bytes:
        if isinstance(payload, str):
            payload = payload.encode()
        self.s.sendall(b"$" + payload + b"#" + f"{sum(payload) & 0xFF:02x}".encode())
        return self._read() if wait else b""

    def close(self):
        self.s.close()


@pytest.fixture
def served():
    """(session, server, client) with the executor pumped on a background 'emulation' thread."""
    started = []

    def make(fw=BLINKY, chip="mk22fn1m0vll12", **kw):
        s = Session(chip, fw, quiet=True, **kw)
        ex = QueueExecutor(s)
        srv = GdbServer(ex, 0).start()
        stop = threading.Event()
        t = threading.Thread(target=ex.serve, args=(stop.is_set,), daemon=True)
        t.start()
        c = Rsp(srv.port)
        started.append((srv, stop, t, c))
        return s, srv, c
    yield make
    for srv, stop, t, c in started:
        c.close()
        srv.close()
        stop.set()
        t.join(5)


def le32(v: int) -> str:
    return v.to_bytes(4, "little").hex()


def test_handshake_registers_memory(served):
    s, srv, c = served(clock_hz=120e6)
    feat = c.cmd("qSupported:multiprocess+;swbreak+;hwbreak+")
    assert b"qXfer:features:read+" in feat and b"PacketSize" in feat
    assert c.cmd("QStartNoAckMode") == b"OK"
    c.ack = False
    xml = c.cmd("qXfer:features:read:target.xml:0,ffff")
    assert xml[:1] == b"l" and b"org.gnu.gdb.arm.m-profile" in xml and b"org.gnu.gdb.arm.m-system" in xml
    assert b"org.gnu.gdb.arm.vfp" in xml                         # K22: Cortex-M4F
    mm = c.cmd("qXfer:memory-map:read::0,ffff")
    assert b'type="rom"' in mm and b'type="ram"' in mm
    assert c.cmd("qAttached") == b"1" and c.cmd("qC") == b"QC1"
    assert c.cmd("?").startswith(b"T05")
    g = c.cmd("g").decode()
    # 16 core + xpsr + 6 system + 16 d + fpscr: 23 * 4 + 16 * 8 + 4 bytes
    assert len(g) == 2 * (23 * 4 + 16 * 8 + 4)
    pc = int.from_bytes(bytes.fromhex(c.cmd("pf").decode()), "little")
    assert pc == s.chip.core.pc == 0x414                       # Reset_Handler
    assert c.cmd("m0,8").decode() == s.chip.core.read_mem(0, 8).hex()
    assert c.cmd("M20000000,4:efbeadde") == b"OK"
    assert c.cmd("m20000000,4") == b"efbeadde"
    assert c.cmd("X20000004,2:" + "\x11\x22") == b"OK"
    assert c.cmd("m20000004,2") == b"1122"
    assert c.cmd("P5=" + le32(0x1234)) == b"OK" and s.chip.core.read_reg("r5") == 0x1234
    assert c.cmd("m2f000000,4").startswith(b"E")                # unmapped


def test_breakpoint_continue_step_watch(served):
    s, srv, c = served(clock_hz=120e6)
    d = s.debug.dbg()
    main = d.symbol_addr("main")
    assert c.cmd(f"Z0,{main:x},2") == b"OK"
    r = c.cmd("vCont;c")
    assert r.startswith(b"T05") and b"swbreak:" in r and f"0f:{le32(main)}".encode() in r
    assert c.cmd(f"z0,{main:x},2") == b"OK" and not s.debug.breakpoints()
    r = c.cmd("s")
    assert r.startswith(b"T05") and s.chip.core.pc == main + 2
    gpioc = s.chip.peripherals["GPIOC"]
    ptor = gpioc.base + gpioc.regs["PTOR"].offset
    assert c.cmd(f"Z2,{ptor:x},4") == b"OK"
    r = c.cmd("c")
    assert r.startswith(b"T05") and f"watch:{ptor:x};".encode() in r
    assert c.cmd(f"z2,{ptor:x},4") == b"OK"
    # Ctrl-C while running (nothing stops blinky)
    c.s.sendall(b"$c#63")
    time.sleep(0.3)
    c.s.sendall(b"\x03")
    r = c._read()
    assert r.startswith(b"T02")


def test_monitor_commands(served):
    s, srv, c = served(clock_hz=120e6)
    s.debug.add_break("main")
    s.debug.run_until_stop(1e6)
    c.s.sendall(b"$" + b"qRcmd," + "regs".encode().hex().encode() + b"#" +
                f"{sum(b'qRcmd,' + 'regs'.encode().hex().encode()) & 0xFF:02x}".encode())
    out = b""
    while True:
        p = c._read()
        if p == b"OK":
            break
        assert p.startswith(b"O")
        out += bytes.fromhex(p[1:].decode())
    assert b"r15/pc=" in out
    p = "qRcmd," + "reset".encode().hex()
    c.s.sendall(b"$" + p.encode() + b"#" + f"{sum(p.encode()) & 0xFF:02x}".encode())
    msg = c._read()
    assert msg.startswith(b"O") and b"reset" in bytes.fromhex(msg[1:].decode())
    assert c._read() == b"OK" and s.chip.core.pc == 0x414


@pytest.mark.skipif(not C28_BLINKY.exists(), reason="C28x sample not built")
def test_c28x_custom_target(served):
    s, srv, c = served(C28_BLINKY, "tms320f28232")
    xml = c.cmd("qXfer:features:read:target.xml:0,ffff")
    assert b"<architecture>c28x</architecture>" in xml and b"org.chipem.c28x" in xml
    regs = s.chip.arch.reg_names
    assert len(c.cmd("g")) == 8 * len(regs)                   # every register 32 bits
    loop = s.image.symbols["loop"]
    assert c.cmd(f"Z0,{loop:x},1") == b"OK"
    r = c.cmd("c")
    assert r.startswith(b"T05") and s.chip.core.pc == loop
    # word addresses: 2 bytes per word, little-endian words
    assert c.cmd(f"m{loop:x},4").decode() == s.chip.core.read_mem(loop, 2).hex()


def test_worker_executor(served):
    """chipem serve: requests go through the web Worker; `c` is the Worker's free run."""
    from chipem.web.server import WebApp
    s = Session("mk22fn1m0vll12", BLINKY, clock_hz=120e6, quiet=True)
    app = WebApp(s, "127.0.0.1", 0).start()
    srv = GdbServer(WorkerExecutor(app.worker), 0).start()
    c = Rsp(srv.port)
    try:
        main = app.worker.call(lambda: s.debug.dbg().symbol_addr("main"))
        assert c.cmd(f"Z0,{main:x},2") == b"OK"
        r = c.cmd("c")
        assert r.startswith(b"T05") and app.worker.last_stop == "DEBUG"
        assert not app.worker.running
        assert c.cmd("s").startswith(b"T05")
    finally:
        c.close()
        srv.close()
        app.stop()
        s.close()


def _gdb_has_arm() -> bool:
    gdb = shutil.which("gdb-multiarch") or shutil.which("arm-none-eabi-gdb") or shutil.which("gdb")
    if gdb is None:
        return False
    try:
        r = subprocess.run([gdb, "-batch", "-nx", "-ex", "set architecture arm"], capture_output=True, text=True,
                           timeout=30)
    except Exception:
        return False
    return r.returncode == 0 and "is set to \"arm\"" in r.stdout


@pytest.mark.skipif(not _gdb_has_arm(), reason="no gdb with ARM support")
def test_gdb_end_to_end():
    gdb = shutil.which("gdb-multiarch") or shutil.which("arm-none-eabi-gdb") or shutil.which("gdb")
    s = Session("mk22fn1m0vll12", BLINKY, clock_hz=120e6, quiet=True)
    ex = QueueExecutor(s)
    srv = GdbServer(ex, 0).start()
    cmds = ["set pagination off", "set confirm off", f"target extended-remote :{srv.port}", "break main", "continue",
            "next", "info registers pc", "bt", "watch counter", "continue", "continue", "print counter",
            "monitor state", "delete", "break WDOG_Disable", "monitor reset", "continue", "finish", "detach"]
    args = [gdb, "-batch", "-nx", str(BLINKY)] + sum((["-ex", x] for x in cmds), [])
    out = {}

    def run():
        out["p"] = subprocess.run(args, capture_output=True, text=True, timeout=90)
    t = threading.Thread(target=run)
    t.start()
    ex.serve(lambda: not t.is_alive())
    srv.close()
    so = out["p"].stdout + out["p"].stderr
    assert "Breakpoint 1, main () at" in so, so
    assert "7\t    SIM_SCGC5" in so                              # next
    assert "Old value = 0" in so and "New value = 1" in so      # watch counter (stack local)
    assert "$1 = 2" in so
    assert "WDOG_Disable () at" in so and "0x000004ba in main () at blinky.c:6" in so   # finish returned
    assert "detached" in so
