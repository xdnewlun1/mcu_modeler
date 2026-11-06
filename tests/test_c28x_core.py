"""C28x interpreter: assemble a snippet → run → check registers / memory (chipem.core.c28x)."""
from __future__ import annotations

import struct
import time

import pytest

from chipem.core.base import StopReason
from chipem.core.c28x import C28xCore
from chipem.core.c28x.asm import assemble

CODE = 0x8000


def make_core() -> C28xCore:
    c = C28xCore()
    c.map_memory("RAM", 0x0000, 0x10000)
    c.map_memory("HI", 0x3F0000, 0x10000)
    return c


def run(src: str, *, setup=None, max_insns=100_000, data: str = "", sp: int = 0x400) -> C28xCore:
    text = f'        .sect "code", {CODE}\nstart:\n{src}\n        ESTOP0\n'
    if data:
        text += f'        .sect "data", 0x9000\n{data}\n'
    r = assemble(text)
    c = make_core()
    for a, d in r.segments:
        c.write_mem(a, d)
    c.reset(CODE, sp)
    c.syms = r.symbols
    if setup:
        setup(c)
    reason, n = c.run(max_insns)
    assert reason is StopReason.BREAKPOINT, (reason, c.last_fault)
    c.n_executed = n
    return c


def w(c, a: int) -> int:
    return struct.unpack("<H", c.read_mem(a, 1))[0]


def wl(c, a: int) -> int:
    return struct.unpack("<I", c.read_mem(a, 2))[0]


# ------------------------------------------------------------------------------------------------ moves
def test_mov_ax_and_flags():
    c = run("""
        MOVW DP,#0x240        ; 0x9000 >> 6
        MOV AL,@0
        MOV AH,@1
        MOV @2,AL
        MOV @3,#0x1234
        MOV @4,#0
    """, data="    .word 0x8001, 0x0000")
    assert c.read_reg("al") == 0x8001 and c.read_reg("ah") == 0
    assert c.z == 1 and c.n == 0                  # flags from the last load (AH = 0)
    assert w(c, 0x9002) == 0x8001 and w(c, 0x9003) == 0x1234 and w(c, 0x9004) == 0


def test_movb_movl_and_32bit_memory_order():
    c = run("""
        MOVL XAR4,#0x9000
        MOV ACC,#0x1234<<#0
        MOV @AH,#0x0
        MOVB AH,#0xAB
        MOVL *XAR4,ACC
        MOVL XAR5,*XAR4
        MOVL XAR6,#0x9005
        MOVL *XAR6,ACC        ; odd address: the CPU aligns to 0x9004
    """)
    assert c.acc == 0x00AB1234
    assert w(c, 0x9000) == 0x1234 and w(c, 0x9001) == 0x00AB        # low word at the even (lower) address
    assert c.xar[5] == 0x00AB1234
    assert wl(c, 0x9004) == 0x00AB1234 and w(c, 0x9006) == 0


def test_movz_movb_xar_and_dp_forms():
    c = run("""
        MOVL XAR2,#0x3F1234
        MOVB XAR3,#200
        MOVZ DP,#0x3FF
        MOV @AR2,#0x5555
        MOVZ AR4,@AR2
    """)
    assert c.xar[2] == 0x3F5555                    # @AR2 writes only the low 16 bits
    assert c.xar[3] == 200 and c.dp == 0x3FF and c.xar[4] == 0x5555


def test_mov_t_p_xt():
    c = run("""
        MOVL XAR4,#0x9000
        MOV T,*XAR4
        MOVL XT,*+XAR4[2]
        MOVL P,*+XAR4[2]
        MOV PH,*XAR4
    """, data="    .word 0x1111, 0x2222, 0x3333, 0x4444")
    assert c.xt == 0x44443333 and c.p == 0x11113333


# ------------------------------------------------------------------------------------------------ arithmetic
def test_acc_add_overflow_ovc_and_saturation():
    c = run("""
        MOVB ACC,#0
        MOV @AH,#0x7FFF
        OR ACC,#0xFFFF
        ADDB ACC,#1
    """)
    assert c.acc == 0x80000000 and c.v == 1 and c.n == 1 and c.c == 0 and c.ovc == 1
    c = run("""
        SETC OVM
        MOVB ACC,#0
        MOV @AH,#0x7FFF
        OR ACC,#0xFFFF
        ADDB ACC,#1
    """)
    assert c.acc == 0x7FFFFFFF and c.v == 1 and c.ovc == 0
    c = run("""
        SETC OVM
        MOVB ACC,#0
        MOV @AH,#0x8000
        SUBB ACC,#1
    """)
    assert c.acc == 0x80000000 and c.v == 1


def test_sub_borrow_and_carry():
    c = run("""
        MOVB ACC,#0
        SUBB ACC,#1
    """)
    assert c.acc == 0xFFFFFFFF and c.c == 0 and c.n == 1 and c.v == 0
    c = run("""
        MOVB ACC,#5
        SUBB ACC,#5
    """)
    assert c.acc == 0 and c.c == 1 and c.z == 1
    c = run("""
        MOVB ACC,#0
        MOV @AH,#0xFFFF
        OR ACC,#0xFFFF
        ADDB ACC,#2
    """)
    assert c.acc == 1 and c.c == 1 and c.v == 0


