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


CAPTURE_BOARD = ROOT / "boards" / "pwm_capture_3way.yaml"
need_capture = pytest.mark.skipif(not ((STM / "tim_pwm.elf").exists() and (K22 / "ftm_capture.elf").exists()
                                       and (C2K / "ecap.c28img").exists()), reason="PWM / capture samples not built")


@need_capture
def test_pwm_capture_3way_board():
    """boards/pwm_capture_3way.yaml: the STM32 TIM3 CH1 PWM (PA6, 1 kHz, 25 %) is measured by a probe, by the K22
    FTM1 input capture (PTA12, rising edges, 60 MHz: 1 ms = 60000 ticks) and by the F28232 eCAP1 (GPIO24, CAP1 =
    period, CAP2 = high time in SYSCLKOUT cycles at 100 MHz: 100000 / 25000)."""
    import json

    from chipem.session import Session
    sym = json.loads((C2K / "ecap.c28img").read_text())["symbols"]
    s = Session.from_board(CAPTURE_BOARD, quiet=True)
    try:
        s.run_for(12e6)                                         # C28x PLL locks at ~6.6 ms: 5 eCAP periods after it
        st = s.board.parts["P"].state()
        out = s.uart_output("B.UART0").decode()
        c = s.ctxs["C"].chip
        period, high, n = c.bus.read(sym["ecap_period"], 2), c.bus.read(sym["ecap_high"], 2), \
            c.bus.read(sym["ecap_count"], 1)
        pins = {(cid, p): s.ctxs[cid].chip.pins.get(p) for cid, p in (("A", "PA6"), ("B", "PTA12"), ("C", "GPIO24"))}
        warns = [e.fields["msg"] for e in s.events(kinds={"warn"})]
    finally:
        s.close()
    assert st["freq_hz"] is not None and abs(st["freq_hz"] - 1000) < 0.5, st
    # K22: the period is a difference of two captures, so the 3-bus-clock synchroniser delay cancels. PTA12 muxed to
    # FTM1_CH0 before C0SC is set is an input (ELS = 00: pin not used by the FTM), so no spurious first edge.
    lines = out.split("\r\n")
    assert lines[0] == "ftm_capture" and len(lines) == 7 and lines[-1] == "", out
    vals = [int(l.split()[1]) for l in lines[1:6]]
    assert all(l.startswith("period ") for l in lines[1:6]), out
    assert vals == [60000] * 5, vals
    # F28232 eCAP: 1 ms period and 25 % high time at SYSCLKOUT 100 MHz (board quantum 1 us keeps the edges exact)
    assert c.clock.hz == 100e6
    assert period == 100000 and high == 25000, (period, high)
    assert 4 <= n <= 6, n
    assert pins["A", "PA6"].owner == "TIM3_CH1" and pins["A", "PA6"].direction == "out"
    assert pins["B", "PTA12"].owner == "FTM1_CH0" and pins["B", "PTA12"].direction == "in"
    assert pins["C", "GPIO24"].owner == "ECAP1" and pins["C", "GPIO24"].direction == "in"
    assert not [w for w in warns if "conflict" not in w], warns


@need_capture
def test_pwm_capture_3way_cli_pin_report(capsys):
    from chipem.cli import main
    assert main(["run", "--board", str(CAPTURE_BOARD), "--for", "12ms", "--pin-report"]) == 0
    out = capsys.readouterr().out
    events = out.split("\nA| === summary chip A")[0].splitlines()
    assert len(events) < 120, len(events)                       # PWM edges collapsed into waveform summaries
    assert sum("waveform:" in l and "1.000 kHz" in l and "duty 25.0 %" in l for l in events) >= 3
    assert "B|   PTA12   42  FTM1_CH0       in" in out
    assert "C|   GPIO24  68  ECAP1          in" in out
    assert "A|   PA6     42  TIM3_CH1       out" in out
    assert "=== pin report chip C ===" in out and "GPIO24" in out.split("=== pin report chip C ===")[1]
