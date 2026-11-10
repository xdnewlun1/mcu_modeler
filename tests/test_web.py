"""chipem serve: HTTP API, worker thread and SSE stream against the blinky sample."""
from __future__ import annotations

import http.client
import json
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BLINKY = ROOT / "firmware" / "samples" / "build" / "blinky.elf"

pytest.importorskip("chipem.session", reason="chipem/session.py not available yet")
pytestmark = pytest.mark.skipif(not BLINKY.exists(), reason="sample firmware not built")

from chipem.session import Session  # noqa: E402
from chipem.web.server import WebApp  # noqa: E402


@pytest.fixture
def app():
    s = Session("mk22fn1m0vll12", BLINKY)
    a = WebApp(s, "127.0.0.1", 0).start()
    yield a
    a.stop()
    s.close()


def get(app, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{app.port}{path}", timeout=10) as r:
        body = r.read()
        return r.status, r.headers.get("Content-Type", ""), body


def get_json(app, path):
    return json.loads(get(app, path)[2])


def post(app, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.02)
    return cond()


def test_chip_info(app):
    c = get_json(app, "/api/chip")
    assert c["name"] == "MK22FN1M0VLL12"
    assert len(c["pins"]) == 100
    p77 = next(p for p in c["pins"] if p["num"] == 77)
    assert p77["short"] == "PTC5" and p77["port"] == "C" and p77["bit"] == 5
    assert "UART0" in c["uarts"] and c["clock_hz"] > 0


def test_run_advances_time(app):
    t0 = get_json(app, "/api/state")["t_ns"]
    code, r = post(app, "/api/run", {"ms": 5})
    assert code == 200 and r["ok"]
    st = wait_for(lambda: (lambda s: s if not s["running"] and s["t_ns"] > t0 else None)(get_json(app, "/api/state")))
    assert st and st["t_ns"] >= t0 + 5e6 - 1
    assert st["last_stop"] == "DONE"
    assert len(st["pins"]) == 100


def test_free_run_and_pause(app):
    post(app, "/api/run", {})
    assert wait_for(lambda: get_json(app, "/api/state")["t_ns"] > 2e6)
    code, _ = post(app, "/api/pause")
    assert code == 200
    st = wait_for(lambda: (lambda s: s if not s["running"] else None)(get_json(app, "/api/state")))
    t1 = st["t_ns"]
    time.sleep(0.2)
    assert get_json(app, "/api/state")["t_ns"] == t1


def test_pin_external(app):
    code, r = post(app, "/api/pin", {"name": "PTC6", "level": "H"})
    assert code == 200 and r["external"] == "H"
    row = next(p for p in get_json(app, "/api/state")["pins"] if p["short"] == "PTC6")
    assert row["external"] == "H"
    post(app, "/api/pin", {"name": "PTC6", "level": None})
    row = next(p for p in get_json(app, "/api/state")["pins"] if p["short"] == "PTC6")
    assert row["external"] is None
    code, r = post(app, "/api/pin", {"name": "NOPE", "level": "H"})
    assert code == 400 and not r["ok"]


def test_step_reset_uart_adc(app):
    code, r = post(app, "/api/step", {"n": 3})
    assert code == 200 and r["stop"] == "COUNT"
    assert get_json(app, "/api/state")["cycles"] >= 3
    code, r = post(app, "/api/reset")
    assert code == 200
    assert get_json(app, "/api/state")["last_stop"] == "RESET"
    code, r = post(app, "/api/uart", {"uart": "UART0", "text": "hi\\r\\n"})
    assert code == 200 and r["bytes"] == 4
    code, r = post(app, "/api/adc", {"channel": "ADC0_SE14", "value": 2048})
    assert code == 200
    assert get_json(app, "/api/uart?uart=UART0")["uart"] == "UART0"


def test_events_and_static(app):
    post(app, "/api/run", {"ms": 20})
    wait_for(lambda: not get_json(app, "/api/state")["running"])
    ev = get_json(app, "/api/events?since=0")
    assert ev["events"] and ev["events"][0]["kind"] in {"pin", "uart", "irq", "reset", "fault", "warn", "info", "mmio"}
    json.dumps(ev)
    later = get_json(app, f"/api/events?since={ev['last']}")
    assert later["events"] == []
    code, ctype, body = get(app, "/")
    assert code == 200 and "text/html" in ctype and b"<svg" in body
    for f in ("app.js", "package.js", "board.js", "parts.js", "style.css", "layout.css"):
        code, ctype, body = get(app, f"/static/{f}")
        assert code == 200 and body
    with pytest.raises(urllib.error.HTTPError):
        get(app, "/static/../server.py")


def test_sse_stream(app):
    post(app, "/api/run", {})
    conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)
    conn.request("GET", "/api/stream")
    resp = conn.getresponse()
    assert resp.status == 200 and resp.getheader("Content-Type").startswith("text/event-stream")
    states, kind = [], None
    end = time.monotonic() + 10
    while time.monotonic() < end and len(states) < 2:
        line = resp.fp.readline()
        if not line:
            break
        if line.startswith(b"event: "):
            kind = line[7:].strip().decode()
        elif line.startswith(b"data: ") and kind == "state":
            states.append(json.loads(line[6:]))
    conn.close()
    post(app, "/api/pause")
    assert states, "no state frame received"
    assert "t_ns" in states[0] and len(states[0]["pins"]) == 100


