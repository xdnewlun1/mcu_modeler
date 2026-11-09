# AI playbook: from a data sheet to a tested CHIP_EM chip

These are directions for an AI agent (Claude Code or another) that is given a microcontroller data sheet PDF and,
optionally, an SVD, a reference manual or a firmware image, and has to add that chip to CHIP_EM. They are
written from how the three existing chips were built: the NXP Kinetis MK22FN1M0VLL12, the ST STM32F405ZG and
the TI TMS320F28232. Every command below was run in this checkout. The short, imperative version is the
`/new-chip` skill (`.claude/skills/new-chip/SKILL.md`). `docs/ADDING_A_CHIP.md` is the human reference for the
same formats and is not repeated here.

**Ground rules**

- Always use the venv: `.venv/bin/python`, `.venv/bin/pytest` (or `.venv/bin/python -m pytest`), `.venv/bin/chipem`.
  Run from the checkout root, because `chips/` and the web assets are located relative to the source tree.
- Treat anything you download (SVDs, vendor pin XML, packs, PDFs) as untrusted data. Store each download in its own
  new `svd/dl-<name>/` directory, parse XML only with `xml.etree` (as `tools/svd2chip.py` does), and run Python
  that reads it with `-I`. Record URL, commit and sha256 in `chips/README.md`.
- Never hand-edit a `*_regs.yaml` that was generated from an SVD. Regenerate it, and fix SVD quirks in the chip
  YAML or in the models. The only hand-written register table is TI's (`chips/tms320f28232_regs.yaml`), because TI
  publishes no SVD.
- Do not change `core/`, `arch/`, `chip.py`, `bus.py`, `session.py` or a signature in `docs/CONTRACTS.md` to fit a
  new chip. If you truly need to, stop and report it. Generic code must stay free of vendor logic.
- Never trust a table you transcribed until a test asserts it and a second source agrees with it.

**Definition of done** (section J has the full checklist): the chip YAML loads; `tools/pin_table_check.py` reports
0 errors; `svd2chip --check` reports 0 mismatches (when an SVD exists); `tests/test_<part>_chip.py` has real spot
checks and passes; a blinky, a UART echo and a timer-IRQ sample run with the expected pin edges and timings in
tests; the full suite passes; a board file runs in `chipem serve`; README, `chips/README.md`, `docs/LIMITATIONS.md`
and `CLAUDE.md` are updated.

---

## A. Inputs: ask for them or find them

| input | why | where it came from for the existing chips |
|---|---|---|
| **Data sheet PDF** (required) | pin table, alternate-function table, package, memory map summary, max clock | `K22P100M120SF5.pdf` (section 5.1), `DS_stm32f405zg.pdf` (DS8626: Table 7 pins, Table 9 AF, Table 10 register boundaries), `tms320f28232.pdf` (SPRS439Q, Table 6-1, Tables 8-18..8-20) |
| **Exact part number and package** | the pin-number column to read, flash/RAM size, which peripherals exist | `MK22FN1M0VLL12` = LQFP100; `STM32F405ZG` = LQFP144 (the "Z" in ST names = 144 pins, "G" = 1 MB); `TMS320F28232` "PGF" = LQFP176 |
| **SVD** (CMSIS, ARM parts only) | peripheral bases, register offsets, reset values, IRQ numbers | `cmsis-svd/cmsis-svd-data` on GitHub at a pinned commit (`data/Freescale/MK22F12.svd`, `data/STMicro/STM32F405.svd`). Also vendor CMSIS packs (Keil `*.pack` are zip files) or the vendor SDK. **TI C2000 has no SVD**: hand-write the regs YAML from the TRM |
| **Reference manual / TRM** | register *behaviour*: reset clock, which status bits firmware polls, w1c/rc_w0 flags, the IRQ vector table, clock tree | K22 RM (`svd/dl-k22rm/K22P100M120SF5RM.pdf`), RM0090 for the STM32F4, SPRUFB0/SPRUFZ5/SPRU812 for the C2000 |
| **Firmware** (optional) | the real target. Decides which peripherals need models beyond the minimum | the Withings image `s1_off000084_503080.bin` (K22, raw `.bin` linked at 0x4000) |
| **Second pin source** (optional, strongly recommended) | cross-check of the transcribed pin table | ST: `STM32_open_pin_data` (`mcu/<part>.xml` + `mcu/IP/GPIO-*_Modes.xml`); the package pinout figure for every vendor |

What to detect yourself:

1. `pdfinfo DATASHEET.pdf` gives the page count. `.venv/bin/python -I .claude/skills/new-chip/pdf_find.py DATASHEET.pdf --toc`
   lists every table and figure caption with its page.
2. The **family**. Reuse an existing family only when the register layout matches, not just the vendor: `stm32.*` is
   the STM32F4 layout (GPIO `MODER/AFRL`, USART `SR/DR/BRR`, RCC `PLLCFGR/AHB1ENR`). An STM32F1 (GPIO
   `CRL/CRH`), F0/L0/G0/L4/H7 (USART `ISR/TDR/RDR`) needs new models. `kinetis.*` is the K22F/K64-style layout, and
   KL/KE parts differ. `ti_c2000.*` is the F2833x/F2823x layout. `tools/new_chip.py` checks this per block (section C).
3. Does an SVD exist? Search `cmsis-svd-data` (`data/<Vendor>/`). If the part is missing, a sibling SVD of the same
   die family is acceptable (`MK22F12.svd` covers both K22 parts). Note the superset problem in section C.

## B. Pin table

The pin table is the part users see first (web UI package drawing, pin names in stimulus, pin report). It is also
where transcription errors hide. Budget most of the review effort here.

### B.1 Find the tables

