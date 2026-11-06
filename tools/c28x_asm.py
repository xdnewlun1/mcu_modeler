#!/usr/bin/env python3
"""C28x assembler / disassembler CLI (implementation: chipem/core/c28x/asm.py).

    python tools/c28x_asm.py in.asm -o out.c28img [--map out.map] [--bin out.bin] [--list out.lst]
                             [--sect name=addr ...]
    python tools/c28x_asm.py --disasm image.c28img|image.out|image.bin [--addr 0x8000]
        (.out: TI cl2000 output, ELF/EABI or COFF; symbols are printed as labels like dis2000 does)
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chipem.core.c28x.asm import Assembler, disasm  # noqa: E402
from chipem.core.c28x.isa import AsmError  # noqa: E402


def _int(s: str) -> int:
    return int(s, 0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("-o", "--output", help="output .c28img (JSON segments) or .bin (single segment)")
    ap.add_argument("--map", help="write a symbol map")
    ap.add_argument("--bin", help="also write a plain binary (exactly one segment)")
    ap.add_argument("--list", help="write a listing (address, words, source)")
    ap.add_argument("--sect", action="append", default=[], metavar="NAME=ADDR", help="section start address")
    ap.add_argument("--disasm", action="store_true", help="disassemble a .c28img / TI .out / .bin instead")
    ap.add_argument("--addr", type=_int, default=0, help="load address of a .bin for --disasm")
    a = ap.parse_args(argv)

    if a.disasm:
        p = Path(a.input)
        labels: dict[int, list[str]] = {}
        if p.suffix == ".c28img":
            d = json.loads(p.read_text())
            segs = [(int(s["addr"]), bytes.fromhex(s["data_hex"])) for s in d["segments"]]
            for k, v in (d.get("symbols") or {}).items():
                labels.setdefault(int(v), []).append(k)
        elif p.suffix == ".bin":
            segs = [(a.addr, p.read_bytes())]
        else:                                           # TI .out (ELF or COFF) or anything load_firmware sniffs
            from chipem.firmware import load_firmware
            im = load_firmware(p)
            segs = list(im.segments)
            for k, v in im.symbols.items():
                labels.setdefault(v, []).append(k)
        for base, data in segs:
            words = struct.unpack(f"<{len(data) // 2}H", data[: len(data) // 2 * 2])
            for addr, n, text in disasm(words, base):
                for lb in sorted(labels.get(addr, [])):
                    print(f"{addr:06X}  {lb}:")
                hexw = " ".join(f"{w:04X}" for w in words[addr - base: addr - base + n])
                print(f"{addr:06X}  {hexw:<10} {text}")
        return 0

    sections = {}
    for s in a.sect:
        k, v = s.split("=", 1)
        sections[k] = int(v, 0)
    try:
        res = Assembler().assemble(Path(a.input).read_text(), filename=a.input, sections=sections)
    except AsmError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    out = a.output or str(Path(a.input).with_suffix(".c28img"))
    if out.endswith(".bin"):
        res.write_bin(out)
    else:
        res.write_c28img(out)
    if a.bin:
        res.write_bin(a.bin)
    if a.map:
        Path(a.map).write_text(res.map_text())
    if a.list:
        lines = []
        for addr, words, src in res.listing:
            lines.append(f"{addr:06X}  {' '.join(f'{w:04X}' for w in words[:4]):<20} {src}")
        Path(a.list).write_text("\n".join(lines) + "\n")
    nwords = sum(len(d) // 2 for _, d in res.segments)
    print(f"{out}: {len(res.segments)} segment(s), {nwords} words, {len(res.symbols)} symbols, "
          f"entry {('0x%06X' % res.entry) if res.entry is not None else '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