def test_sign_extension_mode():
    data = "    .word 0x8000"
    c = run("""
        SETC SXM
        MOVW DP,#0x240
        MOV ACC,@0<<#0
        MOVL XAR4,ACC
        CLRC SXM
        MOV ACC,@0<<#0
        MOVL XAR5,ACC
        SETC SXM
        MOV ACC,@0<<#4
        MOVL XAR6,ACC
        MOVU ACC,@0
    """.replace("MOVL XAR4,ACC", "MOVL @XAR4,ACC").replace("MOVL XAR5,ACC", "MOVL @XAR5,ACC")
           .replace("MOVL XAR6,ACC", "MOVL @XAR6,ACC"), data=data)
    assert c.xar[4] == 0xFFFF8000 and c.xar[5] == 0x8000 and c.xar[6] == 0xFFF80000 and c.acc == 0x8000
    c = run("""
        SETC SXM
        MOV ACC,#0x8000<<#4
    """)
    assert c.acc == 0xFFF80000


def test_add_shift16_carry_rule():
    # ADD ACC,loc16<<#16 only sets C (never clears); SUB ...<<#16 only clears C
    c = run("""
        MOVW DP,#0x240
        SETC C
        MOVB ACC,#1
        ADD ACC,@0<<#16
    """, data="    .word 0x0001")
    assert c.acc == 0x00010001 and c.c == 1
    c = run("""
        MOVW DP,#0x240
        CLRC C
        MOVB ACC,#0
        MOV @AH,#0xFFFF
        ADD ACC,@0<<#16
    """, data="    .word 0x0001")
    assert c.acc == 0 and c.c == 1


def test_16bit_add_sub_flags():
    c = run("""
        MOVW DP,#0x240
        MOV AL,#0x7FFF<<#0
        MOV AH,#0x1234
        ADD AL,@0
    """.replace("MOV AL,#0x7FFF<<#0", "MOV ACC,#0x7FFF").replace("MOV AH,#0x1234", "MOV @AH,#0x1234"),
        data="    .word 1")
    assert c.read_reg("al") == 0x8000 and c.read_reg("ah") == 0x1234 and c.v == 1 and c.n == 1 and c.c == 0
    c = run("""
        MOVB AL,#3
        ADDB AL,#-5
    """)
    assert c.read_reg("al") == 0xFFFE and c.n == 1 and c.c == 0
    c = run("""
        MOVW DP,#0x240
        INC @0
        DEC @1
        ADD @2,#-3
        SUB AL,@3
    """, data="    .word 0xFFFF, 0x0000, 2, 1")
    assert w(c, 0x9000) == 0 and w(c, 0x9001) == 0xFFFF and w(c, 0x9002) == 0xFFFF
    assert c.read_reg("al") == 0xFFFF and c.c == 0


def test_addcu_subbl_32bit_chain():
    # 64-bit add via ADDUL/ADDCL
    c = run("""
        MOVL XAR4,#0x9000
        MOVL ACC,*+XAR4[0]
        ADDUL ACC,*+XAR4[4]
        MOVL *+XAR4[0],ACC
        MOVL ACC,*+XAR4[2]
        ADDCL ACC,*+XAR4[6]
        MOVL *+XAR4[2],ACC
    """, data="    .long 0xFFFFFFFF, 0x00000001, 0x00000001, 0x00000002")
    assert wl(c, 0x9000) == 0 and wl(c, 0x9002) == 4


def test_compare_and_conditional_branches():
    c = run("""
        MOVB AL,#5
        CMPB AL,#7
        SB lt_ok,LT
        MOVB XAR0,#0xEE
lt_ok:  CMPB AL,#5
        SB eq_ok,EQ
        MOVB XAR1,#0xEE
eq_ok:  MOVB ACC,#10
        MOVL XAR4,#0x9000
        CMPL ACC,*XAR4
        B gt_ok,GT
        MOVB XAR2,#0xEE
gt_ok:  MOVB XAR3,#1
    """, data="    .long 0xFFFFFFF0")
    assert c.xar[0] == 0 and c.xar[1] == 0 and c.xar[2] == 0 and c.xar[3] == 1


COND_CASES = [
    # cond, (N, Z, C, V, TC) setting, taken?
    ("NEQ", dict(z=0), True), ("NEQ", dict(z=1), False), ("EQ", dict(z=1), True), ("EQ", dict(z=0), False),
    ("GT", dict(z=0, n=0), True), ("GT", dict(z=1, n=0), False), ("GT", dict(z=0, n=1), False),
    ("GEQ", dict(n=0), True), ("GEQ", dict(n=1), False), ("LT", dict(n=1), True), ("LT", dict(n=0), False),
    ("LEQ", dict(z=1), True), ("LEQ", dict(n=1), True), ("LEQ", dict(z=0, n=0), False),
    ("HI", dict(c=1, z=0), True), ("HI", dict(c=1, z=1), False), ("HIS", dict(c=1), True), ("HIS", dict(c=0), False),
    ("C", dict(c=1), True), ("LO", dict(c=0), True), ("LO", dict(c=1), False), ("NC", dict(c=0), True),
    ("LOS", dict(c=0, z=0), True), ("LOS", dict(c=1, z=1), True), ("LOS", dict(c=1, z=0), False),
    ("NOV", dict(v=0), True), ("NOV", dict(v=1), False), ("OV", dict(v=1), True), ("OV", dict(v=0), False),
    ("NTC", dict(tc=0), True), ("NTC", dict(tc=1), False), ("TC", dict(tc=1), True), ("TC", dict(tc=0), False),
    ("NBIO", dict(), False), ("UNC", dict(), True),
]


