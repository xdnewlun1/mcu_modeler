"""Side-effect-free register peeks (MMIOBlock.peek_reg; the debugger's Registers tab and GDB memory reads).

For every register of representative chips (each with a sample running): peeking it twice changes nothing (block
storage and attributes, scheduler events, the event log, IRQ lines, pin levels), and an exact peek returns what a
CPU read would return at that moment. Registers that still cannot be peeked (exact=False) must be in
NON_PEEKABLE (documented in docs/LIMITATIONS.md); `python tests/test_peek.py` prints the current list.
"""
from __future__ import annotations

import collections
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# (chip, firmware, clock_hz, run ns)
CASES = [
    ("mk22fn1m0vll12", "firmware/samples/build/uart_echo.elf", 120e6, 3e6),
    ("stm32f405zg", "firmware/samples_stm32/build/uart_echo.elf", None, 3e6),
    ("stm32f407zg", "firmware/samples_stm32/build/uart_echo.elf", None, 2e6),
    ("stm32f437ai", "firmware/samples_stm32/build/uart_echo.elf", None, 2e6),
    ("stm32f446ze", "firmware/samples_stm32/build/uart_echo.elf", None, 2e6),
    ("stm32f401re", "firmware/samples_stm32/build/uart_echo.elf", None, 2e6),
    ("stm32f217zg", "firmware/samples_stm32/build/uart_echo.elf", None, 2e6),
    ("stm32f103t6", "firmware/samples_stm32f1/build/uart_echo.elf", None, 3e6),
    ("stm32f107vc", "firmware/samples_stm32f1/build/uart_echo.elf", None, 3e6),
    ("stm32f103ze", "firmware/samples_stm32f1/build/uart_echo.elf", None, 3e6),
    ("tms320f28232", "firmware/samples_c2000/build/sci_echo.c28img", None, 8e6),
    ("tms320f28335", "firmware/samples_c2000/build/sci_echo.c28img", None, 8e6),
    ("tms320f2808", "firmware/samples_c2000/build/f2808_sci_echo.c28img", None, 8e6),
    ("p89cv51rd2", "firmware/samples_mcs51/build/uart_hello.hex", None, 20e6),
]

# model -> registers that are genuinely not peekable (exact=False is expected). Keep in sync with
# docs/LIMITATIONS.md ("Debugger register peeks").
NON_PEEKABLE: dict[str, set[str]] = {
    "stm32.quadspi": {"SR"},        # a read first advances the transfer to now (pulls flash bytes, may raise the IRQ)
    "stm32f1.flash": {"SR"},        # while CR.PG = 1 and not busy: the read checks the written half-words, may program
}

_SIMPLE = (int, float, str, bool, bytes, type(None))


def _val(v, depth=0):
    if isinstance(v, _SIMPLE):
        return v
    if isinstance(v, bytearray):
        return bytes(v)
    if depth > 2:
        return None
    if isinstance(v, (list, tuple, collections.deque)):
        return tuple(_val(x, depth + 1) for x in v)
    if isinstance(v, (set, frozenset)):
        try:
            return tuple(sorted(_val(x, depth + 1) for x in v))
        except TypeError:
            return len(v)
    if isinstance(v, dict):
        try:
            return tuple(sorted((repr(k), _val(x, depth + 1)) for k, x in v.items()))
        except TypeError:
            return len(v)
    if hasattr(v, "cycles") and hasattr(v, "cancelled"):     # scheduler Event
        return (v.cycles, v.cancelled)
    return None


def block_state(b):
    st = {"mem": bytes(b.mem)}
    for k, v in vars(b).items():
        if k in ("mem", "chip", "log", "regs", "_by_off", "_seen", "params"):
            continue
        x = _val(v)
        if x is not None:
            st[k] = x
    return st


def chip_state(chip):
    arch = chip.arch
    return (tuple((e.cycles, e.seq) for e in sorted(chip.sched._heap) if not e.cancelled), len(chip.log),
            tuple(sorted(getattr(arch, "pending", ()) or ())), tuple(sorted(getattr(arch, "_lines", {}).items()))
            if isinstance(getattr(arch, "_lines", None), dict) else None,
            tuple(p.level() for p in chip.pins.pins) if chip.pins is not None else None,
            chip.clock.cycles)


def _session(chip, fw, clk):
    from chipem.session import Session
    return Session(chip, ROOT / fw, clock_hz=clk, quiet=True)


def walk(chip):
    """Peek every register twice; returns (changes, inexact, mismatches)."""
    changes, inexact, mism = [], [], []
    blocks = [b for b in chip.peripherals.values() if hasattr(b, "peek_reg")]
    for b in blocks:
        for r in list(b.regs.values()):
            s0, c0 = block_state(b), chip_state(chip)
            v1, ex1 = b.peek_reg(r)
            v2, ex2 = b.peek_reg(r)
            s1, c1 = block_state(b), chip_state(chip)
            if s0 != s1 or c0 != c1:
                diff = sorted(k for k in set(s0) | set(s1) if s0.get(k) != s1.get(k))
                changes.append((type(b).model, b.name, r.name, diff or "chip"))
            if v1 != v2:
                changes.append((type(b).model, b.name, r.name, "peek value unstable"))
            if not ex1:
                inexact.append((type(b).model, b.name, r.name))
    # exact peeks equal a CPU read at the same instant (reads may have side effects: done last, on this throwaway
    # chip; each register is peeked right before it is read)
    for b in blocks:
        for r in list(b.regs.values()):
            v, ex = b.peek_reg(r)
            if not ex:
                continue
            try:
                rd = b.read(r.offset, r.width)
            except Exception as e:                       # pragma: no cover - reported, not fatal
                mism.append((type(b).model, b.name, r.name, f"read raised {e!r}"))
                continue
            if rd != v:
                mism.append((type(b).model, b.name, r.name, f"peek 0x{v:x} read 0x{rd:x}"))
    return changes, inexact, mism


def _run_case(case):
    chip, fw, clk, ns = case
    if not (ROOT / fw).exists():
        pytest.skip(f"{fw} not built")
    if not (ROOT / "chips" / f"{chip}.yaml").exists():
        pytest.skip(f"no chip {chip}")
    s = _session(chip, fw, clk)
    try:
        s.run_for(ns)
        return walk(s.chip)
    finally:
        s.close()


def _unexpected(inexact):
    return [x for x in inexact if x[2] not in NON_PEEKABLE.get(x[0], set())]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_peek_has_no_side_effects_and_matches_reads(case):
    changes, inexact, mism = _run_case(case)
    assert not changes, changes[:20]
    assert not mism, mism[:20]
    assert not _unexpected(inexact), f"{len(_unexpected(inexact))} non-peekable: {_unexpected(inexact)[:40]}"


if __name__ == "__main__":          # report: python tests/test_peek.py [chip-filter]
    sys.path.insert(0, str(ROOT))
    flt = sys.argv[1:] or [""]
    tot = collections.Counter()
    for case in CASES:
        if not any(f in case[0] for f in flt):
            continue
        try:
            ch, ix, mm = _run_case(case)
        except BaseException as e:      # skip
            print(case[0], "skipped", e)
            continue
        print(f"== {case[0]}: {len(ch)} side effects, {len(ix)} non-peekable, {len(mm)} peek != read")
        for x in ch:
            print("  CHANGE", *x)
        for x in mm:
            print("  MISMATCH", *x)
        for x in ix:
            tot[(x[0], x[2])] += 1
    print("non-peekable (model, register):")
    for (m, r), n in sorted(tot.items()):
        print(f"  {m:28s} {r}")
