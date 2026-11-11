#!/usr/bin/env python3
"""MCS-51 (8051) assembler / disassembler CLI (implementation: chipem/core/mcs51/asm.py, Intel ASM51 syntax).

    python tools/mcs51_asm.py in.asm -o out.hex [--map out.map] [--bin out.bin] [--list out.lst]
                              [--regs chips/p89cv51_regs.yaml] [-I dir]
        writes Intel HEX (the usual 8051 firmware format); `--map` defaults to <out>.map next to a .hex output,
        so chipem picks the symbols up when it loads the .hex (sidecar symbols, `C:0x0030 main` lines)
    python tools/mcs51_asm.py --disasm image.hex|image.bin [--addr 0] [--count N] [--regs ...]

The register table (default chips/p89cv51_regs.yaml) supplies the chip's SFR and bit names (T2CON, CCAPM0, TF2,
CR, ...) on top of the standard 8051 set.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from chipem.core.mcs51.asm import AsmError, Assembler, Names, disasm, symbols_from_regs  # noqa: E402

DEFAULT_REGS = ROOT / "chips" / "p89cv51_regs.yaml"


def _int(s: str) -> int:
    return int(s, 0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("-o", "--output", help="output .hex (Intel HEX) or .bin")
    ap.add_argument("--map", help="symbol map (default: <output>.map for .hex output)")
    ap.add_argument("--bin", help="also write a plain binary from address 0")
    ap.add_argument("--list", help="write a listing (address, bytes, source)")
    ap.add_argument("--regs", default=str(DEFAULT_REGS), help="register table with the chip's SFR / bit names")
    ap.add_argument("-I", dest="incdirs", action="append", default=[], help="include directory")
    ap.add_argument("--disasm", action="store_true", help="disassemble a .hex / .bin instead")
    ap.add_argument("--addr", type=_int, default=0, help="start address for --disasm")
    ap.add_argument("--count", type=_int, default=0, help="instructions to disassemble (default: whole image)")
    a = ap.parse_args(argv)
    sfrs, bits = symbols_from_regs(a.regs) if a.regs and Path(a.regs).exists() else ({}, {})

    if a.disasm:
        from chipem.firmware import load_firmware
        img = load_firmware(a.input)
        mem = {}
        for base, data in img.segments:
            for i, b in enumerate(data):
                mem[base + i] = b
        labels = {int(v): k for k, v in (img.symbols or {}).items() if int(v) < 0x10000}
        names = Names.standard(sfrs, bits, labels)
        n = 0
        for base, data in img.segments:                  # each segment from its start (gaps are not code)
            end = base + len(data)
            pc = max(base, a.addr)
            while pc < end and (not a.count or n < a.count):
                for addr, raw, text in disasm(lambda x: mem.get(x, 0xFF), pc, 1, names):
                    if addr in labels:
                        print(f"{labels[addr]}:")
                    print(f"  {addr:04X}: {raw.hex(' ').upper():<9} {text}")
                    pc = addr + len(raw)
                n += 1
        return 0

    try:
        res = Assembler(sfrs=sfrs, bits=bits, include_dirs=a.incdirs).assemble(Path(a.input).read_text(), a.input)
    except AsmError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    out = Path(a.output) if a.output else Path(a.input).with_suffix(".hex")
    if out.suffix.lower() == ".bin":
        out.write_bytes(res.to_bin())
    else:
        out.write_text(res.to_ihex())
        mp = Path(a.map) if a.map else out.with_suffix(".map")
        mp.write_text(res.map_text())
    if a.map and out.suffix.lower() == ".bin":
        Path(a.map).write_text(res.map_text())
    if a.bin:
        Path(a.bin).write_bytes(res.to_bin())
    if a.list:
        Path(a.list).write_text("\n".join(res.listing) + "\n")
    size = sum(len(d) for _, d in res.segments)
    print(f"{out}: {size} bytes in {len(res.segments)} segment(s), {len(res.symbols)} symbols")
    return 0


if __name__ == "__main__":
    sys.exit(main())