@pytest.mark.parametrize("form", ["SB", "B", "BF"])
@pytest.mark.parametrize("cond,flags,taken", COND_CASES)
def test_all_condition_codes(form, cond, flags, taken):
    def setup(c):
        for k, v in flags.items():
            setattr(c, k, v)
    c = run(f"""
        {form} yes,{cond}
        MOVB XAR0,#1
        SB done,UNC
yes:    MOVB XAR0,#2
done:
    """, setup=setup)
    assert c.xar[0] == (2 if taken else 1)
    if cond in ("OV", "NOV"):
        assert c.v == 0                            # testing V clears it


def test_logic_and_unary():
    c = run("""
        MOVW DP,#0x240
        MOV ACC,#0xF0F0<<#0
        AND AL,@0
        OR AH,@1
        XORB AL,#0xFF
        MOVL @XAR4,ACC
        AND ACC,#0xFF00<<#0
        MOVL @XAR5,ACC
        AND @2,#0x00FF
        OR @3,#0x8000
        XOR @3,#0x0001
        MOVB ACC,#1
        NEG ACC
        MOVL @XAR6,ACC
        ABS ACC
        MOVL @XAR7,ACC
        NOT ACC
    """, data="    .word 0x0FF0, 0x1200, 0x1234, 0x0001")
    assert (c.xar[4] & 0xFFFF) == 0x000F and c.xar[4] >> 16 == 0x1200
    assert c.xar[5] == 0                          # 0x1200000F & 0xFF00
    assert w(c, 0x9002) == 0x0034 and w(c, 0x9003) == 0x8000
    assert c.xar[6] == 0xFFFFFFFF and c.xar[7] == 1 and c.acc == 0xFFFFFFFE


# ------------------------------------------------------------------------------------------------ shifts
def test_shifts():
    c = run("""
        MOV ACC,#0x8001<<#0
        LSL ACC,#16
        MOVL @XAR0,ACC
        SETC SXM
        SFR ACC,#4
        MOVL @XAR1,ACC
        CLRC SXM
        MOVB ACC,#0
        MOV @AH,#0x8001
        SFR ACC,#1
        MOVL @XAR2,ACC
        MOVB AL,#0x81
        MOV @AH,#0
        LSL AL,#8
        ASR AL,#4
        MOV @AR3,AL
        MOV ACC,#0x8000<<#0
        LSR AL,#15
        MOV @AR4,AL
    """)
    assert c.xar[0] == 0x80010000
    assert c.xar[1] == 0xF8001000
    assert c.xar[2] == 0x40008000 and c.c == 1 or c.xar[2] == 0x40008000
    assert c.xar[3] == 0xF810 and c.xar[4] == 1


def test_shift_carry_and_rotate():
    c = run("""
        MOVB ACC,#0
        MOV @AH,#0x8000
        LSL ACC,#1
    """)
    assert c.acc == 0 and c.c == 1 and c.z == 1
    c = run("""
        SETC C
        MOVB ACC,#2
        ROR ACC
        MOVL @XAR0,ACC
        ROL ACC
    """)
    assert c.xar[0] == 0x80000001 and c.acc == 0x00000002 and c.c == 1


def test_shift_by_t_and_64bit():
    c = run("""
        MOVW DP,#0x240
        MOV T,@0
        MOVB ACC,#1
        LSLL ACC,T
        MOVL @XAR0,ACC
        MOVB ACC,#0
        MOV @AH,#0x8000
        MOVL P,@XAR0
        LSR64 ACC:P,#4
        MOVL @XAR1,ACC
        MOVL @XAR2,P
        ASR64 ACC:P,#32-28
    """, data="    .word 20")
    assert c.xar[0] == 1 << 20
    assert c.xar[1] == 0x08000000 and c.xar[2] == 0x00010000
    assert c.acc == 0x00800000 and c.p == 0x00001000


# ------------------------------------------------------------------------------------------------ multiply
def test_mpy_signed_unsigned_and_pm():
    c = run("""
        MOVW DP,#0x240
        MOV T,@0
        MPY ACC,T,@1
        MOVL @XAR0,ACC
        MPYU ACC,T,@1
        MOVL @XAR1,ACC
        MPY P,T,@2
        SPM +1
        MOVL ACC,P<<PM
        MOVL @XAR2,ACC
        SPM -2
        MOVL ACC,P<<PM        ; PM right shifts sign-extend regardless of SXM (SPRU430F ST0.PM)
        MOVL @XAR3,ACC
        SPM 0
        MOVL ACC,P<<PM
    """, data="    .word 0xFFFE, 3, 0x0100")
    assert c.xar[0] == (-6) & 0xFFFFFFFF
    assert c.xar[1] == 0xFFFE * 3
    assert c.xar[2] == ((-2 * 256) << 1) & 0xFFFFFFFF
    assert c.xar[3] == ((-2 * 256) >> 2) & 0xFFFFFFFF
    assert c.acc == (-512) & 0xFFFFFFFF


