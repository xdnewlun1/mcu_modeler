"""Generic CAN bus: frames on a wire shared by several controller nodes (any family), with real bus timing.

Vendor-free (docs/CONTRACTS.md "CAN bus"): the controller models (kinetis.flexcan, ti_c2000.ecan, later stm32.can)
implement the node protocol below; the bus decides who transmits, when a frame ends, who receives and whether it is
acknowledged.  A board joins controllers of several chips (`links: [{type: can, nodes: [A.CAN0, B.ECANA]}]`,
chipem/board.py `CanLink`); a controller that is on no board bus sits alone on a private bus (no other node: its
frames are not acknowledged, as on a real unterminated bus), and its loopback / self-test mode uses a private
`CanBus(self_ack=True)`.

Timing (ISO 11898-1 / Bosch CAN 2.0B frame format).
* A frame takes `CanFrame.bits` bit times of the transmitter's bit rate from SOF to the end of EOF: the exact frame
  length *with* stuff bits, computed from the real bit stream (SOF, arbitration and control fields, data, CRC-15 are
  stuffed: a complementary bit after 5 equal bits), plus CRC delimiter, ACK slot, ACK delimiter and 7 EOF bits.
  Without stuffing that is 44 + 8*DLC bits (standard data frame) or 64 + 8*DLC (extended); remote frames carry no
  data.  Then 3 intermission bits: the next SOF is at least 3 bits after the previous EOF.
* Arbitration: every node that is ready (it asked with `request()`) at the moment the bus becomes free (or within
  the first bit time of a SOF another node started on an idle bus) competes; the lowest arbitration value wins
  (`CanFrame.arb`: the identifier bits in transmission order, RTR/SRR/IDE included, so a standard data frame beats an
  extended or remote frame with the same base ID).  The losers simply stay ready and compete again after the
  winner's frame (no frame is lost).  Chips run in lockstep quanta, so a request may arrive from a chip that is
  behind: a decision is revised while the winner's SOF bit has not passed in the requester's time.
* ACK: at the ACK slot (bit L-9) the bus looks at the other nodes.  A node acknowledges when it is on the bus in
  normal mode (`can_rx_state() == "active"`) with a bit time within CAN_BITRATE_TOLERANCE of the transmitter's.  A
  listen-only node ("listen") receives but does not acknowledge.  No acknowledgement -> ACK error: the transmitter
  sends an error frame starting at the ACK delimiter (6 flag + 8 delimiter bits, then the intermission) and gets
  `can_tx_done(ok=False, err="ack")`; nobody receives the frame (the error frame destroys it) and the transmitter
  retries automatically (the controller keeps its request).
* Bit-rate mismatch (simplified): a receiver whose bit time differs by more than the tolerance does not receive or
  acknowledge the frame and counts a form error (`can_rx_error(kind="form")`).  (On a real bus it would also send
  error flags that destroy the frame for everyone; not modelled.)
* Error-passive transmitters wait 8 more bit times ("suspend transmission") after their frame before they compete
  again.
* A transmitter whose TX/RX pins are not muxed to the controller (`can_tx_connected()` False) puts nothing on the
  wire: it reads back recessive where it sent the dominant SOF -> bit error after 1 bit (`err="bit0"`), local to
  that node (the bus stays free for the others), retried after the error frame.

Error counting (fault confinement) is done by the controllers with `CanErrorCounters` (TEC/REC rules of the CAN
spec): transmit error +8 (ACK errors no longer count once error passive), successful transmission -1, receive
error +1 (frozen at 128), successful reception -1 (set back to 127 from >127); warning >= 96, error passive >= 128,
bus off when TEC > 255; recovery after 128 x 11 recessive bits.

Node protocol (duck-typed; every callback gets the bus first):
    chip                                   the node's Chip (scheduler, now_ns); `log` its EventLog (optional)
    can_name                               short name for logs ("CAN0", "ECANA")
    can_bit_ns() -> float                  nominal bit time (0: not configured / no clock)
    can_tx_frame(bus) -> CanFrame | None   the frame it would send now (its internal arbitration winner)
    can_tx_connected(bus) -> bool          TX and RX pins muxed to the controller (else local bit errors)
    can_tx_done(bus, frame, ok, err, t_sof_ns, t_ns)
    can_rx_state(bus) -> None | "listen" | "active"     None = not on the bus (off, init/freeze, gated, no RX pin)
    can_rx(bus, frame, t_sof_ns, t_ns)     a valid frame ended (EOF) at t_ns; acceptance filtering is the node's
    can_rx_error(bus, kind, t_ns)          "form" (bit-rate mismatch), "noack" (listen-only saw an unacked frame)
    can_error_passive() -> bool            optional
A node calls `bus.request(node)` whenever it gets something to send (or its best frame changes), `bus.withdraw(node)`
when it no longer can (freeze, init mode) and `bus.node_reset(node)` from its reset().

Events: every completed frame is logged once as a `can` event on the transmitter's EventLog: {link, node, dir: "tx",
id, ext, rtr, dlc, data (hex), bits, sof_ns}; an unacknowledged attempt as {..., err: "ack"} (only the first of a
run of failed attempts, `failed` counts the rest).  `listeners` get (bus, frame, info) for every completed frame.
"""
from __future__ import annotations

