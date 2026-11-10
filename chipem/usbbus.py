"""Generic USB 2.0 full-/low-speed bus (one host port, one device), transaction level, with real wire timing.

Vendor-free (docs/CONTRACTS.md "USB bus"): device controller models (stm32.otg_fs / stm32.otg_hs, kinetis.usbfs)
implement the device protocol below; `UsbBus` owns the cable (VBUS, D+/D- pull-up, reset/suspend/resume signalling,
1 ms frames) and `UsbHost` is a scriptable host controller + USB stack on the other end (enumeration, standard
requests, CDC-ACM / HID / MSC class drivers).  A board plugs a chip's port into a host with
`links: [{type: usb, device: A.OTG_FS}]` (chipem/board.py `UsbLink`); a test does `bus = UsbBus(); bus.attach(blk);
host = bus.add_host()`.

Timing (USB 2.0 spec ch. 7/8, full speed 12 Mbit/s, low speed 1.5 Mbit/s).
* Every packet takes its real length on the wire: SYNC (8 bits) + PID (8) + payload + stuff bits + EOP (2 bit times
  SE0 + 1 J).  Payload = token address/endpoint + CRC5 (16 bits), SOF frame number + CRC5 (16), DATAx bytes + CRC16;
  handshakes are SYNC + PID + EOP.  CRC5/CRC16 are computed (`crc5`, `crc16`) and the bit stuffing (a 0 after six
  consecutive 1s of the NRZI input) is counted on the real bit stream, so the duration depends on the data.
* Inter-packet delay IPG_BITS (4 bit times) between the packets of a transaction (the spec allows 2 .. 6.5 at the
  transmitter); a device that does not answer is detected RESP_TIMEOUT_BITS (18) bit times after the end of the
  host's packet (16 .. 18 in the spec); the host leaves HOST_GAP_BITS between transactions.
* 1 ms frames: SOF (full speed) or a keep-alive EOP (low speed) at every frame start; the host starts a transaction
  only if it fits before the end of the frame (EOF1 margin, sized with the endpoint's max packet).
* Bus states: detached (no pull-up or no VBUS) -> attached (D+ pull-up: full speed, D- pull-up: low speed) ->
  debounce TATTDB (100 ms) -> reset (SE0 TDRST = 10 ms; a device sees it after 2.5 us) -> reset recovery TRSTRCY
  (10 ms) -> enumeration.  No traffic (no SOF) for 3 ms = suspend; host resume = K for TDRSMDN (20 ms) then
  SOFs again, TRSMRCY (10 ms) before traffic; device remote wakeup (device drives K) -> the host takes over the
  resume signalling 1 ms later (only when the device enabled remote wakeup through SET_FEATURE, as on a real host).
  All these times are host parameters (`UsbHost(..., debounce_ns=..., reset_ns=...)`) so tests can shorten them.
* Handshakes ACK / NAK / STALL / no response (timeout: the host retries 3 times, then the transfer fails).
  Data toggles: the host keeps DATA0/DATA1 per endpoint; an IN packet with the wrong toggle is acknowledged and
  discarded (a retransmission after a lost ACK), as the spec requires.  The device checks OUT toggles itself.
* Host scheduling: at each SOF the periodic list (interrupt endpoints whose bInterval is due) first, then control
  and bulk transfers round robin.  A control transaction that is NAKed is retried after `ctrl_nak_retry_ns` (20 us);
  a bulk/interrupt endpoint that NAKs is not polled again in the same frame (bulk: next frame, interrupt: next
  interval).  On real host controllers bulk NAK retries happen as often as bus time allows (OHCI/UHCI) or are
  throttled (EHCI NAK counter); polling once per frame keeps an idle CDC port at one transaction per ms.
* High speed and isochronous transfers are not modelled.

Device protocol (duck-typed; every callback gets the bus first; times are virtual ns on the device's chip):
    chip, usb_name, log                     the device's Chip, a short name ("OTG_FS"), its EventLog
    usb_pullup(bus) -> "fs" | "ls" | None   D+ (full speed) / D- (low speed) pull-up switched on (and powered)
    usb_vbus(bus, on, t)                    VBUS turned on / off by the host (session valid / end)
    usb_reset(bus, active, t)               USB reset (SE0) detected (active=True, 2.5 us after it started) / ended
    usb_suspend(bus, t)                     3 ms without bus activity
    usb_resume(bus, t)                      resume signalling (K) from the host started
    usb_sof(bus, frame, t)                  a SOF packet (11-bit frame number) ended at t (full speed only)
    usb_setup(bus, x) -> "ACK" | None       SETUP + DATA0 (x.data, 8 bytes) received at x.t (end of the data packet)
    usb_out(bus, x) -> "ACK" | "NAK" | "STALL" | None     OUT + DATAx received (x.pid "DATA0"/"DATA1", x.data)
    usb_in(bus, x) -> ("DATA0"|"DATA1", bytes) | "NAK" | "STALL" | None    IN token received at x.t (end of token)
    usb_in_done(bus, x, acked, t)           the host's handshake for the data the device sent (always ACK here)
x is a `Transaction` (kind, addr, ep, pid, data, t).  The device answers only tokens for its own address (None =
no response).  A device calls `bus.device_changed()` when its pull-up changes (soft connect/disconnect, reset) and
`bus.remote_wakeup()` when it starts driving K in suspend.

Events (`usb` kind, on the device's EventLog; dense traffic is summarised, never one event per packet):
    {link, dev, event: "attach"|"detach"|"reset"|"suspend"|"resume"|"wakeup"|"vbus"|"enumerated"|"error", msg}
    {link, dev, xfer: "control", req, setup (hex), dir, len, data (hex, <= 64 bytes), status, naks, msg}
    {link, dev, xfer: "bulk"|"interrupt", ep: 0x81, dir, len, data, text, status, msg}   one per completed transfer
`bus.listeners` get (bus, kind, info) for each of them.
"""
from __future__ import annotations

import math
import struct
from collections import deque
from typing import Any, Callable, Generator

__all__ = ["UsbBus", "UsbHost", "Transaction", "crc5", "crc16", "packet_bits", "token_bits", "data_bits",
           "handshake_bits", "FS_BIT_NS", "LS_BIT_NS", "FRAME_NS", "describe_setup", "parse_descriptors",
           "CdcAcm", "HidDevice", "MscDevice", "UsbDeviceInfo", "build_setup"]

FS_BIT_NS = 1e9 / 12e6
LS_BIT_NS = 1e9 / 1.5e6
FRAME_NS = 1_000_000.0
IPG_BITS = 4                    # inter-packet delay within a transaction
RESP_TIMEOUT_BITS = 18          # bus turnaround timeout
HOST_GAP_BITS = 2               # host idle between transactions
EOF_BITS = 32                   # EOF1 margin before the next SOF
RESET_DETECT_NS = 2_500.0       # SE0 a device needs to see before it recognises a reset (TDETRST)
SUSPEND_NS = 3_000_000.0        # idle time before a device suspends
MAX_ERRORS = 3                  # transaction errors (timeouts) before a transfer fails
T_EPS = 1e-3                    # ns: Scheduler.at_ns may fire a few 1e-8 ns early (cycle rounding); times within
                                # T_EPS count as reached, else an event re-schedules itself at the same cycle forever

PID = {"OUT": 0x1, "IN": 0x9, "SOF": 0x5, "SETUP": 0xD, "DATA0": 0x3, "DATA1": 0xB, "ACK": 0x2, "NAK": 0xA,
       "STALL": 0xE, "PRE": 0xC}


# ------------------------------------------------------------------------------------------------ packets
def crc5(value: int, nbits: int = 11) -> int:
    """USB token CRC5 (x^5 + x^2 + 1, init 11111, inverted) over `nbits` bits sent LSB first."""
    crc = 0x1F
    for i in range(nbits):
        if (crc ^ (value >> i)) & 1:
            crc = (crc >> 1) ^ 0x14
        else:
            crc >>= 1
    return crc ^ 0x1F


def _crc16_table() -> list[int]:
    t = []
    for b in range(256):
        c = b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
        t.append(c)
    return t


_CRC16 = _crc16_table()


def crc16(data: bytes) -> int:
    """USB data CRC16 (x^16 + x^15 + x^2 + 1, init 0xFFFF, inverted), as the 16-bit value sent LSB first."""
    crc = 0xFFFF
    for b in data:
        crc = (crc >> 8) ^ _CRC16[(crc ^ b) & 0xFF]
    return crc ^ 0xFFFF


def _ones_runs(payload: bytes) -> int:
    """Stuff bits in SYNC + payload (bytes sent LSB first): a 0 after every six consecutive 1s."""
    run = 1                                   # SYNC = 00000001: its last bit is a 1
    stuffed = 0
    for byte in payload:
        if byte == 0xFF:                      # fast path: 8 ones
            run += 8
            while run >= 6:
                stuffed += 1
                run -= 6
            continue
        for i in range(8):
            if (byte >> i) & 1:
                run += 1
                if run == 6:
                    stuffed += 1
                    run = 0
            else:
                run = 0
    return stuffed


def packet_bits(pid: str, payload: bytes = b"") -> int:
    """Bit times of one packet on the wire: SYNC + PID + payload (+ stuff bits) + EOP (2 SE0 + 1 J)."""
    p = PID[pid]
    body = bytes([p | ((~p & 0xF) << 4)]) + payload
    return 8 + 8 * len(body) + _ones_runs(body) + 3


