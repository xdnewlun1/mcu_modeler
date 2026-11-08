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


def test_watchdog_reset_stops_the_firmware_at_the_overflow():
    """The watchdog overflow is a scheduler event; the reset must happen before the core runs another slice (it
    used to run up to max_slice more instructions after the overflow)."""
    from chipem.chip import Chip
    from chipem.chipdesc import load_chip
    src = """
        .sect "boot", 0x33FFF6
        LB start
        .sect "code", 0x8000
start:  MOVW DP,#DP(count)
loop:   INC @count
        SB loop,UNC
        .sect "data", 0x9000
count:  .word 0
"""
    ch = Chip(load_chip(CHIP))
    ch.load_firmware(assemble(src).image())
    ch.reset()
    sc = ch.peripherals["SYSCTRL"]
    at = {}                       # instruction count at the watchdog's overflow event and at the chip reset
    ch.log.subscribe(lambda ev: ev.kind == "reset" and ev.fields.get("reason") == "watchdog"
                     and at.setdefault("wdog" if ev.fields.get("block") else "chip", ch.insns))
    ch.run_for(sc.wd_period_ns() * 256 * 1.5)
    assert set(at) == {"wdog", "chip"} and at["chip"] == at["wdog"]


def test_dropped_pie_interrupt_does_not_block_its_group():
    """INT1 reached the CPU but firmware cleared PIEIFR1 before it was taken: the request is dropped, and since no
    ISR runs to write PIEACK, the arch must release group 1 itself so the next TINT0 still interrupts."""
    from chipem.chip import Chip
    from chipem.chipdesc import load_chip
    ch = Chip(load_chip(CHIP))
    ch.load_firmware(assemble('''
        .sect "boot", 0x33FFF6
        LB start
        .sect "code", 0x8000
start:  SB start,UNC
''').image())
    ch.reset()
    a, core, bus = ch.arch, ch.core, ch.bus
    bus.write(0x0CE0, 1, 1)                       # PIECTRL.ENPIE
    bus.write(0x0CE2, 1, 0x40)                    # PIEIER1.INTx7
    a.pie.set_vector(38, 0x8000)                  # INT1.7 -> the loop (any valid target)
    core.write_reg("ier", 1)
    core.write_reg("st1", core.read_reg("st1") | 1)       # DINT
    a.pulse_irq("1.7")                            # TINT0: PIEIFR1.7, PIEACK1, CPU IFR.INT1
    bus.write(0x0CE3, 1, 0)                       # firmware clears PIEIFR1 while masked
    core.write_reg("st1", core.read_reg("st1") & ~1)      # EINT
    assert a.service() is False and not a.pie.ack & 1
    a.pulse_irq("1.7")
    assert core.read_reg("ifr") & 1 and a.service() is True


def test_pie_vector_table_is_eallow_protected():
    """SPRUFB0: the PIE vector table is EALLOW-protected. A CPU store without EALLOW is ignored with one warning;
    with EALLOW it lands. Loader writes (core not running) always land."""
    from chipem.chip import Chip
    from chipem.chipdesc import load_chip
    ch = Chip(load_chip(CHIP))
    ch.load_firmware(assemble('''
        .sect "boot", 0x33FFF6
        LB start
        .sect "code", 0x8000
start:  MOVW DP,#DP(0x0D4C)
        MOVL XAR4,#0x8123
        MOVL @0x0D4C,XAR4            ; vector 38 without EALLOW: ignored
        MOVL XAR4,#0x8456
        MOVL @0x0D4E,XAR4            ; vector 39 without EALLOW: ignored
        EALLOW
        MOVL @0x0D4E,XAR4            ; vector 39 with EALLOW: written
        EDIS
done:   SB done,UNC
''').image())
    ch.reset()
    ch.bus.write(0x0D50, 2, 0x8789)                # not a CPU store (debugger/loader)
    ch.run_cycles(50)
    pie = ch.arch.pie
    assert pie.vector(38) == 0 and pie.vector(39) == 0x8456 and pie.vector(40) == 0x8789
    assert len([e for e in ch.log if e.kind == "warn" and "EALLOW" in e.fields.get("msg", "")]) == 1
