# Adding a chip or a peripheral model

A new Cortex-M0/M3/M4 chip needs three things: a register table generated from the vendor SVD, a chip YAML
written by hand, and peripheral models. Models for a family that already exists can be reused. A new vendor
starts with its family's GPIO/mux, clock and watchdog models, and everything else begins as a stub. Nothing
in `core/`, `arch/` or the front ends should need to change.

Before starting, read the "Known gaps for non-Kinetis chips" section at the end. A few Kinetis assumptions
still sit in generic code.

## 1. Get the SVD

CMSIS-SVD files exist for nearly every Cortex-M part. Common sources are the vendor's CMSIS device pack
(Keil `*.pack` files are zip archives), the vendor SDK, or the `cmsis-svd/cmsis-svd-data` GitHub
repository. Pin a commit, and record the URL, SVD version and commit in `chips/README.md` the way the
`MK22F12.svd` entry does.

Store the download under `svd/dl-<something>/` and treat it as untrusted data. `tools/svd2chip.py` parses
it with `xml.etree` only. Run it with `python -I`.

## 2. Generate the register table

```sh
.venv/bin/python -I tools/svd2chip.py svd/dl-xyz/STM32F401.svd --flat -o chips/stm32f401_regs.yaml \
    --source "https://.../STM32F401.svd @ <commit> (SVD version x.y)"
```

- `--flat` inlines `derivedFrom` peripherals, so every block has its full register list. This is what
  `mk22_regs.yaml` uses.
- `--peripherals A,B,...` limits the output. `--enums auto|names|none` controls the enum labels.
- Never hand-edit a `*_regs.yaml`. Fix the SVD handling in the generator and regenerate.
- The output schema and the `regtable` Python API are documented in `chips/README.md`.

Many vendor SVDs mark write-1-to-clear flags as plain `read-write`. Models must then declare `w1c_mask`
themselves.

## 3. Write `chips/<part>.yaml`

This is the shape of `chips/mk22fn1m0vll12.yaml` (abridged; every key shown is read by
`chipem/chipdesc.py`):

```yaml
# NXP/Freescale Kinetis MK22FN1M0VLL12 (K22F, 120 MHz, 100-pin LQFP).
# Sources: data sheet section 5.1, reference manual memory map, device header IRQ numbering.
name: MK22FN1M0VLL12
package: LQFP100
family: kinetis
regs_file: mk22_regs.yaml
core: {arch: armv7e-m, backend: unicorn, cpu: cortex-m4, fpu: true, clock_hz: 120000000, addr_unit_bits: 8, num_irqs: 96}

memory:
  - {name: FLASH, base: 0x00000000, size: 0x100000, kind: flash}
  - {name: SRAM_L, base: 0x1FFF0000, size: 0x10000, kind: ram}
  - {name: SRAM_U, base: 0x20000000, size: 0x10000, kind: ram}

# armv7m.scs (0xE000E000, SysTick/NVIC/SCB) is created by the arch layer, not listed here.
peripherals:
  - {name: DMA, model: stub, base: 0x40008000, size: 0x1000}
  - {name: FTFE, model: kinetis.ftfe, base: 0x40020000, size: 0x1000}
  - {name: PIT, model: kinetis.pit, base: 0x40037000, size: 0x1000}
  - {name: ADC0, model: kinetis.adc, base: 0x4003B000, size: 0x1000, params: {index: 0}}
  - {name: SIM, model: kinetis.sim, base: 0x40047000, size: 0x2000}
  - {name: PORTC, model: kinetis.port, base: 0x4004B000, size: 0x1000, params: {port: C}}
  - {name: UART0, model: kinetis.uart, base: 0x4006A000, size: 0x1000, params: {index: 0}}
  - {name: GPIOC, model: kinetis.gpio, base: 0x400FF080, size: 0x40, params: {port: C}}
  # ...

# External interrupt numbers (exception = 16 + n).
irqs:
  0: DMA0
  22: WDOG_EWM
  31: UART0_RX_TX
  48: PIT0
  61: PORTC
  # ...

# All package pins (data sheet pinout table). functions: only columns with a value.
pins:
  - {num: 1, name: PTE0, kind: gpio, port: E, bit: 0, default: ADC1_SE4a, functions: {ALT0: ADC1_SE4a, ALT1: PTE0, ALT2: SPI1_PCS1, ALT3: UART1_TX, ALT4: SDHC0_D1, ALT5: TRACE_CLKOUT, ALT6: I2C1_SDA, ALT7: RTC_CLKOUT}}
  - {num: 8, name: VDD, kind: power, default: VDD, functions: {ALT0: VDD}}
  - {num: 14, name: ADC0_DP1, kind: analog, default: ADC0_DP1, functions: {ALT0: ADC0_DP1}}
  - {num: 34, name: PTA0, kind: gpio, port: A, bit: 0, default: JTAG_TCLK/SWD_CLK/EZP_CLK, functions: {ALT1: PTA0, ALT2: UART0_CTS_b, ALT3: FTM0_CH5, ALT7: JTAG_TCLK/SWD_CLK, EzPort: EZP_CLK}}
  - {num: 52, name: RESET_b, kind: special, default: RESET_b, functions: {ALT0: RESET_b}}
  - {num: 77, name: PTC5/LLWU_P9, kind: gpio, port: C, bit: 5, default: DISABLED, functions: {ALT1: PTC5/LLWU_P9, ALT2: SPI0_SCK, ALT3: LPTMR0_ALT2, ALT4: I2S0_RXD0, ALT5: FB_AD10, ALT6: CMP0_OUT, ALT7: FTM0_CH2}}
```

