"""Generic I2C bus engine shared by the family I2C models (kinetis.i2c, stm32.i2c, ti_c2000.i2c) and the I2C board
parts (chipem/parts/_i2c.py).  No vendor knowledge: register semantics stay in the family models.

The bus is real: SDA and SCL are open-drain signals.  A controller pulls a line low with
`PinBoard.drive_open_drain(signal, True)` and releases it with `False`; the level it sees is the wired-AND level of
the pins carrying the signal (`PinBoard.signal_level`), which a board net resolves from every driver (chip pins,
parts) and the pull-up.  "H" is 1, "L" is 0, and "X" (a released line nobody pulls up) also reads as 1: an
undriven input floats high in this model, so a missing device shows up as a NACK.  Bit edges are scheduled on the
chip's scheduler in wall-clock ns (a few events per bit, none per cycle), so a VCD of the SDA/SCL pins shows the
real transaction: START, 8 data bits MSB first, the ACK bit, repeated START, STOP.

Pieces:

* `I2CTiming`: SCL low/high time, SDA hold after SCL falls, START setup/hold, STOP setup, bus-free time (ns).
* `PinBus`: a controller's SDA/SCL signals on its chip's PinBoard; calls `on_change(scl, sda)` whenever the bus
  level of either line changes (own drives included), listening on every pin that offers the signals.
* `I2CMaster`: the master bit sequencer.  Operations `start()` (START or repeated START), `write(byte)` (8 bits +
  the receiver's ACK), `read(ack_fn)` (8 bits + our ACK/NACK, decided by `ack_fn()` when the ACK bit is driven) and
  `stop()`; each runs as a coroutine of timed steps and calls its `done` callback at the SCL falling edge that ends
  it (the 9th clock for a byte).  Between operations the master holds SCL low (a controller waiting for software).
  Releasing SCL waits until SCL reads high, so a slave stretching the clock (or another master in its low phase)
  delays the high phase: clock synchronisation and stretching work across chips and parts.  A transmitted 1 read
  back as 0 while SCL is high is lost arbitration: the master releases both lines and calls `on_arb_lost`.
* `I2CTarget`: bus decoder + slave responder fed with every bus change.  It reports START/STOP, shifts address and
  data bits on SCL rising edges, and calls its handler at the byte boundaries: `i2c_address` / `i2c_rx_byte` at the
  8th falling edge (return True to ACK: SDA is pulled low for the 9th clock), `i2c_ack_done` at the 9th falling edge
  after an ACK we sent (return True to stretch SCL until `release()`), `i2c_tx_byte` when the next byte to transmit
  is needed (None: SCL is stretched until `send(byte)`), `i2c_tx_ack(acked)` with the master's ACK of our byte.

Timing notes (documented approximations): edges are instantaneous (no rise/fall time, TRISE/filters ignored); a
slave answers at the SCL edge itself (zero output delay); the master samples SDA at the end of the SCL high phase.
Across chips the board quantum must be shorter than the SCL low time (boards with I2C use `quantum: 1us`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Generator

__all__ = ["I2CTiming", "PinBus", "I2CMaster", "I2CTarget", "bit_of"]


def bit_of(level: Any) -> int:
    """Bus bit of a pin level: L -> 0; H, X (released, floats high) and None (no pin) -> 1."""
    return 0 if level == "L" else 1


@dataclass(frozen=True)
class I2CTiming:
    low_ns: float                   # SCL low period (tLOW)
    high_ns: float                  # SCL high period (tHIGH)
    hold_ns: float = 0.0            # SDA change after the SCL falling edge (tHD;DAT)
    su_sta_ns: float = 0.0          # repeated START: SCL high to SDA falling (tSU;STA); 0 = high_ns
    hd_sta_ns: float = 0.0          # START: SDA falling to SCL falling (tHD;STA); 0 = high_ns
    su_sto_ns: float = 0.0          # STOP: SCL rising to SDA rising (tSU;STO); 0 = high_ns
    buf_ns: float = 0.0             # bus free time between STOP and the next START (tBUF)

    @property
    def period_ns(self) -> float:
        return self.low_ns + self.high_ns

    @property
    def hz(self) -> float:
        return 1e9 / self.period_ns if self.period_ns > 0 else 0.0

    def su_sta(self) -> float:
        return self.su_sta_ns or self.high_ns

    def hd_sta(self) -> float:
        return self.hd_sta_ns or self.high_ns

    def su_sto(self) -> float:
        return self.su_sto_ns or self.high_ns

    def hold(self) -> float:
        h = self.hold_ns if self.hold_ns > 0 else self.low_ns / 4.0
        return min(h, self.low_ns * 0.9)


class PinBus:
    """SDA/SCL of one controller on a chip's PinBoard (signal names such as "I2C0_SDA" / "I2C0_SCL")."""

    def __init__(self, chip: Any, sda: str, scl: str, on_change: Callable[[int, int], None] | None = None):
        self.chip = chip
        self.board = getattr(chip, "pins", None)
        self.sda_sig, self.scl_sig = sda, scl
        self.on_change = on_change
        self._last = (1, 1)
        self._hooked: list[tuple[Any, Callable]] = []
        b = self.board
        if b is not None:
            for sig in (sda, scl):
                for p in b.signal_pins(sig):
                    cb = self._pin_changed
                    if p.id not in b.input_listeners or cb not in b.input_listeners[p.id]:
                        b.input_listeners[p.id].append(cb)
                        self._hooked.append((p, cb))
        self._last = self.levels()

    def close(self) -> None:
        b = self.board
        for p, cb in self._hooked:
            lst = b.input_listeners.get(p.id)
            if lst and cb in lst:
                lst.remove(cb)
        self._hooked = []

    # ------------------------------------------------------------------ levels
    def sda(self) -> int:
        return bit_of(self.board.signal_level(self.sda_sig)) if self.board is not None else 1

    def scl(self) -> int:
        return bit_of(self.board.signal_level(self.scl_sig)) if self.board is not None else 1

    def levels(self) -> tuple[int, int]:
        return self.scl(), self.sda()

    def muxed(self) -> bool:
        """Is at least one pin muxed to each signal?"""
        b = self.board
        return b is not None and b.signal_level(self.sda_sig) is not None and b.signal_level(self.scl_sig) is not None

    def set_sda(self, low: bool) -> None:
        if self.board is not None:
            self.board.drive_open_drain(self.sda_sig, low)

    def set_scl(self, low: bool) -> None:
        if self.board is not None:
            self.board.drive_open_drain(self.scl_sig, low)

    def release(self) -> None:
        self.set_sda(False)
        self.set_scl(False)

    def resync(self) -> None:
        """Re-read the levels (after a pin mux change) and report a change."""
        self._pin_changed(None)

    def _pin_changed(self, _pin: Any) -> None:
        now = self.levels()
        if now != self._last:
            self._last = now
            if self.on_change is not None:
                self.on_change(*now)


