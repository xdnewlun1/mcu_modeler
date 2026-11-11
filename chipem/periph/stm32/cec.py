"""STM32F446 HDMI-CEC controller (RM0390 Rev 9 ch. 32): a bit-level single-wire CEC bus on the board.

The CEC line is the open-drain signal "CEC" (PA15 AF4 / PB6 AF3 on the STM32F446RE): the model pulls it low with
`PinBoard.drive_open_drain("CEC", True)` and releases it, and decodes every level change of the wired-AND line (its own
drive included) from the pins muxed to CEC (`input_listeners`).  A board net with `open_drain: true` connects two chips
(or a chip and a script part); a released line nobody pulls up reads 1 (`i2cwire.bit_of`), like the 27 kOhm pull-up
of 32.3.1.  Scheduling is per bit edge: an initiator schedules the falling edge, the release, the sampling point and the
next bit (4 events per bit); a follower schedules at most a BRE and an LBPE timeout per bit.

What follows RM0390 (section numbers):
* 32.3.3 message = start bit + blocks of 8 data bits (MSB first) + EOM + ACK; header = initiator[7:4] / destination[3:0]
  (0xF = broadcast).  32.3.4 bit timing: start bit low 3.7 ms, period 4.5 ms; data bit period 2.4 ms, logical 0 low
  1.5 ms, logical 1 low 0.6 ms; follower ACK = the follower holds the ACK bit low until 1.5 ms.
* Timings are nominal times scaled by 32768 Hz / CEC kernel clock (RCC DCKCFGR2.CECSEL: HSI/488 = 32.787 kHz or LSE,
  `RCC.cec_clk_hz()`): the controller counts kernel clock ticks (32.2).  No kernel clock (RCC model without CEC mux):
  32768 Hz is assumed.
* Receiver windows of Table 237 (RXTOL = 0 / 1): rise T1..T2 = logical 1, T3..T4 = logical 0, otherwise BRE (a rise
  outside both windows, or still low at T4 -- the "end of the maximum BRE tolerance"); a falling edge before T5 = SBPE;
  no falling edge by T6 = LBPE (except after the last ACK bit: end of message).  Start bit: low 3.7 +/-0.2 ms
  (+/-0.4 with RXTOL), period 4.5 +/-0.2 (+/-0.4); a wrong start bit is ignored (32.4.1, the bus is idle again when
  the line rises).  BRESTP = 0: the bit is sampled at 1.05 ms (rise before Tns = 1).
* Error bits (32.5, 32.7.2): a follower pulls the line low for 3.6 ms (1.5 nominal bit periods) on SBPE (always,
  except in listen mode for a message to another device, or a broadcast with BRDNOGEN), on LBPE if LBPEGEN or (broadcast
  and not BRDNOGEN), on BRE with BRESTP if BREGEN or (broadcast and not BRDNOGEN).  LBPE and BRE with BRESTP stop the
  reception.
* Reception (32.7.2 OAR / LSTN, 32.7.5): at the end of the header the destination is compared with OAR (one bit per
  logical address); a match is received and ACKed, a broadcast is always received (ACK bit left released; RXOVR in a
  broadcast pulls it low = negative acknowledge); another destination is received only with LSTN = 1 and never ACKed,
  else ignored (no flags).  At the EOM bit of each received block: RXOVR if RXBR is still set (reception stops, no
  ACK), otherwise RXDR = byte, RXBR (+ RXEND with EOM).  RXACKE (broadcast: someone pulled the ACK bit low; listen mode,
  other destination: nobody acknowledged) stops the reception.
* Transmission (32.4, 32.7.1, 32.7.5): TXSOM starts a message (the header is TXDR[7:0]: the initiator address comes
  from TXDR, not OAR); the start bit begins after the signal free time: SFT = 0 -> 2.5 / 4 / 6 nominal bit periods
  (last initiator unsuccessful / new initiator / last initiator successful), SFT = 1..7 -> 0.5..6.5; counted from the
  TXSOM write (SFTOPT = 0) or from the last bus event (SFTOPT = 1: end of message, TXERR, TXACKE, TXUDR, receive error
  or the end of an error bit); never while the bus is busy (a TXSOM written during a reception starts after it).  Each
  block takes EOM = CR.TXEOM when it is loaded from TXDR.  TXBR is set when the 4th bit of a block that is not the last
  is sent; TXDR not written by the end of the ACK bit -> TXUDR.  ACK bit: the initiator sends 1 and samples it at
  1.05 ms: low = acknowledged (direct) / rejected (broadcast) -> TXACKE when wrong.  A 1 read back as 0 during the
  initiator address bits = ARBLST (the controller becomes a follower and decodes the rest of the header, TXSOM stays
  set: retried after SFT); elsewhere a 0 read while the line should be released (1.05 ms for a 1, T4 for a 0) = TXERR.
  TXEND at the end (2.4 ms) of the last ACK bit.  TXEND/TXUDR/TXACKE/TXERR clear TXSOM and TXEOM.
* CECEN = 0 clears CR (TXSOM, TXEOM), aborts a reception / transmission and releases the line (32.7.1).  CFGR and IER
  writes while CECEN = 1 are ignored with one `warn` (the RM makes them mandatory with CECEN = 0).  ISR bits are
  rc_w1; the CEC interrupt (vector "CEC") is ISR & IER.  Registers: 32-bit accesses (32.7).

Approximations: edges are instantaneous (no filter); the follower reacts at the edge itself (zero delay; across chips
the board quantum adds up to one quantum, so keep it ~10 us); an initiator samples the line only at the RM's checking
points (1.05 ms and T4), not continuously over the TXERR window of Figure 442; ARBLST is reported only for a lost
initiator-address bit (a contending device that starts earlier simply makes the bus busy: the transmission waits, no
flag); the Stop-mode wake-up of 32.2 is not modelled.
"""
from __future__ import annotations