def token_bits(pid: str, addr: int, ep: int) -> int:
    v = (addr & 0x7F) | ((ep & 0xF) << 7)
    v |= crc5(v) << 11
    return packet_bits(pid, struct.pack("<H", v))


def sof_bits(frame: int) -> int:
    v = frame & 0x7FF
    v |= crc5(v) << 11
    return packet_bits("SOF", struct.pack("<H", v))


def data_bits(pid: str, data: bytes) -> int:
    return packet_bits(pid, bytes(data) + struct.pack("<H", crc16(data)))


def handshake_bits(pid: str = "ACK") -> int:
    return packet_bits(pid)


# ------------------------------------------------------------------------------------------------ requests
STD_REQ = {0: "GET_STATUS", 1: "CLEAR_FEATURE", 3: "SET_FEATURE", 5: "SET_ADDRESS", 6: "GET_DESCRIPTOR",
           7: "SET_DESCRIPTOR", 8: "GET_CONFIGURATION", 9: "SET_CONFIGURATION", 10: "GET_INTERFACE",
           11: "SET_INTERFACE", 12: "SYNCH_FRAME"}
CLASS_REQ = {0x20: "SET_LINE_CODING", 0x21: "GET_LINE_CODING", 0x22: "SET_CONTROL_LINE_STATE",
             0x23: "SEND_BREAK", 0x0A: "SET_IDLE", 0x0B: "SET_PROTOCOL", 0x01: "GET_REPORT", 0x09: "SET_REPORT",
             0x02: "GET_IDLE", 0x03: "GET_PROTOCOL", 0xFE: "GET_MAX_LUN", 0xFF: "BULK_ONLY_RESET"}
DESC = {1: "DEVICE", 2: "CONFIGURATION", 3: "STRING", 4: "INTERFACE", 5: "ENDPOINT", 6: "DEVICE_QUALIFIER",
        0x21: "HID", 0x22: "HID_REPORT", 0x0F: "BOS"}


def build_setup(bm: int, req: int, value: int = 0, index: int = 0, length: int = 0) -> bytes:
    return struct.pack("<BBHHH", bm & 0xFF, req & 0xFF, value & 0xFFFF, index & 0xFFFF, length & 0xFFFF)


def describe_setup(setup: bytes) -> str:
    bm, req, value, index, length = struct.unpack("<BBHHH", bytes(setup[:8]))
    typ = (bm >> 5) & 3
    if typ == 0:
        name = STD_REQ.get(req, f"REQ{req}")
        if req == 6:
            d = DESC.get(value >> 8, f"0x{value >> 8:02X}")
            return f"GET_DESCRIPTOR({d}{'' if (value & 0xFF) == 0 else f' {value & 0xFF}'}, {length})"
        if req == 5:
            return f"SET_ADDRESS({value})"
        if req == 9:
            return f"SET_CONFIGURATION({value})"
        if req in (1, 3):
            return f"{name}({value}{'' if (bm & 3) == 0 else f', ep 0x{index:02X}'})"
        return f"{name}(0x{value:X}, 0x{index:X}, {length})"
    name = CLASS_REQ.get(req, f"REQ 0x{req:02X}") if typ == 1 else f"VENDOR 0x{req:02X}"
    return f"{name}(0x{value:X}, 0x{index:X}, {length})"


class Transaction:
    """One token transaction as the device sees it."""
    __slots__ = ("kind", "addr", "ep", "pid", "data", "t")

    def __init__(self, kind: str, addr: int, ep: int, pid: str | None = None, data: bytes = b"", t: float = 0.0):
        self.kind, self.addr, self.ep, self.pid, self.data, self.t = kind, addr, ep, pid, bytes(data), t

    def __repr__(self) -> str:
        return f"<{self.kind} {self.addr}.{self.ep} {self.pid or ''} {self.data.hex()}>"


# ------------------------------------------------------------------------------------------------ descriptors
class UsbEndpointInfo:
    def __init__(self, address: int, attributes: int, mps: int, interval: int):
        self.address, self.attributes, self.mps, self.interval = address, attributes, mps, interval

    @property
    def is_in(self) -> bool:
        return bool(self.address & 0x80)

    @property
    def type(self) -> str:
        return ("control", "isochronous", "bulk", "interrupt")[self.attributes & 3]

    def __repr__(self) -> str:
        return f"<EP 0x{self.address:02X} {self.type} mps={self.mps} interval={self.interval}>"


class UsbInterfaceInfo:
    def __init__(self, number: int, alt: int, cls: int, subclass: int, protocol: int, string: int = 0):
        self.number, self.alt, self.cls, self.subclass, self.protocol = number, alt, cls, subclass, protocol
        self.string = string
        self.endpoints: list[UsbEndpointInfo] = []
        self.extra: list[bytes] = []          # class-specific descriptors (CDC functional, HID)

    def __repr__(self) -> str:
        return f"<IF {self.number} class 0x{self.cls:02X}/{self.subclass}/{self.protocol} {self.endpoints}>"


class UsbDeviceInfo:
    """What enumeration learned about the device."""

    def __init__(self) -> None:
        self.address = 0
        self.speed = "fs"
        self.device_desc = b""
        self.config_desc = b""
        self.vid = self.pid = self.bcd_usb = self.bcd_device = 0
        self.cls = self.subclass = self.protocol = 0
        self.mps0 = 8
        self.num_configs = 0
        self.i_manufacturer = self.i_product = self.i_serial = 0
        self.strings: dict[int, str] = {}
        self.langid = 0x0409
        self.config_value = 0
        self.attributes = 0
        self.max_power_ma = 0
        self.interfaces: list[UsbInterfaceInfo] = []
        self.configured = False
        self.remote_wakeup = False
        self.drivers: list[Any] = []

    @property
    def manufacturer(self) -> str:
        return self.strings.get(self.i_manufacturer, "")

    @property
    def product(self) -> str:
        return self.strings.get(self.i_product, "")

    @property
    def serial(self) -> str:
        return self.strings.get(self.i_serial, "")

    def class_names(self) -> list[str]:
        names = {0x02: "CDC", 0x0A: "CDC-Data", 0x03: "HID", 0x08: "MSC", 0x01: "Audio", 0xFF: "vendor",
                 0xFE: "app-specific", 0x07: "printer", 0x0E: "video", 0xDC: "diagnostic"}
        out = []
        for i in self.interfaces:
            if i.alt:
                continue
            n = names.get(i.cls, f"0x{i.cls:02X}")
            if i.cls == 0x02 and i.subclass == 0x02:
                n = "CDC-ACM"
            if n not in out:
                out.append(n)
        return out

    def summary(self) -> dict:
        return {"address": self.address, "speed": self.speed, "vid": f"{self.vid:04X}", "pid": f"{self.pid:04X}",
                "manufacturer": self.manufacturer, "product": self.product, "serial": self.serial,
                "classes": self.class_names(), "configured": self.configured,
                "drivers": [getattr(d, "name", type(d).__name__) for d in self.drivers]}

    def describe(self) -> str:
        s = f"{self.vid:04X}:{self.pid:04X}"
        strs = " ".join(f'"{x}"' for x in (self.manufacturer, self.product) if x)
        if strs:
            s += " " + strs
        if self.serial:
            s += f" serial {self.serial}"
        s += f" [{', '.join(self.class_names()) or 'no interfaces'}] addr {self.address} {self.speed.upper()}"
        if not self.configured:
            s += " (not configured)"
        return s


def parse_descriptors(blob: bytes) -> list[tuple[int, bytes]]:
    """Split a configuration descriptor set into (bDescriptorType, descriptor bytes)."""
    out, i = [], 0
    blob = bytes(blob)
    while i + 2 <= len(blob):
        n = blob[i]
        if n < 2 or i + n > len(blob):
            break
        out.append((blob[i + 1], blob[i:i + n]))
        i += n
    return out


def _parse_config(info: UsbDeviceInfo, blob: bytes) -> None:
    info.interfaces = []
    cur: UsbInterfaceInfo | None = None
    for typ, d in parse_descriptors(blob):
        if typ == 2 and len(d) >= 9:
            info.config_value, info.attributes, info.max_power_ma = d[5], d[7], d[8] * 2
        elif typ == 4 and len(d) >= 9:
            cur = UsbInterfaceInfo(d[2], d[3], d[5], d[6], d[7], d[8])
            info.interfaces.append(cur)
        elif typ == 5 and len(d) >= 7 and cur is not None:
            cur.endpoints.append(UsbEndpointInfo(d[2], d[3], struct.unpack_from("<H", d, 4)[0] & 0x7FF, d[6]))
        elif cur is not None:
            cur.extra.append(d)


# ------------------------------------------------------------------------------------------------ bus
def _at(chip: Any, t: float, cb: Callable, *args: Any):
    if chip.now_ns() >= t - T_EPS:
        cb(*args)
        return None
    return chip.sched.at_ns(t, cb, *args)


