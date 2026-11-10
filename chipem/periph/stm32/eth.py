"""STM32F407/F417 Ethernet MAC with DMA (RM0090 ch. 33): MAC, MMC, PTP and DMA registers in one 0x1400 block
(SVD tables Ethernet_MAC +0x0, Ethernet_MMC +0x100, Ethernet_PTP +0x700, Ethernet_DMA +0x1000), descriptor rings in
RAM, an MDIO-managed PHY (chipem/ethernet.py `EthPhy`) and frames on an `EthSegment` (board `eth` link).

Checked against RM0090 Rev 22 ch. 33 (register descriptions 33.8, reset values Table 195, descriptors 33.6.7/33.6.8,
DMA interrupts 33.6.9, MMC 33.5.7, filtering 33.5.5, SMI 33.4.1); ST's STM32CubeF4 HAL ETH driver (vendored) runs
on it.  Deviations are listed in docs/LIMITATIONS.md.  Where the manual's wording and the HAL disagree, silicon
behaviour proven by the HAL wins: TDES0 control bits (TCH/TER/IC/CIC...) are kept when the DMA closes a descriptor
(33.6.7 says it "resets all the control bits"; the HAL sets TCH once and reuses the ring).

Presence: the block exists on STM32F407/F417 only.  The shared base YAML gives it `params.only: [STM32F407,
STM32F417]`; on other parts the model behaves like the `stub` (`_util.make_absent`).

* Clocks / reset: RCC AHB1ENR ETHMACEN gates register access (`GatedBlock`, one `warn`), ETHMACTXEN / ETHMACRXEN must
  be set for frames to be sent / received (one `warn` each otherwise); AHB1RSTR ETHMACRST resets the block.  The
  MII/RMII selection (SYSCFG_PMC.MII_RMII_SEL) is latched when the MAC clock is enabled and at every MAC reset (RCC
  ETHMACRST or DMABMR.SR; ST's HAL selects the interface after enabling the clocks and then issues DMABMR.SR); a PMC
  change that is not followed by a reset logs one `warn` and is ignored.  DMABMR.SR (software reset) resets every MAC/MMC/PTP/DMA register and clears
  itself after a few clocks - only while the PHY supplies the interface clock (RMII: a pin muxed to
  ETH_RMII_REF_CLK; MII: ETH_MII_TX_CLK and ETH_MII_RX_CLK), otherwise it stays set (one `warn`), as on silicon.
  DMABMR resets to 0x0002 0101 (SR = 1 until the reset completes).  HCLK below 25 MHz with TE/RE set: one `warn`
  (RM0090 33.2 note).
* Pins (signals table + mux checks): MII needs TX_CLK, RX_CLK, TX_EN, TXD0-3, RXD0-3, RX_DV (CRS/COL/RX_ER are
  optional); RMII needs REF_CLK, TX_EN, TXD0-1, RXD0-1, CRS_DV.  Missing pins: one `warn`, frames are lost in that
  direction.  The data pins are not toggled; the PHY's interface clocks (50 MHz REF_CLK, 25/2.5 MHz TX_CLK/RX_CLK)
  are summarised as `pin` `wave` events while the MAC clock is enabled (chipem/periph/sdbus.py ClockWave).
* MDIO (MACMIIAR/MACMIIDR): writing MB starts a clause-22 transaction of 64 MDC clocks (32-bit preamble + frame), MDC
  = HCLK / 42, 62, 16, 26, 102 for CR = 0..4 (reserved CR: /102 and one `warn`; MDC above 2.5 MHz: one `warn`); MB
  reads 1 until it ends, then a read stores the PHY register in MACMIIDR.  Writes to MACMIIAR/MACMIIDR while MB = 1
  are ignored (one `warn`).  ETH_MDC/ETH_MDIO not muxed: reads return 0xFFFF, writes are lost.  MDC is summarised as
  a gated `wave`.
* MAC (MACCR): RE / TE enable reception / transmission; FES (100 / 10 Mbit/s) and DM (duplex) must match the PHY's
  link (a speed mismatch loses frames, one `warn`); LM loops frames back inside the MAC; IFG (96 - 8 x IFG bit
  times); IPCO receive checksum offload; CSTF / APCS strip the FCS (and pad, APCS) of type / length frames; ROD stops
  receiving the own frames in half duplex.  Frame filter (MACFFR, RM0090 33.5.5, Tables 191/192): perfect DA filter
  on MACA0..A3 for unicast, MACA1..A3 only for multicast (AE, SA, MBC byte masks), 64-bit hash filter (MACHTHR/HTLR, index = upper 6 bits of the bit-reversed CRC-32 of the DA)
  for unicast (HU) and multicast (HM), HPF (hash or perfect), DAIF, PAM, BFD, SA filter (SAF/SAIF with MACAx.SA),
  PM promiscuous, RA receive all (failures then only flagged: RDES0 AFM/SAF).  Pause frames (MACFCR): FCB sends a
  pause frame (PT quanta) in full duplex; RFCE honours received pause frames (transmitter waits PT x 512 bit times);
  PCF decides whether control frames reach memory.  VLAN tag register is storage (frames with tag 0x8100 get RDES0
  VLAN).  PMT: PD drops every frame; with MPE a magic packet (6 x FF + 16 x MAC address) sets MPR, clears PD, sets
  MACSR.PMTS (MACIMR.PMTIM masks only DMASR.PMTS / the interrupt, Figure 370) and pulses EXTI line 19 (ETH_WKUP); remote wake-up frames (WFE) are not detected
  (one `warn`).  Reading MACPMTCSR clears MPR/WFR.
* MMC (33.5.7): TGFCR (good frames sent: every frame not aborted by no carrier / loss of carrier, IP checksum
  error frames and pause frames included), RGUFCR (good unicast received: filter passed, no CRC error, not a runt),
  RFCECR (CRC errors), RFAECR (alignment errors, never counted), TGFSCCR/TGFMSCCR (collisions: never); MMCCR CR (reset, self-clearing), CSR (stop at the maximum), ROR
  (reset on read), MCF (freeze), MCP/MCFHP (preset to half / almost full); MMCRIR/MMCTIR flags when a counter reaches
  half or full scale, cleared when the counter is read; MACSR.MMCRS/MMCTS/MMCS follow (masks MMCRIMR/MMCTIMR).
* PTP registers are storage; setting PTPTSCR.TSE logs one `warn` (time stamps are written as 0; without TSE the
  RDES6/7 words are left unchanged, 33.6.8).
* DMA (DMABMR, DMAOMR, DMASR, DMAIER): DMAOMR.ST / SR start/stop the transmit / receive process (TPS / RPS states 0
  stopped, 1 fetching, 2 waiting for status, 3 waiting for a frame, 4/6 suspended, 7 transferring); DMATDLAR /
  DMARDLAR set the list start (current descriptor = list start whenever written while stopped); DMATPDR / DMARPDR
  poll demands resume a suspended process; FTF flushes the TX FIFO (self-clearing).  Descriptors: normal (16 bytes) or
  enhanced (DMABMR.EDFE: 32 bytes, RDES4 extended status, RDES6/7 and TDES6/7 time stamps), ring mode (TER/RER end of
  ring, buffer 2 = TDES3/RDES3, next descriptor = this + size + DSL words) or chained (TCH/RCH: TDES3/RDES3 = next;
  TER/RER take precedence).
  TX: OWN, FS/LS spanning several descriptors, IC, DC (CRC appended unless set), DP (padding to 60 bytes unless set;
  a padded frame always gets its CRC),
  CIC checksum insertion (01 IPv4 header, 10 + TCP/UDP/ICMP payload with the pseudo-header sum provided in the
  checksum field, 11 + pseudo-header computed; IPv4 and IPv6; IHE / IPE errors), status (ES, IHE, IPE, NC/LCA in
  half duplex without carrier) in the last descriptor, OWN cleared in all of them, TS when IC.  A host-owned
  descriptor suspends the transmit process (TBUS).  RX: frames fill RBS1/RBS2 buffers of as many descriptors as
  needed (FS, LS, FL = cumulative byte count, the frame length in the last one), status AFM, ES, DE, SAF, LE, OE,
  VLAN, FT, RE, CE, IPHCE/PCE (IPCO, normal descriptors, Table 193) or ESA + RDES4 (IPPT, IPHE, IPPE, IPCB, IPV4PR,
  IPV6PR; enhanced descriptors: ESA for IPv4/IPv6 frames only, FT keeps its plain meaning), RS unless DIC.  After
  closing a frame the engine looks at the next descriptor at once (33.6.8 step 8): host-owned -> RBUS and Suspended.
  A frame that finds no DMA-owned descriptor sets RBUS (once, when the previous one was DMA-owned), suspends the
  process and is flushed (MFC counts it) unless DMAOMR.DFRF holds it in the 2 KB RX FIFO (overflow: MFA, ROS); a
  suspended receive process resumes on DMARPDR or when the next frame arrives.  With IPCO, RSF and DTCEFD = 0 frames
  with an IP header / TCP/UDP/ICMP checksum error are dropped.  CRC errors are dropped unless FEF, runts unless FUGF
  (dropped runts count in DMAMFBOCR.MFA).  DMAMFBOCR (missed frames) clears when read.  Bus errors (unmapped addresses, the CCM
  RAM which the Ethernet DMA cannot reach) set FBES and stop both processes.
  DMASR: bits 0-16 are write-1-to-clear; NIS / AIS are sticky summaries of the enabled normal (TS, TBUS, RS, ERS) /
  abnormal (TPSS, TJTS, ROS, TUS, RBUS, RPSS, PWTS, ETS, FBES) bits; MMCS/PMTS/TSTS mirror MACSR.  IRQ "ETH" =
  (NIS & NISE) | (AIS & AISE) | MMCS | PMTS | TSTS (level).
* Timing: a frame occupies the wire for (8 + max(len, 64)) x 8 bit times at the MAC speed; the descriptor is closed
  (status, OWN cleared, TS) when the last bit has been sent, the next frame starts after the IFG.  Received frames are
  written to memory (zero bus time) when their last bit arrives.  Frames go through `EthPhy.transmit` (loopback, link
  down -> lost: no carrier / loss of carrier status in half duplex), one `eth` event per frame (chipem/ethernet.py).
Not modelled: collisions / back-off / half-duplex deferral (frames never collide), jabber / receive watchdog timers,
the receive status watchdog (DMARSWTR: with DIC no RS is raised later), early transmit/receive interrupts, TX/RX FIFO
thresholds (store-and-forward behaviour always), the TX FIFO holding several frames, remote wake-up frame filters,
PTP time stamping and the PPS output, VLAN tag filtering, the SA filter status with SAF = 0 (RDES0 SAF is only
reported when MACFFR.SAF is set), DFRF keeping a truncated frame for the next descriptor (a frame that runs out of
descriptors is always closed with DE and LS).
"""
from __future__ import annotations

