"""chipem serve: runtime wiring API (/api/net, /api/link, /api/board, /api/board/yaml) on the three-chip demo."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
THREE = ROOT / "boards" / "three_chip_demo.yaml"
FW = [ROOT / "firmware/samples/build/uart_link_sender.elf", ROOT / "firmware/samples_stm32/build/uart_link_echo.elf",
      ROOT / "firmware/samples_c2000/build/uart_link_echo.c28img"]
pytestmark = pytest.mark.skipif(not all(p.exists() for p in FW), reason="sample firmware not built")

from chipem.board import parse_board  # noqa: E402
from chipem.session import Session  # noqa: E402
from chipem.web.server import WebApp  # noqa: E402


@pytest.fixture
def app():
    s = Session.from_board(THREE)
    s.set_view("A")
    a = WebApp(s, "127.0.0.1", 0).start()
    yield a
    a.stop()
    s.close()


def req(app, method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            raw, ctype, code = resp.read(), resp.headers.get("Content-Type", ""), resp.status
    except urllib.error.HTTPError as e:
        raw, ctype, code = e.read(), e.headers.get("Content-Type", ""), e.code
    return code, (json.loads(raw) if "json" in ctype else raw.decode())


def run_ms(app, ms):
    code, r = req(app, "POST", "/api/run", {"ms": ms})
    assert code == 200
    end = time.monotonic() + 60
    while time.monotonic() < end:
        _, st = req(app, "GET", "/api/state")
        if not st["running"] and st["last_stop"]:
            return st
        time.sleep(0.02)
    raise AssertionError("run did not finish")


def test_three_chip_chip_info(app):
    _, c = req(app, "GET", "/api/chip")
    assert [x["id"] for x in c["chips"]] == ["A", "B", "C"]
    assert [x["name"] for x in c["chips"]] == ["MK22FN1M0VLL12", "STM32F405ZG", "TMS320F28232"]
    assert "B.USART1" in c["chips"][1]["uart_ids"] and "C.SCIA" in c["chips"][2]["uart_ids"]
    assert [n["name"] for n in c["nets"]] == ["A_TX", "B_TX", "C_TX", "LED_A", "ACT_C", "SW_B"]
    assert [ln["describe"] for ln in c["links"]] == ["B.USART1 -> A.UART0", "C.SCIA -> A.UART1"]
    assert c["board"]["wiring"] >= 1
    _, st = req(app, "GET", "/api/state")
    assert set(st["chips"]) == {"A", "B", "C"} and {r["chip"] for r in st["pins"]} == {"A", "B", "C"}
    # after a run the firmware has muxed its UART pins: auto links appear in /api/chip and the state
    st = run_ms(app, 8)
    assert st["board"]["wiring"] > c["board"]["wiring"] and st["board"]["skew_ns"] <= st["board"]["quantum_ns"]
    _, c2 = req(app, "GET", "/api/chip")
    autos = [ln for ln in c2["links"] if ln["auto"]]
    assert {ln["describe"] for ln in autos} == {"A.UART0 -> B.USART1", "A.UART0 -> C.SCIA"}
    assert all(ln["net"] == "A_TX" for ln in autos)
    assert next(ln for ln in c2["links"] if ln["describe"] == "A.UART0 -> B.USART1")["stats"]["a_to_b"] > 0


def test_net_api(app):
    code, r = req(app, "POST", "/api/net", {"pins": ["B.PB0", "A.PTC7"]})
    assert code == 200 and r["ok"] and r["net"]["name"] == "NET6" and r["net"]["pins"] == ["B.PB0", "A.PTC7"]
    code, r = req(app, "POST", "/api/net", {"pins": ["A.PTC7", "C.GPIO30"]})            # extends NET5
    assert code == 200 and r["net"]["name"] == "NET6" and len(r["net"]["pins"]) == 3
    code, r = req(app, "POST", "/api/net/rename", {"name": "NET6", "new": "ACT_B"})
    assert code == 200 and r["net"]["name"] == "ACT_B"
    _, b = req(app, "GET", "/api/board")
    assert b["name"] == "three_chip_demo" and [c["id"] for c in b["chips"]] == ["A", "B", "C"]
    assert "ACT_B" in [n["name"] for n in b["nets"]] and "C.SCIA" in b["chips"][2]["uarts"]
    _, st = req(app, "GET", "/api/state")
    assert next(p for p in st["pins"] if p["chip"] == "C" and p["name"].startswith("GPIO30"))["net"] == "ACT_B"
    # errors
    for body in ({"pins": ["PTC5", "B.PA1"]}, {"pins": []}, {"pins": ["A.NOPE", "B.PA1"]}):
        code, r = req(app, "POST", "/api/net", body)
        assert code == 400 and not r["ok"], body
    code, r = req(app, "POST", "/api/net/rename", {"name": "ACT_B", "new": "A_TX"})
    assert code == 400 and "exists" in r["error"]
    # removal: DELETE and POST forms
    code, r = req(app, "DELETE", "/api/net/ACT_B")
    assert code == 200 and r["removed"] == "ACT_B"
    code, r = req(app, "POST", "/api/net/remove", {"name": "LED_A"})
    assert code == 200
    _, c = req(app, "GET", "/api/chip")
    assert [n["name"] for n in c["nets"]] == ["A_TX", "B_TX", "C_TX", "ACT_C", "SW_B"]
    code, r = req(app, "DELETE", "/api/net/ACT_B")
    assert code == 400


def test_link_api_and_yaml_export(app):
    code, r = req(app, "POST", "/api/link", {"from": "A.UART2", "to": "B.USART2", "bidir": True})
    assert code == 200 and r["link"]["describe"] == "A.UART2 <-> B.USART2" and not r["link"]["auto"]
    code, r = req(app, "POST", "/api/link", {"from": "B.USART2", "to": "A.UART2"})
    assert code == 400 and "already" in r["error"]
    code, r = req(app, "POST", "/api/link", {"from": "A.UART2", "to": "B.NOPE"})
    assert code == 400
    _, c = req(app, "GET", "/api/chip")
    assert "A.UART2 <-> B.USART2" in [ln["describe"] for ln in c["links"]]
    code, text = req(app, "GET", "/api/board/yaml")
    assert code == 200 and text.startswith("# board three_chip_demo")
    d = yaml.safe_load(text)
    bd = parse_board(d)
    assert [c.id for c in bd.chips] == ["A", "B", "C"] and all(Path(c.firmware).exists() for c in bd.chips)
    assert [n.name for n in bd.nets] == ["A_TX", "B_TX", "C_TX", "LED_A", "ACT_C", "SW_B"]
    assert [(ln.a, ln.b, ln.bidir) for ln in bd.links] == [
        (("B", "USART1"), ("A", "UART0"), False), (("C", "SCIA"), ("A", "UART1"), False),
        (("A", "UART2"), ("B", "USART2"), True)]
    with urllib.request.urlopen(f"http://127.0.0.1:{app.port}/api/board/yaml?download=1", timeout=20) as resp:
        assert "attachment" in resp.headers.get("Content-Disposition", "")
    code, r = req(app, "POST", "/api/link/remove", {"from": "B.USART2", "to": "A.UART2"})
    assert code == 200 and r["removed"] == "A.UART2 <-> B.USART2"
    code, r = req(app, "DELETE", "/api/link?from=C.SCIA&to=A.UART1")
    assert code == 200
    _, c = req(app, "GET", "/api/chip")
    assert [ln["describe"] for ln in c["links"]] == ["B.USART1 -> A.UART0"]


def test_wiring_api_needs_a_board():
    s = Session("mk22fn1m0vll12", ROOT / "firmware/samples/build/blinky.elf", quiet=True)
    a = WebApp(s, "127.0.0.1", 0).start()
    try:
        code, r = req(a, "POST", "/api/net", {"pins": ["A.PTC5", "A.PTC6"]})
        assert code == 400 and "board" in r["error"]
        code, _ = req(a, "GET", "/api/chip")
        assert code == 200
    finally:
        a.stop()
        s.close()