```sh
.venv/bin/python -I .claude/skills/new-chip/pdf_find.py DS_stm32f405zg.pdf "pin and ball definitions" "Alternate function mapping"
#   49: Table 7. STM32F40xxx pin and ball definitions(1)          ... (continued) through page 61
#   64: Table 9. Alternate function mapping                        ... through page 67
pdftotext -layout -f 49 -l 61 DS_stm32f405zg.pdf $SCRATCH/pins.txt    # $SCRATCH = your scratchpad dir, not the repo
pdftotext -layout -f 64 -l 67 DS_stm32f405zg.pdf $SCRATCH/af.txt
pdftotext -layout -f 67 -l 70 K22P100M120SF5.pdf $SCRATCH/k22_pins.txt     # K22: 5.1 Signal Multiplexing and Pin Assignments
pdftotext -layout -f 22 -l 31 tms320f28232.pdf $SCRATCH/c2k_pins.txt       # F28232: Table 6-1 Signal Descriptions
pdftotext -layout -f 153 -l 155 tms320f28232.pdf $SCRATCH/c2k_mux.txt      # F28232: Tables 8-18..8-20 GPIO mux
```

Always use `-layout`, because columns are recovered by character position. Read the whole page range once by eye
before you write a parser: the header tells you which column is your package (STM32 Table 7 has LQFP64, WLCSP90,
LQFP100, LQFP144, UFBGA176 and LQFP176 columns. Use exactly one of them).

### B.2 Parse it

Write a throw-away parser in the scratchpad (not in the repo) that:

1. **Anchors on rows that have a pin number in your package's column.** In `-layout` output, a table row is one
   anchor line plus continuation lines *above and below* it: long cells wrap and are vertically centred. Example
   (STM32 Table 7, LQFP144 column = 4th):
   ```
                                                                                   FSMC_A1 / I2C2_SCL /
    -         -         -        11        H3         17        PF1      I/O   FT     -                          -
                                                                                      EVENTOUT
   ```
   The alternate-function cell of pin 11 is `FSMC_A1 / I2C2_SCL / EVENTOUT`.
2. **Assigns each continuation fragment to the column whose x-range contains its start**, and to the nearest anchor
   row. Fragments above the anchor come before it, fragments below come after it.
3. **Rejoins wrapped signal names.** A fragment that ends in `/`, `_` or `-`, or one that is an unbalanced
   `(...`, continues on the next line without a space (`FB_CS5_b/FB_TSIZ1/` + `FB_BE23_16_BLS15_8_b`). Split
   multi-signal cells on `/` only where the vendor uses `/` as a separator: Kinetis uses `/` *inside* one ALT cell
   for aliases (`JTAG_TCLK/SWD_CLK`), so keep the K22 cell text verbatim.
4. **Drops footnote markers** (`(1)`, `(5)`) and family-only signals (STM32F407 `ETH_*`/`DCMI_*` do not exist on an
   F405; the C2000 table lists F2833x-only `ECAP5`/`ECAP6` and SCI-C names).
5. **Builds the AF/ALT matrix from the separate mux table** when there is one (STM32 Table 9: one row per GPIO,
   columns AF0..AF15; C2000 Tables 8-18..8-20: GPxMUX values 0..3). The K22 has everything in the one 5.1 table
   (columns ALT0..ALT7 plus EzPort).

Then cross-check against a second source and record what you compared in the chip YAML header:

