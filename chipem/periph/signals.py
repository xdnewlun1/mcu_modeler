"""Peripheral signal roles: what a pin does electrically while a peripheral function owns it.

When a family mux model (Kinetis PORT, STM32 GPIO in AF mode, C2000 GPxMUX) hands a pin to a peripheral signal,
it looks the signal name up in its family table (`chipem/periph/<family>/signals.py`, a `SignalTable`) and pushes
the resulting direction and idle drive to the PinBoard. Rules are `(regex, role)` pairs tried in order against
each `/`-separated part of the function name ("JTAG_TDO/TRACE_SWO" -> "JTAG_TDO" first); the first match wins.

Roles (see docs/CONTRACTS.md, "Peripheral-owned pins"):
  OUT_H   output idling high (UART TX, SPI chip selects, CAN TX, active-low strobes)
  OUT_L   output idling low (SPI clock/data out, timer/PWM channels, clock outputs, trace)
  IN      input (UART RX, SPI MISO, CAN RX, JTAG TCK/TMS/TDI); the level comes from the pull / stimulus / net, else X
  OD      open-drain (I2C SDA/SCL): released, i.e. an input; H when pulled up, else X
  ANALOG  analog input/output (ADC, CMP, DAC)
  NONE    unknown signal: direction "none", level X

This module has no vendor knowledge; it only interprets a table supplied by a family package. Serial traffic (UART
bytes, SPI frames) does not toggle peripheral-owned pins: the pin shows the signal's idle level. Models that drive a
signal edge by edge (timer/PWM channels, e.g. stm32.tim) call `PinBoard.drive_signal(name, level)`; a pin owned by
that signal then shows the live level instead of the idle one (also when it is muxed to the signal later).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class SignalRole:
    name: str              # "out_h" | "out_l" | "in" | "od" | "analog" | "none"
    direction: str         # PinBoard direction: "out" | "in" | "analog" | "none"
    idle: str | None       # drive level when direction == "out"


OUT_H = SignalRole("out_h", "out", "H")
OUT_L = SignalRole("out_l", "out", "L")
IN = SignalRole("in", "in", None)
OD = SignalRole("od", "in", None)
ANALOG = SignalRole("analog", "analog", None)
NONE = SignalRole("none", "none", None)


class SignalTable:
    """Ordered (regex, SignalRole) rules; `role(fn)` -> the role of a peripheral function name."""

    def __init__(self, rules: Iterable[tuple[str, SignalRole]]):
        self.rules = [(re.compile(rx, re.IGNORECASE), role) for rx, role in rules]
        self._cache: dict[str, SignalRole] = {}

    def role(self, fn: str | None) -> SignalRole:
        if not fn:
            return NONE
        r = self._cache.get(fn)
        if r is None:
            r = self._cache[fn] = self._lookup(fn)
        return r

    def _lookup(self, fn: str) -> SignalRole:
        for part in str(fn).split("/"):
            part = part.strip()
            for rx, role in self.rules:
                if rx.fullmatch(part):
                    return role
        return NONE

    def pin_state(self, fn: str | None) -> tuple[str, str | None]:
        """(direction, drive) to push to the PinBoard for a pin owned by `fn` (pull is the mux model's business)."""
        r = self.role(fn)
        return r.direction, r.idle
