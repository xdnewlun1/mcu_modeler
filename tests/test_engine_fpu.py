"""FP availability on ARMv7-M: a Cortex-M3 (fpu: false) has no FP unit (CPACR and the FP extension registers RAZ/WI;
every VFP instruction raises UsageFault NOCP, escalating to HardFault when UsageFault is disabled); a Cortex-M4F
raises the same NOCP fault until CPACR grants CP10/CP11. Implemented without hooks (core/unicorn_cm.py
`set_fp_enabled`: the CPU's MVFR fields are cleared while FP is unavailable)."""
from __future__ import annotations

from pathlib import Path

import pytest

from engine_util import DATA, make_chip

CPACR, SHCSR, CFSR, HFSR = 0xE000ED88, 0xE000ED24, 0xE000ED28, 0xE000ED2C
NOCP = 1 << 19
FORCED = 1 << 30
ROOT = Path(__file__).resolve().parents[1]

HANDLERS = {
    "hard": f"ldr r3, ={DATA:#x}\n movs r2, #3\n str r2, [r3]\n mov r2, sp\n ldr r2, [r2, #24]\n str r2, [r3, #4]\n b .",
    "usage": f"ldr r3, ={DATA:#x}\n movs r2, #6\n str r2, [r3]\n mov r2, sp\n ldr r2, [r2, #24]\n str r2, [r3, #4]\n b .",
}


def _chip(body: str, *, cpu: str, fpu: bool):
    src = f"""
        ldr r3, ={DATA:#x}
        movs r2, #0
        str r2, [r3]
        {body}
    fp_insn:
        vadd.f32 s0, s0, s1
        ldr r3, ={DATA:#x}
        movs r2, #9
        str r2, [r3]
    spin:
        b spin
    """
    chip, addrs = make_chip({"reset": src, **HANDLERS}, vectors={3: "hard", 6: "usage"}, cpu=cpu, fpu=fpu)
    return chip, addrs


def _word(chip, a):
    return chip.bus.read(a, 4)


ENABLE_USAGE = f"ldr r0, ={SHCSR:#x}\n ldr r1, [r0]\n orr r1, r1, #0x40000\n str r1, [r0]"
ENABLE_CP = f"ldr r0, ={CPACR:#x}\n ldr r1, [r0]\n orr r1, r1, #0xF00000\n str r1, [r0]\n dsb\n isb"


def test_m3_cpacr_reads_zero_and_fp_regs_absent():
    chip, _ = _chip(ENABLE_CP, cpu="cortex-m3", fpu=False)
    chip.run_cycles(50)
    assert _word(chip, CPACR) == 0
    chip.bus.write(CPACR, 4, 0x00F00000)
    assert _word(chip, CPACR) == 0
    for a in (0xE000EF34, 0xE000EF38, 0xE000EF3C, 0xE000EF40, 0xE000EF44):     # FPCCR FPCAR FPDSCR MVFR0 MVFR1
        chip.bus.write(a, 4, 0xFFFFFFFF)
        assert _word(chip, a) == 0


def test_m3_vfp_escalates_to_hardfault_nocp():
    chip, _ = _chip(ENABLE_CP, cpu="cortex-m3", fpu=False)            # enabling CP10/11 does nothing on an M3
    chip.run_cycles(200)
    assert _word(chip, DATA) == 3
    assert _word(chip, CFSR) & NOCP and _word(chip, HFSR) & FORCED


def test_m3_vfp_takes_usagefault_when_enabled():
    chip, addrs = _chip(ENABLE_USAGE + "\n" + ENABLE_CP, cpu="cortex-m3", fpu=False)
    chip.run_cycles(200)
    assert _word(chip, DATA) == 6
    assert _word(chip, CFSR) & NOCP and not _word(chip, HFSR) & FORCED
    fp_at = _word(chip, DATA + 4)                                     # stacked PC = the VFP instruction
    assert chip.bus.read(fp_at & ~1, 4) == int.from_bytes(bytes.fromhex("30ee200a"), "little")


def test_m4f_vfp_faults_until_cpacr_enables_it():
    chip, _ = _chip(ENABLE_USAGE, cpu="cortex-m4", fpu=True)          # CPACR left at its reset value 0
    chip.run_cycles(200)
    assert _word(chip, DATA) == 6 and _word(chip, CFSR) & NOCP
    chip, _ = _chip(ENABLE_USAGE + "\n" + ENABLE_CP, cpu="cortex-m4", fpu=True)
    chip.run_cycles(200)
    assert _word(chip, DATA) == 9 and not _word(chip, CFSR) & NOCP
    assert _word(chip, CPACR) == 0x00F00000


def test_fp_gate_is_per_chip():
    """An M3 and an M4F in one process: each core's gate only affects itself."""
    m3, _ = _chip("", cpu="cortex-m3", fpu=False)
    m4, _ = _chip(ENABLE_CP, cpu="cortex-m4", fpu=True)
    m4.run_cycles(200)
    m3.run_cycles(200)
    assert _word(m4, DATA) == 9 and _word(m3, DATA) == 3


@pytest.mark.parametrize("chip_name,fw,clk", [
    ("mk22fn1m0vll12", "firmware/samples/build/fpu_test.elf", 120e6),
])
def test_fpu_samples_still_run(chip_name, fw, clk):
    from chipem.session import Session
    p = ROOT / fw
    if not p.exists():
        pytest.skip("sample not built")
    s = Session(chip_name, p, clock_hz=clk, quiet=True)
    try:
        s.run_for(5e6)
        assert s.chip.last_fault is None
        assert not [e for e in s.chip.log if e.kind == "fault"]
        assert s.chip.core.fp_enabled
    finally:
        s.close()
