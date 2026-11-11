"""DWARF variables for the debugger (chipem/dwarfvars.py): frame locals / arguments, globals, evaluate(), the web
endpoints (/api/debug/locals|globals|eval) and the shell commands (info locals/args, print, display, frame/up/down).

A small C program is compiled with clang + ld.lld at -O0 (skipped without them) for structs, arrays, enums,
bitfields, floats and outer frames; the -O1 sample uart_console.elf gives location lists / optimized-out values; the
TI-compiled C28x selftest.out gives cl2000 DWARF (type units, 16-bit units, register pairs)."""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from chipem.core import StopReason
from chipem.session import Session

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "firmware" / "samples"
BLINKY = SAMPLES / "build" / "blinky.elf"
CONSOLE = SAMPLES / "build" / "uart_console.elf"
C28_SELFTEST = ROOT / "firmware" / "samples_c2000_c" / "build" / "selftest.out"
C28_BLINKY = ROOT / "firmware" / "samples_c2000" / "build" / "blinky.c28img"

SRC = r"""
#include "k22.h"
typedef enum { RED = 1, GREEN = 2, BLUE = 7 } color_t;
struct pt { int16_t x; int16_t y; };
struct rec { uint32_t id; struct pt pos[2]; color_t col; unsigned flag : 1; unsigned mode : 3; float gain;
             const char *label; };
struct rec g_rec = { 0x1234, {{1, -2}, {3, 4}}, BLUE, 1, 5, 1.5f, "hello" };
volatile uint32_t g_counter = 7;
int16_t g_matrix[2][3] = {{1, 2, 3}, {4, 5, 6}};
static uint8_t s_bytes[4] = {9, 8, 7, 6};
volatile int sink;

__attribute__((noinline)) int leaf(struct rec *r, int k)
{
    int local = r->pos[1].x * k;
    sink = local;                                   /* BREAK_LEAF */
    return local + 1;
}

__attribute__((noinline)) int middle(int a)
{
    int mid = a + 100;
    struct pt p = { 10, 20 };
    int r = leaf(&g_rec, a);
    return r + mid + p.x + s_bytes[0];
}

int main(void)
{
    static int calls;
    WDOG_Disable();
    calls++;
    for (;;)
        sink = middle(2);
}
"""


@pytest.fixture(scope="module")
def vars_elf(tmp_path_factory) -> Path:
    if not (shutil.which("clang") and shutil.which("ld.lld")):
        pytest.skip("clang / ld.lld not installed")
    d = tmp_path_factory.mktemp("dwarfvars")
    (d / "vars.c").write_text(SRC)
    out = d / "vars.elf"
    cmd = ["clang", "--target=thumbv7em-none-eabi", "-mcpu=cortex-m4", "-mfpu=fpv4-sp-d16", "-mfloat-abi=hard",
           "-mthumb", "-O0", "-g", "-ffreestanding", "-nostdlib", "-fno-builtin", f"-I{SAMPLES}",
           f"-T{SAMPLES / 'link.ld'}", "-fuse-ld=lld", "-Wl,--image-base=0", "-Wl,-e,Reset_Handler",
           "-o", str(out), str(SAMPLES / "startup.c"), str(d / "vars.c")]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"cannot build the test program: {r.stderr[-400:]}")
    return out


def _line(marker: str) -> int:
    return next(i for i, ln in enumerate(SRC.splitlines(), 1) if marker in ln)


@pytest.fixture(scope="module")
def leaf_session(vars_elf):
    s = Session("mk22fn1m0vll12", vars_elf, clock_hz=120e6, quiet=True)
    D = s.debug
    D.add_break(f"vars.c:{_line('BREAK_LEAF')}")
    assert D.run_until_stop(20e6) is StopReason.DEBUG
    yield s
    s.close()


def by_name(items: list[dict]) -> dict[str, dict]:
    return {v["name"]: v for v in items}


# ---------------------------------------------------------------------------------------------- frames
def test_frame0_args_and_locals(leaf_session):
    d = leaf_session.debug.dbg()
    fv = d.frame_vars(0)
    assert fv["func"] == "leaf" and fv["has_dwarf"] and fv["reason"] is None
    v = by_name(fv["vars"])
    assert [x["name"] for x in fv["vars"]][:2] == ["r", "k"]            # arguments first
    assert v["r"]["kind"] == "arg" and v["r"]["type"] == "struct rec *"
    assert v["r"]["value"] == d.symbol_addr("g_rec") and "<g_rec>" in v["r"]["text"]
    assert v["k"]["value"] == 2 and v["k"]["type"] == "int" and v["k"]["avail"]
    assert v["local"]["kind"] == "local" and v["local"]["value"] == 6     # pos[1].x (3) * k
    assert v["local"]["addr"] is not None and v["local"]["loc"].startswith("[")