- **The pin count**: `len(pins) == package pins`, numbers exactly 1..N, unique.
- **The pinout drawing** (STM32 Figure 14, K22 5.2 Pinouts): spot-read every 10th pin and the four corners.
- **Vendor machine-readable data** when it exists. For the STM32F405, `STM32_open_pin_data` gave 0 differences in pin
  numbers and AF numbers, apart from naming (`USB_OTG_HS_*` vs the data sheet's `OTG_HS_*`). Keep the data sheet
  spelling, because users search for it.
- **Data sheet typos**: DS8626 prints `UART6_CTS` for PG13 AF8, but the peripheral is USART6. Fix it, add a comment in
  the YAML header, and assert the corrected value in a test.

### B.3 YAML shape

```yaml
gpio_name_format: "P{port}{bit}"     # Kinetis "PT{port}{bit}" (default), STM32 "P{port}{bit}", C2000 "GPIO{num}"
pins:
  - {num: 101, name: PA9, kind: gpio, port: A, bit: 9, default: GPIO, functions: {AF1: TIM1_CH2, AF4: I2C3_SMBA, AF7: USART1_TX, AF15: EVENTOUT, additional: OTG_FS_VBUS}}
  - {num: 25, name: NRST, kind: special}
  - {num: 31, name: VSSA, kind: power}
```

| key | rule |
|---|---|
| `num` | package pin number. BGA parts need a numbering decision (row-major ball index). Document it |
| `name` | the data sheet pin name verbatim, including `/` aliases (`PTC5/LLWU_P9`, `PC14/OSC32_IN`, `GPIO28/SCIRXDA/XZCS6`). It must contain the GPIO name built from `gpio_name_format` as one `/`-part |
| `kind` | `gpio` (has `port`/`bit`), `analog` (dedicated analog input), `power` (names with `VSS`/`VREFL` are tied low, every other power pin high), `special` (reset, boot, crystal, JTAG, USB PHY) |
| `port`, `bit` | only on `gpio` pins. Port letter A..Z, bit 0..31. C2000: port A/B/C = GPIO0-31/32-63/64-87, so `{num}` = 32 × port + bit |
| `default` | function after reset. `GPIO` (STM32, C2000), `DISABLED` (K22 pins whose ALT0 is "disabled"), or the ALT0 signal (`ADC1_SE4a`) |
| `functions` | only the columns that have a value. Keys per family: Kinetis `ALT0`..`ALT7`, `EzPort`; STM32 `AF0`..`AF15`, `ADC` (owner in analog mode), `analog`, `additional`; C2000 `GPIO`, `MUX1`..`MUX3`, `ANALOG`. The mux model turns these into the pin owner, so they must match what the models and users search for (`USART1_TX`) |

### B.4 Mandatory tests

`tools/pin_table_check.py` runs the mechanical checks: count, uniqueness, 1..N, kinds, port/bit on every GPIO, GPIO
name vs `gpio_name_format`, functions naming another pin's GPIO, duplicate names, family mux keys and power-name
sanity. Run it until it reports 0 errors and 0 warnings:

```sh
.venv/bin/python -I tools/pin_table_check.py chips/stm32f405zg.yaml            # expected count = number in `package:`
.venv/bin/python -I tools/pin_table_check.py chips/<part>.yaml --expect 48 --strict -v
```

Then write the **spot checks** in `tests/test_<part>_chip.py`, 10 to 20 asserts copied by eye from the PDF, not from
your parsed output. Cover the console UART TX/RX, an LED and a button candidate, a pin with a long wrapped name, a
pin whose name has aliases, the reset/boot pins, every kind of power pin, the first and the last pin, and every typo
you corrected. Examples from the real tests:

```python
assert pin("PA9").num == 101 and pin("PA9").functions["AF7"] == "USART1_TX"          # tests/test_stm32.py
assert pin("PG13").functions["AF8"] == "USART6_CTS"                                  # data sheet typo corrected
assert desc.pin(90).functions["ALT5"] == "FB_CS5_b/FB_TSIZ1/FB_BE23_16_BLS15_8_b"    # tests/test_chipdesc.py (wrapped)
assert p12.functions == {"GPIO": "GPIO12", "MUX1": "TZ1", "MUX2": "CANTXB", "MUX3": "MDXB"}   # test_c2000_chip.py
assert len(power) == 52 and sum(p.name == "VSS" for p in power) == 22                # power pin census
```

## C. Memory map, peripherals, IRQs

### C.1 Scaffold

```sh
.venv/bin/python -I tools/new_chip.py --part STM32F405ZG --package LQFP144 --family stm32 \
    --arch armv7e-m --cpu cortex-m4 --fpu --clock-hz 16e6 \
    --svd svd/dl-stm32f405-Ij9M/STM32F405.svd \
    --mem FLASH:flash:0x08000000:0x100000 --mem CCMRAM:ram:0x10000000:0x10000 --mem SRAM:ram:0x20000000:0x20000 \
    [--pins pins.csv] [--exclude UART7,UART8,SPI4,SPI5,SPI6,SAI1,LTDC,GPIOJ,GPIOK]
# TI (no SVD): --family ti_c2000 --regs chips/<part>_regs.yaml (a hand-written table in the svd2chip schema)
```

It writes `chips/<part>_regs.yaml` (svd2chip `--flat`), `chips/<part>.yaml` and `tests/test_<part>_chip.py`. It
refuses to overwrite without `--force`, and it marks every guess with `TODO`. `--svd` also accepts an `https://` URL:
the file is cached under `svd/dl-<part>/` and its sha256 printed. Your pin parser can emit `pins.csv` with the
columns `num,name,kind,port,bit,functions[,default]` (`functions` = `AF7=USART1_TX;AF8=UART4_TX`).

How it maps peripherals: a per-family rule table (`FAMILIES` in `tools/new_chip.py`). A block gets the family model
only when its SVD name matches **and** the SVD block has the registers that model is written against. For example,
`stm32.gpio` needs `MODER/AFRL/IDR/ODR/BSRR`, so an STM32F1 `GPIOA` (`CRL/CRH`) becomes `stub` with a TODO. Run on
the three real register tables, it reproduces the hand-written chips' model choice for every block they share
(`tests/test_new_chip_tools.py`).

| family | mapped (everything else becomes `stub`) |
|---|---|
| `stm32` | `GPIOx`→`stm32.gpio {port}`, `USARTn`/`UARTn`→`stm32.usart {index, kind}`, `RCC`→`stm32.rcc {ext_hz}`, `TIM1..14`→`stm32.tim` (one model; channels, break/dead time and RCR follow the SVD registers of each timer), `IWDG`, `WWDG`, `EXTI`, `SYSCFG`, `FLASH`, `PWR`, `DBG`→`stm32.dbgmcu` |
| `kinetis` | `PORTx`→`kinetis.port`, `GPIOx`→`kinetis.gpio`, `UARTn`, `SIM`, `MCG`, `OSC`, `WDOG`, `PIT`, `FTMn`→`kinetis.ftm` (plus `FTM2_ALIAS`→`kinetis.ftm_alias {target: FTM2}` at 0x4003A000, which the SVD does not list), `LPTMR0`, `RTC`, `FTFE/FTFL/FTFA`→`kinetis.ftfe`, `ADCn`, `SPIn`→`kinetis.dspi`, `DMA`, `DMAMUX`, `PDB0`, `SMC`, `PMC`, `RCM`, `LLWU`, `RFVBAT`→`VBAT kinetis.vbat`, `RFSYS`→`ram` |
| `ti_c2000` | `GPIO`, `SCIx`, `CPUTIMERn`, `SYSCTRL {ext_hz, boot_divsel}`, `ADC`, `ADCMIRROR`, `XINT`, `SPIx`, `FLASH`→`FLASHREGS ti_c2000.flash`, `CSM`, `PIE` (arch-owned), `EPWMn`→`ti_c2000.epwm {gate: EPWMnENCLK, index}`, `ECAPn`→`ti_c2000.ecap {gate: ECAPnENCLK, index}`, eQEP/eCAN/McBSP/I2C/DMA→`ti_c2000.storage {gate: <NAME>ENCLK}`, the rest `ti_c2000.storage` |
| anything else | every block `stub` (a new family, section E) |

Sizes: the SVD `addressBlock` extent rounded up to the family's granule (0x400 for STM32, 0x1000 for Kinetis, exact
for C2000), then clamped so that a block does not run into the next one (Kinetis `GPIOA..E` at 0x40 strides, STM32
`ADC1..3` at 0x100). Blocks inside the Cortex-M private peripheral bus (0xE0000000–0xE00FFFFF) are skipped unless
mapped, because the arch layer owns the SCS at 0xE000E000. So are blocks inside a declared memory region (the K22
flash configuration field at 0x400).