def test_blinky_pin_events_via_web(app):
    post(app, "/api/run", {"ms": 50})
    wait_for(lambda: not get_json(app, "/api/state")["running"])
    evs = get_json(app, "/api/events?since=0&kinds=pin&limit=0")["events"]
    lv = [e for e in evs if e.get("pin") == "PTC5" and e.get("field") == "level"]
    if not lv:
        pytest.skip("GPIO model does not report pin events yet")
    assert len(lv) >= 2


# ---------------------------------------------------------------------------------------------------------------
# Board parts (switch, LED, gates, ...): /api/parts/types, /api/part*, parts in /api/board, `part` SSE events.
BOARD2 = ROOT / "boards" / "two_k22_uart.yaml"


@pytest.fixture
def board_app():
    if not BOARD2.exists() or not (ROOT / "firmware" / "samples" / "build" / "uart_link_sender.elf").exists():
        pytest.skip("board sample firmware not built")
    pytest.importorskip("chipem.parts.registry")
    s = Session.from_board(BOARD2)
    a = WebApp(s, "127.0.0.1", 0).start()
    yield a
    a.stop()
    s.close()


def test_part_types(app):
    pytest.importorskip("chipem.parts.registry")
    types_ = get_json(app, "/api/parts/types")
    by = {t["type"]: t for t in types_}
    for t in ("switch", "button", "led", "and", "not", "dff", "pulse"):
        assert t in by, t
    assert by["led"]["ports"] == {"in": "in"} and by["switch"]["ports"] == {"out": "out"}
    assert "inputs" in by["and"]["params"]


def test_part_api(board_app):
    app = board_app
    types_ = {t["type"] for t in get_json(app, "/api/parts/types")}
    assert {"switch", "led", "and"} <= types_
    w0 = get_json(app, "/api/board")["wiring"]
    code, r = post(app, "/api/part", {"id": "SW1", "type": "switch", "params": {"level": "L"}, "ports": {"out": "TST"}})
    assert code == 200 and r["ok"], r
    assert r["part"]["id"] == "SW1" and r["part"]["ports"]["out"] == "TST" and r["part"]["dirs"]["out"] == "out"
    code, r = post(app, "/api/part", {"type": "led", "params": {}, "ports": {"in": "TST"}})    # id auto-assigned
    assert code == 200 and r["part"]["id"] == "LED1"
    code, r = post(app, "/api/part", {"type": "and", "ports": {"in0": "TST", "in1": "A.PTC6", "out": "GOUT"}})
    assert code == 200 and r["part"]["id"] == "U1"
    b = get_json(app, "/api/board")
    assert b["wiring"] > w0
    parts = {p["id"]: p for p in b["parts"]}
    assert set(parts) == {"SW1", "LED1", "U1"}
    assert parts["SW1"]["type"] == "switch" and parts["LED1"]["ports"] == {"in": "TST"}
    assert "state" in parts["SW1"]
    # /api/chip carries the same parts (the UI refetches it on wiring changes)
    assert {p["id"] for p in get_json(app, "/api/chip")["parts"]} == {"SW1", "LED1", "U1"}

    code, r = post(app, "/api/part/set", {"id": "sw1", "values": {"level": "H"}})    # ids are case-insensitive
    assert code == 200 and r["state"]["level"] == "H"
    def pstate(st, pid):
        return next((p["state"] for p in st["board"].get("parts", []) if p["id"] == pid), {})
    st = wait_for(lambda: (lambda s: s if pstate(s, "SW1").get("level") == "H" else None)(get_json(app, "/api/state")))
    assert st, "state.board.parts not updated"
    assert pstate(st, "LED1")["on"] is True                 # zero-delay propagation switch -> net -> LED

    code, r = post(app, "/api/part/port", {"id": "LED1", "port": "in", "to": "GOUT"})
    assert code == 200 and r["net"] == "GOUT"
    assert {p["id"]: p for p in get_json(app, "/api/board")["parts"]}["LED1"]["ports"]["in"] == "GOUT"
    code, r = post(app, "/api/part/port", {"id": "LED1", "port": "in", "to": None})
    assert code == 200 and r["net"] is None

    code, r = post(app, "/api/part/remove", {"id": "U1"})
    assert code == 200 and r["removed"] == "U1"
    assert {p["id"] for p in get_json(app, "/api/board")["parts"]} == {"SW1", "LED1"}

    # errors
    for path, body in [("/api/part", {"type": "flux_capacitor"}), ("/api/part", {"ports": {}}),
                       ("/api/part", {"id": "SW1", "type": "switch"}),
                       ("/api/part/set", {"id": "NOPE", "values": {"level": "H"}}),
                       ("/api/part/set", {"id": "SW1"}), ("/api/part/remove", {"id": "NOPE"}),
                       ("/api/part/port", {"id": "NOPE", "port": "in", "to": "X"}),
                       ("/api/part/port", {"id": "LED1", "port": "zz", "to": "X"})]:
        code, r = post(app, path, body)
        assert code == 400 and not r["ok"], (path, body, r)

    text = get(app, "/api/board/yaml")[2].decode()
    assert "parts:" in text and "SW1" in text


