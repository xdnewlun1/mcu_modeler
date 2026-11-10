"""STM32F407 Ethernet MAC: behaviour checked against RM0090 Rev 22 chapter 33 (reset values, PMT interrupt
masking, MMC good-frame rules, padding/CRC, ring precedence, multicast perfect filter, runt accounting, enhanced
RX descriptor status, receive descriptor look-ahead)."""
from __future__ import annotations

import struct

from chipem.ethernet import fcs, fcs_ok, wire_ns
from test_stm32_eth import (DMABMR, DMAIER, DMAMFBOCR, DMAOMR, DMARDLAR, DMASR, DMATDLAR, ETH_IRQ, FES, FS, HOST_MAC,
                            IC, IPCO, LS, MAC, MACA0HR, MACA0LR, MACA1HR, MACA1LR, MACCR, MACFCR, MACIMR, MACPMTCSR,
                            MACSR, MMCRGUFCR, MMCTGFCR, NIS, OWN, RBUS, RCH, RE, RER, RS, SRAM, ST_TX, TCH, TE, TER, DM,
                            AIS, CSTF, _mux, _rx_setup, d407, eth_chip, er, ew, r32, rmem, rx_ring, start_mac, tx_ring,
                            udp_frame, w32, wait_link, wmem, GPIOA)
from test_stm32 import spin_chip

__all__ = ["d407"]


def test_reset_values_rm0090(d407):
    chip, mac, _ = eth_chip(d407, segment=False)
    assert er(chip, DMABMR) == 0x00020101                                   # RDP = 1, PBL = 1 (33.8.4)
    ew(chip, MACA0HR, 0x7FFF1234)                                           # MO always 1, 30:16 reserved
    assert er(chip, MACA0HR) == 0x80001234
    ew(chip, 0x54, 0xFFFFFFFF)                                              # MACA2LR: all 32 bits rw
    assert er(chip, 0x54) == 0xFFFFFFFF
    ew(chip, 0x54, 0x80000000)
    assert er(chip, 0x54) == 0x80000000


def test_pmt_status_visible_when_masked(d407):
    chip, mac, host, rx = _rx_setup(d407)
    start_mac(chip, tx=False)
    ew(chip, MACIMR, 1 << 3)                                                # PMTIM
    ew(chip, MACPMTCSR, 3)                                                  # PD + MPE
    host.send(b"\xff" * 6 + HOST_MAC + b"\x08\x42" + b"\xff" * 6 + MAC * 16)
    chip.run_for(20_000)
    assert er(chip, MACSR) & (1 << 3)                                       # status set, only the IRQ is masked
    assert not er(chip, DMASR) & (1 << 28) and (16 + ETH_IRQ) not in chip.arch.asserted
    ew(chip, MACIMR, 0)
    assert er(chip, DMASR) & (1 << 28) and (16 + ETH_IRQ) in chip.arch.asserted
    er(chip, MACPMTCSR)
    assert not er(chip, MACSR) & (1 << 3)


def test_hclk_below_25mhz_warns(d407):
    chip, mac, _ = eth_chip(d407, segment=False, clock_hz=16e6)
    ew(chip, MACCR, RE | TE)
    assert any("25 MHz" in str(e.fields.get("msg")) for e in chip.log.filter("warn"))
    chip2, _m, _ = eth_chip(d407, segment=False, clock_hz=50e6)
    ew(chip2, MACCR, RE | TE)
    assert not any("25 MHz" in str(e.fields.get("msg")) for e in chip2.log.filter("warn"))


def test_mmc_counts_ip_error_and_pause_frames_as_good(d407):
    chip, mac, host = eth_chip(d407)
    wait_link(chip, mac)
    f = bytearray(udp_frame(dst=HOST_MAC, src=MAC, sip=MAC[:4], dip=HOST_MAC[:4]))
    f[14] = 0x43                                                            # IHL 3 < 5: IP header error
    d = tx_ring(chip, [bytes(f)], extra0=1 << 22)                           # CIC = 01
    ew(chip, DMATDLAR, d[0])
    start_mac(chip, rx=False)
    chip.run_for(20_000)
    d0 = r32(chip, d[0])
    assert d0 & (1 << 16) and d0 & (1 << 15)                               # IHE, ES
    assert len(host.received) == 1 and er(chip, MMCTGFCR) == 1             # still transmitted: a good frame
    ew(chip, MACFCR, 100 << 16 | 1)                                         # FCB: pause frame
    chip.run_for(20_000)
    assert not er(chip, MACFCR) & 1 and er(chip, MMCTGFCR) == 2


def test_padding_appends_crc_despite_dc(d407):
    chip, mac, host = eth_chip(d407)
    wait_link(chip, mac)
    short = HOST_MAC + MAC + b"\x88\xb5" + b"abc"
    d = tx_ring(chip, [short], extra0=1 << 27)                              # DC set, DP clear
    ew(chip, DMATDLAR, d[0])
    start_mac(chip, rx=False)
    chip.run_for(20_000)
    tail = [e for e in chip.log.filter("eth")]
    assert tail and tail[0].fields["len"] == 64                             # 60 bytes + CRC
    assert host.received and len(host.received[0][1]) == 60


