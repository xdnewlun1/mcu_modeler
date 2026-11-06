# C28x core (TMS320C28x + FPU32) and assembler

`chipem/core/c28x/` is a pure-Python interpreter for the TI C28x CPU behind the `Core` interface
(docs/CONTRACTS.md, "16-bit address units and the C28x core"), plus an assembler and disassembler driven by the same
instruction table. The target is the F2833x family (TMS320F28232: C28x + FPU32, 150 MHz).

| file | content |
|---|---|
| `chipem/arch/c28x/isa.yaml` | instruction table: one entry per instruction form (503 entries) |
| `chipem/core/c28x/isa.py` | table loader, operand kinds, shared loc16/loc32 decoder/formatter/parser, decoder |
| `chipem/core/c28x/sem.py` | base-ISA semantics (factories returning per-instruction closures), addressing modes, flags |
| `chipem/core/c28x/fpu.py` | FPU32 semantics |
| `chipem/core/c28x/core.py` | `C28xCore`: registers, memory, decode cache, run loop, interrupts |
| `chipem/core/c28x/asm.py` | assembler, `disasm()`, `disasm_core()`, `encode()` |
| `tools/c28x_asm.py` | CLI: `python tools/c28x_asm.py in.asm -o out.c28img [--map m] [--list l] [--bin b]`, `--disasm img` |
| `tests/test_c28x_core.py`, `test_c28x_asm.py`, `test_c28x_system.py` | instruction groups, round trips, full-chip interrupt test |
| `tests/test_c28x_golden.py`, `test_c28x_refsem.py` | exact machine words from the TI opcode tables; semantics pinned to the TI pseudo-code |
| `tests/test_c28x_rptb_amode.py` | RPTB (counts, interrupts inside the block, slice stops), AMODE = 1, MOVB byte select, SUBCUL, FPU traps |
| `tests/test_firmware_c28x.py` | hand-built cl2000-layout COFF2 and C2000 ELF images (loader, symbols, a run on the F28232) |
| `tests/test_c28x_ticompat.py` | fixes found with TI's cl2000/dis2000 (N/Z on stores to @AX, MOV32 RaH↔ACC/P/XT/XARn, MOVST0, INTR, spellings) |
| `tests/test_c2000_compiled.py` | TI-compiled C (`firmware/samples_c2000_c`): loader, blinky, chat alone and on a board, RTS self-test |

The instruction table has been checked against TI's reference guides (section "Reference and verification" below):
every base-ISA opcode against SPRU430F, every FPU32 opcode against SPRUHS1C chapter 1 (and SPRUEO2B), and then
against TI's own tools (section "Verified against cl2000 25.11.1 output"): every entry assembles to the same words
as TI's assembler, and the disassembler agrees with `dis2000` on all C28x/FPU32 code of TI's run-time libraries.
Every entry is `conf: ref` (the `conf: low` marker remains available for entries the references do not settle).
Not covered: `SXTB`, C27x object mode, FPU64, FINTDIV, TMU/VCU/CLA (see "Known gaps").

## Coverage

| | entries | mnemonics | ref | low | assembler-only aliases | parallel (`A || B`) |
|---|---|---|---|---|---|---|
| base C28x | 410 | 160 | 410 | 0 | 7 | – |
| FPU32 (incl. `RPTB`, `PUSH/POP RB`) | 93 | 47 | 93 | 0 | 3 | 12 |

Base: every instruction page of SPRU430F chapter 6 that is valid in C28x object mode (OBJMODE = 1), including the
C2xLP-compatible `XB/XCALL/XRETC/XBANZ/XPREAD/XPWRITE/XMAC/XMACD`, `NORM ACC,*ind`, `CMPR`, the `MOV/MOVH loc16,ACC<<1..8`
stores, `MOV XARn,PC` and the SETC/CLRC spellings (`C28OBJ` = `SETC OBJMODE`, ...). The C27x-only (OBJMODE = 0)
encodings in the same tables are not included. FPU32: everything in SPRUHS1C chapter 1, including `RPTB`: loads/stores
(`MOV32`, `MOV16`, `MOVD32`, `MOV32 *(0:16bit)`), immediates (`MOVIZF32`/`MOVIZ`, `MOVXI`), arithmetic, compares,
min/max, conversions, `EINVF32`/`EISQRTF32`, `SAVE/RESTORE/SETFLG/MOVST0/TESTTF/SWAPF/ZERO/ZEROA`, `PUSH/POP RB`,
the parallel forms `MPYF32||ADDF32`, `MPYF32||SUBF32`, `{MPY,ADD,SUB}F32||MOV32` (both directions),
`MACF32||MOV32`, `MAXF32/MINF32||MOV32`, and `MACF32` (register alias forms and `MACF32 R7H,R3H,mem32,*XAR7++`).
The assembler adds the pseudo-instructions `MOVF32` and `MOVI32`.

Speed: about 4–5 M instructions/s on a mixed load/store/branch loop (CPython 3.14). `test_interpreter_speed` prints
the number and xfails below 1 M/s.

## Reference and verification

