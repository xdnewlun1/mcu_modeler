"""Events scheduled from inside an MMIO callback fire exactly when due (not at the end of the running slice), for
every kind of accessing instruction: plain loads/stores, writeback, load/store multiple, IT blocks.  The core ends
the slice at the event's cycle by lowering the instruction limit of the running emu_start (no per-instruction hook);
the accessing instruction completes exactly once."""
from __future__ import annotations

import pytest

from chipem.periph.base import MMIOBlock, Reg
from engine_util import DATA, make_chip

BASE = 0x40010000


class _Sched(MMIOBlock):
    """R (+0) / W (+4): every read of R / write of W schedules an event `delay` cycles after the accessing
    instruction; the event records the cycle it fires at."""

    def __init__(self, delay: int):
        super().__init__("SCHED", BASE, 0x400, [Reg("R", 0, on_read=self._rd), Reg("W", 4, on_write=self._wr),
                                                 Reg("R2", 8, on_read=self._rd)])
        self.delay = delay
        self.due: list[int] = []
        self.fired: list[int] = []
        self.reads = 0
        self.writes = 0

    def _sched(self):
        ev = self.chip.sched.in_cycles(self.delay, self._fire)
        self.due.append(ev.cycles)

    def _fire(self):
        self.fired.append(self.chip.clock.cycles)

    def _rd(self, blk, reg):
        self.reads += 1
        self._sched()
        return 0x1234

    def _wr(self, blk, reg, old, new):
        self.writes += 1
        self._sched()


def _chip(body: str, delay: int = 5, **kw):
    src = f"""
        ldr r1, =0x{BASE:08x}
        ldr r3, ={DATA:#x}
        movs r0, #7
        movs r2, #0
        {body}
        str r1, [r3]
        str r0, [r3, #4]
    loop:
        adds r2, #1
        b loop
    """
    chip, _ = make_chip({"reset": src}, max_slice=kw.pop("max_slice", 5000), **kw)
    blk = _Sched(delay)
    chip.bus.map_mmio(blk)
    blk.attach(chip)
    chip.peripherals[blk.name] = blk
    return chip, blk


BODIES = {
    "ldr": ("ldr r0, [r1]", 1, 0),
    "ldr_writeback_post": ("ldr r0, [r1], #8", 1, 0),
    "ldr_writeback_pre": ("ldr r0, [r1, #8]!", 1, 0),
    "ldm": ("ldm r1, {r0, r4}", 1, 0),            # R and W: one read hook (W has none)
    "ldm_two_hooks": ("ldm r1!, {r0, r4, r5}", 2, 0),
    "it_ldr": ("cmp r0, r0\n it eq\n ldreq r0, [r1]", 1, 0),
    "it_ldr_then_more": ("cmp r0, r0\n itt eq\n ldreq r0, [r1]\n addeq r0, #1", 1, 0),
    "str": ("str r0, [r1, #4]", 0, 1),
    "str_writeback": ("str r0, [r1, #4]!", 0, 1),
    "stm_writeback": ("adds r1, #4\n stm r1!, {r0}", 0, 1),
    "it_str": ("cmp r0, r0\n itt eq\n streq r0, [r1, #4]\n addeq r0, #1", 0, 1),
}


@pytest.mark.parametrize("precise", [False, True])
@pytest.mark.parametrize("name", list(BODIES))
def test_event_scheduled_by_mmio_access_fires_on_time(name, precise):
    body, nreads, nwrites = BODIES[name]
    chip, blk = _chip(body, precise=precise)
    chip.run_cycles(3000)
    assert blk.reads == nreads and blk.writes == nwrites       # the accessing instruction ran exactly once
    assert blk.due and blk.fired == blk.due[:len(blk.fired)] and len(blk.fired) == len(blk.due)
    # the instruction after the access saw its results once (writeback applied once, IT state kept)
    r1 = chip.bus.read(DATA, 4)
    expect_r1 = {"ldr_writeback_post": BASE + 8, "ldr_writeback_pre": BASE + 8, "ldm_two_hooks": BASE + 12,
                 "str_writeback": BASE + 4, "stm_writeback": BASE + 8}.get(name, BASE)
    assert r1 == expect_r1
    r0 = chip.bus.read(DATA + 4, 4)
    expect_r0 = {"it_ldr_then_more": 0x1235, "it_str": 8, "str": 7, "str_writeback": 7, "stm_writeback": 7}
    assert r0 == expect_r0.get(name, 0x1234)


def test_event_due_now_stops_after_the_access():
    """Due at the accessing instruction itself: it completes first, the event fires right after it."""
    chip, blk = _chip("ldr r0, [r1]", delay=0)
    chip.run_cycles(3000)
    assert blk.fired == [blk.due[0] + 1]


