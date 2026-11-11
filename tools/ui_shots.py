#!/usr/bin/env python3
"""Drive the chipem web UI in a real headless browser: screenshots, console errors, failed requests.

Uses Firefox's built-in WebDriver BiDi endpoint (``firefox --remote-debugging-port``) over a small stdlib-only
WebSocket client: no geckodriver, no Selenium, no pip packages. Starts its own ``chipem serve --port 0`` (never
touches a server you are running), waits until the page's event stream is live (header says ``live`` and the
status badge left ``CONNECTING``), runs a scripted list of steps and writes PNGs plus ``report.json``.

Command line (from the checkout)::

    .venv/bin/python tools/ui_shots.py --board boards/three_chip_demo.yaml --out /tmp/shots \\
        --size 1366x768 --size 1920x1080 --theme dark --theme light
    .venv/bin/python tools/ui_shots.py --fw firmware/samples/build/blinky.elf --steps steps.json --out /tmp/shots

``--steps`` is a JSON list; each step is an object with ``do`` and arguments:

    {"do": "shot", "name": "main"}                     screenshot <out>/<tag>_<name>.png (tag = board_WxH_theme)
    {"do": "click", "sel": "#tabDebug"}                real pointer click on the element's centre
    {"do": "click", "sel": "#pkg", "x": 10, "y": 20}   ... at an offset from the element's top-left corner
    {"do": "dblclick", "sel": "..."}
    {"do": "hover", "sel": "..."}
    {"do": "key", "key": "F10", "mods": ["Shift"]}      real key event (to the focused element)
    {"do": "type", "sel": "#runFor", "text": "5ms"}     focus, select all, type text (use "\\n" for Enter)
    {"do": "eval", "js": "document.title"}              result stored in the report
    {"do": "wait", "ms": 500}
    {"do": "waitfor", "js": "!!document.querySelector('.x')", "timeout": 10}
    {"do": "api", "method": "POST", "path": "/api/pause", "body": {}}   call the server directly
    {"do": "reload"}  /  {"do": "stop_server"}  /  {"do": "start_server"}  (same port, for the offline check)

Python use (tests, ad-hoc sessions)::

    from tools.ui_shots import UiSession
    with UiSession(["--board", "boards/logic_demo.yaml"], size=(1366, 768), theme="dark") as ui:
        ui.wait_live(); ui.click("#tabDebug"); ui.shot("/tmp/x.png")
        print(ui.console_errors, ui.failed_requests)

Every console message of level error/warn, every uncaught exception and every HTTP response >= 400 (or network
failure) is collected; ``report.json`` lists them per run. Exit status is 1 when any run collected errors.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python") if (ROOT / ".venv" / "bin" / "python").exists() else sys.executable

KEYS = {  # WebDriver key codes
    "Enter": "", "Escape": "", "Esc": "", "Tab": "", "Backspace": "",
    "Space": " ", "Delete": "", "Home": "", "End": "", "PageUp": "", "PageDown": "",
    "ArrowLeft": "", "ArrowUp": "", "ArrowRight": "", "ArrowDown": "",
    "Shift": "", "Control": "", "Ctrl": "", "Alt": "", "Meta": "",
    **{f"F{i}": chr(0xE031 + i - 1) for i in range(1, 13)},
}


# --------------------------------------------------------------------------------------------- WebSocket client
class WebSocket:
    """Minimal RFC 6455 client (text frames, masking, fragmentation, ping/pong); enough for WebDriver BiDi."""

    def __init__(self, host: str, port: int, path: str, timeout: float = 30.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake: connection closed")
            buf += chunk
        head, self._buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"websocket handshake failed: {head[:200]!r}")

    def send(self, text: str) -> None:
        data = text.encode()
        hdr = bytearray([0x81])
        n = len(data)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        self.sock.sendall(bytes(hdr) + bytes(b ^ mask[i & 3] for i, b in enumerate(data)))

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise ConnectionError("websocket closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self) -> str:
        parts = []
        while True:
            b0, b1 = self._read(2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
            if op == 0x9:                                   # ping -> pong
                self._control(0xA, data)
                continue
            if op == 0x8:
                raise ConnectionError("websocket closed by peer")
            if op in (0x1, 0x2, 0x0):
                parts.append(data)
                if b0 & 0x80:
                    return b"".join(parts).decode()

    def _control(self, op: int, data: bytes) -> None:
        mask = os.urandom(4)
        self.sock.sendall(bytes([0x80 | op, 0x80 | len(data)]) + mask
                          + bytes(b ^ mask[i & 3] for i, b in enumerate(data)))

    def close(self) -> None:
        try:
            self._control(0x8, b"")
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------------------------- BiDi browser
class Browser:
    """Headless Firefox driven over WebDriver BiDi."""

    def __init__(self, width: int = 1366, height: int = 768, binary: str | None = None, prefs: dict | None = None):
        self.binary = binary or os.environ.get("CHIPEM_UI_BROWSER") or shutil.which("firefox")
        if not self.binary:
            raise RuntimeError("firefox not found (set CHIPEM_UI_BROWSER)")
        self.profile = tempfile.mkdtemp(prefix="chipem-ui-ff-")
        all_prefs = {
            "browser.shell.checkDefaultBrowser": False, "datareporting.policy.dataSubmissionEnabled": False,
            "toolkit.telemetry.reportingpolicy.firstRun": False, "browser.startup.homepage_override.mstone": "ignore",
            "app.update.disabledForTesting": True, "browser.aboutwelcome.enabled": False,
            "network.proxy.type": 0, "dom.disable_beforeunload": True, "ui.prefersReducedMotion": 1,
            **(prefs or {}),
        }
        with open(os.path.join(self.profile, "user.js"), "w") as f:
            for k, v in all_prefs.items():
                f.write(f"user_pref({json.dumps(k)}, {json.dumps(v)});\n")
        port = _free_port()
        self.proc = subprocess.Popen(
            [self.binary, "--headless", "--no-remote", "--profile", self.profile, f"--remote-debugging-port={port}",
             f"--width={width}", f"--height={height}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.time() + 30
        while True:
            try:
                self.ws = WebSocket("127.0.0.1", port, "/session")
                break
            except (OSError, ConnectionError):
                if time.time() > deadline or self.proc.poll() is not None:
                    self.close()
                    raise RuntimeError("firefox did not open its remote debugging port")
                time.sleep(0.2)
        self._id = 0
        self.events: list[dict] = []
        self.cmd("session.new", {"capabilities": {"alwaysMatch": {"acceptInsecureCerts": True,
                                                                   "unhandledPromptBehavior": {"default": "accept"}}}})
        tree = self.cmd("browsingContext.getTree", {})
        self.ctx = tree["contexts"][0]["context"]
        self.set_viewport(width, height)
        self.cmd("session.subscribe", {"events": ["log.entryAdded", "network.responseCompleted",
                                                  "network.fetchError", "browsingContext.userPromptOpened"]})

    def cmd(self, method: str, params: dict, timeout: float = 60.0) -> dict:
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        self.ws.sock.settimeout(timeout)
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("type") == "event" or ("method" in msg and "id" not in msg):
                self._on_event(msg)
                continue
            if msg.get("id") == mid:
                if msg.get("type") == "error" or "error" in msg and msg.get("type") != "success":
                    raise RuntimeError(f"{method}: {msg.get('error')}: {msg.get('message')}")
                return msg.get("result", {})

    def _on_event(self, msg: dict) -> None:
        self.events.append(msg)
        if msg.get("method") == "browsingContext.userPromptOpened":
            try:
                self.ws.send(json.dumps({"id": 0, "method": "browsingContext.handleUserPrompt",
                                         "params": {"context": msg["params"]["context"], "accept": True}}))
            except OSError:
                pass

    def pump(self, seconds: float) -> None:
        """Let the page run, collecting events."""
        end = time.time() + seconds
        while True:
            left = end - time.time()
            if left <= 0:
                return
            self.ws.sock.settimeout(left)
            try:
                msg = json.loads(self.ws.recv())
            except (TimeoutError, socket.timeout):
                return
            if "id" not in msg or msg.get("type") == "event":
                self._on_event(msg)

    def set_viewport(self, w: int, h: int) -> None:
        self.cmd("browsingContext.setViewport", {"context": self.ctx, "viewport": {"width": w, "height": h}})

    def navigate(self, url: str) -> None:
        self.cmd("browsingContext.navigate", {"context": self.ctx, "url": url, "wait": "complete"})

    def reload(self) -> None:
        self.cmd("browsingContext.reload", {"context": self.ctx, "wait": "complete", "ignoreCache": False})

    def js(self, expr: str, await_promise: bool = True) -> Any:
        r = self.cmd("script.evaluate", {"expression": expr, "target": {"context": self.ctx},
                                         "awaitPromise": await_promise, "resultOwnership": "none",
                                         "serializationOptions": {"maxObjectDepth": 6}})
        if r.get("type") == "exception":
            d = r.get("exceptionDetails", {})
            raise RuntimeError(f"js: {d.get('text')}")
        return _deser(r.get("result"))

    def screenshot(self, path: str | os.PathLike) -> None:
        r = self.cmd("browsingContext.captureScreenshot", {"context": self.ctx, "origin": "viewport"})
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(base64.b64decode(r["data"]))

    def rect(self, sel: str) -> dict:
        r = self.js(f"""(() => {{ const e = document.querySelector({json.dumps(sel)});
            if (!e) return null; e.scrollIntoView({{block: 'nearest', inline: 'nearest'}});
            const r = e.getBoundingClientRect(); return {{x: r.left, y: r.top, w: r.width, h: r.height}}; }})()""")
        if not r:
            raise RuntimeError(f"no element {sel!r}")
        return r

    def pointer(self, x: float, y: float, clicks: int = 1, button: int = 0, mods: list[str] | None = None,
                move_only: bool = False) -> None:
        acts: list[dict] = [{"type": "pointerMove", "x": int(x), "y": int(y), "duration": 0}]
        if not move_only:
            for _ in range(clicks):
                acts += [{"type": "pointerDown", "button": button}, {"type": "pointerUp", "button": button}]
        sources: list[dict] = [{"type": "pointer", "id": "mouse", "parameters": {"pointerType": "mouse"},
                                "actions": acts}]
        if mods:
            kd = [{"type": "keyDown", "value": KEYS[m]} for m in mods]
            ku = [{"type": "keyUp", "value": KEYS[m]} for m in mods]
            pad = [{"type": "pause", "duration": 0}] * (len(acts))
            sources.insert(0, {"type": "key", "id": "kbm", "actions": kd + pad + ku})
            sources[1]["actions"] = [{"type": "pause", "duration": 0}] * len(kd) + acts
        self.cmd("input.performActions", {"context": self.ctx, "actions": sources})

    def keys(self, key: str, mods: list[str] | None = None) -> None:
        v = KEYS.get(key, key)
        down = [{"type": "keyDown", "value": KEYS[m]} for m in (mods or [])]
        up = [{"type": "keyUp", "value": KEYS[m]} for m in reversed(mods or [])]
        self.cmd("input.performActions", {"context": self.ctx, "actions": [
            {"type": "key", "id": "kb", "actions": down + [{"type": "keyDown", "value": v},
                                                          {"type": "keyUp", "value": v}] + up}]})

    def type_text(self, text: str) -> None:
        acts = []
        for ch in text:
            v = KEYS["Enter"] if ch == "\n" else ch
            acts += [{"type": "keyDown", "value": v}, {"type": "keyUp", "value": v}]
        self.cmd("input.performActions", {"context": self.ctx, "actions": [{"type": "key", "id": "kb",
                                                                              "actions": acts}]})

    def close(self) -> None:
        try:
            if getattr(self, "ws", None):
                try:
                    self.cmd("browser.close", {}, timeout=5)
                except Exception:
                    pass
                self.ws.close()
        finally:
            if self.proc.poll() is None:
                try:
                    os.killpg(self.proc.pid, signal.SIGTERM)
                    self.proc.wait(5)
                except Exception:
                    try:
                        os.killpg(self.proc.pid, signal.SIGKILL)
                    except Exception:
                        pass
            shutil.rmtree(self.profile, ignore_errors=True)


def _deser(v: Any) -> Any:
    """BiDi RemoteValue -> Python."""
    if not isinstance(v, dict) or "type" not in v:
        return v
    t = v["type"]
    if t in ("undefined", "null"):
        return None
    if t in ("string", "boolean"):
        return v["value"]
    if t == "number":
        x = v["value"]
        return x if not isinstance(x, str) else float(x.replace("Infinity", "inf")) if "Inf" in x else None
    if t == "array":
        return [_deser(x) for x in v.get("value", [])]
    if t == "object":
        return {(k if isinstance(k, str) else str(_deser(k))): _deser(x) for k, x in v.get("value", [])}
    return v.get("value", f"<{t}>")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# --------------------------------------------------------------------------------------------- server + session
class Server:
    """`chipem serve --port P` on 127.0.0.1 in its own process (port 0 = pick a free one)."""

    def __init__(self, args: list[str], port: int = 0, log_path: str | None = None):
        self.args, self.port = list(args), port or _free_port()
        self.log_path = log_path or os.path.join(tempfile.gettempdir(), f"chipem-ui-serve-{self.port}.log")
        self.proc: subprocess.Popen | None = None

    def start(self) -> "Server":
        logf = open(self.log_path, "ab")
        self.proc = subprocess.Popen([PY, "-m", "chipem.cli", "serve", *self.args, "--port", str(self.port)],
                                     cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.time() + 60
        import urllib.request
        while True:
            try:
                urllib.request.urlopen(f"{self.url}/api/chip", timeout=2).read()
                return self
            except Exception:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"chipem serve exited; see {self.log_path}")
                if time.time() > deadline:
                    raise RuntimeError(f"chipem serve did not come up; see {self.log_path}")
                time.sleep(0.2)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def api(self, method: str, path: str, body: Any = None) -> Any:
        import urllib.request
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode(errors="replace")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(10)
            except Exception:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except Exception:
                    pass
        self.proc = None


class UiSession:
    """A served session (own `chipem serve --port 0`) opened in headless Firefox."""

    def __init__(self, serve_args: list[str], size: tuple[int, int] = (1366, 768), theme: str | None = "dark",
                 local_storage: dict | None = None, server: Server | None = None):
        self.server = server or Server(serve_args)
        self.size, self.theme = size, theme
        self.local_storage = dict(local_storage or {})
        self.b: Browser | None = None
        self._ev_seen = 0
        self.console: list[dict] = []
        self.failed_requests: list[dict] = []

    def __enter__(self) -> "UiSession":
        if self.server.proc is None:
            self.server.start()
        self.b = Browser(*self.size)
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self.b:
            self.collect()
            self.b.close()
            self.b = None
        self.server.stop()

    def open(self) -> None:
        b = self.b
        b.navigate(self.server.url + "/static/fmt.js")         # same origin: preset localStorage before the app runs
        ls = dict(self.local_storage)
        if self.theme:
            ls["chipem.theme"] = self.theme
        if ls:
            b.js("(() => { localStorage.clear(); "
                 + "".join(f"localStorage.setItem({json.dumps(k)}, {json.dumps(v)});" for k, v in ls.items())
                 + " })()")
        b.navigate(self.server.url + "/")

    # -- collection
    def collect(self) -> None:
        b = self.b
        if not b:
            return
        for m in b.events[self._ev_seen:]:
            meth, p = m.get("method"), m.get("params", {})
            if meth == "log.entryAdded" and p.get("level") in ("error", "warn", "warning"):
                self.console.append({"level": p.get("level"), "text": p.get("text"), "type": p.get("type"),
                                     "source": (p.get("stackTrace") or {}).get("callFrames", [{}])[0:1]})
            elif meth == "network.responseCompleted":
                st = p.get("response", {}).get("status", 0)
                if st >= 400:
                    self.failed_requests.append({"url": p["request"]["url"], "status": st,
                                                 "method": p["request"].get("method")})
            elif meth == "network.fetchError":
                url = p.get("request", {}).get("url", "")
                if "/api/stream" in url and getattr(self, "_expect_offline", False):
                    continue
                self.failed_requests.append({"url": url, "error": p.get("errorText")})
        self._ev_seen = len(b.events)

    @property
    def console_errors(self) -> list[dict]:
        self.collect()
        return [c for c in self.console if c["level"] == "error"]

    # -- waiting
    def wait_js(self, expr: str, timeout: float = 15.0, poll: float = 0.15) -> Any:
        end = time.time() + timeout
        while True:
            v = self.b.js(expr)
            if v:
                return v
            if time.time() > end:
                raise TimeoutError(f"timed out waiting for: {expr}")
            self.b.pump(poll)

    def wait_live(self, timeout: float = 30.0) -> None:
        """Until the event stream is connected and the status badge shows a run state; then settle briefly."""
        self.wait_js("(() => { const c = document.getElementById('conn'), s = document.getElementById('status');"
                     " return c && /live/.test(c.textContent) && s && !/CONNECTING/.test(s.textContent); })()",
                     timeout)
        self.b.pump(0.6)

    def settle(self, s: float = 0.4) -> None:
        self.b.pump(s)

    # -- actions
    def shot(self, path: str | os.PathLike) -> str:
        self.b.screenshot(path)
        return str(path)

    def click(self, sel: str, x: float | None = None, y: float | None = None, clicks: int = 1,
              mods: list[str] | None = None) -> None:
        r = self.b.rect(sel)
        px = r["x"] + (x if x is not None else r["w"] / 2)
        py = r["y"] + (y if y is not None else r["h"] / 2)
        self.b.pointer(px, py, clicks=clicks, mods=mods)
        self.b.pump(0.15)

    def hover(self, sel: str, x: float | None = None, y: float | None = None) -> None:
        r = self.b.rect(sel)
        self.b.pointer(r["x"] + (x if x is not None else r["w"] / 2), r["y"] + (y if y is not None else r["h"] / 2),
                       move_only=True)
        self.b.pump(0.2)

    def key(self, key: str, mods: list[str] | None = None) -> None:
        self.b.keys(key, mods)
        self.b.pump(0.15)

    def type(self, sel: str, text: str, clear: bool = True) -> None:
        self.b.js(f"(() => {{ const e = document.querySelector({json.dumps(sel)}); e.focus();"
                  f" {'if (e.select) e.select(); else if (e.value !== undefined) e.value = \"\";' if clear else ''} }})()")
        self.b.type_text(text)
        self.b.pump(0.15)

    def js(self, expr: str) -> Any:
        return self.b.js(expr)


# --------------------------------------------------------------------------------------------- CLI
def run_steps(ui: UiSession, steps: list[dict], out: Path, tag: str, report: dict) -> None:
    for st in steps:
        do = st.get("do")
        try:
            if do == "shot":
                p = out / f"{tag}_{st.get('name', 'shot')}.png"
                ui.settle(st.get("settle", 0.3))
                ui.shot(p)
                report["shots"].append(str(p))
            elif do == "click":
                ui.click(st["sel"], st.get("x"), st.get("y"), mods=st.get("mods"))
            elif do == "dblclick":
                ui.click(st["sel"], st.get("x"), st.get("y"), clicks=2)
            elif do == "hover":
                ui.hover(st["sel"], st.get("x"), st.get("y"))
            elif do == "key":
                ui.key(st["key"], st.get("mods"))
            elif do == "type":
                ui.type(st["sel"], st["text"], st.get("clear", True))
            elif do == "eval":
                report["evals"].append({"js": st["js"], "value": ui.js(st["js"])})
            elif do == "wait":
                ui.settle(st.get("ms", 500) / 1000)
            elif do == "waitfor":
                ui.wait_js(st["js"], st.get("timeout", 15))
            elif do == "api":
                report["evals"].append({"api": st["path"], "value": ui.server.api(st.get("method", "GET"),
                                                                                  st["path"], st.get("body"))})
            elif do == "reload":
                ui.b.reload()
            elif do == "wait_live":
                ui.wait_live(st.get("timeout", 30))
            elif do == "stop_server":
                ui._expect_offline = True
                ui.server.stop()
            elif do == "start_server":
                ui.server.start()
            else:
                raise ValueError(f"unknown step {do!r}")
        except Exception as e:                                              # noqa: BLE001 - record and go on
            report["step_errors"].append({"step": st, "error": f"{type(e).__name__}: {e}"})
            if st.get("required"):
                raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--board", action="append", default=[], help="board YAML (repeatable: one run each)")
    ap.add_argument("--fw", action="append", default=[], help="firmware for a single-chip run (repeatable)")
    ap.add_argument("--serve-arg", action="append", default=[], help="extra argument for chipem serve")
    ap.add_argument("--out", required=True, help="directory for PNGs and report.json")
    ap.add_argument("--size", action="append", default=[], help="WxH (repeatable; default 1366x768)")
    ap.add_argument("--theme", action="append", default=[], choices=["dark", "light"],
                    help="repeatable; default dark")
    ap.add_argument("--steps", help="JSON file with the step list (default: wait for the stream, one shot)")
    ap.add_argument("--run", default="", help="run the emulation this long before the steps (e.g. 50ms); "
                                              "'go' = press Run and leave it running")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    steps = json.loads(Path(a.steps).read_text()) if a.steps else [{"do": "shot", "name": "main"}]
    sizes = [tuple(int(x) for x in s.lower().split("x")) for s in (a.size or ["1366x768"])]
    themes = a.theme or ["dark"]
    targets = [(Path(b).stem, ["--board", b]) for b in a.board] + [(Path(f).stem, [f]) for f in a.fw]
    if not targets:
        ap.error("give --board or --fw")
    reports, bad = [], 0
    for name, sargs in targets:
        for (w, h) in sizes:
            for theme in themes:
                tag = f"{name}_{w}x{h}_{theme}"
                rep: dict = {"tag": tag, "shots": [], "evals": [], "step_errors": []}
                try:
                    with UiSession(sargs + a.serve_arg, size=(w, h), theme=theme) as ui:
                        ui.wait_live()
                        if a.run == "go":
                            ui.click("#btnRun")
                        elif a.run:
                            ui.type("#runFor", a.run + "\n")
                            ui.wait_js("!/RUNNING/.test(document.getElementById('status').textContent)", 120)
                        run_steps(ui, steps, out, tag, rep)
                        ui.collect()
                        rep["console"] = ui.console
                        rep["failed_requests"] = ui.failed_requests
                except Exception as e:                                       # noqa: BLE001
                    rep["error"] = f"{type(e).__name__}: {e}"
                errs = [c for c in rep.get("console", []) if c["level"] == "error"]
                n = len(errs) + len(rep.get("failed_requests", [])) + len(rep["step_errors"]) + ("error" in rep)
                bad += bool(n)
                print(f"{tag}: {len(rep['shots'])} shots, {len(errs)} console errors, "
                      f"{len(rep.get('failed_requests', []))} failed requests, {len(rep['step_errors'])} step errors"
                      + (f", ERROR {rep['error']}" if "error" in rep else ""))
                reports.append(rep)
    (out / "report.json").write_text(json.dumps(reports, indent=1, default=str))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    sys.exit(main())
