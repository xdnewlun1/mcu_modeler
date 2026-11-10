"""STM32F407 Ethernet MAC (chipem/periph/stm32/eth.py) and the vendor-free Ethernet layer (chipem/ethernet.py):
frame helpers, the clause-22 PHY (MDIO registers, auto-negotiation timing, vendor status registers), MDIO timing
through MACMIIAR, DMABMR software reset, descriptor rings (TX/RX, ring and chained, normal and enhanced), address
filtering, checksum offload, flags/IRQ, the host node (ARP, ping), pcap output and the sample board."""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.ethernet import (EthHost, EthPhy, EthSegment, arp_frame, describe, fcs, fcs_ok, icmp_echo_frame,
                             inet_checksum, ipv4_packet, wire_ns)
from chipem.periph.stm32.eth import checksum_insert, checksum_verify, hash_index
from test_stm32 import rd, spin_chip, wr

ROOT = Path(__file__).resolve().parent.parent
CHIPS = ROOT / "chips"
BUILD = ROOT / "firmware" / "samples_stm32" / "build"
ETH = 0x40028000
MACCR, MACFFR, MACHTHR, MACHTLR, MACMIIAR, MACMIIDR, MACFCR = 0x0, 0x4, 0x8, 0xC, 0x10, 0x14, 0x18
MACPMTCSR, MACSR, MACIMR, MACA0HR, MACA0LR, MACA1HR, MACA1LR = 0x2C, 0x38, 0x3C, 0x40, 0x44, 0x48, 0x4C
MMCCR, MMCTGFCR, MMCRGUFCR, MMCRFCECR = 0x100, 0x168, 0x1C4, 0x194
DMABMR, DMATPDR, DMARPDR, DMARDLAR, DMATDLAR, DMASR, DMAOMR, DMAIER, DMAMFBOCR = (
    0x1000, 0x1004, 0x1008, 0x100C, 0x1010, 0x1014, 0x1018, 0x101C, 0x1020)
DMACHTDR, DMACHRDR = 0x1048, 0x104C
RCC, SYSCFG = 0x40023800, 0x40013800
GPIOA, GPIOB, GPIOC = 0x40020000, 0x40020400, 0x40020800
SRAM = 0x20000000
ETH_IRQ = 61
MAC = bytes.fromhex("020000000010")
HOST_MAC = bytes.fromhex("020000000001")
IP, HOST_IP = bytes([192, 168, 0, 10]), bytes([192, 168, 0, 1])
RE, TE, IPCO, DM, LM, FES, CSTF = 4, 8, 1 << 10, 1 << 11, 1 << 12, 1 << 14, 1 << 25
SR_RX, ST_TX, RSF, TSF = 2, 1 << 13, 1 << 25, 1 << 21
TS, TBUS, RS, RBUS, NIS, AIS = 1, 4, 64, 128, 1 << 16, 1 << 15
OWN, IC, LS, FS, TCH, TER = 1 << 31, 1 << 30, 1 << 29, 1 << 28, 1 << 20, 1 << 21
RCH, RER = 1 << 14, 1 << 15


@pytest.fixture(scope="module")
def d407():
    return load_chip(CHIPS / "stm32f407zg.yaml")


def _mux(chip, port, pins, af=11):
    moder, afr = rd(chip, port), [rd(chip, port + 0x20), rd(chip, port + 0x24)]
    for p in pins:
        moder = (moder & ~(3 << 2 * p)) | 2 << 2 * p
        r, s = p >> 3, 4 * (p & 7)
        afr[r] = (afr[r] & ~(0xF << s)) | af << s
    wr(chip, port + 0x20, afr[0])
    wr(chip, port + 0x24, afr[1])
    wr(chip, port, moder)


def eth_chip(desc, *, rmii=True, pins=True, clock_hz=50e6, phy="lan8742", aneg="100us", segment=True):
    chip = spin_chip(desc, clock_hz=clock_hz)
    wr(chip, RCC + 0x30, rd(chip, RCC + 0x30) | 0x7)                 # GPIOA-C
    wr(chip, RCC + 0x44, rd(chip, RCC + 0x44) | 1 << 14)             # SYSCFG
    if rmii:
        wr(chip, SYSCFG + 4, 1 << 23)                                 # PMC.MII_RMII_SEL before the MAC clock
    wr(chip, RCC + 0x30, rd(chip, RCC + 0x30) | 7 << 25)              # ETHMAC, TX, RX
    if pins:
        _mux(chip, GPIOA, (1, 2, 7))
        _mux(chip, GPIOB, (11, 12, 13))
        _mux(chip, GPIOC, (1, 4, 5))
    mac = chip.peripherals["Ethernet_MAC"]
    host = None
    if segment:
        seg = EthSegment("LAN")
        mac.eth_attach(seg, model=phy, addr=0, aneg=aneg)
        host = seg.add_host("HOST", mac=HOST_MAC, ip=HOST_IP)
    return chip, mac, host


def ew(chip, off, v):
    wr(chip, ETH + off, v)


def er(chip, off):
    return rd(chip, ETH + off)


def set_mac_addr(chip, mac=MAC):
    ew(chip, MACA0HR, mac[4] | mac[5] << 8)
    ew(chip, MACA0LR, int.from_bytes(mac[:4], "little"))


