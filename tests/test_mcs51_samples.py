"""P89CV51 sample firmware (firmware/samples_mcs51, built by build.sh with tools/mcs51_asm.py) and boards: behaviour
and timings at 11.0592 MHz, 12-clock mode."""
from __future__ import annotations

from pathlib import Path

import pytest

from chipem.core.mcs51.asm import assemble_file, symbols_from_regs
from chipem.session import Session

from m51util import MC_NS

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "firmware" / "samples_mcs51"
BUILD = SRC / "build"
CHIP = ROOT / "chips" / "p89cv51rd2.yaml"


def _need(name: str) -> Path:
    p = BUILD / f"{name}.hex"
    if not p.exists():
        pytest.skip(f"{p} not built (firmware/samples_mcs51/build.sh)")
    return p


def sess(name: str, chip: Path = CHIP) -> Session:
    return Session(chip, _need(name), quiet=True)


def edges(s: Session, pin: str, level: str | None = None) -> list[float]:
    return [e.t_ns for e in s.events() if e.kind == "pin" and e.fields.get("pin") == pin
            and e.fields.get("field") == "level" and (level is None or e.fields.get("new") == level)]


def iram(s: Session, a: int, chip: str | None = None) -> int:
    c = s.chips[chip] if chip else s.chip
    return c.core.iram[a]


def warns(s: Session) -> list:
    return [e for e in s.events() if e.kind in ("warn", "fault")]


def test_samples_build_from_source_matches_build_dir():
    sfrs, bits = symbols_from_regs(ROOT / "chips" / "p89cv51_regs.yaml")
    for src in sorted(SRC.glob("*.asm")):
        r = assemble_file(src, sfrs=sfrs, bits=bits)
        hexf = BUILD / f"{src.stem}.hex"
        if hexf.exists():
            assert hexf.read_text() == r.to_ihex(), f"{hexf} is stale: run build.sh"


def test_blinky_50ms():
    s = sess("blinky")
    s.run_for(260e6)
    e = edges(s, "P1.0")
    assert len(e) == 5 and not warns(s)
    # 46080 counts + the reload instructions (CLR TR0 .. SETB TR0: 7 machine cycles) + JNB polling granularity
    for a, b in zip(e[1:], e[2:]):
        assert (b - a) / MC_NS == pytest.approx(46080 + 7 + 1, abs=1.01)    # + 0..2: JNB polling granularity


def test_uart_hello_banner_and_echo():
    s = sess("uart_hello")
    s.run_for(60e6)
    assert s.uart_output("UART") == b"Hello from P89CV51RD2 (UART 9600 from Timer 1)\r\n"
    s.uart_input(b"ok", "UART")
    s.run_for(5e6)
    assert s.uart_output("UART").endswith(b"ok") and not warns(s)
    tx = [e.t_ns for e in s.events() if e.kind == "uart" and e.fields.get("dir") == "tx"]
    assert (tx[2] - tx[1]) == pytest.approx(10e9 / 9600, rel=0.01)       # back to back at 9600 8N1


def test_uart_t2_interrupt_echo_uppercase():
    s = sess("uart_t2")
    s.run_for(30e6)
    assert s.uart_output("UART").startswith(b"P89CV51 T2 9600 echo\r\n")
    s.uart_input(b"abcXYZ!", "UART")
    s.run_for(10e6)
    assert s.uart_output("UART").endswith(b"ABCXYZ!") and iram(s, 0x32) == 7
    assert dict((n, c) for _, n, c in s.irq_summary()).get("SERIAL") == 7


def test_timer_irq_10ms_ticks_high_priority():
    s = sess("timer_irq")
    s.run_for(101e6)
    e = edges(s, "P1.1")
    assert iram(s, 0x31) == 10 and len(e) == 10
    for a, b in zip(e, e[1:]):
        assert (b - a) / MC_NS == pytest.approx(9216 + 8, abs=1)          # reload in the ISR: +8 machine cycles
    assert s.chip.arch._prio(s.chip.arch.source("TF0")) == 3


def test_ext_int_edges_and_level():
    s = sess("ext_int")
    s.run_for(1e5)
    for _ in range(4):
        s.set_pin("P3.2", "L")
        s.run_for(5e4)
        s.set_pin("P3.2", "H")
        s.run_for(5e4)
    assert iram(s, 0x30) == 4 and len(edges(s, "P1.0")) == 4