Field reference:

| key | meaning |
|---|---|
| `name`, `package` | display name; the package (`LQFP100`) is shown in the UI |
| `family` | vendor family. Informational: model names carry the family prefix themselves |
| `regs_file` | informational (see gaps below); models read `params.regs_file` |
| `core.arch` | `armv7e-m` (M4/M7), `armv7-m` (M3), `armv6-m` (M0/M0+) |
| `core.cpu` | `cortex-m0`, `cortex-m0plus`, `cortex-m3`, `cortex-m4`, `cortex-m7`, `cortex-m33` (Unicorn models) |
| `core.fpu` | enables FP frames and FP register reset |
| `core.clock_hz` | initial clock, used until a clock model calls `clock.set_hz` |
| `core.num_irqs` | NVIC size; raised automatically to cover every `irqs:` entry |
| `memory[]` | `{name, base, size, kind: flash\|ram\|flexnvm\|flexram\|rom, writable?, fill?}`; base and size must be 1 KB-aligned; flash/flexnvm/rom are filled with `fill` (0xFF) |
| `peripherals[]` | `{name, model, base, size (default 0x1000), params}`; `name` is also the key for the regtable lookup |
| `irqs` | `{n: name}`, 0-based external IRQ numbers; models look their IRQ up by name |
| `pins[]` | `{num, name, kind: gpio\|analog\|power\|special, port, bit, default, functions}` |
| `include:` | load another YAML first; this file's top-level keys replace it (lists are replaced, not merged) |
| extra keys | `vtor: 0x...` (reset VTOR), `bitband: true\|false` (default on for M3/M4) |

Pin table rules:

- `kind: gpio` pins need `port` and `bit`. The family's mux model finds pins through them.
- `default` is the function after reset. `DISABLED` or empty makes the pin start as `disabled`.
- `functions` keys are family-specific (`ALT0`..`ALT7`, `EzPort` on Kinetis; for STM32 use `AF0`..`AF15`
  plus whatever the mux model expects for analog). Values are the signal names exactly as the datasheet
  writes them. They become the pin's `owner` when muxed, so make them match what peripheral models and
  users will search for (`UART1_TX`).
- `power` pins are fixed high, except names containing `VSS` or `VREFL`, which are fixed low.
- Number pins exactly as the package does. The web UI draws a quad package counter-clockwise from pin 1
  with `n/4` pins per side.

Variants of the same die (different flash size) should `include:` the base file and override `name` and
`memory`, as `mk22fx512vll12.yaml` does.

Check the bases and IRQ numbers against the SVD:

```sh
.venv/bin/python -I tools/svd2chip.py svd/dl-xyz/STM32F401.svd --check chips/stm32f401rct6.yaml
```

This prints `MISMATCH`/`NOTE` lines and always exits 0.

## 4. Choose models

For each peripheral, either point `model:` at an existing model, write a new one (section 6), or leave it
as `stub`. A stub is RAM-like: firmware reads back what it wrote, and the first access to each word is
logged. This is enough for most init code. It is not enough when firmware polls a status bit that hardware
sets (ready/complete/lock flags). Such a loop spins forever. Before SPI was modelled, the Withings image
got stuck on SPI0 `SR.RXCTR` exactly like this. When `chipem run` shows the same PC every time or the speed collapses, find the loop with
`chipem shell ... --exec "run 1s; dis; bt"` and model that status bit.

The order of work that has paid off:

1. Pin mux + GPIO, so a blinky shows pin edges.
2. The clock tree, so SysTick and timer periods come out right.
3. Watchdog, so the firmware does not reset, or so you can see it reset.
4. Timer(s) with IRQs and the console UART.
5. Whatever the target firmware polls on.

## 5. A new vendor family

