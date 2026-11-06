"""Golden C28x/FPU32 encodings: exact machine words from the opcode fields of TI's reference guides
(SPRU430F chapter 6 for the base ISA and Table 5-1 for loc16/loc32 bytes; SPRUHS1C chapter 1 / SPRUEO2B for the FPU).
Each word below is the reference's bit pattern with the operand fields filled in by hand (see the comments), so a
table edit that breaks silicon compatibility fails here even if assembler and disassembler still agree."""
from __future__ import annotations

import pytest

from chipem.core.c28x.asm import assemble, disasm_one

PC = 0x100

GOLDEN = [
    # ---- control / status (fixed opcodes)
    ("NOP", [0x7700]),                                   # 0111 0111 LLLL LLLL, loc @0
    ("ABORTI", [0x0001]),
    ("ABS ACC", [0xFF56]),
    ("IRET", [0x7602]),
    ("LRETR", [0x0006]),
    ("LRET", [0x7614]),
    ("LRETE", [0x7610]),
    ("EALLOW", [0x7622]),
    ("EDIS", [0x761A]),
    ("DINT", [0x3B10]),                                  # = SETC INTM
    ("SETC INTM", [0x3B10]),
    ("EINT", [0x2910]),                                  # = CLRC INTM
    ("CLRC SXM", [0x2901]),
    ("SETC SXM,OVM", [0x3B03]),
    ("IDLE", [0x7621]),
    ("ESTOP0", [0x7625]),
    ("ASP", [0x761B]),
    ("NASP", [0x7617]),
    ("ZAPA", [0x5633]),
    ("CLRC OVC", [0x565C]),
    ("ZAP OVC", [0x565C]),
    ("SPM +1", [0xFF68]),
    ("SPM -4", [0xFF6D]),
    ("SPM -6", [0xFF6F]),
    ("TRAP #19", [0x0033]),                              # 0000 0000 001C CCCC
    ("INTR INT5", [0x0014]),                             # 0000 0000 0001 CCCC, CCCC = vector - 1 (cl2000 25.11.1)
    ("INTR INT1", [0x0010]),
    ("INTR INT14", [0x001D]),
    ("INTR DLOGINT", [0x001E]),
    ("INTR RTOSINT", [0x001F]),
    ("INTR NMI", [0x7616]),
    ("INTR EMUINT", [0x761C]),
    ("C28OBJ", [0x561F]),
    ("C28ADDR", [0x5616]),
    ("CLRC AMODE", [0x5616]),
    # ---- stack
    ("PUSH ST0", [0x7618]),
    ("PUSH ST1", [0x7608]),
    ("PUSH DP", [0x760B]),
    ("PUSH DP:ST1", [0x7609]),
    ("PUSH T:ST0", [0x7619]),
    ("PUSH AR1:AR0", [0x760D]),
    ("PUSH AR3:AR2", [0x760F]),
    ("PUSH AR5:AR4", [0x760C]),
    ("PUSH AR1H:AR0H", [0x0005]),
    ("PUSH DBGIER", [0x760E]),
    ("PUSH IFR", [0x760A]),
    ("PUSH P", [0x761D]),
    ("PUSH RPC", [0x0004]),
    ("POP ST0", [0x7613]),
    ("POP ST1", [0x7600]),
    ("POP DP", [0x7603]),
    ("POP DP:ST1", [0x7601]),
    ("POP T:ST0", [0x7615]),
    ("POP AR1:AR0", [0x7607]),
    ("POP AR3:AR2", [0x7605]),
    ("POP AR5:AR4", [0x7606]),
    ("POP AR1H:AR0H", [0x0003]),
    ("POP DBGIER", [0x7612]),
    ("POP IFR", [0x0002]),
    ("POP P", [0x7611]),
    ("POP RPC", [0x0007]),
    ("PUSH ACC", [0x1EBD]),                              # = MOVL *SP++,ACC
    ("POP ACC", [0x06BE]),
    ("PUSH XAR7", [0xC3BD]),
    ("POP XAR4", [0x8ABE]),
    ("PUSH XT", [0xABBD]),
    ("POP XT", [0x87BE]),
    # ---- branches / calls (offsets relative to the branch's own address; 22-bit absolute targets)
    ("SB 0x105,NEQ", [0x6005]),                          # 0110 COND CCCC CCCC
    ("SB 0xFE,UNC", [0x6FFE]),
    ("B 0x200,GEQ", [0xFFE3, 0x0100]),                   # 1111 1111 1110 COND + 16-bit offset
    ("BF 0x80,EQ", [0x56C1, 0xFF80]),
    ("SBF 0x110,NTC", [0xEF10]),
    ("BANZ 0xFE,AR3--", [0x000B, 0xFFFE]),               # 0000 0000 0000 1nnn
    ("BAR 0x104,AR1,AR2,NEQ", [0x8FCA, 0x0004]),         # 1000 1111 11nn nmmm
    ("LB 0x3F1234", [0x007F, 0x1234]),                   # 0000 0000 01CC CCCC
    ("LC 0x8000", [0x0080, 0x8000]),
    ("LCR #0x8123", [0x7640, 0x8123]),
    ("FFC XAR7,0x3F8000", [0x00FF, 0x8000]),
    ("LB *XAR7", [0x7620]),
    ("LC *XAR7", [0x7604]),
    ("LCR *XAR3", [0x3E63]),                             # 0011 1110 0110 0RRR
    ("XB 0x8000,UNC", [0x56DF, 0x8000]),
    ("XCALL *AL", [0x5634]),
    ("XRETC TC", [0x56FD]),
    ("XBANZ 0x1234,*++", [0x560A, 0x1234]),
    ("XBANZ 0x1234,*0--,ARP2", [0x3E52, 0x1234]),
    ("RPT #15", [0xF60F]),
    ("RPT @5", [0xF705]),
    # ---- moves
    ("MOVW DP,#0x1234", [0x761F, 0x1234]),
    ("MOV DP,#0x3FF", [0xFBFF]),                         # 1111 10CC CCCC CCCC
    ("MOVZ DP,#0x155", [0xB955]),
    ("MOVL XAR4,*XAR0++", [0x8A80]),
    ("MOVL XAR0,*--XAR1", [0x8E89]),
    ("MOVL XAR4,#0x123456", [0x8F12, 0x3456]),           # 1000 1111 00CC CCCC
    ("MOVL XAR6,#0x3F0000", [0x76BF, 0x0000]),
    ("MOVL *-SP[2],ACC", [0x1E42]),
    ("MOVL ACC,P<<PM", [0x16AC]),
    ("MOV *XAR4++,#0x5A5A", [0x2884, 0x5A5A]),           # MOV loc16,#16bit
    ("MOV @0x10,#0", [0x2B10]),
    ("MOV AL,@0x3F", [0x923F]),                          # 1001 001A LLLL LLLL
    ("MOV AH,@0x3F", [0x933F]),
    ("MOV *-SP[1],AL", [0x9641]),
    ("MOV *+XAR4[3],AL", [0x96DC]),
    ("MOV @AR2,AH", [0x97A2]),
    ("MOV @0,AH,NEQ", [0x562B, 0x0000]),                 # 0101 0110 0010 101A / 0000 COND LLLL LLLL
    ("MOVB XAR6,#0x10", [0xBE10]),
    ("MOVB XAR0,#5", [0xD005]),
    ("MOVB AR6,#0x10", [0xD610]),
    ("MOVB ACC,#3", [0x0203]),
    ("MOVZ AR7,@AL", [0x80A9]),
    ("MOVZ AR2,@1", [0x5A01]),
    ("MOV @2,ACC<<#1", [0xB102]),
    ("MOV @2,ACC<<#4", [0x562D, 0x0302]),                # 0000 0SHF LLLL LLLL, SHF = shift-1
    ("MOVH @2,ACC<<#1", [0xB302]),
    ("MOVU @3,OVC", [0x5628, 0x0003]),
    ("MOV XAR5,PC", [0x3E5D]),
    ("MOV *(0:0x1234),@AL", [0xF4A9, 0x1234]),
    # ---- ACC arithmetic / logic
    ("ADD ACC,#0x1234<<#4", [0xFF14, 0x1234]),           # 1111 1111 0001 SHFT
    ("SUB ACC,#1<<#15", [0xFF0F, 0x0001]),
    ("AND ACC,#0xFF00<<#16", [0x5608, 0xFF00]),
    ("OR ACC,#0x8000", [0x3E10, 0x8000]),
    ("XOR ACC,#0x00FF<<#3", [0x3E23, 0x00FF]),
    ("ADD ACC,@5<<#16", [0x0505]),
    ("ADD ACC,*-SP[2]<<#3", [0x5604, 0x0342]),
    ("ADD ACC,@1", [0x8101]),
    ("SUB ACC,@0", [0xAE00]),
    ("MOV ACC,@7<<#16", [0x2507]),
    ("ADDB ACC,#200", [0x09C8]),
    ("ADDL ACC,@ACC", [0x07A9]),
    ("SUBL ACC,*XAR2++", [0x0382]),
    ("CMPL ACC,P<<PM", [0xFF59]),
    ("MAXCUL P,@4", [0x5651, 0x0004]),
    ("MINCUL P,@4", [0x5659, 0x0004]),
    ("MAX AH,@4", [0x5673, 0x0004]),
    ("MIN AL,@4", [0x5674, 0x0004]),
    ("LSL ACC,#1", [0xFF30]),                            # 1111 1111 0011 SHFT, SHFT = shift-1
    ("LSL ACC,#16", [0xFF3F]),
    ("ASR AL,#16", [0xFFAF]),
    ("SFR ACC,#8", [0xFF47]),
    ("LSL64 ACC:P,#4", [0x56A3]),
    ("NORM ACC,XAR2++", [0xFF7A]),
    ("NORM ACC,*0++", [0x5677]),
    ("TBIT @3,#15", [0x4F03]),
    ("TSET @3,#2", [0x560D, 0x0203]),
    ("ADDB XAR3,#7", [0xDB07]),
    ("SUBB XAR3,#7", [0xDB87]),
    ("ADDB SP,#4", [0xFE04]),
    ("SUBB SP,#4", [0xFE84]),
    ("CMPR 1", [0x5619]),
    # ---- multiply
    ("MAC P,*XAR4++,*XAR7++", [0x5607, 0x8784]),         # second word: 1000 0111 LLLL LLLL
    ("MAC P,@1,*XAR7", [0x5607, 0xC701]),
    ("DMAC ACC:P,*XAR2++,*XAR7++", [0x564B, 0x8782]),
    ("IMACL P,*XAR2++,*XAR7", [0x564D, 0xC782]),
    ("QMACL P,*XAR2++,*XAR7++", [0x564F, 0x8782]),
    ("IMPYL P,XT,@ACC", [0x5605, 0x00A9]),
    ("QMPYL P,XT,@ACC", [0x5667, 0x00A9]),
    ("IMPYXUL P,XT,@4", [0x5665, 0x0004]),
    ("QMPYUL P,XT,@4", [0x5647, 0x0004]),
    ("XMAC P,@1,*(0x1234)", [0x8401, 0x1234]),
    ("XMACD P,@1,*(0x1234)", [0xA401, 0x1234]),
    ("MPY P,T,@1", [0x3301]),
    ("MPYB P,T,#5", [0x3105]),
    # ---- addressing-mode bytes (SPRU430F Table 5-1)
    ("MOV AL,*BR0++", [0x92AE]),
    ("MOV AL,*BR0--", [0x92AF]),
    ("MOV AL,*,ARP3", [0x92B3]),
    ("MOV AL,*AR6%++", [0x92BF]),
    ("MOV AL,@TH", [0x92AC]),
    ("MOV AL,*", [0x92B8]),
    ("MOV AL,*0--", [0x92BC]),
    ("MOV AL,*SP++", [0x92BD]),
    ("MOV AL,*+XAR1[AR0]", [0x9291]),
    ("MOV AL,*+XAR7[AR1]", [0x929F]),
    # ---- FPU32 (SPRUHS1C chapter 1), LSW first
    ("MOVIZF32 R0H,#1.5", [0xE801, 0xFE00]),             # 1110 1000 0000 0III / IIII IIII IIII Iaaa
    ("MOVIZ R0H,#0x3FC0", [0xE801, 0xFE00]),
    ("MOVXI R2H,#0xCCCD", [0xE80E, 0x666A]),             # 1110 1000 0000 1III ...
    ("ADDF32 R0H,R1H,R2H", [0xE710, 0x0088]),            # MSW 0000 000c ccbb baaa
    ("MPYF32 R3H,R4H,R5H", [0xE700, 0x0163]),
    ("SUBF32 R3H,R4H,R5H", [0xE720, 0x0163]),
    ("ADDF32 R0H,#2.5,R1H", [0xE890, 0x0808]),           # 1110 1000 10II IIII / IIII IIII IIbb baaa
    ("CMPF32 R1H,#0.0", [0xE5A1]),
    ("CMPF32 R1H,R2H", [0xE694, 0x0011]),
    ("MAXF32 R1H,#1.0", [0xE821, 0xFC01]),
    ("MOV32 R0H,*XAR4++", [0xE2AF, 0x0084]),
    ("MOV32 R0H,*XAR4++,EQ", [0xE2A1, 0x0084]),
    ("MOV32 *XAR4++,R1H", [0xE203, 0x0184]),
    ("MOV16 @2,R1H", [0xE213, 0x0102]),
    # MOV32 RaH,{ACC|P|XT|XARn} and back: MOV32 *(0:0x0F12+4a) opcodes (SPRUHS1C pages; cl2000 25.11.1 / dis2000)
    ("MOV32 R0H,ACC", [0xBDA9, 0x0F12]),
    ("MOV32 R0H,@ACC", [0xBDA9, 0x0F12]),
    ("MOV32 ACC,R2H", [0xBFA9, 0x0F1A]),
    ("MOV32 @P,R0H", [0xBFAB, 0x0F12]),
    ("MOV32 R1H,XAR7", [0xBDA7, 0x0F16]),
    ("MOV32 @XT,R7H", [0xBFAC, 0x0F2E]),
    ("MOV32 R1H,R2H", [0xE6CF, 0x0011]),
    ("MOV32 *(0:0x1234),@ACC", [0xBDA9, 0x1234]),
    ("MOV32 @ACC,*(0:0x1234)", [0xBFA9, 0x1234]),
    ("MOVD32 R1H,*XAR4++", [0xE223, 0x0184]),
    ("F32TOI32 R1H,R2H", [0xE688, 0x0011]),
    ("F32TOI16R R0H,R1H", [0xE68C, 0x8008]),
    ("F32TOUI16 R0H,R1H", [0xE68E, 0x0008]),
    ("I32TOF32 R0H,R1H", [0xE689, 0x0008]),
    ("UI32TOF32 R0H,R1H", [0xE68B, 0x0008]),
    ("FRACF32 R0H,R1H", [0xE6F1, 0x0008]),
    ("ABSF32 R0H,R1H", [0xE695, 0x0008]),
    ("NEGF32 R0H,R1H", [0xE6AF, 0x0008]),
    ("EINVF32 R0H,R1H", [0xE693, 0x0008]),
    ("EISQRTF32 R0H,R1H", [0xE692, 0x0008]),
    ("I16TOF32 R0H,@0", [0xE2C8, 0x0000]),
    ("UI16TOF32 R0H,@0", [0xE2C4, 0x0000]),
    ("I32TOF32 R0H,@0", [0xE288, 0x0000]),
    ("UI32TOF32 R0H,@0", [0xE284, 0x0000]),
    ("MOV32 @0,STF", [0xE200, 0x0000]),
    ("MOV32 STF,@0", [0xE280, 0x0000]),
    ("MOVST0 ZF,NF", [0xAD14]),                          # cl2000 25.11.1 / dis2000: AD14 = MOVST0 NF,ZF
    ("MOVST0 TF", [0xAD80]),
    ("MOVST0 NI", [0xAD08]),
    ("SETFLG RNDF32=1", [0xE610, 0x0200]),               # 1110 0110 00FF FFFF / FFFF FVVV VVVV VVVV
    ("SAVE RNDF32=1,TF=0", [0xE652, 0x0200]),
    ("TESTTF EQ", [0xE581]),
    ("SWAPF R0H,R1H", [0xE6EF, 0x0008]),
    ("ZERO R3H", [0xE593]),
    ("ZEROA", [0xE563]),
    ("RESTORE", [0xE562]),
    ("PUSH RB", [0xFFF0]),
    ("POP RB", [0xFFF1]),
    ("MPYF32 R0H,R1H,R2H || ADDF32 R3H,R4H,R5H", [0xE742, 0xC688]),   # 1110 0111 0100 00ff / feee dddc ccbb baaa
    ("MPYF32 R0H,R1H,R2H || SUBF32 R3H,R4H,R5H", [0xE752, 0xC688]),
    ("MACF32 R3H,R2H,R0H,R1H,R4H", [0xE741, 0x3708]),    # = MPYF32 R0H,R1H,R4H || ADDF32 R3H,R3H,R2H
    ("MACF32 R7H,R3H,*XAR6++,*XAR7++", [0xE250, 0x1F86]),
    ("MPYF32 R1H,R2H,R3H || MOV32 R0H,@4", [0xE306, 0x8804]),   # fffe eedd daaa: f=3 e=2 d=1 a=0
    ("ADDF32 R1H,R2H,R3H || MOV32 @4,R0H", [0xE016, 0x8804]),
    ("MAXF32 R0H,R1H || MOV32 R2H,R3H", [0xE69C, 0x0688]),
]


def _assemble_one(text: str) -> list[int]:
    r = assemble(f" .org {PC}\n {text}\n")
    return [w for _, w in sorted(r.words().items())]


@pytest.mark.parametrize("text,words", GOLDEN, ids=[t for t, _ in GOLDEN])
def test_golden_encoding(text, words):
    assert _assemble_one(text) == words, f"{text}: got {[hex(x) for x in _assemble_one(text)]}"
    n, dis, e = disasm_one(words + [0], PC)
    assert e is not None and n == len(words), (text, dis)
    assert _assemble_one(dis) == words, (text, dis)


def test_parallel_two_line_syntax_and_rpt_bars():
    r = assemble("""
        .org 0x100
        MPYF32 R0H,R1H,R2H
     || ADDF32 R3H,R4H,R5H
        RPT #3
     || MOV *XAR4++,AL
        RPT #3 || NOP
    """)
    assert [w for _, w in sorted(r.words().items())] == [0xE742, 0xC688, 0xF603, 0x9684, 0xF603, 0x7700]
