"""chipem serve, live data for the UI rework: slim pin stream (deltas, `pins_full`, `waves` at <= 2 Hz), `adc` per pin,
`periph` status, non-UART `buses`, POST /api/step {chip}, /api/restart, /api/stimulus, GET /api/vcd, /api/events?pin=,
/api/uart/lines, compact event text for can/eth/usb/i2s."""
from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "firmware" / "samples" / "build"
BLINKY = SAMPLES / "blinky.elf"
SENDER = SAMPLES / "uart_link_sender.elf"
BOARD2 = ROOT / "boards" / "two_k22_uart.yaml"
CAN_BOARD = ROOT / "boards" / "k22_stm32_can.yaml"
USB_BOARD = ROOT / "boards" / "stm32_usb_cdc.yaml"

pytestmark = pytest.mark.skipif(not BLINKY.exists(), reason="sample firmware not built")

from chipem.session import Session  # noqa: E402
from chipem.web.server import SLIM_KEYS, WebApp, uart_lines  # noqa: E402


def _start(s, **kw):
    return WebApp(s, "127.0.0.1", 0, **kw).start()


@pytest.fixture
def app():
    s = Session("mk22fn1m0vll12", BLINKY)
    a = _start(s)
    yield a
    a.stop()
    a.session.close()


def get(app, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{app.port}{path}", timeout=30) as r:
        return r.status, r.headers, r.read()


def get_json(app, path):
    return json.loads(get(app, path)[2])


def post(app, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def wait_for(cond, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.02)
    return cond()


def run_ms(app, ms, timeout=60):
    assert post(app, "/api/run", {"ms": ms})[0] == 200
    assert wait_for(lambda: get_json(app, "/api/state").get("last_stop") == "DONE", timeout)


def stream_frames(app, seconds):
    """[(arrival time, frame)] of the `state` frames of /api/stream for `seconds`."""
    conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)
    conn.request("GET", "/api/stream")
    resp = conn.getresponse()
    out, kind = [], None
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        line = resp.fp.readline()
        if not line:
            break
        if line.startswith(b"event: "):
            kind = line[7:].strip().decode()
        elif line.startswith(b"data: ") and kind == "state":
            out.append((time.monotonic(), json.loads(line[6:])))
    conn.close()
    return out


# ---- slim pin stream ------------------------------------------------------------------------------------------
def test_stream_slim_pins_deltas_and_waves(app):
    post(app, "/api/run", {})
    frames = stream_frames(app, 2.5)
    post(app, "/api/pause")
    assert frames
    first = frames[0][1]
    assert first["pins_full"] is True and len(first["pins"]) == 100
    assert all(set(r) == set(SLIM_KEYS) for r in first["pins"])          # no name/short/kind/functions/wave
    later = [f for _t, f in frames[1:] if "pins" in f]
    assert later, "blinky toggles PTC5: some delta frame expected"
    for f in later:
        assert f["pins_full"] is False and 0 < len(f["pins"]) < 100
    wt = [t for t, f in frames if "waves" in f]
    assert all(b - a >= 0.45 for a, b in zip(wt, wt[1:])), wt              # at most every 500 ms
    st = get_json(app, "/api/state")                                       # /api/state keeps the full rows
    assert {"name", "short", "functions"} <= set(st["pins"][0]) and "waves" in st


def test_reset_resends_full_pin_set(app):
    post(app, "/api/run", {"ms": 2})
    conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)
    conn.request("GET", "/api/stream")
    resp = conn.getresponse()
    fulls, kind = 0, None
    end = time.monotonic() + 4
    reset_done = False
    while time.monotonic() < end and fulls < 2:
        line = resp.fp.readline()
        if line.startswith(b"event: "):
            kind = line[7:].strip().decode()
        elif line.startswith(b"data: ") and kind == "state":
            if json.loads(line[6:]).get("pins_full"):
                fulls += 1
                if not reset_done:
                    assert post(app, "/api/reset")[0] == 200
                    reset_done = True
    conn.close()
    assert fulls == 2


# ---- analog inputs ------------------------------------------------------------------------------------------------
def test_adc_channel_per_pin_and_values(app):
    pins = get_json(app, "/api/chip")["pins"]
    by = {p["short"]: p for p in pins}
    assert by["PTB0"]["adc"] == "ADC0_SE8" and by["PTC5"]["adc"] is None
    assert sum(1 for p in pins if p["adc"]) >= 20
    code, _ = post(app, "/api/adc", {"channel": by["PTB0"]["adc"], "value": 2048})
    assert code == 200
    assert get_json(app, "/api/state")["adc"]["A"]["ADC0_SE8"] == 2048


@pytest.mark.parametrize("chip", ["stm32f407vg", "tms320f28232"])
def test_adc_channel_accepted_by_other_families(chip):
    from chipem.web.server import adc_channel
    s = Session(chip, None)
    try:
        chans = [adc_channel(p) for p in s.chip.pins.pins]
        chans = [c for c in chans if c]
        assert len(chans) >= 8
        n = len(list(s.chip.log))
        for c in chans:
            s.set_adc(c, 100)
        assert not [e for e in list(s.chip.log)[n:] if e.kind == "warn"]
    finally:
        s.close()


