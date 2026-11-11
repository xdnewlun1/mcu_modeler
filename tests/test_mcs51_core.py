"""MCS-51 interpreter (chipem.core.mcs51): every opcode's semantics and cycle count, flags against a bit-level
reference (Intel MCS-51 user's manual definitions), addressing modes, banks, stack, MOVX/MOVC, dual DPTR, the
read-modify-write latch rule, debugger hooks and the jump-to-self skip."""
from __future__ import annotations

import random

import pytest

from chipem.core.base import StopReason
from chipem.core.mcs51 import MCS51Core
from chipem.core.mcs51.asm import assemble
from chipem.core.mcs51.isa import OPCODES

ACC, B, PSW, SP, DPL, DPH = 0xE0, 0xF0, 0xD0, 0x81, 0x82, 0x83

# Machine cycles per opcode, transcribed independently from the Intel MCS-51 instruction set table (rows 0x0_..0xF_,
# columns _0.._F; 0 = reserved A5).  Checks chipem/core/mcs51/isa.py.
CYCLES = [
    1, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,      # 0x
    2, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,      # 1x
    2, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,      # 2x
    2, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,      # 3x
    2, 2, 1, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,      # 4x
    2, 2, 1, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,      # 5x
    2, 2, 1, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,      # 6x
    2, 2, 2, 2, 1, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,      # 7x
    2, 2, 2, 2, 4, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2,      # 8x
    2, 2, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,      # 9x
    2, 2, 1, 2, 4, 0, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2,      # Ax
    2, 2, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2,      # Bx
    2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,      # Cx
    2, 2, 1, 1, 1, 2, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2,      # Dx
    2, 2, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,      # Ex
    2, 2, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,      # Fx
]
# bytes per opcode (same source)
SIZES = [
    1, 2, 3, 1, 1, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    3, 2, 3, 1, 1, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    3, 2, 1, 1, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    3, 2, 1, 1, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    2, 2, 2, 3, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    2, 2, 2, 3, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    2, 2, 2, 3, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    2, 2, 2, 1, 2, 3, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2,
    2, 2, 2, 1, 1, 3, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2,
    3, 2, 2, 1, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    2, 2, 2, 1, 1, 0, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2,
    2, 2, 2, 1, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3,
    2, 2, 2, 1, 1, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    2, 2, 2, 1, 1, 3, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2,
    1, 2, 1, 1, 1, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    1, 2, 1, 1, 1, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
]


def make_core() -> MCS51Core:
    c = MCS51Core()
    c.map_memory(0x00000, 0x10000, writable=False)
    c.map_memory(0x20000, 0x100)
    c.map_memory(0x40000, 0x300)
    c.map_memory(0x10000, 0x10000)                 # external XDATA as plain RAM for these tests
    c.configure(auxr1=0xA2)
    return c


def run(src: str, *, setup=None, n: int | None = None, org: int = 0x100) -> MCS51Core:
    """Assemble `src` at `org` followed by SJMP $; run it (n instructions, else until the SJMP)."""
    r = assemble(f" ORG 0x{org:04X}\n{src}\nhalt: SJMP halt\n")
    c = make_core()
    for a, d in r.segments:
        c.write_mem(a, d)
    c.reset(org)
    if setup:
        setup(c)
    halt = r.symbols["halt"][1]
    steps = 0
    while c.pc != halt and steps < (n if n is not None else 10_000):
        c.run(1)
        steps += 1
    return c


# --------------------------------------------------------------------------------------------- table checks
def test_opcode_table_matches_intel_cycles_and_sizes():
    assert sum(1 for e in OPCODES if e is not None) == 255
    for op in range(256):
        e = OPCODES[op]
        if op == 0xA5:
            assert e is None
            continue
        assert e.cycles == CYCLES[op], f"{op:02X} {e.mnem} cycles"
        assert e.size == SIZES[op], f"{op:02X} {e.mnem} size"


