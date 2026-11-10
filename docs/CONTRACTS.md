# CHIP_EM interface contracts (all modules must conform; agents working in parallel rely on this)

Python 3.14, package `chipem/`. Run with `.venv/bin/python`. Dependencies: unicorn, capstone, keystone-engine,
pyelftools, pyyaml, pytest (install pytest into .venv if missing). No other third-party deps.
Addresses/offsets are ints in the chip's **address units** (`addr_unit_bits`, 8 for ARM). Everything is little-endian
unless the chip says otherwise.

## chipem/time.py — virtual time
```python
class Clock:
    cycles: int                   # elapsed core cycles since reset
    hz: float                     # current core clock (changeable at runtime via set_hz; ns-time stays monotonic)
    def now_ns(self) -> float     # virtual time in ns
    def set_hz(self, hz: float)   # rescale: future cycle→ns conversion uses new hz, past time preserved
    def advance(self, cycles: int)
class Scheduler:
    def at_cycles(self, cycles_abs: int, cb, *args) -> Event      # schedule callback at absolute cycle
    def in_ns(self, ns: float, cb, *args) -> Event                # relative to now, converted using current hz
    def cancel(self, ev: Event)
    def next_due(self) -> int | None                              # absolute cycles of earliest event
    def run_due(self)                                             # fire all events with cycles <= clock.cycles
```

## chipem/pins.py — generic pin model (no family knowledge)
```python
Level = Literal["H", "L", "X"]            # X = undetermined/floating
class Pin:
    num: int; name: str; functions: dict[str, str]; kind: str   # kind: gpio|analog|power|special
    port: str | None; bit: int | None
    owner: str          # "GPIO" | function name e.g. "UART1_TX" | "analog" | "disabled"
    direction: str      # "in" | "out" | "analog" | "none"
    drive: Level | None # level the MCU drives when direction == "out"
    pull: str | None    # "up" | "down" | None
    external: Level | None   # user stimulus; None = nothing connected
    def level(self) -> Level # resolution: out→drive; external if set; pull→H/L; else "X"
class PinBoard:
    def __init__(self, pins: list[Pin], clock: Clock)
    def get(self, name_or_num) -> Pin
    def set_external(self, name, level: Level | None)                 # user stimulus; emits event if level changed
    def mcu_update(self, name, *, owner=None, direction=None, drive=None, pull=None)   # from family mux/gpio models
    def subscribe(self, cb)        # cb(event: PinEvent)  PinEvent(t_ns, pin, field, old, new) ; field in {"level","owner","direction","pull"}
    def input_listeners: pin name -> list[cb(pin)]  # family models register to learn of external level changes (for PORT IRQs)
    # peripheral output signals (timer/PWM channels; names only, no pins or vendor knowledge in the caller)
    signal_levels: dict[str, Level]                    # live level of every signal a peripheral currently drives
    def drive_signal(self, name, level)                # e.g. "TIM3_CH1", "FTM0_CH0", "EPWM1A": every output pin whose owner
                                                       #   carries `name` follows (instruction-precise "level" events)
    def release_signal(self, name, idle=None)          # no longer driven: pins go to `idle`, else to the drive the mux
                                                       #   model last passed to mcu_update (the table's idle level)
    def signal_pins(self, name) -> list[Pin]           # pins offering `name` in any function (static, cached)
    @staticmethod
    def signal_matches(owner, name) -> bool            # owner carries name: equal, a '/'-part equal, or a '/'-part
                                                       #   "<name>_<LETTERS>[_<LETTERS>]" ("TIM2_CH1_ETR" carries "TIM2_CH1";
                                                       #   "TIM1_CH10", "TIM1_CH1N" and "TIM1_CH1" for name "TIM1" do not)
```
**`mcu_update` drive override.** While a signal is driven (`name in signal_levels`), `mcu_update(pin, drive=d)` on an
output pin whose owner carries it stores `d` as the mux model's requested drive but sets `pin.drive` to the live
signal level, so a pin muxed to a running PWM channel shows its current level at once. If the owner carries several
driven signals the exact match (owner or one of its '/'-parts equals the name) wins over a bundled one, then the
longest name: deterministic whatever the order of `drive_signal` calls. `reset()` clears `signal_levels`.
**Peripheral-owned pins.** When a family mux model gives a pin to a peripheral signal (Kinetis PORT MUX=2..7, STM32
MODER=AF, C2000 GPxMUX≠GPIO) it sets `owner` to the signal name and takes `direction`/`drive` from its family table
`chipem/periph/<family>/signals.py` (`TABLE = chipem.periph.signals.SignalTable([(regex, role), ...])`, first match on
any `/`-part of the name; `TABLE.pin_state(fn) -> (direction, drive)`). It does so on every owner change and at reset
(default functions such as JTAG/SWD). `pull` stays the mux model's (PCR PE/PS, PUPDR, GPxPUD). Roles:

