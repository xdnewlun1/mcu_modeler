"""Serial-cable board parts: the PC end (`usb_serial_adapter`), an RS-232 line driver/receiver (`rs232_transceiver`,
MAX3232-like), connectors (`connector`, `db9`, `rj50`) and `cable`s with an explicit pin map. Generic glue: no vendor
knowledge; UART models are reached only through their board-link hooks (`tx_listeners`, `receive`, `baud`,
`char_bits`, `char_ns`) and pin owners.

How data travels (the "serial router", one per board, created with the first serial part):

* **Levels** are ordinary net levels. On an RS-232 net H means a positive voltage (+6 V: space / control line
  asserted) and L a negative one (-6 V: mark, the idle state); on a TTL net H is 3.3 V. A transceiver output is the
  inverse of its input; the adapter's TXD idles at mark (L on RS-232, H on TTL); RS-232 receiver inputs have their
  3-7 kOhm to ground (a pull-down: an open input reads as mark). Part states show the voltages.
* **Bytes** (UART models do not toggle their pins) travel by path resolution: a byte from a chip UART (its TX pin, as
  muxed now) or from the adapter's TXD walks the nets and parts (`serial_paths`: cable wires pass through, a
  transceiver channel inverts and changes the level domain, buf/not gates pass/invert) to the receivers it reaches:
  chip pins offering a UART RX function, the adapter's RXD. Each receiver gets the byte at its stop bit (sender's
  character time) if the path is electrically sound; otherwise it is lost, like on hardware:
  - polarity: the number of inversions on the way must leave the data in the receiver's sense (TTL: mark = high;
    RS-232 receiver: mark = negative). A TTL UART wired straight to an RS-232 port idles at "space": the receiver
    sees a break and no valid characters (reason `polarity`);
  - levels: RS-232 voltages on a TTL input (a 3.3 V MCU pin or a TTL adapter): over-voltage (`over_voltage`);
  - ground: the driver and the receiver of every segment must share a ground (`no_ground`); a chip's ground is a net
    with a `gnd` part, `drive: L` or one of its VSS pins; a transceiver needs its `gnd` port on a grounded (L) net;
  - a disabled stage (transceiver shut down / not powered) passes nothing (`stage_off`);
  - a chip RX pin not muxed to the UART -> `pin_not_muxed`; then the UART's own rules (not listening, overrun).
  A lost byte is a `uart` event dir=drop with the reason; the first loss per path and reason is also one `warn`.
  Frame format: each character travels as its transmitter's frame (chipem/periph/uartwire.py `Frame`: baud, data
  bits, parity bit, stop bits, breaks) and the receiver decodes it at its own settings: a chip UART through its line
  decoder (framing / parity / noise errors, garbage at a wrong baud), the adapter like the PC's UART (what a raw-mode
  tty reads: 0x00 for a framing error or a break; chipem/hostserial.py). A settings mismatch on a path is reported
  once as a `warn` (`framing`); the bytes are decoded, not dropped.
  A path with a swapped TX/RX cable simply reaches no receiver (TXD meets TXD): nothing arrives.
  Paths are cached per wiring/owner/part-state version, so the per-byte cost is a dictionary lookup.
* Direct chip-to-chip nets (no part in between) are not handled here: the board's automatic UART links do that.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .base import Part, PortDesc
from .registry import part
from .std import _bool

__all__ = ["UsbSerialAdapter", "Rs232Transceiver", "Connector", "Db9", "Rj50", "Cable", "serial_router",
           "SerialRouter", "DB9_DTE", "PRESETS"]

# DE-9 DTE pinout (EIA/TIA-574): PC serial port / USB-serial adapter, male connector
DB9_DTE = {1: "dcd", 2: "rxd", 3: "txd", 4: "dtr", 5: "gnd", 6: "dsr", 7: "rts", 8: "cts", 9: "ri"}

# Cable presets. DB9<->RJ50 pinouts are vendor specific (no standard): give `map:` explicitly in the board YAML.
PRESETS: dict[str, list[tuple[int, int]]] = {
    # DE-9 straight-through extension (pin n to pin n)
    "straight": [(i, i) for i in range(1, 10)],
    # DE-9 null modem with full handshaking (the common wiring, e.g. https://en.wikipedia.org/wiki/Null_modem):
    # TXD-RXD crossed, GND-GND, DTR -> DSR+DCD of the other side, RTS-CTS crossed; RI not connected
    "null_modem": [(3, 2), (2, 3), (5, 5), (4, 6), (4, 1), (6, 4), (1, 4), (7, 8), (8, 7)],
    # DE-9 three-wire null modem (no handshake lines)
    "null_modem_3wire": [(3, 2), (2, 3), (5, 5)],
}

_VOLTS = {"rs232": {"H": "+6.0V", "L": "-6.0V"}, "ttl": {"H": "3.3V", "L": "0V"}}


def _volts(domain: str, lvl: str | None) -> str:
    if lvl is None:
        return "open"
    return _VOLTS.get(domain, {}).get(lvl, "?")


def _inv(lvl: str | None) -> str:
    return {"H": "L", "L": "H"}.get(lvl or "X", "X")


def _escapes(text: str) -> bytes:
    from ..session import decode_escapes
    return decode_escapes(str(text))


# ================================================================================================ router
@dataclass
class _Sink:
    kind: str                         # "pin" | "part"
    label: str                        # "A.PTA1" | "PC.rxd"
    issues: tuple = ()                # ((code, message), ...) on the way
    cid: str | None = None
    pin: Any = None
    part: Any = None
    port: str | None = None
    path: str = ""


@dataclass
class _Walk:
    net: Any
    inv: int
    dom: str
    seg: Any                          # driver of the current segment: a Part or ("chip", cid)
    entry: tuple | None               # (part, port) we came in through
    issues: tuple = ()
    via: bool = False                 # crossed at least one part
    path: tuple = field(default_factory=tuple)


def serial_router(board: Any) -> "SerialRouter":
    r = getattr(board, "_serial_router", None)
    if r is None:
        r = SerialRouter(board)
        board._serial_router = r
    return r


class SerialRouter:
    """Byte transport along modelled serial chains of one board (see the module docstring)."""

    def __init__(self, board: Any):
        self.board = board
        self.version = 0
        self._key: tuple | None = None
        self._sinks: dict = {}
        self._txpins: dict = {}
        self._comp: dict[int, int] | None = None
        self._chip_gnd: dict[str, set] = {}
        self._warned: set = set()
        self._last: dict[int, float] = {}
        self._hooks: list = []
        self._unsubs: list = []
        self._rxb: dict = {}                # (cid, pin id) -> [(UART block, RX signal)] (static per chip description)
        for cid, chip in board.chips.items():
            blocks = chip.uart_blocks() if hasattr(chip, "uart_blocks") else {}
            for name, blk in blocks.items():
                cb = (lambda b_, x, _c=cid, _n=name: self._chip_tx(_c, _n, b_, x))
                lst = getattr(blk, "frame_listeners", None)
                lst = lst if lst is not None else blk.tx_listeners
                lst.append(cb)
                self._hooks.append((lst, cb))
            if chip.pins is not None:
                self._unsubs.append(chip.pins.subscribe(self._pin_event))

    def _pin_event(self, ev: Any) -> None:
        if ev.field == "owner":
            self.version += 1

    def changed(self) -> None:
        self.version += 1

    def close(self) -> None:
        for lst, cb in self._hooks:
            if cb in lst:
                lst.remove(cb)
        self._hooks = []
        for u in self._unsubs:
            u()
        self._unsubs = []
        if getattr(self.board, "_serial_router", None) is self:
            self.board._serial_router = None

    # ---------------------------------------------------------------- caches
    def _check_cache(self) -> None:
        key = (getattr(self.board, "wiring", 0), self.version)
        if key != self._key:
            self._key = key
            self._sinks.clear()
            self._txpins.clear()
            self._comp = None
            self._chip_gnd.clear()

    def _components(self) -> dict[int, int]:
        """id(net) -> conductive component id (nets joined by cable wires)."""
        if self._comp is not None:
            return self._comp
        parent: dict[int, int] = {id(n): id(n) for n in self.board.nets}

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        pn = self.board._port_net
        for p in self.board.parts.values():
            groups = getattr(p, "conductor_groups", None)
            if groups is None:
                continue
            for g in groups():
                ids = [id(pn[(p.id, port)]) for port in g if (p.id, port) in pn]
                for a in ids[1:]:
                    ra, rb = find(ids[0]), find(a)
                    if ra != rb:
                        parent[rb] = ra
        self._comp = {k: find(k) for k in parent}
        return self._comp

    def _chip_ground(self, cid: str) -> set:
        """Components holding the ground of chip `cid`: a `gnd` part, a net with drive L, one of its VSS pins."""
        got = self._chip_gnd.get(cid)
        if got is not None:
            return got
        comp = self._components()
        out = set()
        for n in self.board.nets:
            if n.drive == "L" or any(getattr(p, "type", "") == "gnd" for p, _port in n.ports) or \
                    any(c == cid and pin.fixed == "L" for c, _ch, pin in n.members):
                out.add(comp.get(id(n)))
        self._chip_gnd[cid] = out
        return out

    def _gnd_comp(self, p: Any) -> int | None | str:
        port = getattr(p, "gnd_port", None)
        if port is None:
            return "board"                  # abstract logic part: on the board's ground
        net = self.board._port_net.get((p.id, port))
        return None if net is None else self._components().get(id(net))

    def _ground_issue(self, a: Any, b: Any) -> tuple | None:
        """(code, message) if the segment driver `a` and receiver `b` (Part or ("chip", cid)) share no ground."""
        def name(x):
            return f"chip {x[1]}" if isinstance(x, tuple) else x.id
        if a is b or (isinstance(a, tuple) and isinstance(b, tuple)):
            return None
        for x in (a, b):
            if not isinstance(x, tuple) and self._gnd_comp(x) is None:
                return ("no_ground", f"{x.id}.{x.gnd_port} is not connected")
        if isinstance(a, tuple) or isinstance(b, tuple):
            chip, prt = (a, b) if isinstance(a, tuple) else (b, a)
            g = self._gnd_comp(prt)
            gs = self._chip_ground(chip[1])
            if g == "board" or not gs or g in gs:
                return None
            return ("no_ground", f"no common ground between {name(chip)} and {prt.id}")
        ga, gb = self._gnd_comp(a), self._gnd_comp(b)
        if ga == "board" or gb == "board" or ga == gb:
            return None
        return ("no_ground", f"no common ground between {a.id} and {b.id}")

    # ---------------------------------------------------------------- path walk
    @staticmethod
    def _paths_of(p: Any, port: str) -> list:
        fn = getattr(p, "serial_paths", None)
        if fn is not None:
            return fn(port)
        if p.type in ("buf", "not") and port == "in":
            return [("out", p.type == "not", "ttl", None)]
        return []

    def _rx_blocks(self, cid: str, pin: Any) -> list[tuple[Any, str]]:
        """[(UART block, RX signal)] that pin `pin` offers (static, from the chip description)."""
        got = self._rxb.get((cid, pin.id))
        if got is not None:
            return got
        chip = self.board.chips[cid]
        out = []
        for fn in (pin.functions or {}).values():
            for sig in str(fn).split("/"):
                r = self.board._uart_of(cid, chip, sig)
                if r is not None and r[1] == "rx":
                    blk = chip.peripherals.get(r[0][1])
                    if blk is not None and all(b is not blk for b, _s in out):
                        out.append((blk, sig))
        self._rxb[(cid, pin.id)] = out
        return out

    def sinks(self, start: tuple, net: Any, inv: int, dom: str, seg: Any, entry: tuple | None) -> list[_Sink]:
        """Receivers reachable from a source that drives `net` (cached)."""
        self._check_cache()
        got = self._sinks.get(start)
        if got is not None:
            return got
        out: list[_Sink] = []
        seen: set = set()
        # a chip TX pin reaching an RX pin on its own net is the board's automatic UART link, not ours
        stack = [_Walk(net, inv, dom, seg, entry, via=start[0] != "pin", path=(".".join(str(x) for x in start[1:]),))]
        pn = self.board._port_net
        while stack:
            w = stack.pop()
            key = (id(w.net), w.inv, w.dom, id(w.seg), w.issues)
            if key in seen:
                continue
            seen.add(key)
            if w.via:
                for cid, _chip, pin in w.net.members:
                    if start[0] == "pin" and (cid, pin.id) == (start[1], start[2]):
                        continue
                    if not self._rx_blocks(cid, pin):
                        continue
                    iss = list(w.issues)
                    if w.dom == "rs232":
                        iss.append(("over_voltage", f"RS-232 levels (+/-6 V) on {cid}.{pin.id}, a TTL input"))
                    if w.inv:
                        iss.append(("polarity", "inverted polarity: the line idles at space (break), no valid "
                                                "characters"))
                    g = self._ground_issue(w.seg, ("chip", cid))
                    if g:
                        iss.append(g)
                    out.append(_Sink("pin", f"{cid}.{pin.id}", tuple(iss), cid=cid, pin=pin,
                                     path=" → ".join(w.path + (f"{cid}.{pin.id}",))))
            for p, port in list(w.net.ports):
                if w.entry is not None and p is w.entry[0] and port == w.entry[1]:
                    continue
                sink = getattr(p, "serial_sink", None)
                info = sink(port) if sink is not None else None
                if info is not None:
                    iss = list(w.issues)
                    if w.dom == "rs232" and info["domain"] == "ttl":
                        iss.append(("over_voltage", f"RS-232 levels (+/-6 V) on {p.id}.{port}, a TTL input"))
                    if w.inv != info["inv"]:
                        iss.append(("polarity", "inverted polarity: the line idles at space (break), no valid "
                                                "characters"))
                    g = self._ground_issue(w.seg, p)
                    if g:
                        iss.append(g)
                    out.append(_Sink("part", f"{p.id}.{port}", tuple(iss), part=p, port=port,
                                     path=" → ".join(w.path + (f"{p.id}.{port}",))))
                paths = self._paths_of(p, port)
                if not paths:
                    continue
                stage_iss = list(w.issues)
                if any(od is not None for _o, _i, od, _b in paths):     # an active stage's input
                    in_dom = getattr(p, "serial_input_domain", lambda _p: "ttl")(port)
                    if w.dom == "rs232" and in_dom == "ttl":
                        stage_iss.append(("over_voltage", f"RS-232 levels (+/-6 V) on {p.id}.{port}, a TTL input"))
                    g = self._ground_issue(w.seg, p)
                    if g:
                        stage_iss.append(g)
                for out_port, invert, od, blocked in paths:
                    onet = pn.get((p.id, out_port))
                    if onet is None:
                        continue
                    iss = tuple(stage_iss) + ((("stage_off", blocked),) if blocked else ())
                    stack.append(_Walk(onet, w.inv ^ int(bool(invert)), od or w.dom, p if od else w.seg,
                                       (p, out_port), iss, True, w.path + (f"{p.id}.{port}",)))
        self._sinks[start] = out
        return out

    def _tx_pins(self, cid: str, name: str) -> list:
        self._check_cache()
        key = (cid, name)
        got = self._txpins.get(key)
        if got is None:
            chip = self.board.chips[cid]
            got = []
            for pin in chip.pins.pins if chip.pins is not None else ():
                r = self.board._uart_of(cid, chip, pin.owner)
                if r is not None and r[1] == "tx" and r[0] == (cid, name):
                    got.append(pin)
            self._txpins[key] = got
        return got

    # ---------------------------------------------------------------- traffic
    def _chip_tx(self, cid: str, name: str, blk: Any, x: Any) -> None:
        """A chip UART transmits `x`: a Frame (frame_listeners) or a byte (models without frames)."""
        chip = self.board.chips[cid]
        fr = None if isinstance(x, int) else x
        for pin in self._tx_pins(cid, name):
            net = self.board._net_of.get((cid, pin.id))
            if net is None or not net.ports:
                continue
            sinks = self.sinks(("pin", cid, pin.id), net, 0, "ttl", ("chip", cid), None)
            if fr is not None and fr.baud > 0:
                t = chip.now_ns() + fr.duration_ns()
            else:
                t = chip.now_ns() + (blk.char_ns() if hasattr(blk, "char_ns") else 0.0)
            self.send(f"{cid}.{name}", chip, sinks, (fr.data if fr is not None else x) & 0xFF, t, src_blk=blk,
                      frame=fr)

    def send(self, src: str, src_chip: Any, sinks: list[_Sink], b: int, t: float, *, src_blk: Any = None,
             src_line: Any = None, frame: Any = None) -> None:
        """Deliver byte `b` whose stop bit ends at virtual time `t` to every sink. `frame` (a uartwire.Frame) is
        the character as transmitted; without it the frame comes from `src_line` (a hostserial.LineSettings) or, if
        neither is given, the byte arrives in each receiver's own format (no framing check)."""
        if frame is None and src_line is not None:
            frame = src_line.frame(b)
        if not sinks:
            self._once(("nosink", src), "info", f"serial: {src} transmits but its line reaches no receiver "
                                                f"(bytes go nowhere)", t)
            return
        for s in sinks:
            if s.issues:
                code, msg = s.issues[0]
                self._once((src, s.label, code), "warn", f"serial: {src} → {s.label}: {msg}; bytes are lost "
                                                         f"(path {s.path})", t)
                self._drop(s, b, code, t, src_chip)
                continue
            why = self._framing(s, src_blk, src_line)
            if why is not None:
                if frame is None:
                    self._once((src, s.label, "framing"), "warn", f"serial: {src} → {s.label}: {why}; the "
                                                                  f"receiver sees framing errors, bytes are lost", t)
                    self._drop(s, b, "framing", t, src_chip)
                    continue
                self._once((src, s.label, "framing"), "warn", f"serial: {src} → {s.label}: {why}; the receiver "
                                                              f"decodes at its own settings (framing / parity "
                                                              f"errors, garbage)", t)
            if s.kind == "part":
                rf = getattr(s.part, "serial_receive_frame", None)
                if frame is not None and rf is not None:
                    self._at(src_chip, t, rf, frame)
                elif frame is not None:
                    self._at(src_chip, t, self._part_receive, s.part, frame)
                else:
                    self._at(src_chip, t, s.part.serial_receive, b)
            else:
                self._at(self.board.chips[s.cid], t, self._pin_receive, s, b, t, frame)

    def _framing(self, s: _Sink, src_blk: Any, src_line: Any) -> str | None:
        from ..hostserial import LineSettings, framing_mismatch
        try:
            if s.kind == "part":
                ln = s.part.line()
                if src_blk is None:
                    return None
                return framing_mismatch(float(src_blk.baud()), float(src_blk.char_bits()), ln)
            blks = self._rx_blocks(s.cid, s.pin)
            blk = next((bk for bk, sig in blks if self._owned(s.pin, sig)), blks[0][0] if blks else None)
            if blk is None:
                return None
            if src_line is not None:
                return framing_mismatch(float(blk.baud()), float(blk.char_bits()), src_line)
            if src_blk is not None:
                bs = float(src_blk.baud())
                ln = LineSettings(bs, framed=False) if bs > 0 else None
                why = framing_mismatch(float(blk.baud()), None, ln)
                if why is None and bs > 0 and float(blk.baud()) > 0 and src_blk.char_bits() != blk.char_bits():
                    why = f"frame {src_blk.char_bits()} vs {blk.char_bits()} bits/char"
                return why
        except Exception:
            return None
        return None

    @staticmethod
    def _owned(pin: Any, sig: str) -> bool:
        own = pin.owner
        return own == sig or sig in str(own).split("/")

    def _at(self, chip: Any, t: float, fn: Any, *args: Any) -> None:
        if chip.now_ns() >= t:
            fn(*args)
        else:
            chip.sched.at_ns(t, fn, *args)

    @staticmethod
    def _part_receive(part: Any, frame: Any) -> None:
        """A byte-level part sink (serial_receive only): the frame decoded at the part's `line()` (a framing error
        or a break reads as 0x00, as on a raw tty; chipem/hostserial.py)."""
        from ..hostserial import host_bytes_for, host_rx_format
        ln = part.line() if hasattr(part, "line") else None
        for b, _ch in host_bytes_for(frame, host_rx_format(ln, frame)):
            part.serial_receive(0 if b == "break" else b)

    def _pin_receive(self, s: _Sink, b: int, t: float, frame: Any = None) -> None:
        blks = self._rx_blocks(s.cid, s.pin)
        for blk, sig in blks:
            if self._owned(s.pin, sig):
                rf = getattr(blk, "receive_frame", None)
                if frame is not None and rf is not None:
                    rf(frame, t)
                elif frame is None or not frame.brk:
                    blk.receive(bytes([b]))
                return
        if blks:
            blks[0][0]._rx_drop(b, "pin_not_muxed")

    def _drop(self, s: _Sink, b: int, code: str, t: float, src_chip: Any) -> None:
        if s.kind == "part":
            self._at(src_chip, t, s.part.serial_drop, b, code)
            return
        blks = self._rx_blocks(s.cid, s.pin)
        if not blks:
            return
        blk = next((bk for bk, sig in blks if self._owned(s.pin, sig)), blks[0][0])
        drop = getattr(blk, "_rx_drop", None)
        if drop is not None:
            self._at(self.board.chips[s.cid], t, drop, b, code)

    def _once(self, key: tuple, kind: str, msg: str, t: float) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        (self.board.warn if kind == "warn" else self.board.info)(msg, t_ns=t)

    def describe(self) -> list[dict]:
        """Every resolved source -> receiver path with its verdict (UIs, tests)."""
        out = []
        for start, sinks in list(self._sinks.items()):
            for s in sinks:
                out.append({"from": ".".join(str(x) for x in start[1:]), "to": s.label, "ok": not s.issues,
                            "issues": [m for _c, m in s.issues], "path": s.path})
        return out


