# CLAUDE.md: working on CHIP_EM

CHIP_EM is a description-driven MCU emulator: chip YAML + firmware → run on Unicorn (ARM) or a pure-Python
C28x interpreter, with our own ARMv7-M / C28x-PIE interrupt models and Kinetis, STM32 and TI C2000 peripheral
models. Several chips can run on one board (nets, UART links). Start with `README.md` (`docs/USER_GUIDE.md` is the task-oriented user manual). `docs/ARCHITECTURE.md` explains
how it works, and `docs/CONTRACTS.md` defines the module interfaces.

## Running things

Always use the venv; there is no system install.

```sh
.venv/bin/python -m pytest -q                       # full suite (~1400 tests, ~45 s)
.venv/bin/python -m pytest -q tests/test_kinetis_uart.py -k rx
.venv/bin/chipem info|run|shell|serve <fw> [--chip mk22fn1m0vll12|mk22fx512vll12|path.yaml]
.venv/bin/chipem run firmware/samples/build/button_mirror.elf --for 3ms --at 1ms:PTC6=0
.venv/bin/chipem shell firmware/samples/build/blinky.elf --exec "run 1ms; pins changed; quit"
.venv/bin/chipem run s1_off000084_503080.bin --chip mk22fx512vll12 --for 2s --summary-only
firmware/samples/build.sh                           # rebuild samples (clang + ld.lld; no GNU arm toolchain here)
firmware/samples_stm32/build.sh; firmware/samples_c2000/build.sh   # STM32 (clang) / C28x (tools/c28x_asm.py)
firmware/samples_stm32_variants/build.sh           # STM32F405/F407 package-variant samples (needs ld.lld)
.venv/bin/chipem run --board boards/stm32f405rg_f407ig_uart.yaml --for 20ms   # LQFP64 F405RG <-> LQFP176 F407IG
firmware/samples_c2000_c/build.sh                   # C28x C with TI cl2000 (found under toolchains/; EABI + COFF .out)
.venv/bin/chipem run --board boards/c2000_stm32_uart.yaml --for 20ms --pin-report
.venv/bin/chipem run --board boards/k22_rs232_console.yaml   # PC software on the printed /dev/pts/N; real time, until Ctrl-C
.venv/bin/chipem info firmware/samples_c2000/build/blinky.c28img --chip tms320f28232
.venv/bin/python -I tools/svd2chip.py svd/dl-Sasv/MK22F12.svd --check chips/mk22fn1m0vll12.yaml
```

- Python 3.14 in `.venv`. Dependencies: unicorn 2.1.4, capstone, pyelftools, pyyaml; tests also need pytest
  and keystone-engine (the `test` extra in `pyproject.toml`; keystone is used by `tests/engine_util.py`).
- `chips/` and `chipem/web/static/` are located relative to the source tree (`cli.CHIPS_DIR`,
  `regtable.DEFAULT_REGS`), so run from the checkout.
- Treat downloaded SVDs (`svd/dl-*`) as untrusted data. Parse them only with `xml.etree` and run svd2chip
  with `python -I`.
- `chipem serve` blocks. In scripts, run it in the background with `--port 0` or a fixed port, then
  `curl` the `/api/*` endpoints.

## Adding a chip

Follow `docs/AI_NEW_CHIP_PLAYBOOK.md`, or run the `/new-chip <datasheet.pdf>` skill (`.claude/skills/new-chip/`), which
condenses it. `/new-board` wires chips into `boards/*.yaml` and checks them in `chipem serve`. Scaffold with
`.venv/bin/python -I tools/new_chip.py --part X --package P --family stm32|kinetis|ti_c2000 --svd S`, which writes the
chip YAML, the regs file and a test skeleton and never overwrites without `--force`. Validate pins with
`.venv/bin/python -I tools/pin_table_check.py chips/<part>.yaml`, which must give 0 errors; the three existing chips pass.
The chip is done only when the §J checklist of the playbook is complete. That includes spot-checked pins,
`svd2chip --check` at 0 mismatches, sample tests with timings and a board run.