import struct
from collections import deque
from typing import Any

from ...bus import BusError
from ...ethernet import (EthPhy, describe, fcs, fcs_ok, inet_checksum, mac_str, wire_ns, BROADCAST)
from ..base import Reg
from ..registry import register
from ..sdbus import ClockWave
from ._util import GatedBlock, exti_input, find_model, irq_number, make_absent, part_present, rcc, svd_regs

BLOCKS = (("Ethernet_MAC", 0x0), ("Ethernet_MMC", 0x100), ("Ethernet_PTP", 0x700), ("Ethernet_DMA", 0x1000))

# MACCR
RE, TE, DC_, APCS, RD, IPCO, DM, LM, ROD, FES, CSD, JD, WD, CSTF = (
    1 << 2, 1 << 3, 1 << 4, 1 << 7, 1 << 9, 1 << 10, 1 << 11, 1 << 12, 1 << 13, 1 << 14, 1 << 16, 1 << 22, 1 << 23,
    1 << 25)
# MACFFR
PM, HU, HM, DAIF, PAM, BFD, SAIF, SAF_EN, HPF, RA = (1, 2, 4, 8, 16, 32, 1 << 8, 1 << 9, 1 << 10, 1 << 31)
# MACMIIAR
MB, MW = 1, 2
MDC_DIV = {0: 42, 1: 62, 2: 16, 3: 26, 4: 102}
# MACFCR
FCB, TFCE, RFCE, UPFD = 1, 2, 4, 8
# MACPMTCSR
PD, MPE, WFE, MPR, WFR, GU, WFFRPR = 1, 2, 4, 1 << 5, 1 << 6, 1 << 9, 1 << 31
# MACSR / MACIMR
PMTS_M, MMCS_M, MMCRS_M, MMCTS_M, TSTS_M = 1 << 3, 1 << 4, 1 << 5, 1 << 6, 1 << 9
# DMABMR
SR_RESET, EDFE = 1, 1 << 7
# DMAOMR
SR_RX, OSF, FUGF, FEF, ST_TX, FTF, TSF, DFRF, RSF, DTCEFD = (1 << 1, 1 << 2, 1 << 6, 1 << 7, 1 << 13, 1 << 20,
                                                             1 << 21, 1 << 24, 1 << 25, 1 << 26)
# DMASR
TS, TPSS, TBUS, TJTS, ROS, TUS, RS, RBUS, RPSS, PWTS, ETS, FBES, ERS, AIS, NIS = (
    1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 1 << 13, 1 << 14, 1 << 15, 1 << 16)
NORMAL = TS | TBUS | RS | ERS
ABNORMAL = TPSS | TJTS | ROS | TUS | RBUS | RPSS | PWTS | ETS | FBES
W1C = 0x1FFFF
MMCS_S, PMTS_S, TSTS_S = 1 << 27, 1 << 28, 1 << 29
NISE, AISE = 1 << 16, 1 << 15
# TX descriptor 0
T_OWN, T_IC, T_LS, T_FS, T_DC, T_DP, T_TTSE, T_TER, T_TCH = (1 << 31, 1 << 30, 1 << 29, 1 << 28, 1 << 27, 1 << 26,
                                                             1 << 25, 1 << 21, 1 << 20)
T_IHE, T_ES, T_IPE, T_LCA, T_NC = 1 << 16, 1 << 15, 1 << 12, 1 << 11, 1 << 10
T_STATUS = 0x3FFFF
# RX descriptor 0 / 1
R_OWN, R_AFM, R_ES, R_DE, R_SAF, R_LE, R_OE, R_VLAN, R_FS, R_LS, R_IPHCE, R_FT, R_RE, R_CE, R_PCE = (
    1 << 31, 1 << 30, 1 << 15, 1 << 14, 1 << 13, 1 << 12, 1 << 11, 1 << 10, 1 << 9, 1 << 8, 1 << 7, 1 << 5, 1 << 3,
    1 << 1, 1)
R_DIC, R_RER, R_RCH = 1 << 31, 1 << 15, 1 << 14
RX_FIFO = 2048

TPS_STOP, TPS_FETCH, TPS_WAIT, TPS_SUSP = 0, 1, 2, 6
RPS_STOP, RPS_FETCH, RPS_WAIT, RPS_SUSP, RPS_XFER = 0, 1, 3, 4, 7

MII_SIGNALS = ("ETH_MII_TX_CLK", "ETH_MII_RX_CLK", "ETH_MII_TX_EN", "ETH_MII_TXD0", "ETH_MII_TXD1", "ETH_MII_TXD2",
               "ETH_MII_TXD3", "ETH_MII_RXD0", "ETH_MII_RXD1", "ETH_MII_RXD2", "ETH_MII_RXD3", "ETH_MII_RX_DV")
RMII_SIGNALS = ("ETH_RMII_REF_CLK", "ETH_RMII_TX_EN", "ETH_RMII_TXD0", "ETH_RMII_TXD1", "ETH_RMII_RXD0",
                "ETH_RMII_RXD1", "ETH_RMII_CRS_DV")
TX_SIG = {"mii": ("ETH_MII_TX_CLK", "ETH_MII_TX_EN", "ETH_MII_TXD0", "ETH_MII_TXD1", "ETH_MII_TXD2", "ETH_MII_TXD3"),
          "rmii": ("ETH_RMII_REF_CLK", "ETH_RMII_TX_EN", "ETH_RMII_TXD0", "ETH_RMII_TXD1")}
RX_SIG = {"mii": ("ETH_MII_RX_CLK", "ETH_MII_RX_DV", "ETH_MII_RXD0", "ETH_MII_RXD1", "ETH_MII_RXD2", "ETH_MII_RXD3"),
          "rmii": ("ETH_RMII_REF_CLK", "ETH_RMII_CRS_DV", "ETH_RMII_RXD0", "ETH_RMII_RXD1")}
PAUSE_DA = bytes.fromhex("0180c2000001")


