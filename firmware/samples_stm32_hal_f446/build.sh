#!/bin/sh
# Build the STM32CubeF4 HAL F446 QSPI sample (device header STM32F446xx) with clang + ld.lld (no GNU arm toolchain needed):
#   qspi_hal.c -> build/qspi_hal.elf/.bin/.map (180 MHz PLL_R, USART2, HAL_QSPI; chips/stm32f446re.yaml)
#   fmpi2c_hal.c -> build/fmpi2c_hal.elf (HAL_FMPI2C Fm+ against a 24C02, boards/stm32f446re_fmpi2c_eeprom.yaml chip)
#   cec_hal.c -> build/cec_hal.elf (HAL_CEC follower = TV, address 0; boards/stm32f446re_cec_hal.yaml chip B)
# Separate from ../samples_stm32_hal because it needs the STM32F446xx device header (QUADSPI, PLL_R, DCKCFGR2; the other HAL
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
DEFS="-DSTM32F446xx -DUSE_HAL_DRIVER -DHSE_VALUE=8000000U"
INC="-I. -I$HAL/Inc -I$CUBE/CMSIS/Device/ST/STM32F4xx/Include -I$CUBE/CMSIS/Include"
CFLAGS="$ARCH $DEFS $INC -Os -g -ffreestanding -nostdlib -fno-builtin -ffunction-sections -fdata-sections"
LDFLAGS="-T $COMMON/link.ld -fuse-ld=lld -nostdlib -Wl,--image-base=0x08000000 -Wl,-e,Reset_Handler -Wl,--gc-sections"
HAL_SRCS="stm32f4xx_hal.c stm32f4xx_hal_cortex.c stm32f4xx_hal_rcc.c stm32f4xx_hal_rcc_ex.c stm32f4xx_hal_gpio.c
 stm32f4xx_hal_uart.c stm32f4xx_hal_dma.c stm32f4xx_hal_pwr.c stm32f4xx_hal_pwr_ex.c stm32f4xx_hal_flash.c
 stm32f4xx_hal_flash_ex.c stm32f4xx_hal_exti.c stm32f4xx_hal_qspi.c stm32f4xx_hal_fmpi2c.c stm32f4xx_hal_fmpi2c_ex.c
 stm32f4xx_hal_cec.c"
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
for name in ${SAMPLES:-qspi_hal fmpi2c_hal cec_hal}; do
    $CC $CFLAGS -Wall -Wextra -c "$name.c" -o "build/obj/$name.o"
    echo "LD  $name"
    # shellcheck disable=SC2086
    $CC $ARCH $LDFLAGS -o "build/$name.elf" "build/obj/$name.o" $objs -Wl,-Map,"build/$name.map"
    if command -v llvm-objcopy >/dev/null 2>&1; then
        llvm-objcopy -O binary "build/$name.elf" "build/$name.bin"
    else
        "$PY" ../samples/elf2bin.py "build/$name.elf" "build/$name.bin"
    fi
    echo "ok: build/$name.elf ($(wc -c < "build/$name.bin") bytes)"
done
