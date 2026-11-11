"""stm32f1.usb / stm32f1.pma (STM32F103T6 USB full-speed device, RM0008 ch. 23 layout) on a chipem/usbbus.py cable:
PMA access geometry, EPnR toggle / rc_w0 write semantics, ISTR, IRQ lines, transceiver -> PA11/PA12, the 48 MHz
clock check, SETUP / OUT / IN with toggles, NAK / STALL, double-buffered bulk, the pull-up options, and the
bare-metal CDC-ACM sample end to end."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.firmware import FirmwareImage
from chipem.session import Session
from chipem.usbbus import Transaction, UsbBus

from engine_util import asm

ROOT = Path(__file__).resolve().parents[1]
YAML = ROOT / "chips" / "stm32f103t6.yaml"
FW = ROOT / "firmware/samples_stm32f1/build/usb_cdc.elf"
FLASH = 0x08000000
RCC, USB, PMA = 0x40021000, 0x40005C00, 0x40006000
CNTR, ISTR, FNR, DADDR, BTABLE = USB + 0x40, USB + 0x44, USB + 0x48, USB + 0x4C, USB + 0x50
LP, HP = 20, 19
CTR_RX, DTOG_RX, SETUP, CTR_TX, DTOG_TX = 0x8000, 0x4000, 0x0800, 0x0080, 0x0040
FAST = dict(debounce_ns=1e6, reset_ns=1e6, reset_recovery_ns=1e6, set_address_recovery_ns=0.5e6)


@pytest.fixture(scope="module")
def desc():
    return load_chip(YAML)


def w(c, a, v, size=4):
    c.bus.write(a, size, v)


def r(c, a, size=4):
    return c.bus.read(a, size)


def epr(n):
    return USB + 4 * n


def warns(c):
    return [e.fields.get("msg", "") for e in c.log if e.kind == "warn"]


def spin_chip(desc, *, pll=True, usben=True):
    code = asm("spin: b spin", FLASH + 0x200)
    img = bytearray(0x200 + len(code))
    img[0:8] = struct.pack("<II", 0x20002800, (FLASH + 0x200) | 1)
    img[0x200:] = code
    c = Chip(desc, clock_hz=8e6)
    c.load_firmware(FirmwareImage([(FLASH, bytes(img))]))
    c.reset()
    if pll:                                                  # HSE 8 MHz x 9 = 72 MHz PLL, USBPRE = 0: 48 MHz
        w(c, RCC, r(c, RCC) | (1 << 16))
        w(c, RCC + 4, (1 << 16) | (7 << 18))
        w(c, RCC, r(c, RCC) | (1 << 24))
    if usben:
        w(c, RCC + 0x1C, r(c, RCC + 0x1C) | (1 << 23))
    return c


def pma16(c, a, v=None):
    if v is None:
        return r(c, PMA + 2 * a) & 0xFFFF
    w(c, PMA + 2 * a, v)


def set_stat(c, n, rx=None, tx=None, extra=0):
    """Firmware-style EPnR update: keep CTRs (write 1), toggle STAT fields to the wanted values."""
    v = r(c, epr(n))
    t = 0
    if rx is not None:
        t |= (v & 0x3000) ^ (rx << 12)
    if tx is not None:
        t |= (v & 0x0030) ^ (tx << 4)
    w(c, epr(n), (v & 0x070F) | CTR_RX | CTR_TX | t | extra)


class Dev:
    """The USB block on a host-less cable: tests send transactions by hand."""

    def __init__(self, c):
        self.c = c
        self.usb = c.peripherals["USB"]
        self.bus = UsbBus("T")
        self.bus.vbus = True
        self.bus.attach(self.usb)

    def x(self, kind, ep, pid=None, data=b"", addr=0):
        return Transaction(kind, addr, ep, pid, data, self.c.now_ns())

    def setup(self, data, addr=0):
        return self.usb.usb_setup(self.bus, self.x("SETUP", 0, "DATA0", data, addr))

    def out(self, ep, data, pid="DATA0", addr=0):
        return self.usb.usb_out(self.bus, self.x("OUT", ep, pid, data, addr))

    def inn(self, ep, addr=0):
        t = self.x("IN", ep, addr=addr)
        res = self.usb.usb_in(self.bus, t)
        if isinstance(res, tuple):
            self.usb.usb_in_done(self.bus, t, True, self.c.now_ns())
        return res


def enable(c, *, mask=0xBF00):
    w(c, CNTR, 1)                                            # PDWN = 0, FRES = 1
    w(c, CNTR, 0)
    w(c, ISTR, 0)
    w(c, CNTR, mask)
    w(c, BTABLE, 0)
    pma16(c, 0, 0x40)                                        # EP0: TX 0x40, RX 0x80 64 bytes
    pma16(c, 2, 0)
    pma16(c, 4, 0x80)
    pma16(c, 6, 0x8400)
    w(c, epr(0), 0x0200)                                     # control, EA 0
    set_stat(c, 0, rx=3, tx=2)
    w(c, DADDR, 0x80)


# ------------------------------------------------------------------------------------------------ registers
def test_reset_values_and_pma_geometry(desc):
    c = spin_chip(desc)
    assert r(c, CNTR) == 0x3 and r(c, ISTR) == 0 and r(c, DADDR) == 0
    w(c, PMA + 0x10, 0xDEADBEEF)                             # word at local 8: low half only
    assert r(c, PMA + 0x10) == 0xBEEF and r(c, PMA + 0x12, 2) == 0
    pma = c.peripherals["USB_CAN_SRAM"]
    assert pma.data[8:10] == b"\xEF\xBE"
    w(c, PMA + 0x11, 0x12, 1)                                # byte access: high byte of local word 8
    assert pma.data[9] == 0x12 and r(c, PMA + 0x10, 2) == 0x12EF and r(c, PMA + 0x10, 1) == 0xEF
    w(c, PMA + 0x12, 0x5555, 2)                              # dead upper half: ignored
    assert pma.data[10:12] == b"\0\0"
    assert pma.r16(0x1FE) == 0 and r(c, PMA + 0x3FC) == 0    # last word: local 0x1FE
    pma.w16(0x1FE, 0xA55A)
    assert r(c, PMA + 0x3FC) == 0xA55A
    c.reset()
    assert pma.r16(0x1FE) == 0xA55A                          # SRAM keeps its content over a reset


def test_epr_write_semantics(desc):
    c = spin_chip(desc)
    w(c, epr(1), 0x0623)                                     # EP_TYPE 3 (interrupt), EA 3, STAT_TX toggle 10
    v = r(c, epr(1))
    assert v & 0x070F == 0x0603 and v & 0x7070 == 0x0020     # STAT_TX toggled 00 -> 10 (NAK)
    w(c, epr(1), 0x0603 | 0x0020)                            # writing the same 1 again toggles back to 00
    assert r(c, epr(1)) & 0x30 == 0
    w(c, epr(1), 0x0603 | 0x3000 | 0x4040)                   # STAT_RX 11, both DTOGs flip
    assert r(c, epr(1)) & 0x7070 == 0x7040
    w(c, epr(1), 0x0603)                                     # 0s: nothing toggles
    assert r(c, epr(1)) & 0x7070 == 0x7040
    usb = c.peripherals["USB"]
    usb._s(4, r(c, epr(1)) | CTR_RX | CTR_TX | SETUP)        # as after a SETUP + IN
    w(c, epr(1), 0x0603 | CTR_TX)                            # rc_w0: CTR_RX cleared, CTR_TX kept, SETUP read-only
    v = r(c, epr(1))
    assert v & CTR_RX == 0 and v & CTR_TX and v & SETUP
    w(c, epr(1), 0x0603 | CTR_RX)
    assert r(c, epr(1)) & (CTR_RX | CTR_TX) == 0
    w(c, epr(1) + 1, 0x30, 1)                                # byte write to the high byte: low-byte toggles kept
    v = r(c, epr(1))
    assert v & 0x7070 == 0x4040 and v & 0x0F == 3            # STAT_RX flipped 11 -> 00
    w(c, epr(1) + 2, 0xFFFF, 2)                              # reserved upper half
    assert r(c, epr(1)) >> 16 == 0


def test_istr_rc_w0_ctr_and_irq_lines(desc):
    c = spin_chip(desc)
    d = Dev(c)
    enable(c, mask=0x0400)                                   # RESETM only
    usb = d.usb
    assert 16 + LP not in c.arch.asserted
    w(c, CNTR, 0x0401)                                       # FRES 0 -> 1: RESET flag (like a bus reset)
    assert r(c, ISTR) & 0x0400 and 16 + LP in c.arch.asserted
    w(c, ISTR, 0xFFFF & ~0x0400)
    assert r(c, ISTR) == 0 and 16 + LP not in c.arch.asserted
    assert r(c, epr(0)) == 0 and r(c, DADDR) == 0            # the reset cleared endpoint and address registers
    w(c, CNTR, 0x0400)
    enable(c, mask=0x8000)                                   # CTRM only
    assert d.setup(bytes(8)) == "ACK"
    v = r(c, ISTR)
    assert v & 0x8000 and v & 0x1F == 0x10                  # CTR, DIR = 1 (RX), EP_ID 0
    w(c, ISTR, 0)                                            # CTR is read-only: clearing ISTR does not clear it
    assert r(c, ISTR) & 0x8000 and 16 + LP in c.arch.asserted and 16 + HP not in c.arch.asserted
    w(c, epr(0), (r(c, epr(0)) & 0x070F) | CTR_TX)           # firmware clears CTR_RX
    assert r(c, ISTR) & 0x8000 == 0 and 16 + LP not in c.arch.asserted
    # SOF flag with SOFM
    w(c, CNTR, 0x8200)
    usb.usb_sof(d.bus, 5, c.now_ns())
    usb.usb_sof(d.bus, 6, c.now_ns())
    assert r(c, ISTR) & 0x0200 and r(c, FNR) & 0x27FF == 0x2006 and 16 + LP in c.arch.asserted   # FN, LCK
    w(c, ISTR, ~0x0200 & 0xFFFF)
    assert 16 + LP not in c.arch.asserted


def test_transceiver_owns_pa11_pa12_and_can_warning(desc):
    c = spin_chip(desc)
    pa11, pa12 = c.pins.get("PA11"), c.pins.get("PA12")
    assert pa11.owner != "USBDM"
    assert not c.peripherals["USB"].transceiver_on()
    w(c, CNTR, 1)                                            # PDWN = 0
    assert c.peripherals["USB"].transceiver_on() and pa11.owner == "USBDM" and pa12.owner == "USBDP"
    w(c, RCC + 0x1C, r(c, RCC + 0x1C) & ~(1 << 23))          # USB clock off: transceiver off
    assert pa11.owner != "USBDM" and not c.peripherals["USB"].active()
    w(c, CNTR, 3)
    can = c.peripherals.get("CAN")
    if can is not None:
        can.active = lambda: True                            # the CAN model reports itself in use
        w(c, RCC + 0x1C, r(c, RCC + 0x1C) | (1 << 25) | (1 << 23))
        w(c, CNTR, 1)
        assert any("share the 512-byte SRAM" in m for m in warns(c))


def test_clock_must_be_48mhz(desc):
    c = spin_chip(desc, pll=False)
    d = Dev(c)
    w(c, CNTR, 0)
    assert d.bus.state == "detached"
    assert any("USBCLK is stopped" in m for m in warns(c))
    w(c, RCC, r(c, RCC) | (1 << 16))
    w(c, RCC + 4, (1 << 16) | (7 << 18))
    w(c, RCC, r(c, RCC) | (1 << 24))                         # the PLL comes up: the RCC listener reconnects
    assert d.bus.state == "attached"
    c2 = spin_chip(desc, pll=False)
    w(c2, RCC + 4, (1 << 16) | (4 << 18))                    # PLL 48 MHz with USBPRE = 0: USBCLK 32 MHz
    w(c2, RCC, r(c2, RCC) | (1 << 16) | (1 << 24))
    d2 = Dev(c2)
    w(c2, CNTR, 0)
    assert d2.bus.state == "detached" and any("32.000 MHz" in m for m in warns(c2))


# ------------------------------------------------------------------------------------------------ transactions
def test_control_setup_in_out_toggles(desc):
    c = spin_chip(desc)
    d = Dev(c)
    enable(c)
    assert d.setup(bytes(8), addr=5) is None                 # not our address
    w(c, DADDR, 0x00)
    assert d.setup(bytes(8)) is None                         # EF = 0: no answer
    w(c, DADDR, 0x80)
    pkt = bytes([0x80, 6, 0, 1, 0, 0, 18, 0])
    assert d.setup(pkt) == "ACK"
    pma = c.peripherals["USB_CAN_SRAM"]
    assert pma.read_bytes(0x80, 8) == pkt and pma16(c, 6) == 0x8408
    v = r(c, epr(0))
    assert v & (CTR_RX | SETUP | DTOG_RX | DTOG_TX) == CTR_RX | SETUP | DTOG_RX | DTOG_TX
    assert (v >> 12) & 3 == 2 and (v >> 4) & 3 == 2         # both directions NAK until firmware answers
    assert d.inn(0) == "NAK"
    w(c, epr(0), (r(c, epr(0)) & 0x070F) | CTR_TX)           # clear CTR_RX
    pma.write_bytes(0x40, b"0123456789AB")
    pma16(c, 2, 12)
    set_stat(c, 0, tx=3, rx=3)
    assert d.inn(0) == ("DATA1", b"0123456789AB")
    v = r(c, epr(0))
    assert v & CTR_TX and not v & DTOG_TX and (v >> 4) & 3 == 2
    assert d.out(0, b"", "DATA0") == "ACK"                   # wrong toggle (DTOG_RX = 1): ACKed, ignored
    assert not r(c, epr(0)) & CTR_RX
    assert d.out(0, b"", "DATA1") == "ACK"                   # status OUT
    v = r(c, epr(0))
    assert v & CTR_RX and not v & SETUP and pma16(c, 6) & 0x3FF == 0 and (v >> 12) & 3 == 2
    assert d.out(0, b"x", "DATA0") == "NAK"
    set_stat(c, 0, rx=1)                                     # STALL both ways; a SETUP still gets through
    set_stat(c, 0, tx=1)
    assert d.out(0, b"x", "DATA0") == "STALL" and d.inn(0) == "STALL"
    assert d.setup(pkt) == "ACK"
    set_stat(c, 0, rx=3, extra=0x0100)                       # STATUS_OUT: an OUT with data is STALLed
    assert d.out(0, b"x", "DATA1") == "STALL"
    assert d.out(0, b"", "DATA1") == "ACK"


def test_bulk_out_overrun_and_disabled(desc):
    c = spin_chip(desc)
    d = Dev(c)
    enable(c)
    pma16(c, 8 + 4, 0x100)
    pma16(c, 8 + 6, 4 << 10)                                 # BL_SIZE 0, NUM_BLOCK 4: 8 bytes
    w(c, epr(1), 0x0002)                                     # bulk, EA 2
    assert d.out(2, b"abc") is None                          # STAT_RX DISABLED: no answer
    set_stat(c, 1, rx=3)
    assert d.out(2, b"123456789") == "STALL"                 # 9 > 8: buffer overrun
    assert not r(c, epr(1)) & CTR_RX
    assert d.out(2, b"12345678") == "ACK"
    assert c.peripherals["USB_CAN_SRAM"].read_bytes(0x100, 8) == b"12345678" and pma16(c, 0xE) & 0x3FF == 8
    assert r(c, epr(1)) & DTOG_RX and 16 + LP in c.arch.asserted and 16 + HP not in c.arch.asserted


def test_double_buffered_bulk_in_uses_hp_line(desc):
    c = spin_chip(desc)
    d = Dev(c)
    enable(c)
    pma16(c, 8, 0x100)
    pma16(c, 10, 3)                                          # buffer 0: "AAA"
    pma16(c, 12, 0x140)
    pma16(c, 14, 2)                                          # buffer 1: "BB"
    pma = c.peripherals["USB_CAN_SRAM"]
    pma.write_bytes(0x100, b"AAA")
    pma.write_bytes(0x140, b"BB")
    w(c, epr(1), 0x0101)                                     # bulk + EP_KIND (double buffer), EA 1
    set_stat(c, 1, tx=3)
    assert d.inn(1) == "NAK"                                 # DTOG_TX == SW_BUF (DTOG_RX): no buffer for the USB
    w(c, epr(1), 0x0101 | CTR_RX | CTR_TX | DTOG_RX)         # application releases a buffer (SW_BUF = 1)
    assert d.inn(1) == ("DATA0", b"AAA")
    v = r(c, epr(1))
    assert v & CTR_TX and v & DTOG_TX and (v >> 4) & 3 == 3  # STAT_TX stays VALID
    assert 16 + HP in c.arch.asserted and 16 + LP in c.arch.asserted
    assert d.inn(1) == "NAK"                                 # DTOG_TX caught up with SW_BUF
    w(c, epr(1), 0x0101 | CTR_RX | DTOG_RX)                  # clear CTR_TX, SW_BUF = 0
    assert 16 + HP not in c.arch.asserted
    assert d.inn(1) == ("DATA1", b"BB")


def test_bus_reset_suspend_resume_and_remote_wakeup(desc):
    c = spin_chip(desc)
    d = Dev(c)
    enable(c, mask=0x1F00)
    usb = d.usb
    usb.usb_reset(d.bus, True, c.now_ns())
    assert r(c, ISTR) & 0x0400 and r(c, epr(0)) == 0 and r(c, DADDR) == 0
    w(c, ISTR, 0)
    usb.usb_suspend(d.bus, c.now_ns())
    assert r(c, ISTR) & 0x0900 == 0x0900 and (r(c, FNR) >> 11) & 3 == 3
    w(c, ISTR, 0)
    w(c, CNTR, r(c, CNTR) | 0x0C)                            # FSUSP | LP_MODE
    calls = []
    d.bus.remote_wakeup = lambda: calls.append(1)
    w(c, CNTR, r(c, CNTR) | 0x10)                            # RESUME: remote wakeup signalling
    assert calls == [1]
    usb.usb_resume(d.bus, c.now_ns())
    assert r(c, ISTR) & 0x1000 and not r(c, CNTR) & 0x04     # WKUP, LP_MODE cleared by hardware
    assert getattr(c, "_stm32f1_exti_inputs", {}).get(18) is False   # line 18 was pulsed


def test_gpio_switched_pullup(desc):
    desc2 = load_chip(YAML)
    usbd = next(p for p in desc2.peripherals if p.name == "USB")
    usbd.params = dict(usbd.params or {}, pullup="PA8")
    c = spin_chip(desc2)
    d = Dev(c)
    w(c, CNTR, 0)
    assert d.bus.state == "detached"
    w(c, RCC + 0x18, r(c, RCC + 0x18) | (1 << 2))            # IOPAEN
    w(c, 0x40010804, (r(c, 0x40010804) & ~0xF) | 0x2)       # PA8 push-pull output
    w(c, 0x40010810, 1 << 8)                                 # BSRR: PA8 high -> D+ pulled up
    assert d.bus.state == "attached"
    w(c, 0x40010810, 1 << 24)                                # PA8 low: disconnect
    assert d.bus.state == "detached"


# ------------------------------------------------------------------------------------------------ sample
def test_cdc_sample_enumerates_and_echoes():
    if not FW.exists():
        pytest.skip("usb_cdc.elf not built (firmware/samples_stm32f1/build.sh)")
    s = Session("stm32f103t6", str(FW))
    usb = s.chip.peripherals["USB"]
    bus = UsbBus("USB1")
    bus.attach(usb)
    host = bus.add_host(**dict(FAST, debounce_ns=5e6))
    s.run_for(30e6)
    dev = host.device
    assert dev is not None and dev.configured
    assert (dev.vid, dev.pid) == (0x0483, 0x5740) and dev.product == "F103 bare-metal CDC" and dev.serial == "0001"
    assert dev.class_names()[0] == "CDC-ACM" and host.cdc.open
    assert bytes(host.cdc_rx) == b"F103 CDC ready\r\n"
    host.cdc_write("hello, f103!\r\n")
    s.run_for(3e6)
    assert bytes(host.cdc_rx).endswith(b"HELLO, F103!\r\n")
    host.cdc_write(bytes(range(0x40, 0x80)))                 # exactly 64 bytes: echoed with a ZLP after it
    s.run_for(3e6)
    assert bytes(host.cdc_rx).endswith(bytes(range(0x40, 0x80)).upper())
    reqs = [ev.fields.get("req") for ev in s.events(kinds={"usb"}) if ev.fields.get("xfer") == "control"]
    assert reqs[:2] == ["GET_DESCRIPTOR(DEVICE, 64)", "SET_ADDRESS(1)"] and "SET_CONFIGURATION(1)" in reqs
    assert bus.timeouts == 0 and bus.stalls == 0
    # timing: attach once the firmware cleared FRES (well under 1 ms at 72 MHz), enumerated after debounce 5 ms
    # + reset 1 ms + recovery 1 ms + transfers; every transaction is paced by the real 12 Mbit/s bus
    ev = {e.fields.get("event"): e.t_ns for e in s.events(kinds={"usb"}) if e.fields.get("event")}
    assert ev["attach"] < 1e6 and 7e6 < ev["enumerated"] < 15e6
    assert usb.usb_stats()["address"] == 1 and usb.usb_stats()["sof"] > 20
    host.suspend()
    s.run_for(5e6)
    assert usb.usb_stats()["suspended"]
    host.resume()
    s.run_for(35e6)
    host.cdc_write("x")
    s.run_for(3e6)
    assert bytes(host.cdc_rx).endswith(b"X")
    s.close()


def test_board_run_summary(capsys):
    if not FW.exists():
        pytest.skip("usb_cdc.elf not built")
    from chipem.cli import main
    rc = main(["run", "--board", str(ROOT / "boards/stm32f103_usb_cdc.yaml"), "--for", "250ms", "--summary-only"])
    out = capsys.readouterr().out
    assert rc in (0, None)
    assert 'enumerated: 0483:5740 "CHIP_EM" "F103 bare-metal CDC"' in out
    assert 'CDC-ACM output (23 bytes): "F103 CDC ready\\r\\nHELLO\\r\\n"' in out