| document | fetched from (2026-11-04, `curl -L`, browser User-Agent) | file | sha256 |
|---|---|---|---|
| SPRU430F, TMS320C28x CPU and Instruction Set Reference Guide (rev. F, April 2015), 551 pages | https://www.ti.com/lit/ug/spru430f/spru430f.pdf | `svd/c28x-docs/dl1/spru430f.pdf` | `f705551b1c4630a0476b58d0ba7509e254c5bf1378956c41ade1e2ac16115a72` |
| SPRUEO2B, TMS320C28x Floating Point Unit and Instruction Set (rev. B, January 2015), 150 pages | https://www.ti.com/lit/ug/sprueo2c/sprueo2c.pdf (ti.com redirects to `sprueo2b`; `/lit/pdf/sprueo2` returns the same file) | `svd/c28x-docs/dl1/sprueo2b.pdf` | `da5c631340ae19d4f1db872f343a0fac181618912c660a10242ed45c9ae253a7` |
| SPRUHS1C, TMS320C28x Extended Instruction Sets Technical Reference Manual (rev. C, November 2019), 800 pages | https://www.ti.com/lit/pdf/spruhs1 (redirects to `spruhs1c`) | `svd/c28x-docs/dl2/spruhs1c.pdf` | `2c4f366b2aff43af84389b0699a1c2d8118f4a015cbb124ab822fd2e0a63dce2` |
| SPRAAO8, Common Object File Format (April 2009) (fetched 2026-11-05) | https://www.ti.com/lit/pdf/spraao8 | `svd/c28x-docs/dl3/spraao8.pdf` | `4332fdcce8e0b604ed5845dca6897604519a921afb0413ac2ca2736ab7d68b14` |
| SPRAC71C, C28x Embedded Application Binary Interface (rev. C, March 2026) (fetched 2026-11-05) | https://www.ti.com/lit/pdf/sprac71 | `svd/c28x-docs/dl3/sprac71c.pdf` | `dd5c681b89dd14ad7f9fd75221491f9250757381741e752fae7a3f4cdbaf59af` |
| SPRU514AA, TMS320C28x Optimizing C/C++ Compiler v25.11.0.LTS User's Guide (March 2026) (fetched 2026-11-05) | https://www.ti.com/lit/pdf/spru514 | `svd/c28x-docs/dl3/spru514aa.pdf` | `e4eff9216caac16b04d38764408bf4d7bf15aea77583ba7bc0f06fec03c6b73f` |

Method: `pdftotext -layout` (text in `svd/c28x-docs/*.txt`); a parser (kept outside the repo) pulled every
"Syntax Options / Opcode" block of SPRU430F chapter 6 (311 instruction pages, 429 encodings) and every
"LSW/MSW" opcode of SPRUHS1C chapter 1 into (syntax, bit pattern) records. Each table entry's fixed bits and field
positions were compared with the records; every mismatch was fixed by hand from the instruction page, including the
operand pseudo-code where it differed from `sem.py`/`fpu.py`. `tests/test_c28x_golden.py` holds ~220 exact machine
words built from the reference bit patterns. SPRUHS1C is used for the FPU where it differs from SPRUEO2B
(`UI16TOF32 RaH,mem16` = `0xE2C4`; SPRUEO2B prints the `I16TOF32` opcode there).

Independent re-check (2026-11-05, for TI-compiled code): a second parser, written from scratch, collected every
opcode pattern of SPRU430F chapter 6 (430 records, OBJMODE 1/X rows) and compared fixed bits and field positions
in both directions: every manual encoding is covered by a table entry of the same mnemonic and every base-ISA table
entry has an identical manual pattern (or is a register-specialised form of one: `MOVZ ARn`, `MOVB XARn,#8bit`,
`SETC/CLRC` flags, `SPM`). The only differences are the documented `MAX AX,loc16` line-break error and parser
artefacts. No opcode was wrong. The instructions cl2000 emits for C code (`LCR/LRETR`, `ADDB/SUBB SP`,
`MOVL *-SP[n]`, `MOVZ/MOVB/MOVU`, `MOV ACC,loc16<<#n`, `SETC/CLRC` combinations, `RPT ||`, `MOVL XARn,#22bit`,
`CMPL/CMP`, `B/SB/BF/SBF`, `LC/LRET/IRET`, `MOVAD/MOVDL/MAC/IMPYL/MPY/DMAC/QMPYL`, `SUBCU/SUBCUL`, `NORM/CSB`,
shifts by T, `TBIT/FLIP/MIN/MAX/MINL/MAXL/ADDCU/XOR/SAT/ZAPA`, `PREAD/PWRITE`, `MOVH`, `XB/XCALL`, `IN/OUT/UOUT`)
were then checked against their pseudo-code. Semantic fixes from that pass:
- `MOVB AX.LSB,loc16` now clears AX.MSB (it kept it), and all four `MOVB AX.LSB/MSB` forms select the high byte of
  the word for `*+XARn[offset]` with an odd offset (3-bit, AR0 or AR1 index) as the instruction pages specify (they
  always used the low byte).
- `SUBCUL`: N and Z come from ACC (the remainder), not from the 64-bit ACC:P.
- `SXTB AX` appears in SPRU430F's summary table but has no instruction page or opcode in any of the cached manuals;
  it is left out (an illegal-instruction trap) rather than guessed.