Step = tuple[str, float]


class I2CMaster:
    """Master bit sequencer on a PinBus.  `timing()` is read at every bit (clock changes apply at the next bit).
    `now()` and `sched` come from the chip.  See the module docstring."""

    def __init__(self, chip: Any, bus: PinBus, timing: Callable[[], I2CTiming],
                 on_arb_lost: Callable[[], None] | None = None):
        self.chip = chip
        self.bus = bus
        self.timing = timing
        self.on_arb_lost = on_arb_lost
        self._gen: Generator[Step, Any, Any] | None = None
        self._done: Callable[..., None] | None = None
        self._ev = None
        self._wait_scl = False
        self.t_fall: float | None = None        # last SCL falling edge we generated (None: we do not hold SCL)
        self.t_stop: float = -1e18              # last STOP we generated
        self.owns_bus = False                   # between our START and our STOP
        self.arb_check = True

    # ------------------------------------------------------------------ public
    @property
    def busy(self) -> bool:
        return self._gen is not None

    def now(self) -> float:
        return float(self.chip.now_ns())

    def abort(self, release: bool = True) -> None:
        """Stop whatever is running (module disabled / reset); release SDA and SCL."""
        if self._ev is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None
        self._gen = None
        self._done = None
        self._wait_scl = False
        self.owns_bus = False
        self.t_fall = None
        if release:
            self.bus.release()

    def start(self, done: Callable[[], None] | None = None) -> None:
        self._run(self._start(), done)

    def write(self, byte: int, done: Callable[[bool], None] | None = None, nbits: int = 8) -> None:
        self._run(self._write(byte & 0xFF, nbits), done)

    def read(self, ack_fn: Callable[[], bool], done: Callable[[int, bool], None] | None = None,
             nbits: int = 8) -> None:
        self._run(self._read(ack_fn, nbits), done)

    def stop(self, done: Callable[[], None] | None = None) -> None:
        self._run(self._stop(), done)

    def scl_changed(self, scl: int) -> None:
        """Feed from the owner's PinBus change callback: resumes a release that waits for SCL high."""
        if self._wait_scl and scl:
            self._wait_scl = False
            self._advance(self.now())

    # ------------------------------------------------------------------ runner
    def _run(self, gen, done) -> None:
        if self._gen is not None:
            raise RuntimeError("I2C master: operation already running")
        self._gen = gen
        self._done = done
        self._advance(None)

    def _advance(self, val: Any) -> None:
        self._ev = None
        gen = self._gen
        while gen is not None:
            try:
                kind, arg = gen.send(val)
            except StopIteration as e:
                if self._gen is not gen:              # aborted / replaced from a callback inside the step
                    return
                self._gen = None
                cb, self._done = self._done, None
                if cb is not None:
                    r = e.value
                    if r is None:
                        cb()
                    elif isinstance(r, tuple):
                        cb(*r)
                    else:
                        cb(r)
                return
            if self._gen is not gen:
                return
            now = self.now()
            if kind == "at":
                if arg <= now:
                    val = now                         # already due (a stretch or slow software delayed us)
                    continue
                self._ev = self.chip.sched.at_ns(arg, self._fire, arg)
                return
            if kind == "scl_high":
                if self.bus.scl():
                    val = now
                    continue
                self._wait_scl = True
                return
            raise ValueError(kind)

    def _fire(self, t: float) -> None:
        self._ev = None
        self._advance(t)

    # ------------------------------------------------------------------ bit level (generators)
    def _lost(self) -> None:
        self.bus.release()
        self.owns_bus = False
        self.t_fall = None
        self._gen = None
        self._done = None
        if self.on_arb_lost is not None:
            self.on_arb_lost()

    def _clock(self, sda_low: bool | Callable[[], bool], check: bool):
        """One SCL pulse with SDA set during the low phase. Returns the SDA bit sampled at the end of the high
        phase, or None after lost arbitration (the generator then stops)."""
        T = self.timing()
        t_fall = self.t_fall if self.t_fall is not None else self.now()
        now = self.now()
        t_sda = max(t_fall + T.hold(), now)
        t = yield ("at", t_sda)
        low = sda_low() if callable(sda_low) else sda_low
        self.bus.set_sda(low)
        t = yield ("at", max(t_fall + T.low_ns, t + (T.low_ns - T.hold())))
        self.bus.set_scl(False)                       # release SCL; wait for it to read high (stretching)
        t_rise = yield ("scl_high", 0.0)
        t = yield ("at", t_rise + T.high_ns)
        v = self.bus.sda()
        if check and not low and v == 0 and self.arb_check:
            self._lost()
            return None
        self.bus.set_scl(True)
        self.t_fall = t
        return v

    def _start(self):
        T = self.timing()
        now = self.now()
        if self.t_fall is not None and self.owns_bus:        # repeated START: SCL is low (we hold it)
            t_fall = self.t_fall
            t = yield ("at", max(t_fall + T.hold(), now))
            self.bus.set_sda(False)
            yield ("at", max(t_fall + T.low_ns, t + (T.low_ns - T.hold())))
            self.bus.set_scl(False)
            t_rise = yield ("scl_high", 0.0)
            t = yield ("at", t_rise + T.su_sta())
        else:
            t = yield ("at", max(now, self.t_stop + T.buf_ns))
        if self.arb_check and (not self.bus.sda() or not self.bus.scl()):
            self._lost()                                     # somebody else holds the bus
            return None
        self.bus.set_sda(True)                               # START: SDA falls while SCL is high
        self.owns_bus = True
        t = yield ("at", t + T.hd_sta())
        self.bus.set_scl(True)
        self.t_fall = t
        return None

    def _write(self, byte: int, nbits: int):
        for i in range(nbits - 1, -1, -1):
            bit = (byte >> i) & 1
            v = yield from self._clock(not bit, True)
            if v is None:
                return None
        v = yield from self._clock(False, False)             # ACK bit: SDA released, the receiver pulls it low
        if v is None:
            return None
        return v == 0

    def _read(self, ack_fn: Callable[[], bool], nbits: int):
        val = 0
        for _ in range(nbits):
            v = yield from self._clock(False, False)
            if v is None:
                return None
            val = (val << 1) | v
        acked = []

        def drive_ack() -> bool:
            a = bool(ack_fn())
            acked.append(a)
            return a                                         # ACK: SDA low; NACK: released
        v = yield from self._clock(drive_ack, False)
        if v is None:
            return None
        return val, acked[0]

    def _stop(self):
        T = self.timing()
        now = self.now()
        if self.t_fall is None:                              # we do not hold SCL: nothing to stop
            self.owns_bus = False
            return None
        t_fall = self.t_fall
        t = yield ("at", max(t_fall + T.hold(), now))
        self.bus.set_sda(True)
        yield ("at", max(t_fall + T.low_ns, t + (T.low_ns - T.hold())))
        self.bus.set_scl(False)
        t_rise = yield ("scl_high", 0.0)
        t = yield ("at", t_rise + T.su_sto())
        self.bus.set_sda(False)                              # STOP: SDA rises while SCL is high
        self.t_fall = None
        self.owns_bus = False
        self.t_stop = t
        return None


