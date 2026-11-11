#!/bin/sh
# Build the TMS320F28232 C samples (*.c here; f2808_*.c are TMS320F2808 samples linked with f2808.cmd) with TI's C2000 code generation tools into build/<name>.out:
#   build/<name>.out      ELF / EABI (--abi=eabi,    rts2800_ml_eabi.lib)   the compiler's default ABI
#   build/<name>_coff.out COFF ABI   (--abi=coffabi, rts2800_ml.lib)        if this compiler still supports COFF
# plus <name>.map and a dis2000 listing <name>.dis.  CHIP_EM loads both (COFF and C2000 ELF are sniffed).
#
# Toolchain: TI_CGT, i.e. $TI_CGT/bin/cl2000, include/, lib/.  Default: toolchains/ti-cgt-c2000 at the repo root,
# else the newest toolchains/**/ti-cgt-c2000*/bin/cl2000 (TI's installer creates
# toolchains/ti-cgt-c2000/ti-cgt-c2000_25.11.1.LTS).  Tested with cl2000 25.11.1.LTS.
# Compatibility: cl2000 has taken -v28 -ml -mt --float_support --abi --rom_model --stack_size --entry_point since
# v6; EABI exists from v18.12 (the default since v20.2) and COFF ABI was deprecated afterwards — this script
# probes `--abi=coffabi` and skips the COFF build with a message if the installed version rejects it.
#   ABI=eabi ./build.sh     only EABI;   ABI=coff ./build.sh   only COFF
set -eu
case "${TI_CGT:-}" in                                  # a relative TI_CGT is relative to the caller's directory
    ""|/*) ;;
    *) TI_CGT="$(cd "$TI_CGT" 2>/dev/null && pwd || echo "$PWD/$TI_CGT")" ;;
esac
cd "$(dirname "$0")"
ROOT="$(cd ../.. && pwd)"
if [ -z "${TI_CGT:-}" ]; then
    TI_CGT="$ROOT/toolchains/ti-cgt-c2000"
    if [ ! -x "$TI_CGT/bin/cl2000" ]; then          # TI's installer names the directory ti-cgt-c2000_<version>
        found="$(find "$ROOT/toolchains" -path '*ti-cgt-c2000*/bin/cl2000' -type f 2>/dev/null | sort | tail -n 1)"
        [ -n "$found" ] && TI_CGT="$(dirname "$(dirname "$found")")"
    fi
fi
CL="$TI_CGT/bin/cl2000"
DIS="$TI_CGT/bin/dis2000"
ABI="${ABI:-both}"

if [ ! -x "$CL" ]; then
    cat >&2 <<MSG
error: TI C2000 compiler not found at $CL
  Install TI's C2000 code generation tools, e.g.
    chmod +x $ROOT/toolchains/ti_cgt_c2000_25.11.1.LTS_linux-x64_installer.bin
    $ROOT/toolchains/ti_cgt_c2000_25.11.1.LTS_linux-x64_installer.bin   (install into $ROOT/toolchains/,
                                                                       so that ti-cgt-c2000/bin/cl2000 exists)
  or point TI_CGT at an existing installation:  TI_CGT=/opt/ti/ti-cgt-c2000_25.11.1.LTS $0
  (The assembler samples in firmware/samples_c2000/ need no TI tools: firmware/samples_c2000/build.sh)
MSG
    exit 1
fi
"$CL" --compiler_revision 2>/dev/null | sed 's/^/cl2000 /' || true

CFLAGS="-v28 -ml -mt --float_support=softlib -O2 -g --diag_warning=225 --display_error_number -I. -I$TI_CGT/include"
# --rom_model (-c): _c_int00 performs the .cinit auto-initialisation at run time (RAM model -cr would mark .cinit
# COPY and expect the loader to do it; CHIP_EM's loader skips COPY sections and does not auto-initialise).
LFLAGS="-z --rom_model --diag_suppress=10063 --stack_size=0x300 --heap_size=0 --entry_point=code_start --warn_sections -i$TI_CGT/lib"

lib_for() {   # $1 = coff|eabi: the run-time library name, falling back to libc.a (TI's selecting index library)
    if [ "$1" = coff ] && [ -f "$TI_CGT/lib/rts2800_ml.lib" ]; then echo rts2800_ml.lib
    elif [ "$1" = eabi ] && [ -f "$TI_CGT/lib/rts2800_ml_eabi.lib" ]; then echo rts2800_ml_eabi.lib
    else echo libc.a; fi
}

coff_ok() {
    probe="build/.probe"
    mkdir -p "$probe"
    echo 'int x;' > "$probe/p.c"
    "$CL" -v28 -ml --abi=coffabi --obj_directory="$probe" -c "$probe/p.c" >/dev/null 2>&1
}

# TMS320F2808 samples (f2808_*.c): their own linker file (f2808.cmd: flash at 0x3E8000, codestart 0x3F7FF6, H0 for
# variables) and no --float_support (the 280x has no FPU; soft-float comes from the RTS as on the F28232 build).
CFLAGS_F2808="-v28 -ml -mt -O2 -g --diag_warning=225 --display_error_number -I. -I$TI_CGT/include"

build_one() {   # $1 = coff|eabi, $2 = sample name, $3 = output suffix
    abi=$1; name=$2; out="build/$name$3.out"; obj="build/obj_$abi/$name"
    case "$name" in
        f2808_*) cflags="$CFLAGS_F2808"; cmd=f2808.cmd ;;
        *)       cflags="$CFLAGS"; cmd=f28232.cmd ;;
    esac
    mkdir -p "$obj"
    echo "CL  $name ($abi) -> $out"
    "$CL" $cflags --abi="$( [ "$abi" = coff ] && echo coffabi || echo eabi )" --obj_directory="$obj" \
        "$name.c" codestart.asm $LFLAGS -m "build/$name$3.map" -o "$out" -l "$(lib_for "$abi")" "$cmd"
    if [ -x "$DIS" ]; then "$DIS" "$out" > "build/$name$3.dis" 2>/dev/null || true; fi
}

mkdir -p build
abis=""
case "$ABI" in
    both) if coff_ok; then abis="eabi coff"; else
              echo "note: this cl2000 rejects --abi=coffabi (COFF ABI removed); building EABI only" >&2; abis="eabi"; fi ;;
    coff) abis="coff" ;;
    eabi) abis="eabi" ;;
    *) echo "ABI must be both, coff or eabi" >&2; exit 2 ;;
esac
for src in *.c; do
    name=${src%.c}
    for abi in $abis; do
        if [ "$abi" = eabi ]; then build_one eabi "$name" ""
        elif [ "$abis" = coff ]; then build_one coff "$name" ""        # COFF only: plain <name>.out
        else build_one coff "$name" "_coff"; fi
    done
done
echo "ok: $(ls build/*.out | wc -l) images in build/"