def mdio(chip, reg, value=None, pa=0, cr=0):
    ar = pa << 11 | reg << 6 | cr << 2 | 1
    if value is not None:
        ew(chip, MACMIIDR, value)
        ar |= 2
    ew(chip, MACMIIAR, ar)
    for _ in range(100):
        chip.run_cycles(200)
        if not er(chip, MACMIIAR) & 1:
            break
    return er(chip, MACMIIDR) & 0xFFFF


def wmem(chip, addr, data):
    chip.bus.write_bytes(addr, bytes(data))


def rmem(chip, addr, n):
    return bytes(chip.bus.read_bytes(addr, n))


def w32(chip, addr, v):
    chip.bus.write(addr, 4, v)


def r32(chip, addr):
    return chip.bus.read(addr, 4)


def tx_ring(chip, frames, base=SRAM + 0x1000, bufs=SRAM + 0x2000, chained=True, ic=True, extra0=0):
    """One descriptor per frame (FS|LS), returns the descriptor addresses."""
    descs = []
    n = len(frames)
    for i, f in enumerate(frames):
        d = base + 16 * i
        buf = bufs + 0x600 * i
        wmem(chip, buf, f)
        d0 = OWN | FS | LS | (IC if ic else 0) | extra0
        if chained:
            d0 |= TCH
            d3 = base + 16 * ((i + 1) % n)
        else:
            d3 = 0
            if i == n - 1:
                d0 |= TER
        w32(chip, d, d0)
        w32(chip, d + 4, len(f))
        w32(chip, d + 8, buf)
        w32(chip, d + 12, d3)
        descs.append(d)
    return descs


def rx_ring(chip, n=4, base=SRAM + 0x4000, bufs=SRAM + 0x5000, size=1536, chained=True, enhanced=False):
    step = 32 if enhanced else 16
    descs = []
    for i in range(n):
        d = base + step * i
        w32(chip, d, OWN)
        d1 = size
        if chained:
            d1 |= RCH
            w32(chip, d + 12, base + step * ((i + 1) % n))
        elif i == n - 1:
            d1 |= RER
        w32(chip, d + 4, d1)
        w32(chip, d + 8, bufs + 0x800 * i)
        descs.append(d)
    return descs


def start_mac(chip, *, rx=True, tx=True, cr=RE | TE | DM | FES, omr=RSF | TSF, ier=0, bmr=None):
    set_mac_addr(chip)
    if bmr is not None:
        ew(chip, DMABMR, bmr)
    ew(chip, MACCR, cr)
    ew(chip, DMAIER, ier)
    ew(chip, DMAOMR, omr | (ST_TX if tx else 0) | (SR_RX if rx else 0))


def wait_link(chip, mac, ns=1e6):
    chip.run_for(ns)
    assert mac.phy.link_up, mac.phy.mode()


def udp_frame(dst=MAC, src=HOST_MAC, sip=HOST_IP, dip=IP, payload=b"hello world", sport=1234, dport=7):
    udp = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload
    ph = sip + dip + struct.pack("!BBH", 0, 17, len(udp))
    c = inet_checksum(ph + udp) or 0xFFFF
    udp = udp[:6] + struct.pack("!H", c) + udp[8:]
    return dst + src + b"\x08\x00" + ipv4_packet(sip, dip, 17, udp)


# ================================================================================================ generic layer
def test_fcs_and_checksums():
    f = bytes(range(60))
    w = f + fcs(f)
    assert fcs_ok(w) and not fcs_ok(w[:-1] + bytes([w[-1] ^ 1]))
    assert int.from_bytes(fcs(b"123456789"), "little") == 0xCBF43926       # CRC-32 check value
    hdr = bytes.fromhex("450000730000400040110000c0a80001c0a800c7")       # RFC 1071 style example
    assert inet_checksum(hdr) == 0xB861
    assert wire_ns(64, 100e6) == pytest.approx(5760)                       # (8 + 64) x 8 bit x 10 ns
    assert wire_ns(30, 10e6) == pytest.approx(57600)                       # short frames take 64 bytes


def test_describe_arp_icmp():
    a = arp_frame(1, HOST_MAC, HOST_IP, b"\0" * 6, IP)
    d = describe(a)
    assert d["dst"] == "ff:ff:ff:ff:ff:ff" and d["type"] == "0x0806" and "who-has 192.168.0.10" in d["info"]
    e = icmp_echo_frame(MAC, HOST_MAC, HOST_IP, IP, reply=False, ident=1, seq=7, data=b"abc")
    assert "echo request" in describe(e)["info"] and "seq 7" in describe(e)["info"]
    assert checksum_verify(e)[2:] == (False, False, True)


def test_hash_index_matches_stmmac():
    # Linux stmmac (same Synopsys MAC): bitrev32(~crc32_le(~0, addr, 6)) >> 26
    for da in (bytes.fromhex("01005e000001"), bytes.fromhex("333300000001"), MAC):
        crc = zlib.crc32(da) & 0xFFFFFFFF
        assert hash_index(da) == int(f"{crc:032b}"[::-1], 2) >> 26