def _bitrev32(x: int) -> int:
    return int(f"{x & 0xFFFFFFFF:032b}"[::-1], 2)


def hash_index(da: bytes) -> int:
    """Hash filter bit (0..63) of a destination address: upper 6 bits of the bit-reversed Ethernet CRC-32."""
    import zlib
    return _bitrev32(zlib.crc32(bytes(da)) & 0xFFFFFFFF) >> 26


class _Fault(Exception):
    def __init__(self, addr: int, msg: str):
        super().__init__(msg)
        self.addr = addr
        self.msg = msg


def _l3(frame: bytes) -> tuple[int, int]:
    """(ethertype, offset of the L3 header), skipping one VLAN tag."""
    if len(frame) < 14:
        return 0, 14
    et = int.from_bytes(frame[12:14], "big")
    if et == 0x8100 and len(frame) >= 18:
        return int.from_bytes(frame[16:18], "big"), 18
    return et, 14


def _l4_sum(proto: int, l4: bytes, pseudo: int) -> int:
    return inet_checksum(l4, pseudo if proto != 1 else 0)


def _pseudo4(src: bytes, dst: bytes, proto: int, length: int) -> int:
    return sum(struct.unpack("!HHHH", src + dst)) + proto + length


def _pseudo6(src: bytes, dst: bytes, nh: int, length: int) -> int:
    return sum(struct.unpack("!16H", src + dst)) + (length >> 16) + (length & 0xFFFF) + nh


_CSUM_OFF = {6: 16, 17: 6, 1: 2, 58: 2}


def checksum_insert(frame: bytearray, cic: int) -> int:
    """TX checksum offload (CIC 1..3) on `frame` in place; returns TDES0 error bits (IHE / IPE)."""
    et, o = _l3(frame)
    if et == 0x0800:
        if len(frame) < o + 20 or frame[o] >> 4 != 4:
            return T_IHE
        ihl = (frame[o] & 0xF) * 4
        tot = int.from_bytes(frame[o + 2:o + 4], "big")
        if ihl < 20 or len(frame) < o + ihl:
            return T_IHE
        frame[o + 10:o + 12] = b"\0\0"
        frame[o + 10:o + 12] = inet_checksum(bytes(frame[o:o + ihl])).to_bytes(2, "big")
        if cic < 2:
            return 0
        if tot < ihl or o + tot > len(frame):
            return T_IPE
        flags = int.from_bytes(frame[o + 6:o + 8], "big")
        proto = frame[o + 9]
        if flags & 0x3FFF or proto not in (1, 6, 17):
            return 0                                            # fragment / other payload: bypassed
        l4o, l4len = o + ihl, tot - ihl
        pseudo = _pseudo4(bytes(frame[o + 12:o + 16]), bytes(frame[o + 16:o + 20]), proto, l4len)
    elif et == 0x86DD:
        if cic < 2:
            return 0
        if len(frame) < o + 40:
            return T_IHE
        plen = int.from_bytes(frame[o + 4:o + 6], "big")
        proto = frame[o + 6]
        if o + 40 + plen > len(frame):
            return T_IPE
        if proto not in (6, 17, 58):
            return 0
        l4o, l4len = o + 40, plen
        pseudo = _pseudo6(bytes(frame[o + 8:o + 24]), bytes(frame[o + 24:o + 40]), proto, l4len)
    else:
        return 0
    co = l4o + _CSUM_OFF[proto]
    if co + 2 > l4o + l4len:
        return T_IPE
    if cic == 3 or proto in (1,):
        frame[co:co + 2] = b"\0\0"
        base = pseudo if proto != 1 else 0
    else:
        base = 0                                                # CIC 10: the field holds the pseudo-header sum
    c = inet_checksum(bytes(frame[l4o:l4o + l4len]), base)
    if proto == 17 and c == 0:
        c = 0xFFFF
    frame[co:co + 2] = c.to_bytes(2, "big")
    return 0


def checksum_verify(frame: bytes) -> tuple[int, bool, bool, bool]:
    """RX checksum offload: (payload type 0 unknown/1 UDP/2 TCP/3 ICMP, IP version 4/6/0, header error, payload
    error, payload checked)."""
    et, o = _l3(frame)
    if et == 0x0800 and len(frame) >= o + 20 and frame[o] >> 4 == 4:
        ihl = (frame[o] & 0xF) * 4
        tot = int.from_bytes(frame[o + 2:o + 4], "big")
        herr = ihl < 20 or len(frame) < o + ihl or inet_checksum(frame[o:o + ihl]) != 0
        if herr or tot < ihl or o + tot > len(frame):
            return 0, 4, True, False, False
        proto = frame[o + 9]
        flags = int.from_bytes(frame[o + 6:o + 8], "big")
        if flags & 0x3FFF or proto not in (1, 6, 17):
            return 0, 4, False, False, False
        l4 = frame[o + ihl:o + tot]
        if proto == 17 and len(l4) >= 8 and l4[6:8] == b"\0\0":
            return 1, 4, False, False, True                     # UDP without checksum
        pseudo = _pseudo4(frame[o + 12:o + 16], frame[o + 16:o + 20], proto, len(l4))
        ok = _l4_sum(proto, l4, pseudo) == 0
        return {17: 1, 6: 2, 1: 3}[proto], 4, False, not ok, True
    if et == 0x86DD and len(frame) >= o + 40:
        plen = int.from_bytes(frame[o + 4:o + 6], "big")
        proto = frame[o + 6]
        if o + 40 + plen > len(frame) or proto not in (6, 17, 58):
            return 0, 6, False, False, False
        l4 = frame[o + 40:o + 40 + plen]
        pseudo = _pseudo6(frame[o + 8:o + 24], frame[o + 24:o + 40], proto, len(l4))
        ok = inet_checksum(l4, pseudo) == 0
        return {17: 1, 6: 2, 58: 3}[proto], 6, False, not ok, True
    return 0, 0, False, False, False


def _build_regs(regs_file: str | None, overrides: dict[str, dict]) -> list[Reg]:
    out: list[Reg] = []
    for svd, delta in BLOCKS:
        pr = svd_regs((svd,), regs_file)
        if pr is None:
            raise KeyError(f"no SVD register table {svd}")
        for spec in pr.reg_specs():
            spec["offset"] += delta
            spec.update(overrides.get(spec["name"], {}))
            out.append(Reg(**spec))
    return out