# ---------------------------------------------------------------------------------------------- target
IDLE, ADDR, RX, TX, ACK_OUT, ACK_IN, IGNORE, WAIT_TX = range(8)


class I2CTarget:
    """Bus decoder and slave responder.  `drive_sda(low)` / `drive_scl(low)` pull the lines (open drain); call
    `bus(scl, sda)` with the bus levels after every change (own drives included).  `handler` implements any of:

      i2c_start(repeated: bool)                 START (repeated = no STOP since the previous START)
      i2c_stop()                                STOP
      i2c_address(addr: int, read: bool) -> bool  7-bit address byte received (8th SCL fall): True = ACK
      i2c_rx_byte(byte: int) -> bool            data byte received while addressed for writing: True = ACK
      i2c_ack_done(read: bool) -> bool          9th SCL fall after our ACK: True = stretch SCL until release()
      i2c_tx_byte() -> int | None               next byte to send (None = stretch SCL until send(byte))
      i2c_tx_ack(acked: bool)                   the master's ACK (True) / NACK of the byte we sent
      i2c_bus(busy: bool)                       bus busy (START seen) / free (STOP seen)

    `monitor`: also decode traffic that this target does not answer (addresses that do not match are IGNOREd)."""

    def __init__(self, handler: Any, drive_sda: Callable[[bool], None], drive_scl: Callable[[bool], None]):
        self.h = handler
        self._sda_out = drive_sda
        self._scl_out = drive_scl
        self.reset()

    def reset(self) -> None:
        self.state = IDLE
        self.scl, self.sda = 1, 1
        self.bits = 0
        self.shift = 0
        self.read = False
        self.busy = False
        self.addressed = False
        self.holding = False
        self.sda_low = False
        self.enabled = True
        self._tx = 0
        self._master_ack = False

    # ------------------------------------------------------------------ outputs
    def _set_sda(self, low: bool) -> None:
        if low != self.sda_low:
            self.sda_low = low
            self._sda_out(low)

    def hold(self) -> None:
        """Stretch SCL (only effective while SCL is low)."""
        if not self.holding:
            self.holding = True
            self._scl_out(True)

    def release(self) -> None:
        """Stop stretching SCL."""
        if self.holding:
            self.holding = False
            self._scl_out(False)

    def disable(self) -> None:
        """Module off: release everything and forget the transfer (bus state is still tracked)."""
        self._set_sda(False)
        self.release()
        self.state = IDLE if not self.busy else IGNORE
        self.addressed = False
        self.enabled = False

    def enable(self) -> None:
        self.enabled = True

    def send(self, byte: int) -> None:
        """Data for a slave transmit that stretched SCL (i2c_tx_byte returned None)."""
        if self.state != WAIT_TX:
            return
        self._load(byte)
        self.release()

    # ------------------------------------------------------------------ decoding
    def _call(self, name: str, *args: Any, default: Any = None) -> Any:
        f = getattr(self.h, name, None)
        return f(*args) if f is not None else default

    def _load(self, byte: int) -> None:
        self._tx = byte & 0xFF
        self.state = TX
        self.bits = 0
        self._set_sda(not (self._tx >> 7) & 1)

    def bus(self, scl: int, sda: int) -> None:
        pscl, psda = self.scl, self.sda
        self.scl, self.sda = scl, sda
        if scl and pscl and sda != psda:
            if not sda:
                self._start()
            else:
                self._stop()
            return
        if scl and not pscl:
            self._rise()
        elif pscl and not scl:
            self._fall()

    def _start(self) -> None:
        repeated = self.busy
        self.busy = True
        self._set_sda(False)
        self.release()
        self.state = ADDR if self.enabled else IGNORE
        self.bits = 0
        self.shift = 0
        self.addressed = False
        if not repeated:
            self._call("i2c_bus", True)
        self._call("i2c_start", repeated)

    def _stop(self) -> None:
        self.busy = False
        self._set_sda(False)
        self.release()
        self.state = IDLE
        self.addressed = False
        self._call("i2c_stop")
        self._call("i2c_bus", False)

    def _rise(self) -> None:
        st = self.state
        if st in (ADDR, RX):
            self.shift = ((self.shift << 1) | self.sda) & 0xFF
            self.bits += 1
        elif st == TX:
            self.bits += 1
        elif st == ACK_IN:
            self._master_ack = self.sda == 0

    def _fall(self) -> None:
        st = self.state
        if st in (ADDR, RX):
            if self.bits < 8:
                return
            if st == ADDR:
                addr, rd = self.shift >> 1, bool(self.shift & 1)
                ack = bool(self._call("i2c_address", addr, rd, default=False)) if self.enabled else False
                if ack:
                    self.read = rd
                    self.addressed = True
            else:
                ack = bool(self._call("i2c_rx_byte", self.shift, default=False))
            if ack:
                self._set_sda(True)
                self.state = ACK_OUT
            else:
                self._set_sda(False)
                self.state = IGNORE
                self.addressed = False
            return
        if st == ACK_OUT:                                     # end of the 9th clock: our ACK is done
            self._set_sda(False)
            stretch = bool(self._call("i2c_ack_done", self.read, default=False))
            if self.read:
                self._next_tx(stretch)
            else:
                self.state = RX
                self.bits = 0
                self.shift = 0
                if stretch:
                    self.hold()
            return
        if st == TX:
            if self.bits < 8:
                self._set_sda(not (self._tx >> (7 - self.bits)) & 1)
            else:
                self._set_sda(False)                          # release for the master's ACK
                self.state = ACK_IN
                self._master_ack = False
            return
        if st == ACK_IN:
            acked = self._master_ack
            self._call("i2c_tx_ack", acked)
            if acked and self.state == ACK_IN:
                self._next_tx(False)
            elif self.state == ACK_IN:
                self.state = IGNORE                           # NACK: the master ends the transfer
                self.addressed = False
            return

    def _next_tx(self, stretch: bool) -> None:
        self.state = WAIT_TX
        b = None if stretch else self._call("i2c_tx_byte")
        if b is None:
            self.hold()
            return
        self._load(b)

    def provide(self) -> None:
        """Stretch ended by the handler (after i2c_ack_done returned True on a read): ask for the byte now."""
        if self.state == WAIT_TX:
            b = self._call("i2c_tx_byte")
            if b is not None:
                self.send(b)
        else:
            self.release()


