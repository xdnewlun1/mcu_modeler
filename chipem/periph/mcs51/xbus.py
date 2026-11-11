"""80C51 external data bus as seen by MOVX (XDATA space, tagged 0x10000-0x1FFFF): P0 = multiplexed A0-A7 / D0-D7
latched by ALE, P2 = A8-A15 (MOVX @DPTR; MOVX @Ri leaves the P2 latch on P2), P3.6 WR / P3.7 RD strobes
(data sheet Table 3, sec. 6.2.1 Table 7: with AUXR.EXTRAM = 0 MOVX below 0300H stays on chip and asserts no
strobe).

A board `sram` device (chipem/devices/sram.py, `devices: [{type: sram, chip: A, bus: XBUS, size: 32K, width: 8}]`)
attaches with `attach_memory`: reads and writes of the bus reach it (location = address modulo its size: upper
address lines not connected).  Without a device the bus floats: reads return FFH (a pulled-up P0) and writes are
lost; one `info` the first time.  The cycle itself is not shown on the pins (P0/P2/RD/WR/ALE are not toggled per
access: the core performs MOVX in one step); with --log-mmio every access is logged as an `mmio` event.
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock
from ..registry import register


@register("mcs51.xbus")
class XBus(MMIOBlock):
    model = "mcs51.xbus"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, (), ram_like=False, log=log, params=params)
        self.devices: list[Any] = []
        self.reads = self.writes = 0
        self._noted = False

    def attach_memory(self, dev: Any) -> None:
        self.devices.append(dev)

    def reset(self) -> None:
        super().reset()

    def _float(self) -> None:
        if not self._noted:
            self._noted = True
            self.emit("info", block=self.name, msg=f"{self.name}: MOVX to the external bus with no memory attached "
                      "(reads FFH, writes are lost)")

    def read(self, offset: int, size: int) -> int:
        self.reads += 1
        v = 0
        for i in range(size):
            if self.devices:
                d = self.devices[0]
                b = d.read((offset + i) % d.size) & 0xFF
            else:
                self._float()
                b = 0xFF
            v |= b << (8 * i)
        if self.log_all:
            self.emit("mmio", block=self.name, addr=self.base + offset, reg=None, size=size, access="r", value=v)
        return v

    def write(self, offset: int, size: int, value: int) -> None:
        self.writes += 1
        if not self.devices:
            self._float()
        for i in range(size):
            for d in self.devices:
                d.write((offset + i) % d.size, (value >> (8 * i)) & 0xFF, 0xFF)
        if self.log_all:
            self.emit("mmio", block=self.name, addr=self.base + offset, reg=None, size=size, access="w", value=value)
