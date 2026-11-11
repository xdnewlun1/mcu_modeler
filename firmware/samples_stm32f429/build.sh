#!/bin/sh
# Build every <name>.c in this directory into build/<name>.elf and .bin (clang + ld.lld; no GNU arm toolchain needed).
# STM32F429xx/F439xx samples (chips/stm32f429zi.yaml and its package variants): the F427/F437 die with the LCD-TFT
# controller, so the 91-entry vector table (RM0090 Table 63, LTDC_IRQHandler = 88), the linker script (192 KB SRAM) and
# the register header are shared with ../samples_stm32_variants/f42x/.
set -eu
cd "$(dirname "$0")"
CC=${CC:-clang}
PY=${PY:-"$(cd ../.. && pwd)/.venv/bin/python"}
F42X=../samples_stm32_variants/f42x
CFLAGS="--target=thumbv7em-none-eabi -mcpu=cortex-m4 -mfpu=fpv4-sp-d16 -mfloat-abi=hard -mthumb -O1 -g \
 -ffreestanding -nostdlib -fno-builtin -Wall -Wextra -I../samples_stm32 -I$F42X"
mkdir -p build
for src in *.c; do
    name=${src%.c}
    echo "CC  $name"
    $CC $CFLAGS -ffunction-sections -fdata-sections -T "$F42X/link.ld" -fuse-ld=lld -Wl,--image-base=0x08000000 \
        -Wl,-e,Reset_Handler -Wl,--gc-sections -o "build/$name.elf" "$F42X/startup.c" "$src" -Wl,-Map,"build/$name.map"
    if command -v llvm-objcopy >/dev/null 2>&1; then
        llvm-objcopy -O binary "build/$name.elf" "build/$name.bin"
    else
        "$PY" ../samples/elf2bin.py "build/$name.elf" "build/$name.bin"
    fi
done
echo "ok: $(ls build/*.elf | wc -l) samples built"
