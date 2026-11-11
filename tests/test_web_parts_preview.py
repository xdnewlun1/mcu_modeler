"""Part previews in the web UI: GET /api/part/frame (camera, LCD panel) and /api/part/samples (I2S codec), and the
`views` the parts announce next to their state (chipem/web/server.py part_views)."""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

from chipem import cli  # noqa: E402
from chipem.session import Session  # noqa: E402
from chipem.web.server import WebApp  # noqa: E402


def _start(board: str):
    p = ROOT / board
    if not p.exists():
        pytest.skip(f"{board} missing")
    args = cli.build_parser().parse_args(["serve", "--board", str(p), "--port", "0"])
    try:
        s = Session.from_args(args)
    except (FileNotFoundError, OSError) as e:
        pytest.skip(f"firmware not built: {e}")
    return s, WebApp(s, "127.0.0.1", 0).start()


@pytest.fixture
def served():
    made = []

    def make(board):
        s, a = _start(board)
        made.append((a, s))
        return a
    yield make
    for a, s in made:
        a.stop()
        s.close()


def get(app, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{app.port}{path}", timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def post(app, path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def run(app, ms):
    post(app, "/api/run", {"ms": ms})
    end = time.monotonic() + 60
    while time.monotonic() < end:
        st = get(app, "/api/state")[1]
        if not (st.get("state", st)).get("running"):
            return
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_camera_frame_endpoint_and_views(served):
    app = served("boards/stm32f407_dcmi_camera.yaml")
    st, info = get(app, "/api/chip")
    cam = next(p for p in info["parts"] if p["type"] == "camera")
    assert cam["views"]["frame"]["w"] == cam["params"]["width"] and cam["views"]["frame"]["seq"] == 0
    run(app, 40)
    st, r = get(app, f"/api/part/frame?id={cam['id']}")
    assert st == 200 and r["format"] == "rgb" and r["id"] == cam["id"]
    data = base64.b64decode(r["data"])
    assert len(data) == r["w"] * r["h"] * 3 and (r["src_w"], r["src_h"]) == (r["w"], r["h"])
    # bars pattern: left column brighter than the right one, grey (R == G == B)
    assert data[0] == data[1] == data[2] and data[0] > data[3 * (r["w"] - 1)]
    assert r["seq"] >= 1
    # the stream state carries the views with the frame counter
    st, s = get(app, "/api/state")
    parts = (s.get("state", s))["board"]["parts"]
    assert next(p for p in parts if p["id"] == cam["id"])["views"]["frame"]["seq"] == r["seq"]
    # downscaling
    st, r2 = get(app, f"/api/part/frame?id={cam['id']}&max_w=20")
    assert r2["w"] <= 20 and len(base64.b64decode(r2["data"])) == r2["w"] * r2["h"] * 3
    # errors: unknown part, part without a frame / samples
    assert get(app, "/api/part/frame?id=NOPE")[0] == 400
    assert get(app, f"/api/part/samples?id={cam['id']}")[0] == 400


def test_codec_samples_endpoint(served):
    app = served("boards/stm32_i2s_codec.yaml")
    st, info = get(app, "/api/chip")
    codec = next(p for p in info["parts"] if p["type"] == "i2s_codec")
    assert "samples" in codec["views"] and "frame" not in codec["views"]
    run(app, 20)
    st, r = get(app, f"/api/part/samples?id={codec['id']}&n=64")
    assert st == 200 and r["channels"] == 2 and r["bits"] == 16
    assert len(r["samples"]) == min(64, r["kept"]) and r["received"] >= r["kept"]
    assert all(len(f) == 2 for f in r["samples"])
    assert get(app, f"/api/part/frame?id={codec['id']}")[0] == 400


def test_lcd_panel_frame_view_downscales():
    from chipem.parts.lcd_panel import LcdPanel
    p = LcdPanel.__new__(LcdPanel)
    p.width, p.height, p.frames = 640, 4, 3
    p.image = bytes(range(256)) * (640 * 4 * 3 // 256)
    v = p.frame_view(320)
    assert (v["w"], v["h"], v["src_w"], v["seq"]) == (320, 2, 640, 3) and len(v["data"]) == 320 * 2 * 3
    assert v["data"][:3] == p.image[:3] and v["data"][3:6] == p.image[6:9]
    assert p.frame_info() == {"w": 640, "h": 4, "seq": 3, "format": "rgb"}
