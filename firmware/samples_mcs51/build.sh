#!/bin/sh
# Build every <name>.asm in this directory for the P89CV51RD2 into build/<name>.hex (Intel HEX), build/<name>.map
# (symbols, picked up by chipem next to the .hex) and build/<name>.lst, with tools/mcs51_asm.py.
# C samples would need SDCC (sdcc -mmcs51); it is not used here (not installed on the build host).
set -eu
cd "$(dirname "$0")"
PY=${PY:-"$(cd ../.. && pwd)/.venv/bin/python"}
mkdir -p build
for src in *.asm; do
    name=${src%.asm}
    "$PY" ../../tools/mcs51_asm.py "$src" -o "build/$name.hex" --list "build/$name.lst" >/dev/null
    echo "AS  $name"
done
echo "ok: $(ls build/*.hex | wc -l) samples built"