Known errors and gaps in the documents and how the table resolves them:
- `MOV32 RaH,{ACC|P|XT|XARn}` and `MOV32 {ACC|P|XT|XARn},RaH`: both FPU manuals print the opcode of
  `MOV32 *(0:16bit),loc32` / `MOV32 loc32,*(0:16bit)` (`1011 1101 loc32 / IIII` and `1011 1111 loc32 / IIII`) on
  these pages. That is correct: the 16-bit word is the FPU register's internal address 0x0F12 + 4·a (cl2000 emits
  `0xBFAB 0x0F12` for `MOV32 @P,R0H`, `0xBDA9 0x0F16` for `MOV32 R1H,@ACC`; dis2000 decodes them so). The table
  has one entry per RaH (a+4 in bits 6:2 is not a plain field) with operand kind `reg32` (@XARn/@ACC/@P/@XT only),
  ahead of the mem32 forms; memory operands keep `0xE2AF`/`0xE203`. Until 2026-11-05 the table guessed
  `0xE2AF 0aaa A9`, which TI's tools never produce.
- `MOVST0 FLAG`: SPRUHS1C gives no FLAG layout. TI's assembler: LVF 0x01, LUF 0x02, NF 0x04, NI 0x08, ZF 0x10,
  ZI 0x20, TF 0x80 (`isa.MOVST0_BITS`; not STF order, which the table had assumed). `SETFLG`/`SAVE` use STF order.
- `INTR INTx`: the CCCC field is the vector number − 1 (INT1 = 0 … INT14 = 13, DLOGINT = 14, RTOSINT = 15, from TI's
  assembler). SPRU430F's table lists x → vector, which the table had read as the field value.
- `XPREAD loc16,*(pma)`: the opcode is printed as `1010 1100 MMMM MMMM / LLLL ...`; the table uses
  `1010 1100 LLLL LLLL / pma` like `XMAC`/`XMACD`.
- `MAX AX,loc16` prints `0101 0110 0111 / 001A 0000 0000 LLLL LLLL` (a line break in the wrong place).
- `CMPR 1` and `CMPR 2` both read "AR0 > AR[ARP]"; the table keeps C2xLP meaning (1: AR[ARP] < AR0, 2: AR[ARP] > AR0).
- `SHFT` fields of the `#1..16` shifts (`LSL/LSR/ASR/SFR`, `LSL64/LSR64/ASR64`) and `SHF` of `MOV/MOVH loc16,ACC<<2..8`
  are stored as shift−1. The documents do not say so; the C27x forms of `MOV/MOVH loc16,ACC<<1..8` (8 values in
  3 bits) fix the bias at 1 and a 4-bit field cannot hold 16 otherwise. `#0..15`/`#1..15` shifts are stored as is.
- The branch-offset base: SPRU430F's pseudo-code (`if COND false: PC = PC + 1` for `SB`, `PC + 2` for `B`) shows the
  offsets of `SB`/`SBF`/`B`/`BF`/`BANZ`/`BAR` are relative to the address of the branch itself, not to the next
  instruction. `LB/LC/LCR/FFC` take 22-bit absolute addresses.

## ISA table format

```yaml
- {mn: MOV,  ops: "AX, loc16",          enc: "1001 001A LLLL LLLL", sem: mov, a: {d: ax, s: loc16, f: nz16}, conf: ref}
- {mn: LCR,  ops: "#addr22",            enc: "0111 0110 01XX XXXX XXXX XXXX XXXX XXXX", sem: lcr, conf: ref, norpt: true}
- {mn: ADDCL, ops: "ACC, loc32",        enc: "0101 0110 0100 0000 xxxx xxxx LLLL LLLL", sem: acc32, a: {op: addc, s: loc32}, conf: ref}
- {mn: ZAP,  ops: "OVC",                enc: "0101 0110 0101 1100", sem: mov, a: {d: ovcu, s: zero}, conf: ref, alias: true}
- {mn: "MPYF32||ADDF32", ops: "RaH, RbH, RcH, RdH, ReH, RfH", par: 3, enc: "1110 0111 0100 00ff feee dddc ccbb baaa", sem: f_par, a: {p: mpy_add}, conf: ref}
```

- `enc`: 16 or 32 bit characters, MSB first (first word, then second word). `0`/`1` are fixed bits. A letter is a
  field bit, and a field's value is its bits concatenated in string order. Letters: `L` loc byte, `A` AL/AH,
  `n`/`m` register numbers, `C` immediate, `B` bit number, `H` shift, `K` condition, `O` relative offset, `X` 22-bit
  address, `P` product-shift mode, `M` SETC/CLRC mask, `I` INTR number, `S` RPTB block size, `a`–`f` FPU registers, `F`/`V` FPU flags,
  `x` don't-care bits (SPRU430F `xxxx`: ignored by the decoder, assembled as 0). The FPU's 32-bit opcodes are given
  in the references as LSW then MSW; the LSW is the first (lower-address) word.
- `ops`: comma-separated operand patterns. A pattern is either an operand *kind* or literal text (`ACC`, `XAR4`,
  `*XAR7++`, `ACC:P`, `#16`). `X<<Y` splits an operand into a value part and a shift part. Kinds (defined in
  `isa.py: KINDS`) know their field letter, the role name that semantics receive, and how the value maps to the
  field, prints and parses: `loc16 loc32 AX AX.LSB AX.MSB ARn ARn-- XARn XARn++ XARn-- *XARn ARm ARPn #u5 #u7 #u8 #u10 #u16
  #s8 #s16 #bit #sh0_15 #sh1_15 #sh1_16 #sh1_8 COND CNDF intx off8 off16 addr22 #addr22 rblock pma *pma pa abs16 pm mode
  fflags flagset #f16 RaH..RfH`.
- `sem` names a factory `sem_<name>(core, operands, pc, npc)` in `sem.py`/`fpu.py`. The factory returns a closure
  that executes the instruction and returns the next PC. `a:` holds fixed arguments for it.
