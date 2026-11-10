"""STM32F4 CRC calculation unit (RM0090 ch. 4).

Registers from the SVD: DR (+0), IDR (+4), CR (+8).
* Fixed CRC-32 polynomial 0x04C11DB7, no reflection: every write to DR feeds one 32-bit word, MSB first, into the
  CRC register (4.2: "the CRC calculator ... computes the CRC of the whole 32-bit data word"); reading DR returns the
  CRC register. This is CRC-32/MPEG-2 over the word stream, e.g. one word 0x12345678 from reset -> 0xDF8A8A2B, which
  is what the STM32 HAL `HAL_CRC_Calculate()` returns for a uint32_t buffer {0x12345678}.
* CR.RESET (bit 0, write 1) loads 0xFFFFFFFF into DR; CR reads 0. IDR is an 8-bit scratch register that RESET does
  not touch.
* The F4 unit has no byte enables: an 8/16-bit write to DR is fed as a 32-bit word (the access value zero-extended)
  and logs one `warn`.
The calculation is instantaneous (4.2/4.3: 4 HCLK cycles during which the DR write is stalled, so back-to-back
writes and a write-then-read need no polling; bus stall cycles are not modelled). Clock gate:
RCC_AHB1ENR.CRCEN (a gated-off access logs one `warn`); RCC_AHB1RSTR resets the unit.
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ._util import GatedBlock, build_regs

POLY = 0x04C11DB7


def _table() -> list[int]:
    t = []
    for i in range(256):
        c = i << 24
        for _ in range(8):
            c = ((c << 1) ^ POLY) & 0xFFFFFFFF if c & 0x80000000 else (c << 1) & 0xFFFFFFFF
        t.append(c)
    return t


_T = _table()


def crc32_word(crc: int, word: int) -> int:
    """One STM32 CRC step: the 32-bit `word` fed MSB first into the CRC register `crc`."""
    for sh in (24, 16, 8, 0):
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _T[((crc >> 24) ^ (word >> sh)) & 0xFF]
    return crc


def crc32_words(words, crc: int = 0xFFFFFFFF) -> int:
    for w in words:
        crc = crc32_word(crc, w)
    return crc


@register("stm32.crc")
class CRC(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = "CRC"
        ov = {"DR": {"reset": 0xFFFFFFFF, "on_write": self._dr_write},
              "CR": {"on_write": self._cr_write, "on_read": lambda b, r: 0}}
        r = build_regs((name, "CRC"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self._dr_off = self.regs["DR"].offset
        self._warned_narrow = False
        self.words = 0

    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()
        if offset == self._dr_off and size != 4:
            if not self._warned_narrow:
                self._warned_narrow = True
                self.emit("warn", block=self.name, msg=f"{self.name}: {8 * size}-bit write to CRC_DR: the F4 CRC unit "
                          f"takes 32-bit words only (fed as 0x{value:08x})")
            MMIOBlock.write(self, offset, 4, value & ((1 << (8 * size)) - 1))
            return
        MMIOBlock.write(self, offset, size, value)

    def _dr_write(self, blk, reg, old, new):
        self.words += 1
        return crc32_word(old, new)

    def _cr_write(self, blk, reg, old, new):
        if new & 1:
            self.set("DR", 0xFFFFFFFF)
        return 0