import math
from functools import lru_cache
from typing import Any, Callable

CAN_BITRATE_TOLERANCE = 0.015           # relative bit-time difference a receiver still synchronises to
INTERMISSION = 3
ERROR_FRAME = 14                        # 6 error flag + 8 error delimiter bits
SUSPEND = 8                             # error-passive "suspend transmission"
BUS_IDLE_BITS = 11                      # recessive bits a joining node waits for (bus integration)
BUSOFF_RECOVERY_BITS = 128 * 11

__all__ = ["CanFrame", "CanBus", "CanErrorCounters", "HostNode", "frame_bits", "crc15", "parse_frame",
           "CAN_BITRATE_TOLERANCE", "INTERMISSION", "ERROR_FRAME", "BUS_IDLE_BITS", "BUSOFF_RECOVERY_BITS",
           "pin_reason"]


# ------------------------------------------------------------------------------------------------ frames
def crc15(bits) -> int:
    """CAN CRC-15 (polynomial 0x4599, init 0) over a bit sequence."""
    crc = 0
    for b in bits:
        nxt = b ^ ((crc >> 14) & 1)
        crc = (crc << 1) & 0x7FFF
        if nxt:
            crc ^= 0x4599
    return crc


def _bits(v: int, n: int) -> list[int]:
    return [(v >> i) & 1 for i in range(n - 1, -1, -1)]


@lru_cache(maxsize=4096)
def frame_bits(ident: int, ext: bool, rtr: bool, dlc: int, data: bytes) -> tuple[int, int, int]:
    """(total bits SOF..EOF incl. stuff bits, number of stuff bits, CRC-15) of a frame."""
    s = [0]                                                     # SOF (dominant)
    if ext:
        s += _bits(ident >> 18, 11) + [1, 1] + _bits(ident & 0x3FFFF, 18) + [1 if rtr else 0, 0, 0]
    else:
        s += _bits(ident, 11) + [1 if rtr else 0, 0, 0]          # RTR, IDE, r0
    s += _bits(dlc, 4)
    if not rtr:
        for byte in data:
            s += _bits(byte, 8)
    crc = crc15(s)
    s += _bits(crc, 15)
    stuffed, prev, run = 0, -1, 0
    for b in s:
        if b == prev:
            run += 1
        else:
            prev, run = b, 1
        if run == 5:
            stuffed += 1
            prev, run = 1 - b, 1
    return len(s) + stuffed + 10, stuffed, crc                 # + CRC delim, ACK slot, ACK delim, 7 EOF


