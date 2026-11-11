"""Host serial bridge (chipem/hostserial.py) and real-time pacing (chipem/realtime.py): PTY, TCP and pyserial
transports against the uart_console sample, wire rules (baud pacing, dropped when not listening, framing mismatch),
reconnects, real-time tolerance and idle CPU, CLI options. Hermetic: PTYs and loopback sockets only."""
from __future__ import annotations

import os
import select
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from chipem.hostserial import (LineSettings, MemoryPort, framing_mismatch, open_port, parse_bridge_arg,
                               parse_format)
from chipem.session import Session

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "firmware" / "samples" / "build" / "uart_console.elf"
BLINKY = ROOT / "firmware" / "samples" / "build" / "blinky.elf"
need_fw = pytest.mark.skipif(not FW.exists(), reason="uart_console sample not built")
need_pty = pytest.mark.skipif(not hasattr(os, "openpty") or sys.platform == "win32", reason="no PTY on this platform")
BANNER = b"\r\nCHIP_EM K22 console - type help\r\n> "


def console(**kw) -> Session:
    return Session("mk22fn1m0vll12", FW, clock_hz=120e6, **kw)


class Runner:
    """Runs the session on its own thread (the only one touching it), like the web worker."""

    def __init__(self, s: Session, ns: float):
        self.s, self.ns, self.reason = s, ns, None
        self.t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        self.reason = self.s.run_for(self.ns)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *exc):
        self.s.stop()
        self.t.join(10)


def host_open(path: str, baud: int = 115200) -> int:
    import termios
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    a = termios.tcgetattr(fd)
    a[0] = a[1] = a[3] = 0                              # raw
    a[4] = a[5] = getattr(termios, f"B{baud}")
    termios.tcsetattr(fd, termios.TCSANOW, a)
    return fd


def read_until(fd: int, want: bytes, timeout: float = 5.0) -> bytes:
    buf = bytearray()
    end = time.monotonic() + timeout
    while want not in buf and time.monotonic() < end:
        r, _, _ = select.select([fd], [], [], 0.02)
        if r:
            try:
                buf += os.read(fd, 4096)
            except BlockingIOError:
                pass
    return bytes(buf)


def wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return bool(cond())


# ------------------------------------------------------------------------------------------------ helpers
def test_parse_helpers():
    assert parse_format("8N1") == (8, "N", 1.0) and parse_format("7e2") == (7, "E", 2.0)
    with pytest.raises(ValueError):
        parse_format("9X1")
    assert parse_bridge_arg("pty", "UART0") == ("UART0", "pty")
    assert parse_bridge_arg("pty", "B.usart1=/tmp/x") == ("B.USART1", "pty:/tmp/x")
    assert parse_bridge_arg("serial", "UART0=/dev/ttyUSB0:115200:8N1") == ("UART0", "serial:/dev/ttyUSB0:115200:8N1")
    assert parse_bridge_arg("tcp", "UART0=0.0.0.0:5555") == ("UART0", "tcp:0.0.0.0:5555")
    with pytest.raises(ValueError):
        parse_bridge_arg("tcp", "UART0=abc")
    assert LineSettings(115200).char_bits() == 10 and LineSettings(9600, 7, "E", 2).char_bits() == 11
    assert framing_mismatch(115200, 10, LineSettings(115200)) is None
    assert framing_mismatch(115200, 10, LineSettings(113000)) is None              # within 3 %
    assert "baud" in framing_mismatch(115200, 10, LineSettings(9600))
    assert "frame" in framing_mismatch(115200, 11, LineSettings(115200))
    assert framing_mismatch(115200, 11, LineSettings(115200, framed=False)) is None  # pty: frame unknown
    assert open_port("none") is None


@need_fw
def test_memory_bridge_round_trip_and_framing_errors():
    s = console()
    br = s.attach_host_serial("UART0", "memory")
    port: MemoryPort = br.port
    try:
        s.run_for(5e6)
        assert port.host_recv(1.0) == BANNER
        port.host_send(b"echo ab\r")
        s.run_for(5e6)
        assert port.host_recv(1.0) == b"echo ab\r\nab\r\n> "
        port.set_line(LineSettings(9600))
        port.host_send(b"xyz")
        s.run_for(10e6)
        assert port.host_recv(0.2) == b""
        w = [e.fields["msg"] for e in s.events(kinds={"warn"})]
        assert len(w) == 1 and "9600" in w[0] and "framing" in w[0]
        # a 9600 baud character seen by a 115200 baud receiver: its low bits are breaks (0x00 with FE), several
        # per character, never the typed text
        rx = [e.fields for e in s.events(kinds={"uart"}) if e.fields.get("dir") == "rx" and e.t_ns > 10.2e6]
        assert len(rx) >= 3 and all(f["fe"] and f["byte"] == 0 and f["brk"] >= 12 for f in rx)
    finally:
        s.close()