def test_part_events_on_sse(board_app):
    app = board_app
    post(app, "/api/part", {"id": "SW9", "type": "switch", "params": {"level": "L"}, "ports": {"out": "SSE_NET"}})
    conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)
    conn.request("GET", "/api/stream")
    resp = conn.getresponse()
    got, kind = [], None
    time.sleep(0.2)
    post(app, "/api/part/set", {"id": "SW9", "values": {"level": "H"}})
    end = time.monotonic() + 10
    while time.monotonic() < end and not got:
        line = resp.fp.readline()
        if not line:
            break
        if line.startswith(b"event: "):
            kind = line[7:].strip().decode()
        elif line.startswith(b"data: ") and kind == "event":
            ev = json.loads(line[6:])
            if ev.get("kind") == "part" and ev.get("part") == "SW9":
                got.append(ev)
    conn.close()
    assert got, "no part event on the SSE stream"


def test_part_on_single_chip(app):
    """A lone chip becomes a one-chip board on its first part (Session.ensure_board); the UI then sees a board."""
    if not hasattr(app.session, "ensure_board"):
        code, r = post(app, "/api/part", {"type": "led", "ports": {"in": "PTC5"}})
        assert code == 400
        return
    code, r = post(app, "/api/part/set", {"id": "LED1", "values": {"color": "green"}})
    assert code == 400                                      # no board yet: nothing to set
    code, r = post(app, "/api/part", {"type": "led", "params": {"color": "green"}, "ports": {"in": "A.PTC5"}})
    assert code == 200, r
    assert r["part"]["id"] == "LED1" and r["part"]["ports"]["in"]
    c = get_json(app, "/api/chip")
    assert c["board"] and [p["id"] for p in c["parts"]] == ["LED1"] and len(c["chips"]) == 1
    assert get_json(app, "/api/board")["parts"][0]["type"] == "led"
    post(app, "/api/run", {"ms": 30})                       # blinky toggles PTC5: the LED follows
    wait_for(lambda: not get_json(app, "/api/state")["running"])
    st = get_json(app, "/api/state")
    led = next(p for p in st["board"]["parts"] if p["id"] == "LED1")
    assert led["state"]["changes"] >= 1
    evs = get_json(app, "/api/events?since=0&kinds=part&limit=0")["events"]
    assert any(e.get("part") == "LED1" for e in evs)
    seqs = [e["seq"] for e in get_json(app, "/api/events?since=0&limit=0")["events"]]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))     # one numbering across the log switch


