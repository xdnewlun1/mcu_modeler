"""Exception model: SysTick, NVIC priorities, BASEPRI/PRIMASK, SVC, PendSV, FP frames, EXC_RETURN forms, reset."""
import pytest

from chipem.core import StopReason

from engine_util import (CPSID_I, CPSIE_I, DATA, STACK_TOP, make_chip, mrs, msr, read_log, record, word)

SYST = 0xE000E010
NVIC_ISER = 0xE000E100
NVIC_ISPR = 0xE000E200
NVIC_IPR = 0xE000E400
ICSR = 0xE000ED04
AIRCR = 0xE000ED0C
SHPR3 = 0xE000ED20


def systick_setup(reload: int) -> str:
    return f"""
  ldr r0, =0x{SYST:08X}
  ldr r1, ={reload}
  str r1, [r0, #4]
  movs r1, #7
  str r1, [r0]
"""


COUNT_HANDLER = f"""
  ldr r0, =0x{DATA + 0x80:08X}
  ldr r1, [r0]
  adds r1, #1
  str r1, [r0]
  ldr r0, =0xDEAD0000
  mov r1, r0
  mov r2, r0
  mov r3, r0
  mov r12, r0
  adds r0, #1
  bx lr
"""


def run_to_thread(chip, limit=10_000):
    for _ in range(limit):
        if not chip.arch.active:
            return
        chip.run_cycles(1)
    raise AssertionError("never returned to thread mode")


@pytest.mark.parametrize("misalign", [False, True])
@pytest.mark.parametrize("precise", [False, True])
def test_systick_frame_round_trip(precise, misalign):
    adjust = "sub sp, #4" if misalign else "nop"
    thread = systick_setup(99) + f"""
  {adjust}
  ldr r0, =0x11111111
  ldr r1, =0x22222222
  ldr r2, =0x33333333
  ldr r3, =0x44444444
  ldr r4, =0x55555555
  mov r12, r4
  ldr r4, =0x66666666
  mov lr, r4
  movs r4, #0
  cmp r4, #1
spin:
  b spin
"""
    chip, a = make_chip({"reset": thread, "tick": COUNT_HANDLER}, vectors={15: "tick"}, precise=precise)
    chip.run_cycles(40)                       # setup done, first tick at ~100 cycles
    assert not chip.arch.active
    names = ["r0", "r1", "r2", "r3", "r12", "lr", "pc", "sp"]
    before = dict(zip(names, chip.core.read_regs(names)))
    flags_before = chip.core.read_reg("xpsr") & 0xF8000000
    assert before["sp"] == STACK_TOP - (4 if misalign else 0)
    chip.run_cycles(5000)
    run_to_thread(chip)
    assert word(chip, DATA + 0x80) >= 45
    after = dict(zip(names, chip.core.read_regs(names)))
    assert after == before
    assert chip.core.read_reg("xpsr") & 0xF8000000 == flags_before
    assert chip.core.read_reg("msp") == before["sp"]


def test_systick_period_and_registers():
    chip, a = make_chip({"reset": systick_setup(999) + "spin:\n  b spin", "tick": COUNT_HANDLER},
                        vectors={15: "tick"})
    chip.run_cycles(10_500)
    assert word(chip, DATA + 0x80) == 10
    scs = chip.arch.scs
    assert chip.bus.read(SYST + 4, 4) == 999
    cvr = chip.bus.read(SYST + 8, 4)
    assert 0 <= cvr <= 999
    assert chip.bus.read(0xE000ED00, 4) == 0x410FC241
    assert scs.get("SYST_CALIB") & 0x40000000


def test_systick_countflag_polling_without_interrupt():
    thread = f"""
  ldr r0, =0x{SYST:08X}
  ldr r1, =499
  str r1, [r0, #4]
  movs r1, #5
  str r1, [r0]
  movs r5, #0
poll:
  ldr r1, [r0]
  lsls r1, r1, #15
  bpl poll
  adds r5, #1
  cmp r5, #4
  bne poll
  ldr r0, =0x{DATA:08X}
  str r5, [r0]
done:
  b done
"""
    chip, _ = make_chip({"reset": thread}, precise=True)
    chip.run_cycles(1900)
    assert word(chip, DATA) == 0
    chip.run_cycles(300)
    assert word(chip, DATA) == 4


