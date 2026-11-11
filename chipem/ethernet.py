"""Generic Ethernet: frames, an MDIO-managed PHY (IEEE 802.3 clause 22), a shared segment and a scriptable host node,
plus pcap capture.  Vendor-free (docs/CONTRACTS.md "Ethernet"); the MAC models (stm32.eth) own a `EthPhy` and talk to
it through the small MAC protocol below.

Frames on the wire are `bytes` *including* the 4-byte FCS (CRC-32, `fcs()`), without preamble/SFD.  Wire time of a
frame at `rate` bit/s: (8 preamble/SFD bytes + max(len, 64)) x 8 bits (`wire_ns`); the inter-frame gap (96 bit times
by default) is the transmitter's business.

PHY (`EthPhy`, one per MAC, owned by the MAC model; `PHY_MODELS`: lan8720, lan8742, dp83848, ksz8081, generic):
* Clause 22 registers 0-6: BMCR (reset and restart-AN self-clearing, loopback, speed/duplex when AN is off, AN
  enable, power down, isolate), BMSR (abilities 10/100 HD/FD, AN ability, AN complete, link status latching low until
  read), PHYIDR1/2, ANAR (default 0x01E1), ANLPAR (the link partner's advertisement + ACK after AN), ANER.
  Vendor status registers that drivers poll: LAN87xx reg 17 MCSR (ENERGYON), 18 SMR (PHYAD), 29 ISR (latched, clear
  on read), 30 IMR, 31 PSCSR (AUTODONE, speed indication 001/101/010/110); DP83848 reg 0x10 PHYSTS (link, speed 10,
  duplex, loopback, AN complete), 0x11 MICR, 0x12 MISR (latched), 0x19 PHYCR (address); KSZ8081 reg 0x1E PHYCTRL1
  (operation mode 001/010/101/110, link) .  Other registers read 0 and ignore writes.
* MDIO: `mdio_read(phyad, reg)` / `mdio_write(phyad, reg, v)`; a PHY answers only its own address (`addr` param,
  0..31) - other addresses read 0xFFFF (the pulled-up MDIO line), as on a board with one PHY.
* Link: the PHY has a cable when it is on an `EthSegment`.  With BMCR.ANE the link comes up `aneg_ns` (default
  1.5 s, the typical 100BASE-TX auto-negotiation time) after power-on / reset / restart-AN / leaving power-down, at the
  best mode in ANAR & the segment's advertisement (100FD > 100HD > 10FD > 10HD); with ANE = 0 it comes up `link_ns`
  (default 10 ms) after the speed/duplex were forced.  No cable, power down or isolate: link down (BMSR link status
  latches low).  A software reset (BMCR.15) restores the defaults and reads 1 for `reset_ns` (default 1 ms).
* Data: `transmit(wire, rate, t_sof, t_end)` from the MAC: loopback (BMCR.14) returns the frame to the MAC; no link
  drops it ("nolink"); a MAC clocked at another speed than the link is garbage on the wire ("speed", one warn; the
  frame is lost); otherwise the segment carries it.  Received frames go to `mac.phy_rx(phy, wire, rate, t_end)`.
* MAC protocol (duck-typed): `chip`, `log`, `name`, `phy_rx(phy, wire, rate, t_end)`, optional
  `phy_link_changed(phy)` (link up/down, speed/duplex after AN).

Segment (`EthSegment`, hub semantics): every frame a port sends reaches every *other* linked port at the end of its
transmission (`t_end`; ports of chips that are behind get it scheduled at that time, ports ahead get it at once, so
cross-chip latency is at most one lockstep quantum).  Each port links at its own negotiated speed against the
segment's advertisement `caps` (default 100FD/100HD/10FD/10HD), i.e. the segment behaves like a dual-speed repeater:
no collisions, no store-and-forward delay.  One `eth` event per frame on the sender's log: `{link, node, dir: "tx",
len (bytes incl. FCS), dst, src, type ("0x0800"), info (ARP/IPv4/ICMP/UDP/TCP summary), sof_ns, msg}`.  Optional
pcap capture (`pcap` path; nanosecond pcap, LINKTYPE_ETHERNET, FCS stripped).  `listeners` get (segment, wire, info).

Host node (`EthHost`, `segment.add_host(...)`): a PC on the segment (MAC `mac`, IPv4 `ip`) that answers ARP requests
for its address and ICMP echo requests (ping) when enabled, can `ping(target, count, interval, start, size)` (ARP
first if the target's MAC is unknown) and `send(frame_hex_or_bytes, t_ns)` raw frames (FCS appended).  It transmits
at the segment's best speed with a 96-bit gap and keeps `received` [(t_ns, bytes without FCS)], `replies`
[(seq, rtt_ns)], `arp_cache`.  Scheduling uses the first chip port's scheduler (or `chip=`).
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Any, Callable

__all__ = ["fcs", "fcs_ok", "wire_ns", "inet_checksum", "mac_bytes", "mac_str", "ip_bytes", "ip_str", "describe",
           "arp_frame", "icmp_echo_frame", "ipv4_packet", "EthPhy", "EthSegment", "EthHost", "PcapWriter",
           "PHY_MODELS", "CAPS_ALL", "PREAMBLE_BYTES", "MIN_FRAME", "IFG_BITS", "parse_caps", "parse_mac", "EthLink",
           "eth_blocks", "find_eth", "phy_options", "BROADCAST"]

PREAMBLE_BYTES = 8                      # 7 preamble + SFD
MIN_FRAME = 64                          # incl. FCS
IFG_BITS = 96
BROADCAST = b"\xff" * 6
CAPS_ALL = ("100FD", "100HD", "10FD", "10HD")
_CAP_BITS = {"100FD": 1 << 8, "100HD": 1 << 7, "10FD": 1 << 6, "10HD": 1 << 5}


# ------------------------------------------------------------------------------------------------ frame helpers
def fcs(data: bytes) -> bytes:
    """Ethernet FCS (CRC-32, transmitted least significant byte first) of `data`."""
    return (zlib.crc32(bytes(data)) & 0xFFFFFFFF).to_bytes(4, "little")


def fcs_ok(wire: bytes) -> bool:
    return len(wire) >= 4 and fcs(wire[:-4]) == bytes(wire[-4:])


def wire_ns(nbytes: int, rate: float) -> float:
    """Duration of a frame of `nbytes` (incl. FCS) at `rate` bit/s, from the first preamble bit to the last FCS bit."""
    return (PREAMBLE_BYTES + max(int(nbytes), MIN_FRAME)) * 8 * 1e9 / float(rate)


def inet_checksum(data: bytes, start: int = 0) -> int:
    """RFC 1071 one's complement checksum of `data` (16-bit words, big endian), `start` = partial sum to add."""
    if len(data) % 2:
        data = bytes(data) + b"\0"
    s = start + sum(struct.unpack(f"!{len(data) // 2}H", data))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def parse_mac(v: Any) -> bytes:
    if isinstance(v, (bytes, bytearray)) and len(v) == 6:
        return bytes(v)
    s = str(v).strip().replace("-", ":")
    parts = s.split(":") if ":" in s else [s[i:i + 2] for i in range(0, len(s), 2)]
    b = bytes(int(p, 16) for p in parts)
    if len(b) != 6:
        raise ValueError(f"bad MAC address {v!r}")
    return b


