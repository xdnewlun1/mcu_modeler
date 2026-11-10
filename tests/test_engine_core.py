import pytest

from chipem.core import StopReason
from chipem.core.unicorn_cm import UnicornCortexM

from engine_util import asm

CODE = 0x1000


def _core(src: str, precise=False, cpu="cortex-m4"):
    c = UnicornCortexM(cpu, precise=precise)
    c.map_memory(0, 0x10000)
    c.map_memory(0x20000000, 0x10000)
    c.write_mem(CODE, asm(src, CODE))
    c.reset(CODE, 0x20008000)
    return c


LOOP = """
  movs r0, #0
loop:
  adds r0, #1
  b loop
"""

# 3 + 50 * 4 + 1 (wfi) instructions
COUNTED = """
  movs r0, #50
  movs r1, #0
  nop
again:
  adds r1, #1
  subs r0, #1
  cmp r0, #0
  bne again
  wfi
  b again
"""


@pytest.mark.parametrize("cpu", ["cortex-m0", "cortex-m3", "cortex-m4", "cortex-m7"])
def test_run_count(cpu):
    c = _core(LOOP, cpu=cpu)
    r, n = c.run(101)
    assert (r, n) == (StopReason.COUNT, 101)
    assert c.read_reg("r0") == 50          # movs + 50 x (adds, b)


@pytest.mark.parametrize("precise", [False, True])
def test_wfi_and_exact_counts(precise):
    c = _core(COUNTED, precise=precise)
    r, n = c.run(10_000)
    assert r is StopReason.WFI
    assert n == 3 + 50 * 4 + 1
    assert c.read_reg("r1") == 50
    assert c.pc == CODE + len(asm(COUNTED.split("wfi")[0], CODE)) + 2


def test_precise_and_fast_agree_on_slices():
    fast, prec = _core(LOOP), _core(LOOP, precise=True)
    for n in (1, 7, 33, 500):
        assert fast.run(n) == prec.run(n) == (StopReason.COUNT, n)
        assert fast.read_reg("r0") == prec.read_reg("r0")
        assert fast.pc == prec.pc


def test_svc_bkpt_exc_return_and_faults():
    c = _core("movs r0, #1\nsvc #7\nmovs r0, #2\nbkpt #1")
    r, n = c.run(100)
    assert r is StopReason.SVC and n == 2
    assert c.pc == CODE + 4
    r, n = c.run(100)
    assert r is StopReason.BREAKPOINT and c.read_reg("r0") == 2

    c = _core("ldr r0, =0xFFFFFFF9\nbx r0")
    r, _ = c.run(100)
    assert r is StopReason.EXC_RETURN and c.stop_info == 0xFFFFFFF9

    c = _core("ldr r0, =0x60000000\nnop\nldr r1, [r0]")
    r, n = c.run(100)
    assert r is StopReason.FAULT and n == 2
    assert c.last_fault["type"] == "read_unmapped" and c.last_fault["addr"] == 0x60000000
    assert c.last_fault["pc"] == CODE + 4

    c = _core(".short 0xDE00")     # udf
    r, _ = c.run(100)
    assert r is StopReason.FAULT and c.last_fault["type"] == "undefined_instruction"


def test_registers_by_name_fp_and_batch():
    c = _core(LOOP)
    c.write_reg("s5", 0x40490FDB)
    c.write_reg("fpscr", 0x03C00000)
    c.write_reg("basepri", 0x40)
    assert c.read_regs(["s5", "fpscr", "basepri", "sp"]) == [0x40490FDB, 0x03C00000, 0x40, 0x20008000]
    c.write_regs(["r0", "r1"], [5, 6])
    assert c.read_reg("r0") == 5 and c.read_reg("r1") == 6


@pytest.mark.parametrize("precise", [False, True])
def test_slice_ending_after_bw_with_wfi_like_halfword_is_not_wfi(precise):
    """Regression: a `b.w` whose second halfword is 0xBF30 (the WFI encoding) must not be mistaken for a WFI
    when a slice ends exactly at its target.  Seen in real firmware (the Withings image at 0x55244)."""
    # .short 0xF000, 0xBF30 == b.w +0xE64 (imm11 = 0x730, I1 = I2 = 0)
    c = _core("movs r0, #7\n.short 0xF000\n.short 0xBF30", precise=precise)
    target = CODE + 2 + 4 + 0xE60
    c.write_mem(target, asm("movs r1, #9\nnop\nb .", target))
    r, n = c.run(2)                      # movs + b.w: the slice ends right after the branch
    assert (r, n) == (StopReason.COUNT, 2)
    assert c.pc == target
    assert c.read_reg("r0") == 7 and c.read_reg("r1") == 0
    r, n = c.run(1)                      # continues normally at the target
    assert (r, n) == (StopReason.COUNT, 1) and c.read_reg("r1") == 9
    # and a real wfi as the last instruction of a slice is still reported as WFI
    c2 = _core("movs r0, #1\nwfi\nmovs r0, #2\nb .", precise=precise)
    r, n = c2.run(2)
    assert r is StopReason.WFI and n == 2 and c2.read_reg("r0") == 1


@pytest.mark.parametrize("precise", [False, True])
def test_wfe_halts_like_wfi(precise):
    # Unicorn 2.1 reports WFE as an invalid instruction; the core turns it into a WFI-type stop (stop_info "wfe")
    c = _core("movs r0, #1\nmovs r1, #2\nwfe\nmovs r0, #3", precise=precise)
    r, n = c.run(100)
    assert r is StopReason.WFI and c.stop_info == "wfe" and c.last_fault is None
    assert n == 3
    assert c.pc == CODE + 6
    r, n = c.run(1)
    assert c.read_reg("r0") == 3