def test_checksum_insert_all_modes():
    f = bytearray(udp_frame())
    good = bytes(f)
    o = 14
    f[o + 10:o + 12] = b"\0\0"
    f[o + 20 + 6:o + 20 + 8] = b"\0\0"
    assert checksum_insert(f, 3) == 0 and bytes(f) == good
    f2 = bytearray(good)
    f2[o + 10:o + 12] = b"\xde\xad"
    assert checksum_insert(f2, 1) == 0
    assert inet_checksum(bytes(f2[o:o + 20])) == 0                         # header fixed, payload untouched
    ph = sum(struct.unpack("!HHHH", HOST_IP + IP)) + 17 + 19             # CIC 10: software puts the pseudo sum
    while ph >> 16:
        ph = (ph & 0xFFFF) + (ph >> 16)
    f3 = bytearray(good)
    f3[o + 26:o + 28] = ph.to_bytes(2, "big")
    assert checksum_insert(f3, 2) == 0 and bytes(f3) == good
    bad = bytearray(good)
    bad[o + 2:o + 4] = (2000).to_bytes(2, "big")                           # total length beyond the frame
    assert checksum_insert(bad, 3) & (1 << 12)                             # IPE


class _Mac:
    def __init__(self, chip):
        self.chip = chip
        self.log = chip.log
        self.name = "ETHX"
        self.rx = []
        self.events = []

    def phy_rx(self, phy, wire, rate, t):
        self.rx.append((t, wire))

    def phy_link_changed(self, phy):
        self.events.append((self.chip.now_ns(), phy.mode()))


def test_phy_registers_and_autonegotiation(d407):
    chip = spin_chip(d407, clock_hz=16e6)
    m = _Mac(chip)
    phy = EthPhy(m, model="lan8720", addr=1, aneg="2ms")
    assert phy.mdio_read(1, 2) == 0x0007 and phy.mdio_read(1, 3) == 0xC0F1
    assert phy.mdio_read(0, 2) == 0xFFFF                                   # other address: nobody answers
    assert phy.mdio_read(1, 1) & 0x4 == 0                                  # no cable
    seg = EthSegment("LAN")
    seg.attach(phy)
    chip.run_for(1.9e6)
    assert not phy.link_up
    chip.run_for(0.2e6)
    assert phy.link_up and phy.mode() == "100FD" and m.events[-1][1] == "100FD"
    assert m.events[-1][0] == pytest.approx(2e6, abs=2e3)
    assert phy.mdio_read(1, 1) & 0x4 == 0                                  # link status latched low once
    bmsr = phy.mdio_read(1, 1)
    assert bmsr & 0x24 == 0x24                                             # AN complete + link
    assert phy.mdio_read(1, 5) & 0x4000                                    # ANLPAR ACK
    assert phy.mdio_read(1, 31) >> 2 & 7 == 6 and phy.mdio_read(1, 31) & 0x1000   # PSCSR 100FD, autodone
    assert phy.mdio_read(1, 18) & 0x1F == 1
    phy.mdio_write(1, 4, 0x0061)                                           # advertise 10 only, restart AN
    phy.mdio_write(1, 0, 0x1200)
    assert phy.mdio_read(1, 1) & 0x4 == 0                                  # link dropped (latched low)
    chip.run_for(2.1e6)
    assert phy.mode() == "10FD"
    phy.mdio_write(1, 0, 0x0000)                                           # forced 10 HD, link_ns (10 ms)
    chip.run_for(5e6)
    assert not phy.link_up
    chip.run_for(6e6)
    assert phy.mode() == "10HD"
    phy.mdio_write(1, 0, 0x8000)                                           # soft reset reads 1 for 1 ms
    assert phy.mdio_read(1, 0) & 0x8000
    chip.run_for(1.1e6)
    assert not phy.mdio_read(1, 0) & 0x8000 and phy.mdio_read(1, 0) & 0x1000
    phy.mdio_write(1, 0, 0x0800)                                           # power down: link down
    assert not phy.link_up


def test_phy_dp83848_status(d407):
    chip = spin_chip(d407, clock_hz=16e6)
    phy = EthPhy(_Mac(chip), model="dp83848", addr=1, aneg="1ms")
    EthSegment(caps="10FD,10HD").attach(phy)
    chip.run_for(1.2e6)
    assert (phy.mdio_read(1, 2), phy.mdio_read(1, 3)) == (0x2000, 0x5C90)
    sts = phy.mdio_read(1, 0x10)
    assert sts & 1 and sts & 2 and sts & 4 and sts & 0x10                 # link, 10 Mb/s, full duplex, AN done
    assert phy.mdio_read(1, 0x12) & (1 << 13)                              # MISR link interrupt (latched)
    assert phy.mdio_read(1, 0x12) == 0


# ================================================================================================ MAC registers
def test_absent_on_f405():
    d = load_chip(CHIPS / "stm32f405zg.yaml")
    chip = spin_chip(d, clock_hz=16e6)
    mac = chip.peripherals["Ethernet_MAC"]
    assert mac.absent and rd(chip, ETH + DMABMR) == 0
    wr(chip, ETH + MACCR, 0x1234)
    assert rd(chip, ETH + MACCR) == 0x1234                                 # plain storage like the stub
    assert any("not present" in str(e.fields.get("msg")) for e in chip.log.filter("warn"))


def test_reset_values(d407):
    chip, mac, _ = eth_chip(d407, segment=False)
    assert er(chip, MACCR) == 0x8000 and er(chip, DMABMR) == 0x2101
    assert er(chip, MACA0HR) == 0x8000FFFF and er(chip, MACA0LR) == 0xFFFFFFFF
    assert mac.eth_stats()["iface"] == "rmii"


