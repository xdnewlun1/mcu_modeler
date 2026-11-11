"""Debugger engine (chipem/debug.py, core breakpoints / watchpoints, StopReason.DEBUG) on the sample firmware:
ARM (Unicorn) and C28x (interpreter), single chip and a board."""
from __future__ import annotations

from pathlib import Path

import pytest

from chipem.core import StopReason
from chipem.debug import DebugError, compile_expr
from chipem.session import Session

ROOT = Path(__file__).resolve().parent.parent
BLINKY = ROOT / "firmware" / "samples" / "build" / "blinky.elf"
BLINKY_BIN = ROOT / "firmware" / "samples" / "build" / "blinky.bin"
C28_BLINKY = ROOT / "firmware" / "samples_c2000" / "build" / "blinky.c28img"
C28_C_BLINKY = ROOT / "firmware" / "samples_c2000_c" / "build" / "blinky.out"
BOARD = ROOT / "boards" / "k22_stm32_uart.yaml"

need_arm = pytest.mark.skipif(not BLINKY.exists(), reason="sample firmware not built")


def arm():
    return Session("mk22fn1m0vll12", BLINKY, clock_hz=120e6, quiet=True)


def c28(fw=C28_BLINKY):
    return Session("tms320f28232", fw, quiet=True)


def sym(s, name):
    return s.debug.dbg().symbol_addr(name)


# ---------------------------------------------------------------------------------------------- core level
@need_arm
def test_arm_breakpoint_stops_before_the_instruction_with_exact_counts():
    s = arm()
    main = sym(s, "main")
    core = s.chip.core
    core.set_breakpoints([main])
    assert s.run_for(1e6) is StopReason.DEBUG
    assert core.pc == main and core.stop_info == {"type": "breakpoint", "addr": main}
    n_bp = s.chip.insns
    # the same point reached by single steps: identical instruction count
    s2 = arm()
    while s2.chip.core.pc != main:
        s2.step()
    assert s2.chip.insns == n_bp
    # resume steps over the breakpoint once, then the next pass stops again (main is entered once: run on)
    assert s.run_for(1e6) is StopReason.COUNT
    core.set_breakpoints([])


@need_arm
def test_arm_watchpoint_mmio_and_ram_stop_after_the_access():
    s = arm()
    core = s.chip.core
    gpioc = s.chip.peripherals["GPIOC"]
    ptor = gpioc.base + gpioc.regs["PTOR"].offset
    core.set_watchpoints([(ptor, ptor + 4, "w")])
    assert s.run_for(10e6) is StopReason.DEBUG
    info = core.stop_info
    assert info["type"] == "watchpoint" and info["addr"] == ptor and info["access"] == "w" and info["value"] == 0x20
    d = s.debug.dbg()
    ins = d.decode(info["pc"])
    assert ins["text"].startswith("str") and core.pc == info["pc"] + ins["size"]     # after the store
    core.set_watchpoints([])
    # RAM: the volatile counter on the stack (str r2, [sp, #4])
    sp = core.read_reg("sp")
    core.set_watchpoints([(sp + 4, sp + 8, "w")])
    assert s.run_for(1e6) is StopReason.DEBUG
    v = core.stop_info["value"]
    assert int.from_bytes(core.read_mem(sp + 4, 4), "little") == v
    core.set_watchpoints([])


@pytest.mark.skipif(not C28_BLINKY.exists(), reason="C28x sample not built")
def test_c28x_breakpoint_and_watchpoint():
    s = c28()
    core = s.chip.core
    loop = s.image.symbols["loop"]
    core.set_breakpoints([loop])
    assert s.run_for(50e6) is StopReason.DEBUG and core.pc == loop
    n = s.chip.insns
    assert s.run_for(50e6) is StopReason.DEBUG and core.pc == loop and s.chip.insns > n   # stepped over, looped
    core.set_breakpoints([])
    tog = s.image.symbols["GPBTOGGLE"]
    core.set_watchpoints([(tog, tog + 1, "w")])
    assert s.run_for(50e6) is StopReason.DEBUG
    info = core.stop_info
    assert info["addr"] == tog and info["access"] == "w" and info["pc"] == loop
    assert core.pc == loop + 2                         # MOV @0xE,#4 is two words: stopped after it
    core.set_watchpoints([])
    assert s.run_for(1e6) is StopReason.COUNT


@need_arm
def test_no_hooks_when_nothing_is_set():
    s = arm()
    s.debug.add_break("main")
    s.debug.add_watch("GPIOC.PTOR")
    s.debug.delete_all()
    core = s.chip.core
    assert core._bp_hooks == {} and core._wp_hooks == [] and core.debug_hook is None


