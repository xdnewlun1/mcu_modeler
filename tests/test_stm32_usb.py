"""stm32.otg_fs / stm32.otg_hs (DWC2 device mode, RM0090 ch. 34/35) on a chipem/usbbus.py cable: register reset
values (SVD), core reset / FIFO flush, RX FIFO entries for SETUP / OUT / completion, TX FIFOs and IN packets, toggles,
NAK/STALL, interrupts, session (VBUS sensing / NOVBUSSENS), soft disconnect, the 48 MHz clock check, pins; and the
CDC-ACM samples end to end (bare metal and ST's HAL + USB Device Library)."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from chipem.regtable import regs_for
from chipem.session import Session
from chipem.usbbus import Transaction, UsbBus
from test_stm32 import RCC, desc, spin_chip, warns  # noqa: F401  (desc: fixture)

ROOT = Path(__file__).resolve().parents[1]
BARE = ROOT / "firmware/samples_stm32/build/usb_cdc.elf"
HAL = ROOT / "firmware/samples_stm32_hal/build/usb_cdc_hal.elf"
FS, HS = 0x50000000, 0x40040000
GPIOA = 0x40020000
GOTGCTL, GOTGINT, GAHBCFG, GUSBCFG, GRSTCTL, GINTSTS, GINTMSK = 0x0, 0x4, 0x8, 0xC, 0x10, 0x14, 0x18
GRXSTSR, GRXSTSP, GRXFSIZ, DIEPTXF0, GCCFG, CID, GSNPSID = 0x1C, 0x20, 0x24, 0x28, 0x38, 0x3C, 0x40
DCFG, DCTL, DSTS, DIEPMSK, DOEPMSK, DAINT, DAINTMSK, DIEPEMPMSK = 0x800, 0x804, 0x808, 0x810, 0x814, 0x818, 0x81C, 0x834
PCGCCTL = 0xE00
RXFLVL, USBSUSP, USBRST, ENUMDNE, IEPINT, OEPINT, SRQINT, WKUINT, OTGINT = (1 << 4, 1 << 11, 1 << 12, 1 << 13,
                                                                          1 << 18, 1 << 19, 1 << 30, 1 << 31, 1 << 2)
EPENA, CNAK, SNAK, STALL, USBAEP, NAKSTS = 1 << 31, 1 << 26, 1 << 27, 1 << 21, 1 << 15, 1 << 17
PWRDWN, VBUSBSEN, NOVBUSSENS = 1 << 16, 1 << 19, 1 << 21
FAST = dict(debounce_ns=1e6, reset_ns=1e6, reset_recovery_ns=1e6, set_address_recovery_ns=0.5e6)


def DIEPCTL(n): return 0x900 + 0x20 * n
def DIEPINT(n): return 0x908 + 0x20 * n
def DIEPTSIZ(n): return 0x910 + 0x20 * n
def DTXFSTS(n): return 0x918 + 0x20 * n
def DOEPCTL(n): return 0xB00 + 0x20 * n
def DOEPINT(n): return 0xB08 + 0x20 * n
def DOEPTSIZ(n): return 0xB10 + 0x20 * n


def w(c, a, v):
    c.bus.write(a, 4, v & 0xFFFFFFFF)


def r(c, a):
    return c.bus.read(a, 4)


def usb_chip(desc, *, pll=True, pins=True, base=FS):
    c = spin_chip(desc, clock_hz=16e6)
    w(c, RCC + 0x30, r(c, RCC + 0x30) | 1 | (1 << 29))       # GPIOA, OTGHSEN
    w(c, RCC + 0x34, r(c, RCC + 0x34) | (1 << 7))            # OTGFSEN
    if pll:                                                  # HSE 8 MHz / 8 * 336 / 7 = 48 MHz PLL48CLK
        w(c, RCC + 0x04, 8 | (336 << 6) | (1 << 22) | (7 << 24))
        w(c, RCC, r(c, RCC) | (1 << 16) | (1 << 24))
    if pins:
        afrh = r(c, GPIOA + 0x24) | (10 << 12) | (10 << 16)  # PA11/PA12 AF10
        w(c, GPIOA + 0x24, afrh)
        w(c, GPIOA, r(c, GPIOA) | (2 << 22) | (2 << 24))
    return c


def core_init(c, b=FS, *, vbus_sense=False):
    w(c, b + GRSTCTL, 1)
    assert r(c, b + GRSTCTL) & 1 == 0 and r(c, b + GRSTCTL) >> 31
    w(c, b + GCCFG, PWRDWN | (VBUSBSEN if vbus_sense else NOVBUSSENS))
    w(c, b + GRXFSIZ, 64)
    w(c, b + DIEPTXF0, (16 << 16) | 64)
    w(c, b + 0x104, (32 << 16) | 80)                          # DIEPTXF1
    w(c, b + GINTSTS, 0xBFFFFFFF)
    w(c, b + GINTMSK, RXFLVL | USBRST | ENUMDNE | IEPINT | OEPINT | USBSUSP | WKUINT | SRQINT | OTGINT)
    w(c, b + GAHBCFG, 1)
    w(c, b + DCFG, (r(c, b + DCFG) & ~3) | 3)


def run(c, ns):
    c.run_for(ns) if hasattr(c, "run_for") else None


def setup_pkt(bm, req, value=0, index=0, length=0):
    return struct.pack("<BBHHH", bm, req, value, index, length)


class Bus:
    """A bus with the OTG core attached and no host: tests send transactions by hand."""

    def __init__(self, c, b=FS, name="OTG_FS"):
        self.c, self.b = c, b
        self.otg = c.peripherals[name]
        self.bus = UsbBus("T")
        self.bus.vbus = True
        self.bus.attach(self.otg)

    def setup(self, data, addr=0):
        return self.otg.usb_setup(self.bus, Transaction("SETUP", addr, 0, "DATA0", data, self.c.now_ns()))

    def out(self, ep, data, pid="DATA0", addr=0):
        return self.otg.usb_out(self.bus, Transaction("OUT", addr, ep, pid, data, self.c.now_ns()))

    def inn(self, ep, addr=0):
        x = Transaction("IN", addr, ep, None, b"", self.c.now_ns())
        res = self.otg.usb_in(self.bus, x)
        if isinstance(res, tuple):
            self.otg.usb_in_done(self.bus, x, True, self.c.now_ns())
        return res

    def pop(self):
        st = r(self.c, self.b + GRXSTSP)
        n = (st >> 4) & 0x7FF
        words = [r(self.c, self.b + 0x1000) for _ in range((n + 3) // 4)]
        data = b"".join(x.to_bytes(4, "little") for x in words)[:n]
        return (st >> 17) & 0xF, st & 0xF, n, (st >> 15) & 3, data


# ------------------------------------------------------------------------------------------------ registers
def test_reset_values_match_svd(desc):
    c = spin_chip(desc)
    svd = {s["name"]: s["reset"] for name in ("OTG_FS_GLOBAL", "OTG_FS_DEVICE")
           for s in regs_for(name, str(ROOT / "chips/stm32f405_regs.yaml")).reg_specs()}
    base_dev = {"FS_DCFG": DCFG, "FS_DCTL": DCTL, "FS_DSTS": DSTS, "FS_DAINTMSK": DAINTMSK, "DVBUSDIS": 0x828,
                "DVBUSPULSE": 0x82C, "DOEPCTL1": DOEPCTL(1), "DIEPTSIZ0": DIEPTSIZ(0)}
    base_glb = {"FS_GOTGINT": GOTGINT, "FS_GAHBCFG": GAHBCFG, "FS_GRSTCTL": GRSTCTL, "FS_GINTMSK": GINTMSK,
                "FS_GCCFG": GCCFG, "FS_CID": CID, "FS_HPTXFSIZ": 0x100, "FS_DIEPTXF1": 0x104, "FS_GRXFSIZ": GRXFSIZ}
    for name, off in {**base_glb, **base_dev}.items():
        exp = svd[name]
        if name == "FS_GRSTCTL":
            exp |= 1 << 31                                   # AHBIDL: the AHB master is idle
        assert r(c, FS + off) == exp, name
    assert r(c, FS + GUSBCFG) == 0x00000A40                  # SVD 0xA00 + PHYSEL "always 1" (RM0090 34.16.4)
    assert r(c, FS + GSNPSID) == 0x4F54281A                  # core 2.81a (the HAL tests > 3.00a)
    assert r(c, FS + GINTSTS) & 1 == 0                       # CMOD: device (ID pin pulled up)
    assert r(c, FS + DOEPCTL(0)) & USBAEP and r(c, FS + DIEPCTL(0)) & USBAEP
    assert r(c, HS + GSNPSID) == 0x4F54281A and r(c, HS + GUSBCFG) & 0x40 == 0
    assert set(c.usb_blocks()) == {"OTG_HS", "OTG_FS"} and c.find_usb("fs") == "OTG_FS"
    assert c.find_usb("OTG_HS_GLOBAL") == "OTG_HS" and c.find_usb(None) == "OTG_HS"


def test_core_soft_reset_keeps_config_and_flushes(desc):
    c = usb_chip(desc)
    core_init(c)
    t = Bus(c)
    assert t.setup(setup_pkt(0x80, 6, 0x100, 0, 18)) == "ACK"
    assert r(c, FS + GINTSTS) & RXFLVL
    w(c, FS + GRSTCTL, 1)
    assert r(c, FS + GINTSTS) & RXFLVL == 0
    assert r(c, FS + GRXFSIZ) == 64 and r(c, FS + GCCFG) & PWRDWN
    assert r(c, FS + GINTMSK) & USBRST


def test_forced_mode_takes_25ms(desc):
    c = usb_chip(desc)
    w(c, FS + GUSBCFG, r(c, FS + GUSBCFG) | (1 << 29))       # FHMOD
    assert r(c, FS + GINTSTS) & 1 == 0
    c.run_for(26e6)
    assert r(c, FS + GINTSTS) & 1 == 1
    assert any("host mode" in m for m in warns(c))
    w(c, FS + GUSBCFG, (r(c, FS + GUSBCFG) & ~(1 << 29)) | (1 << 30))
    c.run_for(26e6)
    assert r(c, FS + GINTSTS) & 1 == 0


def test_setup_rx_fifo_entries_and_stup(desc):
    c = usb_chip(desc)
    core_init(c)
    t = Bus(c)
    w(c, FS + DOEPMSK, 0x0B)
    w(c, FS + DAINTMSK, 0x10001)
    w(c, FS + DOEPTSIZ(0), (3 << 29) | (1 << 19) | 24)
    w(c, FS + DIEPCTL(0), r(c, FS + DIEPCTL(0)) | STALL)
    pkt = setup_pkt(0x80, 6, 0x100, 0, 18)
    assert t.setup(pkt) == "ACK"
    assert r(c, FS + DIEPCTL(0)) & STALL == 0                # cleared by the SETUP
    assert r(c, FS + DIEPCTL(0)) & NAKSTS and r(c, FS + DOEPCTL(0)) & NAKSTS
    assert (r(c, FS + DOEPTSIZ(0)) >> 29) & 3 == 2           # STUPCNT 3 -> 2
    assert r(c, FS + GRXSTSR) == r(c, FS + GRXSTSR)          # peek does not pop
    assert t.pop() == (6, 0, 8, 0, pkt)                      # SETUP data, DATA0
    assert r(c, FS + GINTSTS) & RXFLVL == 0
    assert r(c, FS + DOEPINT(0)) & 8 == 0                    # STUP waits for the setup stage to end
    assert t.inn(0) == "NAK"                                 # data stage IN token, nothing in the FIFO
    st, ep, n, _, _ = t.pop()
    assert (st, ep, n) == (4, 0, 0)                          # SETUP stage done
    assert r(c, FS + DOEPINT(0)) & 8
    assert r(c, FS + GINTSTS) & OEPINT and r(c, FS + DAINT) == 1 << 16
    assert 67 + 16 in c.arch.asserted                        # OTG_FS line
    w(c, FS + DOEPINT(0), 8)
    assert r(c, FS + GINTSTS) & OEPINT == 0


def test_in_packets_from_tx_fifo_with_toggles(desc):
    c = usb_chip(desc)
    core_init(c)
    t = Bus(c)
    t.setup(setup_pkt(0x80, 6, 0x100, 0, 18))
    t.pop()
    data = bytes(range(18))
    w(c, FS + DIEPTSIZ(0), (1 << 19) | 18)
    w(c, FS + DIEPCTL(0), r(c, FS + DIEPCTL(0)) | EPENA | CNAK)
    assert r(c, FS + DTXFSTS(0)) == 16
    for i in range(0, 16, 4):
        w(c, FS + 0x1000, int.from_bytes(data[i:i + 4], "little"))
    assert t.inn(0) == "NAK"                                 # 16 of 18 bytes: the packet is not complete
    assert r(c, FS + DIEPINT(0)) & 0x10                      # ITTXFE
    w(c, FS + 0x1000, int.from_bytes(data[16:18] + b"\0\0", "little"))
    assert r(c, FS + DTXFSTS(0)) == 11
    assert t.inn(0) == ("DATA1", data)                       # first data packet after SETUP: DATA1
    assert r(c, FS + DIEPINT(0)) & 1 and r(c, FS + DIEPCTL(0)) & EPENA == 0
    assert r(c, FS + DTXFSTS(0)) == 16 and r(c, FS + DIEPTSIZ(0)) == 0
    # bulk IN EP1: DPID toggles, SD0PID resets
    w(c, FS + DIEPCTL(1), 64 | (2 << 18) | (1 << 22) | USBAEP | (1 << 28))
    for k in range(2):
        w(c, FS + DIEPTSIZ(1), (1 << 19) | 4)
        w(c, FS + DIEPCTL(1), r(c, FS + DIEPCTL(1)) | EPENA | CNAK)
        w(c, FS + 0x2000, 0x64636261)
        assert t.inn(1) == ("DATA0" if k == 0 else "DATA1", b"abcd")
    w(c, FS + DIEPCTL(1), r(c, FS + DIEPCTL(1)) | STALL)
    assert t.inn(1) == "STALL"
    w(c, FS + DIEPCTL(1), (r(c, FS + DIEPCTL(1)) & ~STALL) | (1 << 28))
    assert t.inn(1) == "NAK"                                 # not enabled


def test_out_transfer_completion_and_toggle(desc):
    c = usb_chip(desc)
    core_init(c)
    t = Bus(c)
    w(c, FS + DOEPCTL(1), 64 | (2 << 18) | USBAEP | (1 << 28))
    assert t.out(1, b"x") == "NAK"                           # not enabled
    w(c, FS + DOEPTSIZ(1), (2 << 19) | 128)
    w(c, FS + DOEPCTL(1), r(c, FS + DOEPCTL(1)) | EPENA | CNAK)
    assert t.out(1, b"A" * 64, "DATA0") == "ACK"
    assert t.out(1, b"B" * 64, "DATA0") == "ACK"              # wrong toggle: acknowledged and dropped
    assert t.out(1, b"C" * 10, "DATA1") == "ACK"              # short packet ends the transfer
    assert r(c, FS + DOEPCTL(1)) & EPENA == 0
    assert t.pop() == (2, 1, 64, 0, b"A" * 64)
    assert t.pop() == (2, 1, 10, 2, b"C" * 10)
    assert r(c, FS + DOEPINT(1)) & 1 == 0
    assert t.pop()[:3] == (3, 1, 0)                          # OUT transfer completed -> XFRC on pop
    assert r(c, FS + DOEPINT(1)) & 1
    assert (r(c, FS + DOEPTSIZ(1)) >> 19) & 0x3FF == 0 and r(c, FS + DOEPTSIZ(1)) & 0x7FFFF == 54
    assert t.out(1, b"D", "DATA0") == "NAK"                   # disabled after completion


def test_rx_fifo_full_naks(desc):
    c = usb_chip(desc)
    core_init(c)
    w(c, FS + GRXFSIZ, 20)
    t = Bus(c)
    w(c, FS + DOEPCTL(1), 64 | (2 << 18) | USBAEP | (1 << 28))
    w(c, FS + DOEPTSIZ(1), (8 << 19) | 512)
    w(c, FS + DOEPCTL(1), r(c, FS + DOEPCTL(1)) | EPENA | CNAK)
    assert t.out(1, b"A" * 64, "DATA0") == "ACK"              # 17 words
    assert t.out(1, b"B" * 64, "DATA1") == "NAK"              # no room for another packet
    t.pop()
    assert t.out(1, b"B" * 64, "DATA1") == "ACK"


def test_global_out_nak(desc):
    c = usb_chip(desc)
    core_init(c)
    t = Bus(c)
    w(c, FS + DOEPCTL(1), 64 | (2 << 18) | USBAEP | (1 << 28))
    w(c, FS + DOEPTSIZ(1), (1 << 19) | 64)
    w(c, FS + DOEPCTL(1), r(c, FS + DOEPCTL(1)) | EPENA | CNAK)
    w(c, FS + DCTL, r(c, FS + DCTL) | (1 << 9))              # SGONAK
    assert r(c, FS + GINTSTS) & (1 << 7) == 0
    assert t.pop()[0] == 1                                   # global OUT NAK pattern
    assert r(c, FS + GINTSTS) & (1 << 7) and r(c, FS + DCTL) & 8
    assert t.out(1, b"x") == "NAK"
    w(c, FS + DCTL, r(c, FS + DCTL) | (1 << 10))             # CGONAK
    assert t.out(1, b"x") == "ACK"


def test_address_change_takes_effect_after_status(desc):
    c = usb_chip(desc)
    core_init(c)
    t = Bus(c)
    t.setup(setup_pkt(0, 5, 9))
    w(c, FS + DCFG, (r(c, FS + DCFG) & ~0x7F0) | (9 << 4))    # the ST stack programs DAD in the SETUP stage
    w(c, FS + DIEPTSIZ(0), 1 << 19)
    w(c, FS + DIEPCTL(0), r(c, FS + DIEPCTL(0)) | EPENA | CNAK)
    assert t.inn(0, addr=0) == ("DATA1", b"")               # status stage still at address 0
    assert t.inn(0, addr=0) is None                          # then only the new address answers
    assert t.inn(0, addr=9) == "NAK"


def test_session_vbus_sensing_and_soft_disconnect(desc):
    c = usb_chip(desc)
    core_init(c, vbus_sense=True)
    otg = c.peripherals["OTG_FS"]
    bus = UsbBus("U")
    bus.attach(otg)
    host = bus.add_host(**FAST)
    assert c.pins.get("PA9").external == "H"                 # the cable drives VBUS onto PA9
    assert r(c, FS + GINTSTS) & SRQINT                       # new session
    assert r(c, FS + GOTGCTL) & (1 << 19)                    # BSVLD
    assert bus.state == "attached"
    w(c, FS + DCTL, r(c, FS + DCTL) | 2)                     # SDIS: soft disconnect
    assert bus.state == "detached"
    w(c, FS + DCTL, r(c, FS + DCTL) & ~2)
    assert bus.state == "attached"
    host.unplug()
    assert r(c, FS + GOTGINT) & 4 and r(c, FS + GINTSTS) & OTGINT   # SEDET
    assert bus.state == "detached" and c.pins.get("PA9").external is None


def test_no_pins_no_connection_and_no_clock_no_answer(desc):
    c = usb_chip(desc, pins=False)
    core_init(c)
    bus = UsbBus("U")
    bus.attach(c.peripherals["OTG_FS"])
    bus.add_host(**FAST)
    assert bus.state == "detached"
    assert any("not muxed" in m for m in warns(c))
    c = usb_chip(desc, pll=False)
    core_init(c)
    t = Bus(c)
    assert t.setup(setup_pkt(0x80, 6, 0x100, 0, 18)) is None
    assert any("PLL48CLK is stopped" in m for m in warns(c))
    w(c, RCC + 0x04, 8 | (336 << 6) | (1 << 22) | (6 << 24))   # Q = 6: 56 MHz
    w(c, RCC, r(c, RCC) | (1 << 16) | (1 << 24))
    assert t.setup(setup_pkt(0x80, 6, 0x100, 0, 18)) is None
    assert any("56.000 MHz" in m for m in warns(c))


def test_bus_reset_suspend_resume_interrupts(desc):
    c = usb_chip(desc)
    core_init(c)
    bus = UsbBus("U")
    bus.attach(c.peripherals["OTG_FS"])
    host = bus.add_host(**FAST, auto_enumerate=False)
    c.run_for(1.5e6)
    from chipem.usbbus import Reset

    def reset_only():
        yield Reset()
    host.start_task(reset_only())
    c.run_for(0.1e6)
    assert r(c, FS + GINTSTS) & USBRST and r(c, FS + GINTSTS) & ENUMDNE == 0
    c.run_for(1.5e6)
    assert r(c, FS + GINTSTS) & ENUMDNE and (r(c, FS + DSTS) >> 1) & 3 == 3
    host.suspend()
    c.run_for(4e6)
    assert r(c, FS + GINTSTS) & USBSUSP and r(c, FS + DSTS) & 1
    host.resume()
    c.run_for(1e6)
    assert r(c, FS + GINTSTS) & WKUINT and r(c, FS + DSTS) & 1 == 0


def test_hs_core_ulpi_warns_and_dma_out(desc):
    c = usb_chip(desc)
    w(c, HS + GUSBCFG, r(c, HS + GUSBCFG) & ~0x40)
    assert any("ULPI" in m for m in warns(c))
    w(c, HS + GUSBCFG, r(c, HS + GUSBCFG) | 0x40)
    core_init(c, HS)
    w(c, HS + GAHBCFG, r(c, HS + GAHBCFG) | (1 << 5))       # DMAEN
    t = Bus(c, HS, "OTG_HS")
    w(c, HS + 0xB14, 0x20001000)                             # DOEPDMA0
    pkt = setup_pkt(0x21, 0x20, 0, 0, 7)
    assert t.setup(pkt) == "ACK"
    assert bytes(c.bus.read(0x20001000 + i, 1) for i in range(8)) == pkt
    assert r(c, HS + GINTSTS) & RXFLVL == 0
    w(c, HS + DOEPTSIZ(0), (1 << 19) | 64)
    w(c, HS + DOEPCTL(0), r(c, HS + DOEPCTL(0)) | EPENA | CNAK)
    assert t.out(0, bytes(7), "DATA1") == "ACK"
    assert r(c, HS + DOEPINT(0)) & 9 == 9                    # STUP (setup stage done) + XFRC
    assert r(c, HS + 0xB14) == 0x20001000 + 8 + 7


# ------------------------------------------------------------------------------------------------ samples
def _run_board(fw: Path, host_kw: dict, run_ns: float, send=None):
    if not fw.exists():
        pytest.skip(f"{fw.name} not built")
    s = Session("stm32f405zg", str(fw))
    otg = s.chip.peripherals["OTG_FS"]
    bus = UsbBus("USB1")
    bus.attach(otg)
    host = bus.add_host(**host_kw)
    s.run_for(run_ns)
    return s, bus, host


def test_bare_metal_cdc_enumerates_and_echoes():
    s, bus, host = _run_board(BARE, dict(FAST, debounce_ns=5e6), 50e6)
    d = host.device
    assert d is not None and d.configured
    assert (d.vid, d.pid) == (0x0483, 0x5740) and d.product == "STM32 bare-metal CDC" and d.serial == "0001"
    assert d.class_names()[0] == "CDC-ACM" and host.cdc.open
    assert bytes(host.cdc_rx) == b"STM32 CDC ready\r\n"
    host.cdc_write("hi\r\n")
    s.run_for(3e6)
    assert bytes(host.cdc_rx).endswith(b"echo: hi\r\n")
    sym = s.chip.symbols if hasattr(s.chip, "symbols") else None
    reqs = [ev.fields.get("req") for ev in s.events(kinds={"usb"}) if ev.fields.get("xfer") == "control"]
    assert reqs[:2] == ["GET_DESCRIPTOR(DEVICE, 64)", "SET_ADDRESS(1)"] and "SET_CONFIGURATION(1)" in reqs
    assert bus.timeouts == 0 and bus.stalls == 0
    del sym


def test_bare_metal_cdc_suspend_resume_reset_and_unplug():
    s, bus, host = _run_board(BARE, dict(FAST, debounce_ns=5e6), 50e6)
    fw = s.ctxs[s.view].image if hasattr(s, "ctxs") else None
    del fw
    otg = s.chip.peripherals["OTG_FS"]
    host.suspend()
    s.run_for(5e6)
    assert otg.usb_stats()["suspended"]
    host.resume()
    s.run_for(5e6)
    assert not otg.usb_stats()["suspended"]
    host.cdc_write("x")                                      # sent after TDRSMDN (20 ms) + TRSMRCY (10 ms)
    s.run_for(30e6)
    assert bytes(host.cdc_rx).endswith(b"echo: x")
    host.reset_port()                                        # re-enumeration after a port reset
    s.run_for(10e6)
    assert len(host.devices) == 2 and host.device.address == 2
    host.unplug()                                            # VBUS off: session end, pull-up off
    assert bus.state == "detached" and host.device is None
    host.plug()
    s.run_for(50e6)
    assert host.device is not None and len(host.devices) == 3
    kinds = [ev.fields.get("event") for ev in s.events(kinds={"usb"})]
    assert {"suspend", "resume", "detach", "vbus"} <= set(kinds)


def test_hal_usb_device_library_cdc():
    s, bus, host = _run_board(HAL, dict(FAST, debounce_ns=20e6), 60e6)
    d = host.device
    assert d is not None and d.manufacturer == "STMicroelectronics" and d.product == "STM32 HAL Virtual ComPort"
    assert bytes(host.cdc_rx) == b"HAL CDC ready\r\n"
    host.cdc_write("abc\r\n")
    s.run_for(5e6)
    assert bytes(host.cdc_rx).endswith(b"ABC\r\n")
    assert s.chip.arch is not None
    # the OTG interrupt fired only for real work (no storm: every flag the HAL handles is cleared)
    irqs = dict((name, n) for _e, name, n in s.irq_summary())
    assert irqs.get("OTG_FS", 0) < 200


def test_board_link_and_summary(capsys):
    if not BARE.exists():
        pytest.skip("usb_cdc.elf not built")
    from chipem.cli import main
    rc = main(["run", "--board", str(ROOT / "boards/stm32_usb_cdc.yaml"), "--for", "250ms", "--summary-only"])
    out = capsys.readouterr().out
    assert rc in (0, None)
    assert 'enumerated: 0483:5740 "CHIP_EM" "STM32 bare-metal CDC"' in out
    assert 'CDC-ACM output (30 bytes): "STM32 CDC ready\\r\\necho: hello\\r\\n"' in out


def test_cli_usb_host_and_memory_cdc_bridge():
    if not BARE.exists():
        pytest.skip("usb_cdc.elf not built")
    s = Session("stm32f405zg", str(BARE))
    link = s.attach_usb_host("OTG_FS:debounce=20ms,reset=1ms,reset_recovery=1ms")
    br = s.attach_usb_cdc("memory")
    assert br in s.host_bridges and link.endpoint is br
    br.port.host_send("ping\r\n")
    for _ in range(80):
        s.run_for(1e6)
        br.pump()
    got = br.port.host_recv(timeout=1.0)
    for _ in range(10):
        if b"echo: ping" in got:
            break
        got += br.port.host_recv(timeout=0.2)
    assert got.startswith(b"STM32 CDC ready\r\n") and b"echo: ping\r\n" in got
    s.close()


def test_cdc_bridge_forwards_dtr_and_line_speed():
    if not BARE.exists():
        pytest.skip("usb_cdc.elf not built")
    from chipem.hostserial import LineSettings
    s = Session("stm32f405zg", str(BARE))
    s.attach_usb_host("OTG_FS:debounce=5ms,reset=1ms,reset_recovery=1ms")
    br = s.attach_usb_cdc("memory")
    s.run_for(50e6)
    br.pump()
    br.port.set_host_modem(dtr=False, rts=False)              # the PC program closes the port: DTR low
    br.pump()
    s.run_for(3e6)
    br.port.set_host_modem(dtr=True, rts=True)                # opens it again: DTR high -> new banner
    br.port.set_line(LineSettings(9600))
    br.pump()
    s.run_for(3e6)
    reqs = [ev.fields.get("req", "") for ev in s.events(kinds={"usb"}) if ev.fields.get("xfer") == "control"]
    assert "SET_CONTROL_LINE_STATE(0x0, 0x0, 0)" in reqs and reqs[-1].startswith("SET_LINE_CODING")
    assert bytes(s.usb_links()[0].host.cdc_rx).count(b"STM32 CDC ready") == 2
    s.close()


def test_hs_dma_in_reads_memory(desc):
    c = usb_chip(desc)
    w(c, HS + GUSBCFG, r(c, HS + GUSBCFG) | 0x40)
    core_init(c, HS)
    w(c, HS + GAHBCFG, r(c, HS + GAHBCFG) | (1 << 5))
    t = Bus(c, HS, "OTG_HS")
    for i, b in enumerate(b"dma-in-data"):
        c.bus.write(0x20002000 + i, 1, b)
    w(c, HS + DIEPCTL(1), 64 | (2 << 18) | (1 << 22) | USBAEP | (1 << 28))
    w(c, HS + 0x934, 0x20002000)                             # DIEPDMA1
    w(c, HS + DIEPTSIZ(1), (1 << 19) | 11)
    w(c, HS + DIEPCTL(1), r(c, HS + DIEPCTL(1)) | EPENA | CNAK)
    assert t.inn(1) == ("DATA0", b"dma-in-data")
    assert r(c, HS + DIEPINT(1)) & 1 and r(c, HS + 0x934) == 0x2000200B
