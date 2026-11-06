"""C28x assembler / disassembler (chipem.core.c28x.asm) and the ISA table."""
from __future__ import annotations

import itertools
import json
import subprocess
import sys
from pathlib import Path

import pytest

from chipem.core.c28x.asm import assemble, disasm, disasm_one, encode
from chipem.core.c28x.isa import AsmError, get_isa, loc_fmt, loc_parse

ROOT = Path(__file__).resolve().parents[1]
ISA = get_isa()

SAMPLES = {
    "loc16": [0xDC, 0x05, 0x42, 0x83, 0x8A, 0x92, 0x9B, 0xA3, 0xA9, 0xA8, 0xAD, 0xBD, 0xBE, 0xBF, 0xAE, 0xB3, 0xB8],
    "loc32": [0xDC, 0x06, 0x44, 0x83, 0xA9, 0xAB, 0xAC, 0xA5, 0xBD, 0xBE],
    "reg32": [0xA9, 0xAB, 0xAC, 0xA0, 0xA5, 0xA7],
    "AX": [0, 1], "AX.LSB": [1], "AX.MSB": [0], "ARn": [3], "ARn--": [2], "XARn": [5], "XARn++": [6],
    "XARn--": [1], "*XARn": [4], "ARm": [6], "ARPn": [3], "#u5": [17], "#u7": [100], "#u8": [200], "#u10": [513],
    "#u16": [0x1234], "#s8": [-5], "#s16": [-1234], "#bit": [13], "#sh0_15": [7], "#sh1_15": [9],
    "#sh1_16": [16], "#sh1_8": [3], "COND": [2, 15], "CNDF": [3], "intx": [5], "off8": [0x110],
    "off16": [0x2000], "addr22": [0x3F1234], "#addr22": [0x33FFF6], "pma": [0x4567], "*pma": [0x4321],
    "pa": [0x0100], "abs16": [0x0F12], "pm": [-3], "mode": [0x03], "fflags": [0x0C], "flagset": [(0x81, 0x80)],
    "#f16": [0x3FC0], "rblock": [0x102 + 20], "RaH": [3], "RbH": [5], "RcH": [7], "RdH": [1], "ReH": [2], "RfH": [6],
}


def _kinds(e):
    return [k.name for op in e.operands for k in (op.value_kind, op.shift_kind) if k is not None]


@pytest.mark.parametrize("e", ISA.entries, ids=lambda e: f"{e.idx}:{e.syntax()}")
def test_every_entry_round_trips(e):
    """encode sample operands → disassemble → assemble the text → same words (aliases of more specific entries
    are skipped per sample; every entry needs at least one sample that decodes to itself).  Assembler-only
    aliases (`alias: true`, e.g. ZAP OVC = CLRC OVC) must assemble their own text to the same words."""
    pc = 0x100
    kinds = _kinds(e)
    if e.alias:
        for combo in itertools.islice(itertools.product(*[SAMPLES[k] for k in kinds]), 40):
            ws = encode(e, dict(zip(kinds, combo)), pc)
            text = e.mn + " " + ",".join(
                (op.value_kind.fmt(dict(zip(kinds, combo))[op.value_kind.name]) if op.value_kind else op.value)
                for op in e.operands)
            r = assemble(f" .org {pc}\n {text.strip()}\n")
            assert [w for _, w in sorted(r.words().items())] == ws, text
            n, canon, e2 = disasm_one(ws, pc)
            assert e2 is not None and not e2.alias
        return
    ok = 0
    for combo in itertools.islice(itertools.product(*[SAMPLES[k] for k in kinds]), 40):
        ws = encode(e, dict(zip(kinds, combo)), pc)
        n, text, e2 = disasm_one(ws, pc)
        if e2 is not e:
            continue
        assert n == e.nwords
        r = assemble(f" .org {pc}\n {text}\n")
        ws2 = [w for _, w in sorted(r.words().items())]
        assert disasm_one(ws2, pc)[1] == text, (text, ws, ws2)
        ok += 1
    assert ok, f"no sample decodes back to {e.syntax()}"


