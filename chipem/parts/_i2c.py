"""Shared base of the I2C board parts (i2c_eeprom, i2c_register_device): two open-drain ports `sda` and `scl`, a
7-bit bus address and optional pull-up resistors on both lines (`pullup: true`, the default: a breakout board with
its own pull-ups; the resistors pull the nets high when nothing drives them, see `Part.pull`).

The bus is decoded from the net edges by the generic I2C engine (`chipem.periph.i2cwire.I2CTarget`): START/STOP,
address and data bits on SCL rising edges.  The device answers at the SCL falling edges by pulling SDA low (ACK, data
0 bits) or releasing it (high impedance): its `sda` output is "L" or None, never "H".  A subclass implements the
`I2CTarget` handler methods (`i2c_address`, `i2c_rx_byte`, `i2c_tx_byte`, `i2c_tx_ack`, `i2c_start`, `i2c_stop`).
Generic glue: no vendor knowledge.
"""
from __future__ import annotations

from typing import Any

from ..periph.i2cwire import I2CTarget, bit_of
from .base import Part
from .std import _bool

__all__ = ["I2CDevice", "parse_int"]


def parse_int(v: Any, what: str) -> int:
    if isinstance(v, bool):
        raise ValueError(f"{what}: expected an integer, got {v!r}")
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    try:
        return int(str(v).strip(), 0)
    except ValueError:
        raise ValueError(f"{what}: expected an integer, got {v!r}") from None


class I2CDevice(Part):
    """Base class of I2C target parts (not registered itself)."""
    PARAMS: dict[str, Any] = {"address": 0x50, "pullup": True}
    PORTS = {"sda": "inout", "scl": "inout"}
    ID_PREFIX = "I2C"

    def _setup(self) -> None:
        a = parse_int(self.params["address"], f"{self.id}.address")
        if not 0x08 <= a <= 0x77:
            raise ValueError(f"part {self.id}: address must be a 7-bit address 0x08..0x77 (got 0x{a:x})")
        self.address = a
        self.params["address"] = f"0x{a:02X}"
        self.pullup = _bool(self.params["pullup"], f"{self.id}.pullup")
        self.params["pullup"] = self.pullup

    def pull(self, port: str) -> str | None:
        return "up" if self.pullup and port in ("sda", "scl") else None

    # ---------------------------------------------------------------- bus plumbing
    def _sda(self, low: bool) -> None:
        self._drive("sda", "L" if low else None, quiet=True)

    def _scl(self, low: bool) -> None:
        self._drive("scl", "L" if low else None, quiet=True)

    def _reset_state(self) -> None:
        super()._reset_state()
        self.target = I2CTarget(self, self._sda, self._scl)
        self.transfers = 0
        self.naks = 0
        self._device_reset()

    def _device_reset(self) -> None:
        """Device-specific power-on state."""

    def on_input(self, port: str, level: str, t_ns: float) -> None:
        self.target.bus(bit_of(self.inputs.get("scl")), bit_of(self.inputs.get("sda")))

    def matches(self, addr: int) -> bool:
        return addr == self.address

    def i2c_start(self, repeated: bool) -> None:
        if not repeated:
            self.transfers += 1