# ---- step / restart / stimulus ------------------------------------------------------------------------------------
@pytest.fixture
def board_app():
    if not BOARD2.exists() or not SENDER.exists():
        pytest.skip("board sample firmware not built")
    s = Session.from_board(BOARD2)
    a = _start(s)
    yield a
    a.stop()
    a.session.close()


def test_step_selected_chip(board_app):
    st0 = get_json(board_app, "/api/state")["chips"]
    code, r = post(board_app, "/api/step", {"n": 3, "chip": "B"})
    assert code == 200 and r["chip"] == "B"
    st1 = get_json(board_app, "/api/state")["chips"]
    assert st1["B"]["insns"] == st0["B"]["insns"] + 3 and st1["A"]["insns"] == st0["A"]["insns"]
    assert post(board_app, "/api/step", {"n": 1, "chip": "Z"})[0] == 400


def test_restart_board_keeps_setup_and_time_zero(board_app):
    run_ms(board_app, 3)
    boot = get_json(board_app, "/api/chip")["boot"]
    code, r = post(board_app, "/api/restart")
    assert code == 200 and r["boot"] != boot and r["chips"] == ["A", "B"]
    c = get_json(board_app, "/api/chip")
    assert c["boot"] == r["boot"] and c["board"] and len(c["chips"]) == 2
    assert get_json(board_app, "/api/state")["board"]["t_ns"] == 0


def test_restart_single_chip_with_factory():
    s = Session("mk22fn1m0vll12", BLINKY)
    a = _start(s, factory=lambda: Session("mk22fn1m0vll12", BLINKY))
    try:
        run_ms(a, 2)
        assert get_json(a, "/api/state")["t_ns"] > 1e6
        code, r = post(a, "/api/restart")
        assert code == 200
        st = get_json(a, "/api/state")
        assert st["t_ns"] == 0 and "board" not in st or st.get("board") is None
        assert get_json(a, "/api/chip")["board"] is None              # still a single chip
    finally:
        a.stop()
        a.session.close()


def test_restart_single_chip_without_factory(app):
    run_ms(app, 2)
    code, r = post(app, "/api/restart")
    assert code == 200 and r["chips"] == ["A"]
    assert get_json(app, "/api/state")["t_ns"] == 0


def test_stimulus_relative_absolute_now(app):
    run_ms(app, 1)
    now = get_json(app, "/api/state")["t_ns"]
    code, r = post(app, "/api/stimulus", {"spec": "+2ms:PTC6=H"})
    assert code == 200 and abs(r["t_ns"] - (now + 2e6)) < 1
    code, r = post(app, "/api/stimulus", {"spec": "50ms:PTC7=1"})
    assert code == 200 and r["t_ns"] == 50e6
    code, r = post(app, "/api/stimulus", {"spec": "PTC8=H"})
    assert code == 200 and r["t_ns"] is None
    row = lambda n: next(p for p in get_json(app, "/api/state")["pins"] if p["short"] == n)    # noqa: E731
    assert row("PTC8")["external"] == "H" and row("PTC6")["external"] is None
    run_ms(app, 3)
    assert row("PTC6")["external"] == "H" and row("PTC7")["external"] is None
    assert post(app, "/api/stimulus", {"spec": "NOPE=H"})[0] == 400
    assert post(app, "/api/stimulus", {})[0] == 400


# ---- VCD, events pin=, uart lines -------------------------------------------------------------------------------
def test_vcd_and_events_pin_filter(app):
    run_ms(app, 60)
    evs = get_json(app, "/api/events?since=0&kinds=pin&pin=PTC5&limit=0")["events"]
    if not evs:
        pytest.skip("GPIO model does not report pin events")
    assert all(e["pin"] == "PTC5" for e in evs)
    code, hdr, body = get(app, "/api/vcd?pins=PTC5,PTC6")
    text = body.decode()
    assert code == 200 and ".vcd" in hdr["Content-Disposition"]
    assert "$timescale 1ns $end" in text and " PTC5 $end" in text and " PTC6 $end" in text
    lv = [e for e in evs if (e.get("field") or "level") == "level"]
    assert text.count("\n#") >= len({round(e["t_ns"]) for e in lv})


