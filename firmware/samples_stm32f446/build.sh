#!/bin/sh
# Build every <name>.c in this directory into build/<name>.elf and .bin (clang + ld.lld; no GNU arm toolchain needed).
# STM32F446xx samples (chips/stm32f446re.yaml and its package variants): f446/startup.c (97-entry vector table,
# stm32f446xx.h IRQn_Type), f446/link.ld (512 KB flash, 128 KB SRAM) and f446/stm32f446.h (on top of
# ../samples_stm32/stm32f405.h).
#   qspi_flash.c -> STM32F446RE (LQFP64, dual I/O)   qspi_quad.c -> STM32F446ZE (LQFP144, quad I/O)
#   fmpi2c_eeprom.c -> STM32F446RE FMPI2C1 Fm+ controller + 24C02 (boards/stm32f446re_fmpi2c_eeprom.yaml)
set -eu
cd "$(dirname "$0")"
CC=${CC:-clang}
PY=${PY:-"$(cd ../.. && pwd)/.venv/bin/python"}
CFLAGS="--target=thumbv7em-none-eabi -mcpu=cortex-m4 -mfpu=fpv4-sp-d16 -mfloat-abi=hard -mthumb -O1 -g \
 -ffreestanding -nostdlib -fno-builtin -Wall -Wextra -If446 -I../samples_stm32"
mkdir -p build
for src in *.c; do
    name=${src%.c}
    echo "CC  $name"
    $CC $CFLAGS -ffunction-sections -fdata-sections -T f446/link.ld -fuse-ld=lld -Wl,--image-base=0x08000000 \
        -Wl,-e,Reset_Handler -Wl,--gc-sections -o "build/$name.elf" f446/startup.c "$src" -Wl,-Map,"build/$name.map"
    if command -v llvm-objcopy >/dev/null 2>&1; then
        llvm-objcopy -O binary "build/$name.elf" "build/$name.bin"
    else
        "$PY" ../samples/elf2bin.py "build/$name.elf" "build/$name.bin"
    fi
done
echo "ok: $(ls build/*.elf | wc -l) samples built"