class CanFrame:
    """One CAN 2.0B frame. `data` is truncated/padded to the transmitted length (min(dlc, 8), 0 for remote frames).
    `tag` is the sender's private marker (e.g. its mailbox number); not part of equality."""
    __slots__ = ("id", "ext", "rtr", "dlc", "data", "tag")

    def __init__(self, ident: int, data: bytes | bytearray | list = b"", *, ext: bool | None = None,
                 rtr: bool = False, dlc: int | None = None, tag: Any = None):
        data = bytes(data)
        if ext is None:
            ext = ident > 0x7FF
        self.ext = bool(ext)
        self.id = int(ident) & (0x1FFFFFFF if self.ext else 0x7FF)
        self.rtr = bool(rtr)
        if dlc is None:
            dlc = len(data)
        self.dlc = int(dlc) & 0xF
        n = 0 if self.rtr else min(self.dlc, 8)
        self.data = (data + bytes(8))[:n]
        self.tag = tag

    @property
    def arb(self) -> int:
        """Arbitration value (lower wins): the arbitration-field bits in transmission order (FlexCAN RM Table 46-75)."""
        if self.ext:
            return ((self.id >> 18) << 21) | (1 << 20) | (1 << 19) | ((self.id & 0x3FFFF) << 1) | int(self.rtr)
        return (self.id << 21) | (int(self.rtr) << 20)

    @property
    def bits(self) -> int:
        return frame_bits(self.id, self.ext, self.rtr, self.dlc, self.data)[0]

    @property
    def stuff_bits(self) -> int:
        return frame_bits(self.id, self.ext, self.rtr, self.dlc, self.data)[1]

    @property
    def crc(self) -> int:
        return frame_bits(self.id, self.ext, self.rtr, self.dlc, self.data)[2]

    def key(self) -> tuple:
        return (self.id, self.ext, self.rtr, self.dlc, self.data)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, CanFrame) and self.key() == other.key()

    def __hash__(self) -> int:
        return hash(self.key())

    def __str__(self) -> str:
        """candump style: 123#DEADBEEF, 1ABCDEF0#00, 123#R2 (remote, DLC 2)."""
        ident = f"{self.id:08X}" if self.ext else f"{self.id:03X}"
        if self.rtr:
            return f"{ident}#R{self.dlc if self.dlc else ''}"
        return f"{ident}#{self.data.hex().upper()}"

    def __repr__(self) -> str:
        return f"CanFrame({self})"


def parse_frame(spec: str) -> CanFrame:
    """'123#DEADBEEF' (11-bit), '12345678#00' or any 8-digit id (29-bit), '123#R' / '123#R4' (remote)."""
    s = str(spec).strip()
    if "#" not in s:
        raise ValueError(f"bad CAN frame {spec!r} (expected ID#DATA, e.g. 123#DEADBEEF)")
    ident_s, data_s = s.split("#", 1)
    ident = int(ident_s, 16)
    ext = len(ident_s) > 3 or ident > 0x7FF
    if data_s[:1].upper() == "R":
        n = data_s[1:]
        return CanFrame(ident, ext=ext, rtr=True, dlc=int(n) if n else 0)
    data_s = data_s.replace(".", "").replace(" ", "")
    if len(data_s) % 2 or len(data_s) > 16:
        raise ValueError(f"bad CAN frame data {data_s!r} (0..8 bytes as hex digits)")
    return CanFrame(ident, bytes.fromhex(data_s), ext=ext)