@pytest.mark.skipif(not BLINKY.exists(), reason="blinky sample not built")
def test_dropped_when_receiver_not_listening():
    s = Session("mk22fn1m0vll12", BLINKY)                 # never enables UART0
    br = s.attach_host_serial("UART0", "memory")
    try:
        br.port.host_send(b"abc")
        s.run_for(3e6)
        drops = [e.fields["reason"] for e in s.events(kinds={"uart"}) if e.fields.get("dir") == "drop"]
        assert len(drops) == 3 and set(drops) <= {"clock_gated", "rx_disabled", "pin_not_muxed"}
    finally:
        s.close()


@need_fw
def test_host_bytes_paced_at_host_baud():
    s = console()
    br = s.attach_host_serial("UART0", "memory")
    try:
        s.run_for(5e6)
        t0 = s.chip.clock.now_ns()
        br.port.host_send(b"0123456789")
        s.run_for(3e6)
        rx = [e.t_ns for e in s.events(kinds={"uart"}) if e.fields.get("dir") == "rx" and e.t_ns >= t0]
        assert len(rx) == 10
        char = 10e9 / 115200
        assert all(abs((b - a) - char) < 0.05 * char for a, b in zip(rx, rx[1:]))
        assert rx[0] - t0 == pytest.approx(char, rel=0.1)     # first byte at its stop bit, not at once
    finally:
        s.close()


# ------------------------------------------------------------------------------------------------ PTY
@need_fw
@need_pty
def test_pty_round_trip_reconnect_and_link(tmp_path):
    link = tmp_path / "ttyK22"
    s = console()
    br = s.attach_host_serial("UART0", f"pty:{link}")
    s.set_realtime(True)
    port = br.port
    assert link.is_symlink() and os.readlink(link) == port.path
    try:
        with Runner(s, float("inf")):
            fd = host_open(str(link))
            try:
                assert wait(lambda: port.connected)
                os.write(fd, b"echo one\r")
                got = read_until(fd, b"one\r\n> ")
                assert b"echo one\r\none\r\n> " in got
            finally:
                os.close(fd)
            assert wait(lambda: not port.connected)
            # the chip talks while nobody has the port open: lost, like a PC driver with the port closed
            time.sleep(0.05)
            fd = host_open(port.path)
            try:
                assert wait(lambda: port.connected)
                t0 = time.monotonic()
                os.write(fd, b"echo two\r")
                got = read_until(fd, b"two\r\n> ")
                assert b"two\r\n> " in got
                assert time.monotonic() - t0 < 2.0
            finally:
                os.close(fd)
        assert port.connects >= 2 and port.bytes_in >= 18
    finally:
        s.close()
    assert not link.exists()                                  # symlink removed on close


@need_fw
@need_pty
def test_pty_baud_mismatch_warns_and_gives_framing_errors():
    s = console()
    br = s.attach_host_serial("UART0", "pty")
    try:
        with Runner(s, float("inf")):
            fd = host_open(br.port.path, 9600)
            try:
                assert wait(lambda: br.port.connected)
                assert wait(lambda: br.port.line() is not None and br.port.line().baud == 9600)
                os.write(fd, b"help\r")
                assert wait(lambda: br.blk.rx_errors >= 5)
                time.sleep(0.05)
                assert b"help" not in read_until(fd, b"help", 0.2)
            finally:
                os.close(fd)
        msgs = [e.fields["msg"] for e in s.events(kinds={"warn"})]
        assert any("9600" in m and "framing" in m for m in msgs)
    finally:
        s.close()


@need_fw
@need_pty
def test_realtime_tolerance_idle_cpu_and_latency():
    s = console()
    br = s.attach_host_serial("UART0", "pty")
    s.set_realtime(True)
    try:
        s.run_for(10e6)                                       # banner out, console asleep in WFI
        time.sleep(0.1)                                       # a pause: the next run starts a new anchor
        w0, c0 = time.monotonic(), time.process_time()
        s.run_for(300e6)
        wall, cpu = time.monotonic() - w0, time.process_time() - c0
        assert 0.28 <= wall <= 0.9, wall
        assert cpu < 0.5 * wall, (cpu, wall)                  # idle firmware: the pacer sleeps
        assert s.pacer.rebases == 0
        # host bytes are taken within a few ms of wall time while the chip sleeps
        with Runner(s, float("inf")):
            fd = host_open(br.port.path)
            try:
                assert wait(lambda: br.port.connected)
                time.sleep(0.1)
                t0 = time.monotonic()
                os.write(fd, b"x")
                got = read_until(fd, b"x", 2.0)
                lat = time.monotonic() - t0
                assert got == b"x" and lat < 0.05, lat
            finally:
                os.close(fd)
    finally:
        s.close()