## Architecture rule: three layers, three directories

| layer | directory | knows about | must not know about |
|---|---|---|---|
| core | `chipem/core/` | executing instructions (Unicorn today), registers, memory mapping, stop reasons | interrupts, peripherals, vendors |
| arch | `chipem/arch/<isa>/` (`armv7m`, `c28x`) | exception entry/return, NVIC/SysTick/SCB or PIE, priorities, FP frames | vendor peripherals |
| family | `chipem/periph/<family>/` (`kinetis`, `stm32`, `ti_c2000`) | vendor peripheral models, pin muxing, clock tree | Unicorn, exception stacking |

Generic glue sits beside them: `chip.py` (slice loop), `bus.py`, `time.py`, `pins.py`, `log.py`,
`session.py`. Generic code (pins, UI, VCD, CLI) must stay free of vendor logic. Only the family pin models
(Kinetis PORT/GPIO, STM32 GPIO, C2000 GPIO) map their registers to pin owner, direction and drive, and they
report it through `PinBoard.mcu_update` (and the pin report hooks). The STM32 reuses `arch/armv7m`. The C28x has
its own `core/c28x` (interpreter) and `arch/c28x` (PIE, CPU vectors).

`docs/CONTRACTS.md` defines the interfaces (Clock/Scheduler, Pin/PinBoard, Reg/MMIOBlock, Bus, Core,
ArmV7M, Chip, EventLog, Session). Parallel agents rely on it. Do not change a signature listed there
without updating the contract. Do not edit `docs/PLAN.md` or `docs/CONTRACTS.md` unless asked.

## Conventions

- **Register tables come from the SVD.** Models build their `Reg` list with
  `periph/kinetis/_util.build_regs((name, generic_name), size, overrides, params.get("regs_file"))`, which
  reads `chips/mk22_regs.yaml` through `regtable.regs_for`. Only behaviour (`on_read`, `on_write`,
  `w1c_mask`, `ro_mask`, reset overrides) is hand-written. Never edit a `*_regs.yaml`; regenerate it with
  `tools/svd2chip.py`. Kinetis w1c bits are plain read-write in this SVD, so models declare `w1c_mask`
  themselves. STM32 and C2000 have the same helper in their `_util.py` (`chips/stm32f405_regs.yaml` from ST's SVD;
  `chips/tms320f28232_regs.yaml` hand-written in the same schema because TI publishes no SVD, 16-bit units).
- **Models register by name.** Use `@register("family.model")` from `chipem.periph.registry`. The chip
  YAML refers to the name (`model: kinetis.uart`). `registry.create` imports `chipem.periph.<family>` on
  demand. The Kinetis package imports all its submodules, so a new file needs no registration edits. An
  unknown model name falls back to `stub` with a `warn` event. Constructor signature:
  `__init__(self, name, base, size, regs=(), *, params=None, log=None)`.
- **All observations go through `chip.log`** (`EventLog.event(kind, t_ns, **fields)`, kinds
  `pin mmio irq uart reset fault info warn`). In a model use `self.emit(kind, ...)`: it timestamps with
  `chip.now_ns()`. Do not print. The CLI, VCD, shell and web all subscribe to the log.
- **Peripherals must cancel their scheduler events on reset.** `Chip.reset()` does not clear the
  scheduler. Keep the `Event` handle, call `chip.sched.cancel(ev)` in `reset()` and before rescheduling.
  Use `sched.at_ns`/`in_ns` (wall-clock domain, rescaled when the core clock changes) for clocks that do not
  derive from the core (LPO, RTC, 32 kHz). Use `at_cycles`/`in_cycles` for core-clocked things (SysTick).
- **IRQs:** `self.irq(n, True/False)` drives a level-sensitive NVIC line (`arch.set_irq`). It re-pends on
  exception return while asserted, so a model must deassert when its flag clears. Use
  `chip.arch.pulse_irq(n)` for one-shot sources. Look IRQ numbers up by name from the chip YAML
  (`_util.irq_number(chip, "PIT0")`); never hard-code them.
