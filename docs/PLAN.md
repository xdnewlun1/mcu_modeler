# CHIP_EM — description-driven MCU emulator (v1: Kinetis K22 / Cortex-M4F)

## Context

Goal: load an MCU description (pinout, memory map, peripherals) plus arbitrary firmware, run it, and watch
how the chip's pins respond while the user drives input pins. First target: NXP/Freescale Kinetis K22
(`K22P100M120SF5.pdf`, 100-pin LQFP, MK22FN1M0VLL12 / MK22FX512VLL12, 120 MHz Cortex-M4F, 128 KB SRAM).

Example firmware already in the folder: `s1_off000084_503080.bin` (Withings WBS05 scale, 503,080 bytes,
linked at 0x0, initial SP 0x20010000, references FlexNVM at 0x10000000 → **MK22FX512** memory layout).
Static scan shows it uses: SIM, all PORTs + GPIO, RTC, VBAT, ADC0, PDB0, LLWU, SMC/PMC/RCM, I2C0/1,
SPI0/1, UART0/1/2, USB0, DMA/DMAMUX, FTFE, SysTick/NVIC, DWT/ITM, WDOG, MCG, LPTMR, FTM0-2. It has a UART
console with a `gpio set/get` command.

Decisions made with the user:
- **CPU core: Unicorn** (QEMU-based) behind a small pluggable `Core` interface; other ISAs can be added later
  (Unicorn covers ARM/RISC-V/MIPS/…; a hand-written interpreter can implement the same interface).
- **Language: Python 3.14**, venv already at `.venv/` with unicorn 2.1.4, capstone 5.0.7, keystone,
  pyelftools, pyyaml installed.
- **Interface: CLI + REPL + VCD export, plus a local browser UI** drawing the LQFP100 with live pin states.
- **Firmware: user has real images** (the .bin above); I also build small clang test firmware for validation.

Facts established by probing Unicorn 2.1.4 (Cortex-M4 model, `UC_MODE_THUMB|UC_MODE_MCLASS`):
- `mmio_map` works and reports exact access offset/size for byte/half/word accesses.
- VFP instructions execute with no CPACR/FPEXC setup. `UC_ARM_REG_S0..S31`, `FPSCR` readable.
- `wfi` ends `emu_start` with PC just after the WFI (halfword at PC-2 == 0xBF30).
- `bx` to an EXC_RETURN value (0xFFFFFFFx) raises `UC_HOOK_INTR` intno **8** (EXCP_EXCEPTION_EXIT), then
  spins on intno 3 until stopped → we must handle exception return ourselves (unstack + emu_stop).
- 0xE000E000 (SCS) is **not** mapped: SysTick/NVIC/SCB must be our MMIO models.
- Speed: ~400 M insn/s with `count`-based slices and no hooks; ~8.5 M insn/s with a per-block Python hook.
- `UC_ARM_REG_PRIMASK/BASEPRI/FAULTMASK/CONTROL/MSP/PSP/IPSR/XPSR` exist; `UC_ARM_REG_CPACR` does not.
- Toolchain: `clang --target=thumbv7em-none-eabi -mcpu=cortex-m4 -mfloat-abi=hard -fuse-ld=lld
  -Wl,--image-base=0` builds Cortex-M4 firmware (no GNU arm toolchain installed; no qemu).

## Architecture

