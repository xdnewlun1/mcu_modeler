#!/bin/sh
# Build the STM32CubeF4 HAL F429 sample (device header STM32F429xx) with clang + ld.lld (no GNU arm toolchain needed):
#   ltdc_sdram_hal.c -> build/ltdc_sdram_hal.elf/.bin/.map (180 MHz, HAL SDRAM on FMC bank 2, HAL DMA2D fills, HAL LTDC
#   240x320 RGB565 with a line event; chips/stm32f429zi.yaml, boards/stm32f429zi_disco_lcd.yaml wiring)
# The vector table (LTDC_IRQHandler = IRQ 88) and linker script come from ../samples_stm32_variants/f42x (STM32F42x/43x
# startup, 192 KB SRAM); the CMSIS system file comes from ../samples_stm32_hal; the HAL from ../vendor/STM32CubeF4
# (see ../samples_stm32_hal/build.sh for the fetch).
set -eu
cd "$(dirname "$0")"
CC=${CC:-clang}
PY=${PY:-"$(cd ../.. && pwd)/.venv/bin/python"}
CUBE=${CUBE:-../vendor/STM32CubeF4/Drivers}
HAL=$CUBE/STM32F4xx_HAL_Driver
COMMON=../samples_stm32_hal
F42X=../samples_stm32_variants/f42x
if [ ! -f "$HAL/Src/stm32f4xx_hal_ltdc.c" ]; then
    echo "error: STM32CubeF4 HAL not found under $CUBE (see ../samples_stm32_hal/build.sh)" >&2
    exit 1
fi
ARCH="--target=thumbv7em-none-eabi -mcpu=cortex-m4 -mfpu=fpv4-sp-d16 -mfloat-abi=hard -mthumb"
DEFS="-DSTM32F429xx -DUSE_HAL_DRIVER -DHSE_VALUE=8000000U"
INC="-I. -I$HAL/Inc -I$CUBE/CMSIS/Device/ST/STM32F4xx/Include -I$CUBE/CMSIS/Include"
CFLAGS="$ARCH $DEFS $INC -Os -g -ffreestanding -nostdlib -fno-builtin -ffunction-sections -fdata-sections"
LDFLAGS="-T $F42X/link.ld -fuse-ld=lld -nostdlib -Wl,--image-base=0x08000000 -Wl,-e,Reset_Handler -Wl,--gc-sections"
HAL_SRCS="stm32f4xx_hal.c stm32f4xx_hal_cortex.c stm32f4xx_hal_rcc.c stm32f4xx_hal_rcc_ex.c stm32f4xx_hal_gpio.c
 stm32f4xx_hal_uart.c stm32f4xx_hal_dma.c stm32f4xx_hal_pwr.c stm32f4xx_hal_pwr_ex.c stm32f4xx_hal_flash.c
 stm32f4xx_hal_flash_ex.c stm32f4xx_hal_exti.c stm32f4xx_hal_sdram.c stm32f4xx_ll_fmc.c stm32f4xx_hal_ltdc.c
 stm32f4xx_hal_ltdc_ex.c stm32f4xx_hal_dma2d.c"
mkdir -p build/obj
objs=""
$CC --target=thumbv7em-none-eabi -mcpu=cortex-m4 -mfpu=fpv4-sp-d16 -mfloat-abi=hard -mthumb -Os -g -ffreestanding \
    -nostdlib -fno-builtin -ffunction-sections -fdata-sections -Wall -Wextra -I../samples_stm32 -I$F42X \
    -c "$F42X/startup.c" -o build/obj/startup.o
objs="build/obj/startup.o"
for s in system_stm32f4xx.c aeabi_rt.c; do
    o=build/obj/${s%.c}.o
    $CC $CFLAGS -Wall -Wextra -c "$COMMON/$s" -o "$o"
    objs="$objs $o"
done
for s in $HAL_SRCS; do
    o=build/obj/${s%.c}.o
    $CC $CFLAGS -w -c "$HAL/Src/$s" -o "$o"      # vendor code: warnings silenced
    objs="$objs $o"
done
$CC $CFLAGS -Wall -Wextra -c ltdc_sdram_hal.c -o build/obj/ltdc_sdram_hal.o
echo "LD  ltdc_sdram_hal"
# shellcheck disable=SC2086
$CC $ARCH $LDFLAGS -o build/ltdc_sdram_hal.elf build/obj/ltdc_sdram_hal.o $objs -Wl,-Map,build/ltdc_sdram_hal.map
if command -v llvm-objcopy >/dev/null 2>&1; then
    llvm-objcopy -O binary build/ltdc_sdram_hal.elf build/ltdc_sdram_hal.bin
else
    "$PY" ../samples/elf2bin.py build/ltdc_sdram_hal.elf build/ltdc_sdram_hal.bin
fi
echo "ok: build/ltdc_sdram_hal.elf ($(wc -c < build/ltdc_sdram_hal.bin) bytes)"
