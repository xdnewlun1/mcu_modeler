"""MCS-51 assembler / disassembler (chipem/core/mcs51/asm.py, tools/mcs51_asm.py): every opcode round-trips,
directives, expressions, errors, Intel HEX + sidecar symbols through chipem.firmware."""
from __future__ import annotations

import random
import subprocess
import sys
from pathlib import Path

import pytest

from chipem.core.mcs51.asm import AsmError, Assembler, Names, assemble, disasm_one, symbols_from_regs
from chipem.core.mcs51.isa import OPCODES
from chipem.firmware import load_firmware

ROOT = Path(__file__).resolve().parents[1]
REGS = ROOT / "chips" / "p89cv51_regs.yaml"


@pytest.mark.parametrize("names", ["std", "chip"])
def test_every_opcode_round_trips(names):
    sfrs, bits = symbols_from_regs(REGS) if names == "chip" else ({}, {})
    asm = Assembler(sfrs=sfrs, bits=bits)
    nm = Names.standard(sfrs, bits)
    rnd = random.Random(7)
    count = 0
    for e in OPCODES:
        if e is None:
            continue
        for _ in range(12):
            pc = rnd.choice([0x0000, 0x0123, 0x07FC, 0x8000, 0xF7F0, 0xFFF0])
            raw = bytes([e.op] + [rnd.randrange(256) for _ in range(e.size - 1)])
            mem = {(pc + i) & 0xFFFF: b for i, b in enumerate(raw)}
            size, text = disasm_one(lambda a: mem.get(a, 0), pc, nm)
            assert size == e.size
            if pc + size > 0x10000:
                continue
            got = asm.assemble(f" ORG 0x{pc:04X}\n {text}\n").segments[0][1]
            assert got == raw, (hex(e.op), text)
            count += 1
    assert count > 255 * 8


def test_directives_expressions_symbols():
    r = assemble("""
CONST   EQU     10h + 2 * 3
RAMV    DATA    30h
FLAG    BIT     20h.5
X       SET     1
X       SET     X + 1
        ORG     0100h
start:  MOV     A, #CONST
        MOV     RAMV, #HIGH(1234h)
        MOV     RAMV+1, #LOW(1234h)
        SETB    FLAG
        MOV     DPTR, #tbl
        MOV     A, #'A' + X
        MOV     R0, #(0F0h SHR 4) OR 1
        MOV     R1, #NOT 0 AND 0FFh
        LJMP    start
tbl:    DB      1, 2, "hi", 'Z', -1
        DW      1234h, tbl
        DS      3
after:  NOP
        END
        NOP                     ; after END: ignored
""")
    code = r.image_bytes()
    assert code[0x100:0x102] if False else (code[0x100], code[0x101]) == (0x74, 0x16)
    assert r.symbols["FLAG"] == ("BIT", 5) and r.symbols["X"] == ("NUMBER", 2) and r.symbols["RAMV"] == ("DATA", 0x30)
    tbl = r.symbols["tbl"][1]
    assert bytes(code[tbl + i] for i in range(9)) == b"\x01\x02hiZ\xff\x12\x34" + bytes([tbl >> 8])
    assert r.symbols["after"][1] == tbl + 9 + 3 + 1
    assert max(code) == r.symbols["after"][1]


def test_bit_operand_forms():
    r = assemble(" SETB P1.0\n CLR ACC.7\n CPL 20h.0\n SETB 2Fh.7\n JNB TI, $\n MOV C, 0x45\n")
    b = r.segments[0][1]
    assert b[:2] == b"\xd2\x90" and b[2:4] == b"\xc2\xe7" and b[4:6] == b"\xb2\x00" and b[6:8] == b"\xd2\x7f"
    assert b[8:11] == b"\x30\x99\xfd" and b[11:13] == b"\xa2\x45"


@pytest.mark.parametrize("src,msg", [
    (" MOV A, #300", "out of 8-bit range"),
    (" SJMP far\n ORG 200h\nfar: NOP", "out of range"),
    (" AJMP 0800h", "2 KB page"),
    (" MOV R8, A", "undefined symbol"),
    (" FOO A", "unknown instruction"),
    (" SETB 30h.1", "not bit addressable"),
    (" MOV @R2, A", "bad indirect operand"),
    (" MOVX A, @R0+1", "bad indirect operand"),
    (" INC DPTR, A", "wrong number of operands"),
    ("x: NOP\nx: NOP", "redefined"),
])
def test_errors(src, msg):
    with pytest.raises(AsmError) as ei:
        assemble(src)
    assert msg in str(ei.value)


def test_include_and_hex_with_sidecar_map(tmp_path):
    (tmp_path / "defs.inc").write_text("LED BIT P1.2\n")
    src = tmp_path / "prog.asm"
    src.write_text("$INCLUDE (defs.inc)\n ORG 0\n LJMP main\n ORG 30h\nmain: CPL LED\n SJMP main\nbuf DATA 40h\n")
    out = tmp_path / "prog.hex"
    res = subprocess.run([sys.executable, str(ROOT / "tools" / "mcs51_asm.py"), str(src), "-o", str(out),
                          "--list", str(tmp_path / "prog.lst")], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    img = load_firmware(out)
    assert img.fmt == "hex" and img.read(0x30, 2) == b"\xb2\x92"
    assert img.symbols["main"] == 0x30 and img.symbols["buf"] == 0x20040          # DATA symbol: IDATA tag
    dis = subprocess.run([sys.executable, str(ROOT / "tools" / "mcs51_asm.py"), "--disasm", str(out)],
                         capture_output=True, text=True)
    assert "main:" in dis.stdout and "CPL   P1.2" in dis.stdout


def test_sdcc_style_sidecars(tmp_path):
    hexf = tmp_path / "a.hex"
    hexf.write_text(":03000000020030CB\n:00000001FF\n")
    (tmp_path / "a.map").write_text("Area  Addr\n     C:   00000030  _main\n     D:   00000008  _counter\n")
    (tmp_path / "a.cdb").write_text("L:G$isr$0_0$0:45\nL:C$a.c$3$0_0$1:31\n")
    img = load_firmware(hexf)
    assert img.symbols["_main"] == 0x30 and img.symbols["_counter"] == 0x20008 and img.symbols["isr"] == 0x45
