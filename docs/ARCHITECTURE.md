# CHIP_EM architecture

This document describes how the emulator works as the code stands. Interfaces are specified in
[CONTRACTS.md](CONTRACTS.md), and the original design rationale is in [PLAN.md](PLAN.md).

## Overview

```
              chips/<part>.yaml ──load_chip──> ChipDescription (pins, memory, peripherals, irqs, core)
                                                     │
 firmware.bin/.elf ──load_firmware──> FirmwareImage   │
                                │                     ▼
                                └────────────>  Chip (chip.py)
                                                 ├─ clock: Clock            (time.py)
                                                 ├─ sched: Scheduler        (time.py)
                                                 ├─ log:   EventLog         (log.py)
                                                 ├─ pins:  PinBoard         (pins.py)
                                                 ├─ core:  UnicornCortexM   (core/unicorn_cm.py)
                                                 ├─ bus:   Bus              (bus.py)  ── maps memory + MMIO into the core
                                                 ├─ arch:  ArmV7M           (arch/armv7m/exceptions.py)
                                                 │           └─ scs: SCSBlock (SysTick/NVIC/SCB @0xE000E000)
                                                 └─ peripherals: {name: MMIOBlock}  (periph/kinetis/*, stub, ram)

  Session (session.py) = Chip + image + stimulus + VCD   ← used by `run`, `shell`, `serve`
```

`Chip.__init__` builds everything in a fixed order:

1. Clock, scheduler, event log and pin board (`PinBoard.from_desc`).
2. Core (`core.create_core(backend, cpu, fpu, precise)`) and Bus.
3. Memory regions. Flash, FlexNVM and ROM are filled with the region's `fill` byte (0xFF by default). All
   regions are writable.
4. `ArmV7M`, which creates the SCS block, mapped at 0xE000E000.
5. One model per `peripherals:` entry through `registry.create(model, name, base, size, params, chip)`,
   each mapped on the bus.
6. `attach(chip)` on every block, so models can find their peers (`find_model`) and the pin board.
7. `reset()` on every block.
8. Bit-band aliases (on by default for Cortex-M3/M4).

## The slice loop and virtual time

Time is counted in **core cycles** (`Clock.cycles`), and one executed instruction counts as one cycle.
`Clock.hz` can change at runtime (MCG/RCC/SYSCTRL reprogramming, `--clock`). `set_hz` re-bases the cycle→ns
conversion so that nanosecond time stays continuous and monotonic. The re-base point is the precise current
instruction: the Chip sets `Clock.slice_offset = core.insns_in_slice`, so a PLL switch written in the middle of a
slice changes the rate from that instruction on, and events later in the slice are not stamped earlier than the
ones before it. Scheduler events live in a heap keyed by absolute
cycle. Events created with `at_ns`/`in_ns` also remember their nanosecond deadline and are converted back
to cycles after every frequency change, so a 1 kHz LPO watchdog keeps its real-time period when the core
clock moves.

`Chip.run_for(ns)` / `run_cycles(n)` / `step()` all go through `Chip._run`:

```
loop:
  stop flag set?                 → return USER
  arch.reset_requested?          → chip.reset(reason)        (SYSRESETREQ, watchdog, ...)
  scheduler events due?          → sched.run_due()           (timers, stimulus, UART pacing ...)
  now >= target?                 → return COUNT
  sleeping (after WFI)?
      wake-up pending?           → sleeping = False
      else                       → clock.advance(to next event or target); continue   ← time skip
  arch.lockup?                   → return FAULT
  arch.pending?                  → arch.service()            (exception entry, between slices)
  n = min(target - now, next_due - now, max_slice=2000)
  (fast mode without exact counts, handler active: n = min(n, handler_slice=256))
  sched.horizon = now + n
  reason, executed = core.run(n)                             (Unicorn emu_start(count=n))
  clock.advance(executed)
  COUNT / USER   → continue
  EXC_RETURN/SVC → arch.on_stop(...)                         (unstack / enter SVCall)
  WFI            → sleeping = True unless a wake-up is already pending
  FAULT          → arch.on_stop → HardFault entry (or lockup / --stop-on-fault → return FAULT)
  BREAKPOINT     → return
```