NESTED_SETUP = f"""
  ldr r0, =0x{NVIC_IPR:08X}
  movs r1, #0x80
  strb r1, [r0]
  movs r1, #0x20
  strb r1, [r0, #1]
  ldr r0, =0x{NVIC_ISER:08X}
  movs r1, #3
  str r1, [r0]
"""


def test_nested_preemption_by_higher_priority():
    thread = NESTED_SETUP + f"""
  ldr r0, =0x{NVIC_ISPR:08X}
  movs r1, #1
  str r1, [r0]
""" + record(9) + "spin:\n  b spin"
    low = "push {lr}\n" + record(1) + f"""
  ldr r0, =0x{NVIC_ISPR:08X}
  movs r1, #2
  str r1, [r0]
""" + record(3) + "pop {pc}"
    high = "push {lr}\n" + record(2) + "pop {pc}"
    chip, _ = make_chip({"reset": thread, "low": low, "high": high}, vectors={16: "low", 17: "high"})
    chip.run_cycles(3000)
    assert read_log(chip) == [1, 2, 3, 9]
    assert not chip.arch.active and not chip.arch.pending
    assert chip.core.read_reg("msp") == STACK_TOP


def test_lower_priority_waits_for_higher():
    thread = NESTED_SETUP + f"""
  ldr r0, =0x{NVIC_ISPR:08X}
  movs r1, #2
  str r1, [r0]
spin:
  b spin
"""
    high = "push {lr}\n" + record(1) + f"""
  ldr r0, =0x{NVIC_ISPR:08X}
  movs r1, #1
  str r1, [r0]
""" + record(2) + "pop {pc}"
    low = "push {lr}\n" + record(3) + "pop {pc}"
    chip, _ = make_chip({"reset": thread, "low": low, "high": high}, vectors={16: "low", 17: "high"})
    chip.run_cycles(3000)
    assert read_log(chip) == [1, 2, 3]
    # ICSR / IABR reflect state while idle
    assert chip.bus.read(ICSR, 4) & 0x1FF == 0


def test_basepri_masks_lower_priorities():
    thread = NESTED_SETUP + f"""
  movs r0, #0x40
  {msr("basepri", 0)}
  ldr r0, =0x{NVIC_ISPR:08X}
  movs r1, #3
  str r1, [r0]
""" + record(10) + """
  ldr r4, =1000
wait:
  subs r4, #1
  bne wait
""" + record(11) + f"""
  movs r0, #0
  {msr("basepri", 0)}
  ldr r4, =1000
wait2:
  subs r4, #1
  bne wait2
""" + record(12) + "spin:\n  b spin"
    low = "push {lr}\n" + record(1) + "pop {pc}"
    high = "push {lr}\n" + record(2) + "pop {pc}"
    chip, _ = make_chip({"reset": thread, "low": low, "high": high}, vectors={16: "low", 17: "high"})
    chip.run_cycles(10_000)
    # high (0x20 < BASEPRI 0x40) is taken right away; low only once BASEPRI is cleared
    assert read_log(chip) == [2, 10, 11, 1, 12]


def test_primask_defers_interrupts():
    thread = NESTED_SETUP + f"""
  {CPSID_I}
  ldr r0, =0x{NVIC_ISPR:08X}
  movs r1, #2
  str r1, [r0]
  ldr r4, =1000
wait:
  subs r4, #1
  bne wait
""" + record(10) + f"""
  {CPSIE_I}
  ldr r4, =1000
wait2:
  subs r4, #1
  bne wait2
""" + record(11) + "spin:\n  b spin"
    high = "push {lr}\n" + record(2) + "pop {pc}"
    chip, _ = make_chip({"reset": thread, "high": high}, vectors={17: "high"})
    chip.run_cycles(10_000)
    assert read_log(chip) == [10, 2, 11]


