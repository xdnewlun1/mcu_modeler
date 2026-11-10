"""Vendor-free helpers shared by the SD host controller models (stm32.sdio, kinetis.sdhc).

`ClockWave`: an SD card clock runs at up to 50 MHz, far too fast to toggle a pin per edge in an instruction-level
emulator. The models keep the clock pin at its idle level (L) and summarise the clock in the event log the same way
EventLog does for dense pins (docs/CONTRACTS.md "Events / logging"): one stored `pin` event with `field="wave"`,
`new="L"`, `edges`, `since_ns`, `freq_hz`, `duty` = 50.0 (percent, as EventLog `wave` events), per `window_ns` (100 ms) of running clock, when the frequency
changes and when the clock stops. A continuously running clock counts edges from elapsed time; a clock gated to bus
activity (STM32 PWRSAV, Kinetis automatic gating) counts the clocks of each command/data token (`active(clocks)`).

`lines_muxed(chip, signals)`: True when every named peripheral signal is owned by some pin (the card only sees the
bus through pins muxed to the host's signals).
"""
from __future__ import annotations

from typing import Any

WINDOW_NS = 100e6


def lines_muxed(chip: Any, signals) -> bool:
    board = getattr(chip, "pins", None)
    if board is None:
        return True
    for sig in signals:
        if not any(board.signal_matches(p.owner, sig) for p in board.signal_pins(sig)):
            return False
    return True


class ClockWave:
    def __init__(self, block: Any, signal: str, window_ns: float = WINDOW_NS):
        self.block = block
        self.signal = signal
        self.window_ns = window_ns
        self.hz = 0.0
        self.gated = False                # True: count only active() clocks
        self._t0: float | None = None     # start of the current continuous segment
        self._edges = 0.0
        self._since: float | None = None
        self._ev = None

    @property
    def chip(self) -> Any:
        return self.block.chip

    def _now(self) -> float:
        return self.chip.now_ns()

    def set(self, hz: float, gated: bool) -> None:
        """New clock state (hz 0 = stopped)."""
        if self.chip is None:
            return
        if hz != self.hz:
            self.flush(stop=True)
            self.hz = hz
            self.gated = gated
            if hz:
                now = self._now()
                self._since = now
                self._t0 = None if gated else now
                self._ev = self.chip.sched.at_ns(now + self.window_ns, self._window)
        elif hz and gated != self.gated:
            self._accumulate()
            self.gated = gated
            self._t0 = None if gated else self._now()

    def active(self, clocks: float) -> None:
        if self.hz and self.gated:
            self._edges += 2 * clocks

    def _accumulate(self) -> None:
        if self._t0 is not None and self.hz:
            now = self._now()
            self._edges += 2 * (now - self._t0) * self.hz / 1e9
            self._t0 = now

    def flush(self, stop: bool = False) -> None:
        chip = self.chip
        if chip is None:
            return
        self._accumulate()
        if stop:
            self._t0 = None
            if self._ev is not None:
                chip.sched.cancel(self._ev)
                self._ev = None
        n = int(self._edges)
        if n and self._since is not None:
            board = chip.pins
            for p in board.signal_pins(self.signal):
                if board.signal_matches(p.owner, self.signal):
                    self.block.emit("pin", pin=p.id, field="wave", old=None, new="L", edges=n,
                                    since_ns=self._since, freq_hz=self.hz, duty=50.0)
        self._edges = 0.0
        self._since = None if stop else self._now()

    def _window(self) -> None:
        self._ev = None
        self.flush()
        if self.hz:
            self._ev = self.chip.sched.at_ns(self._now() + self.window_ns, self._window)

    def reset(self) -> None:
        """Block reset: emit what was counted, forget the clock (the caller cancels nothing else)."""
        if self.chip is not None and (self.hz or self._edges):
            self.flush(stop=True)
        self.hz = 0.0
        self._edges = 0.0
        self._since = None
        self._t0 = None