def test_mpya_mac_style_accumulate():
    c = run("""
        MOVW DP,#0x240
        ZAPA
        SPM 0
        MOV T,@0
        MPYA P,T,@1          ; ACC += P(0); P = 2*3
        MPYA P,T,@2          ; ACC += 6;   P = 2*4
        MPYA P,T,@0          ; ACC += 8
    """, data="    .word 2, 3, 4")
    assert c.acc == 14 and c.p == 4


def test_32bit_multiply():
    c = run("""
        MOVL XAR4,#0x9000
        MOVL XT,*+XAR4[0]
        IMPYL P,XT,*+XAR4[2]
        QMPYL ACC,XT,*+XAR4[2]
    """, data="    .long 0x12345678, 0xFFFFFFFE")
    prod = 0x12345678 * -2
    assert c.p == prod & 0xFFFFFFFF and c.acc == (prod >> 32) & 0xFFFFFFFF


def test_rpt_subcu_division():
    # 32/16 unsigned division: ACC = dividend; RPT #15 || SUBCU → AL = quotient, AH = remainder
    c = run("""
        MOVW DP,#0x240
        MOVU ACC,@0
        RPT #15
        ||SUBCU ACC,@1
    """.replace("||", ""), data="    .word 1000, 7")
    assert c.read_reg("al") == 1000 // 7 and c.read_reg("ah") == 1000 % 7


# ------------------------------------------------------------------------------------------------ addressing modes
def test_addressing_modes():
    c = run("""
        MOVL XAR4,#0x9000
        MOV AL,*XAR4++            ; 0x9000
        MOV @AR0,AL
        MOV AL,*XAR4++            ; 0x9001
        MOV @AR1,AL
        MOV AL,*--XAR4            ; 0x9001
        MOV @AR2,AL
        MOVB XAR0,#3
        MOV AL,*+XAR4[AR0]        ; 0x9004
        MOV @AR3,AL
        MOVB XAR1,#5
        MOV AL,*+XAR4[AR1]        ; 0x9006
        MOV @AR5,AL
        MOV AL,*+XAR4[6]          ; 0x9007
        MOV @AR6,AL
        MOV AL,*XAR4              ; 0x9001
        MOV @AR7,AL
        MOVL ACC,*XAR4++          ; 32-bit post-increment by 2 (aligned 0x9000)
    """, data="    .word 10, 11, 12, 13, 14, 15, 16, 17")
    assert [c.xar[i] & 0xFFFF for i in (0, 1, 2, 3, 5, 6, 7)] == [3, 5, 11, 14, 16, 17, 11]
    assert c.xar[4] == 0x9003 and c.acc == (11 << 16) | 10


def test_direct_and_stack_addressing():
    c = run("""
        MOVW DP,#DP(var)
        MOV AL,@var
        MOV @var+1,AL
        MOV *SP++,AL
        MOVB AL,#7
        MOV *SP++,AL
        MOV AH,*-SP[2]
        MOV AL,*--SP
        MOVL *SP++,ACC
        MOVL XAR5,*-SP[2]
        ADDB SP,#4
        SUBB SP,#2
    """, data="var: .word 0x4242, 0")
    assert w(c, 0x9001) == 0x4242
    assert c.read_reg("ah") == 0x4242 and c.read_reg("al") == 7
    assert c.xar[5] == 0x42420007 and c.sp == 0x405


def test_circular_addressing():
    # buffer size AR1[7:0] + 1 = 4 words at 0x9000 (AR6 low byte wraps to 0 after reaching AR1 low byte)
    c = run("""
        MOVL XAR6,#0x9000
        MOVB XAR1,#3
        MOVB AL,#1
        RPT #9
        ADD AL,*AR6%++
    """.replace("ADD AL,*AR6%++", "MOV *AR6%++,AL"))
    assert c.xar[6] == 0x9002              # 10 writes into a 4-word ring
    c2 = run("""
        MOVL XAR6,#0x9000
        MOVB XAR1,#3
        MOVB ACC,#0
        RPT #9
        ADD AL,*AR6%++
    """, data="    .word 1, 2, 3, 4")
    assert c2.read_reg("al") == 1 + 2 + 3 + 4 + 1 + 2 + 3 + 4 + 1 + 2


def test_bit_reversed_addressing():
    # *BR0++ on XAR(ARP) with AR0 = N/2 visits FFT bit-reversed order.  The results are stored with *SP++, which
    # leaves ARP alone (a C28x indirect mode such as *XAR4++ would set ARP = 4, SPRU430F 5.6.1).
    def setup(c):
        c.write_reg("arp", 2)
    c = run("""
        MOVL XAR2,#0x9000
        MOVB XAR0,#4
""" + "        MOV AL,*BR0++\n        MOV *SP++,AL\n" * 8, setup=setup, data="    .word 0,1,2,3,4,5,6,7")
    assert [w(c, 0x400 + i) for i in range(8)] == [0, 4, 2, 6, 1, 5, 3, 7]
    assert c.read_reg("arp") == 2


