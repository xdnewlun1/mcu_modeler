#!/usr/bin/env python3
"""elf2bin.py in.elf out.bin: flat binary from the PT_LOAD segments' load addresses (LMA), starting at the lowest
one, gaps filled with 0xFF (erased flash). Used because llvm-objcopy is not installed."""
import sys

from elftools.elf.elffile import ELFFile


def main(src: str, dst: str) -> None:
    with open(src, "rb") as f:
        elf = ELFFile(f)
        segs = [(s["p_paddr"], s.data()[:s["p_filesz"]]) for s in elf.iter_segments()
                if s["p_type"] == "PT_LOAD" and s["p_filesz"] > 0]
    base = min(a for a, _ in segs)
    end = max(a + len(d) for a, d in segs)
    out = bytearray(b"\xff" * (end - base))
    for a, d in segs:
        out[a - base:a - base + len(d)] = d
    with open(dst, "wb") as f:
        f.write(out)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