class UsbBus:
    """The cable between one host port and one device controller."""

    def __init__(self, name: str = "USB"):
        self.name = name
        self.device: Any = None
        self.host: UsbHost | None = None
        self.listeners: list[Callable] = []
        self.vbus = False
        self.state = "detached"         # detached | attached | reset | default | suspended | resume
        self.speed: str | None = None   # "fs" / "ls" of the attached device
        self.packets = 0
        self.transactions = 0
        self.naks = 0
        self.stalls = 0
        self.timeouts = 0
        self.sofs = 0
        self.bytes_in = 0
        self.bytes_out = 0
        self.busy_until = -math.inf      # end of the transaction on the wire
        self.last_activity = -math.inf
        self._susp_ev = None
        self._dev_sus = False            # suspend reported to the device

    def __repr__(self) -> str:
        return f"<UsbBus {self.name}: {getattr(self.device, 'usb_name', None)} {self.state}>"

    # ------------------------------------------------------------------ membership
    @property
    def chip(self) -> Any:
        return getattr(self.device, "chip", None)

    def attach(self, device: Any) -> None:
        """Plug a device controller into this cable (it calls bus.device_changed() itself when ready)."""
        if self.device is not None and self.device is not device:
            raise ValueError(f"usb bus {self.name}: already has a device ({self.device.usb_name})")
        self.device = device
        att = getattr(device, "usb_attach", None)
        if att is not None:
            att(self)
        if self.vbus:
            device.usb_vbus(self, True, self.now())
        self.device_changed()

    def detach(self) -> None:
        dev = self.device
        if dev is None:
            return
        self._cancel_suspend()
        det = getattr(dev, "usb_detach", None)
        if det is not None:
            det()
        self.device = None
        self.state = "detached"
        if self.host is not None:
            self.host._disconnected("device removed")

    def add_host(self, **kw: Any) -> "UsbHost":
        if self.host is None:
            power = kw.pop("power", True)
            self.host = UsbHost(self, power=False, **kw)
            if power:
                self.host.power(True)
        return self.host

    def now(self) -> float:
        c = self.chip
        return c.now_ns() if c is not None else 0.0

    # ------------------------------------------------------------------ line state
    def pullup(self) -> str | None:
        dev = self.device
        if dev is None or not self.vbus:
            return None
        return dev.usb_pullup(self)

    def device_changed(self) -> None:
        """The device's pull-up may have changed (soft connect / disconnect)."""
        pu = self.pullup()
        if pu and self.state == "detached":
            self.state = "attached"
            self.speed = pu
            self.event("attach", f"device attached ({'full' if pu == 'fs' else 'low'} speed pull-up)")
            if self.host is not None:
                self.host._connected(pu)
        elif not pu and self.state != "detached":
            self.state = "detached"
            self._cancel_suspend()
            self.event("detach", "device disconnected (pull-up off)" if self.vbus else "VBUS off")
            if self.host is not None:
                self.host._disconnected("pull-up off")

    def set_vbus(self, on: bool) -> None:
        on = bool(on)
        if on == self.vbus:
            return
        self.vbus = on
        dev = self.device
        if dev is not None:
            self.event("vbus", f"VBUS {'on' if on else 'off'}")
            dev.usb_vbus(self, on, self.now())
        self.device_changed()

    @property
    def bit_ns(self) -> float:
        return LS_BIT_NS if self.speed == "ls" else FS_BIT_NS

    # ------------------------------------------------------------------ signalling (called by the host)
    def drive_reset(self, t0: float, dur: float, done: Callable[[], None]) -> None:
        dev, chip = self.device, self.chip
        self._cancel_suspend()
        self.state = "reset"
        self._dev_sus = False
        self.event("reset", f"USB reset (SE0 {dur / 1e6:g} ms)", t=t0)

        def start():
            if self.device is dev and self.state == "reset":
                dev.usb_reset(self, True, t0 + RESET_DETECT_NS)

        def end():
            if self.device is not dev or self.state != "reset":
                return
            self.speed = self.pullup() or self.speed
            self.state = "default"
            dev.usb_reset(self, False, t0 + dur)
            self.activity(t0 + dur)
            done()

        _at(chip, t0 + RESET_DETECT_NS, start)
        _at(chip, t0 + dur, end)

    def activity(self, t: float) -> None:
        """Bus traffic at t: restart the 3 ms suspend timer."""
        self.last_activity = t
        if self._dev_sus:
            return
        chip = self.chip
        if chip is None:
            return
        if self._susp_ev is not None:
            chip.sched.cancel(self._susp_ev)
        self._susp_ev = chip.sched.at_ns(t + SUSPEND_NS, self._suspend_timeout)

    def _cancel_suspend(self) -> None:
        if self._susp_ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._susp_ev)
        self._susp_ev = None

    def _suspend_timeout(self) -> None:
        self._susp_ev = None
        if self.state in ("detached", "reset") or self.device is None:
            return
        self._dev_sus = True
        self.state = "suspended"
        t = self.chip.now_ns()
        self.event("suspend", "bus idle 3 ms: device suspended", t=t)
        self.device.usb_suspend(self, t)

    def drive_resume(self, t0: float) -> None:
        """Host resume signalling (K) starts at t0."""
        if self.device is None or not self._dev_sus:
            return
        self._dev_sus = False
        self.state = "resume"
        self.event("resume", "resume signalling (K)", t=t0)
        self.device.usb_resume(self, t0)

    def remote_wakeup(self) -> None:
        """The device drives K (remote wakeup) while suspended."""
        h = self.host
        t = self.now()
        if self.state != "suspended":
            return
        self.event("wakeup", "device remote wakeup signalling (K)", t=t)
        if h is not None:
            h._remote_wakeup(t)

    def sof(self, frame: int, t0: float) -> float:
        """Send a SOF (full speed) or keep-alive (low speed) at t0; returns its end time."""
        dev = self.device
        self.sofs += 1
        if self.speed == "ls":
            t1 = t0 + 3 * LS_BIT_NS
            ka = getattr(dev, "usb_keepalive", None)
            if ka is not None:
                _at(self.chip, t1, ka, self, t1)
        else:
            t1 = t0 + sof_bits(frame) * FS_BIT_NS
            _at(self.chip, t1, dev.usb_sof, self, frame & 0x7FF, t1)
        self.activity(t1)
        return t1

    # ------------------------------------------------------------------ logging
    def event(self, event: str, msg: str, t: float | None = None, **fields: Any) -> None:
        info = {"link": self.name, "dev": getattr(self.device, "usb_name", "?"), "event": event, "msg": msg,
                **fields}
        self._log(info, t)

    def _log(self, info: dict, t: float | None = None) -> None:
        if t is None:
            t = self.now()
        log = getattr(self.device, "log", None)
        if log is not None:
            log.event("usb", t, **info)
        for cb in list(self.listeners):
            cb(self, info.get("xfer") or info.get("event"), dict(info, t_ns=t))


# ------------------------------------------------------------------------------------------------ host
class Delay:
    __slots__ = ("ns",)

    def __init__(self, ns: float):
        self.ns = ns


class Reset:
    __slots__ = ()


class Transfer:
    """A control, bulk or interrupt transfer queued on the host."""

    def __init__(self, kind: str, addr: int, ep: int, *, setup: bytes = b"", data: bytes = b"", length: int = 0,
                 mps: int = 64, interval: int = 1, cb: Callable | None = None, zlp: bool = False,
                 label: str | None = None, log: bool = True):
        self.kind = kind                 # control | bulk | interrupt
        self.addr, self.ep = addr, ep    # ep with direction bit 0x80 for IN (bulk/interrupt)
        self.setup = bytes(setup)
        self.out = bytes(data)
        self.length = length             # IN: bytes wanted
        self.mps = max(1, mps)
        self.interval = max(1, interval)
        self.cb = cb
        self.zlp = zlp
        self.label = label
        self.log = log
        self.data = bytearray()          # IN data received
        self.sent = 0                    # OUT bytes sent
        self.stage = "setup" if kind == "control" else "data"
        self.status: str | None = None
        self.naks = 0
        self.errors = 0
        self.blocked_until = -math.inf
        self.t_start: float | None = None
        self.zlp_sent = False
        self.seq = 0

    @property
    def is_in(self) -> bool:
        if self.kind == "control":
            return bool(self.setup[0] & 0x80)
        return bool(self.ep & 0x80)

    def __repr__(self) -> str:
        return f"<Transfer {self.kind} {self.addr}.{self.ep:02X} {self.stage} {self.status}>"