- `norpt: true` marks flow-control instructions: under `RPT` they execute once.
- `conf`: `ref` = opcode checked against SPRU430F / SPRUHS1C; `low` = not fully defined there.
- `alias: true`: assembler-only spelling of another entry's encoding (`ZAP OVC` = `CLRC OVC`, `SETC OBJMODE` =
  `C28OBJ`, `MOVIZ` = `MOVIZF32`, `ADDF32 RaH,RbH,#f16` = `ADDF32 RaH,#f16,RbH` ...). Never decoded.
- `par: k` (mnemonic `A||B`): a parallel FPU instruction; the first k operands belong to A. It prints and assembles
  as `A ops || B ops`, also in TI's two-line form (a line starting with `||` continues the previous one). After `RPT`
  the bars are only the repeat marker (`RPT #n || insn` on one line or two).
- Decoding picks the matching entry with the most fixed bits, so an alias such as `PUSH ACC` (= `MOVL *SP++,ACC`) wins
  over the general form. `test_table_has_no_ambiguous_overlaps` rejects overlaps where neither entry is more specific.
  Reserved loc bytes are rejected per entry and decode as illegal.

### loc16 / loc32 (C28x mode, AMODE = 0): one decoder for every instruction (`sem.loc_access`), SPRU430F Table 5-1

| byte | syntax | effective address |
|---|---|---|
| `00IIIIII` | `@6bit` | DP:6bit |
| `01IIIIII` | `*-SP[6bit]` | SP − 6bit |
| `10000nnn` / `10001nnn` | `*XARn++` / `*--XARn` | post-inc / pre-dec by 1 (loc16) or 2 (loc32); ARP = n |
| `10010nnn` / `10011nnn` | `*+XARn[AR0]` / `*+XARn[AR1]` | XARn + AR0/AR1 (unsigned 16 bit); ARP = n |
| `11IIInnn` | `*+XARn[3bit]` (`*XARn` = `[0]`) | XARn + 3bit; ARP = n |
| `10100nnn` | `@ARn` / `@XARn` | register (writing `@ARn` keeps ARnH) |
| `10101000..10101101` | `@AH @AL @PH @PL @T @SP` (`@TH` accepted); loc32 `@ACC`(A9) `@P`(AB) `@XT`(AC) | register |
| `10101110` / `10101111` | `*BR0++` / `*BR0--` | XAR(ARP), then AR(ARP) reverse-carry ± AR0 (upper half kept) |
| `10110nnn` | `*,ARPn` | XAR(ARP), then ARP = n |
| `10111000..10111100` | `*` `*++` `*--` `*0++` `*0--` | XAR(ARP) with C2xLP post-modify (±1/2, ±AR0) |
| `10111101` / `10111110` | `*SP++` / `*--SP` | SP post-inc / pre-dec by 1 or 2 |
| `10111111` | `*AR6%++` | circular: if AR6[7:0] == AR1[7:0] then AR6[7:0] = 0 else AR6[15:0] += size; ARP = 6 |

loc32 `@AH/@PH/@SP` (A8/AA/AD) decode as illegal. A loc32 effective address is forced even.

AMODE = 1 (`LPADDR` = `SETC AMODE`, or writing ST1.AMODE; the decode cache is flushed on every change) uses the right
column of Table 5-1 where it differs (`sem._loc_access_amode1`):

| byte | syntax | effective address |
|---|---|---|
| `0IIIIIII` | `@@7bit` | DP(15:1):7bit (DP bit 0 ignored) |
| `11mmmRRR`, mmm = 0..5 | `*++,ARPn` `*--,ARPn` `*0++,ARPn` `*0--,ARPn` `*BR0++,ARPn` `*BR0--,ARPn` | XAR(ARP), then post-modify XAR(ARP), then ARP = n |
| `10111111` | `*+XAR6[AR1%++]` | XAR6 + AR1; AR1 = 0 if AR1 == XAR1(31:16) else AR1 += 1/2; ARP = 6 |
| `11110xxx`, `11111xxx` | reserved | illegal instruction (ITRAP) |

Every other byte decodes as in AMODE = 0. PAGE0 = AMODE = 1 traps every loc16/loc32 instruction (SPRU430F ST1).
The C compiler always works in AMODE = 0 (SPRU430F 5.3) and the RTS `_c_int00` executes `C28ADDR`; AMODE = 1 only
matters for hand-written C2xLP-style assembly. The assembler and disassembler know only the AMODE = 0 syntax.

## Semantics notes

- ACC add/sub: C is the carry out (subtract: NOT borrow). V is set on overflow and otherwise kept. Testing V with an
  `OV`/`NOV` condition clears it. OVM = 0 counts OVC (6-bit signed, wraps). OVM = 1 saturates ACC and leaves OVC.
  `ADD ACC,loc16<<#16` can only set C and `SUB ACC,loc16<<#16` can only clear it (C2xLP compatibility).
- 16-bit operations (AX, loc16) set N/Z/C/V from the 16-bit result, with no OVC or saturation. Moves into `@AL`/`@AH`
  (or `@ACC`) update N/Z.
