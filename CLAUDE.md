# CLAUDE.md: working on CHIP_EM

CHIP_EM is a description-driven MCU emulator: chip YAML + firmware → run on Unicorn (ARM) or a pure-Python
C28x interpreter, with our own ARMv7-M / C28x-PIE interrupt models and Kinetis, STM32 and TI C2000 peripheral
models. Several chips can run on one board (nets, UART links). Start with `README.md` (`docs/USER_GUIDE.md` is the task-oriented user manual). `docs/ARCHITECTURE.md` explains
how it works, and `docs/CONTRACTS.md` defines the module interfaces.

## Running things

Always use the venv; there is no system install. Package and tool requirements: README "Requirements";
`.venv/bin/chipem doctor` checks them.

```sh
.venv/bin/python -m pytest -q                       # full suite (~1400 tests, ~45 s)
.venv/bin/python -m pytest -q tests/test_kinetis_uart.py -k rx
.venv/bin/chipem info|run|shell|serve <fw> [--chip mk22fn1m0vll12|mk22fx512vll12|path.yaml]
.venv/bin/chipem run firmware/samples/build/button_mirror.elf --for 3ms --at 1ms:PTC6=0
.venv/bin/chipem shell firmware/samples/build/blinky.elf --exec "run 1ms; pins changed; quit"
.venv/bin/chipem run s1_off000084_503080.bin --chip mk22fx512vll12 --for 2s --summary-only
firmware/samples/build.sh                           # rebuild samples (clang + ld.lld; no GNU arm toolchain here)
firmware/samples_stm32/build.sh; firmware/samples_c2000/build.sh   # STM32 (clang) / C28x (tools/c28x_asm.py)
firmware/samples_stm32_variants/build.sh           # STM32F405/F407 package-variant samples + STM32F427/F437 f42x_*.c (needs ld.lld)
.venv/bin/chipem run --board boards/stm32f427vg_f405rg_uart7.yaml --for 20ms  # F427VG (180 MHz, UART7) <-> F405RG
.venv/bin/chipem run --board boards/stm32f437ai_sdram.yaml --for 20ms         # F437AI FMC SDRAM init + memory test
firmware/samples_stm32_hal_f427/build.sh            # ST HAL built for STM32F427xx (over-drive, PLLSAI, UART7)
firmware/samples_stm32f429/build.sh; firmware/samples_stm32_hal_f429/build.sh   # STM32F429 LTDC + SDRAM (bare metal / HAL)
.venv/bin/chipem run --board boards/stm32f429zi_disco_lcd.yaml --for 250ms   # F429ZI LTDC -> lcd_panel part, SDRAM frame buffers
firmware/samples_stm32f4x1/build.sh; firmware/samples_stm32_hal_f4x1/build.sh   # STM32F401/F411 samples (+ HAL)
firmware/samples_stm32f446/build.sh; firmware/samples_stm32_hal_f446/build.sh   # STM32F446 QUADSPI/FMPI2C/CEC/SPDIFRX (bare metal / HAL)
.venv/bin/chipem run --board boards/stm32f446re_qspi_flash.yaml --for 70ms   # NUCLEO-F446RE + W25Q128 on QUADSPI
.venv/bin/chipem run --board boards/stm32f405rg_f407ig_uart.yaml --for 20ms   # LQFP64 F405RG <-> LQFP176 F407IG
firmware/samples_stm32f1/build.sh                   # STM32F103T6 (clang thumbv7m)
firmware/samples_stm32f1_variants/build.sh; firmware/samples_stm32f1_cl/build.sh   # F103 Blue Pill/high density; F105/F107
.venv/bin/python -I tools/gen_stm32f103_variants.py --check   # F103 package files are generated: edit the generator
firmware/samples_stm32f2/build.sh                   # STM32F205/F207/F215/F217 (clang thumbv7m, Cortex-M3)
.venv/bin/chipem run --board boards/stm32f103_stm32f405_can.yaml --for 35ms         # F103 bxCAN <-> F405 can_echo
firmware/samples_c2000_c/build.sh                   # C28x C with TI cl2000 (found under toolchains/; EABI + COFF .out)
.venv/bin/python tools/bench.py [--preset fast] [c2000]  # speed table (x real, MIPS per chip); not in the suite
.venv/bin/chipem run --board boards/c2000_stm32_uart.yaml --for 20ms --pin-report
.venv/bin/chipem run --board boards/k22_rs232_console.yaml   # PC software on the printed /dev/pts/N; real time, until Ctrl-C
.venv/bin/chipem info firmware/samples_c2000/build/blinky.c28img --chip tms320f28232
firmware/samples_mcs51/build.sh; .venv/bin/chipem run --board boards/p89cv51_stm32_uart.yaml --for 20ms   # 8051
firmware/samples_mcs51/c/build.sh                   # 8051 C with SDCC (sdcc or Fedora's sdcc-sdcc) -> c/build/*.ihx + .cdb
.venv/bin/python tools/mcs51_ucsim_diff.py --programs   # 8051 core vs ucsim (sdcc-s51 / s51), ~12 min; fast subset: test_mcs51_ucsim
.venv/bin/chipem run firmware/samples_c2000/build/f2808_adc.c28img --chip tms320f2808 --for 14ms --adc ADCINA0=1234
.venv/bin/python -I tools/gen_tms320f2808_regs.py [--check-ds ds.txt]   # regenerate the F2808 register table
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
| STM32F2 family | `chips/stm32f20[57]*.yaml`, `chips/stm32f21[57]*.yaml` (base `stm32f207zg.yaml`), `chips/stm32f2xx_regs.yaml`, `periph/stm32/f2.py` (`stm32.rcc_f2`, `stm32.pwr_f2`, `stm32.rtc_f2`, DMA map `f2`), `firmware/samples_stm32f2/*`, `boards/k22_stm32f207_{uart,can}.yaml`, `boards/stm32f207_eth_ping.yaml`; `test_stm32f2_chip`, `test_stm32f2_system`, `test_stm32f2_samples` |
| PWM / timer outputs, input capture | `periph/stm32/tim.py` (output compare, PWM, input capture, PWM input, dead time / break, encoder; TIM1-14), `periph/kinetis/ftm.py` (FTM0-3 + `ftm_alias`: PWM, input / dual edge capture, faults, quadrature, external clock), `periph/ti_c2000/epwm.py`, `periph/ti_c2000/ecap.py` (eCAP1-4), `periph/signals.py` and the family `signals.py` tables, `PinBoard.drive_signal`/`release_signal`/`input_listeners` in `pins.py`, samples `firmware/samples/{ftm_pwm,ftm_capture}.c`, `firmware/samples_stm32/{tim_pwm,tim_capture}.c`, `firmware/samples_c2000/{epwm,ecap}.asm`, `firmware/samples_c2000_c/epwm.c`, boards `pwm_led.yaml`, `k22_ftm_capture.yaml`, `c2000_ecap_loopback.yaml`, `pwm_capture_3way.yaml`; `test_stm32_pwm`, `test_stm32_tim_capture`, `test_kinetis_ftm`, `test_kinetis_ftm_capture`, `test_c2000_epwm`, `test_c2000_hrpwm`, `test_c2000_ecap`, `test_integration_families` (3-way board) |
| C2000 family | `chips/tms320f28232*` (register table hand-written), `chips/tms320f2833*`, `chips/tms320f2823[45].yaml` (F28335 superset + variants, ZHH/ZJZ ball files; `tms320f2833x_regs.yaml` generated by `tools/gen_tms320f2833x_regs.py`), `chips/tms320f280[1269].yaml`, `chips/tms320f2801[56].yaml` (F280x variants including `tms320f2808.yaml`), the variant keys in `chipdesc.py`, `periph/ti_c2000/*` (incl. `spi.py`, `mcbsp.py`, `xintf.py`), `arch/c28x/{arch,pie}.py`, `devices/sram.py` (board `sram` device, also usable by other external-bus models via `attach_memory()`), `firmware/samples_c2000/*` (incl. `spi_flash`, `mcbsp`, `xintf_sram`, `standby`), `boards/c2000_*.yaml` (incl. `c2000_spi_flash`, `c2000_mcbsp_flash`, `c2000_xintf_sram`); `test_c2000_*` (incl. `test_c2000_spi`, `test_c2000_mcbsp`, `test_c2000_xintf`, `test_c2000_lowpower`, `test_c2000_f2833x_chip`, `test_c2000_f2833x_samples`, `test_c2000_f280x_chip`), `test_engine_units` |
| TMS320F2808 (280x) | `chips/tms320f2808.yaml`, `chips/tms320f2808_regs.yaml` (generated: `tools/gen_tms320f2808_regs.py`, `--check-ds` against the data sheet), `chips/tms320f2808_bootrom.asm` (boot ROM stand-in, `boot.rom_words`), the 280x parameters of `periph/ti_c2000/{sysctrl,xint,misc,ecan}.py` (CLKINDIV layout and gates from the table, present XINTs, CSM `pwl_base`/`secure_ranges`, eCAN `clock_div`), `firmware/samples_c2000/f2808_*.asm`, `firmware/samples_c2000_c/f2808_*.c` + `f2808.{cmd,h}`, `boards/f2808_*.yaml`, `svd/c28x-docs/dl4-dl17` (280x guides); `test_c2000_f2808_{chip,models,samples}` |
| STM32F1 family | `chips/stm32f103t6.yaml`, `chips/stm32f103_regs.yaml`, `periph/stm32f1/*` (RCC, GPIO/AFIO, EXTI, PWR, BKP, RTC, FLASH/option bytes/system memory/DBGMCU, DMA, ADC, USB/PMA, bxCAN subclass), `firmware/samples_stm32f1/*`, `boards/stm32f103_*.yaml`; `test_stm32f103t6_chip`, `test_stm32f1_*` |
| STM32F103 variants | `chips/stm32f103{t4,c4,c6,r4,r6,t8,tb,c8,cb,r8,rb,v8,vb,rc,re,vc,ve,zc,ze}.yaml` (generated by `tools/gen_stm32f103_variants.py`, `--check`; bases `stm32f103vb`, `stm32f103ze`), `svd/dl-open-pin-data-f1`, `svd/dl-stm32f10[12]`, `firmware/samples_stm32f1_variants/*`, `boards/stm32f103c8_*`, `boards/stm32f103ze_*`; F1 flags in shared models (`stm32.dac`/`stm32.sdio` `f1`, `stm32.i2s` `i2s_clock`, `reserved_zero` in `stm32/_util.py`); `test_stm32f103_variants`, `test_stm32f103xx_chips`, `test_stm32f1_hd_samples`, `test_stm32f1_rm_system`, `test_stm32f1_rm_periph` |
| STM32F105/F107 connectivity line | `chips/stm32f10[57]{rc,vc}.yaml`, `chips/stm32f107_regs.yaml`, `periph/stm32f1/{rcc_cl,cl}.py` (RCC ch. 8, `stm32f1.eth`, `stm32f1.otg_fs`, `stm32f1.can_cl`, AFIO line `cl`), `tools/opd_stm32f1_cl_pins.py`, `svd/dl-stm32f10[57]`, `svd/dl-open-pin-data-f10x-cl`, `firmware/samples_stm32f1_cl/*`, `boards/stm32f10[57]*_{eth_ping,usb_cdc,dual_can}.yaml`; `test_stm32f10x_cl_chip`, `test_stm32f1_rcc_cl`, `test_stm32f10x_cl_samples` |
| C2000 Piccolo (F2802x / F2806x) | `chips/tms320f2802*.yaml`, `chips/tms320f28069*.yaml`, `chips/tms320f2806[2-7].yaml`, `chips/tms320f280{2,6}x_regs.yaml` (generated by `tools/gen_piccolo_regs.py`), `chips/tms320f280{2,6}x_bootrom.asm`, `periph/ti_c2000/{piccolo_sysctrl,piccolo_gpio,piccolo_misc,adc_soc,comp,epwm_dc}.py`, `firmware/samples_c2000/f28027_*.asm`, `firmware/samples_c2000_c/{piccolo.h,f2802x.cmd,f2806x.cmd,f28027_*.c,f28069_*.c}`, `boards/f28027_*.yaml`, `boards/f28069_stm32_can.yaml`, `boards/parts/ramp_source.py`; `test_c2000_piccolo_{chip,models,samples}` |
| C2000 C samples (TI compiler) | `firmware/samples_c2000_c/*` (C, `codestart.asm`, `f28232.cmd`, `build.sh`; incl. `mcbsp_spi.c`), `boards/c2000_stm32_chat_c.yaml`, `boards/c2000_mcbsp_flash.yaml`; `test_c2000_compiled` (skips if not built) |
| C28x core / ISA | `core/c28x/*` (interpreter, assembler, disassembler), `arch/c28x/isa.yaml` (instruction table), `tools/c28x_asm.py`; `test_c28x_*`. Encodings are verified against TI's manuals and cl2000/dis2000 25.11.1: see `docs/C28X_CORE.md` before editing |
| MCS-51 (8051) / P89CV51 | `core/mcs51/*` (interpreter, isa table, assembler/disassembler), `arch/mcs51/*` (IE/IP/IPH, CPU SFR block, debugger support), `periph/mcs51/*` (ports, timers 0/1/2, UART, SPI, PCA, WDT, IAP flash, sysctrl, xbus), `firmware_mcs51.py` (`chipem info`), `chips/p89cv51*.yaml` + `p89cv51_regs.yaml` (hand-written), `chips/i80c52*.yaml` (generic 80C52, not from a data sheet), `tools/mcs51_asm.py`, `tools/mcs51_ucsim_diff.py`, `chipem/sdcc_cdb.py` (SDCC .cdb line table for the debugger), `firmware/samples_mcs51/*` (incl. `c/`: SDCC samples, `p89cv51.h`), `boards/p89cv51_*.yaml`; `test_mcs51_*` (`tests/m51util.py`, `tests/mcs51_ucsim.py`: ucsim harness). See `docs/MCS51_CORE.md` |
| pin report / symbols | `pinreport.py`, `symbols.py`; `test_pinreport` |
| cross-family integration | `test_integration_families` (UART names, static pin roles, C2000 ↔ STM32 board) |
| SD cards / external memory buses | `devices/sdcard.py` (SD card device, `--sd-card`, board `sd_card`), `periph/sdbus.py` (clock `wave` summary, line-mux check), `periph/stm32/sdio.py`, `periph/kinetis/sdhc.py`, `periph/stm32/fsmc.py` (NE1-4 windows through `Bus.map_window` / `Core.map_shared`/`unmap`), samples `firmware/samples_stm32/{sdio_card,fsmc_sram}.c`, `firmware/samples/sdhc_card.c`, `firmware/samples_stm32_hal/sd_hal.c`, boards `stm32_sdcard.yaml`, `k22_sdcard.yaml`, `stm32_fsmc_sram.yaml`; `test_stm32_sdio`, `test_kinetis_sdhc`, `test_stm32_fsmc` |
| parts | `chipem/parts/*`, `tests/test_parts.py` (board parts: switches, LEDs, gates; wiring in `board.py`) |
| script parts (custom parts in Python) | `chipem/parts/script.py` (`ScriptPart` user API, `ScriptHost` part type `script`: SPI/I2C/UART helpers, faults, hot reload), `chipem/cmd_new_part.py` (`chipem new-part`), `boards/parts/*` (examples), web: `/api/part/reload`, `/api/part/script`, `allow_scripts` consent + `_script_guard` in `web/server.py`, `scriptControls`/editor in `app.js`; docs/USER_GUIDE.md 6.4; `test_script_part`. Scripts run with the user's privileges: no sandbox |
| Ethernet | `chipem/ethernet.py` (PHY, segment, host node, pcap, `EthLink`), `periph/stm32/eth.py` (`stm32.eth`, F407/F417), `firmware/samples_stm32/eth_ping.c`, `firmware/samples_stm32_hal_eth/*` (HAL ETH sample, own `build.sh`, STM32F407xx), `boards/stm32f407_eth_{ping,hal}.yaml`, `eth` links in `board.py`, `--eth-pcap`/`--eth-host`; `test_stm32_eth` |
| STM32F427/F437 | `chips/stm32f427*.yaml` (base `stm32f427vg.yaml`), `chips/stm32f437*.yaml`, `chips/stm32f427_regs.yaml` (from `svd/dl-stm32f427/STM32F427.svd`), `periph/stm32/f42x.py` (`stm32.rcc_f42x` PLLSAI/DCKCFGR/TIMPRE, `stm32.pwr_f42x` over-drive, `stm32.flash_f42x` dual bank), `periph/stm32/fmc.py` + `devices/sdram.py` (FMC SDRAM controller, board `sdram` device, `Session.attach_sdram`), `periph/stm32/sai.py`, `periph/stm32/dma2d.py`, GCM/CCM in `periph/stm32/cryp.py` (`gcm: true`), I2C_FLTR in `i2c.py`, F42x DMA requests in `dma.py`, `firmware/samples_stm32_variants/f42x_*.c` + `f42x/` (startup, link.ld, `stm32f42x.h`), `firmware/samples_stm32_hal_f427/*`, `boards/stm32f427vg_f405rg_uart7.yaml`, `boards/stm32f437ai_sdram.yaml`, `boards/stm32f427vg_sai_codec.yaml`; `test_stm32f427_*`, `test_stm32f437_crypto` |
| STM32F429/F439 + LTDC | `chips/stm32f429*.yaml` (base `stm32f429zi.yaml`), `chips/stm32f439*.yaml`, `chips/stm32f429_ltdc_regs.yaml` (generated, LTDC only), `periph/stm32/ltdc.py` (`ltdc_clk_hz()` in `f42x.py`), `parts/lcd_panel.py`, `firmware/samples_stm32f429/*`, `firmware/samples_stm32_hal_f429/*`, `boards/stm32f429zi_disco_lcd.yaml`; `test_stm32f429_pins`, `test_stm32f429_ltdc`, `test_stm32f429_samples` |
| STM32F401 / STM32F411 | `chips/stm32f401*.yaml`, `chips/stm32f411*.yaml`, `chips/stm32f401_regs.yaml` / `stm32f411_regs.yaml` (generated), `periph/stm32/f4x1.py` (`stm32.rcc_f4x1`, `stm32.pwr_f4x1`, `stm32.flash_f4x1`, DMA maps `f401`/`f411` via `dma.register_request_table`), `firmware/samples_stm32f4x1/*`, `firmware/samples_stm32_hal_f4x1/*`, `boards/stm32f411re_f401re_uart.yaml`, `boards/stm32f411_i2s5_codec.yaml`; `test_stm32f4x1_*` |
| STM32F446 | `chips/stm32f446*` (`stm32f446_regs.yaml`), `periph/stm32/{f446,quadspi}.py` (`stm32.rcc_f446`, `pwr_f446`, `flash_f446`, `syscfg_f446`, `fmc_f446`, `sysmem_f446`, `quadspi`, DMA map `f446`), `devices/qspi_flash.py`, `firmware/samples_stm32f446/*`, `firmware/samples_stm32_hal_f446/*`, `boards/stm32f446re_qspi_flash.yaml`; `test_stm32f446_*` (RM0390 regressions: `test_stm32f446_rm0390`) |
| STM32F446 FMPI2C / CEC / SPDIFRX | `periph/stm32/{fmpi2c,cec,spdifrx}.py`, `parts/spdif_source.py`, `firmware/samples_stm32f446/{fmpi2c_eeprom,cec_ping,cec_tv,spdifrx_rx}.c`, `firmware/samples_stm32_hal_f446/{fmpi2c_hal,cec_hal}.c`, boards `stm32f446re_fmpi2c_eeprom.yaml`, `stm32f446re_cec_pair.yaml`, `stm32f446re_cec_hal.yaml`, `stm32f446re_spdifrx.yaml`; `test_stm32f446_fmpi2c`, `test_stm32f446_cec`, `test_stm32f446_spdifrx` |
| STM32 I2S | `periph/stm32/i2s.py`, the I2S parts of `periph/stm32/spi.py` (`stm32.i2s_ext`), `rcc.py` PLLI2S, `firmware/samples_stm32/i2s_tone.c`, `boards/stm32_i2s_codec.yaml`; `test_stm32_i2s` |
| DCMI / camera | `periph/stm32/dcmi.py`, `parts/camera.py`, `firmware/samples_stm32/dcmi_capture.c`, `boards/stm32f407_dcmi_camera.yaml`; `test_stm32_dcmi` |
| crypto (CRYP/HASH) | `chipem/crypto_algos.py`, `periph/stm32/{cryp,hash}.py`, `chips/stm32f415zg.yaml`, `chips/stm32f417zg.yaml`; `test_stm32_cryp_hash` |
| host serial / real time / cable chains | `hostserial.py` (PTY, pyserial, TCP, memory ports; `UartBridge`), `realtime.py` (pacing), `parts/serial.py` (`usb_serial_adapter`, `rs232_transceiver`, connectors, `cable`, `SerialRouter`), `boards/k22_rs232_console.yaml`, `firmware/samples/uart_console.c`; `test_hostserial`, `test_serial_chain`, `test_web` (host serial) |
| USB | `usbbus.py` (bus engine, `UsbHost` + CDC-ACM/HID/MSC drivers, `UsbLink`, `UsbCdcBridge`), `periph/stm32/otg.py` (`stm32.otg_fs` / `stm32.otg_hs`), `periph/kinetis/{usbfs,usbdcd}.py`, `firmware/samples_stm32/usb_cdc.c`, `firmware/samples_stm32_hal/usb_cdc_hal.c` (+ `usbd_conf.h`; ST USB Device Library vendored in `firmware/vendor/STM32CubeF4/Middlewares`), `firmware/samples/usb_cdc.c`, `boards/{stm32_usb_cdc,stm32_usb_cdc_hal,k22_usb_cdc}.yaml`; `test_usbbus`, `test_stm32_usb`, `test_kinetis_usb` |
| run / session | `session.py`, `cmd_run.py`; `test_session`, `test_cmd_run` |
| speed / fidelity | `perf.py` (PerfSettings, presets, SpeedMeter), `Board.run_for` auto quantum / `chip_wall`, C28x decode cache, MMIO dispatch cache and self-branch skip (`core/c28x/core.py`), poll skip (`PollWatch` in `core/base.py`, `_poll_read` in `unicorn_cm.py` and the C28x core, `Chip._skip_poll`), `/api/perf`, the toolbar rate control and Status-pane performance block, `tools/bench.py`; `test_perf` |
| shell / web | `cmd_shell.py`, `cmd_serve.py`, `web/*`; `test_shell`, `test_web` |
| debugger / GDB server | `debug.py` (breakpoints, watchpoints, stepping, listing, call stack, peek memory), `dwarf.py` (line info, CFI), `gdbserver.py` (RSP), `shell_debug.py` (shell commands), `web/debugapi.py` + `web/static/{debug,regview}.js`, `debug.css` (Debug / Registers tabs), core hooks in `core/unicorn_cm.py` / `core/c28x/core.py` (`set_breakpoints`, `set_watchpoints`), `MMIOBlock.peek_reg`/`peek_bytes`; `test_debug`, `test_debug_shell`, `test_gdbserver` |
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
  SLEEPONEXIT is handled in `Chip._run`. Kinetis SMC and STM32 PWR share `chipem/periph/lowpower.Freezer`: blocks
  without `clock_stop` get their events held (helper objects set `lp_owner = block`), `lp_frozen` = the mode while held
  (ignore pin inputs then), and `time_shift(dns, dcycles)` on exit, where a model with an absolute start-time anchor
  must move it. C2000 uses `cycle_anchors` / `clock_resumed(dt_cycles)` instead.
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
- **FP gate (CPACR / Cortex-M3).** Unicorn 2.1 runs VFP on every M model and ignores CPACR. `core.set_fp_enabled`
  (called by the SCS on reset and CPACR writes; never on for `fpu: false`) clears ARMCPU.isar.mvfr0/1, located by
  calibration (`_calibrate_fp`), and flushes the TB cache, so QEMU's decoder raises NOCP (intno 17) at translation:
  no hooks. Firmware must enable CP10/CP11 before FP code, as on silicon (the samples' startup does). After a Unicorn
  upgrade check `UnicornCortexM(...).fp_gate` is True.
- **Stopping from MMIO.** Never `emu_stop()` from an MMIO callback: in a *write* callback it leaves PC at the store
  (re-executed on resume), in a *read* callback it aborts the load (re-read on resume). `request_stop(from_mmio=True)`
  and `end_slice_at(n)` (core/unicorn_cm.py) lower the running emu_start's instruction limit instead (the calibrated
  counter's neighbour word): the accessing instruction completes once, whatever it is (ldm/stm, writeback, IT
  block), and the slice ends in front of the next one, or exactly at an event scheduled from the callback
  (`Chip._early_event`). `Chip.stop()` from the emulation thread takes the same path. Only without the calibrated
  counter does the old capstone/plain-`str` fallback apply. The C28x and MCS-51 interpreters stop after the current
  instruction. Mapping changes from callbacks (`Bus.map_window`/`unmap_window`) are queued by the core and applied
  after that instruction (Unicorn raises a spurious prefetch abort after `uc_mem_unmap` inside a hook).
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
- **STM32F427/F437 are a different die (DEV_ID 0x419).** `chips/stm32f427vg.yaml` is their base file (own register
  table `stm32f427_regs.yaml`, RCC/PWR/FLASH models `stm32.*_f42x` subclassing the F405 ones; `_util.find_model`
  matches a subclass model by its base name, so `find_model(chip, "stm32.rcc")` finds `stm32.rcc_f42x`). CRYP/HASH
  are gated `only: [STM32F437, STM32F439]`; LTDC is `omitted:` (F429/F439 only). STM32F427.svd lacks several F42x
  registers/bits and has F405 reset values for the LPENR registers: `stm32.rcc_f42x` fixes them (with the SVD values
  the F42x-only blocks would be frozen in Sleep mode). The LQFP100 has no FMC_SDCLK (PG8): SDRAM samples run on the
  F437AI (UFBGA169). f42x samples use `firmware/samples_stm32_variants/f42x/startup.c` (91 vectors, RM0090 Table 63).
- **STM32F429/F439 = the F427 die + LTDC.** `chips/stm32f429zi.yaml` (base) keeps `stm32f427_regs.yaml` for the shared
  blocks; the LTDC registers come from `chips/stm32f429_ltdc_regs.yaml` (STM32F429.svd, LTDC only). IRQ 88/89 are named
  `LCD_TFT`/`LCD_TFT_1` (SVD); `stm32.ltdc` also accepts `LTDC`/`LTDC_ER`. The LTDC never schedules per pixel or per line:
  CPSR/CDSR are computed from time, and at most the line interrupt and the end of the active area are events.
- **STM32F401/F411 are verified against RM0368/RM0383** (`docs/datasheets/st/`, tests `test_stm32f4x1_rm_*.py`); their
  data sheets are still missing (pins from ST's open pin data). The RM wins over the SVD and the CubeF4 LL reset constants
  (e.g. PLLI2SCFGR 0x24003000, bit 26 reserved = 1). DMA maps are explicit per-line tables registered with
  `dma.register_request_table` (`params.requests` on DMA1/DMA2).
- **The STM32F446 is verified against RM0390** (`docs/datasheets/st/RM0390_*`, `test_stm32f446_rm0390.py`); DS10693 is
  still missing (pins from ST's open pin data). The RM wins over the SVD: the F446 subclasses (`stm32.rcc_f446`
  `_RM_REGS`, `syscfg_f446`, `fmc_f446`, `sysmem_f446`) and model params (`stm32.sdio` `ceata: false`, `stm32.otg_*`
  `core: otg2`, `stm32.exti` `reserved_edge_lines`) carry the corrections. The STM32F446.svd SDMMC block is the wrong
  IP (`stm32.sdio` falls back to the F405 SDIO table) and its IRQ list is wrong (the F446 chip YAML follows
  `stm32f446xx.h` = RM0390 Table 38). A board `spi_flash` device on a QUADSPI block becomes a
  `devices/qspi_flash.QspiNorFlash` (quad/dual commands, QE bit; `bank: 2` for dual-flash mode). F446 HAL samples that
  use IRQs above 81 must install their own vector table (the shared `samples_stm32_hal/startup.c` has the 82 F405
  vectors), as `cec_hal.c` does.
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
- **TMS320F2808 (280x) shares the `ti_c2000` models**; the differences are data: the register table (generated by
  `tools/gen_tms320f2808_regs.py`, never edit it) selects the sysctrl layout (PLLSTS.CLKINDIV, PCLKCR0/1 gates, no
  PCLKCR3) and which XINTs exist; the chip YAML passes CSM `pwl_base`/`secure_ranges` and eCAN `clock_div: 1` (280x
  eCAN clock = SYSCLKOUT). Reset runs a boot ROM stand-in (`chips/tms320f2808_bootrom.asm`, boot pins GPIO18/29/34)
  only when the image has no entry point.
- **FPU32 only on the F2833x parts** (`chips/tms320f28335.yaml`, F28334/F28333/F28332: `fpu: true`). The F28232 and
  the F28235/F28234 (F2823x) and every 280x part have no FPU: FPU opcodes trap (ITRAP), as on silicon. C for the
  F28335 is built with `--float_support=fpu32`.
- **C2000 part variants** `include:` a superset file (`tms320f28335.yaml`, `tms320f2808.yaml`) and use the generic
  `chipdesc` keys `drop:` (peripherals and IRQ names), `peripheral_params:` (merged into a peripheral's params) and
  `add_peripherals:`. Include stays one level deep. Per-part PARTID/CLASSID come from `ti_c2000.storage`
  `params.reset`; `SYSCTRL.max_hz` and `ADC.adcclk_max_hz` warn when firmware clocks a part above its rating.
- **MCS-51 address spaces are tagged ranges** (docs/MCS51_CORE.md): CODE 0x00000, XDATA 0x10000, IDATA 0x20000, SFR
  0x30080-0x300FF, on-chip XRAM 0x40000.  SFR models are sparse MMIO blocks (base 0x30000, offset = SFR address,
  `ranges` mapped by `Bus._map_sparse`).  The MCS-51 core counts OSCILLATOR periods (`cycle_counted`, 12 or 6 per
  machine cycle) and `core.clock_hz` / `--clock` is the crystal.  Its models set flags only from scheduler events (the
  core skips `JB/JNB bit,$` wait loops to the next event) and must schedule through `SfrBlock.at` so they act at the
  due cycle (a slice may end one instruction after it).  8051 pins are `P1.0`: never split a pin name on "." to strip
  a chip id unless the prefix IS a chip id.
- **8051 interrupt response includes the polling cycle**: a flag is not taken before flag time + 1 machine cycle (the
  instruction in that cycle completes; after an Idle/Power-down wake the cycle is charged).  The flag time is the due
  cycle of the `SfrBlock._fire` event that set it (`arch.event_due`): raise 8051 interrupt lines from events
  scheduled with `SfrBlock.at`, or the response is timed from the slice end.  Timer 2 is at 002Bh (not Table 41's
  003Bh: docs/MCS51_CORE.md "Timer 2 vector").
- **SDCC binaries carry a `sdcc-` prefix on Fedora** (`sdcc-sdcc`, `sdcc-sdas8051`, `sdcc-s51`, ...); upstream names are
  `sdcc`, `sdas8051`, `s51` (ucsim may also be `ucsim_51`).  Scripts and tests must try both and skip cleanly when
  absent.  SDCC 4.5 has no ELF/DWARF for mcs51: source-level debugging comes from the `.cdb` (`chipem/sdcc_cdb.py`).
  ucsim's console is only reliable on a pty with `-P` (NUL prompt), and it slows to ~0.1 s per command after about a
  second: `tests/mcs51_ucsim.py` restarts it every second.
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
- **Piccolo (F2802x / F2806x) models are subclasses** registered under their own names with `implements = ("ti_c2000.sysctrl",)`
  etc.; `_util.find_model` honours `implements`, so look models up with `find_model`, never by comparing `b.model`. The
  Piccolo ePWM (`epwm_dc`) overrides `EPWM._trip_actions`; keep that hook when editing `epwm.py`. The type-3 ADC
  (`adc_soc`) takes ePWM SOCs through `soc_listeners`, not `epwm_soc`. After reset a Piccolo runs at INTOSC1 / 1 =
  10 MHz (the boot ROM's DIVSEL = 3); the PLL locks in PLLLOCKPRD OSCCLK cycles (6.55 ms at reset). The PIE vector RAM
  is EALLOW protected: a boot ROM / test writing EMU_KEY (0x0D00) needs EALLOW. Serial FIFOs are 4 words
  (`params.fifo_depth`). PARTID is an OTP page block (`ti_c2000.partid`): the bus maps 0x40-word pages.
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
- **UART TX pins toggle.** `_tx_emit` hands each frame's edges to `PinBoard.drive_wave(tx_signal, ...)` (models set
  `tx_signal`; `tx_wave_od()` for half duplex). Edges are applied lazily with exact timestamps (no scheduler event per
  bit) unless an input listener has `wave_rt` or a board net observes the pin; GPIO input models call
  `pins.sync_waves()` on a cache miss and do not cache while `pins.wave_pending()`. An input listener that must see
  edges of a pin owned by another peripheral (pin IRQ, EXTI) sets `cb.wave_rt`.
- **UART characters travel as frames** (`uartwire.Frame`: real baud, data/9th/address bit, parity bit, stop bits,
  breaks). Transmit through `UartRx._tx_emit(frame)` (it feeds `frame_listeners` (links, router, host bridges) and the
  byte-level `tx_listeners`); receive through `_rx_frame(frame, src, t_end)` -> `LineDecoder` -> the model's
  `_rx_accept(RxChar)` (data + pe/fe/nf/brk/idle). A model describes its receiver with `rx_format()`. The fast path
  (same frame length, baud within the sample points) does no per-bit work; keep it that way. Tests that inject
  directly use `blk._rx_frame(fr, "host")` (no pin check) or `receive_frame(fr)` (pin must be muxed).
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
- **Debugger hooks cost nothing only while none are set.** Breakpoints are one bounded `UC_HOOK_CODE` per address
  (C28x: the decoded closure of that PC is wrapped), watchpoints ranged `UC_HOOK_MEM_READ/WRITE` hooks (C28x: their
  pages take the slow access path). While any hook exists Unicorn retranslates with slower helpers (about 20-35 %);
  deleting hooks schedules one more code-cache flush after the next run (`_wp_flush`), restoring full speed. A watch
  stop lowers Unicorn's private `emu_count` (the word after the calibrated counter) so the run ends right after the
  accessing instruction; without counter calibration the access would be repeated. A resume skips a breakpoint at the
  start PC once (`core.bp_skip`, set by a breakpoint stop and by `Chip.step`).
- **Reading registers for display must not have side effects.** Use `MMIOBlock.peek_reg`/`peek_bytes` (debugger,
  register browser, gdb `m`): a register with an `on_read` hook shows its stored value with exact=False unless the
  model lists it in `pure_reads`. Never call `bus.read` for display.
- **Speed settings are opt-in fidelity trades** (`chipem/perf.py`, docs/USER_GUIDE.md "Making it faster"). The default preset keeps the old behaviour. The C28x interpreter's self-branch skip (`spin_pc`) is always on and exact. Poll skip (`--poll-skip`, preset fast) fast-forwards MMIO polling loops whose CPU registers repeat. A model that derives a status bit from the time on each read, instead of from a scheduled event, is then seen up to `poll_skip_ns` late. So new models should flip status flags in scheduler events. A loop that writes RAM without a register trace is not detected as impure (rare). On ARM the polling load runs once more after a skip, so status reads must not have side effects when they return the same value. Keep the cost zero when off: `core._poll is None` is the only check on the MMIO read path. Auto quantum lets sleeping or parked chips lag. Anything scheduled on such a chip from outside must go through its scheduler (it does: nets, links, parts), which ends the sender's run (`Chip.idle_watch` -> `Board._idle_poke` -> `yield_run`). Threads do not help (measured 0.99x for an ARM and a C28x chip run in parallel; this Python is not free-threaded), and `PYTHON_JIT=1` gives about +10 % on pure C28x loops and nothing on MMIO-heavy code.
- **Not thread-safe.** Only the web `Worker` thread touches the Session/Chip. `Session.stop()` is the
  one call that is safe from another thread.
- **STM32F1 is its own family (`stm32f1`)**: GPIO CRL/CRH + AFIO remap, single-PLL RCC, FPEC flash, counter RTC.
  Same-IP blocks reuse `stm32.*` models by parameter (TIM2 `bits: 16`, TIM1 `irq: TIM1_UP`, IWDG `lsi_hz: 40000`;
  bxCAN as the `stm32f1.can` subclass). `stm32f1.rcc` exposes the `stm32.rcc` interface plus its own `GATES`/`RST_REGS`
  (F1 bit positions), which `stm32._util.rcc()`, `stm32.usart` and `stm32.tim` pick up. There is no AF number: the
  F1 GPIO derives a pin's owner from the AFIO remap and the peripheral's configuration and re-evaluates on writes to
  the peripherals' CR1/CR2/CR3/CCMRx/CCER/SMCR/BDTR (hooks wrapped by `stm32f1.afio`). The F1 IRQ names follow CMSIS
  (`USB_LP_CAN1_RX0`, `ADC1_2`, `TIM1_UP`); the SVD names are in the chip YAML's `svd_irq_names`. The F1 models are
  verified against RM0008 Rev 21 (docs/LIMITATIONS.md "STM32F1"). Density/line differences are parameters:
  `stm32f1.afio`/`stm32f1.dbgmcu` `density` (low/medium/high/xl/cl; `gpio.register_line` adds a line), `stm32f1.exti`
  `lines`, `stm32f1.bkp` `data_regs`, `stm32f1.flash` `page_size`, `stm32f1.can` `filter_banks`; put
  `reserved_zero: true` on reused tim/usart/spi/i2c entries.
- The arch layer uses 4 NVIC priority bits for ARMv7-M and 2 for ARMv6-M. These are not read from the
  SVD's `nvic_prio_bits`.