| signals | direction | drive / level |
|---|---|---|
| UART/USART/LPUART/SCI TX (`*_TX`, `SCITXD*`), RTS | out | H |
| UART RX (`*_RX`, `SCIRXD*`), CTS | in | pull, else X |
| SPI `SCK`/`MOSI`/`SOUT`/`SIMO`/`SPICLK` | out | L (CPOL not seen by the mux model) |
| SPI `MISO`/`SIN`/`SOMI` | in | pull, else X |
| SPI `PCS*`/`NSS`/`SPISTE*` | out | H |
| I2C `SDA`/`SCL` (open-drain) | in | H when pulled up, else X |
| CAN TX / RX | out / in | H / pull |
| PWM/timer channels (`FTMx_CHn`, `TIMx_CHn[N]`, `EPWMxA/B`) | out | L idle; the live level while the timer drives the signal (`drive_signal`) |
| clock outputs (`CLKOUT`, `RTC_CLKOUT`, `XCLKOUT`, `MCOx`), trace clock/data | out | L |
| ADC/CMP/DAC | analog | — |
| JTAG/SWD `TCK`/`TMS`/`TDI`/`TRST` / `TDO`/`SWO` | in / out | pull / H |
| anything else | none | X |

The level does not follow serial traffic (UART bytes, SPI frames): it is the signal's idle state. Timer/PWM
models are the exception: they call `PinBoard.drive_signal` at every output edge, and a pin owned by that signal
shows the live level (see the drive override above) until `release_signal`. Nets treat a peripheral output like any
driven output.

## chipem/periph/base.py — MMIO blocks
```python
class Reg:  # descriptor; width in bytes (1/2/4); masks are over the register width
    def __init__(self, name, offset, width=4, reset=0, ro_mask=0, w1c_mask=0, on_read=None, on_write=None)
    # on_read(block, reg) -> int|None  (return value overrides stored); on_write(block, reg, old, new) -> int|None (return value stored instead of new)
class MMIOBlock:
    name: str; base: int; size: int
    def __init__(self, name, base, size, regs: list[Reg] = (), *, ram_like=True, log=None)
    def read(self, offset, size) -> int      # sub-word access OK; unknown offsets: ram_like → stored bytes (logged once)
    def write(self, offset, size, value)     # applies ro/w1c masks then on_write of the containing Reg
    def reset(self)
    def get(self, regname) -> int; def set(self, regname, value)   # programmatic, no hooks
    def attach(self, chip)                   # called once; gives access to chip.clock/sched/pins/irq
    def irq(self, n: int, pending: bool=True)   # chip.arch.set_irq(n, pending): drive the LEVEL of IRQ line n
                                             # (True while the flag&enable condition holds, False once cleared;
                                             # call it on transitions, repeated True calls are harmless)
    def pulse_irq(self, n: int)              # pend IRQ n once without asserting the line (flag-less pulse sources)
```
Peripheral models subclass MMIOBlock and are constructed by the registry: `periph.registry.create(model_name, name, base, size, params, chip)`.
Model names: `"stub"`, `"ram"`, `"kinetis.sim"`, `"kinetis.port"`, `"kinetis.gpio"`, `"kinetis.wdog"`, `"kinetis.mcg"`, `"kinetis.osc"`, `"kinetis.smc"`, `"kinetis.pmc"`, `"kinetis.rcm"`, `"kinetis.llwu"`, `"kinetis.pit"`, `"kinetis.lptmr"`, `"kinetis.uart"`, `"kinetis.rtc"`, `"kinetis.ftfe"`, `"kinetis.adc"`, `"kinetis.vbat"`, `"kinetis.dma"`, `"kinetis.dmamux"`, `"kinetis.dspi"`, `"kinetis.pdb"`, `"armv7m.scs"`. DMA-capable models call `kinetis.dma.dma_request(chip, source)` and register `dmamux.register_source(source, active_fn)`.

## chipem/bus.py
```python
class Bus:
    addr_unit_bits: int
    def map_memory(self, name, base, size, kind, data: bytes|None=None, writable=True)   # flash/ram → backed by core memory
    def map_mmio(self, block: MMIOBlock)
    def read(self, addr, size) -> int ; def write(self, addr, size, value) ; def read_bytes(addr, n) -> bytes ; def write_bytes(addr, data)
    def lookup(self, addr) -> (MMIOBlock | MemoryRegion | None)
```
Unicorn-backed implementation lives in core/unicorn_cm.py (the Bus calls `core.map_memory`/`core.map_mmio`).