class UsbHost:
    """Host controller + minimal USB stack on a UsbBus: powers VBUS, waits for a connect, debounces, resets,
    enumerates, configures and binds class drivers (CDC-ACM, HID, MSC).  Scriptable: `control()`, `bulk_out()`,
    `bulk_in()`, `interrupt_in()`, `suspend()`, `resume()`, `reset_port()`, `unplug()`, `plug()`."""

    def __init__(self, bus: UsbBus, *, power: bool = True, debounce_ns: float = 100e6, reset_ns: float = 10e6,
                 reset_recovery_ns: float = 10e6, set_address_recovery_ns: float = 2e6,
                 resume_ns: float = 20e6, resume_recovery_ns: float = 10e6, ctrl_nak_retry_ns: float = 20e3,
                 ctrl_timeout_ns: float = 5e9,
                 second_reset: bool = True, enum_retries: int = 2, auto_enumerate: bool = True,
                 drivers: bool = True, line_coding: tuple[int, int, int, int] = (115200, 0, 0, 8),
                 cdc_open: bool = True, name: str = "host"):
        self.bus = bus
        self.name = name
        self.debounce_ns = float(debounce_ns)
        self.reset_ns = float(reset_ns)
        self.reset_recovery_ns = float(reset_recovery_ns)
        self.set_address_recovery_ns = float(set_address_recovery_ns)
        self.resume_ns = float(resume_ns)
        self.resume_recovery_ns = float(resume_recovery_ns)
        self.ctrl_nak_retry_ns = float(ctrl_nak_retry_ns)
        self.ctrl_timeout_ns = float(ctrl_timeout_ns)   # a control transfer NAKed this long fails (Linux: 5 s)
        self.second_reset = bool(second_reset)
        self.enum_retries = int(enum_retries)
        self.auto_enumerate = bool(auto_enumerate)
        self.use_drivers = bool(drivers)
        self.line_coding = tuple(line_coding)
        self.cdc_open = bool(cdc_open)
        self.device: UsbDeviceInfo | None = None      # the enumerated device (None until enumeration completes)
        self.devices: list[UsbDeviceInfo] = []        # every successful enumeration (re-plug -> new entry)
        self.state = "off"           # off | powered | debounce | enumerating | configured | failed | suspended
        self.frame = 0
        self.running = False         # SOFs on
        self._frame_t0 = 0.0
        self._sof_ev = None
        self._kick_ev = None
        self._kick_t = math.inf
        self._xfers: deque[Transfer] = deque()
        self._cur: Transfer | None = None             # transfer whose transaction is on the wire
        self._toggles: dict[tuple[int, int], int] = {}
        self._ipoll: dict[tuple[int, int], int] = {}   # interrupt endpoint -> frame of its last poll
        self._seq = 0                                 # transfer submission order (one transfer per endpoint at a time)
        self._next_addr = 1
        self._task: Generator | None = None
        self._task_ev = None
        self._timers: list = []
        self._gen = 0                                 # connection generation (stale callbacks are ignored)
        self._resume_until = -math.inf
        self.failures: list[str] = []
        self.log_transfers = True
        self.cdc_rx = bytearray()                     # every byte any CDC-ACM driver received (all enumerations)
        self.cdc_listeners: list[Callable[[bytes], None]] = []   # host-side byte stream (survives re-enumeration)
        self._cdc_pending = bytearray()               # cdc_write() data waiting for an open port
        if power:
            self.power(True)

    def __repr__(self) -> str:
        return f"<UsbHost {self.bus.name} {self.state}>"

    # ------------------------------------------------------------------ time helpers
    @property
    def chip(self) -> Any:
        return self.bus.chip

    def now(self) -> float:
        return self.bus.now()

    def _after(self, t: float, cb: Callable, *args: Any) -> None:
        gen = self._gen

        def fire():
            if gen == self._gen:
                cb(*args)
        if self.chip is None:
            return
        self._timers.append(_at(self.chip, t, fire))

    def _cancel_timers(self) -> None:
        sched = getattr(self.chip, "sched", None)
        for ev in self._timers:
            if ev is not None and sched is not None:
                sched.cancel(ev)
        self._timers = []
        for name in ("_sof_ev", "_kick_ev", "_task_ev"):
            ev = getattr(self, name)
            if ev is not None and sched is not None:
                sched.cancel(ev)
            setattr(self, name, None)
        self._kick_t = math.inf

    # ------------------------------------------------------------------ port power / connect
    def power(self, on: bool = True) -> None:
        """Switch VBUS on the port."""
        self.state = "powered" if on else "off"
        if not on:
            self._stop_all("VBUS off")
        was = self.bus.vbus
        self.bus.set_vbus(on)
        if on and was and self.bus.state != "detached":
            self._connected(self.bus.speed or "fs")

    def unplug(self) -> None:
        """Pull the cable: VBUS off, device gone."""
        self.power(False)

    def plug(self) -> None:
        self.power(True)

    def _connected(self, speed: str) -> None:
        if self.state == "off":
            return
        self._gen += 1
        self.state = "debounce"
        t = self.now()
        self._after(t + self.debounce_ns, self._debounced)

    def _disconnected(self, why: str) -> None:
        self._stop_all(why)
        if self.state != "off":
            self.state = "powered"

    def _stop_all(self, why: str) -> None:
        self._gen += 1
        self._cancel_timers()
        self.running = False
        for x in list(self._xfers):
            self._finish(x, "disconnected", log=False)
        self._xfers.clear()
        self._cur = None
        self._task = None
        if self.device is not None:
            for d in self.device.drivers:
                f = getattr(d, "disconnected", None)
                if f:
                    f()
        self.device = None

    def _debounced(self) -> None:
        if not self.bus.pullup():
            self.state = "powered"
            return
        if self.auto_enumerate:
            self.start_task(self._enumerate())

    # ------------------------------------------------------------------ frames
    def _start_sof(self, t: float) -> None:
        if self.running:
            return
        self.running = True
        self._frame_t0 = t
        self._sof_ev = None
        self._do_sof()

    def _stop_sof(self) -> None:
        self.running = False
        if self._sof_ev is not None:
            self.chip.sched.cancel(self._sof_ev)
            self._sof_ev = None

    def _do_sof(self) -> None:
        if not self.running:
            return
        t0 = max(self.chip.now_ns(), self._frame_t0)
        if self.bus.busy_until > t0:          # cannot happen with the EOF margin, but keep frames honest
            t0 = self.bus.busy_until
        self._frame_t0 = t0
        self.frame = (self.frame + 1) & 0x7FF
        t1 = self.bus.sof(self.frame, t0)
        self.bus.busy_until = max(self.bus.busy_until, t1)
        self._sof_ev = self.chip.sched.at_ns(t0 + FRAME_NS, self._do_sof)
        for x in self._xfers:
            if x.blocked_until < t1 and x.kind == "bulk":
                x.blocked_until = -math.inf
        self._kick(t1)

    def _frame_end(self) -> float:
        return self._frame_t0 + FRAME_NS - EOF_BITS * self.bus.bit_ns

    # ------------------------------------------------------------------ tasks (generator coroutines)
    def start_task(self, gen: Generator) -> None:
        self._task = gen
        self._step_task(None)

    def _step_task(self, value: Any) -> None:
        g = self._task
        if g is None:
            return
        try:
            req = g.send(value)
        except StopIteration:
            self._task = None
            return
        gen = self._gen
        if isinstance(req, Delay):
            self._after(self.now() + req.ns, self._step_task, None)
        elif isinstance(req, Reset):
            self._port_reset(lambda: self._step_task(None) if gen == self._gen else None)
        elif isinstance(req, Transfer):
            user_cb = req.cb

            def done(x, _u=user_cb, _g=gen):
                if _u is not None:
                    _u(x)
                if _g == self._gen and self._task is g:
                    self._step_task(x)
            req.cb = done
            self.submit(req)
        else:
            raise TypeError(f"usb host task yielded {req!r}")

    def _port_reset(self, done: Callable[[], None]) -> None:
        self._stop_sof()
        for x in list(self._xfers):
            self._finish(x, "reset", log=False)
        self._xfers.clear()
        self._toggles.clear()
        t = self.now()
        self.bus.drive_reset(t, self.reset_ns, lambda: (self._start_sof(self.chip.now_ns()), done()))

    def reset_port(self) -> None:
        """Reset the device and enumerate it again."""
        self._gen += 1
        self._cancel_timers()
        if self.device is not None:
            for d in self.device.drivers:
                f = getattr(d, "disconnected", None)
                if f:
                    f()
        self.device = None
        self.start_task(self._enumerate())

    # ------------------------------------------------------------------ enumeration
    def _ctrl(self, addr: int, bm: int, req: int, value: int = 0, index: int = 0, data: bytes | int = 0,
              mps: int = 8, label: str | None = None) -> Transfer:
        if isinstance(data, int):
            setup = build_setup(bm, req, value, index, data)
            return Transfer("control", addr, 0, setup=setup, length=data, mps=mps, label=label)
        setup = build_setup(bm, req, value, index, len(data))
        return Transfer("control", addr, 0, setup=setup, data=bytes(data), mps=mps, label=label)

    def _enumerate(self):
        attempt = 0
        while True:
            self.state = "enumerating"
            err = yield from self._enumerate_once()
            if err is None:
                return
            attempt += 1
            if attempt > self.enum_retries:
                self.state = "failed"
                self.failures.append(err)
                self.bus.event("error", f"device not recognised: {err}")
                return
            self.bus.event("error", f"enumeration failed ({err}); retrying")
            yield Delay(self.reset_recovery_ns)

    def _enumerate_once(self):
        bus = self.bus
        yield Reset()
        yield Delay(self.reset_recovery_ns)
        info = UsbDeviceInfo()
        info.speed = bus.speed or "fs"
        mps0 = 8 if info.speed == "ls" else 64            # unknown yet: a full-speed host asks with 64
        x = yield self._ctrl(0, 0x80, 6, 0x0100, 0, 64, mps=mps0)
        if x.status != "ok" or len(x.data) < 8:
            return f"GET_DESCRIPTOR(DEVICE) at address 0: {x.status if x.status != 'ok' else 'short'}"
        mps0 = x.data[7]
        if mps0 not in (8, 16, 32, 64):
            return f"bad bMaxPacketSize0 {mps0}"
        info.mps0 = mps0
        if self.second_reset:
            yield Reset()
            yield Delay(self.reset_recovery_ns)
        addr = self._next_addr
        self._next_addr = self._next_addr % 127 + 1
        x = yield self._ctrl(0, 0x00, 5, addr, 0, b"", mps=mps0)
        if x.status != "ok":
            return f"SET_ADDRESS({addr}): {x.status}"
        info.address = addr
        yield Delay(self.set_address_recovery_ns)
        x = yield self._ctrl(addr, 0x80, 6, 0x0100, 0, 18, mps=mps0)
        if x.status != "ok" or len(x.data) < 18:
            return f"GET_DESCRIPTOR(DEVICE): {x.status if x.status != 'ok' else f'{len(x.data)} bytes'}"
        d = bytes(x.data[:18])
        info.device_desc = d
        (info.bcd_usb, info.cls, info.subclass, info.protocol, _m, info.vid, info.pid, info.bcd_device,
         info.i_manufacturer, info.i_product, info.i_serial, info.num_configs) = struct.unpack("<xxHBBBBHHHBBBB", d)
        x = yield self._ctrl(addr, 0x80, 6, 0x0200, 0, 9, mps=mps0)
        if x.status != "ok" or len(x.data) < 9:
            return f"GET_DESCRIPTOR(CONFIGURATION, 9): {x.status}"
        total = struct.unpack_from("<H", x.data, 2)[0]
        x = yield self._ctrl(addr, 0x80, 6, 0x0200, 0, total, mps=mps0)
        if x.status != "ok":
            return f"GET_DESCRIPTOR(CONFIGURATION, {total}): {x.status}"
        info.config_desc = bytes(x.data)
        _parse_config(info, info.config_desc)
        idx = [i for i in (info.i_manufacturer, info.i_product, info.i_serial) if i]
        if idx:
            x = yield self._ctrl(addr, 0x80, 6, 0x0300, 0, 255, mps=mps0)
            if x.status == "ok" and len(x.data) >= 4:
                info.langid = struct.unpack_from("<H", x.data, 2)[0]
                for i in idx:
                    x = yield self._ctrl(addr, 0x80, 6, 0x0300 | i, info.langid, 255, mps=mps0)
                    if x.status == "ok" and len(x.data) >= 2:
                        n = min(x.data[0], len(x.data))
                        info.strings[i] = bytes(x.data[2:n]).decode("utf-16-le", "replace")
        x = yield self._ctrl(addr, 0x00, 9, info.config_value or 1, 0, b"", mps=mps0)
        if x.status != "ok":
            return f"SET_CONFIGURATION: {x.status}"
        info.configured = True
        for k in list(self._toggles):
            if k[0] == addr:
                del self._toggles[k]
        self.device = info
        self.devices.append(info)
        self.state = "configured"
        self.bus.event("enumerated", f"enumerated {info.describe()}", device=info.summary())
        if self.use_drivers:
            yield from self._bind_drivers(info)
        return None

    def _bind_drivers(self, info: UsbDeviceInfo):
        ifs = [i for i in info.interfaces if i.alt == 0]
        for i in ifs:
            if i.cls == 0x02 and i.subclass == 0x02:
                data_if = next((j for j in ifs if j.cls == 0x0A and j.number in _cdc_data_ifs(i, ifs)), None)
                if data_if is not None:
                    drv = CdcAcm(self, info, i, data_if)
                    info.drivers.append(drv)
                    yield from drv.start()
            elif i.cls == 0x03:
                drv = HidDevice(self, info, i)
                info.drivers.append(drv)
                yield from drv.start()
            elif i.cls == 0x08 and i.subclass == 0x06 and i.protocol == 0x50:
                drv = MscDevice(self, info, i)
                info.drivers.append(drv)
                yield from drv.start()

    def driver(self, kind: str) -> Any:
        """The first bound class driver of a kind ("cdc", "hid", "msc"), or None."""
        if self.device is None:
            return None
        cls = {"cdc": CdcAcm, "hid": HidDevice, "msc": MscDevice}[kind]
        return next((d for d in self.device.drivers if isinstance(d, cls)), None)

    @property
    def cdc(self) -> "CdcAcm | None":
        return self.driver("cdc")

    def cdc_write(self, data: bytes | str) -> None:
        """Send bytes to the device's CDC-ACM port; kept until a port is enumerated and open."""
        if isinstance(data, str):
            data = data.encode("latin-1")
        cdc = self.cdc
        if cdc is not None and cdc.open:
            cdc.write(data)
        else:
            self._cdc_pending += data

    @property
    def hid(self) -> "HidDevice | None":
        return self.driver("hid")

    @property
    def msc(self) -> "MscDevice | None":
        return self.driver("msc")

    # ------------------------------------------------------------------ public transfer API
    def submit(self, x: Transfer) -> Transfer:
        self._seq += 1
        x.seq = self._seq
        x.t_start = self.now()
        self._xfers.append(x)
        self._kick(self.now())
        return x

    def control(self, bm: int, req: int, value: int = 0, index: int = 0, data: bytes | int = 0,
                cb: Callable | None = None) -> Transfer:
        """Queue a control transfer to the enumerated device (data: bytes to send, or the IN length)."""
        dev = self.device
        x = self._ctrl(dev.address if dev else 0, bm, req, value, index, data, mps=dev.mps0 if dev else 8)
        x.cb = cb
        return self.submit(x)

    def bulk_out(self, ep: int, data: bytes, cb: Callable | None = None, *, mps: int = 64, zlp: bool = False,
                 log: bool = True) -> Transfer:
        dev = self.device
        return self.submit(Transfer("bulk", dev.address if dev else 0, ep & 0x0F, data=data, mps=mps, cb=cb,
                                    zlp=zlp, log=log))

    def bulk_in(self, ep: int, length: int, cb: Callable | None = None, *, mps: int = 64,
                log: bool = True) -> Transfer:
        dev = self.device
        return self.submit(Transfer("bulk", dev.address if dev else 0, 0x80 | ep, length=length, mps=mps, cb=cb,
                                    log=log))

    def interrupt_in(self, ep: int, length: int, interval: int, cb: Callable | None = None, *, mps: int = 64,
                     log: bool = True) -> Transfer:
        dev = self.device
        return self.submit(Transfer("interrupt", dev.address if dev else 0, 0x80 | ep, length=length, mps=mps,
                                    interval=interval, cb=cb, log=log))

    def interrupt_out(self, ep: int, data: bytes, interval: int, cb: Callable | None = None, *,
                      mps: int = 64) -> Transfer:
        dev = self.device
        return self.submit(Transfer("interrupt", dev.address if dev else 0, ep & 0x0F, data=data, mps=mps,
                                    interval=interval, cb=cb))

    def cancel(self, x: Transfer) -> None:
        if x in self._xfers and x is not self._cur:
            self._xfers.remove(x)
            x.status = "cancelled"

    # ------------------------------------------------------------------ suspend / resume
    def suspend(self) -> None:
        """Stop SOFs: the device suspends after 3 ms of idle bus.  A configured device whose configuration
        descriptor announces remote wakeup (bmAttributes bit 5) first gets SET_FEATURE(DEVICE_REMOTE_WAKEUP), as
        Linux / Windows do before selective suspend."""
        dev = self.device
        if dev is not None and dev.configured and dev.attributes & 0x20 and not dev.remote_wakeup and self.running:
            def done(x, _d=dev):
                if x.status == "ok":
                    _d.remote_wakeup = True
                self._suspend_now()
            self.control(0x00, 3, 1, 0, b"", cb=done)
            return
        self._suspend_now()

    def _suspend_now(self) -> None:
        self._stop_sof()
        if self.state in ("configured", "enumerating"):
            self._pre_suspend = self.state
        self.state = "suspended"

    def resume(self) -> None:
        """Drive resume (K) for resume_ns, then restart SOFs; traffic after resume_recovery_ns."""
        if self.running:
            return
        t = self.now()
        self.bus.drive_resume(t)
        self._resume_until = t + self.resume_ns + self.resume_recovery_ns
        self._after(t + self.resume_ns, self._resumed)

    def _resumed(self) -> None:
        self.state = getattr(self, "_pre_suspend", "configured")
        self.bus.state = "default"
        self._start_sof(self.now())

    def _remote_wakeup(self, t: float) -> None:
        dev = self.device
        if dev is None or not dev.remote_wakeup:
            self.bus.event("wakeup", "remote wakeup ignored: not enabled by SET_FEATURE(DEVICE_REMOTE_WAKEUP)", t=t)
            return
        self._after(t + 1e6, self.resume)

    # ------------------------------------------------------------------ scheduler
    def _kick(self, t: float) -> None:
        """Run the next transaction at t or later (when the bus is idle)."""
        if self.chip is None:
            return
        t = max(t, self.bus.busy_until)
        if t >= self._kick_t:
            return
        if self._kick_ev is not None:
            self.chip.sched.cancel(self._kick_ev)
        self._kick_t = t
        gen = self._gen

        def run():
            self._kick_ev = None
            self._kick_t = math.inf
            if gen == self._gen:
                self._schedule()
        if self.chip.now_ns() >= t - T_EPS and self._cur is None:
            self._kick_t = math.inf
            run()
        else:
            self._kick_ev = self.chip.sched.at_ns(t, run)

    def _schedule(self) -> None:
        if self._cur is not None or not self.running or not self._xfers:
            return
        now = self.chip.now_ns()
        if now < self.bus.busy_until - T_EPS:
            self._kick(self.bus.busy_until)
            return
        if now < self._resume_until - T_EPS:
            self._kick(self._resume_until)
            return
        bit = self.bus.bit_ns
        frame_end = self._frame_end()
        best, wake = None, math.inf
        periodic = [x for x in self._xfers if x.kind == "interrupt"]
        others = [x for x in self._xfers if x.kind != "interrupt"]
        for x in [x for x in others if x.kind == "control" and x.t_start is not None
                  and now - x.t_start > self.ctrl_timeout_ns]:
            self._finish(x, "timeout")
            others.remove(x)
        first: dict[tuple[int, int], int] = {}        # endpoint queue heads: transfers to one endpoint run in order
        for x in self._xfers:
            k = (x.addr, 0 if x.kind == "control" else x.ep)
            if k not in first or x.seq < first[k]:
                first[k] = x.seq
        for x in periodic + others:
            if first.get((x.addr, 0 if x.kind == "control" else x.ep)) != x.seq:
                continue
            if x.kind == "interrupt":
                last = self._ipoll.get((x.addr, x.ep))
                if last is not None and ((self.frame - last) & 0x7FF) < x.interval:
                    continue                         # polled less than bInterval frames ago
            elif x.blocked_until > now + T_EPS:
                wake = min(wake, x.blocked_until)
                continue
            dur = self._max_duration(x) * bit
            if now + dur > frame_end:
                continue
            best = x
            break
        if best is None:
            if wake < frame_end:
                self._kick(wake)
            return                                   # next SOF kicks again
        if best in others:                           # round robin among control / bulk
            self._xfers.remove(best)
            self._xfers.append(best)
        self._run(best, now)

    def _max_duration(self, x: Transfer) -> int:
        n = 8 if x.stage == "setup" else x.mps
        return (token_bits("IN", 0, 0) + IPG_BITS + data_bits("DATA0", b"\xff" * n) * 1 + IPG_BITS
                + handshake_bits() + HOST_GAP_BITS)

    def _toggle(self, addr: int, ep: int) -> int:
        return self._toggles.get((addr, ep), 0)

    def _set_toggle(self, addr: int, ep: int, v: int) -> None:
        self._toggles[(addr, ep)] = v & 1

    # ------------------------------------------------------------------ one transaction
    def _run(self, x: Transfer, t0: float) -> None:
        self._cur = x
        bus, dev, bit = self.bus, self.bus.device, self.bus.bit_ns
        chip = self.chip
        if x.t_start is None:
            x.t_start = t0
        # what goes on the wire now
        if x.kind == "control":
            if x.stage == "setup":
                kind, ep, pid, payload = "SETUP", 0, "DATA0", x.setup
            elif x.stage == "data":
                if x.is_in:
                    kind, ep = "IN", 0
                else:
                    kind, ep = "OUT", 0
                    pid = "DATA1" if self._toggle(x.addr, 0) else "DATA0"
                    payload = x.out[x.sent:x.sent + x.mps]
            else:                                    # status stage: opposite direction, DATA1, zero length
                kind = "OUT" if x.is_in else "IN"
                ep, pid, payload = 0, "DATA1", b""
        else:
            ep = x.ep & 0x0F
            if x.is_in:
                kind = "IN"
            else:
                kind = "OUT"
                pid = "DATA1" if self._toggle(x.addr, ep) else "DATA0"
                payload = x.out[x.sent:x.sent + x.mps]
        self.bus.transactions += 1
        if x.kind == "interrupt":
            self._ipoll[(x.addr, x.ep)] = self.frame
        t_tok = t0 + token_bits(kind, x.addr, ep) * bit
        bus.packets += 1
        if kind in ("SETUP", "OUT"):
            t_data = t_tok + IPG_BITS * bit + data_bits(pid, payload) * bit
            bus.packets += 1
            tr = Transaction(kind, x.addr, ep, pid, payload, t_data)

            def at_data():
                if self._cur is not x or self.bus.device is not dev:
                    return
                if kind == "SETUP":
                    r = dev.usb_setup(bus, tr)
                else:
                    r = dev.usb_out(bus, tr)
                if r is None:
                    t_end = t_data + RESP_TIMEOUT_BITS * bit
                else:
                    t_end = t_data + IPG_BITS * bit + handshake_bits(r) * bit
                    bus.packets += 1
                bus.busy_until = t_end
                bus.activity(t_end)
                _at(chip, t_end, self._out_result, x, r, len(payload), t_end)
            bus.busy_until = t_data + IPG_BITS * bit + handshake_bits() * bit
            _at(chip, t_data, at_data)
        else:
            tr = Transaction("IN", x.addr, ep, None, b"", t_tok)

            def at_token():
                if self._cur is not x or self.bus.device is not dev:
                    return
                r = dev.usb_in(bus, tr)
                if r is None or isinstance(r, str):
                    if r is None:
                        t_end = t_tok + RESP_TIMEOUT_BITS * bit
                    else:
                        t_end = t_tok + IPG_BITS * bit + handshake_bits(r) * bit
                        bus.packets += 1
                    bus.busy_until = t_end
                    bus.activity(t_end)
                    _at(chip, t_end, self._in_result, x, r, None, t_end, tr)
                    return
                dpid, data = r
                data = bytes(data)
                t_d = t_tok + IPG_BITS * bit + data_bits(dpid, data) * bit
                t_end = t_d + IPG_BITS * bit + handshake_bits("ACK") * bit
                bus.packets += 2
                bus.busy_until = t_end
                bus.activity(t_end)
                _at(chip, t_end, self._in_result, x, dpid, data, t_end, tr)
            bus.busy_until = t_tok + RESP_TIMEOUT_BITS * bit
            _at(chip, t_tok, at_token)

    def _next(self, t: float) -> None:
        self._cur = None
        self._kick(t + HOST_GAP_BITS * self.bus.bit_ns)

    def _error(self, x: Transfer, what: str, t: float) -> None:
        x.errors += 1
        if what == "timeout":
            self.bus.timeouts += 1
        if x.errors >= MAX_ERRORS:
            self._finish(x, what)
        self._next(t)

    def _out_result(self, x: Transfer, r: str | None, n: int, t: float) -> None:
        bus = self.bus
        if self._cur is not x:
            return
        if r is None:
            self._error(x, "timeout", t)
            return
        if r == "NAK":
            bus.naks += 1
            x.naks += 1
            self._nak_block(x, t)
            self._next(t)
            return
        if r == "STALL":
            bus.stalls += 1
            self._finish(x, "stall")
            self._next(t)
            return
        x.errors = 0
        if x.kind == "control":
            if x.stage == "setup":
                self._set_toggle(x.addr, 0, 1)
                bus.bytes_out += 8
                x.stage = "data" if (x.out or (x.is_in and x.length)) else "status"
            elif x.stage == "data":
                self._set_toggle(x.addr, 0, self._toggle(x.addr, 0) ^ 1)
                x.sent += n
                bus.bytes_out += n
                if x.sent >= len(x.out):
                    x.stage = "status"
            else:                                    # OUT status of a control read
                self._finish(x, "ok")
        else:
            ep = x.ep & 0x0F
            self._set_toggle(x.addr, ep, self._toggle(x.addr, ep) ^ 1)
            x.sent += n
            bus.bytes_out += n
            if x.sent >= len(x.out):
                if x.zlp and len(x.out) and n == x.mps and not x.zlp_sent:
                    x.zlp_sent = True                # a zero-length packet follows: next round sends b""
                else:
                    self._finish(x, "ok")
        self._next(t)

    def _in_result(self, x: Transfer, r: str | None, data: bytes | None, t: float, tr: Transaction) -> None:
        bus, dev = self.bus, self.bus.device
        if self._cur is not x:
            return
        if data is None:
            if r is None:
                self._error(x, "timeout", t)
                return
            if r == "NAK":
                bus.naks += 1
                x.naks += 1
                self._nak_block(x, t)
                self._next(t)
                return
            bus.stalls += 1
            self._finish(x, "stall")
            self._next(t)
            return
        # data packet received: the host acknowledges (also a repeated one with the wrong toggle)
        if dev is not None:
            done = getattr(dev, "usb_in_done", None)
            if done is not None:
                done(bus, tr, True, t)
        x.errors = 0
        if x.kind == "control" and x.stage == "status":
            self._finish(x, "ok")
            self._next(t)
            return
        ep = 0 if x.kind == "control" else x.ep
        want = "DATA1" if self._toggle(x.addr, ep) else "DATA0"
        if r != want:
            x.errors = 0                             # retransmission: ACKed and ignored
            self._next(t)
            return
        self._set_toggle(x.addr, ep, self._toggle(x.addr, ep) ^ 1)
        room = max(0, x.length - len(x.data))
        x.data += data[:room]
        bus.bytes_in += len(data)
        short = len(data) < x.mps
        if short or len(x.data) >= x.length:
            if x.kind == "control":
                x.stage = "status"
            else:
                self._finish(x, "ok")
        self._next(t)

    def _nak_block(self, x: Transfer, t: float) -> None:
        if x.kind == "control":
            x.blocked_until = t + self.ctrl_nak_retry_ns
        elif x.kind == "bulk":
            x.blocked_until = self._frame_t0 + FRAME_NS          # next frame


    def _finish(self, x: Transfer, status: str, log: bool = True) -> None:
        if x.status is not None and x.status != "pending":
            pass
        x.status = status
        if x in self._xfers:
            self._xfers.remove(x)
        if log and x.log and self.log_transfers:
            self._log_transfer(x)
        if x.cb is not None:
            x.cb(x)

    def _log_transfer(self, x: Transfer) -> None:
        bus = self.bus
        if x.kind == "control":
            data = bytes(x.data) if x.is_in else x.out
            req = describe_setup(x.setup)
            msg = f"{req}: " + (f"{len(data)} bytes {'in' if x.is_in else 'out'}" if data else "no data")
            if x.status != "ok":
                msg += f" {x.status.upper()}"
            if x.naks:
                msg += f" ({x.naks} NAKs)"
            info = {"link": bus.name, "dev": getattr(bus.device, "usb_name", "?"), "xfer": "control", "req": req,
                    "setup": x.setup.hex(), "dir": "in" if x.is_in else "out", "len": len(data),
                    "data": data[:64].hex(), "status": x.status, "naks": x.naks, "addr": x.addr, "msg": msg}
        else:
            data = bytes(x.data) if x.is_in else x.out
            txt = _printable(data)
            msg = (f"{x.kind} {'IN' if x.is_in else 'OUT'} ep 0x{x.ep:02X}: {len(data)} bytes"
                   + (f" {txt!r}" if txt else (f" {data[:16].hex()}" if data else ""))
                   + ("" if x.status == "ok" else f" {x.status.upper()}"))
            info = {"link": bus.name, "dev": getattr(bus.device, "usb_name", "?"), "xfer": x.kind, "ep": x.ep,
                    "dir": "in" if x.is_in else "out", "len": len(data), "data": data[:64].hex(), "text": txt,
                    "status": x.status, "addr": x.addr, "msg": msg}
        bus._log(info, self.now())

    # ------------------------------------------------------------------ status
    def summary(self) -> dict:
        b = self.bus
        return {"state": self.state, "frame": self.frame, "device": self.device.summary() if self.device else None,
                "transactions": b.transactions, "naks": b.naks, "stalls": b.stalls, "timeouts": b.timeouts,
                "sofs": b.sofs, "bytes_in": b.bytes_in, "bytes_out": b.bytes_out, "failures": list(self.failures)}