# ================================================================================================ parts
class _SerialPart(Part):
    """Common plumbing: register with the board's serial router."""

    def attach(self, board: Any) -> None:
        super().attach(board)
        serial_router(board)

    def _router_changed(self) -> None:
        if self.board is not None:
            serial_router(self.board).changed()


@part("usb_serial_adapter")
class UsbSerialAdapter(_SerialPart):
    """PC end of the line: a USB-to-serial adapter, DTE with a DE-9 male connector (1 DCD, 2 RXD, 3 TXD, 4 DTR,
    5 GND, 6 DSR, 7 RTS, 8 CTS, 9 RI). `levels: rs232` (default; mark = -6 V) or `ttl` (FTDI-cable style, 3.3 V,
    active-low RTS#/DTR#). `port`: host transport (pty | pty:/link | tcp:PORT | serial:/dev/ttyUSB1[:BAUD] |
    memory | none); without one, `PART=TEXT` stimulus / `set(send=...)` plays the host. `baud`/`format`: the line
    when the transport does not tell (PTY: the baud the PC program set)."""
    PARAMS = {"port": None, "baud": 115200, "format": "8N1", "levels": "rs232"}
    PORTS = {"dcd": "in", "rxd": "in", "txd": "out", "dtr": "out", "gnd": "out", "dsr": "in", "rts": "out",
             "cts": "in", "ri": "in"}
    ID_PREFIX = "PC"
    connector = ("db9", "male", 9)
    gnd_port = "gnd"

    def _setup(self) -> None:
        from ..hostserial import LineSettings, parse_format
        lv = str(self.params["levels"]).strip().lower()
        if lv not in ("rs232", "ttl"):
            raise ValueError(f"part {self.id}: levels must be rs232 or ttl")
        self.params["levels"] = lv
        self.domain = lv
        try:
            baud = float(self.params["baud"])
        except (TypeError, ValueError):
            raise ValueError(f"part {self.id}: bad baud {self.params['baud']!r}") from None
        bits, par, stop = parse_format(self.params["format"])
        self.params["format"] = f"{bits}{par}{stop:g}"
        self.params["baud"] = int(baud) if baud.is_integer() else baud
        self.default_line = LineSettings(baud, bits, par, stop)
        self.host = None
        self.manual: dict[str, bool] = {}
        self.to_host = 0
        self.from_host = 0
        self.dropped = 0
        self.errors = 0
        self.rx_tail = bytearray()
        self._q: deque[int] = deque()
        self._ev = None
        self._modem_seen: tuple | None = None

    # ---- lifecycle ---------------------------------------------------------------------------------------------
    def attach(self, board: Any) -> None:
        from ..hostserial import open_port
        super().attach(board)
        if self.host is None:
            self.host = open_port(self.params["port"], line=self.default_line)

    def detach(self) -> None:
        self.close_host()
        super().detach()

    def close_host(self) -> None:
        if self.host is not None:
            self.host.close()

    @property
    def host_port(self):
        return self.host

    def _reset_state(self) -> None:
        self._out["txd"] = "L" if self.domain == "rs232" else "H"      # idle = mark
        self._out["gnd"] = "L"                                          # the PC's ground (through USB)
        self._ev = None
        self._modem_seen = None
        self._out["dtr"] = self._modem_level("dtr")
        self._out["rts"] = self._modem_level("rts")
        if self._q and self.board is not None:
            self._ev = self._in(self.line().char_ns(), self._tx_tick)

    # ---- line / modem ---------------------------------------------------------------------------------------------
    def line(self):
        from ..hostserial import LineSettings
        ln = self.host.line() if self.host is not None else None
        if ln is None:
            return self.default_line
        if not ln.framed:
            d = self.default_line
            return LineSettings(ln.baud, d.bits, d.parity, d.stop, framed=False)
        return ln

    def _asserted(self, name: str) -> bool:
        if name in self.manual:
            return self.manual[name]
        if self.host is None or not self.host.connected:
            return False                    # no program has the port open: the driver drops DTR/RTS
        v = self.host.modem_from_host().get(name)
        return bool(v) if v is not None else True

    def _modem_level(self, name: str) -> str:
        on = self._asserted(name)
        if self.domain == "rs232":
            return "H" if on else "L"       # asserted = space = positive voltage
        return "L" if on else "H"           # TTL RTS#/DTR#: active low

    def _in_asserted(self, port: str) -> bool:
        lvl = self.inputs.get(port)
        return lvl == ("H" if self.domain == "rs232" else "L")

    def pull(self, port: str) -> str | None:
        if self.ports[port].is_input:
            # RS-232 receiver input resistance to ground (an open input reads as mark); FTDI TTL inputs pull up
            return "down" if self.domain == "rs232" else "up"
        return None

    def on_input(self, port: str, level: str, t_ns: float) -> None:
        if port in ("cts", "dsr", "dcd", "ri") and self.host is not None:
            self.host.set_modem_to_host(**{port: self._in_asserted(port)})

    # ---- emulation-thread pump (Session between run slices) -----------------------------------------------------
    def pump(self) -> None:
        h = self.host
        if h is None:
            return
        for item in h.read_stream():
            if isinstance(item, tuple):
                self._send_break(item[1])
            else:
                self._send(item)
        seen = (h.connected, tuple(sorted(h.modem_from_host().items())))
        if seen != self._modem_seen:
            self._modem_seen = seen
            self._drive("dtr", self._modem_level("dtr"))
            self._drive("rts", self._modem_level("rts"))
            for port in ("cts", "dsr", "dcd", "ri"):
                h.set_modem_to_host(**{port: self._in_asserted(port)})

    def _send(self, data: bytes) -> None:
        self._q.extend(data)
        self._kick()

    def _send_break(self, seconds: float = 0.0) -> None:
        """The PC sends a break (seconds; 0: two character times, at least 13 bits)."""
        ln = self.line()
        bit = 1e9 / ln.baud if ln.baud > 0 else 0.0
        bits = max(int(seconds * 1e9 / bit), 1) if seconds and bit else max(13, int(round(2 * ln.char_bits())))
        self._q.append(("brk", bits))
        self._kick()

    def _kick(self) -> None:
        if self._ev is None and self.board is not None and self._q:
            self._ev = self._in(self._item_ns(self._q[0]), self._tx_tick)

    def _item_ns(self, item: Any) -> float:
        ln = self.line()
        if isinstance(item, tuple):
            return (item[1] + ln.stop) * 1e9 / ln.baud if ln.baud > 0 else 0.0
        return ln.char_ns()

    def _tx_tick(self) -> None:
        from ..periph.uartwire import Frame
        self._ev = None
        if not self._q:
            return
        item = self._q.popleft()
        ln = self.line()
        if isinstance(item, tuple):
            b, fr = 0, Frame.brk_frame(item[1], ln.stop, ln.baud)
        else:
            b, fr = item, ln.frame(item)
            self.from_host += 1
        board = self.board
        net = board._port_net.get((self.id, "txd")) if board is not None else None
        if net is not None:
            r = serial_router(board)
            sinks = r.sinks(("part", self.id, "txd"), net, 1 if self.domain == "rs232" else 0, self.domain, self,
                            (self, "txd"))
            r.send(f"{self.id}.txd", board.clock_chip, sinks, b, self.now(), src_line=ln, frame=fr)
        self._kick()

    # ---- receive side ---------------------------------------------------------------------------------------------
    def serial_sink(self, port: str) -> dict | None:
        if port != "rxd":
            return None
        return {"inv": 1 if self.domain == "rs232" else 0, "domain": self.domain}

    def serial_receive_frame(self, fr: Any) -> None:
        """A chip frame reaches RXD: the PC's UART decodes it with the host line settings (chipem/hostserial.py:
        a framing error or a break reads as 0x00, a break also goes to a pyserial port as a break)."""
        from ..hostserial import host_bytes_for, host_rx_format
        ln = self.line()
        for b, ch in host_bytes_for(fr, host_rx_format(ln, fr)):
            if ch is not None and ch.flags() and self.board is not None:
                self.errors += 1
                self.board.log.event("uart", self.now(), chip="board", uart=f"{self.id}.rxd", dir="rx",
                                     byte=0 if b == "break" else b, **ch.flags())
            if b == "break":
                if self.host is not None:
                    self.host.send_break((ch.brk if ch is not None else fr.brk) / ln.baud if ln.baud > 0 else 0.0)
                self.rx_tail.append(0)
                continue
            self.serial_receive(b)

    def serial_receive(self, b: int) -> None:
        self.to_host += 1
        self.rx_tail.append(b)
        if len(self.rx_tail) > 256:
            del self.rx_tail[:-256]
        if self.host is not None:
            self.host.write(bytes([b]))

    def serial_drop(self, b: int, reason: str) -> None:
        self.dropped += 1
        if self.board is not None:
            self.board.log.event("uart", self.now(), chip="board", uart=f"{self.id}.rxd", dir="drop", byte=b,
                                 dropped=1, reason=reason)

    # ---- user side -------------------------------------------------------------------------------------------------
    def state(self) -> dict:
        h = self.host
        d = {"transport": h.kind if h is not None else None, "path": h.describe() if h is not None else None,
             "connected": bool(h.connected) if h is not None else False, "line": str(self.line()),
             "to_host": self.to_host, "from_host": self.from_host, "dropped": self.dropped, "errors": self.errors,
             "queued": len(self._q), "levels": self.domain, "txd": _volts(self.domain, self._out.get("txd")),
             "rx": self.rx_tail[-48:].decode("latin-1")}
        for p in ("dtr", "rts"):
            d[p] = self._asserted(p)
        for p in ("cts", "dsr", "dcd", "ri"):
            d[p] = self._in_asserted(p)
        return d

    def info(self) -> dict:
        h = self.host
        base = h.info() if h is not None else {"transport": None, "path": None, "connected": False}
        return {"name": self.id, "kind": "part", **base, "line": str(self.line()), "to_host": self.to_host,
                "from_host": self.from_host, "dropped": self.dropped, "errors": self.errors, "queued": len(self._q)}

    def set(self, **values: Any) -> dict:
        for k, v in values.items():
            if k == "send":
                self._send(_escapes(v))
            elif k == "break":
                self._send_break(float(v) if str(v).strip() not in ("", "1", "true", "True") else 0.0)
            elif k in ("dtr", "rts"):
                self.manual[k] = _bool(v, f"{self.id}.{k}")
                self._drive(k, self._modem_level(k))
            elif k == "clear":
                self.rx_tail.clear()
            else:
                raise ValueError(f"part {self.id} (usb_serial_adapter): cannot set {k!r} (use send, break, dtr, rts, "
                                 "clear)")
        return self.state()

    def stimulus(self, value: str) -> dict:
        return self.set(send=value)

    def connector_port(self, pin: int) -> str | None:
        return DB9_DTE.get(int(pin))


