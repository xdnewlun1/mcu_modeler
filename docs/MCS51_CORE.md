# MCS-51 (8051) support: core, arch, P89CV51 family

CHIP_EM runs 80C51 firmware with a pure-Python interpreter (`chipem/core/mcs51`), its own interrupt architecture
(`chipem/arch/mcs51`) and the NXP P89CV51RB2/RC2/RD2 peripheral models (`chipem/periph/mcs51`).  Chips:
`chips/p89cv51rd2.yaml` (64 KB, PLCC44), `p89cv51rd2fbc.yaml` (TQFP44), `p89cv51rc2[fbc].yaml` (32 KB),
`p89cv51rb2[fbc].yaml` (16 KB); register table `chips/p89cv51_regs.yaml` (hand-written).  `chips/i80c52.yaml` is a
generic 80C52 (DIP40, classic 8052 SFR set, `chips/i80c52_regs.yaml`) for arbitrary 8051/8052 firmware: written from
the public Intel MCS-51 architecture, **not from a vendor data sheet**.  Source: data sheet
P89CV51RB2_RC2_RD2 Rev. 03 (25 Aug 2009), `docs/datasheets/nxp/P89CV51RB2_RC2_RD2.pdf`.  The instruction set (not in
the data sheet) follows the Intel MCS-51 user's manual / NXP 80C51 programmer's guide; it was implemented from that
(public, stable) definition without downloading the manual again.