## chipem/core/base.py
```python
class StopReason(Enum): COUNT, WFI, EXC_RETURN, SVC, FAULT, BREAKPOINT, USER
class Core(ABC):
    def reset(self, pc: int, sp: int)
    def run(self, max_insns: int) -> (StopReason, insns_executed: int)   # insns_executed may be approximate for early stops
    def read_reg(self, name: str) -> int ; def write_reg(self, name, value)   # names: r0..r15, sp, lr, pc, xpsr, msp, psp, primask, basepri, faultmask, control, fpscr, s0..s31
    pc: property ; sp: property
    def read_mem(addr, n) -> bytes ; def write_mem(addr, data)
    last_fault: dict | None      # {"type": "...", "pc":..., "addr":..., "intno":...}
```

## chipem/arch/armv7m — exception model (owns SCS MMIO at 0xE000E000..0xE000EFFF)
```python
class ArmV7M:   # created by Chip; chip.arch
    def __init__(self, chip, *, fpu: bool, num_irqs: int)
    def set_irq(self, irq: int, pending: bool)          # external IRQ line n (0-based, exception 16+n), level-sensitive:
                                                        #   rising edge pends; while asserted the IRQ re-pends on return
                                                        #   from its handler, on ISER enable and after an ICPR write;
                                                        #   deasserting keeps an already-latched pending bit (cleared
                                                        #   only by taking the exception or by ICPR), as on silicon
    def pulse_irq(self, irq: int)                       # pend once without asserting (ISPR/STIR also only pend)
    asserted: set[int]                                  # exception numbers whose line is currently high
    def set_pending_exc(self, exc: int)                 # internal exceptions (SysTick 15, PendSV 14, SVC 11...)
    def service(self) -> bool                           # called between slices: take highest-priority pending exception if allowed; returns True if entered
    def on_stop(self, reason, info) -> ...              # handles EXC_RETURN pops, SVC entry, SYSRESETREQ
    def reset(self)                                     # VTOR-driven: sp=[VTOR], pc=[VTOR+4]
    vtor: int
```

## chipem/chip.py
```python
class ChipDescription:   # parsed YAML (chips/*.yaml); see schema in docs/PLAN.md "Chip description"
class Chip:              # runtime assembly: description + clock + sched + pins + bus + core + arch + peripherals + log
    def __init__(self, desc: ChipDescription, *, clock_hz=None, log=None)
    def load_firmware(self, image: FirmwareImage)   # image.segments: list[(addr, bytes)], image.entry (optional)
    def reset(self)
    def run_for(self, ns: float) / run_cycles(self, cycles: int) -> StopReason   # slice loop: events, service(), core.run()
    def step(self)                                   # one instruction
    clock, sched, pins, bus, core, arch, peripherals: dict[name, MMIOBlock], events (list), uart_out(name) ...
    def uart_blocks(self) -> dict[str, MMIOBlock]     # UART-like blocks: every model exposing `tx_listeners` (any family)
    def adc_blocks(self) -> dict[str, MMIOBlock]      # analog-input blocks: every model exposing set_channel(name, value)
    # analog-input model conventions (any family):
    #   set_channel(name, value) -> bool | None   value as below; False = not this block's channel
    #   set_input(name, value) -> bool            optional: resolve other names (pins "PA0", "ADC123_IN0", "CMP0_IN3");
    #                                             False (never raise) for a name it does not know
    #   adc_volts = True                          float values are volts (stm32.adc, kinetis.cmp); default raw codes only
    #   adc_volts_only = True                     no raw codes (kinetis.cmp): ints are rejected
    def find_uart(self, name, *, fallback=False) -> str | None   # "USART1"/"UART1"/"SCIA"/"SCI-A" -> block name;
                                                       # fallback: unknown -> lowest-numbered UART (default "UART0")
```
`Clock.slice_offset` (set by the Chip to `core.insns_in_slice`): `set_hz` re-bases at the current instruction.
`regtable.RegTable.unit_bits` (from `address_unit_bits`); `Register.full_mask/ro_mask` and
`PeripheralRegs.reg_specs(unit_bits=None)` span `width * unit_bits` bits; specs carry `unit_bits`.
Chip YAML keys: `name, core{arch, backend, cpu, fpu, clock_hz, addr_unit_bits}, family, memory[], peripherals[], irqs{n: name}, pins[]`.