@pytest.mark.parametrize("x2", [False, True])
def test_every_opcode_charges_its_machine_cycles(x2):
    """Each opcode executed once from a neutral state costs cycles x 12 (or x 6 in 6-clock mode)."""
    rnd = random.Random(5)
    for op in range(256):
        e = OPCODES[op]
        if e is None:
            continue
        c = make_core()
        c.set_clocks_per_mc(6 if x2 else 12)
        code = bytes([op] + [rnd.randrange(256) for _ in range(e.size - 1)])
        c.write_mem(0x200, code)
        c.reset(0x200)
        c.sfr[SP] = 0x30
        c.iram[0x2F] = 0x02                       # RET/RETI pop a valid address
        if op == 0x32:
            r, cyc = c.run(1)
            assert r is StopReason.EXC_RETURN
        else:
            r, cyc = c.run(1)
            assert r in (StopReason.COUNT, StopReason.USER), (hex(op), r)
        assert cyc == e.cycles * (6 if x2 else 12), f"{op:02X} {e.mnem}"
        assert c.last_insns == 1


def test_reserved_opcode_faults():
    c = make_core()
    c.write_mem(0x10, b"\xa5")
    c.reset(0x10)
    r, n = c.run(100)
    assert r is StopReason.FAULT and c.last_fault["type"] == "illegal_opcode" and c.last_fault["pc"] == 0x10
    assert n == 0


# --------------------------------------------------------------------------------------------- ALU flags
def ref_add(a, v, c):
    """Bit-level reference: carry chain bit by bit (Intel: CY = carry out of bit 7, AC = out of bit 3, OV = carry
    into bit 7 XOR carry out of bit 7)."""
    carry = c
    res = 0
    carries = []
    for i in range(8):
        s = (a >> i & 1) + (v >> i & 1) + carry
        res |= (s & 1) << i
        carry = s >> 1
        carries.append(carry)
    return res, carries[7], carries[3], carries[6] ^ carries[7]


def ref_subb(a, v, c):
    """Bit-level borrow chain (CY = borrow into bit 7 from outside i.e. out of bit 7, AC = borrow out of bit 3,
    OV = borrow into bit 7 XOR borrow out of bit 7)."""
    borrow = c
    res = 0
    borrows = []
    for i in range(8):
        d = (a >> i & 1) - (v >> i & 1) - borrow
        res |= (d & 1) << i
        borrow = 1 if d < 0 else 0
        borrows.append(borrow)
    return res, borrows[7], borrows[3], borrows[6] ^ borrows[7]


def _alu_core(op_bytes):
    c = make_core()
    c.write_mem(0, bytes(op_bytes))
    return c


@pytest.mark.parametrize("mnem,op,ref", [("ADD", 0x24, "add"), ("ADDC", 0x34, "addc"), ("SUBB", 0x94, "subb")])
def test_add_addc_subb_flags_exhaustive(mnem, op, ref):
    c = _alu_core([op, 0])
    for a in range(256):
        for v in range(0, 256, 3 if a % 7 else 1):
            for cy in (0, 1):
                c.code[1] = v
                c._cache[0] = None
                c.reset(0)
                c.sfr[ACC] = a
                c.sfr[PSW] = (cy << 7) | 0x18 | 0x20          # RS bits and F0 must be kept
                c.run(1)
                if ref == "subb":
                    r, rc, rac, rov = ref_subb(a, v, cy)
                else:
                    r, rc, rac, rov = ref_add(a, v, cy if ref == "addc" else 0)
                psw = c.psw()
                assert c.sfr[ACC] == r, (mnem, a, v, cy)
                assert (psw >> 7, psw >> 6 & 1, psw >> 2 & 1) == (rc, rac, rov), (mnem, a, v, cy, hex(psw))
                assert psw & 0x38 == 0x38
                assert psw & 1 == bin(r).count("1") & 1