def test_pca_pwm_loopback_board_measures_its_own_pwm():
    b = ROOT / "boards" / "p89cv51_pca_loopback.yaml"
    _need("pca_pwm")
    s = Session.from_board(b, quiet=True)
    s.run_for(3e6)
    c = s.chips["A"].core
    caps = [c.iram[0x40 + 2 * i] << 8 | c.iram[0x41 + 2 * i] for i in range(c.iram[0x30])]
    assert len(caps) == 8
    diffs = [(y - x) & 0xFFFF for x, y in zip(caps, caps[1:])]
    assert diffs == [64, 192] * 3 + [64]                # 25 % of the 256-count period high
    led = s.board.parts["LED1"].state()
    assert led["changes"] > 20


def test_spi_flash_board_jedec_id():
    _need("spi_flash")
    s = Session.from_board(ROOT / "boards" / "p89cv51_spi_flash.yaml", quiet=True)
    s.run_for(30e6)
    c = s.chips["A"].core
    assert bytes(c.iram[0x30:0x33]) == b"\xef\x40\x16" and bytes(c.iram[0x40:0x50]) == b"\xff" * 16
    assert s.uart_output("A.UART") == b"ID EF4016\r\n"


def test_iap_flash_sample():
    s = sess("iap_flash")
    s.run_for(30e6)
    assert iram(s, 0x30) == 0x55 and iram(s, 0x31) == 0 and iram(s, 0x32) == 0x15
    assert s.uart_output("UART") == b"IAP OK!"
    assert bytes(s.chip.core.code[0xE000:0xE008]) == b"IAP OK!\0"


def test_wdt_reset_sample_three_resets():
    s = sess("wdt_reset")
    s.run_for(300e6)
    resets = [e.t_ns for e in s.events() if e.kind == "reset" and e.fields.get("reason") == "wdt"]
    assert len(resets) == 3 and iram(s, 0x7F) == 3
    gap = (resets[1] - resets[0]) / MC_NS
    assert 4 * 9000 < gap < 4 * 9300 + 16383 + 100      # 4 feeds ~10 ms apart, then 16383 cycles


def test_idle_and_powerdown_sample():
    s = sess("idle_pd")
    s.run_for(60e6)
    assert iram(s, 0x30) == 5 and s.chip.sleeping and iram(s, 0x31) == 0
    infos = [e.fields.get("msg") for e in s.events() if e.kind == "info"]
    assert infos.count("Idle mode entered") == 5 and "Power-down mode entered" in infos
    s.set_pin("P3.2", "L")
    s.run_for(1e5)
    s.set_pin("P3.2", "H")
    s.run_for(1e5)
    assert iram(s, 0x31) == 1 and iram(s, 0x32) == 0xA5 and not s.chip.sleeping


def test_p89cv51_stm32_uart_board():
    _need("uart_ping")
    if not (ROOT / "firmware" / "samples_stm32" / "build" / "uart_link_echo.elf").exists():
        pytest.skip("STM32 sample not built")
    s = Session.from_board(ROOT / "boards" / "p89cv51_stm32_uart.yaml", quiet=True)
    s.run_for(12e6)
    c = s.chips["A"].core
    assert bytes(c.iram[0x31:0x41]) == b"PING 0\r\nPING 1\r\n"
    assert s.uart_output("A.UART").startswith(b"ping 0\r\nping 1\r\nping 2")
    assert s.chips["A"].peripherals["UART"].baud() == pytest.approx(115200)
    assert not [e for e in s.events() if e.kind == "warn"]


def test_rs232_console_board_round_trip():
    """The console behind the MAX232-style transceiver and the USB-serial adapter: bytes typed on the PC side reach
    the UART (inverted twice) and the replies come back (memory port instead of a PTY)."""
    _need("uart_console")
    import copy

    import yaml
    from chipem.board import parse_board
    path = ROOT / "boards" / "p89cv51_rs232_console.yaml"
    data = yaml.safe_load(path.read_text())
    for p in data["parts"]:
        if p["id"] == "PC":
            p["port"] = "memory"
    s = Session.from_board(parse_board(copy.deepcopy(data), base=path.parent, path=path), quiet=True)
    pc = s.board.part("PC")
    try:
        s.run_for(40e6)
        assert pc.host.host_recv(1.0) == b"\r\nP89CV51RD2 console (9600 8N1)\r\n> "
        pc.host.host_send(b"l\r")
        s.run_for(20e6)
        assert pc.host.host_recv(1.0) == b"l\r\nLED toggled\r\n> "
        assert s.board.part("LED1").state()["on"] is True          # P1.0 low: active-low LED on
        assert not [e for e in s.events() if e.kind == "warn"]
    finally:
        s.close()