# ---------------------------------------------------------------------------------------------- Debugger
@need_arm
def test_locations_conditions_hits_temp_and_ignore():
    s = arm()
    D = s.debug
    d = D.dbg()
    main = sym(s, "main")
    (bp,) = D.add_break("main")
    assert bp.addr > main and "blinky.c:" in bp.label             # after the prologue
    assert D.add_break("*main")[0].addr == main
    assert D.add_break("main+4")[0].addr == main + 4
    (b16,) = D.add_break("blinky.c:16")
    assert d.dwarf.line_for(b16.addr).line == 16
    with pytest.raises(DebugError):
        D.add_break("nosuchfile.c:3")
    with pytest.raises(DebugError):
        D.add_break("no_such_symbol")
    D.delete_all()
    # condition on a register + ignore count + temporary
    (b,) = D.add_break("blinky.c:16", cond="r0 == 0x20 && r4 != 0")
    b.ignore = 2
    for _ in range(3):
        assert D.run_until_stop(100e6) is StopReason.DEBUG
    assert b.hits == 5 and D.last_stop["bp"] == [b.id]       # gdb: ignored hits count too
    D.modify(b.id, cond="r0 == 1")                                  # never true: runs to the time limit
    assert D.run_until_stop(2e6) is StopReason.COUNT
    D.delete(b.id)
    (t,) = D.add_break("blinky.c:16", temp=True)
    assert D.run_until_stop(100e6) is StopReason.DEBUG
    assert t.id not in d.bps and not D.breakpoints()


def test_expressions():
    t = compile_expr("r0 == 3 && !(r1 & 4) || mem32[0x20000000] > 1")
    assert t is not None
    for bad in ("__import__('os')", "r0.x", "f(1)", "[1, 2]"):
        with pytest.raises(DebugError):
            compile_expr(bad)


@need_arm
def test_stepping_next_step_finish_until():
    s = arm()
    D = s.debug
    D.add_break("main")
    D.run_until_stop(1e6)
    d = D.dbg()
    line = lambda: d.dwarf.line_for(s.chip.core.pc).line   # noqa: E731
    assert line() == 6                                     # WDOG_Disable();
    D.do("step")                                           # into WDOG_Disable (after its prologue)
    assert d.dwarf.function_for(s.chip.core.pc).name == "WDOG_Disable"
    D.do("finish")                                         # back in main, after the call
    assert d.dwarf.function_for(s.chip.core.pc).name == "main"
    D.do("next")
    assert line() == 7
    D.do("until", location="blinky.c:16")
    assert line() == 16
    pc = s.chip.core.pc
    D.do("stepi")
    assert s.chip.core.pc == pc + d.decode(pc)["size"]
    assert not any(b.internal for b in d.bps.values())     # internal breakpoints are gone after every stop


@need_arm
def test_nexti_runs_over_a_call():
    s = arm()
    D = s.debug
    D.add_break("main")
    D.run_until_stop(1e6)
    d = D.dbg()
    pc = s.chip.core.pc
    ins = d.decode(pc)
    assert ins["call"]                                     # bl WDOG_Disable
    D.do("nexti")
    assert s.chip.core.pc == pc + ins["size"]


@need_arm
def test_backtrace_cfi_and_listing_modes():
    s = arm()
    D = s.debug
    D.add_break("WDOG_Disable")
    D.run_until_stop(1e6)
    d = D.dbg()
    bt = d.backtrace()
    assert [f["func"] for f in bt][:2] == ["WDOG_Disable", "main"]
    assert bt[1]["how"] in ("cfi", "lr")
    mixed = d.listing(mode="mixed")
    kinds = {r["kind"] for r in mixed["rows"]}
    assert kinds == {"src", "asm"} and any(r["pc"] for r in mixed["rows"] if r["kind"] == "asm")
    src = d.listing(mode="source")
    assert src["mode"] == "source" and any(r["pc"] for r in src["rows"])
    asm = d.listing(mode="asm")
    assert {r["kind"] for r in asm["rows"]} == {"asm"}


@need_arm
def test_raw_bin_has_assembly_only():
    if not BLINKY_BIN.exists():
        pytest.skip("blinky.bin not built")
    s = Session("mk22fn1m0vll12", BLINKY_BIN, clock_hz=120e6, quiet=True)
    d = s.debug.dbg()
    assert d.dwarf is None
    s.debug.add_break("*0x4ac")
    assert s.debug.run_until_stop(1e6) is StopReason.DEBUG
    lst = d.listing(mode="source")
    assert lst["mode"] == "mixed" and {r["kind"] for r in lst["rows"]} == {"asm"}