@need_fw
def test_realtime_speed_factor():
    s = console()
    s.set_realtime(True, 4.0)
    try:
        s.run_for(5e6)
        time.sleep(0.1)
        w0 = time.monotonic()
        s.run_for(400e6)
        assert 0.09 <= time.monotonic() - w0 <= 0.5
    finally:
        s.close()


@need_fw
def test_stop_wakes_a_waiting_paced_run():
    s = console()
    s.set_realtime(True)
    try:
        r = Runner(s, 60e9)
        with r:
            time.sleep(0.1)
        assert not r.t.is_alive() and getattr(r.reason, "name", "") == "USER"
    finally:
        s.close()


# ------------------------------------------------------------------------------------------------ TCP / pyserial
@need_fw
def test_tcp_round_trip_one_client_at_a_time():
    s = console()
    br = s.attach_host_serial("UART0", "tcp:127.0.0.1:0")
    port = br.port
    try:
        with Runner(s, float("inf")):
            c = socket.create_connection(("127.0.0.1", port.port), timeout=5)
            try:
                assert wait(lambda: port.connected)
                c2 = socket.create_connection(("127.0.0.1", port.port), timeout=5)
                c2.settimeout(2)
                assert c2.recv(10) == b""                     # refused while busy
                c2.close()
                c.sendall(b"echo tcp\r")
                buf = b""
                c.settimeout(5)
                while b"tcp\r\n> " not in buf:
                    buf += c.recv(100)
                assert b"echo tcp\r\ntcp\r\n> " in buf
            finally:
                c.close()
            assert wait(lambda: not port.connected)
    finally:
        s.close()


@need_fw
@need_pty
def test_serial_port_via_pyserial_on_a_pty_pair():
    pytest.importorskip("serial")
    import termios
    import tty
    m, sl = os.openpty()
    path = os.ttyname(sl)
    tty.setraw(sl)
    s = console()
    try:
        br = s.attach_host_serial("UART0", f"serial:{path}:115200:8N1")
        assert br.port.line() == LineSettings(115200, 8, "N", 1.0)
        os.set_blocking(m, False)
        with Runner(s, float("inf")):
            assert BANNER in read_until(m, b"help\r\n> ")
            os.write(m, b"echo ser\r")
            assert b"ser\r\n> " in read_until(m, b"ser\r\n> ")
    finally:
        s.close()
        os.close(sl)
        os.close(m)
    assert termios is not None


def test_missing_pyserial_is_a_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "serial", None)
    with pytest.raises(ValueError, match="pyserial"):
        open_port("serial:/dev/ttyUSB0:115200")


# ------------------------------------------------------------------------------------------------ CLI
@need_fw
def test_cli_options_and_auto_realtime():
    from chipem.cli import build_parser
    ap = build_parser()
    a = ap.parse_args(["run", str(FW), "--clock", "120e6", "--uart-tcp", "UART0=127.0.0.1:0"])
    s = Session.from_args(a)
    try:
        assert s.pacer is not None and len(s.host_bridges) == 1     # host bridge: real time on by default
    finally:
        s.close()
    a = ap.parse_args(["run", str(FW), "--clock", "120e6", "--uart-tcp", "UART0=127.0.0.1:0", "--no-realtime"])
    s = Session.from_args(a)
    try:
        assert s.pacer is None
    finally:
        s.close()
    a = ap.parse_args(["shell", str(FW), "--speed", "2"])
    s = Session.from_args(a)
    try:
        assert s.pacer is not None and s.pacer.speed == 2
    finally:
        s.close()


@need_fw
@need_pty
def test_cli_run_prints_the_pty_path(capsys):
    from chipem.cli import main
    rc = main(["run", str(FW), "--clock", "120e6", "--uart-pty", "UART0", "--for", "20ms", "-q"])
    out = capsys.readouterr()
    assert rc == 0
    assert "host serial: UART0 <-> pty /dev/" in out.err
    assert "real time x1" in out.out and "host serial:" in out.out


