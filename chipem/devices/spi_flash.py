"""Board-level SPI NOR flash attached to a DSPI model (JEDEC-style command set, e.g. Micron M25P16).

A transaction starts when chip select is asserted and ends when it is released.  Chip select is either a pin
(`cs="PTC4"`, active low, watched through PinBoard input listeners; works for GPIO-driven CS) or, without a pin,
the DSPI frame stream itself: a PUSHR entry with CONT=0 (PCS released after the frame) or EOQ ends the
transaction.  Commands: 0x9F RDID, 0x90 REMS, 0xAB RES (release from deep power-down + electronic signature),
0x05 RDSR, 0x01 WRSR, 0x06 WREN, 0x04 WRDI, 0x03 READ, 0x0B FAST_READ, 0x02 PP (page program, bits 1->0,
wraps within the 256-byte page), 0x20 4 KB / 0x52 32 KB / 0xD8 64 KB sector erase, 0x60/0xC7 chip erase,
0xB9 deep power-down.  Program/erase need WEL (set by WREN, cleared at the end of the operation) and complete
instantly (WIP always reads 0).  Unknown commands return 0xFF.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

KNOWN = {0x202015: 2 << 20, 0x202016: 4 << 20, 0x202017: 8 << 20, 0x208015: 2 << 20, 0xBF2541: 2 << 20,
         0xEF4015: 2 << 20, 0xEF4016: 4 << 20, 0xEF4017: 8 << 20, 0x1F4501: 1 << 20, 0x1F4601: 2 << 20}


def _parse_size(v: Any) -> int:
    if isinstance(v, int):
        return v
    s = str(v).strip().upper()
    mult = 1
    for suf, m in (("K", 1 << 10), ("M", 1 << 20)):
        if s.endswith(suf + "B"):
            s = s[:-2]
            mult = m
        elif s.endswith(suf):
            s = s[:-1]
            mult = m
    return int(s, 0) * mult


class SpiNorFlash:
    def __init__(self, jedec: int = 0x202015, size: int | str | None = None, *, image: str | Path | None = None,
                 name: str = "spi_flash"):
        self.name = name
        self.jedec = int(jedec) & 0xFFFFFF
        self.size = _parse_size(size) if size is not None else KNOWN.get(self.jedec, 2 << 20)
        self.mem = bytearray(b"\xff" * self.size)
        if image is not None:
            data = Path(image).read_bytes()[: self.size]
            self.mem[: len(data)] = data
        self.wel = False
        self.sr = 0
        self.powered_down = False
        self._cmd: int | None = None
        self._buf = bytearray()
        self._addr = 0
        self.selected = False
        self.ops: list[tuple[str, int]] = []      # (op, address) history of program/erase operations

    # ------------------------------------------------------------------ attach to a chip
    def attach(self, chip: Any, spi: str = "SPI0", cs: str | None = None) -> "SpiNorFlash":
        blk = chip.peripherals[spi]
        blk.responder = self.transfer
        if cs is None:
            blk.frame_end = self._frame_end
        else:
            board = chip.pins
            pin = board.get(cs)
            board.input_listeners[pin.id].append(self._cs_changed)
            self.selected = pin.level() == "L"
        return self

    def _cs_changed(self, pin: Any) -> None:
        sel = pin.level() == "L"
        if sel and not self.selected:
            self._begin()
        elif not sel and self.selected:
            self._end()
        self.selected = sel

    def _frame_end(self, cont: bool, eoq: bool) -> None:
        if not cont or eoq:
            self._end()

    # ------------------------------------------------------------------ protocol
    def _begin(self) -> None:
        self._cmd = None
        self._buf.clear()

    def _end(self) -> None:
        cmd, n = self._cmd, len(self._buf)
        if cmd is not None and self.wel:
            a = self._addr_from_buf() % self.size if n >= 3 else 0
            span = {0x20: 0x1000, 0x52: 0x8000, 0xD8: 0x10000}.get(cmd)
            if span is not None and n >= 3:
                a -= a % span
                self.mem[a:a + span] = b"\xff" * span
                self.ops.append((f"erase{span // 1024}k", a))
                self.wel = False
            elif cmd in (0x60, 0xC7):
                self.mem[:] = b"\xff" * self.size
                self.ops.append(("chip_erase", 0))
                self.wel = False
            elif cmd == 0x02 and n >= 3:
                self.ops.append(("program", a))
                self.wel = False
            elif cmd == 0x01:
                self.sr = self._buf[0] & 0x9C if self._buf else self.sr
                self.wel = False
        self._cmd = None
        self._buf.clear()

    def _addr_from_buf(self) -> int:
        b = self._buf
        return (b[0] << 16) | (b[1] << 8) | b[2]

    def transfer(self, tx: int, pcs: int = 0, bits: int = 8) -> int:
        tx &= 0xFF
        if self._cmd is None:
            self._cmd = tx
            self._buf.clear()
            if tx == 0x06:
                self.wel = True
            elif tx == 0x04:
                self.wel = False
            elif tx == 0xB9:
                self.powered_down = True
            elif tx == 0xAB:
                self.powered_down = False
            return 0xFF
        cmd = self._cmd
        k = len(self._buf)
        self._buf.append(tx)
        if self.powered_down and cmd != 0xAB:
            return 0xFF
        if cmd == 0x9F:
            ident = self.jedec.to_bytes(3, "big")
            return ident[k] if k < 3 else 0x00
        if cmd == 0x05:
            return self.sr | (0x02 if self.wel else 0)
        if cmd == 0xAB:
            return (self.jedec & 0xFF) - 1 if k >= 3 else 0xFF
        if cmd == 0x90:
            if k < 3:
                return 0xFF
            return (self.jedec >> 16) & 0xFF if (k - 3) % 2 == 0 else (self.jedec & 0xFF) - 1
        if cmd in (0x03, 0x0B):
            hdr = 3 if cmd == 0x03 else 4
            if k < hdr:
                if k == 2:
                    self._addr = self._addr_from_buf()
                return 0xFF
            v = self.mem[self._addr % self.size]
            self._addr += 1
            return v
        if cmd == 0x02:
            if k == 2:
                self._addr = self._addr_from_buf()
            elif k >= 3 and self.wel:
                a = self._addr % self.size
                self.mem[a] &= tx
                self._addr = (self._addr & ~0xFF) | ((self._addr + 1) & 0xFF)
            return 0xFF
        return 0xFF


def parse_spec(spec: str) -> tuple[str, dict]:
    """"SPI0:cs=PTC4,jedec=0x202015,size=2M,file=flash.bin" -> ("SPI0", {...})."""
    spi, _, rest = spec.partition(":")
    opts: dict[str, Any] = {}
    for part in filter(None, (p.strip() for p in rest.split(","))):
        k, _, v = part.partition("=")
        opts[k.strip().lower()] = v.strip()
    return spi.strip() or "SPI0", opts


def attach_from_spec(chip: Any, spec: str) -> SpiNorFlash:
    spi, o = parse_spec(spec)
    dev = SpiNorFlash(int(o.get("jedec", "0x202015"), 0), o.get("size"), image=o.get("file"),
                      name=f"{spi}_flash")
    return dev.attach(chip, spi, o.get("cs"))
