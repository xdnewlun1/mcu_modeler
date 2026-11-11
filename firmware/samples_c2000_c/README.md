# TMS320F28232 C samples (TI C2000 compiler)

C versions of the F28232 samples, built with TI's C2000 code generation tools (`cl2000` 25.11.1.LTS) into TI `.out`
images that CHIP_EM loads directly. Status (2026-11-05): all three samples build for EABI (ELF) and the COFF ABI and
run correctly on the emulator; `tests/test_c2000_compiled.py` checks them (it skips when `build/` is empty, because
the compiler is not part of the repository).

| file | what |
|---|---|
| `f28232.h` | register definitions written for this repo: plain `volatile unsigned int/long` structs (no bitfields) at the absolute word addresses also used by `firmware/samples_c2000/*.asm`, plus `EALLOW`/`EDIS`/`EINT`/`DINT`/`IDLE` macros and `__cregister IER/IFR` |
| `uart_chat.c` | the two-way UART chat protocol (same as `firmware/samples_c2000/uart_chat.asm`): HELLO at boot, PING every 5 ms, PONG replies, LED GPIO34 per PONG, GPIO35 high on HELLO, the first HELLO answered once. SCI-A RX interrupt (PIE INT9.1), CPU timer 0 interrupt (INT1.7) |
| `blinky.c` | GPIO34 toggles every 1 ms (CPU timer 0, TIF polled) |
| `selftest.c` | no peripherals: 47 results of 16/32/64-bit integer arithmetic (RTS division helpers), soft-float, conversions, `memcpy`/`strcmp`/`qsort`/`atol`/`snprintf`, a switch table, recursion and function pointers, written to `results[]` and compared with host-computed values by the test |
| `adc_dma.c` | ePWM1 SOCA (10 kHz) → ADC SEQ1 (ADCINA0-A3) → SEQ1INT → DMA CH1: 4-word bursts from the PF0 results (source wrap RESULT0-3 / RESULT4-7) into a 2 x 32-word ping-pong buffer in L4; continuous mode, DINTCH1 (PIE 7.1) at each transfer start swaps the destination shadow; main prints each completed buffer on SCI-A (`DMA <n> BUF<b> <A0> <A1> <A2> <A3> SUM <sum>`) |
| `i2c_eeprom.c` | I2C-A master at 100 kHz (module clock 10 MHz) on GPIO32/33: writes "Hello over I2C" into a 24C32 EEPROM at 0x50 (FIFO mode, I2CCNT + STP), acknowledge-polls the write cycle, reads it back (dummy write, ARDY, repeated START) and prints `EEPROM: Hello over I2C` on SCI-A; board `boards/c2000_i2c_eeprom.yaml` |
| `codestart.asm` | `codestart` section at 0x33FFF6: `LB wd_disable` → watchdog off → `LB _c_int00` (TI's CodeStartBranch scheme) |
| `f28232.cmd` | linker command file: flash 0x330000–0x33FF7F, BEGIN 0x33FFF6, CSM areas, M0/M1, L0–L5 (the F28232 has 26K SARAM, so there is no L6/L7); `.stack` in M1, variables in L4, `.esysmem`/`.sysmem` in L5; COFF and EABI section names via `#if defined(__TI_EABI__)` |
| `build.sh` | builds every `*.c` here (with `codestart.asm`) for both ABIs; `f2808_*.c` are linked with `f2808.cmd` and built without `--float_support`. Prints install instructions if `cl2000` is missing |
| `f2808.h`, `f2808.cmd` | TMS320F2808 register definitions (PLLSTS.CLKINDIV, 280x PCLKCR0 gates, ports A/B) and linker file (flash 0x3E8000-0x3F7F7F, BEGIN 0x3F7FF6, CSM areas, M0/M1, L0/L1, H0 for variables). `codestart.asm` is shared (its section is placed by the .cmd file) |
| `f2808_blinky.c` | F2808: GPIO34 toggles every 1 ms (CPU timer 0, TIF polled) |
| `f2808_adc.c` | F2808: ePWM1 SOCA every 500 us → ADC SEQ1 (ADCINA0/A1) → `interrupt void seq1_isr` (PIE 1.1); prints `ADC <n> <a0> <a1>` on SCI-A every 10 samples |
| `f2808_can.c` | F2808: eCAN-B (GPIO8/GPIO10, 500 kbit/s from the 100 MHz eCAN clock) sends `100#00<n>4B32` every 10 ms and prints the `202#...` answers of the STM32 `can_echo` (`boards/f2808_stm32_can.yaml`); tests in `tests/test_c2000_f2808_samples.py` |
| `f28335.h`, `f28335.cmd`, `f28335_fpu_asm.asm` | TMS320F28335: register definitions (`f28232.h` + ePWM/ADC: same die, same addresses), linker file (flash 0x300000-0x33FF7F, BEGIN 0x33FFF6, M0/M1, L0-L7), and hand-written FPU32 helpers for `f28335_fpu` (STF.RND32 = 0, STF/MOVST0 flags, RPTB #imm). `build.sh` compiles `f28335_*.c` with `--float_support=fpu32` and `rts2800_fpu32[_eabi].lib`, and links `<name>_asm.asm` when it exists |
| `f28335_fpu.c` | F28335: 44 FPU32 results (arithmetic, conversions with rounding ties, compares via MOVST0, MIN/MAXF32, RPTB + RPT‖MACF32 dot product and FIR, RPTB Horner, truncation mode, LVF/LUF flags) checked against host IEEE single precision by `tests/test_c2000_f2833x_fpu.py` |
| `f28335_pid.c` | F28335 at 150 MHz: ePWM1 20 kHz SOCA → ADC SEQ1 → `__interrupt adc_isr` float PI loop (RPTB average, FPU context save) driving CMPA; main repeats float work and counts differences; status lines on SCI-A; `boards/f28335_fpu_loop.yaml` (RC plant script part) |
| `f28335_can.c` | F28335 at 150 MHz: eCAN-A (GPIO30/31, 500 kbit/s from the SYSCLKOUT/2 eCAN clock) sends `100#00<n>4B32` every 10 ms and prints the STM32 `can_echo` answers (`202#...`) on SCI-A; `boards/f28335_stm32_can.yaml` |

## Building

```sh
firmware/samples_c2000_c/build.sh                 # EABI + COFF
ABI=eabi firmware/samples_c2000_c/build.sh        # one ABI only (ABI=coff likewise; the output is then <name>.out)
firmware/samples_c2000_c/build.sh f28335_fpu f28335_pid   # only these samples (other outputs untouched)
TI_CGT=/opt/ti/ti-cgt-c2000_25.11.1.LTS firmware/samples_c2000_c/build.sh
```

The compiler is found without any environment variable: `TI_CGT` defaults to `toolchains/ti-cgt-c2000`, and if that
has no `bin/cl2000`, to the newest `toolchains/**/ti-cgt-c2000*/bin/cl2000` (TI's installer creates
`toolchains/ti-cgt-c2000/ti-cgt-c2000_25.11.1.LTS`). A relative `TI_CGT` is taken relative to the caller's directory.

Flags that work with cl2000 25.11.1.LTS:

- compile: `-v28 -ml -mt --float_support=softlib -O2 -g --abi=eabi|coffabi` (the F28232 has no FPU;
  `--float_support=none` does not exist in 25.x, the choices are `fpu32`, `fpu64`, `softlib`)
- link: `-z --rom_model --diag_suppress=10063 --stack_size=0x300 --heap_size=0 --entry_point=code_start`, with
  `rts2800_ml_eabi.lib` (EABI) or `rts2800_ml.lib` (COFF). Warning 10063 ("entry point other than _c_int00") is
  expected: the entry is `code_start`, which disables the watchdog and branches to `_c_int00`.
- `--abi=coffabi` is still accepted by 25.11.1 (the script probes it and skips COFF if a future version drops it).
  `__interrupt`, `__cregister`, `asm(" EALLOW")` and the `#if defined(__TI_EABI__)` linker-file sections work as
  written.

Outputs in `build/`: `<name>.out` (EABI ELF, the compiler's default ABI) and `<name>_coff.out` (TI COFF2), each with
a `.map` file and a `dis2000` listing (`.dis`), plus the object files in `obj_eabi/` and `obj_coff/`.

## Running

```sh
.venv/bin/chipem run firmware/samples_c2000_c/build/blinky.out --chip tms320f28232 --for 10ms
.venv/bin/chipem run firmware/samples_c2000_c/build/uart_chat.out --chip tms320f28232 --for 30ms \
    --uart-in 'SCIA:HELLO STM32\r\nPING 1\r\n'
.venv/bin/chipem run --board boards/c2000_stm32_chat_c.yaml --for 40ms --pin-report
.venv/bin/chipem info firmware/samples_c2000_c/build/uart_chat.out --chip tms320f28232
.venv/bin/python tools/c28x_asm.py --disasm firmware/samples_c2000_c/build/uart_chat.out    # like dis2000
```

What they do (EABI and COFF behave the same; the C start-up adds about 0.02–0.09 ms compared with the asm samples):

- `blinky`: the PLL relocks at 6.58 ms (100 MHz), then GPIO34 toggles at 7.58, 8.58, 9.58 ms ... (1.000 ms).
- `uart_chat` alone (with `--uart-in` as above, sent after 7.5 ms in the test): `HELLO C2000` at 6.6 ms, then
  `HELLO C2000` (the once-only answer), `PONG 1`, and `PING 1`, `PING 2` ... every 5.000 ms from 11.6 ms. GPIO35 goes
  high on the HELLO line.
- `boards/c2000_stm32_chat_c.yaml` (with the STM32 HAL firmware): the same 130-byte conversation in each direction
  as `boards/c2000_stm32_chat_hal.yaml` with the asm firmware, line for line, within 0.1 ms; GPIO34/PB0 toggle per
  PONG and GPIO35/PB1 end high.
- `selftest`: `selftest_done` = 0x600D after about 4 ms (it stays at the 10 MHz reset clock); all 47 results match.

## What the first real build exposed

- `build.sh`: the versioned install directory was not found and a relative `TI_CGT` broke after the script's `cd`;
  `--float_support=none` is rejected by 25.x (`softlib` is the FPU-less choice).
- Loader (`chipem/firmware.py`): COFF debug sections (`.debug_*`, `$build.attributes`; non-allocated COPY sections)
  have their size in bytes, not words, so every real COFF image failed with "section .debug_info data runs past the
  end of the file". COFF asm labels (C_LABEL, e.g. `wd_disable`) are now symbols; `chipem info` no longer warns that
  the entry 0x33FFF6 (`code_start`) differs from the boot branch target.
- Core: `PREAD AL,*XAR7` did not set N/Z (SPRU430F: "if loc16 = @AX"), nor did the other store-type instructions with
  an @AL/@AH destination. The COFF RTS `.cinit` loop (`PREAD AL` / `SB GET_ADDR,GEQ` / `NEG AL`) then treated record
  sizes wrongly and ran off the end of RAM (`write_unmapped` at 0xE000). EABI's `_c_int00` (copy tables, `memcpy`)
  was not affected, so EABI blinky and uart_chat worked first time.
- Comparing with `dis2000` and TI's assembler found four table errors that compiled code for FPU parts would hit
  (`MOV32` RaH↔ACC/P/XT/XARn, `MOVST0`, `INTR`) and three spellings; see docs/C28X_CORE.md, "Verified against
  cl2000 25.11.1 output".
- The C sources needed no changes.

## How the loader handles these images (chipem/firmware.py)

- EABI (ELF, e_machine 141): `PT_LOAD` segments at `p_paddr` (word addresses; sizes in bytes), `e_entry`, and the
  ELF symbol table (`main`, `_c_int00`, `code_start`, `wd_disable`, globals such as `pongs`).
- COFF2: allocated sections with data at their load address; `.ebss`/`.stack` (BSS), `.reset` (DSECT) and the debug
  COPY sections are skipped; C_EXT/C_STAT/C_LABEL symbols (`_main`, `_c_int00`, `code_start`, `wd_disable`).
- `--rom_model` (used here): `.cinit` is ordinary flash data and `_c_int00` initialises the variables at run time.
  For COFF `--ram_model` the loader applies the `.cinit` records itself; EABI `--ram_model` is not supported.
- `main()` in `uart_chat.c`/`blinky.c` still clears its globals itself: the COFF ABI does not zero `.ebss` (EABI does).
- Not modelled: flash wait states (TI's `InitFlash` is not needed in the emulator and not part of these samples; on
  real hardware at 100 MHz configure them first), the boot ROM (reset starts at the image entry, `code_start`).