def test_table_has_no_ambiguous_overlaps():
    """Two entries may overlap only if one is strictly more specific (an alias such as PUSH ACC = MOVL *SP++,ACC)."""
    for a, b in itertools.combinations([e for e in ISA.entries if not e.alias], 2):
        if a.nwords == b.nwords:
            ma, va, mb, vb = a.mask, a.value, b.mask, b.value
        else:
            ma, va, mb, vb = a.mask1, a.value1, b.mask1, b.value1
        if (va ^ vb) & ma & mb == 0:
            assert (ma & mb) in (ma, mb) and ma != mb, (a.syntax(), b.syntax())


def test_table_coverage():
    mns = set(ISA.by_mn)
    base = """ABS ABSTC ADD ADDB ADDCL ADDCU ADDL ADDU ADDUL ADRK AND ANDB ASP ASR ASR64 ASRL B BANZ BAR BF C27MAP C27OBJ
    C28ADDR C28MAP C28OBJ CLRC CMP CMPB CMPL CMPR CSB DEC DINT DMAC DMOV EALLOW EDIS EINT ESTOP0 ESTOP1 FFC FLIP IACK
    IDLE IMACL IMPYAL IMPYL IMPYSL IMPYXUL IN INC INTR IRET LB LC LCR LOOPNZ LOOPZ LPADDR LRET LRETE LRETR LSL LSL64
    LSLL LSR LSR64 LSRL MAC MAX MAXCUL MAXL MIN MINCUL MINL MOV MOVA MOVAD MOVB MOVDL MOVH MOVL MOVP MOVS MOVU MOVW
    MOVX MOVZ MPY MPYA MPYB MPYS MPYU MPYXU NASP NEG NEG64 NEGTC NOP NORM NOT OR ORB OUT POP PREAD PUSH PWRITE QMACL
    QMPYAL QMPYL QMPYSL QMPYUL QMPYXUL ROL ROR RPT SAT SAT64 SB SBBU SBF SBRK SETC SFR SPM SQRA SQRS SUB SUBB SUBBL
    SUBCU SUBCUL SUBL SUBR SUBRL SUBU SUBUL TBIT TCLR TEST TRAP TSET UOUT XB XBANZ XCALL XMAC XMACD XOR XORB XPREAD
    XPWRITE XRET XRETC ZALR ZAPA""".split()
    fpu = """ABSF32 ADDF32 CMPF32 EINVF32 EISQRTF32 F32TOI16 F32TOI16R F32TOI32 F32TOUI16 F32TOUI16R F32TOUI32 FRACF32
    I16TOF32 I32TOF32 MACF32 MAXF32 MINF32 MOV16 MOV32 MOVD32 MOVIZ MOVIZF32 MOVST0 MOVXI MPYF32 NEGF32 RESTORE SAVE
    SETFLG SUBF32 SWAPF TESTTF UI16TOF32 UI32TOF32 ZERO ZEROA""".split()
    assert not [m for m in base + fpu if m not in mns]
    assert all(e.conf in ("ref", "low") for e in ISA.entries)


def test_loc_modes_parse_and_format():
    ev = lambda t: int(t, 0)          # noqa: E731
    cases = {"@5": 0x05, "@0x3F": 0x3F, "*-SP[2]": 0x42, "*SP++": 0xBD, "*--SP": 0xBE, "*XAR3++": 0x83,
             "*--XAR2": 0x8A, "*+XAR1[AR0]": 0x91, "*+XAR7[AR1]": 0x9F, "*+XAR4[3]": 0xDC, "*XAR4": 0xC4,
             "*AR6%++": 0xBF, "*BR0++": 0xAE, "*BR0--": 0xAF, "@AL": 0xA9, "@AH": 0xA8, "@SP": 0xAD, "@AR5": 0xA5,
             "@T": 0xAC, "@TH": 0xAC, "*": 0xB8, "*0++": 0xBB, "*,ARP5": 0xB5}
    for text, code in cases.items():
        assert loc_parse(text, False, ev) == code, text
    assert loc_parse("@ACC", True, ev) == 0xA9 and loc_parse("@XAR5", True, ev) == 0xA5
    assert loc_parse("@ACC", False, ev) is None and loc_parse("@AL", True, ev) is None
    assert loc_parse("@0x9001", False, ev) == 0x01                   # direct: low 6 bits of the address
    for code in range(256):
        for w32 in (False, True):
            t = loc_fmt(code, w32)
            if not t.startswith("<"):
                p = loc_parse(t, w32, ev)
                assert p == code or (code < 0x40 and p == code), (code, t)