def test_mdio_timing_and_phy_id(d407):
    chip, mac, _ = eth_chip(d407)
    ew(chip, MACMIIAR, 2 << 11 | 2 << 6 | 3 << 2 | 1)                     # PA=2: no PHY there; CR=3 /26
    t0 = chip.now_cycles()
    ev = []
    for t in (t0 + 64 * 26 - 2, t0 + 64 * 26 + 2):
        chip.sched.at_cycles(t, lambda: ev.append(er(chip, MACMIIAR) & 1))
    chip.run_cycles(64 * 26 + 10)
    assert ev == [1, 0]
    assert er(chip, MACMIIDR) == 0xFFFF
    assert mdio(chip, 2) == 0x0007 and mdio(chip, 3) == 0xC131             # LAN8742A
    mdio(chip, 4, 0x01E1)
    assert mdio(chip, 4) == 0x01E1


def test_mdio_without_pins_reads_ffff(d407):
    chip, mac, _ = eth_chip(d407, pins=False)
    assert mdio(chip, 2) == 0xFFFF
    assert any("MDIO" in str(e.fields.get("msg")) for e in chip.log.filter("warn"))


def test_mdio_busy_write_ignored(d407):
    chip, mac, _ = eth_chip(d407)
    ew(chip, MACMIIAR, 2 << 6 | 1)
    ew(chip, MACMIIAR, 3 << 6 | 1)                                         # ignored while busy
    chip.run_cycles(64 * 42 + 10)
    assert er(chip, MACMIIAR) >> 6 & 0x1F == 2 and er(chip, MACMIIDR) == 0x0007


def test_software_reset_needs_ref_clock(d407):
    chip, mac, _ = eth_chip(d407, pins=False)
    ew(chip, MACCR, 0x1234 << 2)
    ew(chip, DMABMR, er(chip, DMABMR) | 1)
    chip.run_cycles(1000)
    assert er(chip, DMABMR) & 1                                            # stuck: no REF_CLK
    assert er(chip, MACCR) == 0x8000                                       # registers were reset
    assert any("DMABMR.SR" in str(e.fields.get("msg")) for e in chip.log.filter("warn"))
    _mux(chip, GPIOA, (1,))                                                # REF_CLK appears
    er(chip, DMABMR)
    chip.run_cycles(100)
    assert not er(chip, DMABMR) & 1


def test_software_reset_completes(d407):
    chip, mac, _ = eth_chip(d407)
    ew(chip, DMABMR, er(chip, DMABMR) | 1)
    assert er(chip, DMABMR) & 1
    chip.run_cycles(100)
    assert er(chip, DMABMR) == 0x2100


def test_pmc_change_after_clock_warns(d407):
    chip, mac, _ = eth_chip(d407, rmii=False, pins=False, segment=False)
    assert mac.eth_stats()["iface"] == "mii"
    wr(chip, SYSCFG + 4, 1 << 23)
    assert mac._iface_now() == "mii"                                       # not sampled without a MAC reset
    assert any("MII_RMII_SEL" in str(e.fields.get("msg")) for e in chip.log.filter("warn"))
    wr(chip, RCC + 0x10, 1 << 25)                                          # ETHMACRST
    wr(chip, RCC + 0x10, 0)
    assert mac._iface == "rmii"
    wr(chip, SYSCFG + 4, 0)
    ew(chip, DMABMR, 1)                                                    # HAL order: PMC, then software reset
    assert mac._iface == "mii"


# ================================================================================================ transmit
def test_transmit_frame_timing_status_and_irq(d407):
    chip, mac, host = eth_chip(d407)
    wait_link(chip, mac)
    f = udp_frame(dst=HOST_MAC, src=MAC, sip=IP, dip=HOST_IP)
    d = tx_ring(chip, [f])
    ew(chip, DMATDLAR, d[0])
    t0 = chip.now_ns()
    start_mac(chip, rx=False, ier=1 | NIS)
    assert r32(chip, d[0]) & OWN
    dur = wire_ns(len(f) + 4, 100e6)
    chip.run_for(dur - 200)
    assert r32(chip, d[0]) & OWN and not er(chip, DMASR) & TS
    chip.run_for(400)
    assert not r32(chip, d[0]) & OWN and not r32(chip, d[0]) & (1 << 15)
    sr = er(chip, DMASR)
    assert sr & TS and sr & NIS and (16 + ETH_IRQ) in chip.arch.asserted
    chip.run_for(2000)
    assert er(chip, DMASR) >> 20 & 7 == 6                                  # TPS suspended (next desc host-owned)
    assert host.received[0][1][:len(f)] == f and host.received[0][0] == pytest.approx(t0 + dur, abs=50)
    assert er(chip, MMCTGFCR) == 1
    ev = chip.log.filter("eth")
    assert ev and ev[0].fields["dst"] == "02:00:00:00:00:01" and ev[0].fields["len"] == 64
    ew(chip, DMASR, TS | NIS)
    assert (16 + ETH_IRQ) not in chip.arch.asserted


def test_transmit_back_to_back_ring_mode_ifg(d407):
    chip, mac, host = eth_chip(d407)
    wait_link(chip, mac)
    frames = [bytes(HOST_MAC + MAC + b"\x88\xb5" + bytes([i]) * 30) for i in range(3)]
    d = tx_ring(chip, frames, chained=False)
    ew(chip, DMATDLAR, d[0])
    start_mac(chip, rx=False)
    chip.run_for(30_000)
    got = [f for _t, f in host.received]
    assert [g[14] for g in got] == [0, 1, 2]
    assert all(len(g) == 60 for g in got)                                  # padded to 60 bytes (+FCS)
    ts = [t for t, _ in host.received]
    per = wire_ns(64, 100e6) + 960                                         # frame + 96-bit gap
    assert ts[1] - ts[0] == pytest.approx(per, abs=30) and ts[2] - ts[1] == pytest.approx(per, abs=30)
    assert er(chip, DMACHTDR) == d[2]