@part("rs232_transceiver")
class Rs232Transceiver(_SerialPart):
    """RS-232 line driver/receiver (MAX3232-like): `drivers` T channels (tNin TTL -> tNout RS-232, inverting) and
    `receivers` R channels (rNin RS-232 -> rNout TTL, inverting; rNin has the receiver's input resistance to ground,
    so an open input reads mark and rNout idles high). Needs its `gnd` port on a grounded (L) net. `shdn: true` adds
    an active-low `shdn_n` (L: drivers off, outputs open); `en: true` adds an active-low `en_n` (H: receiver outputs
    open), like the MAX3222. A floating control input counts as active (shut down / disabled)."""
    PARAMS = {"drivers": 2, "receivers": 2, "shdn": False, "en": False}
    ID_PREFIX = "U"
    gnd_port = "gnd"

    @classmethod
    def port_descs(cls, params: dict[str, Any]) -> dict[str, PortDesc]:
        nd, nr = int(params.get("drivers", 2)), int(params.get("receivers", 2))
        if not (0 <= nd <= 8 and 0 <= nr <= 8 and nd + nr > 0):
            raise ValueError("rs232_transceiver: drivers/receivers must be within 0..8")
        d: dict[str, PortDesc] = {}
        for i in range(1, nd + 1):
            d[f"t{i}in"], d[f"t{i}out"] = PortDesc("in"), PortDesc("out")
        for i in range(1, nr + 1):
            d[f"r{i}in"], d[f"r{i}out"] = PortDesc("in"), PortDesc("out")
        d["gnd"] = PortDesc("in")
        if _bool(params.get("shdn", False), "shdn"):
            d["shdn_n"] = PortDesc("in")
        if _bool(params.get("en", False), "en"):
            d["en_n"] = PortDesc("in")
        return d

    def _setup(self) -> None:
        for k in ("drivers", "receivers"):
            try:
                self.params[k] = int(self.params[k])
            except (TypeError, ValueError):
                raise ValueError(f"part {self.id}: {k} must be an integer") from None
        self.params["shdn"] = _bool(self.params["shdn"], f"{self.id}.shdn")
        self.params["en"] = _bool(self.params["en"], f"{self.id}.en")
        self._mode = None

    def _powered(self) -> bool:
        return self.inputs.get("gnd") == "L"

    def _drivers_on(self) -> bool:
        return self._powered() and (not self.params["shdn"] or self.inputs.get("shdn_n") == "H")

    def _receivers_on(self) -> bool:
        return self._powered() and (not self.params["en"] or self.inputs.get("en_n") == "L")

    def _eval(self) -> None:
        don, ron = self._drivers_on(), self._receivers_on()
        for i in range(1, self.params["drivers"] + 1):
            self._drive(f"t{i}out", _inv(self.inputs.get(f"t{i}in")) if don else None, quiet=True)
        for i in range(1, self.params["receivers"] + 1):
            self._drive(f"r{i}out", _inv(self.inputs.get(f"r{i}in") or "L") if ron else None, quiet=True)
        mode = (don, ron)
        if mode != self._mode:
            self._mode = mode
            self._router_changed()

    def _reset_state(self) -> None:
        super()._reset_state()
        self._mode = None

    def pull(self, port: str) -> str | None:
        return "down" if port.startswith("r") and port.endswith("in") else None

    def on_input(self, port: str, level: str, t_ns: float) -> None:
        self._eval()

    def serial_input_domain(self, port: str) -> str:
        return "rs232" if port.startswith("r") else "ttl"

    def serial_paths(self, port: str) -> list:
        if port.startswith("t") and port.endswith("in"):
            why = None if self._drivers_on() else (f"{self.id} drivers off" + ("" if self._powered()
                                                                                 else " (gnd not on a grounded net)"))
            return [(port[:-2] + "out", True, "rs232", why)]
        if port.startswith("r") and port.endswith("in"):
            why = None if self._receivers_on() else (f"{self.id} receivers off" + ("" if self._powered()
                                                                                     else " (gnd not on a grounded "
                                                                                          "net)"))
            return [(port[:-2] + "out", True, "ttl", why)]
        return []

    def state(self) -> dict:
        d: dict[str, Any] = {"powered": self._powered(), "drivers_on": self._drivers_on(),
                             "receivers_on": self._receivers_on()}
        for i in range(1, self.params["drivers"] + 1):
            d[f"t{i}out"] = _volts("rs232", self._out.get(f"t{i}out"))
        for i in range(1, self.params["receivers"] + 1):
            d[f"r{i}out"] = _volts("ttl", self._out.get(f"r{i}out"))
        return d


