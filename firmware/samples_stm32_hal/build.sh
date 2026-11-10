#!/bin/sh
# Build the STM32CubeF4 HAL samples for the STM32F405ZG with clang + ld.lld (no GNU arm toolchain needed):
#   main.c         -> build/uart_chat_hal.elf/.bin/.map  (chat protocol, HAL_UART_Receive_IT)
#   uart_dma_hal.c -> build/uart_dma_hal.elf/.bin/.map   (HAL_UART_Transmit_DMA / HAL_UART_Receive_DMA)
#   i2c_eeprom_hal.c -> build/i2c_eeprom_hal.elf/.bin/.map (HAL_I2C_Mem_Write/Mem_Read, Master_Transmit/Receive)
#   sd_hal.c       -> build/sd_hal.elf/.bin/.map         (HAL_SD_Init / WriteBlocks / ReadBlocks / Erase, SDIO)
#   usb_cdc_hal.c  -> build/usb_cdc_hal.elf/.bin/.map    (HAL PCD + ST USB Device Library CDC-ACM on OTG_FS)
# The HAL, startup and runtime objects are compiled once and linked into each sample.
#
# The HAL comes from ../vendor/STM32CubeF4 (STM32CubeF4 at a depth-1 sparse clone + its two submodules,
# stm32f4xx_hal_driver and cmsis_device_f4).  Fetch it once with:
#   git clone --depth 1 --filter=blob:none --sparse https://github.com/STMicroelectronics/STM32CubeF4 ../vendor/STM32CubeF4
#   cd ../vendor/STM32CubeF4 && git sparse-checkout set Drivers/CMSIS/Include Drivers/CMSIS/Core/Include \
#     && git submodule update --init --depth 1 Drivers/CMSIS/Device/ST/STM32F4xx Drivers/STM32F4xx_HAL_Driver
# stm32f4xx_hal_conf.h is ST's stm32f4xx_hal_conf_template.h with the unused modules commented out and
# HSE_VALUE = 8 MHz; system_stm32f4xx.c is the CMSIS template with USER_VECT_TAB_ADDRESS defined.
set -eu
cd "$(dirname "$0")"
CC=${CC:-clang}
PY=${PY:-"$(cd ../.. && pwd)/.venv/bin/python"}
CUBE=${CUBE:-../vendor/STM32CubeF4/Drivers}
HAL=$CUBE/STM32F4xx_HAL_Driver
if [ ! -f "$HAL/Src/stm32f4xx_hal.c" ]; then
    echo "error: STM32CubeF4 HAL not found under $CUBE (see the fetch commands at the top of build.sh)" >&2
    exit 1
fi
ARCH="--target=thumbv7em-none-eabi -mcpu=cortex-m4 -mfpu=fpv4-sp-d16 -mfloat-abi=hard -mthumb"
DEFS="-DSTM32F405xx -DUSE_HAL_DRIVER -DHSE_VALUE=8000000U"
INC="-I. -I$HAL/Inc -I$CUBE/CMSIS/Device/ST/STM32F4xx/Include -I$CUBE/CMSIS/Include"
CFLAGS="$ARCH $DEFS $INC -Os -g -ffreestanding -nostdlib -fno-builtin -ffunction-sections -fdata-sections"
LDFLAGS="-T link.ld -fuse-ld=lld -nostdlib -Wl,--image-base=0x08000000 -Wl,-e,Reset_Handler -Wl,--gc-sections"
MW=${MW:-../vendor/STM32CubeF4/Middlewares/ST/STM32_USB_Device_Library}
HAL_SRCS="stm32f4xx_hal.c stm32f4xx_hal_cortex.c stm32f4xx_hal_rcc.c stm32f4xx_hal_rcc_ex.c stm32f4xx_hal_gpio.c
 stm32f4xx_hal_uart.c stm32f4xx_hal_dma.c stm32f4xx_hal_pwr.c stm32f4xx_hal_pwr_ex.c stm32f4xx_hal_flash.c
 stm32f4xx_hal_flash_ex.c stm32f4xx_hal_exti.c stm32f4xx_hal_i2c.c stm32f4xx_hal_sd.c stm32f4xx_ll_sdmmc.c"
mkdir -p build/obj
objs=""
for s in startup.c system_stm32f4xx.c aeabi_rt.c; do
    o=build/obj/${s%.c}.o
    $CC $CFLAGS -Wall -Wextra -c "$s" -o "$o"
    objs="$objs $o"
done
for s in $HAL_SRCS; do
    o=build/obj/${s%.c}.o
    $CC $CFLAGS -w -c "$HAL/Src/$s" -o "$o"      # vendor code: warnings silenced
    objs="$objs $o"
done
for pair in main.c:uart_chat_hal uart_dma_hal.c:uart_dma_hal i2c_eeprom_hal.c:i2c_eeprom_hal sd_hal.c:sd_hal; do
    src=${pair%%:*}
    name=${pair#*:}
    $CC $CFLAGS -Wall -Wextra -c "$src" -o "build/obj/${src%.c}.o"
    echo "LD  $name"
    # shellcheck disable=SC2086
    $CC $ARCH $LDFLAGS -o "build/$name.elf" "build/obj/${src%.c}.o" $objs -Wl,-Map,"build/$name.map"
    if command -v llvm-objcopy >/dev/null 2>&1; then
        llvm-objcopy -O binary "build/$name.elf" "build/$name.bin"
    else
        "$PY" ../samples/elf2bin.py "build/$name.elf" "build/$name.bin"
    fi
    echo "ok: build/$name.elf ($(wc -c < "build/$name.bin") bytes)"
done
# USB CDC-ACM: HAL PCD / LL USB + the ST USB Device Library (vendored, see $MW/VENDORED.md), only linked here
usb_objs=""
for s in $HAL/Src/stm32f4xx_hal_pcd.c $HAL/Src/stm32f4xx_hal_pcd_ex.c $HAL/Src/stm32f4xx_ll_usb.c \
         $MW/Core/Src/usbd_core.c $MW/Core/Src/usbd_ctlreq.c $MW/Core/Src/usbd_ioreq.c $MW/Class/CDC/Src/usbd_cdc.c; do
    o=build/obj/$(basename "${s%.c}").o
    $CC $CFLAGS -I$MW/Core/Inc -I$MW/Class/CDC/Inc -w -c "$s" -o "$o"
    usb_objs="$usb_objs $o"
done
$CC $CFLAGS -I$MW/Core/Inc -I$MW/Class/CDC/Inc -Wall -Wextra -c usb_cdc_hal.c -o build/obj/usb_cdc_hal.o
echo "LD  usb_cdc_hal"
# shellcheck disable=SC2086
$CC $ARCH $LDFLAGS -o build/usb_cdc_hal.elf build/obj/usb_cdc_hal.o $usb_objs $objs -Wl,-Map,build/usb_cdc_hal.map
if command -v llvm-objcopy >/dev/null 2>&1; then
    llvm-objcopy -O binary build/usb_cdc_hal.elf build/usb_cdc_hal.bin
else
    "$PY" ../samples/elf2bin.py build/usb_cdc_hal.elf build/usb_cdc_hal.bin
fi
echo "ok: build/usb_cdc_hal.elf ($(wc -c < build/usb_cdc_hal.bin) bytes)"