### C.2 Fix what the scaffold cannot know

- **Memory** (`memory:`): take every region from the data sheet memory map for *this* part number. Flash and RAM size
  depend on the suffix letter (`MK22FX512` = 512 KB + FlexNVM, `MK22FN1M0` = 1 MB). Bases and sizes must be 1 KB
  aligned (Unicorn pages). Kinds: `flash`, `ram`, `flexnvm`, `flexram`, `rom`. Unmapped regions are bus faults, so
  map what firmware touches: STM32 `BKPSRAM`, C2000 `OTP`/`BOOTROM`/XINTF zones. A variant of the same die should
  `include:` the base YAML and override `name` and `memory` (`chips/mk22fx512vll12.yaml`).
- **SVD supersets**: ST's `STM32F405.svd` also describes F42x blocks (UART7/8, SPI4-6, SAI1, LTDC, GPIOJ/K). Delete
  every block that is not in the data sheet's register boundary table (STM32 DS8626 Table 10, K22 RM memory map), or
  pass `--exclude`. Keep F407-only blocks such as Ethernet as `stub` with a comment if they are harmless.
- **Large windows**: map USB OTG as one 256 KB stub (`OTG_HS_GLOBAL size 0x40000`) instead of the SVD's sub-blocks.
  The F405 SVD's `OTG_HS_PWRCLK` claims 0x3F200 bytes.
- **Catch-all windows**: accesses outside listed blocks and memory are bus faults, except in the auto-stub windows
  (`bus.DEFAULT_CATCH_ALL` = 0x40000000–0x400FFFFF and 0xE0000000–0xE00FFFFF). STM32 AHB blocks at
  0x48000000/0x50000000 (and the F0/F3/L4 GPIO ports at 0x48000000) must be listed explicitly. A non-ARM chip sets
  its own windows: `auto_stub_ranges: [[0x0800, 0x0FFF], [0x5000, 0x7FFF]]` (C2000, inclusive, word addresses).
- **Vector table / boot**: STM32 boots through an alias at 0x00000000 that CHIP_EM does not map, so set
  `vtor: 0x08000000`. Kinetis flash is at 0 and needs nothing. The C2000 has no boot ROM:
  `boot: {entry: 0x33FFF6, sp: 0x0400, rom_words: {0x380080: [0x0006]}}` seeds what TI's OTP/ROM would contain
  (ADC_cal = `LRETR`, PARTID).
- **IRQs** (`irqs:`): exception number = 16 + n on ARM. Compare with the RM vector table, because SVDs drop
  entries. `MK22F12.svd` lacks 23 RNG, 30 UART0_LON, 55, 57 MCG and 64 SWI, and `STM32F405.svd` lacks 4 FLASH. The
  scaffold writes a `TODO ... defines no interrupt [...]` line listing the gaps. `num_irqs` = highest + 1 (82 on the
  F405, not the SVD superset's 90). The C2000 uses PIE vector numbers (`INTx.y = 32 + 8(x-1) + (y-1)`, TINT0 = 38).
- **SysTick**: `systick_ext_div` = core cycles per SysTick tick with `CLKSOURCE=0` (STM32F4: HCLK/8 → 8).

### C.3 Check

```sh
.venv/bin/python -I tools/svd2chip.py svd/dl-stm32f405-Ij9M/STM32F405.svd --check chips/stm32f405zg.yaml   # "0 mismatch(es)"
.venv/bin/python -I tools/svd2chip.py svd/dl-Sasv/MK22F12.svd --check chips/mk22fn1m0vll12.yaml
```

`MISMATCH` lines (base, size < SVD extent, IRQ name/number) must be 0. `NOTE` lines (a block renamed or not in the SVD,
SVD blocks you left out) are fine when each one is intentional. The exit status is always 0, so read the last line, or
assert `svd2chip.check(...) == 0` in the test as the generated skeleton does.

### C.4 Hand-written register table (TI, or any vendor without an SVD)

Use the svd2chip output schema (`chips/README.md`): header `device`, `svd_version: "hand-written"`,
`address_unit_bits`, `cpu`, `interrupts`, then `peripherals: {NAME: {base, size, registers: [{name, offset, width,
reset, access, fields}]}}`. Transcribe it from the TRM register maps and cite the documents in the header (see
`chips/tms320f28232_regs.yaml`). On a 16-bit-unit chip, offsets, sizes and widths count words, and a width-2
register is 32 bits, low word first. Test a few bases and reset values against the TRM (`test_register_table` in
`tests/test_c2000_chip.py`).

## D. Core settings

```yaml
core: {arch: armv7e-m, backend: unicorn, cpu: cortex-m4, fpu: true, clock_hz: 16000000, addr_unit_bits: 8, num_irqs: 82}
```

| CPU | `arch` | `backend` | `cpu` | notes |
|---|---|---|---|---|
| Cortex-M0/M0+ | `armv6-m` | `unicorn` | `cortex-m0` / `cortex-m0plus` | 2 NVIC priority bits; no bit-band |
| Cortex-M3 | `armv7-m` | `unicorn` | `cortex-m3` | bit-band on by default (`bitband: false` to disable) |
| Cortex-M4/M4F | `armv7e-m` | `unicorn` | `cortex-m4` | `fpu: true` only for M4F parts |
| Cortex-M7 | `armv7e-m` | `unicorn` | `cortex-m7` | untested so far: add exception/FP tests first |
| TI C28x | `c28x` | `c28x` | `c28x` | `addr_unit_bits: 16`, `num_irqs: 0` (PIE), pure-Python interpreter in `chipem/core/c28x` |

- `fpu`: does the part have an FPU? Do not trust the SVD here: `STM32F405.svd` says `fpuPresent false` and
  `nvicPrioBits 3`, but the part has an FPU and 4 priority bits. The F28232 has no FPU32 (`fpu: false`); F2833x parts have one.
- `clock_hz`: **the clock after reset, not the data sheet maximum.** STM32F4: 16 MHz HSI (168 MHz only after the
  firmware programs the PLL). K22: 20.97 MHz FEI (32768 × 640); the YAML says 120 MHz, but `kinetis.mcg` computes the
  reset clock itself. F28232: 10 MHz with a 20 MHz OSCCLK and the boot ROM's DIVSEL /2. The YAML value is used until
  the clock model calls `chip.clock.set_hz()`. A chip without a clock model keeps it forever, so set it right.
- `addr_unit_bits`: 8 for ARM. 16 for the C28x, where every address, size, `--load-addr` and event address is a word.
- `num_irqs`: NVIC lines (raised automatically to cover `irqs:`).

## E. Deciding the models

**Reuse** when the vendor family *and* the register layout match. The scaffold has already done this per block.
Re-check the TODO comments, then confirm `Chip(desc)` builds with no "not available" warning (the generated test does).
Then read the family's models for part-specific constants: `stm32.dbgmcu` IDCODE, `stm32.rcc` `ext_hz` (board
crystal), `kinetis.sim` SDID, C2000 `SYSCTRL` `ext_hz`/`boot_divsel` and the PARTID `rom_words`.

