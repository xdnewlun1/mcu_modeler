"""C28x additions for TI-compiled code: RPTB (repeat block, FPU cores), AMODE = 1 (C2xLP) addressing, MOVB byte
selection, SUBCUL flags, FPU opcodes trapping on a core without the FPU."""
from __future__ import annotations

import pytest

from chipem.core.base import StopReason
from chipem.core.c28x import C28xCore
from chipem.core.c28x.asm import assemble, disasm
from chipem.core.c28x.isa import AsmError

CODE = 0x8000
RA, RAS = 1 << 30, 1 << 31


def load(src: str, *, fpu: bool = True, sp: int = 0x400) -> tuple[C28xCore, dict]:
    r = assemble(f'        .sect "code", {CODE}\nstart:\n{src}\n        ESTOP0\n')
    c = C28xCore(fpu=fpu)
    c.map_memory("RAM", 0x0000, 0x10000)
    for a, d in r.segments:
        c.write_mem(a, d)
    c.reset(CODE, sp)
    return c, r.symbols


def run_to_end(c: C28xCore, max_insns: int = 100_000) -> int:
    reason, n = c.run(max_insns)
    assert reason is StopReason.BREAKPOINT, (reason, c.last_fault)
    return n


# ======================================================================== RPTB
BLOCK8 = """
        .align 2
        NOP                     ; makes RPTB odd-aligned so the block starts odd: 8 words allowed
        RPTB    blk_end, {rc}
        ADDB    AL,#1
        ADDB    AL,#1
        ADDB    AL,#1
        ADDB    AL,#1
        ADDB    AL,#1
        ADDB    AL,#1
        ADDB    AL,#1
        ADDB    AH,#1
blk_end:
        MOV     @AR3,AL
"""


def test_rptb_immediate_count():
    c, syms = load(BLOCK8.format(rc="#4"))
    n = run_to_end(c)
    assert c.acc & 0xFFFF == 7 * 5 and c.acc >> 16 == 5 and c.xar[3] & 0xFFFF == 35
    # RB after the loop: RA cleared, RC = 0, RSIZE = 8, RE = low 7 bits of the last block word
    assert c.rb & RA == 0 and c.rb & 0xFFFF == 0
    assert (c.rb >> 23) & 0x7F == 8 and (c.rb >> 16) & 0x7F == (syms["blk_end"] - 1) & 0x7F
    # NOP + RPTB + 5 x 8 block instructions + MOV + ESTOP0: every instruction counted once
    assert n == 1 + 1 + 40 + 1 + 1


def test_rptb_zero_count_runs_block_once():
    c, _ = load(BLOCK8.format(rc="#0"))
    run_to_end(c)
    assert c.acc & 0xFFFF == 7 and c.rb & RA == 0


def test_rptb_count_from_loc16():
    c, _ = load("        MOVL XAR7,#9\n" + BLOCK8.format(rc="@AR7"))
    run_to_end(c)
    assert c.acc >> 16 == 10 and c.acc & 0xFFFF == 70


def test_rptb_code_after_block_reached_by_branch_is_not_a_loop():
    """The word after a block keeps its normal behaviour once RA is clear (here: re-entered through a branch)."""
    c, _ = load("""
        NOP
        MOVB    XAR5,#1
again:
""" + BLOCK8.format(rc="#1") + """
        BANZ    again,AR5--
""")
    run_to_end(c)
    assert c.acc >> 16 == 4 and c.acc & 0xFFFF == 28


def test_rptb_interrupted_in_the_middle_with_nested_rptb_in_isr():
    """An interrupt taken inside the block: RAS = RA, RA = 0 on entry; the ISR saves RB, runs its own RPTB, restores
    RB; IRET sets RA = RAS and the outer block completes its remaining iterations."""
    src = BLOCK8.format(rc="#4") + """
        ESTOP0
isr:
        PUSH    RB
        RPTB    isr_end,#2
        ADDB    XAR6,#1
        ADDB    XAR6,#1
        ADDB    XAR6,#1
        ADDB    XAR6,#1
        ADDB    XAR6,#1
        ADDB    XAR6,#1
        ADDB    XAR6,#1
        ADDB    XAR6,#1
        ADDB    XAR6,#1
isr_end:
        POP     RB
        IRET
"""
    c, syms = load(src)
    reason, n = c.run(2 + 13)               # NOP, RPTB, then 13 block instructions: second iteration, 6th insn
    assert reason is StopReason.COUNT
    assert c.rb & RA and c.rb & 0xFFFF == 3
    rb_before = c.rb
    c.take_interrupt(syms["isr"], 1)
    assert c.rb & RA == 0 and c.rb & RAS          # entry: RAS = RA, RA = 0
    c.run(1)                                      # PUSH RB
    reason, n2 = c.run(100_000)
    assert reason is StopReason.BREAKPOINT, c.last_fault
    assert c.xar[6] == 9 * 3                       # ISR block: 9 words, executed RC+1 = 3 times
    assert c.acc & 0xFFFF == 35 and c.acc >> 16 == 5
    assert c.rb & (RA | RAS) == 0 and (c.rb >> 16) & 0x7F == (rb_before >> 16) & 0x7F