- **Hot paths stay lean.** RTOS firmware hits SysTick/NVIC about 10 times per ms, and polling loops hit
  GPIO PDIR and UART S1 millions of times. Follow the existing patterns: cache PDIR, use `_RegBatch` for
  multi-register reads/writes in the exception path, fast-path `read()`/`write()` overrides for hot
  offsets, no logging unless `log_all`. Do not add per-instruction or per-block Unicorn hooks in fast mode.
- Mid-slice "now": use `chip.now_ns()` / `chip.now_cycles()` or `sched.now_ns()`. They add
  `core.insns_in_slice()` to `clock.cycles`. `chip.clock.now_ns()` alone is the slice start.
- Tests: put new model tests next to the existing ones (`tests/test_kinetis_*.py`, using `tests/kutil.py`
  and keystone-assembled snippets). Put end-to-end tests on sample firmware in `tests/test_samples_*.py`.
  Keep the suite fast (seconds).

## File ownership (who wrote what; check before large edits)

| area | files |
|---|---|
| engine | `core/*`, `arch/armv7m/*`, `bus.py`, `time.py`, `chip.py`, `periph/base.py`, `periph/registry.py`; tests `test_engine_*` |
| chip data / SVD | `chips/*`, `chipdesc.py`, `regtable.py`, `tools/svd2chip.py`, `svd/`; tests `test_chipdesc`, `test_svd2chip` |
| pins / trace / log | `pins.py`, `trace.py`, `log.py`; tests `test_pins`, `test_trace`, `test_log` |
| firmware / analysis | `firmware.py`, `cli.py` (`info`); `test_firmware` |
| Kinetis system models | `periph/kinetis/{sim,mcg,port,gpio,wdog,power,ftfe,adc,_util}.py`; `test_kinetis_system`, `test_samples_system` |
| Kinetis low power / clock monitors / UART flow control | `periph/kinetis/power.py` (SMC power hook, `_Freezer`, LLWU, RCM, PMC), `mcg.py` monitors, `clock_stop` in `pit/wdog/uart.py`, `uart.py` MODEM, `lptmr.py` pulse counter, sample `firmware/samples/lls_wakeup.c`; `test_kinetis_lowpower` |
| Kinetis timers / UART | `periph/kinetis/{pit,lptmr,rtc,uart,clockutil}.py`; `test_kinetis_timers`, `test_kinetis_uart`, `test_samples_timers` |
| Kinetis DMA / SPI | `periph/kinetis/{dma,dspi}.py` (added late; check their tests before editing); DSPI is bit-level on the pins (master + slave), `firmware/samples/spi_master.c`, `boards/k22_stm32_spi.yaml`; `test_kinetis_dspi`, `test_kinetis_dma` |
| Kinetis SAI / CMT / system blocks | `periph/kinetis/{sai,cmt,sysblocks,flexbus}.py` (I2S0, CMT, MCM/FMC/AIPS/AXBS/MPU, FlexBus + `FB_SPACE`), `parts/i2s_codec.py`, samples `firmware/samples/{i2s_tone,cmt_nec}.c`, `boards/k22_i2s_codec.yaml`; `test_kinetis_sai`, `test_kinetis_cmt`, `test_kinetis_sysblocks` |
| STM32 family | `chips/stm32f405*`, `chips/stm32f407*` (package variants, include `stm32f405zg.yaml`), `periph/stm32/*`, `firmware/samples_stm32/*`, `firmware/samples_stm32_variants/*`, `boards/stm32f405rg_f407ig_uart.yaml`, `firmware/samples_stm32_hal/*` (ST HAL; vendor code in `firmware/vendor/`), `boards/k22_stm32_uart.yaml`, `boards/c2000_stm32_chat_hal.yaml`, `boards/k22_stm32_can.yaml` and `boards/k22_c2000_stm32_can.yaml` (bxCAN `periph/stm32/can.py` on `chipem/canbus.py`); `test_stm32*` (variants: `test_stm32_variants`; bxCAN: `test_stm32_can`) |
| PWM / timer outputs, input capture | `periph/stm32/tim.py` (output compare, PWM, input capture, PWM input, dead time / break, encoder; TIM1-14), `periph/kinetis/ftm.py` (FTM0-3 + `ftm_alias`: PWM, input / dual edge capture, faults, quadrature, external clock), `periph/ti_c2000/epwm.py`, `periph/ti_c2000/ecap.py` (eCAP1-4), `periph/signals.py` and the family `signals.py` tables, `PinBoard.drive_signal`/`release_signal`/`input_listeners` in `pins.py`, samples `firmware/samples/{ftm_pwm,ftm_capture}.c`, `firmware/samples_stm32/{tim_pwm,tim_capture}.c`, `firmware/samples_c2000/{epwm,ecap}.asm`, `firmware/samples_c2000_c/epwm.c`, boards `pwm_led.yaml`, `k22_ftm_capture.yaml`, `c2000_ecap_loopback.yaml`, `pwm_capture_3way.yaml`; `test_stm32_pwm`, `test_stm32_tim_capture`, `test_kinetis_ftm`, `test_kinetis_ftm_capture`, `test_c2000_epwm`, `test_c2000_hrpwm`, `test_c2000_ecap`, `test_integration_families` (3-way board) |
| C2000 family | `chips/tms320f28232*` (register table hand-written), `periph/ti_c2000/*` (incl. `spi.py`, `mcbsp.py`, `xintf.py`), `arch/c28x/{arch,pie}.py`, `devices/sram.py` (board `sram` device, also usable by other external-bus models via `attach_memory()`), `firmware/samples_c2000/*` (incl. `spi_flash`, `mcbsp`, `xintf_sram`, `standby`), `boards/c2000_*.yaml` (incl. `c2000_spi_flash`, `c2000_mcbsp_flash`, `c2000_xintf_sram`); `test_c2000_*` (incl. `test_c2000_spi`, `test_c2000_mcbsp`, `test_c2000_xintf`, `test_c2000_lowpower`), `test_engine_units` |
| C2000 C samples (TI compiler) | `firmware/samples_c2000_c/*` (C, `codestart.asm`, `f28232.cmd`, `build.sh`; incl. `mcbsp_spi.c`), `boards/c2000_stm32_chat_c.yaml`, `boards/c2000_mcbsp_flash.yaml`; `test_c2000_compiled` (skips if not built) |
| C28x core / ISA | `core/c28x/*` (interpreter, assembler, disassembler), `arch/c28x/isa.yaml` (instruction table), `tools/c28x_asm.py`; `test_c28x_*`. Encodings are verified against TI's manuals and cl2000/dis2000 25.11.1: see `docs/C28X_CORE.md` before editing |
| pin report / symbols | `pinreport.py`, `symbols.py`; `test_pinreport` |
| cross-family integration | `test_integration_families` (UART names, static pin roles, C2000 ↔ STM32 board) |
| SD cards / external memory buses | `devices/sdcard.py` (SD card device, `--sd-card`, board `sd_card`), `periph/sdbus.py` (clock `wave` summary, line-mux check), `periph/stm32/sdio.py`, `periph/kinetis/sdhc.py`, `periph/stm32/fsmc.py` (NE1-4 windows through `Bus.map_window` / `Core.map_shared`/`unmap`), samples `firmware/samples_stm32/{sdio_card,fsmc_sram}.c`, `firmware/samples/sdhc_card.c`, `firmware/samples_stm32_hal/sd_hal.c`, boards `stm32_sdcard.yaml`, `k22_sdcard.yaml`, `stm32_fsmc_sram.yaml`; `test_stm32_sdio`, `test_kinetis_sdhc`, `test_stm32_fsmc` |
| parts | `chipem/parts/*`, `tests/test_parts.py` (board parts: switches, LEDs, gates; wiring in `board.py`) |
| Ethernet | `chipem/ethernet.py` (PHY, segment, host node, pcap, `EthLink`), `periph/stm32/eth.py` (`stm32.eth`, F407/F417), `firmware/samples_stm32/eth_ping.c`, `firmware/samples_stm32_hal_eth/*` (HAL ETH sample, own `build.sh`, STM32F407xx), `boards/stm32f407_eth_{ping,hal}.yaml`, `eth` links in `board.py`, `--eth-pcap`/`--eth-host`; `test_stm32_eth` |
| STM32 I2S | `periph/stm32/i2s.py`, the I2S parts of `periph/stm32/spi.py` (`stm32.i2s_ext`), `rcc.py` PLLI2S, `firmware/samples_stm32/i2s_tone.c`, `boards/stm32_i2s_codec.yaml`; `test_stm32_i2s` |
| DCMI / camera | `periph/stm32/dcmi.py`, `parts/camera.py`, `firmware/samples_stm32/dcmi_capture.c`, `boards/stm32f407_dcmi_camera.yaml`; `test_stm32_dcmi` |
| crypto (CRYP/HASH) | `chipem/crypto_algos.py`, `periph/stm32/{cryp,hash}.py`, `chips/stm32f415zg.yaml`, `chips/stm32f417zg.yaml`; `test_stm32_cryp_hash` |
| host serial / real time / cable chains | `hostserial.py` (PTY, pyserial, TCP, memory ports; `UartBridge`), `realtime.py` (pacing), `parts/serial.py` (`usb_serial_adapter`, `rs232_transceiver`, connectors, `cable`, `SerialRouter`), `boards/k22_rs232_console.yaml`, `firmware/samples/uart_console.c`; `test_hostserial`, `test_serial_chain`, `test_web` (host serial) |
| USB | `usbbus.py` (bus engine, `UsbHost` + CDC-ACM/HID/MSC drivers, `UsbLink`, `UsbCdcBridge`), `periph/stm32/otg.py` (`stm32.otg_fs` / `stm32.otg_hs`), `periph/kinetis/{usbfs,usbdcd}.py`, `firmware/samples_stm32/usb_cdc.c`, `firmware/samples_stm32_hal/usb_cdc_hal.c` (+ `usbd_conf.h`; ST USB Device Library vendored in `firmware/vendor/STM32CubeF4/Middlewares`), `firmware/samples/usb_cdc.c`, `boards/{stm32_usb_cdc,stm32_usb_cdc_hal,k22_usb_cdc}.yaml`; `test_usbbus`, `test_stm32_usb`, `test_kinetis_usb` |
| run / session | `session.py`, `cmd_run.py`; `test_session`, `test_cmd_run` |
| shell / web | `cmd_shell.py`, `cmd_serve.py`, `web/*`; `test_shell`, `test_web` |
| samples | `firmware/samples/*` |
| docs | `README.md`, `CLAUDE.md`, `docs/ARCHITECTURE.md`, `docs/ADDING_A_CHIP.md`, `docs/LIMITATIONS.md`, `docs/C2000_ROADMAP.md` |