Properties of this loop:

- **Slices end on scheduler events.** A slice never runs past the next due event, so timer expiries fire at
  their exact cycle. If an MMIO callback schedules an event earlier than the current slice's end
  (`sched.horizon`), the scheduler calls `chip._early_event` → `core.request_stop(from_mmio=True)`, and the
  slice ends right after the accessing instruction.
- **Exceptions are taken between slices.** When something pends an exception during a slice and it could
  preempt now, `ArmV7M.set_pending_exc` asks the core to stop after the current instruction. The stop is
  only possible after a plain store (see "Stopping from MMIO" below). Otherwise the exception is taken at
  the end of the slice, up to 2000 instructions late.
- **WFI/WFE skip time.** After a WFI the chip advances the clock directly to the next scheduler event. That
  is why idle RTOS firmware runs faster than real time (the Withings image: about 140 M "insn"/s of virtual
  cycles).
- **Instruction counts.**
  - Fast mode (default): Unicorn runs `count=n` with no Python hooks. For early stops (WFI, SVC,
    EXC_RETURN, MMIO stop, fault) the number executed is read from Unicorn's private `emu_counter` through
    ctypes. Its offset is found once by calibration (`_calibrate_counter` in `core/unicorn_cm.py`). If
    calibration fails, early stops are charged half the slice.
  - `--precise`: installs a `UC_HOOK_BLOCK` hook that counts Thumb instructions per translated block
    (cached per block). This is exact, but runs at about 10 M insn/s instead of about 100 M.
- **Mid-slice "now".** `clock.cycles` only moves at slice end. MMIO callbacks need the time of the
  accessing instruction, so `chip.now_cycles()` / `chip.now_ns()` and `sched.now_ns()` add
  `core.insns_in_slice()`, read from the same counter. Pin events, model `emit()` calls and relative
  scheduling (`in_ns`/`in_cycles`) all use this precise "now".

```
 cycles ──────────────────────────────────────────────────────────────────────────────►
        │◄──── slice (≤2000 insns) ────►│◄─ slice ─►│      WFI: skip      │◄── slice ──►│
        ^ sched.run_due()               ^ event due  ^ wfi    ............ ^ next event (timer IRQ pends)
        ^ arch.service()                                                   ^ arch.service() → handler
                   ^ MMIO write at insn k → event/pin timestamp = slice start + k
```

## Core interface

`core/base.py` defines `Core`, which covers instruction execution only:

- `reset(pc, sp)` and `run(max_insns) -> (StopReason, executed)`.
- `read_reg`/`write_reg`/`read_regs`/`write_regs`. Names are `r0..r15, sp, lr, pc, xpsr, msp, psp,
  primask, basepri, faultmask, control, fpscr, s0..s31`.
- `read_mem`/`write_mem`, `map_memory`, `map_mmio(base, size, read, write)`, `set_unmapped_handler`.
- `request_stop(from_mmio)` and `insns_in_slice()`.
- `StopReason` values: `COUNT, WFI, EXC_RETURN, SVC, FAULT, BREAKPOINT, USER`.

`UnicornCortexM` maps those reasons from Unicorn's interrupt numbers:

| Unicorn event | meaning | StopReason |
|---|---|---|
| intno 8 | branch to EXC_RETURN (PC = value & ~1) | `EXC_RETURN` (`stop_info` = value) |
| intno 3 after 8 | Unicorn spinning on the unhandled exit | ignored |
| intno 2 | `svc` (PC already past it) | `SVC` |
| intno 7 | `bkpt` | `BREAKPOINT` |
| other intno, `UcError`, unmapped/protection hooks | fault | `FAULT` (`last_fault = {type, pc, addr, ...}`) |
| `emu_start` returns with `wfi`/`wfe` at PC-2 | sleep | `WFI` |