def test_rptb_interrupt_exactly_at_block_end():
    """Interrupt taken when PC is the word after the block (the loop-back point): the return goes back there and the
    loop-back happens after IRET."""
    src = BLOCK8.format(rc="#2") + """
        ESTOP0
isr:
        IRET
"""
    c, syms = load(src)
    c.run(2 + 8)                                  # first iteration done; pc = blk_end
    assert c.pc == syms["blk_end"]
    c.take_interrupt(syms["isr"], 1)
    run_to_end(c)
    assert c.acc >> 16 == 3 and c.acc & 0xFFFF == 21


def test_rptb_request_stop_inside_loop_resumes_correctly():
    c, _ = load(BLOCK8.format(rc="#9"))
    total = 0
    while True:                                   # tiny slices: every slice boundary falls somewhere in the loop
        reason, n = c.run(3)
        total += n
        if reason is StopReason.BREAKPOINT:
            break
    assert c.acc >> 16 == 10 and c.acc & 0xFFFF == 70 and total == 1 + 1 + 80 + 1 + 1


def test_rptb_is_illegal_without_fpu():
    c, _ = load(BLOCK8.format(rc="#4"), fpu=False)
    reason, _ = c.run(100)
    assert reason is StopReason.FAULT and c.last_fault["type"] == "illegal_opcode"
    assert c.last_fault["opcode"] >> 8 == 0xB5


def test_fpu_opcode_is_illegal_without_fpu():
    c, _ = load("        MOVIZF32 R0H,#1.0", fpu=False)
    reason, _ = c.run(10)
    assert reason is StopReason.FAULT and c.last_fault["type"] == "illegal_opcode"


def test_rptb_encoding_and_disassembly():
    r = assemble(""" .org 0x101
 RPTB end,#3
 .space 8
end:
 RPTB #end2,@AR7
 .space 9
end2:
""")
    w = r.words()
    assert (w[0x101], w[0x102]) == (0xB588, 0x0003)            # 1011 0101 1 RSIZE=8 / RC
    assert (w[0x10B], w[0x10C]) == (0xB509, 0x00A7)            # 1011 0101 0 RSIZE=9 / 0000 0000 loc16
    assert disasm([0xB588, 0x0003], 0x101)[0][2] == "RPTB 0x10B,#3"
    assert disasm([0xB509, 0x00A7], 0x10B)[0][2] == "RPTB 0x116,@AR7"


@pytest.mark.parametrize("org,size", [(0x100, 8), (0x101, 7), (0x101, 128)])
def test_rptb_block_size_rules(org, size):
    """Block start even: >= 9 words; odd: >= 8; at most 127."""
    with pytest.raises(AsmError):
        assemble(f" .org {org}\n RPTB e,#1\n .space {size}\ne:\n")


# ======================================================================== AMODE = 1
def amode1(words: list[int], setup) -> C28xCore:
    """Run raw words (no AMODE=1 assembler syntax) after LPADDR."""
    c = C28xCore()
    c.map_memory("RAM", 0x0000, 0x10000)
    prog = [0x561E] + words + [0x7625]           # LPADDR ... ESTOP0
    c.write_mem(CODE, b"".join(w.to_bytes(2, "little") for w in prog))
    c.reset(CODE, 0x400)
    setup(c)
    reason, _ = c.run(100)
    assert reason is StopReason.BREAKPOINT, (reason, c.last_fault)
    return c


def test_setc_amode_alias():
    assert assemble(" SETC AMODE\n CLRC AMODE\n").words() == {0: 0x561E, 1: 0x5616}


def test_amode1_direct_7bit():
    """@@7bit: address = DP(15:1):7bit (DP bit 0 ignored). 0x45 would be *-SP[5] in AMODE = 0."""
    def setup(c):
        c.dp = 0x101                              # page base (0x100 << 6) = 0x4000
        c.write_mem(0x4045, (0x1234).to_bytes(2, "little"))
    c = amode1([0x9245], setup)                   # MOV AL,@@0x45
    assert c.acc & 0xFFFF == 0x1234 and c.amode == 1


def test_amode1_c2xlp_post_modify_with_arp():
    """1 1 mmm RRR: address XAR(ARP), modify it, then ARP = RRR (SPRU430F Table 5-1, AMODE = 1)."""
    def setup(c):
        c.arp = 4
        c.xar[4] = 0x5000
        c.xar[5] = 0x6000
        c.xar[0] = 4
        c.write_mem(0x5000, (0x1111).to_bytes(2, "little"))
        c.write_mem(0x6000, (0x2222).to_bytes(2, "little"))
    # MOV AL,*++,ARP5 ; MOV AH,*0--,ARP4 ; MOVL ACC... no: keep to 16-bit moves
    c = amode1([0x92C5, 0x93DC], setup)           # MOV AL,*++,ARP5 ; MOV AH,*0--,ARP4
    assert c.acc == 0x22221111
    assert c.xar[4] == 0x5001 and c.xar[5] == 0x6000 - 4 and c.arp == 4