## Events / logging
All runtime observations go through `chip.log` (a `chipem.log.EventLog`): `log.event(kind, t_ns, **fields)` with kinds
`pin`, `mmio`, `irq`, `uart`, `reset`, `fault`, `info`, `warn`. The CLI/VCD/web subscribe to it.
Subscribers receive every event. The stored history (`iter(log)`, `filter`, `Session.events`) bounds fast periodic
pins: `EventLog(maxlen=1_000_000, *, pin_burst=1000, pin_dense_edges=20, pin_dense_ns=10e6, pin_wave_ns=100e6)`; a
`pin` level event of a pin (keyed by `chip`, `pin`) with `pin_dense_edges` edges within `pin_dense_ns` is dense; after
`pin_burst` stored dense edges, the edges are not stored but counted, and one stored-only (not notified) event
`pin` with `field="wave", new=<level>, edges=N, since_ns, freq_hz, duty` is appended per `pin_wave_ns` window, when the
pin slows down, or on `flush_waves(now_ns=None)` (all open windows; with `now_ns`, those of pins quiet for
`pin_dense_ns`). `pin_burst=None` stores every edge. Consumers of stored `pin` events must ignore `field == "wave"`
or use it as a summary.

## chipem/session.py — one emulation (a chip, or a board of chips) shared by run/shell/serve
```python
class Session:
    def __init__(self, chip: str | Path, firmware: str | Path | None = None, *, load_addr=None, fmt=None,
                 vtor=None, clock_hz=None, precise=False, log_mmio=False, vcd_path=None, quiet=False, chip_id="A")
    @classmethod
    def from_args(cls, args) -> "Session"      # argparse Namespace from chipem.cli (common + session_opts); --board FILE builds a
                                               # board session (firmware positional then forbidden); applies --spi-flash/--set/--at/--uart-in/--adc
    @classmethod
    def from_board(cls, board: str | Path | BoardDesc, *, precise=False, log_mmio=False, vcd_path=None,
                   quantum_ns=None, clock_hz=None, quiet=False) -> "Session"   # nets, links, devices, stimulus applied
    chips: dict[str, Chip]                     # chip id -> Chip, board order ({"A": chip} for a single chip)
    ctxs: dict[str, ChipContext]               # per chip: chip, desc, image, vtor, clock_override, irq_counts, reset_pins, adc_values, devices, vcd
    board: Board | None                        # None for a single-chip session (no board indirection on that path)
    view: str                                  # chip id that unqualified names and the attributes below refer to (first chip)
    pin_scope: str | None                      # if set, pin_table() without `chip` lists only this chip (web UI)
    chip; desc; image; vtor; clock_override; irq_counts; reset_pins; adc_values; devices; vcd   # of the view chip
    log                                        # board.log (merged) or chip.log: what front ends subscribe to
    def set_view(self, cid: str)
    def resolve(self, name: str) -> (ChipContext, str)   # "B.PTC6" -> (ctx B, "PTC6"); unqualified -> view chip
    def attach_spi_flash(self, spec: str, chip: str | None = None)   # "[CHIP.]SPI0:cs=PTC4,jedec=0x202015,size=2M,file=img.bin"
    def reset(self)                            # every chip
    def run_for(self, ns: float) -> StopReason # honours stop(); a board runs in lockstep (Board.run_for)
    def run_cycles(self, n: int) -> StopReason # board: n cycles of the view chip, converted to time
    def step(self) -> StopReason               # one instruction of the view chip (other board chips do not move)
    def stop(self)                             # thread-safe request to end the current run early
    # all names below may be qualified with a chip id: "B.PTC6", "B.UART0", "B.ADC0_SE14"
    def set_pin(self, name: str, level: "H"|"L"|"X"|None)              # external stimulus now (a net pin drives its net)
    def schedule_pin(self, t_ns: float, name: str, level)              # stimulus at absolute virtual time
    def apply_stimulus(self, spec: str)           # "PTC6=1" | "50ms:B.PTC6=0" | "1.5s:PTC6=X" ; levels 1/0/H/L/X/Z(None)
    def uart_input(self, data: bytes, uart: str = "UART0")
    def uart_output(self, uart: str = "UART0") -> bytes                 # all bytes transmitted so far
    def set_adc(self, channel: str, value: int | float)                 # "ADC0_SE14", "PA0", "CMP0_IN0"; int = raw code,
        # float = volts (only for `adc_volts` models; ValueError otherwise); an int for an `adc_volts_only` model is a
        # ValueError naming the V/mV form. A name that is not "<block>_<channel>" of a block with set_channel goes to
        # the first adc_blocks() model whose set_input(name, value) returns True.
    # module function: parse_adc_value(text) -> int | float   "2048"/"0x800" -> int (raw code);
    #   "1.65" / "1.65V" / "1650mV" -> float volts.  Used by --adc, shell `adc`, web POST /api/adc (string values).
    def pin_table(self, chip: str | None = None) -> list[dict]
        # [{num, name, short, kind, owner, direction, level, drive, pull, external, functions, chip}] (+ net on a board)
        # chip=None: every chip (or pin_scope); on a board, `short` of non-view chips is qualified ("B.PTC5")
    def state(self, chip: str | None = None) -> dict
        # chip.state() + t_ns + cycles + insns + hz + running + chip; board & chip=None: + chips {id: state} + board {name,
        # quantum_ns, t_ns, skew_ns, nets [{name, level, conflict, pins}], links [{link, a_to_b, b_to_a}]}
    def events(self, since: int = 0, kinds: set[str] | None = None, chip: str | None = None) -> list[Event]
        # index-based (ev.seq > since); board: from the merged log, every event has a `chip` field ("board" for
        # board-level warnings), sorted by time; `chip` filters on it
    def irq_summary(self, chip: str | None = None) -> list[(exc, name, count)]
    def close(self)                               # flush VCD(s), detach the board
# parse helpers exported: parse_time("50ms") -> ns ; parse_level("1") -> "H" ; parse_uart_in("B.UART0:hi") -> ("B.UART0", b"hi")
```
Time strings: `<float><unit>` with unit in ns|us|ms|s (default ms if omitted).