Multi-register access goes through `_RegBatch` (ctypes arrays for `uc_reg_read_batch`/`uc_reg_write_batch`),
because each Python `reg_read` costs about 1 µs.

## Exception model (`arch/armv7m`)

`ArmV7M` keeps its own state: `pending` (set of exception numbers), `enabled` (external IRQs), `active`
(stack of exception numbers, innermost last), `prio[exc]`, `prigroup`, `asserted` (external IRQ lines
currently high), `vtor`, `lockup`. Unicorn's own NVIC is never used. The SCS at 0xE000E000 is not mapped
by Unicorn, and our `SCSBlock` sits there.

**Priorities.** Configurable priorities keep the top `prio_bits` bits (4 for ARMv7-M, 2 for ARMv6-M).
Reset, NMI and HardFault have fixed priorities -3, -2 and -1. The group priority used for preemption is the
value masked per `AIRCR.PRIGROUP`. The execution priority is the minimum of:

- the group priority of every active exception;
- `BASEPRI` (if non-zero);
- -1 if `FAULTMASK`;
- 0 if `PRIMASK` (only when it lowers the priority).

`best_pending()` picks the lowest priority value among pending, enabled exceptions, with the lower
exception number winning ties. It is taken only if its group priority is strictly lower than the execution
priority.

**Entry** (`_enter`, called from `service()` between slices, or synchronously for SVC and faults):

```
   read r0-r3, r12, lr, pc, xpsr, control, sp           (one batch read)
   stack = PSP if (thread mode and CONTROL.SPSEL) else MSP
   FP frame if fpu and CONTROL.FPCA and FPCCR.ASPEN      → 26 words: r0-r3,r12,lr,pc,xpsr,s0-s15,FPSCR,reserved
   else basic frame                                       → 8 words
   frame = (sp - size) aligned down to 8 (CCR.STKALIGN); xPSR bit 9 records the padding
   write frame; vec = [VTOR + 4*exc]
   EXC_RETURN = 0xFFFFFFE1 | (0x10 if no FP frame) | (0x08 if from thread) | (0x04 if PSP)
   write  sp → xpsr(IPSR=exc) → control(clear SPSEL, FPCA) → lr=EXC_RETURN → pc=vec   (order matters)
   pending -= exc; active.push(exc)
```

FP stacking is eager: the extended frame is written at entry whenever CONTROL.FPCA is set. Lazy stacking
(FPCCR.LSPEN, FPCAR) is not modelled. Software sees a correct frame either way, and FreeRTOS-style PendSV
handlers that test EXC_RETURN bit 4 and save s16–s31 work.

**Return.** The core stops with `EXC_RETURN`, and `_exc_return(value)`:

1. Validates the value (thread/handler, MSP/PSP, FP frame), checking it against the active stack.
2. Reads the frame from the selected stack and pops `active`.
3. Writes `control` (SPSEL from bit 2; FPCA **and** SFPA restored when an FP frame is popped, because
   Unicorn runs M-profile in Secure state), then `msp`, `psp`, then `xpsr` (the IPSR of the next active
   exception, or 0), then r0–r3, r12, lr, pc. The FP registers are restored first.
4. If the returning exception's IRQ line is still asserted, re-pends it.

Tail-chaining is not a special case: the loop calls `service()` again before the next slice.

**Sources of exceptions.**

