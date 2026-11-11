# MCS-51 (8051) support: core, arch, P89CV51 family

CHIP_EM runs 80C51 firmware with a pure-Python interpreter (`chipem/core/mcs51`), its own interrupt architecture
(`chipem/arch/mcs51`) and the NXP P89CV51RB2/RC2/RD2 peripheral models (`chipem/periph/mcs51`).  Chips:
`chips/p89cv51rd2.yaml` (64 KB, PLCC44), `p89cv51rd2fbc.yaml` (TQFP44), `p89cv51rc2[fbc].yaml` (32 KB),
`p89cv51rb2[fbc].yaml` (16 KB); register table `chips/p89cv51_regs.yaml` (hand-written).  Source: data sheet
P89CV51RB2_RC2_RD2 Rev. 03 (25 Aug 2009), `docs/datasheets/nxp/P89CV51RB2_RC2_RD2.pdf`.  The instruction set (not in
the data sheet) follows the Intel MCS-51 user's manual / NXP 80C51 programmer's guide; it was implemented from that
(public, stable) definition without downloading the manual again.

```sh
firmware/samples_mcs51/build.sh                          # tools/mcs51_asm.py -> build/*.hex + .map + .lst
.venv/bin/chipem run firmware/samples_mcs51/build/uart_hello.hex --chip p89cv51rd2 --for 100ms --at 60ms:UART=hi
.venv/bin/chipem run --board boards/p89cv51_stm32_uart.yaml --for 20ms
.venv/bin/chipem shell firmware/samples_mcs51/build/uart_t2.hex --chip p89cv51rd2 --exec "break putc; continue; bt"
.venv/bin/python tools/mcs51_asm.py prog.asm -o prog.hex      # --disasm prog.hex to disassemble
```

## Address spaces (tagged ranges)

The 8051 has separate spaces; CHIP_EM maps them onto one flat 8-bit-unit address space so the Bus, `mem`, events,
symbols and the web UI work unchanged:

| tag range | space | contents |
|---|---|---|
| `0x00000-0x0FFFF` | CODE | flash (MOVC, fetch); unmapped code reads FFh; the boot ROM overlays FC00-FFFF while AUXR1.ENBOOT = 1 |
| `0x10000-0x1FFFF` | XDATA | the external bus as MOVX sees it (`mcs51.xbus`, board `sram` devices) |
| `0x20000-0x200FF` | IDATA | internal RAM; R0-R7 banks at 00-1F, bits at 20-2F, 80-FF indirect only |
| `0x30080-0x300FF` | SFR | special function registers (direct 80-FF) |
| `0x40000-0x402FF` | XRAM | 768-byte on-chip expanded RAM (MOVX below 300h while AUXR.EXTRAM = 0) |

`mem 0x30087 1` reads PCON, `mem 0x20030 8` internal RAM 30h.  Symbols from `.map`/`.cdb` sidecars carry these tags
(DATA symbols at 0x2xxxx, XDATA at 0x1xxxx).

**SFR blocks are sparse.**  A peripheral's SFRs are scattered (the PCA has registers at D8-DE, E9-EE, F9-FE), so every
SFR model is an MMIOBlock with `base = 0x30000`, `size = 0x100`, register offset = SFR address, and a `ranges`
attribute listing the addresses actually mapped.  `Bus.map_mmio` maps only the ranges (`Bus._map_sparse`); the
callbacks it hands to the core carry `target`/`delta` so the core calls `block.read(sfr, 1)` directly.
The core's own SFRs (ACC E0, B F0, PSW D0, SP 81, DPL 82, DPH 83, AUXR1 A2) live in the core; the arch maps a
`CPU` block over them for the Bus/tools, and IE/IP/IPH form the arch's `INTC` block.

## Core (`chipem/core/mcs51/core.py`)

* All 255 opcodes (A5h is reserved: `StopReason.FAULT`, type `illegal_opcode`).  Flags exactly as defined by Intel:
  CY/AC/OV from the carry/borrow chain (bit 3, bit 6 vs bit 7), P = parity of ACC computed on every PSW read (a write
  cannot change it), DA A two-step rule, MUL (OV = product > FFh, CY = 0), DIV (B = 0: OV = 1, CY = 0, A/B kept),
  CJNE CY = unsigned less-than, register banks from PSW.RS1:RS0, 8-bit SP pre-increment pushes into IDATA, PC pushed
  low byte first.