```
CHIP_EM/
  chipem/                    Python package
    cli.py                   `chipem` entry: info | run | shell | serve
    chip.py                  loads chips/*.yaml → ChipDescription (pins, memory, peripherals, irq names)
    bus.py                   maps RAM/flash into Unicorn, mmio_map() peripheral blocks, unknown-MMIO catch-all
    pins.py                  Pin model + PinBoard (drive/pull/external level resolution, change events)
    time.py                  virtual clock (cycles ↔ seconds), event scheduler for timers
    firmware.py              load .bin/.hex/.elf (pyelftools), optional load address; static analysis
    core/base.py             Core interface: reset(), run(max_insns), regs, pc, exception entry/return hooks
    core/unicorn_cm.py       Cortex-M implementation on Unicorn (slices, WFI, EXC_RETURN handling)
    arch/armv7m/scs.py       SysTick, NVIC (ISER/ICER/ISPR/ICPR/IPR), SCB (VTOR, AIRCR, SHPR, CCR, ICSR, CPUID)
    arch/armv7m/exceptions.py  priority resolution, stack-frame push/pop (8-word and 26-word FP frames)
    periph/base.py           MMIOBlock: bytearray backing store, named registers, read/write hooks, sub-word access
    periph/kinetis/{sim,port,gpio,wdog,mcg,osc,smc_pmc_rcm,pit,lptmr,uart,rtc,llwu,ftfe,stub}.py
    trace.py                 pin-change log, VCD writer
    web/server.py            stdlib http.server + SSE; web/static/index.html (SVG LQFP100 from chip YAML)
  chips/mk22fn1m0vll12.yaml  pinout (100 pins, ALT0-7 from datasheet §5.1), memory map, peripheral instances
  chips/mk22fx512vll12.yaml  same chip, 512K flash + 128K FlexNVM @0x10000000 + 4K FlexRAM @0x14000000
  firmware/samples/          C test programs + linker script + build.sh (clang/lld)
  tests/                     pytest: pin model, MMIO block, exception entry/exit, each sample firmware
  README.md
```

### Chip description (YAML, the "pinout + memory architecture" input)
- `core: {arch: armv7e-m, backend: unicorn, cpu: cortex-m4, fpu: true, clock_hz: 120000000}`
- `memory:` list of `{name, base, size, kind: flash|ram|flexnvm|flexram}`.
  MK22FN1M0: flash 0x0–0xFFFFF; SRAM_L 0x1FFF0000 (64K); SRAM_U 0x20000000 (64K).
  MK22FX512: flash 0x0–0x7FFFF; FlexNVM 0x10000000 (128K); FlexRAM 0x14000000 (4K); same SRAM.
- `peripherals:` list of `{name, model, base, size, params}` e.g. `{name: PORTC, model: kinetis.port, base: 0x4004B000, params: {port: C}}`,
  `{name: GPIOC, model: kinetis.gpio, base: 0x400FF080, size: 0x40}`, SIM 0x40047000 (size 0x2000), WDOG 0x40052000,
  MCG 0x40064000, OSC 0x40065000, PIT 0x40037000, LPTMR0 0x40040000, UART0..3 0x4006A000+n*0x1000, UART4 0x400AB000,
  RTC 0x4003D000, VBAT 0x4003E000, LLWU 0x4007C000, PMC 0x4007D000, SMC 0x4007E000, RCM 0x4007F000, FTFE 0x40020000,
  everything else in 0x40000000–0x400FFFFF → `stub` (RAM-like, logs first access per register).
  Register offsets to be checked against the K22 Reference Manual (K22P100M120SF5RM) while implementing.
- `irqs:` K22 IRQ 0..~85 names (DMA0.., WDOG_EWM=22, UART0_RX_TX=31, PIT0=48.., PORTA=59..PORTE=63, LPTMR0=58 …) used to
  label vector-table entries and NVIC activity.
- `pins:` 100 entries `{num, name, functions: {ALT0:.., ALT1: PTE0, …}, default, kind: gpio|analog|power|special, port: E, bit: 0}`
  transcribed from datasheet §5.1 (already extracted to text; see pinout table).

### Core + exception model (`core/unicorn_cm.py`, `arch/armv7m/`)
- Reset: SP=[VTOR+0], PC=[VTOR+4]; run in slices via `emu_start(pc|1, 0, count=N)`; N = min(cycles to next
  timer event, 20k). Virtual time advances by N cycles per completed slice (fast mode). Optional `--timing precise`
  installs a block hook with a capstone-derived per-block instruction count (exact counts, ~8 M insn/s).
- WFI: detected after early stop (halfword at PC-2 == 0xBF30) → jump virtual time to the next pending event.
- Interrupt entry (between slices): pick highest-priority pending+enabled exception whose priority beats the current
  execution priority (PRIMASK/BASEPRI/FAULTMASK read from Unicorn regs); push 8-word frame (or 26-word with FPSCR+S0-15
  when CONTROL.FPCA) at 8-byte-aligned SP on MSP/PSP per CONTROL.SPSEL; set LR=0xFFFFFFF9/FFFFFFFD/FFFFFFF1 (|0x10 when no FP
  frame); set IPSR; PC = [VTOR + 4*n]. Keep our own active-exception stack.