def test_amode1_circular_xar6_ar1():
    """*+XAR6[AR1%++]: address XAR6 + AR1; AR1 wraps to 0 after reaching XAR1(31:16) (buffer size + 1)."""
    def setup(c):
        c.xar[6] = 0x7000
        c.xar[1] = (2 << 16) | 0
        for i in range(3):
            c.write_mem(0x7000 + i, (0x10 + i).to_bytes(2, "little"))
    # ADD AL,loc16 = 1001 010A LLLL LLLL; four reads: 0x10 + 0x11 + 0x12 + 0x10
    c = amode1([0x94BF] * 4, setup)
    assert c.acc & 0xFFFF == 0x43 and c.xar[1] == (2 << 16) | 1 and c.arp == 6


def test_amode1_common_modes_unchanged():
    """*XARn++, @ARn, @AL ... decode as in AMODE = 0."""
    def setup(c):
        c.xar[2] = 0x5000
        c.write_mem(0x5000, (0x0042).to_bytes(2, "little"))
    c = amode1([0x9282], setup)                   # MOV AL,*XAR2++
    assert c.acc & 0xFFFF == 0x42 and c.xar[2] == 0x5001


def test_amode1_reserved_loc_and_page0_trap():
    c = C28xCore()
    c.map_memory("RAM", 0x0000, 0x10000)
    c.write_mem(CODE, b"".join(w.to_bytes(2, "little") for w in (0x561E, 0x92F0)))
    c.reset(CODE, 0x400)
    reason, _ = c.run(10)
    assert reason is StopReason.FAULT and c.last_fault["type"] == "illegal_opcode" and c.last_fault["pc"] == CODE + 1
    c.write_mem(CODE, b"".join(w.to_bytes(2, "little") for w in (0x561E, 0x3B40, 0x9282)))   # LPADDR; SETC PAGE0
    c.reset(CODE, 0x400)
    reason, _ = c.run(10)
    assert reason is StopReason.FAULT and c.last_fault["pc"] == CODE + 2


def test_amode_back_to_zero():
    c, _ = load("""
        MOVW    DP,#0x100
        LPADDR
        C28ADDR
        MOV     @5,#7
        MOV     AL,@5
""")
    run_to_end(c)
    assert c.acc & 0xFFFF == 7 and c.amode == 0


# ======================================================================== MOVB byte selection, SUBCUL flags
def test_movb_odd_offset_selects_high_byte():
    c, _ = load("""
        MOVL    XAR4,#0x9000
        MOV     *+XAR4[0],#0xA1B2
        MOV     @AL,#0xFFFF
        MOV     @AH,#0xFFFF
        MOVB    AL.LSB,*+XAR4[1]          ; odd offset: high byte of [XAR4+1]
        MOVB    AH.LSB,*+XAR4[0]          ; even: low byte; AX.MSB cleared
        MOV     *+XAR4[1],#0x0000
        MOVB    *+XAR4[1],AH.LSB          ; odd offset: high byte of [XAR4+1] written
        MOVB    *+XAR4[0],AL.LSB          ; even: low byte of [XAR4]
""")
    c.write_mem(0x9001, (0xC3D4).to_bytes(2, "little"))
    run_to_end(c)
    assert c.acc == 0x00B200C3
    assert int.from_bytes(c.read_mem(0x9001, 1), "little") == 0xB200
    assert int.from_bytes(c.read_mem(0x9000, 1), "little") == 0xA1C3


def test_movb_ar0_index_parity_and_msb_forms():
    c, _ = load("""
        MOVL    XAR4,#0x9000
        MOVB    XAR0,#3
        MOV     @AL,#0x1234
        MOVB    AL.MSB,*+XAR4[AR0]        ; AR0 odd: high byte of [0x9003]; AL.LSB unchanged
        MOVB    *+XAR4[AR0],AL.LSB        ; writes the high byte of [0x9003]
""")
    c.write_mem(0x9003, (0xAB00).to_bytes(2, "little"))
    run_to_end(c)
    assert c.acc & 0xFFFF == 0xAB34
    assert int.from_bytes(c.read_mem(0x9003, 1), "little") == 0x3400


def test_subcul_division_and_flags():
    """RPT #31 || SUBCUL ACC,loc32 with ACC = 0, P = numerator: P = quotient, ACC = remainder; N/Z from ACC."""
    c, _ = load("""
        MOVL    XAR4,#0x9000
        MOV     ACC,#0
        MOVL    P,*+XAR4[2]
        RPT     #31
     || SUBCUL  ACC,*XAR4
""")
    c.write_mem(0x9000, (1000).to_bytes(4, "little") + (123456789).to_bytes(4, "little"))
    run_to_end(c)
    assert c.p == 123456789 // 1000 and c.acc == 123456789 % 1000 and c.z == 0
    c, _ = load("""
        MOVL    XAR4,#0x9000
        MOV     ACC,#0
        MOVL    P,*+XAR4[2]
        RPT     #31
     || SUBCUL  ACC,*XAR4
""")
    c.write_mem(0x9000, (10).to_bytes(4, "little") + (50).to_bytes(4, "little"))
    run_to_end(c)
    assert c.p == 5 and c.acc == 0 and c.z == 1 and c.n == 0