def test_transmit_suspend_and_poll_demand(d407):
    chip, mac, host = eth_chip(d407)
    wait_link(chip, mac)
    base = SRAM + 0x1000
    for i in range(2):
        w32(chip, base + 16 * i, TCH)
        w32(chip, base + 16 * i + 12, base + 16 * ((i + 1) % 2))
    ew(chip, DMATDLAR, base)
    start_mac(chip, rx=False)
    chip.run_for(1000)
    assert er(chip, DMASR) & TBUS and er(chip, DMASR) >> 20 & 7 == 6
    f = HOST_MAC + MAC + b"\x08\x00" + bytes(46)
    wmem(chip, SRAM + 0x2000, f)
    w32(chip, base + 4, len(f))
    w32(chip, base + 8, SRAM + 0x2000)
    w32(chip, base, OWN | FS | LS | TCH)
    chip.run_for(10_000)
    assert not host.received                                               # still suspended
    ew(chip, DMATPDR, 0)
    chip.run_for(10_000)
    assert len(host.received) == 1


def test_transmit_checksum_insertion(d407):
    chip, mac, host = eth_chip(d407)
    wait_link(chip, mac)
    f = bytearray(udp_frame(dst=HOST_MAC, src=MAC, sip=IP, dip=HOST_IP))
    good = bytes(f)
    f[14 + 10:14 + 12] = b"\0\0"
    f[14 + 26:14 + 28] = b"\0\0"
    d = tx_ring(chip, [bytes(f)], extra0=3 << 22)                          # CIC = 11
    ew(chip, DMATDLAR, d[0])
    start_mac(chip, rx=False)
    chip.run_for(20_000)
    assert host.received[0][1][:len(good)] == good


def test_transmit_split_over_descriptors(d407):
    chip, mac, host = eth_chip(d407)
    wait_link(chip, mac)
    f = HOST_MAC + MAC + b"\x08\x00" + bytes(range(100))
    base, buf = SRAM + 0x1000, SRAM + 0x2000
    parts = [f[:20], f[20:70], f[70:]]
    for i, p in enumerate(parts):
        wmem(chip, buf + 0x100 * i, p)
        d0 = OWN | TCH | (FS if i == 0 else 0) | (LS | IC if i == 2 else 0)
        w32(chip, base + 16 * i, d0)
        w32(chip, base + 16 * i + 4, len(p))
        w32(chip, base + 16 * i + 8, buf + 0x100 * i)
        w32(chip, base + 16 * i + 12, base + 16 * (i + 1))
    w32(chip, base + 48, TCH)
    w32(chip, base + 60, base)
    ew(chip, DMATDLAR, base)
    start_mac(chip, rx=False)
    chip.run_for(20_000)
    assert host.received[0][1] == f
    assert all(not r32(chip, base + 16 * i) & OWN for i in range(3))


def test_transmit_link_down_half_duplex_sets_carrier_errors(d407):
    chip, mac, _ = eth_chip(d407, segment=False)
    f = HOST_MAC + MAC + b"\x08\x00" + bytes(46)
    d = tx_ring(chip, [f])
    ew(chip, DMATDLAR, d[0])
    start_mac(chip, rx=False, cr=RE | TE | FES)                            # half duplex
    chip.run_for(20_000)
    d0 = r32(chip, d[0])
    assert not d0 & OWN and d0 & (1 << 15) and d0 & (1 << 10) and d0 & (1 << 11)   # ES, NC, LCA


def test_speed_mismatch_loses_frames(d407):
    chip, mac, host = eth_chip(d407)
    wait_link(chip, mac)
    d = tx_ring(chip, [HOST_MAC + MAC + b"\x08\x00" + bytes(46)])
    ew(chip, DMATDLAR, d[0])
    start_mac(chip, rx=False, cr=RE | TE | DM)                             # FES = 0: 10 Mbit/s MAC, 100 link
    chip.run_for(200_000)
    assert not host.received and any("Mbit/s" in str(e.fields.get("msg")) for e in chip.log.filter("warn"))


def test_mac_loopback(d407):
    chip, mac, host = eth_chip(d407, segment=False)
    rx = rx_ring(chip)
    ew(chip, DMARDLAR, rx[0])
    f = MAC + HOST_MAC + b"\x08\x00" + bytes(range(46))
    d = tx_ring(chip, [f])
    ew(chip, DMATDLAR, d[0])
    start_mac(chip, cr=RE | TE | DM | FES | LM)
    chip.run_for(20_000)
    d0 = r32(chip, rx[0])
    assert not d0 & OWN and d0 >> 16 & 0x3FFF == 64
    assert rmem(chip, SRAM + 0x5000, 60) == f


# ================================================================================================ receive
def _rx_setup(d407, **kw):
    chip, mac, host = eth_chip(d407)
    wait_link(chip, mac)
    rx = rx_ring(chip, **kw)
    ew(chip, DMARDLAR, rx[0])
    return chip, mac, host, rx


