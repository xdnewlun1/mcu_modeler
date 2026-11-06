"""Cross-family integration: UART name resolution by capability, family-aware static pin roles (`chipem info`),
C28x pin-report attribution and the C2000 <-> STM32 board."""
from pathlib import Path

import pytest

from chipem.chipdesc import load_chip
from chipem.firmware import analyze, format_report, load_firmware

ROOT = Path(__file__).resolve().parent.parent
K22 = ROOT / "firmware" / "samples" / "build"
STM = ROOT / "firmware" / "samples_stm32" / "build"
C2K = ROOT / "firmware" / "samples_c2000" / "build"
BOARD = ROOT / "boards" / "c2000_stm32_uart.yaml"

need = pytest.mark.skipif(not ((STM / "uart_link_echo.elf").exists() and (C2K / "uart_link_sender.c28img").exists()
                               and (K22 / "uart_link_sender.elf").exists()), reason="samples not built")


@pytest.fixture(scope="module")
def stm():
    return load_chip(ROOT / "chips" / "stm32f405zg.yaml")


@pytest.fixture(scope="module")
def c2k():
    return load_chip(ROOT / "chips" / "tms320f28232.yaml")


@need
def test_static_pin_roles_stm32(stm):
    r = analyze(load_firmware(STM / "button_mirror.elf"), stm)
    pc = r.pins_configured
    assert any("GPIOA_MODER" in n and "mode input" in n for n in pc["PA0"])
    assert any("GPIOA_PUPDR" in n and "pull-down" in n for n in pc["PA0"])
    assert any("GPIOB_MODER" in n and "mode output" in n for n in pc["PB0"])
    assert not any("PDDR" in n for notes in pc.values() for n in notes)     # no Kinetis names on an STM32
    r = analyze(load_firmware(STM / "uart_link_echo.elf"), stm)
    assert any("AF7 (USART1_TX)" in n for n in r.pins_configured["PA9"])
    assert any("AF7 (USART1_RX)" in n for n in r.pins_configured["PA10"])
    assert any("mode AF" in n for n in r.pins_configured["PA10"])
    assert "Provisional pin roles" in format_report(r)


@need
def test_static_pin_roles_c2000(c2k):
    r = analyze(load_firmware(C2K / "blinky.c28img"), c2k)
    notes = r.pins_configured["GPIO34"]
    assert any("GPBMUX1" in n and "mux 0" in n for n in notes)
    assert any("GPBDIR" in n and "output" in n for n in notes)
    r = analyze(load_firmware(C2K / "uart_link_sender.c28img"), c2k)
    assert any("SCITXDA" in n for n in r.pins_configured["GPIO29"])
    assert any("SCIRXDA" in n for n in r.pins_configured["GPIO28"])
    assert "Provisional pin roles" in format_report(r)


@need
def test_static_pin_roles_kinetis_unchanged():
    fn = load_chip(ROOT / "chips" / "mk22fn1m0vll12.yaml")
    r = analyze(load_firmware(K22 / "uart_link_sender.elf"), fn)
    assert any("ALT2 (UART0_TX)" in n for n in r.pins_configured["PTA2"])


@need
def test_uart_names_resolve_by_capability():
    from chipem.session import Session
    s = Session.from_board(BOARD, quiet=True)
    try:
        a, b = s.chips["A"], s.chips["B"]
        assert "SCIA" in a.uart_blocks() and "USART1" in b.uart_blocks()
        assert b.find_uart("uart1") == "USART1" and b.find_uart("USART1") == "USART1"
        assert a.find_uart("sci-a") == "SCIA" and a.find_uart("UARTA") == "SCIA"
        assert b.find_uart("UART0") is None and b.find_uart("UART0", fallback=True) == "USART1"
        s.run_for(12e6)
        assert s.uart_output("A.SCIA").startswith(b"ping 1\r\n")
        assert s.uart_output("B.uart1").startswith(b"PING 1\r\n")
        with pytest.raises(ValueError):
            s.uart_output("B.SPI1")
    finally:
        s.close()


@need
def test_board_run_summary_and_pin_report(capsys):
    from chipem.cli import main
    assert main(["run", "--board", str(BOARD), "--for", "12ms", "--summary-only", "--pin-report"]) == 0
    out = capsys.readouterr().out
    assert 'A|   SCIA (' in out and 'B|   USART1 (' in out
    assert "UART output:\nB|   (none)" not in out
    # per-chip pin reports with family roles; C28x PCs attributed to the instruction (not the slice start)
    assert "=== pin report chip A ===" in out and "=== pin report chip B ===" in out
    a = out.split("=== pin report chip A ===")[1].split("=== pin report chip B ===")[0]
    b = out.split("=== pin report chip B ===")[1]
    assert "GPIO34" in a and "GPIO out" in a and "SCITXDA" in a and "SCIRXDA" in a
    assert "SCIA: GPIO29 TXD, GPIO28 RXD" in a
    assert "USART1_TX" in b and "USART1_RX" in b and "PB0" in b and "GPIO out" in b