## Gotchas

- **Unicorn register write order on exception entry/return.** QEMU ignores MSR-style writes to
  MSP/PSP/CONTROL while unprivileged, and an xPSR write that changes IPSR performs the MSP/PSP banking
  switch. Entry writes `sp → xpsr → control → lr → pc`. Return writes `control, msp, psp` while still in
  handler mode and only then `xpsr` (`arch/armv7m/exceptions.py`, `_ENTRY_W`/`_RET_W`). Reordering them
  silently corrupts stacks.
- **EXC_RETURN and FP.** A branch to 0xFFFFFFFx raises `UC_HOOK_INTR` intno 8, after which Unicorn spins on
  intno 3. The core records it and `emu_stop`s, and the arch unstacks. Unicorn runs M-profile in Secure
  state, so the return path restores CONTROL.FPCA **and** SFPA (bits 2 and 3). Otherwise the next FP
  instruction opens a fresh FP context (FPSCR := FPDSCR).
- **SVC** is intno 2 with PC already past the `svc`. **BKPT** is intno 7. `wfi`/`wfe` end `emu_start` with
  PC just after the instruction. The core detects this by checking the halfword at PC-2 (0xBF30/0xBF20).
  Unicorn 2.1 actually raises `wfe` as `UC_ERR_INSN_INVALID` (PC past it); the core turns that into a WFI stop with
  `stop_info = "wfe"`.