**A new family** needs the minimum package below, in this order (it is the order that worked: first pins show
edges, then periods are right, then nothing resets unexpectedly, then IRQs and console):

1. `chipem/periph/<family>/__init__.py`: copy `chipem/periph/stm32/__init__.py`. It imports every submodule with
   `pkgutil.iter_modules` and records failures in `IMPORT_ERRORS` instead of raising.
2. `_util.py`: copy `stm32/_util.py` (`build_regs(names, size, overrides, regs_file)`, `irq_number`, `find_model`,
   `GatedBlock`). Point `DEFAULT_REGS` at your regs file. The Chip passes the YAML's `regs_file` to every model as
   `params["regs_file"]`.
3. **Clock / reset control** (`rcc.py`-like): the reset clock, the oscillator/PLL/divider tree, clock gates
   (`gate_location`/`check_gate`, which only `warn` on access to a gated block), peripheral clock getters for
   UART/timer models, and `chip.clock.set_hz(core_hz)` on every switch. Honour `frozen`: set it when
   `getattr(chip, "clock_override", None)` is set (`--clock`, `Chip(clock_hz=...)`) as `stm32.rcc` does, because
   `Session.apply_clock_override` only freezes blocks named `MCG` or `SYSCTRL` by itself.
4. **GPIO + pin mux** (`gpio.py`): the only code that maps vendor registers to
   `chip.pins.mcu_update(pin, owner=..., direction=..., drive=..., pull=...)` and pin levels back to input registers.
   Find pins with `chip.pins.gpio(port, bit)` in `attach`, and register `chip.pins.input_listeners[pin.id]` for input
   caches and edge interrupts. Owners are `"GPIO"`, a function name from `pin.functions`, or the analog function with
   direction `"analog"`. Floating (`X`) reads 0 with one `warn`. Call the pin-report hooks
   (`pinreport.ensure(chip)`, `config(...)`, `touch_cfg/touch_data`) like `stm32/gpio.py`. Keep the input-data
   register on a fast path, because it is polled millions of times.
5. **UART-class peripheral** (`usart.py`/`sci.py`): byte-level, no pin toggling. A block is a UART when it has
   `tx_listeners` (`Chip.uart_blocks()`). Subclass `chipem/periph/uartwire.py: UartRx` (together with `MMIOBlock`),
   which supplies `feed(data)` (host input, paced at the receiver's baud) and `receive(data)` (board links, one call
   per byte at its stop-bit time) and the hardware rule "no input queue": a byte is dropped (logged as a `uart` event
   with `dir="drop"` and a reason) unless the receiver is listening. You implement `_rx_block_reason()` (return
   `rx_disabled`, `clock_gated`, `reset` or None from the family's enable/gate/reset bits), `_rx_accept(b)` (store
   into the holding register or FIFO, or set the overrun flag and lose the byte, exactly as the manual says) and set
   `rx_signal` (the RX pin function name, e.g. `USART1_RX`, so a byte is dropped with `pin_not_muxed` when no pin
   is muxed to it). Also provide `tx_listeners: list[cb(uart, byte)]`, `output: bytearray`, `baud()`, `char_bits()`,
   `char_ns()`, `frame()` ("8N1"), and `self.emit("uart", uart=self.name, dir="tx"|"rx", byte=b)`. Set TX-empty/
   complete one character time after a write (scheduler), and RX-ready + IRQ when a byte is accepted.
6. **A timer with an IRQ** (`tim.py`/`cputimer.py`/the skeleton in `docs/ADDING_A_CHIP.md` section 6). Use the
   scheduler (`sched.in_ns` for wall-clock sources, `in_cycles` for core-clocked ones), keep the `Event` handle, cancel
   it in `reset()` and before rescheduling, and drive a level IRQ with `self.irq(n, level)` that drops when the flag
   is cleared. Look the IRQ number up by name from `chip.desc.irqs`.
7. **The watchdog**: model unlock/refresh/timeout → `chip.reset("watchdog")`, or leave a stub if firmware only disables
   it. The F28232 watchdog runs from reset (about 6.5 ms), so a missing model there hides real resets.
8. Optional: static pin roles for `chipem info` live in `chipem/firmware.py` (owned by the firmware/analysis area). They
   are per family (Kinetis PCR, STM32 MODER/AFR, C2000 GPxMUX). Ask before editing.

Conventions every model follows (from `CLAUDE.md`): `@register("<family>.<model>")`; constructor
`__init__(self, name, base, size, regs=(), *, params=None, log=None)`; register lists from the regs table with only
behaviour hand-written (`on_read`, `on_write`, `w1c_mask`, `ro_mask`, reset overrides); all observations through
`self.emit(kind, ...)`; never `print`; no per-instruction hooks.