def test_tx_ter_takes_precedence_over_tch(d407):
    chip, mac, host = eth_chip(d407)
    wait_link(chip, mac)
    frames = [HOST_MAC + MAC + b"\x88\xb5" + bytes([i]) * 46 for i in range(2)]
    d = tx_ring(chip, frames)                                               # chained ring: d0 -> d1 -> d0
    w32(chip, d[0], r32(chip, d[0]) | TER)                                  # TER + TCH: back to DMATDLAR (d0)
    ew(chip, DMATDLAR, d[0])
    start_mac(chip, rx=False)
    chip.run_for(20_000)
    got = [f for _t, f in host.received]
    assert len(got) == 1 and got[0] == frames[0]                            # d1 (the TCH pointer) never used
    assert er(chip, DMASR) >> 20 & 7 == 6 and r32(chip, d[1]) & OWN


def test_rx_rer_takes_precedence_over_rch(d407):
    chip, mac, host, rx = _rx_setup(d407)
    w32(chip, rx[0] + 4, r32(chip, rx[0] + 4) | RER)                        # RCH and RER: back to the list start
    start_mac(chip, tx=False)
    host.send(udp_frame())
    host.send(udp_frame())
    chip.run_for(30_000)
    assert not r32(chip, rx[0]) & OWN and r32(chip, rx[1]) & OWN
    assert er(chip, DMASR) & RBUS                                           # second frame: rx[0] host-owned


def test_multicast_perfect_filter_uses_address_registers_1_to_3(d407):
    chip, mac, host, rx = _rx_setup(d407)
    mc = bytes.fromhex("01005e000001")
    start_mac(chip, tx=False)
    ew(chip, MACA0HR, mc[4] | mc[5] << 8)                                   # MAC address 0 = a multicast address
    ew(chip, MACA0LR, int.from_bytes(mc[:4], "little"))
    host.send(mc + HOST_MAC + b"\x88\xb5" + bytes(46))
    chip.run_for(10_000)
    assert r32(chip, rx[0]) & OWN                                           # address 0 does not match multicast
    ew(chip, MACA1HR, 1 << 31 | mc[4] | mc[5] << 8)                         # AE
    ew(chip, MACA1LR, int.from_bytes(mc[:4], "little"))
    host.send(mc + HOST_MAC + b"\x88\xb5" + bytes(46))
    chip.run_for(10_000)
    assert not r32(chip, rx[0]) & OWN


def test_runt_frames_counted_in_mfa_and_not_good(d407):
    chip, mac, host, rx = _rx_setup(d407)
    start_mac(chip, tx=False)
    runt = MAC + HOST_MAC + b"\x88\xb5" + bytes(10)
    w = runt + fcs(runt)
    host.segment.send(host, w, 100e6, chip.now_ns(), chip.now_ns())
    chip.run_for(1000)
    assert r32(chip, rx[0]) & OWN
    assert er(chip, DMAMFBOCR) >> 17 & 0x7FF == 1                           # MFA (33.8.4 DMAMFBOCR)
    ew(chip, DMAOMR, er(chip, DMAOMR) | 1 << 6)                             # FUGF: forwarded
    host.segment.send(host, w, 100e6, chip.now_ns(), chip.now_ns())
    chip.run_for(1000)
    assert not r32(chip, rx[0]) & OWN
    assert er(chip, MMCRGUFCR) == 0                                         # runt: not a good frame (33.5.7)


def test_enhanced_descriptor_non_ip_frame_status(d407):
    chip, mac, host, rx = _rx_setup(d407, enhanced=True)
    for d in rx:
        w32(chip, d + 24, 0x11111111)
        w32(chip, d + 28, 0x22222222)
    start_mac(chip, tx=False, cr=RE | TE | DM | FES | CSTF | IPCO, bmr=0x20100 | 1 << 7, omr=1 << 25)
    host.send(b"\xff" * 6 + HOST_MAC + b"\x08\x06" + bytes(46))            # ARP: type frame, not IP
    chip.run_for(10_000)
    d0 = r32(chip, rx[0])
    assert not d0 & OWN and d0 & (1 << 5) and not d0 & 1                   # FT set, ESA clear
    assert r32(chip, rx[0] + 24) == 0x11111111 and r32(chip, rx[0] + 28) == 0x22222222   # no TSE: unchanged
    host.send(udp_frame())
    chip.run_for(10_000)
    d0 = r32(chip, rx[1])
    assert d0 & 1 and d0 & (1 << 5) and r32(chip, rx[1] + 16) & (1 << 6)   # ESA, IPV4PR


def test_rbus_raised_when_next_descriptor_host_owned(d407):
    chip, mac, host = eth_chip(d407)
    wait_link(chip, mac)
    rx = rx_ring(chip, n=2)
    w32(chip, rx[1], 0)                                                     # second descriptor host-owned
    ew(chip, DMARDLAR, rx[0])
    start_mac(chip, tx=False, ier=RBUS | AIS | RS | NIS)
    assert er(chip, DMASR) >> 17 & 7 == 3
    host.send(udp_frame())
    chip.run_for(10_000)
    sr = er(chip, DMASR)
    assert not r32(chip, rx[0]) & OWN and sr & RS
    assert sr & RBUS and sr >> 17 & 7 == 4                                  # look-ahead: suspended at once
    ew(chip, DMASR, RBUS | AIS)
    host.send(udp_frame())                                                  # still no descriptor: flushed
    chip.run_for(10_000)
    assert er(chip, DMAMFBOCR) & 0xFFFF == 1 and not er(chip, DMASR) & RBUS
