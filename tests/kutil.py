"""Helpers for Kinetis timer/UART model tests: a small chip with selected Kinetis peripherals (no MCG, so the
bus clock is core/2) and keystone-assembled firmware."""
from __future__ import annotations

from chipem.chip import Chip
from chipem.chipdesc import ChipDescription, CoreDesc, MemoryDesc, PeripheralDesc

from engine_util import DATA, FLASH, RAM, build_image

PIT_BASE = 0x40037000
LPTMR_BASE = 0x40040000
RTC_BASE = 0x4003D000
UART0_BASE = 0x4006A000
UART2_BASE = 0x4006C000
NVIC_ISER = 0xE000E100
COUNTER = DATA + 0x80

IRQS = {0: "PIT0", 1: "PIT1", 2: "PIT2", 3: "PIT3", 4: "LPTMR0", 5: "RTC", 6: "RTC_Seconds", 7: "UART0_RX_TX",
        8: "UART2_RX_TX"}
PERIPHS = [
    PeripheralDesc("PIT", "kinetis.pit", PIT_BASE),
    PeripheralDesc("LPTMR0", "kinetis.lptmr", LPTMR_BASE),
    PeripheralDesc("RTC", "kinetis.rtc", RTC_BASE),
    PeripheralDesc("UART0", "kinetis.uart", UART0_BASE, params={"index": 0, "fifo_depth": 8}),
    PeripheralDesc("UART2", "kinetis.uart", UART2_BASE, params={"index": 2}),
]


def kdesc(hz: float = 1_000_000.0) -> ChipDescription:
    return ChipDescription(
        name="KTEST", core=CoreDesc(arch="armv7e-m", cpu="cortex-m4", fpu=True, clock_hz=hz, num_irqs=32),
        family="kinetis",
        memory=[MemoryDesc("FLASH", FLASH, 0x10000, "flash"), MemoryDesc("RAM", RAM, 0x10000, "ram")],
        peripherals=list(PERIPHS), irqs=dict(IRQS), pins=[])


def kchip(code: dict[str, str], vectors: dict[int, str] | None = None, hz: float = 1_000_000.0, **kw):
    chip = Chip(kdesc(hz), **kw)
    img, addrs = build_image(code, vectors=vectors)
    chip.load_firmware(img)
    chip.reset()
    return chip, addrs


def enable_irq(n: int) -> str:
    return f"""
  ldr r0, =0x{NVIC_ISER:08X}
  ldr r1, =0x{1 << n:08X}
  str r1, [r0]
"""


SPIN = """
spin:
  b spin
"""


def count_handler(pre: str = "") -> str:
    """Handler: optional `pre` (e.g. clear a flag), then [COUNTER] += 1."""
    return pre + f"""
  ldr r0, =0x{COUNTER:08X}
  ldr r1, [r0]
  adds r1, #1
  str r1, [r0]
  bx lr
"""


def counter(chip) -> int:
    return chip.bus.read(COUNTER, 4)