class _Lines:
    """One driver's view of a PinBus: the port ORs the master's and the target's pulls on each line."""

    def __init__(self, port: "I2CPort", who: int):
        self.port, self.who = port, who

    def sda(self) -> int:
        return self.port.bus.sda()

    def scl(self) -> int:
        return self.port.bus.scl()

    def set_sda(self, low: bool) -> None:
        self.port._set(0, self.who, low)

    def set_scl(self, low: bool) -> None:
        self.port._set(1, self.who, low)

    def release(self) -> None:
        self.set_sda(False)
        self.set_scl(False)


class I2CPort:
    """Plumbing of one I2C controller of a chip: its PinBus, an I2CMaster and an I2CTarget sharing the two lines
    (each line is pulled low while the master or the target pulls it).  `handler` gets the I2CTarget callbacks;
    `timing()` gives the master's I2CTiming; `on_arb_lost()` is called when the master loses arbitration."""

    def __init__(self, chip: Any, sda: str, scl: str, handler: Any, timing: Callable[[], I2CTiming],
                 on_arb_lost: Callable[[], None] | None = None):
        self._low = [[False, False], [False, False]]          # [line][who]: line 0 = SDA, 1 = SCL; who 0 = master
        self.bus = PinBus(chip, sda, scl, self._changed)
        self.master = I2CMaster(chip, _Lines(self, 0), timing, on_arb_lost)
        t = _Lines(self, 1)
        self.target = I2CTarget(handler, t.set_sda, t.set_scl)
        self.target.scl, self.target.sda = self.bus.levels()

    def _set(self, line: int, who: int, low: bool) -> None:
        lo = self._low[line]
        before = lo[0] or lo[1]
        lo[who] = bool(low)
        after = lo[0] or lo[1]
        if after != before:
            (self.bus.set_sda if line == 0 else self.bus.set_scl)(after)

    def _changed(self, scl: int, sda: int) -> None:
        self.target.bus(scl, sda)
        self.master.scl_changed(scl)

    def reset(self) -> None:
        """Release everything (module disabled / reset); the decoder keeps watching the bus levels."""
        self.master.abort(release=False)
        busy = self.target.busy
        self.target.reset()
        self.target.busy = busy
        self._low = [[False, False], [False, False]]
        self.bus.release()
        self.target.scl, self.target.sda = self.bus.levels()

    def close(self) -> None:
        self.bus.close()