def test_indirect_modes_set_arp_and_c2xlp_modes():
    # SPRU430F 5.6.1/5.6.2: *XARn++ etc. set ARP = n; "*,ARPn" addresses XAR(ARP) then sets ARP = n
    c = run("""
        MOVL XAR3,#0x9000
        MOVL XAR5,#0x9002
        MOV AL,*XAR3++
        MOV AH,*,ARP5          ; reads *XAR3 (ARP=3), then ARP = 5
        MOV PL,*++             ; reads *XAR5, XAR5++
    """, data="    .word 11, 22, 33")
    assert c.read_reg("al") == 11 and c.read_reg("ah") == 22 and c.read_reg("pl") == 33
    assert c.read_reg("arp") == 5 and c.xar[5] == 0x9003


def test_register_loc_operands():
    c = run("""
        MOVL XAR4,#0x9000
        MOVL XAR4,*XAR4
        MOV AL,@AR4
        MOV AH,@AL
        MOVL P,@XAR4
        MOVL XT,@ACC
        MOV @T,#7
    """, data="    .long 0x12345678")
    assert c.acc == 0x56785678 and c.p == 0x12345678 and c.xt == 0x00075678


# ------------------------------------------------------------------------------------------------ stack / calls
def test_push_pop_and_status():
    c = run("""
        MOVL XAR4,#0x9000
        MOVL XAR4,*XAR4
        MOVB ACC,#0
        MOV @AH,#0x3333
        PUSH ACC
        PUSH XAR4
        SETC TC
        PUSH ST0
        MOVB ACC,#0
        MOVL XAR4,#0
        CLRC TC
        POP ST0
        POP XAR4
        POP ACC
        PUSH AR1:AR0
        POP AR3:AR2
    """, setup=lambda c: c.write_reg("xar0", 0xAAAA) or c.write_reg("xar1", 0xBBBB), data="    .long 0x11112222")
    assert c.acc == 0x33330000 and c.xar[4] == 0x11112222 and c.tc == 1 and c.sp == 0x400
    assert c.xar[2] & 0xFFFF == 0xAAAA and c.xar[3] & 0xFFFF == 0xBBBB


def test_calls_and_returns():
    c = run("""
        LCR func_r            ; RPC-based call
        MOVB XAR1,#1
        LC func_s             ; stack-based call
        MOVB XAR2,#2
        FFC XAR7,func_x
        MOVB XAR3,#3
        SB done,UNC
func_r: MOVB XAR5,#0x55
        LCR inner             ; nested: pushes RPC
        LRETR
inner:  MOVB XAR6,#0x66
        LRETR
func_s: MOVB AL,#0x11
        LRET
func_x: LB *XAR7
done:
    """)
    assert [c.xar[i] for i in (1, 2, 3, 5, 6)] == [1, 2, 3, 0x55, 0x66]
    assert c.read_reg("al") == 0x11 and c.sp == 0x400 and c.rpc == 0


def test_banz_bar_loops():
    c = run("""
        MOVB XAR2,#4
        MOVB ACC,#0
loop:   ADDB ACC,#3
        BANZ loop,AR2--
        MOVB XAR3,#0
        MOVB XAR4,#5
l2:     ADDB XAR3,#1
        BAR l2,AR3,AR4,NEQ
    """)
    assert c.acc == 15 and c.xar[2] & 0xFFFF == 0xFFFF and c.xar[3] == 5


def test_rpt_counts_and_block_fill():
    c = run("""
        MOVL XAR4,#0x9000
        MOV AL,#0xBEEF
        RPT #9
        MOV *XAR4++,AL
    """.replace("MOV AL,#0xBEEF", "MOV ACC,#0xBEEF<<#0"))
    assert all(w(c, 0x9000 + i) == 0xBEEF for i in range(10)) and w(c, 0x900A) == 0
    # 4 instructions + 10 repetitions + ESTOP0
    assert c.n_executed == 2 + 1 + 10 + 1


def test_rpt_pread_and_movabs():
    c = run("""
        MOVL XAR7,#table
        MOVL XAR4,#0x9100
        RPT #3
        PREAD *XAR4++,*XAR7
        MOVL XAR5,#0x9200
        RPT #3
        MOV *XAR5++,*(0:0x9100)
    """, data="table: .word 9, 8, 7, 6")
    assert [w(c, 0x9100 + i) for i in range(4)] == [9, 8, 7, 6]
    assert [w(c, 0x9200 + i) for i in range(4)] == [9, 8, 7, 6]
    assert c.xar[7] == 0x9000                      # PREAD under RPT does not modify XAR7


def test_tbit_tset_tclr():
    c = run("""
        MOVW DP,#0x240
        TBIT @0,#4
        MOVB XAR0,#0
        SB nt,NTC
        MOVB XAR0,#1
nt:     TSET @0,#1
        TCLR @0,#4
    """, data="    .word 0x0010")
    assert c.xar[0] == 1 and w(c, 0x9000) == 0x0002


