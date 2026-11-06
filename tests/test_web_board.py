"""chipem serve --board: whole-board API (two K22s over a UART link)."""
from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BOARD = ROOT / "boards" / "two_k22_uart.yaml"
SAMPLES = ROOT / "firmware" / "samples" / "build"

pytestmark = pytest.mark.skipif(
    not BOARD.exists() or not (SAMPLES / "uart_link_sender.elf").exists()
    or not (SAMPLES / "uart_link_echo.elf").exists(), reason="board file or sample firmware missing")

from chipem import cli  # noqa: E402
from chipem.session import Session  # noqa: E402
from chipem.web.server import WebApp  # noqa: E402


@pytest.fixture
def app():
    # same construction as `chipem serve --board boards/two_k22_uart.yaml --port 0`
    args = cli.build_parser().parse_args(["serve", "--board", str(BOARD), "--port", "0"])
    s = Session.from_args(args)
    a = WebApp(s, "127.0.0.1", 0).start()
    yield a
    a.stop()
    s.close()


def get(app, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{app.port}{path}", timeout=15) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


def get_json(app, path):
    return json.loads(get(app, path)[2])


def post(app, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def run_ms(app, ms):
    code, r = post(app, "/api/run", {"ms": ms})
    assert code == 200 and r["ok"]
    end = time.monotonic() + 30
    while time.monotonic() < end:
        st = get_json(app, "/api/state")
        if not st["running"] and st["last_stop"]:
            return st
        time.sleep(0.02)
    raise AssertionError("run did not finish")


def test_board_chip_info(app):
    c = get_json(app, "/api/chip")
    assert c["board"]["name"] == "two_k22_uart" and c["board"]["quantum_ns"] > 0
    assert [x["id"] for x in c["chips"]] == ["A", "B"]
    for chip in c["chips"]:
        assert chip["pin_count"] == 100 and len(chip["pins"]) == 100
        assert chip["name"] == "MK22FN1M0VLL12" and chip["package"]
        assert "UART0" in chip["uarts"] and f"{chip['id']}.UART0" in chip["uart_ids"]
        p = next(p for p in chip["pins"] if p["num"] == 77)
        assert p["short"] == "PTC5" and p["qname"] == f"{chip['id']}.PTC5" and p["kind"] == "gpio"
    assert len(c["links"]) == 1
    ln = c["links"][0]
    assert ln["type"] == "uart" and ln["from"] == "A.UART0" and ln["to"] == "B.UART0" and "a_to_b" in ln["stats"]
    assert c["nets"] == [{"name": "BTN", "pins": ["A.PTC6", "B.PTC6"], "pull": "up", "drive": None,
                          "level": c["nets"][0]["level"], "conflict": False}]
    # single-chip top-level fields of the view chip are still there
    assert c["view"] == "A" and len(c["pins"]) == 100


def test_board_run_uart_and_state(app):
    st = run_ms(app, 10)
    assert st["last_stop"] == "DONE"
    assert set(st["chips"]) == {"A", "B"}
    for cid in "AB":
        cs = st["chips"][cid]
        assert cs["insns"] > 0 and cs["cycles"] > 0 and "pc" in cs
    assert len(st["pins"]) == 200 and {r["chip"] for r in st["pins"]} == {"A", "B"}
    assert st["board"]["links"][0]["a_to_b"] > 0 and st["board"]["links"][0]["b_to_a"] > 0
    assert st["uart_out_len"]["B.UART0"] > 0
    u = get_json(app, "/api/uart?uart=B.UART0")
    assert "PING" in u["text"] and u["uart"] == "B.UART0"
    assert "ping" in get_json(app, "/api/uart?uart=A.UART0")["text"]
    # uart events carry the chip id; the event buffer stays in sequence order
    evs = get_json(app, "/api/events?since=0&kinds=uart&limit=0")["events"]
    assert {e["chip"] for e in evs} == {"A", "B"}
    seqs = [e["seq"] for e in get_json(app, "/api/events?since=0&limit=0")["events"]]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
    # link stats are live in /api/chip too
    assert get_json(app, "/api/chip")["links"][0]["stats"]["a_to_b"] > 0


def test_board_pin_qualified(app):
    code, r = post(app, "/api/pin", {"name": "B.PTC6", "level": "L"})
    assert code == 200 and r["ok"] and r["name"] == "B.PTC6" and r["chip"] == "B" and r["external"] == "L"
    rows = [p for p in get_json(app, "/api/state")["pins"] if p["chip"] == "B" and p["num"] == 78]
    assert rows and rows[0]["external"] == "L" and rows[0]["net"] == "BTN"
    assert next(r for r in app.session.pin_table(chip="B") if r["num"] == 78)["external"] == "L"
    code, r = post(app, "/api/pin", {"name": "C.PTC6", "level": "L"})
    assert code == 400 and not r["ok"]
    code, r = post(app, "/api/pin", {"name": "B.NOPE", "level": "H"})
    assert code == 400 and not r["ok"]


def test_board_pinreport_per_chip(app):
    run_ms(app, 5)
    rb = get_json(app, "/api/pinreport?chip=B")
    assert rb["chip"] == "B" and "text" in rb and isinstance(rb["pins"], list)
    ra = get_json(app, "/api/pinreport?chip=a&static=0")
    assert ra["chip"] == "A"
    with pytest.raises(urllib.error.HTTPError):
        get(app, "/api/pinreport?chip=Z")


def test_board_page_and_static(app):
    code, ctype, body = get(app, "/")
    assert code == 200 and "text/html" in ctype
    assert b'class="board-canvas"' in body and b'id="chipSel"' in body and b'id="linkMon"' in body
    for f in ("app.js", "package.js", "board.js", "style.css"):
        code, _, js = get(app, f"/static/{f}")
        assert code == 200 and js
    # no vendor GPIO naming pattern in the client
    for f in ("app.js", "package.js", "board.js"):
        assert b"^PT" not in get(app, f"/static/{f}")[2]


def test_board_sse_pins_only_when_changed(app):
    conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=15)
    conn.request("GET", "/api/stream")
    resp = conn.getresponse()
    frames, kind = [], None
    end = time.monotonic() + 5
    while time.monotonic() < end and len(frames) < 2:
        line = resp.fp.readline()
        if not line:
            break
        if line.startswith(b"event: "):
            kind = line[7:].strip().decode()
        elif line.startswith(b"data: ") and kind == "state":
            frames.append(json.loads(line[6:]))
    conn.close()
    assert frames and len(frames[0]["pins"]) == 200 and "board" in frames[0]
    if len(frames) > 1:                       # heartbeat with unchanged pins carries no pin table
        assert "pins" not in frames[1] or frames[1]["pins_version"] != frames[0]["pins_version"]


def test_static_js_parses(tmp_path):
    """The UI has no build step: catch syntax errors in the ES modules (needs node; skipped otherwise)."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    static = ROOT / "chipem" / "web" / "static"
    for f in ("app.js", "package.js", "board.js", "parts.js"):
        dst = tmp_path / (f[:-3] + ".mjs")
        dst.write_bytes((static / f).read_bytes())
        r = subprocess.run([node, "--check", str(dst)], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"{f}: {r.stderr}"