@register("stm32.eth")
class ETH(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = "ETH"
        ro = {"ro_mask": 0xFFFFFFFF}
        ov: dict[str, dict] = {
            "MACCR": {"on_write": self._maccr_write},
            "MACMIIAR": {"on_write": self._miiar_write},
            "MACMIIDR": {"on_write": self._miidr_write},
            "MACFCR": {"on_write": self._macfcr_write},
            "MACPMTCSR": {"ro_mask": MPR | WFR, "on_read": self._pmt_read, "on_write": self._pmt_write},
            "MACDBGR": ro, "MACSR": {"ro_mask": 0xFFFFFFFF, "on_read": self._macsr_read},
            "MACIMR": {"on_write": self._update_w},
            # RM0090 33.8.1: MO (bit 31) always 1, bits 30:16 reserved; MACA2LR is a plain 32-bit address (the SVD
            # marks bit 31 read-only)
            "MACA0HR": {"reset": 0x8000FFFF, "ro_mask": 0xFFFF0000},
            "MACA2LR": {"ro_mask": 0},
            "MMCCR": {"on_write": self._mmccr_write},
            "MMCRIR": ro, "MMCTIR": ro,
            "MMCRIMR": {"on_write": self._update_w}, "MMCTIMR": {"on_write": self._update_w},
            "PTPTSCR": {"on_write": self._ptp_write},
            # RM0090 33.8.4: reset 0x0002 0101 (RDP = 1, PBL = 1, SR = 1); the SVD gives 0x2101 (PBL = 33, invalid)
            "DMABMR": {"reset": 0x00020101, "on_write": self._dmabmr_write, "on_read": self._dmabmr_read},
            "DMATPDR": {"on_write": self._tpdr_write, "on_read": lambda b, r: 0},
            "DMARPDR": {"on_write": self._rpdr_write, "on_read": lambda b, r: 0},
            "DMARDLAR": {"on_write": self._rdlar_write}, "DMATDLAR": {"on_write": self._tdlar_write},
            "DMASR": {"ro_mask": ~W1C & 0xFFFFFFFF, "w1c_mask": W1C, "on_write": self._dmasr_write},
            "DMAOMR": {"on_write": self._dmaomr_write},
            "DMAIER": {"on_write": self._update_w},
            "DMAMFBOCR": {"ro_mask": 0xFFFFFFFF, "on_read": self._mfbocr_read},
            "DMACHTDR": ro, "DMACHRDR": ro, "DMACHTBAR": ro, "DMACHRBAR": ro,
        }
        for c in ("MMCTGFSCCR", "MMCTGFMSCCR", "MMCTGFCR", "MMCRFCECR", "MMCRFAECR", "MMCRGUFCR"):
            ov[c] = {"ro_mask": 0xFFFFFFFF, "on_read": self._counter_read(c)}
        r = _build_regs(params.get("regs_file"), ov)
        super().__init__(name, base, size, r, log=log, params=params)
        self.absent = False
        self.phy = EthPhy(self, model=params.get("phy", "lan8720"), addr=int(params.get("phy_addr", 0)),
                          aneg=params.get("aneg"), link=params.get("link"), reset=params.get("phy_reset"))
        self.event_frames = 1
        self._irqn: int | None = None
        self._line = False
        self._warned: set[str] = set()
        self.stats = {"tx": 0, "rx": 0, "filtered": 0, "crc": 0, "missed": 0, "lost": 0}
        self.tx_frames: deque = deque(maxlen=10_000)          # (t_ns, frame without FCS)
        self.rx_frames: deque = deque(maxlen=10_000)          # (t_ns, frame without FCS) stored to memory
        self._iface: str | None = None
        self._waves: dict[str, ClockWave] = {}
        self._mdc: ClockWave | None = None
        self._init_state()

    def _init_state(self) -> None:
        self._mdio_ev = None
        self._sr_ev = None
        self._tx_ev = None
        self._fcb_ev = None
        self._tps = TPS_STOP
        self._rps = RPS_STOP
        self._tx_cur = 0
        self._rx_cur = 0
        self._tx_busy = False
        self._tx_pending: tuple | None = None          # frame fetched, waiting for TE / pause end
        self._pause_until = 0.0
        self._rxq: deque = deque()
        self._rxq_bytes = 0
        self._rbus_armed = True
        self._next_tx_t = 0.0

    # ------------------------------------------------------------------ setup
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        if not part_present(chip, self.params):
            make_absent(self, "the Ethernet MAC (STM32F407/F417 only)")
            return
        self._irqn = irq_number(chip, "ETH")
        self._syscfg = find_model(chip, "stm32.syscfg", "SYSCFG")
        r = rcc(chip)
        if r is not None:
            r.gate_listeners.append(self._gate_changed)
        self._mdc = ClockWave(self, "ETH_MDC")
        for s in ("ETH_RMII_REF_CLK", "ETH_MII_TX_CLK", "ETH_MII_RX_CLK"):
            self._waves[s] = ClockWave(self, s)

    def reset(self) -> None:
        if getattr(self, "absent", False):
            super(GatedBlock, self).reset()
            return
        chip = self.chip
        if chip is not None:
            for ev in (self._mdio_ev, self._sr_ev, self._tx_ev, self._fcb_ev):
                if ev is not None:
                    chip.sched.cancel(ev)
            if self._line and self._irqn is not None:
                self.irq(self._irqn, False)
        self._line = False
        for w in list(self._waves.values()) + ([self._mdc] if self._mdc else []):
            w.reset()
        self._init_state()
        self._warned.clear()
        super().reset()
        self._latch_iface()
        self._clock_waves()

    def _regs_reset(self) -> None:
        """DMABMR.SR: every register back to its reset value (SR itself stays set until the reset completes)."""
        chip = self.chip
        for ev in (self._mdio_ev, self._tx_ev, self._fcb_ev):
            if ev is not None and chip is not None:
                chip.sched.cancel(ev)
        sr_ev = self._sr_ev
        self._init_state()
        self._sr_ev = sr_ev
        mem = self.mem
        for rg in self.regs.values():
            mem[rg.boff:rg.boff + rg.bw] = rg.reset.to_bytes(rg.bw, "little")
        self._update()

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    # ------------------------------------------------------------------ board / segment
    def eth_attach(self, segment: Any, **phy: Any) -> None:
        """Connect this MAC's PHY to an EthSegment (board `eth` link); `phy` options reconfigure the PHY (model,
        addr, aneg, link, reset)."""
        if self.absent:
            raise ValueError(f"{self.name}: the Ethernet MAC is not present on this part")
        if phy:
            self.phy.configure(**phy)
        if self.phy.segment is not None and self.phy.segment is not segment:
            self.phy.segment.detach(self.phy)
        segment.attach(self.phy)

    def eth_detach(self) -> None:
        if self.phy.segment is not None:
            self.phy.segment.detach(self.phy)

    def eth_stats(self) -> dict:
        return {**self.stats, "link": self.phy.mode(), "phy": self.phy.model, "phy_addr": self.phy.addr,
                "iface": self._iface or "?", "segment": getattr(self.phy.segment, "name", None)}

    # ------------------------------------------------------------------ helpers
    def _f(self, reg: str, lsb: int, width: int = 1) -> int:
        return (self.get(reg) >> lsb) & ((1 << width) - 1)

    def _muxed(self, sig: str) -> bool:
        board = getattr(self.chip, "pins", None)
        if board is None:
            return True
        return any(board.signal_matches(p.owner, sig) for p in board.signal_pins(sig))

    def _missing(self, sigs) -> list[str]:
        return [s for s in sigs if not self._muxed(s)]

    def _hclk(self) -> float:
        return float(self.chip.clock.hz)

    def _rate(self) -> float:
        return 100e6 if self.get("MACCR") & FES else 10e6

    def _gate(self, bit: int) -> bool:
        r = self._rcc
        if r is None:
            return True
        return bool(r.get("AHB1ENR") >> bit & 1)

    # ------------------------------------------------------------------ interface mode / clocks
    def _latch_iface(self) -> None:
        sc = getattr(self, "_syscfg", None)
        pmc = sc.get("PMC") if sc is not None and "PMC" in sc.regs else 0
        self._iface = "rmii" if pmc >> 23 & 1 else "mii"

    def _iface_now(self) -> str:
        sc = getattr(self, "_syscfg", None)
        if sc is not None and "PMC" in sc.regs:
            now = "rmii" if sc.get("PMC") >> 23 & 1 else "mii"
            if now != self._iface:
                self._warn_once("pmc", f"{self.name}: SYSCFG_PMC.MII_RMII_SEL changed to {now.upper()} while the MAC "
                                f"clock was running: ignored until the MAC is reset (RCC ETHMACRST or "
                                f"DMABMR.SR); still {str(self._iface).upper()}")
        return self._iface or "mii"

    def _gate_changed(self, enr: str, old: int, new: int) -> None:
        if enr != "AHB1ENR":
            return
        if (new & ~old) >> 25 & 1:
            self._latch_iface()
        if (old ^ new) & (1 << 25):
            self._clock_waves()

    def _clock_waves(self) -> None:
        if self.chip is None or not self._waves:
            return
        on = self._gate(25)
        iface = self._iface or "mii"
        mii_hz = 2.5e6 if self.phy.link_up and self.phy.speed == 10 else 25e6
        want = {"ETH_RMII_REF_CLK": 50e6 if on and iface == "rmii" else 0.0,
                "ETH_MII_TX_CLK": mii_hz if on and iface == "mii" else 0.0,
                "ETH_MII_RX_CLK": mii_hz if on and iface == "mii" else 0.0}
        for s, hz in want.items():
            w = self._waves[s]
            if w.hz != hz:
                w.set(hz, False)

    def _phy_clock(self, warn: bool = True) -> bool:
        iface = self._iface_now() if warn else (self._iface or "mii")
        sigs = ("ETH_RMII_REF_CLK",) if iface == "rmii" else ("ETH_MII_TX_CLK", "ETH_MII_RX_CLK")
        return not self._missing(sigs)

    # ------------------------------------------------------------------ PHY callbacks
    def phy_link_changed(self, phy: Any) -> None:
        if self.absent or self.chip is None:
            return
        self._clock_waves()
        self.emit("info", block=self.name, msg=f"{self.name}: PHY {phy.model} link {phy.mode()}"
                  + (f" ({'auto-negotiated' if phy.an_done else 'forced'})" if phy.link_up else ""))
        if phy.link_up:
            mac_fd = bool(self.get("MACCR") & DM)
            if mac_fd != phy.full_duplex and self.get("MACCR") & (TE | RE):
                self._warn_once("duplex", f"{self.name}: MACCR.DM ({'full' if mac_fd else 'half'} duplex) differs "
                                f"from the PHY link ({phy.mode()})")

    def phy_rx(self, phy: Any, wire: bytes, rate: float, t_end: float) -> None:
        if self.absent:
            return
        self._mac_rx(bytes(wire), rate, loop=bool(phy.bmcr & (1 << 14)))

    # ------------------------------------------------------------------ IRQ
    def _update(self) -> None:
        if self.chip is None or self.absent:
            return
        sr = self.get("DMASR")
        ier = self.get("DMAIER")
        if sr & ier & NORMAL:
            sr |= NIS
        if sr & ier & ABNORMAL:
            sr |= AIS
        macsr = self._macsr_value()
        sr &= ~(MMCS_S | PMTS_S | TSTS_S)
        if macsr & MMCS_M:
            sr |= MMCS_S
        if macsr & PMTS_M and not self.get("MACIMR") & PMTS_M:     # PMTI = PMTS & !PMTIM (RM0090 Figure 370)
            sr |= PMTS_S
        sr = (sr & ~(0x3F << 17 | 7 << 20)) | (self._rps << 17) | (self._tps << 20)
        self.set("DMASR", sr)
        line = bool((sr & NIS and ier & NISE) or (sr & AIS and ier & AISE) or sr & (MMCS_S | PMTS_S | TSTS_S))
        if line != self._line and self._irqn is not None:
            self._line = line
            self.irq(self._irqn, line)

    def _update_w(self, blk, reg, old, new):
        self._store(reg, new)
        self._update()
        return None

    def _status(self, bits: int) -> None:
        self.set("DMASR", self.get("DMASR") | bits)
        self._update()

    def _macsr_value(self) -> int:
        v = self.get("MACSR") & ~(MMCS_M | MMCRS_M | MMCTS_M)
        rx = self.get("MMCRIR") & ~self.get("MMCRIMR")
        tx = self.get("MMCTIR") & ~self.get("MMCTIMR")
        if rx:
            v |= MMCRS_M | MMCS_M
        if tx:
            v |= MMCTS_M | MMCS_M
        return v                        # PMTS stays visible when masked: MACIMR.PMTIM only masks the interrupt

    def _macsr_read(self, blk, reg):
        return self._macsr_value()

    # ------------------------------------------------------------------ MDIO
    def _miiar_write(self, blk, reg, old, new):
        if old & MB:
            self._warn_once("mdiobusy", f"{self.name}: MACMIIAR written while MB=1 (MDIO transaction in progress): "
                            f"ignored")
            return old
        if not new & MB:
            return None
        cr = (new >> 2) & 7
        div = MDC_DIV.get(cr)
        if div is None:
            self._warn_once("cr", f"{self.name}: MACMIIAR.CR={cr} is reserved: MDC = HCLK/102 assumed")
            div = 102
        hz = self._hclk() / div
        if hz > 2.5e6 * 1.001:
            self._warn_once("mdc", f"{self.name}: MDC {hz / 1e6:.2f} MHz exceeds 2.5 MHz (MACMIIAR.CR={cr} for "
                            f"HCLK {self._hclk() / 1e6:.1f} MHz)")
        if self._mdc is not None:
            self._mdc.set(hz, True)
            self._mdc.active(64)
        self._mdio_ev = self.chip.sched.at_cycles(self.chip.now_cycles() + 64 * div, self._mdio_done)
        return None

    def _miidr_write(self, blk, reg, old, new):
        if self.get("MACMIIAR") & MB:
            self._warn_once("mdiobusy", f"{self.name}: MACMIIDR written while MB=1 (MDIO transaction in progress): "
                            f"ignored")
            return old
        return None

    def _mdio_done(self) -> None:
        self._mdio_ev = None
        ar = self.get("MACMIIAR")
        pa, mr = (ar >> 11) & 0x1F, (ar >> 6) & 0x1F
        wired = not self._missing(("ETH_MDC", "ETH_MDIO"))
        if not wired:
            self._warn_once("mdiopins", f"{self.name}: MDIO transaction with ETH_MDC/ETH_MDIO not muxed to pins: the "
                            f"PHY does not see it (reads 0xFFFF)")
        if ar & MW:
            if wired:
                self.phy.mdio_write(pa, mr, self.get("MACMIIDR") & 0xFFFF)
        else:
            self.set("MACMIIDR", self.phy.mdio_read(pa, mr) if wired else 0xFFFF)
        self.set("MACMIIAR", ar & ~MB)

    # ------------------------------------------------------------------ MAC registers
    def _maccr_write(self, blk, reg, old, new):
        self._store(reg, new)
        if new & (TE | RE) and self.chip is not None and self._hclk() < 25e6:
            self._warn_once("hclk", f"{self.name}: HCLK {self._hclk() / 1e6:.2f} MHz is below the 25 MHz the Ethernet "
                            f"MAC needs (RM0090 33.2)")
        if (new & ~old) & TE:
            self._tx_resume()
        return None

    def _macfcr_write(self, blk, reg, old, new):
        if new & FCB and not old & FCB:
            if not self.get("MACCR") & DM:
                self.emit("info", block=self.name, msg=f"{self.name}: back pressure (MACFCR.BPA) in half duplex is "
                          f"not modelled")
                return new & ~FCB
            self._store(reg, new)
            self._send_pause()
            return None
        return (new & ~FCB) | (old & FCB)

    def _send_pause(self) -> None:
        if self._tx_busy or self.chip is None:
            if self.chip is not None and self._fcb_ev is None:
                self._fcb_ev = self.chip.sched.at_ns(self.chip.now_ns() + 1000, self._fcb_retry)
            return
        pt = self.get("MACFCR") >> 16
        a0 = self._mac_addr(0)
        frame = PAUSE_DA + a0 + b"\x88\x08" + struct.pack("!HH", 1, pt)
        frame += bytes(60 - len(frame))
        self._start_wire(frame + fcs(frame), None)

    def _fcb_retry(self) -> None:
        self._fcb_ev = None
        if self.get("MACFCR") & FCB:
            self._send_pause()

    def _pmt_read(self, blk, reg):
        v = self.get("MACPMTCSR")
        if v & (MPR | WFR):
            self.set("MACPMTCSR", v & ~(MPR | WFR))
            self.set("MACSR", self.get("MACSR") & ~PMTS_M)
            self._update()
        return v

    def _pmt_write(self, blk, reg, old, new):
        if new & WFE and not old & WFE:
            self._warn_once("wfe", f"{self.name}: remote wake-up frame detection (MACPMTCSR.WFE) is not modelled")
        if new & WFFRPR:
            new &= ~WFFRPR
        return new

    def _ptp_write(self, blk, reg, old, new):
        if new & 1 and not old & 1:
            self._warn_once("ptp", f"{self.name}: PTP time stamping (PTPTSCR.TSE) is not modelled: registers are "
                            f"storage, descriptor time stamps read 0")
        return None

    def _mac_addr(self, i: int) -> bytes:
        hi, lo = self.get(f"MACA{i}HR"), self.get(f"MACA{i}LR")
        return lo.to_bytes(4, "little") + (hi & 0xFFFF).to_bytes(2, "little")

    # ------------------------------------------------------------------ MMC
    def _mmc_inc(self, name: str, tx: bool, bit: int) -> None:
        cr = self.get("MMCCR")
        if cr & 8:                                              # MCF: frozen
            return
        v = self.get(name)
        if v == 0xFFFFFFFF and cr & 2:                         # CSR: stop at the maximum
            return
        nv = (v + 1) & 0xFFFFFFFF
        self.set(name, nv)
        if nv in (0x80000000, 0xFFFFFFFF, 0):
            r = "MMCTIR" if tx else "MMCRIR"
            self.set(r, self.get(r) | bit)
            self._update()

    def _counter_read(self, name: str):
        bits = {"MMCTGFSCCR": ("MMCTIR", 1 << 14), "MMCTGFMSCCR": ("MMCTIR", 1 << 15), "MMCTGFCR": ("MMCTIR", 1 << 21),
                "MMCRFCECR": ("MMCRIR", 1 << 5), "MMCRFAECR": ("MMCRIR", 1 << 6), "MMCRGUFCR": ("MMCRIR", 1 << 17)}
        ireg, bit = bits[name]

        def rd(blk, reg):
            v = self.get(name)
            if self.get(ireg) & bit:
                self.set(ireg, self.get(ireg) & ~bit)
                self._update()
            if self.get("MMCCR") & 4:                          # ROR: reset on read
                self.set(name, 0)
            return v
        return rd

    def _mmccr_write(self, blk, reg, old, new):
        names = ("MMCTGFSCCR", "MMCTGFMSCCR", "MMCTGFCR", "MMCRFCECR", "MMCRFAECR", "MMCRGUFCR")
        if new & 1:
            for n in names:
                self.set(n, 0)
            new &= ~1
        if new & (1 << 4):                                       # MCP: preset
            v = 0xFFFFFFF0 if new & (1 << 5) else 0x7FFFFFF0
            for n in names:
                self.set(n, v)
            new &= ~(1 << 4)
        return new

    # ------------------------------------------------------------------ DMA registers
    def _dmabmr_write(self, blk, reg, old, new):
        if new & SR_RESET:
            self._latch_iface()                                 # the software reset samples MII_RMII_SEL again
            self._store(reg, new)
            if self._sr_ev is not None:
                self.chip.sched.cancel(self._sr_ev)
                self._sr_ev = None
            self._regs_reset()
            self.set("DMABMR", self.get("DMABMR") | SR_RESET)
            if self._phy_clock():
                self._sr_ev = self.chip.sched.at_cycles(self.chip.now_cycles() + 32, self._sr_done)
            else:
                self._sr_ev = None
                need = "ETH_RMII_REF_CLK" if self._iface_now() == "rmii" else "ETH_MII_TX_CLK and ETH_MII_RX_CLK"
                self._warn_once("srclk", f"{self.name}: DMABMR.SR stays set: the software reset needs the PHY clock "
                                f"on {need} (pin not muxed)")
            return self.get("DMABMR")
        return new

    def _dmabmr_read(self, blk, reg):
        v = self.get("DMABMR")
        if v & SR_RESET and self._sr_ev is None and self.chip is not None and self._phy_clock(warn=False):
            self._sr_ev = self.chip.sched.at_cycles(self.chip.now_cycles() + 32, self._sr_done)   # clock appeared
        return v

    def _sr_done(self) -> None:
        self._sr_ev = None
        self.set("DMABMR", self.get("DMABMR") & ~SR_RESET)

    def _tdlar_write(self, blk, reg, old, new):
        if self._tps != TPS_STOP:
            self._warn_once("tdlar", f"{self.name}: DMATDLAR written while the transmit process runs: ignored")
            return old
        self._tx_cur = new & ~3
        return new & ~3

    def _rdlar_write(self, blk, reg, old, new):
        if self._rps != RPS_STOP:
            self._warn_once("rdlar", f"{self.name}: DMARDLAR written while the receive process runs: ignored")
            return old
        self._rx_cur = new & ~3
        return new & ~3

    def _tpdr_write(self, blk, reg, old, new):
        if self._tps == TPS_SUSP:
            self._tps = TPS_FETCH
            self._tx_kick()
        return 0

    def _rpdr_write(self, blk, reg, old, new):
        if self._rps == RPS_SUSP:
            self._rps = RPS_FETCH
            self._rx_service()
        return 0

    def _dmasr_write(self, blk, reg, old, new):
        self._store(reg, new)
        self._update()
        return None

    def _dmaomr_write(self, blk, reg, old, new):
        if new & FTF:
            new &= ~FTF                                         # TX FIFO flush: immediate (the FIFO holds no frame)
        self._store(reg, new)
        rise, fall = new & ~old, old & ~new
        if rise & ST_TX:
            self._tps = TPS_FETCH
            self._tx_kick()
        elif fall & ST_TX:
            if not self._tx_busy:
                self._tx_stop()
        if rise & SR_RX:
            self._rps = RPS_FETCH
            self._rx_service()
        elif fall & SR_RX:
            self._rps = RPS_STOP
            self._status(RPSS)
        self._update()
        return None

    def _mfbocr_read(self, blk, reg):
        v = self.get("DMAMFBOCR")
        self.set("DMAMFBOCR", 0)
        return v

    def _missed(self, by_controller: bool) -> None:
        v = self.get("DMAMFBOCR")
        if by_controller:
            mfc = (v & 0xFFFF) + 1
            v = (v & ~0xFFFF) | (0x1FFFF if mfc > 0xFFFF else mfc) & 0x1FFFF
        else:
            mfa = ((v >> 17) & 0x7FF) + 1
            v = (v & ~(0xFFF << 17)) | ((0xFFF if mfa > 0x7FF else mfa) << 17)
        self.set("DMAMFBOCR", v)
        self.stats["missed"] += 1

    # ------------------------------------------------------------------ memory access
    def _check(self, addr: int, n: int) -> None:
        bus = self.chip.bus
        obj = bus.lookup(addr) if hasattr(bus, "lookup") else None
        if obj is not None and str(getattr(obj, "name", "")).upper().startswith("CCM"):
            raise _Fault(addr, "CCM data RAM is not reachable by the Ethernet DMA")

    def _rd32(self, addr: int) -> int:
        self._check(addr, 4)
        try:
            return self.chip.bus.read(addr, 4)
        except BusError as e:
            raise _Fault(addr, str(e)) from None

    def _wr32(self, addr: int, v: int) -> None:
        self._check(addr, 4)
        try:
            self.chip.bus.write(addr, 4, v & 0xFFFFFFFF)
        except BusError as e:
            raise _Fault(addr, str(e)) from None

    def _rdb(self, addr: int, n: int) -> bytes:
        if n <= 0:
            return b""
        self._check(addr, n)
        try:
            return bytes(self.chip.bus.read_bytes(addr, n))
        except BusError as e:
            raise _Fault(addr, str(e)) from None

    def _wrb(self, addr: int, data: bytes) -> None:
        if not data:
            return
        self._check(addr, len(data))
        try:
            self.chip.bus.write_bytes(addr, data)
        except BusError as e:
            raise _Fault(addr, str(e)) from None

    def _bus_error(self, e: _Fault, tx: bool) -> None:
        self.emit("warn", block=self.name, msg=f"{self.name}: DMA bus error at 0x{e.addr:08x} ({e.msg}): FBES set, "
                  f"transmit and receive processes stopped")
        self._tps = TPS_STOP
        self._rps = RPS_STOP
        sr = self.get("DMASR") & ~(7 << 23)
        sr |= (1 if tx else 0) << 25 | (1 << 23)            # EBS: error during data transfer by Tx/Rx DMA
        self.set("DMASR", sr | FBES)
        self._update()

    def _desc_step(self) -> int:
        bmr = self.get("DMABMR")
        return (32 if bmr & EDFE else 16) + 4 * ((bmr >> 2) & 0x1F)

    # ------------------------------------------------------------------ transmit
    def _tx_stop(self) -> None:
        self._tps = TPS_STOP
        self._tx_pending = None
        self._status(TPSS)

    def _tx_resume(self) -> None:
        if self._tx_pending is not None and not self._tx_busy:
            self._tx_send_pending()

    def _tx_kick(self) -> None:
        if self.chip is None or self._tx_busy or self._tx_ev is not None or self._tx_pending is not None:
            return
        if not self.get("DMAOMR") & ST_TX or self._tps in (TPS_STOP, TPS_SUSP):
            return
        try:
            got = self._tx_fetch()
        except _Fault as e:
            self._bus_error(e, True)
            return
        if got is None:
            self._tps = TPS_SUSP
            self._status(TBUS)
            return
        self._tx_pending = got
        self._tx_send_pending()

    def _tx_fetch(self) -> tuple | None:
        """Read the next frame from the descriptor list: (descriptor addresses, frame bytes, first TDES0, last
        TDES0, next descriptor) or None when the current descriptor is host-owned."""
        cur = self._tx_cur
        descs: list[int] = []
        data = bytearray()
        step = self._desc_step()
        first_d0 = None
        for _ in range(256):
            d0 = self._rd32(cur)
            if not d0 & T_OWN:
                if descs:
                    self._warn_once("txmid", f"{self.name}: transmit descriptor 0x{cur:08x} owned by the CPU in the "
                                    f"middle of a frame (LS not seen): frame postponed")
                return None
            d1, d2, d3 = self._rd32(cur + 4), self._rd32(cur + 8), self._rd32(cur + 12)
            if first_d0 is None:
                first_d0 = d0
                if not d0 & T_FS:
                    self._warn_once("txfs", f"{self.name}: transmit descriptor 0x{cur:08x} starts a frame without FS")
            descs.append(cur)
            data += self._rdb(d2, d1 & 0x1FFF)
            if not d0 & T_TCH:
                data += self._rdb(d3, (d1 >> 16) & 0x1FFF)
            if d0 & T_TER:                                      # TER takes precedence over TCH (RM0090 TDES0)
                nxt = self.get("DMATDLAR")
            elif d0 & T_TCH:
                nxt = d3 & ~3
            else:
                nxt = cur + step
            self.set("DMACHTDR", cur)
            self.set("DMACHTBAR", d2)
            cur = nxt
            if d0 & T_LS:
                return descs, data, first_d0, d0, cur
        self._warn_once("txloop", f"{self.name}: no LS in 256 transmit descriptors: frame dropped")
        return None

    def _tx_send_pending(self) -> None:
        descs, data, d0f, d0l, nxt = self._tx_pending
        if not self.get("MACCR") & TE:
            self._tps = TPS_WAIT                                 # waits for the MAC transmitter
            self._update()
            return
        now = self.chip.now_ns()
        t0 = max(now, self._next_tx_t, self._pause_until)
        if t0 > now + 0.5:
            self._tps = TPS_WAIT
            self._tx_ev = self.chip.sched.at_ns(t0, self._tx_deferred)
            self._update()
            return
        self._tx_pending = None
        frame = bytearray(data)
        err = 0
        cic = (d0f >> 22) & 3
        if cic:
            err = checksum_insert(frame, cic)
        crc = not d0f & T_DC
        if not d0f & T_DP and len(frame) < 60:
            frame += bytes(60 - len(frame))
            crc = True                                          # RM0090 TDES0 DP: padding adds the CRC despite DC
        wire = bytes(frame) + (fcs(frame) if crc else b"")
        self._tx_cur = nxt
        self._tps = TPS_WAIT
        self._start_wire(wire, (descs, d0l, err))

    def _tx_deferred(self) -> None:
        self._tx_ev = None
        if self._tx_pending is not None:
            self._tx_send_pending()

    def _start_wire(self, wire: bytes, close: tuple | None) -> None:
        rate = self._rate()
        t0 = self.chip.now_ns()
        t_end = t0 + wire_ns(len(wire), rate)
        self._tx_busy = True
        self._tx_ev = self.chip.sched.at_ns(t_end, self._tx_done, wire, rate, t0, t_end, close)
        self._update()

    def _tx_done(self, wire: bytes, rate: float, t0: float, t_end: float, close: tuple | None) -> None:
        self._tx_ev = None
        self._tx_busy = False
        cr = self.get("MACCR")
        status = 0
        lost = None
        if not self._gate(26):
            self._warn_once("txgate", f"{self.name}: frame transmitted with RCC_AHB1ENR.ETHMACTXEN clear: lost")
            lost = "gate"
        elif cr & LM:
            self._mac_rx(wire, rate, loop=True)
        else:
            miss = self._missing(TX_SIG[self._iface_now()])
            if miss:
                self._warn_once("txpins", f"{self.name}: {self._iface_now().upper()} transmit signals not muxed "
                                f"({', '.join(miss)}): frames do not reach the PHY")
                lost = "pins"
            else:
                lost = self.phy.transmit(wire, rate, t0, t_end)
            if lost in ("nolink", "nocable") and not cr & DM and not cr & CSD:
                status |= T_LCA | T_NC
            if lost is None and not cr & DM and not cr & ROD:
                self._mac_rx(wire, rate, loop=True)               # half duplex: the own frame comes back
        if lost:
            self.stats["lost"] += 1
        self.stats["tx"] += 1
        self.tx_frames.append((t_end, wire[:-4]))
        ifg = 96 - 8 * ((cr >> 17) & 7)
        self._next_tx_t = t_end + ifg * 1e9 / rate
        if close is None:                                      # pause frame
            self.set("MACFCR", self.get("MACFCR") & ~FCB)
            if not status & (T_LCA | T_NC):
                self._mmc_inc("MMCTGFCR", True, 1 << 21)
        else:
            descs, d0l, err = close
            status |= err
            if status & (T_IHE | T_IPE | T_LCA | T_NC):
                status |= T_ES
            try:
                for a in descs[:-1]:
                    self._wr32(a, self._rd32(a) & ~T_OWN)
                last = descs[-1]
                self._wr32(last, (self._rd32(last) & ~(T_OWN | T_STATUS)) | status)
            except _Fault as e:
                self._bus_error(e, True)
                return
            if not status & (T_LCA | T_NC):                     # RM0090 33.5.7: only aborts make a frame bad
                self._mmc_inc("MMCTGFCR", True, 1 << 21)        # (IHE / IPE frames are still sent)
            if d0l & T_IC:
                self.set("DMASR", self.get("DMASR") | TS)
        if not self.get("DMAOMR") & ST_TX:
            self._tx_stop()
        elif self._tps != TPS_STOP:
            self._tps = TPS_FETCH
            nxt = self._next_tx_t
            if nxt > self.chip.now_ns():
                self._tx_ev = self.chip.sched.at_ns(nxt, self._tx_after_ifg)
            else:
                self._tx_kick()
        self._update()

    def _tx_after_ifg(self) -> None:
        self._tx_ev = None
        self._tx_kick()
        self._update()

    # ------------------------------------------------------------------ receive: MAC
    def _filter(self, f: bytes) -> tuple[bool, bool, bool]:
        """(pass, DA filter fail, SA filter fail) per MACFFR (RM0090 33.5.x)."""
        ffr = self.get("MACFFR")
        da, sa = f[0:6], f[6:12]
        perfect, perfect_mc, sa_hit = False, False, False
        for i in range(4):
            hi = self.get(f"MACA{i}HR")
            if i and not hi >> 31 & 1:
                continue
            addr = self._mac_addr(i)
            mbc = (hi >> 24) & 0x3F if i else 0
            is_sa = bool(i and hi >> 30 & 1)
            target = sa if is_sa else da
            if all(mbc >> k & 1 or target[k] == addr[k] for k in range(6)):
                if is_sa:
                    sa_hit = True
                else:
                    perfect = True
                    perfect_mc = perfect_mc or i > 0    # multicast: MAC address registers 1-3 only (RM0090 33.5.5)
        bcast = da == BROADCAST
        mcast = bool(da[0] & 1) and not bcast
        hidx = hash_index(da)
        ht = (self.get("MACHTHR") << 32) | self.get("MACHTLR")
        hash_hit = bool(ht >> hidx & 1)
        if bcast:
            da_ok = not ffr & BFD
        elif mcast:
            if ffr & PAM:
                da_ok = True
            elif ffr & HM:
                da_ok = hash_hit or (bool(ffr & HPF) and perfect_mc)
            else:
                da_ok = perfect_mc
            if ffr & DAIF and not ffr & PAM:
                da_ok = not da_ok
        else:
            if ffr & HU:
                da_ok = hash_hit or (bool(ffr & HPF) and perfect)
            else:
                da_ok = perfect
            if ffr & DAIF:
                da_ok = not da_ok
        sa_fail = False
        if ffr & SAF_EN:
            sa_fail = not sa_hit
            if ffr & SAIF:
                sa_fail = not sa_fail
        ok = (da_ok and not sa_fail) or bool(ffr & (PM | RA))
        return ok, not da_ok, sa_fail

    def _mac_rx(self, wire: bytes, rate: float, *, loop: bool = False) -> None:
        if self.chip is None:
            return
        cr = self.get("MACCR")
        if not self._gate(25) or not self._gate(27):
            self._warn_once("rxgate", f"{self.name}: frame arriving with RCC_AHB1ENR.ETHMACEN/ETHMACRXEN clear: lost")
            return
        if not cr & RE:
            return
        if not loop:
            if abs(rate - self._rate()) > 1:
                self.stats["lost"] += 1
                self._warn_once("rxspeed", f"{self.name}: frame at {rate / 1e6:g} Mbit/s but MACCR.FES selects "
                                f"{self._rate() / 1e6:g} Mbit/s: not received")
                return
            miss = self._missing(RX_SIG[self._iface_now()])
            if miss:
                self.stats["lost"] += 1
                self._warn_once("rxpins", f"{self.name}: {self._iface_now().upper()} receive signals not muxed "
                                f"({', '.join(miss)}): frames from the PHY are lost")
                return
        f = wire[:-4] if len(wire) >= 4 else wire
        pmt = self.get("MACPMTCSR")
        if pmt & PD:
            if pmt & MPE and self._magic(f):
                self.set("MACPMTCSR", (pmt & ~PD) | MPR)
                self.set("MACSR", self.get("MACSR") | PMTS_M)
                self.emit("info", block=self.name, msg=f"{self.name}: magic packet received: wake-up")
                exti_input(self.chip, 19, True)
                exti_input(self.chip, 19, False)
                self._update()
            return
        crc_ok = fcs_ok(wire)
        if len(f) < 14:
            return
        et = int.from_bytes(f[12:14], "big")
        if et == 0x8808 and f[0:6] in (PAUSE_DA, self._mac_addr(0)) and crc_ok:
            if (f[0:6] == PAUSE_DA or self.get("MACFCR") & UPFD) and self.get("MACFCR") & RFCE \
                    and int.from_bytes(f[14:16], "big") == 1:
                quanta = int.from_bytes(f[16:18], "big")
                self._pause_until = self.chip.now_ns() + quanta * 512 * 1e9 / self._rate()
            pcf = (self.get("MACFFR") >> 6) & 3
            if pcf in (0, 1):
                return
        ok, afm, saf = self._filter(f)
        if not ok:
            self.stats["filtered"] += 1
            return
        if not crc_ok:
            self.stats["crc"] += 1
            self._mmc_inc("MMCRFCECR", False, 1 << 5)
            if not self.get("DMAOMR") & FEF:
                return
        runt = len(wire) < 64
        if runt and not self.get("DMAOMR") & FUGF:
            if crc_ok:
                self._missed(False)                             # DMAMFBOCR.MFA counts dropped runt frames
            return
        if crc_ok and not runt and not f[0] & 1:                # good unicast: no CRC error, not a runt (33.5.7)
            self._mmc_inc("MMCRGUFCR", False, 1 << 17)
        # status
        st = 0
        if afm:
            st |= R_AFM
        if saf:
            st |= R_SAF
        if not crc_ok:
            st |= R_CE
        if et == 0x8100:
            st |= R_VLAN
        type_frame = _l3(f)[0] >= 0x600
        data = wire
        if type_frame and cr & CSTF:
            data = f
        elif not type_frame and cr & APCS and et <= 1500:
            data = f[:14 + et]
        if type_frame:
            st |= R_FT
        ext = 0
        drop_csum = False
        if cr & IPCO and type_frame:
            ptype, ipv, herr, perr, checked = checksum_verify(bytes(f))
            enh = bool(self.get("DMABMR") & EDFE)
            if enh:
                # RDES0 FT keeps its plain meaning (Table 193 applies to normal descriptors only); ESA says RDES4
                # holds IP checksum status, so it is set for IPv4/IPv6 frames only (RM0090 33.6.8 RDES4)
                if ipv:
                    ext = ptype | (8 if herr else 0) | (16 if perr else 0) | (0 if (checked or herr) else 32)
                    ext |= 64 if ipv == 4 else 128
                    st |= 1                                     # ESA: RDES4 valid
            else:
                if ipv:
                    if not checked and not herr:
                        st = (st & ~R_FT) | R_PCE               # IPv4/6, payload check bypassed: 0 0 1
                    else:
                        st |= (R_IPHCE if herr else 0) | (R_PCE if perr else 0)
                else:
                    st = (st & ~R_FT) | R_IPHCE | R_PCE         # neither IPv4 nor IPv6: 0 1 1
            drop_csum = herr or perr
        if drop_csum and self.get("DMAOMR") & RSF and not self.get("DMAOMR") & DTCEFD:
            self.stats["lost"] += 1
            return
        if st & R_CE:
            st |= R_ES
        self._rx_enqueue(bytes(data), st, ext)

    def _magic(self, f: bytes) -> bool:
        mac = self._mac_addr(0)
        pat = b"\xff" * 6 + mac * 16
        return pat in bytes(f[12:])

    # ------------------------------------------------------------------ receive: DMA
    def _rx_enqueue(self, data: bytes, st: int, ext: int) -> None:
        if self._rps == RPS_SUSP:
            self._rps = RPS_FETCH                             # a new frame wakes a suspended receive process
        omr = self.get("DMAOMR")
        if self._rps != RPS_STOP and not self._rxq and self._rx_store(data, st, ext):
            return
        if self._rps == RPS_SUSP and not omr & DFRF:
            self._missed(True)                                 # no descriptor: flushed
            self._update()
            return
        if self._rxq_bytes + len(data) > RX_FIFO:
            self._missed(False)
            self._status(ROS)
            return
        self._rxq.append((data, st, ext))
        self._rxq_bytes += len(data)
        self._rx_service()

    def _rx_service(self) -> None:
        while self._rxq and self._rps not in (RPS_STOP, RPS_SUSP) and self.get("DMAOMR") & SR_RX:
            data, st, ext = self._rxq[0]
            if not self._rx_store(data, st, ext):
                break
            self._rxq.popleft()
            self._rxq_bytes -= len(data)
        if self._rps == RPS_FETCH and self.get("DMAOMR") & SR_RX:
            try:
                if not self._rd32(self._rx_cur) & R_OWN:
                    self._rx_unavailable()
                else:
                    self._rps = RPS_WAIT
            except _Fault as e:
                self._bus_error(e, False)
        self._update()

    def _rx_unavailable(self) -> None:
        self._rps = RPS_SUSP
        if self._rbus_armed:
            self._rbus_armed = False
            self.set("DMASR", self.get("DMASR") | RBUS)

    def _rx_store(self, data: bytes, st: int, ext: int) -> bool:
        """Write one frame to the receive descriptors; False if the first descriptor is host-owned."""
        if not self.get("DMAOMR") & SR_RX or self._rps == RPS_STOP:
            return False
        cur = self._rx_cur
        step = self._desc_step()
        enh = bool(self.get("DMABMR") & EDFE)
        off = 0
        n = len(data)
        first = True
        try:
            for _ in range(256):
                d0 = self._rd32(cur)
                if not d0 & R_OWN:
                    if first:
                        self._rx_unavailable()
                        self._update()
                        return False
                    # ran out of descriptors in the middle of the frame: the rest is lost (DE in the last one)
                    self._wr32(prev, (self._rd32(prev) & ~0x3FFF0000) | R_LS | R_DE | R_ES | (min(off, 0x3FFF) << 16))
                    self._rx_cur = cur
                    self._rx_unavailable()
                    self._update()
                    return True
                d1, d2, d3 = self._rd32(cur + 4), self._rd32(cur + 8), self._rd32(cur + 12)
                bufs = [(d2, d1 & 0x1FFF)]
                if not d1 & R_RCH:
                    bufs.append((d3, (d1 >> 16) & 0x1FFF))
                for addr, size in bufs:
                    if size and off < n:
                        k = min(size, n - off)
                        self._wrb(addr, data[off:off + k])
                        off += k
                        self.set("DMACHRBAR", addr)
                last = off >= n
                v = (min(off, 0x3FFF) << 16) | (R_FS if first else 0)
                if last:
                    v |= R_LS | st
                    if enh:
                        self._wr32(cur + 16, ext)
                        if self.get("PTPTSCR") & 1:             # time stamps only with TSE; else RDES6/7 unchanged
                            self._wr32(cur + 24, 0)
                            self._wr32(cur + 28, 0)
                self._wr32(cur, v)
                self.set("DMACHRDR", cur)
                if d1 & R_RER:                                  # RER takes precedence over RCH (RM0090 RDES1)
                    nxt = self.get("DMARDLAR")
                elif d1 & R_RCH:
                    nxt = d3 & ~3
                else:
                    nxt = cur + step
                prev = cur
                cur = nxt
                first = False
                if last:
                    self._rx_cur = cur
                    self._rbus_armed = True
                    self.stats["rx"] += 1
                    self.rx_frames.append((self.chip.now_ns(), bytes(data)))
                    if not d1 & R_DIC:
                        self.set("DMASR", self.get("DMASR") | RS)
                    # RM0090 33.6.8 step 8: the engine checks the next descriptor at once; host-owned -> RBUS and
                    # Suspended (the previous descriptor was DMA-owned, so RBUS is reported)
                    if self._rd32(cur) & R_OWN:
                        self._rps = RPS_WAIT
                    else:
                        self._rx_unavailable()
                    self._update()
                    return True
        except _Fault as e:
            self._bus_error(e, False)
            return True
        return True