- Store-type instructions with an @AL/@AH destination set N/Z from AX (SPRU430F "if (loc16 = @AX)"): `MOV`/`MOVB`
  stores, `PREAD`/`XPREAD`, `POP loc16`, `MOV loc16,*(0:16bit)`, `MOVB loc16,AX.LSB/MSB`, `MOV loc16,AX,COND` and
  `MOVB loc16,#8bit,COND` (only when the condition is true), likewise `MOVL loc32,...` to @ACC (`sem.ax_dest_nz`).
  The COFF RTS `.cinit` loop depends on it (`PREAD AL,*XAR7` / `SB GET_ADDR,GEQ`).
- Compares (CMP, CMPB, CMPL, MIN/MAX): N is the sign of the exact difference, so `LT`/`GT` behave as signed compares
  even when the subtraction overflows.
- `SXM` sign-extends 16-bit operands of the ACC shift-load/add/sub forms, including `#16bit<<#n` immediates.
  `MOVU`/`ADDU`/`ADDB`/logic forms zero-extend. Product shift `P<<PM`: PM 0 = <<1, 1 = none, 2..7 = >>1..6; right
  shifts always sign-extend (SPRU430F ST0.PM description; the old SXM dependence was wrong).
- `SUBCU`: `temp = (ACC<<1) - (loc16<<16)`; if temp ≥ 0, `ACC = temp + 1`, else `ACC <<= 1`. So `RPT #15 || SUBCU`
  divides 32/16 (AL = quotient, AH = remainder).
- `RPTB end,#RC` / `RPTB end,loc16` (FPU cores; SPRUHS1C): RB = RAS(31) | RA(30) | RSIZE(29:23) | RE(22:16) |
  RC(15:0), RSIZE = words in the block (the field of the opcode), RE = low 7 bits of (pc + 1 + RSIZE), RA = (RC != 0).
  RPTB records the block's end address; the instruction there is decoded with a loop-back check
  (`C28xCore._rptb_wrap`): when RA is set and RE matches, RC is decremented (RA cleared at 0) and the block's first
  instruction runs instead, so the block executes RC+1 times and each instruction is counted once. Interrupt entry
  (`_ctx_save`: hardware interrupts, TRAP, INTR) copies RA to RAS and clears RA; `IRET` copies RAS back and clears it,
  so a block interrupted anywhere (also at the loop-back point) resumes, and an ISR may run its own RPTB between
  `PUSH RB` / `POP RB` as the manual prescribes. Matching on the 7-bit RE like the hardware means a restored RB loops
  correctly. Blocks cannot nest (as on silicon). The assembler takes the end label with or without `#`, computes
  RSIZE and enforces 9..127 words for an even block start and 8..127 for an odd one.
- FPU32 opcodes (`sem` `f_*`, `MOV32 *(0:16bit)`, `RPTB`, `PUSH/POP RB`) are illegal instructions on a core created
  with `fpu=False` (the F28232), as on silicon: the arch layer gets ITRAP.
- `RPT #n` / `RPT loc16` executes the next instruction n+1 times atomically. Interrupts cannot be taken in between,
  and the returned count includes the repetitions, so it may exceed `max_insns` by up to 257. PREAD/PWRITE/XPREAD/
  MAC/XMAC and `MOV loc16,*(0:16bit)` increment their internal program address per repetition, as on silicon.
- Interrupt context save (`take_interrupt`, `TRAP`, `INTR`, ITRAP), SPRU430 §3.4: SP += 1, then seven aligned 32-bit
  pushes T:ST0, AH:AL, PH:PL, AR1:AR0, DP:ST1, DBGSTAT:IER, PC(22 bit). For INT1–14/DLOGINT/RTOSINT
  (`take_interrupt` and `INTR`), the IFR and IER bit is cleared after IER was saved. `TRAP` and `ITRAP` leave
  IFR/IER alone. Then INTM = DBGM = 1 and EALLOW = LOOP = IDLESTAT = 0. `IRET` pops in reverse order (AR0/AR1 restore
  only their low halves) and does SP −= 1.
- Calls (SPRU430F pseudo-code): `LC` pushes the return address and `LCR` pushes RPC as two 16-bit writes
  (`[SP] = low; SP++; [SP] = high; SP++`, no 32-bit alignment, so an odd SP works as on silicon); `LCR` then loads RPC
  with the return address. `LRET`/`LRETE`/`LRETR` pop the same way (`LRETE` also clears INTM; `LRETR` jumps to RPC
  and reloads RPC from the stack). `FFC` puts the return address in XAR7. `PUSH/POP RPC` and the register-pair
  pushes are 32-bit stack operations.
- C2xLP flow control: `XB`/`XCALL`/`XBANZ` targets are 0x3F:pma (`XB *AL`/`XCALL *AL`: 0x3F:AL). `XCALL` pushes only
  the low 16 bits of the return address (one stack word) and `XRETC`/`XRET` pop one word into PC(15:0) with
  PC(21:16) = 0x3F. `XBANZ pma,*ind{,ARPn}` tests AR(ARP), then modifies XAR(ARP) per `*ind` (also when not taken),
  then sets ARP. `XB/XCALL pma,*,ARPn` set ARP. `NORM ACC,*ind` modifies XAR(ARP) only when ACC is shifted.
- ARP: every C28x indirect mode sets ARP = n, `*AR6%++` sets ARP = 6 (SPRU430F 5.6). This matters for the C2xLP
  forms (`*`, `*++`, `*BR0++`, `CMPR`, `NORM ACC,*`, `XBANZ`) and for the ST1 image pushed on interrupts.