- Exception return: `UC_HOOK_INTR` intno 8 → read LR, `emu_stop()`, pop frame (26-word if EXC_RETURN bit 4 clear),
  restore CONTROL.SPSEL/FPCA, SP, IPSR, continue; re-check pending exceptions (tail-chain by just looping).
- SVC (intno 2) → exception 11 entry with the return address after the `svc`; PendSV via ICSR.PENDSVSET (exception
  14); SysTick (15); IRQn → 16+n. Priority = IPR/SHPR top bits grouped per AIRCR.PRIGROUP; execution priority
  considers PRIMASK, FAULTMASK, BASEPRI (non-zero BASEPRI masks priorities ≥ BASEPRI), and the active exception.
- AIRCR write with VECTKEY 0x05FA and SYSRESETREQ → full chip reset (logged). Faults (intno 3/4…): report PC and
  registers, stop, drop into the REPL.
- `Core` interface is backend-agnostic (`read_reg/write_reg/pc/sp/run(n)/set_pc`, bus access); interrupt logic sits in
  `arch/armv7m`, so another ARMv7-M chip reuses it and another ISA plugs in under `core/`.

### Peripheral framework (`periph/base.py`)
- `MMIOBlock(base, size)`: `bytearray` store; `Reg(name, offset, width, reset, ro_mask, w1c_mask, on_read, on_write)`;
  `read(off, size)`/`write(off, size, value)` handle sub-word accesses, then invoke the containing register's hooks
  with the full register value. Unknown offsets behave as RAM and are logged once (`--log-mmio` for all).
- Kinetis models for v1:
  - **SIM**: SCGCx clock gates (reset values from RM), SOPT1/2, CLKDIV1, SDID/FCFG/UID constants. Accessing a
    peripheral whose gate is off logs a warning (real chip hard-faults; optional `--strict-clock-gates`).
  - **PORT**: PCR0-31 (MUX bits 8-10, PE bit 1, PS bit 0, IRQC bits 16-19, ISF w1c), GPCLR/GPCHR, ISFR; pin interrupts
    (rising/falling/either/level) raise PORTx IRQ.
  - **GPIO**: PDOR/PSOR/PCOR/PTOR/PDIR/PDDR; PDIR built from the PinBoard; writes push drive state to pins.
  - **WDOG**: unlock sequence (0xC520,0xD928), STCTRLH WDOGEN, TOVAL, REFRESH (0xA602,0xB480), timeout → logged
    "watchdog reset" + core reset (modelled on the LPO 1 kHz clock).
  - **MCG/OSC**: status register mirrors control writes immediately (CLKST/IREFST/PLLST/LOCK0/OSCINIT0/IRCST) so SDK
    clock-init polling loops terminate; derives the core clock from MCG mode (FEI with DMX32/DRS → 24/48/72/96 MHz,
    PEE from PRDIV/VDIV × external 8 MHz default, IRC modes) and SIM_CLKDIV1 OUTDIV1; `--clock` overrides. The
    virtual-time clock re-scales when the core clock changes so SysTick/WDOG/LPO timings stay consistent.
  - **VBAT register file**: 32 bytes of plain RAM (battery-backed). **ADC0/ADC1**: SC3.CAL → COCO set, CALF clear;
    SC1n write → COCO set, result = configurable per-channel value (`--adc ADC0_SE14=2048`).
  - **SMC/PMC/RCM/LLWU**: reset-value stubs; PMSTAT=RUN; RCM_SRS0=POR; WFI + stop mode = time skip.
  - **SysTick/NVIC/SCB** (arch-level). **PIT** and **LPTMR** timers with IRQs. **UART0-4**: TX bytes to console/log,
    RX from `--uart-in`/REPL/web; status flags TDRE/TC/RDRF; RX IRQ. **RTC**: seconds counter from virtual time.
  - **FTFE/FlexNVM**: flash as memory; FTFE FSTAT reports CCIF=1; FCCOB read-resource/program commands minimal.
  - Everything else (ADC, PDB, DMA, USB, I2C, SPI, FTM, CAN, CRC, DAC, CMP, VREF, EWM, CMT, I2S): `stub` with logged
    first-touch; ADC/I2C/SPI given "always done" status bits so polling loops exit (ADC COCO=1, I2C TCF=1, SPI TCF=1).