def test_expressions_labels_and_directives():
    r = assemble("""
VAL     .set    0x10 + 2*3
        .global start
        .sect   "vectors", 0x9000
        .long   start, isr          ; 32-bit, low word first
        .word   VAL, -1, 'A', 1Fh, 101b, $ - 0x9000
        .sect   "code"
        .org    0x8000
start:  MOVW    DP,#DP(var)
        MOV     AL,@var
        LCR     #isr
        SB      start,UNC
isr:    IRET
        .data
        .org    0xA040
var:    .word   (VAL << 4) | 3, HI16(0x12345678), LO16(0x12345678)
str:    .string "hi", 0
fl:     .float  1.5
        .align  4
blk:    .space  3
end_:
        .bss    buf, 16
    """)
    s = r.symbols
    assert s["VAL"] == 0x16 and s["start"] == 0x8000 and s["var"] == 0xA040
    w = r.words()
    assert [w[0x9000 + i] for i in range(4)] == [0x8000, 0, s["isr"], 0]
    assert [w[0x9004 + i] for i in range(6)] == [0x16, 0xFFFF, 0x41, 0x1F, 5, 4]
    assert w[0x8000] == 0x761F and w[0x8001] == 0xA040 >> 6
    assert [w[0xA040 + i] for i in range(3)] == [0x163, 0x1234, 0x5678]
    assert [w[s["str"] + i] for i in range(3)] == [ord("h"), ord("i"), 0]
    assert s["fl"] % 2 == 0 and (w[s["fl"]], w[s["fl"] + 1]) == (0x0000, 0x3FC0)
    assert s["blk"] % 4 == 0 and s["end_"] == s["blk"] + 3
    assert "buf" in s and r.entry == 0x8000
    lines = [t for _, _, t in disasm([w[0x8000 + i] for i in range(7)], 0x8000)]
    assert lines[:4] == ["MOVW DP,#0x281", "MOV AL,@0", f"LCR #0x{s['isr']:X}", "SB 0x8000,UNC"]


def test_forward_references_and_branch_ranges():
    r = assemble("""
        .org 0x100
        SB fwd,EQ
        B far,NEQ
        .space 100
fwd:    NOP
        .space 30000
far:    NOP
    """)
    w, s = r.words(), r.symbols
    assert w[0x100] == 0x6100 | (s["fwd"] - 0x100)
    assert w[0x101] == 0xFFE0 and w[0x102] == s["far"] - 0x101
    with pytest.raises(AsmError, match="range"):
        assemble(" .org 0x100\n SB far,UNC\n .space 300\nfar: NOP\n")
    with pytest.raises(AsmError, match="range"):
        assemble(" .org 0x100\n B far,UNC\n .space 40000\nfar: NOP\n")


def test_errors_are_reported_with_line_numbers():
    with pytest.raises(AsmError, match=r"<asm>:2: unknown mnemonic FOO"):
        assemble("  NOP\n  FOO AL\n")
    with pytest.raises(AsmError, match="undefined symbol"):
        assemble("  LB nowhere\n")
    with pytest.raises(AsmError, match="no form of MOV"):
        assemble("  MOV AL,AH\n")
    with pytest.raises(AsmError, match="out of range"):
        assemble("  MOVB AL,#300\n")
    with pytest.raises(AsmError, match="overlapping"):
        assemble("  .org 0\n  NOP\n  .sect \"b\", 0\n  NOP\n")