def test_da_matches_intel_definition_exhaustive():
    """DA A after ADD of two BCD bytes gives the BCD sum (and the carry); every A/AC/CY combination follows the
    two-step rule of the manual."""
    c = _alu_core([0xD4])
    for a in range(256):
        for ac in (0, 1):
            for cy in (0, 1):
                c.reset(0)
                c.sfr[ACC] = a
                c.sfr[PSW] = (cy << 7) | (ac << 6) | 0x04            # OV set: DA leaves it alone
                c.run(1)
                x, k = a, cy
                if (x & 0xF) > 9 or ac:
                    x += 6
                    if x > 0xFF:
                        k = 1
                    x &= 0xFF
                if (x >> 4) > 9 or k:
                    x += 0x60
                    if x > 0xFF:
                        k = 1
                    x &= 0xFF
                assert c.sfr[ACC] == x and c.psw() >> 7 == k and c.psw() & 0x44 == (ac << 6) | 0x04
    # BCD addition
    for x in range(100):
        for y in range(0, 100, 7):
            bx, by = int(str(x), 16), int(str(y), 16)
            c2 = run("MOV A, #%d\n ADD A, #%d\n DA A" % (bx, by))
            s = x + y
            assert c2.sfr[ACC] == int(str(s % 100), 16) and c2.psw() >> 7 == (s >= 100)


def test_mul_div():
    for a, b in [(0, 0), (7, 9), (0x80, 2), (255, 255), (16, 16), (15, 17)]:
        c = run(f"MOV A, #{a}\n MOV B, #{b}\n SETB C\n MUL AB")
        p = a * b
        assert c.sfr[ACC] == p & 0xFF and c.sfr[B] == p >> 8
        assert c.psw() >> 7 == 0 and (c.psw() >> 2 & 1) == (p > 255)
    for a, b in [(251, 18), (0, 3), (255, 1), (17, 255)]:
        c = run(f"MOV A, #{a}\n MOV B, #{b}\n SETB C\n DIV AB")
        assert (c.sfr[ACC], c.sfr[B]) == divmod(a, b) and c.psw() & 0x84 == 0
    c = run("MOV A, #42\n MOV B, #0\n SETB C\n DIV AB")
    assert c.psw() & 0x84 == 0x04 and c.sfr[ACC] == 42          # divide by zero: OV = 1, CY = 0


def test_parity_tracks_acc_and_cannot_be_written():
    c = run("MOV A, #07h\n MOV PSW, #0FFh\n MOV R0, PSW")
    assert c.iram[0x18] == 0xFF                                  # bank 3 (RS=11): R0 at 18h; P = parity(07h) = 1
    c = run("MOV A, #03h\n MOV PSW, #01h\n MOV 30h, PSW")
    assert c.iram[0x30] == 0x00                                  # P written 1 is ignored, parity(03h) = 0
    c = run("MOV A, #01h\n MOV C, P")
    assert c.psw() >> 7 == 1


# --------------------------------------------------------------------------------------------- data transfer
def test_register_banks_and_indirect():
    c = run("""
        MOV R0, #11h
        MOV PSW, #08h       ; bank 1
        MOV R0, #22h
        MOV PSW, #10h       ; bank 2
        MOV R7, #33h
        MOV PSW, #18h
        MOV R1, #40h
        MOV @R1, #5Ah
        MOV A, @R1
        MOV R1, #90h         ; indirect reaches the upper 128 bytes (not SFRs)
        MOV @R1, #77h
    """)
    assert c.iram[0x00] == 0x11 and c.iram[0x08] == 0x22 and c.iram[0x17] == 0x33
    assert c.iram[0x40] == 0x5A and c.sfr[ACC] == 0x5A and c.iram[0x90] == 0x77 and c.sfr[0x90] == 0


def test_direct_vs_sfr_and_mov_dir_dir_order():
    c = run("MOV 30h, #12h\n MOV 31h, 30h\n MOV B, 31h\n MOV 0F0h, #9\n MOV A, B")
    assert c.iram[0x31] == 0x12 and c.sfr[B] == 9 and c.sfr[ACC] == 9
    raw = assemble(" MOV 31h, 30h\n").segments[0][1]
    assert raw == bytes([0x85, 0x30, 0x31])                       # source byte first


