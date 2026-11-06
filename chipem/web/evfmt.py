"""One text form of a log event, shared by the browser and the server.

The web UI shows each event as a row `time  [chip]  kind  text`. The server computes `text` once per event (the
`desc` field it adds to every event it sends, on /api/events and the SSE stream), and the browser displays it as is,
so there is a single formatter. The browser's event filter matches the tab-joined row (`event_line`); the server's
`/api/events?q=` filter and the `.txt` download use the same `event_line`, so a download contains exactly the rows the
filter shows in the browser. `fmt_time` is the Python twin of `fmtTime` in app.js (the only part the browser still
formats itself, because state frames carry raw nanoseconds too); tests/test_web.py checks the two agree.
"""
from __future__ import annotations

import json
import math
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

_SKIP = frozenset(("kind", "t_ns", "seq", "chip", "desc"))
_MSG_SKIP = frozenset(("block", "addr", "offset", "size", "access", "value", "pc", "type", "handled"))
_HEX_KEYS = frozenset(("addr", "pc", "value"))


def _fixed(x: float, digits: int) -> str:
    """JavaScript Number.prototype.toFixed: the exact binary value rounded half up (Python's format rounds half to
    even, which differs on exact ties such as 1.0625 -> 1.063)."""
    return str(Decimal(x).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))


def fmt_time(ns: Any) -> str:
    """Virtual time as the UI shows it (app.js fmtTime): 812 ns, 1.250 µs, 3.000 ms, 1.000000 s."""
    if ns is None:
        return "—"
    ns = float(ns)
    if ns >= 1e9:
        return f"{_fixed(ns / 1e9, 6)} s"
    if ns >= 1e6:
        return f"{_fixed(ns / 1e6, 3)} ms"
    if ns >= 1e3:
        return f"{_fixed(ns / 1e3, 3)} µs"
    return f"{math.floor(ns + 0.5)} ns"                    # Math.round


def _js_number(v: float | int) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if math.isnan(v) or math.isinf(v):
        return "null"
    if v == int(v) and abs(v) < 1e21:
        return str(int(v))
    r = repr(v)
    if "e" in r:                                           # 1e-07 -> 1e-7, 1e+22 stays
        m, e = r.split("e")
        r = f"{m}e{'+' if not e.startswith('-') else '-'}{e.lstrip('+-').lstrip('0') or '0'}"
    return r


def js_json(v: Any) -> str:
    """JSON.stringify for event field values (compact separators, non-ASCII kept, integral floats without .0)."""
    if v is None or isinstance(v, (bool, int, float)):
        return "null" if v is None else _js_number(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, dict):
        return "{" + ",".join(f"{json.dumps(str(k), ensure_ascii=False)}:{js_json(x)}" for k, x in v.items()) + "}"
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(js_json(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)


def _js_str(v: Any) -> str:
    """String(v) as in a JS template literal."""
    if isinstance(v, str):
        return v
    if v is None:
        return "null"
    if isinstance(v, (bool, int, float)):
        return _js_number(v)
    if isinstance(v, (list, tuple)):
        return ",".join("" if x is None else _js_str(x) for x in v)
    return js_json(v)


def _hex32(v: float | int) -> str:
    return "0x" + format(int(v) & 0xFFFFFFFF, "08X")


def _byte_char(b: int) -> str:
    if 32 <= b < 127:
        return chr(b)
    return {10: "\\n", 13: "\\r"}.get(b, f"\\x{b:02x}")


def describe_event(e: dict) -> str:
    """The text column of an event row (everything after time, chip and kind)."""
    kind = e.get("kind")
    if kind == "pin":
        f = e.get("field") or "level"
        old, new = e.get("old"), e.get("new")
        return f"{_js_str(e.get('pin'))} {_js_str(f)} {'—' if old is None else _js_str(old)} → " \
               f"{'—' if new is None else _js_str(new)}"
    if kind == "part":
        s = f"{'' if e.get('part') is None else _js_str(e.get('part'))}{'.' + _js_str(e['port']) if e.get('port') else ''}"
        for k, v in e.items():
            if k in _SKIP or k in ("part", "port"):
                continue
            s += f" {k}={v if isinstance(v, str) else js_json(v)}"
        return s
    if kind == "uart":
        d = e.get("dir") or "tx"
        b = e.get("byte")
        if b is not None:
            b = int(b)
            return f"{e.get('uart') or e.get('name') or ''} {d} 0x{b:02x} '{_byte_char(b)}'"
        data = e.get("data")
        if data is None:
            data = e.get("text")
        if data is None:
            data = ""
        return f"{e.get('name') or e.get('uart') or ''} {d} {js_json(data)}"
    msg = e.get("msg")
    s = str(msg) if msg else ""
    for k, v in e.items():
        if k in _SKIP or k == "msg":
            continue
        if msg and k in _MSG_SKIP:
            continue
        num = isinstance(v, (int, float)) and not isinstance(v, bool)
        s += f" {k}={_hex32(v) if num and k in _HEX_KEYS else js_json(v)}"
    return s.strip()


def event_line(e: dict, board: bool, default_chip: str = "A") -> str:
    """The row as the browser filters and copies it: `time \\t [chip \\t] kind \\t text`."""
    desc = e.get("desc")
    if desc is None:
        desc = describe_event(e)
    chip = e.get("chip")
    if chip is None:
        chip = default_chip
    return f"{fmt_time(e.get('t_ns'))}\t{chip}\t{e.get('kind', '')}\t{desc}" if board else \
        f"{fmt_time(e.get('t_ns'))}\t{e.get('kind', '')}\t{desc}"


def text_matcher(q: str):
    """Predicate for one filter string, with the browser's rules: plain text = case-insensitive substring;
    `re:pattern` = regular expression, case-insensitive unless the pattern contains an upper-case letter (smart
    case, as the `/re/` filter boxes), `(?i)` at the start forces case-insensitive. None for an empty filter.
    Raises ValueError for a bad regex."""
    q = q or ""
    if not q:
        return None
    if q.startswith("re:"):
        pat = q[3:]
        flags = 0 if re.search(r"[A-Z]", pat) else re.I
        try:
            rx = re.compile(pat, flags)
        except re.error as err:
            raise ValueError(f"bad regex: {err}") from None
        return lambda text: rx.search(text) is not None
    ql = q.lower()
    return lambda text: ql in text.lower()