# ---- data extraction: filtered / exported events, incremental and downloadable UART output -------------------
def test_events_filter_and_export(app):
    post(app, "/api/run", {"ms": 20})
    wait_for(lambda: not get_json(app, "/api/state")["running"])
    allev = get_json(app, "/api/events?since=0&limit=0")["events"]
    assert allev
    pins = get_json(app, "/api/events?since=0&limit=0&kinds=pin")["events"]
    assert pins and all(e["kind"] == "pin" for e in pins)
    name = pins[0]["pin"]
    # q matches the row text the browser shows and filters (evfmt.event_line), case-insensitively
    from urllib.parse import quote

    from chipem.web.evfmt import event_line
    assert all(e.get("desc") for e in allev)
    q = get_json(app, f"/api/events?since=0&limit=0&q={name.lower()}")["events"]
    assert q and all(name.lower() in event_line(e, False).lower() for e in q)
    assert len(q) < len(allev)
    rx = get_json(app, "/api/events?since=0&limit=0&kinds=pin&q=" + quote(f"re:^nomatch$|\tpin\t{name} level"))["events"]
    assert rx and all(e["pin"] == name and e["field"] == "level" for e in rx)
    # smart case as in the browser: an upper-case letter makes the regex case-sensitive, (?i) overrides
    assert get_json(app, "/api/events?since=0&limit=0&kinds=pin&q=" + quote(f"re:{name.lower()} LEVEL"))["events"] == []
    assert len(get_json(app, "/api/events?since=0&limit=0&kinds=pin&q=" + quote(f"re:(?i){name.lower()} LEVEL"))["events"]) == len(rx)
    assert get_json(app, "/api/events?since=0&limit=0&chip=Z")["events"] == []
    assert len(get_json(app, "/api/events?since=0&limit=0&chip=A")["events"]) == len(allev)
    with pytest.raises(urllib.error.HTTPError) as ei:
        get(app, "/api/events?q=re:(")
    assert ei.value.code == 400
    code, ctype, body = get(app, "/api/events?since=0&kinds=pin&format=csv")
    assert code == 200 and "text/csv" in ctype
    rows = body.decode().splitlines()
    assert rows[0] == "seq,t_ns,chip,kind,text,detail" and len(rows) == len(pins) + 1 and ",pin," in rows[1]
    assert pins[0]["desc"] in rows[1]
    code, ctype, body = get(app, "/api/events?since=0&kinds=pin&format=txt")
    assert code == 200 and "text/plain" in ctype
    lines = body.decode().splitlines()
    assert lines == [event_line(e, False) for e in pins]           # the rows exactly as the view shows them
    assert "\tpin\t" in lines[0] and name in body.decode()
    with urllib.request.urlopen(f"http://127.0.0.1:{app.port}/api/events?format=txt", timeout=10) as r:
        assert "attachment" in r.headers.get("Content-Disposition", "")


@pytest.fixture
def sender_app():
    fw = ROOT / "firmware" / "samples" / "build" / "uart_link_sender.elf"
    if not fw.exists():
        pytest.skip("uart_link_sender sample not built")
    s = Session("mk22fn1m0vll12", fw, clock_hz=120_000_000)
    a = WebApp(s, "127.0.0.1", 0).start()
    yield a
    a.stop()
    s.close()


def test_uart_since_and_txt(sender_app):
    app = sender_app
    post(app, "/api/run", {"ms": 8})
    wait_for(lambda: not get_json(app, "/api/state")["running"])
    full = get_json(app, "/api/uart?uart=UART0")
    assert full["since"] == 0 and full["len"] == len(full["text"].encode("latin-1"))
    n = full["len"]
    assert n > 10 and "ping" in full["text"]
    part = get_json(app, f"/api/uart?uart=UART0&since={n // 2}")
    assert part["since"] == n // 2 and part["text"] == full["text"][n // 2:] and part["len"] == n
    beyond = get_json(app, f"/api/uart?uart=UART0&since={n + 100}")
    assert beyond["since"] == 0 and beyond["text"] == full["text"]       # buffer shorter than offset: start over
    with urllib.request.urlopen(f"http://127.0.0.1:{app.port}/api/uart?uart=UART0&format=txt", timeout=10) as r:
        assert r.status == 200 and "text/plain" in r.headers["Content-Type"]
        assert 'filename="UART0.txt"' in r.headers.get("Content-Disposition", "")
        assert r.read().decode() == full["text"]
    with pytest.raises(urllib.error.HTTPError) as ei:
        get(app, "/api/uart?uart=UART0&format=xml")
    assert ei.value.code == 400


