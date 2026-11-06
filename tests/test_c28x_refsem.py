"""C28x/FPU32 semantics checked against the pseudo-code of TI's reference guides (SPRU430F, SPRUHS1C).  Each test
names the reference behaviour it pins down; these are the places where the earlier memory-based implementation
differed from the documents."""
from __future__ import annotations

import struct

from chipem.core.base import StopReason
from chipem.core.c28x import C28xCore
from chipem.core.c28x.asm import assemble
from chipem.core.c28x.isa import f32_bits

CODE = 0x8000


def run(src: str, *, data: str = "", sp: int = 0x400, setup=None) -> C28xCore:
    text = f'        .sect "code", {CODE}\nstart:\n{src}\n        ESTOP0\n'
    if data:
        text += f'        .sect "data", 0x9000\n{data}\n'
    r = assemble(text)
    c = C28xCore()
    c.map_memory("RAM", 0x0000, 0x10000)
    c.map_memory("HI", 0x3F0000, 0x10000)
    for a, d in r.segments:
        c.write_mem(a, d)
    c.reset(CODE, sp)
    c.syms = r.symbols
    if setup:
        setup(c)
    reason, _ = c.run(10_000)
    assert reason is StopReason.BREAKPOINT, (reason, c.last_fault)
    return c


def w(c, a: int) -> int:
    return struct.unpack("<H", c.read_mem(a, 1))[0]


def _run_hi(src: str) -> C28xCore:
    """Run code placed at 0x3F8000 (the C2xLP-compatible X* instructions only reach 0x3F0000-0x3FFFFF)."""
    r = assemble('        .sect "hi", 0x3F8000\n' + src)
    c = C28xCore()
    c.map_memory("RAM", 0x0000, 0x10000)
    c.map_memory("HI", 0x3F0000, 0x10000)
    for a, d in r.segments:
        c.write_mem(a, d)
    c.reset(0x3F8000, 0x400)
    assert c.run(1000)[0] is StopReason.BREAKPOINT, c.last_fault
    return c


def test_lc_lret_push_two_16bit_words_at_odd_sp():
    # LC: [SP] = ret(15:0); SP++; [SP] = ret(21:16); SP++  (no 32-bit alignment); LRET pops the same way
    c = run("""
        LC sub
        MOVB AH,#7
        SB done,UNC
sub:    MOV AL,*-SP[2]        ; low word of the return address
        LRET
done:
    """, sp=0x401)
    assert c.read_reg("sp") == 0x401 and c.read_reg("ah") == 7
    assert w(c, 0x401) == CODE + 2 and w(c, 0x402) == 0
    assert c.read_reg("al") == CODE + 2


def test_lcr_lretr_rpc_pushed_as_two_words():
    def setup(c):
        c.rpc = 0x3F1234
    c = run("""
        LCR #f1
        SB done,UNC
f1:     LCR #f2
        LRETR
f2:     LRETR
done:
    """, sp=0x401, setup=setup)
    assert w(c, 0x401) == 0x1234 and w(c, 0x402) == 0x3F
    assert c.read_reg("sp") == 0x401 and c.rpc == 0x3F1234


def test_xcall_xretc_use_one_16bit_stack_word():
    c = _run_hi("""
        XCALL 0x9000,UNC
        MOVB AH,#5
        ESTOP0
        .org 0x3F9000
        MOVB AL,#3
        XRETC UNC
    """)
    assert c.read_reg("al") == 3 and c.read_reg("ah") == 5
    assert w(c, 0x400) == 0x8002 and c.read_reg("sp") == 0x400


def test_xbanz_tests_ar_arp_then_modifies_and_sets_arp():
    c = _run_hi("""
        MOVB XAR3,#2
        MOV AH,*XAR3++              ; any C28x indirect mode sets ARP = 3; XAR3 = 3
        XBANZ 0x9100,*--,ARP5       ; AR3 = 3 != 0: branch to 0x3F9100; XAR3-- ; ARP = 5
        ESTOP0
        .org 0x3F9100
        MOVB AL,#1
        ESTOP0
    """)
    assert c.read_reg("al") == 1 and c.xar[3] == 2 and c.read_reg("arp") == 5


def test_movst0_copies_tf_to_tc_and_c_and_clears_latched_flags():
    c = run("""
        SETFLG TF=1, LVF=1
        CLRC C
        MOVST0 TF, LVF
        MOV32 @0,STF
    """)
    assert c.read_reg("tc") == 1 and c.read_reg("c") == 1 and c.read_reg("v") == 1
    assert w(c, 0) & 0x41 == 0x40                         # LVF cleared, TF kept