- Pin model (`pins.py`): each pin has `mux` (from PCR), `dir` (PDDR), `drive` (PDOR), `pull` (PE/PS), `external` (user
  stimulus, None if floating). Resolved level: driven output → drive; else external if set; else pull; else `X`
  (floating, logged on read). Peripheral-owned functions (UART_TX etc.) show as e.g. `UART1_TX`. Changes emit events
  (virtual time, pin, old, new) to the trace log, VCD and web SSE.

### Static analysis (`firmware.py analyze`)
- Vector table: initial SP, reset PC, which IRQ slots point to non-default handlers (named via `irqs`) → "firmware
  installs handlers for PORTC, UART1_RX_TX, PIT0 …".
- Literal-pool scan of 32-bit words against the peripheral map → "references PORTC, GPIOC, UART1…" (what the firmware
  can touch). Scan `ldr/str` targets with capstone for PORTx_PCRn immediates where determinable → provisional pin roles.
- Dynamic report (always available after a run): per pin, how it was configured (mux, direction, pull) and when.

### CLI / REPL / web
- `chipem info chips/mk22fx512vll12.yaml fw.bin` → analysis report.
- `chipem run fw.bin --chip mk22fx512vll12 [--load-addr 0] --for 500ms --set PTC6=1 --at 50ms:PTC6=0 --uart-in "gpio get D0\n" --vcd out.vcd`
  → pin-change log, UART output, configuration summary.
- `chipem shell …` → `run 10ms | step | set PTC6 1 | pins | regs | mem 0x20000000 64 | bt | uart "..." | trace on`.
- `chipem serve …` → emulator in a background thread, `http.server` on localhost, SSE stream of pin/UART/time events,
  POST endpoints for set-pin/run/pause/reset/uart. `index.html` renders the LQFP100 from `/api/chip` (SVG, 25 pins per
  side, colour = level, click input pins to toggle, hover shows name/mux/function), UART console pane, event log.
  No JS build step; plain ES modules.

### Sample firmware (`firmware/samples/`, clang/lld)
`startup.c` (vector table, .data/.bss init), `k22.h` (register defs), `link.ld`; samples: `blinky` (PTC5 toggles via
busy loop), `button_mirror` (PTC6 in w/ pull-up → PTC5 out), `systick_irq`, `pit_irq`, `uart_echo`, `port_irq`.
Used by pytest to assert expected pin events and timings.

## Extensibility (user requirement: other Cortex-M3/M4 chips, and later TI C2000)

Design rules enforced from step 1 so adding chips/ISAs is data + a family package, not a rewrite:
- **Three layers, three directories.** `core/` = instruction execution only (Unicorn today; a hand-written interpreter
  later) behind `Core` (`run(n_insns)`, `read_reg/write_reg` by name, `pc`, `halted`, `attach_bus`).
  `arch/` = ISA-level machinery that is shared by every chip with that ISA: `arch/armv7m` (SCS, exception
  entry/return, EXC_RETURN, lazy FP), later `arch/c28x` (PIE interrupt controller, RPC/RTC stack, interrupt vectors).
  `periph/<family>/` = vendor peripheral models (`kinetis`, later `stm32`, `ti_c2000`). A chip YAML names
  `core`, `arch`, `family` and instantiates models by name.
- **Bus is address-unit agnostic.** `Bus(addr_unit_bits=8|16)`; MMIO register offsets/widths in the chip's address
  units; the Unicorn-backed bus maps units→bytes; a C28x bus is word-addressed natively. No peripheral code assumes
  bytes.
- **Pins are generic, muxing is per family.** `pins.py` knows only pin name/number/functions/level/drive/pull;
  the family's mux model (Kinetis PORT PCR; STM32 GPIO MODER/AFRL/AFRH; C2000 GPxMUX/GPxDIR/GPxPUD) resolves which
  function owns a pin and tells the PinBoard. UI/VCD/CLI never touch family code.