- **Low-power modes** are family logic on top of a generic sleep: `chip.power_hooks` (`on_sleep`/`on_wake`),
  `chip.wake_gate`, `chip.wake()`, optional `clock_stop(stopped, mode)` on blocks (CONTRACTS.md "Low-power modes").
  SLEEPONEXIT is handled in `Chip._run`.
- **VTOR must be preset for images linked above 0.** The Withings image (linked at 0x4000) never writes
  VTOR; its bootloader did. Session sets `vtor = image.start` for raw `.bin` images, and `Chip.reset()`
  falls back to the image start if `[VTOR]` is not a valid vector table. For ELF images VTOR defaults to 0
  unless `--vtor` is given.
- **Default clock after reset is 20.97 MHz (FEI), not 120 MHz.** `clock_hz: 120000000` in the YAML is only
  the nominal value. The MCG model calls `clock.set_hz()` from its reset state (32768 × 640). The samples
  assume 120 MHz core / 60 MHz bus without programming the MCG, so tests pass `clock_hz=` (or the CLI
  `--clock`), which freezes the MCG (`mcg.frozen`).
- **Pin timestamps.** In fast mode the Chip only advances `clock.cycles` at slice end. Pin and MMIO events
  are timestamped with `chip.now_ns()` (`PinBoard.time_fn`), which adds the in-slice instruction count, so
  edges are instruction-precise. Anything that timestamps with `clock.now_ns()` directly is up to one
  slice (2000 instructions) early.