def test_svc_call_and_return():
    thread = f"""
  movs r0, #5
  svc #3
  ldr r1, =0x{DATA:08X}
  str r0, [r1]
spin:
  b spin
"""
    handler = f"""
  ldr r0, [sp]
  adds r0, #10
  str r0, [sp]
  ldr r1, [sp, #24]
  subs r1, #2
  ldrb r1, [r1]
  ldr r2, =0x{DATA + 4:08X}
  str r1, [r2]
  str lr, [r2, #4]
  bx lr
"""
    chip, _ = make_chip({"reset": thread, "svc": handler}, vectors={11: "svc"})
    chip.run_cycles(200)
    assert word(chip, DATA) == 15
    assert word(chip, DATA + 4) == 3
    assert word(chip, DATA + 8) == 0xFFFFFFF9
    assert chip.core.read_reg("msp") == STACK_TOP


def test_pendsv_via_icsr_is_taken_immediately():
    thread = f"""
  ldr r0, =0x{ICSR:08X}
  ldr r1, =0x10000000
  str r1, [r0]
""" + record(2) + "spin:\n  b spin"
    pendsv = "push {lr}\n" + record(1) + "pop {pc}"
    chip, _ = make_chip({"reset": thread, "pendsv": pendsv}, vectors={14: "pendsv"})
    chip.run_cycles(2000)
    assert read_log(chip) == [1, 2]


def test_thread_on_psp_and_unprivileged():
    thread = f"""
  ldr r0, =0x20006000
  {msr("psp", 0)}
  movs r0, #3
  {msr("control", 0)}
  isb
""" + systick_setup(99) + f"""
  movs r5, #0
spin:
  adds r5, #1
  b spin
"""
    tick = f"""
  ldr r0, =0x{DATA:08X}
  str lr, [r0]
  {mrs(1, "psp")}
  str r1, [r0, #4]
  {mrs(1, "control")}
  str r1, [r0, #8]
  bx lr
"""
    chip, _ = make_chip({"reset": thread, "tick": tick}, vectors={15: "tick"})
    chip.run_cycles(1000)
    run_to_thread(chip)
    assert word(chip, DATA) == 0xFFFFFFFD
    assert word(chip, DATA + 4) == 0x20006000 - 0x20
    assert word(chip, DATA + 8) & 3 == 1                 # SPSEL cleared in handler, nPRIV kept
    regs = chip.core.read_regs(["sp", "control"])
    assert regs == [0x20006000, 3]
    assert chip.arch.state()["active"] == []


def test_fp_extended_frame_round_trip():
    setup = "\n".join(f"  ldr r0, =0x{0x3F800000 + i * 0x1000:08X}\n  vmov s{i}, r0" for i in range(16))
    thread = setup + f"""
  ldr r0, =0x03C00000
  vmsr fpscr, r0
  ldr r0, =0x{ICSR:08X}
  ldr r1, =0x10000000
  str r1, [r0]
  ldr r0, =0x{DATA + 0x40:08X}
  vstmia r0, {{s0-s15}}
  vmrs r1, fpscr
  str r1, [r0, #64]
spin:
  b spin
"""
    clobber = "\n".join(f"  vmov s{i}, r1" for i in range(16))
    pendsv = f"""
  ldr r0, =0x{DATA:08X}
  str lr, [r0]
  {mrs(3, "control")}
  str r3, [r0, #4]
  mov r2, sp
  str r2, [r0, #8]
  ldr r1, =0x12345678
{clobber}
  movs r1, #0
  vmsr fpscr, r1
  bx lr
"""
    chip, _ = make_chip({"reset": thread, "pendsv": pendsv}, vectors={14: "pendsv"}, cpu="cortex-m4", fpu=True)
    chip.run_cycles(1000)
    assert word(chip, DATA) == 0xFFFFFFE9                # FP frame, thread, MSP
    assert word(chip, DATA + 4) & 4 == 0                  # FPCA cleared on entry
    assert word(chip, DATA + 8) == STACK_TOP - 0x68       # 26-word frame
    vals = [word(chip, DATA + 0x40 + 4 * i) for i in range(16)]
    assert vals == [0x3F800000 + i * 0x1000 for i in range(16)]
    assert word(chip, DATA + 0x80) == 0x03C00000
    assert chip.core.read_reg("control") & 4               # FPCA restored
    assert chip.core.read_reg("sp") == STACK_TOP