def test_outer_frames_use_the_cfi_unwound_registers(leaf_session):
    d = leaf_session.debug.dbg()
    f1 = d.frame_vars(1)
    assert f1["func"] == "middle"
    v = by_name(f1["vars"])
    assert v["a"]["kind"] == "arg" and v["a"]["value"] == 2
    assert v["mid"]["value"] == 102
    p = v["p"]
    assert p["type"] == "struct pt" and [(c["name"], c["value"]) for c in p["children"]] == [("x", 10), ("y", 20)]
    assert p["text"] == "{x = 10, y = 20}"
    f2 = d.frame_vars(2)
    assert f2["func"] == "main"
    calls = by_name(f2["vars"])["calls"]
    assert calls["kind"] == "static" and calls["value"] == 1
    assert d.evaluate("mid - a", 1)["value"] == 100
    with pytest.raises(ValueError):
        d.frame_vars(9)


def test_globals_list_types_and_filter(leaf_session):
    d = leaf_session.debug.dbg()
    g = by_name(d.globals_list())
    assert g["g_rec"]["type"] == "struct rec" and g["g_rec"]["addr"] == d.symbol_addr("g_rec")
    assert g["g_matrix"]["type"] == "int16_t [2][3]" and g["g_matrix"]["size"] == 12
    assert g["g_counter"]["type"] == "volatile uint32_t" and g["g_counter"]["size"] == 4
    assert g["s_bytes"]["file"].endswith("vars.c")
    assert g["main::calls"]["func"] == "main"
    assert [x["name"] for x in d.globals_list("matr")] == ["g_matrix"]


def test_struct_array_enum_bitfield_float(leaf_session):
    d = leaf_session.debug.dbg()
    rec = d.evaluate("g_rec")
    kids = by_name(rec["children"])
    assert kids["id"]["value"] == 0x1234 and kids["id"]["text"] == "4660 (0x1234)"
    assert kids["col"]["value"] == 7 and kids["col"]["text"] == "BLUE" and kids["col"]["type"] == "color_t"
    assert kids["flag"]["value"] == 1 and kids["mode"]["value"] == 5 and kids["mode"]["type"].endswith(": 3")
    assert kids["gain"]["value"] == 1.5 and kids["gain"]["text"] == "1.5"
    assert '"hello"' in kids["label"]["text"]
    pos = kids["pos"]
    assert pos["count"] == 2 and pos["children"][1]["children"][1]["value"] == 4
    m = d.evaluate("g_matrix")
    assert m["type"] == "int16_t [2][3]" and [c["value"] for c in m["children"][1]["children"]] == [4, 5, 6]
    b = d.evaluate("s_bytes")
    assert [c["value"] for c in b["children"]] == [9, 8, 7, 6] and b["children"][0]["text"] == "9"   # uint8_t: numbers


def test_evaluate_expression_forms(leaf_session):
    d = leaf_session.debug.dbg()
    ev = d.evaluate
    a = d.symbol_addr("g_counter")
    assert ev("g_rec.pos[1].y")["value"] == 4
    assert ev("r->pos[0].y")["value"] == -2                        # an argument of frame 0
    assert ev("(*r).id")["value"] == 0x1234 and ev("*r")["type"] == "struct rec"
    assert ev("&g_counter")["value"] == a and ev("&g_counter")["type"] == "volatile uint32_t *"
    assert ev("*&g_counter")["value"] == 7 and ev("g_counter + 1")["value"] == 8
    assert ev(f"*0x{a:x}")["value"] == 7 and ev(f"*0x{a:x}")["type"] == "uint32_t"
    assert ev(f"*(uint16_t *)0x{a:x}")["value"] == 7
    assert ev("g_matrix[1][2]")["value"] == 6
    assert ev("(&g_rec)->id")["value"] == 0x1234
    assert ev("sizeof(struct rec)")["value"] == ev("sizeof g_rec")["value"] > 16
    assert ev("k * 10")["value"] == 20 and ev("local == 6")["value"] == 1
    assert ev("$sp")["value"] == leaf_session.chip.core.read_reg("sp") and ev("$sp")["kind"] == "register"
    assert ev("mem32[addr(g_counter)]")["value"] == 7                 # falls back to the integer evaluator
    assert ev("g_rec.col == 7")["value"] == 1
    reg = ev("SIM.SCGC5")
    assert reg["kind"] == "register" and any(c["name"] == "PORTC" for c in reg["children"])
    assert ev("SIM.SCGC5.PORTC")["kind"] == "field"
    for bad in ("nosuch", "g_rec.nosuch", "g_rec[", "*(void *)0", "&k + 1 +"):
        with pytest.raises(ValueError):
            ev(bad)


