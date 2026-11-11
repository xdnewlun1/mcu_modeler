"""stm32.dma2d (RM0090 Rev 22 chapter 11, Chrom-ART Accelerator) on the STM32F427VG chip YAML: pixel format
conversion (Tables 53-59, 11.3.4 MSB replication), alpha modes (Table 55), the blender (11.3.6), the four modes
(11.3.11), CLUT loading (11.3.5), configuration/transfer/CLUT-access errors (11.3.11, 11.3.14), watermark (11.3.13),
suspend/abort (11.3.12), write protection (11.5), IRQ 90 and the line-by-line timing; plus the f42x_dma2d sample."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.firmware import FirmwareImage, load_firmware
from chipem.periph.stm32.dma2d import alpha_mod, blend_pixel, decode, encode

from engine_util import asm

ROOT = Path(__file__).resolve().parent.parent
YAML = ROOT / "chips" / "stm32f427vg.yaml"
SAMPLE = ROOT / "firmware" / "samples_stm32_variants" / "build" / "f42x_dma2d.elf"
FLASH = 0x08000000
RCC = 0x40023800
D = 0x4002B000
CR, ISR, IFCR, FGMAR, FGOR, BGMAR, BGOR, FGPFCCR, FGCOLR, BGPFCCR, BGCOLR, FGCMAR, BGCMAR, OPFCCR, OCOLR, OMAR, \
    OOR, NLR, LWR, AMTCR = range(0, 0x50, 4)
START, SUSP, ABORT = 1, 2, 4
TEIE, TCIE, TWIE, CAEIE, CTCIE, CEIE = (1 << b for b in range(8, 14))
TEIF, TCIF, TWIF, CAEIF, CTCIF, CEIF = (1 << b for b in range(6))
M2M, PFC, BLEND, R2M = (m << 16 for m in range(4))
IRQ = 90
SRC, BGSRC, DST = 0x20004000, 0x20008000, 0x20010000


@pytest.fixture(scope="module")
def desc():
    return load_chip(YAML)


def spin_chip(desc) -> Chip:
    code = asm("spin: b spin", FLASH + 0x200)
    img = bytearray(0x200 + len(code))
    img[0:8] = struct.pack("<II", 0x20030000, (FLASH + 0x200) | 1)
    img[0x200:] = code
    chip = Chip(desc)
    chip.load_firmware(FirmwareImage([(FLASH, bytes(img))]))
    chip.reset()
    wr(chip, RCC + 0x30, rd(chip, RCC + 0x30) | 1 << 23)        # DMA2DEN
    return chip


@pytest.fixture
def chip(desc):
    return spin_chip(desc)


def rd(chip, addr, n=4):
    return chip.bus.read(addr, n)


def wr(chip, addr, v, n=4):
    chip.bus.write(addr, n, v)


def r(chip, off):
    return rd(chip, D + off)


def w(chip, off, v):
    wr(chip, D + off, v)


def mem(chip, addr, n):
    return chip.bus.read_bytes(addr, n)


def put(chip, addr, data):
    chip.bus.write_bytes(addr, bytes(data))


def irq(chip) -> bool:
    return (16 + IRQ) in chip.arch.asserted


def warns(chip):
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "warn"]


def nlr(pl, nl):
    return pl << 16 | nl


# ------------------------------------------------------------------------------------- pure conversions (RM0090)
CLUT = [0xFF000000 | i * 0x010101 for i in range(256)]
CLUT[3], CLUT[5] = 0x12ABCDEF, 0x80102030


def test_decode_direct_formats_replicate_msbs():
    # Table 54 byte order, 11.3.4: expansion copies the MSBs into the LSBs, no alpha -> 0xFF
    assert decode(0, struct.pack("<I", 0x80112233), 1) == [0x80112233]
    assert decode(1, bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66]), 2) == [0xFF332211, 0xFF665544]
    assert decode(2, struct.pack("<4H", 0xF800, 0x07E0, 0x001F, 0x8410), 4) == \
        [0xFFFF0000, 0xFF00FF00, 0xFF0000FF, 0xFF848284]
    assert decode(3, struct.pack("<2H", 0x7C00, 0x8000 | 0x0210), 2) == [0x00FF0000, 0xFF008484]
    assert decode(4, struct.pack("<H", 0x8421), 1) == [0x88442211]


def test_decode_indirect_and_alpha_formats():
    assert decode(5, bytes([3, 200]), 2, CLUT) == [0x12ABCDEF, 0xFFC8C8C8]                   # L8
    assert decode(6, bytes([0xA3]), 1, CLUT) == [0xAAABCDEF]                                # AL44: A from pixel
    assert decode(7, bytes([5, 0x40]), 1, CLUT) == [0x40102030]                             # AL88: L @+0, A @+1
    assert decode(8, bytes([0x21, 0x53]), 4, CLUT) == [CLUT[1], CLUT[2], CLUT[3], CLUT[5]]  # L4: low nibble first
    assert decode(9, bytes([0x80, 0x00]), 2, color=0xFF123456) == [0x80123456, 0x00123456]  # A8 + COLR
    assert decode(10, bytes([0x5F]), 2, color=0x00ABCDEF) == [0xFFABCDEF, 0x55ABCDEF]       # A4


def test_encode_output_formats_truncate():
    assert encode(0, [0x80112233]) == struct.pack("<I", 0x80112233)
    assert encode(1, [0xFF332211]) == bytes([0x11, 0x22, 0x33])
    assert encode(2, [0xFF848284, 0xFFFFFFFF]) == struct.pack("<2H", 0x8410, 0xFFFF)
    assert encode(3, [0x80FF0000, 0x7FFF0000]) == struct.pack("<2H", 0xFC00, 0x7C00)
    assert encode(4, [0x88442211]) == struct.pack("<H", 0x8421)


def test_alpha_modes_and_blender():
    px = [0xFF112233, 0x40112233]
    assert alpha_mod(px, 0, 0x80) == px
    assert alpha_mod(px, 1, 0x80) == [0x80112233, 0x80112233]
    assert alpha_mod(px, 2, 0x80) == [0x80112233, 0x20112233]           # 0x40 * 0x80 / 255 = 32.1 -> 32
    # 11.3.6: aMult = 128*255/255 = 128, aOUT = 255; B = (255*255 - 255*128) / 255 = 127
    assert blend_pixel(0x80FF0000, 0xFF0000FF) == 0xFF80007F
    # aMult = 64*128/255 = 32, aOUT = 160; R = (17*64 + 68*128 - 68*32)/160 = 47.6 -> 47 ...
    assert blend_pixel(0x40112233, 0x80445566) == 0xA02F4051
    assert blend_pixel(0xFF123456, 0x80FFFFFF) == 0xFF123456              # opaque FG wins
    assert blend_pixel(0x00123456, 0x00FFFFFF) == 0                       # aOUT = 0 (not defined by the RM)


# ------------------------------------------------------------------------------------- register-to-memory + timing
def test_r2m_fill_rgb565_timing_and_irq(chip):
    put(chip, DST, b"\xAA" * (80 * 2 * 48))
    w(chip, OPFCCR, 2)                                  # RGB565
    w(chip, OCOLR, 0xF81F)                              # magenta, already in the output format (11.5.15)
    w(chip, OMAR, DST)
    w(chip, OOR, 16)                                    # 64-pixel window in an 80-pixel-wide frame
    w(chip, NLR, nlr(64, 48))
    w(chip, CR, R2M | TCIE | START)
    assert r(chip, CR) & START and r(chip, ISR) == 0
    # 32 word writes per line (no pipeline cost in R2M) -> 32 HCLK per line, 1536 for 48 lines
    chip.run_cycles(32 * 10 + 5)
    done = mem(chip, DST, 80 * 2 * 48)
    assert done[:128] == b"\x1f\xf8" * 64 and done[128:160] == b"\xAA" * 32        # OOR gap untouched
    assert done[160 * 10:160 * 10 + 2] == b"\xAA\xAA"                              # line 10 not yet written
    assert r(chip, CR) & START and not irq(chip)
    chip.run_cycles(1536 - 325 - 10)
    assert r(chip, CR) & START
    chip.run_cycles(40)
    # LWR resets to 0: the watermark (line 0) is passed in every transfer, so TWIF is set too (11.3.13)
    assert r(chip, ISR) == TCIF | TWIF and not r(chip, CR) & START and irq(chip)
    done = mem(chip, DST, 80 * 2 * 48)
    assert all(done[160 * y:160 * y + 128] == b"\x1f\xf8" * 64 for y in range(48))
    w(chip, IFCR, TCIF)
    assert r(chip, ISR) == TWIF and not irq(chip) and r(chip, IFCR) == 0      # TWIE = 0: no IRQ


def test_dead_time_slows_transfer(chip):
    w(chip, OPFCCR, 0)
    w(chip, OCOLR, 0x11223344)
    w(chip, OMAR, DST)
    w(chip, NLR, nlr(4, 2))
    w(chip, AMTCR, 3 << 8 | 1)                          # 3 dead cycles between AHB accesses (11.3.15)
    w(chip, CR, R2M | START)
    chip.run_cycles(4 * 4 * 2 - 3)                      # 4 beats x (1 + 3) per line, 2 lines = 32
    assert r(chip, CR) & START
    chip.run_cycles(10)
    assert r(chip, ISR) & TCIF and mem(chip, DST, 32) == struct.pack("<I", 0x11223344) * 8


def test_r2m_rgb888_and_argb4444(chip):
    w(chip, OPFCCR, 1)
    w(chip, OCOLR, 0x00ABCDEF)
    w(chip, OMAR, DST + 1)                              # RGB888 needs only byte alignment
    w(chip, NLR, nlr(3, 1))
    w(chip, CR, R2M | START)
    chip.run_cycles(50)
    assert mem(chip, DST + 1, 9) == bytes([0xEF, 0xCD, 0xAB]) * 3
    w(chip, OPFCCR, 4)
    w(chip, OCOLR, 0x8421)
    w(chip, OMAR, DST + 0x100)
    w(chip, CR, R2M | START)
    chip.run_cycles(50)
    assert mem(chip, DST + 0x100, 6) == struct.pack("<3H", 0x8421, 0x8421, 0x8421)


# ------------------------------------------------------------------------------------- memory-to-memory modes
def test_m2m_raw_copy_with_offsets(chip):
    src = bytes(range(256)) * 2
    put(chip, SRC, src)
    put(chip, DST, bytes(64))
    w(chip, FGPFCCR, 0)                                 # ARGB8888: 32 bpp in and out
    w(chip, FGMAR, SRC)
    w(chip, FGOR, 2)
    w(chip, OMAR, DST)
    w(chip, OOR, 1)
    w(chip, NLR, nlr(2, 3))
    w(chip, CR, M2M | START)
    chip.run_cycles(60)
    assert r(chip, ISR) & TCIF
    out = mem(chip, DST, 36)
    for y in range(3):
        assert out[12 * y:12 * y + 8] == src[16 * y:16 * y + 8]
        assert out[12 * y + 8:12 * y + 12] == bytes(4)


def test_m2m_pfc_argb8888_to_rgb565_with_alpha_replace(chip):
    px = [0xFFFF0000, 0x8000FF00, 0x400000FF, 0xFF848284]
    put(chip, SRC, struct.pack("<4I", *px))
    w(chip, FGPFCCR, 0x80 << 24 | 1 << 16 | 0)          # ARGB8888, AM = replace by 0x80
    w(chip, FGMAR, SRC)
    w(chip, OPFCCR, 3)                                  # ARGB1555: alpha bit = A[7] of the replaced alpha
    w(chip, OMAR, DST)
    w(chip, NLR, nlr(4, 1))
    w(chip, CR, PFC | START)
    chip.run_cycles(30)
    assert r(chip, ISR) & TCIF
    assert struct.unpack("<4H", mem(chip, DST, 8)) == (0xFC00, 0x83E0, 0x801F, 0xC210)
    w(chip, OPFCCR, 2)
    w(chip, CR, PFC | START)
    chip.run_cycles(30)
    assert struct.unpack("<4H", mem(chip, DST, 8)) == (0xF800, 0x07E0, 0x001F, 0x8410)


def test_pfc_pipeline_cost_one_pixel_per_clock(chip):
    put(chip, SRC, bytes(64))
    w(chip, FGPFCCR, 9)                                 # A8: 64 pixels = 16 read beats
    w(chip, FGMAR, SRC)
    w(chip, OPFCCR, 2)                                  # RGB565: 32 write beats -> 48 beats < 64 pixels
    w(chip, OMAR, DST)
    w(chip, NLR, nlr(64, 2))
    w(chip, CR, PFC | START)
    chip.run_cycles(125)
    assert r(chip, CR) & START
    chip.run_cycles(10)
    assert r(chip, ISR) & TCIF


def test_blend_a8_foreground_over_rgb565_background(chip):
    put(chip, SRC, bytes([0x80, 0xFF, 0x00, 0x40]))                    # A8 coverage
    put(chip, BGSRC, struct.pack("<4H", 0x001F, 0x001F, 0x001F, 0xFFFF))
    w(chip, FGPFCCR, 9)
    w(chip, FGCOLR, 0xFF0000)                                           # red text
    w(chip, FGMAR, SRC)
    w(chip, BGPFCCR, 2)
    w(chip, BGMAR, BGSRC)
    w(chip, OPFCCR, 0)
    w(chip, OMAR, DST)
    w(chip, NLR, nlr(4, 1))
    w(chip, CR, BLEND | START)
    chip.run_cycles(30)
    assert r(chip, ISR) & TCIF
    got = struct.unpack("<4I", mem(chip, DST, 16))
    assert got == (0xFF80007F, 0xFFFF0000, 0xFF0000FF,
                   blend_pixel(0x40FF0000, 0xFFFFFFFF))
    assert got[3] == 0xFFFFBFBF                         # (255*64 + 255*255 - 255*64)/255 ... = 191 for G/B


def test_blend_uses_bg_alpha_multiply(chip):
    put(chip, SRC, struct.pack("<I", 0x40112233))
    put(chip, BGSRC, struct.pack("<I", 0xFF445566))
    w(chip, FGPFCCR, 0)
    w(chip, FGMAR, SRC)
    w(chip, BGPFCCR, 0x80 << 24 | 2 << 16)              # multiply: 255 * 128 / 255 = 128
    w(chip, BGMAR, BGSRC)
    w(chip, OPFCCR, 0)
    w(chip, OMAR, DST)
    w(chip, NLR, nlr(1, 1))
    w(chip, CR, BLEND | START)
    chip.run_cycles(20)
    assert struct.unpack("<I", mem(chip, DST, 4))[0] == blend_pixel(0x40112233, 0x80445566) == 0xA02F4051


# ------------------------------------------------------------------------------------- CLUT
def test_clut_auto_load_argb8888_then_l8_and_l4(chip):
    clut = [0xFF000000 | (i * 0x030507) & 0xFFFFFF for i in range(16)]
    clut[2] = 0x40ABCDEF
    put(chip, BGSRC, struct.pack("<16I", *clut))
    w(chip, FGCMAR, BGSRC)
    w(chip, FGPFCCR, 15 << 8 | 0x20 | 5)                # CS = 15 (16 entries), ARGB8888, START, CM = L8
    w(chip, CR, CTCIE)
    assert r(chip, FGPFCCR) & 0x20
    chip.run_cycles(14)                                 # 16 word reads
    assert r(chip, FGPFCCR) & 0x20 and not r(chip, ISR) & CTCIF
    chip.run_cycles(6)
    assert r(chip, ISR) & CTCIF and not r(chip, FGPFCCR) & 0x20 and irq(chip)
    assert rd(chip, D + 0x400 + 8) == 0x40ABCDEF         # CLUT memory at offset 0x400
    w(chip, IFCR, CTCIF)
    put(chip, SRC, bytes([2, 7, 0x72, 0x00]))
    w(chip, FGMAR, SRC)
    w(chip, OPFCCR, 0)
    w(chip, OMAR, DST)
    w(chip, NLR, nlr(2, 1))
    w(chip, CR, PFC | START)
    chip.run_cycles(20)
    assert struct.unpack("<2I", mem(chip, DST, 8)) == (clut[2], clut[7])
    w(chip, FGPFCCR, 15 << 8 | 8)                       # L4: pixel 0 in the low nibble
    w(chip, FGMAR, SRC + 2)
    w(chip, NLR, nlr(2, 1))
    w(chip, CR, PFC | START)
    chip.run_cycles(20)
    assert struct.unpack("<2I", mem(chip, DST, 8)) == (clut[2], clut[7])


def test_clut_rgb888_auto_load_and_manual_write(chip):
    put(chip, BGSRC, bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66]))
    w(chip, BGCMAR, BGSRC + 1)                          # RGB888 CLUT: byte alignment is enough
    w(chip, BGPFCCR, 1 << 8 | 0x10 | 0x20 | 5)
    chip.run_cycles(10)
    assert r(chip, ISR) & CTCIF
    # Table 57 RGB888 order (B, G, R per entry); stored as opaque 32-bit entries
    assert rd(chip, D + 0x800) == 0xFF443322 and rd(chip, D + 0x804) == 0xFF006655
    wr(chip, D + 0x400 + 4 * 9, 0x7F010203)             # manual CLUT loading (CPU write)
    assert rd(chip, D + 0x400 + 4 * 9) == 0x7F010203


def test_clut_access_conflict_sets_caeif(chip):
    put(chip, BGSRC, bytes(1024))
    w(chip, FGCMAR, BGSRC)
    w(chip, FGPFCCR, 255 << 8 | 0x20)
    w(chip, CR, CAEIE)
    wr(chip, D + 0x400, 0x12345678)                     # CPU write during the automatic load: ignored
    assert r(chip, ISR) & CAEIF and irq(chip)
    assert rd(chip, D + 0x800) == 0                     # the other CLUT stays accessible
    assert r(chip, ISR) == CAEIF
    chip.run_cycles(300)
    assert r(chip, ISR) & CTCIF and rd(chip, D + 0x400) == 0


def test_clut_load_unaligned_is_config_error(chip):
    w(chip, FGCMAR, BGSRC + 2)
    w(chip, FGPFCCR, 0x20)                              # ARGB8888 CLUT needs 32-bit alignment
    assert r(chip, ISR) == CEIF and not r(chip, FGPFCCR) & 0x20


# ------------------------------------------------------------------------------------- errors
@pytest.mark.parametrize("setup,why", [
    (lambda c: (w(c, FGPFCCR, 0), w(c, FGMAR, SRC + 2), w(c, CR, PFC)), "FGMAR"),
    (lambda c: (w(c, FGPFCCR, 2), w(c, FGMAR, SRC + 1), w(c, CR, M2M)), "FGMAR"),
    (lambda c: (w(c, FGPFCCR, 11), w(c, FGMAR, SRC), w(c, CR, PFC)), "CM = 11"),
    (lambda c: (w(c, FGPFCCR, 8), w(c, FGMAR, SRC), w(c, NLR, nlr(3, 1)), w(c, CR, PFC)), "4-bit"),
    (lambda c: (w(c, FGPFCCR, 10), w(c, FGMAR, SRC), w(c, FGOR, 1), w(c, CR, PFC)), "4-bit"),
    (lambda c: (w(c, FGPFCCR, 0), w(c, FGMAR, SRC), w(c, BGPFCCR, 0), w(c, BGMAR, BGSRC + 1), w(c, CR, BLEND)),
     "BGMAR"),
    (lambda c: (w(c, OPFCCR, 5), w(c, CR, R2M)), "OPFCCR"),
    (lambda c: (w(c, OPFCCR, 0), w(c, OMAR, DST + 2), w(c, CR, R2M)), "OMAR"),
    (lambda c: (w(c, NLR, nlr(4, 0)), w(c, CR, R2M)), "NL = 0"),
    (lambda c: (w(c, NLR, nlr(0, 4)), w(c, CR, R2M)), "PL = 0"),
])
def test_configuration_errors(chip, setup, why):
    w(chip, OMAR, DST)
    w(chip, NLR, nlr(4, 1))
    setup(chip)
    put(chip, DST, b"\x55" * 16)
    w(chip, CR, r(chip, CR) | CEIE | START)
    assert r(chip, ISR) == CEIF and not r(chip, CR) & START and irq(chip)
    assert any(why in m for m in warns(chip))
    chip.run_cycles(100)
    assert mem(chip, DST, 16) == b"\x55" * 16 and not r(chip, ISR) & TCIF


def test_m2m_omar_alignment_not_checked(chip):
    put(chip, SRC, b"\x01\x02\x03\x04")
    w(chip, FGPFCCR, 0)
    w(chip, FGMAR, SRC)
    w(chip, OMAR, DST + 1)                              # 11.3.11: OMAR is not checked in memory-to-memory mode
    w(chip, NLR, nlr(1, 1))
    w(chip, CR, M2M | START)
    chip.run_cycles(20)
    assert r(chip, ISR) & TCIF and mem(chip, DST + 1, 4) == b"\x01\x02\x03\x04"


@pytest.mark.parametrize("omar", [0x10000100, 0x30000000])      # CCM data RAM, unmapped
def test_transfer_error_teif(chip, omar):
    w(chip, OPFCCR, 0)
    w(chip, OMAR, omar)
    w(chip, NLR, nlr(4, 4))
    w(chip, CR, R2M | TEIE | START)
    chip.run_cycles(20)
    assert r(chip, ISR) == TEIF and not r(chip, CR) & START and irq(chip)


# ------------------------------------------------------------------------------------- control
def test_watermark_twif(chip):
    w(chip, OPFCCR, 0)
    w(chip, OMAR, DST)
    w(chip, NLR, nlr(4, 20))                            # 4 beats per line
    w(chip, LWR, 10)
    w(chip, CR, R2M | TWIE | START)
    chip.run_cycles(4 * 11 - 2)
    assert not r(chip, ISR) & TWIF
    chip.run_cycles(4)
    assert r(chip, ISR) == TWIF and irq(chip) and r(chip, CR) & START
    chip.run_cycles(60)
    assert r(chip, ISR) == TWIF | TCIF


def test_suspend_resume_and_abort(chip):
    put(chip, DST, bytes(16 * 10))
    w(chip, OPFCCR, 0)
    w(chip, OCOLR, 0xFFFFFFFF)
    w(chip, OMAR, DST)
    w(chip, NLR, nlr(4, 10))
    w(chip, CR, R2M | TCIE | START)
    chip.run_cycles(4 * 3 + 1)
    w(chip, CR, R2M | TCIE | START | SUSP)
    chip.run_cycles(200)
    assert r(chip, CR) & START and not r(chip, ISR) & TCIF
    written = sum(1 for y in range(10) if mem(chip, DST + 16 * y, 4) == b"\xff" * 4)
    assert 3 <= written <= 4
    w(chip, CR, R2M | TCIE | START)                     # clear SUSP: resume
    chip.run_cycles(60)
    assert r(chip, ISR) & TCIF and all(mem(chip, DST + 16 * y, 16) == b"\xff" * 16 for y in range(10))
    w(chip, IFCR, 0x3F)
    put(chip, DST, bytes(160))
    w(chip, CR, R2M | TCIE | START)
    chip.run_cycles(9)
    w(chip, CR, R2M | TCIE | START | ABORT)
    assert r(chip, CR) & (START | ABORT | SUSP) == 0
    chip.run_cycles(100)
    assert not r(chip, ISR) & TCIF and not irq(chip)
    assert mem(chip, DST + 16 * 9, 16) == bytes(16)


def test_write_protection_while_running(chip):
    w(chip, OPFCCR, 0)
    w(chip, OMAR, DST)
    w(chip, NLR, nlr(4, 10))
    w(chip, CR, R2M | START)
    w(chip, NLR, nlr(8, 1))
    w(chip, OMAR, DST + 0x100)
    w(chip, CR, M2M | START)                            # MODE cannot change during a transfer
    assert r(chip, NLR) == nlr(4, 10) and r(chip, OMAR) == DST and (r(chip, CR) >> 16) & 3 == 3
    w(chip, FGPFCCR, 0x20)                              # CLUT START while a transfer runs: reset, not started
    assert not r(chip, FGPFCCR) & 0x20
    assert any("read-only" in m for m in warns(chip))
    chip.run_cycles(60)
    assert r(chip, ISR) & TCIF
    w(chip, NLR, nlr(8, 1))
    assert r(chip, NLR) == nlr(8, 1)


def test_reserved_bits_and_ifcr(chip):
    w(chip, FGPFCCR, 0xFFFFFFFF & ~0x20)
    assert r(chip, FGPFCCR) == 0xFF03FF1F               # CSS/AI/RBS (F7 SVD fields) are reserved on the F4
    w(chip, CR, 0xFFFFFFF8)
    assert r(chip, CR) == 0x00033F00
    w(chip, CR, 0)
    w(chip, OPFCCR, 0xFF)
    assert r(chip, OPFCCR) == 7
    w(chip, AMTCR, 0xFFFF)
    assert r(chip, AMTCR) == 0xFF01
    w(chip, ISR, 0x3F)
    assert r(chip, ISR) == 0                            # read-only


def test_rcc_reset_stops_transfer(chip):
    w(chip, OPFCCR, 0)
    w(chip, OMAR, DST)
    w(chip, NLR, nlr(4, 100))
    w(chip, CR, R2M | TCIE | START)
    chip.run_cycles(20)
    wr(chip, RCC + 0x10, 1 << 23)                       # AHB1RSTR.DMA2DRST
    wr(chip, RCC + 0x10, 0)
    assert r(chip, CR) == 0
    chip.run_cycles(1000)
    assert not r(chip, ISR) & TCIF and not irq(chip)


# ------------------------------------------------------------------------------------- sample firmware
def data_symbol(elf: Path, name: str) -> int:
    from elftools.elf.elffile import ELFFile
    with open(elf, "rb") as f:
        return ELFFile(f).get_section_by_name(".symtab").get_symbol_by_name(name)[0]["st_value"]


@pytest.mark.skipif(not SAMPLE.exists(), reason="run firmware/samples_stm32_variants/build.sh")
def test_sample_f42x_dma2d(desc):
    chip = Chip(desc)
    chip.load_firmware(load_firmware(SAMPLE))
    chip.reset()
    chip.run_for(30e6)              # 2 ms buffer setup at 16 MHz, 3 transfers, ~20 ms of firmware pixel checks
    sym = lambda n: data_symbol(SAMPLE, n)              # noqa: E731
    res = rd(chip, sym("result"))
    assert res == 0x600D, hex(res)                      # firmware compared every pixel with the RM formulas
    assert rd(chip, sym("tc_irqs")) == 3
    # HCLK cycles from START to the TC IRQ (TIM2 at 16 MHz = HCLK): the model's line timing plus IRQ entry
    #   fill 64x48 RGB565: 32 write beats/line x 48 = 1536
    #   PFC ARGB8888 -> RGB565: 64 read + 32 write beats/line x 48 = 4608
    #   blend ARGB8888 over RGB565 -> RGB565: (64 + 32 + 32) x 48 = 6144
    t = [rd(chip, sym("t_done") + 4 * k) for k in range(3)]
    for got, want in zip(t, (1536, 4608, 6144)):
        assert want <= got <= want + 100, t
