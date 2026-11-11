"""chipem shell debugger commands (chipem/shell_debug.py) and the web debugger / register browser API
(chipem/web/debugapi.py)."""
from __future__ import annotations

import io
import json
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from chipem import cmd_shell
from chipem.cli import build_parser
from chipem.session import Session

ROOT = Path(__file__).resolve().parent.parent
BLINKY = ROOT / "firmware" / "samples" / "build" / "blinky.elf"
C28_BLINKY = ROOT / "firmware" / "samples_c2000" / "build" / "blinky.c28img"
pytestmark = pytest.mark.skipif(not BLINKY.exists(), reason="sample firmware not built")


def run_exec(cmds: str, fw: Path = BLINKY, *extra: str) -> tuple[int, str]:
    args = build_parser().parse_args(["shell", str(fw), "--exec", cmds, *extra])
    buf = io.StringIO()
    old, sys.stdout = sys.stdout, buf
    try:
        rc = cmd_shell.main(args)
    finally:
        sys.stdout = old
    return rc, buf.getvalue()


def test_shell_break_continue_next_list_info():
    rc, out = run_exec("break main; tbreak blinky.c:16 if r0 == 0x20; watch GPIOC.PDDR; info break; continue; "
                       "list; next; continue; bt; continue; info break; delete; info break; dis 0x4f8 3", BLINKY, "--clock=120e6")
    assert rc == 0, out
    assert "Breakpoint 1 at 0x000004B6: main+0xa (blinky.c:6)" in out
    assert "Temporary breakpoint 2" in out and "Watchpoint 3: GPIOC.PDDR" in out
    assert "breakpoint 1 at main+0xa (blinky.c:6)\n6\t    WDOG_Disable();" in out
    assert "=>B    6      WDOG_Disable();" in out                  # list marks PC and breakpoint
    assert "7\t    SIM_SCGC5 |= SIM_SCGC5_PORTC;" in out            # next
    assert "watchpoint 3: write 0x400FF094 = 0x20 by main+0x" in out
    assert "#0  pc 0x000004" in out                                 # bt (CFI)
    assert "breakpoint 2 at main+0x4e (blinky.c:16)" in out         # the temporary one, then gone
    assert "No breakpoints or watchpoints." in out
    assert "blinky.c:16  GPIOC->PTOR = 1u << 5;" in out              # dis shows source lines
    assert "error" not in out.lower()


def test_shell_finish_until_and_step_into():
    rc, out = run_exec("break WDOG_Disable; continue; finish; until 14; s; ni; info line; info source", BLINKY, "--clock=120e6")
    assert rc == 0, out
    assert "breakpoint 1 at WDOG_Disable" in out
    assert "6\t    WDOG_Disable();" in out                          # back in main after finish
    assert "14\t        if (++counter >= 1000) {" in out
    assert "Line " in out and "blinky.c" in out


@pytest.mark.skipif(not C28_BLINKY.exists(), reason="C28x sample not built")
def test_shell_c28x_symbols_only():
    rc, out = run_exec("break pll; continue; ni; dis pll 3; list; info break", C28_BLINKY, "--chip", "tms320f28232")
    assert rc == 0, out
    assert "Breakpoint 1 at 0x33000A" in out and "breakpoint 1 at pll" in out
    assert "B  33000A:" in out or "B> 33000A:" in out
    assert "(no line info in this image: disassembly)" in out


# ---------------------------------------------------------------------------------------------- web API
@pytest.fixture
def app():
    from chipem.web.server import WebApp
    s = Session("mk22fn1m0vll12", BLINKY, clock_hz=120e6, quiet=True)
    a = WebApp(s, "127.0.0.1", 0).start()
    yield a
    a.stop()
    s.close()