from typing import Any

from ..base import pure_read
from ..i2cwire import bit_of
from ..registry import register
from ._util import GatedBlock, build_regs, irq_number, rcc

# CR
CECEN, TXSOM, TXEOM = 1, 2, 4
# ISR / IER
RXBR, RXEND, RXOVR, BRE, SBPE, LBPE, RXACKE, ARBLST = 1, 2, 4, 8, 0x10, 0x20, 0x40, 0x80
TXBR, TXEND, TXUDR, TXERR, TXACKE = 0x100, 0x200, 0x400, 0x800, 0x1000
ISR_MASK = 0x1FFF
# CFGR
SFT_MASK, RXTOL, BRESTP, BREGEN, LBPEGEN, BRDNOGEN, SFTOPT, LSTN = 7, 8, 0x10, 0x20, 0x40, 0x80, 0x100, 1 << 31

MS = 1e6                                    # ns per ms
BIT = 2.4                                   # nominal data bit period (ms)
# Table 237 / 238 (ms): (RXTOL=0, RXTOL=1)
T1 = (0.4, 0.3)
T2 = (0.8, 0.9)
TNS = 1.05
T3 = (1.3, 1.2)
T4 = (1.7, 1.8)
T5 = (2.05, 1.85)
T6 = (2.75, 2.95)
START_LOW, START_PERIOD = 3.7, 4.5
START_TOL = (0.2, 0.4)                      # 32.7.2 RXTOL: start bit +/-200 us / +/-400 us
ERROR_BIT = 3.6                             # 32.5.1
SFT_FIXED = (None, 0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5)
SIG = "CEC"