def test_every_data_pane_has_filter_copy_download_collapse():
    """index.html: each data pane (all but the board canvas) has a collapse toggle, a filter box, a copy and a
    download control; the shell stylesheet is linked."""
    from html.parser import HTMLParser

    html = (ROOT / "chipem" / "web" / "static" / "index.html").read_text()
    assert "/static/layout.css" in html

    class P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.panes, self.stack, self.cur, self.btn = {}, [], None, None

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            cls = (a.get("class") or "").split()
            if tag == "section" and "pane" in cls:
                self.cur = a["data-pane"]
                self.panes[self.cur] = {"tog": False, "filter": False, "buttons": []}
                self.stack.append(tag)
            elif tag == "section":
                self.stack.append(tag)
            if self.cur is None:
                return
            if "pane-tog" in cls:
                self.panes[self.cur]["tog"] = True
            if "pane-filter" in cls:
                self.panes[self.cur]["filter"] = True
            if tag == "button":
                self.btn = ""

        def handle_data(self, data):
            if self.btn is not None:
                self.btn += data

        def handle_endtag(self, tag):
            if tag == "button" and self.btn is not None and self.cur is not None:
                self.panes[self.cur]["buttons"].append(self.btn.strip())
                self.btn = None
            if tag == "section" and self.stack:
                self.stack.pop()
                if not self.stack:
                    self.cur = None

    p = P()
    p.feed(html)
    assert {"board", "pins", "uart", "events", "nets", "links", "parts"} <= set(p.panes)
    for name, d in p.panes.items():
        if name == "board":
            continue
        assert d["tog"] and d["filter"], name
        assert "copy" in d["buttons"], (name, d["buttons"])
        assert {"txt", "csv"} & set(d["buttons"]), (name, d["buttons"])


# ---- shared event formatter (chipem/web/evfmt.py), theme, toasts ---------------------------------------------------
def test_event_formatter():
    from chipem.web.evfmt import describe_event, event_line, fmt_time, text_matcher
    assert describe_event({"kind": "pin", "pin": "PTC5", "field": "level", "old": "L", "new": "H"}) == "PTC5 level L → H"
    assert describe_event({"kind": "pin", "pin": "PTC5", "old": None, "new": "H"}) == "PTC5 level — → H"
    assert describe_event({"kind": "uart", "uart": "UART0", "dir": "tx", "byte": 10}) == "UART0 tx 0x0a '\\n'"
    assert describe_event({"kind": "uart", "uart": "UART2", "byte": 0x41}) == "UART2 tx 0x41 'A'"
    assert describe_event({"kind": "uart", "name": "UART0", "dir": "rx", "data": "hi\n"}) == 'UART0 rx "hi\\n"'
    assert describe_event({"kind": "part", "part": "SW1", "port": "out", "level": "H", "n": 2.0}) == "SW1.out level=H n=2"
    assert describe_event({"kind": "info", "msg": "MCG", "core_hz": 120000000.0, "mode": "PEE"}) == \
        'MCG core_hz=120000000 mode="PEE"'
    assert describe_event({"kind": "fault", "msg": "bus fault", "addr": 0x40000000, "pc": -2, "seq": 4}) == "bus fault"
    assert describe_event({"kind": "warn", "addr": 0x4006A000, "value": 3, "block": "UART0"}) == \
        'addr=0x4006A000 value=0x00000003 block="UART0"'
    assert describe_event({"kind": "irq", "irq": 31, "name": "UART0", "enter": True, "w": [1, 0.5]}) == \
        'irq=31 name="UART0" enter=true w=[1,0.5]'
    assert [fmt_time(x) for x in (None, 0, 999.5, 1062.5, 2.5e6, 1.5e9)] == \
        ["—", "0 ns", "1000 ns", "1.063 µs", "2.500 ms", "1.500000 s"]
    e = {"kind": "pin", "t_ns": 2.5e6, "pin": "PTC6", "field": "level", "old": "H", "new": "L", "seq": 3}
    assert event_line(e, False) == "2.500 ms\tpin\tPTC6 level H → L"
    assert event_line({**e, "chip": "B"}, True) == "2.500 ms\tB\tpin\tPTC6 level H → L"
    assert event_line(e, True, "A") == "2.500 ms\tA\tpin\tPTC6 level H → L"
    m = text_matcher("ptc6 LEVEL")
    assert m("PTC6 level") and text_matcher("") is None
    assert text_matcher("re:ptc\\d")("PTC6") and not text_matcher("re:PTc6")("PTC6") and text_matcher("re:(?i)PTc6")("PTC6")
    with pytest.raises(ValueError):
        text_matcher("re:(")