## chipem/board.py — several chips on one board
```python
def load_board(path) -> BoardDesc             # boards/*.yaml; paths relative to the board file
def parse_board(data: dict, *, base: Path | None = None, path=None) -> BoardDesc
class BoardDesc: name; path; chips: list[BoardChipSpec(id, chip, firmware, load_addr, clock_hz, vtor, format)];
                 nets: list[NetSpec(name, pins: [(cid, pin)], pull, drive)]; links: list[LinkSpec(type, a, b, bidir)];
                 devices: list[DeviceSpec(type, chip, options)]; stimulus: list[str]; quantum_ns: float | None
def split_qualified(name, ids, default=None) -> (cid | default, rest)
class Board:
    def __init__(self, chips: dict[str, Chip], *, quantum_ns=None, name="board", log=None)   # quantum default 10 us
    chips: dict[str, Chip]; nets: list[Net]; links: list[UartLink]; quantum_ns: float
    log: EventLog                              # merged: every chip event copied with field chip=<id>; board events chip="board"
    def run_for(self, ns) -> StopReason        # lockstep: run the chip furthest behind up to min(target, others' min now +
                                               # quantum); COUNT, USER (stop) or the first other stop reason (last_stop_chip)
    def stop(self)                             # thread-safe
    def now_ns(self) -> float                  # earliest chip time; skew_ns(): latest - earliest
    def add_net(self, name, pins: [(cid, pin)], *, pull=None, drive=None) -> Net
    def add_uart_link(self, a: (cid, periph), b: (cid, periph), *, bidir=True) -> UartLink
    def set_pin(self, cid, pin, level)         # stimulus: net pin -> net.stimulus (strong), else pins.set_external
    def net_of(self, cid, pin) -> Net | None
    def warn(self, msg, *, t_ns=None, **fields)
class Net:  level; conflict; conflicts; members [(cid, chip, pin)]; pull; drive; stimulus
    # strong (outputs' drive, fixed pins, net drive, stimulus) > net pull > member pin pulls > X; H vs L -> X + one warn
    # per episode; level written to every member's `external` (scheduled at the source time on chips behind it)
class UartLink: bytes_ab; bytes_ba; warned    # sender tx_listeners -> peer.receive() one sender character time later
                                              # (peer's scheduler); >3 % baud or frame mismatch -> one warn per link
```
UART models used by links provide `tx_listeners: list[cb(uart, byte)]`, `receive(data)` (no extra character time),
`baud()`, `char_bits()`, `char_ns()` (kinetis.uart does).

## 16-bit address units and the C28x core (added for TI C2000 support)

All address/offset/size numbers stay in the chip's address units (`desc.core.addr_unit_bits`, 16 for C28x).
- `MMIOBlock`: `offset`/`size` and `Reg.offset`/`Reg.width` are in units; the backing store holds `unit_bytes = addr_unit_bits // 8`
  bytes per unit; register values are little-endian in unit order (C28x: a 32-bit register's low word sits at the lower,
  even address). Kinetis/STM32 (8-bit units) are unaffected.
- `Bus(addr_unit_bits)`: `read(addr, size_units) -> int`, `write(addr, size_units, value)`; `read_bytes/write_bytes` keep working
  on the underlying little-endian byte image (2 bytes per word), which is also the format of `FirmwareImage.segments`
  for C28x (COFF/`.out` loader and the assembler emit little-endian word streams; segment addresses are word addresses).