@register("stm32.cec")
class CEC(GatedBlock):
    model = "stm32.cec"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = params.get("gate", "CEC")
        self.sig = params.get("signal", SIG)
        ov = {"CEC_CR": {"ro_mask": 0xFFFFFFF8, "on_write": self._cr_write},
              "CEC_CFGR": {"ro_mask": 0x0000FE00, "on_write": self._cfgr_write},
              "CEC_TXDR": {"ro_mask": 0xFFFFFF00, "on_read": pure_read(lambda b, r: 0), "on_write": self._txdr_write},
              "CEC_RXDR": {"ro_mask": 0xFFFFFFFF},
              "CEC_ISR": {"ro_mask": ~ISR_MASK & 0xFFFFFFFF, "w1c_mask": ISR_MASK, "on_write": self._isr_write},
              "CEC_IER": {"ro_mask": ~ISR_MASK & 0xFFFFFFFF, "on_write": self._ier_write}}
        r = build_regs((name, "HDMI_CEC", "CEC"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self._irqn: int | None = None
        self._hooked: list = []
        self._evs: dict[str, Any] = {}
        self._warned: set[str] = set()
        self.messages: list[dict] = []           # observation: every message seen on the bus (decoded)
        self._init_state()

    # ------------------------------------------------------------------ state
    def _init_state(self) -> None:
        self._line_irq = False
        self._driving = False
        self._line = 1
        self._k = 1.0                            # 32768 / kernel clock
        # receiver
        self._rx = "idle"                        # idle / start / start_hi / bit_lo / bit_hi / end / ignore
        self._t_fall = 0.0
        self._bitn = 0
        self._byte = 0
        self._block = 0
        self._eom = False
        self._mode: str | None = None            # None (header) / own / bcast / listen / ignore / tx
        self._ack_drive = False
        self._ack_hold = False
        self._msg: list[int] = []
        self._dest = 0
        # transmitter
        self._tx_pending = False
        self._tx_active = False
        self._tx_bits: list[int] = []
        self._tx_k = 0
        self._tx_block = 0
        self._tx_byte = 0
        self._tx_eom = False
        self._tx_dest = 0
        self._txdr_full = False
        self._txsom_t = 0.0
        self._history = "new"                    # SFT = 0 rule: new / ok / fail
        self._idle_t = -1e18                     # last bus event (SFTOPT = 1 reference)

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, "CEC", "HDMI_CEC")
        board = getattr(chip, "pins", None)
        if board is not None:
            for p in board.signal_pins(self.sig):
                if p.id not in [h[0] for h in self._hooked]:
                    cb = self._pin_changed
                    board.input_listeners[p.id].append(cb)
                    self._hooked.append((p.id, cb))

    def reset(self) -> None:
        self._cancel_all()
        if self.chip is not None:
            self._release()
            if self._line_irq and self._irqn is not None:
                self.irq(self._irqn, False)
        self._init_state()
        super().reset()
        if self.chip is not None:
            self._line = self._read_line()

    # ------------------------------------------------------------------ helpers
    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=f"{self.name}: {msg}")

    def _now(self) -> float:
        return float(self.chip.now_ns())

    def _cfgr(self) -> int:
        return self.get("CEC_CFGR")

    def _tol(self) -> int:
        return 1 if self._cfgr() & RXTOL else 0

    def _ns(self, ms: float) -> float:
        return ms * MS * self._k

    def _clock(self) -> None:
        hz = 0.0
        r = rcc(self.chip) if self.chip is not None else None
        f = getattr(r, "cec_clk_hz", None) if r is not None else None
        if f is not None:
            try:
                hz = float(f())
            except Exception:  # noqa: BLE001 - an RCC without the mux
                hz = 0.0
        self._k = 32768.0 / hz if hz > 0 else 1.0

    def _ev(self, key: str, t: float, cb, *args) -> None:
        old = self._evs.pop(key, None)
        if old is not None:
            self.chip.sched.cancel(old)
        self._evs[key] = self.chip.sched.at_ns(max(t, self._now()), self._fire, key, cb, args)

    def _fire(self, key: str, cb, args) -> None:
        self._evs.pop(key, None)
        cb(*args)

    def _cancel(self, *keys: str) -> None:
        for k in keys:
            ev = self._evs.pop(k, None)
            if ev is not None and self.chip is not None:
                self.chip.sched.cancel(ev)

    def _cancel_all(self) -> None:
        if self.chip is not None:
            for ev in self._evs.values():
                self.chip.sched.cancel(ev)
        self._evs = {}

    def _read_line(self) -> int:
        b = getattr(self.chip, "pins", None)
        if b is None:
            return 1
        return bit_of(b.signal_level(self.sig))

    def _drive(self, low: bool) -> None:
        if self._driving == low:
            return
        self._driving = low
        b = getattr(self.chip, "pins", None)
        if b is not None:
            b.drive_open_drain(self.sig, low)

    def _release(self) -> None:
        self._drive(False)

    def _flag(self, f: int) -> None:
        self.set("CEC_ISR", self.get("CEC_ISR") | f)
        self._update_irq()

    def _update_irq(self) -> None:
        if self.chip is None or self._irqn is None:
            return
        on = bool(self.get("CEC_ISR") & self.get("CEC_IER"))
        if on != self._line_irq:
            self._line_irq = on
            self.irq(self._irqn, on)

    def _enabled(self) -> bool:
        return bool(self.get("CEC_CR") & CECEN)

    # ------------------------------------------------------------------ registers
    def _cr_write(self, blk, reg, old, new):
        if not new & CECEN:
            if old & CECEN:
                self._disable()
            return 0                                  # CECEN = 0 clears every CR bit (32.7.1)
        if not old & CECEN:
            self._enable()
        if new & ~old & TXSOM:
            self._txsom(new)
        elif (old & TXSOM) and not new & TXSOM:
            new |= TXSOM                              # rs: only hardware (or CECEN = 0) clears it
        if (old & TXEOM) and not new & TXEOM:
            new |= TXEOM
        return new

    def _cfgr_write(self, blk, reg, old, new):
        if self._enabled() and new != old:
            self._warn_once("cfgr", "CEC_CFGR written while CECEN = 1: ignored (RM0390 32.7.2: write it with CECEN = 0)")
            return old
        return None

    def _ier_write(self, blk, reg, old, new):
        if self._enabled() and new != old:
            self._warn_once("ier", "CEC_IER written while CECEN = 1: ignored (RM0390 32.7.6: write it with CECEN = 0)")
            return old
        self._update_irq()
        return None

    def _isr_write(self, blk, reg, old, new):
        self._update_irq()
        return None

    def _txdr_write(self, blk, reg, old, new):
        self._txdr_full = True
        return None

    # ------------------------------------------------------------------ enable / disable
    def _enable(self) -> None:
        self._clock()
        self._line = self._read_line()
        self._rx = "idle" if self._line else "ignore"
        self._mode = None
        if self._idle_t < -1e17:
            self._idle_t = -1e17

    def _disable(self) -> None:
        self._cancel_all()
        self._release()
        self._rx = "idle"
        self._mode = None
        self._tx_pending = self._tx_active = False
        self._ack_hold = False

    # ------------------------------------------------------------------ line decoding
    def _pin_changed(self, _pin: Any) -> None:
        if self.chip is None:
            return
        lvl = self._read_line()
        if lvl == self._line:
            return
        self._line = lvl
        if not self._enabled():
            return
        t = self._now()
        if lvl:
            self._rise(t)
        else:
            self._fall(t)

    def _fall(self, t: float) -> None:
        tol = self._tol()
        st = self._rx
        if st == "start_hi":
            d = (t - self._t_fall) / (MS * self._k)
            if abs(d - START_PERIOD) <= START_TOL[tol] + 1e-9:
                self._begin_message(t)
                return
            self._start_candidate(t)                  # wrong start bit period: this edge may start a new one
            return
        if st == "bit_hi":
            self._cancel("lbpe")
            d = (t - self._t_fall) / (MS * self._k)
            if d < T5[tol] - 1e-9:
                self._rx_error(SBPE, t)
                return
            self._next_bit(t)
            return
        if st in ("idle", "end"):
            if st == "end":
                self._cancel("idle")
                self._bus_idle(t, quiet=True)
            self._start_candidate(t)
            return
        # start / bit_lo (cannot fall while low) / ignore / errbit: nothing

    def _rise(self, t: float) -> None:
        tol = self._tol()
        st = self._rx
        d = (t - self._t_fall) / (MS * self._k)
        if st == "start":
            self._cancel("bre")
            if abs(d - START_LOW) <= START_TOL[tol] + 1e-9:
                self._rx = "start_hi"
                self._ev("startto", self._t_fall + self._ns(START_PERIOD + START_TOL[tol]), self._start_timeout)
            else:
                self._bus_idle(t)                     # wrong start bit: idle again when the line rises (32.4.1)
            return
        if st == "bit_lo":
            self._cancel("bre")
            if T1[tol] - 1e-9 <= d <= T2[tol] + 1e-9:
                bit = 1
            elif T3[tol] - 1e-9 <= d <= T4[tol] + 1e-9:
                bit = 0
            else:
                bit = 1 if d < TNS else 0
                if self._bre(t):
                    return
            self._rx = "bit_hi"
            self._bit_done(bit, t)
            return
        if st == "ignore":
            self._bus_idle(t)
            return
        if st == "errbit":
            return

    def _start_candidate(self, t: float) -> None:
        self._rx = "start"
        self._t_fall = t
        self._cancel("startto", "lbpe")
        # a start bit still low after the longest valid start low time is not a start bit
        self._ev("bre", t + self._ns(START_LOW + START_TOL[self._tol()]), self._start_too_long)

    def _start_too_long(self) -> None:
        if self._rx == "start":
            self._rx = "ignore"

    def _start_timeout(self) -> None:
        if self._rx == "start_hi":
            self._bus_idle(self._now())

    def _begin_message(self, t: float) -> None:
        self._cancel("startto")
        self._block = 0
        self._msg = []
        self._mode = "tx" if self._tx_active else None
        self._eom = False
        self._next_block()
        self._t_fall = t
        self._rx = "bit_lo"
        self._arm_bre()

    def _next_block(self) -> None:
        self._bitn = 0
        self._byte = 0

    def _next_bit(self, t: float) -> None:
        self._t_fall = t
        self._rx = "bit_lo"
        if self._bitn == 9 and self._ack_drive and self._mode not in ("tx", "ignore"):
            # follower acknowledge: hold the ACK bit low until the nominal logical-0 release (32.3.4)
            self._drive(True)
            self._ack_hold = True
            self._ev("ackrel", t + self._ns(1.5), self._ack_release)
        self._arm_bre()

    def _arm_bre(self) -> None:
        self._ev("bre", self._t_fall + self._ns(T4[self._tol()]), self._bre_timeout)

    def _ack_release(self) -> None:
        if self._ack_hold:
            self._ack_hold = False
            self._release()

    def _bre_timeout(self) -> None:
        if self._rx == "bit_lo":
            self._bre(self._now())                    # rising edge still missing at the end of the BRE window

    def _receiving(self) -> bool:
        return self._mode not in ("tx", "ignore")

    def _bre(self, t: float) -> bool:
        """Bit rising error; returns True when the reception stops."""
        if not self._receiving():
            return False
        self._flag(BRE)
        cfg = self._cfgr()
        if not cfg & BRESTP:
            return False
        gen = bool(cfg & BREGEN) or (self._mode == "bcast" and not cfg & BRDNOGEN)
        self._stop_rx(t, gen)
        return True

    def _rx_error(self, flag: int, t: float) -> None:
        """SBPE (falling edge too early) or LBPE (none in time): reception stops (32.5.4 / 32.5.5)."""
        if self._mode == "tx":
            return                                    # our own message: the transmitter checks the line itself
        if self._mode == "ignore":                    # a message for another device: no flags, no error bit
            if self._line:
                self._bus_idle(t)
            else:
                self._start_candidate(t)
            return
        cfg = self._cfgr()
        self._flag(flag)
        if flag == SBPE:
            gen = not (cfg & LSTN and (self._mode == "listen" or (self._mode == "bcast" and cfg & BRDNOGEN)))
        else:
            gen = bool(cfg & LBPEGEN) or (self._mode == "bcast" and not cfg & BRDNOGEN)
        self._stop_rx(t, gen)

    def _stop_rx(self, t: float, error_bit: bool) -> None:
        self._cancel("bre", "lbpe", "ackrel")
        self._ack_hold = False
        self._mode = "ignore"
        if error_bit:
            self._rx = "errbit"
            self._drive(True)
            self._ev("errbit", t + self._ns(ERROR_BIT), self._error_bit_end)
        else:
            self._release()
            if self._line:
                self._bus_idle(t)
            else:
                self._rx = "ignore"

    def _error_bit_end(self) -> None:
        self._release()
        t = self._now()
        if self._read_line():
            self._bus_idle(t)
        else:
            self._rx = "ignore"

    def _bit_done(self, bit: int, t: float) -> None:
        n = self._bitn
        tol = self._tol()
        if n < 8:
            self._byte = (self._byte << 1) | bit
            self._bitn += 1
        elif n == 8:
            self._eom = bool(bit)
            self._bitn = 9
            self._byte_received()
        else:
            self._ack_bit(bit, t)
            if self._eom:
                self._message_end(t)
                return
            self._block += 1
            self._next_block()
        self._ev("lbpe", self._t_fall + self._ns(T6[tol]), self._lbpe_timeout)

    def _lbpe_timeout(self) -> None:
        if self._rx == "bit_hi":
            self._rx_error(LBPE, self._now())

    def _byte_received(self) -> None:
        """End of the EOM bit: decide on the ACK and deliver the byte (32.7.2 OAR / LSTN, 32.7.5 RXOVR)."""
        b = self._byte & 0xFF
        self._msg.append(b)
        self._ack_drive = False
        if self._mode == "tx":
            return
        cfg = self._cfgr()
        if self._block == 0:
            self._dest = b & 0xF
            if self._dest == 0xF:
                self._mode = "bcast"
            elif (cfg >> 16) & 0x7FFF & (1 << self._dest):
                self._mode = "own"
            elif cfg & LSTN:
                self._mode = "listen"
            else:
                self._mode = "ignore"
        if self._mode == "ignore":
            return
        if self.get("CEC_ISR") & RXBR:
            self._flag(RXOVR)                       # reception stops: no ACK (broadcast: negative acknowledge)
            self._ack_drive = self._mode == "bcast"
            self._mode = "overrun"
            return
        self._ack_drive = self._mode == "own"
        self.set("CEC_RXDR", b)
        self._flag(RXBR | (RXEND if self._eom else 0))

    def _ack_bit(self, bit: int, t: float) -> None:
        mode = self._mode
        if mode == "overrun":
            self._mode = "ignore"
            return
        if mode == "bcast" and bit == 0:
            self._flag(RXACKE)
            self._mode = "ignore"
        elif mode == "listen" and bit == 1:
            self._flag(RXACKE)
            self._mode = "ignore"

    def _message_end(self, t: float) -> None:
        self._cancel("lbpe")
        self._rx = "end"
        self.messages.append({"t_ns": t, "bytes": list(self._msg), "mode": self._mode})
        if self._mode not in ("tx", None):
            self.emit("info", block=self.name, msg=f"{self.name}: CEC message "
                      + " ".join(f"{x:02X}" for x in self._msg) + f" ({self._mode})")
        # the bus is idle when no further bit starts within T6 (32.3.4: the line stays high after the final bit)
        self._ev("idle", self._t_fall + self._ns(T6[self._tol()]), self._idle_timeout)
        if self._mode != "tx":
            self._history = "new"
        self._idle_t = self._t_fall + self._ns(BIT)     # TXEND / RXEND: the SFT reference (32.4.1)

    def _idle_timeout(self) -> None:
        if self._rx == "end":
            self._bus_idle(self._now(), quiet=True)

    def _bus_idle(self, t: float, quiet: bool = False) -> None:
        """The bus is idle again; `quiet`: keep the SFT reference already set at the end of the message."""
        self._cancel("bre", "lbpe", "startto", "idle")
        self._rx = "idle"
        self._mode = None
        if not quiet:
            self._idle_t = t
        self._arm_tx()

    # ------------------------------------------------------------------ transmitter
    def _txsom(self, cr: int) -> None:
        self._tx_pending = True
        self._txsom_t = self._now()
        self._arm_tx()

    def _sft_ms(self) -> float:
        sft = self._cfgr() & SFT_MASK
        if sft:
            return SFT_FIXED[sft]
        return {"fail": 2.5, "new": 4.0, "ok": 6.0}[self._history]

    def _arm_tx(self) -> None:
        if not (self._tx_pending and not self._tx_active and self._enabled()):
            return
        if self._rx not in ("idle",) or not self._line:
            return                                    # bus busy: retried at the next bus idle
        ref = self._idle_t if self._cfgr() & SFTOPT else max(self._txsom_t, self._idle_t)
        self._ev("txstart", ref + self._ns(BIT * self._sft_ms()), self._tx_start)

    def _tx_start(self) -> None:
        if not (self._tx_pending and self._enabled()) or self._tx_active:
            return
        if self._rx != "idle" or not self._read_line():
            return                                    # someone started first: wait for the bus to be idle again
        self._tx_active = True
        self._tx_block = 0
        self._tx_load()
        t = self._now()
        self._drive(True)                            # start bit (our own falling edge is decoded by _fall)
        self._ev("tx", t + self._ns(START_LOW), self._tx_rel)
        self._ev("txbit", t + self._ns(START_PERIOD), self._tx_bit, 0)

    def _tx_load(self) -> None:
        b = self.get("CEC_TXDR") & 0xFF
        self._txdr_full = False
        self._tx_byte = b
        self._tx_eom = bool(self.get("CEC_CR") & TXEOM)
        if self._tx_block == 0:
            self._tx_dest = b & 0xF
        self._tx_bits = [(b >> (7 - i)) & 1 for i in range(8)] + [1 if self._tx_eom else 0, 1]

    def _tx_rel(self) -> None:
        if self._tx_active:
            self._release()

    def _tx_bit(self, k: int) -> None:
        if not self._tx_active:
            return
        t = self._now()
        bit = self._tx_bits[k]
        if k == 4 and not self._tx_eom:
            self._flag(TXBR)                          # 4th bit of a block that is not the last one sent (32.7.5)
        self._drive(True)
        self._ev("tx", t + self._ns(0.6 if bit else 1.5), self._tx_rel)
        if bit:
            self._ev("txchk", t + self._ns(TNS), self._tx_check, k, 1)
        else:
            self._ev("txchk", t + self._ns(T4[self._tol()]), self._tx_check, k, 0)
        self._ev("txbit", t + self._ns(BIT), self._tx_next, k)

    def _tx_check(self, k: int, bit: int) -> None:
        if not self._tx_active:
            return
        low = not self._read_line()
        if k == 9:
            self._tx_ack(low)
            return
        if not low:
            return
        if bit and self._tx_block == 0 and k < 4:
            self._arb_lost()                         # initiator address bit lost (32.4)
        else:
            self._tx_fail(TXERR)

    def _tx_ack(self, low: bool) -> None:
        bcast = self._tx_dest == 0xF
        if low == bcast:
            self._tx_fail(TXACKE)                    # direct: not acknowledged; broadcast: rejected (32.7.5)

    def _tx_next(self, k: int) -> None:
        if not self._tx_active:
            return
        if k < 9:
            self._tx_bit(k + 1)
            return
        if self._tx_eom:                             # end of the last ACK bit
            self._tx_done()
            return
        if not self._txdr_full:
            self._tx_fail(TXUDR)
            return
        self._tx_block += 1
        self._tx_load()
        self._tx_bit(0)

    def _tx_done(self) -> None:
        self._tx_active = False
        self._tx_pending = False
        self._history = "ok"
        self.set("CEC_CR", self.get("CEC_CR") & ~(TXSOM | TXEOM))
        self._flag(TXEND)
        self._idle_t = self._now()

    def _tx_fail(self, flag: int) -> None:
        self._cancel("tx", "txbit", "txchk", "txstart")
        self._release()
        self._tx_active = False
        self._tx_pending = False
        self._history = "fail"
        self.set("CEC_CR", self.get("CEC_CR") & ~(TXSOM | TXEOM))
        self._flag(flag)
        t = self._now()
        if self._rx in ("idle", "end", "errbit"):
            self._idle_t = t
        elif self._read_line():
            self._bus_idle(t)                         # SFT reference = the error detection (32.4.1)
        else:
            self._cancel("bre", "lbpe", "startto")
            self._rx = "ignore"                       # e.g. a follower's error bit: idle when the line rises
            self._mode = None
            self._idle_t = t

    def _arb_lost(self) -> None:
        """Lost arbitration: stop driving and become a follower; TXSOM stays set (retried after SFT)."""
        self._cancel("tx", "txbit", "txchk")
        self._release()
        self._tx_active = False
        self._history = "fail"
        self._mode = None                            # the decoder continues the header as a follower
        self._flag(ARBLST)

    # ------------------------------------------------------------------ introspection
    def state(self) -> dict:
        return {"rx": self._rx, "mode": self._mode, "tx_pending": self._tx_pending, "tx_active": self._tx_active,
                "messages": len(self.messages)}
