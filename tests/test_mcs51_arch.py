"""MCS-51 interrupt architecture on the P89CV51 (chipem/arch/mcs51): vectors, IE/EA, the 4 priority levels of IP/IPH,
polling order, nesting, RETI, the one-more-instruction rule, edge vs level external interrupts, LCALL latency,
reset, CPU SFR block on the bus, idle / power-down."""
from __future__ import annotations

from chipem.core.base import StopReason

from m51util import MC_NS, chip_for, iram, program

EXT0, T0, EXT1, T1, SER, T2V, PCAV = 0x03, 0x0B, 0x13, 0x1B, 0x23, 0x3B, 0x33


def test_reset_state_and_cpu_block():
    c = chip_for(program("NOP"))
    assert c.core.pc == 0 and c.core.read_reg("sp") == 0x07
    c.run_cycles(12 * 3)
    assert c.core.read_reg("sp") == 0x6F
    # the core's SFRs are visible on the bus (tools): SFR space tag 0x30000
    assert c.bus.read(0x30081, 1) == 0x6F
    c.bus.write(0x300E0, 1, 0x5A)
    assert c.core.read_reg("a") == 0x5A and c.bus.read(0x300D0, 1) & 1 == 0     # parity of 5Ah (4 ones) = 0
    assert c.bus.read(0x300A8, 1) == 0                                          # IE (arch INTC block)
    assert c.peripherals["CPU"].ranges and c.peripherals["INTC"].ranges


def test_timer0_interrupt_vector_and_latency():
    """TF0 set at the overflow cycle; the LCALL to 000BH is taken after the current instruction and costs 2 machine
    cycles; TF0 is cleared by the hardware when vectoring."""
    c = chip_for(program("""
        MOV TMOD, #02h          ; T0 mode 2
        MOV TH0, #0F0h
        MOV TL0, #0F0h          ; 16 counts to overflow
        SETB ET0
        SETB EA
        SETB TR0
        SJMP $
    """, isrs={T0: " INC 30h\n MOV 31h, TCON\n RETI"}))
    c.run_for(400 * MC_NS)
    assert iram(c, 0x30) >= 1
    assert iram(c, 0x31) & 0x20 == 0                     # TF0 cleared on vectoring
    n = iram(c, 0x30)
    c.run_for(16 * 10 * MC_NS)
    assert iram(c, 0x30) - n in (9, 10, 11)              # one interrupt per 16 machine cycles


def test_priority_levels_and_nesting():
    """A level-1 interrupt preempts a level-0 handler; a same-level one waits; IPH:IP give 4 levels."""
    src = program("""
        MOV IPH, #04h           ; PX1H: INT1 level 2
        SETB PX0                ; INT0 level 1
        SETB IT0
        SETB IT1
        SETB EX0
        SETB EX1
        SETB EA
        SJMP $
    """, isrs={EXT0: " MOV 30h, #1\n MOV R7, #50\n DJNZ R7, $\n MOV 32h, 31h\n RETI",
               EXT1: " MOV 31h, #2\n RETI"})
    c = chip_for(src)
    c.run_for(50e3)
    c.pins.set_external("P3.2", "L")                   # INT0 edge
    c.run_for(20 * MC_NS)
    c.pins.set_external("P3.3", "L")                   # INT1 (higher level) arrives during INT0's handler
    c.run_for(400 * MC_NS)
    assert iram(c, 0x30) == 1 and iram(c, 0x31) == 2 and iram(c, 0x32) == 2      # INT1 ran inside INT0's handler
    assert c.arch.active == []


def test_same_level_polling_order():
    """Two requests at the same level: the earlier one in the polling order (Table 41) is taken first."""
    src = program("""
        SETB IT0
        SETB IT1
        SETB EX1
        SETB EX0
        SJMP $
    """, isrs={EXT0: " INC R0\n MOV @R0, #0\n RETI", EXT1: " INC R0\n MOV @R0, #1\n RETI"})
    c = chip_for(src)
    c.core.write_reg("r0", 0x3F)
    c.run_for(20e3)
    c.pins.set_external("P3.2", "L")
    c.pins.set_external("P3.3", "L")
    c.run_for(5e3)
    assert c.core.read_reg("r0") == 0x3F                # EA = 0: nothing taken
    c.bus.write(0x300A8, 1, c.bus.read(0x300A8, 1) | 0x80)
    c.run_for(200 * MC_NS)
    assert (iram(c, 0x40), iram(c, 0x41)) == (0, 1)


def test_one_more_instruction_after_reti():
    """While INT1 is held low (level mode) its handler is re-entered after every RETI, but one main-loop instruction
    executes in between (Intel: at least one more instruction after RETI)."""
    src = program("""
        CLR IT1
        SETB EX1
        SETB EA
loop:   INC 32h
        SJMP loop
    """, isrs={EXT1: " INC 31h\n RETI"})
    c = chip_for(src)
    c.run_for(10e3)
    c.pins.set_external("P3.3", "L")
    before = iram(c, 0x32)
    c.run_for(1000 * MC_NS)
    runs, main = iram(c, 0x31), (iram(c, 0x32) - before) & 0xFF
    assert runs > 50 and abs(2 * main - runs) <= 2      # one main instruction per pass: INC and SJMP alternate