| exception | how it becomes pending |
|---|---|
| SVCall (11) | core stops with `SVC` → `_sync_exception(11)`; escalates to HardFault if it cannot be taken |
| PendSV (14) | `ICSR.PENDSVSET` write |
| SysTick (15) | `SCSBlock` schedules one event per counter wrap at `now + CVR` (then every RVR+1 counts); sets COUNTFLAG, pends 15 if TICKINT. A count is 1 core cycle with CLKSOURCE=1 and `systick_ext_div` cycles (chip YAML, default 8 = STM32 HCLK/8) with CLKSOURCE=0 |
| IRQ n (16+n) | `arch.set_irq(n, True/False)` from a model (level-sensitive line), `arch.pulse_irq(n)` (one-shot), `NVIC_ISPR`, `STIR` |
| HardFault (3) | core fault → `fault()` sets CFSR/HFSR/BFAR bits and enters HardFault; a fault while in HardFault/NMI or with FAULTMASK → lockup |
| NMI (2) | `ICSR.NMIPENDSET` |

External interrupt lines are level-sensitive, like the Kinetis NVIC inputs. While a model holds a line
high, the IRQ is re-pended on exception return and when its pending bit is cleared. Deasserting the line
keeps a pending bit that is already latched (it clears only when the exception is taken or software writes
ICPR), as on silicon. MemManage, BusFault and UsageFault are never entered
separately: every fault escalates to HardFault, with the CFSR bits set.

`AIRCR` writes with VECTKEY 0x05FA set PRIGROUP. SYSRESETREQ (or VECTRESET) sets `reset_requested`, and the
slice loop then performs a full `chip.reset("SYSRESETREQ")`. `ArmV7M.reset()` loads SP and PC from
`[VTOR]` and `[VTOR+4]`, using `reset_vtor` as preset by the Session/CLI.

## The C28x (TI C2000) path

The TMS320F28232 is the first non-ARM chip. It keeps the three-layer split:

| layer | where | role |
|---|---|---|
| core | `core/c28x/` | `C28xCore` implements `Core` with a pure-Python interpreter (decode cache keyed by PC, one closure per decoded instruction); the instruction table `arch/c28x/isa.yaml` drives the decoder, the assembler (`tools/c28x_asm.py`) and the disassembler. Exact instruction counts, 1 cycle per instruction, no pipeline |
| arch | `arch/c28x/` | `C28xArch`, the counterpart of `ArmV7M`: PIE block (`pie.py`, PIECTRL/PIEACK/PIEIER/PIEIFR 1-12 and the 128-entry vector RAM at 0x0D00), CPU vector tables when ENPIE=0, ITRAP → vector 19, reset (PC = image entry or 0x33FFF6) |
| family | `periph/ti_c2000/` | SYSCTRL (PLL, clock gates, watchdog), GPIO, XINT, CPU timers, SCI, ADC, flash/CSM/SPI, storage blocks |

**Address units.** `addr_unit_bits: 16`: every address, offset and size is a 16-bit word. `Bus(addr_unit_bits=16)`
reads and writes in units; `MMIOBlock` keeps `unit_bytes` (2) bytes per unit and `Reg.boff/bw` cache the byte
geometry so the hot path costs the same as on ARM. A width-2 register is 32 bits with its low word at the lower
address. `regtable` reads `address_unit_bits` from the register-table header, so `ro_mask`/`reg_specs()` span
`width x 16` bits. Firmware segments are little-endian word streams (`FirmwareImage.unit_bytes = 2`).

**Interrupts.** A peripheral line (`set_irq(name | "x.y" | vector, level)`) latches PIEIFRx.y on its rising
edge. A group with an enabled flag and a clear PIEACKx sets PIEACKx and raises CPU interrupt INTx (IFR bit x-1).
Between slices `service()` takes the highest-priority IFR & IER interrupt when INTM is clear: it picks the group's
highest-priority pending channel, clears its flag, fetches vector n = 32 + 8(x-1) + (y-1) from the PIE RAM and
calls `core.take_interrupt(target, x)`, which pushes
the hardware context (T:ST0, AH:AL, PH:PL, AR1:AR0, DP:ST1, DBGSTAT:IER, PC) and sets INTM/DBGM. The ISR writes
PIEACK to re-enable the group. `IDLE` ends the slice with `StopReason.WFI`; `wakeup_pending()` (IFR & IER, ignoring
INTM) ends the sleep. `irqs:` in the chip YAML are keyed by PIE vector number, and the run summary labels them
`PIE x.y`.