# ------------------------------------------------------------------------------------------------ connectors
_KIND_PINS = {"db9": 9, "db25": 25, "rj11": 6, "rj12": 6, "rj45": 8, "rj50": 10}


def _parse_end(v: Any, what: str) -> tuple[str, str, int]:
    """'db9m' / 'db9f' / 'db9-male' / 'rj50' / 'rj45' / 'pins:12' -> (kind, gender, pins)."""
    s = str(v).strip().lower().replace("_", "").replace("-", "")
    if s.startswith(("pins:", "conn:")):
        n = int(s.split(":", 1)[1])
        return "conn", "any", n
    for k, n in _KIND_PINS.items():
        if s.startswith(k):
            rest = s[len(k):]
            if k.startswith("db"):
                g = {"m": "male", "male": "male", "f": "female", "female": "female", "": "female"}.get(rest)
                if g is None:
                    raise ValueError(f"{what}: bad connector {v!r} (e.g. db9m, db9f)")
                return k, g, n
            if rest not in ("", "plug", "jack"):
                raise ValueError(f"{what}: bad connector {v!r}")
            return k, rest or "plug", n
    raise ValueError(f"{what}: unknown connector {v!r} (db9m, db9f, db25m, db25f, rj11, rj45, rj50, pins:N)")


class Connector(_SerialPart):
    """Board connector: ports "1".."N" (the pins), passive. Cables plug into it (`cable` `plug_a`/`plug_b`)."""
    PARAMS = {"kind": "db9", "gender": "female", "pins": None, "names": None}
    ID_PREFIX = "J"
    KIND: str | None = None

    @classmethod
    def _npins(cls, params: dict) -> int:
        kind = cls.KIND or str(params.get("kind", "db9")).lower()
        n = params.get("pins")
        if n in (None, ""):
            if kind not in _KIND_PINS:
                raise ValueError(f"connector: unknown kind {kind!r}; give `pins`")
            return _KIND_PINS[kind]
        return int(n)

    @classmethod
    def port_descs(cls, params: dict[str, Any]) -> dict[str, PortDesc]:
        return {str(i): PortDesc("in") for i in range(1, cls._npins(params) + 1)}

    def _setup(self) -> None:
        kind = self.KIND or str(self.params["kind"]).strip().lower()
        if "kind" in self.params:
            self.params["kind"] = kind
        g = str(self.params.get("gender", "female")).strip().lower()
        if kind.startswith("rj"):
            g = "jack"
        elif g in ("m", "male"):
            g = "male"
        elif g in ("f", "female"):
            g = "female"
        else:
            raise ValueError(f"part {self.id}: gender must be male or female")
        if "gender" in self.params:
            self.params["gender"] = g
        self.npins = self._npins(self.params)
        names = self.params.get("names") or {}
        if not isinstance(names, dict):
            raise ValueError(f"part {self.id}: names must map pin -> label")
        self.names = {str(k): str(v) for k, v in names.items()}
        self.connector = (kind, g, self.npins)

    def connector_port(self, pin: int) -> str | None:
        return str(int(pin)) if 1 <= int(pin) <= self.npins else None

    def state(self) -> dict:
        pins = {}
        for i in range(1, self.npins + 1):
            lvl = self.inputs.get(str(i))
            if self.board is not None and self.board.part_net(self.id, str(i)) is not None:
                key = f"{i}" + (f" {self.names[str(i)]}" if str(i) in self.names else "")
                pins[key] = lvl if lvl in ("H", "L") else "X"
        return {"pins": pins}