- `MOV/MOVH loc16,ACC<<1..8` store the low/high word of ACC<<n (N/Z only for `@AL`/`@AH`); `MOV loc16,OVC` stores OVC
  in bits 15:10, `MOVU loc16,OVC` in bits 5:0; `MOV OVC,loc16` loads bits 15:10, `MOVU OVC,loc16` bits 5:0.
  `MOV XARn,PC` loads the address of the instruction itself.
- FPU32: registers are raw IEEE bit patterns. Denormal inputs count as 0, underflow flushes to ±0 (LUF), overflow gives
  ±inf (LVF). STF.RND32 = 1 rounds to nearest even, 0 truncates toward zero (the reset value). The delay slots are not
  modelled, so results are visible at once. `MOVST0` (SPRUHS1C) maps TF→TC and C, ZF|ZI→Z, NF|NI→N, LVF|LUF→V for
  the selected flags and clears the selected LVF/LUF. `SETFLG`/`SAVE` FLAG and VALUE are 11-bit masks of STF[10:0]
  (`RNDF32` is bit 9). `SAVE` copies R0H–R7H/STF to the shadow set and sets STF.SHDWS; `RESTORE` reloads them and
  clears SHDWS. `CMPF32` sets ZF/NF. `MOV32 RaH,mem32` with UNCF (the default) and the `|| MOV32 RaH,mem32` halves of
  parallel instructions set NF/ZF/NI/ZI from the loaded value. `NEGF32 RaH,RbH,CNDF` copies RbH when the condition is
  false. `MACF32 R7H,R3H,mem32,*XAR7++` does R3H += R2H, R2H = [mem32]·[XAR7++]; under RPT the accumulate alternates
  with R7H += R6H, R6H = product. Parallel instructions read all source registers before writing.
  `EINVF32`/`EISQRTF32` return correctly rounded values instead of 8-bit estimates.
- Every instruction takes 1 cycle (`cycles_per_insn = 1`). The pipeline is not modelled.

## Core interface (for `arch/c28x`, the bus and peripheral models)

```python
core = chipem.core.c28x.create_core("c28x", fpu=True)       # or C28xCore(...)
core.map_memory(base, size_units, *, writable=True)          # also map_memory(name, base, size, data=b"", writable=True)
core.map_mmio(base, size_units, read_cb, write_cb)           # read_cb(off, n) -> int, write_cb(off, n, value)
                                                             #   n = 1 (16 bit) or 2 (32 bit: one call, low word first)
core.set_unmapped_handler(fn)                                # fn(addr, n_units, 'r'|'w'|'x') -> True = mapped, retry
core.read_mem(addr, n_units) -> bytes; core.write_mem(addr, data)   # little-endian word image; loader writes ROM
core.reset(pc, sp)                                           # ST1 = 0x0A0B (OBJMODE=1, AMODE=0, INTM=DBGM=VMAP=M0M1MAP=1)
core.run(max_insns) -> (StopReason, executed)                # exact counts (RPT may overshoot, see above)
    # COUNT | WFI (IDLE: pc after it, IDLESTAT=1) | BREAKPOINT (ESTOP0/1: pc after it)
    # FAULT (last_fault = {type, pc, addr, ...}; types illegal_opcode(+opcode), read/write/fetch_unmapped,
    #        illegal_addressing_mode, io_space_unsupported) | USER (request_stop)
core.request_stop(from_mmio=False) -> bool; core.stop()      # from MMIO callbacks or another thread; the current
                                                             # instruction completes, run() returns USER before the next
core.insns_in_slice()                                        # executed so far in the running slice (exact, also mid-MMIO)
core.interrupt_pending() -> int | None                       # 18 if nmi_pending, else lowest set bit of IFR&IER as INTn
                                                             #   (1..14, 15 DLOGINT, 16 RTOSINT) when INTM = 0
core.take_interrupt(vector_addr, int_number)                 # context save + jump (return address = current pc)
core.itrap_handler = fn(pc) -> bool                          # illegal opcode: called with core.pc = the bad instruction;
                                                             #   return True after take_interrupt(...) to continue
core.vector_fetch = fn(vector_number) -> addr                # TRAP #n / INTR targets (default: CPU table at
                                                             #   0x3FFFC0 if VMAP else 0x000000)
core.nmi_pending                                             # NMI hook for interrupt_pending()
core.read_reg/write_reg(name)                                # acc ah al p ph pl xt t tl xar0..7 ar0..7 sp dp pc rpc st0
                                                             # st1 ier ifr dbgier dbgstat r0h..r7h stf rb, plus each flag
                                                             # (ovc pm v n z c tc ovm sxm arp xf m0m1map objmode amode
                                                             #  idlestat eallow loop spa vmap page0 dbgm intm)
core.eallow, core.intm, core.ier, core.ifr                   # live attributes (peripherals check core.eallow)
core.flush_code_cache()
```

Memory is a flat 4 M-word `array('H')` with a 4096-entry page table (1 K words per page). Reads of pages that one
RAM/ROM region fully covers, and writes of fully RAM pages, index the array directly. Every other access bisects the
region list. Decoded instructions are cached per PC. A write to a page that holds decoded code takes the slow path
and drops that page's cache entries, so self-modifying code and code loaded into RAM at runtime work. `pc` read from
an MMIO callback during `run()` is the PC at the start of the slice. `page_size = 0x40` is only the Bus MMIO
dispatch granularity.

## Assembler syntax

TI style, case-insensitive mnemonics and registers, case-sensitive symbols:

