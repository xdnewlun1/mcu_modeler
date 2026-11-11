"""Differential tests of the MCS-51 core against ucsim (`s51` / `sdcc-s51`, an independent 8051 simulator shipped
with SDCC): tests/mcs51_ucsim.py runs the same code in both and compares PC, ACC/B/PSW/SP/DPTR/P1/P2, all 256 bytes
of internal RAM, the XDATA bytes touched and the oscillator clock count.

Fast subset here (skipped without s51): every opcode (A5h excluded) with boundary operands, random instruction
sequences, and the SDCC-compiled programs of firmware/samples_mcs51/c (memory models, --stack-auto, long/float
library) from reset to `finish()`.  The exhaustive grid and long random runs: tools/mcs51_ucsim_diff.py.

The ucsim deviations from the Intel definition found by the harness are avoided by the generators and our core's
behaviour for each of them is checked here without s51 (test_intel_behaviour_where_ucsim_deviates)."""
from __future__ import annotations

import random
from pathlib import Path

import pytest

import mcs51_ucsim as U
from chipem.core.mcs51.asm import assemble
from chipem.firmware import load_firmware

ROOT = Path(__file__).resolve().parents[1]
CBUILD = ROOT / "firmware" / "samples_mcs51" / "c" / "build"
need_s51 = pytest.mark.skipif(U.find_s51() is None, reason="ucsim s51 not installed (SDCC package)")


def _report(bad):
    return "\n".join(f"{c.note}: {d[:6]}" for c, d in bad[:10])


@need_s51
def test_every_opcode_boundary_operands_match_ucsim():
    cases = U.opcode_cases(random.Random(1), per_op=1)
    assert len(cases) == 255 * 3
    bad = U.compare(cases)
    assert not bad, _report(bad)


@need_s51
def test_random_instruction_sequences_match_ucsim():
    rnd = random.Random(7)
    cases = [U.random_program_case(rnd, n=40) for _ in range(30)]
    bad = U.compare(cases)
    assert not bad, _report(bad)


SDCC_PROGRAMS = ["models_small", "models_medium", "models_large", "models_small_sa", "models_large_sa",
                 "math_small", "math_large"]


@need_s51
@pytest.mark.parametrize("name", SDCC_PROGRAMS)
def test_sdcc_program_matches_ucsim_state_and_clock_count(name):
    p = CBUILD / f"{name}.ihx"
    if not p.exists():
        pytest.skip(f"{p} not built (firmware/samples_mcs51/c/build.sh)")
    img = load_firmware(p)
    stop = img.symbols["finish"]
    m = U.run_program_model(img, stop)
    r = U.run_program_s51(p, stop)
    assert m.pc == r.pc == stop
    assert m.ticks == r.ticks
    assert m.iram == r.iram
    assert m.xdata == r.xdata


def _run_asm(src: str, steps: int, setup=None):
    r = assemble(" ORG 0x100\n" + src + "\n")
    c = U.ModelBatch.fresh()
    for a, d in r.segments:
        c.write_mem(a, d)
    c.reset(0x100)
    if setup:
        setup(c)
    for _ in range(steps):
        c.run(1)
    return c


def test_intel_behaviour_where_ucsim_deviates():
    """The five ucsim 0.8.15 deviations (UCSIM_DEVIATIONS) - our core follows the Intel MCS-51 definitions."""
    # P always reflects ACC parity: a byte write to PSW cannot set it
    c = _run_asm("MOV A,#03h\nMOV PSW,#01h", 2)
    assert c.psw() & 1 == 0
    # bank switch through a bit instruction takes effect at once
    c = _run_asm("SETB RS0\nMOV R0,#55h", 2)
    assert c.iram[0x08] == 0x55 and c.iram[0x00] == 0
    # MOVC @A+PC / @A+DPTR wrap at 16 bits
    c = _run_asm("MOV DPTR,#0FFF0h\nMOV A,#20h\nMOVC A,@A+DPTR", 3,
                 setup=lambda k: k.write_mem(0x0010, b"\x5a"))
    assert c.sfr[0xE0] == 0x5A
    # PUSH SP stores the incremented SP ("(SP) <- (SP)+1; ((SP)) <- (direct)")
    c = _run_asm("MOV SP,#30h\nPUSH SP", 2)
    assert c.sfr[0x81] == 0x31 and c.iram[0x31] == 0x31
    # SP wraps from FFh to 00h without any error
    c = _run_asm("MOV SP,#0FFh\nPUSH ACC", 2)
    assert c.sfr[0x81] == 0x00
    assert set(U.UCSIM_DEVIATIONS) == {"psw_parity_write", "bank_switch_by_bit", "movc_wrap", "push_sp",
                                       "stack_overflow_stop"}