- `Core` (chipem/core/base.py) gains `map_memory(name, base, size_units, data: bytes|None, writable)` and
  `map_mmio(base, size_units, read_cb, write_cb)` with `read_cb(offset, size_units) -> int` / `write_cb(offset, size_units, value)`;
  the Unicorn core keeps its byte semantics (units == bytes there). `read_mem(addr, n_units) -> bytes` (little-endian image) and
  `write_mem(addr, data: bytes)`.
- `core/c28x/` implements `Core` with a pure-Python interpreter: `cpu: c28x`, `fpu: true` means FPU32 (F2833x). Register names
  for `read_reg/write_reg`: acc, ah, al, p, ph, pl, xt, t, tl, xar0..xar7, ar0..ar7, sp, dp, pc, rpc, st0, st1, ier, ifr,
  dbgier, r0h..r7h (FPU32), stf, rb. Flags via st0/st1 (OVC, OVM, SXM, C, TC, Z, N, V, PM in ST0; ARP, XF, M0M1MAP, OBJMODE,
  AMODE, IDLESTAT, EALLOW, LOOP, SPA, VMAP, PAGE0, DBGM, INTM in ST1). Reset: OBJMODE=1 is assumed (C28x object mode),
  AMODE=0. `run(max_insns)` returns `(StopReason, insns)` with exact counts; `StopReason.WFI` for `IDLE`;
  `StopReason.FAULT` with `last_fault` for an illegal opcode (ITRAP0 → vector 19 is taken instead if the PIE/vector is set).
  Interrupts: `core.interrupt_pending()` is consulted between slices by `arch/c28x`; `core.take_interrupt(vector_addr, int_number)`
  performs the hardware context save (ST0, T, AL, AH, PL, PH, AR0, AR1, DP, ST1, DBGSTAT, return PC; clears INTM-related bits
  per the C28x TRM: DBGM=1, INTM=1, EALLOW=0, LOOP=0, IDLESTAT=0) and jumps; `IRET` restores and continues.
  `cycles_per_insn` defaults to 1 (the Clock advances by instruction count).
- `arch/c28x/`: `C28xArch` (same role as `ArmV7M`): PIE controller model (PIECTRL/PIEACK/PIEIERx/PIEIFRx at 0x0CE0, vector
  table at 0x0D00 (256 words), ENPIE), maps peripheral interrupt lines (`set_irq(line_name_or_number, level)`) to PIE group/channel
  → core INTx (IFR bit) → vector fetch from the PIE table (or the CPU vector table at 0x3FFFC0/0x000000 when ENPIE=0);
  `set_pending_exc` not used. Reset: PC = `desc.extra['boot']['entry']` (default 0x33FFF6, the "jump to flash" entry) unless the
  firmware image provides an entry (COFF). `vector_table()` for the `info` command.
- Chip YAML for C2000 (TMS320F28232: C28x without FPU, 100 MHz):
  `core: {arch: c28x, backend: c28x, cpu: c28x, fpu: false, clock_hz: 100000000, addr_unit_bits: 16, num_irqs: 0}`
  (`fpu: true` only for FPU32 parts such as the F28335 at 150 MHz), `family: ti_c2000`, memory in words,
  `peripherals` with model names `ti_c2000.sysctrl`, `ti_c2000.gpio`, `ti_c2000.pie` (arch-owned, like armv7m.scs),
  `ti_c2000.cputimer`, `ti_c2000.sci`, `ti_c2000.xint`, `ti_c2000.flash`, `ti_c2000.csm`, `ti_c2000.spi`,
  `ti_c2000.adc`, `ti_c2000.adcmirror`, `ti_c2000.alias`, `ti_c2000.epwm` (ePWM1-6), `ti_c2000.ecap` (eCAP1-4) and
  `ti_c2000.storage` (register storage for HRPWM, eQEP, eCAN, I2C, McBSP, XINTF, DMA). `irqs` are keyed by PIE **vector number**, not by "x.y":
  ```yaml
  irqs:
    13: INT13          # CPU timer 1
    35: XINT1          # INT1.4 = 32 + 8*(1-1) + (4-1)
    38: TINT0          # INT1.7
    96: SCIRXINTA      # INT9.1 = 32 + 8*(9-1) + 0
    97: SCITXINTA      # INT9.2
  ```
  INTx.y = 32 + 8(x-1) + (y-1); 13/14 =
  INT13/INT14, 18 NMI, 19 ILLEGAL; the chip loader keys IRQs by integer, so "x.y" keys are not used in YAML — `set_irq`
  still accepts "x.y". `pins` with functions keyed `GPIO`, `MUX1`, `MUX2`, `MUX3`
  (the GPxMUX field values 0..3) for GPIO pins and `kind` as today.
