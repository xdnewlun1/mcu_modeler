#!/bin/sh
# Build the STM32F401 / STM32F411 samples (chips/stm32f401*.yaml, chips/stm32f411*.yaml) with clang + ld.lld:
#   clock_uart.c          -> build/f401_clock_uart.elf (-DLINE_F401, 84 MHz) and build/f411_clock_uart.elf (-DLINE_F411, 100 MHz)
#   f401_*.c / f411_*.c   -> build/<name>.elf, built for that line
# startup.c (86-entry vector table), link.ld (256 KB flash / 64 KB SRAM: fits every F401/F411 part), stm32f4x1.h.
set -eu
cd "$(dirname "$0")"
CC=${CC:-clang}
PY=${PY:-"$(cd ../.. && pwd)/.venv/bin/python"}
CFLAGS="--target=thumbv7em-none-eabi -mcpu=cortex-m4 -mfpu=fpv4-sp-d16 -mfloat-abi=hard -mthumb -O1 -g \
 -ffreestanding -nostdlib -fno-builtin -Wall -Wextra -Wno-initializer-overrides"
LDFLAGS="-T link.ld -fuse-ld=lld -Wl,--image-base=0x08000000 -Wl,-e,Reset_Handler -Wl,--gc-sections"
mkdir -p build
build() {   # build <name> <define> <source>
    echo "CC  $1"
    $CC $CFLAGS -D"$2" -ffunction-sections -fdata-sections $LDFLAGS -o "build/$1.elf" startup.c "$3" -Wl,-Map,"build/$1.map"
    if command -v llvm-objcopy >/dev/null 2>&1; then
        llvm-objcopy -O binary "build/$1.elf" "build/$1.bin"
    else
        "$PY" ../samples/elf2bin.py "build/$1.elf" "build/$1.bin"
    fi
}
build f401_clock_uart LINE_F401 clock_uart.c
build f411_clock_uart LINE_F411 clock_uart.c
for src in f401_*.c; do build "${src%.c}" LINE_F401 "$src"; done
for src in f411_*.c; do build "${src%.c}" LINE_F411 "$src"; done
echo "ok: $(ls build/*.elf | wc -l) samples built"
