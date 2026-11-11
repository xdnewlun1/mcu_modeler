#!/bin/sh
# Build the STM32F103 package-variant samples (clang + ld.lld; no GNU arm toolchain needed) into build/<name>.elf/.bin.
# Shares stm32f103.h / console.h with firmware/samples_stm32f1 (include path), its own 60-vector startup_f1x.c.
#   bluepill_*  : medium density (STM32F103C8 "Blue Pill" and up): link_64k_20k.ld
#   hd_*        : high density (STM32F103RC/RE/VC/VE/ZC/ZE): link_256k_48k.ld
set -eu
cd "$(dirname "$0")"
CC=${CC:-clang}
PY=${PY:-"$(cd ../.. && pwd)/.venv/bin/python"}
CFLAGS="--target=thumbv7m-none-eabi -mcpu=cortex-m3 -mfloat-abi=soft -mthumb -O1 -g \
 -ffreestanding -nostdlib -fno-builtin -Wall -Wextra -I../samples_stm32f1"
mkdir -p build
for src in *.c; do
    name=${src%.c}
    case "$name" in
        startup_f1x) continue ;;
        hd_*) ld=link_256k_48k.ld ;;
        *) ld=link_64k_20k.ld ;;
    esac
    echo "CC  $name ($ld)"
    $CC $CFLAGS -ffunction-sections -fdata-sections -T "$ld" -fuse-ld=lld -Wl,--image-base=0x08000000 \
        -Wl,-e,Reset_Handler -Wl,--gc-sections -o "build/$name.elf" startup_f1x.c "$src" -Wl,-Map,"build/$name.map"
    if command -v llvm-objcopy >/dev/null 2>&1; then
        llvm-objcopy -O binary "build/$name.elf" "build/$name.bin"
    else
        "$PY" ../samples/elf2bin.py "build/$name.elf" "build/$name.bin"
    fi
done
echo "ok: $(ls build/*.elf | wc -l) samples built"
