#!/bin/sh
# Build the SDCC C samples for the P89CV51RD2 into build/: <name>.ihx (Intel HEX, loaded by chipem), <name>.cdb
# (SDCC debug info: line table, functions, symbols -> chipem/sdcc_cdb.py), <name>.map / .mem (linker map, memory
# usage).  The outputs are tracked so the tests run without SDCC.
#
# SDCC binary names: upstream `sdcc`, Fedora `sdcc-sdcc` (all SDCC tools carry the `sdcc-` prefix there).
# Set SDCC=/path/to/sdcc to override.  Tested with SDCC 4.5.0 (mcs51 port).
#   ./build.sh                 all samples
#   ./build.sh blinky math     only these (models / math build every memory-model variant)
#
# Samples that include uart.h are linked with uart.c (interrupt-driven UART, putchar/getchar for printf).
# models.c is built as models_small, models_medium, models_large, models_small_sa, models_large_sa (sa:
# --stack-auto); math.c as math_small and math_large.  Code at 0000h, 256 B internal RAM, 768 B on-chip XRAM.
set -eu
cd "$(dirname "$0")"
if [ -z "${SDCC:-}" ]; then
    for n in sdcc sdcc-sdcc; do
        if command -v "$n" >/dev/null 2>&1; then SDCC=$n; break; fi
    done
fi
if [ -z "${SDCC:-}" ]; then
    echo "error: SDCC not found (looked for sdcc, sdcc-sdcc); install SDCC 4.x or set SDCC=" >&2
    exit 1
fi
COMMON="-mmcs51 --debug --iram-size 256 --xram-size 768 --code-size 65536 --std-sdcc11"
mkdir -p build

build() {      # build <out-name> <main.c> <extra flags...>
    out=$1; src=$2; shift 2
    objdir="build/obj_$out"
    mkdir -p "$objdir"
    objs=""
    mods="$src"
    if grep -q '"uart.h"' "$src"; then mods="$mods uart.c"; fi
    for m in $mods; do
        "$SDCC" $COMMON "$@" -c "$m" -o "$objdir/${m%.c}.rel"
        objs="$objs $objdir/${m%.c}.rel"
    done
    "$SDCC" $COMMON "$@" $objs -o "build/$out.ihx"
    # keep the .ihx, .cdb, .map, .mem next to each other; drop the rest of the linker byproducts
    rm -f "build/$out.lk" "build/$out.omf" "build/$out.noi"
    rm -rf "$objdir"
    echo "CC  $out"
}

ALL="blinky uart_console timer_tick pca_pwm spi_flash iap_flash xram_test power_down models math"
SEL="${*:-$ALL}"
for name in $SEL; do
    case "$name" in
        models)
            build models_small models.c --model-small
            build models_medium models.c --model-medium
            build models_large models.c --model-large
            build models_small_sa models.c --model-small --stack-auto
            build models_large_sa models.c --model-large --stack-auto ;;
        math)
            build math_small math.c --model-small
            build math_large math.c --model-large ;;
        *)  build "$name" "$name.c" --model-small ;;
    esac
done
echo "ok: $(ls build/*.ihx | wc -l) images in build/"
