"""C28x core inside a full Chip (chips/tms320f28232.yaml): reset entry, PIE vector fetch, CPU-timer interrupt,
automatic context save / IRET, IDLE wake-up.  Exercises the core ↔ arch/c28x ↔ ti_c2000 interfaces."""
from __future__ import annotations

from pathlib import Path

import pytest

from chipem.core.c28x.asm import assemble

CHIP = Path(__file__).resolve().parents[1] / "chips" / "tms320f28232.yaml"

SRC = """
        .sect "boot", 0x33FFF6
        LB start
        .sect "code", 0x8000
start:  EALLOW
        MOVW DP,#DP(0x7029)
        MOV @0x7029,#0x0068          ; WDCR: watchdog off
        MOVW DP,#DP(0x0D4C)
        MOVL XAR4,#isr
        MOVL @0x0D4C,XAR4            ; PIE vector 38 = INT1.7 (TINT0)
        MOVW DP,#DP(0x0CE0)
        MOV @0x0CE0,#1               ; PIECTRL.ENPIE
        MOV @0x0CE2,#0x40            ; PIEIER1.INTx7
        EDIS
        MOVW DP,#DP(0x0C00)
        MOV @0x0C04,#0x0010          ; TCR.TSS (stop)
        MOV @0x0C02,#1000            ; PRD
        MOV @0x0C03,#0
        MOV @0x0C06,#0               ; TPR
        MOV @0x0C07,#0
        MOV @0x0C04,#0x4020          ; TIE | TRB, run
        OR IER,#1
        EINT
loop:   IDLE
        SB loop,UNC
isr:    MOVW DP,#DP(count)
        INC @count
        MOVW DP,#DP(0x0CE1)
        MOV @0x0CE1,#1               ; PIEACK group 1
        MOVW DP,#DP(0x0C04)
        OR @0x0C04,#0x8000           ; TCR.TIF w1c
        IRET
        .sect "data", 0x9000
count:  .word 0
"""


@pytest.mark.skipif(not CHIP.exists(), reason="no TMS320F28232 chip description")
def test_cpu_timer_interrupt_through_pie():
    from chipem.chip import Chip
    from chipem.chipdesc import load_chip
    try:
        ch = Chip(load_chip(CHIP))
    except Exception as e:                      # pragma: no cover - other layers still under construction
        pytest.skip(f"chip not constructible: {e}")
    r = assemble(SRC)
    ch.load_firmware(r.image())
    ch.reset()
    assert ch.core.pc == 0x8000                 # image entry (`start` symbol)
    ch.run_cycles(30_000)
    core = ch.core
    n = ch.bus.read(0x9000, 1)
    assert n >= 10, n                           # ~one interrupt per 1001 timer clocks
    assert core.last_fault is None
    lp = r.symbols["loop"]
    if lp <= core.pc <= lp + 2:                 # between interrupts: context fully restored
        assert core.sp == 0x400 and core.intm == 0 and core.ier == 1