```sh
firmware/samples_mcs51/build.sh                          # tools/mcs51_asm.py -> build/*.hex + .map + .lst
firmware/samples_mcs51/c/build.sh                        # SDCC C samples -> c/build/*.ihx + .cdb + .map + .mem
.venv/bin/chipem shell firmware/samples_mcs51/c/build/timer_tick.ihx --chip p89cv51rd2 --exec "break t0_isr; continue; bt; dis"
.venv/bin/python tools/mcs51_ucsim_diff.py --programs     # exhaustive differential run against ucsim (~12 min)
.venv/bin/chipem run firmware/samples_mcs51/build/uart_hello.hex --chip i80c52 --for 60ms   # generic 80C52
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
* Native hooks (`install_native(addr, fn)`): the IAP entry FFF0h is Python code; it charges its duration with
  `add_cycles` (`add_clocks` for oscillator periods).
* Debugger: `set_breakpoints` (the closure of a breakpoint PC is wrapped: no cost elsewhere), `bp_skip` resume,
  `set_watchpoints` (a separate run loop compares watched RAM after each instruction; SFR and MOVX accesses are
  checked as they happen, reads included; IDATA reads: `idata_reads(pc)` lists the internal-RAM bytes the next
  instruction reads - direct sources and RMW targets below 80h, R0-R7, @Ri pointers and targets, bit-addressable
  bytes tested, POP/RET/RETI stack bytes - checked before it runs), `StopReason.DEBUG` with `stop_info` as in
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

**Response time (polling cycle).**  Intel MCS-51 user's manual, "Response Time": a flag set in machine cycle C1 is
polled in C2 and the hardware LCALL occupies C3-C4, so the handler's first instruction runs no earlier than three
machine cycles after the flag.  The arch records the flag time (the due cycle of the family event that set it,
`arch.event_due`, or `now_cycles()` for a flag set by software) and does not take the source before flag + 1
machine cycle: the instruction executing in the polling cycle completes first (`core.inhibit`); after an Idle or
Power-down wake-up the polling cycle is charged as idle time (`core.add_clocks`).  Example (tests/test_mcs51_gaps.py):
Timer 0 overflows at the end of cycle 114, a NOP runs in 115, LCALL 116-117, handler at 118.  `mcs51: {poll_cycle:
false}` in a chip YAML restores the 2-cycle response.  (ucsim enters the handler 1 machine cycle after the flag and
its Timer 0 drifts by one count per interrupt, so interrupt timing is not part of the ucsim comparison.)
Reset: PC = 0000h or boot vector << 8 (flash model), SP = 07h; `request_reset(reason)` (watchdog, PCA watchdog).

### Timer 2 vector

P89CV51 table in use: INT0 0003h, T0 000Bh, INT1 0013h, T1 001Bh, UART/SPI 0023h, **T2 002Bh**, PCA 0033h; polling
INT0, T0, INT1, T1, PCA, UART/SPI, T2.  Data sheet Table 41 prints T2 at **003Bh** with service priorities UART 5, T2
6, PCA 7; the rest of the data sheet and every other source disagree with that row:

* 80C51 vectors are 8 x n + 3 with n the IE bit (hardware): ET2 is IE.5 (Table 42) -> 002Bh; 003Bh would be IE.7 (EA).
  With T2 at 003Bh nothing would use 002Bh.
* Fig. 25 (interrupt structure) chains INT0, T0, INT1, T1, PCA (CF/CCFn), UART (RI/TI) / SPI (SPIF), T2 (TF2/EXF2).
* The P89V51RB2/RC2/RD2 data sheet Rev. 05 (12 Nov 2009, same family, same SFR map), Table 43: "PCA CF/CCFn 0033H
  service priority 6, UART/SPI TI/RI/SPIF 0023H 7, T2 TF2, EXF2 002BH 8" (plus a brown-out source the CV51 lacks).
* SDCC's `8052.h`/`mcs51reg.h` (TF2_VECTOR 5 = 0x2B), `at89c51ed2.h` (TF2 5 = 0x2B, PCA 6 = 0x33); SDCC's own
  `p89v51rd2.h` defines no vectors.  Keil's header was not checked (not available here).

So `chips/p89cv51rd2.yaml` uses 002Bh and the Fig. 25 / P89V51 polling order (one line per source; `vector: 0x003B`
on TIMER2 follows Table 41 literally).  `firmware/samples_mcs51/c/p89cv51.h` defines `TF2_VECTOR 5`.

## P89CV51 family (`chipem/periph/mcs51`)

| model | SFRs | notes |
|---|---|---|
| `mcs51.port` | P0-P3 | quasi-bidirectional (latch 1 = weak pull-up input, 0 = strong low), P0 open drain, pin reads vs latch (RMW), alternate outputs claimed by peripherals (pin = latch AND alt) |
| `mcs51.sysctrl` | PCON AUXR CKCON | idle / power-down (`power_hooks`, `wake_gate`, Freezer), POF, SMOD1/SMOD0, EXTRAM, X2 and the per-peripheral CKCON bits |
| `mcs51.timer01` | TCON TMOD TL0/1 TH0/1 | modes 0-3, gate, counter inputs, INT0/INT1 edge/level |
| `mcs51.timer2` | T2CON T2MOD RCAP2 TL2/TH2 | capture, auto-reload up/down, baud generator, clock-out |
| `mcs51.uart` | SCON SBUF SADDR SADEN | modes 0-3 (mode 0 receive: REN = 1, RI = 0 start 8 RXD samples with the shift clock on TXD), Timer 1/2 baud, SMOD1, FE (SMOD0), multiprocessor SM2 + automatic address recognition; uartwire frames (9-bit modes carry TB8 as the 9th data bit) |
| `mcs51.spi` | SPCR SPSR SPDAT | master/slave bit-level on P1.4-P1.7, WCOL, board SPI flash responder |
| `mcs51.pca` | CCON CMOD CCAPMn CL/CH CCAPn | capture, software timer, high-speed output, 8-bit PWM, module 4 watchdog, 4 clock sources |
| `mcs51.wdt` | WDTRST | 1Eh/E1h enable/feed, 14-bit, reset |
| `mcs51.flash` | (none) | native IAP (FFF0h, Table 12; 30 ms page / 150 ms block erase charged as CPU time), boot ROM overlay (ENBOOT), boot vector / status bit / PSEN boot, security bits, FX2 |
| `mcs51.xbus` | XDATA 0x10000 | external data bus; board `sram` device (`bus: XBUS`), else floating FFh; `waveform(t0, t1)`: ALE / RD / WR / P0 / P2 edges computed lazily from the recorded accesses (Table 55 timing) |

Counters are computed lazily from a start time (exact to the oscillator period) and flags are set by scheduler
events at the exact overflow / match cycle, armed only while the flag is clear (Timer 1 as baud clock costs one
event).  Peripheral clock = `periph_div(chip, bit)`: 12 oscillator periods, or 6 in 6-clock mode when the CKCON bit
is 0 (Table 52); the data sheet text that says "fosc/6" assumes 6-clock mode, Table 23's baud rates likewise.

## SDCC (C) workflow

SDCC 4.x builds for the mcs51 port; binaries are `sdcc`, `sdas8051`, `s51` upstream and `sdcc-sdcc`,
`sdcc-sdas8051`, `sdcc-s51` on Fedora (scripts and tests look for both).  `firmware/samples_mcs51/c/build.sh` uses
`-mmcs51 --debug --iram-size 256 --xram-size 768` and keeps `<name>.ihx` (loaded by chipem), `.cdb` (debug info),
`.map`, `.mem`.  `p89cv51.h` is the SFR header written from data sheet Table 4 (+ the IE/IP tables and Table 41).

| sample | proves |
|---|---|
| `blinky.c` | SDCC startup, Timer 0 polling: P1.0 every 46080 counts (+ loop overhead) |
| `uart_console.c` + `uart.c` | printf/putchar, getchar in Idle, interrupt-driven TX and RX rings in one `__interrupt(4) __using(2)` handler |
| `timer_tick.c` | `__interrupt(1) __using(1)` (the handler sees PSW.RS = 1), PT0, 1 ms tick: 922 + 15 machine cycles per tick (polling cycle, LCALL, prologue, reload) |
| `pca_pwm.c` | 8-bit PWM CEX0 + capture CEX1 in `__interrupt(6)` (boards/p89cv51_pca_loopback_c.yaml) |
| `spi_flash.c` | SPI master, JEDEC ID + READ into `__xdata` (boards/p89cv51_spi_flash_c.yaml) |
| `iap_flash.c` | IAP from C through a `__naked` wrapper around `LCALL 0FFF0h`: erase, program, read back via IAP and `__code` pointer |
| `xram_test.c` | 768 B on-chip XRAM (`__xdata __at(0)`), AUXR.EXTRAM switching MOVX to the (floating) external bus |
| `power_down.c` | Idle wake-ups from Timer 0, Power-down left by level INT0 (after the 1024-clock oscillator restart) |
| `models.c` | small / medium / large and `--stack-auto` builds (`models_*`): recursion, `__reentrant`, struct copies, generic pointers into data/idata/xdata/code, function-pointer table |
| `math.c` | `_mullong`/`_divslong`/`_modslong`, float `+ - * /`, `sqrtf sinf expf logf atanf powf`, conversions (`math_small`, `math_large`) |

Memory models: small (variables in DATA), medium (PDATA: MOVX @Ri with the page in P2), large (XDATA: MOVX @DPTR);
`--stack-auto` puts parameters and locals on the stack (reentrant).  XDATA symbols below the on-chip XRAM size get the
XRAM tag (0x4xxxx) when the image is loaded (`arch.map_symbols`): `mem data 16` shows what MOVX reads with EXTRAM = 0.
SDCC note: passing a `__code` pointer cast to a generic pointer through `printf`'s varargs prints garbage with
SDCC 4.5 (ucsim shows the same), so `iap_flash.c` copies the string to RAM first.

## Source-level debugging (SDCC)

SDCC 4.5 has no ELF/DWARF for mcs51 (`--out-fmt-elf` is rejected: "unknown compiler option"); the debugger reads the
CDB file instead (`chipem/sdcc_cdb.py`): `L:C$file$line...:addr` line records, `L:G$name`/`L:XG$name` (and `F`/`XF`
for static functions) function start / end (XG = address of the RET), `L:A$module$line` for assembler-only modules,
`S:` records for the address space of each symbol (E/G/H/B/R internal RAM, F/A/P XDATA, C/D code, I SFR, J bit).
`load_cdb` builds a `CdbInfo`, a `chipem.dwarf.DwarfInfo` without CFI, so everything built on DwarfInfo works:
`break main` (the first line after the opening one), `break file.c:line`, `list`, `next`/`step`/`finish`, the shell's
`dis` and the web Debug tab's mixed source/assembly (`Debugger.listing`), symbols in `describe`.  Call stack: the
arch's stack walk (`MCS51DebugSupport.backtrace`): return addresses after LCALL/ACALL found on the 8-bit stack plus
the interrupted PC of every interrupt in progress (stack slot recorded at interrupt entry, `how = "interrupt"`).
`next` without CFI treats a breakpoint as "same frame" when SP is at most one byte above the SP at the start (SDCC may
keep one saved byte across a call; a deeper call pushed at least its return address), so recursion is stepped over.
Not available from CDB: local variables and C types (`info locals`, `print struct.member`); globals print as
addresses (`mem`/`x` show the bytes).

GDB RSP: `--gdb PORT` works with generic RSP clients; gdb has no 8051 architecture.  target.xml: architecture
`mcs51`, feature `org.chipem.mcs51`: r0-r7 (current bank), a, b, psw, sp (8 bit), dptr, pc (16 bit), dps; addresses
are the tagged 8051 spaces above (CODE 0x00000, XDATA 0x10000, IDATA 0x20000, SFR 0x30080, XRAM 0x40000).

## Verification status

* **Differential testing against ucsim** (`sdcc-s51` 0.8.15, SDCC's independent 8051 simulator; `tests/mcs51_ucsim.py`
  drives it on a pseudo-terminal with a NUL prompt).  Compared after each case: PC, ACC, B, PSW, SP, DPTR, P1, P2,
  all 256 bytes of IRAM, the XDATA bytes involved and the oscillator clock count.
  - all 255 opcodes x the boundary grid A x operand x CY x AC over {00 01 0F 10 7F 80 99 F0 FE FF} (400 cases per
    opcode, random other state, operands from internal RAM and the core SFRs, relative jumps of 00/01/7F/80/FE/FF,
    PCs at page and 64 KB edges), plus 2000 random 60-instruction programs (jumps landing on instruction boundaries):
    104 000 cases, 222 000 instructions, **no difference** (`tools/mcs51_ucsim_diff.py`, ~12 min);
  - the seven SDCC programs (`models_*`, `math_*`) from reset to `finish()`: IRAM, XDATA 0000-02FF and the clock
    count identical (e.g. models_large_sa 1 272 588 clocks in both);
  - fast subset in `tests/test_mcs51_ucsim.py` (3 cases per opcode, 30 random programs, the SDCC programs; ~6 s;
    skipped without s51).
  No core bug was found.  ucsim 0.8.15 deviates from the Intel definitions in five places; the generators avoid them
  and `test_intel_behaviour_where_ucsim_deviates` checks our (Intel) behaviour: (1) PSW.P is recomputed only when ACC
  is written, so MOV/POP/XCH/INC PSW keep the written P bit; (2) SETB/CLR/CPL/MOV RS0/RS1 do not switch ucsim's
  register bank until PSW is written as a byte; (3) MOVC A,@A+PC / @A+DPTR beyond FFFFh read 00h instead of wrapping
  to 0000h; (4) PUSH SP stores the old SP (Intel: "(SP) <- (SP)+1; ((SP)) <- (direct)" stores the incremented one;
  not verified on silicon); (5) an SP wrap is a "stack_overflow" error that stops ucsim (switched off).  Opcode A5h
  (reserved) is not compared.

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
* Not verified against silicon or NXP's simulator; the IAP ID bytes are not in either data sheet (placeholders); the
  "read security bits" layout follows the printed bit list (`chipem/periph/mcs51/flash.py`).

## ISP bootloader (not emulated)

Data sheet sec. 6.3.6-6.3.7 and Table 11 describe the protocol (autobaud on an uppercase "U", auto-echo, Intel HEX
records of up to 32 data bytes, "X" on a checksum error, "." on success, record types 00 program, 01 EOF, 03 erase /
security / status / boot vector / chip erase, 04 display or blank check, 05 misc read, 06 direct baud load), but not
enough to answer a host tool byte for byte: the reply format of record types 04 (how displayed data is formatted and
terminated, what blank check returns) and 05 (how the read value is sent), the device ID values, the replies to an
erase or program failure / security violation, whether "." follows the echoed CR/LF, the timing (autobaud accuracy,
how many bit times the measurement uses) and what happens on records longer than 32 bytes.  The P89V51 data sheet
Rev. 05 has the same gaps.  A model would have to invent them; reset into the boot vector FCh still halts at FC00h
with a `warn`.