def test_uart_lines_endpoint():
    if not SENDER.exists():
        pytest.skip("uart_link_sender sample not built")
    s = Session("mk22fn1m0vll12", SENDER, clock_hz=120_000_000)
    a = _start(s)
    try:
        run_ms(a, 8)
        r = get_json(a, "/api/uart/lines")
        raw = get_json(a, "/api/uart?uart=UART0")["text"]
        want = [x for x in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
        got = [ln["text"] for ln in r["lines"] if ln["uart"] == "UART0"]
        assert got and got == (want[:-1] if want[-1] == "" else want)[:len(got)]
        assert r["last"] > 0 and all(ln["t"] is not None for ln in r["lines"])
    finally:
        a.stop()
        s.close()


def test_uart_lines_crlf_no_stray_line():
    ev = lambda b, d="tx": {"kind": "uart", "uart": "UART0", "dir": d, "byte": b, "t_ns": 1.0}   # noqa: E731
    r = uart_lines([ev(ord("a")), ev(13), ev(10), ev(ord("b")), ev(10), ev(1), ev(0x41, "drop")], False, "A")
    assert [(ln["text"], ln["open"]) for ln in r["lines"]] == [("a", False), ("b", False), ("\\x01", True)]
    assert r["drops"] == {"UART0": {"n": 1, "reason": "?"}}


# ---- buses / periph -------------------------------------------------------------------------------------------
@pytest.mark.skipif(not CAN_BOARD.exists(), reason="no CAN board")
def test_can_bus_and_periph_status():
    s = Session.from_board(CAN_BOARD)
    a = _start(s)
    try:
        c = get_json(a, "/api/chip")
        assert c["links"] == [] or all(ln["type"] == "uart" for ln in c["links"])
        (bus,) = c["buses"]
        assert bus["type"] == "can" and len(bus["nodes"]) == 2 and set(bus["stats"]) >= {"frames", "error_frames"}
        run_ms(a, 20, 120)
        st = get_json(a, "/api/state")
        assert st["board"]["buses"][0]["stats"]["frames"] > 0
        assert get_json(a, "/api/chip")["buses"][0]["stats"]["frames"] > 0
        cans = [p for p in st["periph"] if p["type"] == "can"]
        assert len(cans) == 2 and all({"tec", "rec", "state", "tx", "rx"} <= set(p) for p in cans)
        evs = get_json(a, "/api/events?kinds=can&limit=0")["events"]
        assert evs and " 0x" in evs[0]["desc"] and "[" in evs[0]["desc"] and "sof_ns" not in evs[0]["desc"]
    finally:
        a.stop()
        s.close()


@pytest.mark.skipif(not USB_BOARD.exists(), reason="no USB board")
def test_usb_bus_static():
    s = Session.from_board(USB_BOARD)
    a = _start(s)
    try:
        (bus,) = get_json(a, "/api/chip")["buses"]
        assert bus["type"] == "usb" and bus["device"].endswith("OTG_FS") and "host_state" in bus["stats"]
    finally:
        a.stop()
        s.close()


# ---- event text -------------------------------------------------------------------------------------------------
def test_compact_bus_event_text():
    from chipem.log import EventLog, format_event
    from chipem.web.evfmt import describe_event
    assert describe_event({"kind": "can", "link": "CANBUS", "node": "CAN0", "dir": "tx", "id": 256, "ext": False,
                           "rtr": False, "dlc": 4, "data": "00CA4B32", "bits": 80, "sof_ns": 2.0e9}) == \
        "CAN0 tx 0x100 [4] 00 CA 4B 32"
    assert describe_event({"kind": "can", "node": "CAN0", "id": 0x1ABCDEF, "ext": True, "rtr": True, "dlc": 2,
                           "data": "", "err": "ack"}) == "CAN0 tx 0x01ABCDEF [2] rtr ERROR ack"
    assert describe_event({"kind": "i2s", "block": "I2S0", "words": list(range(64)), "msg": "I2S0 tx: 64 frames"}) \
        == "I2S0 tx: 64 frames"
    assert describe_event({"kind": "eth", "link": "LAN", "len": 64, "sof_ns": 5.0, "msg": "HOST a > b ARP"}) == \
        "HOST a > b ARP"
    assert describe_event({"kind": "info", "msg": "PA9 → USART1_TX", "pin": "PA9"}) == "PA9 → USART1_TX"
    assert describe_event({"kind": "info", "msg": "core clock 16.000 MHz", "core_hz": 16e6, "src": "HSI"}) == \
        'core clock 16.000 MHz src="HSI"'
    assert describe_event({"kind": "warn", "msg": "late", "delay_ns": 1500.0, "w": list(range(20))}) == \
        "late delay_ns=1.500 µs w=[20 items]"
    log = EventLog()
    assert format_event(log.event("info", 0, msg="PA9 -> USART1_TX", pin="PA9")).endswith("info PA9 -> USART1_TX")
    assert format_event(log.event("i2s", 0, msg="I2S0 tx", words=[1, 2])).endswith("i2s I2S0 tx")


def test_discovery_wave_without_duty():
    from chipem.pinreport import fmt_wave
    assert fmt_wave({"freq_hz": 1000.0, "duty": None}) == "periodic 1 kHz, duty ?"
    assert fmt_wave({"freq_hz": 1000.0, "duty": 25.0}) == "periodic 1 kHz, duty 25.0 %"


def test_pinreport_after_run(app):
    run_ms(app, 30)
    r = get_json(app, "/api/pinreport?static=1")
    assert r["chip"] and r["pins"]