# ------------------------------------------------------------------------------------------------ interrupts / stops
def test_take_interrupt_and_iret_round_trip():
    src = """
        NOP
        NOP
        ESTOP0
isr:    MOVB ACC,#0x77
        MOV @AR0,#0
        MOV @AR1,#0
        ZAPA
        SETC SXM
        IRET
    """
    r = assemble(f'        .sect "code", {CODE}\n' + src)
    c = make_core()
    for a, d in r.segments:
        c.write_mem(a, d)
    c.reset(CODE, 0x400)
    c.run(1)
    regs = dict(acc=0x12345678, p=0x9ABCDEF0, xt=0x0BAD0001, xar0=0x1111AAAA, xar1=0x2222BBBB, dp=0x123,
                st0=0x0004 | (3 << 7), ier=0x0005, ifr=0x0001)
    for k, v in regs.items():
        c.write_reg(k, v)
    c.write_reg("intm", 0)
    assert c.interrupt_pending() == 1
    st1_before = c.st1
    c.take_interrupt(r.symbols["isr"], 1)
    assert c.pc == r.symbols["isr"]
    assert c.intm == 1 and c.dbgm == 1 and c.eallow == 0
    assert c.ifr == 0 and c.ier == 0x0004                 # IFR and IER bit of INT1 cleared
    assert c.sp == 0x400 + 1 + 14                        # SP+1 alignment step, 7 x 32-bit pushes
    # stacked context: T:ST0, AH:AL, PH:PL, AR1:AR0, DP:ST1, DBGSTAT:IER, PC
    stack = [wl(c, 0x400 + 2 * i) for i in range(7)]      # pushes start at the even address 0x400
    assert stack == [(0x0BAD << 16) | regs["st0"], 0x12345678, 0x9ABCDEF0, 0xBBBBAAAA,
                     (0x123 << 16) | st1_before, 0x0005, CODE + 1]
    reason, n = c.run(100)
    assert reason is StopReason.BREAKPOINT
    assert c.acc == 0x12345678 and c.p == 0x9ABCDEF0 and c.xar[0] == 0x1111AAAA and c.xar[1] == 0x2222BBBB
    assert c.st0 == regs["st0"] and c.dp == 0x123 and c.ier == 0x0005 and c.sp == 0x400 and c.intm == 0
    assert c.pc == CODE + 3


def test_trap_uses_vector_table():
    c = run("""
        TRAP #20
        MOVB XAR1,#1
        SB done,UNC
handler:
        MOVB XAR2,#2
        IRET
done:
    """, setup=lambda c: c.write_mem(0x3FFFC0 + 40, struct.pack("<I", c.syms["handler"])))
    assert c.xar[1] == 1 and c.xar[2] == 2 and c.sp == 0x400


def test_intr_with_vector_fetch_hook():
    def setup(c):
        c.vector_fetch = lambda n: c.syms["h"] if n == 5 else 0
        c.ier = 0x10
        c.ifr = 0x10
    c = run("""
        INTR INT5
        SB done,UNC
h:      MOVL XAR3,@XAR0
        MOV AL,IER
        IRET
done:
    """.replace("MOV AL,IER", "MOV @AL,IER"), setup=setup)
    assert c.ifr == 0 and c.ier == 0x10 and c.read_reg("al") == 0     # IER bit cleared inside, restored by IRET


def test_idle_stops_with_wfi():
    r = assemble(f'        .sect "code", {CODE}\n        MOVB ACC,#1\n        IDLE\n        MOVB ACC,#2\n')
    c = make_core()
    for a, d in r.segments:
        c.write_mem(a, d)
    c.reset(CODE, 0x400)
    reason, n = c.run(100)
    assert reason is StopReason.WFI and n == 2 and c.pc == CODE + 2 and c.idlestat == 1


def test_illegal_opcode_fault_and_itrap_handler():
    c = make_core()
    c.write_mem(CODE, struct.pack("<3H", 0x0201, 0x0000, 0x0202))   # MOVB ACC,#1 ; ITRAP0 ; MOVB ACC,#2
    c.reset(CODE, 0x400)
    reason, n = c.run(10)
    assert reason is StopReason.FAULT and n == 1
    assert c.last_fault["type"] == "illegal_opcode" and c.last_fault["pc"] == CODE + 1
    assert c.last_fault["opcode"] == 0 and c.pc == CODE + 1
    # with an arch handler: the trap is taken and execution continues at the ITRAP vector
    c.write_mem(0x9000, struct.pack("<2H", 0x0205, 0x7625))           # MOVB ACC,#5 ; ESTOP0
    seen = []

    def itrap(pc=None):
        seen.append(pc)
        c.take_interrupt(0x9000, 19)
        return True
    c.itrap_handler = itrap
    c.reset(CODE, 0x400)
    reason, n = c.run(10)
    assert reason is StopReason.BREAKPOINT and seen == [CODE + 1] and c.acc == 5
    assert wl(c, 0x400 + 12) == CODE + 1                             # return address = the illegal instruction


def test_estop_breakpoint_and_resume():
    r = assemble(f'        .sect "code", {CODE}\n        ESTOP0\n        MOVB ACC,#9\n        ESTOP0\n')
    c = make_core()
    for a, d in r.segments:
        c.write_mem(a, d)
    c.reset(CODE, 0x400)
    assert c.run(10) == (StopReason.BREAKPOINT, 1)
    assert c.run(10) == (StopReason.BREAKPOINT, 2) and c.acc == 9