**Status registers must mirror control writes so init loops terminate.** A stub returns what was written, so a loop
that waits for hardware to set a bit spins forever. Every trap seen so far:

| block | firmware does | model must |
|---|---|---|
| K22 `MCG` | `C1 = 0x04`, then polls `S.IREFST` (and `CLKST`, `LOCK0` for PLL modes) | set `S` from `C1/C5/C6` immediately |
| K22 `ADC0` | `SC3.CAL = 1`, polls `SC1A.COCO`, checks `SC3.CALF` | set COCO, clear CALF on CAL; set COCO on every conversion start |
| K22 `FTFE` | launches a command, polls `FSTAT.CCIF` | complete commands at once (CCIF = 1) |
| K22 `SPI0` | polls `SR.TXRXS/TXCTR/RXCTR` (Withings image, before DSPI was modelled) | model FIFO counters |
| K22 `PDB0` | busy-waits on `CNT` | count in virtual time |
| STM32 `RCC` | `HSEON` → poll `HSERDY`; `PLLON` → poll `PLLRDY`; `SW` → poll `SWS` | RDY = ON at write time; `SWS` follows `SW` when the source is ready |
| C2000 `SYSCTRL` | writes `PLLCR`, polls `PLLSTS.PLLLOCKS` | clear PLLLOCKS, set it again after 131072 OSCCLK cycles (scheduler) |
| any UART | polls TX-empty / TX-complete | set them one character time after the write |

When a run stalls (same PC in every slice, or `chipem run` speed collapses), find the loop and model that bit
(section G).

**Tests for models**: next to the existing ones (`tests/test_<family>*.py`). Build the real chip YAML with a spin-loop
firmware (`spin_chip` in `tests/test_stm32.py`), poke registers through `chip.bus.write/read`, `chip.run_for(ns)`, and
assert on registers, `chip.log` events, pin state and `chip.arch.asserted`. Keep each test in milliseconds.

## F. Sample firmware

The minimal set, one directory per family (`firmware/samples_<family>/`, a `build.sh` that builds every source):

| sample | proves | test asserts |
|---|---|---|
| `blinky` | GPIO output, mux, clock gate | ≥ 10 level edges on the LED pin, owner `GPIO`, direction `out`, `chip.clock.hz` = reset clock |
| `button_mirror` | GPIO input, pulls, stimulus | `--at 1ms:PIN=0` → output follows within a few µs; floating input reads the pull |
| `uart_echo` | UART TX/RX, baud from the clock tree | output starts `hello\r\n`; `--at 1ms:USART1=hi` (sent once the receiver is up) is echoed |
| `uart_link_echo` | board link endpoint | on a board, the peer receives the uppercased echo |
| `timer_irq` | timer + NVIC/PIE + clock tree | handler toggles a pin; periods = 1 ms ± 0.1 %; IRQ count |
| `pin_irq` | edge interrupt (PORT ISF / EXTI / XINT) | one IRQ per edge, flag cleared |

Toolchains (no GNU arm toolchain on this machine; `clang` and `ld.lld` are installed, `llvm-objcopy` is not, so the
scripts fall back to `firmware/samples/elf2bin.py`):

```sh
# Cortex-M (firmware/samples_stm32/build.sh): adjust --target/-mcpu/-mfpu and the image base per chip
clang --target=thumbv7em-none-eabi -mcpu=cortex-m4 -mfpu=fpv4-sp-d16 -mfloat-abi=hard -mthumb -O1 -g \
      -ffreestanding -nostdlib -fno-builtin -ffunction-sections -fdata-sections \
      -T link.ld -fuse-ld=lld -Wl,--image-base=0x08000000 -Wl,-e,Reset_Handler -Wl,--gc-sections \
      -o build/blinky.elf startup.c blinky.c
#   M0: --target=thumbv6m-none-eabi -mcpu=cortex-m0 -mfloat-abi=soft ; M3: thumbv7m-none-eabi -mcpu=cortex-m3 -mfloat-abi=soft
# C28x (firmware/samples_c2000/build.sh): our assembler, JSON word image
.venv/bin/python tools/c28x_asm.py blinky.asm -o build/blinky.c28img
```

`link.ld` places `.isr_vector` at the flash base, `.data` in RAM with `AT > FLASH`, and `_estack` at the top of RAM.
`startup.c` holds the full vector table (initial SP + 15 system exceptions + every IRQ from your `irqs:`), enables the
FPU on M4F/M7 (CPACR), copies `.data`, zeroes `.bss` and calls `main`. Copy both from `firmware/samples_stm32/` and
change the memory sizes and the IRQ handler list. Write samples against the *reset* clock unless the sample is about
the PLL (`tim_irq` programs 168 MHz on purpose). Use `static inline` register helpers or macros with constant pin
numbers. A pin passed as a function argument through a non-inlined driver defeats `chipem info`'s static pin roles.

End-to-end tests (`tests/test_<family>_samples.py`) load `build/*.elf` (skip if not built), run in virtual time and
assert timings. From `tests/test_c2000_samples.py`:

```python
s = session("timer_irq", tmp_path); s.run_for(12.2e6)
assert s.irq_counts.get(38, 0) == 5                       # TINT0 = PIE 1.7
gaps = [b - a for a, b in zip(ev, ev[1:])]
assert gaps and all(abs(g - 1e6) < 1e3 for g in gaps)     # 1 ms ± 1 µs
```

## G. Booting unknown firmware

```sh
.venv/bin/chipem info fw.elf --chip chips/<part>.yaml                 # vector table, referenced blocks, provisional pin roles
.venv/bin/chipem info fw.bin --chip <part> --load-addr 0x4000 --vtor 0x4000
.venv/bin/chipem run fw.elf --chip <part> --for 100ms --summary-only --pin-report
.venv/bin/chipem run fw.elf --chip <part> --for 100ms --events warn,fault,reset,info
.venv/bin/chipem shell fw.elf --chip <part> --exec "run 50ms; state; dis; bt; periph RCC; pins changed; quit"
```

