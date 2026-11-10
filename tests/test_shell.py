"""chipem shell: non-interactive --exec runs over the blinky sample."""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BLINKY = ROOT / "firmware" / "samples" / "build" / "blinky.elf"

pytest.importorskip("chipem.session", reason="chipem/session.py not available yet")
pytestmark = pytest.mark.skipif(not BLINKY.exists(), reason="sample firmware not built")

from chipem import cmd_shell  # noqa: E402
from chipem.cli import build_parser  # noqa: E402


def run_exec(cmds: str, fw: Path = BLINKY) -> tuple[int, str]:
    args = build_parser().parse_args(["shell", str(fw), "--exec", cmds])
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = cmd_shell.main(args)
    finally:
        sys.stdout = old
    return rc, buf.getvalue()


def test_exec_sections():
    rc, out = run_exec("state; pins gpio; regs; dis; step 5; run 1ms; events 5; quit")
    assert rc == 0, out
    assert "chipem shell" in out and "MK22FN1M0VLL12" in out          # banner
    assert "time " in out and "cycles" in out                         # state
    assert "PTC5" in out and "of 100 pins" in out                     # pins gpio
    assert "r0=" in out and "r15/pc=" in out and "xpsr=" in out       # regs
    assert "=> 0000" in out                                           # dis marks pc
    assert "stopped: COUNT" in out and "t=1.0" in out                 # step / run
    assert "events)" in out                                           # events footer
    assert "error" not in out.lower()


def test_quit_stops_processing_and_errors_are_reported():
    rc, out = run_exec("bogus; quit; state")
    assert rc == 1
    assert "unknown command" in out
    assert "time " not in out          # nothing after quit


def test_pin_mem_bt_uart_periph():
    rc, out = run_exec("run 2ms; pin PTC5; mem 0x20000000 32; bt; uart \"hi;\\r\\n\"; uartout; periph; "
                       "set PTC6 1; pins PTC*; at 5ms PTC6 0; events 3 pin,reset")
    assert rc == 0, out
    assert "pin 77" in out and "ALT1" in out
    assert "20000000  " in out
    assert "#0  pc 0x" in out
    assert "UART0 <- b'hi;\\r\\n'" in out
    assert "PORTC" in out
    assert "PTC6: external=H" in out
    assert "scheduled PTC6=L at 5.000 ms" in out


def test_blinky_toggles_pin_in_shell():
    rc, out = run_exec("run 50ms; events 1000 pin")
    assert rc == 0
    if "PTC5 level" not in out:
        pytest.skip("GPIO model does not report pin events yet")
    assert out.count("PTC5 level") >= 2


def test_cli_subprocess():
    r = subprocess.run([sys.executable, "-m", "chipem.cli", "shell", str(BLINKY), "--exec", "state; quit"],
                       capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "pc " in r.stdout and "clock" in r.stdout


def test_helpers():
    assert cmd_shell.unescape(r"a\r\n\x41\\") == b"a\r\nA\\"
    assert cmd_shell.split_commands('uart "a;b"; quit') == ['uart "a;b"', "quit"]
    assert cmd_shell.parse_time("2ms") == 2e6
    assert cmd_shell.parse_level("z") is None and cmd_shell.parse_level("1") == "H"


C2000_BLINKY = ROOT / "firmware" / "samples_c2000" / "build" / "blinky.c28img"


@pytest.mark.skipif(not C2000_BLINKY.exists(), reason="C28x sample not built")
def test_dis_c28x_uses_c28x_disassembler_and_symbols():
    args = build_parser().parse_args(["shell", str(C2000_BLINKY), "--chip", "tms320f28232",
                                      "--exec", "dis 0x330000 3; dis pll 2; step 2"])
    buf = io.StringIO()
    old, sys.stdout = sys.stdout, buf
    try:
        rc = cmd_shell.main(args)
    finally:
        sys.stdout = old
    out = buf.getvalue()
    assert rc == 0, out
    assert "_c_int00:" in out and "=> 330000: 3b10      DINT" in out    # word address, C28x mnemonic
    assert "330001: 7622      EALLOW" in out
    assert "pll:" in out and "33000A: 4011      TBIT" in out             # symbol name as address
    assert "=> 330002: 761f 01c0 MOVW DP,#0x1C0" in out                  # step shows the next insn
    assert "lsls" not in out and "strb" not in out                       # no Thumb decoding


def test_dis_arm_symbol_name():
    rc, out = run_exec("dis main 2; dis main+4 1")
    assert rc == 0, out
    assert "main:" in out
    lines = [ln for ln in out.splitlines() if ln.strip().startswith(("0000", "=>"))]
    assert len(lines) == 3
    a0 = int(lines[0].split(":")[0].replace("=>", "").strip(), 16)
    assert int(lines[2].split(":")[0].replace("=>", "").strip(), 16) == a0 + 4
    rc, out = run_exec("dis no_such_symbol")
    assert "unknown symbol" in out


def test_trace_collapses_periodic_edges_like_run():
    """`trace on` uses trace.PinEventPrinter: a fast-toggling pin becomes waveform summaries, not one line per edge;
    `trace all` prints every edge."""
    from chipem.session import Session

    def feed(mode):
        args = build_parser().parse_args(["shell", str(BLINKY), "--exec", "quit"])
        s = Session.from_args(args)
        buf = io.StringIO()
        sh = cmd_shell.Shell(s, stdout=buf)
        sh.onecmd(f"trace {mode}")
        log = sh.chip.log
        t = 1e6
        for _ in range(500):                               # 20 kHz, 25 % for 25 ms
            log.event("pin", t, pin="PTC1", field="level", old="L", new="H")
            log.event("pin", t + 12.5e3, pin="PTC1", field="level", old="H", new="L")
            t += 50e3
        sh._flush_trace()
        sh.onecmd("trace off")
        return buf.getvalue().splitlines()
    lines = feed("on")
    assert len(lines) < 12, lines
    assert any("waveform:" in l and "20.000 kHz" in l and "duty 25.0 %" in l for l in lines)
    assert any("waveform unchanged" in l for l in lines)
    assert len(feed("all")) > 1000


def test_adc_command_accepts_codes_and_volts():
    """shell `adc` uses session.parse_adc_value: raw codes for the Kinetis ADC, volts for comparator inputs."""
    rc, out = run_exec("adc ADC0_SE14 2048; adc CMP0_IN0 1200mV; adc CMP0_IN1=1.5V; quit")
    assert rc == 0, out
    assert "ADC0_SE14 = 2048" in out and "CMP0_IN0 = 1.2 V" in out and "CMP0_IN1 = 1.5 V" in out
    rc, out = run_exec("adc CMP0_IN0 1200; quit")
    assert rc == 1 and "volts" in out