# ------------------------------------------------------------------------------------------------ error counters
class CanErrorCounters:
    """TEC/REC and the fault confinement state of one controller (CAN 2.0B part B, ch. 8).

    The update methods return the transitions they caused, a set of: "tx_warn" / "rx_warn" (counter reached 96),
    "passive", "busoff", "active" (back to error active)."""
    WARN, PASSIVE = 96, 128

    def __init__(self) -> None:
        self.tec = 0
        self.rec = 0
        self.busoff = False

    @property
    def state(self) -> str:
        if self.busoff:
            return "busoff"
        if self.tec >= self.PASSIVE or self.rec >= self.PASSIVE:
            return "passive"
        return "active"

    def _snap(self):
        return self.state, self.tec >= self.WARN, self.rec >= self.WARN

    def _diff(self, before) -> set[str]:
        st0, tw0, rw0 = before
        st1, tw1, rw1 = self._snap()
        out = set()
        if tw1 and not tw0 and not self.busoff:
            out.add("tx_warn")
        if rw1 and not rw0:
            out.add("rx_warn")
        if st1 != st0:
            out.add(st1)
        return out

    def reset(self) -> set[str]:
        b = self._snap()
        self.tec = self.rec = 0
        self.busoff = False
        return self._diff(b)

    def set(self, tec: int, rec: int) -> set[str]:
        """CPU write (FlexCAN ECR in freeze mode)."""
        b = self._snap()
        self.tec, self.rec = tec & 0xFF, rec & 0xFF
        return self._diff(b)

    def tx_error(self, kind: str = "bit") -> set[str]:
        b = self._snap()
        if self.busoff:
            return set()
        if kind == "ack" and self.state == "passive":
            return set()                     # exception 1: an error-passive transmitter's ACK error does not count
        self.tec += 8
        if self.tec > 255:
            self.busoff = True
            self.tec = 256                   # reads as 0 in an 8-bit field (FlexCAN resets TXERRCNT on bus off)
            self.rec = 0
        return self._diff(b)

    def tx_ok(self) -> set[str]:
        b = self._snap()
        if self.tec > 0 and not self.busoff:
            self.tec -= 1
        return self._diff(b)

    def rx_error(self) -> set[str]:
        b = self._snap()
        if not self.busoff and self.rec < self.PASSIVE:
            self.rec += 1
        return self._diff(b)

    def rx_ok(self) -> set[str]:
        b = self._snap()
        if self.rec > 127:
            self.rec = 127                   # "a value between 119 and 127"
        elif self.rec > 0:
            self.rec -= 1
        return self._diff(b)

    def recover(self) -> set[str]:
        """Bus-off recovery (128 x 11 recessive bits seen): error active, both counters 0."""
        return self.reset()


# ------------------------------------------------------------------------------------------------ pins
def pin_reason(chip: Any, signal: str, cache: dict) -> str | None:
    """None if a pin offering `signal` (e.g. "CAN0_RX", "CANRXA") is muxed to it on `chip`, "pin_not_muxed" if
    pins offer it but none is muxed, None as well if no pin of the chip description offers it at all (test chips
    without a pin table: the controller is then assumed to be wired directly).  `cache` keeps the pin list."""
    pins = cache.get(signal)
    if pins is None:
        pb = getattr(chip, "pins", None)
        pins = [] if pb is None else [p for p in pb.pins
                                     if any(signal in str(f).split("/") for f in (p.functions or {}).values())]
        cache[signal] = pins
    if not pins:
        return None
    for p in pins:
        own = p.owner
        if own == signal or signal in str(own).split("/"):
            return None
    return "pin_not_muxed"


# ------------------------------------------------------------------------------------------------ bus
def _at(chip: Any, t: float, cb: Callable, *args: Any):
    """Run cb at virtual time t on `chip` (now if that chip is already there): returns the Event or None."""
    if chip.now_ns() >= t:
        cb(*args)
        return None
    return chip.sched.at_ns(t, cb, *args)


class _Tx:
    __slots__ = ("node", "frame", "start", "bit", "ev", "end")

    def __init__(self, node, frame, start, bit):
        self.node, self.frame, self.start, self.bit = node, frame, start, bit
        self.ev = None
        self.end = start + frame.bits * bit


