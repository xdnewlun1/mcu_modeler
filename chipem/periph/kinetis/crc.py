"""Kinetis CRC (K22P100M120SF5V2RM ch. 32): programmable 16/32-bit CRC engine.

Registers (SVD table): DATA (+0, with the 8/16-bit views DATAL/DATALL/DATALU/DATAH/DATAHL/DATAHU), GPOLY (+4, views
GPOLYL/GPOLYH/...), CTRL (+8: TOT 31-30, TOTR 29-28, FXOR 26, WAS 25, TCRC 24; CTRLHU = its top byte at +0xB).

Behaviour (32.2/32.3):
* CTRL.WAS=1: a write to DATA is a seed: it replaces the CRC register (16-bit mode: only DATA[15:0] is used).
* CTRL.WAS=0: a write to DATA is data. The access is first transposed per CTRL.TOT (00 none, 01 bits in each byte,
  10 bits and bytes, 11 bytes only), within the bytes the access covers (an 8/16-bit access is transposed "with zeros
  on the unused bytes, the CRC calculated on the valid bytes only", 32.3.3.1 note), then its bytes are fed to the
  engine most-significant byte first (32.2.1 "MSB of data value written first"): a 32-bit write processes 4 bytes,
  a 16-bit write 2, an 8-bit write 1 (at any of the four byte lanes). The engine is the plain (non-reflected) MSB-
  first shift register with polynomial GPOLY (16-bit: GPOLY[15:0], 32-bit: all 32 bits). Seed writes are not
  transposed: 32.3.2.1 step 5 puts a 16-bit seed in DATA[LU:LL] whatever TOT is (a byte transpose would move it to
  HU:HL), and KSDK-style drivers write the seed with TOT already programmed.
* A read of DATA returns the CRC register transposed per CTRL.TOTR and complemented when CTRL.FXOR (32.3.4). In
  16-bit mode the result is in DATA[15:0] (TOTR 10/11 move it to DATA[31:16], as the 32.3.3.1 note says); the
  register's upper half (RM: "indeterminate") keeps its last 32-bit-mode / reset value (0xFFFF) and is not
  touched by 16-bit seeds or data.
* GPOLY[31:16] (HIGH) is not writable in 16-bit mode (32.2.2); GPOLY resets to 0x1021, DATA to 0xFFFFFFFF.
* CRC-32 (IEEE 802.3, the RM/KSDK setting): GPOLY=0x04C11DB7, TCRC=1, TOT=10, TOTR=10, FXOR=1, seed 0xFFFFFFFF ->
  "123456789" gives 0xCBF43926. CRC-16/CCITT-FALSE: GPOLY=0x1021, TCRC=0, TOT=TOTR=0, FXOR=0, seed 0xFFFF -> 0x29B1.

The engine computes instantly (the RM: 2 clocks per byte, far below an instruction's bus access). Clock gate:
SIM_SCGC6[CRC] (a gated-off access logs one `warn`).
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ..base import MMIOBlock
from ._util import GatedBlock, build_regs

TCRC, WAS, FXOR = 1 << 24, 1 << 25, 1 << 26
CTRL_MASK = 0xF7000000                      # TOT, TOTR, FXOR, WAS, TCRC (bit 27 and 23-0 reserved, read 0)

_REV8 = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))


def transpose(value: int, tot: int, nbytes: int = 4) -> int:
    """CRC transpose type `tot` (0..3) of an `nbytes`-byte value (32.3.3.1)."""
    b = value.to_bytes(nbytes, "little")
    if tot == 1:
        b = bytes(_REV8[x] for x in b)
    elif tot == 2:
        b = bytes(_REV8[x] for x in reversed(b))
    elif tot == 3:
        b = bytes(reversed(b))
    return int.from_bytes(b, "little")


@register("kinetis.crc")
class CRC(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = params.get("gate", "CRC")
        r = build_regs((name, "CRC"), size, {}, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.crc = 0xFFFFFFFF
        self.poly = 0x1021
        self.ctrl = 0
        self.bytes_in = 0                    # statistics: data bytes fed to the engine

    def reset(self) -> None:
        super().reset()
        self.crc = 0xFFFFFFFF
        self.poly = 0x1021
        self.ctrl = 0

    # ------------------------------------------------------------ engine
    def _width(self) -> int:
        return 32 if self.ctrl & TCRC else 16

    def feed(self, data: bytes) -> None:
        """Feed raw bytes (already transposed) to the engine, first byte first."""
        w = self._width()
        top = 1 << (w - 1)
        mask = (1 << w) - 1
        poly = self.poly & mask
        crc = self.crc & mask
        for byte in data:
            crc ^= byte << (w - 8)
            for _ in range(8):
                crc = ((crc << 1) ^ poly) & mask if crc & top else (crc << 1) & mask
        self.crc = crc if w == 32 else (self.crc & 0xFFFF0000) | crc
        self.bytes_in += len(data)

    def result(self) -> int:
        """The value a 32-bit read of DATA returns (TOTR transpose, FXOR complement)."""
        v = transpose(self.crc, (self.ctrl >> 28) & 3)
        if self.ctrl & FXOR:
            v ^= 0xFFFFFFFF
        return v

    # ------------------------------------------------------------ bus
    def read(self, offset: int, size: int) -> int:
        if self._gate_loc is not None:
            self._gate_ok()
        if offset < 4:
            v = self.result()
        elif offset < 8:
            v = self.poly
        elif offset < 12:
            v = self.ctrl
        else:
            return MMIOBlock.read(self, offset, size)
        v = (v >> (8 * (offset & 3))) & ((1 << (8 * size)) - 1)
        if self.log_all:
            self.emit("mmio", block=self.name, addr=self.base + offset, size=size, access="r", value=v)
        return v

    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()
        if self.log_all:
            self.emit("mmio", block=self.name, addr=self.base + offset, size=size, access="w", value=value)
        if offset >= 12:
            MMIOBlock.write(self, offset, size, value)
            return
        lane = offset & 3
        size = min(size, 4 - lane)
        value &= (1 << (8 * size)) - 1
        if offset < 4:
            self._data_write(lane, size, value)
        elif offset < 8:
            sh = 8 * lane
            m = ((1 << (8 * size)) - 1) << sh
            if not self.ctrl & TCRC:
                m &= 0xFFFF                     # GPOLY[HIGH] not writable in 16-bit mode
            self.poly = (self.poly & ~m) | ((value << sh) & m)
        else:
            sh = 8 * lane
            m = ((1 << (8 * size)) - 1) << sh
            self.ctrl = ((self.ctrl & ~m) | ((value << sh) & m)) & CTRL_MASK
        self._mirror()

    def _data_write(self, lane: int, size: int, value: int) -> None:
        if self.ctrl & WAS:
            sh = 8 * lane
            m = ((1 << (8 * size)) - 1) << sh
            if self.ctrl & TCRC:
                self.crc = (self.crc & ~m) | ((value << sh) & m)
            else:
                m &= 0xFFFF                     # 16-bit mode: the seed is DATA[LU:LL] only
                self.crc = (self.crc & ~m) | ((value << sh) & m)
            return
        v = transpose(value, (self.ctrl >> 30) & 3, size)
        self.feed(v.to_bytes(size, "big"))           # most-significant byte of the access first

    def _mirror(self) -> None:
        """Keep the register file in step (so `get()` / dumps show the live values)."""
        for n, v in (("DATA", self.result()), ("GPOLY", self.poly), ("CTRL", self.ctrl)):
            if n in self.regs:
                self.set(n, v)

