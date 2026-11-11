"""SDCC-compiled C on the P89CV51RD2 (firmware/samples_mcs51/c, built by its build.sh with SDCC 4.5; the outputs are
tracked, so these tests need no SDCC): SDCC's startup code, interrupt handlers with __interrupt/__using, printf
through putchar, memory models and --stack-auto, the long/float runtime library, IAP from C, on-chip XRAM, power-down
wake-up, and source-level debugging from the .cdb (chipem/sdcc_cdb.py).  Timings at 11.0592 MHz, 12-clock mode."""
from __future__ import annotations

import math
import struct
from pathlib import Path

import pytest

from chipem.debug import Debugger
from chipem.session import Session

from m51util import MC_NS

ROOT = Path(__file__).resolve().parents[1]
CDIR = ROOT / "firmware" / "samples_mcs51" / "c"
BUILD = CDIR / "build"
CHIP = ROOT / "chips" / "p89cv51rd2.yaml"


def _img(name: str) -> Path:
    p = BUILD / f"{name}.ihx"
    if not p.exists():
        pytest.skip(f"{p} not built (firmware/samples_mcs51/c/build.sh, needs SDCC)")
    return p


def sess(name: str) -> Session:
    return Session(CHIP, _img(name), quiet=True)


def sym(s: Session, name: str) -> int:
    return s.image.symbols[name]


def mem(s: Session, name: str, n: int) -> bytes:
    return s.chip.core.read_mem(sym(s, name), n)


def edges(s: Session, pin: str) -> list[float]:
    return [e.t_ns for e in s.events() if e.kind == "pin" and e.fields.get("pin") == pin
            and e.fields.get("field") == "level"]


def problems(s: Session) -> list:
    return [e for e in s.events() if e.kind in ("warn", "fault")]


def test_every_c_source_has_a_build():
    for src in sorted(CDIR.glob("*.c")):
        if src.stem == "uart":
            continue
        names = {"models": ["models_small", "models_medium", "models_large", "models_small_sa", "models_large_sa"],
                 "math": ["math_small", "math_large"]}.get(src.stem, [src.stem])
        for n in names:
            for ext in (".ihx", ".cdb", ".map"):
                assert (BUILD / f"{n}{ext}").exists(), f"{n}{ext} missing: run firmware/samples_mcs51/c/build.sh"


def test_blinky_timer0_50ms():
    s = sess("blinky")
    s.run_for(260e6)
    e = edges(s, "P1.0")
    assert len(e) == 6 and not problems(s)
    d = [(b - a) / MC_NS for a, b in zip(e[1:], e[2:])]
    assert max(d) - min(d) < 1.01                      # constant period
    assert 46080 < d[0] < 46080 + 30                   # 46080 counts + the reload / toggle code


def test_uart_console_printf_and_rx_ring():
    s = sess("uart_console")
    s.run_for(60e6)
    assert s.uart_output("UART") == b"\r\nP89CV51 SDCC console (9600 8N1)\r\n> "
    s.uart_input(b"hello\rc\rl\r", "UART")
    s.run_for(80e6)
    assert s.uart_output("UART").endswith(b"> hello\r\nHELLO\r\n> c\r\ncmds 2 chars 8\r\n> l\r\nLED on\r\n> ")
    assert s.chip.pins.get("P1.0").level() == "L" and not problems(s)
    tx = [e.t_ns for e in s.events() if e.kind == "uart" and e.fields.get("dir") == "tx"]
    assert (tx[3] - tx[2]) == pytest.approx(10e9 / 9600, rel=0.01)     # TX ring: back to back at 9600 8N1


def test_timer_tick_interrupt_using_bank1():
    s = sess("timer_tick")
    s.run_for(205e6)
    ticks = int.from_bytes(mem(s, "ticks", 2), "little")
    assert ticks == 200 and mem(s, "seconds10", 1) == b"\x02"
    assert mem(s, "isr_bank", 1) == b"\x01"                    # __using(1): the handler ran in register bank 1
    e = edges(s, "P1.1")
    d = [(b - a) / MC_NS for a, b in zip(e, e[1:])]
    # each tick is 922 counts + the 15 machine cycles from the overflow to the TL0 reload, waking from Idle: polling
    # cycle 1, hardware LCALL 2, LJMP 2, PUSH ACC 2, PUSH PSW 2, MOV PSW 2 (__using(1)), MOV TH0 2, MOV TL0 2
    assert all(x == pytest.approx(10 * (922 + 15), abs=0.01) for x in d)