def test_far_event_does_not_cut_the_slice():
    """An event due after the slice end leaves the slice alone (zero cost)."""
    chip, blk = _chip("ldr r0, [r1]", delay=100000, max_slice=2000)
    chip.run_cycles(3000)
    assert blk.fired == [] and len(blk.due) == 1


def test_two_events_in_one_slice():
    """A second access before the first event lowers the limit further; a later one never raises it."""
    chip, blk = _chip("ldr r0, [r1]\n ldr r0, [r1, #8]", delay=40)
    chip.run_cycles(3000)
    assert blk.fired == blk.due and len(blk.due) == 2


def test_clock_and_insn_count_stay_consistent():
    chip, blk = _chip("ldr r0, [r1]", delay=17)
    chip.run_cycles(3000)
    assert chip.clock.cycles == 3000 and chip.insns == 3000


def test_chip_stop_from_a_write_callback_does_not_repeat_the_store():
    """chip.stop() called from this chip's own MMIO write hook (a UART listener, a script) ends the run after the
    store; the store is not executed a second time on resume."""
    chip, blk = _chip("str r0, [r1, #4]!\n str r0, [r1, #4]")
    blk._sched = lambda: chip.stop()
    chip.run_cycles(3000)
    chip.run_cycles(3000)
    assert blk.writes == 1                     # the second store hits +8 (R2: no write hook)
    assert chip.bus.read(DATA, 4) == BASE + 4


# ---------------------------------------------------------------- interpreters (C28x, MCS-51)
from pathlib import Path                                         # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class _Sched16(_Sched):
    def __init__(self, base, delay, unit_bits=8, size=0x40):
        MMIOBlock.__init__(self, "SCHED", base, size, [Reg("R", 0, width=1, on_read=self._rd),
                                                     Reg("W", 1, width=1, on_write=self._wr)], unit_bits=unit_bits)
        self.delay = delay
        self.due, self.fired = [], []
        self.reads = self.writes = 0


def _free(chip, cands, size):
    for a in cands:
        if all(chip.bus.lookup(x) is None for x in range(a, a + size)):
            return a
    raise AssertionError("no free address for the test block")


@pytest.mark.parametrize("access", ["read", "write"])
def test_c28x_event_scheduled_by_mmio_access_fires_on_time(access):
    from chipem.chip import Chip
    from chipem.chipdesc import load_chip
    from chipem.core.c28x.asm import assemble
    chip = Chip(load_chip(ROOT / "chips" / "tms320f28232.yaml"))
    base = _free(chip, [0x3D00, 0x4000, 0x5000, 0x7E00, 0x8E00], 0x40)
    blk = _Sched16(base, 7, unit_bits=16)
    chip.bus.map_mmio(blk)
    blk.attach(chip)
    op = f"MOV AL,@0x{base & 0x3F:x}" if access == "read" else f"MOV @0x{(base & 0x3F) + 1:x},AL"
    src = f"""
        .sect "boot", 0x33FFF6
        LB start
        .sect "code", 0x8000
start:  EALLOW
        MOVW DP,#DP(0x7029)
        MOV @0x7029,#0x0068
        EDIS
        MOVW DP,#DP(0x{base:x})
        {op}
loop:   ADDB AL,#1
        SB loop,UNC
"""
    chip.load_firmware(assemble(src).image())
    chip.reset()
    chip.run_cycles(3000)
    assert (blk.reads, blk.writes) == ((1, 0) if access == "read" else (0, 1))
    assert blk.fired == blk.due


@pytest.mark.parametrize("access", ["read", "write"])
def test_mcs51_event_scheduled_by_mmio_access_fires_on_time(access):
    from m51util import chip_for, program
    from chipem.core.mcs51.core import SFR_BASE
    op = "MOV A,0C0h" if access == "read" else "MOV 0C1h,A"
    c = chip_for(program(f" {op}\nspin: INC R0\n SJMP spin"))
    assert c.bus.lookup(SFR_BASE + 0xC0) is None and c.bus.lookup(SFR_BASE + 0xC1) is None
    blk = MMIOBlock("SCHED", SFR_BASE, 0x100, [Reg("R", 0xC0, width=1, on_read=None), Reg("W", 0xC1, width=1)])
    sched = _Sched(40)                      # 40 oscillator periods (cycle-counted core: the scheduler counts periods)
    blk.regs["R"].on_read = sched._rd
    blk.regs["W"].on_write = sched._wr
    blk.ranges = [(SFR_BASE + 0xC0, 2)]
    c.bus.map_mmio(blk)
    blk.attach(c)
    sched.chip = c
    c.run_cycles(12 * 2000)
    assert (sched.reads, sched.writes) == ((1, 0) if access == "read" else (0, 1))
    assert len(sched.fired) == 1
    # the interpreter ends its slice after the accessing instruction; the event can fire at most one instruction
    # (<= 48 periods) late, never a slice late
    assert 0 <= sched.fired[0] - sched.due[0] < 48
