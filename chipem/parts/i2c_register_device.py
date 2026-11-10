"""Generic I2C register-map device (board part `i2c_register_device`): a sensor/expander-style target with 8-bit
register addresses and 8-bit registers.  Generic glue (no vendor knowledge).

Parameters: `address` (7-bit, default 0x48), `regs` (initial register values {reg: value}; registers not listed read
`default`), `readonly` (list of registers that ignore writes), `default` (0x00), `pullup` (true).

Protocol (the common "register pointer" convention): a write transfer's first data byte sets the register pointer,
the following bytes write registers at the pointer, which increments after each byte (wrapping at 0xFF); a read
transfer returns registers from the pointer, incrementing.  Writes to `readonly` registers are acknowledged and
ignored.  `set(reg=.., value=..)` changes a register from the UI / stimulus; `regs` holds the live map.
"""
from __future__ import annotations

from typing import Any

from ._i2c import I2CDevice, parse_int
from .registry import part

__all__ = ["I2CRegisterDevice"]


@part("i2c_register_device")
class I2CRegisterDevice(I2CDevice):
    """Register-map I2C target on `sda`/`scl`: `address` (0x48), `regs` {reg: value} initial map, `readonly` list,
    `default` value of unlisted registers; first written byte = register pointer, auto-increment on read/write."""
    PARAMS = {"address": 0x48, "regs": None, "readonly": None, "default": 0, "pullup": True}
    ID_PREFIX = "DEV"

    def _setup(self) -> None:
        super()._setup()
        raw = self.params["regs"] or {}
        if not isinstance(raw, dict):
            raise ValueError(f"part {self.id}: regs must be a mapping register -> value")
        init = {}
        for k, v in raw.items():
            r = parse_int(k, f"{self.id}.regs key")
            if not 0 <= r <= 0xFF:
                raise ValueError(f"part {self.id}: register 0x{r:x} outside 0x00..0xFF")
            init[r] = parse_int(v, f"{self.id}.regs[0x{r:02x}]") & 0xFF
        self._init = init
        ro = self.params["readonly"] or []
        if isinstance(ro, (str, int)):
            ro = [x for x in str(ro).replace(",", " ").split()] if isinstance(ro, str) else [ro]
        self.readonly = {parse_int(x, f"{self.id}.readonly") & 0xFF for x in ro}
        self.default = parse_int(self.params["default"], f"{self.id}.default") & 0xFF
        self.params["regs"] = {f"0x{k:02X}": v for k, v in sorted(init.items())} or None
        self.params["readonly"] = sorted(self.readonly) or None
        self.regs = [self.default] * 256
        for k, v in init.items():
            self.regs[k] = v

    def _device_reset(self) -> None:
        self.pointer = 0
        self._first = False
        self.writes = getattr(self, "writes", 0)
        self.reads = getattr(self, "reads", 0)

    def state(self) -> dict:
        return {"address": self.params["address"], "pointer": self.pointer, "writes": self.writes,
                "reads": self.reads, "transfers": self.transfers}

    def set(self, **values: Any) -> dict:
        if set(values) - {"reg", "value"} or "reg" not in values or "value" not in values:
            raise ValueError(f"part {self.id} (i2c_register_device): use set(reg=.., value=..)")
        r = parse_int(values["reg"], f"{self.id}.reg") & 0xFF
        self.regs[r] = parse_int(values["value"], f"{self.id}.value") & 0xFF
        return self.state()

    # ---------------------------------------------------------------- I2C target handler
    def i2c_address(self, addr: int, read: bool) -> bool:
        if not self.matches(addr):
            return False
        self._first = not read
        return True

    def i2c_rx_byte(self, byte: int) -> bool:
        if self._first:
            self.pointer = byte
            self._first = False
            return True
        if self.pointer not in self.readonly:
            self.regs[self.pointer] = byte
            self.writes += 1
        self.pointer = (self.pointer + 1) & 0xFF
        return True

    def i2c_tx_byte(self) -> int:
        v = self.regs[self.pointer]
        self.pointer = (self.pointer + 1) & 0xFF
        self.reads += 1
        return v
