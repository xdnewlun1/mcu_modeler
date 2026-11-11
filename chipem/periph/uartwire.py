"""The serial line shared by the UART-like models (kinetis.uart, stm32.usart, ti_c2000.sci): what a transmitted
character looks like on the wire (`Frame`), how a receiver with its own settings decodes it (`RxFormat`, `RxChar`,
`LineDecoder`), the host's serial line and the "is the receiver listening?" rule. Vendor-free.

Frames
------
A transmitter describes every character it shifts out as a `Frame`: data bits (5..9, LSB first unless `msbf`), an
optional address bit (C2000 address-bit multiprocessor mode), an optional parity bit (N/E/O, or M/S from a host
port) with the value actually transmitted, the stop bits, its baud rate (the real one, from the divider) and the line
polarity (`inv`, Kinetis TXINV). A break is a frame of `brk` low bit times followed by its stop bits. Models call
`_tx_emit(frame)`: it appends the data byte to `output`, logs the `uart` event ({dir: 'tx', byte[, b8]} or {dir:
'tx', brk: bits}) and calls `frame_listeners` (cb(uart, frame): board links, the serial router, host bridges) and,
for data frames only, the byte-level `tx_listeners` (cb(uart, byte): Session, shell, web, tests). Old consumers keep
working unchanged.

Receive decoding (the model)
----------------------------
A receiver decodes against ITS OWN configuration (`rx_format()`: baud, data bits, address bit, parity, polarity, bit
order) with the sample points of the family: three samples around the middle of each bit (RT8/9/10 of 16 on
Kinetis and STM32 with 16x oversampling, 4/5/6 of 8 with OVER8 and on the C2000), a 2-of-3 majority vote, the noise
flag when the three disagree, start-bit verification samples, and the stop bit sampled the same way (only the first
stop bit is checked). Sample times are counted from the falling edge of the start bit at the receiver's bit time:
a baud mismatch moves the late samples into neighbouring bits, exactly as the accumulated misalignment does on
silicon (K22 RM §49.4.2.9: about +4.5 %/-3.9 % for 8-bit characters; RM0090 §30.3.5). Kinetis receivers also
resynchronise on falling data edges inside a character (§49.4.2.4).

* Fast path: when the transmitted frame has the same number of bit cells as the receiver expects, the same polarity
  and bit order, no break, and every sample point of the receiver lands inside the right transmitted bit (checked
  once per baud pair and cached), the character is taken directly from the frame: data, address bit and parity bit
  are read by the receiver's layout (an 8N1 byte received as 7E1 gives 7 data bits and a parity check on bit 7). No
  per-bit work and no scheduler events: this is the normal case.
* Line path: otherwise (baud outside the tolerance, different frame length, polarity, breaks) the frame's waveform is
  appended to the receiver's line history and decoded like hardware does: hunt for a 1->0 edge, verify the start
  bit, sample every bit, check the first stop bit (framing error), detect breaks (all zero including the stop bit;
  the low time is measured for LIN break / BRKDT detection), then hunt again from the stop sample. One transmitted
  character can give several received ones (fast transmitter seen by a slow receiver ... or the reverse) and
  back-to-back characters are decoded as one continuous line. When a character's sample points extend past the end
  of what has been transmitted so far, decoding waits for the next frame or, if none comes, one event fires after one
  more transmitter character time and decodes the rest against the idle line. Characters decoded this way are
  delivered when the decode happens, i.e. at the end of the transmitted frame that completes them (or at that
  event), not at their own stop-bit sample time; a sender and receiver on different chips are aligned only to the
  board quantum. A frame that would overlap the previous one on the same line (two transmitters on one RX) is
  delayed to its end.
Not modelled: the start-edge detection jitter (up to 1/16 bit), glitches (noise is only what the sampling of the
transmitted bits yields), clock drift inside a character.

Receiver listening rule
-----------------------
A UART has no input queue. A character exists for the receiver only when its stop bit arrives on the RX pin. If
the receiver cannot take it at that moment the character is lost:

* the receiver is not listening (`_rx_block_reason()`, family specific: receiver disabled, module clock gated off,
  module held in reset, clock stopped) or the RX pin is not muxed to this instance's RX function (`rx_signal`,
  checked against the PinBoard owner of every pin that offers that function in the chip description) -> the byte is
  dropped;
* the holding register (or FIFO) is still full -> overrun, handled by the family model in `_rx_accept()`.

A dropped byte is logged as a `uart` event {uart, dir: 'drop', byte, dropped: 1, reason} (reasons: rx_disabled,
clock_gated, reset, pin_not_muxed, no_pin, overrun, clock_stopped, stop_mode, mute, sleep, framing_error,
break_detected, ...); `rx_dropped` counts them. No `warn`: losing bytes is what the hardware does. A received
character with errors is a `uart` event {dir: 'rx', byte, fe/pe/nf: 1, brk: low bits}.

Sources of characters:
* `feed(data)`: the host (`--uart-in`, `--at TIME:UARTn=TEXT`, shell `uart`, web console). The host transmits from
  "now" in the receiver's current format (a terminal set up to match), one character per character time,
  re-evaluated per byte (115200 8N1 while the receiver has no baud generator). `feed_break(bits)` queues a break.
  The bytes still to be sent are the host's transmit buffer (`rx_pending()` counts them); they keep going while the
  chip is reset, like a terminal would.
* `receive_frame(frame, t_end)`: board links, the serial router and host bridges, called at the frame's stop-bit
  end (t_end: when that was, if the receiver's chip is already past it).
* `receive(data)`: legacy byte-level hook: each byte is a frame in the receiver's own format arriving now.
* `_rx_arrive(b, "loop")` / `_rx_frame(frame, "loop")`: internal loopback / half duplex; the pin is not involved.

Pin rule: when no pin of the chip description offers this instance's RX function, a board link cannot reach the
receiver (reason `no_pin`), while host input is still delivered (one `info` event says so): the host is then assumed
to talk to the peripheral directly (test chips without a pin table, or a pin table that lacks the function).

The checks run once per arriving character (at most once per character time), never on register polling, so the hot
paths (status-register polling loops) are not affected.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import deque
from typing import Any

HOST_FALLBACK_BAUD = 115200.0
HOST_FALLBACK_BITS = 10

SAMPLES_16 = (7 / 16, 8 / 16, 9 / 16)          # RT8, RT9, RT10 (Kinetis, STM32 OVER8 = 0)
START_16 = (2 / 16, 4 / 16, 6 / 16)            # RT3, RT5, RT7 start-bit verification
SAMPLES_8 = (3 / 8, 4 / 8, 5 / 8)              # 4th/5th/6th of 8 (STM32 OVER8 = 1, C2000 SCICLK)
START_8 = (1 / 8, 2 / 8)                       # STM32 OVER8 start-bit samples


def parity_bit(value: int, nbits: int, parity: str) -> int:
    """Transmitted parity bit for the low `nbits` of `value`: E (even number of 1s overall), O, M (1), S (0)."""
    if parity == "M":
        return 1
    if parity == "S":
        return 0
    ones = bin(value & ((1 << nbits) - 1)).count("1") & 1
    return ones ^ 1 if parity == "O" else ones


class Frame:
    """One character on the line, as transmitted (see the module docstring)."""
    __slots__ = ("data", "bits", "parity", "pbit", "stop", "baud", "addr", "brk", "msbf", "inv")

    def __init__(self, data: int = 0, bits: int = 8, parity: str = "N", stop: float = 1.0, baud: float = 0.0, *,
                 pbit: int | None = None, addr: int | None = None, brk: int = 0, msbf: bool = False,
                 inv: bool = False):
        self.bits = int(bits)
        self.data = int(data) & ((1 << self.bits) - 1)
        self.parity = parity
        self.addr = None if addr is None else int(addr) & 1
        if parity != "N" and pbit is None:
            v, n = self.data, self.bits
            if self.addr is not None:
                v, n = v | (self.addr << n), n + 1
            pbit = parity_bit(v, n, parity)
        self.pbit = None if parity == "N" else int(pbit) & 1
        self.stop = float(stop)
        self.baud = float(baud or 0.0)
        self.brk = int(brk)
        self.msbf = bool(msbf)
        self.inv = bool(inv)

    @classmethod
    def brk_frame(cls, bits: int, stop: float = 1.0, baud: float = 0.0, *, inv: bool = False) -> "Frame":
        return cls(0, 8, "N", stop, baud, brk=max(int(bits), 1), inv=inv)

    def ncells(self) -> int:
        """Bit cells between the start bit and the stop bit(s)."""
        return self.bits + (self.addr is not None) + (self.parity != "N")

    def raw(self) -> int:
        """The cells after the start bit as an int, bit i = i-th cell on the wire."""
        n = self.bits
        d = self.data
        if self.msbf:
            d = int(f"{d:0{n}b}"[::-1], 2) if n else 0
        if self.addr is not None:
            d |= self.addr << n
            n += 1
        if self.pbit is not None:
            d |= self.pbit << n
        return d

    def nbits(self) -> float:
        """Line time in bit times (start, cells, stop bits; a break: its low bits and stop bits)."""
        return (self.brk if self.brk else 1 + self.ncells()) + self.stop

    def duration_ns(self, baud: float | None = None) -> float:
        b = self.baud if baud is None else baud
        return self.nbits() * 1e9 / b if b > 0 else 0.0

    def fmt(self) -> str:
        """'8N1', '7E2', '9N1', '8N1+A' (address bit)."""
        st = f"{self.stop:g}"
        return f"{self.bits}{self.parity}{st}" + ("+A" if self.addr is not None else "")

    def __repr__(self) -> str:
        if self.brk:
            return f"Frame(break {self.brk} bits @{self.baud:g})"
        return f"Frame(0x{self.data:x} {self.fmt()} @{self.baud:g}{' msbf' if self.msbf else ''}" \
               f"{' inv' if self.inv else ''})"


class RxFormat:
    """What a receiver expects (built by the family model's `rx_format()`)."""
    __slots__ = ("baud", "bits", "parity", "stop", "addr", "msbf", "inv", "samples", "start", "start_all", "resync")

    def __init__(self, baud: float, bits: int = 8, parity: str = "N", stop: float = 1.0, *, addr: bool = False,
                 msbf: bool = False, inv: bool = False, samples: tuple = SAMPLES_16, start: tuple = START_16,
                 start_all: bool = False, resync: bool = False):
        self.baud = float(baud or 0.0)
        self.bits = int(bits)
        self.parity = parity
        self.stop = float(stop)
        self.addr = bool(addr)
        self.msbf = bool(msbf)
        self.inv = bool(inv)
        self.samples = samples
        self.start = start
        self.start_all = start_all
        self.resync = resync

    def ncells(self) -> int:
        return self.bits + self.addr + (self.parity != "N")

    def frame(self, data: int, *, addr: int | None = None) -> Frame:
        """A frame in exactly this format (what a matching host terminal sends)."""
        return Frame(data, self.bits, self.parity, self.stop, self.baud,
                     addr=(addr or 0) if self.addr else None, msbf=self.msbf, inv=self.inv)

    def bit_ns(self) -> float:
        return 1e9 / self.baud if self.baud > 0 else 0.0


class RxChar:
    """A character as the receiver decoded it. `data`: the data bits (incl. a 9th bit), `addr`: address bit (or
    None), `pbit`: the received parity bit (or None), `pe`/`fe`/`nf`: parity / framing (first stop bit 0) / noise,
    `brk`: a break character (all cells and the stop bit 0): the line's low time in receiver bit times (0 otherwise),
    `idle`: idle bit times on the line before its start bit (inf if unknown), `t`: stop-bit sample time (ns)."""
    __slots__ = ("data", "addr", "pbit", "pe", "fe", "nf", "brk", "idle", "t")

    def __init__(self, data: int, addr: int | None = None, pbit: int | None = None, pe: bool = False,
                 fe: bool = False, nf: bool = False, brk: float = 0.0, idle: float = float("inf"), t: float = 0.0):
        self.data, self.addr, self.pbit = data, addr, pbit
        self.pe, self.fe, self.nf = pe, fe, nf
        self.brk, self.idle, self.t = brk, idle, t

    def flags(self) -> dict:
        d: dict = {}
        if self.fe:
            d["fe"] = 1
        if self.pe:
            d["pe"] = 1
        if self.nf:
            d["nf"] = 1
        if self.brk:
            d["brk"] = round(self.brk, 1)
        return d

    def __repr__(self) -> str:
        fl = " ".join(k.upper() for k in self.flags())
        return f"RxChar(0x{self.data:x}{' ' + fl if fl else ''})"


def interpret(raw: int, rx: RxFormat) -> tuple[int, int | None, int | None, bool]:
    """Cells (bit i = i-th cell after the start bit) -> (data, addr, pbit, parity error) by the receiver's layout."""
    n = rx.bits
    d = raw & ((1 << n) - 1)
    if rx.msbf:
        d = int(f"{d:0{n}b}"[::-1], 2) if n else 0
    addr = pbit = None
    k = n
    v, vn = d, n
    if rx.addr:
        addr = (raw >> k) & 1
        v, vn = v | (addr << n), n + 1
        k += 1
    pe = False
    if rx.parity != "N":
        pbit = (raw >> k) & 1
        pe = pbit != parity_bit(v, vn, rx.parity)
    return d, addr, pbit, pe


_ALIGN: dict = {}


def aligned(tx_baud: float, rx_baud: float, ncells: int, samples: tuple) -> bool:
    """True if every sample point of a receiver at `rx_baud` (start, `ncells` cells, first stop bit) falls inside the
    corresponding bit of a transmitter at `tx_baud` (both clocks start at the start-bit edge)."""
    if tx_baud <= 0 or rx_baud <= 0:
        return True
    key = (tx_baud, rx_baud, ncells, samples)
    ok = _ALIGN.get(key)
    if ok is None:
        r = tx_baud / rx_baud                       # receiver bit time in transmitter bit times
        ok = all(k <= (k + f) * r < k + 1 for k in range(ncells + 2) for f in samples)
        if len(_ALIGN) > 4096:
            _ALIGN.clear()
        _ALIGN[key] = ok
    return ok


class LineDecoder:
    """One receiver's view of its RX line (see the module docstring). `deliver(ch)` gets each decoded RxChar;
    `fmt()` returns the receiver's current RxFormat; `sched` is the receiver chip's scheduler (None: stateless use,
    every push is decoded to the end against an idle line)."""

    def __init__(self, deliver, fmt, sched: Any = None, now=None):
        self.out = deliver
        self.fmt = fmt
        self.sched = sched
        self.now = now
        self.ev = None
        self.qev = None
        self.reset()

    def reset(self) -> None:
        if self.sched is not None:
            for ev in (self.ev, self.qev):
                if ev is not None:
                    self.sched.cancel(ev)
        self.ev = self.qev = None
        self.q: deque = deque()            # decoded characters waiting for their delivery time
        self.last_ch_t: float | None = None
        self.last_td = 0.0
        self.t: list[float] = []           # transition times (ns)
        self.v: list[int] = []             # physical line level after each transition
        self.v0 = 1                        # level before the first transition
        self.pos = float("-inf")           # hunting for a start bit from here
        self.horizon = float("-inf")       # line known up to here
        self.last_dur = 0.0
        self.last_end = float("-inf")      # end of the last received character's first stop bit
        self.final_at = None

    @property
    def active(self) -> bool:
        return bool(self.t) or bool(self.q)

    def _now(self) -> float:
        return self.now() if self.now is not None else (self.sched.now_ns() if self.sched is not None else 0.0)

    def deliver(self, ch: RxChar) -> None:
        """Line-path characters keep their spacing: each is delivered as long after the previous one as their stop
        samples are apart, but never before now (the decode happens at the end of the frame that completes it)."""
        if self.sched is None:
            self.out(ch)
            return
        now = self._now()
        td = now if self.last_ch_t is None else max(now, self.last_td + (ch.t - self.last_ch_t))
        self.last_ch_t, self.last_td = ch.t, td
        if not self.q and td <= now + 1e-6:
            self.out(ch)
            return
        self.q.append((td, ch))
        if self.qev is None:
            self.qev = self.sched.at_ns(self.q[0][0], self._drain)

    def _drain(self) -> None:
        self.qev = None
        now = self._now()
        while self.q and self.q[0][0] <= now + 1e-6:
            self.out(self.q.popleft()[1])
        if self.q:
            self.qev = self.sched.at_ns(self.q[0][0], self._drain)

    # ------------------------------------------------------------------ input
    def push(self, fr: Frame, t_end: float, rx: RxFormat | None = None) -> None:
        rx = rx or self.fmt()
        tb = fr.baud if fr.baud > 0 else (rx.baud if rx.baud > 0 else HOST_FALLBACK_BAUD)
        dur = fr.duration_ns(tb)
        t0 = t_end - dur
        if not self.t and not fr.brk and fr.inv == rx.inv and fr.ncells() == rx.ncells() and \
                (fr.msbf == rx.msbf or fr.bits <= 1) and aligned(fr.baud, rx.baud, rx.ncells(), rx.samples):
            # fast path: the receiver samples every transmitted bit where it is
            data, addr, pbit, pe = interpret(fr.raw(), rx)
            rb = rx.bit_ns() or (dur / fr.nbits() if fr.nbits() else 0.0)
            idle = (t0 - self.last_end) / rb if rb > 0 and self.last_end > float("-inf") else float("inf")
            self.last_end = t0 + (rx.ncells() + 2) * rb if rb > 0 else t_end
            self.horizon = max(self.horizon, t_end)
            self.pos = self.horizon
            self.last_ch_t = None
            self.out(RxChar(data, addr, pbit, pe, idle=idle, t=t_end))
            return
        if t0 < self.horizon:                   # overlaps what is already on the line: queue behind it
            t0 = self.horizon
            t_end = t0 + dur
        idle_lvl = 1 ^ fr.inv
        if not self.t:
            self.v0 = idle_lvl
            self.pos = min(self.pos, t0)
        tbit = 1e9 / tb
        t = t0
        cells: list[tuple[int, float]]
        if fr.brk:
            cells = [(0, fr.brk), (1, fr.stop)]
        else:
            raw, n = fr.raw(), fr.ncells()
            cells = [(0, 1)] + [((raw >> i) & 1, 1) for i in range(n)] + [(1, fr.stop)]
        for lvl, nb in cells:
            self._set(t, lvl ^ fr.inv)
            t += nb * tbit
        self._set(t_end, idle_lvl)
        self.horizon = max(self.horizon, t_end)
        self.last_dur = dur
        self._run(False)

    def _set(self, t: float, lvl: int) -> None:
        cur = self.v[-1] if self.v else self.v0
        if lvl == cur:
            return
        if self.t and t <= self.t[-1]:
            t = self.t[-1] + 1e-3
        self.t.append(t)
        self.v.append(lvl)

    def _lvl(self, t: float) -> int:
        i = bisect_right(self.t, t) - 1
        return self.v[i] if i >= 0 else self.v0

    # ------------------------------------------------------------------ decoding
    def _run(self, final: bool) -> None:
        rx = self.fmt()
        tb = rx.bit_ns()
        if tb <= 0:
            tb = 1e9 / HOST_FALLBACK_BAUD
        inv = 1 if rx.inv else 0
        n = rx.ncells()
        while True:
            te = self._next_fall(self.pos, inv)
            if te is None:
                self.pos = max(self.pos, self.horizon)
                break
            need = self._char(te, tb, n, rx, inv, final)
            if need is not None:                # its samples reach past what has been transmitted so far
                self._wait(need)
                return
        self._trim()

    def _next_fall(self, pos: float, inv: int) -> float | None:
        i = bisect_left(self.t, pos)
        while i < len(self.t):
            if (self.v[i] ^ inv) == 0 and ((self.v[i - 1] if i else self.v0) ^ inv) == 1:
                return self.t[i]
            i += 1
        return None

    def _bit(self, base: float, tb: float, fs: tuple, inv: int) -> tuple[int, bool]:
        s = [self._lvl(base + f * tb) ^ inv for f in fs]
        ones = sum(s)
        return (1 if 2 * ones > len(s) else 0), (0 < ones < len(s))

    def _char(self, te: float, tb: float, n: int, rx: RxFormat, inv: int, final: bool) -> float | None:
        """Decode the character whose start edge is at `te`; None when done, else the line time still needed."""
        fs = rx.samples
        st = [self._lvl(te + f * tb) ^ inv for f in rx.start]
        st_c, nf = self._bit(te, tb, fs, inv)
        if te + fs[-1] * tb > self.horizon and not final:
            return te + fs[-1] * tb
        nf = nf or any(st)
        if (any(st) if rx.start_all else 2 * sum(st) > len(st)) or st_c:
            # not a valid start bit: search again once the line is back at 1
            r = self._next_rise(te, inv)
            if r is None and not final:
                return self.horizon + tb
            self.pos = r if r is not None else max(self.horizon, te + tb)
            return None
        anchor, ka = te, 0
        cells = 0
        prev = 0
        for k in range(1, n + 1):
            base = anchor + (k - ka) * tb
            v, noisy = self._bit(base, tb, fs, inv)
            if rx.resync and prev == 1 and v == 0:
                e = self._next_fall(base - tb + fs[-1] * tb, inv)
                if e is not None and e < base + fs[-1] * tb:
                    anchor, ka = e, k
                    base = e
                    v, noisy = self._bit(base, tb, fs, inv)
            nf = nf or noisy
            cells |= v << (k - 1)
            prev = v
        base = anchor + (n + 1 - ka) * tb
        if base + fs[-1] * tb > self.horizon and not final:
            return base + fs[-1] * tb
        stop, noisy = self._bit(base, tb, fs, inv)
        nf = nf or noisy
        t_stop = base + fs[len(fs) // 2] * tb
        data, addr, pbit, pe = interpret(cells, rx)
        brk = 0.0
        if cells == 0 and stop == 0:
            tr = self._next_rise(te, inv)
            if tr is None:
                if not final:
                    return self.horizon + tb
                tr = max(self.horizon, t_stop)
            brk = (tr - te) / tb
            self.pos = tr
        else:
            self.pos = t_stop
        idle = (te - self.last_end) / tb if self.last_end > float("-inf") else float("inf")
        self.last_end = base + tb
        self.deliver(RxChar(data, addr, pbit, pe, fe=not stop, nf=nf, brk=brk, idle=idle, t=t_stop))
        return None

    def _next_rise(self, t: float, inv: int) -> float | None:
        i = bisect_right(self.t, t)
        while i < len(self.t):
            if (self.v[i] ^ inv) == 1:
                return self.t[i]
            i += 1
        return None

    def _trim(self) -> None:
        i = bisect_right(self.t, self.pos)
        if i:
            self.v0 = self.v[i - 1]
            del self.t[:i]
            del self.v[:i]
        if self.ev is not None and not self.t and self.sched is not None:
            self.sched.cancel(self.ev)
            self.ev = None

    def _wait(self, need: float) -> None:
        if self.sched is None:
            self._run(True)
            return
        if self.ev is not None:
            self.sched.cancel(self.ev)
        now = self.now() if self.now is not None else self.sched.now_ns()
        self.ev = self.sched.at_ns(max(need, now) + max(self.last_dur, 0.0) + 1.0, self._final)

    def _final(self) -> None:
        self.ev = None
        now = self.now() if self.now is not None else self.sched.now_ns()
        self.horizon = max(self.horizon, now - self.last_dur)
        self._run(True)


def decode_frame(fr: Frame, rx: RxFormat) -> list[RxChar]:
    """Stateless: what a receiver set to `rx` reads from this one frame on an otherwise idle line (host ports)."""
    out: list[RxChar] = []
    d = LineDecoder(out.append, lambda: rx)
    d.push(fr, 0.0, rx)
    if d.active:
        d._run(True)
    return out


class UartRx:
    """Mixin for an MMIOBlock UART model. The model provides `char_ns()`, `rx_format()`, `rx_signal`,
    `_rx_block_reason()` and `_rx_accept(ch: RxChar)`, calls `_rx_init()` before its first reset() and
    `_wire_reset()` from reset(); it transmits through `_tx_emit(frame)`."""

    rx_signal: str = ""

    def _rx_init(self) -> None:
        self._wire: deque = deque()
        self._wire_ev = None
        self.rx_dropped = 0
        self.rx_errors = 0
        self._rx_pins: list | None = None
        self._nopin_noted = False
        self._ln: LineDecoder | None = None
        self.frame_listeners: list = []       # cb(uart, Frame) for every transmitted frame (links, router, bridges)
        self.tx_frame: Frame | None = None    # the frame being transmitted / last transmitted

    # ------------------------------------------------------------------ transmit
    def _tx_emit(self, fr: Frame) -> None:
        """A frame enters the shift register (start bit now): log it and hand it to the listeners."""
        self.tx_frame = fr
        if fr.brk:
            self.emit("uart", uart=self.name, dir="tx", brk=fr.brk)
        else:
            b = fr.data & 0xFF
            self.output.append(b)
            if fr.bits > 8 and fr.data & 0x100:
                self.emit("uart", uart=self.name, dir="tx", byte=b, b8=1)
            else:
                self.emit("uart", uart=self.name, dir="tx", byte=b)
        for cb in self.frame_listeners:
            cb(self, fr)
        if not fr.brk:
            for cb in self.tx_listeners:
                cb(self, fr.data & 0xFF)

    # ------------------------------------------------------------------ receive format
    def rx_format(self) -> RxFormat:                   # family: what the receiver expects now
        return RxFormat(self.baud() if hasattr(self, "baud") else 0.0)

    def _own_frame(self, b: int) -> Frame:
        return self.rx_format().frame(b)

    # ------------------------------------------------------------------ host line
    def feed(self, data: bytes) -> None:
        """Host -> chip: transmit `data` from now on, in this UART's format, paced at its character time."""
        data = bytes(data)
        if not data:
            return
        self._wire.extend(data)
        self._wire_kick()

    def feed_break(self, bits: int | None = None) -> None:
        """Host -> chip: a break of `bits` low bit times (default: two character times, at least 13 bits) after
        what the host is still sending."""
        if bits is None:
            try:
                cb = float(self.char_bits())
            except Exception:
                cb = 10.0
            bits = max(13, int(round(2 * cb)))
        self._wire.append(("brk", int(bits)))
        self._wire_kick()

    def _wire_kick(self) -> None:
        if self._wire_ev is None and self.chip is not None and self._wire:
            self._wire_ev = self.chip.sched.in_ns(self._wire_item_ns(self._wire[0]), self._wire_tick)

    def _host_char_ns(self) -> float:
        t = self.char_ns()
        return t if t > 0 else HOST_FALLBACK_BITS * 1e9 / HOST_FALLBACK_BAUD

    def _wire_item_ns(self, item: Any) -> float:
        if isinstance(item, tuple):
            rx = self.rx_format()
            b = rx.baud if rx.baud > 0 else HOST_FALLBACK_BAUD
            return (item[1] + rx.stop) * 1e9 / b
        return self._host_char_ns()

    def _wire_tick(self) -> None:
        self._wire_ev = None
        if not self._wire:
            return
        item = self._wire.popleft()
        if isinstance(item, tuple):
            rx = self.rx_format()
            self._rx_frame(Frame.brk_frame(item[1], rx.stop, rx.baud, inv=rx.inv), "host")
        else:
            self._rx_arrive(item, "host")
        self._wire_kick()

    def _wire_reset(self) -> None:
        """Model reset: cancel our scheduler events; the host keeps transmitting what it still has."""
        ev = getattr(self, "_wire_ev", None)
        if ev is not None and self.chip is not None:
            self.chip.sched.cancel(ev)
        self._wire_ev = None
        if getattr(self, "_ln", None) is not None:
            self._ln.reset()
        if getattr(self, "_wire", None) and self.chip is not None:
            self._wire_kick()

    def wire_pending(self) -> int:
        """Host bytes not yet on the wire."""
        return len(self._wire)

    # ------------------------------------------------------------------ board links
    def receive(self, data: bytes) -> None:
        """Legacy board-link hook: each byte's stop bit arrives now, in this receiver's own format."""
        for b in bytes(data):
            self._rx_arrive(b, "link")

    def receive_frame(self, fr: Frame, t_end: float | None = None) -> None:
        """A transmitted frame whose last stop bit ended at `t_end` (default now) reaches the RX pin."""
        self._rx_frame(fr, "link", t_end)

    # ------------------------------------------------------------------ arrival
    def _rx_arrive(self, b: int, src: str) -> None:
        """A character in the receiver's own format (host terminal, loopback, legacy links)."""
        self._rx_frame(self._own_frame(b), src)

    def _rx_frame(self, fr: Frame, src: str, t_end: float | None = None) -> None:
        why = self._rx_block_reason()
        if why is None and src not in ("loop", "direct"):
            why = self._rx_pin_reason(src)
        if why is not None:
            self._rx_drop(fr.data if not fr.brk else 0, why)
            if self._ln is not None:
                self._ln.reset()
            return
        ln = self._ln
        if ln is None:
            chip = self.chip
            ln = self._ln = LineDecoder(self._rx_char, self.rx_format, chip.sched if chip is not None else None,
                                        chip.now_ns if chip is not None else None)
            ln.lp_owner = self                          # low-power freezers hold its events with the block
        now = self.chip.now_ns() if self.chip is not None else 0.0
        ln.push(fr, now if t_end is None or t_end > now else t_end)

    def _rx_char(self, ch: RxChar) -> None:
        why = self._rx_block_reason()
        if why is not None:                             # the receiver stopped listening while decoding
            self._rx_drop(ch.data, why)
            return
        self._rx_accept(ch)

    def _rx_event(self, ch: RxChar, word: int | None = None) -> None:
        """Log a received character (with its error flags)."""
        w = ch.data if word is None else word
        fl = ch.flags()
        if fl:
            self.rx_errors += 1
        if w > 0xFF:
            fl["b8"] = 1
        self.emit("uart", uart=self.name, dir="rx", byte=w & 0xFF, **fl)

    def _rx_drop(self, b: int, why: str) -> None:
        self.rx_dropped += 1
        self.emit("uart", uart=self.name, dir="drop", byte=b & 0xFF, dropped=1, reason=why)

    def _rx_block_reason(self) -> str | None:          # family: receiver enable / clock gate / module reset
        return None

    def _rx_accept(self, ch: RxChar) -> None:          # family: holding register / FIFO / overrun / error flags
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
