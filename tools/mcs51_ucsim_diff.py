#!/usr/bin/env python3
"""Exhaustive / randomised differential run of the MCS-51 core against ucsim (s51 / sdcc-s51); the fast subset is
tests/test_mcs51_ucsim.py.  Uses tests/mcs51_ucsim.py.

    .venv/bin/python tools/mcs51_ucsim_diff.py                 # full boundary grid (~102 000 cases) + 2000 random
    .venv/bin/python tools/mcs51_ucsim_diff.py --random 20000 --seed 3 --no-grid
    .venv/bin/python tools/mcs51_ucsim_diff.py --programs        # also the SDCC C programs (firmware/samples_mcs51/c)

Prints the opcode coverage (cases per opcode, operand kinds) and every difference.  Exit status 1 if any differ.
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

import mcs51_ucsim as U  # noqa: E402
from chipem.core.mcs51.isa import OPCODES  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--no-grid", action="store_true", help="skip the full boundary grid")
    ap.add_argument("--per-op", type=int, default=0, help="random grid picks per opcode instead of the full grid")
    ap.add_argument("--random", type=int, default=2000, help="random instruction sequences")
    ap.add_argument("--length", type=int, default=60, help="instructions per random sequence")
    ap.add_argument("--programs", action="store_true", help="run the SDCC sample programs too")
    ap.add_argument("--s51", default=None, help="ucsim binary (default: s51 / sdcc-s51 / ucsim_51 on PATH)")
    a = ap.parse_args(argv)
    exe = a.s51 or U.find_s51()
    if exe is None:
        print("ucsim s51 not found", file=sys.stderr)
        return 2
    rnd = random.Random(a.seed)
    cases: list[U.Case] = []
    if not a.no_grid:
        cases += U.opcode_cases(rnd, full=not a.per_op, per_op=a.per_op or 8)
    cases += [U.random_program_case(rnd, n=a.length) for _ in range(a.random)]
    t0 = time.time()
    bad = U.compare(cases, exe=exe)
    dt = time.time() - t0
    per_op = Counter(int(c.note[:2], 16) for c in cases if not c.note.startswith("random"))
    insns = len(cases) - sum(1 for c in cases if c.note.startswith("random")) + \
        sum(c.steps for c in cases if c.note.startswith("random"))
    print(f"{len(cases)} cases ({insns} instructions) in {dt:.1f} s against {exe}")
    if per_op:
        covered = sum(1 for op in range(256) if OPCODES[op] is not None and per_op[op])
        print(f"opcode coverage: {covered}/255 opcodes, {min(per_op.values())}-{max(per_op.values())} cases each")
    status = 0
    if bad:
        status = 1
        print(f"{len(bad)} differences:")
        for c, d in bad[:200]:
            print(f"  {c.note}: {'; '.join(d[:8])}")
    if a.programs:
        from chipem.firmware import load_firmware
        for p in sorted((ROOT / "firmware" / "samples_mcs51" / "c" / "build").glob("*.ihx")):
            img = load_firmware(p)
            if "finish" not in img.symbols:
                continue
            m = U.run_program_model(img, img.symbols["finish"])
            r = U.run_program_s51(p, img.symbols["finish"], exe=exe)
            same = (m.iram, m.xdata, m.ticks, m.pc) == (r.iram, r.xdata, r.ticks, r.pc)
            print(f"  {p.name}: {'same' if same else 'DIFFERENT'} ({m.ticks} clocks; ucsim {r.ticks})")
            status |= 0 if same else 1
    print("ok" if status == 0 else "FAILED")
    return status


if __name__ == "__main__":
    sys.exit(main())