```asm
VAL     .set    0x10                  ; also `.equ`, `.set name, expr`
        .sect   "code", 0x8000        ; or `.sect "code"` + `.org 0x8000`; also .text / .data / --sect name=addr
start:  MOVW    DP,#DP(var)           ; DP(x) = x >> 6; LO16(x), HI16(x); $ = current address
        MOV     AL,@var               ; @expr uses the low 6 bits (DP must be set)
        MOV     *+XAR4[3],AL
        MOV     ACC,*XAR4++<<#4
        B       start,NEQ             ; targets are labels/addresses; offsets relative to the branch
        LCR     #func                 ; 22-bit targets: LB/LC/FFC addr, LCR #addr
        RPT     #15
        SUBCU   ACC,@den
        MOVF32  R1H,#1.5              ; pseudo: MOVIZ (+ MOVXI)
        .long   isr                   ; even-aligned, low word first
        .word   1, 'A', 1Fh, 101b
        .string "text"                ; .cstring adds a NUL; .float, .space n, .align n, .bss sym,size, .entry expr
```

Output: `AsmResult.segments` (word address, little-endian bytes), `.symbols`, `.entry` (`.entry`, else `_c_int00`,
`start` or `_start`), `.listing`, `write_c28img()` (the JSON that `chipem.firmware` loads: `{"format": "c28img",
"unit_bits": 16, "entry", "segments": [{"addr", "data_hex"}], "symbols"}`), `write_bin()` (single segment),
`map_text()`, `image()` (a `FirmwareImage`). `disasm(words, addr)` and `disasm_core(core, addr, n)` print text that
the assembler accepts back. `test_every_entry_round_trips` checks this for every table entry.

## Known gaps

- Not implemented: `SXTB AX` (no opcode in the cached manuals), AMODE = 1 syntax in the assembler/disassembler
  (execution is implemented), instructions the manual marks "not repeatable" still repeat under `RPT` (only flow
  control executes once; the compiler never emits such pairs), C27x object mode (OBJMODE = 0 encodings are not in the table; OBJMODE is only a status
  bit), TMU/VCU/CLA instructions, the I/O space (`IN/OUT/UOUT` fault), the BIO pin (`NBIO` is never true), the XF pin (only the
  status bit), debug features (DBGSTAT is a plain register, `ABORTI`/`IACK` are no-ops), FPU pipeline delay slots,
  and per-instruction cycle counts.
- FPU64 (`*F64`, `MOV64`, `MOVIX`, RnL registers) and FINTDIV (`ABSI32DIV32`, `SUBC4UI32`, `SUBC2UI64`,
  `NEGI32DIV32` ...: the fast integer division of newer C28x devices) are not in the table; they decode as `.word`
  and trap. F2833x has neither; TI's `rts2800_fpu64*` and FINTDIV division helpers are not usable here.
- The assembler needs `@` on register operands used as loc16/loc32 (`MOV @AH,#1`, not TI's optional `MOV AH,#1`),
  except where the table has a dedicated spelling (`MOV32 R0H,ACC`).
- `RPT` runs atomically. A `request_stop` that arrives during the repeated instruction ends the slice after it.

## `conf: low` entries

None since 2026-11-05 (`MOVST0 FLAG` was the last; its layout now comes from TI's assembler).
`grep "conf: low" chipem/arch/c28x/isa.yaml` gives the authoritative list.

## Verified against cl2000 25.11.1 output

TI's C2000 code generation tools 25.11.1.LTS (`toolchains/ti-cgt-c2000/ti-cgt-c2000_25.11.1.LTS`: `cl2000`,
`dis2000`, `ofd2000`) were used on 2026-11-05 in three ways (scripts kept outside the repository):

1. **Disassembler vs `dis2000`**: every instruction of `dis2000 -qq -r -e -d` listings decoded from the same words
   by `asm.disasm_one`, compared after normalising spelling (TI's relative branch offsets vs our absolute targets,
   number formats, `PUSH XAR7` = `MOVL *SP++,XAR7`, `C28OBJ` = `SETC OBJMODE`, `#1.5` vs `#0x3FC0`, `ZERO R2` =
   `ZERO R2H`, implicit `UNCF`). CLA sections (`Cla1Prog:*`) are skipped.

   | corpus | objects | instructions | words | agree | length / text mismatches |
   |---|---|---|---|---|---|
   | `rts2800_ml.lib` (COFF) | 502 | 146,152 | 170,319 | 146,152 | 0 / 0 |
   | `rts2800_ml_eabi.lib` | 516 | 233,069 | 278,203 | 233,069 | 0 / 0 |
   | `rts2800_fpu32_eabi.lib` | 545 | 227,900 | 274,768 | 227,412 | 0 / 488 (FINTDIV only) |
   | `firmware/samples_c2000_c/build/*.out` (EABI + COFF) | 6 | 12,818 | 14,817 | 12,818 | 0 / 0 |
   | `rts2800_fpu64_eabi.lib` (not a target) | 539 | 202,830 | 255,378 | 191,125 | 5,720 / 5,985 (FPU64) |

   Before the fixes the libraries showed: `MOV32` RaH↔ACC/P/XT/XARn (1,174 instructions, wrong encoding), `MOVST0`
   (151, wrong field layout), every use of loc byte 0xAC (`@TH` vs `@T`: SPRU430F's table says `@TH`, its examples
   and TI's tools `@T`; we now print `@T` and accept both) and every FPU `GEQ/LEQ` condition (we printed `GE/LE`;
   SPRUHS1C and TI spell `GEQ/LEQ`; both accepted).
2. **Assembler vs TI's assembler**: every table entry with sample operands (7,238 lines, the
   `test_every_entry_round_trips` samples plus every loc byte class, every condition and the field extremes) assembled
   by `cl2000 -v28 -ml --float_support=fpu32` (`-al` listing) and by `asm.assemble` at the same address: all 7,003
   lines TI accepts give identical words, covering 500 of the 503 entries; the other three (RPTB with a label,
   `MACF32 R3H,R2H,RaH,...` with RaH ≠ R3H) were checked by hand, also identical. TI rejects 235 sample lines (C2xLP
   indirect forms with `*,ARPn` in FPU operands, `MOV16 @AR0,...`, register addressing it forbids), none of which
   cl2000 emits. Fixed: `INTR` (field = vector − 1), `MOVST0`, `MOV32` register forms; `XB/XCALL/XBANZ` now print
   `pma` as `0x4567` (TI's syntax; `0:0x4567` still accepted). RPTB's RSIZE (block words) and the 9/8-word minimum
   are confirmed.
3. **Execution**: `firmware/samples_c2000_c` (`blinky`, `uart_chat`, `selftest`; EABI and COFF) run on the
   interpreter, including TI's `_c_int00` (both `.cinit` formats), `I$$DIV/U$$DIV`, `L$$DIV/UL$$DIV`,
   `LL$$DIV/ULL$$DIV`, 64-bit multiply and shifts, soft-float add/mul/div/compare/convert, `memcpy`, `strcmp`,
   `qsort`, `atol`, `snprintf`. `selftest` checks 47 results against host C semantics (`test_c2000_compiled.py`).
   Bugs found: `PREAD` and the other store-type instructions did not set N/Z for an @AL/@AH destination (the COFF
   `.cinit` loop ran off the end of RAM); the COFF loader read debug sections as words (cl2000 gives non-allocated
   COPY sections in bytes). Two soft-float results differ from IEEE round-to-nearest by 1 ulp because TI's RTS
   truncates (`L$$TOFS`: `SFR ACC,7`; `FS$$DIV`: SUBCU quotient with a first-order correction); a host model of the
   RTS code gives the same words as the interpreter.

## Still unverified

- RPTB's run-time behaviour (RE = pc + 1 + RSIZE, the loop-back) is read from SPRUHS1C's RB description; the
  encoding matches TI's assembler, but no TI-compiled RPTB loop has run (cl2000 emits RPTB only with
  `--float_support=fpu32`; the F28232 has no FPU).
