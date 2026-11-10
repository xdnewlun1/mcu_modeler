"""24Cxx-style serial EEPROM on the I2C bus (board part `i2c_eeprom`).  Generic glue (no vendor knowledge).

Parameters: `address` (7-bit bus address, default 0x50), `size` in bytes (default 256 = 24C02), `page` (write page
size; default from the size: 8 for <= 256 B, 16 up to 2 KiB, 32 for 4-8 KiB, 64 for 16-32 KiB, 128 above), `twr`
(internal write cycle time, default 5ms), `fill` (erased value, 0xFF), `init` (initial contents from address 0: a
text string, or "hex:48656C6C6F"), `pullup` (SDA/SCL pull-ups on the breakout, default true).

Addressing (as the Atmel/Microchip AT24C family): up to 2 KiB one word-address byte follows the device address, and
the memory blocks above 256 bytes are selected by the low device-address bits (a 24C16 answers 0x50..0x57); above
2 KiB two word-address bytes (MSB first).  Write: the word address, then data bytes into the page buffer (the
address rolls over within the page, as on silicon); the STOP after at least one data byte starts the internal write
cycle: the page is programmed after `twr`, and during that time the device does not acknowledge its address
(acknowledge polling works).  A STOP after the word address only (a "dummy write" before a random read) just sets
the address pointer.  Read: sequential bytes from the pointer, which rolls over at the end of the memory; a read
without a preceding dummy write continues at the current address (the last address accessed + 1).  The master's
NACK ends a read.

State: `mem` (bytearray), `pointer`, `busy` (write cycle running), counters.  `set(data=..., at=0)` / `set(fill=...)`
change the contents; `dump(at, n)` returns bytes.
"""
from __future__ import annotations

from typing import Any

from ._i2c import I2CDevice, parse_int
from .registry import part
from .std import _time

__all__ = ["I2CEeprom"]


def _default_page(size: int) -> int:
    if size <= 256:
        return 8
    if size <= 2048:
        return 16
    if size <= 8192:
        return 32
    if size <= 32768:
        return 64
    return 128


def _init_bytes(v: Any) -> bytes:
    if v in (None, ""):
        return b""
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    s = str(v)
    if s.lower().startswith("hex:"):
        return bytes.fromhex(s[4:].replace(" ", ""))
    return s.encode("latin-1")