class CanBus:
    def __init__(self, name: str = "CAN", *, self_ack: bool = False):
        self.name = name
        self.self_ack = self_ack             # loopback / self-test bus: the transmitter acknowledges itself
        self.nodes: list = []
        self.listeners: list[Callable] = []  # cb(bus, frame, info dict) per completed frame
        self.frames = 0                      # completed frames
        self.error_frames = 0                # failed attempts (no ACK, bit errors)
        self.bytes = 0
        self._req: dict = {}                 # node -> time it became ready (ns)
        self._suspend: dict = {}             # node -> earliest next SOF (error-passive suspend)
        self._local: dict = {}               # node -> Event of a pending local (pin-less) failure
        self._cur: _Tx | None = None         # frame decided but not yet past its ACK slot
        self._onair: _Tx | None = None       # last frame on the wire (for status queries)
        self.idle_ns = -math.inf             # the bus is free for a SOF from this time on
        self._fails: dict = {}               # node -> consecutive unacknowledged attempts (log compression)
        self._fail_logged: set = set()
        self._defer = False

    def __repr__(self) -> str:
        return f"<CanBus {self.name}: {', '.join(getattr(n, 'can_name', '?') for n in self.nodes)}>"

    # ------------------------------------------------------------------ membership
    def attach(self, node: Any) -> None:
        if node not in self.nodes:
            self.nodes.append(node)

    def detach(self, node: Any) -> None:
        self.node_reset(node)
        if node in self.nodes:
            self.nodes.remove(node)

    def add_host(self, name: str = "host", *, ack: bool = True, chip: Any = None) -> "HostNode":
        """An external node (e.g. a PC CAN adapter): acknowledges frames when `ack`, records what it receives in
        `received`, and transmits frames given to `inject()`.  Its timing uses `chip` (default: the first node's)."""
        h = HostNode(self, name, ack=ack, chip=chip)
        self.attach(h)
        return h

    def inject(self, frame: CanFrame | str, t_ns: float | None = None, *, bit_ns: float | None = None) -> None:
        """Send `frame` from a host node (created on first use) at virtual time `t_ns` (now if None)."""
        host = next((n for n in self.nodes if isinstance(n, HostNode)), None) or self.add_host()
        host.send(frame if isinstance(frame, CanFrame) else parse_frame(frame), t_ns, bit_ns=bit_ns)

    # ------------------------------------------------------------------ status
    def activity(self, node: Any, t: float) -> str | None:
        """"tx" if `node` is transmitting at time t, "rx" if another node is, None if the bus is idle."""
        for x in (self._onair, self._cur):
            if x is not None and x.start <= t < x.end:
                return "tx" if x.node is node else "rx"
        return None

    def transmitting(self, node: Any) -> CanFrame | None:
        """The frame `node` has on the bus or has committed to (past arbitration), else None."""
        now = node.chip.now_ns()
        for x in (self._cur, self._onair):
            if x is not None and x.node is node and x.start + x.bit <= now < x.end:
                return x.frame
        return None

    # ------------------------------------------------------------------ requests
    def request(self, node: Any, t: float | None = None) -> None:
        """`node` has a frame to send from time t (default: its chip's now)."""
        if node in self._local:
            return
        if t is None:
            t = node.chip.now_ns()
        prev = self._req.get(node)
        if prev is None or t < prev:
            self._req[node] = t
        if self._defer:
            return                               # inside a tx_done callback: the bus arbitrates afterwards
        cur = self._cur
        if cur is None:
            self._arbitrate()
        elif t < cur.start + cur.bit:
            self._revise()

    def withdraw(self, node: Any) -> None:
        """`node` has nothing to send any more (or cannot): drop its request; a frame of it that has not passed its
        SOF bit yet is not started."""
        self._req.pop(node, None)
        cur = self._cur
        if cur is not None and cur.node is node and node.chip.now_ns() < cur.start + cur.bit:
            self._revise()

    def node_reset(self, node: Any) -> None:
        """Controller reset: forget its request; a frame of it in progress ends with an error frame now."""
        self._req.pop(node, None)
        self._suspend.pop(node, None)
        self._fails.pop(node, None)
        ev = self._local.pop(node, None)
        if ev is not None:
            node.chip.sched.cancel(ev)
        cur = self._cur
        if cur is not None and cur.node is node:
            node.chip.sched.cancel(cur.ev)
            self._cur = None
            now = node.chip.now_ns()
            if now >= cur.start + cur.bit:              # it was on the wire: the others see an error frame
                self.error_frames += 1
                self.idle_ns = now + (ERROR_FRAME + INTERMISSION) * cur.bit
            self._arbitrate()

    # ------------------------------------------------------------------ arbitration
    def _revise(self) -> None:
        cur = self._cur
        if cur is not None:
            cur.node.chip.sched.cancel(cur.ev)
            self._cur = None
        self._arbitrate()

    def _arbitrate(self) -> None:
        if self._cur is not None or not self._req:
            return
        cands = []
        for node, t_req in list(self._req.items()):
            fr = node.can_tx_frame(self)
            bit = node.can_bit_ns() if fr is not None else 0.0
            if fr is None or bit <= 0:
                del self._req[node]
                continue
            ready = max(t_req, self.idle_ns, self._suspend.get(node, -math.inf))
            if not node.can_tx_connected(self):
                del self._req[node]
                self._local_fail(node, fr, ready, bit)
                continue
            cands.append((ready, fr.arb, node, fr, bit))
        if not cands:
            return
        start = min(c[0] for c in cands)
        ready = [c for c in cands if c[0] < start + c[4]]        # ready within the first bit: they all send SOF
        _r, _a, node, fr, bit = min(ready, key=lambda c: (c[1], c[0]))
        tx = self._cur = _Tx(node, fr, start, bit)
        t_ack = start + (fr.bits - 8) * bit                     # end of the ACK slot
        tx.ev = _at(node.chip, t_ack, self._ack_point, tx)

    def _local_fail(self, node: Any, fr: CanFrame, start: float, bit: float) -> None:
        """No TX/RX pin: the SOF reads back recessive -> bit error, error frame, retry (nothing on the wire)."""
        t_err = start + bit

        def fail():
            self._local.pop(node, None)
            self.error_frames += 1
            self._suspend[node] = t_err + (ERROR_FRAME + INTERMISSION) * bit
            self._log_fail(node, fr, start, t_err, "bit0")
            node.can_tx_done(self, fr, False, "bit0", start, t_err)

        self._local[node] = node.chip.sched.at_ns(t_err, fail) if node.chip.now_ns() < t_err else None
        if self._local[node] is None:
            fail()

    def _ack_point(self, tx: _Tx) -> None:
        if self._cur is not tx:
            return
        self._cur = None
        node, fr, bit, start = tx.node, tx.frame, tx.bit, tx.start
        self._req.pop(node, None)
        t_ack = start + (fr.bits - 8) * bit
        receivers, mismatched, acked = [], [], self.self_ack
        for n in self.nodes:
            if n is node:
                continue
            st = n.can_rx_state(self)
            if not st:
                continue
            nb = n.can_bit_ns()
            if nb <= 0 or abs(nb - bit) > CAN_BITRATE_TOLERANCE * bit:
                mismatched.append(n)
                continue
            receivers.append(n)
            if st == "active":
                acked = True
        for n in mismatched:
            _at(n.chip, t_ack, n.can_rx_error, self, "form", t_ack)
        passive = getattr(node, "can_error_passive", None)
        if acked:
            self._onair = tx
            self.idle_ns = tx.end + INTERMISSION * bit
            if passive is not None and passive():
                self._suspend[node] = self.idle_ns + SUSPEND * bit
            else:
                self._suspend.pop(node, None)
            for n in receivers:
                _at(n.chip, tx.end, n.can_rx, self, fr, start, tx.end)
            _at(node.chip, tx.end, self._complete, tx)
            self._arbitrate()
            return
        # no acknowledgement: error frame from the ACK delimiter on, nobody receives the frame
        self._onair = _Tx(node, fr, start, bit)
        self._onair.end = t_ack + ERROR_FRAME * bit
        self.idle_ns = self._onair.end + INTERMISSION * bit
        self.error_frames += 1
        for n in receivers:
            _at(n.chip, t_ack, n.can_rx_error, self, "noack", t_ack)
        self._log_fail(node, fr, start, t_ack, "ack")
        self._defer = True
        try:
            node.can_tx_done(self, fr, False, "ack", start, t_ack)  # error counters first (passive -> suspend)
        finally:
            self._defer = False
        if passive is not None and passive():
            self._suspend[node] = self.idle_ns + SUSPEND * bit
        self._arbitrate()

    def _complete(self, tx: _Tx) -> None:
        node, fr = tx.node, tx.frame
        self.frames += 1
        self.bytes += len(fr.data)
        n_failed = self._fails.pop(node, 0)
        self._fail_logged.discard(node)
        info = {"link": self.name, "node": getattr(node, "can_name", "?"), "dir": "tx", "id": fr.id, "ext": fr.ext,
                "rtr": fr.rtr, "dlc": fr.dlc, "data": fr.data.hex().upper(), "bits": fr.bits, "sof_ns": tx.start}
        if n_failed:
            info["retries"] = n_failed
        self._log(node, tx.end, info)
        for cb in list(self.listeners):
            cb(self, fr, dict(info, t_ns=tx.end))
        node.can_tx_done(self, fr, True, None, tx.start, tx.end)

    # ------------------------------------------------------------------ logging
    def _log_fail(self, node: Any, fr: CanFrame, start: float, t: float, err: str) -> None:
        n = self._fails[node] = self._fails.get(node, 0) + 1
        if node in self._fail_logged:
            return
        self._fail_logged.add(node)
        self._log(node, t, {"link": self.name, "node": getattr(node, "can_name", "?"), "dir": "tx", "id": fr.id,
                            "ext": fr.ext, "rtr": fr.rtr, "dlc": fr.dlc, "data": fr.data.hex().upper(),
                            "bits": fr.bits, "sof_ns": start, "err": err,
                            "msg": f"{getattr(node, 'can_name', '?')}: frame {fr} not "
                                   + ("acknowledged (no other node listening); retrying"
                                      if err == "ack" else "sent: TX/RX pin not muxed (bit error); retrying")
                                   + (f" (attempt {n})" if n > 1 else "")})

    def _log(self, node: Any, t: float, fields: dict) -> None:
        log = getattr(node, "log", None)
        if log is None:
            log = next((getattr(n, "log", None) for n in self.nodes if getattr(n, "log", None) is not None), None)
        if log is not None:
            log.event("can", t, **fields)


