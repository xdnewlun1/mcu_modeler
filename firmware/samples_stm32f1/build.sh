#!/bin/sh
# Build every <name>.c in this directory (except startup.c) for the STM32F103T6 (Cortex-M3, 32 KB flash, 10 KB SRAM)
# into build/<name>.elf and .bin (clang + ld.lld; no GNU arm toolchain needed).
set -eu
cd "$(dirname "$0")"
CC=${CC:-clang}
PY=${PY:-"$(cd ../.. && pwd)/.venv/bin/python"}
CFLAGS="--target=thumbv7m-none-eabi -mcpu=cortex-m3 -mfloat-abi=soft -mthumb -O1 -g \
 -ffreestanding -nostdlib -fno-builtin -Wall -Wextra"
LDFLAGS="-T link.ld -fuse-ld=lld -Wl,--image-base=0x08000000 -Wl,-e,Reset_Handler -Wl,--gc-sections"
mkdir -p build
for src in *.c; do
    name=${src%.c}
    [ "$name" = startup ] && continue
    echo "CC  $name"
    $CC $CFLAGS -ffunction-sections -fdata-sections $LDFLAGS -o "build/$name.elf" startup.c "$src" -Wl,-Map,"build/$name.map"
    if command -v llvm-objcopy >/dev/null 2>&1; then
        llvm-objcopy -O binary "build/$name.elf" "build/$name.bin"
    else
        "$PY" ../samples/elf2bin.py "build/$name.elf" "build/$name.bin"
    fi
done
echo "ok: $(ls build/*.elf | wc -l) samples built"