def test_no_fp_frame_without_fpca_and_on_m3():
    thread = f"""
  ldr r0, =0x{ICSR:08X}
  ldr r1, =0x10000000
  str r1, [r0]
spin:
  b spin
"""
    pendsv = f"""
  ldr r0, =0x{DATA:08X}
  str lr, [r0]
  bx lr
"""
    for cpu, fpu in (("cortex-m4", True), ("cortex-m3", False)):
        chip, _ = make_chip({"reset": thread, "pendsv": pendsv}, vectors={14: "pendsv"}, cpu=cpu, fpu=fpu,
                            arch="armv7-m" if cpu == "cortex-m3" else "armv7e-m")
        chip.run_cycles(200)
        assert word(chip, DATA) == 0xFFFFFFF9
        if cpu == "cortex-m3":
            assert chip.bus.read(0xE000ED00, 4) == 0x410FC231


def test_pop_pc_exc_return_and_tail_chain():
    thread = systick_setup(199) + """
  movs r5, #0
spin:
  adds r5, #1
  b spin
"""
    tick = f"""
  push {{r4, lr}}
  bl bump
  ldr r0, =0x{ICSR:08X}
  ldr r1, =0x10000000
  str r1, [r0]
  pop {{r4, pc}}
bump:
  ldr r0, =0x{DATA:08X}
  ldr r1, [r0]
  adds r1, #1
  str r1, [r0]
  bx lr
"""
    pendsv = f"""
  ldr r0, =0x{DATA + 4:08X}
  ldr r1, [r0]
  adds r1, #1
  str r1, [r0]
  bx lr
"""
    chip, _ = make_chip({"reset": thread, "tick": tick, "pendsv": pendsv}, vectors={15: "tick", 14: "pendsv"})
    chip.run_cycles(2050)
    run_to_thread(chip)
    assert word(chip, DATA) == 10
    assert word(chip, DATA + 4) == 10
    assert chip.core.read_reg("sp") == STACK_TOP
    assert chip.core.read_reg("r5") > 0


def test_sysresetreq_resets_chip():
    thread = f"""
  ldr r0, =0x{DATA:08X}
  ldr r1, [r0]
  adds r1, #1
  str r1, [r0]
  cmp r1, #3
  beq done
  ldr r0, =0x{AIRCR:08X}
  ldr r1, =0x05FA0004
  str r1, [r0]
  movs r7, #1
done:
  b done
"""
    chip, _ = make_chip({"reset": thread})
    chip.run_cycles(500)
    assert word(chip, DATA) == 3
    resets = [e for e in chip.log if e.kind == "reset"]
    assert [e.get("reason") for e in resets] == ["reset", "SYSRESETREQ", "SYSRESETREQ"]
    assert chip.core.read_reg("r7") == 0                  # reset happened right after the store
    assert chip.bus.read(AIRCR, 4) >> 16 == 0xFA05


def test_wrong_vectkey_ignored_and_prigroup():
    thread = f"""
  ldr r0, =0x{AIRCR:08X}
  ldr r1, =0x12340004
  str r1, [r0]
  ldr r1, =0x05FA0500
  str r1, [r0]
spin:
  b spin
"""
    chip, _ = make_chip({"reset": thread})
    chip.run_cycles(100)
    assert chip.arch.prigroup == 5
    assert chip.bus.read(AIRCR, 4) == 0xFA050500
    assert [e.get("reason") for e in chip.log if e.kind == "reset"] == ["reset"]


def test_wfi_sleeps_until_interrupt():
    thread = systick_setup(9999) + """
sleep:
  wfi
  b sleep
"""
    chip, _ = make_chip({"reset": thread, "tick": COUNT_HANDLER}, vectors={15: "tick"})
    r = chip.run_cycles(100_100)
    assert r is StopReason.COUNT
    assert word(chip, DATA + 0x80) == 10
    assert chip.clock.cycles == 100_100
    assert chip.sleeping