- **Instruction counter calibration** (`core/unicorn_cm.py`, `_calibrate_counter`). Unicorn's
  `emu_start(count=N)` increments a private `uc_struct.emu_counter`. We find its offset once with ctypes by
  running known counts (1234, 777, 50001) and looking for adjacent words equal to `count+1` and `count`.
  This makes early-stop instruction counts exact in fast mode. It depends on Unicorn 2.1.x internals: after
  a Unicorn upgrade check that `UnicornCortexM(...).exact_counts` is True. If calibration fails, early
  stops are charged half the slice and the Chip limits handler-mode slices to 256 instructions.
- **Stopping from MMIO.** `emu_stop()` inside an MMIO *write* callback leaves PC at the store. The core
  steps PC past it only for a plain single-register `str/strb/strh` without writeback, outside an IT block
  (checked with capstone). Otherwise the stop request is refused and the effect (e.g. a newly pended IRQ)
  is noticed at the end of the slice.
- **Flash is writable by the CPU** (`MemoryDesc.writable=None` → writable in `Chip`). FTFE commands modify
  memory directly too.
- **Clock gates only warn.** Accessing a gated-off PORT/ADC/FTFE logs one `warn` and proceeds. Real silicon
  bus-faults.
- **`regs_file` at the top of a chip YAML is informational.** Kinetis models take
  `params.regs_file` (resolved against the current directory) or fall back to `chips/mk22_regs.yaml`.
- **Die-family gating.** The STM32F405/F407/F415/F417 share one base YAML; Ethernet/DCMI (F407/F417) and CRYP/HASH
  (F415/F417) carry `params: {only: [...]}` and become stubs elsewhere (`_util.part_present` / `make_absent`).