**Clocks.** OSCCLK is wall-clock domain (watchdog, PLL lock); SYSCLKOUT is the core clock (`clock.set_hz` on
PLLCR/DIVSEL changes, re-based at the writing instruction). The CPU timers count SYSCLKOUT cycles; SCI timing
follows LSPCLK. The SCI receives like the ARM UARTs (no input queue, see the Session section): in SW RESET, with
RXENA clear or GPIO28 not muxed to SCIRXDA, arriving bytes are dropped.

**Analysis and front ends.** `firmware.analyze` tracks `MOVW/MOVZ DP` and decodes direct-addressed stores with
the ISA table to find pin roles (`GPxMUX`, `GPxDIR`). `C28xCore.pc` is written back only at slice end, so the
pin report reads the live PC from the run loop's frame. `/api/state` for a C28x chip carries `pc`, `insns`,
`acc`, `xar0..7`, `st0/st1`, `ier/ifr` and the PIE registers (no `xpsr`).

## MMIO framework (`periph/base.py`, `bus.py`, `periph/registry.py`)

```
 CPU load/store ──► Unicorn mmio_map callback ──► Bus routing ──► MMIOBlock.read/write(offset, size[, value])
                                                    │                     │
                       page-aligned block: direct ──┘                     ├─ containing Reg found?
                       sub-page blocks: _PageDispatch (one per 1 KB page)  │     merge sub-word lane, apply
                       unmapped in catch-all window: auto StubBlock        │     ro_mask / w1c_mask, store,
                       elsewhere: bus fault                                │     call on_write(blk, reg, old, new)
                                                                          │     (return value replaces stored)
                                                                          └─ no Reg: RAM-like bytes,
                                                                                first touch logged as `mmio`
```

- `Reg(name, offset, width, reset, ro_mask, w1c_mask, on_read, on_write)`. The width is in bytes. `on_read`
  returns the value the CPU sees (storage unchanged). `on_write` runs after storing and may return a
  replacement value.
- A block keeps its whole register file in a `bytearray`. Byte and halfword accesses into a word register
  are merged into the full register before the hooks run, so hooks always see whole-register values.
  `get`/`set` are programmatic access without hooks; `_store` is the same as `set`, given a `Reg`.
- Unknown offsets behave as RAM (`ram_like=True`) and are logged once per word as an `mmio` event.
  `--log-mmio` (`log_all`) logs every access.
- Kinetis models build their `Reg` lists from the SVD table (`_util.build_regs`) and add only behaviour.
- The bus maps blocks whose base and size are 1 KB-aligned directly into Unicorn. Smaller or unaligned
  blocks (e.g. GPIOA–E at 0x400FF000 + 0x40·n) share a `_PageDispatch`, which routes by address and falls
  back to a stub for unclaimed bytes. Bit-band alias regions (0x22000000, 0x42000000) are MMIO callbacks
  that read-modify-write one byte of the target.
- The registry maps model names to classes. `create()` imports `chipem.periph.<family>[.<sub>]` (or
  `chipem.arch.<family>` for `armv*`) on demand, and an unknown name falls back to `stub` with a `warn`.

**Stopping from MMIO.** A model sometimes needs the current slice to end right after the accessing
instruction: it scheduled an earlier event, pended a preempting exception, or requested a reset. Unicorn's
`emu_stop()` inside a write callback leaves PC at the store instruction, which has already taken effect.
The core checks with capstone that the instruction is a single-register `str/strb/strh` without writeback
and not inside an IT block, stops, and advances PC past it. For any other instruction the request is
declined and the effect is seen at the end of the slice.

## Pins: PinBoard, PORT and GPIO

