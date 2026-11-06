"""chipem.session: parsing helpers, stimulus scheduling, pin table, events, stop, clock override."""
import threading
import time
from pathlib import Path

import pytest

from chipem.cli import build_parser
from chipem.core import StopReason
from chipem.session import (Session, decode_escapes, parse_level, parse_stimulus, parse_time, parse_uart_in,
                            parse_uart_stimulus)

ROOT = Path(__file__).resolve().parent.parent
BLINKY = ROOT / "firmware/samples/build/blinky.elf"
UART_ECHO = ROOT / "firmware/samples/build/uart_echo.elf"
needs_fw = pytest.mark.skipif(not BLINKY.exists(), reason="sample firmware not built (firmware/samples/build.sh)")


def test_parse_time():
    assert parse_time("50ms") == 50e6
    assert parse_time("1.5s") == 1.5e9
    assert parse_time("200us") == 200e3
    assert parse_time("10ns") == 10.0
    assert parse_time("7") == 7e6                    # default unit ms
    assert parse_time(" 2.5 ms ") == 2.5e6
    with pytest.raises(ValueError):
        parse_time("abc")
    with pytest.raises(ValueError):
        parse_time("5min")


def test_parse_level():
    assert [parse_level(x) for x in ("1", "0", "H", "l", "x", "Z", 1, 0)] == ["H", "L", "H", "L", "X", None, "H", "L"]
    with pytest.raises(ValueError):
        parse_level("2")


def test_parse_stimulus_and_uart_in():
    assert parse_stimulus("PTC6=1") == (None, "PTC6", "H")
    assert parse_stimulus("50ms:PTC6=0") == (50e6, "PTC6", "L")
    assert parse_stimulus("1.5s:PTC6=X") == (1.5e9, "PTC6", "X")
    assert parse_stimulus("2:PTA4=Z") == (2e6, "PTA4", None)
    with pytest.raises(ValueError):
        parse_stimulus("PTC6")
    assert decode_escapes(r"gpio get\r\n\x41\\") == b"gpio get\r\nA\\"
    assert parse_uart_in(r"UART1:hi\n") == ("UART1", b"hi\n")
    assert parse_uart_in("hello") == ("UART0", b"hello")
    assert parse_uart_stimulus(r"100ms:UART2=help\r\n") == (1e8, "UART2", b"help\r\n")
    assert parse_uart_stimulus("B.usart1=a=b:c") == (None, "B.USART1", b"a=b:c")
    assert parse_uart_stimulus("5ms:SCIA=ok") == (5e6, "SCIA", b"ok")
    assert parse_uart_stimulus("50ms:PTC6=0") is None and parse_uart_stimulus("PA9=1") is None


@needs_fw
def test_uart_input_paced_at_baud_and_dropped_when_not_listening():
    # uart_echo enables UART0 (and muxes PTA1) a few us after reset; at 48 MHz SBR 32/BRFA 18 -> ~92 kbaud
    s = Session("mk22fn1m0vll12", UART_ECHO, clock_hz=48e6)
    s.uart_input(b"x")                              # sent from t=0 at the baud UART0 has then
    s.apply_stimulus("3ms:UART0=abcd")
    s.run_for(6e6)
    u = s.chip.peripherals["UART0"]
    rx = [e for e in s.events(kinds={"uart"}) if e.get("dir") == "rx"]
    assert [chr(e.byte) for e in rx][-4:] == list("abcd")
    t = [e.t_ns for e in rx[-4:]]
    assert t[0] == pytest.approx(3e6 + u.char_ns(), rel=0.01)
    assert all(b - a == pytest.approx(u.char_ns(), rel=0.01) for a, b in zip(t, t[1:]))
    assert s.uart_output("UART0").endswith(b"abcd")