def test_pca_pwm_capture_board_c():
    _img("pca_pwm")
    s = Session.from_board(ROOT / "boards" / "p89cv51_pca_loopback_c.yaml", quiet=True)
    s.run_for(3e6)
    c = s.chips["A"]
    n = c.core.read_mem(s.chips["A"].firmware.symbols["ncap"], 1)[0]
    raw = c.core.read_mem(c.firmware.symbols["caps"], 16)
    caps = struct.unpack("<8H", raw)[:n]
    assert n == 8
    assert [(y - x) & 0xFFFF for x, y in zip(caps, caps[1:])] == [64, 192] * 3 + [64]


def test_spi_flash_board_c():
    _img("spi_flash")
    s = Session.from_board(ROOT / "boards" / "p89cv51_spi_flash_c.yaml", quiet=True)
    s.run_for(40e6)
    c = s.chips["A"]
    assert c.core.read_mem(c.firmware.symbols["id"], 3) == b"\xef\x40\x16"
    assert c.core.read_mem(c.firmware.symbols["data"], 16) == b"\xff" * 16      # XDATA symbol -> on-chip XRAM
    assert s.uart_output("A.UART") == b"ID EF4016 data FF FF\r\n"


def test_iap_flash_from_c():
    s = sess("iap_flash")
    s.run_for(90e6)                                      # includes the 30 ms page erase
    assert s.uart_output("UART") == b"IAP from C mfr 15 err 00 PASS\r\n"
    assert bytes(s.chip.core.code[0xE000:0xE00B]) == b"IAP from C\0"
    assert mem(s, "result", 1) == b"\x55"


def test_xram_and_extram():
    s = sess("xram_test")
    s.run_for(80e6)
    assert s.uart_output("UART") == b"XRAM ok ext FF kept 1\r\n"
    x = s.chip.core.read_mem(sym(s, "xram"), 768)
    assert sym(s, "xram") == 0x40000                         # __xdata __at(0) -> on-chip XRAM tag
    assert all(x[i] == (i ^ (i >> 8) ^ 0x5A) & 0xFF for i in range(768))


def test_power_down_wake_from_c():
    s = sess("power_down")
    s.run_for(80e6)
    assert mem(s, "idle_wakes", 1) == b"\x05" and s.chip.sleeping and mem(s, "pd_wakes", 1) == b"\x00"
    s.set_pin("P3.2", "L")
    s.run_for(1e5)
    s.set_pin("P3.2", "H")
    s.run_for(3e6)
    assert mem(s, "pd_wakes", 1) == b"\x01" and mem(s, "state", 1) == b"\xa5" and not s.chip.sleeping
    assert len(edges(s, "P1.0")) >= 3                          # main loop running again


MODELS = ["models_small", "models_medium", "models_large", "models_small_sa", "models_large_sa"]


@pytest.mark.parametrize("name", MODELS)
def test_memory_models_and_stack_auto(name):
    s = sess(name)
    s.run_for(200e6)
    assert mem(s, "done", 1) == b"\xa5" and s.chip.core.pc == sym(s, "finish")
    fib, fact, dot, crc, sorted_ok, ptr_sum, fsum = struct.unpack("<HIhHBBH", mem(s, "res", 14))
    assert (fib, fact, dot, crc, sorted_ok, ptr_sum, fsum) == (610, 479001600, 31, 0x29B1, 1, 10, (~3021) & 0xFFFF)


def _f32(x: float) -> float:
    return struct.unpack("<f", struct.pack("<f", x))[0]