def test_receive_frame_status_and_irq(d407):
    chip, mac, host, rx = _rx_setup(d407)
    start_mac(chip, tx=False, ier=RS | NIS)
    assert er(chip, DMASR) >> 17 & 7 == 3                                  # RPS waiting for frames
    f = udp_frame()
    host.send(f)
    chip.run_for(wire_ns(len(f) + 4, 100e6) + 100)
    d0 = r32(chip, rx[0])
    assert not d0 & OWN and d0 & FS >> 19 and d0 & (1 << 8) and d0 & (1 << 9)
    assert d0 >> 16 & 0x3FFF == 64                                         # FL incl. CRC (CSTF = 0), padded
    assert d0 & (1 << 5)                                                   # FT: type frame
    data = rmem(chip, SRAM + 0x5000, 64)
    assert data[:len(f)] == f and fcs_ok(data)
    assert er(chip, DMASR) & RS and (16 + ETH_IRQ) in chip.arch.asserted
    assert er(chip, DMACHRDR) == rx[0] and er(chip, MMCRGUFCR) == 1


def test_receive_crc_strip_and_filtering(d407):
    chip, mac, host, rx = _rx_setup(d407)
    start_mac(chip, tx=False, cr=RE | TE | DM | FES | CSTF)
    other = bytes.fromhex("02aabbccddee")
    host.send(udp_frame(dst=other))                                        # not for us: filtered
    host.send(b"\xff" * 6 + HOST_MAC + b"\x08\x06" + bytes(46))            # broadcast: accepted
    chip.run_for(30_000)
    d0 = r32(chip, rx[0])
    assert not d0 & OWN and d0 >> 16 & 0x3FFF == 60 and r32(chip, rx[1]) & OWN
    assert mac.stats["filtered"] == 1
    ew(chip, MACFFR, 1 << 5)                                               # BFD: broadcast dropped
    host.send(b"\xff" * 6 + HOST_MAC + b"\x08\x06" + bytes(46))
    chip.run_for(10_000)
    assert r32(chip, rx[1]) & OWN
    ew(chip, MACFFR, 1)                                                    # promiscuous
    host.send(udp_frame(dst=other))
    chip.run_for(10_000)
    assert not r32(chip, rx[1]) & OWN


def test_receive_hash_and_perfect_filters(d407):
    chip, mac, host, rx = _rx_setup(d407, n=8)
    start_mac(chip, tx=False)
    mc = bytes.fromhex("01005e000001")
    host.send(mc + HOST_MAC + b"\x08\x00" + bytes(46))                     # multicast, no filter: dropped
    chip.run_for(10_000)
    assert r32(chip, rx[0]) & OWN
    h = hash_index(mc)
    ew(chip, MACHTHR if h >= 32 else MACHTLR, 1 << (h & 31))
    ew(chip, MACFFR, 4)                                                    # HM
    host.send(mc + HOST_MAC + b"\x08\x00" + bytes(46))
    chip.run_for(10_000)
    assert not r32(chip, rx[0]) & OWN
    alt = bytes.fromhex("020000000099")                                    # perfect filter MACA1
    ew(chip, MACA1HR, 1 << 31 | alt[4] | alt[5] << 8)
    ew(chip, MACA1LR, int.from_bytes(alt[:4], "little"))
    host.send(alt + HOST_MAC + b"\x08\x00" + bytes(46))
    chip.run_for(10_000)
    assert not r32(chip, rx[1]) & OWN
    ew(chip, MACFFR, 1 << 31)                                              # RA: everything, AFM flagged
    host.send(bytes.fromhex("020000000077") + HOST_MAC + b"\x08\x00" + bytes(46))
    chip.run_for(10_000)
    assert r32(chip, rx[2]) & (1 << 30)


def test_receive_no_descriptor_rbus_flush_and_poll(d407):
    chip, mac, host = eth_chip(d407)
    wait_link(chip, mac)
    base = SRAM + 0x4000
    w32(chip, base, 0)                                                     # host-owned
    w32(chip, base + 4, RCH | 1536)
    w32(chip, base + 8, SRAM + 0x5000)
    w32(chip, base + 12, base)
    ew(chip, DMARDLAR, base)
    start_mac(chip, tx=False, ier=RBUS | AIS)
    sr = er(chip, DMASR)
    assert sr & RBUS and sr >> 17 & 7 == 4 and sr & AIS
    ew(chip, DMASR, RBUS | AIS)
    host.send(udp_frame())
    chip.run_for(10_000)
    assert er(chip, DMAMFBOCR) & 0xFFFF == 1 and er(chip, DMAMFBOCR) == 0  # missed, cleared on read
    assert not er(chip, DMASR) & RBUS                                      # only when the previous was DMA-owned
    w32(chip, base, OWN)
    ew(chip, DMARPDR, 0)
    assert er(chip, DMASR) >> 17 & 7 == 3
    host.send(udp_frame())
    chip.run_for(10_000)
    assert not r32(chip, base) & OWN


def test_receive_spans_descriptors_cumulative_length(d407):
    chip, mac, host, rx = _rx_setup(d407, size=64)
    start_mac(chip, tx=False, cr=RE | TE | DM | FES | CSTF)
    f = MAC + HOST_MAC + b"\x08\x00" + bytes(range(150))
    host.send(f)
    chip.run_for(20_000)
    d = [r32(chip, a) for a in rx[:3]]
    assert d[0] & (1 << 9) and not d[0] & (1 << 8) and d[0] >> 16 & 0x3FFF == 64
    assert d[1] >> 16 & 0x3FFF == 128 and d[2] & (1 << 8) and d[2] >> 16 & 0x3FFF == len(f)
    got = rmem(chip, SRAM + 0x5000, 64) + rmem(chip, SRAM + 0x5800, 64) + rmem(chip, SRAM + 0x6000, len(f) - 128)
    assert got == f