- Operand semantics of instructions that neither the samples nor the RTS paths they reach execute (the tool
  comparisons check encodings and decoding, not flag rules).

## Image loaders for TI tools (`chipem/firmware.py`)

cl2000 v25 defaults to EABI (`--abi=eabi`, ELF); `--abi=coffabi` produces COFF2. Both are `.out` and are sniffed by
content.

- **ELF (EABI)**: `e_machine` = EM_TI_C2000 = 141 (`/usr/include/elf.h`, SPRAC71C 11.2). Target addresses
  (`p_paddr`, `e_entry`, `st_value`) are 16-bit word addresses, sizes (`p_filesz`) are bytes (SPRAC71C chapter 11),
  so each PT_LOAD segment's bytes are the word stream at word address `p_paddr` (the load address; run-address copies
  are separate uninitialised segments). `STT_FUNC/OBJECT/NOTYPE` symbols of real sections go to
  `FirmwareImage.symbols` (EABI names have no underscore prefix: `main`, `_c_int00`). An entry of 0 falls back to
  `_c_int00`.
- **COFF2** (SPRAAO8): file header (target id 0x009D = TMS320C2800 is required), optional header (entry), 48-byte
  section headers (size in words; bytes 46–47 = memory page, ignored because the C28x space is unified), 18-byte
  symbols with aux entries, string table for names over 8 characters. Sections are loaded at their physical (load)
  address; STYP_BSS (`.ebss`, `.bss`, `.stack`, `.esysmem`), NOLOAD, DSECT and COPY sections (debug info) are skipped.
  `--rom_model`: `.cinit` is an ordinary section, processed by `_c_int00` at run time. `--ram_model`: the linker marks
  `.cinit` STYP_COPY and the loader must initialise; the records (SPRU514AA 7.10.3.1: size in words stored negative,
  32-bit address, data, 0 = end; confirmed with a cl2000 25.11.1 `--ram_model` image) are applied as image segments,
  all or nothing. Other COPY sections (`.debug_*`, `$build.attributes`) are skipped before their data is read:
  cl2000 gives their size in bytes, not words. Symbols: C_EXT/C_STAT/C_LABEL symbols of real sections, by COFF name
  (`_main`, `_c_int00`, asm labels such as `wd_disable`; compiler labels `$C$L*` are dropped); entry falls back to
  `_c_int00` without an optional header.
- `chipem info` lists the symbol count, the reset target's name and `_c_int00`/`main`. An entry equal to the boot
  address 0x33FFF6 (cl2000 `--entry_point=code_start`, TI's CodeStartBranch scheme) is not reported as a mismatch.
- `tools/c28x_asm.py --disasm` lists `.c28img`, TI `.out` (ELF or COFF, with symbol labels) and `.bin` images.
- Not done: EABI `--ram_model` (compressed `.cinit`, SHT_TI_INITINFO), zero-filling `.ebss` (RAM starts at 0).