def test_setflg_rndf32_is_stf_bit9():
    c = run("""
        SETFLG RNDF32=1
        MOV32 @0,STF
        SAVE RNDF32=0
        MOV32 @2,STF
        RESTORE
        MOV32 @4,STF
    """)
    assert w(c, 0) == 0x0200
    assert w(c, 2) == 0x0000 and w(c, 3) == 0x8000        # SAVE sets SHDWS (bit 31)
    assert w(c, 4) == 0x0200 and w(c, 5) == 0x0000        # RESTORE: shadow STF, SHDWS forced 0


def _ld(reg: int, x: float) -> str:
    b = f32_bits(x)
    return f"        MOVIZ R{reg}H,#0x{b >> 16:X}\n        MOVXI R{reg}H,#0x{b & 0xFFFF:X}\n"


def test_parallel_mpy_add_reads_old_values():
    src = _ld(1, 3.0) + _ld(2, 4.0) + _ld(4, 1.0) + _ld(5, 2.0) + """
        SETFLG RNDF32=1
        MPYF32 R4H,R1H,R2H || ADDF32 R5H,R4H,R5H
        MACF32 R3H,R2H,R0H,R1H,R2H
        MOV32 @0,R4H
        MOV32 @2,R5H
    """
    c = run(src)
    f = lambda a: struct.unpack("<f", c.read_mem(a, 2))[0]          # noqa: E731
    assert f(0) == 12.0 and f(2) == 3.0                    # R5H = old R4H (1) + R5H (2)
    assert struct.unpack("<f", struct.pack("<I", c.r[0]))[0] == 12.0


def test_parallel_add_with_mov32_load_sets_mov_flags():
    c = run(_ld(1, 1.5) + _ld(2, 2.5) + """
        ADDF32 R3H,R1H,R2H || MOV32 R0H,@0x10
    """, setup=lambda c: c.write_mem(0x10, struct.pack("<f", -2.0)))
    assert struct.unpack("<f", struct.pack("<I", c.r[3]))[0] == 4.0
    assert struct.unpack("<f", struct.pack("<I", c.r[0]))[0] == -2.0
    assert c.stf & 0x14 == 0x14                           # NF and NI from the MOV32 part


def test_macf32_mem_form_accumulates_r3_from_r2():
    # MACF32 R7H,R3H,mem32,*XAR7++: R3H = R3H + R2H, R2H = [mem32] * [XAR7++]
    c = run(_ld(2, 10.0) + _ld(3, 1.0) + """
        MOVL XAR7,#0x9002
        MOVL XAR6,#0x9000
        MACF32 R7H,R3H,*XAR6++,*XAR7++
    """, data="    .float 3.0, 5.0")
    f = lambda r: struct.unpack("<f", struct.pack("<I", c.r[r]))[0]  # noqa: E731
    assert f(3) == 11.0 and f(2) == 15.0 and c.xar[7] == 0x9004


def test_mov_movh_loc16_acc_shifted():
    c = run("""
        MOV @AH,#0x1234
        MOV @AL,#0x5678
        MOV @0,ACC<<#4
        MOVH @1,ACC<<#4
        MOV @2,ACC<<#1
        MOVH @3,ACC<<#1
    """)
    v = 0x12345678
    assert [w(c, i) for i in range(4)] == [(v << 4) & 0xFFFF, ((v << 4) >> 16) & 0xFFFF,
                                           (v << 1) & 0xFFFF, ((v << 1) >> 16) & 0xFFFF]


def test_movu_ovc_uses_low_six_bits():
    c = run("""
        MOVW DP,#0
        MOV @0,#0x0025
        MOVU OVC,@0
        MOV @1,OVC            ; [loc16(15:10)] = OVC
        MOVU @2,OVC           ; [loc16(5:0)] = OVC
        CLRC OVC
        MOVU @3,OVC
    """)
    assert w(c, 1) == 0x25 << 10 and w(c, 2) == 0x25 and w(c, 3) == 0


def test_negf32_conditional():
    c = run(_ld(1, 2.0) + """
        ZERO R0H
        CMPF32 R0H,#0.0          ; ZF = 1
        NEGF32 R2H,R1H,EQ
        NEGF32 R3H,R1H,NEQ       ; condition false: R3H = R1H
    """)
    f = lambda r: struct.unpack("<f", struct.pack("<I", c.r[r]))[0]  # noqa: E731
    assert f(2) == -2.0 and f(3) == 2.0


def test_mov_xarn_pc():
    c = run("""
        NOP
here:   MOV XAR2,PC
    """)
    assert c.xar[2] == c.syms["here"]
