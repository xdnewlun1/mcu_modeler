"""Peripheral-owned pins: per-family signal tables (chipem/periph/<family>/signals.py) give direction and idle drive."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from chipem.board import Board
from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.firmware import FirmwareImage
from chipem.periph.kinetis.signals import TABLE as KTAB
from chipem.periph.signals import ANALOG, IN, NONE, OD, OUT_H, OUT_L
from chipem.periph.stm32.signals import TABLE as STAB
from chipem.periph.ti_c2000.signals import TABLE as CTAB
from engine_util import asm

from c28x_fake import make_fake_chip

ROOT = Path(__file__).resolve().parent.parent
SIM_SCGC5 = 0x40048038
PORTA, PORTC, PORTE = 0x40049000, 0x4004B000, 0x4004D000
GPIOC = 0x400FF080
RCC, GPIOA = 0x40023800, 0x40020000


@pytest.mark.parametrize("table, fn, role", [
    (KTAB, "UART0_TX", OUT_H), (KTAB, "UART1_RX", IN), (KTAB, "SPI0_SCK", OUT_L), (KTAB, "SPI0_SOUT", OUT_L),
    (KTAB, "SPI0_SIN", IN), (KTAB, "SPI1_PCS0", OUT_H), (KTAB, "I2C0_SDA", OD), (KTAB, "CAN0_TX", OUT_H),
    (KTAB, "CAN0_RX", IN), (KTAB, "FTM0_CH5", OUT_L), (KTAB, "CLKOUT", OUT_L), (KTAB, "RTC_CLKOUT", OUT_L),
    (KTAB, "JTAG_TCLK/SWD_CLK", IN), (KTAB, "JTAG_TDO/TRACE_SWO", OUT_H), (KTAB, "ADC0_SE14", ANALOG),
    (KTAB, "SDHC0_D1", IN), (KTAB, "I2S0_TX_FS", IN), (KTAB, "I2S0_TXD1", OUT_L),
    (STAB, "USART1_TX", OUT_H), (STAB, "UART4_RX", IN), (STAB, "SPI1_MOSI/I2S1_SD", OUT_L), (STAB, "SPI1_MISO", IN),
    (STAB, "SPI1_NSS/I2S1_WS", OUT_H), (STAB, "I2C1_SCL", OD), (STAB, "TIM2_CH1/TIM2_ETR", OUT_L),
    (STAB, "TIM1_CH1N", OUT_L), (STAB, "MCO1", OUT_L), (STAB, "JTMS-SWDIO", IN), (STAB, "JTDO/TRACESWO", OUT_H),
    (STAB, "FSMC_D0", NONE),
    (CTAB, "SCITXDA", OUT_H), (CTAB, "SCIRXDB", IN), (CTAB, "SPICLKA", OUT_L), (CTAB, "SPISIMOA", OUT_L),
    (CTAB, "SPISOMIA", IN), (CTAB, "SPISTEA", OUT_H), (CTAB, "SDAA", OD), (CTAB, "CANTXB", OUT_H),
    (CTAB, "EPWM3B", OUT_L), (CTAB, "XCLKOUT", OUT_L), (CTAB, "XZCS6", NONE),
])
def test_signal_tables(table, fn, role):
    assert table.role(fn) == role


# ------------------------------------------------------------------ Kinetis
@pytest.fixture
def k22():
    return Chip(load_chip(ROOT / "chips" / "mk22fn1m0vll12.yaml"))


def test_kinetis_uart_pins_and_jtag_at_reset(k22):
    pins = k22.pins
    assert pins.get("PTA0").owner.startswith("JTAG_TCLK") and pins.get("PTA0").direction == "in"
    tdo = pins.get("PTA2")
    assert tdo.owner.startswith("JTAG_TDO") and tdo.direction == "out" and tdo.level() == "H"
    k22.bus.write(SIM_SCGC5, 4, k22.bus.read(SIM_SCGC5, 4) | (0x1F << 9))
    k22.bus.write(PORTA + 4 * 2, 4, 0x200)                # PTA2 ALT2 = UART0_TX
    k22.bus.write(PORTA + 4 * 1, 4, 0x203)                # PTA1 ALT2 = UART0_RX, pull-up
    tx, rx = pins.get("PTA2"), pins.get("PTA1")
    assert (tx.owner, tx.direction, tx.drive, tx.level()) == ("UART0_TX", "out", "H", "H")
    assert (rx.owner, rx.direction, rx.level()) == ("UART0_RX", "in", "H")
    k22.bus.write(PORTA + 4 * 1, 4, 0x200)                # pull off: an unconnected RX floats
    assert rx.level() == "X"
    k22.bus.write(PORTE + 4 * 0, 4, 0x600)                # PTE0 ALT6 = I2C1_SDA (open-drain, no pull): X
    sda = pins.get("PTE0")
    assert sda.direction == "in" and sda.level() == "X"
    k22.bus.write(PORTE + 4 * 0, 4, 0x603)                # pulled up: idles H
    assert sda.level() == "H"


def test_kinetis_pdir_no_floating_warning_for_peripheral_pins(k22):
    k22.bus.write(SIM_SCGC5, 4, k22.bus.read(SIM_SCGC5, 4) | (0x1F << 9))
    k22.bus.write(PORTC + 4 * 3, 4, 0x300)                # PTC3 ALT3 = UART1_RX, no pull (floating)
    k22.bus.write(PORTC + 4 * 4, 4, 0x300)                # PTC4 ALT3 = UART1_TX
    assert k22.bus.read(GPIOC + 0x10, 4) == 0             # not GPIO-muxed: PDIR reads 0 for both
    assert not [e for e in k22.log if e.kind == "warn" and "floating" in str(e.fields.get("msg"))]


# ------------------------------------------------------------------ STM32
def test_stm32_af_pins_and_debug_pins():
    desc = load_chip(ROOT / "chips" / "stm32f405zg.yaml")
    chip = Chip(desc)
    code = asm("spin: b spin", 0x08000200)
    img = bytearray(0x200 + len(code))
    img[0:8] = struct.pack("<II", 0x20020000, 0x08000201)
    img[0x200:] = code
    chip.load_firmware(FirmwareImage([(0x08000000, bytes(img))]))
    chip.reset()
    pins = chip.pins
    assert pins.get("PA13").direction == "in" and pins.get("PB3").direction == "out"     # SWDIO in, SWO out (H)
    chip.bus.write(RCC + 0x30, 4, 1)                      # GPIOAEN
    moder = chip.bus.read(GPIOA, 4)
    moder = (moder & ~(0xF << 18)) | (0xA << 18)          # PA9/PA10 alternate function
    chip.bus.write(GPIOA + 0x24, 4, 0x770)                # AFRH: PA9/PA10 AF7 (USART1_TX/RX)
    chip.bus.write(GPIOA, 4, moder)
    tx, rx = pins.get("PA9"), pins.get("PA10")
    assert (tx.owner, tx.direction, tx.level()) == ("USART1_TX", "out", "H")
    assert (rx.owner, rx.direction, rx.level()) == ("USART1_RX", "in", "X")
    chip.bus.write(GPIOA + 0x0C, 4, chip.bus.read(GPIOA + 0x0C, 4) | (1 << 20))   # PUPDR PA10 pull-up
    assert rx.level() == "H"
    assert chip.bus.read(GPIOA + 0x10, 4) >> 9 & 3 == 3   # IDR reads the AF pins (TX idles H, RX pulled up)


# ------------------------------------------------------------------ C2000
def test_c2000_sci_and_i2c_pins():
    c = make_fake_chip(load_chip(ROOT / "chips" / "tms320f28232.yaml"))
    c.reset()
    c.core.regs["st1"] |= 1 << 6                          # EALLOW
    c.bus.write(0x7029, 1, 0x0068)                        # watchdog off
    c.bus.write(0x6F88, 2, (1 << 24) | (1 << 26))         # GPIO28/29 -> SCIRXDA / SCITXDA
    rx, tx = c.pins.get("GPIO28"), c.pins.get("GPIO29")
    assert (tx.direction, tx.drive) == ("out", "H")
    assert rx.direction == "in" and rx.level() == "H"     # GPIO28 has its pull-up enabled out of reset
    c.bus.write(0x6F88, 2, 0x5 << 0)                      # GPAMUX2: GPIO16/17 -> MUX1 = SPISIMOA / SPISOMIA
    assert c.pins.get("GPIO16").direction == "in"         # SPI-A resets as a slave with TALK = 0 (live role)
    c.bus.write(0x7041, 1, 0x6)                           # SPICTL: master, TALK -> SIMO drives (idle L)
    assert c.pins.get("GPIO16").direction == "out" and c.pins.get("GPIO16").drive == "L"
    assert c.pins.get("GPIO17").direction == "in"


# ------------------------------------------------------------------ nets
def test_net_tx_output_drives_rx_input_high():
    desc = load_chip(ROOT / "chips" / "mk22fn1m0vll12.yaml")
    a, b = Chip(desc), Chip(desc)
    board = Board({"A": a, "B": b})
    board.add_net("A_TX_B_RX", [("A", "PTA2"), ("B", "PTA1")])
    for ch in (a, b):
        ch.bus.write(SIM_SCGC5, 4, ch.bus.read(SIM_SCGC5, 4) | (0x1F << 9))
    b.bus.write(PORTA + 4 * 1, 4, 0x200)                  # B.PTA1 = UART0_RX (no pull)
    a.bus.write(PORTA + 4 * 2, 4, 0x200)                  # A.PTA2 = UART0_TX: idles H and drives the net
    net = board.net_of("A", "PTA2")
    assert net.level == "H" and not net.conflict
    assert b.pins.get("PTA1").level() == "H"