* Read-modify-write instructions (ANL/ORL/XRL direct, INC/DEC/DJNZ direct, CPL/CLR/SETB bit, MOV bit,C, JBC) read the
  port **latch** through `set_latch_reader` (registered by `mcs51.port`); all other reads see the pins.
* MOVX @DPTR / @Ri (P2 latch = high address byte for @Ri); on-chip XRAM below `xram_size` unless `core.extram`
  (AUXR.EXTRAM); dual DPTR: the inactive one is kept aside and swapped when AUXR1.DPS toggles (bit 2 is a hard 0,
  so `INC AUXR1` toggles DPS).
* **Timing.** The Chip clock runs at the oscillator frequency.  `run(n)` budgets and returns oscillator periods:
  each instruction costs its machine cycles (isa.py, Intel table) x `clocks_per_mc` (12, or 6 in 6-clock mode).
  `slice_scale = 12` scales the Chip's max_slice; `cycle_counted` tells the Chip to count `last_insns` as
  instructions.  An interrupt LCALL costs 2 machine cycles.  A slice can end up to one instruction past a scheduled
  event (the instruction in progress completes); family models process their own events at the due cycle
  (`SfrBlock.at`/`_fire`: `now()`, log events and pin edges are stamped with the due time).
* **Speed.** Predecoded closure per PC (`cache[pc]`, a 64K list) with operands and SFR accessors bound; costs in a
  parallel list; the loop is `f = cache[pc]; count += costs[pc]; pc = f()`.  A jump to itself (SJMP/AJMP/LJMP $) and
  a `JB/JNB bit,$` wait loop raise `_Spin`: the rest of the slice is spent at once and `spin_pc` lets the Chip run to
  the next event (every family model sets its flags from scheduler events, so nothing else can end such a loop).
  The generic poll-skip (`set_poll_watch`) is not implemented (returns False): the spin skip covers 8051 wait loops.
* Native hooks (`install_native(addr, fn)`): the IAP entry FFF0h is Python code.
* Debugger: `set_breakpoints` (the closure of a breakpoint PC is wrapped: no cost elsewhere), `bp_skip` resume,
  `set_watchpoints` (a separate run loop compares watched RAM after each instruction; SFR and MOVX accesses are
  checked as they happen, reads included; IDATA reads are not seen), `StopReason.DEBUG` with `stop_info` as in
  CONTRACTS.md, `live_pc()`, `debug_hook`.  `chipem/arch/mcs51/debug.py` gives chipem/debug.py registers, decode
  (calls/returns), the stack walk and `finish`.

Measured (this machine, Python 3.14): about 9 MIPS on code that really executes (timer_irq sample: 19x real time at
11.0592 MHz, 12-clock: real time needs 0.92 M machine cycles/s, about 0.5 MIPS); wait loops are skipped (blinky
650x, UART polling ~110x real time).

## Assembler / disassembler (`chipem/core/mcs51/asm.py`, `tools/mcs51_asm.py`)

Intel ASM51 syntax: labels, `ORG DB DW DS END EQU SET DATA IDATA XDATA BIT CODE $INCLUDE USING`, numbers `12 0Ch
0x0C 1100b 14o 'c'`, `$`, `+ - * / MOD SHL SHR AND OR XOR NOT HIGH() LOW()`, bit operands `P1.0 ACC.7 20h.3 TI`,
generic `JMP/CALL` (= LJMP/LCALL), the standard 8051 SFR/bit names plus the chip's from its register table.  Output
Intel HEX + `.map` sidecar (`C:0x0030 main`).  The disassembler output assembles back to the same bytes (tested over
all opcodes).  Symbols keep their spelling; lookups ignore case.

## Arch (`chipem/arch/mcs51/arch.py`)