def test_stub_peripheral_first_touch_and_catch_all():
    thread = """
  ldr r0, =0x40010000
  movs r1, #1
  str r1, [r0, #4]
  str r1, [r0, #4]
  ldr r2, [r0, #8]
  ldr r3, [r0, #4]
  ldr r0, =0x40080000
  str r1, [r0]
  ldr r4, [r0]
spin:
  b spin
"""
    chip, _ = make_chip({"reset": thread}, peripherals=[dict(name="FOO", model="stub", base=0x40010000),
                                                         dict(name="BAR", model="kinetis.nothere", base=0x40011000)])
    r = chip.run_cycles(100)
    assert r is StopReason.COUNT
    assert chip.core.read_reg("r3") == 1 and chip.core.read_reg("r4") == 1
    mmio = [e for e in chip.log if e.kind == "mmio" and e.get("block") == "FOO"]
    assert [e.get("offset") for e in mmio] == [4, 8]
    warns = [e for e in chip.log if e.kind == "warn"]
    assert any(e.get("model") == "kinetis.nothere" for e in warns)
    assert any(e.get("addr") == 0x40080000 for e in warns)
    assert "BAR" in chip.peripherals


def test_bus_fault_enters_hardfault():
    thread = """
  ldr r0, =0x60000000
  ldr r1, [r0]
spin:
  b spin
"""
    hf = f"""
  ldr r0, =0xE000ED28
  ldr r1, [r0]
  ldr r2, [r0, #4]
  ldr r3, [r0, #16]
  ldr r0, =0x{DATA:08X}
  str r1, [r0]
  str r2, [r0, #4]
  str r3, [r0, #8]
  str lr, [r0, #12]
hang:
  b hang
"""
    chip, a = make_chip({"reset": thread, "hf": hf}, vectors={3: "hf"})
    r = chip.run_cycles(200)
    assert r is StopReason.COUNT
    assert word(chip, DATA) & 0x8200 == 0x8200            # PRECISERR | BFARVALID
    assert word(chip, DATA + 4) & (1 << 30)               # FORCED
    assert word(chip, DATA + 8) == 0x60000000
    assert word(chip, DATA + 12) == 0xFFFFFFF9
    assert chip.arch.active == [3]
    assert any(e.kind == "fault" for e in chip.log)


def test_fault_without_handler_stops():
    chip, _ = make_chip({"reset": "ldr r0, =0x60000000\nldr r1, [r0]\nspin:\n  b spin"})
    assert chip.run_cycles(200) is StopReason.FAULT
    assert chip.last_fault["addr"] == 0x60000000


def test_precise_and_fast_mode_time_agree():
    thread = systick_setup(299) + """
  movs r5, #0
spin:
  adds r5, #1
  b spin
"""
    res = []
    for precise in (False, True):
        chip, _ = make_chip({"reset": thread, "tick": COUNT_HANDLER}, vectors={15: "tick"}, precise=precise)
        chip.run_cycles(30_100)
        res.append((word(chip, DATA + 0x80), chip.core.read_reg("r5")))
    assert res[0] == res[1]
    assert res[0][0] == 100


def test_stop_from_other_thread():
    import threading
    chip, _ = make_chip({"reset": "spin:\n  b spin"})
    t = threading.Timer(0.05, chip.stop)
    t.start()
    r = chip.run_for(1e12)          # would take forever
    assert r is StopReason.USER
    t.join()


def test_step_and_state():
    chip, a = make_chip({"reset": "movs r0, #1\nmovs r0, #2\nspin:\n  b spin"})
    chip.step()
    assert chip.core.read_reg("r0") == 1
    chip.step()
    st = chip.state()
    assert st["pc"] == a["reset"] + 4 and st["sp"] == STACK_TOP and st["mode"] == "thread" and st["cycles"] == 2


def test_armv6m_cortex_m0_systick():
    thread = systick_setup(99) + """
  movs r5, #0
spin:
  adds r5, #1
  b spin
"""
    chip, _ = make_chip({"reset": thread, "tick": COUNT_HANDLER}, vectors={15: "tick"}, cpu="cortex-m0",
                        fpu=False, arch="armv6-m")
    chip.run_cycles(1050)
    run_to_thread(chip)
    assert word(chip, DATA + 0x80) == 10
    assert chip.core.read_reg("sp") == STACK_TOP
    assert chip.bus.read(0xE000ED00, 4) == 0x410CC200
    assert chip.arch.prio_mask == 0xC0