@need_arm
def test_peek_has_no_read_side_effects():
    s = arm()
    s.run_for(1e6)
    d = s.debug.dbg()
    uart = s.chip.peripherals["UART0"]
    calls = []
    for r in uart.regs.values():
        if r.on_read is not None:
            orig = r.on_read
            r.on_read = (lambda o: lambda b, reg: calls.append(reg.name) or o(b, reg))(orig)
    data, exact = d.read_memory(uart.base, 0x20)
    assert len(data) == 0x20 and not calls                 # no read hook ran
    assert all(exact)                                      # every UART register has a side-effect-free peek (D: FIFO head)
    with pytest.raises(DebugError):
        d.read_memory(0x2F000000, 4)                       # unmapped


@need_arm
def test_register_and_memory_writes():
    s = arm()
    d = s.debug.dbg()
    d.set_register("r5", 0x1234)
    assert s.chip.core.read_reg("r5") == 0x1234
    d.write_memory(0x20000000, b"\x11\x22\x33\x44")
    assert d.read_uint(0x20000000, 4) == 0x44332211
    assert d.eval_expr("mem32[0x20000000] == 0x44332211 and r5 == 0x1234") == 1


@pytest.mark.skipif(not C28_C_BLINKY.exists(), reason="TI-compiled C28x sample not built")
def test_c28x_ti_elf_has_line_info_and_steps():
    s = c28(C28_C_BLINKY)
    D = s.debug
    d = D.dbg()
    assert d.dwarf is not None
    (b,) = D.add_break("main")
    assert D.run_until_stop(5e6) is StopReason.DEBUG and D.last_stop["bp"] == [b.id]
    assert "blinky.c:" in D.last_stop["where"]
    l0 = d.dwarf.line_for(s.chip.core.pc).line
    D.do("next")
    assert d.dwarf.line_for(s.chip.core.pc).line != l0
    bt = d.backtrace()
    assert bt[0]["func"] == "main" and len(bt) >= 2
    with pytest.raises(DebugError):
        D.plan("finish")                                   # main is the outermost frame


@pytest.mark.skipif(not C28_BLINKY.exists(), reason="C28x sample not built")
def test_c28x_symbols_only_image():
    s = c28()
    D = s.debug
    d = D.dbg()
    assert d.dwarf is None
    (b,) = D.add_break("pll")
    D.run_until_stop(5e6)
    assert s.chip.core.pc == b.addr
    rows = d.listing(mode="mixed")["rows"]
    assert rows and all(r["kind"] == "asm" for r in rows) and any(r.get("sym") == "pll" for r in rows)
    D.do("nexti")
    assert s.chip.core.pc != b.addr


@pytest.mark.skipif(not BOARD.exists(), reason="board missing")
def test_board_stop_reports_chip_and_skew():
    s = Session.from_board(BOARD, quiet=True)
    for ctx in s.ctxs.values():
        if ctx.image is None or not Path(ctx.image.source).exists():
            pytest.skip("board firmware not built")
    D = s.debug
    (b,) = D.add_break("B:main")
    assert b.chip == "B"
    assert D.run_until_stop(20e6) is StopReason.DEBUG
    ls = D.last_stop
    assert ls["chip"] == "B" and ls["bp"] == [b.id]
    assert 0 <= ls["skew_ns"] <= s.board.quantum_ns + 1     # the other chip is at most one quantum ahead
    D.do("next", "B")
    assert D.last_stop["chip"] == "B"


def test_mcs51_register_locations_use_their_address_space():
    """Memory view `sp` on an 8051 shows IDATA (the stack), `dptr` XDATA; as values they stay plain numbers."""
    from pathlib import Path
    from chipem.core.mcs51.core import IDATA_BASE, XDATA_BASE
    from chipem.session import Session
    fw = Path(__file__).resolve().parents[1] / "firmware/samples_mcs51/build/uart_hello.hex"
    if not fw.exists():
        pytest.skip("sample not built")
    s = Session("p89cv51rd2", fw, quiet=True)
    try:
        s.run_for(1e6)
        d = s.debug.dbg()
        sp = s.chip.core.read_reg("sp")
        assert d.eval_expr("sp") == sp
        assert d.eval_expr("sp", values=False) == IDATA_BASE + sp
        assert d.eval_expr("sp+1", values=False) == IDATA_BASE + sp + 1
        assert d.eval_expr("dptr", values=False) == XDATA_BASE + s.chip.core.read_reg("dptr")
        data, _ = d.read_memory(d.eval_expr("sp", values=False) - 1, 2)
        assert data == bytes(s.chip.core.iram[sp - 1:sp + 1])
    finally:
        s.close()
