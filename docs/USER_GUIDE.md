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
5. [Boards: several chips](#5-boards-several-chips)
6. [Parts: switches, LEDs, clocks, logic](#6-parts-switches-leds-clocks-logic)
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
```

The install must be editable (`-e`): `chips/` and the web assets are found relative to the source tree.

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
- **Peripheral pins do not toggle.** A UART TX pin shows its idle level (H), not the bits. Bytes travel only through
  links.

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
| `--adc CH=VALUE` | `--adc ADC0_SE18=40000` | raw conversion result for an ADC channel |
| `--spi-flash SPIn:cs=PIN[,jedec=,size=,file=]` | `--spi-flash SPI0:cs=PTC4` | attach SPI NOR flash to a Kinetis DSPI |
| `--part ID:TYPE[:K=V,...]` | `--part LED1:led:in=PTC5` | add a board part (section 6) |
| `--vcd FILE` | `--vcd out.vcd` | write pin waveforms |
| `--pin-report` | | after the run, print what the firmware did with every pin |
| `--events KINDS` | `--events pin,uart` | event kinds printed live (default all but `mmio`) |
| `--log-mmio` | | log every peripheral register access as `mmio` events (slow) |
| `--precise` | | exact per-block instruction counting (about 10x slower) |
| `-q`, `--quiet`, `--summary-only` | | summary only |
| `--max-slice N` | `--max-slice 200` | max instructions per core slice (default 2000) |
| `--stop-on-fault` | | stop at the first fault instead of entering the HardFault handler |

Pin names accept the GPIO name (`PTC6`), any part of the datasheet name (`LLWU_P10`) or the pin number (`78`).
UART escapes: `\n \r \t \xNN \\`. Quote anything with `\` or spaces for the shell.

Exit status: 0 when the run reaches the end of the window, 3 when it stops on a fault (`--stop-on-fault`) or a
lockup, 1 on a usage or load error. A fault that enters the firmware's HardFault handler still exits 0.

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
| `bt [WORDS]` | best-effort backtrace from LR and stack scan |
| `periph [NAME]` | list blocks, or dump one block's registers without read side effects |
| `uart [NAME] TEXT`, `uartout [NAME]` | send to a UART receiver; show what a UART sent |
| `adc CHANNEL VALUE` | set an ADC reading |
| `events [N] [kinds]`, `trace on\|off` | recent events; print events live during `run` |
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
[     1.087 ms] uart uart=UART0 dir=rx byte=97
[     1.087 ms] uart uart=UART0 dir=tx byte=97
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
the same edges.

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
| Toolbar | Run, Pause, Step, Reset, "Run for" a duration (Enter runs); readouts: status, virtual time, cycles, instructions, clock, PC, speed; chip selector on boards |
| Package / board canvas | the package drawn to scale, pins coloured by level, owner tags (`UART0_TX`). Wheel zooms, drag pans, double-click fits. Hover a pin for its data, click for the pin card, **alt-click to drive it H → L → open** |
| Pins tab | sortable, filterable pin table; "changed only" and "hide power/disabled" toggles; copy and CSV export |
| Discovery tab | the pin report (section 4.6), with a toggle to compare against the static guess |
| UART | choose a UART, read its output ("lines" with optional timestamps, or "raw"), send text with a line ending; a drop counter shows bytes the receiver lost; copy and download; link monitor on boards |
| Events | live log with per-kind (and per-chip) filters, text filter, follow, copy, `.txt`/CSV download |
| Nets, Links, Parts | on boards (section 5.5 and 6) |

Filters accept plain text (case-insensitive substring) or `/regex/`.

Keyboard shortcuts (ignored while typing in a field; `?` shows the list):

| key | action |
|---|---|
| Space | run / pause |
| S | step one instruction |
| R | reset |
| F | fit the canvas |
| W | wire mode (boards; click two pins) |
| 1–9 | select a board chip |
| Del | remove the selected net |
| / | focus the filter of the pane under the pointer (over the board: find a pin) |
| Esc | clear the filter / cancel |
| End | jump to the newest line (UART, events) |
| ? | shortcut list |

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
devices:                          # SPI NOR flash on a Kinetis DSPI (same options as --spi-flash)
  - {type: spi_flash, chip: A, spi: SPI0, cs: PTC4}
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

All ten were run for this guide (`--for 20ms`, chat boards 40 ms, `logic_demo` 8 ms; 0.9–2.5 s wall each).

| board | chips | demonstrates | result (bytes over the links) |
|---|---|---|---|
| `two_k22_uart.yaml` | K22 + K22 | explicit UART link, a net with a pull-up | 72 / 72 |
| `two_k22_uart_pins.yaml` | K22 + K22 | the same with nets only: automatic links | 72 / 72 |
| `k22_stm32_uart.yaml` | K22 + STM32F405 | two vendors over a link | 72 / 72 |
| `c2000_k22_uart.yaml` | K22 + F28232 | bytes lost before the C28x enables SCI-A | 72 / 50 |
| `c2000_stm32_uart.yaml` | F28232 + STM32F405 | C28x sender, STM32 echo | 48 / 48 |
| `c2000_stm32_chat.yaml` | F28232 + STM32F405 | two-way "chat" protocol (HELLO/PING/PONG), bare-metal STM32, nets only | 122 / 130 |
| `c2000_stm32_chat_hal.yaml` | F28232 + STM32F405 | the same with ST's HAL on the STM32 at 168 MHz | 130 / 130 |
| `c2000_stm32_chat_c.yaml` | F28232 + STM32F405 | the same with TI-compiled C on the F28232 | 130 / 130 |
| `three_chip_demo.yaml` | K22 + STM32 + F28232 | one TX net fanning out to two RX pins, explicit one-way links, LEDs, a switch, a deliberate baud-mismatch warning | 4 links |
| `logic_demo.yaml` | K22 + K22 | every kind of part: switch, LEDs, clock, inverter, AND, D flip-flop, probes, counter | no links |

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
| `led` | in | `active` (H), `color` (red) | lit while the net is at `active`; counts changes |
| `probe` | in | | level, rises, falls, last change, frequency |
| `counter` | in | `edge` (rise): rise/fall/both | edge count |
| `and`, `nand`, `or`, `nor`, `xor`, `xnor` | in0..in{n-1}, out | `inputs` (2) | gates |
| `not`, `buf` | in, out | | inverter, buffer |
| `mux2` | a, b, sel, out | | out = a while sel is L, b while H |
| `dff` | d, clk, rst, q, qn | `edge` (rise), `init` (L), `rst_active` (H) | D flip-flop, async reset |
| `latch` | d, en, q | `init` (L) | transparent while en is H |

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

Parts are digital only: no analog values, no propagation delay, no UART bytes.

---

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
| `firmware/samples/` (K22) | `firmware/samples/build.sh` | `build/*.elf`, `.bin`, `.map` (9 samples) | clang + ld.lld |
| `firmware/samples_stm32/` | `firmware/samples_stm32/build.sh` | 7 samples, linked at 0x08000000 | clang + ld.lld |
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
| `--adc CHANNEL=VALUE` | ADC reading (`ADC0_SE14=2048`; bare `SE14` = ADC0) |
| `--spi-flash SPIn:cs=PIN[,jedec=ID,size=N,file=PATH]` | SPI NOR flash on a Kinetis DSPI |
| `--part ID:TYPE[:KEY=VAL,...]` | add a part (repeatable) |
| `--log-mmio` | log every peripheral access |
| `--pin-report` | print the pin discovery report after `run` |
| `--vcd FILE` | VCD output (board: one file per chip) |

`run` only: `--for TIME` (default 100ms), `-q`/`--quiet`/`--summary-only`, `--events KINDS`, `--max-slice N`,
`--stop-on-fault`. `shell` only: `--exec CMDS`. `serve` only: `--host`, `--port`, `--open`, `--realtime`,
`--chunk MS`, `--start`, `--verbose`, `--view CHIP`.

Times: `<number><ns|us|ms|s>`, bare number = ms. Levels: `1/H/high`, `0/L/low`, `X`, `Z/none/float`.

### 10.2 Event kinds

| kind | emitted for | live line looks like |
|---|---|---|
| `pin` | owner, direction, pull or level change | `PTC5  L -> H` |
| `uart` | byte sent, received or dropped | `USART1 TX: "PONG 1\r\n"`, `SCIA DROP: "hi\n"` |
| `mmio` | register access (only with `--log-mmio`) | `MMIO W GPIOC.PDDR = 0x20` |
| `irq` | interrupt events (current models report counts in the summary instead) | |
| `reset` | chip reset with reason | `RESET SYSRESETREQ` |
| `fault` | CPU fault | `FAULT undefined_instruction at pc=0x0003ae1c → HardFault handler` |
| `info` | clock changes, peripheral enable, auto links | `INFO core clock 168.000 MHz (SYSCLK PLL ...)` |
| `warn` | gated clock access, floating reads, unmodelled access, baud mismatch | |
| `part` | part added or changed | `LED1 ON`, `SW1.out L` |

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
