"""C28x core fixes found by running and disassembling TI cl2000 25.11.1 output (firmware/samples_c2000_c, the RTS
libraries) and by assembling every isa.yaml entry with TI's assembler.  Machine words below are cl2000/dis2000's."""
from __future__ import annotations

import struct

import pytest

from chipem.core.base import StopReason
from chipem.core.c28x import C28xCore
from chipem.core.c28x.asm import assemble, disasm, disasm_one

CODE = 0x8000


def load(src: str, *, fpu: bool = False, sp: int = 0x400) -> C28xCore:
    lines = "\n".join("        " + ln.strip() if ln.strip() and not ln.strip().endswith(":") else ln.strip()
                      for ln in src.strip().splitlines())
    r = assemble(f'        .sect "code", {CODE}\nstart:\n{lines}\n        ESTOP0\n')
    c = C28xCore(fpu=fpu)
    c.map_memory("RAM", 0x0000, 0x10000)
    for a, d in r.segments:
        c.write_mem(a, d)
    c.reset(CODE, sp)
    return c


def load_words(words, *, fpu: bool = False) -> C28xCore:
    c = C28xCore(fpu=fpu)
    c.map_memory("RAM", 0x0000, 0x10000)
    c.write_mem(CODE, struct.pack(f"<{len(words) + 1}H", *words, 0x7625))     # + ESTOP0
    c.reset(CODE, 0x400)
    return c


def run(c: C28xCore) -> C28xCore:
    reason, _ = c.run(100_000)
    assert reason is StopReason.BREAKPOINT, (reason, c.last_fault)
    return c


def nz(c):
    return c.n, c.z


# ============================================ "if (loc16 = @AX) N/Z from AX" on store-type instructions (SPRU430F)
def test_pread_to_al_sets_n_and_z_boot28_cinit_loop():
    """The COFF RTS boot28.asm .cinit loop: `PREAD AL,*XAR7` / `SB GET_ADDR,GEQ` / `NEG AL`.  A negative record size
    is detected by GEQ on the N flag of the loaded AL.  Before the fix PREAD left N/Z alone and _c_int00 copied garbage until
    it ran off the end of RAM (write_unmapped at 0xE000)."""
    c = run(load("""
        MOVL    XAR7,#0x9000
        MOV     @AL,#0xFFFE
        MOV     *(0:0x9000),@AL
        MOVB    AL,#1               ; N = 0
        PREAD   @AL,*XAR7           ; AL = 0xFFFE -> N = 1
        SB      pos,GEQ
        NEG     AL
        MOV     @AR3,AL
        SB      done,UNC
    pos:
        MOV     @AR3,#0x7777
    done:
    """))
    assert c.xar[3] & 0xFFFF == 2


@pytest.mark.parametrize("setup,insn,want", [
    ("MOVL XAR7,#0x9000\n MOV *XAR7,#0\n MOV @AH,#5", "PREAD @AH,*XAR7", (0, 1)),
    ("MOV @AL,#0x8000\n MOV *(0:0x9000),@AL\n MOVB AL,#0", "MOV @AL,*(0:0x9000)", (1, 0)),
    ("MOV @AR0,#0\n PUSH @AR0\n MOVB AL,#1", "POP @AL", (0, 1)),
    ("MOV @AH,#0x8000\n MOVB AL,#1", "MOV @AL,AH,UNC", (1, 0)),
    ("MOVB AL,#5", "MOVB @AL,#0,UNC", (0, 1)),
    ("MOV @AL,#0x0012\n MOV @AH,#0x8000", "MOVB @AH,AL.LSB", (1, 0)),     # AH = 0x8012
    ("MOV @AH,#0x0055\n MOVB AL,#0\n MOVB XAR0,#1\n CMPB AL,#1", "MOVB @AL,AH.MSB", (0, 1)),   # AL = 0x0000
    ("MOVB ACC,#0\n MOVB AL,#0\n ADDB ACC,#0", "MOVL @ACC,ACC,UNC", (0, 1)),
])
def test_store_to_ax_sets_n_z(setup, insn, want):
    c = run(load(setup + "\n" + insn))
    assert nz(c) == want, insn


def test_conditional_store_false_keeps_flags():
    c = run(load("""
        MOV     @AH,#0x8000
        MOVB    AL,#0
        MOVB    AL,#0               ; Z = 1 (and N = 0)
        MOV     @AL,AH,NEQ          ; NEQ false: no move, flags kept
    """))
    assert c.acc & 0xFFFF == 0 and nz(c) == (0, 1)


def test_pread_to_memory_keeps_flags():
    c = run(load("""
        MOVL    XAR7,#0x9000
        MOV     @AL,#0x8000
        MOV     *(0:0x9000),@AL
        MOVL    XAR4,#0x9100
        MOVB    AL,#0               ; Z = 1
        PREAD   *XAR4,*XAR7
    """))
    assert nz(c) == (0, 1)