# ----------------------------------------------------------------------------- level-sensitive IRQ lines
def _flag_chip(clear_in_handler: bool):
    """IRQ0 is driven by a 1-register test peripheral at 0x40000000: writing 1 sets the flag (line high), writing
    0 clears it (line low).  The handler counts entries and clears the flag only if DATA+0x84 is non-zero."""
    from chipem.periph.base import MMIOBlock, Reg

    class Flag(MMIOBlock):
        def __init__(self):
            super().__init__("FLAG", 0x40000000, 0x1000, [Reg("F", 0, on_write=self._w)])

        def _w(self, blk, reg, old, new):
            self.irq(0, bool(new & 1))

    code = {
        "reset": f"""
  ldr r0, ={NVIC_ISER:#x}
  movs r1, #1
  str r1, [r0]
  ldr r0, =0x40000000
  movs r1, #1
  str r1, [r0]
  ldr r0, ={DATA + 0x88:#x}
loop:
  ldr r1, [r0]
  adds r1, #1
  str r1, [r0]
  b loop
""",
        "isr": f"""
  ldr r0, ={DATA + 0x80:#x}
  ldr r1, [r0]
  adds r1, #1
  str r1, [r0]
  ldr r1, [r0, #4]
  cbz r1, done
  ldr r0, =0x40000000
  movs r1, #0
  str r1, [r0]
done:
  bx lr
"""}
    chip, _ = make_chip(code, vectors={16: "isr"})
    blk = Flag()
    chip.bus.map_mmio(blk)
    blk.attach(chip)
    chip.peripherals[blk.name] = blk
    chip.bus.write(DATA + 0x84, 4, 1 if clear_in_handler else 0)
    return chip


@pytest.mark.parametrize("clear", [False, True])
def test_level_irq_reentered_until_flag_cleared(clear):
    chip = _flag_chip(clear)
    chip.run_cycles(20_000)
    entries, thread_loops = word(chip, DATA + 0x80), word(chip, DATA + 0x88)
    if clear:
        assert entries == 1                       # flag cleared in the ISR: line low, not re-entered
        st = chip.state()
        assert 0 < st["insns"] <= st["cycles"] == chip.clock.cycles   # executed instructions are tracked
        assert thread_loops > 1000
        assert 16 not in chip.arch.pending and 16 not in chip.arch.asserted
    else:
        assert entries > 500                      # handler returns with the line still high: re-entered at once
        assert thread_loops == 0                  # ... so thread mode never runs again
        assert 16 in chip.arch.asserted


def test_level_irq_icpr_and_iser_repend_while_asserted():
    chip, _ = make_chip({"reset": "b ."})
    arch = chip.arch
    arch.set_irq(1, True)                          # line high while the IRQ is disabled: pending latched
    assert 17 in arch.pending
    chip.bus.write(0xE000E280, 4, 2)               # ICPR: still asserted -> pending again
    assert 17 in arch.pending
    arch.pending.discard(17)
    chip.bus.write(NVIC_ISER, 4, 2)                # enabling an asserted line pends it
    assert 17 in arch.pending
    arch.set_irq(1, False)                         # deassert keeps the latched pending state (ARM: until taken/ICPR)
    assert 17 in arch.pending and 17 not in arch.asserted
    chip.bus.write(0xE000E280, 4, 2)               # ICPR with the line low: pending cleared for good
    assert 17 not in arch.pending
    chip.bus.write(NVIC_ISPR, 4, 2)                # software pend does not assert the line
    assert 17 in arch.pending and 17 not in arch.asserted


def test_irq_taken_right_after_cpsie_in_wfi_idle_loop():
    # `cpsid i; wfi; cpsie i; b` : the pending SysTick must be taken in the 2-instruction unmasked window
    loop = f"""
idle:
  {CPSID_I}
  wfi
  {CPSIE_I}
  b idle
"""
    chip, _ = make_chip({"reset": systick_setup(999) + loop, "tick": COUNT_HANDLER}, vectors={15: "tick"})
    chip.run_cycles(20_500)
    assert word(chip, DATA + 0x80) == 20