Source table from the chip YAML `mcs51.interrupts` (vector, IE/IP bit, OR-ed lines, polling order, power-down
wake).  Lines are named after the flags: TF0 TF1 IE0 IE1 UART SPI TF2 PCA.  A request is taken when EA, its IE bit
and its flag are set and its level (IPH:IP, 0-3) is above every level in progress; ties by polling order.  Taking
it: the level flip-flop is set, hardware-cleared flags acknowledged (`on_vector`: TF0/TF1, IE0/IE1 in edge mode),
LCALL to the vector.  RETI returns `StopReason.EXC_RETURN`; the arch pops the level.  After RETI and after any
IE/IP/IPH write one more instruction runs first (`core.inhibit`; the Chip's masked-slice path runs exactly one).
Reset: PC = 0000h or boot vector << 8 (flash model), SP = 07h; `request_reset(reason)` (watchdog, PCA watchdog).

P89CV51 table (data sheet Table 41): INT0 0003h, T0 000Bh, INT1 0013h, T1 001Bh, UART/SPI 0023h, **T2 003Bh**, PCA
0033h; polling INT0, T0, INT1, T1, serial, T2, PCA.  The predecessor P89C51RD2 has T2 at 002Bh and Fig. 25 of this
data sheet draws the PCA before the UART; the table is followed (one YAML line to change).

## P89CV51 family (`chipem/periph/mcs51`)

| model | SFRs | notes |
|---|---|---|
| `mcs51.port` | P0-P3 | quasi-bidirectional (latch 1 = weak pull-up input, 0 = strong low), P0 open drain, pin reads vs latch (RMW), alternate outputs claimed by peripherals (pin = latch AND alt) |
| `mcs51.sysctrl` | PCON AUXR CKCON | idle / power-down (`power_hooks`, `wake_gate`, Freezer), POF, SMOD1/SMOD0, EXTRAM, X2 and the per-peripheral CKCON bits |
| `mcs51.timer01` | TCON TMOD TL0/1 TH0/1 | modes 0-3, gate, counter inputs, INT0/INT1 edge/level |
| `mcs51.timer2` | T2CON T2MOD RCAP2 TL2/TH2 | capture, auto-reload up/down, baud generator, clock-out |
| `mcs51.uart` | SCON SBUF SADDR SADEN | modes 0-3, Timer 1/2 baud, SMOD1, FE (SMOD0), multiprocessor SM2 + automatic address recognition; uartwire frames (9-bit modes carry TB8 as the 9th data bit) |
| `mcs51.spi` | SPCR SPSR SPDAT | master/slave bit-level on P1.4-P1.7, WCOL, board SPI flash responder |
| `mcs51.pca` | CCON CMOD CCAPMn CL/CH CCAPn | capture, software timer, high-speed output, 8-bit PWM, module 4 watchdog, 4 clock sources |
| `mcs51.wdt` | WDTRST | 1Eh/E1h enable/feed, 14-bit, reset |
| `mcs51.flash` | (none) | native IAP (FFF0h, Table 12), boot ROM overlay (ENBOOT), boot vector / status bit / PSEN boot, security bits, FX2 |
| `mcs51.xbus` | XDATA 0x10000 | external data bus; board `sram` device (`bus: XBUS`), else floating FFh |

Counters are computed lazily from a start time (exact to the oscillator period) and flags are set by scheduler
events at the exact overflow / match cycle, armed only while the flag is clear (Timer 1 as baud clock costs one
event).  Peripheral clock = `periph_div(chip, bit)`: 12 oscillator periods, or 6 in 6-clock mode when the CKCON bit
is 0 (Table 52); the data sheet text that says "fosc/6" assumes 6-clock mode, Table 23's baud rates likewise.

## Verification status

* Opcode table: byte counts and machine cycles of all 255 opcodes checked against an independent transcription of
  the Intel table (`tests/test_mcs51_core.py`), each opcode executed once in 12- and 6-clock mode.
* ADD/ADDC/SUBB flags compared with a bit-level carry/borrow-chain reference over A x operand x CY; DA A exhaustive
  over A x AC x CY and against BCD sums; MUL/DIV, parity, RMW latch rule, MOVX/MOVC, dual DPTR, banks, stack.
* Assembler round trip of every opcode (random operands, page/wrap edges).
* SFR table: 56 registers, every address from Table 4 (pp. 10-12) and reset values from the per-register tables
  (cited per register in `chips/p89cv51_regs.yaml`; SP/ports/others without a table: 80C51 values, marked).
* Pins: PLCC44 and TQFP44 from Table 3 (pp. 5-8); TQFP numbering cross-checked as PLCC - 6 (mod 44) against Fig. 2/3;
  `tools/pin_table_check.py` 0 errors for all six chip files.  The data sheet lists no DIP40 package.
* Peripherals and samples: `tests/test_mcs51_{arch,periph,samples}.py` (timings in machine cycles, boards).
* Not verified against silicon or NXP's simulator; the IAP ID bytes and the "read security bits" return layout are
  not in the data sheet (placeholders / documented interpretation).