def _printable(data: bytes) -> str:
    if not data:
        return ""
    try:
        s = data.decode("ascii")
    except UnicodeDecodeError:
        return ""
    if all(c.isprintable() or c in "\r\n\t" for c in s):
        return s[:64]
    return ""


def _cdc_data_ifs(comm: UsbInterfaceInfo, ifs: list[UsbInterfaceInfo]) -> list[int]:
    """Data interfaces of a CDC communication interface: the union functional descriptor's slaves, else the next."""
    for d in comm.extra:
        if len(d) >= 5 and d[1] == 0x24 and d[2] == 0x06:
            return list(d[4:])
    return [comm.number + 1]


# ------------------------------------------------------------------------------------------------ class drivers
class CdcAcm:
    """CDC-ACM host driver: SET_LINE_CODING, SET_CONTROL_LINE_STATE (DTR|RTS), keeps a bulk IN read pending
    (bytes -> `rx`, `rx_listeners`), `write(bytes)` queues bulk OUT transfers, polls the notification endpoint."""
    name = "cdc_acm"

    def __init__(self, host: UsbHost, info: UsbDeviceInfo, comm: UsbInterfaceInfo, data: UsbInterfaceInfo):
        self.host, self.info, self.comm, self.data_if = host, info, comm, data
        self.ep_in = next((e for e in data.endpoints if e.is_in and e.type == "bulk"), None)
        self.ep_out = next((e for e in data.endpoints if not e.is_in and e.type == "bulk"), None)
        self.ep_notify = next((e for e in comm.endpoints if e.is_in and e.type == "interrupt"), None)
        self.rx = bytearray()
        self.tx_bytes = 0
        self.rx_listeners: list[Callable[[bytes], None]] = []
        self.notifications: list[bytes] = []
        self.serial_state = 0
        self.line_coding = host.line_coding
        self.open = False
        self._reading = None
        self._txq: deque[bytes] = deque()
        self._writing = None
        self.read_size = 64 * 8

    def start(self):
        h = self.host
        if self.ep_in is None or self.ep_out is None:
            return
        if h.cdc_open:
            baud, stop, parity, bits = self.line_coding
            x = yield h._ctrl(self.info.address, 0x21, 0x20, 0, self.comm.number,
                              struct.pack("<IBBB", baud, stop, parity, bits), mps=self.info.mps0)
            x = yield h._ctrl(self.info.address, 0x21, 0x22, 0x0003, self.comm.number, b"", mps=self.info.mps0)
            self.open = x.status == "ok"
        else:
            self.open = True
        self._read()
        if self.ep_notify is not None:
            self._poll_notify()
        if h._cdc_pending:
            self._txq.append(bytes(h._cdc_pending))
            h._cdc_pending.clear()
        self._flush()

    def _read(self) -> None:
        if self._reading is not None or self.host.device is not self.info:
            return
        self._reading = self.host.bulk_in(self.ep_in.address & 0x0F, self.read_size, self._got, mps=self.ep_in.mps)

    def _got(self, x: Transfer) -> None:
        self._reading = None
        if x.status == "ok":
            if x.data:
                b = bytes(x.data)
                self.rx += b
                self.host.cdc_rx += b
                for cb in list(self.rx_listeners) + list(self.host.cdc_listeners):
                    cb(b)
            self._read()
        elif x.status == "stall":
            self.host.bus.event("error", f"CDC bulk IN ep 0x{self.ep_in.address:02X} stalled")

    def _poll_notify(self) -> None:
        if self.host.device is not self.info:
            return
        e = self.ep_notify
        self.host.interrupt_in(e.address & 0x0F, e.mps, e.interval, self._notified, mps=e.mps, log=False)

    def _notified(self, x: Transfer) -> None:
        if x.status != "ok":
            return
        if x.data:
            self.notifications.append(bytes(x.data))
            if len(x.data) >= 10 and x.data[1] == 0x20:           # SERIAL_STATE
                self.serial_state = struct.unpack_from("<H", x.data, 8)[0]
        self._poll_notify()

    def write(self, data: bytes | str) -> None:
        """Send bytes to the device (bulk OUT; queued, one transfer at a time)."""
        if isinstance(data, str):
            data = data.encode("latin-1")
        if data:
            self._txq.append(bytes(data))
            self._flush()

    def _flush(self) -> None:
        if self._writing is not None or not self._txq or not self.open or self.host.device is not self.info:
            return
        data = self._txq.popleft()
        self._writing = self.host.bulk_out(self.ep_out.address, data, self._wrote, mps=self.ep_out.mps)

    def _wrote(self, x: Transfer) -> None:
        self._writing = None
        if x.status == "ok":
            self.tx_bytes += len(x.out)
        self._flush()

    def disconnected(self) -> None:
        self.open = False
        self._reading = self._writing = None

    def take(self) -> bytes:
        b = bytes(self.rx)
        self.rx.clear()
        return b