def get(app, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{app.port}{path}", timeout=10) as r:
        return json.loads(r.read())


def post(app, path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"status": e.code, **json.loads(e.read() or b"{}")}


def wait_stopped(app, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if not app.worker.running:
            return
        time.sleep(0.02)
    raise AssertionError("still running")


def test_web_debug_flow(app):
    r = post(app, "/api/debug/break", {"location": "main"})
    assert r["ok"] and r["bps"][0]["label"].startswith("main+0xa")
    assert post(app, "/api/run", {})["ok"]
    wait_stopped(app)
    st = get(app, "/api/debug/state")
    assert st["last_stop"]["bp"] == [r["bps"][0]["id"]] and st["pc"] == 0x4B6 and st["has_dwarf"]
    assert dict(st["regs"])["pc"] == 0x4B6 and st["stack"][0]["func"] == "main"
    lst = get(app, "/api/debug/listing?mode=mixed")
    assert any(x["kind"] == "src" for x in lst["rows"]) and any(x.get("pc") for x in lst["rows"])
    src = get(app, "/api/debug/listing?mode=source")
    assert src["mode"] == "source" and any(x["bp"] for x in src["rows"])
    # step over: a line step runs the Worker until the next debugger stop
    assert post(app, "/api/debug/step", {"kind": "next"})["running"]
    wait_stopped(app)
    st2 = get(app, "/api/debug/state")
    assert "blinky.c:7" in st2["last_stop"]["where"]
    # stepi is done at once
    r = post(app, "/api/debug/step", {"kind": "stepi"})
    assert r["done"] and r["pc"] != st2["pc"]
    # registers / memory edits while paused
    assert post(app, "/api/debug/reg", {"name": "r6", "value": "0x55"})["value"] == 0x55
    assert post(app, "/api/debug/mem", {"addr": "0x20000000", "hex": "01020304"})["bytes"] == 4
    m = get(app, "/api/debug/mem?addr=0x20000000&n=4")
    assert m["hex"] == "01020304" and m["inexact"] == []
    # gutter toggle and breakpoint edits
    t = post(app, "/api/debug/toggle", {"addr": 0x4FA})
    bid = t["added"]["id"]
    assert post(app, "/api/debug/bp", {"id": bid, "cond": "r0 == 0x20", "enabled": False})["bp"]["enabled"] is False
    assert post(app, "/api/debug/toggle", {"addr": 0x4FA})["removed"] == [bid]
    w = post(app, "/api/debug/watch", {"target": "GPIOC.PTOR", "access": "w"})
    assert w["bp"]["kind"] == "watch" and w["bp"]["size"] == 4
    post(app, "/api/run", {})
    wait_stopped(app)
    assert get(app, "/api/debug/state")["last_stop"]["type"] == "watchpoint"
    assert post(app, "/api/debug/bp", {"id": w["bp"]["id"], "delete": True})["ok"]
    # pause-only edits are refused while running
    post(app, "/api/run", {})
    assert post(app, "/api/debug/reg", {"name": "r6", "value": 1}).get("status") == 400
    post(app, "/api/pause", {})


def test_web_register_browser(app):
    tree = get(app, "/api/regs/tree")
    names = [b["name"] for b in tree["blocks"]]
    assert "SIM" in names and "UART0" in names and "SCS" in names
    sim = get(app, "/api/regs/block?name=SIM")
    sopt2 = next(r for r in sim["regs"] if r["name"] == "SOPT2")
    assert any(f["name"] == "CLKOUTSEL" and f["enum"] for f in sopt2["fields"])
    uart = get(app, "/api/regs/block?name=UART0")
    hooked = [r for r in uart["regs"] if not r["exact"]]
    assert hooked and all(r["hook"] for r in hooked)               # stored values of read-hooked registers
    v = get(app, "/api/regs/values?blocks=SIM,GPIOC")
    assert set(v) == {"SIM", "GPIOC"} and v["GPIOC"]["PDDR"][1] is True
    r = post(app, "/api/regs/write", {"block": "GPIOC", "reg": "PDDR", "value": "0x20"})
    assert r["value"] == 0x20
    assert get(app, "/api/regs/values?blocks=GPIOC")["GPIOC"]["PDDR"][0] == 0x20
    r = post(app, "/api/regs/read", {"block": "UART0", "reg": "S1"})
    assert r["side_effects"] is True


def test_static_files_served(app):
    for f in ("debug.js", "regview.js", "debug.css"):
        with urllib.request.urlopen(f"http://127.0.0.1:{app.port}/static/{f}", timeout=10) as r:
            assert r.status == 200 and len(r.read()) > 1000
    with urllib.request.urlopen(f"http://127.0.0.1:{app.port}/", timeout=10) as r:
        html = r.read().decode()
    assert 'id="tabDebug"' in html and 'id="regsPane"' in html and "debug.css" in html