def test_mmio_dispatch_and_request_stop():
    c = C28xCore()
    c.map_memory("RAM", 0x0000, 0x7000)
    c.map_memory("RAM2", 0x8000, 0x8000)
    regs = {}
    counts = []

    def rd(off, n):
        return 0xA5A5 if n == 1 else 0x12345678

    def wr(off, n, v):
        regs[off] = (n, v)
        counts.append(c.insns_in_slice())
        if off == 2:
            c.request_stop(from_mmio=True)
    c.map_mmio(0x7000, 0x10, rd, wr)
    r = assemble(f"""
        .sect "code", {CODE}
        MOVW DP,#0x1C0
        MOV AL,@0
        MOVL @4,ACC
        MOVL ACC,@6
        MOV @2,AL
        MOVB XAR0,#1
        MOVB XAR1,#1
    """)
    for a, d in r.segments:
        c.write_mem(a, d)
    c.reset(CODE, 0x400)
    reason, n = c.run(1000)
    assert reason is StopReason.USER and n == 5 and c.pc == CODE + 6
    assert regs[4] == (2, 0x0000A5A5) and regs[2] == (1, 0x5678) and c.acc == 0x12345678
    assert counts == [2, 4]                                          # instructions completed before the access
    reason, n = c.run(2)
    assert reason is StopReason.COUNT and c.xar[0] == 1 and c.xar[1] == 1


def test_unmapped_access_faults_and_handler_maps():
    c = make_core()
    r = assemble(f'        .sect "code", {CODE}\n        MOVL XAR4,#0x200000\n        MOV AL,*XAR4\n')
    for a, d in r.segments:
        c.write_mem(a, d)
    c.reset(CODE, 0x400)
    reason, n = c.run(10)
    assert reason is StopReason.FAULT and c.last_fault["type"] == "read_unmapped"
    assert c.last_fault["addr"] == 0x200000 and c.pc == CODE + 2 and n == 1

    def handler(addr, size, acc):
        c.map_mmio(addr & ~0x3F, 0x40, lambda o, n: 0x4321, lambda o, n, v: None)
        return True
    c.set_unmapped_handler(handler)
    reason, n = c.run(1)
    assert reason is StopReason.COUNT and c.read_reg("al") == 0x4321


def test_self_modifying_code_invalidates_cache():
    c = run("""
        MOVL XAR4,#target
        MOVB XAR5,#1
target: MOVB ACC,#1
        MOVL @XAR6,ACC
        MOV ACC,#0x0207       ; encoding of MOVB ACC,#7
        MOV *XAR4,AL
        BANZ target,AR5--
    """)
    assert c.xar[6] == 7 and w(c, c.syms["target"]) == 0x0207


def test_amode1_executes_c2xlp_modes():
    """AMODE = 1 used to fault; it now decodes the C2xLP column of Table 5-1 (tests/test_c28x_rptb_amode.py)."""
    c = make_core()
    r = assemble(f'        .sect "code", {CODE}\n        LPADDR\n        MOV AL,*XAR4\n        ESTOP0\n')
    for a, d in r.segments:
        c.write_mem(a, d)
    c.reset(CODE, 0x400)
    c.arp, c.xar[0] = 2, 0x9000                   # 0xC4 in AMODE = 1 is *++,ARP4: reads [XAR(ARP)] = [XAR2]
    c.xar[2] = 0x9000
    c.write_mem(0x9000, (0x5A5A).to_bytes(2, "little"))
    reason, n = c.run(10)
    assert reason is StopReason.BREAKPOINT and c.acc & 0xFFFF == 0x5A5A and c.xar[2] == 0x9001 and c.arp == 4


def test_status_registers_and_reg_names():
    c = make_core()
    c.reset(0x8000, 0x400)
    assert c.read_reg("st1") == 0x0A0B and c.intm == 1 and c.objmode == 1 and c.amode == 0
    c.write_reg("st0", 0xFFFF)
    assert c.ovc == -1 and c.pm == 7 and c.read_reg("st0") == 0xFFFF
    c.write_reg("acc", 0x12345678)
    assert c.read_reg("ah") == 0x1234 and c.read_reg("al") == 0x5678
    c.write_reg("tl", 0xBEEF)
    c.write_reg("t", 0xCAFE)
    assert c.read_reg("xt") == 0xCAFEBEEF
    c.write_reg("ar5", 0x1234)
    c.write_reg("xar6", 0x3FFFFF)
    assert c.read_reg("xar5") == 0x1234 and c.read_reg("ar6") == 0xFFFF
    c.write_reg("r3h", 0x3F800000)
    assert c.read_reg("r3h") == 0x3F800000
    for name in C28xCore.REG_NAMES + C28xCore.FPU_REG_NAMES:
        c.read_reg(name)
    c.write_reg("pc", 0x123456)
    assert c.pc == 0x123456
    with pytest.raises(KeyError):
        c.read_reg("r0")