@need_fw
def test_board_yaml_host_serial_entry(tmp_path):
    from chipem.board import parse_board
    d = {"name": "hs", "chips": [{"id": "A", "chip": "mk22fn1m0vll12", "firmware": str(FW), "clock_hz": 120e6}],
         "host_serial": [{"uart": "A.UART0", "memory": True}], "realtime": 2}
    s = Session.from_board(parse_board(d))
    try:
        assert len(s.host_bridges) == 1 and s.pacer.speed == 2
        s.run_for(5e6)
        assert s.host_bridges[0].port.host_recv(1.0) == BANNER
        y = s.board.to_yaml()
        assert "host_serial:" in y and "realtime: 2" in y
    finally:
        s.close()


@pytest.mark.skipif(not (ROOT / "firmware/samples/build/uart_link_echo.elf").exists(), reason="sample not built")
def test_realtime_falls_behind_with_one_warning():
    s = Session("mk22fn1m0vll12", ROOT / "firmware/samples/build/uart_link_echo.elf", clock_hz=120e6)
    s.set_realtime(True, 20.0)                            # a busy-polling loop cannot run 20x real time
    s.pacer.grace_s = 0.0
    s.pacer.max_lag_ns = 20e6
    try:
        s.run_for(20e6)
        info = s.host_status()["realtime"]
        assert info["rebases"] >= 1 and info["max_lag_ms"] >= 20
        w = [e.fields["msg"] for e in s.events(kinds={"warn"}) if "real-time" in str(e.fields.get("msg"))]
        assert len(w) == 1 and "cannot keep up" in w[0]
    finally:
        s.close()


# ------------------------------------------------------------------------------------------------ frames / breaks
def test_host_frame_decoding_helpers():
    from chipem.hostserial import host_bytes_for, host_rx_format
    from chipem.periph.uartwire import Frame
    fr = Frame(0x41, 8, "N", 1.0, 115200)
    assert host_bytes_for(fr, host_rx_format(LineSettings(115200), fr))[0][0] == 0x41
    assert host_bytes_for(fr, None) == [(0x41, None)]                    # TCP / memory: no line settings
    # PTY: only the baud is known; the frame layout is taken from the chip's frame
    rx = host_rx_format(LineSettings(115200, framed=False), Frame(0x41, 7, "E", 1.0, 115200))
    assert rx.bits == 7 and rx.parity == "E"
    # a wrong host baud: a 57600 receiver sees the 5 low bit times of a 115200 0x00 as start + 4 zero bits
    got = host_bytes_for(Frame(0x00, 8, "N", 1.0, 115200), host_rx_format(LineSettings(57600), fr))
    assert [b for b, _ in got] == [0xF0]
    # ... and a 115200 receiver reads 9600 baud low bits as breaks: 0x00 (or a break) on a raw tty
    got = host_bytes_for(Frame(0x00, 8, "N", 1.0, 9600), host_rx_format(LineSettings(115200), fr))
    assert got[0][0] == "break" and got[0][1].fe
    got = host_bytes_for(Frame.brk_frame(13, 1.0, 115200), host_rx_format(LineSettings(115200), fr))
    assert got[0][0] == "break"


@need_fw
def test_memory_bridge_breaks_both_ways_and_session_break_token():
    from chipem.periph.uartwire import Frame
    s = console()
    br = s.attach_host_serial("UART0", "memory")
    port: MemoryPort = br.port
    u = s.chip.peripherals["UART0"]
    try:
        s.run_for(5e6)
        assert port.host_recv(1.0) == BANNER
        port.host_break()                                             # host -> chip: a break reaches the UART
        s.run_for(1e6)
        rx = [e.fields for e in s.events(kinds={"uart"}) if e.fields.get("dir") == "rx"]
        assert rx and rx[-1]["byte"] == 0 and rx[-1]["fe"] and rx[-1]["brk"] >= 13
        u._tx_emit(Frame.brk_frame(13, 1.0, u.baud()))                # chip -> host: what a raw tty reads
        s.run_for(1e6)
        assert port.breaks_out == 1 and port.host_recv(0.5).endswith(b"\x00")
        n = len([e for e in s.events(kinds={"uart"}) if e.fields.get("brk")])
        s.uart_input("<break:20>", "UART0")                           # --uart-in / --at / shell / web token
        s.run_for(1e6)
        brk = [e.fields for e in s.events(kinds={"uart"}) if e.fields.get("brk")]
        assert len(brk) == n + 1 and brk[-1]["brk"] >= 20
    finally:
        s.close()