# ---------------------------------------------------------------------------------------------- -O1 / unavailable
@pytest.mark.skipif(not CONSOLE.exists(), reason="sample firmware not built")
def test_location_lists_and_optimized_out():
    s = Session("mk22fn1m0vll12", CONSOLE, clock_hz=120e6, quiet=True)
    try:
        D = s.debug
        D.add_break("uart_console.c:42")
        assert D.run_until_stop(20e6) is StopReason.DEBUG
        d = D.dbg()
        v = by_name(d.frame_vars(0)["vars"])
        assert v["n"]["avail"] is False and v["n"]["reason"] == "optimized out at this PC" and v["n"]["value"] is None
        assert v["sbr"]["value"] == 120_000_000 // (16 * 115200) and v["sbr"]["loc"] == "computed"
        assert v["s"]["inlined"] == "puts0" and v["s"]["text"].startswith('0x') and '"' in v["s"]["text"]
        assert v["line"]["type"] == "char [48]" and v["line"]["count"] == 48 and len(v["line"]["children"]) == 48
        with pytest.raises(ValueError):
            d.evaluate("n + 1")                                         # arithmetic on an unavailable value
        assert d.evaluate("n")["avail"] is False                        # the variable alone: an unavailable item
    finally:
        s.close()


@pytest.mark.skipif(not BLINKY.exists(), reason="sample firmware not built")
def test_no_location_is_optimized_out_and_unsupported_ops_are_unavailable():
    from chipem import dwarfvars as dv
    s = Session("mk22fn1m0vll12", BLINKY, clock_hz=120e6, quiet=True)
    try:
        d = s.debug.dbg()
        fv = d.frame_vars(0)                         # at reset: Reset_Handler, `src` has no DW_AT_location
        assert fv["func"] == "Reset_Handler"
        src = by_name(fv["vars"])["src"]
        assert src["avail"] is False and src["reason"] == "optimized out"
        ctx = dv.VarCtx(d, dv._index(d), 0, None)
        op = lambda n, *a: SimpleNamespace(op_name=n, args=list(a))       # noqa: E731
        with pytest.raises(dv.Unavailable, match="entry"):
            dv._eval_ops(ctx, None, [op("DW_OP_entry_value", []), op("DW_OP_stack_value")], None)
        with pytest.raises(dv.Unavailable, match="unsupported"):
            dv._eval_ops(ctx, None, [op("DW_OP_GNU_push_tls_address")], None)
        loc = dv._eval_ops(ctx, None, [op("DW_OP_reg0"), op("DW_OP_piece", 2), op("DW_OP_piece", 2)], None)
        with pytest.raises(dv.Unavailable, match="partly optimized out"):
            dv._materialize(ctx, loc, 4)
        outer = dv.VarCtx(d, dv._index(d), 1, {"pc": 0x500, "func": "x"})
        with pytest.raises(dv.Unavailable, match="not recovered"):
            outer.reg("r0")
        # no DWARF: raw binary
        sb = Session("mk22fn1m0vll12", BLINKY.with_suffix(".bin"), clock_hz=120e6, quiet=True)
        try:
            db = sb.debug.dbg()
            r = db.frame_vars(0)
            assert r["has_dwarf"] is False and r["vars"] == [] and "no DWARF" in r["reason"]
            assert db.evaluate("pc + 2")["value"] == sb.chip.core.pc + 2
        finally:
            sb.close()
    finally:
        s.close()


# ---------------------------------------------------------------------------------------------- C28x
@pytest.mark.skipif(not C28_SELFTEST.exists(), reason="TI-compiled C28x sample not built")
def test_c28x_ti_dwarf_locals_structs_and_register_pairs():
    s = Session("tms320f28232", C28_SELFTEST, quiet=True)
    try:
        D = s.debug
        D.add_break("selftest.c:122")
        assert D.run_until_stop(20e6) is StopReason.DEBUG
        d = D.dbg()
        fv = d.frame_vars(0)
        v = by_name(fv["vars"])
        s1 = v["s1"]
        assert s1["type"] == "struct S" and s1["loc"].startswith("[sp-")
        assert [(c["name"], c["value"]) for c in s1["children"][:2]] == [("a", 7), ("b", 123456)]
        assert s1["children"][2]["text"] == '"abcd"'
        assert [c["value"] for c in v["arr"]["children"]] == [5, -3, 9, 0, 2, -8, 4]
        assert v["src"]["kind"] == "static" and v["src"]["text"] == '"hello, c28x"'
        g = by_name(d.globals_list())
        assert g["a32"]["type"] == "volatile long" and g["a32"]["size"] == 2       # 16-bit address units
        assert d.evaluate("a32")["value"] == -1234567890 and d.evaluate("f1")["value"] == 3.25
        assert d.evaluate("d1")["value"] == 2.5 and d.evaluate("ua64")["value"] == 18000000000000000000
        assert "twice" in d.evaluate("fp")["text"]
        assert d.evaluate("s1.c[1]")["text"] == "98 'b'" and d.evaluate("&s1")["value"] == s1["addr"]
        assert d.evaluate("*(long *)&s1.b")["value"] == 123456
        assert d.evaluate("SCIA.SCICCR")["kind"] == "register"
        f1 = d.frame_vars(1)
        assert all(not x["avail"] and "not recovered" in x["reason"] for x in f1["vars"]
                   if x["kind"] != "static") and "C28x" in f1["note"]
    finally:
        s.close()