`pins.py` is generic. A `Pin` has `owner` ("GPIO", a function name such as "UART2_TX", "analog",
"disabled"), `direction` (in/out/analog/none), `drive` (H/L when output), `pull` (up/down/None),
`external` (user stimulus H/L/X or None) and `fixed` (power pins). Level resolution:

```
 fixed? → fixed      out? → drive      external set? → external      pull up/down → H/L      else → X
```

The two sides talk to the board differently:

```
  user (CLI --set/--at, shell `set`, web click)            family models (Kinetis)
        │ PinBoard.set_external(pin, level)                      │ PinBoard.mcu_update(pin, owner=, direction=, drive=, pull=)
        ▼                                                        ▼
  ┌────────────────────────────── PinBoard ───────────────────────────────┐
  │ resolve level; on change: PinEvent(t_ns, pin, field, old, new)        │
  │   → subscribers (Session bridge → chip.log "pin" events → CLI/VCD/web)│
  │   → input_listeners[pin] callbacks (PORT edge/level detect, GPIO PDIR │
  │     cache invalidation)                                               │
  └───────────────────────────────────────────────────────────────────────┘
```

Kinetis split of responsibilities:

- **PORTx** (`kinetis.port`) owns the mux. A PCR write with MUX=1 sets owner "GPIO" and asks the matching
  GPIO model for direction and drive (`gpio.pin_state(i)`). MUX=0 gives the pin's ALT0 function (analog,
  direction "analog") or "disabled". MUX=2..7 gives the function name from the chip YAML, with direction and
  idle drive from the Kinetis signal table (below). PE/PS give the pull (digital modes only).
- PORTx registers an input listener per pin for interrupts. IRQC 9/10/11 detect edges, and 8/12 are level
  "logic 0" / "logic 1" (re-armed after ISF is cleared if the level still matches; X counts as 0). IRQC
  1–3 (DMA request) only set the flag. The PORTx IRQ line is the OR of ISF flags in interrupt modes.
- **GPIOx** (`kinetis.gpio`) keeps PDOR/PDDR. Output changes call `mcu_update(drive=...)` only for pins that
  are outputs and muxed to GPIO. PDIR is computed from resolved pin levels and cached until an input
  listener or mux change invalidates it.
- Peripheral-owned pins show the signal's idle state, not its traffic: the UART model does not drive its TX
  pin bit by bit. Each family has a signal table, `periph/<family>/signals.py` (a `periph.signals.SignalTable`
  of regex → role). The mux model looks the owner up whenever the owner changes and at reset, and pushes the
  role's direction and idle drive: UART TX → output H, RX → input, SPI SCK/MOSI → output L, CS → output H,
  I2C → open-drain (input, H only when pulled), PWM/timer channels and clock outputs → output L, ADC/CMP/DAC →
  analog, JTAG TCK/TMS/TDI → input, TDO/SWO → output H, unknown → "none" (X). The full table is in
  `docs/CONTRACTS.md`. A TX pin on a net therefore drives it H and the connected RX reads H.