- UART links on boards (`chipem/board.py`): any peripheral model that exposes `tx_listeners` (list of callables taking a byte)
  and `receive(data: bytes)` plus `baud()` / `frame()` can be an endpoint. The C2000 SCI model must expose the same.
- Implemented interfaces (TMS320F28232 port, see docs/C2000_ROADMAP.md):
  - `Reg(..., unit_bits=8)`; `MMIOBlock(..., unit_bits=None)` takes `unit_bits` from the argument, `params["unit_bits"]`
    (injected by the Chip for non-8-bit chips) or the class attribute `unit_bits`; `block.unit_bytes`; `Reg.boff/bw` are the
    byte offset/width in the backing store.
  - `Bus(core, addr_unit_bits=, catch_all=[(lo, hi_exclusive), ...])`; the Chip passes `extra.auto_stub_ranges`
    (`[[lo, hi_inclusive], ...]` in the YAML) or the ARM defaults; `bus.unit_bytes`.
  - `FirmwareImage(segments, entry, source, fmt, unit_bytes=1, symbols={})`: `start/end/size` and `read(addr, n)` count units.
    `load_firmware(path, load_addr=, fmt=, unit_bytes=)`; formats `coff` (TI COFF0-2, sniffed for `.out`) and `c28img`:
    `{"format": "c28img", "unit_bits": 16, "entry": addr|null, "segments": [{"addr": word, "data_hex": "<LE bytes>"}
    | {"addr": word, "words": [...]}], "symbols": {name: word}}` (the assembler's `AsmResult.to_c28img()`).
  - `Chip`: `arch_kind` ("armv7m" | "c28x"), `arch_blocks` (arch-owned MMIO: `[scs]` or `[pie]`), `unit_bits`;
    `state()`/`regs()` defer to `arch.cpu_state()` / `arch.reg_names` when the arch has them.
  - `C28xArch(chip)`: `set_irq(line, level)` (edge-latched into PIEIFR), `pulse_irq(line)`, `pending`, `best_pending()`,
    `service()`, `wakeup_pending()` (IFR & IER, ignoring INTM), `on_stop()`, `reset(entry)`, `exc_name(vec)`,
    `exc_label(vec)` ("PIE 1.7"), `vector_table()`, `cpu_state()`, `reg_names`, `trace(kind, vector_number)`, `pie`
    (PIEBlock: `enpie`, `ack`, `ier[1..12]`, `ifr[1..12]`, `vector(n)`). It installs `core.itrap_handler(pc) -> bool`
    (illegal opcode → vector 19) and `core.vector_fetch(n) -> addr` (TRAP/INTR through the active table) and calls
    `core.take_interrupt(target, int_number)`, which saves the context and clears the IFR/IER bit.
  - Board UART endpoints may report `baud() == 0` while their receiver is not configured (C2000 SCI in SWRESET):
    the link then skips its baud/frame check.


## Board wiring additions (runtime nets/links, 2026-11-05)
- `Board.add_net(name|None, pins, pull=, drive=, extend=False)`, `connect(pins, name=None, pull=None)` (creates, extends or
  merges nets), `remove_net(name)`, `rename_net(old, new)`, `net(name)`, `add_link(...)` (= `add_uart_link`, rejects
  duplicates), `find_link(a, b)`, `remove_link(link|index|(a, b))` (refuses auto links), `wiring` (change counter),
  `desc` (BoardDesc), `to_dict(base=None)`, `to_yaml(base=None)` (auto links are emitted as comments only).
- **Auto UART links:** a net joining a pin owned by a UART TX signal (`UARTn_TX`, `USARTn_TX`, `LPUARTn_TX`, `SCITXDx`)
  to a pin owned by a UART RX signal on another chip creates a one-way byte link (`UartLink.auto=True`, `.net=<name>`),
  re-evaluated on owner changes and net/link edits; an explicit link covering the same direction suppresses it.
- Net resolution: an output pin whose drive is `None` does not drive the net (so peripheral-owned outputs without a
  modelled drive do not force X).
- `Session.state()["board"]` has `wiring`, per-net `pull`, per-link `auto`/`net`. Web API: `GET /api/board`,
  `GET /api/board/yaml[?download=1]`, `POST /api/net`, `POST /api/net/rename`, `POST /api/net/remove` / `DELETE /api/net/<name>`,
  `POST /api/link`, `POST /api/link/remove` / `DELETE /api/link?from=&to=` (400 on single-chip sessions).

## Board parts (inputs, indicators, logic; 2026-11-06)
Parts are board-level components with named **ports** attached to nets. They are generic (no vendor knowledge) and
live in `chipem/parts/` (`registry.py`: `@part("type")`, `create(type, id, params)`; `base.py`: `Part`).

- `Part` interface: `id`, `type`, `params: dict`, `ports: dict[name, PortDesc]` (`PortDesc(dir="in"|"out"|"inout")`),
  `attach(board)`, `reset()`, `on_input(port, level, t_ns)` (called by the net when an input port's net level changes,
  level in "H"|"L"|"X"), `drive(port) -> "H"|"L"|None` (what an output port drives; `None` = high-impedance), `state() ->
  dict` (JSON-serialisable, shown in the UI and the run summary, e.g. `{"on": true}`, `{"level": "H"}`, `{"edges": 12}`),
  `set(**values)` (user action: switch level, source value, generator period; returns the new `state()`), `to_dict()`.
  A part asks the board to re-evaluate a net after changing an output with `board.part_drive_changed(part, port)`.
  Timed parts use `board.sched_at_ns(t, cb)` / `board.sched_in_ns(dt, cb)` (board wall-clock domain) and must cancel on
  `reset()`.
- Net rules: a part output port that returns a level is a strong driver (same strength as a chip push-pull output);
  conflicts resolve to X as for pins. `Net.members` stays pins; part endpoints are in `Net.ports: list[(part, port)]`.
  Combinational parts propagate with zero delay; the board guards loops (max 16 iterations per event, then a `warn`).
- Standard types (params in parentheses, port names fixed):
  - inputs: `switch(level="L"|"H"|"X")` port `out`; `button(pressed="L", released="H", pressed=false)` port `out`;
    `const(level)` port `out` (also `vcc`/`gnd` aliases); `pulse(period, duty=0.5, start="L", phase=0)` port `out`
    (free-running clock, period as a time string); `pattern(steps=[[time, level], ...], repeat=false)` port `out`.
  - indicators: `led(active="H"|"L", color="red")` port `in` (`state: {"on": bool, "changes": n}`); `probe()` port `in`
    (`state: {"level", "rises", "falls", "last_change_ns", "freq_hz"|null}`); `counter(edge="rise")` port `in`.
  - logic: `not` (`in`,`out`), `buf`, `and`, `nand`, `or`, `nor`, `xor`, `xnor` (ports `in0..in{n-1}` with `inputs=2`
    default, `out`); `mux2` (`a`,`b`,`sel`,`out`); `dff(edge="rise")` (`d`,`clk`,`q`,`qn`; optional `rst`); `latch`
    (`d`,`en`,`q`). X on any input gives X out except where logic dominates (AND with an L in = L, OR with an H in = H).
- Board YAML:
  ```yaml
  parts:
    - {id: SW1, type: switch, level: H, ports: {out: BTN}}        # port -> net name (created if absent)
    - {id: LED1, type: led, active: L, ports: {in: A.PTC5}}        # port -> chip pin: an anonymous net is created
    - {id: G1, type: and, inputs: 2, ports: {in0: BTN, in1: A.PTB19, out: LED_NET}}
  ```
  Keys other than `id`, `type`, `ports` are params. Stimulus for parts: `--at 5ms:SW1=H`, `--set SW1=H`, board
  `stimulus:` entries (`Session.apply_stimulus` resolves part ids before pin names; `set(level=...)` for switches/consts,
  `set(pressed=...)` for buttons).
- Runtime API: `Board.parts: dict[id, Part]`, `add_part(id, type, params, ports: dict[port, net_or_pin]) -> Part`,
  `remove_part(id)`, `set_part(id, **values) -> dict`, `part_net(part_id, port) -> Net|None`, `connect_port(part_id,
  port, net_or_pin)`, `disconnect_port(part_id, port)`. `wiring` increments on any of these. `to_dict()["parts"]` =
  `[{id, type, <params...>, ports: {port: net name}, state: {...}}]`; `to_yaml()` emits the `parts:` list.
  Events: `board.log` gets `part` events `{part, port?, t_ns, **state_delta}` on every state change (LED on/off, counter
  increment, switch set) so the web SSE stream and the VCD (`parts` scope: one wire per output port and per LED) see them.
- Web API: `/api/board` includes `parts`; `POST /api/part` `{id?, type, params, ports}` (id auto-assigned when absent),
  `POST /api/part/set` `{id, values}`, `POST /api/part/remove` `{id}`, `POST /api/part/port` `{id, port, to}` (to =
  net name, `CHIP.PIN`, or null to disconnect). `GET /api/parts/types` lists `{type, params: {name: default}, ports:
  {name: dir}}` for the add-part dialog.