mac_bytes = parse_mac


def mac_str(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in bytes(b)[:6])


def ip_bytes(v: Any) -> bytes:
    if isinstance(v, (bytes, bytearray)) and len(v) == 4:
        return bytes(v)
    b = bytes(int(x) for x in str(v).strip().split("."))
    if len(b) != 4:
        raise ValueError(f"bad IPv4 address {v!r}")
    return b


def ip_str(b: bytes) -> str:
    return ".".join(str(x) for x in bytes(b)[:4])


def ipv4_packet(src: bytes, dst: bytes, proto: int, payload: bytes, *, ident: int = 0, ttl: int = 64) -> bytes:
    hdr = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(payload), ident & 0xFFFF, 0x4000, ttl, proto, 0, src, dst)
    return hdr[:10] + struct.pack("!H", inet_checksum(hdr)) + hdr[12:] + payload


def arp_frame(op: int, sha: bytes, spa: bytes, tha: bytes, tpa: bytes, dst: bytes | None = None) -> bytes:
    """Ethernet + ARP (no FCS, not padded). op 1 = request (broadcast), 2 = reply."""
    body = struct.pack("!HHBBH6s4s6s4s", 1, 0x0800, 6, 4, op, sha, spa, tha, tpa)
    return (dst or (BROADCAST if op == 1 else tha)) + sha + b"\x08\x06" + body


def icmp_echo_frame(dst_mac: bytes, src_mac: bytes, src_ip: bytes, dst_ip: bytes, *, reply: bool, ident: int,
                    seq: int, data: bytes = b"", ip_id: int = 0) -> bytes:
    icmp = struct.pack("!BBHHH", 0 if reply else 8, 0, 0, ident & 0xFFFF, seq & 0xFFFF) + bytes(data)
    icmp = icmp[:2] + struct.pack("!H", inet_checksum(icmp)) + icmp[4:]
    return dst_mac + src_mac + b"\x08\x00" + ipv4_packet(src_ip, dst_ip, 1, icmp, ident=ip_id)


def describe(frame: bytes) -> dict:
    """{dst, src, type, info} of a frame (with or without FCS; summary only)."""
    f = bytes(frame)
    if len(f) < 14:
        return {"dst": "?", "src": "?", "type": "?", "info": f"runt ({len(f)} bytes)"}
    dst, src = mac_str(f[0:6]), mac_str(f[6:12])
    et = int.from_bytes(f[12:14], "big")
    off = 14
    vlan = ""
    if et == 0x8100 and len(f) >= 18:
        vlan = f"VLAN {int.from_bytes(f[14:16], 'big') & 0xFFF} "
        et = int.from_bytes(f[16:18], "big")
        off = 18
    info = f"{vlan}type 0x{et:04x}" if et >= 0x600 else f"{vlan}802.3 length {et}"
    p = f[off:]
    if et == 0x0806 and len(p) >= 28:
        op = int.from_bytes(p[6:8], "big")
        spa, tpa = ip_str(p[14:18]), ip_str(p[24:28])
        info = (f"{vlan}ARP who-has {tpa} tell {spa}" if op == 1 else
                f"{vlan}ARP {spa} is-at {mac_str(p[8:14])}" if op == 2 else f"{vlan}ARP op {op}")
    elif et == 0x0800 and len(p) >= 20:
        ihl = (p[0] & 0xF) * 4
        proto = p[9]
        s, d = ip_str(p[12:16]), ip_str(p[16:20])
        q = p[ihl:]
        if proto == 1 and len(q) >= 8:
            t = q[0]
            name = {0: "echo reply", 8: "echo request", 3: "unreachable", 11: "time exceeded"}.get(t, f"type {t}")
            info = f"{vlan}ICMP {s} > {d} {name}"
            if t in (0, 8):
                info += f" id {int.from_bytes(q[4:6], 'big')} seq {int.from_bytes(q[6:8], 'big')}"
        elif proto in (6, 17) and len(q) >= 4:
            sp, dp = int.from_bytes(q[0:2], "big"), int.from_bytes(q[2:4], "big")
            info = f"{vlan}{'TCP' if proto == 6 else 'UDP'} {s}:{sp} > {d}:{dp}"
        else:
            info = f"{vlan}IPv4 {s} > {d} proto {proto}"
    elif et == 0x86DD and len(p) >= 40:
        info = f"{vlan}IPv6 next header {p[6]}"
    elif et == 0x8808 and len(p) >= 4:
        op = int.from_bytes(p[0:2], "big")
        info = f"{vlan}MAC control " + (f"PAUSE {int.from_bytes(p[2:4], 'big')} quanta" if op == 1 else f"op {op}")
    return {"dst": dst, "src": src, "type": f"0x{et:04x}", "info": info}


