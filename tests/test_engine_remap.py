"""Mapping changes made from inside an MMIO callback (an external memory controller opening/closing a window when
firmware writes its control register). Unicorn 2.1.4 survives uc_mem_unmap inside a memory hook only until that
emu_start ends: it then raises a spurious UC_HOOK_INTR intno 3 (EXCP_PREFETCH_ABORT) at the next instruction, which
looked like a prefetch abort on the next fetch. The core defers mapping changes made while it runs to the end of the
accessing instruction (the slice ends there), so the next instruction sees the new mapping and nothing faults."""
from __future__ import annotations

import itertools

import pytest

from chipem.periph.base import MMIOBlock, Reg
from engine_util import DATA, make_chip

BASE = 0x40010000
WIN = 0xC0000000


def _kw(kind, buf):
    if kind == "buf":
        return dict(buffer=buf, period=len(buf))
    return dict(read=lambda o, n: 0x1234, write=lambda o, n, v: None)


@pytest.mark.parametrize("access", ["w", "r"])
@pytest.mark.parametrize("before,after", [(a, b) for a, b in itertools.product(("buf", "cb", None), repeat=2)
                                          if a or b])
@pytest.mark.parametrize("size", [0x1000, 0x10000000])
def test_window_change_inside_mmio_callback(access, before, after, size):
    buf = bytearray(min(size, 0x10000))
    buf[0:4] = (0xCAFEBABE).to_bytes(4, "little")
    op = "str r0, [r1]" if access == "w" else "ldr r0, [r1]"
    # the window is read by the very next instruction; with no window afterwards that read faults (no handler)
    probe = "ldr r4, [r5]" if after else "movs r4, #5"
    src = f"""
        ldr r1, ={BASE:#x}
        ldr r3, ={DATA:#x}
        ldr r5, ={WIN:#x}
        movs r0, #1
        movs r4, #0
        {op}
        {probe}
        str r4, [r3]
    loop:
        adds r4, #1
        b loop
    """
    chip, _ = make_chip({"reset": src})

    def hook(*a):
        chip.bus.unmap_window("W")
        if after:
            chip.bus.map_window("W", WIN, size, **_kw(after, buf))
    blk = MMIOBlock("CTL", BASE, 0x400, [Reg("C", 0, on_write=hook if access == "w" else None,
                                             on_read=hook if access == "r" else None)])
    chip.bus.map_mmio(blk)
    blk.attach(chip)
    if before:
        chip.bus.map_window("W", WIN, size, **_kw(before, buf))
    r = chip.run_cycles(300)
    assert chip.last_fault is None, chip.last_fault
    assert r.value == "count"
    assert chip.bus.read(DATA, 4) == {"buf": 0xCAFEBABE, "cb": 0x1234, None: 5}[after]
    assert chip.insns == 300


def test_code_runs_from_a_window_opened_by_a_callback():
    """Firmware enables the window and jumps into it in the next instruction (code in external RAM)."""
    from engine_util import asm
    WIN = 0x60000000                     # external RAM region (0xC0000000 is Device / execute-never on Cortex-M)
    code = asm("movs r4, #42\n str r4, [r3]\n b .", WIN)
    buf = bytearray(0x1000)
    buf[:len(code)] = code
    src = f"""
        ldr r1, ={BASE:#x}
        ldr r3, ={DATA:#x}
        ldr r5, ={WIN | 1:#x}
        movs r0, #1
        str r0, [r1]
        bx r5
    """
    chip, _ = make_chip({"reset": src})
    def hook(*a):
        chip.bus.map_window("W", WIN, 0x1000, buffer=buf)
    blk = MMIOBlock("CTL", BASE, 0x400, [Reg("C", 0, on_write=hook)])
    chip.bus.map_mmio(blk)
    blk.attach(chip)
    chip.run_cycles(100)
    assert chip.last_fault is None and chip.bus.read(DATA, 4) == 42


def test_window_closed_then_reopened_in_one_callback_keeps_bus_view():
    """Bus bookkeeping (lookup, debugger reads) follows the change at once; the core mapping follows after the
    instruction."""
    buf = bytearray(0x1000)
    buf[0] = 0x77
    src = f"""
        ldr r1, ={BASE:#x}
        movs r0, #1
        str r0, [r1]
        b .
    """
    chip, _ = make_chip({"reset": src})

    def hook(*a):
        chip.bus.unmap_window("W")
        chip.bus.map_window("W", WIN, 0x1000, buffer=buf)
    blk = MMIOBlock("CTL", BASE, 0x400, [Reg("C", 0, on_write=hook)])
    chip.bus.map_mmio(blk)
    blk.attach(chip)
    chip.bus.map_window("W", WIN, 0x1000, read=lambda o, n: 0x11, write=lambda o, n, v: None)
    chip.run_cycles(50)
    assert chip.last_fault is None
    assert chip.bus.read(WIN, 1) == 0x77