Create `chipem/periph/<family>/` with an `__init__.py` that imports all submodules. Copy
`periph/kinetis/__init__.py`, which uses `pkgutil.iter_modules` and records import errors instead of
raising. Model names are `<family>.<model>`. `registry.create("stm32.gpio", ...)` imports
`chipem.periph.stm32.gpio`, then `chipem.periph.stm32`.

What the family must provide:

- **A mux/GPIO model**: the only code that turns the vendor's pin registers into
  `PinBoard.mcu_update(pin, owner=..., direction=..., drive=..., pull=...)` calls, and the only code that
  turns resolved pin levels into input-register values. On Kinetis this is split across `kinetis.port`
  (PCR mux, pulls, pin interrupts) and `kinetis.gpio` (PDOR/PDDR/PDIR). An STM32 port would put
  MODER/AFRL/AFRH/PUPDR/ODR/BSRR/IDR in one `stm32.gpio`, with EXTI in a separate model. Requirements:
  - On `attach`, find your pins with `chip.pins.gpio(port, bit)` and register
    `chip.pins.input_listeners[pin.id].append(cb)` for anything that reacts to inputs (input-register
    caches, edge interrupts).
  - Report owners as the function names from the pin table (`pin.functions[...]`), `"GPIO"` for plain I/O,
    and the ALT0/analog function with direction `"analog"` for analog mode.
  - Read inputs from `pin.level()`. `X` (floating) normally reads as 0. Log a `warn` once.
- **A clock model**: the reset clock, the PLL/divider tree, and a `chip.clock.set_hz(core_hz)` call
  whenever firmware switches clocks. Status bits that firmware polls (PLL lock, clock-switch status) must
  mirror control writes immediately. Honour a frozen/override flag the way `kinetis.mcg` does, so `--clock`
  works (Session sets `mcg.frozen` on the block named `MCG`; a new family needs the same hook in
  `Session._apply_clock_override`). Expose the bus/peripheral clock to timer and UART models. Kinetis uses
  `kinetis/clockutil.bus_hz(chip)`.
- **A watchdog model**: either implement the unlock/refresh/timeout sequence and call
  `chip.reset("watchdog")` on expiry, or leave it a stub if the firmware only disables it. A stub watchdog
  never resets anything.
- **Reset-cause/power registers** if firmware reads them at boot. A stub returns 0, which firmware may
  interpret as an odd reset cause.

Register tables: `kinetis/_util.build_regs(names, size, overrides, regs_file)` builds `Reg` objects from
the regtable with per-register hook overrides. Pass `params.get("regs_file")` explicitly, otherwise it
defaults to `chips/mk22_regs.yaml`. You can import it from the Kinetis package or copy the 25-line helper
into your family's `_util.py`. Per-peripheral `params: {regs_file: chips/stm32f401_regs.yaml}` is resolved
relative to the current working directory.

## 6. Adding a peripheral model

A model is an `MMIOBlock` subclass registered under a name. Skeleton with a timer that raises a level IRQ
(compare `kinetis/pit.py` and `kinetis/lptmr.py`):

```python
# chipem/periph/acme/timer.py
from __future__ import annotations
from typing import Any

from ..base import MMIOBlock, Reg
from ..registry import register

CTRL_EN, CTRL_IE, FLAG_TIF = 1 << 0, 1 << 1, 1 << 0


@register("acme.timer")
class Timer(MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        r = [Reg("CTRL", 0x00, on_write=self._ctrl_write),
             Reg("LOAD", 0x04, reset=0xFFFF),
             Reg("FLAG", 0x08, w1c_mask=FLAG_TIF, on_write=self._flag_write)]
        # with an SVD table: r = build_regs((name, "TIMER"), size, {"CTRL": {"on_write": ...}}, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.tick_hz = float(params.get("tick_hz", 1_000_000))
        self._ev = None
        self._irqn: int | None = None

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        irqs = chip.desc.irqs                                   # look the IRQ up by name, never hard-code it
        self._irqn = next((n for n, nm in irqs.items() if nm == self.name), None)

    def reset(self) -> None:
        super().reset()                                         # registers back to reset values
        self._cancel()                                          # Chip.reset() does NOT clear the scheduler

    def _cancel(self) -> None:
        if self._ev is not None:
            self.chip.sched.cancel(self._ev)
            self._ev = None

    def _arm(self) -> None:
        self._cancel()
        period_ns = (self.get("LOAD") + 1) * 1e9 / self.tick_hz
        self._ev = self.chip.sched.in_ns(period_ns, self._expire)   # wall-clock domain; "now" is mid-slice exact

    def _expire(self) -> None:
        self._ev = None
        self.set("FLAG", self.get("FLAG") | FLAG_TIF)
        self._update_irq()
        if self.get("CTRL") & CTRL_EN:
            self._arm()

    def _update_irq(self) -> None:
        if self._irqn is not None:
            on = bool(self.get("FLAG") & FLAG_TIF and self.get("CTRL") & CTRL_IE)
            self.irq(self._irqn, on)                            # level line: deassert when the flag clears

    def _ctrl_write(self, blk, reg, old, new):
        if (old ^ new) & CTRL_EN:
            self._arm() if new & CTRL_EN else self._cancel()
            self.emit("info", block=self.name, msg=f"{self.name} {'started' if new & CTRL_EN else 'stopped'}")
        self._update_irq()
        return None                                             # keep the stored value

    def _flag_write(self, blk, reg, old, new):
        self._update_irq()                                      # w1c already applied to `new`
        return None
```

