"""stm32.ltdc (RM0090 Rev 22 chapter 16, LCD-TFT controller) on chips/stm32f429zi.yaml: reset values (16.7), the pixel
clock from PLLSAI_R / PLLSAIDIVR (6.3.24/6.3.25), the lazy timing generator (CPSR/CDSR, 16.4.1, 16.7.12/16.7.13), the
line interrupt and register reload interrupt (16.5, IRQ 88), shadow registers (IMR/VBR, 16.7.6), layer composition
(pixel formats Table 90, CLUT, colour keying, default colour, windowing, blending 16.7.21, background), FIFO underrun
and transfer error (IRQ 89), the wave summaries of LCD_CLK/HSYNC/VSYNC/DE, RCC reset and gating."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.firmware import FirmwareImage

from engine_util import asm

ROOT = Path(__file__).resolve().parent.parent
YAML = ROOT / "chips" / "stm32f429zi.yaml"
FLASH = 0x08000000
RCC = 0x40023800
L = 0x40016800
SSCR, BPCR, AWCR, TWCR, GCR, SRCR, BCCR, IER, ISR, ICR, LIPCR, CPSR, CDSR = (
    0x08, 0x0C, 0x10, 0x14, 0x18, 0x24, 0x2C, 0x34, 0x38, 0x3C, 0x40, 0x44, 0x48)
LCR, WHPCR, WVPCR, CKCR, PFCR, CACR, DCCR, BFCR, CFBAR, CFBLR, CFBLNR, CLUTWR = (
    0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18, 0x1C, 0x28, 0x2C, 0x30, 0x40)
LIF, FUIF, TERRIF, RRIF = 1, 2, 4, 8
IRQ, IRQ_ER = 88, 89
# test panel: HSYNC 2, HBP 3, 8 active, HFP 3 (16 clocks); VSYNC 1, VBP 2, 4 active, VFP 1 (8 lines)
HSW, AHBP, AAW, TOTALW = 1, 4, 12, 15
VSH, AVBP, AAH, TOTALH = 0, 2, 6, 7
PCLK = 6e6
P_NS = 1e9 / PCLK
FRAME_NS = 16 * 8 * P_NS
FB = 0x20004000


@pytest.fixture(scope="module")
def desc():
    return load_chip(YAML)


def rd(chip, addr):
    return chip.bus.read(addr, 4)


def wr(chip, addr, v):
    chip.bus.write(addr, 4, v)


def r(chip, off):
    return rd(chip, L + off)


def w(chip, off, v):
    wr(chip, L + off, v)


def lw(chip, layer, off, v):
    w(chip, 0x84 + 0x80 * (layer - 1) + off, v)


def lr(chip, layer, off):
    return r(chip, 0x84 + 0x80 * (layer - 1) + off)


def irq(chip, n=IRQ) -> bool:
    return (16 + n) in chip.arch.asserted


def warns(chip):
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "warn"]


def spin_chip(desc, divr=2) -> Chip:
    code = asm("spin: b spin", FLASH + 0x200)
    img = bytearray(0x200 + len(code))
    img[0:8] = struct.pack("<II", 0x20030000, (FLASH + 0x200) | 1)
    img[0x200:] = code
    chip = Chip(desc)
    chip.load_firmware(FirmwareImage([(FLASH, bytes(img))]))
    chip.reset()
    wr(chip, RCC + 0x44, rd(chip, RCC + 0x44) | 1 << 26)          # LTDCEN
    # HSI 16 MHz / PLLM 16 (reset) = 1 MHz x PLLSAIN 192 / PLLSAIR 4 = 48 MHz; PLLSAIDIVR /8 -> 6 MHz
    wr(chip, RCC + 0x88, 192 << 6 | 7 << 24 | 4 << 28)
    wr(chip, RCC + 0x8C, divr << 16)
    wr(chip, RCC, rd(chip, RCC) | 1 << 28)                         # PLLSAION
    return chip


@pytest.fixture
def chip(desc):
    return spin_chip(desc)


def timing(chip):
    w(chip, SSCR, HSW << 16 | VSH)
    w(chip, BPCR, AHBP << 16 | AVBP)
    w(chip, AWCR, AAW << 16 | AAH)
    w(chip, TWCR, TOTALW << 16 | TOTALH)


def ltdc(chip):
    return chip.peripherals["LTDC"]


def layer(chip, n, pf, data, *, x0=0, y0=0, w_=8, h=4, pitch=None, ll=None, lines=None, cr=1, ca=0xFF, bf=0x607,
          addr=FB, dc=0, ck=0):
    """Layer n window at active (x0, y0) of w_ x h pixels reading `data` at `addr` (immediate reload)."""
    bpp = (4, 3, 2, 2, 2, 1, 1, 2)[pf]
    chip.bus.write_bytes(addr, bytes(data))
    lw(chip, n, WHPCR, (AHBP + 1 + x0 + w_ - 1) << 16 | (AHBP + 1 + x0))
    lw(chip, n, WVPCR, (AVBP + 1 + y0 + h - 1) << 16 | (AVBP + 1 + y0))
    lw(chip, n, PFCR, pf)
    lw(chip, n, CACR, ca)
    lw(chip, n, BFCR, bf)
    lw(chip, n, DCCR, dc)
    lw(chip, n, CKCR, ck)
    lw(chip, n, CFBAR, addr)
    lw(chip, n, CFBLR, (pitch if pitch is not None else w_ * bpp) << 16 | ((ll if ll is not None else w_ * bpp) + 3))
    lw(chip, n, CFBLNR, lines if lines is not None else h)
    lw(chip, n, LCR, cr)
    w(chip, SRCR, 1)


# ---------------------------------------------------------------------------------------------------- registers
def test_reset_values_and_reserved_bits(chip):
    assert r(chip, GCR) == 0x2220                  # DRW/DGW/DBW = 2 (16.7.5)
    assert r(chip, CDSR) == 0xF                    # 16.7.13
    assert r(chip, CPSR) == 0
    for n in (1, 2):
        assert lr(chip, n, CACR) == 0xFF           # 16.7.19 (STM32F429.svd says 0)
        assert lr(chip, n, BFCR) == 0x607          # 16.7.21
    w(chip, GCR, 0xFFFFFFFE)
    assert r(chip, GCR) == 0xF0012220              # HSPOL..PCPOL, DEN; dither widths read-only
    w(chip, SSCR, 0xFFFFFFFF)
    assert r(chip, SSCR) == 0x0FFF07FF
    w(chip, IER, 0xFF)
    assert r(chip, IER) == 0xF
    w(chip, ICR, 0xF)
    assert r(chip, ICR) == 0
    lw(chip, 1, CLUTWR, 0x05112233)
    assert lr(chip, 1, CLUTWR) == 0 and ltdc(chip).cluts[0][5] == 0x112233


def test_pixel_clock_from_pllsai(desc):
    for divr, hz in ((0, 24e6), (1, 12e6), (2, 6e6), (3, 3e6)):
        c = spin_chip(desc, divr)
        assert c.peripherals["RCC"].ltdc_clk_hz() == pytest.approx(hz)
        assert ltdc(c).pixel_clock_hz() == pytest.approx(hz)
    c = spin_chip(desc)
    wr(c, RCC, rd(c, RCC) & ~(1 << 28))                              # PLLSAI off
    wr(c, RCC + 0x88, 192 << 6 | 7 << 24 | 1 << 28)                  # PLLSAIR = 1: wrong configuration
    wr(c, RCC, rd(c, RCC) | 1 << 28)
    assert c.peripherals["RCC"].ltdc_clk_hz() == 0.0
    assert any("PLLSAIR = 1" in m for m in warns(c))


def test_ltdc_gate_bit_writable_only_on_f429(desc):
    c = spin_chip(desc)
    assert rd(c, RCC + 0x44) & 1 << 26
    f427 = Chip(load_chip(ROOT / "chips" / "stm32f427zg.yaml"))
    f427.bus.write(RCC + 0x44, 4, 1 << 26)
    assert not f427.bus.read(RCC + 0x44, 4) & 1 << 26              # reserved on the F427/F437


# ---------------------------------------------------------------------------------------------------- timing
def test_timing_generator_position_and_status(chip):
    timing(chip)
    assert r(chip, CPSR) == TOTALW << 16 | TOTALH and r(chip, CDSR) == 0xF   # held while disabled
    w(chip, GCR, 1)
    t0 = chip.now_ns()
    seen = set()
    for _ in range(140):
        chip.run_for(P_NS * 1.37)
        n = int((chip.now_ns() - t0) / P_NS) % 128
        x, y = n % 16, n // 16
        assert r(chip, CPSR) == x << 16 | y
        cd = r(chip, CDSR)
        assert cd == ((x <= HSW) << 3 | (y <= VSH) << 2 | (AHBP < x <= AAW) << 1 | (AVBP < y <= AAH))
        seen.add(cd)
    assert {0b1000, 0b0100, 0b0010, 0b0011, 0b0001, 0} <= seen
    assert ltdc(chip).frame_hz() == pytest.approx(PCLK / 128)
    w(chip, GCR, 0)
    assert r(chip, CPSR) == TOTALW << 16 | TOTALH and r(chip, CDSR) == 0xF


def test_line_interrupt_timing_flag_and_irq(chip):
    timing(chip)
    w(chip, LIPCR, 5)
    w(chip, GCR, 1)
    t0 = chip.now_ns()
    t_line = t0 + 5 * 16 * P_NS
    chip.run_for(t_line - t0 - 2 * P_NS)
    assert not r(chip, ISR) & LIF
    chip.run_for(4 * P_NS)
    assert r(chip, ISR) & LIF and not irq(chip)                     # polled flag without LIE
    w(chip, ICR, LIF)
    assert not r(chip, ISR) & LIF
    w(chip, IER, 1)
    chip.run_for(FRAME_NS)
    assert r(chip, ISR) & LIF and irq(chip)
    w(chip, ICR, LIF)
    assert not irq(chip)
    w(chip, LIPCR, TOTALH + 1)                                       # beyond the frame: never
    chip.run_for(3 * FRAME_NS)
    assert not r(chip, ISR) & LIF


def test_frame_rate_follows_clock_change(chip):
    timing(chip)
    w(chip, GCR, 1)
    lt = ltdc(chip)
    lt.capture = True
    chip.run_for(10 * FRAME_NS + 1)
    assert lt.frames == 10
    wr(chip, RCC + 0x8C, 3 << 16)                                    # /16: 3 MHz (re-based at the current position)
    assert lt.pixel_clock_hz() == pytest.approx(3e6)
    f0 = lt.frames
    chip.run_for(10 * 2 * FRAME_NS)
    assert lt.frames - f0 in (9, 10)


# ---------------------------------------------------------------------------------------------------- shadow registers
def test_shadow_registers_immediate_and_vertical_blanking_reload(chip):
    timing(chip)
    lw(chip, 1, WHPCR, 0x000C0005)
    assert lr(chip, 1, WHPCR) == 0                                   # active value until reloaded
    w(chip, SRCR, 1)
    assert lr(chip, 1, WHPCR) == 0x000C0005 and r(chip, SRCR) == 0   # IMR clears itself
    assert not r(chip, ISR) & RRIF                                   # RRIF only for VBR
    w(chip, IER, 8)
    w(chip, GCR, 1)
    t0 = chip.now_ns()
    lw(chip, 1, WHPCR, 0x000B0006)
    w(chip, SRCR, 2)
    w(chip, SRCR, 0)                                                 # VBR cannot be cleared by software
    assert r(chip, SRCR) == 2 and lr(chip, 1, WHPCR) == 0x000C0005
    chip.run_for((AAH + 1) * 16 * P_NS - 2 * P_NS)
    assert r(chip, SRCR) == 2 and not irq(chip)
    chip.run_for(4 * P_NS)                                           # first line after the active area
    assert r(chip, SRCR) == 0 and lr(chip, 1, WHPCR) == 0x000B0006
    assert r(chip, ISR) & RRIF and irq(chip)
    assert chip.now_ns() - t0 > (AAH + 1) * 16 * P_NS
    w(chip, ICR, RRIF)
    assert not irq(chip)


def test_vbr_waits_while_disabled(chip):
    timing(chip)
    lw(chip, 2, CACR, 0x40)
    w(chip, SRCR, 2)
    chip.run_for(5 * FRAME_NS)
    assert r(chip, SRCR) == 2 and lr(chip, 2, CACR) == 0xFF
    w(chip, GCR, 1)
    chip.run_for(FRAME_NS)
    assert r(chip, SRCR) == 0 and lr(chip, 2, CACR) == 0x40


# ---------------------------------------------------------------------------------------------------- composition
def px(frame, x, y):
    return frame.pixel(x, y)


def test_rgb565_layer_window_background_and_default_colour(chip):
    timing(chip)
    w(chip, BCCR, 0x102030)
    data = struct.pack("<16H", *([0xF800, 0x07E0, 0x001F, 0xFFFF] * 4))      # 4x4 window
    layer(chip, 1, 2, data, x0=2, y0=1, w_=4, h=2, pitch=8, ll=8, lines=2)
    f = ltdc(chip).compose()
    assert (f.width, f.height) == (8, 4)
    assert px(f, 0, 0) == (0x10, 0x20, 0x30)                         # background (default colour transparent)
    assert px(f, 2, 1) == (255, 0, 0) and px(f, 3, 1) == (0, 255, 0) and px(f, 4, 1) == (0, 0, 255)
    assert px(f, 5, 2) == (255, 255, 255)
    assert px(f, 6, 1) == (0x10, 0x20, 0x30) and px(f, 2, 3) == (0x10, 0x20, 0x30)
    lw(chip, 1, DCCR, 0xFF00FF00)                                    # opaque green default colour outside the window
    w(chip, SRCR, 1)
    f = ltdc(chip).compose()
    assert px(f, 0, 0) == (0, 255, 0) and px(f, 2, 1) == (255, 0, 0)


@pytest.mark.parametrize("pf,data,expect", [
    (0, struct.pack("<I", 0xFF123456), (0x12, 0x34, 0x56)),          # ARGB8888
    (1, bytes([0x56, 0x34, 0x12]), (0x12, 0x34, 0x56)),              # RGB888: B, G, R in memory (Table 90)
    (2, struct.pack("<H", 0b10000_100000_00001), (0x84, 0x82, 0x08)),  # RGB565 bit replication (16.4.2)
    (3, struct.pack("<H", 0x8000 | 0b10000 << 10 | 0b00001 << 5 | 0b11111), (0x84, 0x08, 0xFF)),   # ARGB1555
    (4, struct.pack("<H", 0xF4A1), (0x44, 0xAA, 0x11)),              # ARGB4444
    (5, bytes([0x80]), (0x80, 0x80, 0x80)),                          # L8 without CLUT: grey
    (6, bytes([0xF7]), (0x77, 0x77, 0x77)),                          # AL44 without CLUT
    (7, bytes([0x33, 0xFF]), (0x33, 0x33, 0x33)),                    # AL88 without CLUT
])
def test_pixel_formats(chip, pf, data, expect):
    timing(chip)
    layer(chip, 1, pf, data, w_=1, h=1)
    assert px(ltdc(chip).compose(), 0, 0) == expect


def test_clut_formats_and_colour_key(chip):
    timing(chip)
    lw(chip, 1, CLUTWR, 0x01AABBCC)
    lw(chip, 1, CLUTWR, 0x77102030)                                  # AL44 L = 7 -> address 0x77
    layer(chip, 1, 5, bytes([1, 2]), w_=2, h=1, cr=0x11)             # L8 + CLUTEN
    f = ltdc(chip).compose()
    assert px(f, 0, 0) == (0xAA, 0xBB, 0xCC) and px(f, 1, 0) == (0, 0, 0)
    layer(chip, 1, 6, bytes([0xF7]), w_=1, h=1, cr=0x11)             # AL44 + CLUT
    assert px(ltdc(chip).compose(), 0, 0) == (0x10, 0x20, 0x30)
    layer(chip, 1, 7, bytes([0x01, 0xFF]), w_=1, h=1, cr=0x11)       # AL88 + CLUT
    assert px(ltdc(chip).compose(), 0, 0) == (0xAA, 0xBB, 0xCC)
    w(chip, BCCR, 0x0000FF)
    layer(chip, 1, 2, struct.pack("<2H", 0xF800, 0x07E0), w_=2, h=1, cr=0x3, ck=0xFF0000)   # COLKEN: red is keyed
    f = ltdc(chip).compose()
    assert px(f, 0, 0) == (0, 0, 255) and px(f, 1, 0) == (0, 255, 0)


def test_blending_constant_alpha_and_two_layers(chip):
    timing(chip)
    w(chip, BCCR, 0x303030)                                          # Cs = 48
    # 16.7.21 example: BF1 constant alpha, BF2 1 - constant alpha, CA 240, C 128 -> 123
    layer(chip, 1, 1, bytes([128, 128, 128]), w_=1, h=1, ca=240, bf=0x405)
    assert px(ltdc(chip).compose(), 0, 0) == (123, 123, 123)
    # pixel alpha x constant alpha: ARGB8888 alpha 0x80 x CA 0xFF over the background
    layer(chip, 1, 0, struct.pack("<I", 0x80FF0000), w_=1, h=1)
    a = 0x80 * 255 // 255
    assert px(ltdc(chip).compose(), 0, 0) == ((255 * a + 48 * (255 - a)) // 255, 48 * (255 - a) // 255,
                                              48 * (255 - a) // 255)
    # layer 2 (top) half transparent over opaque layer 1
    layer(chip, 1, 0, struct.pack("<I", 0xFF0000FF), w_=1, h=1)
    layer(chip, 2, 0, struct.pack("<I", 0xFFFFFF00), w_=1, h=1, ca=0x40, addr=FB + 0x100)
    assert px(ltdc(chip).compose(), 0, 0) == ((255 * 0x40) // 255, (255 * 0x40) // 255, (255 * 0xBF) // 255)


def test_reserved_blending_factor_warns_and_uses_reset_value(chip):
    timing(chip)
    layer(chip, 1, 1, bytes([50, 100, 200]), w_=1, h=1, bf=0x101)
    f = ltdc(chip).compose()
    assert px(f, 0, 0) == (200, 100, 50) and f.warnings
    assert any("L1BFCR" in m and "reserved" in m for m in warns(chip))


# ---------------------------------------------------------------------------------------------------- errors
def test_fifo_underrun_flag_and_error_irq(chip):
    timing(chip)
    w(chip, IER, 2)
    layer(chip, 1, 2, bytes(64), w_=8, h=4, ll=8, lines=4)           # 8 pixels need 16 bytes per line
    w(chip, GCR, 1)
    chip.run_for((AAH + 1) * 16 * P_NS + 2 * P_NS)
    assert r(chip, ISR) & FUIF and irq(chip, IRQ_ER) and not irq(chip, IRQ)
    assert any("FIFO underrun" in m for m in warns(chip))
    w(chip, ICR, FUIF)
    assert not irq(chip, IRQ_ER)


def test_transfer_error_unmapped_and_ccm(chip):
    timing(chip)
    w(chip, IER, 4)
    w(chip, GCR, 1)
    for addr in (0x30000000, 0x10000000):                            # unmapped; CCM data RAM (CPU only)
        lw(chip, 1, CFBAR, addr)
        lw(chip, 1, WHPCR, AAW << 16 | AHBP + 1)
        lw(chip, 1, WVPCR, AAH << 16 | AVBP + 1)
        lw(chip, 1, CFBLR, 16 << 16 | 19)
        lw(chip, 1, CFBLNR, 4)
        lw(chip, 1, LCR, 1)
        w(chip, SRCR, 1)
        chip.run_for(FRAME_NS)
        assert r(chip, ISR) & TERRIF and irq(chip, IRQ_ER)
        w(chip, ICR, TERRIF)
        assert not irq(chip, IRQ_ER)


# ---------------------------------------------------------------------------------------------------- pins, reset
def test_pin_waves_and_idle_levels(chip):
    timing(chip)
    for port, pin in ((0x40021800, 7), (0x40020800, 6), (0x40020000, 4), (0x40021400, 10)):   # PG7 PC6 PA4 PF10
        wr(chip, RCC + 0x30, rd(chip, RCC + 0x30) | 0x7FF)
        moder = rd(chip, port)
        wr(chip, port, (moder & ~(3 << 2 * pin)) | 2 << 2 * pin)
        reg = port + (0x20 if pin < 8 else 0x24)
        sh = 4 * (pin % 8)
        wr(chip, reg, (rd(chip, reg) & ~(15 << sh)) | 14 << sh)
    pins = chip.pins
    assert pins.get("PC6").level() == "H" and pins.get("PA4").level() == "H"   # active-low sync idles high
    assert pins.get("PF10").level() == "H" and pins.get("PG7").level() == "L"
    w(chip, GCR, 1 << 31 | 1 << 29)                                  # HSPOL, DEPOL active high
    assert pins.get("PC6").level() == "L" and pins.get("PF10").level() == "L"
    w(chip, GCR, 1)
    chip.run_for(120e6)
    w(chip, GCR, 0)
    ev = {e.fields["pin"]: e.fields for e in chip.log if e.kind == "pin" and e.fields.get("field") == "wave"}
    assert ev["PG7"]["freq_hz"] == pytest.approx(PCLK) and ev["PG7"]["duty"] == 50.0
    assert ev["PC6"]["freq_hz"] == pytest.approx(PCLK / 16) and ev["PC6"]["duty"] == pytest.approx(100 * 2 / 16)
    assert ev["PA4"]["freq_hz"] == pytest.approx(PCLK / 128) and ev["PA4"]["duty"] == pytest.approx(100 / 8)
    assert ev["PF10"]["freq_hz"] == pytest.approx(PCLK / 128 * 4)
    total = sum(e.fields["edges"] for e in chip.log if e.kind == "pin" and e.fields.get("field") == "wave"
                and e.fields["pin"] == "PG7")
    assert total == pytest.approx(2 * PCLK * 0.120, rel=1e-3)


def test_rcc_reset_and_chip_reset(chip):
    timing(chip)
    lw(chip, 1, CLUTWR, 0x01AABBCC)
    w(chip, IER, 1)
    w(chip, GCR, 1)
    chip.run_for(FRAME_NS)
    wr(chip, RCC + 0x24, 1 << 26)                                    # APB2RSTR.LTDCRST
    wr(chip, RCC + 0x24, 0)
    assert r(chip, GCR) == 0x2220 and r(chip, IER) == 0 and r(chip, TWCR) == 0
    assert ltdc(chip).cluts[0][1] == 0 and not ltdc(chip)._running
    chip.run_for(3 * FRAME_NS)
    assert r(chip, ISR) == 0


def test_gated_access_warns(desc):
    c = Chip(desc)
    c.reset()
    c.bus.read(L + GCR, 4)
    assert any("LTDC" in m and "clock is disabled" in m for m in warns(c))