def test_stack_push_pop_call_ret():
    c = run("""
        MOV SP, #60h
        MOV A, #0AAh
        PUSH ACC
        MOV A, #0
        POP B
        LCALL sub
        ACALL sub
        SJMP done
sub:    INC 30h
        RET
done:   PUSH SP
        POP 32h
    """)
    assert c.sfr[B] == 0xAA and c.iram[0x30] == 2
    assert c.iram[0x32] == 0x61 and c.sfr[SP] == 0x60             # PUSH SP pushes the incremented SP
    c = run("MOV SP, #40h\n LCALL x\nx: NOP")
    lo, hi = c.iram[0x41], c.iram[0x42]
    assert (hi << 8 | lo) == 0x106 and c.sfr[SP] == 0x42          # PC after the LCALL, pushed low byte first


def test_xch_xchd_swap_rotates():
    c = run("MOV A, #12h\n MOV R2, #34h\n XCH A, R2\n MOV R0, #40h\n MOV @R0, #0ABh\n XCHD A, @R0\n SWAP A")
    assert c.iram[0x02] == 0x12 and c.iram[0x40] == 0xA4 and c.sfr[ACC] == 0xB3
    c = run("MOV A, #81h\n CLR C\n RLC A\n RLC A")
    assert c.sfr[ACC] == 0x05 and c.psw() >> 7 == 0
    c = run("MOV A, #81h\n SETB C\n RRC A")
    assert c.sfr[ACC] == 0xC0 and c.psw() >> 7 == 1
    c = run("MOV A, #81h\n RL A\n MOV 30h, A\n MOV A, #81h\n RR A")
    assert c.iram[0x30] == 0x03 and c.sfr[ACC] == 0xC0


def test_inc_dec_all_forms_no_flags():
    c = run("""
        MOV PSW, #0C4h
        MOV A, #0FFh
        INC A
        MOV 30h, #0
        DEC 30h
        MOV R3, #7Fh
        INC R3
        MOV R0, #40h
        MOV @R0, #1
        DEC @R0
        MOV DPTR, #00FFh
        INC DPTR
        MOV DPTR, #0FFFFh
        INC DPTR
    """)
    assert c.sfr[ACC] == 0 and c.iram[0x30] == 0xFF and c.iram[3] == 0x80 and c.iram[0x40] == 0
    assert c.read_reg("dptr") == 0 and c.psw() & 0xC4 == 0xC4


def test_logic_ops_and_cjne_carry():
    c = run("MOV A, #0F0h\n ANL A, #3Ch\n ORL A, #01h\n XRL A, #0FFh\n MOV 30h, #0F0h\n ANL 30h, A\n ORL 30h, #3\n"
            " XRL 30h, A\n CPL A\n CLR A")
    assert c.sfr[ACC] == 0 and c.iram[0x30] == ((0xF0 & 0xCE) | 3) ^ 0xCE
    for a, v in [(5, 9), (9, 5), (7, 7), (0, 255), (255, 0)]:
        c = run(f"MOV A, #{a}\n CJNE A, #{v}, ne\n MOV 31h, #1\nne: NOP")
        assert c.psw() >> 7 == (a < v) and c.iram[0x31] == (a == v)


def test_branches():
    c = run("""
        MOV A, #0
        JZ z1
        MOV 30h, #1
z1:     JNZ bad
        MOV R7, #5
l:      INC 31h
        DJNZ R7, l
        MOV 32h, #3
l2:     INC 33h
        DJNZ 32h, l2
        SETB C
        JC c1
        SJMP bad
c1:     CLR C
        JNC c2
bad:    MOV 3Fh, #0EEh
c2:     MOV DPTR, #tbl
        MOV A, #2
        JMP @A+DPTR
tbl:    SJMP bad
        SJMP fin
fin:    NOP
    """)
    assert c.iram[0x30] == 0 and c.iram[0x31] == 5 and c.iram[0x33] == 3 and c.iram[0x3F] == 0


