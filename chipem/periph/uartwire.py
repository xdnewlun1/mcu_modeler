"""Receive side shared by the UART-like models (kinetis.uart, stm32.usart, ti_c2000.sci): the host's serial line
and the "is the receiver listening?" rule.

Hardware rule: a UART has no input queue. A character exists for the receiver only when its stop bit arrives on the
RX pin. If the receiver cannot take it at that moment the character is lost:

* the receiver is not listening (`_rx_block_reason()`, family specific: receiver disabled, module clock gated off,
  module held in reset) or the RX pin is not muxed to this instance's RX function (`rx_signal`, checked against the
  PinBoard owner of every pin that offers that function in the chip description) -> the byte is dropped;
* the holding register (or FIFO) is still full -> overrun, handled by the family model in `_rx_accept()`.

A dropped byte is logged as a `uart` event {uart, dir: 'drop', byte, dropped: 1, reason} (reasons: rx_disabled,
clock_gated, reset, pin_not_muxed, no_pin, overrun); `rx_dropped` counts them. No `warn`: losing bytes is what the
hardware does.

Sources of characters:
* `feed(data)`: the host (`--uart-in`, `--at TIME:UARTn=TEXT`, shell `uart`, web console). The host transmits from
  "now", one character per character time of the receiver's current framing and baud (115200 8N1 while the receiver
  has no baud generator), re-evaluated per byte. The bytes still to be sent are the host's transmit buffer
  (`rx_pending()` counts them); they keep going while the chip is reset, like a terminal would.
* `receive(data)`: board links (chipem/board.py). The link already scheduled the call at the stop-bit time of each
  byte (sender's character time), so every byte arrives at once.
* `_rx_arrive(b, "loop")`: internal loopback (C2000 SCICCR.LOOPBKENA); the pin is not involved.

Pin rule: when no pin of the chip description offers this instance's RX function, a board link cannot reach the
receiver (reason `no_pin`), while host input is still delivered (one `info` event says so): the host is then assumed
to talk to the peripheral directly (test chips without a pin table, or a pin table that lacks the function).

The check runs once per arriving byte (at most once per character time), never on register polling, so the hot
paths (status-register polling loops) are not affected.
"""
from __future__ import annotations

from collections import deque
from typing import Any

HOST_FALLBACK_BAUD = 115200.0
HOST_FALLBACK_BITS = 10


class UartRx:
    """Mixin for an MMIOBlock UART model. The model provides `char_ns()`, `rx_signal`, `_rx_block_reason()` and
    `_rx_accept(b)`, calls `_rx_init()` before its first reset() and `_wire_reset()` from reset()."""

    rx_signal: str = ""

    def _rx_init(self) -> None:
        self._wire: deque[int] = deque()
        self._wire_ev = None
        self.rx_dropped = 0
        self._rx_pins: list | None = None
        self._nopin_noted = False

    # ------------------------------------------------------------------ host line
    def feed(self, data: bytes) -> None:
        """Host -> chip: transmit `data` from now on, paced at this UART's character time."""
        data = bytes(data)
        if not data:
            return
        self._wire.extend(data)
        if self._wire_ev is None and self.chip is not None:
            self._wire_ev = self.chip.sched.in_ns(self._host_char_ns(), self._wire_tick)

    def _host_char_ns(self) -> float:
        t = self.char_ns()
        return t if t > 0 else HOST_FALLBACK_BITS * 1e9 / HOST_FALLBACK_BAUD

    def _wire_tick(self) -> None:
        self._wire_ev = None
        if not self._wire:
            return
        self._rx_arrive(self._wire.popleft(), "host")
        if self._wire and self._wire_ev is None:
            self._wire_ev = self.chip.sched.in_ns(self._host_char_ns(), self._wire_tick)

    def _wire_reset(self) -> None:
        """Model reset: cancel our scheduler event; the host keeps transmitting what it still has."""
        ev = getattr(self, "_wire_ev", None)
        if ev is not None and self.chip is not None:
            self.chip.sched.cancel(ev)
        self._wire_ev = None
        if getattr(self, "_wire", None) and self.chip is not None:
            self._wire_ev = self.chip.sched.in_ns(self._host_char_ns(), self._wire_tick)

    def wire_pending(self) -> int:
        """Host bytes not yet on the wire."""
        return len(self._wire)

    # ------------------------------------------------------------------ board links
    def receive(self, data: bytes) -> None:
        """Board-link hook: each byte's stop bit arrives now (the link already waited one sender character time)."""
        for b in bytes(data):
            self._rx_arrive(b, "link")

    # ------------------------------------------------------------------ arrival
    def _rx_arrive(self, b: int, src: str) -> None:
        why = self._rx_block_reason()
        if why is None and src != "loop":
            why = self._rx_pin_reason(src)
        if why is not None:
            self._rx_drop(b, why)
            return
        self._rx_accept(b)

    def _rx_drop(self, b: int, why: str) -> None:
        self.rx_dropped += 1
        self.emit("uart", uart=self.name, dir="drop", byte=b & 0xFF, dropped=1, reason=why)

    def _rx_block_reason(self) -> str | None:          # family: receiver enable / clock gate / module reset
        return None

    def _rx_accept(self, b: int) -> None:              # family: holding register / FIFO / overrun
        raise NotImplementedError

    def _rx_pin_reason(self, src: str) -> str | None:
        pins = self._rx_pins
        if pins is None:
            pins = self._rx_pins = self._find_rx_pins()
        if not pins:
            if src == "link":
                return "no_pin"
            if not self._nopin_noted:
                self._nopin_noted = True
                self.emit("info", block=self.name, msg=f"{self.name}: no pin offers {self.rx_signal or 'the RX'} "
                          "function in the chip description; host input is delivered without a pin check")
            return None
        sig = self.rx_signal
        for p in pins:
            own = p.owner
            if own == sig or sig in str(own).split("/"):
                return None
        return "pin_not_muxed"

    def _find_rx_pins(self) -> list:
        pb = getattr(self.chip, "pins", None)
        sig = self.rx_signal
        if pb is None or not sig:
            return []
        return [p for p in pb.pins if any(sig in str(f).split("/") for f in (p.functions or {}).values())]