def test_ie_write_delays_interrupt_by_one_instruction():
    c = chip_for(program("""
        SETB IT0
        MOV 30h, #0
        SETB EX0
        SETB EA                 ; the pending edge is taken after the NEXT instruction
        MOV 30h, #1
        MOV 30h, #2
        SJMP $
    """, isrs={EXT0: " MOV 31h, 30h\n RETI"}))
    c.pins.set_external("P3.2", "L")                   # IE0 latched before EA is set
    c.run_for(200 * MC_NS)
    assert iram(c, 0x31) == 1


def test_level_vs_edge_external_interrupt_flags():
    c = chip_for(program("SETB IT0\n SJMP $"))
    c.run_for(10e3)
    c.pins.set_external("P3.2", "L")
    c.run_for(1e3)
    assert c.peripherals["TIMER01"].get("TCON") & 0x02          # IE0 latched on the edge
    c.pins.set_external("P3.2", "H")
    c.run_for(1e3)
    assert c.bus.read(0x30088, 1) & 0x02                          # stays until vectoring / software
    c2 = chip_for(program("CLR IT1\n SJMP $"))
    c2.run_for(10e3)
    c2.pins.set_external("P3.3", "L")
    c2.run_for(1e3)
    assert c2.bus.read(0x30088, 1) & 0x08
    c2.pins.set_external("P3.3", "H")
    c2.run_for(1e3)
    assert not c2.bus.read(0x30088, 1) & 0x08                     # level mode follows the pin


def test_idle_wakes_on_enabled_interrupt_and_powerdown_on_level_int0():
    c = chip_for(program("""
        MOV TMOD, #01h
        MOV TH0, #0FCh          ; ~1000 machine cycles
        MOV TL0, #18h
        SETB ET0
        SETB EA
        SETB TR0
        ORL PCON, #01h          ; idle
        MOV 30h, #1             ; runs after the ISR
        CLR ET0
        CLR TR0
        CLR IT0
        SETB EX0
        ORL PCON, #02h          ; power-down
        MOV 31h, #1
        SJMP $
    """, isrs={T0: " INC 32h\n RETI", EXT0: " CLR EX0\n INC 33h\n RETI"}))
    c.run_for(500 * MC_NS)
    assert c.sleeping and iram(c, 0x30) == 0
    c.run_for(1000 * MC_NS)
    assert iram(c, 0x32) == 1 and iram(c, 0x30) == 1
    assert c.sleeping                                              # now in power-down
    t01 = c.peripherals["TIMER01"]
    c.run_for(1e6)
    assert c.sleeping and iram(c, 0x31) == 0
    c.pins.set_external("P3.2", "L")                               # level INT0 ends power-down
    c.run_for(200 * MC_NS)
    assert not c.sleeping and iram(c, 0x33) == 1 and iram(c, 0x31) == 1
    assert c.peripherals["SYSCTRL"].get("PCON") & 0x03 == 0 and t01 is not None


def test_edge_int0_does_not_end_powerdown():
    c = chip_for(program("SETB IT0\n SETB EX0\n SETB EA\n ORL PCON, #02h\n MOV 30h, #1\n SJMP $",
                         isrs={EXT0: " INC 31h\n RETI"}))
    c.run_for(20e3)
    assert c.sleeping
    c.pins.set_external("P3.2", "L")
    c.run_for(50e3)
    assert c.sleeping and iram(c, 0x30) == 0                       # data sheet: level-sensitive interrupts only


def test_watchdog_reset_cause_and_pof():
    c = chip_for(program("MOV A, PCON\n MOV 30h, A\n ANL PCON, #0EFh\n MOV WDTRST, #1Eh\n MOV WDTRST, #0E1h\n SJMP $"))
    c.run_for(1e6)
    assert iram(c, 0x30) & 0x10                                    # POF after power-on
    c.run_for(20e6)                                                # 16383 machine cycles = 17.8 ms
    assert any(e.kind == "reset" and e.fields.get("reason") == "wdt" for e in c.log)
    c.run_for(100 * MC_NS)
    assert not iram(c, 0x30) & 0x10                                # POF stays clear after the WDT reset


def test_reti_stop_reason_and_vector_table():
    c = chip_for(program("SJMP $", isrs={T0: " RETI", SER: " RETI"}))
    vt = {v["name"]: v for v in c.arch.vector_table()}
    assert vt["TIMER0"]["addr"] == 0x0B and vt["SERIAL"]["target"] > 0x50
    assert c.arch.exc_label(5) == "0x002B" and c.arch.exc_name(5) == "TIMER2" and c.arch.exc_name(6) == "PCA"
    c.core.write_mem(0x200, b"\x32")
    c.core.pc = 0x200
    c.core.iram[8], c.core.iram[9] = 0x34, 0x12
    c.core.sfr[0x81] = 9
    r, _ = c.core.run(1)
    assert r is StopReason.EXC_RETURN and c.core.pc == 0x1234