def test_movf32_pseudo_and_float_immediates():
    r = assemble("""
        MOVF32 R1H,#1.5
        MOVF32 R2H,#0.1
        MOVI32 R3H,#0x12345678
        ADDF32 R0H,#2.5,R1H
    """)
    lines = [t for _, _, t in disasm(list(r.words().values()))]
    # MOVIZ and MOVIZF32 share one opcode (SPRUHS1C); the disassembler prints MOVIZF32 with a float immediate
    assert lines == ["MOVIZF32 R1H,#1.5", "MOVIZF32 R2H,#0.099609375", "MOVXI R2H,#0xCCCD",
                     "MOVIZF32 R3H,#5.679798517591285e-28", "MOVXI R3H,#0x5678", "ADDF32 R0H,#2.5,R1H"]


def test_case_insensitive_mnemonics_and_comments():
    r = assemble("""
* full-line comment
lbl     movb    acc,#1      ; trailing comment
        Mov     al,*+xar4[2]
        sb      lbl, unc
    """)
    assert [t for _, _, t in disasm(list(r.words().values()))] == ["MOVB ACC,#1", "MOV AL,*+XAR4[2]", "SB 0x0,UNC"]


def test_disasm_unknown_word_and_c28img_loader(tmp_path):
    assert disasm([0x0000, 0xFFFF]) == [(0, 1, ".word 0x0000"), (1, 1, ".word 0xFFFF")]
    src = tmp_path / "t.asm"
    src.write_text('        .sect "code", 0x8000\n_c_int00: MOVB ACC,#3\n        SB $,UNC\n')
    out, mp, lst = tmp_path / "t.c28img", tmp_path / "t.map", tmp_path / "t.lst"
    p = subprocess.run([sys.executable, str(ROOT / "tools" / "c28x_asm.py"), str(src), "-o", str(out), "--map",
                        str(mp), "--list", str(lst)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    d = json.loads(out.read_text())
    assert d["format"] == "c28img" and d["unit_bits"] == 16 and d["entry"] == 0x8000
    assert d["segments"] == [{"addr": 0x8000, "data_hex": "0302006f"}]
    assert "_c_int00" in mp.read_text() and "MOVB ACC,#3" in lst.read_text()
    from chipem.firmware import load_firmware
    img = load_firmware(out)
    assert img.unit_bytes == 2 and img.entry == 0x8000 and img.segments == [(0x8000, bytes.fromhex("0302006f"))]
    p = subprocess.run([sys.executable, str(ROOT / "tools" / "c28x_asm.py"), "--disasm", str(out)],
                       capture_output=True, text=True)
    assert p.returncode == 0 and "SB 0x8001,UNC" in p.stdout
    binp = tmp_path / "t.bin"
    p = subprocess.run([sys.executable, str(ROOT / "tools" / "c28x_asm.py"), str(src), "-o", str(binp)],
                       capture_output=True, text=True)
    assert p.returncode == 0 and binp.read_bytes() == bytes.fromhex("0302006f")


def test_assembled_image_runs_on_core():
    from chipem.core.base import StopReason
    from chipem.core.c28x import C28xCore
    r = assemble("""
        .sect "code", 0x8000
        MOVL XAR4,#tbl
        MOVB ACC,#0
        MOVB XAR2,#3
lp:     ADD AL,*XAR4++
        BANZ lp,AR2--
        ESTOP0
        .sect "data", 0x9000
tbl:    .word 1, 2, 3, 4
    """)
    c = C28xCore()
    c.map_memory(0, 0x10000)
    c.load = [c.write_mem(a, d) for a, d in r.segments]
    c.reset(0x8000, 0x400)
    assert c.run(100)[0] is StopReason.BREAKPOINT and c.read_reg("al") == 10