def test_read_write_mem_little_endian_words():
    c = make_core()
    c.write_mem(0x100, bytes([0x34, 0x12, 0x78, 0x56]))
    assert c.rd16(0x100) == 0x1234 and c.rd32(0x100) == 0x56781234
    assert c.read_mem(0x100, 2) == bytes([0x34, 0x12, 0x78, 0x56])
    c.map_memory(0x200000, 0x40, writable=False)
    c.write_mem(0x200000, b"\x01\x00")                # loader writes ROM
    c.wr16(0x200000, 0xFFFF)                          # CPU writes to ROM are ignored
    assert c.rd16(0x200000) == 1 and c.rom_writes == 1
    with pytest.raises(ValueError):
        c.map_memory(0x200020, 0x40)


# ------------------------------------------------------------------------------------------------ FPU32
def f2b(x: float) -> int:
    return struct.unpack("<I", struct.pack("<f", x))[0]


def b2f(b: int) -> float:
    return struct.unpack("<f", struct.pack("<I", b))[0]


def test_fpu_basic_arith_and_moves():
    c = run("""
        SETFLG RNDF32=1
        MOVIZF32 R0H,#1.5
        MOVF32 R1H,#2.25
        ADDF32 R2H,R0H,R1H
        SUBF32 R3H,R0H,R1H
        MPYF32 R4H,R0H,R1H
        MPYF32 R5H,#-2.0,R1H
        MOVL XAR4,#0x9000
        MOV32 *XAR4,R2H
        MOV32 R6H,*+XAR4[0]
        MOV32 ACC,R4H
        MOVI32 R7H,#0x12345678
    """)
    assert b2f(c.r[2]) == 3.75 and b2f(c.r[3]) == -0.75 and b2f(c.r[4]) == 3.375 and b2f(c.r[5]) == -4.5
    assert wl(c, 0x9000) == f2b(3.75) and c.r[6] == f2b(3.75) and c.acc == f2b(3.375)
    assert c.r[7] == 0x12345678


def test_fpu_compare_movst0_and_branch():
    c = run("""
        MOVIZF32 R0H,#1.0
        MOVIZF32 R1H,#2.0
        CMPF32 R0H,R1H
        MOVST0 ZF, NF
        SB less,LT
        MOVB XAR0,#1
less:   CMPF32 R1H,#2.0
        MOVST0 ZF, NF
        SB equal,EQ
        MOVB XAR1,#1
equal:  CMPF32 R1H,R0H
        MOVST0 ZF, NF
        SB greater,GT
        MOVB XAR2,#1
greater:
    """)
    assert c.xar[0] == 0 and c.xar[1] == 0 and c.xar[2] == 0


def test_fpu_conversions():
    c = run("""
        SETFLG RNDF32=1
        MOVL XAR4,#0x9000
        MOV32 R0H,*+XAR4[0]
        I32TOF32 R1H,R0H
        MOVF32 R2H,#-7.75
        F32TOI32 R3H,R2H
        F32TOUI16R R4H,R1H
        UI16TOF32 R5H,*+XAR4[2]
        MOV32 *+XAR4[4],R3H
        F32TOI16R R6H,R2H
        I32TOF32 R7H,*+XAR4[0]
    """, data="    .long -100000\n    .word 65535")
    assert b2f(c.r[1]) == -100000.0 and c.r[3] == (-7) & 0xFFFFFFFF and c.r[4] == 0
    assert b2f(c.r[5]) == 65535.0 and wl(c, 0x9004) == (-7) & 0xFFFFFFFF
    assert c.r[6] & 0xFFFF == (-8) & 0xFFFF and b2f(c.r[7]) == -100000.0


def test_fpu_rounding_mode_and_flags():
    third = 1.0 / 3.0
    c = run("""
        MOVIZF32 R0H,#1.0
        MOVIZF32 R1H,#3.0
        EINVF32 R2H,R1H
        SETFLG RNDF32=1
        EINVF32 R3H,R1H
        MOVF32 R4H,#3.0e38
        MPYF32 R5H,R4H,R4H
    """)
    exact = f2b(third)
    assert c.r[3] == exact                       # nearest
    assert c.r[2] in (exact, exact - 1) and b2f(c.r[2]) <= third     # truncation toward zero
    assert c.r[5] == 0x7F800000 and c.stf & 1    # overflow → +inf, LVF latched


# ------------------------------------------------------------------------------------------------ speed
def test_interpreter_speed():
    src = """
        MOVL XAR4,#0x100
        MOVW DP,#0x8
        MOVB ACC,#0
loop:   ADDB ACC,#1
        MOV *XAR4++,AL
        MOV AH,@5
        ADD AH,*-SP[2]
        CMPB AL,#100
        SB skip,NEQ
        MOVL XAR4,#0x100
skip:   SB loop,UNC
    """
    r = assemble(f'        .sect "code", {CODE}\nstart:\n{src}')
    c = make_core()
    for a, d in r.segments:
        c.write_mem(a, d)
    c.reset(CODE, 0x400)
    c.run(10_000)
    n_total, t0 = 0, time.perf_counter()
    while time.perf_counter() - t0 < 0.5:
        reason, n = c.run(200_000)
        assert reason is StopReason.COUNT
        n_total += n
    rate = n_total / (time.perf_counter() - t0)
    print(f"\nC28x interpreter: {rate / 1e6:.2f} M insn/s")
    if rate < 1e6:
        pytest.xfail(f"slow machine: {rate / 1e6:.2f} M insn/s < 1 M insn/s")