@needs_fw
def test_schedule_pin_fires_at_virtual_time():
    s = Session("mk22fn1m0vll12", BLINKY)
    s.schedule_pin(3e6, "PTC6", "L")
    s.apply_stimulus("5ms:PTC6=1")
    s.run_for(2e6)
    assert s.chip.pins.get("PTC6").external is None
    s.run_for(4e6)
    evs = [e for e in s.events(kinds={"pin"}) if e.pin == "PTC6" and e.field == "level"]
    assert len(evs) == 2
    assert evs[0].new == "L" and abs(evs[0].t_ns - 3e6) < 100
    assert evs[1].new == "H" and abs(evs[1].t_ns - 5e6) < 100
    assert s.chip.pins.get("PTC6").external == "H"
    s.set_pin("PTC6", None)
    assert s.chip.pins.get("PTC6").external is None
    s.schedule_pin(1e6, "PTC6", "1")                # in the past -> applied now
    assert s.chip.pins.get("PTC6").external == "H"
    with pytest.raises(KeyError):
        s.schedule_pin(9e6, "PTZ99", "1")


@needs_fw
def test_pin_table_state_and_events_since():
    s = Session("mk22fn1m0vll12", BLINKY)
    tbl = s.pin_table()
    assert len(tbl) == 100
    keys = {"num", "name", "short", "kind", "owner", "direction", "level", "drive", "pull", "external", "functions"}
    assert all(keys <= set(r) and r["chip"] == "A" for r in tbl)
    ptc5 = next(r for r in tbl if r["short"] == "PTC5")
    assert ptc5["kind"] == "gpio" and "ALT1" in ptc5["functions"]
    st = s.state()
    assert {"pc", "t_ns", "cycles", "hz", "running"} <= set(st) and st["running"] is False
    all_ev = s.events()
    assert all_ev and all(e.seq > 0 for e in all_ev)
    last = all_ev[-1].seq
    assert s.events(since=last) == []
    s.set_pin("PTC6", "H")
    new = s.events(since=last)
    assert [e.kind for e in new] == ["pin"] and new[0].pin == "PTC6"
    assert s.events(since=0, kinds={"reset"})[0].kind == "reset"


@needs_fw
def test_from_args_applies_stimulus_and_uart():
    args = build_parser().parse_args(["run", str(UART_ECHO), "--set", "PTC6=1", "--at", "2ms:PTC6=0",
                                      "--at", r"3ms:UART0=x\n", "--clock", "48e6"])
    s = Session.from_args(args)
    assert s.chip.pins.get("PTC6").external == "H"
    s.run_for(10e6)
    assert s.chip.pins.get("PTC6").external == "L"
    assert s.uart_output("UART0").startswith(b"hello\r\n")
    assert s.uart_output("UART0").endswith(b"x\n")
    assert s.chip.clock.hz == 48e6                   # --clock froze the core clock
    assert getattr(s.chip, "clock_override", None) == 48e6


@needs_fw
def test_stop_from_other_thread():
    s = Session("mk22fn1m0vll12", BLINKY)
    res = {}
    th = threading.Thread(target=lambda: res.setdefault("r", s.run_for(60e9)))
    th.start()
    for _ in range(200):
        if s.running:
            break
        time.sleep(0.005)
    time.sleep(0.05)
    s.stop()
    th.join(10)
    assert not th.is_alive()
    assert res["r"] is StopReason.USER
    assert s.chip.clock.now_ns() < 60e9


@needs_fw
def test_vcd_option(tmp_path):
    p = tmp_path / "out.vcd"
    s = Session("mk22fn1m0vll12", BLINKY, vcd_path=p)
    s.apply_stimulus("1ms:PTC6=1")
    s.run_for(2e6)
    s.close()
    text = p.read_text()
    assert "$timescale 1ns $end" in text and " PTC6 $end" in text
    ts = [int(l[1:]) for l in text.splitlines() if l.startswith("#")]
    assert any(1_000_000 <= t <= 1_000_100 for t in ts)          # stimulus at 1 ms (cycle-rounded)


@needs_fw
def test_set_adc_reaches_model():
    s = Session("mk22fn1m0vll12", BLINKY)
    s.set_adc("ADC0_SE14", 2048)
    assert s.adc_values["ADC0_SE14"] == 2048
    adc = s.chip.peripherals["ADC0"]
    if hasattr(adc, "values"):
        assert 2048 in adc.values.values()