- **Load address**: ELF images carry it. For a raw `.bin`, auto-detection tries only 0x0, 0x1000, 0x2000, 0x4000,
  0x8000 and 0x10000. Otherwise score candidate bases by how many vectors land on function prologues. The Withings
  image scored 75/97 at 0x4000 and garbage at 0, because a bootloader occupied 0x0–0x3FFF. Pass `--load-addr`, and
  `--vtor` if the image never writes VTOR (its bootloader did). STM32 images are linked at 0x08000000. C28x `.bin`
  load addresses are in words.
- **Read the summary**: `unmodelled peripherals (RAM-like stubs): ...` is the to-do list. Each `warn` (gated clock,
  floating input, unknown model) and each `fault` (bus fault address → a missing memory region or peripheral) is a
  lead. The first unmapped address in a fault is usually a block missing from `peripherals:`.
- **Stalls**: if the PC repeats, `chipem shell ... --exec "run 1s; dis; bt"` shows the loop. Look up the polled
  register (`periph NAME`), then model that status bit (section E table).
- **Pins**: `--pin-report` lists every pin the firmware configured, which code did it, the toggle period and
  "static vs run" agreement. Drive inputs (`--set`, `--at 5ms:PA0=1`), feed UARTs (`--at 5ms:USART1=help\r`; bytes sent before the receiver listens are dropped like on silicon) and run
  longer to reach more code.
- **Knobs**: `--clock HZ` freezes the core clock (useful when the clock model is not ready yet). `--max-slice N` uses
  smaller slices (default 2000 instructions) when timer periods look skewed. `--precise` gives exact per-instruction
  counting, about 10× slower. `--stop-on-fault` stops at the first fault. `--log-mmio` logs every register access
  (verbose).
- **C28x**: TI-compiled code may hit low-confidence ISA entries (`docs/C28X_CORE.md`). An "illegal instruction" or a
  jump into the weeds there is an emulator gap, not a firmware bug. Report it.

## H. Board file and web check

Use `/new-board` (`.claude/skills/new-board/SKILL.md`). Minimal form:

```yaml
name: mychip_stm32_uart
quantum: 10us
chips:
  - {id: A, chip: <part>, firmware: ../firmware/samples_<family>/build/uart_link_echo.elf}
  - {id: B, chip: stm32f405zg, firmware: ../firmware/samples_stm32/build/uart_link_echo.elf}
links:
  - {type: uart, from: A.USART1, to: B.USART1}
nets:
  - {name: LED, pins: [A.PB0, B.PA0]}
stimulus: ["5ms:A.PA0=1"]
```

```sh
.venv/bin/chipem run --board boards/<name>.yaml --for 20ms --pin-report
.venv/bin/chipem serve --board boards/<name>.yaml --port 8791 --start &     # blocks: run it in the background
curl -s localhost:8791/api/chip | head -c 300; curl -s "localhost:8791/api/uart?uart=A.USART1"
```

Check that the package drawing has N pins and the right pin 1, that pin names show `gpio_name_format` ids, that the
UART console shows output, and that clicking an input pin changes a mirrored output. Then stop the server.

## I. Documentation

- `README.md`: the chip in the intro sentence, a row in "Supported chips" (file, memory), a subsection like
  "STM32F405ZG" (pins, boot, clock, samples, board), the peripheral coverage table, and the sample and board tables.
- `chips/README.md`: a `<part>_regs.yaml provenance` section with the URL at the pinned commit, the SVD version, the
  sha256, the regenerate and check commands, the SVD quirks you worked around, and where the pin table came from
  (tables, revision, second source, corrected typos).
- `docs/LIMITATIONS.md`: a section for the chip (no flash alias, which blocks are stubs, timing approximations).
- `CLAUDE.md`: new gotchas (units, boot quirks, `gpio_name_format`), the run commands for the new samples, and the
  file-ownership row for the new family.
- The chip YAML header: sources with table numbers, revision and second source, so the next agent can re-verify.

## J. Final checklist and definition of done

- [ ] Inputs recorded: data sheet revision, part/package, SVD URL + commit + sha256 (or "no SVD: hand-written from ...").
- [ ] `chips/<part>.yaml` loads (`load_chip`), and `grep TODO chips/<part>.yaml` is empty.
- [ ] `tools/pin_table_check.py chips/<part>.yaml --strict` reports 0 errors and 0 warnings. The pin table was
      cross-checked against a second source, and typos are fixed and commented.
- [ ] `tools/svd2chip.py <svd> --check chips/<part>.yaml` reports `0 mismatch(es)`, and every NOTE is intentional.
- [ ] Core: reset `clock_hz`, `fpu` from the data sheet (not the SVD), `num_irqs`, `vtor`/`boot`, `systick_ext_div`.
- [ ] Memory regions are complete for the exact part. Superset SVD blocks are removed.
- [ ] Every `irqs:` gap was compared with the RM vector table.
- [ ] `Chip(desc)` builds without "not available" warnings, and the family has `IMPORT_ERRORS == {}`.
- [ ] `tests/test_<part>_chip.py`: no `pytest.fail("TODO")` left, and 10+ spot checks. Model tests for every new
      model, including each polled status bit.
- [ ] Samples build (`build.sh`). `test_<family>_samples.py` asserts pin edges, periods, UART bytes and IRQ counts.
- [ ] `.venv/bin/python -m pytest -q` passes in full (about 45 s) and is still fast.
- [ ] A board with the new chip runs in `chipem run --board` and `chipem serve` (UART bytes cross the link).
- [ ] README, `chips/README.md`, `docs/LIMITATIONS.md` and `CLAUDE.md` are updated.

## K. Worked mini-examples