def test_receive_checksum_offload_enhanced_descriptors(d407):
    chip, mac, host, rx = _rx_setup(d407, enhanced=True)
    start_mac(chip, tx=False, cr=RE | TE | DM | FES | CSTF | IPCO, bmr=0x2100 | 1 << 7, omr=RSF)
    host.send(udp_frame())
    chip.run_for(10_000)
    d0 = r32(chip, rx[0])
    assert not d0 & OWN and d0 & 1                                         # ESA
    ext = r32(chip, rx[0] + 16)
    assert ext & 7 == 1 and ext & (1 << 6) and not ext & 0x18             # UDP, IPv4, no errors
    bad = bytearray(udp_frame())
    bad[-1] ^= 0xFF                                                        # payload checksum error: dropped
    host.send(bytes(bad))
    chip.run_for(10_000)
    assert r32(chip, rx[1]) & OWN and mac.stats["lost"] == 1
    ew(chip, DMAOMR, er(chip, DMAOMR) | 1 << 26)                          # DTCEFD: keep it, flag IPPE
    host.send(bytes(bad))
    chip.run_for(10_000)
    assert not r32(chip, rx[1]) & OWN and r32(chip, rx[1] + 16) & (1 << 4)


def test_receive_checksum_status_normal_descriptors(d407):
    chip, mac, host, rx = _rx_setup(d407)
    start_mac(chip, tx=False, cr=RE | TE | DM | FES | CSTF | IPCO, omr=RSF | 1 << 26)
    host.send(udp_frame())
    host.send(MAC + HOST_MAC + b"\x88\xb5" + bytes(46))                    # not IP
    chip.run_for(20_000)
    d0, d1 = r32(chip, rx[0]), r32(chip, rx[1])
    assert (d0 >> 5 & 1, d0 >> 7 & 1, d0 & 1) == (1, 0, 0)
    assert (d1 >> 5 & 1, d1 >> 7 & 1, d1 & 1) == (0, 1, 1)


def test_crc_error_dropped_and_counted(d407):
    chip, mac, host, rx = _rx_setup(d407)
    start_mac(chip, tx=False)
    f = udp_frame()
    w = f + fcs(f)
    w = w[:-1] + bytes([w[-1] ^ 0x55])
    host.segment.send(host, w, 100e6, chip.now_ns(), chip.now_ns())
    chip.run_for(1000)
    assert r32(chip, rx[0]) & OWN and er(chip, MMCRFCECR) == 1


def test_magic_packet_wakeup(d407):
    chip, mac, host, rx = _rx_setup(d407)
    start_mac(chip, tx=False)
    ew(chip, MACPMTCSR, 3)                                                 # PD + MPE
    host.send(MAC + HOST_MAC + b"\x08\x00" + bytes(46))
    chip.run_for(10_000)
    assert r32(chip, rx[0]) & OWN                                          # dropped in power down
    host.send(b"\xff" * 6 + HOST_MAC + b"\x08\x42" + b"\xff" * 6 + MAC * 16)
    chip.run_for(20_000)
    assert er(chip, DMASR) & (1 << 28) and (16 + ETH_IRQ) in chip.arch.asserted
    v = er(chip, MACPMTCSR)
    assert v & (1 << 5) and not v & 1
    assert not er(chip, MACPMTCSR) & (1 << 5) and (16 + ETH_IRQ) not in chip.arch.asserted


def test_pause_frame_delays_transmitter(d407):
    chip, mac, host, rx = _rx_setup(d407)
    start_mac(chip, tx=False)
    ew(chip, MACFCR, 4)                                                    # RFCE
    pause = bytes.fromhex("0180c2000001") + HOST_MAC + b"\x88\x08" + struct.pack("!HH", 1, 100)
    host.send(pause)
    chip.run_for(wire_ns(64, 100e6) + 50)
    f = HOST_MAC + MAC + b"\x08\x00" + bytes(46)
    d = tx_ring(chip, [f])
    ew(chip, DMATDLAR, d[0])
    ew(chip, DMAOMR, er(chip, DMAOMR) | ST_TX)
    t0 = chip.now_ns()
    chip.run_for(100 * 512 * 10 - 1000)
    assert not any(fr[:6] == HOST_MAC for _t, fr in host.received)
    chip.run_for(10_000)
    t = [t for t, fr in host.received if fr[:6] == HOST_MAC]
    assert t and t[0] >= t0 + 100 * 512 * 10 - 5200
    assert r32(chip, rx[0]) & OWN                                          # control frame not passed (PCF 0)


def test_mmc_counter_interrupt_and_reset(d407):
    chip, mac, host = eth_chip(d407, segment=False)
    ew(chip, MMCCR, 1 << 4 | 1 << 5)                                       # preset almost full
    assert er(chip, MMCTGFCR) == 0xFFFFFFF0
    ew(chip, MMCCR, 1)
    assert er(chip, MMCTGFCR) == 0