Then reference it from the chip YAML: `- {name: TIM0, model: acme.timer, base: 0x40010000, params: {tick_hz: 1e6}}`.

Rules (more in `CLAUDE.md`):

- **Hooks.** `on_read(block, reg) -> int | None` overrides the value read, without storing.
  `on_write(block, reg, old, new) -> int | None` runs after `new` (with `ro_mask`/`w1c_mask` applied) has
  been stored; a returned value is stored instead. Hooks always see whole-register values, even for byte
  or halfword accesses.
- **Use `get`/`set`** for programmatic access (no hooks). Use `self.emit(kind, ...)` for events, with
  timestamps taken from `chip.now_ns()`.
- **Scheduler.** Use `sched.in_ns`/`at_ns` for clocks independent of the core clock (rescaled
  automatically on core clock changes). Use `in_cycles`/`at_cycles` for core-clocked counters. Keep the
  `Event` handle. Cancel it in `reset()` and before rescheduling. Scheduling an event earlier than the
  current slice end from a register hook ends the slice after that instruction automatically.
- **IRQs.** `self.irq(n, level)` drives a level-sensitive line that re-pends after return while it is
  high. Use `self.chip.arch.pulse_irq(n)` for one-shot sources (RTC seconds pulse).
- **Clock gates.** If the family has gates, check them on access. Kinetis `GatedBlock` warns once per
  block.
- **Hot registers** (status registers polled in tight loops) should skip work: cache derived values and
  override `read`/`write` with fast paths for the hottest offsets, as `kinetis/gpio.py` does for PDIR and
  PSOR/PCOR/PTOR. Do not log per access.
- **Peers** are found in `attach` through `chip.peripherals` (by name, or by `model` attribute). Do not
  import other models' instances at module level.
- **Tests.** Build a small `ChipDescription` in code (see `tests/kutil.py`), assemble a few instructions
  with keystone (`tests/engine_util.build_image`), run with `chip.run_for(...)`, and assert on registers,
  `chip.log.filter("pin")`, or IRQ counts. Add an end-to-end sample in `firmware/samples/` if the model
  matters for real firmware.

## 7. Try it

```sh
.venv/bin/chipem info --chip chips/stm32f401rct6.yaml fw.elf        # vector table, references, pin roles
.venv/bin/chipem run  --chip chips/stm32f401rct6.yaml fw.elf --for 100ms
.venv/bin/chipem serve --chip chips/stm32f401rct6.yaml fw.elf --open
```

`chipem run` lists unmodelled stub accesses and peripherals whose model name did not resolve
(`unmodelled peripherals (RAM-like stubs): ...`) in its summary. Use that list as the to-do list.

## Known gaps for non-Kinetis chips

These live in generic code and will need small changes (in the owning module) for the first non-Kinetis
chip:

- **Pin ids are `PT<port><bit>`.** `Pin.id`/`gpio_name` and `PinBoard.gpio(port, bit)` build names that
  way, and the web UI's `isGpioOwner` matches `^PT[A-Z]\d+`. An STM32 pin `PA5` with `port: A, bit: 5` gets
  the id `PTA5`. The datasheet name `PA5` still works as an alias for `--set`.
- **Catch-all stub windows** are fixed to 0x40000000–0x400FFFFF and 0xE0000000–0xE00FFFFF
  (`bus.DEFAULT_CATCH_ALL`). STM32 AHB peripherals at 0x48000000/0x50000000 must be listed explicitly in
  `peripherals:`, or they bus-fault.
- **`regs_file`** at the top of the chip YAML is not passed to models. Set `params.regs_file` per
  peripheral, or have the family's helper read `chip.desc.regs_file`.
- **NVIC priority bits** are fixed at 4 (ARMv7-M) or 2 (ARMv6-M), not taken from the SVD's
  `nvic_prio_bits`. Parts with 3 bits (e.g. some NXP LPC) will accept one more bit than silicon.
- **`--clock` override** freezes only a block named `MCG` (`Session._apply_clock_override`).
- **Stubs are not register-aware.** `stub` ignores the regtable: reset values are 0, not the SVD reset
  values, and read-only bits are writable.