def test_faultmask_cleared_on_exception_return():
    handler = """
  .short 0xB671
  bx lr
"""
    chip, _ = make_chip({"reset": systick_setup(499) + "spin:\n  b spin", "tick": handler}, vectors={15: "tick"})
    chip.run_cycles(2_000)
    assert chip.arch.entries >= 3                  # FAULTMASK did not stay set after the first return
    assert chip.core.read_reg("faultmask") == 0 or chip.arch.active


def _systick_src0(reload: int) -> str:
    """SysTick with CLKSOURCE=0 (external reference) and TICKINT."""
    return f"""
  ldr r0, =0x{SYST:08X}
  ldr r1, ={reload}
  str r1, [r0, #4]
  movs r1, #3
  str r1, [r0]
"""


@pytest.mark.parametrize("div", [None, 4])
def test_systick_clksource0_counts_at_external_reference(div):
    """CLKSOURCE=0 counts at core/systick_ext_div (default 8: STM32 HCLK/8); CLKSOURCE=1 at the core clock."""
    chip, _ = make_chip({"reset": _systick_src0(99) + "spin:\n  b spin", "tick": COUNT_HANDLER},
                        vectors={15: "tick"})
    scs = chip.arch.scs
    if div is not None:
        scs.ext_div = div
    d = div or 8
    chip.run_cycles(100 * d * 5 + 50)
    assert word(chip, DATA + 0x80) == 5
    cvr = chip.bus.read(SYST + 8, 4)
    assert 0 <= cvr <= 99


def test_systick_clksource_switch_while_running():
    chip, _ = make_chip({"reset": _systick_src0(999) + "spin:\n  b spin"})
    chip.run_cycles(8 * 400)                               # 400 ticks of the /8 reference
    cvr0 = chip.bus.read(SYST + 8, 4)
    chip.bus.write(SYST, 4, 1 | 4)                         # ENABLE | CLKSOURCE (no TICKINT)
    chip.run_cycles(100)                                   # now one count per core cycle
    cvr1 = chip.bus.read(SYST + 8, 4)
    assert abs((cvr0 - cvr1) - 100) <= 12


def test_systick_no_external_reference_k22():
    """systick_ext_div: 0 (K22, RM 3.2.1.2): CLKSOURCE is fixed at 1, SYST_CALIB = NOREF only, CLKSOURCE=0 writes
    still count at the core clock."""
    from chipem.chip import Chip
    from engine_util import build_image, make_desc
    desc = make_desc()
    desc.extra["systick_ext_div"] = 0
    chip = Chip(desc)
    img, _ = build_image({"reset": _systick_src0(99) + "spin:\n  b spin", "tick": COUNT_HANDLER}, vectors={15: "tick"})
    chip.load_firmware(img)
    chip.reset()
    assert chip.bus.read(SYST + 0xC, 4) == 0x80000000          # NOREF=1, SKEW=0, TENMS=0
    assert chip.bus.read(SYST, 4) & 4                         # CLKSOURCE reads 1 out of reset
    chip.run_cycles(100 * 5 + 50)
    assert word(chip, DATA + 0x80) == 5                      # one count per core cycle despite the write of 0
    assert chip.bus.read(SYST, 4) & 7 == 7


def test_systick_calib_tenms_counts_the_external_reference():
    """TENMS is the 10 ms reload of the CLKSOURCE=0 reference (core / systick_ext_div), from the chip's nominal
    clock; `systick_calib` in the chip YAML overrides the register."""
    from chipem.chip import Chip
    from engine_util import make_desc
    desc = make_desc()
    desc.core.clock_hz = 16e6
    desc.extra["systick_ext_div"] = 8
    chip = Chip(desc, clock_hz=48e6)                           # running clock is not the reference
    assert chip.bus.read(SYST + 0xC, 4) == 0x40000000 | 19999  # 16 MHz / 8 = 2 MHz -> 20000 counts per 10 ms
    desc.extra["systick_calib"] = "0x00002903"
    assert Chip(desc).bus.read(SYST + 0xC, 4) == 0x2903