- **Register tables come from SVD** via `tools/svd2chip.py` (CMSIS-SVD exists for essentially every Cortex-M part;
  TI ships SVDs for C2000 in their `sysconfig`/CCS device packages too). A new Cortex-M chip = SVD + hand-entered
  pinout table + mapping of its peripherals to existing or new models; unmodelled peripherals automatically become
  register-aware stubs. Cortex-M3 = `cpu: cortex-m3, fpu: false` (Unicorn supports it; `arch/armv7m` already covers
  M3 since lazy FP is conditional on `fpu`). Cortex-M0/M0+ (ARMv6-M) works the same with `cpu: cortex-m0`.
- **Firmware loaders are pluggable** (`bin`, `ihex`, `elf`, later TI `COFF` .out for C2000 and `srec`).
- **Timing is in the chip's clock domain** (`time.py` in cycles; each arch supplies cycles-per-slice), so a C28x core
  that reports real cycle counts per instruction plugs straight in.

C2000 roadmap (not in v1; the architecture leaves room): `core/c28x/` interpreter for the C28x ISA (incl. FPU32,
optionally TMU/VCU), decoded via a table in `arch/c28x/isa.yaml` (opcode patterns → operand decoders → semantics
functions) so it is as description-driven as the irregular encoding allows; `arch/c28x/pie.py`; `periph/ti_c2000/`
GPIO mux, ePWM, ADC, SCI, CPU timers; `chips/tms320f28379d.yaml` etc. Realistic expectation: the interpreter is a
multi-week effort and will run at ~1–3 M insn/s in Python (fine for control-loop firmware at the scale of ms–s of
virtual time); if more speed is needed the interpreter can be ported to C/Rust later behind the same `Core` interface.

### Adding a chip (what the user will do)
1. `tools/svd2chip.py vendor.svd > chips/<part>_regs.yaml` (peripheral bases, registers, fields, reset values).
2. Write `chips/<part>.yaml`: core/arch/family, memory regions, pin table (from the datasheet pinout section, same
   shape as the K22 one), peripheral instances → model names, IRQ names.
3. If a family is new, add `periph/<family>/` models for GPIO/mux, clocks, watchdog and timers; everything else
   starts as a stub.
4. `chipem info chips/<part>.yaml fw.elf` then `chipem run …`.