# ================================================================================================ host node
def test_host_answers_arp_and_ping(d407):
    chip, mac, host, rx = _rx_setup(d407)
    req = arp_frame(1, MAC, IP, b"\0" * 6, HOST_IP)
    d = tx_ring(chip, [req, icmp_echo_frame(HOST_MAC, MAC, IP, HOST_IP, reply=False, ident=5, seq=1, data=b"x")])
    ew(chip, DMATDLAR, d[0])
    start_mac(chip, cr=RE | TE | DM | FES | CSTF)
    chip.run_for(50_000)
    rxed = []
    for a in rx:
        d0 = r32(chip, a)
        if not d0 & OWN:
            rxed.append(rmem(chip, r32(chip, a + 8), d0 >> 16 & 0x3FFF))
    assert describe(rxed[0])["info"] == "ARP 192.168.0.1 is-at 02:00:00:00:00:01"
    assert "echo reply" in describe(rxed[1])["info"]


def test_host_ping_resolves_arp_first(d407):
    chip = spin_chip(d407, clock_hz=16e6)
    seg = EthSegment()
    m = _Mac(chip)
    phy = EthPhy(m, aneg="10us")
    seg.attach(phy)
    h = seg.add_host("PC", ip="10.0.0.1")
    h.ping("10.0.0.2", count=2, interval="100us", start="20us")
    chip.run_for(150_000)
    assert len(m.rx) == 2                                                  # ARP request repeated (no answer)
    assert describe(m.rx[0][1])["info"] == "ARP who-has 10.0.0.2 tell 10.0.0.1"
    rep = arp_frame(2, MAC, bytes([10, 0, 0, 2]), h.mac, h.ip)
    h.deliver(rep + fcs(rep), 100e6, chip.now_ns())
    chip.run_for(10_000)
    assert "echo request" in describe(m.rx[-1][1][:-4])["info"]


def test_pcap_output(d407, tmp_path):
    chip, mac, host = eth_chip(d407)
    host.segment.set_pcap(tmp_path / "x.pcap")
    wait_link(chip, mac)
    f = HOST_MAC + MAC + b"\x08\x00" + bytes(46)
    d = tx_ring(chip, [f])
    ew(chip, DMATDLAR, d[0])
    start_mac(chip, rx=False)
    chip.run_for(20_000)
    host.segment.close()
    data = (tmp_path / "x.pcap").read_bytes()
    magic, vmaj, vmin, _z, _s, snap, lt = struct.unpack("<IHHiIII", data[:24])
    assert (magic, vmaj, vmin, lt) == (0xA1B23C4D, 2, 4, 1)
    sec, nsec, incl, orig = struct.unpack("<IIII", data[24:40])
    assert incl == orig == 60 and data[40:100] == f


# ================================================================================================ board / sample
def test_board_yaml_eth_link(tmp_path):
    from chipem.board import parse_board
    bd = parse_board({"name": "b", "chips": [{"id": "A", "chip": "stm32f407zg"}],
                      "links": [{"type": "eth", "nodes": [{"node": "A.ETH", "phy": {"model": "dp83848", "addr": 1}}],
                                 "phy": {"aneg": "1ms"}, "host": {"ip": "192.168.0.1"}, "pcap": "c.pcap"}]},
                     base=tmp_path)
    ln = bd.links[0]
    assert ln.type == "eth" and ln.nodes == [("A", "ETH")]
    assert ln.options["phy"] == [{"aneg": "1ms", "model": "dp83848", "addr": 1}]
    assert ln.options["pcap"] == str(tmp_path / "c.pcap")


@pytest.mark.skipif(not (BUILD / "eth_ping.elf").exists(), reason="sample not built (firmware/samples_stm32/build.sh)")
def test_sample_board_answers_ping(tmp_path):
    from chipem.session import Session
    s = Session.from_board(ROOT / "boards" / "stm32f407_eth_ping.yaml", quiet=True)
    try:
        s.run_for(40e6)
        seg = s.board.eth_segment()
        host = seg.hosts[0]
        assert len(host.replies) >= 3, host.state()
        assert all(0 < rtt < 200_000 for _seq, rtt in host.replies)
        infos = [e.fields["info"] for e in s.events(kinds={"eth"})]
        assert "ARP who-has 192.168.0.10 tell 192.168.0.10" in infos           # gratuitous ARP
        out = s.uart_output("A.USART1").decode()
        assert "PHY 0007:C131" in out and "link 100FD" in out and "ping 3" in out
    finally:
        s.close()


HAL_BUILD = ROOT / "firmware" / "samples_stm32_hal_eth" / "build"


@pytest.mark.skipif(not (HAL_BUILD / "eth_hal.elf").exists(),
                    reason="HAL sample not built (firmware/samples_stm32_hal_eth/build.sh)")
def test_hal_eth_driver_board():
    """ST's HAL ETH driver (enhanced descriptors, HAL_ETH_ReadData / Transmit, checksum insertion) end to end."""
    from chipem.session import Session
    s = Session.from_board(ROOT / "boards" / "stm32f407_eth_hal.yaml", quiet=True)
    try:
        s.run_for(30e6)
        host = s.board.eth_segment().hosts[0]
        out = s.uart_output("A.USART1").decode()
        assert "PHY 0007:C131" in out and "link 100FD" in out and "garp" in out, out
        assert len(host.replies) == 4, (out, host.state())
        assert not [e for e in s.events(kinds={"warn"}) if "Ethernet" in str(e.fields.get("msg"))]
        mac = s.chips["A"].peripherals["Ethernet_MAC"]
        assert mac.get("DMABMR") & (1 << 7)                                  # HAL selects enhanced descriptors
    finally:
        s.close()