# ------------------------------------------------------------------------------------------------ host node
class HostNode:
    """A node outside the emulated chips (a PC CAN interface, a test): sends injected frames at a fixed bit time
    (default: the bit time of the first chip node), acknowledges everything it hears when `ack`, keeps `received`
    [(t_ns, frame)]."""

    def __init__(self, bus: CanBus, name: str = "host", *, ack: bool = True, chip: Any = None):
        self.bus = bus
        self.can_name = name
        self.ack = ack
        self._chip = chip
        self.log = None
        self.received: list[tuple[float, CanFrame]] = []
        self.sent: list[tuple[float, CanFrame]] = []
        self._queue: list[tuple[float, CanFrame]] = []
        self.bit_ns: float | None = None

    @property
    def chip(self) -> Any:
        if self._chip is None:
            self._chip = next((getattr(n, "chip", None) for n in self.bus.nodes
                               if n is not self and getattr(n, "chip", None) is not None), None)
        return self._chip

    def send(self, frame: CanFrame, t_ns: float | None = None, *, bit_ns: float | None = None) -> None:
        if bit_ns:
            self.bit_ns = float(bit_ns)
        now = self.chip.now_ns()
        t = now if t_ns is None else max(float(t_ns), now)
        self._queue.append((t, frame))
        self._queue.sort(key=lambda x: x[0])
        if t <= now:
            self.bus.request(self, t)
        else:
            self.chip.sched.at_ns(t, self.bus.request, self, t)

    # node protocol
    def can_bit_ns(self) -> float:
        if self.bit_ns:
            return self.bit_ns
        for n in self.bus.nodes:
            if n is not self and not isinstance(n, HostNode):
                b = n.can_bit_ns()
                if b > 0:
                    return b
        return 0.0

    def can_tx_frame(self, bus: CanBus) -> CanFrame | None:
        now = self.chip.now_ns()
        due = [f for t, f in self._queue if t <= now + 1e-6]
        return min(due, key=lambda f: f.arb) if due else None

    def can_tx_connected(self, bus: CanBus) -> bool:
        return True

    def can_tx_done(self, bus: CanBus, frame: CanFrame, ok: bool, err: str | None, t_sof: float, t: float) -> None:
        if ok:
            for i, (_t, f) in enumerate(self._queue):
                if f is frame:
                    del self._queue[i]
                    break
            self.sent.append((t, frame))
        if self._queue:
            bus.request(self, t)

    def can_rx_state(self, bus: CanBus) -> str | None:
        return "active" if self.ack else "listen"

    def can_rx(self, bus: CanBus, frame: CanFrame, t_sof: float, t: float) -> None:
        self.received.append((t, frame))

    def can_rx_error(self, bus: CanBus, kind: str, t: float) -> None:
        pass
