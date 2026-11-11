#!/bin/sh
# Build the STM32CubeF4 HAL F401/F411 sample with clang + ld.lld (no GNU arm toolchain needed), once per device header:
#   clock_uart_hal.c -> build/f401_clock_uart_hal.elf (-DSTM32F401xE, chips/stm32f401re.yaml)
#                    -> build/f411_clock_uart_hal.elf (-DSTM32F411xE, chips/stm32f411re.yaml)
# Startup, system file and runtime helpers are shared with ../samples_stm32_hal (the startup's 82-entry F405 vector
# table covers every vector the sample uses); link.ld here has the 64 KB SRAM of the smallest part. The HAL comes from
# ../vendor/STM32CubeF4 (see ../samples_stm32_hal/build.sh for the fetch).
set -eu
cd "$(dirname "$0")"
CC=${CC:-clang}
PY=${PY:-"$(cd ../.. && pwd)/.venv/bin/python"}
CUBE=${CUBE:-../vendor/STM32CubeF4/Drivers}
HAL=$CUBE/STM32F4xx_HAL_Driver
COMMON=../samples_stm32_hal
if [ ! -f "$HAL/Src/stm32f4xx_hal_rcc_ex.c" ]; then
    echo "error: STM32CubeF4 HAL not found under $CUBE (see ../samples_stm32_hal/build.sh)" >&2
    exit 1
fi
ARCH="--target=thumbv7em-none-eabi -mcpu=cortex-m4 -mfpu=fpv4-sp-d16 -mfloat-abi=hard -mthumb"
INC="-I. -I$HAL/Inc -I$CUBE/CMSIS/Device/ST/STM32F4xx/Include -I$CUBE/CMSIS/Include"
LDFLAGS="-T link.ld -fuse-ld=lld -nostdlib -Wl,--image-base=0x08000000 -Wl,-e,Reset_Handler -Wl,--gc-sections"
HAL_SRCS="stm32f4xx_hal.c stm32f4xx_hal_cortex.c stm32f4xx_hal_rcc.c stm32f4xx_hal_rcc_ex.c stm32f4xx_hal_gpio.c
 stm32f4xx_hal_uart.c stm32f4xx_hal_dma.c stm32f4xx_hal_pwr.c stm32f4xx_hal_pwr_ex.c stm32f4xx_hal_flash.c
 stm32f4xx_hal_flash_ex.c stm32f4xx_hal_exti.c"
for dev in F401:STM32F401xE F411:STM32F411xE; do
    tag=$(echo "${dev%%:*}" | tr 'A-Z' 'a-z')
    CFLAGS="$ARCH -D${dev##*:} -DUSE_HAL_DRIVER -DHSE_VALUE=8000000U $INC -Os -g -ffreestanding -nostdlib -fno-builtin \
 -ffunction-sections -fdata-sections"
    obj=build/obj_$tag
    mkdir -p "$obj"
    objs=""
    for s in startup.c system_stm32f4xx.c aeabi_rt.c; do
        $CC $CFLAGS -Wall -Wextra -c "$COMMON/$s" -o "$obj/${s%.c}.o"
        objs="$objs $obj/${s%.c}.o"
    done
    for s in $HAL_SRCS; do
        $CC $CFLAGS -w -c "$HAL/Src/$s" -o "$obj/${s%.c}.o"     # vendor code: warnings silenced
        objs="$objs $obj/${s%.c}.o"
    done
    $CC $CFLAGS -Wall -Wextra -c clock_uart_hal.c -o "$obj/clock_uart_hal.o"
    out=build/${tag}_clock_uart_hal
    echo "LD  ${tag}_clock_uart_hal"
    # shellcheck disable=SC2086
    $CC $ARCH $LDFLAGS -o "$out.elf" "$obj/clock_uart_hal.o" $objs -Wl,-Map,"$out.map"
    if command -v llvm-objcopy >/dev/null 2>&1; then
        llvm-objcopy -O binary "$out.elf" "$out.bin"
    else
        "$PY" ../samples/elf2bin.py "$out.elf" "$out.bin"
    fi
done
echo "ok: $(ls build/*.elf | wc -l) HAL samples built"