@part("i2c_eeprom")
class I2CEeprom(I2CDevice):
    """24Cxx serial EEPROM on `sda`/`scl` (open drain, optional pull-ups): `address` (0x50), `size` in bytes (256;
    above 2 KiB two word-address bytes), `page` write buffer, `twr` write-cycle time (5ms, NACKs its address while
    busy), `init` text or hex:.. contents, `fill` erased value."""
    PARAMS = {"address": 0x50, "size": 256, "page": None, "twr": "5ms", "fill": 0xFF, "init": "", "pullup": True}
    ID_PREFIX = "EE"

    def _setup(self) -> None:
        super()._setup()
        size = parse_int(self.params["size"], f"{self.id}.size")
        if size < 128 or size > 65536 or size & (size - 1):
            raise ValueError(f"part {self.id}: size must be a power of two from 128 to 65536 bytes (got {size})")
        self.size = self.params["size"] = size
        pg = self.params["page"]
        page = _default_page(size) if pg in (None, "", "auto") else parse_int(pg, f"{self.id}.page")
        if page <= 0 or page & (page - 1) or page > size:
            raise ValueError(f"part {self.id}: page must be a power of two <= size (got {page})")
        self.page = page
        self.params["page"] = None if pg in (None, "", "auto") else page
        self.twr_ns = _time(self.params["twr"], f"{self.id}.twr")
        self.fill = parse_int(self.params["fill"], f"{self.id}.fill") & 0xFF
        self.addr_bytes = 2 if size > 2048 else 1
        self.blocks = max(1, size // 256) if self.addr_bytes == 1 else 1     # device-address bits used as A8..A10
        if self.blocks > 1 and self.address & (self.blocks - 1):
            raise ValueError(f"part {self.id}: a {size}-byte EEPROM answers {self.blocks} addresses; "
                             f"address must be a multiple of {self.blocks}")
        init = _init_bytes(self.params["init"])
        if len(init) > size:
            raise ValueError(f"part {self.id}: init has {len(init)} bytes, more than size {size}")
        self._init = init
        if not hasattr(self, "mem"):
            self.mem = bytearray([self.fill]) * size
            self.mem[:len(init)] = init

    # ---------------------------------------------------------------- state
    def _device_reset(self) -> None:
        self.pointer = getattr(self, "pointer", 0)       # the EEPROM array keeps its contents across board resets
        self.busy = False
        self._phase = None          # None | "addr" (word address bytes still due) | "data" | "read"
        self._need = 0
        self._page_buf: dict[int, int] = {}
        self._block = 0
        self.writes = getattr(self, "writes", 0)
        self.reads = getattr(self, "reads", 0)
        self.byte_writes = getattr(self, "byte_writes", 0)

    def matches(self, addr: int) -> bool:
        return (addr & ~(self.blocks - 1)) == self.address

    def state(self) -> dict:
        return {"address": self.params["address"], "size": self.size, "pointer": self.pointer, "busy": self.busy,
                "writes": self.writes, "reads": self.reads, "transfers": self.transfers, "naks": self.naks}

    def dump(self, at: int = 0, n: int = 16) -> bytes:
        return bytes(self.mem[at:at + n])

    def set(self, **values: Any) -> dict:
        at = parse_int(values.pop("at", 0), f"{self.id}.at")
        for k, v in values.items():
            if k == "data":
                b = _init_bytes(v)
                if at + len(b) > self.size:
                    raise ValueError(f"part {self.id}: data does not fit at {at}")
                self.mem[at:at + len(b)] = b
            elif k == "fill":
                self.mem[:] = bytes([parse_int(v, f"{self.id}.fill") & 0xFF]) * self.size
            else:
                raise ValueError(f"part {self.id} (i2c_eeprom): cannot set {k!r} (use data, at, fill)")
        return self.state()

    # ---------------------------------------------------------------- I2C target handler
    def i2c_address(self, addr: int, read: bool) -> bool:
        if not self.matches(addr):
            return False
        if self.busy:
            self.naks += 1
            return False                                  # write cycle in progress: no acknowledge
        self._block = addr & (self.blocks - 1)
        if read:
            self._phase = "read"
        else:
            self._phase = "addr"
            self._need = self.addr_bytes
            self._waddr = 0
            self._page_buf = {}
        return True

    def i2c_rx_byte(self, byte: int) -> bool:
        if self._phase == "addr":
            self._waddr = (self._waddr << 8) | byte
            self._need -= 1
            if self._need == 0:
                a = self._waddr if self.addr_bytes == 2 else (self._block << 8) | self._waddr
                self.pointer = a % self.size
                self._phase = "data"
            return True
        if self._phase == "data":
            base = self.pointer & ~(self.page - 1)
            self._page_buf[self.pointer] = byte
            self.pointer = base | ((self.pointer + 1) & (self.page - 1))     # roll over within the page
            return True
        return False

    def i2c_tx_byte(self) -> int:
        v = self.mem[self.pointer]
        self.pointer = (self.pointer + 1) % self.size
        self.reads += 1
        return v

    def i2c_stop(self) -> None:
        if self._phase == "data" and self._page_buf:
            self._commit = dict(self._page_buf)
            self._page_buf = {}
            self.busy = True
            self.emit(action="write", at=min(self._commit), n=len(self._commit))
            self._in(self.twr_ns, self._write_done)
        self._phase = None

    def i2c_start(self, repeated: bool) -> None:
        super().i2c_start(repeated)
        if self._phase == "data" and self._page_buf:
            self._page_buf = {}                           # a START before STOP aborts the page write
        self._phase = None

    def _write_done(self) -> None:
        for a, v in self._commit.items():
            self.mem[a] = v
        self.writes += 1
        self.byte_writes += len(self._commit)
        self._commit = {}
        self.busy = False
        self.emit(action="write_done")