- **STM32F405/F407 variants share one die.** `chips/stm32f40[57]*.yaml` `include:` `stm32f405zg.yaml` and override
  only name, package, memory and pins (include is one level deep, so a variant cannot include another variant). The
  pin tables were generated from DS8626 Table 7/9 and checked against ST's open pin data (`chips/README.md`);
  `tests/test_stm32_variants.py` asserts every shared pin equals the LQFP144 file, so fix a pin in all files. BGA/WLCSP files use a row-major ball index as `num` plus a `ball:` key; the UFBGA176 files have 201
  balls (`pin_table_check.py --expect 201`).
- **Pin ids come from `gpio_name_format`** in the chip YAML: Kinetis `PT{port}{bit}` (default), STM32
  `P{port}{bit}`, C2000 `GPIO{num}`. Generic code (web `isGpioFunction`, pin report, firmware pin roles) must not
  assume the `PT` prefix.
- **UARTs are found by capability**, not by model name: any block with `tx_listeners` (`Chip.uart_blocks()`).
  `Chip.find_uart(name)` resolves `USART1`/`UART1`/`SCIA`/`SCI-A` uniformly; Session, shell, run summary, web and
  board links use it.
- **16-bit address units (C28x).** On the F28232 every address, offset, `Reg.width`, `--load-addr` and event address
  is a 16-bit word; a width-2 register is 32 bits, low word first. `MMIOBlock`/`Reg` take `unit_bits` (the Chip
  injects `params["unit_bits"]`); `regtable` reads `address_unit_bits` from the table header, so `ro_mask`,
  `full_mask` and `reg_specs()` span `width * unit_bits` bits. `FirmwareImage.unit_bytes` is 2 and `read(addr, n)`
  counts words. Do not reuse ARM helpers that assume bytes (Thumb `read_half`, `addr & ~1`).
- **C28x images**: `.c28img` is JSON from `tools/c28x_asm.py` (segments as little-endian word streams, `entry`,
  `symbols`); TI `.out` is sniffed (ELF magic → ELF, COFF version 0x00C1/0x00C2 → COFF). There is no boot ROM:
  reset jumps to the image entry or 0x33FFF6 (`LB _c_int00`).
- **The F28232 has no FPU** (`fpu: false`; F2833x parts have FPU32). The interpreter still implements FPU32 for
  F28335-class chips.
- **C28x ISA status.** Every base `isa.yaml` entry is `conf: ref`: verified word for word against TI's cl2000 25.11.1
  assembler and against `dis2000` over TI's RTS libraries; TI-compiled C (`firmware/samples_c2000_c`) runs. Not
  covered: FPU64, FINTDIV, VCU/TMU/CLA. See `docs/C28X_CORE.md` ("Verified against cl2000 25.11.1 output").
- **C28x pipeline/timing**: one cycle per instruction, no pipeline (`RPTB` runs on FPU chips only); EALLOW protection is enforced (a
  protected write without EALLOW is ignored with a warning). `core.pc` is only written back at slice end; use
  `pinreport._c28x_live_pc` (or the `pc` local of `C28xCore.run`) for the current instruction mid-slice.
- **C2000 peripheral pin inputs go through the GPIO model**: subscribe with `_util.listen_input(chip, pin, cb)` (not
  `board.input_listeners`) so GPxQSEL qualification applies and STANDBY/HALT (`clk_stopped`) mutes them. A model
  with a time-derived counter lists its absolute-cycle anchors in `cycle_anchors` so `C2000Block.clock_stop` (the
  STANDBY/HALT freeze run by `ti_c2000.sysctrl`, a `chip.power_hooks` entry) can shift them.