class HidDevice:
    """HID host driver: GET_DESCRIPTOR(HID report), SET_IDLE(0), interrupt IN polling at bInterval -> `reports`."""
    name = "hid"

    def __init__(self, host: UsbHost, info: UsbDeviceInfo, intf: UsbInterfaceInfo):
        self.host, self.info, self.intf = host, info, intf
        self.ep_in = next((e for e in intf.endpoints if e.is_in and e.type == "interrupt"), None)
        self.report_desc = b""
        self.reports: list[tuple[float, bytes]] = []
        self.listeners: list[Callable[[bytes], None]] = []

    def start(self):
        h = self.host
        rlen = 0
        for d in self.intf.extra:
            if len(d) >= 9 and d[1] == 0x21:
                rlen = struct.unpack_from("<H", d, 7)[0]
        yield h._ctrl(self.info.address, 0x21, 0x0A, 0, self.intf.number, b"", mps=self.info.mps0)  # SET_IDLE
        if rlen:
            x = yield h._ctrl(self.info.address, 0x81, 6, 0x2200, self.intf.number, rlen, mps=self.info.mps0)
            if x.status == "ok":
                self.report_desc = bytes(x.data)
        self._poll()

    def _poll(self) -> None:
        e = self.ep_in
        if e is None or self.host.device is not self.info:
            return
        self.host.interrupt_in(e.address & 0x0F, e.mps, e.interval, self._got, mps=e.mps)

    def _got(self, x: Transfer) -> None:
        if x.status != "ok":
            return
        r = bytes(x.data)
        self.reports.append((self.host.now(), r))
        for cb in list(self.listeners):
            cb(r)
        self._poll()