**Kinetis (MK22FN1M0VLL12).** Data sheet 5.1 has one row per pin with the 100-pin number, pin name, default and
ALT0..ALT7 + EzPort columns. Row 77 transcribes to

```yaml
- {num: 77, name: PTC5/LLWU_P9, kind: gpio, port: C, bit: 5, default: DISABLED, functions: {ALT1: PTC5/LLWU_P9, ALT2: SPI0_SCK, ALT3: LPTMR0_ALT2, ALT4: I2S0_RXD0, ALT5: FB_AD10, ALT6: CMP0_OUT, ALT7: FTM0_CH2}}
```

`ALT1` is the GPIO function and equals the pin name. `test_chipdesc.py` asserts `functions["ALT1"].split("/")[0] ==
gpio_name` for all 66 GPIO pins. The register table comes from `MK22F12.svd` (`--flat`). Blocks on the second
peripheral bridge sit at AIPS1 = 0x40080000 + slot × 0x1000 (`SPI2` 0x400AC000, `FTM2` 0x400B8000, `UART4` 0x400EA000),
and FTM2 also appears in AIPS0 at 0x4003A000 (`FTM2_ALIAS`). Take bases from the SVD, not from slot arithmetic. The
boot trace of real firmware drove the model list: WDOG unlock/refresh, `MCG S.IREFST` polling, SIM gates, ADC
calibration, then RTOS SysTick/PendSV with lazy FP stacking (`docs/PLAN.md`, "Boot-path findings").

**STM32 (STM32F405ZG).** Table 7 gives the pin (LQFP144 column) and its "additional functions". Table 9 gives AF0..AF15
per GPIO. They merge into

```yaml
- {num: 101, name: PA9, kind: gpio, port: A, bit: 9, default: GPIO, functions: {AF1: TIM1_CH2, AF4: I2C3_SMBA, AF7: USART1_TX, AF15: EVENTOUT, additional: OTG_FS_VBUS}}
```

`svd2chip --check` is 0 after three fixes in the chip YAML: IRQ 4 `FLASH` was added (missing from the SVD), the
superset blocks were dropped, and the USB OTG windows were merged. Model-side SVD fixes: the USART `SR` reset value,
`PWR_CR.VOS` writability and the `DBGMCU_IDCODE` value. `vtor: 0x08000000` replaces the boot alias. The clock is
16 MHz HSI after reset. `tim_irq` programs HSE 8 MHz → PLL 168 MHz and checks a 1 ms TIM2 period.

**C2000 (TMS320F28232).** No SVD: `chips/tms320f28232_regs.yaml` is hand-written in the svd2chip schema with
`address_unit_bits: 16`. Table 6-1 lists each GPIO with its mux names, and the GPIO mux tables give MUX1..MUX3:

```yaml
gpio_name_format: "GPIO{num}"
core: {arch: c28x, backend: c28x, cpu: c28x, fpu: false, clock_hz: 100000000, addr_unit_bits: 16, num_irqs: 0}
- {num: 141, name: "GPIO28/SCIRXDA/XZCS6", kind: gpio, port: A, bit: 28, default: GPIO, functions: {GPIO: "GPIO28", MUX1: "SCIRXDA", MUX2: "XZCS6", MUX3: "XZCS6"}}
```

Every base is a word address (`SCIA` at 0x7050, `GPIO` at 0x6F80). `irqs:` are PIE vector numbers (`38: TINT0`).
Samples are assembled with `tools/c28x_asm.py`. They wait about 6.55 ms for the PLL to lock before they toggle
GPIO34, and the tests account for that.

## L. Common mistakes

- **Wrong bridge addresses**: computing Kinetis AIPS slot addresses by hand, or taking an alias window (`FTM2` at
  0x4003A000) as the primary. Use the SVD and `--check`.
- **Max clock instead of reset clock** in `clock_hz`: every baud rate and period comes out wrong until the PLL code
  runs. The samples then disagree with the tests by exactly the PLL factor.
- **Trusting SVD metadata**: `fpuPresent`, `nvicPrioBits`, reset values (USART `SR`), read-only flags (`PWR_CR.VOS`),
  IDCODE, and missing `<interrupt>` entries. The SVD is a register map, not ground truth.
- **SVD supersets**: blocks and IRQs that do not exist on this part number (UART7/8, LTDC on an F405).
- **Bootloader offsets**: an image linked at 0x4000 run at 0 jumps into garbage. Check the vector table score
  (`chipem info`) and pass `--load-addr`/`--vtor`.
- **Word vs byte units** on the C28x: sizes and offsets in words, a width-2 register is 32 bits, `--load-addr` in
  words. Do not reuse ARM helpers that assume bytes (`addr & ~1`, Thumb half-word reads).
- **Pins as function arguments**: static analysis cannot see `gpio_init(port, pin)` through a non-inlined driver
  (Withings: one direct pin write found statically). Rely on `--pin-report` from a run, and write samples with
  constant pins.
- **Data sheet typos** (`UART6_CTS` on PG13 AF8). Every name you correct gets a comment and a test.
- **Wrong package column**: STM32 Table 7 lists six packages side by side. Reading the LQFP176 column for an
  LQFP144 part shifts every pin.
- **Wrapped names split into two signals** (`FB_CS5_b/FB_TSIZ1/` + `FB_BE23_16_BLS15_8_b`), or footnote digits
  glued onto names (`PF3(5)`).
- **Unmapped AHB/PPB windows**: STM32 blocks above 0x40100000 or at 0x48000000/0x50000000 bus-fault unless listed.
- **Forgetting `reset()`**: a model that does not cancel its scheduler events keeps firing after a watchdog or
  `SYSRESETREQ` reset (`Chip.reset()` does not clear the scheduler).
- **Hard-coded IRQ numbers** in models instead of a lookup by name from `chip.desc.irqs`.
- **Editing a generated `*_regs.yaml`** instead of regenerating it and fixing the quirk in the model or the chip YAML.