@part("connector")
class GenericConnector(Connector):
    """Generic connector: `kind` (db9, db25, rj11, rj45, rj50, or anything with `pins: N`), `gender` (D-sub)."""


@part("db9")
class Db9(Connector):
    """DE-9 (DB9) board connector, ports "1".."9"; `gender` female (default) or male."""
    PARAMS = {"gender": "female", "names": None}
    KIND = "db9"


@part("rj50")
class Rj50(Connector):
    """RJ50 (10P10C) modular jack, ports "1".."10"."""
    PARAMS = {"names": None}
    KIND = "rj50"


# ------------------------------------------------------------------------------------------------ cable
_PIN_SENTINEL = "?"


def _parse_map(v: Any, what: str) -> list[tuple[int, int]]:
    out = []
    if isinstance(v, dict):
        items = list(v.items())
    elif isinstance(v, (list, tuple)):
        items = [tuple(x) for x in v]
    elif isinstance(v, str):
        items = []
        for tok in v.replace(";", ",").split(","):
            if tok.strip():
                a, _, b = tok.partition("-") if "-" in tok else tok.partition(":")
                items.append((a, b))
    else:
        raise ValueError(f"{what}: map must be {{A_PIN: B_PIN | [B_PINS]}}")
    for a, b in items:
        bs = b if isinstance(b, (list, tuple)) else [b]
        for bb in bs:
            if bb in (None, "", "nc", "NC"):
                continue
            out.append((int(a), int(bb)))
    return out