@pytest.mark.parametrize("name", ["math_small", "math_large"])
def test_long_and_float_runtime(name):
    s = sess(name)
    s.run_for(200e6)
    assert mem(s, "done", 1) == b"\xa5"
    raw = mem(s, "out", 80)
    u = struct.unpack("<20I", raw)
    i = struct.unpack("<20i", raw)
    f = struct.unpack("<20f", raw)
    la, lb = 123456789, -9876
    fa, fb = _f32(3.14159265), -2.5
    assert i[0] == (la * lb + 2**31) % 2**32 - 2**31          # 32-bit wrap
    assert i[1] == int(la / lb) and i[2] == la - int(la / lb) * lb      # C truncating division
    assert u[3] == la // 1000 and u[4] == 0xDEADBEEF >> 7 and i[5] == (-la) >> 3
    exact = {6: fa + fb, 7: fa - fb, 8: fa * fb, 9: fa / fb}
    for k, v in exact.items():
        assert f[k] == pytest.approx(_f32(v), rel=2e-7), k     # IEEE single, within an ulp
    approx = {10: math.sqrt(fa), 11: math.sin(fa / 6), 12: math.e, 13: math.log(10), 15: float(la),
              16: 0.25, 17: math.pi, 19: 1024.0}
    for k, v in approx.items():
        assert f[k] == pytest.approx(v, rel=3e-6), k
    assert i[14] == 3141
    assert u[18] == 0xFFFFFFFF          # 65535 * 65537 rounds to 2**32 in single precision: out of range, saturated


# ------------------------------------------------------------------------------------------------ debugging
def _dbg(s: Session) -> Debugger:
    return s.debug.dbg()


def test_cdb_line_table_breakpoints_and_listing():
    s = sess("timer_tick")
    d = _dbg(s)
    assert type(d.dwarf).__name__ == "CdbInfo"
    (addr, _desc), = d.resolve_location("main")
    li = d.dwarf.line_for(addr)
    assert li.basename == "timer_tick.c" and li.line == 27      # first statement after `void main(void) {`
    a2 = d.resolve_location("timer_tick.c:31")[0][0]
    assert d.dwarf.line_for(a2).line == 31
    d.add_break("timer_tick.c:31")
    s.run_for(5e6)
    assert s.chip.core.pc == a2
    lst = d.listing(mode="mixed")
    assert lst["has_dwarf"] and lst["has_source"] and lst["file"].endswith("timer_tick.c")
    src = [r for r in lst["rows"] if r["kind"] == "src"]
    assert any("ET0 = 1;" in r["text"] for r in src)
    srcmode = d.listing(mode="source")
    assert any(r.get("line") == 31 and "ET0" in r["text"] for r in srcmode["rows"])


def test_cdb_backtrace_through_interrupt_and_recursion():
    s = sess("timer_tick")
    d = _dbg(s)
    d.add_break("t0_isr")
    s.run_for(5e6)
    bt = d.backtrace()
    assert bt[0]["func"] == "t0_isr" and bt[1]["func"] == "main" and bt[1]["how"] == "interrupt"
    s = sess("models_small_sa")
    d = _dbg(s)
    d.add_break("fib")
    for _ in range(4):
        s.run_for(2e6)
        if s.chip.core.pc != d.resolve_location("fib")[0][0]:
            continue
    bt = d.backtrace()
    assert [f["func"] for f in bt][-1] == "main" and sum(f["func"] == "fib" for f in bt) >= 2


def test_symbols_from_cdb_spaces():
    s = sess("spi_flash")
    syms = s.image.symbols
    assert syms["main"] < 0x10000 and syms["id"] >> 16 == 2 and syms["data"] >> 16 == 4
    assert "P1" not in syms and not any("$" in k for k in syms)


def test_gdb_target_description_mcs51():
    from chipem.gdbserver import _Layout, target_xml
    s = sess("blinky")
    xml = target_xml(s.chip)
    assert "<architecture>mcs51</architecture>" in xml and "org.chipem.mcs51" in xml
    lay = _Layout(s.chip)
    assert [r[0] for r in lay.regs][:12] == [f"r{i}" for i in range(8)] + ["a", "b", "psw", "sp"]
    assert dict((r[0], r[1]) for r in lay.regs)["pc"] == 16