class MscDevice:
    """Mass storage (bulk-only transport) host driver: GET_MAX_LUN, then INQUIRY and READ CAPACITY(10);
    `read_blocks(lba, n, cb)` issues READ(10)."""
    name = "msc"

    def __init__(self, host: UsbHost, info: UsbDeviceInfo, intf: UsbInterfaceInfo):
        self.host, self.info, self.intf = host, info, intf
        self.ep_in = next((e for e in intf.endpoints if e.is_in and e.type == "bulk"), None)
        self.ep_out = next((e for e in intf.endpoints if not e.is_in and e.type == "bulk"), None)
        self.max_lun = 0
        self.inquiry = b""
        self.vendor = self.product = ""
        self.blocks = 0
        self.block_size = 0
        self._tag = 0

    def start(self):
        h = self.host
        x = yield h._ctrl(self.info.address, 0xA1, 0xFE, 0, self.intf.number, 1, mps=self.info.mps0)
        if x.status == "ok" and x.data:
            self.max_lun = x.data[0]
        ok, data = yield from self.command(bytes([0x12, 0, 0, 0, 36, 0]), 36)
        if ok and len(data) >= 32:
            self.inquiry = data
            self.vendor = data[8:16].decode("ascii", "replace").strip()
            self.product = data[16:32].decode("ascii", "replace").strip()
        ok, data = yield from self.command(bytes([0x25] + [0] * 9), 8)
        if ok and len(data) >= 8:
            last, self.block_size = struct.unpack(">II", data[:8])
            self.blocks = last + 1

    def command(self, cdb: bytes, length: int, data_out: bytes = b""):
        """Bulk-only transport: CBW, data stage, CSW (a generator for host tasks): returns (ok, data)."""
        h = self.host
        self._tag += 1
        flags = 0x80 if length and not data_out else 0
        n = len(data_out) if data_out else length
        cbw = struct.pack("<IIIBBB", 0x43425355, self._tag, n, flags, 0, len(cdb)) + cdb.ljust(16, b"\0")
        x = yield Transfer("bulk", self.info.address, self.ep_out.address & 0x0F, data=cbw, mps=self.ep_out.mps)
        if x.status != "ok":
            return False, b""
        data = b""
        if data_out:
            x = yield Transfer("bulk", self.info.address, self.ep_out.address & 0x0F, data=data_out,
                               mps=self.ep_out.mps)
        elif length:
            x = yield Transfer("bulk", self.info.address, 0x80 | (self.ep_in.address & 0x0F), length=length,
                               mps=self.ep_in.mps)
            data = bytes(x.data)
        x = yield Transfer("bulk", self.info.address, 0x80 | (self.ep_in.address & 0x0F), length=13,
                           mps=self.ep_in.mps)
        if x.status != "ok" or len(x.data) < 13:
            return False, data
        sig, tag, _res, st = struct.unpack("<IIIB", bytes(x.data[:13]))
        return sig == 0x53425355 and tag == self._tag and st == 0, data

    def read_blocks(self, lba: int, count: int, cb: Callable[[bool, bytes], None]) -> None:
        def task():
            ok, data = yield from self.command(struct.pack(">BBIBHB", 0x28, 0, lba, 0, count, 0),
                                               count * (self.block_size or 512))
            cb(ok, data)
        self.host.start_task(task())