def test_ajmp_acall_page_relative():
    r = assemble(" ORG 07FEh\n AJMP 0F00h\n ORG 0F00h\n MOV 30h, #1\n SJMP $\n")
    c = make_core()
    for a, d in r.segments:
        c.write_mem(a, d)
    c.reset(0x7FE)
    c.run(1)
    assert c.pc == 0xF00                                         # next PC 0800h: page 0800-0FFF


# --------------------------------------------------------------------------------------------- bits
def test_bit_addressing_ram_and_sfr():
    c = run("""
        SETB 00h            ; bit 0 = 20h.0
        SETB 7Fh            ; 2Fh.7
        SETB ACC.3
        MOV C, ACC.3
        MOV 30h.0, C        ; not bit addressable -> error in assembler; replaced below
    """.replace("        MOV 30h.0, C        ; not bit addressable -> error in assembler; replaced below\n", "")
              + "        CPL 20h.1\n        CLR 7Fh\n        MOV B.7, C\n        JB B.7, ok\n        SJMP $\nok: ANL C, /B.6\n"
              "        ORL C, 20h.2\n        JBC 20h.0, j\n        SJMP $\nj: NOP")
    assert c.iram[0x20] == 0x02 and c.iram[0x2F] == 0 and c.sfr[ACC] == 0x08 and c.sfr[B] == 0x80
    assert c.psw() >> 7 == 1


def test_psw_bits_set_register_bank():
    c = run("SETB RS0\n SETB RS1\n MOV R0, #5")
    assert c.iram[0x18] == 5


# --------------------------------------------------------------------------------------------- MOVC / MOVX / DPTR
def test_movc_and_movx_spaces_and_dual_dptr():
    c = run("""
AUXR1   DATA 0A2h                   ; chip SFR (not in the standard 8051 set the bare assembler knows)
        MOV DPTR, #tab
        MOV A, #1
        MOVC A, @A+DPTR
        MOV 30h, A
        MOV A, #2
        MOVC A, @A+PC
        SJMP over
        DB 11h, 22h, 33h
over:   MOV 31h, A
        MOV DPTR, #0123h
        MOV A, #5Ah
        MOVX @DPTR, A           ; on-chip XRAM (< 300h)
        MOV DPTR, #0400h
        MOV A, #6Bh
        MOVX @DPTR, A           ; external bus
        MOV P2, #01h
        MOV R0, #23h
        MOVX A, @R0             ; P2:R0 = 0123h
        MOV 32h, A
        INC AUXR1               ; DPS = 1
        MOV DPTR, #0BEEFh
        INC AUXR1               ; bit 2 is a hard 0: DPS back to 0
        MOV 33h, DPL
tab:    DB 0A0h, 0A1h
    """)
    assert c.iram[0x30] == 0xA1 and c.iram[0x31] == 0x11        # A + PC(next insn = SJMP, 2 bytes) -> first DB
    assert c.xram[0x123] == 0x5A and c.xdata[0x400] == 0x6B and c.iram[0x32] == 0x5A
    assert c.iram[0x33] == 0x00 and c.read_reg("dptr1") == 0xBEEF and c.read_reg("dptr0") == 0x0400


def test_extram_sends_movx_to_external_bus():
    def setup(c):
        c.extram = True
    c = run("MOV DPTR, #0010h\n MOV A, #77h\n MOVX @DPTR, A", setup=setup)
    assert c.xram[0x10] == 0 and c.xdata[0x10] == 0x77