- **F28232 XINTF zones are not RAM.** PCLKCR3 resets with XINTFENCLK = 0, and zone data only reaches a board `sram`
  device once firmware enables the clock and muxes the XINTF pins (TI's InitXintf16Gpio); otherwise reads float.
- **C2000 bidirectional pins** (McBSP clocks/frame syncs, SPI master/slave) get their live role from the owning block:
  `owned_signals()` + `signal_state(sig)` (read by `ti_c2000.gpio`), `_util.gpio_resync(chip, sig)` after a change,
  `mux_changed()` when the GPIO mux moves a pin. Synchronous links between chips (McBSP, SPI) need a board quantum
  below half a bit period.
- **Clock changes re-base at the current instruction** (`Clock.slice_offset` = `core.insns_in_slice`), so
  timestamps stay monotonic across a mid-slice PLL switch. The Chip also ends the running slice after that access
  (`Chip._clock_changed`), since its length was computed in old-clock cycles (next event, end of the run window).
- **SysTick CLKSOURCE=0** counts at core / `systick_ext_div` (chip YAML, default 8 = STM32 HCLK/8). The K22 has no
  external reference (K22P100M120SF5V2RM §3.2.1.2): its YAML sets `systick_ext_div: 0`, so CLKSOURCE reads 1 and
  ignores writes, and SYST_CALIB = 0x80000000 (NOREF=1, TENMS=0).
- **Kinetis stop modes hold scheduler events.** In STOP/VLPS/LLS/VLLS the SMC (`kinetis/power.py` `_Freezer`) pulls
  the pending events of clock-stopped blocks (bound-method callbacks, owner = the block) out of the heap and pushes
  them back shifted by the sleep time. A model that computes a counter from a start time (`now - start`) must
  implement `clock_stop(stopped, mode)` (as PIT/WDOG/UART do) or its counter jumps by the time slept. Reset
  reasons drive the Kinetis reset domains: `reset_kind()` maps the RCM's last reason to por / warm / wakeup (VLLS).
- **Bus catch-all windows** (auto-stubs) default to 0x40000000–0x400FFFFF and 0xE0000000–0xE00FFFFF
  (`bus.DEFAULT_CATCH_ALL`); a chip YAML can set its own with `auto_stub_ranges` (the C2000 does). Elsewhere,
  unmapped accesses are bus faults. Memory regions must be aligned to
  Unicorn's 1 KB page. Sub-page MMIO blocks (GPIOx, 0x40 bytes) share a page dispatcher.
- **UART bytes are dropped when the receiver is not listening** (RE/UE/RXENA clear, gate off, module reset, RX
  pin not muxed) or on overrun, like silicon (`uart` event `dir=drop reason=...`; `chipem/periph/uartwire.py`).
  `--uart-in` is paced at baud from t=0; use `--at TIME:UARTn=TEXT` for later injection (e.g. once a console is up).
- **Board parts propagate with zero delay** (`Board._propagate`, waves within one event); a net re-resolved more than
  16 times in one event is reported as a combinational loop (one `warn`) and left as is. Timed parts schedule on the
  first chip's scheduler; `Session.reset` restarts them, firmware resets do not.
- **Host serial bridges keep the threading rule.** Port I/O threads (`chipem/hostserial.py`) never touch the
  Session/Chip: host bytes wait in the port's buffer until `pump()` drains them on the emulation thread
  (`realtime.run_paced`, between ≤ 2 ms slices), chip bytes leave through the port's queue, and I/O threads only set
  the Session's wake Event. A paced `run_for` takes wall time (`chipem run` with a bridge runs until Ctrl-C); tests
  run the Session on one thread and play the host on another, or use `memory` ports. `run_for` without endpoints or
  pacing is unchanged.
- **Serial cable chains carry bytes by path resolution** (`parts/serial.py`): a part that should pass UART bytes
  implements `serial_paths`; chip-to-chip nets without parts stay with the board's auto UART links. On RS-232 nets
  H = positive voltage (space), so an idle RS-232 line reads L.
- **Not thread-safe.** Only the web `Worker` thread touches the Session/Chip. `Session.stop()` is the
  one call that is safe from another thread.
- The arch layer uses 4 NVIC priority bits for ARMv7-M and 2 for ARMv6-M. These are not read from the
  SVD's `nvic_prio_bits`.