# ============================================ MOV32 RaH <-> ACC/P/XT/XARn (TI encoding: MOV32 *(0:0x0F12+4a) forms)
@pytest.mark.parametrize("words,text", [
    ([0xBDA9, 0x0F12], "MOV32 R0H, @ACC"), ([0xBFAB, 0x0F12], "MOV32 @P, R0H"), ([0xBFA9, 0x0F16], "MOV32 @ACC, R1H"),
    ([0xBDA1, 0x0F1A], "MOV32 R2H, @XAR1"), ([0xBFAC, 0x0F2E], "MOV32 @XT, R7H"),
])
def test_mov32_register_forms_decode_like_dis2000(words, text):
    n, t, e = disasm_one(words, CODE)
    assert n == 2 and t.replace(" ", "") == text.replace(" ", "")
    r = assemble(f' .org {CODE}\n {text}\n')
    assert [w for _, w in sorted(r.words().items())] == words


def test_mov32_register_forms_execute():
    # MOV32 R1H,@ACC ; MOV32 @P,R1H ; MOV32 @XAR3,R1H ; MOV32 R2H,@XAR3 ; MOV32 @ACC,R0H (R0H = 0 -> Z)
    c = load_words([0xBDA9, 0x0F16, 0xBFAB, 0x0F16, 0xBFA3, 0x0F16, 0xBDA3, 0x0F1A, 0xBFA9, 0x0F12], fpu=True)
    c.acc = 0xC0490FDB
    run(c)
    assert c.r[1] == c.r[2] == 0xC0490FDB and c.p == 0xC0490FDB and c.xar[3] == 0xC0490FDB
    assert c.acc == 0 and nz(c) == (0, 1)
    # memory operands keep the 0xE2AF / 0xE203 mem32 forms
    r = assemble(f" .org {CODE}\n MOV32 R0H,*XAR4++\n MOV32 *XAR4++,R1H\n")
    assert [w for _, w in sorted(r.words().items())] == [0xE2AF, 0x0084, 0xE203, 0x0184]


def test_mov32_abs16_other_addresses_unchanged():
    n, t, _ = disasm_one([0xBDA9, 0x0F02], CODE)                          # not an FPU register address
    assert t.replace(" ", "") == "MOV32*(0:0x0F02),@ACC"
    n, t, _ = disasm_one([0xBD84, 0x0F12], CODE)                          # memory loc32: the abs16 form
    assert t.replace(" ", "") == "MOV32*(0:0x0F12),*XAR4++"


# ============================================ MOVST0 FLAG layout (TI assembler: TF 0x80 ZI 0x20 ZF 0x10 NI 0x08 NF 0x04)
def test_movst0_field_layout_and_semantics():
    assert disasm_one([0xAD14], CODE)[1].replace(" ", "") == "MOVST0NF,ZF"         # RTS fpu32 library word
    for name, w in (("TF", 0xAD80), ("ZI", 0xAD20), ("ZF", 0xAD10), ("NI", 0xAD08), ("NF", 0xAD04),
                    ("LUF", 0xAD02), ("LVF", 0xAD01)):
        assert [x for _, x in sorted(assemble(f" .org 0\n MOVST0 {name}\n").words().items())] == [w]
    c = load("""
        SETFLG  NF=1, ZF=0, NI=0, ZI=1, TF=1
        MOVST0  NF, ZF
    """, fpu=True)
    run(c)
    assert (c.n, c.z) == (1, 0)                         # ZI=1 is not selected by MOVST0 NF,ZF
    c = load("""
        SETFLG  TF=1
        MOVST0  TF
    """, fpu=True)
    run(c)
    assert c.tc == 1 and c.c == 1


# ============================================ INTR field = vector - 1
@pytest.mark.parametrize("name,word,vec", [("INT1", 0x0010, 1), ("INT5", 0x0014, 5), ("INT14", 0x001D, 14),
                                           ("DLOGINT", 0x001E, 15), ("RTOSINT", 0x001F, 16)])
def test_intr_vector_numbers(name, word, vec):
    assert [w for _, w in sorted(assemble(f" .org 0\n INTR {name}\n").words().items())] == [word]
    assert disasm_one([word], 0)[1] == f"INTR {name}"
    c = load_words([word])
    seen = []
    c.vector_fetch = lambda n: (seen.append(n), CODE + 1)[1]            # vector -> the ESTOP0 after INTR
    run(c)
    assert seen == [vec]


# ============================================ disassembler spellings that match dis2000 (and the manual's examples)
def test_disasm_spellings():
    assert disasm_one([0x92AC], 0)[1] == "MOV AL,@T"                     # loc 0xAC: @T (table 5-1 says @TH)
    assert [w for _, w in sorted(assemble(" .org 0\n MOV AL,@TH\n").words().items())] == [0x92AC]
    assert disasm_one([0x56D0, 0x4567], 0)[1] == "XB 0x4567,NEQ"         # TI: pma without the 0: prefix
    assert [w for _, w in sorted(assemble(" .org 0\n XB 0:0x4567,NEQ\n").words().items())] == [0x56D0, 0x4567]
    assert "GEQ" in disasm(list(assemble(" .org 0\n MOV32 R1H,R5H,GEQ\n").words().values()), 0)[0][2]
