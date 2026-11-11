# CHIP_EM user guide

A task-oriented manual for firmware and hardware engineers: how to load firmware, run simulations, wire chips on a
board, and add a new chip. Everything here was run on this checkout (Python 3.14.8, Fedora, 2026-11-05) and the
output excerpts are real. Lines marked `...` are trimmed.

For how the emulator works inside, read [ARCHITECTURE.md](ARCHITECTURE.md). For what is approximate, read
[LIMITATIONS.md](LIMITATIONS.md). The [README](../README.md) has the full feature reference.

## Contents

1. [Setup and sanity check](#1-setup-and-sanity-check)
2. [Concepts in one page](#2-concepts-in-one-page)
3. [Loading firmware](#3-loading-firmware)
4. [Running simulations](#4-running-simulations)
   (incl. [4.8 Making it faster](#48-making-it-faster-speed-fidelity-and-the-emulation-rate))
5. [Boards: several chips](#5-boards-several-chips)
6. [Parts: switches, LEDs, clocks, logic](#6-parts-switches-leds-clocks-logic)
   (incl. [6.4 Writing your own part in Python](#64-writing-your-own-part-in-python))
7. [Adding a new chip](#7-adding-a-new-chip)
8. [Building firmware](#8-building-firmware)
9. [Troubleshooting](#9-troubleshooting)
10. [Reference](#10-reference)

---

## 1. Setup and sanity check

Work from the checkout root. Always call the tools through the venv; there is no system install.

```sh
python3 -m venv .venv                      # once; Python 3.12 or newer
.venv/bin/pip install -e '.[test]'         # unicorn, capstone, pyelftools, pyyaml + pytest, keystone-engine
.venv/bin/chipem doctor                    # what is installed, and which features that enables
```

The install must be editable (`-e`): `chips/` and the web assets are found relative to the source tree.
Use the `dev` extra (`'.[dev]'`) to add `pyserial` for `--uart-port`. The full list of required and optional
packages and tools (with Fedora, Debian/Ubuntu and Homebrew package names) is in the README's
[Requirements](../README.md#requirements) table. None of the firmware toolchains are needed to run the
prebuilt samples.

`chipem doctor` checks the Python version and required packages, runs the Unicorn instruction-counter calibration
(exact fast-mode timing depends on Unicorn 2.1.x), looks for the optional tools (pyserial, PTYs, gdb, clang /
ld.lld, TI `cl2000`, SDCC under either naming scheme, pdftotext, node, Firefox) and ends with a yes/no feature
list. It exits non-zero only when a required piece is missing; `--json` prints the same as JSON. `chipem
--version` prints the version.

Check that it works:

```
$ .venv/bin/chipem --help
usage: chipem [-h] {info,run,shell,serve} ...
...
    info                static analysis of a firmware image against a chip
                        description
    run                 run firmware and log pin activity
    shell               interactive REPL
    serve               browser UI

$ .venv/bin/chipem run firmware/samples/build/button_mirror.elf --for 3ms --at 1ms:PTC6=0
[     0.000 ms] INFO core clock 20.972 MHz (FEI), bus 20.972 MHz
[     0.000 ms] RESET reset
[     0.002 ms] INFO WDOG disabled
[     0.002 ms] PTC5  owner disabled -> GPIO (in)
[     0.002 ms] PTC6  owner CMP0_IN0 -> GPIO (in), pull none -> up
[     0.002 ms] PTC6  X -> H
...
[     1.000 ms] PTC6  H -> L
[     1.000 ms] PTC5  L -> H

=== summary ===
virtual time : 3 ms (now 3 ms), core clock 20.9715 MHz
...
pins configured by firmware (2):
  pin    num  owner          dir    lvl pull  ext
  PTC5    77  GPIO           out    H   -     -
  PTC6    78  GPIO           in     L   up    L
```

The firmware mirrors PTC6 (input, pull-up) onto PTC5. Pulling PTC6 low at 1 ms drives PTC5 high.

Run the test suite (optional, about 45 s):

```
$ .venv/bin/python -m pytest -q
...
1421 passed in 42.67s
```

Open the browser UI (it blocks; Ctrl-C stops it):

```
$ .venv/bin/chipem serve firmware/samples/build/button_mirror.elf --port 8797
chipem serve — MK22FN1M0VLL12, firmware firmware/samples/build/button_mirror.elf
UI at http://127.0.0.1:8797/   (Ctrl-C to quit)
```

The default port is 8765. Add `--open` to open a browser, `--start` to start running at once.

**What is on this machine** (checked with `which` and `ls`):

| tool | status | used for |
|---|---|---|
| `clang` 22 + `ld.lld` | installed | Cortex-M samples (K22, STM32 bare-metal, STM32 HAL) |
| `llvm-objcopy` | not installed | optional; `firmware/samples/elf2bin.py` makes the `.bin` instead |
| GNU `arm-none-eabi-gcc` | **not installed** | not needed; all ARM builds use clang |
| TI `cl2000` 25.11.1.LTS | installed in `toolchains/ti-cgt-c2000/ti-cgt-c2000_25.11.1.LTS` | C2000 C samples (`firmware/samples_c2000_c`) |
| `tools/c28x_asm.py` | in the repo | C28x assembler samples (`firmware/samples_c2000`) |
| GTKWave / PulseView | **not installed** | VCD viewing (see [4.4](#44-waveforms-vcd)) |

---

## 2. Concepts in one page

**Chip description** (`chips/<part>.yaml`). A hand-written YAML: core type and reset clock, memory map, peripheral
instances with the model that implements each one, IRQ names, and the full package pin table. Register layouts
come from a separate table (`chips/*_regs.yaml`) generated from the vendor SVD. Pass it with `--chip NAME` (looked
up as `chips/NAME.yaml`) or `--chip path/to/file.yaml`. The default is `mk22fn1m0vll12`.

| chip | `--chip` | core | reset clock | pin names |
|---|---|---|---|---|
| NXP Kinetis MK22FN1M0VLL12 | `mk22fn1m0vll12` (default) | Cortex-M4F | 20.97 MHz (FEI) | `PTC5`, `PTA2` |
| NXP Kinetis MK22FX512VLL12 | `mk22fx512vll12` | Cortex-M4F | 20.97 MHz | `PTD2` |
| ST STM32F405ZG | `stm32f405zg` | Cortex-M4F | 16 MHz (HSI) | `PA9`, `PB0` |
| ST STM32F405RG / OG / OE / VG | `stm32f405rg` (LQFP64), `stm32f405og`, `stm32f405oe` (WLCSP90), `stm32f405vg` (LQFP100) | Cortex-M4F | 16 MHz (HSI) | `PA9`, `PB0` |
| ST STM32F407VG / VE / ZG / ZE / IG / IE | `stm32f407vg`, `stm32f407ve` (LQFP100), `stm32f407zg`, `stm32f407ze` (LQFP144), `stm32f407ig`, `stm32f407ie` (LQFP176), `stm32f407igh`, `stm32f407ieh` (UFBGA176) | Cortex-M4F | 16 MHz (HSI) | `PA9`, `PI1` |
| ST STM32F205 / F207 / F215 / F217 (STM32F2) | `stm32f207zg` (base, LQFP144), `stm32f205rg`, `stm32f215rg` (LQFP64), `stm32f205rgy` (WLCSP64+2), `stm32f205vg`, `stm32f207vg`, `stm32f215vg`, `stm32f217vg` (LQFP100), `stm32f205zg`, `stm32f215zg`, `stm32f217zg` (LQFP144), `stm32f207ig`, `stm32f217ig` (LQFP176), `stm32f207igh`, `stm32f217igh` (UFBGA176) | Cortex-M3 (no FPU) | 16 MHz (HSI) | `PA9`, `PB0` |
| ST STM32F103T6 (VFQFPN36, family `stm32f1`) | `stm32f103t6` | Cortex-M3 (no FPU) | 8 MHz (HSI) | `PA9`, `PB0` |
| ST STM32F103 low density (family `stm32f1`) | `stm32f103t4` (VFQFPN36), `stm32f103c4`, `stm32f103c6` (LQFP48), `stm32f103r4`, `stm32f103r6` (LQFP64) | Cortex-M3 (no FPU) | 8 MHz (HSI) | `PA9`, `PB0` |
| ST STM32F103 medium density | `stm32f103vb` (base, LQFP100), `stm32f103v8`, `stm32f103c8` ("Blue Pill"), `stm32f103cb` (LQFP48), `stm32f103r8`, `stm32f103rb` (LQFP64), `stm32f103t8`, `stm32f103tb` (VFQFPN36) | Cortex-M3 (no FPU) | 8 MHz (HSI) | `PC13`, `PA9` |
| ST STM32F103 high density | `stm32f103ze` (base, LQFP144), `stm32f103zc`, `stm32f103vc`, `stm32f103ve` (LQFP100), `stm32f103rc`, `stm32f103re` (LQFP64) | Cortex-M3 (no FPU) | 8 MHz (HSI) | `PA9`, `PG0` |
| ST STM32F105 / F107 (connectivity line) | `stm32f107vc` (base, LQFP100), `stm32f107rc`, `stm32f105rc` (LQFP64), `stm32f105vc` (LQFP100) | Cortex-M3 (no FPU) | 8 MHz (HSI) | `PA9`, `PD8` |
| TI TMS320F28232 | `tms320f28232` | C28x (16-bit words) | 10 MHz | `GPIO34` |

**Firmware formats.** The format is sniffed; `--format` forces it.

| format | extension | notes |
|---|---|---|
| ELF | `.elf` | load address and symbols from the file; symbols show in `dis`, `bt`, pin report |
| raw binary | `.bin` | load address auto-detected from the vector table, or `--load-addr` |
| Intel HEX | `.hex` | addresses from the file |
| Motorola S-record | `.srec` | addresses from the file |
| C28x image | `.c28img` | JSON from `tools/c28x_asm.py` (segments, entry, symbols) |
| TI `.out` | `.out` | both TI COFF and C2000 ELF (EABI), sniffed |

**Load address and VTOR.** A raw `.bin` has no address. CHIP_EM tries 0x0, 0x1000, 0x2000, 0x4000, 0x8000 and
0x10000 and picks the one whose vector table points at the most function starts. For a raw `.bin`, VTOR at reset is
the load address (standing in for a bootloader's jump). For ELF, VTOR defaults to 0 unless `--vtor` is given or
the chip YAML sets `vtor:` (the STM32 YAML sets 0x08000000).

**Virtual time.** Time is instruction count divided by the core clock: one cycle per instruction. It is not wall
time. A run of `--for 2s` may take 0.5 s or 30 s of wall time depending on what the firmware does.

**Pins and levels.** Every pin has an owner (GPIO or a peripheral function), a direction, a drive, a pull and an
external level. Levels are `H`, `L` and `X` (unknown or floating). You drive pins from outside with `--set`/`--at`
(`1/H`, `0/L`, `X`, or `Z` to disconnect). A floating input with no pull reads `X`, and GPIO reads it as 0 with
a one-time warning.

**Nets, boards, parts, links.** A board (`boards/*.yaml`) puts several chips on one virtual PCB. A **net** is a
wire between pins (GPIO levels only). A **UART link** carries bytes between two UARTs. A net that joins a UART TX pin
to a UART RX pin creates an automatic link. **Parts** are board components without firmware: switches, buttons,
clocks, LEDs, probes, counters, gates, flip-flops.

**Event log.** Everything observable is an event with a kind: `pin`, `mmio`, `irq`, `uart`, `reset`, `fault`,
`info`, `warn`, `part`. The CLI printer, the shell, the VCD writer and the web UI all read the same log.

**Fidelity rules that bite.** CHIP_EM follows the silicon where it matters for firmware behaviour:

- **UART input is not queued.** A byte arrives at its stop bit and is dropped if the receiver is not listening
  (receiver disabled, clock gate off, in reset, RX pin not muxed) or still full (overrun). Firmware that boots for a
  while needs its input later: use `--at TIME:UART=TEXT`, not `--uart-in`. Drops are counted in the summary.
- **Clocks start at their reset values.** The K22 starts at 20.97 MHz, not 120 MHz. The STM32 starts at 16 MHz HSI.
  The F28232 starts at 10 MHz and needs about 6.6 ms to relock its PLL. Firmware that assumes a clock it never
  programs needs `--clock`.
- **Clock gates only warn.** Accessing a gated-off peripheral logs one `warn` and proceeds; silicon would fault.
- **Unmodelled peripherals are RAM-like stubs.** A loop that waits for a hardware status bit on a stub spins forever.
- **UART pins do not toggle.** A UART TX pin shows its idle level (H), not the bits. Bytes travel
  only through links. SPI is different: the STM32 SPI (`stm32.spi`) and the Kinetis DSPI (`kinetis.dspi`) drive
  SCK/MOSI(SOUT)/NSS(PCS) as masters or MISO(SOUT) as slaves bit by bit, so chips talk SPI over board nets
  (`boards/stm32_spi_pair.yaml`, `boards/k22_stm32_spi.yaml`); the Kinetis SAI (I2S) and CMT (IR carrier) drive
  their pins too. Timer outputs are different: STM32 TIM, Kinetis FTM, C2000 ePWM / eCAP APWM drive their pins edge by
  edge, and timer inputs (input capture, eCAP, eQEP and FTM quadrature decoders, faults, trip zones) see the edges on
  their pins (section 4.4).

---

## 3. Loading firmware

### 3.1 Inspect before you run: `chipem info`

`chipem info` reads the image without running it. It tells you where it loads, whether it boots, which peripherals
the code references, and a first guess at the pin roles.

```
$ .venv/bin/chipem info firmware/samples/build/button_mirror.elf
Firmware: firmware/samples/build/button_mirror.elf (elf), chip MK22FN1M0VLL12
  segment 0x00000000..0x0000050B (1292 bytes) in FLASH

Vector table @ 0x00000000 (98 entries)
  initial SP 0x20010000   reset PC 0x00000415   bootable
  default handler 0x00000411
  0 non-default handlers:

Peripheral references (literal-pool + code scan, 5 blocks):
  SCS          1  e.g. 0xE000ED88
  WDOG         1  e.g. 0x40052000
  GPIOC        1  e.g. 0x400FF094
  SIM          1  e.g. 0x40048038
  PORTC        1  e.g. 0x4004B014
...
Provisional pin roles (2 pins written directly):
  PTC5   PORTC_PCR5=0x140 MUX=ALT1 (GPIO)
  PTC6   PORTC_PCR6=0x103 MUX=ALT1 (GPIO) pull-up
GPIO direction writes:
  GPIOC_PDDR   @0x000004E4 =0x20 outputs: PTC5
```

What each part means:

| section | what it tells you |
|---|---|
| segment ... in FLASH | where each piece loads, and which memory region it falls in. A segment outside every region is an error (wrong chip or wrong load address) |
| Vector table | initial SP and reset PC; `bootable` means both point into valid memory. Non-default handlers name the interrupts the firmware really uses |
| Peripheral references | which peripheral blocks the code loads addresses of. Blocks that are stubs here will not behave |
| Provisional pin roles | stores of constants to pin-mux/GPIO registers, decoded per family. Pins configured through driver functions with arguments are missed (see [4.6](#46-pin-discovery-for-unknown-firmware)) |

`--no-disasm` skips the code scan (fast, vector table only).

### 3.2 Format by format

**ELF** (the sample builds): nothing to specify. ELF symbols appear in the shell (`dis main`, `bt`) and in the pin
report (`main+0x38`).

**Raw `.bin`**: the load address is detected or given.

```
$ .venv/bin/chipem run firmware/samples/build/blinky.bin --for 1ms --summary-only
...
virtual time : 1 ms (now 1 ms), core clock 20.9715 MHz
```

An STM32 image linked at 0x08000000 is not among the auto-detect candidates, so give the address:

```
$ .venv/bin/chipem run firmware/samples_stm32/build/blinky.bin --chip stm32f405zg --for 1ms
chipem: error: firmware segment byte 0x00000000 is outside mapped memory

$ .venv/bin/chipem run firmware/samples_stm32/build/blinky.bin --chip stm32f405zg --load-addr 0x08000000 --for 2ms --events pin
[     0.002 ms] PB0   dir in -> out
[     0.002 ms] PB0   X -> L
[     0.002 ms] PB7   dir in -> out
...
[     0.315 ms] PB0   H -> L
[     0.315 ms] PB7   L -> H
```

**Intel HEX / S-record**: addresses come from the file. Made here with GNU `objcopy` from a `.bin`:

```
$ objcopy -I binary -O ihex firmware/samples/build/blinky.bin blinky.hex
$ .venv/bin/chipem run blinky.hex --for 1ms --events pin
[     0.002 ms] PTC5  owner disabled -> GPIO (in)
[     0.002 ms] PTC5  dir in -> out
[     0.002 ms] PTC5  X -> L
[     0.241 ms] PTC5  L -> H
...
```

(`-O srec` gives `.srec`; it runs the same way.)

**C28x `.c28img`** (from `tools/c28x_asm.py`):

```
$ .venv/bin/chipem info firmware/samples_c2000/build/timer_irq.c28img --chip tms320f28232
Firmware: firmware/samples_c2000/build/timer_irq.c28img (c28img), chip TMS320F28232 (C28x, 16-bit words)
  segment 0x330000..0x330039 (58 words) in FLASH
  segment 0x33FFF6..0x33FFF7 (2 words) in FLASH

Boot entry 0x33FFF6 -> 0x330000   SP after reset 0x0400   bootable
Symbols: 20   reset target = _c_int00   _c_int00 0x330000
No vector table in the image (PIE vectors are normally copied to 0x0D00 at run time)
...
Provisional pin roles (1 pins written directly):
  GPIO34 GPIO_GPBDIR=0x1 output; GPIO_GPBTOGGLE=0x1 toggle
```

On the C28x every address is a 16-bit word address (also in `--load-addr`, `mem`, `dis` and the event log). There
is no boot ROM: reset jumps to the image entry, or to 0x33FFF6.

**TI `.out`** from cl2000 (EABI ELF or COFF; both are sniffed):

```
$ .venv/bin/chipem run firmware/samples_c2000_c/build/blinky.out --chip tms320f28232 --for 10ms --events pin,info
[     0.000 ms] INFO SYSCLKOUT 10.000 MHz (reset)
[     0.000 ms] INFO watchdog disabled
[     0.028 ms] GPIO34 dir in -> out
...
[     6.582 ms] INFO SYSCLKOUT 100.000 MHz (DIVSEL)
[     7.582 ms] GPIO34 L -> H
[     8.582 ms] GPIO34 H -> L
[     9.582 ms] GPIO34 L -> H
```

`blinky_coff.out` (the COFF build of the same source) behaves the same, 10 µs earlier. `chipem info` on a `.out`
lists the symbols (`Symbols: 57   reset target = wd_disable   _c_int00 0x330231, main 0x330114`).

### 3.3 Options that change how an image loads

| option | use it when |
|---|---|
| `--chip NAME\|PATH` | always, unless the target is the default MK22FN1M0VLL12 |
| `--load-addr ADDR` | a raw `.bin` linked at an address the auto-detection does not try, or detects wrongly |
| `--vtor ADDR` | the vector table is not at the load address (raw `.bin`) or not at 0 (ELF behind a bootloader) |
| `--format bin\|hex\|elf\|srec\|coff\|c28img` | the file extension misleads the sniffer |
| `--clock HZ` | the firmware assumes a clock it never programs (all K22 samples assume 120 MHz). Freezes the clock tree |

`--clock` matters for timing. `systick_irq` programs SysTick for 1 ms at 120 MHz:

```
$ .venv/bin/chipem run firmware/samples/build/systick_irq.elf --clock 120e6 --for 5ms --events pin
[     0.000 ms] PTC5  X -> L
[     1.001 ms] PTC5  L -> H
[     2.001 ms] PTC5  H -> L
[     3.001 ms] PTC5  L -> H
[     4.001 ms] PTC5  H -> L
```

Without `--clock` the chip stays at its reset 20.97 MHz, the period becomes 5.7 ms, and nothing toggles in 5 ms.

### 3.4 Walk-through: an unknown production image (Withings `.bin`)

`s1_off000084_503080.bin` is a 503,080-byte application from a Withings scale, linked at 0x4000 behind a bootloader
that is not in the file. It uses FlexNVM, so it needs the FX512 variant of the K22.

Step 1: inspect it.

```
$ .venv/bin/chipem info s1_off000084_503080.bin --chip mk22fx512vll12
note: auto-detected load address 0x4000 (vector scores 0x0:2, 0x1000:0, 0x2000:4, 0x4000:26, 0x8000:2, 0x10000:2); override with --load-addr
Firmware: s1_off000084_503080.bin (bin), chip MK22FX512VLL12
  segment 0x00004000..0x0007ED27 (503080 bytes) in FLASH

Vector table @ 0x00004000 (98 entries)
  initial SP 0x20010000   reset PC 0x00019A4D   bootable
  default handler 0x00019B01
  33 non-default handlers:
    [  3] exc    HardFault                  0x00019D55
    [ 11] exc    SVCall                     0x00004631
    [ 14] exc    PendSV                     0x00004661
    [ 15] exc    SysTick                    0x0004D121
...
    [ 51] IRQ35  UART2_RX_TX                0x0001D501
...
Peripheral references (literal-pool + code scan, 46 blocks):
  SCS         53  e.g. 0xE000ED04, 0xE000ED00, 0xE000E100, 0xE000E400
  SIM         35  e.g. 0x40047000, 0x40048040, 0x4004803C, 0x40048034
...
Provisional pin roles (1 pins written directly):
  PTE26  PORTE_PCR26=0x600 MUX=ALT6 (RTC_CLKOUT); PORTE_PCR26 configured
```

SVCall + PendSV + SysTick handlers mean an RTOS. UART2 has a handler, so the console is probably on UART2.

Step 2: run it bare.

```
$ .venv/bin/chipem run s1_off000084_503080.bin --chip mk22fx512vll12 --for 2s --events reset,fault
[     0.000 ms] RESET reset
[    21.510 ms] FAULT undefined_instruction at pc=0x0003ae1c → HardFault handler
[   692.914 ms] RESET SYSRESETREQ
[   714.425 ms] FAULT undefined_instruction at pc=0x0003ae1c → HardFault handler
...
UART output:
  UART2 (1656 bytes): "\r\n ------> ENTERING MAINTASK <-------\r\nWDOG_RSTCNT=0\r\n[BAT] Use default cal\r\n[BAT] 0 mv (0 %) ... [Hard fault handler: 0x1fff3918]\r\nR0   = 00000001 ..."
```

It boots, prints a banner, reads 0 mV from the battery ADC, then faults at 21.5 ms and resets itself every 0.69 s.
The fault follows a JEDEC "read ID" (0x9F) on SPI0: with nothing attached MISO reads all ones and the firmware gives
up.

Step 3: give it the hardware it expects.

| option | supplies |
|---|---|
| `--spi-flash SPI0:cs=PTC4` | an SPI NOR flash on SPI0, chip select PTC4 (default Micron M25P16, id 0x202015) |
| `--set PTD2=1` | UART2 RX held idle-high; the firmware only starts its console when PTD2 reads high |
| `--adc ADC0_SE18=40000` | a battery reading (about 0.141 mV per count) |
| `--at '100ms:UART2=help\r\n'` | typing `help` after the console is up (about 35 ms) |

```
$ .venv/bin/chipem run s1_off000084_503080.bin --chip mk22fx512vll12 --for 5s \
      --spi-flash SPI0:cs=PTC4 --set PTD2=1 --adc ADC0_SE18=40000 --at '100ms:UART2=help\r\n' --summary-only
...
stop         : COUNT; pc=0x000045D6 mode=thread
...
  PTD2    95  UART2_RX       in     H   -     H
  PTD3    96  UART2_TX       out    H   -     -

UART output:
  UART2 (1181 bytes): "... [BAT] 5640 mv (77 %) (MIN 4450 MAX 6000) -> State <OK>\r\n ... UART IS CONNECTED***************\r\n\r\nshell>help\r\n\r\naccount_secret\r\nadc\r\nbacklight\r\nb..."

exceptions taken:
  exc 11 SVCall                   1
  exc 14 PendSV                  13
  exc 15 SysTick               4980
  IRQ0 DMA0                       3
  IRQ35 UART2_RX_TX               6
```

No fault, the battery reads 5640 mV, and the firmware's shell answers `help`. Sending the same text with
`--uart-in 'UART2:help\r\n'` from t=0 instead loses it:

```
  UART2 RX dropped 4 byte(s): clock_gated 2, overrun 2 (the receiver was not listening or overran; time input with --at TIME:UART=TEXT)
```

Giving the address explicitly gives the same result: `--load-addr 0x4000 --vtor 0x4000`.

The same device works on an STM32 SPI (SPI1–3, master mode): it answers the frames the firmware clocks out, and
chip select is the GPIO pin named by `cs=`. The `spi_flash` sample reads the JEDEC ID, programs a page and reads it
back:

```sh
.venv/bin/chipem run firmware/samples_stm32/build/spi_flash.elf --chip stm32f405zg --for 20ms --spi-flash SPI1:cs=PA4
#  USART1 (95 bytes): "JEDEC 202015\r\nSR 00\r\nREAD 43 48 49 50 5F 45 4D FF ...\r\nTEXT CHIP_EM\r\ndone\r\n"
```

The STM32 SPI also toggles SCK/MOSI on the pins (visible with `--vcd` or in the web UI), and without the device
MISO floats and reads 0 (`JEDEC 000000`).

On the TMS320F28232 the device attaches to SPI-A (`--spi-flash SPIA:cs=GPIO19`, `boards/c2000_spi_flash.yaml`; with
SPISTEA as chip select, send a multi-byte command through the FIFO so SPISTE stays low, otherwise use a GPIO chip
select) or to McBSP-A in clock-stop (SPI master) mode (`spi: MCBSPA`, `boards/c2000_mcbsp_flash.yaml`, where GPIO24 is
the chip select because MFSXA deasserts after every packet):

```sh
.venv/bin/chipem run --board boards/c2000_mcbsp_flash.yaml --for 15ms
#  SCIA (40 bytes): "MCBSP SPI\r\nID EF4016 DATA MCBS RINT 21\r\n"
```

**SD cards.** `--sd-card HOST:options` inserts an SD memory card (`chipem/devices/sdcard.py`) into the STM32 `SDIO`
or the K22 `SDHC` (an empty `HOST` picks the chip's only SD host). Without it the socket is empty and every command
times out, as on a board without a card. Options: `size=` (default 1G, or the image size; sparse, so large cards cost
nothing), `kind=sdhc|sdsc` (SDHC: block addressing, CSD v2; SDSC: byte addressing, CSD v1, at most 2 GB),
`version=1` (a v1.x SDSC card that ignores CMD8), `image=PATH` (raw card image, e.g. made with `dd` from a real card:
a FAT file system in it is visible to the firmware), `save=PATH` or `writeback=1` (write the contents back when the
session closes), and the timing `init_time=` (ACMD41 power-up, 1 ms), `read_time=` (access time before each block,
50 us), `busy=` (programming time per written block, 250 us), `erase_time=` (1 ms). The bare-metal and ST HAL
samples:

```sh
.venv/bin/chipem run --board boards/stm32_sdcard.yaml --for 30ms           # sdio_card: "... WRITE ok ... DMA ok done"
.venv/bin/chipem run firmware/samples_stm32_hal/build/sd_hal.elf --chip stm32f405zg --for 60ms \
      --sd-card SDIO:size=64M                                                # HAL_SD_Init/WriteBlocks/ReadBlocks/Erase
.venv/bin/chipem run firmware/samples/build/sdhc_card.elf --chip mk22fn1m0vll12 --clock 120e6 --for 30ms \
      --sd-card SDHC:image=card.img,writeback=1                              # K22: CPU, simple DMA and ADMA2
```

In a board file: `devices: [{type: sd_card, chip: A, host: SDIO, size: 64M, image: card.img, save: out.img}]`
(paths relative to the board file). The card checks the protocol like a real one: commands outside their state get
no response (ILLEGAL_COMMAND shows in the next status), an SDHC card stays busy unless the host sent CMD8 and HCS, a
host on a 4-bit bus talking to a card still in 1-bit mode gets CRC errors, and the CK/CMD/DAT pins must be muxed to
the host. The clock pin shows a `wave` summary in the event log instead of every edge (see docs/LIMITATIONS.md).

**External SRAM on the STM32 FSMC.** A board `sram` device with `bus: FSMC` attaches to the first free NOR/SRAM
sub-bank (NE1 first) and appears at 0x60000000 + 64 MB x (n-1) once the firmware sets RCC_AHB3ENR.FSMCEN and
BCRn.MBKEN (accesses before that, or after clearing MBKEN, bus-fault); a memory smaller than 64 MB is mirrored:

```sh
.venv/bin/chipem run --board boards/stm32_fsmc_sram.yaml --for 20ms        # fsmc_sram: "SRAM ok ... FAULT ok done"
```

Two McBSPs (or SPI ports) on different chips exchange words over board nets; give the board a quantum below half a
bit period (`quantum: 100ns` at a few MHz) so that a slave's answer to a clock edge reaches the master in time.

External parallel memory is a board device too: `{type: sram, chip: A, bus: XINTF6, size: 256K, width: 16, file:
init.bin}` puts an SRAM on F28232 XINTF zone 6 (`bus` names a peripheral that can take external memory: XINTF0,
XINTF6, XINTF7; `size` in locations, `width` 8/16/32 bits, `file` preloads a little-endian image). As on the real
board, the firmware must enable the XINTF clock (PCLKCR3.XINTFENCLK) and mux the XINTF pins, or the zone reads the
undriven bus; `boards/c2000_xintf_sram.yaml` runs the `xintf_sram` sample.

### 3.5 The STM32 HAL ELF

`firmware/samples_stm32_hal/build/uart_chat_hal.elf` is built from ST's STM32CubeF4 HAL. It switches to 168 MHz
(HSE 8 MHz + PLL), uses SysTick and `HAL_UART_Receive_IT` on USART1 (PA9/PA10).

```
$ .venv/bin/chipem run firmware/samples_stm32_hal/build/uart_chat_hal.elf --chip stm32f405zg --for 30ms \
      --at '1ms:USART1=HELLO C2000\r\nPING 1\r\n'
[     0.000 ms] INFO core clock 16.000 MHz (SYSCLK HSI 16.000 MHz), APB1 16.000 MHz, APB2 16.000 MHz
...
[     0.054 ms] INFO core clock 168.000 MHz (SYSCLK PLL 168.000 MHz), APB1 42.000 MHz, APB2 84.000 MHz
...
[     0.059 ms] PA9   owner GPIO -> USART1_TX (out)
[     0.059 ms] PA10  owner GPIO -> USART1_RX
[     1.087 ms] USART1 RX: "HELLO C2000"
...
[     4.317 ms] PB1   L -> H
[     4.319 ms] USART1 TX: "PONG 1\r\n"
[     8.056 ms] USART1 TX: "PING 1\r\n"
...
UART output:
  USART1 (74 bytes): "HELLO STM32\r\nHELLO STM32\r\nPONG 1\r\nPING 1\r\nPING 2\r\nPING 3\r\nPING 4\r\nPING 5\r\n"

exceptions taken:
  exc 15 SysTick                 29
  IRQ37 USART1                   21
```

---

## 4. Running simulations

### 4.1 `chipem run`

`chipem run FIRMWARE [options]` runs for a span of virtual time, prints events live, then a summary.

| option | example | effect |
|---|---|---|
| `--for TIME` | `--for 20ms` | virtual time to run (default 100ms; units ns/us/ms/s; bare number = ms) |
| `--set PIN=LEVEL` | `--set PTD2=1` | drive an input from t=0 (`1/0/H/L/X`, `Z` = disconnect) |
| `--at TIME:PIN=LEVEL` | `--at 1ms:PTC6=0` | drive an input at an absolute time |
| `--at TIME:UART=TEXT` | `--at '7ms:SCIA=hi\n'` | start typing TEXT into a receiver at that time, paced at its baud |
| `--uart-in [UART:]TEXT` | `--uart-in 'SCIA:hi\n'` | the same from t=0 (default UART0) |
| `--adc CH=VALUE` | `--adc ADC0_SE18=40000`, `--adc PA0=1.65V`, `--adc CMP0_IN0=1.2V` | analog input: an integer is a raw conversion code, a decimal or V/mV suffix is volts (STM32 ADC, Kinetis CMP inputs: volts only); STM32 also takes pin names (see below) |
| `--spi-flash SPIn:cs=PIN[,jedec=,size=,file=]` | `--spi-flash SPI0:cs=PTC4`, `--spi-flash SPI1:cs=PA4` | attach SPI NOR flash to a Kinetis DSPI or an STM32 SPI (master) |
| `--part ID:TYPE[:K=V,...]` | `--part LED1:led:in=PTC5` | add a board part (section 6) |
| `--vcd FILE` | `--vcd out.vcd` | write pin waveforms |
| `--pin-report` | | after the run, print what the firmware did with every pin |
| `--events KINDS` | `--events pin,uart` | event kinds printed live (default all but `mmio`) |
| `--all-edges` | | print every pin and part edge instead of summarising periodic signals ([4.4](#44-waveforms-vcd)) |
| `--log-mmio` | | log every peripheral register access as `mmio` events (slow) |
| `--precise` | | exact per-block instruction counting (about 10x slower) |
| `-q`, `--quiet`, `--summary-only` | | summary only |
| `--max-slice N` | `--max-slice 200` | max instructions per core slice (default 2000) |
| `--stop-on-fault` | | stop at the first fault instead of entering the HardFault handler |

Pin names accept the GPIO name (`PTC6`), any part of the datasheet name (`LLWU_P10`) or the pin number (`78`).
UART escapes: `\n \r \t \xNN \\`. Quote anything with `\` or spaces for the shell.

ADC values per family. One rule everywhere (`--adc`, shell `adc`, web `POST /api/adc`): an integer (`2048`,
`0x800`) is a raw conversion code, a number with a decimal point or a `V` / `mV` suffix (`1.65`, `1.65V`, `1650mV`) is
volts. Kinetis ADCs (`ADC0_SE14=2048`) and the C2000 ADC (`ADCINA3=2048`) take raw codes only (volts are rejected).
Inputs that have no raw code, the Kinetis comparator inputs, take volts only: `--adc CMP0_IN0=1.2V` (or `1200mV`,
`1.2`); a bare integer there is rejected with a message pointing at the V / mV form. The
STM32 ADC takes volts (VREF+ = 3.3 V) when the value has a decimal point or a unit (`1.65`, `1.65V`, `1650mV`) and a
raw 12-bit code otherwise (`2048`). Its channels can be named by pin (`PA0`), by ADC and channel (`ADC1_IN0`, `IN0`)
or by the pin table name (`ADC123_IN0`). The value belongs to the pin, so `PA0=1.65V` feeds channel 0 of ADC1, ADC2
and ADC3 alike. A channel without a value converts its pin's digital level (H = 3.3 V, L = 0 V). The internal
channels take `ADC1_TEMP=<degC>` (default 25) and `VBAT=<volts>` (default 3.3). Example: `chipem run
firmware/samples_stm32/build/adc_tim_trigger.elf --chip stm32f405zg --for 5ms --adc PA0=1.65V` prints `adc 2048`
every millisecond (TIM3 TRGO triggers ADC1).

Exit status: 0 when the run reaches the end of the window, 3 when it stops on a fault (`--stop-on-fault`) or a
lockup, 1 on a usage or load error. A fault that enters the firmware's HardFault handler still exits 0. On the K22 a
lockup resets the chip (`lockup_reset: true` in its YAML, RCM_SRS1.LOCKUP) unless `--stop-on-fault` is given.

UART input timing, the most common surprise. `sci_echo` enables SCI-A at 6.55 ms, after its PLL relocks:

```
$ .venv/bin/chipem run firmware/samples_c2000/build/sci_echo.c28img --chip tms320f28232 --for 10ms --uart-in 'SCIA:hi\n'
[     0.087 ms] SCIA DROP: "hi\n"
...
UART output:
  SCIA RX dropped 3 byte(s): clock_gated 3 (the receiver was not listening or overran; time input with --at TIME:UART=TEXT)

$ .venv/bin/chipem run firmware/samples_c2000/build/sci_echo.c28img --chip tms320f28232 --for 10ms --at '7ms:SCIA=hi\n'
[     6.554 ms] INFO SCIA: TX on, RX on, 115741 baud 8N1 (LSPCLK 25.000 MHz)
[     7.086 ms] SCIA RX: "h"
[     7.086 ms] SCIA TX: "h"
...
UART output:
  SCIA (3 bytes): "hi\n"
```

Booting a TMS320F2808 over SCI-A, like a real device with its boot-mode pins at 1 1 0 (GPIO34 low): the boot ROM
stand-in (`chips/tms320f2808_bootrom.asm`) waits for the autobaud character `A`, echoes every byte and loads the
SPRU722C stream that `tools/c28x_bootstream.py` builds from a RAM-linked `.c28img` / TI `.out` (`--format at` prints
it, with the leading `A`, as a `--at` value). The firmware argument must have no entry point (otherwise reset skips
the ROM); any flash-only image (such as the `_FLASH_PROBE` of `tests/test_c2000_f280x_boot.py`) does:

```
$ AT=$(.venv/bin/python tools/c28x_bootstream.py ram_prog.c28img --format at)
$ .venv/bin/chipem run probe.c28img --chip tms320f2808 --set GPIO34=0 --at "100us:SCIA=$AT" --for 5ms
...
stop         : COUNT; pc=0x00008008 mode=thread          (running the loaded program in L0)
```

The other loaders take the same stream: SPI-A from a board `spi_flash` device with `addr_bytes: 2` on GPIO19
(pins 1 0 1), I2C-A from an `i2c_eeprom` part at 0x50 with `file: prog.boot` (1 0 0), eCAN-A from frames with ID 0x001,
one word per frame (0 1 1), parallel from GPIO0-15 with the GPIO26/GPIO27 handshake (0 0 0).

`--log-mmio` shows every register access, named from the register table:

```
$ .venv/bin/chipem run firmware/samples/build/blinky.elf --for 20us --log-mmio --events mmio
[     0.000 ms] MMIO R SCS.CPACR = 0x0
[     0.000 ms] MMIO W SCS.CPACR = 0xf00000
[     0.001 ms] MMIO W WDOG.UNLOCK = 0xc520
[     0.001 ms] MMIO W WDOG.UNLOCK = 0xd928
...
[     0.002 ms] MMIO W PORTC.PCR5 = 0x140
[     0.002 ms] MMIO W GPIOC.PDDR = 0x20
```

`--precise` gives the same instruction count, about 10x slower (blinky, 20 ms: 86 M insn/s fast, 10 M insn/s
precise). Fast mode is already exact on this machine (see [Troubleshooting](#9-troubleshooting)), so use
`--precise` only when the calibration warning appears.

### 4.2 Reading the summary

```
=== summary ===
virtual time : 2 s (now 2 s), core clock 23.9862 MHz
cycles       : 47,971,655 (virtual core cycles, incl. time slept in WFI)
instructions : 47,971,655 executed (1 cycle/insn model)
wall time    : 0.517 s, 92.86 M insn/s, 3.87x real time
stop         : COUNT; pc=0x00019D4C mode=handler (HardFault)
last fault   : undefined_instruction pc=0x0003AE1C

pins configured by firmware (16):
  pin    num  owner          dir    lvl pull  ext
  PTD2    95  UART2_RX       in     X   -     -
  PTD3    96  UART2_TX       out    H   -     -
...
UART output:
  UART2 (1656 bytes): "..."

exceptions taken:
  exc 3 HardFault                 3
  exc 15 SysTick                 12
  IRQ0 DMA0                       9

warnings/faults (3):
  undefined_instruction at pc=0x0003ae1c → HardFault handler  (x3)
```

| line | read it as |
|---|---|
| `cycles` vs `instructions` | equal unless the core slept in WFI. The HAL sample above: 5.0 M cycles, 1.4 M instructions |
| `x real time` | above 1 the emulator is faster than silicon |
| `stop` | `COUNT` = the window ended. `mode=handler (HardFault)` = it ended inside a fault handler: something is wrong |
| `pins configured by firmware` | every pin the firmware touched, final owner, direction, level, pull and external drive (`ext`, plus `net` on a board) |
| `UART output` | every byte each UART sent, plus `RX dropped N byte(s): reason` lines |
| `exceptions taken` | counts per exception/IRQ: shows which interrupts really fire |
| `warnings/faults` | faults, warnings and unmodelled accesses, de-duplicated |
| `parts` | final state of every board part (section 6) |

### 4.3 The shell: `chipem shell`

An interactive REPL with readline history (`~/.chipem_history`). `--exec "cmd; cmd; quit"` runs commands without
interaction (handy in scripts). All `run` options (`--clock`, `--set`, `--part`, `--board`, ...) work here too.

| command | does |
|---|---|
| `run [TIME]` | run for TIME more (default 10ms); Ctrl-C stops |
| `step [N]` | execute N instructions and show the next |
| `reset` | reset the chip (external stimulus is kept) |
| `set PIN LEVEL` | drive a pin now; `set PART VALUE` acts on a part |
| `at TIME PIN LEVEL` | schedule a pin change (or part action) at an absolute time |
| `pins [all\|changed\|gpio\|PATTERN]` | pin table |
| `pin NAME [all]` | one pin: ALT functions, mux register, GPIO bits, configuration history |
| `pinreport [CHIP] [nostatic]` | the discovery report so far |
| `regs`, `state` | core registers; time, clock, mode, pending exceptions |
| `mem ADDR [N]` | hex dump through the bus (MMIO reads have side effects) |
| `dis [ADDR\|SYMBOL[+OFF]] [N]` | disassemble (ARM via capstone; C28x via the built-in disassembler) |
| `bt [WORDS]` | call stack: DWARF CFI and exception frames when the ELF has them, else LR + stack scan |
| `break`, `tbreak`, `watch`, `continue`, `next`, `s`, `finish`, `until`, `list`, `info break`, ... | the debugger: see 4.7 |
| `periph [NAME]` | list blocks, or dump one block's registers without read side effects |
| `uart [NAME] TEXT`, `uartout [NAME]` | send to a UART receiver; show what a UART sent |
| `adc CHANNEL VALUE` | set an analog input (same rule as `--adc`: integer = raw code, `1.65V` / `1200mV` = volts) |
| `events [N] [kinds]`, `trace on\|off\|all` | recent events; print events live during `run` (same format and PWM summaries as `chipem run`; `trace all` prints every edge) |
| `parts`, `part ID [KEY=VALUE]`, `part add ID:TYPE:...` | list, set or add board parts |
| `vcd PATH` | start writing a VCD |
| `help [cmd]`, `quit` | |

Example session on `button_mirror` (real output, trimmed):

```
$ .venv/bin/chipem shell firmware/samples/build/button_mirror.elf --clock 120e6 \
    --exec "run 1ms; step 3; set PTC6 0; run 1us; pins changed; at 2ms PTC6 1; run 2ms; events 4 pin; dis main 3; bt; periph GPIOC; quit"
chipem shell — MK22FN1M0VLL12 (LQFP100, cortex-m4+FPU)
firmware button_mirror.elf [elf] load 0x00000000   clock 120 MHz
stopped: COUNT  t=1.000 ms  cycles=120000  pc=0x000004F8 <main+0x4c>
stopped: COUNT  t=1.000 ms  cycles=120003  pc=0x000004EE <main+0x42>
=> 000004EE: 54bf      ite      pl
PTC6: external=L level=L
stopped: COUNT  t=1.001 ms  cycles=120123  pc=0x000004EE <main+0x42>
num  pin      owner/function         dir    lvl pull  ext
 77  PTC5     GPIO                   out    H   -     -
 78  PTC6     GPIO                   in     L   up    L
(2 of 100 pins)
scheduled PTC6=H at 2.000 ms
stopped: COUNT  t=3.001 ms  cycles=360123  pc=0x000004EE <main+0x42>
[     1.000 ms] pin PTC6 level H->L
[     1.000 ms] pin PTC5 level L->H
[     2.000 ms] pin PTC6 level L->H
[     2.000 ms] pin PTC5 level H->L
(4 of 12 events)
main:
   000004AC: 4ff29404  movw     r4, #0xf094
   000004B0: c4f20f04  movt     r4, #0x400f
   000004B4: fff7e8ff  bl       #0x488
#0  pc 0x000004EE <main+0x42>
#1  lr 0x000004B9 <main+0xc>
GPIOC @0x400FF080 model=kinetis.gpio
  +0x000 PDOR           0x00000000
...
  +0x014 PDDR           0x00000020  *
```

Note that `run 2ms` runs 2 ms *more*, while `at 2ms` is an absolute time.

UART and pin details (`uart_echo` at 60 MHz, the clock its baud setting assumes):

```
$ .venv/bin/chipem shell firmware/samples/build/uart_echo.elf --clock 60e6 \
    --exec 'run 1ms; uartout; uart UART0 "abc\r\n"; trace on; run 1ms; trace off; pin PTA2; quit'
--- UART0 (7 bytes) ---
hello

UART0 <- b'abc\r\n' (5 bytes, sent at the UART's baud while the emulation runs)
trace on
[     1.087 ms] UART0 RX: "a"
[     1.087 ms] UART0 TX: "a"
...
pin 36  PTA2  (PTA2, kind=gpio)
  owner=UART0_TX dir=out level=H drive=H pull=- external=none
  functions:
    ALT1  PTA2
    ALT2  UART0_TX  <- active
...
  PORTA_PCR2 = 0x00000200: MUX=ALT2 PE=0 PS=0 (no pull) SRE=0 PFE=0 ODE=0 DSE=0 IRQC=off ISF=0
  firmware activity:
  PTA2: UART0_TX  reads=0 writes=0 level-changes=0 irqs=0
          817 ns  main+0x3c                    mux ALT2 (UART0_TX), pull off
```

On the C28x, `regs` shows the C28x register file and `dis` takes word addresses:

```
$ .venv/bin/chipem shell firmware/samples_c2000/build/timer_irq.c28img --chip tms320f28232 --exec "run 8ms; regs; dis 0x330000 4; quit"
stopped: COUNT  t=8.000 ms  cycles=177350  pc=0x00330030 <idle+0x1>
    pc=0x00330030      sp=0x00000400     acc=0x00000000       p=0x00000000
...
   ier=0x00000001     ifr=0x00000000  dbgier=0x00000000     rpc=0x00000000
_c_int00:
   330000: 3b10      DINT
   330001: 7622      EALLOW
   330002: 761f 01c0 MOVW DP,#0x1C0
   330004: 2829 0068 MOV @0x29,#0x68
```

`state` on the C28x also shows the PIE state (`pieier {1: 64}`, `enpie 1`, `eallow 0`).

### 4.4 Waveforms (VCD)

`--vcd FILE` writes one 1-bit wire per GPIO-capable pin (`x` while floating), 1 ns timescale. On a board it writes
one file per chip (`out.A.vcd`, `out.B.vcd`), and parts get a `parts` scope.

```
$ .venv/bin/chipem run firmware/samples/build/systick_irq.elf --clock 120e6 --for 5ms --vcd systick.vcd -q
$ head -6 systick.vcd
$date chipem $end
$version chipem VCD writer $end
$timescale 1ns $end
$scope module chip $end
$var wire 1 ! PTE0 $end
$var wire 1 " PTE1 $end
```

Viewers: GTKWave (`gtkwave systick.vcd`), PulseView (File → Import → Value Change Dump), or Surfer. **None is
installed on this machine** (`which gtkwave pulseview surfer` finds nothing). Install one with
`sudo dnf install gtkwave` (Fedora) or `sudo apt install gtkwave`, or use the online viewer at
<https://app.surfer-project.org/>. For a quick look without a viewer, the live `pin` events (`--events pin`) carry
the same edges (fast periodic signals summarised, see below; `--all-edges` prints each one).

**Fast signals (PWM): what is printed, stored and traced.** A 20 kHz PWM pin changes 40,000 times per virtual
second, and on a board every net member and part it drives changes with it. Each consumer handles that differently:

| consumer | policy |
|---|---|
| VCD (`--vcd`) | every edge, always |
| live output (`chipem run`, shell `trace on`) | decided per pin or part signal by edge density, not by owner: the first edges print as usual; from the 3rd edge within 10 ms on, edges are gathered per 10 ms window and printed as one line, `waveform: N edges in X ms from T ms, 20.000 kHz, duty 25.0 %, now L`. Windows with the same frequency (±0.1 %) and duty (±0.5 %) are counted into one `waveform unchanged (N more edges, until T ms)` line, printed when the waveform changes, stops or the run ends. Single pulses, slow blinks and `--at` stimulus print one line per edge at once. `--all-edges` (shell: `trace all`) prints every edge |
| stored event log (`Session.events`, shell `events`, web event list, `/api/events`) | every edge of slow pins. A pin with 20 or more edges within 10 ms (≈1 kHz and faster) keeps its first 1000 such edges, then its edges are counted into one `pin` event with `field=wave` per 100 ms (`edges`, `since_ns`, `freq_hz`, `duty`, `new` = level now); when it slows down or stops, the open window is stored and single edges are stored again. Other events are never evicted by PWM (2 s of four 20 kHz signals store a few thousand events, not 320,000) |
| web UI | the event buffer keeps at most 12,000 `pin` events of its 20,000, so reset / info / uart / warn events stay; the pin table's **wave** column shows frequency and duty of periodic pins (hover: edge count), also in the copied / CSV rows |
| `--pin-report` | the `chg` column counts PWM edges (PinBoard edges for peripheral-driven pins); notes show `periodic 20 kHz, duty 25.0 %`; the summary lists "Outputs driven by peripherals" |

```
$ .venv/bin/chipem run firmware/samples_c2000/build/epwm.c28img --chip tms320f28232 --for 2s
...
[     6.572 ms] GPIO0 L -> H
[    16.537 ms] GPIO1 EPWM1B waveform: 400 edges in 9.966 ms from 6.572 ms, 20.000 kHz, duty 69.0 %, now H
[    16.572 ms] GPIO0 EPWM1A waveform: 400 edges in 9.986 ms from 6.587 ms, 20.000 kHz, duty 29.0 %, now H
[  1999.987 ms] GPIO0 waveform unchanged (79337 more edges, until 1999.987 ms), now L
[  1999.987 ms] GPIO1 waveform unchanged (79338 more edges, until 1999.987 ms), now H
```

`boards/pwm_led.yaml` (STM32 `tim_pwm` and K22 `ftm_pwm`, each lighting an LED and watched by a probe) shows the
same for board parts: 20 ms print about 40 lines instead of one per edge.

**PWM outputs and input capture.** Timer channels drive their pins like any other output, so a PWM is visible
everywhere a pin is:

- `chipem run ... --pin-report`: the pin's notes say `periodic 1 kHz, duty 25.0 %`, the summary lists it under
  "Outputs driven by peripherals". The live output prints `waveform:` lines with frequency and duty (above).
- `chipem serve`: the Pins tab's **wave** column shows frequency and duty of every periodic pin.
- `--vcd out.vcd`: every edge, for exact timing in a waveform viewer.
- On a board, a `probe` part on the net measures the frequency (`state.freq_hz`, shown in the board summary).

An input-capture pin (STM32 TIMx_CHn with CCxS = input, Kinetis FTMx_CHn with MSnB:MSnA = 00 and ELSnB:ELSnA != 00, C2000
ECAPx) reads the level of its pin, so feed it like any input:

- `--at TIME:PIN=0|1` for a few edges: `chipem run firmware/samples/build/ftm_capture.elf --clock 60e6 --for 4ms
  --at 1ms:PTA12=1 --at 1.1ms:PTA12=0 --at 1.25ms:PTA12=1` prints `period 15000` (250 us at 60 MHz).
- A board net from a PWM pin to the capture pin, on the same chip (`boards/k22_ftm_capture.yaml`,
  `boards/c2000_ecap_loopback.yaml`) or on another one (`boards/pwm_capture_3way.yaml`: an STM32 PWM measured by a
  K22 FTM and a C2000 eCAP). Edges cross chips up to one board quantum late, so a high time measured on another chip
  is exact only with a small quantum (that board uses `quantum: 1us`); periods are unaffected.
- A `pulse` or `pattern` part on the net (section 6) for a clock or an arbitrary edge sequence.

`--all-edges` prints every edge on the input pin, to compare with what the firmware captured. Captures follow the
silicon's input path where it is modelled: the K22 FTM latches CnV three bus clocks after the edge (synchroniser,
`params.sync_clocks`); the STM32 TIM and C2000 eCAP capture on the cycle the edge arrives (no input filter or GPIO
qualification).

### 4.5 The browser UI: `chipem serve`

```sh
.venv/bin/chipem serve firmware/samples/build/button_mirror.elf --open
.venv/bin/chipem serve --board boards/logic_demo.yaml --open --start
```

`serve` options: `--host` (default 127.0.0.1), `--port` (default 8765; 0 = any free port), `--open`, `--start`,
`--realtime` (pace virtual time to wall time), `--chunk MS` (virtual ms per worker step, default 5), `--view ID`
(board chip selected at start), `--verbose`.

The page:

| area | what you do there |
|---|---|
| Toolbar | Run, Pause, Step N (count field; steps the selected chip), Reset (chips reset, virtual time continues), Restart (rebuild the current setup at t=0, the page reloads), "Run for" a duration (Enter runs); readouts: status badge (with the emulator error inline: click copies it), virtual time, speed, clock, PC (click: CPU registers popover, copyable), instructions, cycles; chip tabs on boards |
| Package / board canvas | the package drawn to scale, pins coloured by level, owner tags (`UART0_TX`); on boards also nets, UART links and CAN / Ethernet / USB buses (bus line with name and live counters; click: its row in Links). When the fit zoom is too small for every pin name (144 / 176-pin packages on a board), only the names that matter are drawn at a readable size: wired pins, the pinned card's and traced pins, then pins with a peripheral function, as long as they do not collide; hover or focus any pin for its name, `chip` / **Z** zooms to the selected chip. Wheel zooms, drag pans, double-click fits. Hover a pin for its data, click for the pin card (copy, locate, drive H / L / open, trace, analog value for ADC pins), **alt-click to drive it H → L → open**. Keyboard: one Tab stop per package, arrows move between pins, Enter opens the card, Esc closes it |
| Pins tab | sortable (click or Enter on a header), filterable pin table with wave (frequency and duty %) and analog columns; "changed only" and "hide power/disabled" toggles; a row click shows the pin on the canvas; copy, CSV, `vcd` (traced pins, else the selected chip); `at` field for timed stimulus (`10ms:PTC6=0`, `+5ms:B.PA0=H`, `1ms:UART0=hi\r\n`, `SW1=H`) with a history |
| Discovery tab | the pin report (section 4.6), with a toggle to compare against the static guess |
| Logic tab | logic-analyser strip of the traced pins (T on a table row, or "trace" in the pin card): wheel zooms, drag pans, double-click fits, cursor with time and levels, follow, copy |
| UART | choose a UART (it follows the first one that prints), read its output ("lines" with optional timestamps, or "raw"; default lines on boards with UART links, else raw, your choice is kept), send text with a line ending; a counter next to the send field shows bytes the receiver lost and received characters with line errors (framing / parity / noise / break; the tooltip has the last one), a break the UART sends is marked `⟨BREAK n bits⟩` in the transcript; copy and download; link monitor on boards |
| Events | live log with per-kind (and per-chip) filters (kinds without events sit behind "+N"), text filter, follow, copy, `.txt`/CSV download; identical consecutive events fold into one row with ×N; received characters with a line error (`FE`, `PE`, `NF`, `BREAK`) are drawn in the error colour, filter them with `/FE|PE|NF|BREAK/` |
| Status | CAN TEC / REC / bus state, USB host state, address and device strings, Ethernet link and frame counters, SD card state and capacity, host serial endpoints (connection, line, bytes, lost, errors, modem lines) and real-time pacing |
| Nets, Links, Parts | on boards (section 5.5 and 6): rename a net inline (double-click), deleting a net, link or part shows a toast with "undo" |

Filters accept plain text (case-insensitive substring) or `/regex/`. Double-click a pane header to collapse it, a
sidebar pane title to maximise / restore it; splitters can be dragged or focused and moved with the arrow keys
(Shift: bigger steps).

Keyboard shortcuts (case-insensitive; ignored while typing in a field or while a dialog is open; `?` shows the list):

| key | action |
|---|---|
| Space | run / pause (on a focused button: presses it) |
| S | step N instructions on the selected chip |
| R | reset (time continues) |
| F, 0 | fit the canvas |
| Z | zoom to the selected chip (again: fit) |
| + − | zoom the canvas |
| B | board setup dialog |
| W | wire mode (boards; click two pins) |
| T | trace the selected pin in the Logic tab |
| 1–9 | select a board chip |
| Del | remove the selected net (undo in the toast) |
| / | focus the filter of the pane under the pointer (over the board: find a pin; Enter / Shift+Enter: next / previous) |
| Esc | close a dialog or popover, cancel pick / wire mode, clear the filter |
| End | jump to the newest line (UART, events) |
| ? | shortcut list |

If the server stops, the badge shows OFFLINE, the run controls are disabled and the page reconnects (and reloads when
the server was restarted).

**(new) Light mode.** The sun/moon button in the header switches between the dark and light themes. The page
starts with your system's preference; once you pick one, it is remembered in the browser (`localStorage`).

On a single chip, "+ part" (or `--part` on the command line) turns the session into a one-chip board so you can wire
parts to it.

Everything the page does goes through a JSON API you can script (`curl`). Real exchange:

```
$ curl -s -X POST localhost:8797/api/run -d '{"ms": 2}'
{"ok": true, "running": true, "until_ns": 2000000.0}
$ curl -s -X POST localhost:8797/api/pin -d '{"name":"PTC6","level":"L"}'
{"ok": true, "name": "PTC6", "chip": "A", "external": "L", "level": "L"}
$ curl -s -X POST localhost:8797/api/part -d '{"type":"led","params":{"active":"L"},"ports":{"in":"PTC5"}}'
{"ok": true, "part": {"id": "LED1", "type": "led", ...}, "wiring": 3}
$ curl -s localhost:8797/api/board/yaml
# board mk22fn1m0vll12: exported by chipem
name: mk22fn1m0vll12
quantum: 10us
chips:
  - {id: A, chip: mk22fn1m0vll12, firmware: /home/.../firmware/samples/build/button_mirror.elf}
nets:
  - {name: LED1_in, pins: [A.PTC5]}
parts:
  - {id: LED1, type: led, active: L, color: red, ports: {in: LED1_in}}
```

The full endpoint list is in the README ("`chipem serve`: browser UI").

### 4.6 Pin discovery for unknown firmware

Two views, use both:

- **Static** (`chipem info`): constant stores to pin registers found in the code. Fast and covers code that never
  runs, but misses pins configured through driver functions (port and pin passed as arguments).
- **Dynamic** (`--pin-report` on `run`; `pinreport` in the shell; the Discovery tab in the UI): every pin
  configuration change seen while running, with the code that made it, read/write counts, level changes and IRQs.
  Covers only code that ran.

```
$ .venv/bin/chipem run firmware/samples/build/button_mirror.elf --for 3ms --at 1ms:PTC6=0 --at 2ms:PTC6=1 --pin-report
...
pin       #  final role                            config@  configured by           reads writes    chg  irqs  static        notes
----------------------------------------------------------------------------------------------------------------------------------
PTC5     77  GPIO out                               2.3 us  main+0x38                   0    13k      2     0  confirmed
PTC6     78  GPIO in (pull-up)                      2.1 us  main+0x2e                 13k      0      0     0  confirmed
```

The `static` column compares the two: `confirmed` (both agree) or `not predicted` (only the run found it). On the
Withings image the static scan found 1 pin and the run found 19:

```
$ .venv/bin/chipem run s1_off000084_503080.bin --chip mk22fx512vll12 --for 3s --spi-flash SPI0:cs=PTC4 --set PTD2=1 --pin-report -q
...
PTA13    43  GPIO in (IRQ logic 0)                 95.1 us  sub_19170+0x36              0      0      0     1  not predicted FLOATING when read
...
    - not predicted statically: 19 (PTA5, PTA12, PTA13, ...) - pins reached through tables/function arguments defeat the static scan
```

Raw images have no symbols, so code is named `sub_XXXX+off` (a guessed function start). `FLOATING when read`
means the firmware read an input with nothing driving it; try `--set PTA13=1` and `--set PTA13=0`. To reach more
code, drive inputs (`--set`/`--at`), type into the console (`--at TIME:UART=...`), set ADC values, and run longer.

---

### 4.7 Debugging: breakpoints, stepping, source, registers, GDB

The debugger works the same in the shell, the browser and over GDB, for ARM and C28x chips. What it can show
depends on the image:

| image | breakpoints by | listing | call stack |
|---|---|---|---|
| ELF with DWARF (`-g`; the samples; TI cl2000 ELF `.out`) | symbol, `file.c:line`, address | source + assembly | DWARF CFI (ARM), RPC + scan (C28x) |
| ELF without DWARF, TI COFF `.out`, `.c28img` | symbol, address | symbols + assembly | stack scan (marked `scan`) |
| raw `.bin` / `.hex` / `.srec` | address | assembly (`sub_XXXX` guesses) | stack scan |

Sources are looked up as recorded in the DWARF (absolute or under the compile directory), then next to the ELF and
in its parent directory, then in every `--source-dir DIR`. `info source` (shell) lists where each file was found.

**Locations** (`break`, `tbreak`, `until`, the web "add" field): `main` (after the prologue with line info),
`*main` / `*0x4ac` (exact address), `main+0x10`, `blinky.c:16`, `16` (current file); on a board prefix the chip:
`B:main`. C28x: word addresses; COFF symbols may carry a leading `_` (both spellings work).

**Watch targets**: `0x20000000[:LEN]`, a data symbol (its size), `PERIPH.REG` (`USART1.SR`, `GPIOC.PDOR`,
`SCIA.SCIRXBUF`). `watch` = write, `rwatch` / `watch -r` = read, `awatch` / `watch -a` = either. A watchpoint stops
after the accessing instruction (PC is the next instruction; the message names the instruction and the value
written). Only CPU accesses are watched: DMA, exception stacking and debugger accesses are not.

**Conditions** (`break LOC if COND`, `condition N COND`): registers (`r0`, `sp`, `xpsr`, `acc`, `xar4`), symbols (a
data symbol stands for its value; `addr(sym)` for its address), `mem8[a]`, `mem16[a]`, `mem32[a]`, arithmetic,
comparisons, `&&`/`and`, `||`/`or`, `!`/`not`. `ignore N COUNT` skips the next COUNT hits (hits still count).

```
$ .venv/bin/chipem shell firmware/samples/build/blinky.elf --clock 120e6
chipem> break blinky.c:16 if r0 == 0x20
Breakpoint 1 at 0x000004FA: main+0x4e (blinky.c:16) if r0 == 0x20
chipem> continue
stopped: DEBUG  t=42.117 us  cycles=5054  pc=0x000004FA <main+0x4e>
breakpoint 1 at main+0x4e (blinky.c:16)
16	            GPIOC->PTOR = 1u << 5;
chipem> list
blinky.c:
      11
      12      volatile uint32_t counter = 0;
      13      for (;;) {
      14          if (++counter >= 1000) {
      15              counter = 0;
=>B   16              GPIOC->PTOR = 1u << 5;
...
chipem> watch -a GPIOC.PDOR
chipem> next
chipem> finish            # error in main: "finish" not meaningful in the outermost frame
```

`next` / `s` step source lines (with no line info at the PC: one instruction); `nexti` runs a call (BL/BLX on ARM,
LCR/LC/FFC on the C28x) and stops after it; `finish` runs until the current function returns to its caller's frame;
`until LOC` runs until LOC. These run the whole board (all chips in lockstep) until the next debugger stop;
`step`/`stepi` moves only the selected chip by one instruction. In `--exec` scripts a run that does not stop within 10
s of wall time is interrupted.

**Variables** (DWARF images): `info args`, `info locals` (the frame selected with `frame N` / `up` / `down`; a stop
selects frame 0 again), `print EXPR` / `p/x EXPR`, and `display[/x] EXPR` (shown after every stop and step;
`display` alone shows them, `info display` lists, `undisplay N`). Expressions: locals and arguments of the selected
frame, then globals and statics (`main::calls` for a function-local static), `a.b`, `a[3]`, `p->x`, `*p`, `&x`,
casts (`*(uint16_t *)0x20000000`), `*0x20000000` (4 bytes; one word on the C28x), `$r0` / `$sp` (registers),
`USART1.SR` / `USART1.SR.RXNE` (peripheral registers and fields, peeked without side effects), `sizeof`, arithmetic;
anything else goes to the condition evaluator above (`mem32[...]`, `addr(sym)`). Values are read without side
effects. A value the debugger cannot determine is shown as `<reason>`, never guessed: `optimized out` (no location,
or no location-list entry at this PC), `register not recovered` (a register value in an outer frame the CFI did not
save; always on the C28x, which has no unwinder for outer frames), or an unsupported DWARF operation. Without DWARF,
`print` still takes symbols, registers and addresses, and globals come from the symbol table (type `?`).

**On a board** a breakpoint or watchpoint on any chip stops every chip. The board runs chips in lockstep slices of
at most one quantum (`quantum:` in the board YAML, default 10 us), and the stopping chip is the one furthest behind,
so the others are ahead of it by at most one quantum: the stop message shows the skew (`[board skew 8625 ns]`).
Lower the quantum for tighter stops.

**Browser** (`chipem serve`, pins tray): the **Debug** tab follows the PC of the selected chip. Toolbar: continue,
pause, into, over, out, stepi, run to cursor; view mixed / asm / source; "follow pc"; a go-to field (any location).
Click a row to place the cursor, click the gutter (or F9) for a breakpoint; in asm view into/over are instruction
steps. A go-to (or a click on a call stack frame / breakpoint address) shows that place until the next stop; with
"follow pc" every stop shows the PC again. Right column, top: tabs **registers** (widths and formats from the server,
`reg_meta` in `/api/chip`: 8-bit 8051 registers print as `0x2A`, the 22-bit C28x PC as `0x3F0012`; changed since the
last stop in amber; double-click a value to edit it while paused), **locals** (arguments and locals of the selected
frame from DWARF, structs / arrays expand; a variable whose location the debugger cannot evaluate shows
`<optimized out>` / `<unavailable: reason>`, never a guess), **watch** (expressions re-evaluated at every stop:
`counter`, `buf[2]`, `p->x`, `*0x20000000`, `USART1.SR.TXE`; kept per chip in the browser) and **globals** (globals
and statics with type, address and size; click one to watch it). Below: call stack (click a frame to show its code
and its locals), breakpoints and watchpoints (enable box, hits, click the condition to
edit, x deletes; the field takes `main`, `file.c:12 if r0==3`, `w USART1.SR`, `r 0x20000000:4`, `a counter`). Memory
view at the bottom (collapsible; a variable's location link opens it there): an address or expression (`sp`, `buffer`, `0x20000000`), or follow a register. Keys: F5
continue, Shift+F5 pause, F10 over, F11 into, Shift+F11 out, F4 run to cursor, F9 breakpoint (F5/F4/F9 only while the
Debug tab is shown). The **Registers** tab lists every peripheral of the selected chip; expand one for its registers,
a register for its bit fields with the enumerated meaning of each value. Values are peeked without side effects:
registers whose read hook may have side effects (a UART data register, a status flag cleared by reading) show the
stored value dimmed with `~`; **read** does a real read while paused. Double-click a register or a field value to
write it (through the bus, so write side effects such as w1c happen). `/?tab=debug` / `/?tab=regs` open the page on
a tab.

#### Attaching gdb, Ghidra, IDA, VS Code, lldb

`--gdb PORT` on `chipem run`, `chipem shell` or `chipem serve` starts a GDB remote serial protocol server on
127.0.0.1:PORT (`--gdb-chip ID` picks the board chip; the server uses the board's debugger, so `continue` runs every
chip). `chipem run --gdb` starts halted at reset and ends when the client kills or detaches; with `shell` and
`serve` the shell / web UI keep working beside the debugger (requests run on the emulation thread). Real-time
pacing (`--realtime`, host serial ports) stays on while gdb continues.

- **gdb** (`gdb`, `gdb-multiarch`, `arm-none-eabi-gdb`):
  ```
  .venv/bin/chipem run firmware/samples/build/blinky.elf --clock 120e6 --gdb 3333
  gdb firmware/samples/build/blinky.elf -ex "target extended-remote :3333"
  (gdb) break main
  (gdb) continue
  (gdb) watch counter          # hardware watchpoint on a stack local: works (gdb computes the address)
  (gdb) monitor reset          # also: monitor halt, monitor pins gpio, monitor periph UART0, any shell command
  ```
  gdb reads the target description (Cortex-M profile, system registers msp/psp/primask/basepri/faultmask/control,
  VFP d0-d15/fpscr with an FPU) and a memory map (flash as ROM, so gdb uses hardware breakpoints there; both kinds
  are the same here). Memory reads never have side effects (MMIO is peeked).
- **Ghidra** (Debugger tool): a "gdb" connector launching `gdb-multiarch` with the program, then in its
  interpreter `target extended-remote :3333`; or the "gdb remote" connector with `localhost:3333`. Ghidra maps the
  module by the ELF; for a raw `.bin` set the image base to the load address first.
- **IDA**: Debugger → "Remote GDB debugger", host 127.0.0.1, port 3333, processor ARM (Thumb), "Debugger options →
  Set specific options → Use target-description XML" on.
- **VS Code** (cortex-debug): `"servertype": "external"`, `"gdbTarget": "localhost:3333"`, `"executable":
  "firmware/samples/build/blinky.elf"`, `"gdbPath": "gdb-multiarch"`; start `chipem run ... --gdb 3333` (or
  `serve`) first.
- **lldb**: `lldb blinky.elf` then `gdb-remote 3333`.

Tested here with GNU gdb (Fedora's multi-arch build) and the protocol tests in tests/test_gdbserver.py; the Ghidra,
IDA, VS Code and lldb settings above are the standard ones for a gdb remote target, not exercised in the test suite.

C28x: gdb has no C28x architecture, so only generic RSP clients work: the server sends a custom target.xml
(`<architecture>c28x</architecture>`, feature `org.chipem.c28x`, every register 32 bits) and uses 16-bit word
addresses for `m`/`M`/`Z` (lengths in bytes, two per word).

### 4.8 Making it faster: speed, fidelity and the emulation rate

**Where the time goes** (profiled with cProfile on the cases below, 2026-11-10):

* **Polling loops.** Most firmware waits by reading a status register in a loop (`while (!(SR & RXNE));`, a PLL lock
  bit, a CPU-timer flag, SysTick COUNTFLAG). Each such read is a Python call through Unicorn's MMIO callback or the
  C28x memory path, about 1–4 µs. A loop of four instructions at 100 MHz takes 40 ns on silicon. In the profiled
  board runs, 50–80 % of the wall time went to these reads.
* **The C28x interpreter.** It is pure Python: about 150 ns per instruction, so 6–7 MIPS against 100 MHz silicon. That
  is about ×0.07 at best and ×0.03 with MMIO.
* **Unicorn.** With the instruction count hook that exact timing needs, a busy loop runs at about 120 MIPS (×1 at
  120 MHz). With the default 2000-instruction slices it reaches 85–100 MIPS.
* **Board lockstep.** Each chip runs at most one quantum (10 µs by default) ahead of the others. A 16 MHz STM32 then
  gets 160-instruction slices, and the per-slice Python overhead (10–20 µs) costs more than the instructions do.
* **The web stream** (pin deltas, event formatting) costs less than 5 % (`chipem serve` with a stream client attached,
  measured on three boards).

**What changed without any fidelity cost** (on by default):

* C28x: the decode cache is a dict with `__missing__` and the run loop is `for count in range(...)`: `pc = cache[pc]()`.
* C28x: MMIO reads and writes are dispatched per address to the block that serves them. Before, every access did a
  region bisect and a page-dispatch search.
* C28x: TBIT, LCR and LRETR have specialised handlers.
* A C28x branch to itself (`SB $,UNC`, the `for(;;);` of an interrupt-driven main loop) spends the rest of the slice
  at once. The next slice then runs to the next event, because nothing can change before it.
* Board: the wall time of each chip is measured, which gives the speed meter and the bottleneck.

**Settings that trade fidelity for speed** (`chipem/perf.py`). Each can be set by CLI option (run/shell/serve), by
shell command (`perf KEY=VALUE`, `quantum`), in the board YAML (where it is a board property), through
`/api/perf`, and in the web UI's Status pane, **performance** block:

| setting | CLI / YAML | what it costs |
|---|---|---|
| preset | `--fast`, `--accurate`, `--preset`; YAML `performance:` | sets the rows below together (accurate: max slice 500, every pin edge logged; fast: max slice 20000, auto quantum, poll skip 100 µs, no mmio events, UI every 200 ms) |
| poll skip | `--poll-skip 100us` | A polling loop is fast-forwarded by whole iterations up to the next event, at most 100 µs at a time. It qualifies when its MMIO reads return the same values, it writes no MMIO register, and all CPU registers are identical in two consecutive iterations. Exact for status bits that models flip in scheduler events, which covers every board in `boards/` (identical event logs). A value a model computes from the time on each read is seen up to the cap late, so a large cap (1 s) breaks such models (USB, RTC, FMC tests fail at 1 s and pass at 100 µs). On ARM the polling load runs once more after a skip. A loop that changes only RAM, with no register trace, would be skipped wrongly. |
| auto quantum | `--auto-quantum`; YAML `auto_quantum: true` | A sleeping chip (WFI/IDLE) or a parked one (self-branch, skipped polling loop) holds the others back only up to its next event, so its timer-driven edges arrive exactly on time. Anything sent to it ends the sender's run. Awake chips keep the quantum bound. Event order between chips may differ from the default, within the quantum. |
| quantum | `--quantum`, YAML `quantum:`; shell `quantum 20us`; live in the UI | the worst-case cross-chip latency of a pin edge or link byte; bit-level SPI/I2C/McBSP between chips needs it below half a bit period |
| max slice | `--max-slice N`; YAML `max_slice:` | Slices end exactly at the next event, also one a model schedules inside an MMIO read or write callback (the core lowers the running slice's instruction limit). Larger slices only delay what is not an event (a timestamp taken with the slice-start clock). 20000 (the `fast` preset) is 10-15 % faster on busy loops and about 30 % on `k22_sdcard`; no measured case gets slower. |
| pin log | `--pin-log all/auto/off` | `off`: no pin events in the log, event list or Discovery tab (VCD and the pin table still see every edge) |
| drop events | `--drop-events mmio,i2s` | the kinds are neither stored nor streamed |
| UI interval | `--ui-interval 200ms` | how often the UI refreshes |

**Measured** with `tools/bench.py` (20 ms warm-up, then 20 ms of virtual time; ×real = virtual / wall time). "before"
is the code before this work (HEAD export, default settings). The machine was shared with other jobs, so expect
about ±10 % noise.

| case | before | default now | fast | skipped (fast) |
|---|---:|---:|---:|---:|
| C2000 asm blinky (pure delay loop, no MMIO) | ×0.060 | ×0.068 | ×0.068 | – |
| C2000 C blinky (TI cl2000; polls) | ×0.026 | ×0.039 | ×7.8 | 99.8 % |
| K22 busy loop 120 MHz | ×0.75 | ×0.80 | ×0.82 | – |
| STM32 busy loop 168 MHz | ×0.51 | ×0.51 | ×0.50 | – |
| STM32 HAL uart_chat | ×0.67 | ×0.60 | ×7.3 | 98.7 % |
| board c2000_stm32_uart | ×0.023 | ×0.033 | ×0.45 | 98 % |
| board k22_c2000_stm32_can | ×0.016 | ×0.018 | ×0.17 | 96 % |
| board pwm_capture_3way (all chips sleep) | ×0.20 | ×0.18 | ×0.51 | – |
| board k22_i2c_eeprom (EEPROM write-cycle wait) | ×0.24 | ×0.23 | ×10.7 | 99.7 % |
| board k22_sdcard | ×1.66 | ×1.68 | ×1.72 | – |
| board stm32_spi_pair (bit-level SPI between chips) | ×0.074 | ×0.070 | ×0.068 | – |

What does not get faster: pure computation on the C28x (×0.07 at 100 MHz is the ceiling of this interpreter), and
busy-but-not-idle firmware such as polling with a timeout counter, or a bit-level bus between chips that needs a
short quantum.

**Emulation rate.** The toolbar has a rate control with presets ×0.01 … ×10 and max (unpaced), or type any factor
and press Enter. The `speed` readout shows achieved/requested and turns amber when the emulation cannot keep up.
Its tooltip lists MIPS per chip and the bottleneck. The same is available as `chipem run --speed 0.1` (or
`--speed max`), shell `speed 0.1`, and `POST /api/realtime {"speed": "0.1"}`. At slow rates a worker chunk is at
most speed × 100 ms of virtual time, so ×0.001 still updates the UI and pause/step react within milliseconds.
`chipem run` prints `speed: requested max, achieved x0.45 · A 45.0 MIPS @ 100 MHz (47 % of wall); B ... ·
bottleneck B` for boards; the shell's `speed` prints the same.

**Things that did not pay:**

* Running board chips on threads: an ARM and a C28x chip in two threads, without any lockstep synchronisation, gave
  ×0.99 (the Unicorn binding and the interpreter both hold the GIL; this Python 3.14 is not free-threaded,
  `Py_GIL_DISABLED=0`). Processes would need IPC on every net edge and link byte.
* `PYTHON_JIT=1` (available in this build): about +10 % on the pure C28x delay loop, nothing on MMIO-heavy runs.
  You can set it in the environment; it is not a chipem option.
* Larger slices: see max slice above.

**Ceiling.** A pure-Python C28x tops out near 7–10 MIPS, so ×0.1 at 100 MHz for firmware that really computes.
Reaching real time would take a native interpreter: a C extension or Cython over the same `isa.yaml` table, about
100× faster per instruction, which would bring the core near ×1. Firmware that mostly waits is already ×5–10
with poll skip.

## 5. Boards: several chips

### 5.1 Writing `boards/<name>.yaml`

Paths are relative to the board file. Every pin and UART name is qualified with a chip id (`A.PTC6`, `B.USART1`).

```yaml
# What it shows, how to run it.
name: my_board
quantum: 10us                     # lockstep: max time one chip runs ahead of another (CLI --quantum overrides)
chips:
  - {id: A, chip: mk22fn1m0vll12, firmware: ../firmware/samples/build/uart_link_sender.elf, clock_hz: 120000000}
  - {id: B, chip: stm32f405zg,    firmware: ../firmware/samples_stm32/build/uart_link_echo.elf}
    # optional per chip: clock_hz (freezes the clock), load_addr, vtor, format
links:                            # byte-level UART links; both directions unless dir: tx
  - {type: uart, from: A.UART0, to: B.USART1}
nets:                             # wires carrying GPIO levels; a pin can be in one net
  - {name: LED_A, pins: [A.PTC5, B.PA0]}
  - {name: BTN, pins: [A.PTC6, B.PB1], pull: up}     # pull: up|down, or drive: H|L for a fixed level
devices:                          # SPI NOR flash on a Kinetis DSPI / STM32 SPI / C2000 SPIA or MCBSPA (same options
  - {type: spi_flash, chip: A, spi: SPI0, cs: PTC4}  # as --spi-flash); parallel SRAM on an external bus:
  # - {type: sram, chip: A, bus: XINTF6, size: 256K, width: 16, file: init.bin}
parts:                            # section 6
  - {id: LED1, type: led, ports: {in: LED_A}}
stimulus: ["5ms:A.PTC6=0", "6ms:A.PTC6=Z"]   # like --set/--at (pins and parts); Z releases
```

This exact file (with absolute firmware paths) ran: `uart A.UART0 <-> B.USART1: 72 bytes A->B, 72 bytes B->A`,
`LED1 ... ON (9 changes, active H)`.

Rules that matter:

- **Nets resolve like a wire.** GPIO outputs, `drive:` and stimulus are strong; they beat the net `pull:`, which
  beats the pins' own pulls. Two outputs at different levels are a conflict: the net goes `X` and one `warn` is logged.
- **Peripheral pins do not carry bytes.** A UART TX pin drives its net at the idle level H. Bytes cross only through
  a `links:` entry or an automatic link.
- **Automatic UART links.** When a net joins a pin owned by a UART TX signal with a pin owned by a UART RX signal,
  the board creates a one-way link TX → RX. It is re-evaluated whenever firmware re-muxes a pin. The other direction
  needs its own net. An explicit `links:` entry for the same direction suppresses the automatic one.
- **Baud check.** A link warns once if the two ends differ by more than 3 % in baud or differ in frame. Bytes are
  still delivered. Make both firmwares agree at the clocks they actually run.
- Lockstep: cross-chip effects arrive up to one quantum late. Smaller is more precise and slower.

### 5.1a CAN links

CAN controllers (`chip.can_blocks()`: Kinetis `CAN0`, F28232 `ECANA`/`ECANB`, STM32 `CAN1`/`CAN2`) join a bus with a `can` link; any
number of nodes:

```yaml
links:
  - {type: can, name: CANBUS, nodes: [A.CAN0, B.ECANA]}   # or {can: [A.CAN0, B.ECANA]}; host: true adds an ACKing PC node
```

- Frames take their real time: the exact stuffed frame length (SOF..EOF) at the transmitter's bit rate, then 3
  intermission bits. Every node must run the same bit rate (within 1.5 %); a mismatched receiver counts a form
  error and does not acknowledge.
- Arbitration: the lowest identifier wins, the others retry after it. A frame no other node acknowledges is an ACK
  error: an error frame, TEC + 8, an automatic retry (error passive at TEC 128, then the retries continue). A single
  chip without a link is in exactly that situation.
- The controller must be on the bus: clock gate on, out of freeze / initialization mode, RX (and TX) pins muxed to
  the CAN function. An STM32 bxCAN does not even leave initialization mode (INAK stays 1) while CANx_RX is not
  muxed, and CAN2 also needs the CAN1 clock (its filter banks live in CAN1, split by FMR.CAN2SB). The pins themselves do not carry bits; nets are not needed.
- Output: each frame is a `can` event (`A: CAN CAN0 tx 100#00014B32` in the live log), the summary of each chip lists
  `CAN frames: N sent`, its controllers (`CAN0: 500 kbit/s, 9 tx, 9 rx, TEC 0 REC 0, active, bus CANBUS`) and the
  first frames; the board summary shows `can CANBUS: A.CAN0, B.ECANA: 18 frames, 0 error frames`.
- From Python: `session.board.can_bus("CANBUS").inject("123#DEADBEEF", t_ns)` sends from a host node; on a single
  chip `chip.peripherals["CAN0"].can_bus.add_host()` gives an acknowledging node (`.send(frame, t)`, `.received`).

`chipem run --board boards/k22_c2000_can.yaml --for 100ms`: the K22 sends `100#<n>4B32` every 10 ms, the F28232
echoes `201#<n>4B32` 6 µs after each frame ends plus its polling time; both print the frames on their UARTs.
`boards/k22_stm32_can.yaml` does the same with an STM32 bxCAN1 (`202#...`, FIFO 0 interrupt), and
`boards/k22_c2000_stm32_can.yaml` puts all three families on one bus (`201#...` wins the arbitration, `202#...`
follows).

### 5.1b Ethernet links, the host node and pcap

An STM32F407/F417 Ethernet MAC (`stm32.eth`, peripheral `Ethernet_MAC`, alias `ETH`) sits behind a PHY that the
board provides. An `eth` link plugs the PHYs of the listed MACs into one segment (a hub: every frame reaches every
other port when its last bit has been sent) and can add a host PC:

```yaml
links:
  - type: eth
    name: LAN
    nodes: [A.ETH]                          # or [{node: A.ETH, phy: {model: dp83848, addr: 1}}, B.ETH]
    phy: {model: lan8742, addr: 0, aneg: 1ms}   # defaults for every node: lan8720 | lan8742 | dp83848 | ksz8081 | generic
    host: {name: HOST, mac: "02:00:00:00:00:01", ip: 192.168.0.1, ping: 192.168.0.10, count: 4, interval: 2ms, start: 5ms}
    inject: [{at: 3ms, hex: "ffffffffffff020000000001080600010800060400010200000000010a0000010000000000000a00000a"}]
    pcap: lan.pcap                          # relative to the board file; Wireshark opens it
    caps: [100FD, 100HD, 10FD, 10HD]        # what the segment advertises in auto-negotiation
```

- **PHY.** Clause 22 registers over MDIO (BMCR/BMSR/PHYID/ANAR/ANLPAR/ANER) plus the vendor status register the
  drivers poll (LAN87xx reg 31 speed indication, DP83848 reg 0x10 PHYSTS, KSZ8081 reg 0x1E). It answers only its own
  MDIO address (other addresses read 0xFFFF). Auto-negotiation takes `aneg` (default 1.5 s, like real 100BASE-TX
  PHYs: shorten it on boards meant for quick runs), forced modes `link` (10 ms); BMSR's link bit latches low, so
  read it twice. A MAC without an `eth` link has no cable: its link stays down.
- **MAC.** The firmware must do what the hardware needs: select MII/RMII in SYSCFG_PMC, enable ETHMAC/TX/RX
  clocks, mux the RMII/MII pins (AF11; DMABMR.SR only completes with the REF_CLK pin muxed), use an MDC divider for
  its HCLK (MACMIIAR.CR), set MACCR.FES/DM to the negotiated mode (frames are lost on a speed mismatch). Frames take
  their wire time at 10 or 100 Mbit/s; the data pins do not toggle, the 50 MHz / 25 MHz PHY clocks and MDC appear as
  `wave` summaries.
- **Host node.** Answers ARP requests for `ip` and ICMP echo requests; with `ping` it ARPs the target and sends
  `count` echo requests every `interval` from `start` (replies in `host.replies` as (seq, round-trip ns)). `inject`
  sends raw frames (hex, without FCS) at given times. From Python: `session.board.eth_segment("LAN").hosts[0]`
  (`send(hex_or_bytes, t_ns)`, `ping(ip, count=, interval=, start=)`, `received`).
- **Output.** One `eth` event per frame on the sender's log (`ETH Ethernet_MAC 02:00:00:00:00:10 > 02:00:00:00:00:01
  ICMP 192.168.0.10 > 192.168.0.1 echo reply id 17224 seq 1 (78 bytes)`); the run summary lists the MAC (`Ethernet
  Ethernet_MAC: RMII, PHY lan8742@0 link 100FD, 5 tx, 4 rx, segment LAN`).
- **CLI.** `--eth-pcap FILE` writes every frame of the board's segment to a pcap file (nanosecond time stamps, FCS
  stripped); `--eth-host ip=..,ping=..,count=..,interval=..,start=..,phy=lan8742,aneg=1ms` adds a host node. On a
  single chip both create a segment for the chip's MAC (its PHY then has a link):
  `chipem run firmware/samples_stm32/build/eth_ping.elf --chip stm32f407zg --for 12ms --eth-host
  ip=192.168.0.1,ping=192.168.0.10,count=2,interval=1ms,start=9ms,phy=lan8742,aneg=1ms --eth-pcap /tmp/lan.pcap`.
- There is no bridge to the real network (TAP): frames stay inside the emulation.

`chipem run --board boards/stm32f407_eth_ping.yaml --for 20ms` runs the bare-metal `eth_ping` sample;
`boards/stm32f407_eth_hal.yaml` (`--for 30ms`) the same with ST's HAL ETH driver.

### 5.1c USB: plugging a chip into an emulated PC

A chip's USB device port (STM32 `OTG_FS` / `OTG_HS`, Kinetis `USB0`) can be plugged into an emulated PC host
(chipem/usbbus.py). The host powers VBUS, waits for the D+ pull-up, debounces 100 ms, resets the port (10 ms),
enumerates the device the way Linux does (GET_DESCRIPTOR, second reset, SET_ADDRESS, device / configuration /
string descriptors, SET_CONFIGURATION) and binds class drivers: CDC-ACM (SET_LINE_CODING, DTR|RTS, bulk IN reads and
bulk OUT writes), HID (interrupt IN polling at bInterval) and MSC bulk-only (INQUIRY, READ CAPACITY). Every packet
takes its real full-speed wire time (CRC and bit stuffing included), frames start every 1 ms with a SOF, and the
device sees reset / suspend (3 ms idle) / resume like on a real cable. The firmware needs a correct 48 MHz USB clock
(STM32: PLL48CLK from PLLQ; K22: SIM_CLKDIV2 USBDIV/USBFRAC), the DP/DM pins in their USB function and (when it
senses VBUS) the VBUS pin: otherwise the host reports "device not recognised" exactly as a PC would.

```yaml
links:
  - {type: usb, name: USB1, device: A.OTG_FS, host: {debounce: 100ms, cdc_send: "hello\r\n", cdc_send_at: 200ms}}
```

Host options (all optional): `debounce`, `reset`, `reset_recovery`, `set_address_recovery`, `resume`,
`resume_recovery`, `ctrl_nak_retry`, `ctrl_timeout` (times), `second_reset`, `enum_retries`, `drivers`, `power`,
`baud` (line coding the CDC driver sets), `cdc_send` + `cdc_send_at` (text typed into the CDC port; without a time,
as soon as the port opens), and `cdc_pty: true | /tmp/link`, `cdc_tcp: 5555`, `cdc_serial: /dev/ttyUSB1` to open the
virtual COM port from PC software. Single chip from the command line:

```sh
chipem run firmware/samples_stm32/build/usb_cdc.elf --chip stm32f405zg --usb-host OTG_FS --for 300ms
chipem run --board boards/stm32_usb_cdc.yaml --usb-cdc pty --realtime    # prints /dev/pts/N: open it with screen/minicom
```

The live output shows one `usb` line per transfer (control requests by name, bulk/interrupt data as text or hex) and
the bus events; the summary lists the enumerated device (`VID:PID "manufacturer" "product" [CDC-ACM, ...]`) and the
CDC-ACM output. In Python, `session.usb_links()[0].host` is the `UsbHost` (`device`, `cdc.write()`, `cdc_rx`,
`hid.reports`, `suspend()`, `resume()`, `unplug()`, `reset_port()`, `control(...)`, `start_task(generator)` for a
scripted sequence). Boards: `stm32_usb_cdc.yaml` (bare-metal driver), `stm32_usb_cdc_hal.yaml` (ST's HAL PCD + USB
Device Library, unmodified), `k22_usb_cdc.yaml` (K22 USB0 with its buffer descriptor table). Not modelled: high
speed (OTG_HS runs on its embedded FS PHY only), hubs, isochronous transfers (docs/LIMITATIONS.md "USB").

### 5.2 Running and watching two chips talk

```
$ .venv/bin/chipem run --board boards/two_k22_uart_pins.yaml --for 20ms --events uart,info
[     0.000 ms] board: INFO net A_TX_B_RX: auto UART link A.UART0 → B.UART0
[     0.000 ms] board: INFO net B_TX_A_RX: auto UART link B.UART0 → A.UART0
[     2.001 ms] A.UART0 TX: "p"
[     2.088 ms] B.UART0 RX: "p"
[     2.088 ms] A.UART0 TX: "i"
[     2.088 ms] B.UART0 TX: "P"
[     2.175 ms] B.UART0 RX: "i"
[     2.175 ms] A.UART0 RX: "P"
...
=== board two_k22_uart_pins: 2 chips (A, B), quantum 10 us, time skew 0 ns ===
nets (4):
  A_TX_B_RX    H  [A.PTA2, B.PTA1]
  B_TX_A_RX    H  [B.PTA2, A.PTA1]
  LED_A_TO_B   H  [A.PTC5, B.PTC7]
  BTN          H  [A.PTC6, B.PTC6] pull=up
links (2):
  uart A.UART0 -> B.UART0: 72 bytes A->B
  uart B.UART0 -> A.UART0: 72 bytes B->A
```

Live events carry a chip prefix, each chip prints its own summary (lines prefixed `A| `, `B| `), then the board
summary. Other front ends on boards:

| front end | board behaviour |
|---|---|
| `run --vcd x.vcd` | one VCD per chip: `x.A.vcd`, `x.B.vcd` |
| `run --pin-report` | one report per chip (`A| Pin report ...`, `B| Pin report ...`) |
| `run --quantum 50us` | override the board quantum |
| `shell --board` | `pins` lists all chips; `run` runs the board; `step` steps the first chip |
| `serve --board` | every chip drawn with nets, links, parts; `--view B` selects chip B |
| `--at 5ms:B.PTC6=0`, `--adc B.ADC0_SE14=100` | qualify names with the chip id; unqualified means the first chip |

### 5.3 The boards in `boards/`

All fourteen were run for this guide (`--for 20ms`, chat boards 40 ms, `logic_demo` 8 ms; 0.9–2.5 s wall each).

| board | chips | demonstrates | result (bytes over the links) |
|---|---|---|---|
| `two_k22_uart.yaml` | K22 + K22 | explicit UART link, a net with a pull-up | 72 / 72 |
| `two_k22_uart_pins.yaml` | K22 + K22 | the same with nets only: automatic links | 72 / 72 |
| `k22_stm32_uart.yaml` | K22 + STM32F405 | two vendors over a link | 72 / 72 |
| `c2000_k22_uart.yaml` | K22 + F28232 | bytes lost before the C28x enables SCI-A | 72 / 50 |
| `k22_c2000_can.yaml` | K22 + F28232 | CAN bus at 500 kbit/s: FlexCAN0 sends every 10 ms, eCAN-A (TI C) echoes | 9 / 9 frames in 100 ms |
| `k22_stm32_can.yaml` | K22 + STM32F405 | the same with bxCAN1 echoing as 0x202 | 4 / 4 frames in 45 ms |
| `stm32f103_stm32f405_uart.yaml` | STM32F103T6 + STM32F405 | F103 (72 MHz) `ping N` every 2 ms, F405 echoes uppercased, TX/RX nets | 4 / 4 lines in 10 ms |
| `stm32f103_stm32f405_can.yaml` | STM32F103T6 + STM32F405 | F103 bxCAN `100#...` every 10 ms, F405 `can_echo` answers `202#...` | 3 / 3 frames in 35 ms |
| `stm32f103_usb_cdc.yaml` | STM32F103T6 + USB host | F1 USB device CDC-ACM enumerates, echoes uppercased | `F103 CDC ready`, `HELLO` |
| `stm32f103c8_stm32f405_uart.yaml` | STM32F103C8 (Blue Pill) + STM32F405 | Blue Pill at 72 MHz pings over USART1, F405 echoes uppercased | echoes in 20 ms |
| `stm32f103ze_spi_i2c.yaml`, `stm32f103ze_sdcard.yaml`, `stm32f103ze_fsmc_sram.yaml` | STM32F103ZE | SPI2 flash + I2C2 EEPROM + SPI3; SDIO SD card with DMA2; FSMC SRAM | `done` |
| `stm32f107vc_eth_ping.yaml` | STM32F107VC + Ethernet host | RMII PHY clocked from MCO = PLL3 50 MHz, answers pings | 4 replies in 20 ms |
| `stm32f105rc_usb_cdc.yaml`, `stm32f105rc_dual_can.yaml` | STM32F105RC | OTG_FS CDC-ACM device; CAN1 <-> CAN2 with CAN2SB = 14 | `F105 CDC ready`; frames both ways |
| `k22_c2000_stm32_can.yaml` | K22 + F28232 + STM32F405 | three CAN families on one bus | 4 / 8 frames in 45 ms |
| `c2000_stm32_uart.yaml` | F28232 + STM32F405 | C28x sender, STM32 echo | 48 / 48 |
| `c2000_stm32_chat.yaml` | F28232 + STM32F405 | two-way "chat" protocol (HELLO/PING/PONG), bare-metal STM32, nets only | 122 / 130 |
| `c2000_stm32_chat_hal.yaml` | F28232 + STM32F405 | the same with ST's HAL on the STM32 at 168 MHz | 130 / 130 |
| `c2000_stm32_chat_c.yaml` | F28232 + STM32F405 | the same with TI-compiled C on the F28232 | 130 / 130 |
| `three_chip_demo.yaml` | K22 + STM32 + F28232 | one TX net fanning out to two RX pins, explicit one-way links, LEDs, a switch, a deliberate baud-mismatch warning | 4 links |
| `logic_demo.yaml` | K22 + K22 | every kind of part: switch, LEDs, clock, inverter, AND, D flip-flop, probes, counter | no links |
| `pwm_led.yaml` | STM32F405 + K22 | timer PWM (TIM3 CH1 on PA6, FTM0 CH0 on PTC1, 1 kHz 25 %) driving an LED and a probe each; the probes read 1000 Hz | no links |
| `k22_ftm_capture.yaml` | K22 | FTM0 PWM (PTC1, 10 kHz) wired to the FTM1 input capture (PTA12) on the same chip | UART0 prints `period 6000` five times |
| `c2000_ecap_loopback.yaml` | F28232 | ePWM1A (GPIO0, 20 kHz 30 %) wired to eCAP1 (GPIO24) | `ecap_period` 5000, `ecap_high` 1500 cycles |
| `c2000_i2c_eeprom.yaml` | F28232 | TI-compiled C: I2C-A page write + ack polling + read of an `i2c_eeprom` part (24C32) | SCI-A `EEPROM: Hello over I2C` |
| `c2000_eqep_encoder.yaml` | F28232 | a `quadrature_encoder` part (100 lines, 1500 rpm, index) on eQEP1 (GPIO20/21/23) | `eqep_pos` = position since the index at each 10 ms unit time out, `eqep_dir` 1, `eqep_cprd` 312 |
| `k22_ftm_qd_encoder.yaml` | K22 | the encoder part (600 rpm) on FTM1_QD_PHA/PHB (PTB0/PTB1), reversed at 30 ms | `qd 40 up`, `qd 80 up`, `qd 119 up`, `qd 79 down`, ... |
| `stm32_i2c_eeprom.yaml` | STM32F405 | I2C1 master + `i2c_eeprom` part | USART1 `EEPROM: Hello I2C!` |
| `k22_stm32_i2c.yaml` | K22 + STM32F405 | K22 I2C0 master, STM32 I2C1 slave (0x42) on open-drain nets, quantum 1 us | K22 `PASS`, STM32 `SLAVE RX 00:48656C…`, `SLAVE TX 10` |
| `k22_stm32_spi.yaml` | K22 + STM32F405 | K22 DSPI0 master (`spi_master`) and STM32 SPI1 slave over nets, quantum 1 us | UART0 `M 1 WORLD`, USART1 `S 1 hello` |
| `k22_i2s_codec.yaml` | K22 | I2S0 master (`i2s_tone`, 8 kHz stereo) and an `i2s_codec` part that records the tone and plays a pattern back | UART0 `I2S 8000 Hz: tx 133 rx 128 ok`, `RX 1234 EDCC 5678 A988 ...` |
| `stm32_i2s_codec.yaml` | STM32F405 | SPI2 in I2S master mode (PLLI2S, MCK on) + I2S2ext receiver against an `i2s_codec` part | USART1 `I2S 8000 Hz: tx 129 rx 128 ok`, `RX 1234 EDCC 5678 A988 ...` |
| `stm32f407_eth_ping.yaml` | STM32F407 | bare-metal Ethernet (RMII, LAN8742A) pinged by the host node (5.1b) | USART1 `PHY 0007:C131`, `link 100FD`, `garp`, `ping 1` .. `ping 4` |
| `stm32f407_eth_hal.yaml` | STM32F407 | the same with ST's HAL ETH driver (`--for 30ms`) | USART1 `ETH HAL`, ..., `ping 4` |
| `stm32f407_dcmi_camera.yaml` | STM32F407 | a `camera` part on the DCMI pins: snapshot, cropped snapshot and continuous frames through DMA2 | USART1 byte sums, `DCMI continuous frames=3 ovr=0`, `done` |
| `k22_i2c_eeprom.yaml` | K22 | I2C0 master and an `i2c_eeprom` part (page writes, ack polling, random read) | UART0 `wrote "Hello over I2C!", read "Hello over I2C!"`, `PASS` |
| `k22_rs232_console.yaml` | K22 | the cable chain to PC software: UART0 → RS-232 transceiver → RJ50 jack → DB9-RJ50 cable → USB-serial adapter → PTY (section 6.2; runs until Ctrl-C) | open the printed `/dev/pts/N`: `CHIP_EM K22 console - type help` |
| `pwm_capture_3way.yaml` | STM32F405 + K22 + F28232 | one STM32 PWM (PA6, 1 kHz 25 %) measured by a probe, a K22 FTM capture and a C2000 eCAP (`--for 12ms`) | probe 1000 Hz, K22 `period 60000`, eCAP 100000 / 25000 cycles |
| `stm32f429zi_disco_lcd.yaml` | STM32F429ZI | LTDC + 8 MB SDRAM frame buffers into a 240x320 `lcd_panel` (`--for 250ms`) | USART1 `LTDC frames 12`, panel 65.3 fps |
| `stm32f411re_f401re_uart.yaml` | STM32F411RE + STM32F401RE | Nucleo-style access-line pair: F411 (100 MHz) `REQ n` on USART1 every 2 ms, F401 (84 MHz) answers `ACK n n²` on USART6 (PA11/PA12 AF8) | 28 / 37 bytes in 9 ms |
| `stm32f411_i2s5_codec.yaml` | STM32F411RE | I2S on SPI5 (I2S5) with the F411's PLLI2SM divider, 32 kHz to an `i2s_codec` part | 128 frames, `I2S5 54002010 tx 256 ok` |
| `stm32f446re_qspi_flash.yaml` | STM32F446RE | NUCLEO-F446RE + W25Q128 on QUADSPI (dual I/O): read ID, erase with status polling, program, indirect / DMA / memory-mapped reads (`--for 70ms`) | USART2 139 bytes, erase 45000 us |
| `stm32f446re_fmpi2c_eeprom.yaml` | STM32F446RE | FMPI2C1 Fm+ (1 MHz, HSI kernel clock) + 24C02: page writes with acknowledge polling, random read (`--for 20ms`) | USART2 `EEPROM: Hello FMPI2C!` |
| `stm32f446re_cec_pair.yaml` | 2x STM32F446RE | HDMI-CEC ping / Get CEC Version / reply / broadcast on PB6, one open-drain net (`--for 400ms`) | A: `...bcast 4F 82 10 00: ack`, `done`; B: `errors 00`, `done` |
| `stm32f446re_cec_hal.yaml` | 2x STM32F446RE | the same, follower B runs HAL_CEC (`--for 400ms`) | same transcript |
| `stm32f446re_spdifrx.yaml` | STM32F446RE + `spdif_source` | SPDIFRX synchronisation, channel status, DMA samples (`--for 30ms`) | `SYNCD WIDTH5 163 Fs ~47929 Hz`, `pairs ok 8/8` |

### 5.4 Pin report on a board

```
$ .venv/bin/chipem run --board boards/c2000_stm32_uart.yaml --for 20ms --pin-report --quantum 50us
...
A| GPIO29    2  SCITXDA (pull-up)                    6.554 ms  pll+0x8                     0      0      0     0  confirmed
A| GPIO28  141  SCIRXDA (pull-up)                    6.554 ms  pll+0x8                     0      0      0     0  confirmed
A| GPIO34  142  GPIO out (pull-up)                   6.554 ms  pll+0xc                     0      6      6     0  confirmed
...
B| PB0      46  GPIO out                               3.4 us  main+0x66                   0     48     48     0  confirmed
B| PA9     101  USART1_TX                              2.3 us  main+0x30                   0      0      0     0  confirmed
B| PA10    102  USART1_RX (pull-up)                    3.1 us  main+0x54                   0      0      0     0  confirmed
```

### 5.5 Wiring at runtime in the UI, and exporting

`chipem serve --board boards/<name>.yaml` (or a single chip plus "+ part"):

- **Wire mode** (button or **W**, **Esc** cancels): click a pin, then another pin on any chip, to make a net. A pin
  already in a net brings the other pin into it; two nets merge. Runtime nets behave like YAML nets, including
  automatic UART links.
- **Nets panel**: every net with members, level and conflict flag. Hover to highlight, click to select (**Del**
  removes), double-click to rename.
- **Links panel**: explicit and automatic (`auto · A_TX`) links with byte counts; × removes; add one by choosing
  `chip.UART` endpoints.
- **Export board YAML** downloads the current board (chips, firmware paths, nets, explicit links, devices, parts).
  The same text is at `GET /api/board/yaml`. Save it under `boards/` and fix the firmware paths to be relative to it
  (the export writes absolute paths for a single-chip session, as shown in 4.5).

---

## 6. Parts: switches, LEDs, clocks, logic

Parts are board components with named ports wired to nets. An output port drives its net like a push-pull pin; an
input port reads the net (H/L/X). Gates are zero-delay. The catalog (from `GET /api/parts/types`):

| type | ports | params (default) | behaviour |
|---|---|---|---|
| `switch` | out | `level` (L): H/L/X/Z | drives its level; keeps it across resets |
| `button` | out | `pressed_level` (L), `released_level` (H), `pressed` (false) | momentary |
| `const` | out | `level` (H) | tie-off |
| `vcc`, `gnd` | out | | H, L |
| `pulse` | out | `period` (1ms), `duty` (0.5), `start` (L), `phase` (0ns) | free-running clock |
| `pattern` | out | `steps` ([[TIME, LEVEL], ...]), `repeat` (false), `period` | level sequence; Z before the first step |
| `quadrature_encoder` | a, b, i | `ppr` (100), `rpm` (60), `hz` (none: quadrature edge rate, overrides rpm), `direction` (cw), `index` (true) | incremental encoder: A and B 90 degrees apart (cw: A leads B), 4 x `ppr` edges per revolution, `i` high for one A period once per revolution (Z with `index: false`); starts half a revolution from the index |
| `led` | in | `active` (H), `color` (red) | lit while the net is at `active`; counts changes |
| `probe` | in | | level, rises, falls, last change, frequency |
| `counter` | in | `edge` (rise): rise/fall/both | edge count |
| `and`, `nand`, `or`, `nor`, `xor`, `xnor` | in0..in{n-1}, out | `inputs` (2) | gates |
| `not`, `buf` | in, out | | inverter, buffer |
| `mux2` | a, b, sel, out | | out = a while sel is L, b while H |
| `dff` | d, clk, rst, q, qn | `edge` (rise), `init` (L), `rst_active` (H) | D flip-flop, async reset |
| `latch` | d, en, q | `init` (L) | transparent while en is H |
| `i2c_eeprom` | sda, scl (open drain) | `address` (0x50), `size` (256), `page` (auto), `twr` (5ms), `fill` (0xFF), `init` (""), `pullup` (true) | 24Cxx serial EEPROM on the I2C bus |
| `i2c_register_device` | sda, scl (open drain) | `address` (0x48), `regs` ({}), `readonly` ([]), `default` (0), `pullup` (true) | generic register-map target (sensor, expander) |
| `i2s_codec` | bclk, ws, sdin (in), sdout (out) | `bits` (16), `format` (i2s / left_justified), `edge` (rising: sample edge), `pattern` ("": comma-separated L,R samples), `wav` ("": 16-bit PCM file), `echo` (false), `record_max` (100000) | I2S slave codec: records every received stereo frame (`frames`, signed), plays back the pattern / WAV / echo on sdout; `set reset_record=true`. Works with the K22 SAI and the STM32 I2S (SPI2/SPI3 + I2SxEXT): `chipem run --board boards/stm32_i2s_codec.yaml --for 15ms`; an external I2S_CKIN clock is the RCC param `i2s_ckin_hz` |
| `camera` | pixclk, hsync, vsync, d0..d13 (out) | `width`, `height`, `bits` (8..14), `format` (gray / raw, or rgb565 / yuv422 = 2 bytes per pixel), `pixclk` (Hz), `hblank` (pixel clocks), `vblank` (lines), `fps` (0 = free running), `pattern` (bars / gradient / counter / checker) or `image` (binary PGM P5 or raw file), `sync` (hardware / embedded), `hsync_blank` / `vsync_blank` (blanking levels, L / H), `start` | parallel (DVP) camera, see 6.3 |

A port is wired to a net name (created if absent) or to `CHIP.PIN` (an anonymous net `<id>_<port>` is created).

**In a board YAML** (`boards/logic_demo.yaml`):

```yaml
parts:
  - {id: SW1, type: switch, level: L, ports: {out: A.PTC6}}
  - {id: LED1, type: led, active: L, color: green, ports: {in: LED_A}}
  - {id: CLK1, type: pulse, period: 1ms, ports: {out: CLK}}
  - {id: INV1, type: not, ports: {in: CLK, out: B.PTC6}}
  - {id: G1, type: and, inputs: 2, ports: {in0: LED_A, in1: CLK, out: GATED}}
  - {id: P1, type: probe, ports: {in: GATED}}
stimulus: ["2ms:SW1=H", "4ms:SW1=L"]
```

**On one chip** with `--part ID:TYPE[:KEY=VALUE,...]`. Keys that name a port wire it; the others are parameters:

```
$ .venv/bin/chipem run firmware/samples/build/button_mirror.elf --clock 120e6 --for 3ms \
      --part SW1:switch:out=PTC6,level=H --part LED1:led:in=PTC5,active=L --at 1ms:SW1=L --at 2ms:SW1=H
...
[     0.000 ms] LED1 ON
[     1.000 ms] SW1.out L
[     1.000 ms] PTC6  H -> L
[     1.000 ms] PTC5  L -> H
[     1.000 ms] LED1 off
[     2.000 ms] SW1.out H
...
  PTC5    77  GPIO           out    L   -     L  net LED1_in
  PTC6    78  GPIO           in     H   up    H  net SW1_out
...
parts (2):
  SW1      switch   out=SW1_out                        level=H
  LED1     led      in=LED1_in                         ON  (2 changes, active L)
```

**Stimulus on parts** works like pins and is looked up before pin names: `--set SW1=H`, `--at 5ms:SW1=L`,
`--at 2ms:BTN1=pressed`, `--at 3ms:CLK1.period=500us` (`ID.KEY=VALUE` sets any parameter). In the shell:
`part SW1 level=L`, `part LED1` (show state), `part add P1:probe:in=PTC5`, `at 3ms SW1 H`, `parts`.

**Typical patterns:**

| goal | part |
|---|---|
| a button on an input pin | `--part SW1:switch:out=PTC6,level=H` then `--at 1ms:SW1=L`; or `button` with `--at 2ms:BTN1=pressed` |
| see an output as an LED | `--part LED1:led:in=PTC5,active=L` (active-low LED to VCC) |
| an external clock into a timer/IRQ pin | `--part CLK:pulse:out=PTC6,period=2ms` |
| a gate between two chips | board YAML: `{id: G1, type: and, ports: {in0: A.PTC5, in1: CLK, out: B.PTC7}}` |
| measure a frequency | `{id: P1, type: probe, ports: {in: B_LED}}`; the summary prints `level L, 6 rises / 6 falls, 1 kHz` |
| count pulses | `{id: CNT1, type: counter, ports: {in: B_LED}}` |
| a motor shaft encoder for eQEP / FTM / TIM quadrature decoding | `{id: ENC, type: quadrature_encoder, ppr: 100, rpm: 1500, ports: {a: A.GPIO20, b: A.GPIO21, i: A.GPIO23}}`, then `--at 30ms:ENC=ccw`, `--at 40ms:ENC=3000rpm`, `--at 50ms:ENC=stop` |

The `logic_demo` board summary shows each kind:

```
$ .venv/bin/chipem run --board boards/logic_demo.yaml --for 8ms -q
...
parts (10):
  SW1      switch   out=SW1_out                        level=L
  LED1     led      in=LED_A                           off (3 changes, active L)
  CLK1     pulse    out=CLK                            level=L period_ns=1e+06 freq_hz=1000 duty=0.5 edges=16
  INV1     not      in=CLK out=INV1_out                out=H in=L
  G1       and      in0=LED_A in1=CLK out=GATED        out=L inputs=in0=H,in1=L
  P1       probe    in=GATED                           level L, 6 rises / 6 falls, 1 kHz, last change 8 ms
  FF1      dff      d=LED_A clk=CLK rst=- q=LATCHED qn=- q=H clocks=8
...
```

**Quadrature encoder.** `quadrature_encoder` makes one edge every `1 / (4 * ppr * rpm / 60)` s (or `1 / hz`),
on absolute board times (no drift), alternating A and B: clockwise A rises, B rises, A falls, B falls. Its stimulus
takes `cw` / `ccw`, `stop`, `<n>rpm` (or a bare number), `<n>hz` / `<n>khz` (edge rate) and `restart` (back to the
start position), several at once (`--at 20ms:ENC=ccw,300rpm`); `ENC.rpm=...` and the web part dialog set single
values. A change keeps the position and restarts the edge timer at that instant (the next edge one period later).
Its edges are not logged as `part` events (they would flood the log); the pins' own events, the VCD and the
`position` / `revolutions` / `edges` state show them. `Session.reset` restarts it at its start position.
Boards: `boards/c2000_eqep_encoder.yaml` (F28232 eQEP1 with index) and `boards/k22_ftm_qd_encoder.yaml` (K22 FTM1
quadrature decoder):

```
$ .venv/bin/chipem run --board boards/c2000_eqep_encoder.yaml --for 50ms --pin-report
...
parts (1):
  ENC      quadrature_encoder a=ENC_a b=ENC_b i=ENC_i            position=700 revolutions=1.75 direction=cw rpm=1500 edge_hz=10000 edges=500 a=L b=L i=L
...
GPIO20   64  EQEP1A (pull-up)                     6.554 ms  pll+0x8                     0      0      0     0  confirmed     periodic 2.5 kHz, duty 50.0 %
```

### 6.1 I2C devices and open-drain nets

I2C buses are real wires: each I2C controller (K22 `kinetis.i2c`, STM32 `stm32.i2c`, C2000 `ti_c2000.i2c`) pulls its
SDA/SCL pins low or releases them at the configured bit rate, and the I2C parts decode the net edges and answer by
pulling SDA low. A VCD of the two nets shows the whole transaction (START, address, ACK, data, STOP).

- **Pull-ups.** A released line must be pulled up. Either declare the nets `open_drain: true` (wired-AND, pulled up;
  an H driver on it counts as released) or `pull: up`, or let the device breakout do it: the I2C parts have
  `pullup: true` by default (a pull resistor on their ports). Without any, the line floats (X), which the
  controllers read as 1, so every transfer is NACKed.
- **Pins in open-drain mode.** K22: set `PORTx_PCRn.ODE` on the SDA/SCL pins (without it the enabled module drives
  the released line H and a slave's ACK becomes a drive conflict, with one `warn`). STM32: alternate function with
  `OTYPER = 1` (`GPIO_MODE_AF_OD` in the HAL). C2000: GPIO32/33 muxed to SDAA/SCLA.
- **Quantum.** A bus between two chips needs `quantum: 1us` (below the SCL low time) so the slave's ACK is seen in
  time; one chip plus parts works with any quantum.

`i2c_eeprom` behaves like an AT24Cxx: a write transfer sends the word address (1 byte up to 2 KiB, where the upper
address bits are the low device-address bits, so a 24C16 answers 0x50-0x57; 2 bytes above), then data into the page
buffer (rolling over within the page); the STOP starts the `twr` write cycle, during which the device does not
acknowledge its address (poll it until it does). A write of the word address only, followed by a repeated START and
a read, reads from that address; reads are sequential and roll over at the end. `init: "text"` or `init:
"hex:48656C6C6F"` presets the contents. `i2c_register_device` uses the common register-pointer convention: the first
written byte selects the register, the next bytes write from there, reads return registers from the pointer, both
auto-incrementing; `readonly` registers acknowledge writes but keep their value; `--at 5ms:DEV1.reg=...` is not
supported, use the shell / web `set(reg=0x10, value=0x5A)`.

```yaml
nets:
  - {name: SCL, pins: [A.PTB2], open_drain: true}
  - {name: SDA, pins: [A.PTB3], open_drain: true}
parts:
  - {id: EE1, type: i2c_eeprom, address: 0x50, size: 256, twr: 5ms, ports: {sda: SDA, scl: SCL}}
  - {id: TMP, type: i2c_register_device, address: 0x48, regs: {0x00: 0x19, 0x01: 0x80}, readonly: [0x00],
     ports: {sda: SDA, scl: SCL}}
```

```
$ .venv/bin/chipem run --board boards/k22_i2c_eeprom.yaml --for 30ms -q
...
  UART0 (108 bytes): "K22 I2C0 -> EEPROM @0x50\r\n0x51: NACK\r\nwrote \"Hello over I2C!\", read \"Hello over I2C!\" (84 ack polls)\r\nPASS\r\n"
...
  EE1      i2c_eeprom sda=SDA scl=SCL                    address=0x50 size=256 pointer=31 busy=False writes=2 reads=15 transfers=90 naks=84
```

Parts are digital only: no analog values, no propagation delay. UART bytes travel only through the serial cable
parts of 6.2.

### 6.2 Connecting PC software to a simulated UART (host serial)

PC software that talks to a real device over a COM port can talk to the emulated MCU instead. chipem opens a port on
the machine it runs on and bridges it to a UART. Three transports:

| option | transport | PC software opens |
|---|---|---|
| `--uart-pty UART0[=/tmp/ttyK22]` | pseudo-terminal (Linux, macOS) | the printed `/dev/pts/N`, or the symlink you named |
| `--uart-tcp UART0=5555` (`=0.0.0.0:5555` for other hosts) | raw TCP listener, one client at a time | `localhost:5555` (nc, PuTTY "Raw", socat, a TCP-aware tool) |
| `--uart-port UART0=COM5[:115200[:8N1]]` | an existing serial port, via pyserial (`pip install pyserial`, extra `chipem[serial]`) | the other end of a null-modem pair |

```
$ .venv/bin/chipem run firmware/samples/build/uart_console.elf --clock 120e6 --uart-pty UART0=/tmp/ttyK22
host serial: UART0 <-> pty /dev/pts/7 (link /tmp/ttyK22)
real-time: on (speed x1); Ctrl-C stops
...
$ picocom -b 115200 /tmp/ttyK22        # second terminal; or screen /tmp/ttyK22 115200, minicom -D, your tool
CHIP_EM K22 console - type help
> help
commands: help, led on, led off, dtr, uptime, echo TEXT
```

- **Real time.** With a host port attached, `run`, `shell` and `serve` lock virtual time to the wall clock
  (`--no-realtime` turns it off, `--speed 0.5` runs at half speed, `--realtime` forces it without a port). `run`
  then runs until Ctrl-C unless you give `--for`. An idle firmware (WFI) costs no CPU and still answers within a few
  ms. A firmware that busy-polls at 120 MHz cannot keep up (the emulator runs it at 0.05-0.2x): one `warn` says so,
  the summary and the web UI show the lag, and host bytes are delivered late but never lost.
- **The wire rules apply.** Host bytes go out one character time apart at the host's speed and reach the UART at
  their stop bit; they are dropped when the receiver is not listening (RX off, clock gated, pin not muxed) or
  overruns, as on silicon. If the PC program sets another baud rate than the UART uses (a PTY reports it), both
  directions are dropped as framing errors and one `warn` names both settings. Bytes the chip sends while no program
  has the port open are lost.
- **Linux, null-modem pair:** `socat -d -d pty,raw,echo=0,link=/tmp/ttyA pty,raw,echo=0,link=/tmp/ttyB`, then
  `--uart-port UART0=/tmp/ttyA:115200` and point the PC software at `/tmp/ttyB` (useful when the software insists on
  a real tty with modem lines; `--uart-pty` is simpler otherwise).
- **Windows:** install com0com, create a pair (e.g. COM20 <-> COM21), run
  `chipem run ... --uart-port UART0=COM20:115200`, open COM21 in the PC software. Without com0com use
  `--uart-tcp UART0=5555` with a program that can open a TCP port (or a TCP-to-COM redirector). PTYs do not exist
  on Windows.
- **Board YAML:** `host_serial: [{uart: A.UART0, pty: /tmp/ttyK22}]` (or `tcp: 5555`, or `port: COM20, baud: 115200,
  format: 8N1`), optional `realtime: true|false|SPEED`.
- **Shell / web:** shell `host` lists the endpoints and the pacing, `realtime on|off [SPEED]` switches it. In the web
  UI the UART pane has a "host serial" box (transport, path, connected, bytes in/out, lost, real-time lag) with a real
  time checkbox and speed factor; `GET /api/hostserial`, `POST /api/realtime {on, speed}`. Press Run (or start with
  `serve --start`): the emulation only runs while the UI runs it.

A direct bridge is a TTL wire on the UART's TX/RX pins (like a USB-TTL cable). To model the real cable chain, use
parts:

**The chain as parts.** `boards/k22_rs232_console.yaml` replicates a typical service port: the K22's UART0 → an RS-232
transceiver → an RJ50 jack on the device → a DB9-to-RJ50 cable → a USB-serial adapter on the PC → a PTY.

```
$ .venv/bin/chipem run --board boards/k22_rs232_console.yaml
host serial: PC <-> pty /dev/pts/7
real-time: on (speed x1); Ctrl-C stops
```

Open the printed `/dev/pts/N` in your PC tool at 115200 baud (add `port: pty:/tmp/ttyK22` to the PC part for a
fixed name). Type `help`, `led on` (LED1 lights in the web UI), `dtr` (the firmware reads the PC's DTR through the
transceiver: PTY host programs raise DTR when they open the port).

| part | ports | what it does |
|---|---|---|
| `usb_serial_adapter` | `dcd rxd txd dtr gnd dsr rts cts ri` (DE-9 male DTE pins 1-9) | the PC end: `port: pty \| pty:LINK \| tcp:PORT \| serial:DEV[:BAUD] \| memory \| none`; `baud`, `format` (when the transport does not tell); `levels: rs232` (mark = -6 V) or `ttl` (FTDI-cable style, active-low RTS#/DTR#). Without a port, `--at 5ms:PC=help\r` types into it. |
| `rs232_transceiver` | `t1in t1out ... r1in r1out ... gnd` (+ `shdn_n`, `en_n`) | MAX3232-like: `drivers`, `receivers` (2+2); inverting; needs `gnd` on a grounded net; `shdn: true` / `en: true` add the MAX3222 control pins |
| `db9`, `rj50`, `connector` | `1`..`N` | board connectors (`gender` for D-sub; `connector` takes `kind`, `pins`); `names` labels pins |
| `cable` | `a1..aN`, `b1..bM` | ends `a`/`b` (`db9m`, `db9f`, `db25m/f`, `rj11`, `rj45`, `rj50`, `pins:N`), `plug_a`/`plug_b` = the connector part (or adapter) each end is plugged into, `map: {A_PIN: B_PIN or [B_PINS]}` or `preset: straight \| null_modem \| null_modem_3wire` (DE-9 to DE-9) |

```yaml
  - {id: W1, type: cable, a: rj50, b: db9f, plug_a: J1, plug_b: PC,
     map: {3: 2, 4: 3, 2: 5, 5: 4, 6: 8}}   # device RJ50 pin: PC DB9 pin (TXD->RXD, RXD<-TXD, GND, DTR, CTS)
```

DB9-to-RJ50 pinouts differ between vendors (there is no standard), so the map is always explicit: copy it from the
device's documentation or measure the cable. Genders must mate (a `db9m` end onto the adapter's male DB9 is an error).

**It fails like the bench.** Bytes reach a receiver only through a sound path: TXD to RXD through the cable, the
right number of inversions, compatible levels and a common ground. A cable with TXD/RXD wrong reaches no receiver
(TXD meets TXD: nothing arrives); a TTL UART wired straight to the RS-232 port idles at "space" and the PC sees a
break and no characters; the adapter's ±6 V on a 3.3 V pin is reported as over-voltage; without the GND wire the two
ends share no ground; an unplugged cable (`--at 2s:W1.plug_b=none`, or `set(plug_b=...)`) disconnects. Each case is
a `uart` drop event with the reason (`polarity`, `over_voltage`, `no_ground`, `framing`, `stage_off`) plus one
`warn` with the path. The nets show the idle levels: an RS-232 line at mark is L (H means a positive voltage); part
states show volts (`U1 t1out=-6.0V`, `PC txd=-6.0V`). Modem lines are plain nets: the PC's DTR/RTS drive pins 4/7,
CTS/DSR/DCD/RI are read from pins 8/6/1/9 and passed to the host where the transport has them (pyserial; a PTY or
TCP has none, so DTR/RTS read asserted while a program has the port open).


---

### 6.3 Camera (parallel DVP sensor)

The `camera` part drives a parallel camera bus: ports `pixclk`, `hsync`, `vsync`, `d0`..`d<bits-1>`. Wire each port
to the chip pins muxed to the camera interface (STM32F407: DCMI_PIXCLK/HSYNC/VSYNC/Dn, AF13). HSYNC and VSYNC toggle
on the nets (line and frame timing from `pixclk`, `width`, `hblank`, `height`, `vblank`, `fps`); the pixel data
reach the camera interface line by line (a vendor-free hook, `dvp_line`, on the peripherals of the chips on the
pixclk net), and PIXCLK is summarised as a `wave` event instead of toggling. With `sync: embedded` the lines carry
ITU-656 style codes instead of HSYNC/VSYNC. `image:` takes a binary PGM (P5) or a raw file (relative to the current
directory, then to the board file); otherwise a `pattern` is generated. `--set CAM=checker` changes the pattern at
run time. One `part` event per frame. Example: `chipem run --board boards/stm32f407_dcmi_camera.yaml --for 50ms`.

### 6.3a LCD-TFT panel (STM32F429/F439 LTDC)

Add an `lcd_panel` part to a board and connect its `clk`, `hsync`, `vsync`, `de` ports (and the data lines `r0..r7`,
`g0..g7`, `b0..b7` the panel has; an 18-bit panel uses r2..r7, g2..g7, b2..b7 and `bits: 6`) to the chip pins muxed to
LCD_CLK, LCD_HSYNC, ... Params `width`, `height`, `hsync`/`vsync` (active level L/H), `de` (L/H/ignore), `pclk_edge`.
The part keeps the last frame: `state()` shows frames, fps, CRC-32 and the centre pixel (also in `chipem run` part
summaries and `/api/board`); Python access: `session.board.parts["LCD"].frame_rgb()`, `.pixel(x, y)`, `.frame_ppm()`.
Example: `chipem run --board boards/stm32f429zi_disco_lcd.yaml --for 250ms`.

### 6.3b Quad-SPI NOR flash (STM32F446 QUADSPI)

```sh
firmware/samples_stm32f446/build.sh; firmware/samples_stm32_hal_f446/build.sh
.venv/bin/chipem run --board boards/stm32f446re_qspi_flash.yaml --for 70ms        # NUCLEO-F446RE + W25Q128 on QUADSPI
.venv/bin/chipem run firmware/samples_stm32f446/build/qspi_quad.elf --chip stm32f446ze \
    --spi-flash "QUADSPI:jedec=0xEF4018,size=16M,t_se_us=45000" --for 70ms
```
A quad-SPI flash is attached with the existing `spi_flash` device type (board `devices: [{type: spi_flash, chip: A,
spi: QUADSPI, jedec: 0xEF4018, size: 16M, qe: sr2, t_se_us: ..., bank: 1}]` or `--spi-flash QUADSPI:...`): when the
named block is a quad-SPI controller, a `chipem.devices.qspi_flash.QspiNorFlash` is built instead of the plain SPI
flash. In memory-mapped mode the flash array is mapped at 0x90000000 (code can execute from it) when the read command
is valid and TCEN = 0.
With two flashes (`bank: 1` and `bank: 2`) and CR.DFM = 1 the QUADSPI runs in dual-flash mode (even bytes in flash 1,
odd bytes in flash 2, RM0390 12.3.4); the memory-mapped window then goes through the per-access path.

### 6.3c STM32F446 FMPI2C, HDMI-CEC and SPDIFRX

```sh
firmware/samples_stm32f446/build.sh; firmware/samples_stm32_hal_f446/build.sh
.venv/bin/chipem run --board boards/stm32f446re_fmpi2c_eeprom.yaml --for 20ms   # FMPI2C1 (I2C v2) + 24C02 EEPROM
.venv/bin/chipem run --board boards/stm32f446re_cec_pair.yaml --for 400ms       # two F446RE talking HDMI-CEC on PB6
.venv/bin/chipem run --board boards/stm32f446re_spdifrx.yaml --for 30ms         # SPDIFRX fed by an spdif_source part
```
FMPI2C1 is a pin-level I2C controller/target like `stm32.i2c` (any `i2c_eeprom` / `i2c_register_device` part or
another chip on the SDA/SCL nets; boards with I2C use `quantum: 1us`). The CEC line is one open-drain net with a
pull-up between the CEC pins of the chips (PA15 AF4 or PB6 AF3 on the RE); use a board quantum of 10 us or less. The
`spdif_source` part (`rate`, `signal`, channel status, parity / coding error injection) feeds IEC 60958 sub-frames to
the SPDIFRX input it is wired to; the line itself is shown as a `wave` summary, not toggled edge by edge.

### 6.4 Writing your own part in Python

When no built-in part fits (a sensor, a modem, the device a PC tool talks to, a test bench), write the part in
Python. A `script` part runs your class inside the emulator, on the emulation thread, at the virtual time of every
event: no IPC, no second process, the same timing as the built-in parts.

> **Security.** A script part is ordinary Python running with your user's privileges: it can read and write your
> files, open sockets, start programs. Loading a board that contains a script part is the same as running a program
> from wherever the board came from. There is no sandbox. `chipem run/shell/serve --board X.yaml` loads the scripts
> of the board you name on the command line; the web UI asks before it opens a board with script parts or adds one
> (it shows the script paths), refuses script requests from other web sites (an `Origin` other than the page) and
> from host names other than an IP address or `localhost`, and the server binds to 127.0.0.1 unless you pass
> `--host`. Do not open boards from sources you would not run code from.

#### A first part

`chipem new-part NAME [--kind gpio|spi|i2c|uart]` writes a commented template (it never overwrites a file):

```sh
chipem new-part boards/parts/my_sensor --kind spi
# wrote boards/parts/my_sensor.py (spi template, class MySensor)
# use it in a board: - {id: U1, type: script, file: my_sensor.py, ports: {CS: A.PTC4, SCK: A.PTC5, MOSI: A.PTC6, MISO: A.PTC7}}
```

The smallest useful part, a delayed inverter (inline in the board YAML; `file:` is better for anything longer):

```yaml
parts:
  - id: U6
    type: script
    params: {delay: 2us}
    ports: {A: BTN, Y: A.PTC5}
    code: |
      from chipem.parts.script import ScriptPart
      class SlowInverter(ScriptPart):
          PORTS = {"A": "in", "Y": "out"}
          PARAMS = {"delay": "1us"}
          def on_change(self, port, level, t_ns):
              if level is not None:
                  self.after(self.params["delay"], self.drive, "Y", 1 - level)
```

Board YAML keys of a script part:

| key | meaning |
|---|---|
| `file` | the `.py` file, relative to the board file |
| `code` | inline source instead of `file` |
| `class` | which class, when the file defines several `ScriptPart` subclasses |
| `params` | `{name: value}` overriding the class's `PARAMS` (any other unknown key is taken as a param too) |
| `ports` | `{port: net or CHIP.PIN}` wiring, as for every part; unwired ports can be wired later in the UI |
| `slow_ms` | wall-time budget of one callback before a `warn` (default 50) |
| `quiet` | no per-change `part` events (as for every part) |

#### Worked example: an SPI slave on real firmware

`boards/parts/k22_spi_world.yaml` puts the K22 `spi_master` sample (sends "hello" every 2 ms and prints what comes
back) on the same nets as `boards/parts/spi_world.py`:

```python
from chipem.parts.script import ScriptPart

class SpiWorld(ScriptPart):
    PORTS = {"CS": "in", "SCK": "in", "MOSI": "in", "MISO": "out"}
    PARAMS = {"reply": "WORLD"}

    def on_reset(self):
        self.spi_slave(cs="CS", sck="SCK", mosi="MOSI", miso="MISO", mode=0)
        self.rx = bytearray()
        self.state["transactions"] = 0
        self.state["last_rx"] = ""

    def on_spi_start(self):            # CS went low: queue the whole reply
        self.rx.clear()
        return self.params["reply"]

    def on_spi_byte(self, value):      # one byte from the master
        self.rx.append(value)

    def on_spi_end(self):              # CS went high
        self.state["transactions"] += 1
        self.state["last_rx"] = self.rx.decode("latin-1")
```

```
$ chipem run --board boards/parts/k22_spi_world.yaml --for 7ms --summary-only
...
UART output:
  UART0 (30 bytes): "M 1 WORLD\r\nM 2 WORLD\r\nM 3 WORL"
...
parts (1):
  U1       script   CS=NSS SCK=SCK MOSI=MOSI MISO=MISO transactions=3 last_rx=hello params=reply=WORLD
```

`part U1 reply=EARTH` in the shell (or the `reply` field in the web UI's Parts panel) changes the answer while it
runs.

#### API reference

Class attributes:

| attribute | meaning |
|---|---|
| `PORTS = {name: kind}` | `"in"`, `"out"` (push-pull, can release), `"inout"` (bidirectional, tri-state), `"od"` (open drain: drives only L), `"analog_in"`, `"analog_out"`; add `" pullup"` / `" pulldown"` for a pull resistor on the port (`"od pullup"` = an I2C breakout) |
| `PARAMS = {name: default}` | settings; the board's `params:` override them, the UI and `part ID name=value` change them at run time (converted to the default's type). `Param(default, min=, max=, step=, unit=, doc=, choices=[...])` adds UI hints and range checks |
| `ADC_FULL_SCALE = 3.3` | volts of the full code for ADC models that take raw codes (TI C2000: 3.0) |

Callbacks (define the ones you need; all optional):

| callback | when |
|---|---|
| `on_reset()` | power-on, every reset and after a reload: set initial outputs, helpers, timers, `self.state` |
| `on_change(port, level, t_ns)` | an input's net changed; `level` is 1, 0 or None (undriven/X). Also once after reset with the first level |
| `@on_rising("CLK")`, `@on_falling("CLK")` | method `fn(self)` on an L→H / H→L edge of an input |
| `@on_edge("CS")` | method `fn(self, level)` on every change of an input |
| `@every("1ms")` | method `fn(self)` periodically, from one period after reset |
| `@action` | method `fn(self)` becomes a button in the web UI and `part ID action=NAME` |
| `on_param(name, value)` | a PARAM was changed at run time |
| `on_analog(port, volts)` | an `analog_out` of another script part on this `analog_in` net changed |
| `on_spi_start()` / `on_spi_byte(value)` / `on_spi_end()` | SPI slave (after `spi_slave()`): return the byte(s) to send first / next |
| `on_i2c_write(data)` / `on_i2c_read(n)` | I2C target (after `i2c_slave()`): one write transfer's bytes / return byte(s) for the master to read |
| `on_uart_byte(b)` / `on_uart_line(text)` | UART (after `uart()`): every byte / every LF-terminated line (CR dropped) |
| `on_host_data(data)` | bytes from a host endpoint opened with `host_open()` |

Methods (call them on `self`):

| method | does |
|---|---|
| `read(port)` → 1/0/None, `level(port)` → "H"/"L"/"X" | level of the port's net |
| `drive(port, v)` | 1/True/"H", 0/False/"L", "X", None/"Z" (release). On an `od` port 1 releases |
| `release(port)`, `driving(port)` | stop driving; what this part drives now |
| `analog(port, volts)`, `read_analog(port)` | set an `analog_out` (chip ADC inputs on its net convert it); read the voltage another script part puts on the net |
| `after(t, fn, *args)`, `repeat(t, fn, *args)` | call later / periodically in virtual time; return a `Timer` (`.cancel()`, `.active`) |
| `now_ns`, `now` | current virtual time in ns / seconds; `ns("250us")` converts a time |
| `spi_slave(cs=, sck=, mosi=, miso=, mode=0..3, bits=8, lsb_first=False, cs_active=0, fill=0xFF)`, `spi_send(data)` | SPI slave engine; queue MISO bytes |
| `i2c_slave(address, sda=, scl=)` | I2C target at a 7-bit address (ports must be `od`); acknowledges its address and every written byte |
| `uart(tx=, rx=, baud=, format="8N1", host=None)`, `uart_send(data)` | UART on two ports (TTL, idle high), or with `host=` ("pty", "pty:/tmp/dev", "tcp:5555", "serial:/dev/ttyUSB0", "memory") talking to PC software instead |
| `host_open(spec)`, `host_write(data)` | a raw host serial endpoint (chipem/hostserial.py), kept across resets and reloads |
| `open(name, mode)`, `path(name)` | a file relative to the script's directory (inline code: the board's directory) |
| `state` (dict) | shown in the web UI, the shell `part ID`, the run summary |
| `params` (dict) | current settings |
| `log(msg)`, `warn(msg)` | a `part` event (action=log) / a `warn` event in the event log |
| `id` | the part id |

Times are strings with a unit (`"250us"`, `"1.5ms"`, `"2s"`) or bare numbers in milliseconds, as everywhere in
chipem. Do not define `__init__`; use `on_reset` (the object is created fresh at every reset).

#### Timing and how it behaves electrically

* Input callbacks run inside the board's zero-delay propagation, at the instruction-precise time of the pin change;
  `drive()` inside a callback joins the same propagation wave (a chain of script gates settles in one event; a
  combinational loop is reported like for the built-in gates).
* Timers are board-clock events: every chip stops at them (lockstep sync points), they are cancelled by a reset and
  `on_reset` starts them again. Very fast periodic timers (below a few µs) slow the emulation down.
* The SPI engine samples and shifts on the SCK edges in the right order for each mode and releases MISO while CS is
  inactive. The I2C engine is the one the I2C EEPROM part uses (START/STOP, ACK, repeated START). The UART helper's
  bytes travel along the board's serial paths (polarity, levels, ground, framing checks of section 6.2), arrive at
  their stop-bit time, and a chip receiver that is not listening drops them like silicon does.
* `analog(port, volts)` reaches chip ADC inputs on the port's net (STM32 ADC pins, Kinetis ADC `SEn` channels with
  the selected reference, C2000 `ADCINxn` as 12-bit codes of `ADC_FULL_SCALE`), and other script parts.

#### Faults, slow callbacks, reload

An exception in your code does not stop the run. The part is marked faulted, its timers are cancelled, it is not
called again until a reset or a reload, and one `warn` gives the error with the script line:

```
warn part U1 faulted (calls stopped until reset or reload): tick: RuntimeError: sensor exploded at dev.py:11 in tick: `raise RuntimeError("sensor exploded")`
```

The Parts panel shows the row in red with the message; `part U1` in the shell prints the last frames. A callback that
takes longer than `slow_ms` of wall time gets one `warn` (the emulation waits for every callback; an endless loop in
a callback hangs the run, there is no way to interrupt Python code safely).

A syntax error or an error while the file is imported fails the board load with the file and line:

```
chipem: error: part U1: syntax error in /home/me/boards/parts/broken.py, line 3, column 13: '{' was never closed
        PORTS = {'A': 'in'
```

**Hot reload.** Edit the file and run `part reload U1` in the shell or press **reload** in the part's row: the file is
read again, the part restarts (`on_reset` on a fresh object; settings changed at run time are kept) and the board
keeps running. Ports cannot change on a reload (the wiring belongs to the board): a reload whose `PORTS` differ is
refused with that message and the old code keeps running; reload the board instead. A file that does not compile is
refused the same way. **edit** opens the source in a plain editor (Ctrl+S saves, Tab indents); Save writes the file
only if the new code loads, then reloads.

#### Examples in `boards/parts/`

| board | script | what it shows |
|---|---|---|
| `k22_spi_world.yaml` | `spi_world.py` | SPI slave answering the K22 `spi_master` sample |
| (tests) | `max31855.py` | MAX31855-like thermocouple converter: read-only 32-bit SPI word, temperature / fault controls |
| `k22_i2c_regs.yaml` | `i2c_regs.py` | I2C register map in ~20 lines; the K22 `i2c_reg_master` sample prints PASS |
| `k22_uart_device.yaml` | `uart_device.py` | line protocol device (ping/pong, id?, temp?) on the K22 UART0; `host: pty` makes it the peer of PC software |
| `stm32_adc_csv.yaml` | `csv_stimulus.py` + `waveform.csv` | analog and digital waveform from a CSV into the STM32 ADC |
| `stm32_pwm_check.yaml` | `pwm_checker.py` | measures a PWM and logs a `warn` when duty or frequency leave their range |

```
$ chipem shell --board boards/parts/stm32_pwm_check.yaml --exec "run 5ms; part CHK1 duty_min=40; run 3ms; events 3 warn; quit"
...
CHK1: freq_hz=1000 duty_pct=25 cycles=3 violations=0 params=duty_min=40.0,duty_max=30.0,freq_min=900.0,freq_max=1100.0
[     5.003 ms] warn part CHK1: PWM out of spec: 1000.0 Hz, duty 25.00 % (want 900..1100 Hz, 40..30 %) chip=board
```

To develop PC software against an emulated device, give the UART helper a host transport: with
`params: {host: pty}` in `k22_uart_device.yaml`, `uart_device.py` answers whatever program opens the printed
`/dev/pts/N` (the run is then paced in real time, like the other host serial bridges).

Limits: one UART, one SPI and one I2C engine per script part; the SPI engine is a slave only; I2C has no clock
stretching or NACK control from the script; analog nets are not resolved electrically (one `analog_out` per net,
no loading); there is no sandbox and no timeout that could interrupt a callback.

## 7. Adding a new chip

Two cases:

- **Another part of a family that already exists** (another STM32F4, another Kinetis K-series, another F2823x/F2833x
  part): register layout is the same, so the existing models work. You write the chip YAML (memory map, pins, IRQs)
  and possibly generate a register table. Recipe in [7.9](#79-short-recipe-another-chip-of-an-existing-family).
- **A new vendor family**: also new peripheral models (GPIO/mux, clock, UART, timer, watchdog). More work; follow the
  full workflow below.

Pick the family by **register layout**, not by vendor: `stm32` means the STM32F4 layout (RM0090), `kinetis` the K22F
layout, `ti_c2000` the F2823x/F2833x. An STM32F1 or an NXP LPC is a new family.

The AI-assisted route: in Claude Code, run `/new-chip <datasheet.pdf> [--svd URL|path] [--part NAME]`
(`.claude/skills/new-chip/`), which follows [AI_NEW_CHIP_PLAYBOOK.md](AI_NEW_CHIP_PLAYBOOK.md), then `/new-board` to
wire it to an existing chip. The steps below are the same workflow done by hand. [ADDING_A_CHIP.md](ADDING_A_CHIP.md)
has the field reference and a model skeleton.

### 7.1 Gather inputs

- The data sheet for the exact part and package (pin table, alternate-function table).
- The reference manual (memory map, IRQ vector table, register behaviour).
- The CMSIS-SVD file. Most Cortex-M parts have one in the `cmsis-svd/cmsis-svd-data` GitHub repo; pin a commit and
  record URL, commit and sha256 in `chips/README.md`. TI C2000 has no SVD: the register table is hand-written in the
  same schema (`chips/tms320f28232_regs.yaml`).
- Store downloads in a new `svd/dl-<name>/` directory. Treat them as untrusted: run the tools with `python -I`.

### 7.2 Scaffold

```
$ .venv/bin/python -I tools/new_chip.py --part STM32F405RG --package LQFP64 --family stm32 --fpu \
      --clock-hz 16000000 --svd svd/dl-stm32f405-Ij9M/STM32F405.svd \
      --mem FLASH:flash:0x08000000:0x100000 --mem SRAM:ram:0x20000000:0x20000 --mem CCM:ram:0x10000000:0x10000
wrote chips/stm32f405rg_regs.yaml
wrote chips/stm32f405rg.yaml
wrote tests/test_stm32f405rg_chip.py
83 peripherals (stm32.dbgmcu 1, stm32.exti 1, stm32.flash 1, stm32.gpio 11, ..., stm32.usart 8, stm32.wwdg 1, stub 48), 83 IRQs, 0 pins; 2 peripheral TODOs.  Next: grep TODO chips/stm32f405rg.yaml
```

(Run here with `--root <scratch dir>` so nothing was written into the repo. Without `--root` it writes to `chips/`
and `tests/`.) It never overwrites without `--force`. Every guess is marked `TODO`:

```
$ grep -n TODO chips/stm32f405rg.yaml
1:# STM32F405RG (LQFP64): generated by tools/new_chip.py -- TODO review every line.
10:# clock_hz: reset clock = HSI (16 MHz on F4, 8 MHz on F1/F0): TODO check the RM's reset clock
12:vtor: 0x08000000    # TODO verify
83:  - {name: RCC, model: stm32.rcc, ..., params: {ext_hz: 8000000}}   # TODO ext_hz = the board's HSE crystal
108:  # TODO the table defines no interrupt [4, 82, 83, 84, 85, 86, 87]: check the RM vector table (SVDs drop some)
195:pins: []
```

Options: `--arch armv6-m|armv7-m|armv7e-m|c28x`, `--cpu cortex-m0|cortex-m0plus|cortex-m3|cortex-m4|cortex-m7|c28x`,
`--fpu/--no-fpu`, `--clock-hz` (the **reset** clock, not the maximum), `--svd` or `--regs`, `--pins pins.csv`,
`--mem NAME:KIND:BASE:SIZE` (repeat), `--exclude BLOCK,...` (SVD blocks the part lacks), `--source`, `--root`.

### 7.3 The pin table

`pins:` lists every package pin, numbered as the package numbers them:

```yaml
gpio_name_format: "P{port}{bit}"      # Kinetis default "PT{port}{bit}"; C2000 "GPIO{num}"
pins:
  - {num: 101, name: PA9, kind: gpio, port: A, bit: 9, default: PA9, functions: {AF1: TIM1_CH2, AF4: I2C3_SMBA, AF7: USART1_TX, ...}}
  - {num: 8, name: VDD, kind: power, default: VDD, functions: {ALT0: VDD}}
```

- `kind`: `gpio` (needs `port` and `bit`), `analog`, `power` (fixed high, or low for names containing VSS/VREFL),
  `special`.
- `default`: the function after reset (`DISABLED` or empty = disabled).
- `functions` keys are per family: Kinetis `ALT0`..`ALT7` and `EzPort`; STM32 `AF0`..`AF15` plus `ADC`, `analog`,
  `additional`; C2000 `GPIO`, `MUX1`..`MUX3`, `ANALOG`. Values are signal names exactly as the data sheet writes them;
  they become the pin owner (`USART1_TX`) that boards and UART links look for.
- `gpio_name_format` sets the pin ids users type (`PA9`, `PTC5`, `GPIO34`).

Transcribe it from `pdftotext -layout` output with a small script (the playbook §B explains the parsing traps), or
give `new_chip.py` a `pins.csv` (`num,name,kind,port,bit,functions[,default]`). Then check it:

```
$ .venv/bin/python -I tools/pin_table_check.py chips/stm32f405zg.yaml
pin table check: chips/stm32f405zg.yaml
  STM32F405ZG LQFP144: 144 pins (gpio 114, power 27, special 3; expected 144 from package 'LQFP144')
  gpio_name_format 'P{port}{bit}', family stm32
  ports: A:0-15 (16), B:0-15 (16), C:0-15 (16), D:0-15 (16), E:0-15 (16), F:0-15 (16), G:0-15 (16), H:0-1 (2)
  function keys: ADC(24) AF0(13) AF1(31) ... AF15(111) additional(5) analog(6)
  NOTE    pin 76 'PB15': RTC_REFIN under AF0, additional
0 error(s), 0 warning(s), 1 note(s)
```

The empty scaffold fails, as it should: `ERROR no pins: the pins: table is empty` / `ERROR 0 pins, expected 64`.
Target: 0 errors (`--strict` for 0 warnings too). Then spot-check 10–20 pins by eye against the PDF (UART pins, an
LED/button pin, reset/boot pins, the first and last pin) and put them in `tests/test_<part>_chip.py`.

### 7.4 Memory map and core settings

```yaml
core: {arch: armv7e-m, backend: unicorn, cpu: cortex-m4, fpu: true, clock_hz: 16000000, addr_unit_bits: 8, num_irqs: 82}
vtor: 0x08000000                 # where the core fetches the vector table at reset
memory:
  - {name: FLASH, base: 0x08000000, size: 0x100000, kind: flash}
  - {name: SRAM,  base: 0x20000000, size: 0x20000,  kind: ram}
```

- Regions must be 1 KB aligned (`kind: flash|ram|flexnvm|flexram|rom`). Map exactly what the part has; SVDs and
  family headers describe supersets.
- `clock_hz` is the clock **after reset**. A chip without a clock model keeps it forever.
- `fpu`: from the data sheet. SVDs are often wrong (`STM32F405.svd` says no FPU).
- `systick_ext_div`: SysTick rate with CLKSOURCE=0 (default 8; 0 = no external reference, as on the K22).
- `auto_stub_ranges`: windows where unknown accesses become RAM-like stubs instead of bus faults.

### 7.5 Peripherals and models

`peripherals:` maps each block to a model: `{name: USART1, model: stm32.usart, base: 0x40011000, size: 0x400,
params: {...}}`. For each block, pick one:

| choice | when |
|---|---|
| an existing model (`kinetis.uart`, `stm32.gpio`, `ti_c2000.sci`, ...) | same family, same register layout |
| `stub` | the firmware only writes configuration there. RAM-like; first access logged |
| a new model | the firmware polls a status bit the hardware sets, or you need its behaviour (pins, IRQs, bytes) |

A **new family package** (`chipem/periph/<family>/`) is needed when no existing family matches the register layout.
Build it in this order, since each step makes the next one observable: GPIO + pin mux (blinky shows edges), clock
tree (periods come out right; honour `--clock`), watchdog, a timer with an IRQ, the console UART, then whatever the
target firmware polls on. ADDING_A_CHIP.md §5–6 and the playbook §E have the contracts and a skeleton.

### 7.6 Check against the SVD

```
$ .venv/bin/python -I tools/svd2chip.py svd/dl-stm32f405-Ij9M/STM32F405.svd --check chips/stm32f405zg.yaml
...
  NOTE  in SVD but not in chip YAML: DBG@0xE0042000, GPIOJ@0x40022400, GPIOK@0x40022800, SPI4@0x40013400, ...
  NOTE  irq 4: chip YAML 'FLASH'; SVD defines no interrupt 4
0 mismatch(es)
```

Target: `0 mismatch(es)`, and every NOTE intentional. Blocks the SVD has but the part lacks go under `omitted:`
with a reason (see `chips/mk22fn1m0vll12.yaml`); `--check` then prints `OMITTED`. Regenerate a register table
with `tools/svd2chip.py <svd> --flat -o chips/<name>_regs.yaml --source "URL @ commit"`; never hand-edit one.

### 7.7 Samples, tests, board check

1. Samples in `firmware/samples_<family>/` with a `build.sh` (copy `firmware/samples_stm32/`): `blinky`,
   `button_mirror`, `uart_echo`, `uart_link_echo`, a timer IRQ and a pin IRQ.
2. Run them: `chipem run ... --chip <part> --for 10ms --events pin`. Check periods and UART bytes.
3. Tests: `tests/test_<part>_chip.py` (pin spot checks, memory sizes, a few IRQ numbers) and
   `tests/test_<family>_samples.py` (edges, periods within 0.1 %, UART bytes, IRQ counts).
4. A board: wire the new chip's `uart_link_echo` to an existing chip's `uart_link_sender` (section 5), run it, and open
   it in `chipem serve --board`. Bytes must cross both ways without a baud warning.
5. `.venv/bin/python -m pytest -q` passes in full.

### 7.8 Definition of done

The playbook §J checklist, in short: inputs and provenance recorded; no `TODO` left in the chip YAML;
`pin_table_check --strict` clean; `svd2chip --check` at 0 mismatches; reset clock, FPU, `num_irqs`, `vtor` right;
memory regions exact; IRQ gaps checked against the RM; the chip builds without "not available" warnings; chip and
sample tests pass with timings; a board runs in `run` and `serve`; README, `chips/README.md`, LIMITATIONS and
CLAUDE.md updated.

### 7.9 Short recipe: another chip of an existing family

Example: a smaller-flash or different-package variant of a chip that already exists.

1. **Same die, different memory**: `include:` the existing YAML and override what differs. Lists are replaced, not
   merged. This is how `mk22fx512vll12.yaml` reuses `mk22fn1m0vll12.yaml`:

   ```yaml
   include: mk22fn1m0vll12.yaml
   name: MK22FX512VLL12
   memory:
     - {name: FLASH, base: 0x00000000, size: 0x80000, kind: flash}
     - {name: FlexNVM, base: 0x10000000, size: 0x20000, kind: flexnvm}
     ...
   ```

   Checked with a scratch variant of `stm32f405zg.yaml` (512 KB flash): `chipem run firmware/samples_stm32/build/blinky.elf
   --chip /path/to/variant.yaml` runs at 16 MHz as expected.
2. **Different package**: also override `package:` and replace `pins:` with the new package's table; run
   `pin_table_check.py`.
3. **Different part in the family** (another STM32F4, another K-series): run `tools/new_chip.py --family stm32|kinetis
   --svd <its SVD>`. It maps blocks to existing models. Then fill the pins (7.3), fix memory and TODOs (7.4),
   `--check` against the SVD (7.6).
4. Check family constants in the models: `stm32.dbgmcu` IDCODE, `stm32.rcc` `ext_hz` (board crystal), `kinetis.sim`
   SDID, the UART FIFO depth (`params.fifo_depth`).
5. Run the family's samples on it (rebuild with the right `-mcpu` and link addresses if needed), add a chip test.

---

## 8. Building firmware

### 8.1 The samples

The prebuilt outputs are in each `build/` directory, and the tests use them. Rebuild only if you change a source.

| directory | build | output | tools |
|---|---|---|---|
| `firmware/samples/` (K22) | `firmware/samples/build.sh` | `build/*.elf`, `.bin`, `.map` (11 samples, incl. `ftm_pwm`, `ftm_capture`) | clang + ld.lld |
| `firmware/samples_stm32/` | `firmware/samples_stm32/build.sh` | 11 samples (incl. `tim_pwm`, `tim_capture`, `uart_dma`, `adc_tim_trigger`), linked at 0x08000000 | clang + ld.lld |
| `firmware/samples_stm32_hal/` | `firmware/samples_stm32_hal/build.sh` | `build/uart_chat_hal.elf` | clang + ld.lld + STM32CubeF4 in `firmware/vendor/` |
| `firmware/samples_c2000/` | `firmware/samples_c2000/build.sh` | `build/*.c28img` | `tools/c28x_asm.py` |
| `firmware/samples_c2000_c/` | `firmware/samples_c2000_c/build.sh` | `build/*.out` (EABI) and `*_coff.out` | TI cl2000 in `toolchains/` |

All five were built for this guide (in a copy of each directory): `ok: 9 samples built`, `ok: 7 samples built`,
`ok: build/uart_chat_hal.elf (6232 bytes)`, the `.c28img` matching the prebuilt one, `ok: 3 images in build/`. The
rebuilt K22 `blinky.bin` is byte-identical to the checked-in one.

Script knobs: `CC=clang`, `PY=.venv/bin/python` (for `elf2bin.py`), `CUBE=` (HAL sources), `TI_CGT=` (TI compiler
root; default `toolchains/ti-cgt-c2000`, then the newest `toolchains/**/ti-cgt-c2000*/bin/cl2000`), `ABI=eabi|coff`.
The HAL build needs the STM32CubeF4 sources; the fetch commands are at the top of its `build.sh`.

### 8.2 Your own Cortex-M firmware (K22)

Reuse the sample startup file, header and linker script. `my_blink.c`:

```c
/* PTD7 as GPIO output, toggled every 5000 loop iterations. */
#include "k22.h"
int main(void)
{
    WDOG_Disable();
    SIM_SCGC5 |= SIM_SCGC5_PORTD;
    PORTD->PCR[7] = PORT_PCR_MUX(1);
    GPIOD->PDDR |= 1u << 7;
    for (volatile uint32_t n = 0;; n++)
        if (n % 5000 == 0) GPIOD->PTOR = 1u << 7;
}
```

```
$ F=firmware/samples
$ clang --target=thumbv7em-none-eabi -mcpu=cortex-m4 -mfpu=fpv4-sp-d16 -mfloat-abi=hard -mthumb -O1 -g \
    -ffreestanding -nostdlib -fno-builtin -I $F -T $F/link.ld -fuse-ld=lld -Wl,--image-base=0 \
    -Wl,-e,Reset_Handler -Wl,--gc-sections -o my_blink.elf $F/startup.c my_blink.c
$ .venv/bin/chipem run my_blink.elf --for 3ms --events pin
[     0.002 ms] PTD7  owner disabled -> GPIO (in)
[     0.002 ms] PTD7  dir in -> out
[     0.002 ms] PTD7  X -> L
[     0.003 ms] PTD7  L -> H
[     2.149 ms] PTD7  H -> L
```

For the STM32 use `firmware/samples_stm32/` (`stm32f405.h`, `startup.c`, `link.ld`, `--image-base=0x08000000`).
There is no libc or compiler-rt for Cortex-M here: bring your own `memset`/`memcpy`/`__aeabi_*` (see
`firmware/samples_stm32_hal/aeabi_rt.c`). Firmware from your own GCC toolchain elsewhere loads the same way (ELF,
HEX or BIN).

Other cores: Cortex-M0 `--target=thumbv6m-none-eabi -mcpu=cortex-m0 -mfloat-abi=soft`; Cortex-M3
`--target=thumbv7m-none-eabi -mcpu=cortex-m3 -mfloat-abi=soft`.

### 8.3 C28x assembly

```
$ .venv/bin/python tools/c28x_asm.py firmware/samples_c2000/blinky.asm -o blinky.c28img
blinky.c28img: 2 segment(s), 38 words, 12 symbols, entry 0x330000
```

Options: `--map`, `--list`, `--bin`, `--sect NAME=ADDR`. The same tool disassembles images like TI's `dis2000`:

```
$ .venv/bin/python tools/c28x_asm.py --disasm firmware/samples_c2000_c/build/uart_chat.out
330000  init_sysctrl:
330000  7622       EALLOW
330001  9A68       MOVB AL,#0x68
330002  8F00 6F88  MOVL XAR4,#0x6F88
```

### 8.3b 8051 assembly (P89CV51)

`tools/mcs51_asm.py prog.asm -o prog.hex` assembles Intel ASM51 syntax (ORG, DB/DW/DS, EQU/SET/DATA/BIT, `$INCLUDE`,
SFR and bit names of the chip from `chips/p89cv51_regs.yaml`) into Intel HEX plus a `prog.map` symbol sidecar that
`chipem` loads with the `.hex` (SDCC `.map`/`.cdb`/`.noi` files are read too).  `--disasm prog.hex` disassembles.
Run with `--chip p89cv51rd2` (the crystal is `--clock`, default 11.0592 MHz).  Address spaces in `mem`, watchpoints
and symbols are tagged: CODE 0x00000, XDATA 0x10000, IDATA 0x20000 (`mem 0x20030 16`), SFR 0x30080 (`mem 0x30087 1`
= PCON), on-chip XRAM 0x40000.  The UART is `UART` (`--at 50ms:UART=hello`); pins are `P1.0`-style.
`firmware/samples_mcs51/` has the samples; docs/MCS51_CORE.md the details.

C with SDCC: `firmware/samples_mcs51/c/build.sh` (SDCC 4.x; the binary is `sdcc` or, on Fedora, `sdcc-sdcc`) builds
each sample with `--debug` into `c/build/<name>.ihx` plus `<name>.cdb`; include `p89cv51.h` (SFRs and bits from the
data sheet, `TF2_VECTOR` 5 = 002Bh, `PCA_VECTOR` 6).  Run the `.ihx` like a `.hex`; the debugger reads the `.cdb`:

```sh
.venv/bin/chipem shell firmware/samples_mcs51/c/build/timer_tick.ihx --chip p89cv51rd2 \
    --exec "break main; continue; list; break t0_isr; continue; bt; dis; next; info break"
```

`break main` stops at the first statement, `break timer_tick.c:31` at a line, `list`/`dis` interleave the C source,
`bt` shows `main` below an interrupt handler (`[interrupt]`) and recursive calls, `next` steps over calls.  Variables
are symbols with tagged addresses (`mem ticks 2`; `__xdata` ones point into the on-chip XRAM, 0x4xxxx); there are no
local variables or C types for SDCC builds.  `--gdb PORT` serves a generic RSP target (`mcs51` target description;
gdb itself has no 8051 support).  For arbitrary 8052 firmware (SDCC `<8052.h>`, no NXP extensions) use
`--chip i80c52`, a generic 80C52 (not from a data sheet).  `tools/mcs51_ucsim_diff.py` compares the core with SDCC's
ucsim simulator (`s51` / `sdcc-s51`).

### 8.4 C for the F28232 with TI's compiler

```
$ TI_CGT=toolchains/ti-cgt-c2000/ti-cgt-c2000_25.11.1.LTS ABI=eabi firmware/samples_c2000_c/build.sh
...
CL  uart_chat (eabi) -> build/uart_chat.out
...
ok: 3 images in build/
```

Flags that work with cl2000 25.11.1 (from `firmware/samples_c2000_c/README.md`): compile `-v28 -ml -mt
--float_support=softlib -O2 -g --abi=eabi`; link `-z --rom_model --stack_size=0x300 --entry_point=code_start` with
`rts2800_ml_eabi.lib`. `codestart.asm` disables the watchdog and branches to `_c_int00`; use it in your own projects,
since the F28232 watchdog fires about 6.5 ms after reset. Start from `f28232.h` and `f28232.cmd` in that directory.

---

## 9. Troubleshooting

| symptom | likely cause | diagnose / fix |
|---|---|---|
| No UART output at all | wrong UART name, UART never enabled, or firmware stuck before it | `chipem info` (UART handler/references?); `--events uart,warn`; shell `periph UART0`; `pinreport` (is a pin muxed to UARTn_TX?) |
| UART output is garbage on a board, or a `baud mismatch` warning | the two ends run different clocks | check the clock with `--events info`; set `--clock`/`clock_hz` for firmware that assumes a clock |
| `RX dropped N byte(s): clock_gated` / `rx_disabled` / `pin_not_muxed` | input sent before the firmware enabled its receiver | send later: `--at 100ms:UART2=...` instead of `--uart-in` |
| STM32 `RX dropped: clock_stopped` / `mute`, or a USART that never transmits | the chip was in Stop mode (`--events info` shows `enter Stop mode`), the receiver is in mute mode (CR1.RWU), or CTSE is set and the USARTn_CTS pin is high | wake the chip first (EXTI pin / RTC); drive the CTS pin low (`--set PA11=0`) or wire it to the peer's RTS |
| Kinetis `RX dropped: stop_mode`, a K22 UART that never transmits, or a K22 that stops running | the chip is in STOP/VLPS/LLS/VLLS (`--events info` shows `power mode LLS`; `SMC.history`), TXCTSE is set and the UARTn_CTS_b pin is high, or the MCG runs from a stopped crystal (`MCGOUTCLK lost` warning) | wake it through the LLWU (pin edge, LPTMR, RTC) or an interrupt for STOP/VLPS (S2.RXEDGIF with BDH.RXEDGIE); drive CTS_b low (`--set PTB3=0`) or wire it to the peer's RTS_b; `MCG.set_external_clock(True)` |
| `RX dropped ...: overrun` | firmware did not read the previous byte in time (busy printing, interrupts off) | send later or slower (one line at a time with several `--at`); this is what silicon does too |
| Timing is 5–6x slower than expected on a K22 | the chip runs at its 20.97 MHz reset clock | `--clock 120e6` if the firmware never programs the MCG |
| Run gets slow, and `stop` shows the same PC every time | firmware polls a status bit of a stub peripheral | `chipem shell fw --exec "run 1s; state; dis; bt"`, then `--log-mmio --events mmio --for 1ms` to see the register it reads; that block needs a model |
| `FAULT write_unmapped at pc=... addr=0x60000000` | access outside every memory region and stub window | `dis` at the PC; wrong chip? missing memory region? |
| `chipem: error: firmware segment byte 0x... is outside mapped memory` (exit 1) | wrong `--chip`, or wrong load address for a `.bin` | `chipem info` to see the segments; `--chip stm32f405zg`; `--load-addr 0x08000000` |
| `stop ... mode=handler (HardFault)` and `last fault : undefined_instruction` | the firmware hit `udf` on purpose (an assert) or jumped to garbage | `--events fault`; `--stop-on-fault` (exit 3) to stop at the first one; shell `bt` |
| Nothing happens, reset PC wrong, immediate fault in a raw `.bin` | wrong VTOR or load address | `chipem info` shows the vector table and `bootable`; `--load-addr` and `--vtor` |
| Firmware behind a bootloader does not boot | VTOR at 0 for an ELF linked higher | `--vtor <image start>` |
| A config register write "does nothing" on the C28x | missing `EALLOW` (protection is enforced) | `--events warn` shows the ignored write |
| `FAULT illegal instruction at 0x... → ITRAP` on the C28x | not C28x code at that address (wrong `--load-addr`, ARM image), an FPU32 instruction on the F28232 (no FPU), or a low-confidence ISA table entry | `chipem info`; shell `dis ADDR`; see `docs/C28X_CORE.md` |
| A `warn` "Unicorn instruction-counter calibration failed" | Unicorn version changed (needs 2.1.x internals) | `.venv/bin/python -c "from chipem.core.unicorn_cm import UnicornCortexM; print(UnicornCortexM().exact_counts)"` must print `True` (it does here); otherwise use `--precise` |
| `reading floating pin ...` warning | an input with no pull and nothing driving it reads 0 | drive it: `--set PIN=1` (the pin report suggests which) |
| `access to unmodelled region` warning | a peripheral without a model was touched | expected for debug blocks (DWT, ITM); for others check the summary list |
| Runs are slow in general | MMIO-heavy polling loops are Python calls; C28x is an interpreter (2–5 M insn/s) | `-q` (no live printing); avoid `--log-mmio` and `--precise`; shorter `--for` |

Real examples:

```
$ .venv/bin/chipem run firmware/samples_stm32/build/blinky.elf --for 1ms          # STM32 image, default K22 chip
chipem: error: firmware segment byte 0x08000000 is outside mapped memory

$ .venv/bin/chipem run badptr.elf --for 1ms --events fault --stop-on-fault        # stores to 0x60000000
[     0.002 ms] FAULT write_unmapped at pc=0x000004b6 addr=0x60000000 → HardFault handler
stop         : FAULT; pc=0x00000410 mode=handler (HardFault)
(exit status 3)

$ .venv/bin/chipem run firmware/samples/build/blinky.bin --chip tms320f28232 --load-addr 0x330000 --for 1ms   # ARM code on a C28x
[     0.000 ms] FAULT illegal instruction at 0x33FFF6 → ITRAP
[     0.000 ms] FAULT ILLEGAL: vector at 0x3FFFE6 holds 0x3FFFFF (not code): interrupt not taken
[     0.000 ms] FAULT illegal_opcode at pc=0x0033fff6 (stopped)

$ .venv/bin/chipem run firmware/samples/build/uart_echo.elf --clock 60e6 --for 2ms --uart-in 'hi' -q
...
  UART0 (8 bytes): "hello\r\nh"
  UART0 RX dropped 1 byte(s): overrun 1 (the receiver was not listening or overran; time input with --at TIME:UART=TEXT)
```

The last one: `uart_echo` polls TX while printing `hello`, so the second byte arrives while the first is unread.

**(new) Kinetis UART FIFOs.** UART0 and UART1 now model the K22's 8-entry TX/RX FIFOs (UART2–4 have 1 entry), set
per instance by `params.fifo_depth` in the chip YAML. The FIFO is used only when the firmware sets `PFIFO.RXFE`/`TXFE`;
otherwise the UART behaves as a single buffer (the reset state), so firmware that does not enable the FIFO still
overruns after one unread byte, as above.

---

## 10. Reference

### 10.1 Command-line options

`chipem info FIRMWARE`: `--chip`, `--load-addr`, `--format`, `--vtor`, `--no-disasm`.

`chipem new-part NAME [--kind gpio|spi|i2c|uart]`: write a commented script-part template `NAME.py` (6.4; never
overwrites).

Options shared by `run`, `shell` and `serve`:

| option | meaning |
|---|---|
| `--chip NAME\|PATH` | chip YAML (`chips/NAME.yaml` or a path); default `mk22fn1m0vll12` |
| `--board FILE` | run a board instead of one firmware |
| `--quantum TIME` | board lockstep quantum (default 10us) |
| `--load-addr ADDR` | load address of a raw `.bin` (disables auto-detection) |
| `--format FMT` | `bin\|hex\|elf\|srec\|coff\|c28img` |
| `--vtor ADDR` | vector table address at reset |
| `--clock HZ` | fix the core clock (freezes the clock tree) |
| `--precise` | exact instruction counting with a block hook (slower) |
| `--set PIN=LEVEL\|PART=VALUE` | drive a pin / set a part from t=0 (repeatable) |
| `--at TIME:PIN=LEVEL\|TIME:PART=VALUE\|TIME:UART=TEXT` | timed stimulus (repeatable) |
| `--uart-in [UART:]TEXT` | host input from t=0 (default UART0) |
| `--uart-pty UART[=LINK]` | bridge a UART to a pseudo-terminal for PC software (6.2) |
| `--uart-tcp UART=[HOST:]PORT` | bridge a UART to a raw TCP listener |
| `--usb-host [PORT][:KEY=VAL,...]` | plug a USB device port into an emulated PC host (section 5.1c) |
| `--usb-cdc [PORT=]TRANSPORT`, `--usb-cdc-pty [LINK]` | open the device's CDC-ACM virtual COM port to PC software (pty, tcp:N, serial:DEV) |
| `--uart-port UART=DEV[:BAUD[:8N1]]` | bridge a UART to a host serial port (pyserial; com0com / socat pair) |
| `--realtime` / `--no-realtime`, `--speed X` | lock virtual time to the wall clock (default on with a host bridge) |
| `--adc CHANNEL=VALUE` | analog input: integer = raw code (`ADC0_SE14=2048`; bare `SE14` = ADC0; C2000 `ADCINA3=2048`), decimal or V/mV = volts (STM32 `PA0=1.65V`; Kinetis comparator `CMP0_IN0=1.2V`, volts only) |
| `--spi-flash SPIn:cs=PIN[,jedec=ID,size=N,file=PATH]` | SPI NOR flash on a Kinetis DSPI or STM32 SPI |
| `--eth-pcap FILE` | write every Ethernet frame to a pcap file (5.1b) |
| `--eth-host ip=..,ping=..,count=..,interval=..` | add a host PC to the Ethernet segment (5.1b) |
| `--part ID:TYPE[:KEY=VAL,...]` | add a part (repeatable) |
| `--log-mmio` | log every peripheral access |
| `--pin-report` | print the pin discovery report after `run` |
| `--vcd FILE` | VCD output (board: one file per chip) |

`run` only: `--for TIME` (default 100ms; until Ctrl-C with a host bridge, or `--for forever`), `-q`/`--quiet`/`--summary-only`, `--events KINDS`, `--max-slice N`,
`--stop-on-fault`. `shell` only: `--exec CMDS`. `serve` only: `--host`, `--port`, `--open`,
`--chunk MS`, `--start`, `--verbose`, `--view CHIP`.

Times: `<number><ns|us|ms|s>`, bare number = ms. Levels: `1/H/high`, `0/L/low`, `X`, `Z/none/float`.

### 10.2 Event kinds

| kind | emitted for | live line looks like |
|---|---|---|
| `pin` | owner, direction, pull or level change | `PTC5  L -> H` |
| `uart` | byte sent, received or dropped | `USART1 TX: "PONG 1\r\n"`, `SCIA DROP: "hi\n"` |
| `can` | CAN frame completed (or not acknowledged) | `A: CAN CAN0 tx 100#00014B32`, `CAN CAN0 tx 100#00004B32 ERROR ack (...)` |
| `mmio` | register access (only with `--log-mmio`) | `MMIO W GPIOC.PDDR = 0x20` |
| `irq` | interrupt events (current models report counts in the summary instead) | |
| `reset` | chip reset with reason | `RESET SYSRESETREQ` |
| `fault` | CPU fault | `FAULT undefined_instruction at pc=0x0003ae1c → HardFault handler` |
| `info` | clock changes, peripheral enable, auto links | `INFO core clock 168.000 MHz (SYSCLK PLL ...)` |
| `warn` | gated clock access, floating reads, unmodelled access, baud mismatch | |
| `part` | part added or changed; script parts: `self.log()` (action=log), reload | `LED1 ON`, `SW1.out L` |
| `i2s` | every 64 I2S frames per direction (Kinetis SAI, STM32 I2S) | `I2S I2S0 tx: 64 frames, ch0 ...` |
| `usb` | USB transfer completed, bus event (attach, reset, suspend, resume, enumerated) | `USB OTG_FS: GET_DESCRIPTOR(DEVICE, 18): 18 bytes in (1 NAKs)`, `USB OTG_FS: bulk IN ep 0x81: 13 bytes 'echo: hello\r\n'` |
| `eth` | Ethernet frame sent on a segment | `ETH HOST 02:00:00:00:00:01 > ff:ff:ff:ff:ff:ff ARP who-has 192.168.0.10 tell 192.168.0.1 (64 bytes)` |

### 10.3 File layout

```
chips/          chip YAMLs (hand-written) and *_regs.yaml register tables (generated; never edit)
boards/         board YAMLs
firmware/       samples per family (+ build.sh, prebuilt build/), vendor/ (STM32CubeF4)
tools/          new_chip.py, pin_table_check.py, svd2chip.py, c28x_asm.py
toolchains/     TI cl2000 (installed) and its installer
svd/            downloaded SVDs (untrusted data)
chipem/         the emulator (core/, arch/, periph/<family>/, parts/, devices/, web/, cli and front ends)
tests/          pytest suite
docs/           this guide and the design documents
```

### 10.4 Where the other docs are

| document | read it for |
|---|---|
| [README.md](../README.md) | feature reference: every CLI flag, the web API, peripheral coverage table, sample list |
| [LIMITATIONS.md](LIMITATIONS.md) | what is approximate or missing; read before trusting timing |
| [ADDING_A_CHIP.md](ADDING_A_CHIP.md) | chip YAML field reference, model skeleton, new-family requirements |
| [AI_NEW_CHIP_PLAYBOOK.md](AI_NEW_CHIP_PLAYBOOK.md) | the full chip procedure (§A–§L), used by `/new-chip` |
| [chips/README.md](../chips/README.md) | register table schema, svd2chip usage, SVD provenance |
| [ARCHITECTURE.md](ARCHITECTURE.md) | slice loop, virtual time, exception model, pins, Session, web worker |
| [C2000_ROADMAP.md](C2000_ROADMAP.md), [C28X_CORE.md](C28X_CORE.md) | F28232 status; the C28x interpreter and ISA verification |
| [firmware/samples_c2000_c/README.md](../firmware/samples_c2000_c/README.md) | building C for the F28232 with cl2000 |
