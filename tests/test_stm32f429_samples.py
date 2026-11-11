"""STM32F429ZI end to end on boards/stm32f429zi_disco_lcd.yaml (SDRAM on FMC bank 2 + 240x320 lcd_panel part): the
bare-metal sample firmware/samples_stm32f429/ltdc_sdram.c (layer 1 RGB565 in SDRAM, layer 2 L8/CLUT/colour key with
constant alpha, VBR reloads, line interrupt) and the STM32CubeF4 HAL sample firmware/samples_stm32_hal_f429 (HAL
SDRAM + DMA2D + LTDC). Checks UART text, IRQ counts, the frame period on PG13 and on the panel, and panel pixels."""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from chipem.board import load_board
from chipem.session import Session

ROOT = Path(__file__).resolve().parent.parent
BOARD = ROOT / "boards" / "stm32f429zi_disco_lcd.yaml"
ELF = ROOT / "firmware" / "samples_stm32f429" / "build" / "ltdc_sdram.elf"
HAL_ELF = ROOT / "firmware" / "samples_stm32_hal_f429" / "build" / "ltdc_sdram_hal.elf"
FRAME_NS = 280 * 328 / 6e6 * 1e9                     # 91840 pixel clocks at 6 MHz = 15.307 ms
BARS = [(255, 255, 255), (255, 255, 0), (0, 255, 255), (0, 255, 0), (255, 0, 255), (255, 0, 0), (0, 0, 255), (0, 0, 0)]


def session(elf: Path | None = None) -> Session:
    bd = load_board(BOARD)
    if elf is not None:
        bd = dataclasses.replace(bd, chips=[dataclasses.replace(bd.chips[0], firmware=str(elf))])
    return Session.from_board(bd)


def six(c):                                          # the panel resolves 6 bits per channel (bits: 6)
    return tuple(v & 0xFC for v in c)


@pytest.fixture(scope="module")
def run():
    if not ELF.exists():
        pytest.skip("firmware/samples_stm32f429/build.sh not run")
    s = session()
    s.run_for(250e6)
    return s


def test_uart_and_irqs(run):
    chip = next(iter(run.ctxs.values())).chip
    assert bytes(chip.peripherals["USART1"].output) == b"LTDC ON\r\nLTDC frames 12 reloads 8 x 96\r\n"
    lt = chip.peripherals["LTDC"]
    assert lt.frames >= 15 and lt.pixel_clock_hz() == pytest.approx(6e6)
    assert lt.frame_hz() == pytest.approx(1e9 / FRAME_NS)
    assert not [e for e in chip.log if e.kind == "fault"]
    assert not [e for e in chip.log if e.kind == "warn" and "LTDC" in str(e.fields.get("msg", ""))]


def test_frame_period_on_led_and_panel(run):
    chip = next(iter(run.ctxs.values())).chip
    t = [e.t_ns for e in chip.log if e.kind == "pin" and e.fields.get("pin") == "PG13" and e.fields.get("field") == "level"]
    assert len(t) >= 10
    for a, b in zip(t[1:], t[2:]):
        assert b - a == pytest.approx(FRAME_NS, abs=200)
    st = run.board.parts["LCD"].state()
    assert st["fps"] == pytest.approx(1e9 / FRAME_NS, rel=1e-6) and st["bad"] == 0 and st["frames"] >= 15


def test_panel_image(run):
    lcd = run.board.parts["LCD"]
    bg = six((0x20, 0x30, 0x40))
    for x in (0, 100, 239):
        for y in (0, 7):
            assert lcd.pixel(x, y) == bg                 # lines 0..7: background colour (layer 1 window starts at 8)
    for k, c in enumerate(BARS):
        assert lcd.pixel(30 * k + 15, 40) == six(c) and lcd.pixel(30 * k, 300) == six(c)
    # layer 2 moved to x = 96 (88 + 8): L8 index 1 = 0xFF8000 / 2 = 0x0080FF at constant alpha 0x80 over the bars
    def blend(c, under):
        return six(tuple((a * 128 + b * 127) // 255 for a, b in zip(c, under)))
    orange, blue = (0xFF, 0x80, 0x00), (0x00, 0x80, 0xFF)
    for x, y in ((120, 160), (100, 144), (104, 144), (159, 175)):
        lx, ly = x - 96, y - 144
        under = BARS[x // 30]
        if lx < 4:
            assert lcd.pixel(x, y) == six(under)         # index 0 = colour key: transparent
        else:
            c = orange if ((lx // 8 + ly // 8) & 1) else blue
            assert lcd.pixel(x, y) == blend(c, under)
    assert lcd.pixel(95, 150) == six(BARS[3]) and lcd.pixel(160, 150) == six(BARS[5])
    assert lcd.pixel(120, 143) == six(BARS[4]) and lcd.pixel(120, 176) == six(BARS[4])
    assert lcd.frame_ppm().startswith(b"P6\n240 320\n255\n") and len(lcd.frame_rgb()) == 240 * 320 * 3


def test_panel_loses_sync_on_polarity_mismatch_and_unmuxed_data():
    if not ELF.exists():
        pytest.skip("firmware/samples_stm32f429/build.sh not run")
    s = session()
    s.run_for(60e6)
    chip = next(iter(s.ctxs.values())).chip
    lcd = s.board.parts["LCD"]
    good = lcd.frames
    lt = chip.peripherals["LTDC"]
    lt.write(0x18, 4, lt.get("GCR") | 1 << 31)            # HSPOL active high: the panel expects active low
    s.run_for(40e6)
    assert lcd.frames == good and lcd.bad >= 2 and "polarity" in lcd.state()["bad_reason"]
    lt.write(0x18, 4, lt.get("GCR") & ~(1 << 31))
    gpioc = chip.peripherals["GPIOC"]                     # PC10 (LCD_R2) back to input: that data line floats
    gpioc.write(0x00, 4, gpioc.get("MODER") & ~(3 << 20))
    s.run_for(40e6)
    assert lcd.frames > good
    r, g, b = lcd.pixel(15, 40)                           # white bar: R2 missing
    assert (r, g, b) == (0xF8, 0xFC, 0xFC)


def test_hal_sample():
    if not HAL_ELF.exists():
        pytest.skip("firmware/samples_stm32_hal_f429/build.sh not run")
    s = session(HAL_ELF)
    s.run_for(120e6)
    chip = next(iter(s.ctxs.values())).chip
    assert bytes(chip.peripherals["USART1"].output) == b"HAL F429 LTDC sys 180000000 sdram ok\r\nframes 5\r\n"
    lcd = s.board.parts["LCD"]
    assert lcd.state()["bad"] == 0 and lcd.frames >= 5
    assert lcd.pixel(0, 0) == (0xFC, 0, 0) and lcd.pixel(239, 159) == (0xFC, 0, 0)
    assert lcd.pixel(0, 160) == (0, 0, 0xFC) and lcd.pixel(239, 319) == (0, 0, 0xFC)