@pytest.mark.skipif(not C28_BLINKY.exists(), reason="C28x sample not built")
def test_c28x_symbols_only_image_globals_from_the_symbol_table():
    s = Session("tms320f28232", C28_BLINKY, quiet=True)
    try:
        d = s.debug.dbg()
        assert d.frame_vars(0)["has_dwarf"] is False
        it = d.evaluate("pll")                        # a code label: its address
        assert it["value"] == d.symbol_addr("pll")
        assert d.evaluate("acc")["kind"] == "register"
    finally:
        s.close()


# ---------------------------------------------------------------------------------------------- web API + shell
def _get(app, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{app.port}{path}", timeout=10) as r:
        return json.loads(r.read())


def _post(app, path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"status": e.code, **json.loads(e.read() or b"{}")}


def test_web_locals_globals_eval(vars_elf):
    from chipem.web.server import WebApp
    s = Session("mk22fn1m0vll12", vars_elf, clock_hz=120e6, quiet=True)
    app = WebApp(s, "127.0.0.1", 0).start()
    try:
        assert _post(app, "/api/debug/break", {"location": f"vars.c:{_line('BREAK_LEAF')}"})["ok"]
        _post(app, "/api/run", {})
        end = time.time() + 10
        while app.worker.running and time.time() < end:
            time.sleep(0.02)
        loc = _get(app, "/api/debug/locals?frame=0")
        assert loc["func"] == "leaf" and by_name(loc["vars"])["k"]["value"] == 2
        assert by_name(_get(app, "/api/debug/locals?frame=1")["vars"])["mid"]["value"] == 102
        gl = _get(app, "/api/debug/globals?q=g_")["vars"]
        assert {"g_rec", "g_counter", "g_matrix"} <= {g["name"] for g in gl}
        r = _post(app, "/api/debug/eval", {"expr": "g_rec.pos[1]"})
        assert r["ok"] and r["type"] == "struct pt" and r["children"][0]["value"] == 3
        assert _post(app, "/api/debug/eval", {"expr": "a + 1", "frame": 1})["value"] == 3
        bad = _post(app, "/api/debug/eval", {"expr": "nosuch"})
        assert bad["status"] == 400 and "nosuch" in bad["error"]
    finally:
        app.stop()
        s.close()


def test_shell_info_locals_print_display(vars_elf):
    from chipem import cmd_shell
    from chipem.cli import build_parser
    cmds = (f"break vars.c:{_line('BREAK_LEAF')}; continue; info args; info locals; p r->pos[1]; p/x g_rec.id; "
            "display k * 3; display/x g_counter; up; info locals; p mid; down; info display; stepi; undisplay 1; "
            "info display; frame 2; info locals; p nosuch")
    args = build_parser().parse_args(["shell", str(vars_elf), "--clock=120e6", "--exec", cmds])
    buf = io.StringIO()
    old, sys.stdout = sys.stdout, buf
    try:
        cmd_shell.main(args)
    finally:
        sys.stdout = old
    out = buf.getvalue()
    assert "r = 0x" in out and "<g_rec>" in out and "k = 2" in out
    assert "local = 6" in out
    assert "r->pos[1] = {x = 3, y = 4}" in out
    assert "g_rec.id = 0x1234" in out
    assert "1: k * 3 = 6" in out and "2: /x g_counter = 0x7" in out
    assert "#1  " in out and "middle" in out and "p = {x = 10, y = 20}" in out and "mid = 102" in out
    assert out.count("1: k * 3 = 6") >= 2                              # shown again after stepi
    assert "1:   k * 3" in out and "2:   /x g_counter" in out
    assert "calls = 1" in out
    assert "error: no symbol 'nosuch'" in out
