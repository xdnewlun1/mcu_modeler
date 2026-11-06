#!/bin/sh
# Assemble the C2000 samples into build/*.c28img (JSON word images, see chipem/firmware.py) with the C28x assembler
# (chipem/core/c28x/asm.py).  Uses tools/c28x_asm.py when present, else the assembler module directly.
set -e
cd "$(dirname "$0")"
ROOT="$(cd ../.. && pwd)"
PY="$ROOT/.venv/bin/python"
mkdir -p build
for src in *.asm; do
    out="build/${src%.asm}.c28img"
    if [ -f "$ROOT/tools/c28x_asm.py" ]; then
        "$PY" "$ROOT/tools/c28x_asm.py" "$src" -o "$out"
    else
        "$PY" -c "import sys; sys.path.insert(0, '$ROOT')
from chipem.core.c28x.asm import assemble
r = assemble(open('$src').read(), filename='$src')
r.write_c28img('$out')"
    fi
    echo "$out"
done