def parse_caps(v: Any) -> tuple[str, ...]:
    if v is None or v == "":
        return CAPS_ALL
    items = v if isinstance(v, (list, tuple)) else str(v).replace(";", ",").split(",")
    out = []
    for x in items:
        k = str(x).strip().upper().replace("BASE", "").replace("-", "").replace("TX", "").replace("T", "")
        k = k.replace("FULL", "FD").replace("HALF", "HD")
        if k not in _CAP_BITS:
            raise ValueError(f"ethernet: unknown capability {x!r} (use 100FD, 100HD, 10FD, 10HD)")
        out.append(k)
    return tuple(out)


def _at(chip: Any, t: float, cb: Callable, *args: Any):
    """Run cb at virtual time t on `chip` (now if that chip is already there)."""
    if chip.now_ns() >= t:
        cb(*args)
        return None
    return chip.sched.at_ns(t, cb, *args)


def _parse_time(v: Any, default: float) -> float:
    if v is None or v == "":
        return default
    if isinstance(v, (int, float)):
        return float(v)
    from .session import parse_time
    return float(parse_time(str(v)))


# ------------------------------------------------------------------------------------------------ pcap
class PcapWriter:
    """Nanosecond-resolution pcap file (magic 0xA1B23C4D, LINKTYPE_ETHERNET), frames without FCS."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._f = open(self.path, "wb")
        self._f.write(struct.pack("<IHHiIII", 0xA1B23C4D, 2, 4, 0, 0, 65535, 1))
        self._f.flush()
        self.count = 0

    def write(self, t_ns: float, frame: bytes) -> None:
        if self._f is None:
            return
        t = int(round(t_ns))
        data = bytes(frame)
        self._f.write(struct.pack("<IIII", t // 1_000_000_000, t % 1_000_000_000, len(data), len(data)) + data)
        self._f.flush()
        self.count += 1

    def close(self) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None


# ------------------------------------------------------------------------------------------------ PHY
# model -> PHY identifier registers 2/3 and the vendor status register family
PHY_MODELS: dict[str, dict] = {
    "lan8720": {"id1": 0x0007, "id2": 0xC0F1, "vendor": "lan87"},     # Microchip/SMSC LAN8720A
    "lan8742": {"id1": 0x0007, "id2": 0xC131, "vendor": "lan87"},     # Microchip LAN8742A (Nucleo-144 boards)
    "dp83848": {"id1": 0x2000, "id2": 0x5C90, "vendor": "dp83848"},   # TI DP83848 (STM3240G-EVAL)
    "ksz8081": {"id1": 0x0022, "id2": 0x1561, "vendor": "ksz"},       # Microchip KSZ8081RNA
    "generic": {"id1": 0x0000, "id2": 0x0000, "vendor": None},
}
BMCR_RESET, BMCR_LOOP, BMCR_SPEED100, BMCR_ANE, BMCR_PD, BMCR_ISO, BMCR_RESTART, BMCR_FD = (
    1 << 15, 1 << 14, 1 << 13, 1 << 12, 1 << 11, 1 << 10, 1 << 9, 1 << 8)
BMSR_ABIL = 0x7809                      # 100TX FD/HD, 10 FD/HD, preamble suppression? no; extended capability
BMSR_ANC, BMSR_ANA, BMSR_LINK = 1 << 5, 1 << 3, 1 << 2


class EthPhy:
    def __init__(self, mac: Any, *, model: str = "lan8720", addr: int = 0, aneg: Any = None, link: Any = None,
                 reset: Any = None, id1: int | None = None, id2: int | None = None):
        self.mac = mac
        self.segment: "EthSegment | None" = None
        self.configure(model=model, addr=addr, aneg=aneg, link=link, reset=reset, id1=id1, id2=id2)
        self._ev = None
        self._reset_ev = None
        self._warned: set[str] = set()
        self.power_on()

    # ------------------------------------------------------------------ configuration
    def configure(self, *, model: str | None = None, addr: Any = None, aneg: Any = None, link: Any = None,
                  reset: Any = None, id1: Any = None, id2: Any = None) -> None:
        if model is not None:
            m = str(model).strip().lower().replace("-", "").replace("_", "")
            m = {"lan8720a": "lan8720", "lan8742a": "lan8742", "dp83848c": "dp83848", "dp83848i": "dp83848",
                 "ksz8081rna": "ksz8081"}.get(m, m)
            if m not in PHY_MODELS:
                raise ValueError(f"ethernet: unknown PHY model {model!r} (known: {', '.join(PHY_MODELS)})")
            self.model = m
            spec = PHY_MODELS[m]
            self.id1, self.id2, self.vendor = spec["id1"], spec["id2"], spec["vendor"]
        if addr is not None:
            a = int(addr)
            if not 0 <= a <= 31:
                raise ValueError("ethernet: PHY address must be 0..31")
            self.addr = a
        if id1 is not None:
            self.id1 = int(id1) & 0xFFFF
        if id2 is not None:
            self.id2 = int(id2) & 0xFFFF
        if aneg is not None or not hasattr(self, "aneg_ns"):
            self.aneg_ns = _parse_time(aneg, 1.5e9)
        if link is not None or not hasattr(self, "link_ns"):
            self.link_ns = _parse_time(link, 10e6)
        if reset is not None or not hasattr(self, "reset_ns"):
            self.reset_ns = _parse_time(reset, 1e6)

    @property
    def chip(self) -> Any:
        return getattr(self.mac, "chip", None)

    def _now(self) -> float:
        c = self.chip
        return c.now_ns() if c is not None else 0.0

    def _warn(self, key: str, msg: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        log = getattr(self.mac, "log", None)
        if log is not None:
            log.event("warn", self._now(), block=getattr(self.mac, "name", "ETH"), msg=msg)

    # ------------------------------------------------------------------ state
    def power_on(self) -> None:
        """Hardware reset (board power-up / Session reset): default registers, auto-negotiation starts."""
        self._cancel()
        self.bmcr = BMCR_ANE | BMCR_SPEED100 | BMCR_FD
        self.anar = 0x01E1
        self.anlpar = 0
        self.aner = 0
        self.vend: dict[int, int] = {30: 0, 17: 0, 0x11: 0}
        self.isr = 0                        # latched vendor interrupt flags (LAN87xx ISR / DP83848 MISR bits)
        self.link_up = False
        self.link_latched = False           # BMSR link status as read (latched low)
        self.an_done = False
        self.speed = 0                      # 10 / 100 Mbit/s while linked
        self.full_duplex = False
        self.resetting = False
        self._link_change()

    def _cancel(self) -> None:
        c = self.chip
        for ev in (getattr(self, "_ev", None), getattr(self, "_reset_ev", None)):
            if ev is not None and c is not None:
                c.sched.cancel(ev)
        self._ev = None
        self._reset_ev = None

    def detach_events(self) -> None:
        """Cancel scheduled link events (the MAC's chip was reset or the PHY moved)."""
        self._cancel()

    def _can_link(self) -> bool:
        return self.segment is not None and not self.bmcr & (BMCR_PD | BMCR_ISO) and not self.resetting

    def _link_change(self) -> None:
        """(Re)start link establishment after a configuration change."""
        c = self.chip
        if self._ev is not None and c is not None:
            c.sched.cancel(self._ev)
        self._ev = None
        was = self.link_up
        self.link_up = False
        self.an_done = False
        self.anlpar = 0
        self.aner = 0
        self.link_latched = False
        if was:
            self.isr |= 0x10                # LAN87xx ISR.4 link down / DP83848 MISR link change
            self._notify()
        if self._can_link() and c is not None:
            dt = self.aneg_ns if self.bmcr & BMCR_ANE else self.link_ns
            self._ev = c.sched.at_ns(c.now_ns() + dt, self._link_established)

    def _link_established(self) -> None:
        self._ev = None
        if not self._can_link():
            return
        seg_caps = self.segment.caps if self.segment is not None else CAPS_ALL
        if self.bmcr & BMCR_ANE:
            mine = {k for k, bit in _CAP_BITS.items() if self.anar & bit}
            common = [k for k in ("100FD", "100HD", "10FD", "10HD") if k in mine and k in seg_caps]
            if not common:
                self._warn("nocommon", f"{self.mac_name()}: PHY auto-negotiation found no common mode (ANAR "
                           f"0x{self.anar:04X}, link partner {','.join(seg_caps)}): no link")
                return
            best = common[0]
            self.anlpar = 0x4001 | sum(_CAP_BITS[k] for k in seg_caps)      # ACK + selector 802.3
            self.aner = 1
            self.an_done = True
            self.isr |= 0x40 | 0x02         # AN complete, page received
        else:
            best = ("100" if self.bmcr & BMCR_SPEED100 else "10") + ("FD" if self.bmcr & BMCR_FD else "HD")
            if best not in seg_caps:
                self._warn("forced", f"{self.mac_name()}: PHY forced to {best} but the segment does not support it: "
                           f"no link")
                return
        self.speed = 100 if best.startswith("100") else 10
        self.full_duplex = best.endswith("FD")
        self.link_up = True
        self.isr |= 0x80                    # ENERGYON
        self._notify()

    def _notify(self) -> None:
        cb = getattr(self.mac, "phy_link_changed", None)
        if cb is not None:
            cb(self)

    def mac_name(self) -> str:
        return getattr(self.mac, "name", "ETH")

    def set_segment(self, seg: "EthSegment | None") -> None:
        """Plug (seg) / unplug (None) the cable."""
        self.segment = seg
        self._link_change()

    def mode(self) -> str:
        return f"{self.speed}{'FD' if self.full_duplex else 'HD'}" if self.link_up else "down"

    # ------------------------------------------------------------------ MDIO
    def mdio_read(self, phyad: int, reg: int) -> int:
        if phyad != self.addr:
            return 0xFFFF
        reg &= 0x1F
        if reg == 0:
            return self.bmcr | (BMCR_RESET if self.resetting else 0)
        if reg == 1:
            v = BMSR_ABIL | BMSR_ANA
            if self.link_latched and self.link_up:
                v |= BMSR_LINK
            if self.an_done:
                v |= BMSR_ANC
            self.link_latched = self.link_up       # latching low: a read re-arms it to the current state
            return v
        if reg == 2:
            return self.id1
        if reg == 3:
            return self.id2
        if reg == 4:
            return self.anar
        if reg == 5:
            return self.anlpar
        if reg == 6:
            return self.aner
        return self._vendor_read(reg)

    def mdio_write(self, phyad: int, reg: int, v: int) -> None:
        if phyad != self.addr:
            return
        reg &= 0x1F
        v &= 0xFFFF
        if reg == 0:
            if self.resetting:
                return
            if v & BMCR_RESET:
                self._soft_reset()
                return
            old = self.bmcr
            self.bmcr = v & ~(BMCR_RESET | BMCR_RESTART)
            restart = bool(v & BMCR_RESTART) and bool(self.bmcr & BMCR_ANE)
            changed = (old ^ self.bmcr) & (BMCR_ANE | BMCR_PD | BMCR_ISO)
            if not self.bmcr & BMCR_ANE:
                changed |= (old ^ self.bmcr) & (BMCR_SPEED100 | BMCR_FD)
            if restart or changed:
                self._link_change()
            return
        if reg == 4:
            self.anar = v
            return
        self._vendor_write(reg, v)

    def _soft_reset(self) -> None:
        c = self.chip
        self.power_on()
        self.resetting = True
        self._link_change()
        if c is not None:
            self._reset_ev = c.sched.at_ns(c.now_ns() + self.reset_ns, self._reset_done)
        else:
            self._reset_done()

    def _reset_done(self) -> None:
        self._reset_ev = None
        self.resetting = False
        self._link_change()

    def _speed_bits(self) -> int:
        if not self.link_up:
            return 0
        return {(10, False): 1, (10, True): 5, (100, False): 2, (100, True): 6}[(self.speed, self.full_duplex)]

    def _vendor_read(self, reg: int) -> int:
        vd = self.vendor
        if vd == "lan87":
            if reg == 17:
                return (self.vend.get(17, 0) & ~0x2) | (0x2 if self.segment is not None else 0)
            if reg == 18:
                return 0x00E0 | self.addr
            if reg == 29:
                v, self.isr = self.isr & 0xFE, 0
                return v
            if reg == 30:
                return self.vend.get(30, 0)
            if reg == 31:
                return 0x0040 | (0x1000 if self.an_done else 0) | (self._speed_bits() << 2)
        elif vd == "dp83848":
            if reg == 0x10:
                v = 0
                if self.link_up:
                    v |= 1 | (2 if self.speed == 10 else 0) | (4 if self.full_duplex else 0)
                if self.bmcr & BMCR_LOOP:
                    v |= 8
                if self.an_done:
                    v |= 0x10
                return v
            if reg == 0x11:
                return self.vend.get(0x11, 0)
            if reg == 0x12:
                v = 0
                if self.isr & 0x10 or self.isr & 0x80:
                    v |= 1 << 13            # LINK_INT
                if self.isr & 0x40:
                    v |= 1 << 11            # ANC_INT
                self.isr = 0
                return v
            if reg == 0x19:
                return 0x8000 | self.addr
        elif vd == "ksz":
            if reg == 0x1E:
                op = {0: 0, 1: 1, 5: 5, 2: 2, 6: 6}[self._speed_bits()]
                return (0x100 if self.link_up else 0) | op
            if reg == 0x1B:
                v, self.isr = self.isr, 0
                return v & 0xFF
        return 0

    def _vendor_write(self, reg: int, v: int) -> None:
        if (self.vendor, reg) in (("lan87", 30), ("lan87", 17), ("dp83848", 0x11)):
            self.vend[reg] = v

    # ------------------------------------------------------------------ data
    def transmit(self, wire: bytes, rate: float, t_sof: float, t_end: float) -> str | None:
        """A frame from the MAC (`rate` = the MAC's own clock rate).  Returns None when it went onto the wire (or was
        looped back), else the reason it was lost: "nolink", "speed", "nocable"."""
        if self.bmcr & BMCR_LOOP:
            self.mac.phy_rx(self, bytes(wire), rate, t_end)
            return None
        if self.segment is None:
            return "nocable"
        if not self.link_up:
            return "nolink"
        if abs(rate - self.speed * 1e6) > 1:
            self._warn("speed", f"{self.mac_name()}: MAC clocked for {rate / 1e6:g} Mbit/s but the PHY link is "
                       f"{self.mode()}: frames are garbage on the wire (check MACCR.FES / DM after auto-negotiation)")
            return "speed"
        self.segment.send(self, bytes(wire), rate, t_sof, t_end)
        return None

    def deliver(self, wire: bytes, rate: float, t_end: float) -> None:
        """From the segment: a frame that ended at t_end."""
        if not self.link_up or self.bmcr & (BMCR_PD | BMCR_ISO | BMCR_LOOP):
            return
        if abs(rate - self.speed * 1e6) > 1:
            return                          # another speed: nothing usable on our RX pair
        self.mac.phy_rx(self, wire, rate, t_end)

    # segment port protocol
    @property
    def eth_name(self) -> str:
        return self.mac_name()

    @property
    def log(self) -> Any:
        return getattr(self.mac, "log", None)


# ------------------------------------------------------------------------------------------------ segment
class EthSegment:
    def __init__(self, name: str = "LAN", *, caps: Any = None, pcap: str | Path | None = None, chip: Any = None):
        self.name = name
        self.caps = parse_caps(caps)
        self.ports: list[Any] = []
        self.listeners: list[Callable[["EthSegment", bytes, dict], None]] = []
        self.frames = 0
        self.bytes = 0
        self.pcap: PcapWriter | None = PcapWriter(pcap) if pcap else None
        self._chip = chip
        self.hosts: list[EthHost] = []

    def __repr__(self) -> str:
        return f"<EthSegment {self.name} ports={len(self.ports)}>"

    @property
    def chip(self) -> Any:
        if self._chip is None:
            self._chip = next((p.chip for p in self.ports if getattr(p, "chip", None) is not None), None)
        return self._chip

    def attach(self, port: Any) -> None:
        if port in self.ports:
            return
        self.ports.append(port)
        if isinstance(port, EthPhy):
            port.set_segment(self)

    def detach(self, port: Any) -> None:
        if port in self.ports:
            self.ports.remove(port)
        if isinstance(port, EthPhy) and port.segment is self:
            port.set_segment(None)

    def set_pcap(self, path: str | Path | None) -> None:
        if self.pcap is not None:
            self.pcap.close()
        self.pcap = PcapWriter(path) if path else None

    def close(self) -> None:
        for p in list(self.ports):
            self.detach(p)
        if self.pcap is not None:
            self.pcap.close()
            self.pcap = None

    def add_host(self, name: str = "HOST", **params: Any) -> "EthHost":
        h = EthHost(self, name, **params)
        self.hosts.append(h)
        self.attach(h)
        return h

    def best_rate(self) -> float:
        return 100e6 if any(c.startswith("100") for c in self.caps) else 10e6

    def send(self, src: Any, wire: bytes, rate: float, t_sof: float, t_end: float) -> None:
        """A frame from port `src` on the wire from t_sof to t_end (called by the sender at t_end)."""
        self.frames += 1
        self.bytes += len(wire)
        d = describe(wire[:-4] if len(wire) >= 18 else wire)
        node = getattr(src, "eth_name", "?")
        info = {"link": self.name, "node": node, "dir": "tx", "len": len(wire), **d, "sof_ns": t_sof,
                "msg": f"{node} {d['src']} > {d['dst']} {d['info']} ({len(wire)} bytes)"}
        log = getattr(src, "log", None)
        if log is None:
            log = next((p.log for p in self.ports if getattr(p, "log", None) is not None), None)
        if log is not None:
            log.event("eth", t_end, **info)
        if self.pcap is not None:
            self.pcap.write(t_end, wire[:-4] if len(wire) >= 4 else wire)
        for cb in list(self.listeners):
            cb(self, wire, dict(info, t_ns=t_end))
        for p in list(self.ports):
            if p is src:
                continue
            chip = getattr(p, "chip", None)
            if chip is None:
                continue
            _at(chip, t_end, p.deliver, wire, rate, t_end)


# ------------------------------------------------------------------------------------------------ host node
class EthHost:
    def __init__(self, segment: EthSegment, name: str = "HOST", *, mac: Any = "02:00:00:00:00:01",
                 ip: Any = "192.168.0.1", arp: bool = True, echo: bool = True, chip: Any = None,
                 rate: float | None = None, **ping: Any):
        self.segment = segment
        self.name = name
        self.mac = parse_mac(mac)
        self.ip = ip_bytes(ip)
        self.arp_reply = bool(arp)
        self.echo_reply = bool(echo)
        self._chip = chip
        self.rate = float(rate) if rate else segment.best_rate()
        self.received: list[tuple[float, bytes]] = []
        self.sent: list[tuple[float, bytes]] = []
        self.replies: list[tuple[int, float]] = []          # (seq, rtt ns) of echo replies to our pings
        self.arp_cache: dict[bytes, bytes] = {}
        self._queue: list[bytes] = []
        self._busy_until = 0.0
        self._ev = None
        self._pings: dict[int, float] = {}
        self._ping_ident = 0x4348                           # "CH"
        self._ping_seq = 0
        self._ping_target: bytes | None = None
        self._ping_left = 0
        self._ping_interval = 1e6
        self._ping_size = 32
        self._ping_ev = None
        self.log = None
        if ping.get("ping"):
            self.ping(ping["ping"], count=int(ping.get("count", 4)), interval=ping.get("interval", "1ms"),
                      start=ping.get("start", 0), size=int(ping.get("size", 32)))
        unknown = set(ping) - {"ping", "count", "interval", "start", "size"}
        if unknown:
            raise ValueError(f"ethernet host {name}: unknown option(s) {', '.join(sorted(unknown))}")

    # port protocol
    @property
    def chip(self) -> Any:
        if self._chip is None:
            self._chip = next((getattr(p, "chip", None) for p in self.segment.ports
                               if p is not self and getattr(p, "chip", None) is not None), None)
        return self._chip

    @property
    def eth_name(self) -> str:
        return self.name

    def state(self) -> dict:
        return {"mac": mac_str(self.mac), "ip": ip_str(self.ip), "received": len(self.received),
                "sent": len(self.sent), "ping_replies": len(self.replies)}

    # ------------------------------------------------------------------ transmit
    def send(self, frame: Any, t_ns: float | None = None) -> None:
        """Queue a raw frame (bytes or hex string, without FCS; padded to 60 bytes) at t_ns (default now)."""
        if isinstance(frame, str):
            frame = bytes.fromhex(frame.replace(":", "").replace(" ", ""))
        frame = bytes(frame)
        chip = self.chip
        if chip is None:
            raise ValueError(f"ethernet host {self.name}: no chip on the segment to schedule on")
        now = chip.now_ns()
        if t_ns is not None and t_ns > now:
            chip.sched.at_ns(float(t_ns), self._enqueue, frame)
        else:
            self._enqueue(frame)

    def _enqueue(self, frame: bytes) -> None:
        self._queue.append(frame)
        self._kick()

    def _kick(self) -> None:
        if self._ev is not None or not self._queue:
            return
        chip = self.chip
        now = chip.now_ns()
        t0 = max(now, self._busy_until)
        if t0 > now:
            self._ev = chip.sched.at_ns(t0, self._start)
        else:
            self._start()

    def _start(self) -> None:
        self._ev = None
        chip = self.chip
        frame = self._queue.pop(0)
        if len(frame) < 60:
            frame = frame + bytes(60 - len(frame))
        wire = frame + fcs(frame)
        t0 = chip.now_ns()
        t_end = t0 + wire_ns(len(wire), self.rate)
        self._busy_until = t_end + IFG_BITS * 1e9 / self.rate
        self._ev = chip.sched.at_ns(t_end, self._done, wire, t0, t_end)

    def _done(self, wire: bytes, t0: float, t_end: float) -> None:
        self._ev = None
        self.sent.append((t_end, wire[:-4]))
        if self.log is None:
            self.log = getattr(self.chip, "log", None)
        self.segment.send(self, wire, self.rate, t0, t_end)
        self._kick()

    # ------------------------------------------------------------------ receive
    def deliver(self, wire: bytes, rate: float, t_end: float) -> None:
        if not fcs_ok(wire):
            return
        f = bytes(wire[:-4])
        self.received.append((t_end, f))
        if len(f) < 14:
            return
        dst = f[0:6]
        if dst != self.mac and dst != BROADCAST:
            return
        et = int.from_bytes(f[12:14], "big")
        p = f[14:]
        if et == 0x0806 and len(p) >= 28:
            op = int.from_bytes(p[6:8], "big")
            sha, spa, tpa = p[8:14], p[14:18], p[24:28]
            if spa != b"\0\0\0\0":
                self.arp_cache[spa] = sha
            if op == 1 and tpa == self.ip and self.arp_reply:
                self.send(arp_frame(2, self.mac, self.ip, sha, spa))
            if op == 2 and self._ping_target == spa and self._ping_left and not self._pings:
                if self._ping_ev is not None:           # resolved: the first echo request goes out at once
                    self.chip.sched.cancel(self._ping_ev)
                    self._ping_ev = None
                self._ping_tick()
        elif et == 0x0800 and len(p) >= 20 and p[0] >> 4 == 4:
            ihl = (p[0] & 0xF) * 4
            proto, src_ip, dst_ip = p[9], p[12:16], p[16:20]
            q = p[ihl:int.from_bytes(p[2:4], "big")]
            if dst_ip != self.ip or proto != 1 or len(q) < 8:
                return
            if q[0] == 8 and self.echo_reply:
                ident, seq = int.from_bytes(q[4:6], "big"), int.from_bytes(q[6:8], "big")
                self.send(icmp_echo_frame(f[6:12], self.mac, self.ip, src_ip, reply=True, ident=ident, seq=seq,
                                          data=q[8:]))
            elif q[0] == 0 and int.from_bytes(q[4:6], "big") == self._ping_ident:
                seq = int.from_bytes(q[6:8], "big")
                t0 = self._pings.pop(seq, None)
                if t0 is not None:
                    self.replies.append((seq, t_end - t0))

    # ------------------------------------------------------------------ ping
    def ping(self, target: Any, *, count: int = 4, interval: Any = "1ms", start: Any = 0, size: int = 32) -> None:
        """Send `count` ICMP echo requests to `target` every `interval` from `start` (ARP first if needed)."""
        self._ping_target = ip_bytes(target)
        self._ping_left = int(count)
        self._ping_interval = _parse_time(interval, 1e6)
        self._ping_size = int(size)
        chip = self.chip
        t = _parse_time(start, 0.0)
        if chip is None:
            self._ping_start = t                # scheduled once a chip joins (EthLink calls start_pending)
            return
        self._ping_ev = chip.sched.at_ns(max(t, chip.now_ns()), self._ping_tick)

    def start_pending(self) -> None:
        t = getattr(self, "_ping_start", None)
        if t is not None and self.chip is not None:
            del self._ping_start
            self._ping_ev = self.chip.sched.at_ns(max(t, self.chip.now_ns()), self._ping_tick)

    def _ping_tick(self) -> None:
        self._ping_ev = None
        if not self._ping_left or self._ping_target is None:
            return
        chip = self.chip
        mac = self.arp_cache.get(self._ping_target)
        if mac is None:
            self.send(arp_frame(1, self.mac, self.ip, b"\0" * 6, self._ping_target))
            self._ping_ev = chip.sched.at_ns(chip.now_ns() + self._ping_interval, self._ping_tick)
            return
        self._ping_seq += 1
        self._ping_left -= 1
        data = bytes((i & 0xFF) for i in range(self._ping_size))
        self._pings[self._ping_seq] = chip.now_ns()
        self.send(icmp_echo_frame(mac, self.mac, self.ip, self._ping_target, reply=False, ident=self._ping_ident,
                                  seq=self._ping_seq, data=data, ip_id=self._ping_seq))
        if self._ping_left:
            self._ping_ev = chip.sched.at_ns(chip.now_ns() + self._ping_interval, self._ping_tick)

    def cancel(self) -> None:
        chip = self.chip
        for ev in (self._ev, self._ping_ev):
            if ev is not None and chip is not None:
                chip.sched.cancel(ev)
        self._ev = self._ping_ev = None


# ------------------------------------------------------------------------------------------------ board link
def eth_blocks(chip: Any) -> dict[str, Any]:
    """Ethernet MACs of a chip: peripherals exposing the board hook `eth_attach(segment, **phy)`."""
    return {n: b for n, b in (getattr(chip, "peripherals", None) or {}).items()
            if hasattr(b, "eth_attach") and not getattr(b, "absent", False)}


def find_eth(chip: Any, name: str) -> str | None:
    """Resolve a user-typed MAC name: exact (case-insensitive) peripheral name, or ETH / ETHERNET / ETH0 / MAC for the
    chip's only Ethernet MAC."""
    macs = eth_blocks(chip)
    key = str(name).strip().upper()
    for n in macs:
        if n.upper() == key:
            return n
    if key in ("ETH", "ETH0", "ETH1", "ETHERNET", "ENET", "MAC", "EMAC") and len(macs) == 1:
        return next(iter(macs))
    return None


PHY_KEYS = ("model", "addr", "aneg", "link", "reset", "id1", "id2", "clock")
# `clock`: where the PHY's reference clock comes from: "phy" (its own crystal, default) or "mco" (an MCU clock output;
# only MACs that model it use it, e.g. stm32f1.eth: the STM32F107 MCO pin; others drop it)


def phy_options(d: Any, where: str) -> dict:
    """PHY options from a board YAML value: a model name string or a mapping (model, addr, aneg, link, reset, id1,
    id2; `phy` is accepted for `model`, `phy_addr` for `addr`)."""
    if d is None:
        return {}
    if isinstance(d, str):
        return {"model": d}
    if not isinstance(d, dict):
        raise ValueError(f"{where}: phy must be a model name or a mapping")
    out = {}
    for k, v in d.items():
        k2 = {"phy": "model", "phy_addr": "addr", "type": "model"}.get(k, k)
        if k2 not in PHY_KEYS:
            raise ValueError(f"{where}: unknown PHY option {k!r} (known: {', '.join(PHY_KEYS)})")
        out[k2] = v
    return out


class EthLink:
    """An Ethernet segment on a board: the listed MACs' PHYs plugged into one `EthSegment`, an optional host node,
    optional pcap capture (`board.yaml` `links: [{type: eth, ...}]`, docs/USER_GUIDE.md "Ethernet")."""

    def __init__(self, board: Any, name: str, nodes: list[tuple[str, str, dict]], *, host: dict | None = None,
                 pcap: str | None = None, caps: Any = None, inject: list | None = None):
        self.board = board
        self.name = name
        self.nodes = [(c, p) for c, p, _ in nodes]
        self.phy_opts = [dict(o) for _, _, o in nodes]
        self.host_opts = dict(host) if host else None
        self.pcap_path = pcap
        self.inject_spec = list(inject or [])
        self.segment = EthSegment(name, caps=caps, pcap=pcap, chip=getattr(board, "clock_chip", None))
        self.macs = []
        for (cid, pname), opts in zip(self.nodes, self.phy_opts):
            chip = board.chips[cid]
            n = find_eth(chip, pname)
            if n is None:
                raise ValueError(f"board: {cid}.{pname} is not an Ethernet MAC (Ethernet MACs of {cid}: "
                                 f"{', '.join(eth_blocks(chip)) or 'none'})")
            self.macs.append(chip.peripherals[n])
        self.nodes = [(c, m.name) for (c, _), m in zip(self.nodes, self.macs)]
        for m, opts in zip(self.macs, self.phy_opts):
            m.eth_attach(self.segment, **opts)
        self.host: EthHost | None = None
        if self.host_opts is not None:
            h = dict(self.host_opts)
            hname = str(h.pop("name", "HOST"))
            self.host = self.segment.add_host(hname, **h)
            self.host.start_pending()
        for i, item in enumerate(self.inject_spec):
            if not isinstance(item, dict) or "hex" not in item:
                raise ValueError(f"board: eth link {name}: inject[{i}] needs {{at: TIME, hex: FRAME}}")
            if self.host is None:
                self.host = self.segment.add_host("HOST")
            self.host.send(str(item["hex"]), _parse_time(item.get("at"), 0.0))

    def describe(self) -> str:
        return f"{self.name}: " + ", ".join(f"{c}.{p}" for c, p in self.nodes) + (
            f" + host {ip_str(self.host.ip)}" if self.host else "")

    def to_dict(self) -> dict:
        nodes: list[Any] = []
        for (c, p), o in zip(self.nodes, self.phy_opts):
            nodes.append({"node": f"{c}.{p}", "phy": dict(o)} if o else f"{c}.{p}")
        d: dict[str, Any] = {"type": "eth", "name": self.name, "nodes": nodes}
        if self.host_opts is not None:
            d["host"] = dict(self.host_opts)
        if self.pcap_path:
            d["pcap"] = str(self.pcap_path)
        if self.segment.caps != CAPS_ALL:
            d["caps"] = list(self.segment.caps)
        if self.inject_spec:
            d["inject"] = list(self.inject_spec)
        return d

    def close(self) -> None:
        """End of the session: stop the host node and the capture file (the PHYs stay plugged in, so the run summary
        still shows the link)."""
        if self.host is not None:
            self.host.cancel()
        if self.segment.pcap is not None:
            self.segment.pcap.close()
            self.segment.pcap = None

    def unplug(self) -> None:
        """Remove the MACs from the segment (their PHYs lose the link)."""
        self.close()
        for m in self.macs:
            m.eth_detach()