def test_rmw_reads_latch_not_pins():
    """ANL/ORL/XRL/INC/DEC/DJNZ/CPL/CLR/SETB/MOV bit,C/JBC on a port SFR read the latch (set_latch_reader); plain
    reads (MOV A,P1) read the pins."""
    c = make_core()
    pins = {"v": 0x0F}                  # pins pulled low externally on the upper nibble
    latch = {"v": 0xFF}
    c.map_mmio(0x30090, 1, lambda off, n: pins["v"], lambda off, n, v: latch.__setitem__("v", v))
    c.set_latch_reader(0x90, lambda: latch["v"])
    r = assemble(" ORG 0\n MOV A, P1\n ANL P1, #0FEh\n MOV 30h, P1\n SETB P1.0\n CPL P1.7\n SJMP $\n")
    for a, d in r.segments:
        c.write_mem(a, d)
    c.reset(0)
    c.run(12 * 9)
    assert c.sfr[ACC] == 0x0F                    # pins
    assert latch["v"] == 0x7F                    # FFh & FEh = FEh (latch, not pins 0Fh), SETB .0 -> FFh, CPL .7 -> 7Fh
    assert c.iram[0x30] == 0x0F


# --------------------------------------------------------------------------------------------- run loop / debugger
def test_cycle_budget_and_spin_skip():
    r = assemble(" ORG 0\n MOV R7, #10\nl: DJNZ R7, l\n SJMP $\n")
    c = make_core()
    for a, d in r.segments:
        c.write_mem(a, d)
    c.reset(0)
    reason, cyc = c.run(10_000)
    # MOV (1) + 10 x DJNZ (2) = 21 machine cycles, then SJMP $ fills the rest of the budget
    assert c.pc == 0x0004 and c.spin_pc == 0x0004 and cyc >= 10_000
    assert reason is StopReason.COUNT


def test_breakpoints_watchpoints_and_resume():
    r = assemble(" ORG 0\nl: INC 30h\n NOP\n SJMP l\n")
    c = make_core()
    for a, d in r.segments:
        c.write_mem(a, d)
    c.reset(0)
    c.set_breakpoints([2])
    reason, _ = c.run(1000)
    assert reason is StopReason.DEBUG and c.pc == 2 and c.stop_info == {"type": "breakpoint", "addr": 2}
    assert c.iram[0x30] == 1
    reason, _ = c.run(1000)                       # resumes over the breakpoint, stops there again next round
    assert reason is StopReason.DEBUG and c.pc == 2 and c.iram[0x30] == 2
    c.set_breakpoints([])
    c.set_watchpoints([(0x20030, 0x20031, "w")])
    reason, _ = c.run(1000)
    assert reason is StopReason.DEBUG and c.stop_info["type"] == "watchpoint" and c.stop_info["addr"] == 0x20030
    assert c.stop_info["pc"] == 0 and c.stop_info["value"] == 3


def test_request_stop_and_registers():
    c = make_core()
    c.write_mem(0, b"\x80\xfe")
    c.reset(0)
    for name, v in [("a", 0x12), ("b", 0x34), ("sp", 0x50), ("dptr", 0xCAFE), ("r5", 0x99), ("pc", 0x1234)]:
        c.write_reg(name, v)
        assert c.read_reg(name) == v
    c.write_reg("cy", 1)
    c.write_reg("rs", 2)
    assert c.read_reg("psw") & 0x98 == 0x90 and c.read_reg("r5") == c.iram[0x15]
    assert c.request_stop() is False                 # not running


def test_native_hook_runs_instead_of_code():
    c = make_core()
    calls = []

    def hook():
        calls.append(c.read_reg("a"))
        c.write_reg("a", 0x42)
        s = c.sfr[SP]
        ret = c.iram[s] << 8 | c.iram[s - 1]
        c.sfr[SP] = s - 2
        return ret
    c.install_native(0xFFF0, hook)
    r = assemble(" ORG 0\n MOV SP, #30h\n MOV A, #7\n LCALL 0FFF0h\n MOV 30h, A\n SJMP $\n")
    for a, d in r.segments:
        c.write_mem(a, d)
    c.reset(0)
    c.run(200)
    assert calls == [7] and c.iram[0x30] == 0x42
