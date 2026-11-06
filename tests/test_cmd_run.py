"""`chipem run` end to end (chipem/cmd_run.py)."""
import struct
from pathlib import Path

import pytest

from chipem.cli import main
from chipem.session import Session

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "firmware/samples/build"
needs_fw = pytest.mark.skipif(not (BUILD / "blinky.elf").exists(), reason="sample firmware not built")


@needs_fw
def test_run_blinky(capsys):
    rc = main(["run", str(BUILD / "blinky.elf"), "--for", "5ms"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "=== summary ===" in out
    assert "virtual time : 5 ms" in out
    assert "instructions :" in out and "M insn/s" in out


@needs_fw
def test_run_uart_echo(capsys):
    rc = main(["run", str(BUILD / "uart_echo.elf"), "--for", "20ms", "--at", "5ms:UART0=abc"])
    out = capsys.readouterr().out
    assert rc == 0
    assert 'UART0 (10 bytes): "hello\\r\\nabc"' in out
    assert "UART0 TX:" in out and "UART0 RX:" in out


@needs_fw
def test_uart_echo_session_output():
    # uart_echo programs SBR=32 BRFA=18 (computed for a 60 MHz module clock -> 115,177 baud). UART0 is clocked by
    # the core clock: at the reset FEI clock (20.97 MHz) that is ~40.25 kbaud, at --clock 120e6 ~230.4 kbaud.
    s = Session("mk22fn1m0vll12", BUILD / "uart_echo.elf")
    s.schedule_uart(5e6, "UART0", b"abc")       # "hello\r\n" at ~40 kbaud takes ~1.7 ms; typed later
    s.run_for(20e6)
    assert s.uart_output("UART0") == b"hello\r\nabc"
    u = s.chip.peripherals["UART0"]
    assert u.sbr() == 32 and u.get("C4") & 0x1F == 18
    assert abs(u.baud() - s.chip.clock.hz / (16 * (32 + 18 / 32))) < 1


@needs_fw
def test_run_quiet_and_event_filter(capsys):
    rc = main(["run", str(BUILD / "uart_echo.elf"), "--for", "5ms", "--quiet"])
    out = capsys.readouterr().out
    assert rc == 0 and out.lstrip().startswith("=== summary ===")
    rc = main(["run", str(BUILD / "uart_echo.elf"), "--for", "5ms", "--events", "uart"])
    out = capsys.readouterr().out
    head = out.split("=== summary ===")[0]
    assert "UART0 TX:" in head and "RESET" not in head
    assert main(["run", str(BUILD / "uart_echo.elf"), "--events", "bogus"]) == 1


def test_run_fault_exit_code(tmp_path, capsys):
    img = bytearray(0x404)
    img[0:8] = struct.pack("<II", 0x20008000, 0x401)
    img[0x400:0x402] = struct.pack("<H", 0xDE00)      # udf #0
    p = tmp_path / "fault.bin"
    p.write_bytes(bytes(img))
    rc = main(["run", str(p), "--load-addr", "0", "--for", "1ms", "--stop-on-fault", "--max-slice", "500"])
    out = capsys.readouterr().out
    assert rc == 3
    assert "FAULT" in out and "stop         : FAULT" in out