@part("cable")
class Cable(_SerialPart):
    """Cable with two ends `a` and `b` (connector types: db9m, db9f, db25m/f, rj11, rj45, rj50, pins:N) and an
    explicit wire map `map: {A_PIN: B_PIN or [B_PINS]}` (or `preset`: straight, null_modem, null_modem_3wire for DE-9
    to DE-9; a DE-9 cable without either is straight through). Ports a1..aN and b1..bM; `plug_a` / `plug_b` name the connector part (or adapter) each end is plugged
    into: the matching connector pins are wired automatically and the genders must mate. A wire is a passive
    conductor: every net it joins sees the same drivers (a contention across the cable is a conflict, one `warn`)."""
    PARAMS = {"a": "db9f", "b": "db9f", "map": None, "preset": None, "plug_a": None, "plug_b": None}
    ID_PREFIX = "W"

    @classmethod
    def port_descs(cls, params: dict[str, Any]) -> dict[str, PortDesc]:
        _ka, _ga, na = _parse_end(params.get("a", "db9f"), "cable a")
        _kb, _gb, nb = _parse_end(params.get("b", "db9f"), "cable b")
        d = {f"a{i}": PortDesc("inout") for i in range(1, na + 1)}
        d.update({f"b{i}": PortDesc("inout") for i in range(1, nb + 1)})
        return d

    def _setup(self) -> None:
        self.end = {"a": _parse_end(self.params["a"], f"part {self.id}.a"),
                    "b": _parse_end(self.params["b"], f"part {self.id}.b")}
        wires: list[tuple[int, int]] = []
        preset = self.params.get("preset")
        if preset not in (None, ""):
            key = str(preset).strip().lower()
            if key not in PRESETS:
                raise ValueError(f"part {self.id}: unknown preset {preset!r} (known: {', '.join(PRESETS)}; "
                                 f"DB9-RJ50 pinouts are vendor specific: give `map`)")
            if self.end["a"][0] != "db9" or self.end["b"][0] != "db9":
                raise ValueError(f"part {self.id}: preset {key} is for DB9 to DB9 cables")
            wires += PRESETS[key]
        if self.params.get("map") not in (None, "", {}):
            wires += _parse_map(self.params["map"], f"part {self.id}.map")
        if not wires and self.end["a"][0] == self.end["b"][0] == "db9":
            wires = list(PRESETS["straight"])        # an unconfigured DE-9 cable: straight through
        if not wires:
            raise ValueError(f"part {self.id}: a cable needs `map` (A_PIN: B_PIN) or `preset`")
        na, nb = self.end["a"][2], self.end["b"][2]
        for a, b in wires:
            if not (1 <= a <= na and 1 <= b <= nb):
                raise ValueError(f"part {self.id}: wire {a}-{b} outside the pins of the ends ({na} / {nb})")
        self.wires = wires
        # electrical groups (several wires on one pin join them)
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for a, b in wires:
            ra, rb = find(f"a{a}"), find(f"b{b}")
            if ra != rb:
                parent[rb] = ra
        groups: dict[str, list[str]] = {}
        for p in list(parent):
            groups.setdefault(find(p), []).append(p)
        self.groups = [sorted(g) for g in groups.values()]
        self.group_of = {p: g for g in self.groups for p in g}
        self.plugged: dict[str, str | None] = {"a": None, "b": None}
        self.plug_error: dict[str, str | None] = {"a": None, "b": None}
        self._plug_ports: set[str] = set()
        self._pulls: dict[str, str | None] = {}

    # ---- conductor interface --------------------------------------------------------------------------------------
    def conductor_groups(self) -> list[list[str]]:
        return self.groups

    def serial_paths(self, port: str) -> list:
        g = self.group_of.get(port)
        if not g:
            return []
        return [(p, False, None, None) for p in g if p != port]

    def _net_set(self, ports: list[str]) -> list:
        """Every net electrically joined to `ports` (through this and other conductors)."""
        b = self.board
        pn = b._port_net
        nets, todo, seen = [], [(self, p) for p in ports], set()
        while todo:
            prt, port = todo.pop()
            if (id(prt), port) in seen:
                continue
            seen.add((id(prt), port))
            net = pn.get((prt.id, port))
            if net is None or any(n is net for n in nets):
                continue
            nets.append(net)
            for q, qport in net.ports:
                groups = getattr(q, "group_of", None)
                if groups is not None and qport in groups:
                    for p2 in groups[qport]:
                        todo.append((q, p2))
        return nets

    @staticmethod
    def _real_drivers(n: Any) -> list[tuple[str, str]]:
        """Strong drivers of net `n` except conductors (cable wires): pins, part outputs, net drive, stimulus."""
        drv = []
        for i, (_cid, _chip, p) in enumerate(n.members):
            if p.fixed is not None:
                drv.append((n.label(i), p.fixed))
            elif p.od_low:
                drv.append((n.label(i), "L"))
            elif p.direction == "out" and p.drive is not None:
                drv.append((n.label(i), p.drive))
        for q, qport in n.ports:
            if hasattr(q, "group_of") or not q.ports[qport].is_output:
                continue
            lvl = q.drive(qport)
            if lvl is not None:
                drv.append((f"{q.id}.{qport}", lvl))
        if n.drive is not None:
            drv.append((f"net {n.name}", n.drive))
        if n.stimulus is not None:
            drv.append(("stimulus", n.stimulus))
        return drv

    @staticmethod
    def _combine(nets: list, drv: list) -> tuple[str | None, str | None]:
        """(level, pull) the drivers `drv` of `nets` put on a wire."""
        vals = {lvl for _w, lvl in drv}
        if any(n.open_drain for n in nets) and vals:
            lvl = "L" if "L" in vals else ("X" if "X" in vals else None)
            return lvl, ("up" if lvl is None else None)
        if vals:
            return (next(iter(vals)) if len(vals) == 1 else "X"), None
        pulls = {n.pull for n in nets if n.pull}
        if not pulls:
            for n in nets:
                for q, qport in n.ports:
                    if not hasattr(q, "group_of"):
                        pl = q.pull(qport)
                        if pl:
                            pulls.add(pl)
        return None, (pulls.pop() if len(pulls) == 1 else None)

    def _update_group(self, g: list[str]) -> None:
        """Drive each net this wire touches with what the *other* nets of the conductor carry (their real drivers),
        so a net never sees its own drivers echoed back: contention across the wire shows as a conflict on the nets,
        and a driver that goes away is gone everywhere at once."""
        if self.board is None:
            return
        pn = self.board._port_net
        live = [p for p in g if pn.get((self.id, p)) is not None]
        nets = self._net_set(live)
        drivers = {id(n): self._real_drivers(n) for n in nets}
        for p in live:
            own = pn[(self.id, p)]
            others = [n for n in nets if n is not own]
            lvl, pull = self._combine(others, [d for n in others for d in drivers[id(n)]])
            self._pulls[p] = pull
            self._drive(p, lvl, quiet=True)

    def on_input(self, port: str, level: str, t_ns: float) -> None:
        # called on every re-resolution of a net this cable touches (inputs reset to a sentinel): a driver that
        # leaves a net without changing its level must still be noticed on the far side
        g = self.group_of.get(port)
        if g:
            self._update_group(g)
        for p in self.ports:
            self.inputs[p] = _PIN_SENTINEL

    def pull(self, port: str) -> str | None:
        return self._pulls.get(port)

    def drive(self, port: str) -> str | None:
        return self._out.get(port)

    # ---- plugging ---------------------------------------------------------------------------------------------------
    def part_added(self, other: Any) -> None:
        for end in ("a", "b"):
            want = self.params.get(f"plug_{end}")
            if want in (None, "") or self.plugged[end] is not None:
                continue
            if other is self or str(other.id).upper() == str(want).upper():
                self._plug(end, raise_errors=other is self)

    def _plug(self, end: str, *, raise_errors: bool) -> None:
        b = self.board
        want = self.params.get(f"plug_{end}")
        tgt = b.find_part(str(want)) if b is not None else None
        if tgt is None:
            return
        conn = getattr(tgt, "connector", None)
        kind, gender, _n = self.end[end]
        err = None
        if conn is None or not hasattr(tgt, "connector_port"):
            err = f"{tgt.id} ({tgt.type}) has no connector"
        else:
            tk, tg, _tn = conn
            if kind != "conn" and tk != "conn" and tk != kind:
                err = f"a {kind} plug does not fit {tgt.id}'s {tk} connector"
            elif kind.startswith("db") and tg == gender:
                err = f"cannot mate a {kind} {gender} end with {tgt.id}'s {kind} {tg} connector"
        if err:
            self.plug_error[end] = err
            if raise_errors:
                raise ValueError(f"part {self.id}: plug_{end}: {err}")
            b.warn(f"cable {self.id}: plug_{end}: {err}", part=self.id)
            return
        pins = sorted({a for a, _b in self.wires} if end == "a" else {bb for _a, bb in self.wires})
        for i in pins:
            tport = tgt.connector_port(i)
            if tport is None:
                continue
            net = b.part_net(tgt.id, tport)
            if net is None:
                net = b.connect_port(tgt.id, tport, f"{tgt.id}_{tport}")
            b.connect_port(self.id, f"{end}{i}", net.name)
            self._plug_ports.add(f"{end}{i}")
        self.plugged[end] = tgt.id
        self.plug_error[end] = None
        self._router_changed()

    def _unplug(self, end: str) -> None:
        for p in sorted(self._plug_ports):
            if p.startswith(end):
                self.board.disconnect_port(self.id, p)
                self._plug_ports.discard(p)
        self.plugged[end] = None
        self._router_changed()

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["ports"] = {k: v for k, v in d["ports"].items() if k not in self._plug_ports}
        return d

    def state(self) -> dict:
        return {"a": self.plugged["a"] or self.plug_error["a"] or "unplugged",
                "b": self.plugged["b"] or self.plug_error["b"] or "unplugged", "wires": len(self.wires)}

    def set(self, **values: Any) -> dict:
        for k, v in values.items():
            if k not in ("plug_a", "plug_b"):
                raise ValueError(f"part {self.id} (cable): cannot set {k!r} (use plug_a, plug_b)")
            end = k[-1]
            if self.plugged[end] is not None:
                self._unplug(end)
            target = None if v in (None, "", "none", "None", "-") or v is False else str(v)
            self.params[k] = target
            if target is not None and self.board is not None:
                self._plug(end, raise_errors=True)
        return self.state()
