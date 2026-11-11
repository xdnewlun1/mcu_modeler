"""MCS-51 (8051) instruction set: one table shared by the interpreter (core.py), the assembler and the disassembler
(asm.py).

The table is the Intel MCS-51 instruction set (Intel "MCS-51 Microcontroller Family User's Manual", 1994,
chapter 2 "Instruction Set", and the NXP "80C51 family programmer's guide and instruction set"): 255 opcodes
(0xA5 is reserved), their byte counts and machine-cycle counts (one machine cycle = 12 oscillator periods in the
standard 12-clock mode, 6 in the 6-clock "X2" mode of NXP parts such as the P89CV51RD2).

Operand kinds (`Insn.ops`, in assembler order):
  A C AB DPTR @DPTR @A+DPTR @A+PC   fixed operands
  R0..R7 @R0 @R1                    fixed registers (encoded in the opcode)
  dir        direct address (0x00-0x7F internal RAM, 0x80-0xFF SFR), 1 byte
  #imm       8-bit immediate, 1 byte;  #imm16  16-bit immediate, 2 bytes (high byte first)
  bit        bit address (0x00-0x7F: RAM 0x20-0x2F; 0x80-0xFF: bits of SFRs at multiples of 8), 1 byte
  /bit       complemented bit operand (ANL/ORL C,/bit), 1 byte
  rel        signed 8-bit offset from the address of the next instruction, 1 byte
  a11        AJMP/ACALL target: A10..A8 in opcode bits 7..5, A7..A0 in the next byte (same 2 KB page as the next
             instruction)
  a16        LJMP/LCALL target, 2 bytes (high byte first)
`Insn.enc` gives the operand indices in byte order (only MOV dir,dir (0x85) differs: source byte first).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Insn:
    op: int
    mnem: str
    ops: tuple[str, ...]
    size: int
    cycles: int                      # machine cycles
    enc: tuple[int, ...] = ()        # operand indices in byte order (default: assembler order)

    @property
    def order(self) -> tuple[int, ...]:
        return self.enc or tuple(range(len(self.ops)))


OPERAND_BYTES = {"dir": 1, "#imm": 1, "#imm16": 2, "bit": 1, "/bit": 1, "rel": 1, "a11": 1, "a16": 2}
RESERVED = (0xA5,)


def _build() -> list[Insn | None]:
    t: list[Insn | None] = [None] * 256

    def d(op: int, mnem: str, ops: tuple[str, ...], cyc: int, enc: tuple[int, ...] = ()) -> None:
        size = 1 + sum(OPERAND_BYTES.get(o, 0) for o in ops)
        assert t[op] is None, hex(op)
        t[op] = Insn(op, mnem, ops, size, cyc, enc)

    for page in range(8):
        d(page << 5 | 0x01, "AJMP", ("a11",), 2)
        d(page << 5 | 0x11, "ACALL", ("a11",), 2)
    # column 0-3 (rows 0x0_ .. 0xF_)
    d(0x00, "NOP", (), 1)
    d(0x02, "LJMP", ("a16",), 2)
    d(0x03, "RR", ("A",), 1)
    d(0x10, "JBC", ("bit", "rel"), 2)
    d(0x12, "LCALL", ("a16",), 2)
    d(0x13, "RRC", ("A",), 1)
    d(0x20, "JB", ("bit", "rel"), 2)
    d(0x22, "RET", (), 2)
    d(0x23, "RL", ("A",), 1)
    d(0x30, "JNB", ("bit", "rel"), 2)
    d(0x32, "RETI", (), 2)
    d(0x33, "RLC", ("A",), 1)
    d(0x40, "JC", ("rel",), 2)
    d(0x42, "ORL", ("dir", "A"), 1)
    d(0x43, "ORL", ("dir", "#imm"), 2)
    d(0x50, "JNC", ("rel",), 2)
    d(0x52, "ANL", ("dir", "A"), 1)
    d(0x53, "ANL", ("dir", "#imm"), 2)
    d(0x60, "JZ", ("rel",), 2)
    d(0x62, "XRL", ("dir", "A"), 1)
    d(0x63, "XRL", ("dir", "#imm"), 2)
    d(0x70, "JNZ", ("rel",), 2)
    d(0x72, "ORL", ("C", "bit"), 2)
    d(0x73, "JMP", ("@A+DPTR",), 2)
    d(0x80, "SJMP", ("rel",), 2)
    d(0x82, "ANL", ("C", "bit"), 2)
    d(0x83, "MOVC", ("A", "@A+PC"), 2)
    d(0x90, "MOV", ("DPTR", "#imm16"), 2)
    d(0x92, "MOV", ("bit", "C"), 2)
    d(0x93, "MOVC", ("A", "@A+DPTR"), 2)
    d(0xA0, "ORL", ("C", "/bit"), 2)
    d(0xA2, "MOV", ("C", "bit"), 1)
    d(0xA3, "INC", ("DPTR",), 2)
    d(0xB0, "ANL", ("C", "/bit"), 2)
    d(0xB2, "CPL", ("bit",), 1)
    d(0xB3, "CPL", ("C",), 1)
    d(0xC0, "PUSH", ("dir",), 2)
    d(0xC2, "CLR", ("bit",), 1)
    d(0xC3, "CLR", ("C",), 1)
    d(0xD0, "POP", ("dir",), 2)
    d(0xD2, "SETB", ("bit",), 1)
    d(0xD3, "SETB", ("C",), 1)
    d(0xE0, "MOVX", ("A", "@DPTR"), 2)
    d(0xE2, "MOVX", ("A", "@R0"), 2)
    d(0xE3, "MOVX", ("A", "@R1"), 2)
    d(0xF0, "MOVX", ("@DPTR", "A"), 2)
    d(0xF2, "MOVX", ("@R0", "A"), 2)
    d(0xF3, "MOVX", ("@R1", "A"), 2)
    # columns 4-F
    srcs = ["#imm", "dir", "@R0", "@R1"] + [f"R{i}" for i in range(8)]
    for row, mnem in ((0x2, "ADD"), (0x3, "ADDC"), (0x4, "ORL"), (0x5, "ANL"), (0x6, "XRL"), (0x9, "SUBB")):
        for col, s in enumerate(srcs, 4):
            d(row << 4 | col, mnem, ("A", s), 1)
    for row, mnem in ((0x0, "INC"), (0x1, "DEC")):
        d(row << 4 | 4, mnem, ("A",), 1)
        for col, s in enumerate(srcs[1:], 5):
            d(row << 4 | col, mnem, (s,), 1)
    d(0x74, "MOV", ("A", "#imm"), 1)
    d(0x75, "MOV", ("dir", "#imm"), 2)
    for i, r in enumerate(srcs[2:]):
        d(0x76 + i, "MOV", (r, "#imm"), 1)
    d(0x84, "DIV", ("AB",), 4)
    d(0x85, "MOV", ("dir", "dir"), 2, (1, 0))           # bytes: 85 src dst
    for i, r in enumerate(srcs[2:]):
        d(0x86 + i, "MOV", ("dir", r), 2)
    d(0xA4, "MUL", ("AB",), 4)
    for i, r in enumerate(srcs[2:]):
        d(0xA6 + i, "MOV", (r, "dir"), 2)
    d(0xB4, "CJNE", ("A", "#imm", "rel"), 2)
    d(0xB5, "CJNE", ("A", "dir", "rel"), 2)
    for i, r in enumerate(srcs[2:]):
        d(0xB6 + i, "CJNE", (r, "#imm", "rel"), 2)
    d(0xC4, "SWAP", ("A",), 1)
    for col, s in enumerate(srcs[1:], 5):
        d(0xC0 | col, "XCH", ("A", s), 1)
    d(0xD4, "DA", ("A",), 1)
    d(0xD5, "DJNZ", ("dir", "rel"), 2)
    d(0xD6, "XCHD", ("A", "@R0"), 1)
    d(0xD7, "XCHD", ("A", "@R1"), 1)
    for i in range(8):
        d(0xD8 + i, "DJNZ", (f"R{i}", "rel"), 2)
    d(0xE4, "CLR", ("A",), 1)
    for col, s in enumerate(srcs[1:], 5):
        d(0xE0 | col, "MOV", ("A", s), 1)
    d(0xF4, "CPL", ("A",), 1)
    for col, s in enumerate(srcs[1:], 5):
        d(0xF0 | col, "MOV", (s, "A"), 1)
    return t


OPCODES: list[Insn | None] = _build()
assert sum(1 for e in OPCODES if e is not None) == 255

# read-modify-write instructions: when the destination is a port SFR they read the port LATCH, not the pins
# (Intel MCS-51 user's manual, "Read-Modify-Write Feature"): ANL, ORL, XRL, JBC, CPL, INC, DEC, DJNZ,
# MOV bit,C, CLR bit, SETB bit
RMW_OPS = frozenset({0x42, 0x43, 0x52, 0x53, 0x62, 0x63, 0x10, 0xB2, 0x05, 0x15, 0xD5, 0x92, 0xC2, 0xD2})


def operands(e: Insn, pc: int, raw: bytes | list[int]) -> list[int]:
    """Operand values of the instruction at pc (raw = its bytes, opcode first), in assembler order. Registers and
    fixed operands give 0; `rel`/`a11`/`a16` give the absolute target address."""
    vals = [0] * len(e.ops)
    pos = 1
    npc = (pc + e.size) & 0xFFFF
    for idx in e.order:
        k = e.ops[idx]
        n = OPERAND_BYTES.get(k, 0)
        if not n:
            continue
        if k == "rel":
            r = raw[pos]
            vals[idx] = (npc + (r - 256 if r & 0x80 else r)) & 0xFFFF
        elif k == "a11":
            vals[idx] = (npc & 0xF800) | ((raw[0] >> 5) << 8) | raw[pos]
        elif n == 2:
            vals[idx] = (raw[pos] << 8) | raw[pos + 1]
        else:
            vals[idx] = raw[pos]
        pos += n
    return vals