def test_fmt_time_matches_app_js(tmp_path):
    """evfmt.fmt_time is the twin of app.js fmtTime (needs node; skipped otherwise)."""
    import re
    import shutil
    import subprocess
    from chipem.web.evfmt import fmt_time
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    src = (ROOT / "chipem" / "web" / "static" / "app.js").read_text()
    fn = re.search(r"export function fmtTime\(ns\) \{.*?\n\}", src, re.S).group(0).replace("export ", "")
    vals = [0, 0.4, 0.5, 1.5, 999.4, 999.5, 1000, 1062.5, 1234.5678, 999999.9, 1e6, 2.0625e6, 123456789, 1e9,
            1234567890123, 7.0000005e9]
    js = tmp_path / "t.mjs"
    js.write_text(fn + f"\nconsole.log(JSON.stringify({json.dumps(vals)}.map(fmtTime)));\n")
    out = json.loads(subprocess.run([node, str(js)], capture_output=True, text=True, timeout=30, check=True).stdout)
    assert out == [fmt_time(v) for v in vals]


def test_theme_and_tokens_static():
    """Light theme: index.html sets html[data-theme] before the stylesheets load and has the toggle; style.css has a
    light token set covering every dark token; colour literals live only in those two token blocks."""
    import re
    static = ROOT / "chipem" / "web" / "static"
    html = (static / "index.html").read_text()
    head = html[:html.index("</head>")]
    assert head.index('setAttribute("data-theme"') < head.index("/static/style.css")
    assert 'localStorage.getItem("chipem.theme")' in head and "prefers-color-scheme: light" in head
    assert 'id="btnTheme"' in html and '<meta name="color-scheme" content="dark light">' in html
    css = (static / "style.css").read_text()
    dark = re.search(r":root, :root\[data-theme=dark\] \{(.*?)\n\}", css, re.S).group(1)
    light = re.search(r":root\[data-theme=light\] \{(.*?)\n\}", css, re.S).group(1)
    names = lambda block: set(re.findall(r"(--[\w-]+):", block))       # noqa: E731
    assert names(dark) == names(light) and len(names(dark)) > 40
    assert "color-scheme: dark" in dark and "color-scheme: light" in light
    lit = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(")
    rest = css.replace(dark, "").replace(light, "")
    assert not lit.findall(rest), lit.findall(rest)
    for f in ("layout.css", "board.css", "app.js", "board.js", "package.js", "parts.js"):
        assert not lit.findall((static / f).read_text()), f
    assert not lit.findall(re.sub(r'id="[^"]*"', "", html))
    assert ".toast.toast-error" in css and ".owner-power { color: var(--fg-3); }" in css
    assert ".legend" not in css + (static / "layout.css").read_text()       # the old legend row is gone


def test_events_carry_desc_on_stream(app):
    post(app, "/api/run", {"ms": 5})
    wait_for(lambda: not get_json(app, "/api/state")["running"])
    evs = get_json(app, "/api/events?since=0&limit=50")["events"]
    from chipem.web.evfmt import describe_event
    assert evs and all(e["desc"] == describe_event(e) for e in evs)


def test_first_part_does_not_duplicate_earlier_events(app):
    """Adding the first part turns the single chip into a one-chip board whose merged log starts with a copy of the
    chip's events: the event stream must continue after them, not publish them all again."""
    code, r = post(app, "/api/run", {"ms": 1})
    assert code == 200
    wait_for(lambda: get_json(app, "/api/state").get("last_stop") == "DONE")
    before = get_json(app, "/api/events?since=0")["events"]
    assert any(e["kind"] == "reset" for e in before)
    code, r = post(app, "/api/part", {"type": "led", "params": {}, "ports": {"in": "PTC5"}})
    assert code == 200 and r["ok"], r
    post(app, "/api/step", {"n": 1})                       # force a publish after the switch
    after = wait_for(lambda: (lambda ev: ev if len(ev) > len(before) else None)(
        get_json(app, "/api/events?since=0")["events"]))
    assert sum(e["kind"] == "reset" for e in after) == 1
    descs = [e["desc"] for e in after if e["kind"] == "pin"]
    assert len(descs) == len([e for e in before if e["kind"] == "pin"]) + \
        len([e for e in after[len(before):] if e["kind"] == "pin"])


def test_api_input_validation(app):
    """Bad run/step arguments are 400s instead of a frozen UI (NaN in the JSON stream) or a worker stuck for hours."""
    for ms in ("nan", "inf", 1e400, -1, 0):
        code, r = post(app, "/api/run", {"ms": ms})
        assert code == 400 and not r["ok"], (ms, r)
    st = get_json(app, "/api/state")
    assert st["running"] is False and st["run_target_ns"] is None
    for n in (0, -5, 10**9):
        code, r = post(app, "/api/step", {"n": n})
        assert code == 400, (n, r)


def test_pause_cancels_a_queued_run(app):
    """A run requested while the worker is busy, then a pause: the run must not start afterwards."""
    import threading
    w = app.worker
    gate = threading.Event()
    w.q.put((lambda: gate.wait(5), __import__("concurrent.futures").futures.Future()))    # keep the worker busy
    t = threading.Thread(target=lambda: post(app, "/api/run", {}))
    t.start()
    time.sleep(0.2)                                          # the run closure is queued behind the busy one
    w.pause()
    gate.set()
    t.join(10)
    time.sleep(0.2)
    assert get_json(app, "/api/state")["running"] is False


def test_http_edge_cases(app):
    import socket
    # HEAD on the event stream: headers only, the connection does not stream
    c = http.client.HTTPConnection("127.0.0.1", app.port, timeout=5)
    c.request("HEAD", "/api/stream")
    r = c.getresponse()
    assert r.status == 200 and r.read() == b""
    c.close()
    # chunked bodies are refused (they used to be ignored and parsed as the next request)
    s = socket.create_connection(("127.0.0.1", app.port), timeout=5)
    s.sendall(b"POST /api/run HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n8\r\n{\"ms\":1}\r\n0\r\n\r\n")
    assert b" 400 " in s.recv(4096).split(b"\r\n")[0]
    s.close()


def test_board_editor_add_remove_save_open(app, tmp_path, monkeypatch):
    """Board dialog API: catalog, add an MCU (session rebuilt, new boot id), wire it, save to boards/, open it
    again, remove the MCU, and errors leave the running session untouched."""
    from chipem import boardedit
    monkeypatch.setattr(boardedit, "BOARDS_DIR", tmp_path)
    cat = get_json(app, "/api/catalog")
    assert any(c["id"] == "stm32f405zg" for c in cat["chips"]) and any(f["name"] == "blinky.elf" for f in cat["firmware"])
    boot0 = get_json(app, "/api/chip")["boot"]
    stm_fw = next(f["path"] for f in cat["firmware"] if f["dir"] == "samples_stm32" and f["name"] == "blinky.elf")
    code, r = post(app, "/api/board/chip/add", {"chip": "stm32f405zg", "firmware": stm_fw})
    assert code == 200 and r["id"] == "B" and r["chips"] == ["A", "B"], r
    info = get_json(app, "/api/chip")
    assert info["boot"] != boot0 and [c["id"] for c in info["chips"]] == ["A", "B"]
    code, r = post(app, "/api/net", {"pins": ["A.PTC5", "B.PA0"], "name": "LNK"})
    assert code == 200, r
    code, r = post(app, "/api/board/save", {"name": "edit_test"})
    assert code == 200 and (tmp_path / "edit_test.yaml").exists(), r
    assert post(app, "/api/board/save", {"name": "edit_test"})[0] == 409            # no silent overwrite
    assert post(app, "/api/board/save", {"name": "../evil"})[0] == 400
    code, r = post(app, "/api/board/chip/remove", {"id": "B"})
    assert code == 200 and r["chips"] == ["A"]
    assert not get_json(app, "/api/board")["nets"] or all("B." not in " ".join(n["pins"])
                                                           for n in get_json(app, "/api/board")["nets"])
    code, r = post(app, "/api/board/open", {"path": str(tmp_path / "edit_test.yaml")})
    assert code == 200 and r["chips"] == ["A", "B"], r
    nets = get_json(app, "/api/board")["nets"]
    assert any(n["name"] == "LNK" and set(n["pins"]) == {"A.PTC5", "B.PA0"} for n in nets)
    boot1 = get_json(app, "/api/chip")["boot"]
    code, r = post(app, "/api/board/chip/add", {"chip": "no_such_chip"})
    assert code == 400 and get_json(app, "/api/chip")["boot"] == boot1              # untouched on error
    code, r = post(app, "/api/run", {"ms": 1})
    assert code == 200
    assert wait_for(lambda: get_json(app, "/api/state").get("last_stop") == "DONE")


def test_event_buffer_reserves_room_for_non_pin_events():
    """A 20 kHz PWM fills the pin ring, but reset/info/uart/warn events stay (per-kind cap); iteration is in seq
    order."""
    from chipem.web.server import EventBuffer
    b = EventBuffer(total=100, pin_cap=60)
    seq = 0

    def add(kind, **f):
        nonlocal seq
        seq += 1
        b.append({"kind": kind, "seq": seq, **f})
    add("reset", reason="por")
    add("info", msg="PLL locked")
    for i in range(10_000):
        add("pin", pin="PA6", field="level")
        if i == 5000:
            add("uart", uart="USART1", byte=0x41)
    assert len(b) == 63
    kinds = [e["kind"] for e in b]
    assert kinds[:3] == ["reset", "info", "uart"] and kinds.count("pin") == 60
    seqs = [e["seq"] for e in b]
    assert seqs == sorted(seqs)
    b.clear()
    assert len(b) == 0 and list(b) == []


def test_wave_event_text():
    from chipem.web.evfmt import describe_event
    d = describe_event({"kind": "pin", "pin": "PA6", "field": "wave", "new": "L", "edges": 4000, "since_ns": 1.5e6,
                        "freq_hz": 20000.0, "duty": 25.0})
    assert d == "PA6 wave 4000 edges since 1.500 ms, 20.000 kHz, duty 25.0 %, now L"


EPWM_IMG = ROOT / "firmware" / "samples_c2000" / "build" / "epwm.c28img"


@pytest.mark.skipif(not EPWM_IMG.exists(), reason="C2000 samples not built")
def test_pwm_pin_wave_in_state_and_events_bounded():
    """ePWM sample (20 kHz on GPIO0/GPIO1): the pin rows carry wave {edges, freq_hz, duty}; the event list keeps the
    reset/info events and holds a bounded number of pin events (EventLog burst + wave summaries)."""
    s = Session("tms320f28232", EPWM_IMG)
    a = WebApp(s, "127.0.0.1", 0).start()
    try:
        assert post(a, "/api/run", {"ms": 300})[0] == 200
        assert wait_for(lambda: get_json(a, "/api/state").get("last_stop") == "DONE", 60)
        st = get_json(a, "/api/state")
        row = next(p for p in st["pins"] if p["short"] == "GPIO0")
        w = row["wave"]
        assert abs(w["freq_hz"] - 20e3) < 1 and w["duty"] == 29.0 and w["edges"] > 10_000
        evs = get_json(a, "/api/events?limit=0")
        evs = evs["events"] if isinstance(evs, dict) else evs
        kinds = [e["kind"] for e in evs]
        assert "reset" in kinds and "info" in kinds
        assert kinds.count("pin") < 4000                         # 300 ms of 2 x 20 kHz = 24,000 edges
        waves = [e for e in evs if e.get("field") == "wave"]
        assert waves and "wave" in waves[0]["desc"] and "kHz" in waves[0]["desc"]
    finally:
        a.stop()
        s.close()


def test_api_adc_codes_and_volts(app):
    """POST /api/adc: JSON int / "2048" = raw code, "1.2V" / "1200mV" / JSON float = volts (session.parse_adc_value);
    a raw code for a volts-only comparator input is a 400 with the V/mV hint."""
    assert post(app, "/api/adc", {"channel": "ADC0_SE14", "value": "0x800"}) == (200, {"ok": True, "channel": "ADC0_SE14", "value": 2048})
    code, r = post(app, "/api/adc", {"channel": "CMP0_IN0", "value": "1200mV"})
    assert code == 200 and r["value"] == 1.2
    assert post(app, "/api/adc", {"channel": "CMP0_IN1", "value": 0.5})[0] == 200
    code, r = post(app, "/api/adc", {"channel": "CMP0_IN0", "value": 1200})
    assert code == 400 and "volts" in str(r)
    code, r = post(app, "/api/adc", {"channel": "ADC0_SE14", "value": "1.65V"})
    assert code == 400 and "raw" in str(r)
