#!/usr/bin/env python3
"""Build the C2000 boot-loader data stream (TI SPRU722C 2.10, the format of `hex2000 -boot`) from a C28x image.

The stream is: key (0x08AA for an 8-bit stream, 0x10AA for a 16-bit stream), 8 reserved words, the 32-bit entry point
(MSW first), then one block per contiguous image segment {size in words, 32-bit destination (MSW first), data}, and a
block size of 0.  In the 8-bit stream every 16-bit word is sent LSB first.  Loaders (chips/tms320f2808_bootrom.asm):
SCI-A / SPI-A EEPROM / I2C-A EEPROM / eCAN-A take the 8-bit stream (CAN: one word = one 2-byte data frame), the
parallel GPIO loader both.  The SPI and I2C loaders read register values from the first reserved words
(`--spi-lospcp/--spi-brr`, `--i2c-psc/--i2c-clkh/--i2c-clkl`).

    .venv/bin/python tools/c28x_bootstream.py prog.c28img -o prog.boot           # 8-bit stream, raw bytes
    .venv/bin/python tools/c28x_bootstream.py prog.out --format hex16 -o p.txt   # 16-bit words, one per line
    .venv/bin/python tools/c28x_bootstream.py prog.c28img --format at            # a ready `--at` value for SCI-A:
    .venv/bin/chipem run prog_any --chip tms320f2808 --set GPIO34=0 --at "1ms:SCIA=$(...)" ...

The image may be a .c28img, a TI .out (COFF or EABI ELF) or anything chipem.firmware.load_firmware reads.  Only the
loaded segments go into the stream (the boot loaders write RAM; flash targets need a flash API, as on silicon).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEY8, KEY16 = 0x08AA, 0x10AA


def stream_words(segments, entry: int, *, key: int = KEY8, reserved: list[int] | None = None,
                 max_block: int = 0xFFFF) -> list[int]:
    """The boot stream as 16-bit words.  `segments`: [(word address, little-endian bytes)]."""
    rsv = list(reserved or [])[:8]
    rsv += [0] * (8 - len(rsv))
    out = [key, *rsv, (entry >> 16) & 0xFFFF, entry & 0xFFFF]
    for addr, data in segments:
        words = [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data) - 1, 2)]
        for i in range(0, len(words), max_block):
            chunk = words[i:i + max_block]
            a = addr + i
            out += [len(chunk), (a >> 16) & 0xFFFF, a & 0xFFFF, *chunk]
    out.append(0)
    return out


def stream_bytes(words: list[int]) -> bytes:
    """8-bit stream: LSB first."""
    return b"".join(w.to_bytes(2, "little") for w in words)


def parse_stream(words: list[int]) -> tuple[int, list[tuple[int, list[int]]]]:
    """(entry, [(address, words)]) of a 16-bit-word boot stream (key already checked)."""
    entry = (words[9] << 16) | words[10]
    i, blocks = 11, []
    while words[i]:
        n = words[i]
        a = (words[i + 1] << 16) | words[i + 2]
        blocks.append((a, words[i + 3:i + 3 + n]))
        i += 3 + n
    return entry, blocks


def from_image(path: str | Path, **kw) -> list[int]:
    sys.path.insert(0, str(ROOT))
    from chipem.firmware import load_firmware
    img = load_firmware(path)
    if img.unit_bytes != 2:
        raise SystemExit(f"{path}: not a C28x (16-bit word) image")
    entry = kw.pop("entry", None)
    if entry is None:
        entry = img.entry if img.entry is not None else img.start
    return stream_words(img.segments, entry, **kw)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("image")
    ap.add_argument("-o", "--output", help="output file (default stdout)")
    ap.add_argument("--format", choices=("bin8", "bin16", "hex8", "hex16", "at"), default="bin8",
                    help="bin8: 8-bit stream bytes (SCI/SPI/I2C/CAN, EEPROM image); bin16: 16-bit stream, little-endian "
                         "words (parallel); hex8/hex16: one byte/word per line; at: the 8-bit stream as escaped text "
                         "for `chipem run --at TIME:SCIA=...`, preceded by the autobaud character 'A'")
    ap.add_argument("--entry", type=lambda v: int(v, 0), help="entry point (default: the image's)")
    ap.add_argument("--spi-lospcp", type=lambda v: int(v, 0), help="SPI loader: LOSPCP value (reserved word 1 LSB)")
    ap.add_argument("--spi-brr", type=lambda v: int(v, 0), help="SPI loader: SPIBRR value (reserved word 1 MSB)")
    ap.add_argument("--i2c-psc", type=lambda v: int(v, 0), help="I2C loader: I2CPSC (reserved word 1)")
    ap.add_argument("--i2c-clkh", type=lambda v: int(v, 0), help="I2C loader: I2CCLKH (reserved word 2)")
    ap.add_argument("--i2c-clkl", type=lambda v: int(v, 0), help="I2C loader: I2CCLKL (reserved word 3)")
    a = ap.parse_args(argv)
    rsv = [0] * 8
    if a.spi_lospcp is not None or a.spi_brr is not None:
        rsv[0] = ((a.spi_brr if a.spi_brr is not None else 0x7F) << 8) | (a.spi_lospcp if a.spi_lospcp is not None
                                                                         else 2)
    if a.i2c_psc is not None or a.i2c_clkh is not None or a.i2c_clkl is not None:
        rsv[0:3] = [a.i2c_psc or 0, a.i2c_clkh if a.i2c_clkh is not None else 0x35,
                    a.i2c_clkl if a.i2c_clkl is not None else 0x35]
    key = KEY16 if a.format in ("bin16", "hex16") else KEY8
    words = from_image(a.image, key=key, reserved=rsv, entry=a.entry)
    if a.format == "bin8":
        data: bytes | str = stream_bytes(words)
    elif a.format == "bin16":
        data = b"".join(w.to_bytes(2, "little") for w in words)
    elif a.format == "hex8":
        data = "".join(f"{b:02X}\n" for b in stream_bytes(words))
    elif a.format == "hex16":
        data = "".join(f"{w:04X}\n" for w in words)
    else:
        data = "A" + "".join(f"\\x{b:02X}" for b in stream_bytes(words))     # 'A': the SCI autobaud character
    if a.output:
        Path(a.output).write_bytes(data if isinstance(data, bytes) else data.encode())
    elif isinstance(data, bytes):
        sys.stdout.buffer.write(data)
    else:
        sys.stdout.write(data + ("" if a.format != "at" else "\n"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
