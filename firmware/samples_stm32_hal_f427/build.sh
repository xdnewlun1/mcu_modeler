#!/bin/sh
# Build the STM32CubeF4 HAL F427 compatibility sample (device header STM32F427xx) with clang + ld.lld (no GNU arm toolchain needed):
#   clock_uart7_hal.c -> build/clock_uart7_hal.elf/.bin/.map (180 MHz over-drive, PLLSAI, UART7; chips/stm32f427vg.yaml)
# Separate from ../samples_stm32_hal because it needs the STM32F427xx device header (UART7, PLLSAI, over-drive; the other HAL
# samples are built for the STM32F405xx).  Startup, system file, runtime helpers and linker script are shared with
# ../samples_stm32_hal; the HAL comes from ../vendor/STM32CubeF4 (see ../samples_stm32_hal/build.sh for the fetch).
set -eu
cd "$(dirname "$0")"
CC=${CC:-clang}
PY=${PY:-"$(cd ../.. && pwd)/.venv/bin/python"}
CUBE=${CUBE:-../vendor/STM32CubeF4/Drivers}
HAL=$CUBE/STM32F4xx_HAL_Driver
COMMON=../samples_stm32_hal
if [ ! -f "$HAL/Src/stm32f4xx_hal_pwr_ex.c" ]; then
    echo "error: STM32CubeF4 HAL not found under $CUBE (see ../samples_stm32_hal/build.sh)" >&2
    exit 1
fi
ARCH="--target=thumbv7em-none-eabi -mcpu=cortex-m4 -mfpu=fpv4-sp-d16 -mfloat-abi=hard -mthumb"
DEFS="-DSTM32F427xx -DUSE_HAL_DRIVER -DHSE_VALUE=8000000U"
INC="-I. -I$HAL/Inc -I$CUBE/CMSIS/Device/ST/STM32F4xx/Include -I$CUBE/CMSIS/Include"
CFLAGS="$ARCH $DEFS $INC -Os -g -ffreestanding -nostdlib -fno-builtin -ffunction-sections -fdata-sections"
LDFLAGS="-T $COMMON/link.ld -fuse-ld=lld -nostdlib -Wl,--image-base=0x08000000 -Wl,-e,Reset_Handler -Wl,--gc-sections"
HAL_SRCS="stm32f4xx_hal.c stm32f4xx_hal_cortex.c stm32f4xx_hal_rcc.c stm32f4xx_hal_rcc_ex.c stm32f4xx_hal_gpio.c
 stm32f4xx_hal_uart.c stm32f4xx_hal_dma.c stm32f4xx_hal_pwr.c stm32f4xx_hal_pwr_ex.c stm32f4xx_hal_flash.c
 stm32f4xx_hal_flash_ex.c stm32f4xx_hal_exti.c"
mkdir -p build/obj
objs=""
for s in startup.c system_stm32f4xx.c aeabi_rt.c; do
    o=build/obj/${s%.c}.o
    $CC $CFLAGS -Wall -Wextra -c "$COMMON/$s" -o "$o"
    objs="$objs $o"
done
for s in $HAL_SRCS; do
    o=build/obj/${s%.c}.o
    $CC $CFLAGS -w -c "$HAL/Src/$s" -o "$o"      # vendor code: warnings silenced
    objs="$objs $o"
done
$CC $CFLAGS -Wall -Wextra -c clock_uart7_hal.c -o build/obj/clock_uart7_hal.o
echo "LD  clock_uart7_hal"
# shellcheck disable=SC2086
$CC $ARCH $LDFLAGS -o build/clock_uart7_hal.elf build/obj/clock_uart7_hal.o $objs -Wl,-Map,build/clock_uart7_hal.map
if command -v llvm-objcopy >/dev/null 2>&1; then
    llvm-objcopy -O binary build/clock_uart7_hal.elf build/clock_uart7_hal.bin
else
    "$PY" ../samples/elf2bin.py build/clock_uart7_hal.elf build/clock_uart7_hal.bin
fi
echo "ok: build/clock_uart7_hal.elf ($(wc -c < build/clock_uart7_hal.bin) bytes)"
