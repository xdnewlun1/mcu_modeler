#!/bin/sh
# Build every <name>.c in this directory (except startup.c) for the STM32F405ZG into build/<name>.elf and .bin (clang + lld).
set -eu
cd "$(dirname "$0")"
CC=${CC:-clang}
PY=${PY:-"$(cd ../.. && pwd)/.venv/bin/python"}
CFLAGS="--target=thumbv7em-none-eabi -mcpu=cortex-m4 -mfpu=fpv4-sp-d16 -mfloat-abi=hard -mthumb -O1 -g \
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