# ------------------------------------------------------------------------------------------------ board link
HOST_OPTIONS = {"debounce": "debounce_ns", "reset": "reset_ns", "reset_recovery": "reset_recovery_ns",
                "set_address_recovery": "set_address_recovery_ns", "resume": "resume_ns",
                "resume_recovery": "resume_recovery_ns", "ctrl_nak_retry": "ctrl_nak_retry_ns",
                "ctrl_timeout": "ctrl_timeout_ns"}
LINK_KEYS = set(HOST_OPTIONS) | {"second_reset", "enum_retries", "drivers", "power", "baud", "cdc_open", "cdc_send",
                                 "cdc_send_at", "cdc_pty", "cdc_tcp", "cdc_serial"}


def host_options(opts: dict | None, what: str = "usb link") -> dict:
    """Validate the `host:` mapping of a board `usb` link (times as '100ms' / '20us' / ms numbers)."""
    from .session import parse_time
    out: dict[str, Any] = {}
    for k, v in (opts or {}).items():
        if k not in LINK_KEYS:
            raise ValueError(f"{what}: unknown host option {k!r} (known: {', '.join(sorted(LINK_KEYS))})")
        if k in HOST_OPTIONS:
            out[k] = parse_time(v)
        elif k == "cdc_send_at":
            out[k] = parse_time(v)
        elif k in ("second_reset", "drivers", "power", "cdc_open"):
            out[k] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
        elif k in ("enum_retries", "baud"):
            out[k] = int(v)
        else:
            out[k] = v
    return out


class UsbLink:
    """A board `usb` link: one chip's USB device port plugged into a UsbHost (chipem/board.py `add_usb_link`).
    `cdc_send` text (escapes like \\r\\n allowed) is written to the CDC-ACM port once it is open, at `cdc_send_at`
    (virtual time) or as soon as the port opens; `cdc_pty` / `cdc_tcp` / `cdc_serial` attach the CDC byte stream to a
    host PC endpoint (chipem/hostserial.py, when available)."""

    def __init__(self, name: str, chip_id: str, port: str, block: Any, opts: dict | None = None):
        self.name = name
        self.chip_id = chip_id
        self.port = port
        self.block = block
        self.opts = dict(opts or {})
        self.bus = UsbBus(name)
        self.bus.attach(block)
        kw = {HOST_OPTIONS[k]: v for k, v in self.opts.items() if k in HOST_OPTIONS}
        for k in ("second_reset", "enum_retries", "drivers", "power", "cdc_open"):
            if k in self.opts:
                kw[k] = self.opts[k]
        if "baud" in self.opts:
            kw["line_coding"] = (int(self.opts["baud"]), 0, 0, 8)
        self.host = self.bus.add_host(**kw)
        self.endpoint = None
        self._sent = False
        self._ev = None
        self.bus.listeners.append(self._on_event)
        if self.opts.get("cdc_send") and self.opts.get("cdc_send_at") is not None and block.chip is not None:
            self._ev = block.chip.sched.at_ns(self.opts["cdc_send_at"], self._send_now)

    def _on_event(self, bus: UsbBus, kind: str, info: dict) -> None:
        if kind == "control" and str(info.get("req", "")).startswith("SET_CONTROL_LINE_STATE") \
                and self.opts.get("cdc_send") and self.opts.get("cdc_send_at") is None and not self._sent:
            self._send_now()

    def _send_now(self) -> None:
        self._ev = None
        if self._sent:
            return
        from .session import decode_escapes
        text = self.opts.get("cdc_send")
        self.host.cdc_write(decode_escapes(text) if isinstance(text, str) else bytes(text))
        self._sent = True

    @property
    def cdc_output(self) -> bytes:
        """Every byte the host's CDC-ACM driver received so far (all enumerations)."""
        return bytes(self.host.cdc_rx)

    def describe(self) -> str:
        return f"{self.name}: {self.chip_id}.{self.port} -> host"

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"type": "usb", "name": self.name, "device": f"{self.chip_id}.{self.port}"}
        if self.opts:
            h = {}
            for k, v in self.opts.items():
                if k in HOST_OPTIONS or k == "cdc_send_at":
                    h[k] = f"{v / 1e6:g}ms"
                else:
                    h[k] = v
            d["host"] = h
        return d

    def close(self) -> None:
        if self._ev is not None and self.block.chip is not None:
            self.block.chip.sched.cancel(self._ev)
        if self.endpoint is not None:
            close = getattr(self.endpoint, "close", None)
            if close:
                close()
        # stop the host's timers (SOFs, pending transactions) but keep its state for the run summary
        self.host._gen += 1
        self.host._cancel_timers()
        self.bus._cancel_suspend()


# ------------------------------------------------------------------------------------------------ host PC bridge
class UsbCdcBridge:
    """Binds a host PC port (chipem.hostserial HostPort: PTY, TCP, real serial port, memory) to the CDC-ACM function of
    an emulated USB device, so PC software opens the emulated chip's virtual COM port.  Emulation thread only (the
    Session pumps it between run slices like a UartBridge).
    PC -> device: bytes go out as bulk OUT transfers (`UsbHost.cdc_write`: held until the device is enumerated and the
    port is open), at USB speed (no baud pacing: a CDC-ACM link has no line rate of its own).  Device -> PC: each
    completed bulk IN transfer is written to the port.  The PC program's DTR/RTS (PTY/serial: modem lines) become
    SET_CONTROL_LINE_STATE and its line speed (PTY tcsetattr) SET_LINE_CODING, as the host CDC driver does; the device's
    SERIAL_STATE notifications drive DCD/DSR/RI towards the PC."""

    def __init__(self, host: UsbHost, port: Any, *, label: str, blk: Any = None):
        self.host = host
        self.port = port
        self.label = label
        self.blk = blk
        self.chip = getattr(blk, "chip", None)
        self.to_host = 0
        self.from_host = 0
        self.dropped = 0
        self._closed = False
        self._modem: tuple = ()
        self._baud: float | None = None
        self._state = -1
        host.cdc_listeners.append(self._to_pc)

    def _to_pc(self, data: bytes) -> None:
        if self._closed:
            return
        n = self.port.write(data)
        self.to_host += n
        self.dropped += len(data) - n

    def pump(self) -> None:
        if self._closed:
            return
        data = self.port.read()
        if data:
            self.from_host += len(data)
            self.host.cdc_write(data)
        cdc = self.host.cdc
        if cdc is None or not cdc.open:
            return
        m = self.port.modem_from_host()
        key = (m.get("dtr"), m.get("rts"))
        if key != self._modem and any(v is not None for v in key):
            self._modem = key
            v = (1 if key[0] else 0) | (2 if key[1] else 0)
            self.host.control(0x21, 0x22, v, cdc.comm.number, b"")
        ln = self.port.line()
        if ln is not None and ln.baud and ln.baud != self._baud:
            self._baud = ln.baud
            parity = {"N": 0, "O": 1, "E": 2, "M": 3, "S": 4}.get(getattr(ln, "parity", "N"), 0)
            stop = {1.0: 0, 1.5: 1, 2.0: 2}.get(float(getattr(ln, "stop", 1.0)), 0)
            self.host.control(0x21, 0x20, 0, cdc.comm.number,
                              struct.pack("<IBBB", int(ln.baud), stop, parity, int(getattr(ln, "bits", 8))))
        if cdc.serial_state != self._state:
            self._state = cdc.serial_state
            st = cdc.serial_state
            self.port.set_modem_to_host(dcd=bool(st & 1), dsr=bool(st & 2), ri=bool(st & 8))

    def pending(self) -> int:
        return len(self.host._cdc_pending)

    def info(self) -> dict:
        cdc = self.host.cdc
        return {"name": self.label, "kind": "usb-cdc", **self.port.info(), "to_host": self.to_host,
                "from_host": self.from_host, "dropped": self.dropped, "queued": len(self.host._cdc_pending),
                "uart_line": ("CDC-ACM open" if cdc is not None and cdc.open else "USB CDC-ACM not enumerated yet")}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._to_pc in self.host.cdc_listeners:
            self.host.cdc_listeners.remove(self._to_pc)
        self.port.close()