## Implementation order (each step runnable)
1. Package skeleton, chip YAML (pins + memory + peripherals + irqs), `chipem info` with static analysis on the Withings bin.
2. Bus + Unicorn core + MMIOBlock + stub peripherals + GPIO/PORT/SIM/WDOG; `blinky` sample runs, pin log shows toggling.
3. SCS (SysTick/NVIC/SCB) + exception entry/return; `systick_irq`/`port_irq` samples; stimulus flags; VCD.
4. MCG/OSC/SMC/PMC/RCM/LLWU/PIT/LPTMR/UART/RTC/FTFE models; boot the Withings image until its main loop/console; log
   what it configures. Add whatever stub behaviour its polling loops need (see fork agent's boot-path trace below).
5. REPL, then web UI (`serve`), README.

## Verification
- `pytest tests/` — unit tests for pin resolution, MMIO sub-word semantics, frame push/pop round-trip, and each
  sample firmware (expected pin transitions at expected virtual times, UART output).
- `chipem run firmware/samples/blinky.elf --for 20ms` prints `PTC5 L→H` edges at the expected period.
- `chipem run s1_off000084_503080.bin --chip mk22fx512vll12 --for 2s --uart-in "help\n"` boots without faults, reports
  pin configuration (expected: scale load-cell/ADC pins, LEDs/display GPIOs, UART console TX) and shows console output.
- `chipem serve` → open the page, toggle an input, see output pins react live.

## Open points to confirm during implementation (not blockers)
- Register offsets/reset values: fetch the NXP CMSIS-SVD for this family (`MK22F12.svd` / `MK22FA12.svd`, e.g. from
  the `cmsis-svd/cmsis-svd-data` GitHub repo under `Freescale/`, or the MCUXpresso SDK device folder; Keil lists the
  pack as MK22FA12_DFP) and generate `chips/mk22_regs.yaml` (peripheral base, register offsets, widths, reset values,
  field names) from it with a small `tools/svd2chip.py`. Peripheral models then reference registers by name; the
  stub model becomes register-aware (correct reset values, read-only bits) for free. If the SVD cannot be fetched,
  fall back to hand-typed tables for the modelled peripherals only, offsets checked against the RM.
- Withings image: linked at **0x4000** (vector scoring: 75/97 vectors hit function prologues at base 0x4000, garbage
  at base 0). The bootloader at 0x0–0x3FFF is not in the file → `--load-addr 0x4000`, map 0x0–0x3FFF as zero-filled
  flash, preset VTOR=0x4000 (the image never writes VTOR; the bootloader would have). Second "table" at 0x24e74 is not
  a vector table.

## Boot-path findings for the Withings image (from static trace; these are hard requirements for step 4)
Reset 0x19a4c, SP 0x20010000. In order:
1. **WDOG** unlock 0xC520/0xD928 → STCTRLH keeps WDOGEN=1, PRESC=0, TOVAL=0x1388 (5 s @ 1 kHz LPO); refresh routine
   writes 0xA602/0xB480 to REFRESH. Model timeout + refresh on virtual time (LPO is independent of core clock).
2. **SIM_SCGC5 |= 0x3E00**; **MCG**: C1=0x04 then **polls MCG_S bit4 IREFST until set** (PC 0x1a2cc, mask 0x10);
   C4 DMX32=1/DRS=0 → FEI ≈ 24 MHz. SIM_CLKDIV1=0. SysTick RVR=23999 → **core clock must derive from MCG state
   (24 MHz here), not a fixed 120 MHz**; `--clock` overrides.
3. GPIO init via driver fn 0x1bc64 (PCR=0x100 MUX=1, outputs set DSE and PDDR): PTA13 input (later PCR IRQC=8,
   interrupt-when-low), PTB19=0, PTA14=1, PTA5=1, PTA12=0, PTB11=1, PTC14=0, PTC13 input. PORTA ISFR used by the
   PORT IRQ dispatcher (0x1bde8). These are the expected pin events of a successful boot.
4. **VBAT register file 0x4003E000** is used as battery-backed RAM for boot-trace → must be plain RAM. Reads/clears
   **LLWU_F1/F2** (0x4007C005/6); PMC_REGSC |= 8; SMC_PMPROT=0x22; SCGC6 |= RTC; RTC init.
5. .data copy flash 0x7e4ec→RAM 0x1fff05c8; .bss zero (two ranges). No VTOR write at startup.
6. NVIC: all IPR = 0xF0 (only top 4 priority bits), ISER writes; **AIRCR = 0x05FA0004 → SYSRESETREQ must reset the
   chip**; fault handler reads CFSR/HFSR/BFAR.
7. **ADC0 calibration**: SC3 |= 0x80 (CAL) then **polls SC1A bit7 COCO** (PC 0x1ae04, mask 0x80), expects SC3 CALF
   clear, reads CLP*/CLM* → ADC model: CAL write sets COCO, CALF=0; any conversion start sets COCO.
8. main (0x18378) creates an RTOS task and starts the scheduler: **SVC #0** (exception 11, Unicorn intno 2), **PendSV**
   (exception 14 via ICSR.PENDSVSET), **SysTick** (15), tasks on **PSP**, handlers set **BASEPRI=0x10**, and PendSV
   tests **EXC_RETURN bit 4** to save s16–s31 → **lazy/eager FP stacking must be real**: on entry with CONTROL.FPCA
   push the 26-word frame (s0–s15, FPSCR), clear bit 4 of EXC_RETURN, clear FPCA; on return restore them. CPACR
   write (0x4d0ac) enables the FPU before tasks run (Unicorn FPU is always on; model CPACR as a plain register).
9. Not in boot path but present: FTFE FSTAT CCIF polling (0x1c4d6/0x1c4f8), FMC 0x4001F000, PDB0 CNT delay loop
   (0x1cc8c), SPI0 SR polls (TXRXS/TXCTR/RXCTR at 0x1d12e/0x1d14e/0x1d178), UART0/1/2 driver at 0x1d4c0; console
   `putc` (0x34cc0) writes a RAM ring buffer that a UART driver drains. 3284 VFP instructions in the image.

Design consequences folded into the architecture above: BASEPRI + AIRCR.PRIGROUP gate injection; SVC/PendSV/SysTick
are first-class; slices ≤ ~2k instructions so a 24k-instruction SysTick period isn't skewed; VBAT/FlexRAM/unknown
blocks default to RAM-like storage; SCS handlers kept lean because RTOS ticks hit them ~10× per ms.
