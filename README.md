# CHIP_EM

CHIP_EM is a microcontroller emulator driven by chip descriptions, written in Python. You give it a chip
description (pinout, memory map, peripheral instances, IRQ names; YAML under `chips/`) and a firmware image.
It runs the firmware and shows what the chip's pins do while you drive input pins, feed UART input and set
ADC readings.

New here? Read [docs/USER_GUIDE.md](docs/USER_GUIDE.md).

Three families are supported: NXP/Freescale Kinetis K22F (Cortex-M4F, LQFP100: `MK22FN1M0VLL12`,
`MK22FX512VLL12`), ST STM32F405xx/STM32F407xx (Cortex-M4F: every part and package of data sheet DS8626, from the
64-pin STM32F405RG to the 176-pin STM32F407IG/IE in LQFP176 and UFBGA176) and TI C2000 TMS320F28232 (C28x, LQFP176). ARM
instructions run on [Unicorn](https://www.unicorn-engine.org/) (QEMU TCG); the C28x runs on a pure-Python
interpreter (`chipem/core/c28x`). CHIP_EM itself implements the ARMv7-M exception model (SysTick/NVIC/SCB), the
C28x PIE interrupt architecture and the vendor peripherals. Several chips can share a board (nets and UART links),
for example a C2000 talking to an STM32 over a UART, shown live in the browser UI.

What works today:

- Load `.elf`, `.bin`, Intel `.hex` and `.srec` images (C28x also TI COFF `.out` and `.c28img`). For raw `.bin`
  files the load address is detected from the vector table.
- Static analysis (`chipem info`): vector table and installed handlers, which peripherals the code
  references, provisional pin roles from stores to the pin-mux/GPIO registers (Kinetis `PORTx_PCRn`, STM32
  `GPIOx_MODER/PUPDR/AFRx`, C2000 `GPxMUX/GPxDIR`), named from the chip's register table.
- Run firmware in virtual time (`chipem run`). The output is a live log of pin, UART, reset, fault and info
  events, followed by a summary: configured pins, UART output, exception counts, warnings.
- An interactive REPL (`chipem shell`) with run/step, pin stimulus, registers, memory, disassembly, a
  best-effort backtrace and peripheral register dumps.
- A local browser UI (`chipem serve`) that draws the chip package (or every chip of a board) with live pin
  levels. You can click pins to drive them, and it has a UART console, an event log and a pin-discovery tab.
- VCD export of every GPIO-capable pin for GTKWave or any other VCD viewer.
- Kinetis models for clocks (MCG/SIM), pin muxing and interrupts (PORT), GPIO, watchdog, PIT, LPTMR,
  UART, SPI (DSPI master and slave, bit by bit on the pins), I2C (pin-level bus, master and slave), I2S/SAI, CMT
  (IR carrier modulator), eDMA/DMAMUX, RTC, ADC, PDB, FTM, CRC, DAC, comparators (CMP), VREF, external watchdog
  monitor (EWM), flash controller (FTFE), FlexBus (chip selects + external SRAM), MCM/FMC/AXBS/AIPS/MPU registers
  and the power/reset blocks; STM32
  RCC/GPIO/EXTI/USART/TIM/DMA/ADC/IWDG; C2000 SYSCTRL/GPIO/XINT/CPU timers/SCI/SPI/McBSP/ADC/ePWM+HRPWM/eCAP/eQEP/DMA/I2C/eCAN/XINTF. Every other peripheral is a RAM-like
  stub that logs its first access (see [coverage](#peripheral-coverage)).

Read [docs/LIMITATIONS.md](docs/LIMITATIONS.md) before trusting timing results. The timing model is one
cycle per instruction.

## Install

Requires Python 3.12 or newer (developed on 3.14). Run from the repository checkout: chip descriptions
(`chips/`) and the web UI assets are found relative to the source tree, so use an editable install.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'      # the test extra adds pytest and keystone-engine
```

`pyproject.toml` declares `unicorn`, `capstone`, `pyelftools` and `pyyaml` as dependencies, plus `pytest` and
`keystone-engine` (imported by `tests/engine_util.py` to assemble test programs) in the `test` extra. The versions in use are unicorn 2.1.4, capstone
5.0.9, keystone-engine 0.9.2, pyelftools 0.33, PyYAML 6.0.3 and pytest 9.2.0.

Rebuilding the sample firmware needs `clang` and `ld.lld`. `llvm-objcopy` is optional: without it,
`firmware/samples/elf2bin.py` is used.

## Quick start

There are four subcommands. Each takes a firmware path and `--chip NAME|PATH`. The default chip is
`mk22fn1m0vll12`, and the name is looked up as `chips/<name>.yaml`.

### `chipem info`: static analysis

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
```

Options: `--load-addr`, `--format bin|hex|elf|srec|coff|c28img`, `--vtor ADDR`, `--no-disasm` (skip the
capstone / C28x store scan).

Pin roles are decoded per family, with register names taken from the chip's register table (`regs_file`):
Kinetis `PORTx_PCRn` (mux, pull, IRQC) and `GPIOx_PDDR`; STM32 `GPIOx_MODER/PUPDR/OTYPER/AFRL/AFRH/BSRR`; C2000
`GPxMUX1/2`, `GPxDIR`, `GPxSET/CLEAR/TOGGLE`. The store scan is a bounded forward dataflow over each function: it
remembers constants in registers (literal pool, `movw/movt`, `add rd, rn, #imm`; C28x `MOVL XARn,#addr`,
`MOVW DP`, `ADDB XARn`), stack slots (argument structs) and the known bits of read-modify-write values
(`ldr; bic/orr/bfi; str` on ARM; `MOVL ACC,*+XAR4[0]; OR @AH,#imm; MOVL *+XAR4[0],ACC`, `AND/OR @reg,#imm` on the
C28x), so compiled code that reaches registers through a base register plus displacement is decoded (C28x
`@off`, `*+XARn[k]`, `*XARn++`, `*(0:abs)`). Join points keep only what all incoming paths agree on, loop heads forget
what the loop body redefines, calls clobber the caller-saved registers. STM32 HAL `HAL_GPIO_Init` / `WritePin` /
`TogglePin` calls (found by ELF symbol) are decoded from their arguments. Unknown bits are assumed to keep their
reset value:

```
$ chipem info firmware/samples_stm32/build/button_mirror.elf --chip stm32f405zg
...
Provisional pin roles (2 pins written directly):
  PA0    GPIOA_MODER=0x0 mode input; GPIOA_PUPDR=0x2 pull-down
  PB0    GPIOB_MODER=0x1 mode output
$ chipem info firmware/samples_c2000/build/uart_link_sender.c28img --chip tms320f28232
...
Peripheral stores (DP @off, *+XARn[k], *XARn++, *(0:abs)): SCIA 7, CPUTIMER0 7, SYSCTRL 5, GPIO 3   (0.15 s)
Provisional pin roles (3 pins written directly):
  GPIO28 GPIO_GPAMUX2=0x1 mux 1 (SCIRXDA)
  GPIO29 GPIO_GPAMUX2=0x1 mux 1 (SCITXDA)
  GPIO34 GPIO_GPBDIR=0x1 output; GPIO_GPBTOGGLE=0x1 toggle
```

### `chipem run`: run for a span of virtual time

The `button_mirror` sample sets PTC6 as an input with a pull-up and drives PTC5 = !PTC6. Pulling PTC6 low
at 1 ms:

```
$ .venv/bin/chipem run firmware/samples/build/button_mirror.elf --for 3ms --at 1ms:PTC6=0
[     0.000 ms] INFO core clock 20.972 MHz (FEI), bus 20.972 MHz
[     0.000 ms] RESET reset
[     0.001 ms] INFO WDOG disabled
[     0.002 ms] PTC5  owner disabled -> GPIO (in)
[     0.002 ms] INFO PTC5 → GPIO input
[     0.002 ms] PTC6  owner CMP0_IN0 -> GPIO (in), pull none -> up
[     0.002 ms] PTC6  X -> H
[     0.002 ms] INFO PTC6 → GPIO input (pull-up)
[     0.002 ms] PTC5  dir in -> out
[     0.002 ms] PTC5  X -> L
[     0.002 ms] INFO PTC5 → GPIO output (L)
[     1.000 ms] PTC6  H -> L
[     1.000 ms] PTC5  L -> H

=== summary ===
virtual time : 3 ms (now 3 ms), core clock 20.9715 MHz
cycles       : 62,915 (virtual core cycles, incl. time slept in WFI)
instructions : 62,915 executed (1 cycle/insn model)
wall time    : 0.025 s, 2.51 M insn/s, 0.12x real time
stop         : COUNT; pc=0x000004F8 mode=thread

pins configured by firmware (2):
  pin    num  owner          dir    lvl pull  ext
  PTC5    77  GPIO           out    H   -     -
  PTC6    78  GPIO           in     L   up    L

UART output:
  (none)

exceptions taken:
  (none)
```

Options specific to `run`:

| option | meaning |
|---|---|
| `--for TIME` | virtual time to run (default `100ms`) |
| `-q`, `--quiet`, `--summary-only` | no live events, summary only |
| `--events KINDS` | comma list of kinds to print live: `pin,mmio,irq,uart,reset,fault,info,warn` (default: all except `mmio`) |
| `--all-edges` | print every pin / part edge; by default a signal with 3 or more edges in 10 ms (PWM, a clock, the GPIO or LED it drives) is summarised per 10 ms window: `waveform: N edges ..., 20.000 kHz, duty 25.0 %`, repeated windows as one `waveform unchanged (N more edges)` line. Single edges and `--at` stimulus always print. The VCD (`--vcd`) has every edge either way |
| `--max-slice N` | max instructions per core slice (default 2000) |
| `--stop-on-fault` | stop at the first fault instead of entering the firmware's HardFault handler |

Exit status: 0 if the run reached the end of the window (or was interrupted), 3 if it stopped on a fault
(`--stop-on-fault`) or a lockup, 1 on a usage or load error. A fault that enters the firmware's HardFault handler
does not stop the run, so it still exits 0.

### `chipem shell`: REPL

```
$ .venv/bin/chipem shell firmware/samples/build/blinky.elf --exec "run 1ms; pins changed; events 4 pin; quit"
chipem shell — MK22FN1M0VLL12 (LQFP100, cortex-m4+FPU)
firmware blinky.elf [elf] load 0x00000000   clock 20.9715 MHz
pc=0x00000414 sp=0x20010000   type 'help' for commands, Ctrl-C stops 'run'
stopped: COUNT  t=1.000 ms  cycles=20972  pc=0x000004F0 <main+0x44>
num  pin      owner/function         dir    lvl pull  ext
 77  PTC5     GPIO                   out    L   -     -
(1 of 100 pins)
[     0.241 ms] pin PTC5 level L->H
[     0.480 ms] pin PTC5 level H->L
[     0.718 ms] pin PTC5 level L->H
[     0.957 ms] pin PTC5 level H->L
(4 of 8 events)
```

Without `--exec` the shell is interactive, with readline history in `~/.chipem_history`. Separate several
commands on one line with `;`.

| command | what it does |
|---|---|
| `run [TIME]` | run for TIME (default 10ms); Ctrl-C stops |
| `step [N]` | execute N instructions |
| `reset` | reset the chip (external pin stimulus is kept) |
| `set PIN LEVEL` | drive an input now (`1/0/H/L/X`, `Z` = disconnect) |
| `at TIME PIN LEVEL` | schedule a pin change at an absolute virtual time |
| `pins [all\|changed\|gpio\|PATTERN]` | pin table |
| `pin NAME [all]` | one pin: ALT functions, owner, PCR and GPIO bits, and the firmware's configuration history for it |
| `pinreport [CHIP] [nostatic]` | what the firmware did with every pin so far (every chip on a board) (see [Discovering pin usage](#discovering-pin-usage-from-unknown-firmware)) |
| `regs`, `state`, `bt [WORDS]` | core registers; time/mode/pending exceptions; stack-scan backtrace |
| `mem ADDR [N]`, `dis [ADDR] [N]` | hex dump through the bus (MMIO reads have side effects); disassembly |
| `uart [NAME] TEXT`, `uartout [NAME]` | feed UART input (escapes `\n \r \t \xNN`); show UART output |
| `adc CHANNEL VALUE` | set an analog input: integer = raw code (`adc ADC0_SE14 2048`), decimal or V/mV = volts (`adc PA0 1.65V`, `adc CMP0_IN0 1200mV`) |
| `events [N] [kinds]`, `trace on\|off\|all` | recent events; print events live while running (periodic signals summarised as in `run`; `all` prints every edge) |
| `periph [NAME]` | list peripherals or dump one block's named registers (no read side effects) |
| `vcd PATH` | start writing a VCD file |
| `quit` | leave |

### `chipem serve`: browser UI

```
$ .venv/bin/chipem serve firmware/samples/build/button_mirror.elf
chipem serve — MK22FN1M0VLL12, firmware firmware/samples/build/button_mirror.elf
UI at http://127.0.0.1:8765/   (Ctrl-C to quit)
```

Options: `--host` (default 127.0.0.1), `--port` (default 8765; 0 = any free port), `--open` (open a browser),
`--start` (start running immediately), `--realtime` (pace virtual time to wall-clock time), `--chunk MS`
(virtual ms per worker chunk, default 5), `--verbose` (log HTTP requests), `--board FILE` (serve a whole board,
see below), `--view ID` (with `--board`: the chip selected at start).

The page shows:

- **Package**: the chip's package as SVG (any quad package: N/4 pins per side, so 100, 144 and 176 pins all
  work), with pin 1 at the top of the left side and numbering counter-clockwise. The mouse wheel zooms
  around the cursor, dragging pans, and double-click, **F** or the "fit" button resets the view.
  Colour shows the level (high, low, floating/unknown, power, analog), a glyph shows direction, a dot shows
  external drive, and a tag shows the owning peripheral function (e.g. `UART2_TX`). At the fitted view pin names
  are never drawn smaller than about 9 px while the pin pitch has room for them; owner tags show when they fit
  (zoom in for the rest). Hovering a pin shows its number, datasheet name, owner, direction, drive, pull, external
  level, every ALT function (the active one highlighted) and its level changes counted from the event log.
- **Driving pins**: alt-click a GPIO-capable pin in the package (a plain click opens the pin card, which also
  has drive buttons), or click the pin table's "external" column, to cycle its external drive H → L → open
  (disconnected). Power and analog pins cannot be driven.
- **UART console**: choose a UART, see its output, send text with CRLF/LF/CR/no line ending (escapes
  `\n \r \xNN` allowed).
- **Events**: the live event log, filterable by kind, chip and text (matches are highlighted). The text filter,
  the server's `GET /api/events?q=` and the `.txt` download all use the same row text, so a download holds exactly
  the rows the filter shows.
- **Pins**: a sortable, filterable table (by name, function or owner) with "changed only" and "hide
  power/disabled" toggles. Rows flash when they change.
- **Toolbar**: Run, Pause, Step, Reset, "Run for" a duration, plus readouts of status, virtual time,
  cycles, core clock and PC, the theme switch and `?` (keyboard shortcuts).
- **Light and dark theme**: the page follows the system setting (`prefers-color-scheme`) until you click the
  sun / moon button in the header; the choice is then kept in the browser (`localStorage`, key `chipem.theme`).
  Both themes use the same encoding (H solid, L hollow, X hatched), so levels never depend on colour alone.

**Boards** (`chipem serve --board boards/three_chip_demo.yaml`): the canvas draws every chip with its id and name
on the body, plus the board wiring. The layout is sized from the canvas: it tries every column count, parts beside
the chips or in a row under them, a smaller part scale and a compact package footprint, and keeps the one whose fit
shows pin names readably (then parts, then the largest scale). It is re-chosen when the window or panes change size:

- **Nets** are drawn as wires coloured by the net level (high, low, X; red on a drive conflict). Each wire
  leaves its pins straight outward into the vertical gutter between columns or the horizontal street between
  rows. It then runs along gutters and one trunk street, so it never crosses a package body. Each net has its
  own track, and the gutters and streets grow with the number of wires.
- The **Nets panel** (right of the canvas) lists every net with its members, live level and conflict flag.
  Hovering over a net (in the panel or on the canvas) highlights its wire and member pins and dims the rest.
  Click to select it (Delete removes it), and double-click or ✎ to rename it. The pin tooltip shows a pin's
  net and the other members.
- **Wire mode** (the "wire" button or **W**; **Esc** cancels): click a pin, then another pin on any chip, to
  create a net. If one of the pins is already in a net, the other pin joins that net; two nets are merged.
  Runtime nets behave exactly like YAML nets: they resolve levels, report conflicts and create automatic UART
  links.
- The **UART links panel** lists explicit and automatic links (`auto · A_TX`). Remove a link with ×, or add
  one by choosing the `chip.UART` endpoints from the dropdowns. **Export board YAML** downloads the current
  board (chips with firmware paths, nets, explicit links, devices, stimulus), so you can wire chips
  interactively and save the result. The file is `GET /api/board/yaml`.
- Clicking a chip body selects that chip and zooms onto it. Click it again, double-click, or press `F` to fit.
- **UART links** are dashed arrows, one per direction. Each runs from the pin whose current owner is the
  sender's TX signal (`UART0_TX`, `USART1_TX`, `SCITXDA`, ...) to the receiver's RX pin. Before the firmware
  muxes those pins, the arrow connects the two chip bodies instead. The label shows the byte count and the
  byte rate (bytes per virtual second). A dot carrying the character travels along the arrow for every byte
  sent. Automatic links are drawn dotted.
- A **chip selector** in the toolbar (also: click a chip body, or keys **1**–**9**) picks the chip that
  the pin table, the Discovery tab and the readouts (PC, cycles, instructions, clock) refer to. Clicking any
  pin drives that chip's pin, and nets propagate the level.
- The **UART console** lists every chip's UARTs qualified (`A.UART0`, `B.USART1`, ...) and sends to the
  selected one. In "lines" view it shows the TX lines of the selected UART and its link peer, prefixed by
  chip and UART (`A.UART0 TX: ping 1`, `B.UART0 TX: PING 1`), and "all" shows every UART. "raw" is the
  single-chip view of one UART's output. Below it, the **link monitor** shows the last bytes that crossed
  each link in each direction, with the byte count and rate.
- The **event log** has a chip column and per-chip filters next to the per-kind filters.
- **Parts** (see "Parts: switches, LEDs, logic") are drawn as schematic symbols in extra columns of the grid:
  inputs (switch, button, const/vcc/gnd, pulse, pattern) left of the chips, logic (gates with distinctive shapes
  and inversion bubbles, mux2, dff, latch) and indicators (LED, probe, counter) right of them. Input ports sit
  on the left of a symbol, outputs on the right, and their wires join the nets through the same gutters as pins.
  Click a **switch** to toggle it (H/L shown on the symbol), hold the mouse button on a **button** to press it;
  an **LED** lights in its colour (grey when off, hatched for X), a **probe** shows level, edges and frequency, a
  **counter** its count. Hovering a port shows its net, level and the other members; clicking it selects the net.
  In wire mode a part port can be wired to a pin or to another port.
- The **Parts panel** (below the UART links) lists every part with its state and inline controls (L/H/X/Z for
  switches and consts, press for buttons, period/duty for pulses, `time:level` steps for patterns, clear for
  probes and counters) and one field per port (a net name or `CHIP.PIN`; empty disconnects). **+ Add part** opens
  a dialog built from the part catalog: type, id (automatic if empty), parameters and ports; "pick" fills a
  port from the next pin or part port clicked on the canvas. Export board YAML includes the parts.
- **One chip**: the "+ part" button (or `--part` on the command line) turns a single-chip session into a one-chip
  board; the page then switches to the board layout.

Wiring API (board sessions; every command runs on the emulator thread, so you can rewire while the board is
running): `GET /api/board` (chips with their UARTs, nets, links, `wiring` version); `POST /api/net
{pins: ["A.PTC6", "B.PA0"], name?, pull?}`; `POST /api/net/rename {name, new}`; `POST /api/net/remove {name}`
or `DELETE /api/net/<name>`; `POST /api/link {from: "A.UART0", to: "B.USART1", bidir}`; `POST /api/link/remove
{from, to}` or `DELETE /api/link?from=..&to=..`; `GET /api/board/yaml[?download=1]`. Parts: `GET /api/parts/types`
(`[{type, params: {name: default}, ports: {name: dir}}]`); `POST /api/part {id?, type, params, ports: {port: net
| "CHIP.PIN"}}` (on a single chip this creates the one-chip board); `POST /api/part/set {id, values: {level: "H"}}`;
`POST /api/part/remove {id}`; `POST /api/part/port {id, port, to}` (`to: null` disconnects). `/api/board` and
`/api/chip` have `parts` (`id`, `type`, `params`, `ports` → net, `dirs`, `state`), `state.board.parts` the live
states, and `part` events arrive on the SSE stream. `state.board.wiring`
increases on every wiring change, including automatic links that appear when firmware muxes a pin.

Keyboard shortcuts (ignored while typing in an input; **?** lists them): **Space** run/pause, **S** single step,
**R** reset, **F** fit the canvas, **W** wire mode, **1**–**9** select a board chip, **Del** remove the selected net,
**/** focus the filter of the pane under the pointer (over the board: find a pin), **Esc** clear/cancel, **End**
jump to the newest UART/event line.

The HTTP API used by the page is plain JSON and can also be scripted:

- `GET`: `/api/chip`, `/api/state`, `/api/events?since=SEQ`, `/api/uart?uart=UART0`,
  `/api/pinreport?chip=B&static=1`, `/api/stream` (Server-Sent Events).
- `POST`: `/api/run {"ms": 2}` (omit `ms` to free-run), `/api/pause`, `/api/step {"n": 1}`, `/api/reset`,
  `/api/pin {"name": "PTC6", "level": "L"}` (`null` = disconnect), `/api/uart {"uart": "UART0", "text": "hi\r\n"}`,
  `/api/adc {"channel": "ADC0_SE14", "value": 2048}`.
- On a board, names are qualified with the chip id (`B.PTC6`, `B.UART0`). `/api/chip` keeps the single-chip
  fields at the top level and adds `chips` (`id`, `name`, `package`, `pin_count`, `pins`, `uarts`, `adcs`),
  `board` (`name`, `quantum_ns`), `nets` (`name`, `pins`, `pull`, `level`) and `links` (`type`, `from`, `to`,
  `stats`). `/api/state` has `pins` rows for every chip (each with `chip`), `chips` (per-chip state) and
  `board` (time, skew, net levels, link byte counts). SSE `state` frames carry `pins` only when a pin changed.

```
$ curl -s -X POST localhost:8765/api/pin -d '{"name":"PTC6","level":"L"}'
{"ok": true, "name": "PTC6", "external": "L", "level": "L"}
```

## Stimulus and session options

(`--set`/`--at` also act on board parts, `--part` adds one: see "Parts: switches, LEDs, logic".)

These options are shared by `run`, `shell` and `serve`:

| option | example | meaning |
|---|---|---|
| `--set PIN=LEVEL` | `--set PTC6=1` | drive an input from t=0 (repeatable) |
| `--at TIME:PIN=LEVEL` | `--at 50ms:PTC6=0` | drive an input at an absolute virtual time (repeatable) |
| `--at TIME:UARTn=TEXT` | `--at '100ms:UART2=help\r\n'` | a host starts sending TEXT to a UART receiver at that time, paced at the UART's baud (repeatable; also `--set UARTn=TEXT` for "now") |
| `--uart-in [UARTn:]TEXT` | `--uart-in 'USART1:hi'` | the same from t=0 (default UART0); C escapes `\n \r \t \xNN \\` |
| `--adc CHANNEL=VALUE` | `--adc ADC0_SE14=2048`, `--adc PA0=1.65V` | ADC input: an integer is a raw conversion result (bare `SE14` means ADC0); on the STM32 a value with a decimal point or `V`/`mV` is a voltage (VREF+ 3.3 V) and the channel can be named by pin (`PA0`), `ADC1_IN0` or `ADC123_IN0` |
| `--sd-card HOST[:size=N,kind=sdhc\|sdsc,version=1\|2,image=PATH,save=PATH,writeback=1,init_time=T,busy=T,read_time=T]` | `--sd-card SDIO:size=64M`, `--sd-card SDHC:image=card.img,writeback=1` | insert an SD memory card (`chipem/devices/sdcard.py`) into the STM32 SDIO or the K22 SDHC; `image=` loads a raw card image, `save=`/`writeback=1` write the contents back when the session closes; without it the socket is empty (commands time out) |
| `--spi-flash SPIn:cs=PIN[,jedec=HEX,size=N,file=PATH]` | `--spi-flash SPI0:cs=PTC4`, `--spi-flash SPI1:cs=PA4` | attach a SPI NOR flash device (default Micron M25P16 id 0x202015, 2 MB) to a Kinetis DSPI or STM32 SPI block, chip-selected by a GPIO pin; `file=` backs it with an image |
| `--clock HZ` | `--clock 120e6` | fix the core clock; MCG writes no longer change it |
| `--precise` | | exact per-block instruction counting with a Unicorn block hook (about 10x slower) |
| `--vcd FILE` | `--vcd out.vcd` | write a 1-bit wire per GPIO-capable pin (`x` for floating), 1 ns timescale |
| `--vtor ADDR` | `--vtor 0x4000` | vector table address at reset (default: start of a raw `.bin`; 0 otherwise) |
| `--load-addr ADDR` | `--load-addr 0x4000` | load address of a raw `.bin` (disables auto-detection) |
| `--log-mmio` | | log every peripheral register access as an `mmio` event (slow, verbose) |
| `--pin-report` | | after `run`, print what the firmware did with each pin and which code did it (see [Discovering pin usage](#discovering-pin-usage-from-unknown-firmware)) |

UART input behaves like a terminal on the RX pin: there is no input queue. Each byte arrives at its stop bit, one
character time (at the receiving UART's current baud) after the previous one, and is lost if the receiver cannot
take it then: receiver disabled, peripheral clock gated off or in reset, RX pin not muxed to the UART, or the
holding register/FIFO still full (overrun flag set). Lost bytes show up as `uart` events with `dir=drop
reason=...` and in the run summary ("RX dropped N byte(s)"). Firmware that needs time to boot therefore needs its
input later: use `--at` rather than `--uart-in`. Board links behave the same way.

Pin names accept the short GPIO name (`PTC6`), any part of the datasheet name (`LLWU_P10`), or the pin
number (`78`). Levels are `1/H/high`, `0/L/low`, `X` (undetermined) and `Z`/`none`/`float` (disconnect).
Times are `<number><ns|us|ms|s>`, and a bare number means milliseconds.

With nothing connected and no pull, a pin reads `X`. GPIO reads it as 0 and logs a one-time warning
`reading floating pin ...`.

**Clock.** After reset the K22 runs in FEI mode at 32.768 kHz × 640 = **20.97 MHz**, not the 120 MHz in the
chip YAML. The MCG model recomputes the core clock whenever firmware reprograms MCG or `SIM_CLKDIV1`. The
sample firmware never programs the clocks but assumes 120 MHz core / 60 MHz bus, so pass `--clock` to get
its intended timing:

```
$ .venv/bin/chipem run firmware/samples/build/systick_irq.elf --clock 120e6 --for 5ms --events pin
[     0.000 ms] PTC5  X -> L
[     1.001 ms] PTC5  L -> H
[     2.001 ms] PTC5  H -> L
...
exceptions taken:
  exc 15 SysTick                  4
```

## Sample firmware

`firmware/samples/` contains small bare-metal C programs for the MK22FN1M0 (no libc). Prebuilt `.elf`,
`.bin` and `.map` files are in `firmware/samples/build/` and are used by the tests.

| sample | behaviour |
|---|---|
| `blinky` | PTC5 GPIO output toggled from a busy loop |
| `button_mirror` | PTC6 input with pull-up; PTC5 = !PTC6 |
| `systick_irq` | SysTick every 1 ms at 120 MHz (RVR 119999); handler toggles PTC5 |
| `pit_irq` | PIT channel 0 every 1 ms at a 60 MHz bus (LDVAL 59999); IRQ 48 toggles PTC5 |
| `port_irq` | PTC6 falling-edge interrupt (PORTC, IRQ 61); handler clears ISFR, toggles PTC5 |
| `uart_echo` | UART0 on PTA1/PTA2, prints `hello\r\n` then echoes received bytes (polling) |
| `fpu_test` | single-precision VFP checks; PTC5 = 1 if they pass |
| `uart_link_sender` | board chip A: UART0 115200 at a 120 MHz core, sends `ping N\r\n` every 2 ms (SysTick, polled); toggles PTC5 per received `\n` |
| `uart_link_echo` | board chip B: UART0 115200 at a 120 MHz core, echoes bytes uppercased; toggles PTC5 per byte |
| `ftm_pwm` | FTM0 CH0 edge-aligned PWM on PTC1 (ALT4), 1 kHz, 25 % high, from a 60 MHz bus (`--clock 60e6`) |
| `ftm_capture` | FTM1 CH0 input capture (rising edges, PTA12 ALT3) measures a period and prints `period <ticks>` on UART0; FTM0 CH0 makes a 10 kHz PWM on PTC1 to measure (`boards/k22_ftm_capture.yaml` wires PTC1 to PTA12: `period 6000`), or use `--at` edges on PTA12; `--clock 60e6` |
| `ftm_qd` | FTM1 quadrature decoder (phase A/B mode, PTB0/PTB1 ALT6 = FTM1_QD_PHA/PHB) prints `qd <CNT> up\|down` on UART0 every 10 ms (SysTick); `boards/k22_ftm_qd_encoder.yaml` drives it with a `quadrature_encoder` part; `--clock 60e6` |
| `i2c_eeprom` | I2C0 master (PTB2/PTB3 ALT2, PCR.ODE, 93.75 kHz from a 60 MHz bus) writes `Hello over I2C!` into a 24C02 at 0x50 in 8-byte pages with acknowledge polling, reads it back with a repeated START and prints it plus `PASS` on UART0; probes 0x51 first (`0x51: NACK`); `boards/k22_i2c_eeprom.yaml` |
| `i2c_reg_master` | the same code aimed at address 0x42 (the STM32 `i2c_slave` of `boards/k22_stm32_i2c.yaml`), 16-byte pages |
| `sdhc_card` | SDHC on port E ALT4 (PTE0-5) with the SD card device (`--sd-card SDHC`, 120 MHz core): RSTA/INITA, identification at 375 kHz, 4-bit bus at 20 MHz, block 2 written / read through DATPORT (BWEN/BREN, WML 16), blocks 4-5 written with simple DMA and read back with ADMA2 (auto CMD12); UART0 prints the steps and `done`; `boards/k22_sdcard.yaml` |
| `spi_master` | DSPI0 master (PTC4-7 ALT2: PCS0/SCK/SOUT/SIN), mode 0, 117 kHz (60 MHz bus / 512), CONT keeps PCS0 low for a 5-byte transaction: sends `hello` every 2 ms and prints `M <n> WORLD` (the STM32 `spi_slave` answer) on UART0; `boards/k22_stm32_spi.yaml` |
| `i2s_tone` | I2S0 (SAI) master: 2.048 MHz MCLK (SYSCLK x 32/1875), 256 kHz bit clock, 8 kHz stereo 16-bit I2S frames on PTB18/PTB19/PTC1, a 1 kHz sine; RX synchronous to TX on PTC5; prints `I2S 8000 Hz: tx .. rx 128 ok` and the first received words; `boards/k22_i2s_codec.yaml` |
| `cmt_nec` | CMT sends one NEC IR frame (address 0x04, command 0x08) on PTD7 = CMT_IRO: 38.1 kHz carrier (33 %), time mode with 1 us modulator ticks (bus 40 MHz / 5), next mark/space written at each EOCF; prints `NEC 04 08 sent, 22 cycles` |
| `can_echo` | FlexCAN0 at 500 kbit/s (PTB18/PTB19 ALT2, bus clock 60 MHz): frame `100#<n>4B32` every 10 ms (SysTick), frames 0x200-0x27F printed on UART0 (`RX 201 [4] 00 01 4B 32`), `TX n` per acknowledged frame; `boards/k22_c2000_can.yaml` |
| `crc_dac` | CRC-32/CRC-16 of `123456789` with the CRC module (`CRC32=CBF43926`, `CRC16=29B1`), VREF start-up, the DAC0 buffer stepped by software triggers, and CMP0 (CMP0_IN0 vs. its 6-bit DAC at 1.65 V) printing `COUT=0/1` on UART0 and driving PTC5 (CMP0_OUT); `--adc CMP0_IN0=2V` (comparator inputs take volts: `2V`, `2000mV` or `2.0`) |

`startup.c` holds the vector table (SP, 15 system exceptions, 82 IRQs), the Kinetis flash configuration
field at 0x400, and a `Reset_Handler` that enables the FPU, copies `.data`, zeroes `.bss` and calls `main`.
`k22.h` holds the register definitions and `link.ld` the linker script (flash at 0x0, 128 KB RAM at
0x1FFF0000).

Rebuild:

```sh
firmware/samples/build.sh          # CC=clang by default; PY=.venv/bin/python for elf2bin.py
```

The script compiles each `<name>.c` together with `startup.c`:

```
clang --target=thumbv7em-none-eabi -mcpu=cortex-m4 -mfpu=fpv4-sp-d16 -mfloat-abi=hard -mthumb -O1 -g \
      -ffreestanding -nostdlib -fno-builtin -Wall -Wextra -ffunction-sections -fdata-sections \
      -T link.ld -fuse-ld=lld -Wl,--image-base=0 -Wl,-e,Reset_Handler -Wl,--gc-sections
```

## Boards: connecting chips

A board file (`boards/<name>.yaml`) puts several chips on one virtual PCB. Each chip runs its own firmware with
its own clock, scheduler and event log. Chips are connected by **nets** (pin-level wires) and **links**
(byte-level peripheral connections). Paths in the file are relative to the board file.

```yaml
name: two_k22_uart
quantum: 10us                      # optional lockstep quantum (CLI --quantum overrides)
chips:
  - {id: A, chip: mk22fn1m0vll12, firmware: ../firmware/samples/build/uart_link_sender.elf, clock_hz: 120000000}
  - {id: B, chip: mk22fn1m0vll12, firmware: ../firmware/samples/build/uart_link_echo.elf, clock_hz: 120000000}
links:                             # byte-level peripheral links (bidirectional unless dir: tx)
  - {type: uart, from: A.UART0, to: B.UART0}
nets:                              # electrical pin-to-pin connections, any number of pins per net
  - {name: BTN, pins: [A.PTC6, B.PTC6], pull: up}         # optional net pull (up|down) or fixed driver (drive: H|L)
devices:
  - {type: spi_flash, chip: A, spi: SPI0, cs: PTC4}        # same options as --spi-flash
  - {type: sd_card, chip: A, host: SDHC, size: 64M}         # same options as --sd-card
stimulus: ["A.PTC6=1", "10ms:B.PTC6=0"]
```

Chip entries also accept `load_addr`, `vtor` and `format`. `chipem run|shell|serve --board FILE` replaces the
firmware argument. Every pin, UART, SPI and ADC name can be qualified with a chip id (`--at 5ms:B.PTC6=0`,
`--at 5ms:B.UART0=hi`, `--adc B.ADC0_SE14=100`). An unqualified name refers to the first chip.

```
$ chipem run --board boards/two_k22_uart.yaml --for 20ms
[     2.001 ms] A.UART0 TX: "p"
[     2.088 ms] B.UART0 RX: "p"
[     2.088 ms] B.PTC5   L -> H
[     2.088 ms] B.UART0 TX: "P"
[     2.175 ms] A.UART0 RX: "P"
...
A| === summary chip A (MK22FN1M0VLL12) ===
A|   UART0 (72 bytes): "ping 1\r\nping 2\r\nping 3\r\n..."
...
B|   UART0 (72 bytes): "PING 1\r\nPING 2\r\nPING 3\r\n..."
...
=== board two_k22_uart: 2 chips (A, B), quantum 10 us, time skew 0 ns ===
nets (1):
  BTN          H  [A.PTC6, B.PTC6] pull=up
links (1):
  uart A.UART0 <-> B.UART0: 72 bytes A->B, 72 bytes B->A
```

Board files in `boards/`:

| board | chips | what it shows |
|---|---|---|
| `two_k22_uart.yaml` | K22 + K22 | UART0 link, `BTN` net with a pull-up and stimulus |
| `two_k22_uart_pins.yaml` | K22 + K22 | the same over nets only: TX/RX pins on a net create automatic UART links |
| `k22_stm32_uart.yaml` | K22 + STM32F405ZG | K22 UART0 → STM32 USART1 echo |
| `c2000_k22_uart.yaml` | K22 + TMS320F28232 | K22 UART0 → F28232 SCI-A echo |
| `k22_stm32_can.yaml` | K22 + STM32F405ZG | CAN bus at 500 kbit/s: K22 FlexCAN0 `can_echo` sends `100#<n>4B32` every 10 ms, the STM32 bxCAN1 `can_echo` (filter bank 0 → FIFO 0, CAN1_RX0 interrupt) echoes it as `202#...` (`chipem run --board boards/k22_stm32_can.yaml --for 50ms`) |
| `k22_c2000_stm32_can.yaml` | K22 + F28232 + STM32F405ZG | three CAN families on one bus: the F28232 echoes as `201#...`, the STM32 as `202#...`, the K22 prints both |
| `k22_c2000_can.yaml` | K22 + TMS320F28232 | one CAN bus at 500 kbit/s (`links: [{type: can, nodes: [A.CAN0, B.ECANA]}]`): K22 FlexCAN0 `can_echo` sends `100#<n>4B32` every 10 ms, the F28232 eCAN-A `can_echo` (TI C) echoes it as `201#...`; both print the frames (`chipem run --board boards/k22_c2000_can.yaml --for 100ms`) |
| `c2000_stm32_uart.yaml` | TMS320F28232 + STM32F405ZG | F28232 SCI-A → STM32 USART1 echo (below) |
| `c2000_stm32_chat.yaml` | TMS320F28232 + STM32F405ZG | two-way "chat" protocol (HELLO/PING/PONG) between the F28232 (`uart_chat.asm`) and the bare-metal STM32 `uart_chat` sample, nets only (auto links); the STM32's first lines are lost while the F28232 relocks its PLL |
| `c2000_stm32_chat_hal.yaml` | TMS320F28232 + STM32F405ZG | "chat" protocol (HELLO/PING/PONG) between the F28232 and an STM32 running ST's STM32CubeF4 HAL at 168 MHz, nets only (auto links) |
| `c2000_stm32_chat_c.yaml` | TMS320F28232 + STM32F405ZG | the same chat with the F28232 running TI-compiled C (`firmware/samples_c2000_c`, cl2000 EABI, built by its `build.sh` when TI's compiler is installed) |
| `stm32f405rg_f407ig_uart.yaml` | STM32F405RG (LQFP64) + STM32F407IG (LQFP176) | two package variants with their UART lines wired TX→RX on nets (auto link USART2 ↔ USART6, 230400 baud): the RG at 168 MHz sends `REQ n` every 2 ms, the IG on its 16 MHz HSI answers `ACK n n²` (see "STM32F405xx / STM32F407xx variants") |
| `three_chip_demo.yaml` | K22 + STM32F405ZG + TMS320F28232 | one K22 TX net fans out to the STM32 and C2000 RX pins (two automatic links), replies over explicit links, GPIO nets, LEDs on the activity nets and a switch |
| `logic_demo.yaml` | K22 + K22 | board parts: a switch and LED around `button_mirror`, a 1 kHz clock through an inverter into `port_irq`, an AND gate, a D flip-flop, probes and a counter (see "Parts" below) |
| `k22_ftm_capture.yaml` | K22 | `ftm_capture` measuring its own PWM: a net from PTC1 (FTM0 CH0 PWM) to PTA12 (FTM1 CH0 input capture), prints `period 6000` (100 us at 60 MHz) |
| `pwm_led.yaml` | STM32F405 + K22 | timer PWM outputs (`tim_pwm` TIM3 CH1 on PA6, `ftm_pwm` FTM0 CH0 on PTC1, 1 kHz 25 %) each driving an LED and a probe; the live output summarises the waveforms |
| `c2000_ecap_loopback.yaml` | TMS320F28232 | `ecap` measuring its own ePWM1A: a net from GPIO0 (20 kHz, 30 %) to GPIO24 (eCAP1); the ISR stores period 5000 / high time 1500 SYSCLKOUT cycles |
| `c2000_eqep_encoder.yaml` | TMS320F28232 | `eqep`: a `quadrature_encoder` part (100 lines, 1500 rpm, index) on GPIO20/21/23 = EQEP1A/B/I; eQEP1 resets at each index marker and its 100 Hz unit time out ISR stores QPOSLAT / direction / QCPRDLAT in M0 (`eqep_pos`, `eqep_dir`, `eqep_cprd`); `--at 30ms:ENC=ccw` reverses it |
| `c2000_spi_flash.yaml` | TMS320F28232 | `spi_flash`: SPI-A (GPIO16-19, mode 0, 5 MHz, FIFO) reads the JEDEC ID and four bytes of a SPI NOR flash device whose chip select is SPISTEA |
| `c2000_mcbsp_flash.yaml` | TMS320F28232 | `mcbsp_spi` (TI C): McBSP-A in clock-stop (SPI master) mode on GPIO20-23 at 1 MHz with a SPI NOR flash on a GPIO24 chip select: RDID, page program, read back; SCI-A prints `ID EF4016 DATA MCBS RINT 21` |
| `c2000_xintf_sram.yaml` | TMS320F28232 | `xintf_sram`: a 16-bit external SRAM device on XINTF zone 6 (`devices: [{type: sram, bus: XINTF6}]`), written and checked through the GPIO-muxed XINTF with wait states |
| `c2000_i2c_eeprom.yaml` | TMS320F28232 | `i2c_eeprom` (TI C, `firmware/samples_c2000_c`): I2C-A on GPIO32/33 at 100 kHz (module clock 10 MHz), page write to a 24C32 part at 0x50 with I2CCNT/STP, ack polling via NACK, repeated-START read; SCI-A prints `EEPROM: Hello over I2C` |
| `k22_ftm_qd_encoder.yaml` | K22 | `ftm_qd`: the same encoder part (600 rpm, no index) on PTB0/PTB1 = FTM1_QD_PHA/PHB; the FTM1 quadrature decoder counts, the sample prints `qd 40 up`, `qd 80 up`, ... every 10 ms and counts down after the board stimulus reverses the encoder at 30 ms |
| `stm32_i2c_eeprom.yaml` | STM32F405ZG | `i2c_eeprom` with an `i2c_eeprom` part: USART1 `EEPROM: Hello I2C!` |
| `k22_stm32_i2c.yaml` | K22 + STM32F405ZG | one I2C bus, two vendors: K22 `i2c_reg_master` (I2C0) writes and reads back `Hello over I2C!` through the STM32 `i2c_slave` (I2C1 at 0x42, SCL stretched by its ISR); K22 UART0 `PASS`, STM32 USART1 `SLAVE RX 00:48656C…` / `SLAVE TX 10` |
| `stm32_sdcard.yaml` | STM32F405ZG | `sdio_card` with a 64 MB SDHC card device (`devices: [{type: sd_card, host: SDIO}]`; `image:`/`save:` back it with a host file) |
| `k22_sdcard.yaml` | K22 | `sdhc_card` with a 64 MB SDHC card device on the SDHC (CPU, simple DMA and ADMA2 transfers) |
| `stm32_fsmc_sram.yaml` | STM32F405ZG | `fsmc_sram` with a 64K x 16 SRAM on FSMC NE1 (`devices: [{type: sram, bus: FSMC}]`) |
| `k22_stm32_spi.yaml` | K22 + STM32F405ZG | SPI bit by bit over board nets: K22 DSPI0 master `spi_master` (PCS0 held low with CONT) and the STM32 SPI1 slave `spi_slave`; UART0 `M <n> WORLD`, USART1 `S <n> hello` (quantum 1 us) |
| `k22_i2s_codec.yaml` | K22 | `i2s_tone` with an `i2s_codec` part (I2S format, 16 bit): the codec records the sine (`frames`) and plays back `4660,-4660,22136,-22136`; UART0 `RX 1234 EDCC 5678 A988 ...` |
| `k22_i2c_eeprom.yaml` | K22 | `i2c_eeprom` with an `i2c_eeprom` part on open-drain nets (quantum 1 us): page writes, acknowledge polling of the 5 ms write cycle, random read; UART0 `wrote "Hello over I2C!", read "Hello over I2C!" (… ack polls)`, `PASS` |
| `pwm_capture_3way.yaml` | STM32F405 + K22 + TMS320F28232 | one PWM, three vendors: the STM32 `tim_pwm` output (PA6, 1 kHz 25 %) on a net to the K22 FTM1 capture (`ftm_capture` on PTA12: `period 60000`) and the F28232 eCAP1 (`ecap` on GPIO24: 100000 / 25000 cycles), plus a probe; quantum 1 us so the cross-chip edges stay exact |

### Parts: switches, LEDs, logic

Parts are board components with named ports wired to nets (`chipem/parts/`): things to put values on inputs,
indicators that show what outputs do, and basic logic between them. A part output is a strong driver (like a
push-pull pin); an input port reads the net level (H/L/X). Gates propagate with zero delay.

| kind | types (ports; params) |
|---|---|
| inputs | `switch` (out; `level` H/L/X/Z), `button` (out; `pressed_level` L, `released_level` H, `pressed`), `const` (out; `level`), `vcc`, `gnd`, `pulse` (out; `period` 1ms, `duty` 0.5, `start` L, `phase`), `pattern` (out; `steps` [[TIME, LEVEL], ...], `repeat`, `period`), `quadrature_encoder` (a, b, i; `ppr` 100, `rpm` 60 or `hz` = edge rate, `direction` cw/ccw, `index` true: A and B 90 degrees apart, cw = A leads, 4 x ppr edges per revolution, index high for one A period per revolution; `ENC=ccw`/`stop`/`600rpm`/`40khz`/`restart` at run time) |
| I2C devices | `i2c_eeprom` (sda, scl open drain; `address` 0x50, `size` 256, `page` auto, `twr` 5ms, `init`, `fill`, `pullup` true): 24Cxx EEPROM, NACKs during the write cycle; `i2c_register_device` (sda, scl; `address` 0x48, `regs` {reg: value}, `readonly`, `default`): register pointer + auto-increment. Nets for I2C: `open_drain: true` (wired-AND, pulled up) |
| Audio | `i2s_codec` (bclk, ws, sdin in, sdout out; `bits` 16, `format` i2s / left_justified, `edge` rising, `pattern`, `wav`, `echo`): I2S slave codec, records the frames it receives (`frames`) and plays back a sample pattern, a 16-bit WAV file or what it received |
| indicators | `led` (in; `active` H/L, `color`): on/off + change count; `probe` (in): level, rises, falls, last change, frequency; `counter` (in; `edge` rise/fall/both) |
| logic | `not`, `buf` (in, out); `and`, `nand`, `or`, `nor`, `xor`, `xnor` (in0..in{n-1}, out; `inputs` 2); `mux2` (a, b, sel, out); `dff` (d, clk, rst, q, qn; `edge`, `init`, `rst_active`); `latch` (d, en, q) |

In a board YAML (`boards/logic_demo.yaml`):

```yaml
parts:
  - {id: SW1, type: switch, level: L, ports: {out: A.PTC6}}        # CHIP.PIN: an anonymous net SW1_out is created
  - {id: LED1, type: led, active: L, color: green, ports: {in: LED_A}}   # a net name (created if absent)
  - {id: CLK1, type: pulse, period: 1ms, ports: {out: CLK}}
  - {id: G1, type: and, inputs: 2, ports: {in0: LED_A, in1: CLK, out: GATED}}
  - {id: P1, type: probe, ports: {in: GATED}}
stimulus: ["2ms:SW1=H", "4ms:SW1=L"]
```

On one chip, without a board file, use `--part ID:TYPE[:KEY=VALUE,...]` (keys that are port names wire the port to a
pin or net, the others are parameters):

```sh
chipem run firmware/samples/build/button_mirror.elf --clock 120e6 --for 3ms \
    --part SW1:switch:out=PTC6,level=H --part LED1:led:in=PTC5,active=L --at 1ms:SW1=L --at 2ms:SW1=H
chipem run --board boards/logic_demo.yaml --for 8ms        # summary lists every part's final state
```

Stimulus works on parts like on pins: `--set SW1=H`, `--at 5ms:SW1=L`, `--at 2ms:BTN1=pressed`,
`--at 3ms:CLK1.period=500us` (`ID.KEY=VALUE` sets any value); part ids are looked up before pin names. Part
changes are `part` events (live output: `[ 2.000 ms] LED1 ON`), the run summary has a `parts` block (LED on/off and
change count, probe edges and frequency, counter values), `--vcd` adds a `parts` scope (one wire per output port, per
LED and per probe input; parts added later from the web or the shell are declared too, `x` before they existed: the
VCD header is written when the file is closed), and the shell has `parts`, `part SW1 level=H` and
`part add ID:TYPE:...`.

Every part type also takes `quiet: true` (`--part P1:probe:in=PTC5,quiet=1`): its per-change `part` events (levels,
LED on/off, counts) are not logged, while its state, the nets and the VCD still follow every change. Use it for a
probe or gate on a fast-toggling signal, where one event per edge would flood the log and the live output; add,
remove and user `set` actions are still logged.

### C2000 ↔ STM32 over UART

`boards/c2000_stm32_uart.yaml`: chip A, a TMS320F28232, runs `uart_link_sender` (it programs its PLL to 100 MHz,
muxes GPIO28/29 to SCI-A at 115200 8N1 and sends `ping N` every 2 ms from CPU timer 0; every reply line toggles
GPIO34). Chip B, an STM32F405ZG on its 16 MHz HSI, runs `uart_link_echo` (USART1 on PA9/PA10, AF7; echoes every byte
upper-cased and toggles PB0). The link is `{type: uart, from: A.SCIA, to: B.USART1}`. Nothing is sent before 6.6 ms
because the F28232 waits for its PLL to lock.

```
$ chipem run --board boards/c2000_stm32_uart.yaml --for 20ms
[     0.000 ms] A: INFO SYSCLKOUT 10.000 MHz (reset)
[     0.000 ms] B: INFO core clock 16.000 MHz (SYSCLK HSI 16.000 MHz), APB1 16.000 MHz, APB2 16.000 MHz
...
[     0.002 ms] B.PA9    owner GPIO -> USART1_TX (none)
[     0.003 ms] B.PA10   owner GPIO -> USART1_RX (none)
[     0.003 ms] B.PB0    dir in -> out
[     6.554 ms] A: INFO SYSCLKOUT 100.000 MHz (DIVSEL)
[     6.554 ms] A.GPIO28 owner GPIO -> SCIRXDA (none)
[     6.554 ms] A.GPIO29 owner GPIO -> SCITXDA (out)
[     6.554 ms] A.GPIO34 dir in -> out
[     6.554 ms] A: INFO SCIA: TX on, RX on, 115741 baud 8N1 (LSPCLK 25.000 MHz)
[     8.555 ms] A.SCIA TX: "pi"
[     8.641 ms] B.USART1 RX: "p"
[     8.642 ms] B.PB0    L -> H
[     8.642 ms] B.USART1 TX: "P"
[     8.729 ms] A.SCIA RX: "P"
...
A| === summary chip A (TMS320F28232) ===
A| virtual time : 20 ms (now 20 ms), core clock 100 MHz
A| pins configured by firmware (3):
A|   GPIO29   2  SCITXDA        out    H   up    -
A|   GPIO28 141  SCIRXDA        none   H   up    -
A|   GPIO34 142  GPIO           out    L   up    -
A| UART output:
A|   SCIA (48 bytes): "ping 1\r\nping 2\r\nping 3\r\nping 4\r\nping 5\r\nping 6\r\n"
...
B| === summary chip B (STM32F405ZG) ===
B| pins configured by firmware (3):
B|   PB0     46  GPIO           out    L   -     -
B|   PA9    101  USART1_TX      none   X   -     -
B|   PA10   102  USART1_RX      none   H   up    -
B| UART output:
B|   USART1 (48 bytes): "PING 1\r\nPING 2\r\nPING 3\r\nPING 4\r\nPING 5\r\nPING 6\r\n"
...
=== board c2000_stm32_uart: 2 chips (A, B), quantum 10 us, time skew 0 ns ===
links (1):
  uart A.SCIA <-> B.USART1: 48 bytes A->B, 48 bytes B->A
```

Add `--pin-report` for a per-chip discovery report (A: GPIO34 GPIO out, GPIO28/29 SCI-A; B: PA9/PA10 USART1, PB0
GPIO out). To watch it in the browser:

```sh
chipem serve --board boards/c2000_stm32_uart.yaml --open      # then "Run for 40ms"
```

The board view draws the F28232 (LQFP176) and the STM32 (LQFP144) side by side. Once the firmware has muxed the
pins, the two dashed link arrows run from GPIO29 (SCITXDA) to PA10 and from PA9 to GPIO28 (SCIRXDA), with byte
counts; the UART console interleaves `A.SCIA TX: ping N` and `B.USART1 TX: PING N`. Selecting a chip shows its
readouts (C28x: PC and instruction count; no xPSR), its pin table and its Discovery tab.

How it runs:

- **Lockstep.** The board repeatedly runs the chip that is furthest behind, up to the earliest time of the other
  chips plus one quantum (default 10 µs). No chip gets more than one quantum ahead of another, so cross-chip
  effects arrive at most one quantum late. Smaller quanta are more precise and slower. A single-chip session
  does not use the board code at all.
- **Nets** resolve like a wire. Driven GPIO outputs, the net's `drive:` and user stimulus are strong. They win
  over the net `pull:`, which wins over the member pins' own pull resistors. Anything else is `X`. Two outputs
  driving different levels are a conflict: the net goes to `X` and the board logs one `warn` per conflict. The
  net level is written into every member pin's external input, and a driving pin still shows its own drive.
  Stimulus on a net pin (`--at 10ms:B.PTC6=0`) drives the whole net. `Z` releases it.
- **UART links** work at the byte level. Any model with the board-link hooks (`kinetis.uart`, `stm32.usart`,
  `ti_c2000.sci`) can be an endpoint; names resolve across families (`USART1`, `UART1`, `SCIA`, `SCI-A`). A byte
  written to the sender's data register arrives in the peer's receiver one character time later, timed by the sender's baud rate and frame. If the two UARTs' baud rates differ by
  more than 3 % (or their frames differ), the board logs one `warn` per link. The bytes are still delivered.

- **Automatic UART links from nets.** Peripheral pin functions such as `UART0_TX` do not drive pin levels
  in this emulator, because the UART models work on bytes. Instead, when a net joins a pin owned by a UART TX
  signal (Kinetis `UARTn_TX`, STM32 `USARTn_TX`/`UARTn_TX`, C2000 `SCITXDA/B`) with a pin owned by a UART RX
  signal, the board creates a one-way byte link TX block → RX block and logs `info` "net A_TX: auto UART link
  A.UART0 → B.USART1". The board re-evaluates this whenever a member pin's owner changes, because firmware
  muxes pins at runtime: before the RX pin is muxed nothing is delivered, and re-muxing the pin to GPIO
  removes the link. The reverse direction needs its own net (RX ← TX). One TX net can feed several RX pins.
  An explicit `links:` entry that already covers a direction suppresses the automatic link, so bytes are never
  delivered twice. `boards/two_k22_uart_pins.yaml` uses nets only. I2C needs no link: its pins toggle with the
  traffic, so an I2C bus is just two nets (`open_drain: true`, quantum 1 us).

Front ends. `run` prefixes live events with the chip id, prints one summary per chip (lines prefixed `A| `)
and then a board summary. `--vcd x.vcd` writes one file per chip (`x.A.vcd`, `x.B.vcd`). In `shell`, `pins`
lists every chip, with other chips' pins qualified (`B.PTC5`). `run` runs the whole board, and `step` steps
the first chip only. `serve` runs and draws the whole board (see "`chipem serve`: browser UI"); `--view B`
selects chip B at start. Each event has a `chip` field.

## Supported chips

| chip | file | memory |
|---|---|---|
| MK22FN1M0VLL12 | `chips/mk22fn1m0vll12.yaml` | 1 MB flash @0x0; SRAM_L 64 KB @0x1FFF0000; SRAM_U 64 KB @0x20000000 |
| MK22FX512VLL12 | `chips/mk22fx512vll12.yaml` | 512 KB flash @0x0; FlexNVM 128 KB @0x10000000; FlexRAM 4 KB @0x14000000; same SRAM |
| STM32F405ZG | `chips/stm32f405zg.yaml` | 1 MB flash @0x08000000; SRAM 128 KB @0x20000000; CCM 64 KB @0x10000000; backup SRAM 4 KB @0x40024000 |
| STM32F405RG/VG/OG, STM32F407VG/ZG/IG/IGH | `chips/stm32f405rg.yaml`, ... (table below) | as the STM32F405ZG |
| STM32F405OE, STM32F407VE/ZE/IE/IEH | `chips/stm32f405oe.yaml`, ... (table below) | 512 KB flash @0x08000000; RAM as the STM32F405ZG |
| TMS320F28232 | `chips/tms320f28232.yaml` | 16-bit words: flash 64K @0x330000; M0/M1 1K+1K @0x0; L0-L5 SARAM 24K @0x8000 (L0-L3 also @0x3F8000); OTP @0x380400; boot ROM @0x3FE000 |

The two K22 parts are 120 MHz Cortex-M4F parts in LQFP100. `mk22fx512vll12.yaml` includes the FN1M0 file and overrides
the name and memory map. All 100 pins are transcribed from datasheet section 5.1 (ALT0–ALT7 and EzPort),
with IRQ numbers 0–81 in MK22F12 header order. Register offsets, widths and reset values come from NXP's
`MK22F12.svd` via `tools/svd2chip.py` → `chips/mk22_regs.yaml`; `chips/README.md` records the provenance.

### STM32F405ZG

STMicroelectronics Cortex-M4F, 168 MHz, LQFP144 (`family: stm32`, models in `chipem/periph/stm32/`), the second
vendor family. It reuses `arch/armv7m` unchanged; everything vendor-specific is the chip YAML, the SVD-generated
`chips/stm32f405_regs.yaml` and the `stm32.*` models.

- **Pins**: all 144 pins from DS8626 Table 7 (LQFP144 column) with `AF0`..`AF15` from Table 9, plus `ADC`
  (channel, owner in analog mode), `analog` (DAC_OUTx, OSC pins) and `additional` (WKUP, RTC, VBUS) keys. Pin ids
  follow ST naming (`gpio_name_format: "P{port}{bit}"`): `PA9`, `--set PA0=1`. Checked against the LQFP144 pinout
  figure and ST's STM32_open_pin_data. Ethernet/DCMI functions (STM32F407 only) are left out.
- **Boot**: the flash alias at 0x00000000 is not mapped. `vtor: 0x08000000` in the YAML makes the core fetch the
  vector table from flash, so images linked at 0x08000000 (ELF or `.bin --load-addr 0x08000000`) boot directly.
- **Clock**: 16 MHz HSI after reset. `stm32.rcc` follows HSE (`ext_hz`, default 8 MHz), PLL (M/N/P), HPRE and
  PPRE1/2 and retunes the core clock. The HAL default HSE 8 MHz → PLL M=8 N=336 P=2 gives 168 MHz, APB1 42 MHz and
  APB2 84 MHz (timers 84/168 MHz). `--clock` / `clock_hz` freeze it, as with the Kinetis MCG.
- **Samples**: `firmware/samples_stm32/` (`build.sh`): `blinky` (PB0/PB7), `button_mirror` (PA0 pull-down → PB0),
  `uart_echo` (USART1 PA9/PA10 AF7, 115200 at HSI; `--at 1ms:USART1=text`), `uart_link_echo`, `exti_irq` (PA0
  rising edge → EXTI0 → toggles PB0), `tim_irq` (PLL to 168 MHz, then TIM2 at 1 ms → toggles PB0) and `tim_pwm`
  (TIM3 CH1 PWM mode 1 on PA6, 1 kHz, 25 %, at the 16 MHz reset clock), `tim_capture` (TIM3 CH2 input capture on PA7,
  rising edges at 1 MHz, prints `period <n> us` on USART1; drive PA7 with `--at` or a net), `uart_dma` (USART1 TX by
  DMA2 Stream7 ch4 with the stream IRQ, RX of 8-byte blocks by DMA2 Stream5 ch4, echoed back; `chipem run
  firmware/samples_stm32/build/uart_dma.elf --chip stm32f405zg --for 3ms --uart-in USART1:abcdefgh`), `adc_tim_trigger`
  (TIM3 TRGO = update at 1 kHz triggers ADC1 IN0 on PA0, the EOC interrupt stores DR and the main loop prints
  `adc <code>` on USART1; `chipem run firmware/samples_stm32/build/adc_tim_trigger.elf --chip stm32f405zg --for 5ms
  --adc PA0=1.65V` prints `adc 2048`), `spi_flash` (SPI1 master on PA5/PA6/PA7, CS PA4: JEDEC ID, page program,
  read back, printed on USART1; `chipem run firmware/samples_stm32/build/spi_flash.elf --chip stm32f405zg --for 20ms
  --spi-flash SPI1:cs=PA4`), `rtc_alarm` (LSE RTC from 2024-02-28 23:59:58, alarm A every second via EXTI17, WFI,
  prints the time; `cold boot`/`warm boot` from BKP0R across resets), `spi_master`/`spi_slave` (for
  `boards/stm32_spi_pair.yaml`: two STM32s on SCK/MOSI/MISO/NSS nets, bit by bit; `chipem run --board
  boards/stm32_spi_pair.yaml --for 9ms`), `i2c_eeprom` (I2C1 master on PB6/PB7 AF4 open drain, 100 kHz from HSI:
  page writes of `Hello I2C!` to a 24C02 part, acknowledge polling, repeated-START read; `chipem run --board
  boards/stm32_i2c_eeprom.yaml --for 20ms` prints `EEPROM: Hello I2C!`), `i2c_slave` (I2C1 slave at 0x42,
  interrupt-driven 16-register map, prints `SLAVE RX <ptr>:<hex>` / `SLAVE TX <n>`; with the K22 master of
  `boards/k22_stm32_i2c.yaml`), `can_echo` (bxCAN1 at 500 kbit/s on PB8/PB9 AF9 from PCLK1 = 16 MHz: filter bank 0
  passes `100#...` into FIFO 0, the CAN1_RX0 interrupt queues it, the main loop echoes it as `202#...` and prints
  `RX 100 [4] 00 01 4B 32 F00` (FIFO, FMI) / `TX 202` on USART1; `boards/k22_stm32_can.yaml`,
  `boards/k22_c2000_stm32_can.yaml`), `sdio_card` (SDIO host on PC8-PC12/PD2 with the SD card device: identification at
  400 kHz from PLL48CLK, 4-bit bus at 8 MHz, block write/read through the FIFO, CMD18 + CMD12 into DMA2 stream 3;
  `boards/stm32_sdcard.yaml`), `fsmc_sram` (FSMC NE1 with a 64K x 16 board SRAM: test, 128 KB mirror, code run from
  the SRAM, bus fault once MBKEN = 0; `boards/stm32_fsmc_sram.yaml`). `boards/pwm_led.yaml` runs
  `tim_pwm` and the K22 `ftm_pwm` side by side, each PWM pin lighting an LED and watched by a probe (1 kHz);
  `boards/pwm_capture_3way.yaml` feeds the `tim_pwm` output to a K22 FTM capture and a C2000 eCAP.
- **Board**: `chipem run --board boards/k22_stm32_uart.yaml --for 20ms` connects a K22 (`uart_link_sender` on UART0)
  to the STM32 (`uart_link_echo` on USART1). The K22 sends `ping N` and the STM32 answers `PING N`.
- **Vendor HAL sample**: `firmware/samples_stm32_hal/` builds `uart_chat_hal.elf` from ST's own STM32CubeF4 HAL
  (HAL driver 1.8.5 + CMSIS device templates, fetched into `firmware/vendor/STM32CubeF4/`; the fetch commands are at
  the top of `build.sh`). `main.c` is CubeMX-style: `HAL_Init` (SysTick 1 kHz), `SystemClock_Config` (HSE 8 MHz →
  PLL 168 MHz, FLASH 5 WS, PWR scale 1), `HAL_GPIO` on PB0/PB1, `HAL_UART` on USART1 (blocking TX,
  `HAL_UART_Receive_IT` RX). It speaks the "chat" protocol: `HELLO STM32` at boot, `PING n` every 5 ms, answers
  `PING n` with `PONG n`, toggles PB0 per `PONG`, raises PB1 on `HELLO` (and answers the first `HELLO` with its own
  once, for a peer that booted later). Rebuild with `firmware/samples_stm32_hal/build.sh` (clang + ld.lld; the
  sample carries its own `memset`/`__aeabi_*` helpers since there is no libc or compiler-rt here).
  `chipem run firmware/samples_stm32_hal/build/uart_chat_hal.elf --chip stm32f405zg --for 30ms --at
  '1ms:USART1=HELLO C2000\r\nPING 1\r\n'`; with the C2000: `chipem run --board boards/c2000_stm32_chat_hal.yaml --for 30ms`.
  The same build also makes `uart_dma_hal.elf` (`uart_dma_hal.c`): `HAL_UART_Transmit_DMA` (DMA2 Stream7) and
  `HAL_UART_Receive_DMA` (DMA2 Stream2) of 8-byte blocks, echoed back, with `HAL_DMA_IRQHandler` driving the callbacks,
  and `i2c_eeprom_hal.elf` (`i2c_eeprom_hal.c`, HAL I2C at 168 MHz / PCLK1 42 MHz, 100 kHz): `HAL_I2C_IsDeviceReady`,
  `HAL_I2C_Mem_Write` + ready polling, `HAL_I2C_Mem_Read`, `HAL_I2C_Master_Transmit`/`Receive` against an
  `i2c_eeprom` part, printing `MEM: HAL I2C!`, `RX: I2C!`, `0x77: none`. Tests: `tests/test_stm32_hal.py`.
  `sd_hal.elf` (`sd_hal.c`) runs ST's SD driver (`stm32f4xx_hal_sd.c`, `stm32f4xx_ll_sdmmc.c`) against the SD card
  device: `HAL_SD_Init` (400 kHz identification, 4-bit bus at 16 MHz), `HAL_SD_WriteBlocks`/`ReadBlocks` of one and two
  blocks, `HAL_SD_Erase`, `HAL_SD_GetCardStatus`; `chipem run firmware/samples_stm32_hal/build/sd_hal.elf --chip
  stm32f405zg --sd-card SDIO:size=64M --for 60ms` prints `CARD v2 SDHC blocks=131072 bs=512` ... `done`
  (`tests/test_stm32_sdio.py`).

### STM32F405xx / STM32F407xx variants

Every part number and package of DS8626 Rev 12 (Table 1, Table 2) has a chip file. They are the same die as the
STM32F405ZG (DEV_ID 0x413, same memory map, peripherals, IRQs and models): each file `include:`s
`chips/stm32f405zg.yaml` and overrides only the name, package, flash size and pin table.

| `--chip` | part | package | pins / GPIOs | flash | Ethernet + DCMI functions |
|---|---|---|---|---|---|
| `stm32f405rg` | STM32F405RG | LQFP64 | 64 / 51 | 1 MB | no |
| `stm32f405og` | STM32F405OG | WLCSP90 | 90 / 72 | 1 MB | no |
| `stm32f405oe` | STM32F405OE | WLCSP90 | 90 / 72 | 512 KB | no |
| `stm32f405vg` | STM32F405VG | LQFP100 | 100 / 82 | 1 MB | no |
| `stm32f405zg` | STM32F405ZG | LQFP144 | 144 / 114 | 1 MB | no |
| `stm32f407vg` / `stm32f407ve` | STM32F407VG / VE | LQFP100 | 100 / 82 | 1 MB / 512 KB | yes |
| `stm32f407zg` / `stm32f407ze` | STM32F407ZG / ZE | LQFP144 | 144 / 114 | 1 MB / 512 KB | yes |
| `stm32f407ig` / `stm32f407ie` | STM32F407IG / IE | LQFP176 | 176 / 140 | 1 MB / 512 KB | yes |
| `stm32f407igh` / `stm32f407ieh` | STM32F407IG / IE (H suffix = BGA) | UFBGA176+25 | 201 / 140 | 1 MB / 512 KB | yes |

- **Pins**: Table 7 (the package's column) and Table 9 (AF0..AF15), cross-checked against ST's STM32_open_pin_data
  for every package (0 differences in positions and AF numbers). The F407 files add AF11 `ETH_*` and AF13 `DCMI_*`.
  Pins the package does not bond out do not exist: on the LQFP64 part `--set PE2=1` is an unknown pin, and firmware
  writing GPIOE..GPIOI there changes no pin (the registers still exist, as on silicon).
- **BGA / WLCSP numbering**: `num` is the row-major ball index (UFBGA176: rows A..R without I and O, A1 = 1 ..
  R15 = 201, including the 25 centre VSS balls; WLCSP90: A1 = 1 .. J10 = 90). Each pin also carries its ball name
  as `ball:` in the YAML. Use pin names (`PA9`) everywhere; the web UI draws these packages as quad packages.
- **Samples**: everything in `firmware/samples_stm32/` runs unchanged on every variant (PA9/PA10, PB0 are bonded
  in every package). `firmware/samples_stm32_variants/` (`build.sh`, shares startup/linker script with
  `samples_stm32`) adds `port_walk` (toggles all 16 bits of GPIOA..GPIOI: exactly the package's GPIOs toggle),
  `link_requester` (STM32F405RG: PLL 168 MHz, USART2 PA2/PA3 230400 baud, TIM2 2 ms → `REQ n`, checks `ACK n n²`,
  PB0 per good ACK, PB1 on error, PC13 = link up) and `link_responder` (STM32F407IG: HSI 16 MHz, USART6 PG14/PG9,
  answers `ACK n n²`, toggles PI1).
- **Board**: `chipem run --board boards/stm32f405rg_f407ig_uart.yaml --for 20ms` (or `chipem serve --board ...`).
  The UART lines are plain nets (`A.PA2`–`B.PG9`, `B.PG14`–`A.PA3`), so the link is created automatically.
- Checks: `.venv/bin/python -I tools/pin_table_check.py chips/stm32f407ig.yaml --strict` (add `--expect 201` for
  the UFBGA files), `tools/svd2chip.py ... --check` (0 mismatches against both STM32F405.svd and STM32F407.svd).
  Tests: `tests/test_stm32_variants.py`.

### TMS320F28232

TI C2000 (C2823x), fixed-point C28x at up to 100 MHz, LQFP176 "PGF" (`family: ti_c2000`, models in
`chipem/periph/ti_c2000/`, interrupt architecture in `chipem/arch/c28x/`, CPU in `chipem/core/c28x/`, a pure-Python
interpreter). The first non-ARM chip: details and limitations in `docs/C2000_ROADMAP.md`.

- **Words, not bytes**: `addr_unit_bits: 16`. Every address in the YAML, the shell, `--load-addr` and the event log is
  a 16-bit word address; 32-bit registers are two words, low word first. Images: `.c28img` (JSON from the C28x
  assembler), TI COFF `.out` (sniffed, ELF `.out` still works), C2000 ELF, raw `.bin` (`--load-addr` in words).
- **Pins**: all 176 pins from SPRS439Q Table 6-1 with `MUX1`..`MUX3` from the GPIO mux tables (8-18..8-20), ids
  `GPIO0`..`GPIO87` (`gpio_name_format: "GPIO{num}"`, port A/B/C = GPIO0-31/32-63/64-87): `--set GPIO12=0`.
- **Boot**: no boot ROM. Reset starts at the image entry, else at 0x33FFF6 (the boot-to-flash location, which should
  hold `LB _c_int00`), with SP = 0x400 and the boot ROM's ST1. The watchdog runs after reset (≈6.5 ms timeout) like
  on silicon, so firmware must service or disable it.
- **Clock**: OSCCLK 20 MHz (`SYSCTRL` `ext_hz`); 10 MHz after reset (boot ROM DIVSEL /2); PLLCR = 10 + DIVSEL /2
  gives 100 MHz after the 131072-cycle PLL lock. LSPCLK (SCI) = SYSCLKOUT / 4 by default. One cycle per instruction.
- **Interrupts**: PIE groups 1-12 with PIEACK, INT13/INT14, NMI, ITRAP; `irqs:` are keyed by PIE vector number
  (TINT0 = 38 = INT1.7) and the run summary labels them `PIE 1.7`.
- **Models**: `sysctrl` (PLL, clock gates, watchdog), `gpio`, `xint`, `cputimer`, `sci` (SCI-A/B with FIFOs and
  interrupts; board-link endpoint; `--at 7ms:SCIA=text` once the PLL has locked), `adc` (software start; `--adc ADCINA3=2048`),
  `flash`/`csm` (minimal), `spi` (SPI-A bit-level on the pins: master/slave, four clock schemes, 16-word FIFOs,
  SPIRXINTA/SPITXINTA, loopback, SPI flash device), `mcbsp` (McBSP-A: sample rate generator, internal or pin clocks
  and frame syncs, 1/2-phase frames of 8-32-bit words, data delay, RJUST, companding, digital loopback, clock-stop SPI
  master/slave, MRINTA/MXINTA, DMA MXEVTA/MREVTA, pins edge by edge), `adc` (dual/cascaded sequencers, start/stop and continuous run, SEQ1/SEQ2
  arbitration, software / ePWM SOCA/SOCB / XINT2-pin starts, ADCCLK/ACQ_PS conversion timing, simultaneous sampling),
  `epwm` (ePWM1-6: time base incl. TBCTR above TBPRD and CTRMAX, compare, action qualifier, dead band, PWM chopper,
  trip zone (software and TZ1-TZ6 pins), event trigger with INT and SOCA/SOCB into the ADC sequencers, sync chain with
  the EPWMSYNCI/EPWMSYNCO pins, EPWMxA/B pins; HRPWM CMPAHR/TBPHSHR/HRCNFG MEP edge refinement on ePWM1-4A, exact
  sub-cycle edge times in `hr_state()`), `ecap` (eCAP1-4: capture of pin edges with prescaler, polarity,
  absolute/delta mode, continuous/one-shot, counter overflow, sync from ePWM1, APWM output, ECAPx_INT), `eqep`
  (eQEP1-2: quadrature / direction-count / up / down decoding with polarity, swap and index gating, phase error,
  position counter with QPOSMAX and the PCRM index / max / first-index / unit-time resets, index markers, index and
  strobe latch / init, position compare with shadow, unit timer with QPOSLAT latch, watchdog, capture unit for edge
  timing, EQEPx_INT), `dma` (6 channels triggered by the ADC sequencers, XINT, CPU timers, ePWM SOCA/SOCB or
  software; bursts, transfers and address wrap, 16/32-bit, one-shot / continuous ping-pong, DINTCHn interrupts, ADC
  sync, round robin and CH1 high priority, 1 + 4n-cycle bursts, DMA bus access rules), `i2c` (I2C-A on GPIO32/33: pin-level
  master/slave, FIFOs, I2CISRC, repeat mode, loopback), `ecan` (eCAN-A/B: 32 mailboxes, masks, remote frames, time
  stamps/time-outs, interrupts, error confinement on a `chipem.canbus` bus), `xintf` (XINTF registers and zones
  0/6/7 with a board `sram` device through the GPIO mux, XINTFENCLK gate, lead/active/trail and XBANK wait states and
  the write buffer charged to the CPU). EALLOW protection is enforced
  (an unprotected write is ignored with a warning). Unmodelled addresses in 0x0800-0x0FFF and 0x5000-0x7FFF become
  RAM-like stubs (`auto_stub_ranges` in the chip YAML).
- **Samples**: `firmware/samples_c2000/` (`build.sh` → `build/*.c28img`): `blinky` (GPIO34, 1 ms), `button_mirror`
  (GPIO12 → GPIO34), `timer_irq` (CPU timer 0 at 1 ms → PIE 1.7 ISR toggles GPIO34, main loop in `IDLE`),
  `sci_echo` (SCI-A on GPIO28/29, 115200), `uart_link_echo` / `uart_link_sender` (the K22 link protocol), `epwm`
  (ePWM1 20 kHz complementary pair with dead band on GPIO0/GPIO1), `ecap` (ePWM1A 20 kHz 30 % on GPIO0 measured by
  eCAP1 on GPIO24: `boards/c2000_ecap_loopback.yaml`), `eqep` (eQEP1 on GPIO20/21/23 decoding the encoder part of
  `boards/c2000_eqep_encoder.yaml`; the 100 Hz UTO ISR stores `eqep_pos`, `eqep_dir`, `eqep_ilat`, `eqep_cprd`,
  `eqep_count`), `spi_flash` (SPI-A + SPI NOR flash, `boards/c2000_spi_flash.yaml`), `mcbsp` (McBSP-A digital
  loopback fed and drained by DMA MXEVTA/MREVTA, waveforms on GPIO20-23), `xintf_sram` (`boards/c2000_xintf_sram.yaml`).
  TI-compiled C in `firmware/samples_c2000_c/` (`build.sh`, needs
  cl2000): `blinky`, `uart_chat`, `selftest`, `mcbsp_spi` (McBSP-A SPI master + flash, `boards/c2000_mcbsp_flash.yaml`), `can_echo` (eCAN-A echo, `boards/k22_c2000_can.yaml`), `epwm` (ePWM1 20 kHz 25 % on GPIO0 with EPWM1_INT and an ADC SOCA) and
  `adc_dma` (ePWM1 SOCA → ADC SEQ1 → DMA CH1 ping-pong buffer in L4, DINTCH1 swaps buffers, printed on SCI-A:
  `chipem run firmware/samples_c2000_c/build/adc_dma.out --chip tms320f28232 --for 20ms --adc ADCINA0=0x400`).
  ```sh
  chipem run firmware/samples_c2000/build/timer_irq.c28img --chip tms320f28232 --for 12ms
  chipem run firmware/samples_c2000/build/sci_echo.c28img --chip tms320f28232 --for 10ms --at '7ms:SCIA=hi\n'
  chipem run --board boards/c2000_k22_uart.yaml --for 20ms      # K22 sends "ping N", the F28232 echoes "PING N"
  chipem run --board boards/c2000_stm32_uart.yaml --for 20ms    # the F28232 sends, the STM32 echoes
  chipem info firmware/samples_c2000/build/timer_irq.c28img --chip tms320f28232
  ```

### Peripheral coverage

Modelled (behaviour beyond register storage):

| block(s) | model | what is modelled |
|---|---|---|
| SysTick, NVIC, SCB, FPU regs (0xE000E000) | `armv7m.scs` | SysTick on virtual time, NVIC enable/pend/active/priority, ICSR, VTOR, AIRCR (PRIGROUP, SYSRESETREQ), CCR.STKALIGN, SHPR, CFSR/HFSR/BFAR, STIR, FPCCR.ASPEN |
| SIM | `kinetis.sim` | SCGC1–7 clock gates (gated-off access logs a warning), CLKDIV1 feeding the clock tree, SDID/UID constants |
| MCG, OSC | `kinetis.mcg`, `kinetis.osc` | FEI/FEE/FBI/FBE/BLPI/BLPE/PBE/PEE clock tree, status mirrors control immediately; derives core and bus clock |
| PORTA–E | `kinetis.port` | PCR MUX → pin owner, PE/PS pulls, LK lock, GPCLR/GPCHR, IRQC edge/level interrupts, ISF/ISFR w1c, level-sensitive PORTx IRQ |
| GPIOA–E | `kinetis.gpio` | PDOR/PSOR/PCOR/PTOR/PDDR drive the pin board; PDIR from resolved pin levels |
| WDOG | `kinetis.wdog` | unlock and refresh sequences, write window, LPO or bus clock with prescaler, timeout → chip reset, RSTCNT |
| PIT | `kinetis.pit` | 4 channels, bus-clocked, live CVAL, TIF/TIE IRQs, chaining |
| LPTMR0 | `kinetis.lptmr` | time-counter mode with prescaler and clock select, compare IRQ (pulse-counter mode not modelled) |
| DMA, DMAMUX | `kinetis.dma`, `kinetis.dmamux` | 16-channel eDMA: TCDs, minor/major loops, linking, scatter/gather, DONE/INT/ERR, IRQs; DMAMUX request routing; transfers complete instantly |
| SDHC | `kinetis.sdhc` | SD host: SDCLK from SIM_SOPT2[SDHCSRC] and SYSCTL SDCLKFS/DVS, INITA, RSTA/RSTC/RSTD, XFERTYP commands (CC/CTOE/CCE/CIE, CMDRSP mapping, R1b busy), CPU data through DATPORT with RDWML/WRWML (BREN/BWEN/BRR/BWR), block count/auto CMD12, DTOE/DCE, clock gating instead of buffer overrun, simple DMA (DSADDR) and ADMA2 descriptors (DINT/DMAE/ADMAES), EMODE byte order, IRQSTATEN/IRQSIGEN/FEVT, CINS from the card device (`--sd-card SDHC`) |
| SPI0–2 | `kinetis.dspi` | master and slave, bit by bit on SCK/SOUT/SIN/PCSx at the CTAR baud rate (PBR/BR/DBR) with tCSC/tASC/tDT delays, CPOL/CPHA, LSBFE, PCSIS, continuous selection (CONT); PUSHR/POPR FIFOs, TCF/EOQF/TFUF/RFOF/RFDF/TFFF flags, EOQ stop, IRQ and DMA requests; slave clocked from the SCK/PCS0 pins; a responder (`--spi-flash`) can answer instead of SIN |
| I2S0 | `kinetis.sai` | SAI transmitter/receiver: MCLK divider (SYSCLK/OSC0ERCLK/MCGPLLCLK, FRACT/DIVIDE, DUF), bit clock from bus or MCLK, frame sync (width, early, polarity), word widths/FBT/MSB first, 8-word FIFOs per channel with watermark, FRF/FWF/FEF/SEF/WSF, word masks, RX/TX synchronous mode, master or slave on the pins, I2S0_Tx/I2S0_Rx IRQs, DMA requests (13/12); `i2s` log events per 64 frames; `i2s_codec` board part |
| CMT | `kinetis.cmt` | carrier generator (CGHx/CGLx, PPS/CMTDIV), modulator mark/space (time, baseband, FSK, extended space), CMT_IRO pin with IROPEN/IROL/CMTPOL, EOCF with its read-MSC-then-CMD clear, CMT IRQ or DMA request (source 47) |
| MCM, FMC | `kinetis.mcm`, `kinetis.fmc` | MCM: PLASC/PLAMC, CR (SRAM write protect warns), ISCR FPU exception flags (from FPSCR) + MCM IRQ, ETB reload; FMC: PFAPR/PFBxCR resets, cache tag/data registers, way invalidation |
| AIPS0/1, AXBS, MPU | `kinetis.aips`, `kinetis.axbs`, `kinetis.mpu` | register files with RM reset values: AXBS priority checks and RO lock, MPU region descriptors (RGD0 restrictions, VLD clearing, RGDAAC aliases); protection is not enforced (one `warn`) |
| FB, FlexBus space | `kinetis.flexbus`, `kinetis.flexbus_space` | chip selects CSAR/CSMR/CSCR 0-5 (global FB_CS0 out of reset, write protect, port size) decoding 0x60000000–0xDFFFFFFF to board memories (`devices: [{type: sram, chip: A, bus: FB}]`) |
| I2C0–2 | `kinetis.i2c` | pin-level bus (chipem/periph/i2cwire.py): F (MULT x ICR Table 48-54) bit timing, START/repeated START/STOP, D read-ahead receive, TXAK, RXAK, TCF/IICIF/BUSY/ARBL, slave mode (A1, general call, range, A2) with SCL stretching until D is accessed, STARTF/STOPF, IRQ and DMA requests (DMAMUX 18/19); PCR.ODE = 0 makes the pins push-pull |
| CAN0 | `kinetis.flexcan` | FlexCAN with 16 MBs on a `chipem.canbus` bus (real frame time at the CTRL1 bit rate, arbitration, ACK): disable/freeze modes, Tx/Rx MB CODE state machines, RXMGMASK/RX14/RX15/RXIMR masks, lock mechanism, remote frames (RANSWER/TANSWER), AEN abort, Rx FIFO (formats A/B/C), TIMER time stamps, ECR/ESR1 error confinement, LOM, loop-back, ORed MB / bus-off / error / warning IRQs; CAN0_TX/RX pins must be muxed |
| UART0–4 | `kinetis.uart` | TX capture, TX/RX data buffers (FIFOs: `params.fifo_depth`, 8 on UART0/1, 1 on UART2–4; used when PFIFO.TXFE/RXFE are set), no input queue (bytes arriving while the receiver is off or full are dropped), baud-rate pacing of TDRE/TC/RDRF, RX/TX/error IRQs, DMA requests |
| RTC | `kinetis.rtc` | seconds counter and prescaler on virtual time, alarm, lock registers, survives system reset |
| ADC0, ADC1 | `kinetis.adc` | instant calibration, software-triggered conversions (4 µs default), per-channel values, COCO IRQ, DMA request; hardware triggers from the PDB or, via SIM_SOPT7, from FTM0–3 / CMP0–2 / PDB0_EXTRG; ADC0_SE23 = DAC0, ADC1_SE18 = VREF_OUT |
| PDB0 | `kinetis.pdb` | software- or hardware-triggered (PDB0_EXTRG, CMP, PIT, FTM, RTC, LPTMR) counter with prescaler/MULT, continuous and one-shot, buffered MOD/IDLY/DLY with LDOK, channel pre-triggers (delayed, back-to-back) into ADC0/ADC1 hardware triggers, sequence errors, IDLY interrupt/DMA, DAC interval trigger into DAC0 |
| FTFE | `kinetis.ftfe` | synchronous command model (read/program/erase/resource/once/partition commands), flash config field → FSEC/FOPT/FPROT |
| SMC, PMC, RCM, LLWU | `kinetis.smc/pmc/rcm/llwu` | reset values, PMSTAT=RUN, RCM reports the last reset cause, w1c flags |
| VBAT register file | `kinetis.vbat` | 32 bytes of RAM that survives chip reset |
| CRC | `kinetis.crc` | 16/32-bit CRC engine, GPOLY, WAS seed protocol, TOT/TOTR transposes, FXOR; 8/16/32-bit data writes |
| DAC0 | `kinetis.dac` (+ alias at 0x4003F000) | 12-bit output (VDDA or VREF_OUT reference) as `voltage()` and `info` events, 16-word buffer (normal/swing/one-time scan), software trigger or PDB DAC interval trigger, SR flags, IRQ or DMA request |
| CMP0–2 | `kinetis.cmp` | 8-input mux (pins in mV via `--adc CMPx_INn=`, DAC0, VREF_OUT, bandgap, 6-bit DAC), hysteresis, INV/COS, bus-clock filter delay, CFR/CFF flags, IRQ or DMA request, CMPx_OUT pin |
| VREF | `kinetis.vref` | VREFEN/MODE_LV, VREFST after start-up, 1.2 V output (TRIM) for DAC/CMP/ADC |
| EWM | `kinetis.ewm` | write-once config, 0xB4/0x2C service, CMPL/CMPH window on the 1 kHz LPO, EWM_in, sticky EWM_OUT_b pin, WDOG_EWM IRQ |
| RFSYS | `ram` | plain storage |
| STM32 RCC | `stm32.rcc` | HSI/HSE/PLL/SYSCLK switch with ready bits mirrored, HPRE/PPRE1/PPRE2 → core/APB/timer clocks, AHB/APB clock gates (gated-off access warns), xxRSTR peripheral resets, CSR reset flags |
| STM32 GPIOA–I | `stm32.gpio` | MODER/OTYPER/PUPDR/AFRL/AFRH → pin owner (AF function names from the pin table), pulls, open-drain; ODR/BSRR drive the pin board; IDR from resolved pin levels; LCKR lock sequence; SWD/JTAG pins owned by AF0 after reset |
| STM32 SYSCFG, EXTI | `stm32.syscfg`, `stm32.exti` | EXTICR port selection; rising/falling edge detection, SWIER, PR w1c, level IRQs EXTI0–4, EXTI9_5, EXTI15_10 (+ lines 16–22 via SWIER) |
| STM32 USART1–3/6, UART4/5 | `stm32.usart` | BRR baud (OVER8), M/PCE/STOP framing, TX capture, RX queue, character-time pacing of TXE/TC/RXNE, IDLE, rc_w0 flags, TXE/TC/RXNE/IDLE IRQ, CR3.DMAT/DMAR DMA requests; board-link endpoint |
| STM32 DMA1, DMA2 | `stm32.dma` | 8 streams, RM0090 Table 42/43 request mapping (CHSEL), P2M/M2P/M2M (DMA2), direct and FIFO mode (packing, FTH), circular and double buffer (CT), PL arbitration, NDTR, TC/HT/TE/FE flags with LIFCR/HIFCR, per-stream IRQs, TEIF on bus errors; transfers take zero bus time; request API `_util.dma_request(chip, name, level)` |
| STM32 TIM1–14 | `stm32.tim` | time base: PSC/ARR preload, up/down/center-aligned counting, live CNT, UG, OPM, URS/UDIS, RCR, UIF/UIE IRQ (TIM2/5 32-bit; TIM9–14 up-only with 1–2 channels); output compare and PWM1/PWM2 (CCRx preload, CCxIF/CCxIE, CCxP/CCxNP, TIM1/8 complementary outputs with MOE/OSSI/OSSR, dead time, break input/BG/AOE) driving TIMx_CHn / TIMx_CHnN pins; input capture from pin edges (direct/indirect, polarity, prescaler, overcapture, TRC, TI1S xor), encoder mode; master mode TRGO (reset/enable/update/compare pulse/OCxREF) and the slave mode controller (reset, gated, trigger, external clock mode 1) on ITR0–3 / TI1F_ED / TI1FP1 / TI2FP2 / ETRF, external clock mode 2 (ETR pin, ETP/ETPS); DMA requests UP/CHx/TRIG; no input filters |
| STM32 ADC1–3, C_ADC | `stm32.adc`, `stm32.adc_common` | regular group: SWSTART and Table 51 timer triggers (TRGO / CCx, EXTEN edges), SQR sequence with SCAN/CONT/DISCEN, SMPx + RES timing at ADCCLK = PCLK2/ADCPRE (event per conversion), RES/ALIGN, EOC/EOCS, OVR, analog watchdog, shared `ADC` IRQ, DMA request per conversion, VREFINT/temperature/VBAT channels; values in volts per pin (`--adc PA0=1.65V`); injected group (JSQR, JSWSTART, Table 52 timer triggers, JOFRx, JDRx, JEOC, JAUTO, JDISCEN, injected preempting regular); multi-ADC modes (CCR.MULTI regular/injected simultaneous, interleaved with DELAY, alternate trigger, dual and triple; CDR with DMA modes 1–3) |
| Kinetis FTM0–3 | `kinetis.ftm` (+ `kinetis.ftm_alias` for FTM2 at 0x4003A000) | 16-bit counter (bus or MCGFFCLK, prescaler, CNTIN/MOD, up and up-down, free running), TOF/CHnF IRQs, output compare, edge- and center-aligned PWM, combine with dead time, SWOCTRL/POL/OUTMASK/OUTINIT, software sync and LDOK, driving FTMx_CHn pins; input capture and dual edge capture from pin edges, fault inputs, quadrature decoder (FTM1/2), external clock (FTM_CLKINx), write protection, EXTTRIG triggers into PDB0, channel DMA requests; no input filters, INVCTRL or hardware sync triggers |
| STM32 IWDG, WWDG | `stm32.iwdg`, `stm32.wwdg` | IWDG keys 0xCCCC/0xAAAA/0x5555, LSI 32 kHz prescaler/reload, timeout → chip reset + CSR.IWDGRSTF; WWDG 7-bit counter at PCLK1/4096/2^WDGTB, window (W) and T6 resets + CSR.WWDGRSTF, EWI interrupt at 0x40 |
| STM32 CAN1, CAN2 | `stm32.can` | bxCAN on a `chipem.canbus` bus: sleep/init/normal handshakes (INRQ/INAK, SLEEP/SLAK, 11-bit bus integration, AWUM wake-up), BTR bit timing from PCLK1, LBKM/SILM, 3 TX mailboxes (ID or request-order priority, CODE/LOW/TME, ABRQ, NART, TXOK/ALST/TERR), two 3-deep RX FIFOs (FMP/FULL/FOVR, RFLM, RFOM), 28 shared filter banks in CAN1 (CAN2SB, mask/list, 16/32 bit, FFA1R, FMI), ESR TEC/REC/LEC/EWGF/EPVF/BOFF, bus-off recovery (ABOM or the INRQ sequence), TTCM time stamps, TX/RX0/RX1/SCE IRQs; CANx_TX/RX pins must be muxed, CAN2 needs the CAN1 clock |
| STM32 I2C1–3 | `stm32.i2c` | pin-level bus: CCR/DUTY SM/FM timing from PCLK1, START/repeated START/STOP after the byte in progress, SR1/SR2 with the RM0090 clear sequences (SB, ADDR, BTF, STOPF, rc_w0 errors), ACK/POS (HAL 1/2/N-byte reads), BTF clock stretching, AF on NACK, slave mode (OAR1/OAR2 dual, general call, ADDR/BTF stretching, AF, STOPF, NOSTRETCH), I2Cx_EV/I2Cx_ER IRQs, DMA I2Cx_TX/RX with LAST; ST's HAL I2C works |
| STM32 SPI1–3 | `stm32.spi` | SPI mode, bit level on the pins: master SCK/MOSI/NSS (SSOE) waveforms per CPOL/CPHA/LSBFIRST/DFF at PCLK/2^(BR+1), MISO sampled from the pin or a responder (`--spi-flash SPI1:cs=PA4`); slave mode on SCK/NSS pin edges (STM32↔STM32 over nets); double buffering, TXE/RXNE/BSY/OVR/MODF, CRC (CRCPR, CRCNEXT, CRCERR), RXONLY/BIDIMODE master, IRQ, DMA requests SPIx_TX/RX; no I2S |
| STM32 SDIO | `stm32.sdio` | SD host: SDIO_CK from PLL48CLK (CLKDIV/BYPASS/CLKEN/PWRSAV, WIDBUS 1/4), CPSM (CMDSENT/CMDREND/CCRCFAIL incl. R3/CTIMEOUT after 64 clocks, RESPCMD/RESP1-4), DPSM (DTIMER/DLEN/DCTRL, DCOUNT/FIFOCNT, block CRC checks, DBCKEND/DATAEND/DCRCFAIL/DTIMEOUT/TXUNDERR/RXOVERR, HWFC_EN), 32-word FIFO with all STA flags, ICR/MASK, SDIO IRQ, DMA request `SDIO`; a card device (`--sd-card SDIO`) answers; ST's HAL SD driver works |
| STM32 FSMC | `stm32.fsmc` | BCR/BTR/BWTR 1-4 (RM0090 reset values), NAND/PC Card registers as storage; NE1-NE4 64 MB windows at 0x60000000 open while MBKEN and the FSMC clock are set (bus fault otherwise), board SRAMs mirrored across the window at full speed, WREN read-only windows; no access timing |
| STM32 CRC, RNG | `stm32.crc`, `stm32.rng` | CRC-32 (0x04C11DB7) on 32-bit words, RESET, IDR; RNG: a value 40 PLL48CLK after enable / DR read, clock error CECS/CEIS, seeded deterministic values, HASH_RNG IRQ |
| STM32 DAC | `stm32.dac` | 2 channels, DHR8/12 R/L/dual → DOR (1 APB1 cycle, 3 after a TIM6/8/7/5/2/4 TRGO trigger, SWTRIG), noise/triangle waves, DMA requests with underrun, `voltage()` + info events, DAC_OUT1/2 pin owner and the voltage on PA4/PA5 for the ADC |
| STM32 RTC | `stm32.rtc` | BCD calendar from LSE/LSI/HSE with PRER, SSR, shadow registers/RSF, WPR protection, PWR DBP, INIT mode, alarms A/B (EXTI17 → RTC_Alarm), wakeup timer (EXTI22 → RTC_WKUP), BKP0R–19R; backup domain kept across system resets, BDRST clears it; no tamper/timestamp/calibration |
| STM32 FLASH, PWR, DBGMCU | `stm32.flash`, `stm32.pwr`, `stm32.dbgmcu` | ACR latency readback, KEYR unlock/CR.LOCK, sector/mass erase; VOSRDY=1; IDCODE 0x413 |

Stubs (RAM-like storage, first access to each word logged): AIPS0/1, AXBS, FB, MPU, FMC, I2S0, CRC,
USBDCD, EWM, CMT, USB0, CMP, VREF, DAC0, MCM (STM32: see below). Any
access to an unlisted address in 0x40000000–0x400FFFFF or 0xE0000000–0xE00FFFFF (DWT, ITM, FPB, TPIU, ...)
creates a 4 KB RAM-like stub on first touch and logs `access to unmodelled region`. Other unmapped addresses
bus-fault.

STM32F405ZG stubs: I2S2ext/I2S3ext (and I2S mode of SPI2/3), USB OTG FS/HS, and (F407/F415/F417-only blocks, kept so family code does not fault) Ethernet, DCMI, CRYP, HASH.
SysTick `CLKSOURCE=0` counts at core clock / `systick_ext_div` (chip YAML, default 8 = STM32 HCLK/8). The K22
YAMLs do not set it, so Kinetis also uses /8 (not confirmed against the K22 reference manual).

The `model:` of each block in `chips/mk22fn1m0vll12.yaml` is the authoritative list. Board-level devices that
hang off a peripheral live in `chipem/devices/` (`spi_flash.py`, `sdcard.py`, `sram.py`).

### Adding a chip with an AI assistant

[docs/AI_NEW_CHIP_PLAYBOOK.md](docs/AI_NEW_CHIP_PLAYBOOK.md) is a step-by-step procedure, written for an AI agent,
that turns a data sheet PDF (plus an optional SVD, reference manual or firmware) into a tested chip description. It
draws on how the K22, STM32F405 and F28232 were added. In Claude Code, run
`/new-chip <datasheet.pdf> [--svd URL|path] [--part NAME]` (`.claude/skills/new-chip/`) and, to wire chips together,
`/new-board`. Two helper tools support it:

```sh
.venv/bin/python -I tools/new_chip.py --part STM32F405ZG --package LQFP144 --family stm32 --fpu \
    --svd svd/dl-stm32f405-Ij9M/STM32F405.svd --root /tmp/scaffold   # chip YAML + regs + test skeleton, models mapped per family
.venv/bin/python -I tools/pin_table_check.py chips/stm32f405zg.yaml   # pin count, uniqueness, port/bit, naming, kinds
```

## The example Withings image

`s1_off000084_503080.bin` is a 503,080-byte application image from a Withings WBS05 scale. It references
FlexNVM, so use the FX512 description. It was linked at 0x4000 behind a bootloader that is not in the file.
`load_image` scores candidate load addresses by counting vector-table entries that point at function
prologues, and picks 0x4000:

```
$ .venv/bin/chipem run s1_off000084_503080.bin --chip mk22fx512vll12 --for 2s --summary-only
note: auto-detected load address 0x4000 (vector scores 0x0:2, 0x1000:0, 0x2000:4, 0x4000:26, 0x8000:2, 0x10000:2); override with --load-addr

=== summary ===
virtual time : 2 s (now 2 s), core clock 23.9862 MHz
cycles       : 47,971,655 (virtual core cycles, incl. time slept in WFI)
instructions : 47,971,655 executed (1 cycle/insn model)
wall time    : 0.517 s, 92.86 M insn/s, 3.87x real time
stop         : COUNT; pc=0x00019D4C mode=handler (HardFault)
last fault   : undefined_instruction pc=0x0003AE1C

pins configured by firmware (16):
  pin    num  owner          dir    lvl pull  ext
  PTE24   31  GPIO           out    L   -     -
  PTA5    39  GPIO           out    H   -     -
  PTA12   42  GPIO           out    L   -     -
  PTA13   43  GPIO           in     X   -     -
  PTA14   44  GPIO           out    L   -     -
  PTA16   46  GPIO           out    L   -     -
  PTB3    56  GPIO           out    H   -     -
  PTB11   59  GPIO           out    H   -     -
  PTB19   65  GPIO           out    L   -     -
  PTC4    76  GPIO           in     X   -     -
  PTC8    80  GPIO           out    H   -     -
  PTC9    81  GPIO           out    L   -     -
  PTC13   85  GPIO           in     X   -     -
  PTC14   86  GPIO           out    L   -     -
  PTD2    95  UART2_RX       in     X   -     -
  PTD3    96  UART2_TX       out    H   -     -

UART output:
  UART2 (1656 bytes): "\r\n ------> ENTERING MAINTASK <-------\r\nWDOG_RSTCNT=0\r\n[BAT] Use default cal\r\n[BAT] 0 mv (0 %) (MIN 4450 MAX 6000) -> State <\x04>\r\n[Hard fault handler: 0x1fff3918]\r\nR0   = 00000001 ..."

exceptions taken:
  exc 3 HardFault                 3
  exc 11 SVCall                   3
  exc 14 PendSV                  18
  exc 15 SysTick                 12
  IRQ0 DMA0                       9

warnings/faults (3):
  undefined_instruction at pc=0x0003ae1c → HardFault handler  (x3)
```

For a raw `.bin`, VTOR at reset is the load address. The 0x0–0x3FFF region is erased flash (0xFF).

In its first 21 ms the image:

- reconfigures the watchdog (5 s on the LPO) and refreshes it;
- moves the clock to FEI with DMX32 (about 24 MHz) and calibrates ADC0;
- configures the GPIOs above and UART2 on PTD2/PTD3;
- starts its RTOS: one SVC, PendSV context switches between tasks on PSP (saving FP registers; it checks
  EXC_RETURN bit 4), and a 1 kHz SysTick;
- prints the banner on UART2 at about 17 ms and a battery reading at about 20 ms.

Then, on a bare board, it faults and resets itself about every 0.69 s:

```
$ .venv/bin/chipem run s1_off000084_503080.bin --chip mk22fx512vll12 --for 2s --events reset,fault
[     0.000 ms] RESET reset
[    21.510 ms] FAULT undefined_instruction at pc=0x0003ae1c → HardFault handler
[   692.914 ms] RESET SYSRESETREQ
[   714.425 ms] FAULT undefined_instruction at pc=0x0003ae1c → HardFault handler
[  1385.837 ms] RESET SYSRESETREQ
[  1407.347 ms] FAULT undefined_instruction at pc=0x0003ae1c → HardFault handler
```

At about 20 ms it measures the battery: PDB0 periodically hardware-triggers ADC0 channel 18 (PTE25) and DMA
channel 0 collects five 16-bit samples. The reading is linear, about 0.141 mV per count, so
`--adc ADC0_SE18=40000` gives "5640 mv (77 %) State OK" (4450–6000 mV is roughly 31,600–42,500 counts); without
`--adc` it reads 0 mV. Right after that it sends a JEDEC "read ID" (0x9F) to an external SPI NOR flash on
SPI0 (chip select PTC4). With no device attached MISO reads all ones, the firmware reaches a deliberate
`udf #0`, its HardFault handler dumps the registers on UART2 and it requests a reset at about 0.69 s, and
the cycle repeats. That is faithful to a bare board.

Attach a flash and hold the UART2 receive pin idle-high (the firmware only starts its console when PTD2
reads high, "UART IS CONNECTED") and the console works. Type after the console is up (`shell>` at about 35 ms):
bytes sent from t=0 with `--uart-in` would be dropped while UART2 is still gated off or busy booting:

```
$ .venv/bin/chipem run s1_off000084_503080.bin --chip mk22fx512vll12 --for 5s \
      --spi-flash SPI0:cs=PTC4 --set PTD2=1 --adc ADC0_SE18=40000 --at '100ms:UART2=help\r\n' --summary-only
...
UART output:
  UART2 (1181 bytes): "... UART IS CONNECTED ... shell>help\r\n\r\naccount_secret\r\nadc\r\nbacklight\r\nbkp\r\nboot_trace\r\nbt\r\ncharger\r\n ... gpio\r\ngpio_dac\r\n ... z_sin\r\nrc=0\r\nshell>"
exceptions taken:
  exc 11 SVCall                   1
  exc 14 PendSV                  13
  exc 15 SysTick               4980
  IRQ0 DMA0                       3
  IRQ35 UART2_RX_TX               6
```

The firmware's ID table only accepts Micron/ST (0x2020xx), Atmel (0x1f45xx/0x1f46xx) and SST (0xbf2541)
parts, which is why the default flash identifies as an M25P16. UART2 receive is interrupt driven, not DMA:
the driver enables IRQ 35 only after the "connected" check. The `gpio set/get` console commands can now be
used to poke pins from inside the firmware. See [docs/LIMITATIONS.md](docs/LIMITATIONS.md) for what is
still approximate.

## Discovering pin usage from unknown firmware

With firmware but no schematic, `--pin-report` (on `run`; `pinreport` in the shell, the **Discovery** tab in
`serve`) tells you what the code did with every pin while it ran, and which code did it:

```
$ .venv/bin/chipem run firmware/samples/build/button_mirror.elf --for 3ms --at 1ms:PTC6=0 --at 2ms:PTC6=1 --pin-report
...
Pin report after 3.000 ms of virtual time (2 pins touched by firmware; PCs resolved via ELF symbols)
firmware: firmware/samples/build/button_mirror.elf

pin       #  final role                            config@  configured by           reads writes    chg  irqs  static        notes
----------------------------------------------------------------------------------------------------------------------------------
PTC5     77  GPIO out                               2.3 us  main+0x38                   0    13k      2     0  confirmed
PTC6     78  GPIO in (pull-up)                      2.1 us  main+0x2e                 13k      0      0     0  confirmed

Discovery summary
* Outputs the firmware drives:
    - PTC5: 2 level changes from 12573 writes; driven by main+0x48, main+0x44, now L
* Inputs it polls:
    - PTC6: 12573 PDIR reads (~4.19 M/s) by main+0x3c, pull-up
* Configuration code:
    - main: 2 pins (PTC5, PTC6)
* Static analysis (chipem info) vs. this run:
    - confirmed static guess: 2 (PTC5, PTC6)
    - not predicted statically: 0
    - predicted but never configured: 0
* Only code paths that ran are covered: drive inputs (--set/--at), feed UARTs (--uart-in) or run longer to exercise more of the firmware.
```

How it works: the PORT and GPIO models report every pin configuration change (PCR mux/pull/IRQC, PDDR
direction) together with the PC of the store that made it. Inside an MMIO callback Unicorn's PC is the
accessing instruction itself, in fast and precise mode alike. ELF images resolve PCs to `symbol+offset`.
Raw images show `sub_XXXX+off`, where `sub_XXXX` is a guessed function start: the nearest `bl` target or
`push {..., lr}` at or below the PC. Hot paths only bump counters, such as PDIR reads per port and
PSOR/PCOR/PTOR writes per value. These are attributed to pins when the report is built, which costs about 4 %
on a tight PDIR polling loop. The static guess from `chipem info` is added as a column, so every pin the run
found is marked `confirmed` (the scan predicted it) or `not predicted`. Pins the scan predicted but the run
never configured are listed in the summary. `chipem shell`'s `pin NAME` prints one pin's full history:

```
$ .venv/bin/chipem shell firmware/samples/build/port_irq.elf --exec "at 1ms PTC6 0; run 2ms; pin PTC6"
...
  firmware activity:
  PTC6: GPIO in (pull-up, IRQ falling)  reads=0 writes=0 level-changes=0 irqs=1
          2.5 us  main+0x2c                    mux ALT1 (GPIO), pull up, irq falling edge
    control registers (PCR/PDDR): first main+0x2c, last main+0x2c
    interrupt flag cleared by PORTC_IRQHandler+0xc
```

On the Withings image (trimmed), 19 pins are found and attributed. The table shows a pin-configuration
function and a GPIO write function in the raw image. It also shows the SPI flash pins being muxed in and out
around every transfer, UART2 on PTD2/PTD3, and PTA13 as a "logic 0" interrupt input that floats on a bare
board:

```
$ .venv/bin/chipem run s1_off000084_503080.bin --chip mk22fx512vll12 --for 3s --spi-flash SPI0:cs=PTC4 --set PTD2=1 --pin-report
...
Pin report after 3.000 s of virtual time (19 pins touched by firmware;
  no symbols: sub_XXXX = heuristic function start, the nearest bl target / push {lr} below the PC)

pin       #  final role                            config@  configured by           reads writes    chg  irqs  static        notes
----------------------------------------------------------------------------------------------------------------------------------
PTA5     39  GPIO out                             113.8 us  sub_1BC64+0x60              0      1      0     0  not predicted
PTA13    43  GPIO in (IRQ logic 0)                 85.4 us  sub_19170+0x36              0      0      0     1  not predicted FLOATING when read
PTA14    44  GPIO out                              89.3 us  sub_1BC64+0x60              0      4      2     0  not predicted
PTC4     76  GPIO in                             34.069 ms  sub_1BC64+0x60              0    381    328     0  not predicted output released to input between uses; reconfigured dir 105x
PTC5     77  disabled                            34.062 ms  sub_54EA4+0x72              0      0      0     0  not predicted reconfigured mux 105x; also SPI0_SCK (53x)
PTC13    85  GPIO in                             15.421 ms  sub_1BC64+0x28              0      0      0     0  not predicted never read
PTD2     95  UART2_RX                            34.414 ms  sub_5527A+0x58              1      0      0     0  not predicted reconfigured mux 2x, pull 1x; also GPIO (1x)
PTD3     96  UART2_TX                            34.414 ms  sub_5527A+0x5e              0      1      0     0  not predicted reconfigured mux 2x; also GPIO (1x)
...

Discovery summary
* Outputs the firmware drives:
    - PTE24: 0 level changes from 2 writes; driven by sub_1BC3C+0x1e, now L
    - PTA5: 0 level changes from 1 write; driven by sub_1BC3C+0x1a, now H
    - PTA14: 2 level changes from 4 writes; driven by sub_1BC3C+0x1e, sub_1BC3C+0x1a, now L
    - PTA16: 2 level changes from 4 writes; driven by sub_1BC3C+0x1e, sub_1BC3C+0x1a, now L
    - PTC4: 328 level changes from 381 writes, irregular (median period 35.0 us); driven by sub_1BC3C+0x1a, sub_1BC3C+0x1e (released to input between uses, e.g. a chip select), now X
    - PTC8: 1 level change from 3 writes; driven by sub_1BC3C+0x1e, sub_1BC3C+0x1a, now H
* Inputs with interrupts:
    - PTA13: IRQ logic 0, 1 interrupt flag set (level reads X: counts as 0)
* Peripheral-owned pins:
    - SPI0: PTC5 SCK (muxed 53x, now disabled), PTC6 SOUT (muxed 53x, now CMP0_IN0), PTC7 SIN (muxed 53x, now CMP0_IN1)
    - UART2: PTD2 RX, PTD3 TX
* Configured but unused so far:
    - PTC13 (GPIO in; never read)
* Floating inputs:
    - PTA13: sampled while floating (no pull, nothing connected; PDIR and the interrupt logic see 0); try --set PTA13=1 or --set PTA13=0
* Configuration code:
    - sub_1BC64: 16 pins (PTE24, PTA5, PTA12, PTA13, PTA14, PTA16, PTB3, PTB11, PTB19, PTC4, PTC8, PTC9, PTC13, PTC14, PTD2, PTD3)
    - sub_1CFB8: 3 pins (PTC5, PTC6, PTC7)
    - sub_54EA4: 3 pins (PTC5, PTC6, PTC7)
    - sub_550CE: 2 pins (PTD2, PTD3)
    - sub_5527A: 2 pins (PTD2, PTD3)
    - sub_19170: 1 pin (PTA13)
* Static analysis (chipem info) vs. this run:
    - confirmed static guess: 0
    - not predicted statically: 19 (PTA5, PTA12, PTA13, PTA14, PTA16, PTB3, PTB11, PTB19, PTC4, PTC5, PTC6, PTC7, PTC8, PTC9, PTC13, PTC14, PTD2, PTD3, PTE24) - pins reached through tables/function arguments defeat the static scan
    - predicted but never configured: 1 (PTE26) - code not exercised in this run, or a misread store
* Only code paths that ran are covered: drive inputs (--set/--at), feed UARTs (--uart-in) or run longer to exercise more of the firmware.
```

Static analysis predicted none of these pins. The driver takes the port and pin as function arguments
(`sub_1BC64(pin_desc, mode, ...)` stores to `PCR[r6]`), so no constant PCR address ever appears in the code.
This is where the dynamic view helps. It has limits:

- **Only code that ran is covered.** A pin used only by the "button pressed" path, or by a console command,
  stays invisible until you exercise that path. Use `--set`/`--at` to drive inputs, `--uart-in` to type
  commands, `--adc` for analog values, or simply run longer.
- **Roles are what the firmware configured, not what is on the board.** An input the firmware never pulls
  and that reads `X` is probably driven by something external. The report suggests trying `--set PIN=1`
  and `--set PIN=0`.
- **Peripheral-owned pins** (UART, SPI) are named from the mux setting. The traffic itself is visible in
  the UART output and the SPI device models, not as GPIO counts.
- On raw images `sub_XXXX` is a heuristic. Leaf functions reached only by pointer or tail call can be
  attributed to the function before them.

## Project layout

```
chipem/
  cli.py              argument parsing; `info`; lazy-loads cmd_run / cmd_shell / cmd_serve
  session.py          Session: chip(s) + firmware + stimulus + VCD, shared by run/shell/serve
  board.py            multi-chip boards: board YAML, lockstep runner, nets, UART links
  cmd_run.py          `chipem run` (live printer, summary)
  cmd_shell.py        `chipem shell` (cmd.Cmd REPL)
  cmd_serve.py        `chipem serve`
  web/server.py       worker thread + stdlib HTTP/SSE server; web/static/ = index.html, app.js, package.js, board.js, parts.js,
                      style.css (theme tokens), layout.css, board.css
  web/evfmt.py        event row text shared by the browser view, /api/events?q= and the .txt download
  chip.py             Chip: assembles clock, scheduler, pins, bus, core, arch, peripherals; the slice loop
  chipdesc.py         ChipDescription dataclasses + YAML loader (chips/*.yaml, `include:`)
  pins.py             generic Pin / PinBoard (no vendor knowledge)
  bus.py              memory regions, MMIO mapping, sub-page dispatch, catch-all stubs, bit-band aliases
  time.py             Clock (cycles <-> ns, runtime frequency changes) and Scheduler (event heap)
  log.py              EventLog: the single stream of pin/mmio/irq/uart/reset/fault/info/warn events
  regtable.py         loader for register tables (chips/*_regs.yaml; SVD-generated or hand-written, unit-aware)
  firmware.py         .bin/.hex/.srec/.elf/COFF/.c28img loaders, load-address guessing, static analysis
  pinreport.py        pin discovery report (`--pin-report`, shell `pinreport`, web Discovery tab)
  symbols.py          address -> symbol (ELF symbols, .c28img symbols, raw-Thumb heuristic)
  trace.py            VCD writer, live event printer
  core/base.py        Core interface (execution only) and StopReason
  core/unicorn_cm.py  Cortex-M core on Unicorn
  core/c28x/          TI C28x core: pure-Python interpreter, assembler/disassembler (table: arch/c28x/isa.yaml)
  arch/armv7m/        exception model (exceptions.py) and System Control Space (scs.py)
  arch/c28x/          C28x interrupt architecture: PIE (pie.py), CPU vectors, interrupt entry (arch.py)
  periph/base.py      Reg / MMIOBlock
  periph/registry.py  model-name registry, `stub` and `ram` models
  periph/kinetis/     Kinetis models (sim, mcg, port, gpio, wdog, pit, lptmr, uart, dspi, dma, rtc, adc, pdb, ftm, ftfe, power,
                      crc, dac, cmp, vref, ewm)
  periph/stm32/       STM32F4 models (rcc, gpio, exti/syscfg, usart, tim, dma, system: iwdg/wwdg/flash/pwr/dbgmcu)
  periph/ti_c2000/    TI C2000 models (sysctrl, gpio, xint, cputimer, sci, adc, epwm, ecap, eqep, misc: flash/csm/spi/storage)
chips/                chip descriptions (hand-written) and register tables (generated); README.md
boards/               board descriptions (several chips + nets/links), e.g. two_k22_uart.yaml
tools/svd2chip.py     CMSIS-SVD → chips/<family>_regs.yaml; --check compares a chip YAML with the SVD
svd/                  cached SVD and vendor pin-data downloads (untrusted data)
firmware/samples/     Kinetis sample C programs, startup, linker script, build.sh, build/
firmware/samples_stm32/  STM32 samples (C, clang); firmware/samples_c2000/  C28x samples (assembly, tools/c28x_asm.py)
firmware/samples_stm32_variants/  STM32F405/F407 package-variant samples (port walk, two-chip UART link)
firmware/samples_stm32_hal/  STM32 sample on ST's STM32CubeF4 HAL; firmware/vendor/  fetched vendor sources (HAL, CMSIS)
tests/                pytest suite
docs/                 design and reference documents
```

## Tests

```sh
.venv/bin/python -m pytest -q          # about 1800 tests, about 70 s
```

The suite covers the pin model, MMIO sub-word semantics, the bus, virtual time, the exception model
(priorities, BASEPRI/PRIMASK, SVC/PendSV, FP frames, EXC_RETURN forms), every Kinetis model, the SVD
importer, the loaders, each sample firmware end to end, `chipem run`, the shell, and the web API, plus the STM32
models (`test_stm32*`, package variants in `test_stm32_variants`), the C2000 chip/models/samples (`test_c2000_*`), the C28x core and assembler (`test_c28x_*`),
16-bit address units (`test_engine_units`) and the cross-family boards (`test_integration_families`). Sample
tests are skipped if the `build/` outputs are missing.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): slice loop and virtual time, exception model, MMIO framework, pins, Session and web worker.
- [docs/ADDING_A_CHIP.md](docs/ADDING_A_CHIP.md): adding another Cortex-M3/M4 chip, a new vendor family, or a peripheral model.
- [docs/AI_NEW_CHIP_PLAYBOOK.md](docs/AI_NEW_CHIP_PLAYBOOK.md): procedure for an AI agent adding a chip from a data sheet (`/new-chip`, `tools/new_chip.py`, `tools/pin_table_check.py`).
- [docs/LIMITATIONS.md](docs/LIMITATIONS.md): what is approximate or missing.
- [docs/C2000_ROADMAP.md](docs/C2000_ROADMAP.md): TI C2000 (TMS320F28232) status, design notes and remaining work.
- [docs/C28X_CORE.md](docs/C28X_CORE.md): the C28x interpreter, its ISA table and its verification against TI's cl2000 25.11.1 tools.
- [docs/PLAN.md](docs/PLAN.md): original design plan and findings from analysing the Withings boot path.
- [docs/CONTRACTS.md](docs/CONTRACTS.md): module interface contracts.
- [chips/README.md](chips/README.md): chip/register file formats, svd2chip usage, SVD provenance.