- Timer/PWM models (`stm32.tim`, `kinetis.ftm`, `ti_c2000.epwm`) drive their output signals edge by edge with
  `PinBoard.drive_signal(name, level)` (e.g. `TIM3_CH1`, `FTM0_CH0`, `EPWM1A`) and `release_signal(name, idle)`.
  The PinBoard keeps the live level per signal (`signal_levels`): every output pin whose owner carries the signal
  follows it, also when the pin is muxed to it later (`mcu_update` then substitutes the live level for the mux
  model's idle drive). The models know signal names only, never pins.

STM32 (`stm32.gpio`) and C2000 (`ti_c2000.gpio`) follow the same pattern in one block each: MODER/AFRx/PUPDR,
or GPxMUX/GPxDIR/GPxPUD, decide owner, direction and pull (peripheral signals through their own `signals.py`); ODR/BSRR or GPxDAT/SET/CLEAR/TOGGLE drive the level.
All three report configuration and data writes, with the PC of the accessing instruction, to the pin report
(`chipem/pinreport.py`).

`Session` subscribes to the board and copies every `PinEvent` into `chip.log` as a `pin` event. The VCD
writer subscribes to the board directly; besides one wire per pin it writes a string variable per pin (scope
`owner`) so a waveform viewer shows function changes. The pin report (`pinreport.PinReport`) also subscribes to
the board: its per-pin edge ring feeds the `chg` column, the frequency/duty note of periodic pins and the web pin
table's `wave` column (`pinreport.pin_waves`).

## Event log

`EventLog.event(kind, t_ns, **fields)` appends a `LogEvent(kind, t_ns, fields, seq)` (bounded deque, 1 M
entries) and calls subscribers synchronously. Subscribers see every event; the stored history bounds fast periodic
pins: after a burst of 1000 dense edges (20 edges within 10 ms) a pin's edges are counted into one `pin` event with
`field="wave"` (`edges`, `since_ns`, `freq_hz`, `duty`) per 100 ms, so PWM cannot evict other events. Kinds: `pin mmio irq uart reset fault info warn` (plus `part` in a
board's merged log, from board parts). Consumers:

- `trace.PinEventPrinter` (live `chipem run` output and shell `trace`; coalesces UART bytes and pin config
  changes, summarises periodic pin and part signals per 10 ms window by edge density; `--all-edges` turns that off);
- `cmd_run.print_summary`;
- the shell's `events`/`trace`;
- the web worker (polls `session.events(since=seq)`; its own buffer caps `pin` events at 12,000 of 20,000 so
  reset/info/uart/warn events survive);
- the RCM model (watches `reset` events to report the reset cause).

## Board parts (`parts/`)

Parts (switches, buttons, clocks, LEDs, probes, counters, gates, flip-flops) are generic glue like `board.py` and
`pins.py`: they know nets and levels, never vendors, registers or Unicorn, so they fit any chip family. They live on a
`Board` (a single-chip Session gets a one-chip Board the first time a part is added, `Session.ensure_board()`).

- `parts/registry.py`: `@part("type")`, `create(type, id, params)`, `catalog()` for UIs. `parts/base.py`: `Part`
  (ports with `PortDesc(dir)`, `inputs`, `drive(port)`, `on_input`, `state`, `set`, `reset`). `parts/std.py`: the
  standard types.
- Nets hold part endpoints in `Net.ports`. Resolution treats a part output that drives H/L/X as a strong driver; the
  resolved level is delivered to member pins (scheduled on chips that are behind the source, as before) and to part
  input ports at once.
- Propagation is zero delay: `Board._propagate` resolves a net, the parts it feeds change outputs
  (`part_drive_changed`) and their nets join the next wave of the same event. A net resolved more than 16 times in
  one event is a combinational loop: one `warn` per loop, propagation stops.
- Time: part events take the time of the chip whose pin change is being propagated (instruction-precise), else the
  board clock. Timed parts (pulse, pattern) schedule on the first chip's scheduler with `at_ns` (wall-clock, so a
  PLL switch does not move them); `Board.reset_parts()` (from `Session.reset` / `Board.reset`) cancels and restarts
  them. A firmware-triggered chip reset does not touch parts.
- Observability: `part` events in `board.log` (`{part, port?, level|on|count|..., action?}`), the run summary's
  `parts` block, the VCD `parts` scope (`VcdWriter.add_parts`, declared before the header is sealed), shell
  `parts`/`part`, and `Board.to_dict()["parts"]` / `to_yaml()` for the web UI.

## Session

`Session` (`session.py`) is what all three front ends use:

- **Construction:** `load_desc`, `load_image` (with load-address auto-detection for `.bin`), `Chip(desc,
  clock_hz, precise, vtor)`, `load_firmware`, `reset`. VTOR defaults to the image start for raw binaries.
  The constructor also installs the pin→log bridge and an `arch.trace` hook that counts exception entries,
  and creates the optional `VcdWriter`.
- **`from_args(args)`:** applies `--set`, `--at` (scheduled with `sched.at_ns`; `TIME:UARTn=TEXT` schedules
  `uart_input`), `--uart-in` (`feed` on the UART model at t=0) and `--adc` (`set_channel`).
- **UART input has no queue.** `uart_input` / `feed(data)` make the host transmit from now, one character per
  character time of the receiving UART. Each byte "arrives" at its stop bit (`chipem/periph/uartwire.py`,
  `UartRx._rx_arrive`); board links call `receive()` at the stop-bit time computed from the sender's baud. The
  family model then decides like silicon: dropped if not listening (`_rx_block_reason`: Kinetis C2.RE / SIM
  gate; STM32 CR1.UE+RE / RCC enable / RCC reset; C2000 RXENA / SW RESET / SCIRST / RXFIFORESET / PCLKCR0) or if
  no pin offering the instance's RX signal is owned by it on the PinBoard; overrun if the holding register/FIFO
  is full (Kinetis S1.OR, new byte lost; STM32 SR.ORE, new byte lost; C2000 OE+RXERROR, previous byte overwritten,
  or RXFFOVF in FIFO mode). Drops are `uart` events with `dir=drop`, `reason`. The check runs per arriving byte,
  never on register polling.
- **`--clock`:** sets `chip.clock_override`, freezes the MCG model (`mcg.frozen`) and re-applies the
  frequency after every reset and run.
- **Running:** `run_for`, `run_cycles` and `step` take `_run_lock` and set `running`. `stop()` is the only
  thread-safe call: it sets the chip's stop flag and calls `core.request_stop()`, which calls Unicorn's
  `emu_stop`.

## Web server threading (`web/server.py`)

```
  browser ──HTTP──► ThreadingHTTPServer (one thread per request)
                      │  GET /api/state|events|uart|chip  → served from the last published snapshot (never blocks)
                      │  GET /api/stream                  → SSE; waits on Worker.cond for new snapshots/events
                      │  POST /api/run|pause|step|reset|pin|uart|adc
                      ▼        → Worker.call(fn): put (closure, Future) on queue, wait (bounded) for result
                   Worker thread (owns the Session; the only thread that touches the Chip)
                      loop: drain command queue → if running: session.run_for(chunk = 5 ms virtual)
                            → publish snapshot (state + pin table) + new log events → cond.notify_all()
```

- While free-running, commands are served between chunks, within one chunk's wall time. `pause` also calls
  `session.stop()` to cut a long chunk short.
- `--realtime` makes the worker sleep whenever virtual time gets ahead of wall-clock time. If it falls more
  than 0.5 s behind, it re-anchors instead of bursting.
- `POST /api/run {"ms": X}` runs toward a target time and then reports `last_stop: "DONE"`.
- The page (`static/app.js`, `package.js`, `board.js`, `parts.js`) is plain ES modules with no build step. It
  fetches `/api/chip` once to draw the package, then follows `/api/stream`.
- Parts: `/api/parts/types` (`chipem.parts.registry.catalog()`), `POST /api/part|part/set|part/remove|part/port`
  run on the worker like the net/link commands (`_Handler._wiring`). `POST /api/part` on a single chip calls
  `Session.ensure_board()`; the worker notices the new board, re-derives `chip_info` (served by `/api/chip`), and
  because the session's log is now the board's merged log, it numbers published events itself (`seq` stays
  monotonic across that switch). `parts` (with port nets and directions) is part of `board_info`, so it is
  refetched when `wiring` changes; live states come in `state.board.parts` and `part` events. `board.js` places
  part symbols (`parts.js`: `PartView`, `partGeom`) in extra grid columns (sources | chips | logic |
  indicators), packed into row bands so streets stay free; a port anchor is a pin-like member of the net router
  (side L/R, gutter of its column), so part-only nets route like any other.
