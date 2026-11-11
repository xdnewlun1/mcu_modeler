"""Web UI round 5: static ETag caching, CPU register metadata, UI driver script (tools/ui_shots.py) pieces."""
from __future__ import annotations

import http.client
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BLINKY = ROOT / "firmware" / "samples" / "build" / "blinky.elf"
STATIC = ROOT / "chipem" / "web" / "static"

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


def _req(app, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, dict(r.getheaders()), body


def test_static_etag_revalidation(app):
    st, h, body = _req(app, "/static/app.js")
    assert st == 200 and body
    assert h["Cache-Control"] == "no-cache" and h["ETag"].startswith('"')
    st2, h2, body2 = _req(app, "/static/app.js", {"If-None-Match": h["ETag"]})
    assert st2 == 304 and body2 == b"" and h2["ETag"] == h["ETag"]
    st3, _, body3 = _req(app, "/static/app.js", {"If-None-Match": '"stale"'})
    assert st3 == 200 and body3 == body
    st4, h4, _ = _req(app, "/")
    assert st4 == 200 and "ETag" in h4
    # API answers stay uncached
    _, ha, _ = _req(app, "/api/chip")
    assert ha["Cache-Control"] == "no-store"


def test_static_etag_changes_with_content(tmp_path):
    from chipem.web import server
    p = tmp_path / "x.js"
    p.write_text("a")
    b1, e1 = server._static_file(p)
    p.write_text("bb")
    b2, e2 = server._static_file(p)
    assert (b1, b2) == (b"a", b"bb") and e1 != e2


def test_reg_meta_in_chip_info(app):
    st, _, body = _req(app, "/api/chip")
    info = json.loads(body)
    meta = (info.get("chips") or [info])[0]["reg_meta"]
    assert meta["pc"] == {"bits": 32, "fmt": "addr"} and meta["r0"]["bits"] == 32


def test_reg_meta_tables():
    from chipem.regmeta import cpu_reg_meta

    class C:
        arch = None

    c = C()
    c.arch_kind = "mcs51"
    m = cpu_reg_meta(c)
    assert m["a"]["bits"] == 8 and m["dptr"]["bits"] == 16 and m["cy"]["fmt"] == "flag" and m["p"]["fmt"] == "flag"
    c.arch_kind = "c28x"
    m = cpu_reg_meta(c)
    assert m["pc"]["bits"] == 22 and m["st0"]["bits"] == 16 and m["intm"]["fmt"] == "flag"

    class A:
        def reg_meta(self):
            return {"x": {"bits": 4, "fmt": "hex"}}
    c.arch = A()
    assert cpu_reg_meta(c) == {"x": {"bits": 4, "fmt": "hex"}}


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_fmt_reg_widths():
    js = ("import('" + (STATIC / "fmt.js").as_uri() + "').then(m => console.log(JSON.stringify(["
          "m.fmtReg(42, {bits: 8, fmt: 'hex'}), m.fmtReg(0x3F0012, {bits: 22, fmt: 'addr'}), m.fmtReg(1, {bits: 1, fmt: 'flag'}),"
          "m.fmtReg(0xFFFFFFFF, null), m.fmtReg(-1, {bits: 16, fmt: 'hex'})])))")
    out = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, timeout=30)
    assert json.loads(out.stdout) == ["0x2A", "0x3F0012", "1", "0xFFFFFFFF", "0xFFFF"]


def test_no_arch_checks_in_generic_ui():
    """Register formatting is data-driven: no per-architecture register-name lists in app.js."""
    src = (STATIC / "app.js").read_text()
    assert "psw|dptr" not in src and "xar\\d|acc" not in src


def test_ui_shots_websocket_frames():
    """The stdlib WebSocket client of tools/ui_shots.py frames and unmasks correctly (loopback socket pair)."""
    import socket
    import sys
    sys.path.insert(0, str(ROOT))
    from tools.ui_shots import WebSocket
    a, b = socket.socketpair()
    ws = WebSocket.__new__(WebSocket)
    ws.sock, ws._buf = a, b""
    ws.send("x" * 300)
    raw = b.recv(1000)
    assert raw[0] == 0x81 and raw[1] == 0x80 | 126 and len(raw) == 4 + 4 + 300
    # server -> client: unmasked, fragmented text + a ping in between
    b.sendall(bytes([0x01, 3]) + b"abc" + bytes([0x89, 0]) + bytes([0x80, 2]) + b"de")
    assert ws.recv() == "abcde"
    pong = b.recv(100)
    assert pong[0] == 0x8A
    a.close()
    b.close()


def test_static_ui_round5_pieces():
    app = (STATIC / "app.js").read_text()
    assert "window.confirm(" not in app and "askConfirm(" in app            # themed consent dialog
    assert 'id="askDlg"' in (STATIC / "index.html").read_text()
    assert "lodKeep()" in (STATIC / "package.js").read_text()                # names of pins that matter at fit
    assert 'data-z="chip"' in (STATIC / "board.js").read_text()              # zoom to the selected chip
    dbg = (STATIC / "debug.js").read_text()
    for api in ("/api/debug/locals", "/api/debug/eval", "/api/debug/globals"):
        assert api in dbg
    assert 'arch === "c28x"' not in dbg                                      # widths from reg_meta, not arch checks
