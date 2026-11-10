"""stm32.otg_fs / stm32.otg_hs behaviour checked against RM0090 ch. 34/35 (one test per corrected rule): SETUP handling,
IN transfer-size accounting, OUT completion, endpoint control bits, status-phase interrupts, suspend, SOF pulse width,
mode mismatch, TRDT / HCLK limits, HS DMA and Flash."""
from __future__ import annotations

from test_stm32 import desc, warns  # noqa: F401  (desc: fixture)
from test_stm32_usb import (CNAK, DAINTMSK, DCFG, DCTL, DIEPCTL, DIEPINT, DIEPTSIZ, DOEPCTL, DOEPINT, DOEPTSIZ, DSTS,
                            EPENA, FS, GCCFG, GINTSTS, GUSBCFG, HS, NAKSTS, SNAK, Bus, core_init, r, setup_pkt,
                            usb_chip, w)

EPDIS, ITTXFE, INEPNE, STUP, STSPHSRX, MMIS = 1 << 30, 1 << 4, 1 << 6, 1 << 3, 1 << 5, 1 << 1


def test_setup_naks_ep0_and_stup_clears_out_enable(desc):
    """34.17.6 SETUP transactions: the core sets IN and OUT NAK on EP0 (INEPNE: NAK set by the core took effect),
    the setup-stage-done word is written at the data/status stage token, and popping it raises STUP and clears
    DOEPCTL0.EPENA.  CNAK clears INEPNE (34.16.4 DIEPINTx)."""
    c = usb_chip(desc)
    core_init(c)
    t = Bus(c)
    w(c, FS + DOEPTSIZ(0), (3 << 29) | (1 << 19) | 64)
    w(c, FS + DOEPCTL(0), r(c, FS + DOEPCTL(0)) | EPENA | CNAK)        # armed (e.g. for a status OUT)
    t.setup(setup_pkt(0x80, 6, 0x100, 0, 18))
    assert r(c, FS + DIEPCTL(0)) & NAKSTS and r(c, FS + DOEPCTL(0)) & NAKSTS
    assert r(c, FS + DIEPINT(0)) & INEPNE
    t.pop()
    assert r(c, FS + DOEPCTL(0)) & EPENA                     # still enabled: the setup stage is not over
    t.inn(0)
    assert t.pop()[0] == 4
    assert r(c, FS + DOEPINT(0)) & STUP and r(c, FS + DOEPCTL(0)) & EPENA == 0
    w(c, FS + DIEPCTL(0), r(c, FS + DIEPCTL(0)) | CNAK)
    assert r(c, FS + DIEPINT(0)) & INEPNE == 0 and r(c, FS + DIEPCTL(0)) & NAKSTS == 0


def test_setup_uses_reserved_rx_words(desc):
    """SETUPs are written "without checking for available space" into the 10 words data packets never use."""
    c = usb_chip(desc)
    core_init(c)
    w(c, FS + 0x24, 27)                                      # GRXFSIZ: one 64-byte packet (17 words) + 10
    t = Bus(c)
    w(c, FS + DOEPCTL(1), 64 | (2 << 18) | (1 << 15) | (1 << 28))
    w(c, FS + DOEPTSIZ(1), (4 << 19) | 256)
    w(c, FS + DOEPCTL(1), r(c, FS + DOEPCTL(1)) | EPENA | CNAK)
    assert t.out(1, b"A" * 64, "DATA0") == "ACK"
    assert t.out(1, b"B" * 4, "DATA1") == "NAK"              # only the SETUP reserve is left
    assert t.setup(setup_pkt(0, 9, 1)) == "ACK"              # ... which takes the SETUP
    assert t.pop()[0] == 2 and t.pop() == (6, 0, 8, 0, setup_pkt(0, 9, 1))


def test_in_xfrsiz_counts_packets_written_and_pktcnt_acks(desc):
    """34.17.6 Generic non-periodic IN: XFRSIZ decrements as each packet is written into the TX FIFO, PKTCNT on
    the host's ACK; XFRC when both are 0.  A zero-length packet needs no FIFO data."""
    c = usb_chip(desc)
    core_init(c)
    t = Bus(c)
    w(c, FS + DIEPCTL(1), 64 | (2 << 18) | (1 << 22) | (1 << 15) | (1 << 28))
    w(c, FS + DIEPTSIZ(1), (2 << 19) | 100)
    w(c, FS + DIEPCTL(1), r(c, FS + DIEPCTL(1)) | EPENA | CNAK)
    for k in range(16):
        w(c, FS + 0x2000, k)
    assert r(c, FS + DIEPTSIZ(1)) == (2 << 19) | 36          # first 64-byte packet written
    for k in range(9):
        w(c, FS + 0x2000, 0x100 + k)
    assert r(c, FS + DIEPTSIZ(1)) == (2 << 19) | 0           # 36-byte short packet written
    res = t.inn(1)
    assert res[0] == "DATA0" and len(res[1]) == 64
    assert r(c, FS + DIEPTSIZ(1)) == (1 << 19) and r(c, FS + DIEPCTL(1)) & EPENA
    res = t.inn(1)
    assert res[0] == "DATA1" and len(res[1]) == 36 and res[1][:4] == (0x100).to_bytes(4, "little")
    assert r(c, FS + DIEPTSIZ(1)) == 0 and r(c, FS + DIEPINT(1)) & 1 and r(c, FS + DIEPCTL(1)) & EPENA == 0
    # IN token with nothing to send (endpoint done, NAK clear): ITTXFE (and the NAK interrupt)
    w(c, FS + DIEPINT(1), 0xFFFF)
    assert t.inn(1) == "NAK" and r(c, FS + DIEPINT(1)) & (ITTXFE | (1 << 13)) == ITTXFE | (1 << 13)
    # NAK bit set: no ITTXFE
    w(c, FS + DIEPINT(1), 0xFFFF)
    w(c, FS + DIEPCTL(1), r(c, FS + DIEPCTL(1)) | SNAK)
    assert t.inn(1) == "NAK" and r(c, FS + DIEPINT(1)) & ITTXFE == 0
    # zero-length packet: XFRSIZ 0, PKTCNT 1
    w(c, FS + DIEPTSIZ(1), 1 << 19)
    w(c, FS + DIEPCTL(1), r(c, FS + DIEPCTL(1)) | EPENA | CNAK)
    assert t.inn(1) == ("DATA0", b"")
    assert r(c, FS + DIEPINT(1)) & 1


def test_ep0_out_cannot_be_disabled_and_reserved_ctl_bits(desc):
    """DOEPCTL0.EPDIS: "The application cannot disable control OUT endpoint 0"; reserved control bits read 0."""
    c = usb_chip(desc)
    core_init(c)
    w(c, FS + DOEPTSIZ(0), (3 << 29) | (1 << 19) | 64)
    w(c, FS + DOEPCTL(0), r(c, FS + DOEPCTL(0)) | EPENA | CNAK)
    w(c, FS + DOEPCTL(0), r(c, FS + DOEPCTL(0)) | EPDIS)
    assert r(c, FS + DOEPCTL(0)) & EPENA and r(c, FS + DOEPINT(0)) & 2 == 0
    w(c, FS + DIEPCTL(0), 0x0000_7FFC | (1 << 20) | (1 << 16))       # reserved bits of DIEPCTL0
    assert r(c, FS + DIEPCTL(0)) & (0x7FFC | (1 << 20) | (1 << 16)) == 0
    w(c, FS + DOEPCTL(1), 0x7800 | (0xF << 22))                       # DOEPCTLx 14:11 and 25:22 reserved
    assert r(c, FS + DOEPCTL(1)) == 0


def test_stsphsrx_on_control_write_status_and_nzlsohsk(desc):
    """DOEPINT.STSPHSRX: "the host has switched from data phase to the status phase of a control write" (the status
    IN token).  DCFG.NZLSOHSK: a non-zero-length status OUT is STALLed and not delivered."""
    c = usb_chip(desc)
    core_init(c)
    t = Bus(c)
    t.setup(setup_pkt(0x21, 0x20, 0, 0, 7))                  # SET_LINE_CODING: control write, 7 bytes
    t.pop()
    w(c, FS + DOEPTSIZ(0), (3 << 29) | (1 << 19) | 7)
    w(c, FS + DOEPCTL(0), r(c, FS + DOEPCTL(0)) | EPENA | CNAK)
    assert t.out(0, bytes(7), "DATA1") == "ACK"
    assert r(c, FS + DOEPINT(0)) & STSPHSRX == 0
    t.inn(0)                                                  # status stage IN token
    assert r(c, FS + DOEPINT(0)) & STSPHSRX
    # control read: status OUT with data
    w(c, FS + DCFG, r(c, FS + DCFG) | 4)                      # NZLSOHSK
    t.setup(setup_pkt(0x80, 6, 0x100, 0, 18))
    w(c, FS + DOEPTSIZ(0), (3 << 29) | (1 << 19) | 64)
    w(c, FS + DOEPCTL(0), r(c, FS + DOEPCTL(0)) | EPENA | CNAK)
    assert t.out(0, b"xy", "DATA1") == "STALL"
    assert t.out(0, b"", "DATA1") == "ACK"


def test_sof_pulse_is_20_hclk(desc):
    """34.7.2: "An SOF pulse signal with a width of 20 HCLK cycles"."""
    c = usb_chip(desc)
    core_init(c)
    w(c, 0x40020000 + 0x24, (r(c, 0x40020000 + 0x24) & ~0xF) | 10)   # PA8 AF10 = OTG_FS_SOF
    w(c, 0x40020000, (r(c, 0x40020000) & ~(3 << 16)) | (2 << 16))
    w(c, FS + GCCFG, r(c, FS + GCCFG) | (1 << 20))            # SOFOUTEN
    otg = c.peripherals["OTG_FS"]
    t = Bus(c)
    t0 = c.now_ns()
    otg.usb_sof(t.bus, 5, t0)
    assert c.pins.get("PA8").drive == "H"
    c.run_for(20 * 1e9 / 16e6 - 300)
    assert c.pins.get("PA8").drive == "H"
    c.run_for(600)
    assert c.pins.get("PA8").drive == "L"


def test_rwusig_clears_suspsts(desc):
    """DSTS.SUSPSTS: the core leaves suspend "when the application writes to the RWUSIG bit"."""
    c = usb_chip(desc)
    core_init(c)
    otg = c.peripherals["OTG_FS"]
    t = Bus(c)
    otg.usb_suspend(t.bus, c.now_ns())
    c.run_for(3.1e6)
    assert r(c, FS + DSTS) & 1
    w(c, FS + DCTL, r(c, FS + DCTL) | 1)
    assert r(c, FS + DSTS) & 1 == 0


def test_mode_mismatch_interrupt(desc):
    """34.15: a host register accessed in device mode sets GINTSTS.MMIS; the access is ignored."""
    c = usb_chip(desc)
    core_init(c)
    w(c, FS + 0x400, 1)                                       # HCFG
    assert r(c, FS + GINTSTS) & MMIS
    w(c, FS + GINTSTS, MMIS)
    assert r(c, FS + 0x400) == 0 and r(c, FS + GINTSTS) & MMIS


def test_usb_reset_leaves_out_nak_to_firmware(desc):
    """34.17.5: on USB reset the application sets SNAK on the OUT endpoints; the core only clears USBAEP (EP 1..n)."""
    c = usb_chip(desc)
    core_init(c)
    otg = c.peripherals["OTG_FS"]
    t = Bus(c)
    w(c, FS + DOEPCTL(1), 64 | (2 << 18) | (1 << 15))
    otg.usb_reset(t.bus, True, c.now_ns())
    assert r(c, FS + DOEPCTL(0)) & NAKSTS == 0 and r(c, FS + DOEPCTL(1)) & (1 << 15) == 0
    assert r(c, FS + DAINTMSK) == 0


def test_trdt_below_table_minimum_warns(desc):
    """Table 203: TRDT minimum for the AHB frequency (HCLK 16 MHz -> 0xD)."""
    c = usb_chip(desc)
    core_init(c)
    w(c, FS + GUSBCFG, (r(c, FS + GUSBCFG) & ~(0xF << 10)) | (6 << 10))
    Bus(c).setup(setup_pkt(0x80, 6, 0x100, 0, 18))
    assert any("TRDT = 6 is below the minimum 13" in m for m in warns(c))


def test_hs_dma_cannot_read_flash(desc):
    """Figure 410 note: "The USB DMA cannot directly address the internal Flash memory"."""
    c = usb_chip(desc, sysclk_pll=True)
    w(c, HS + GUSBCFG, r(c, HS + GUSBCFG) | 0x40)
    core_init(c, HS)
    w(c, HS + 0x008, r(c, HS + 0x008) | (1 << 5))
    t = Bus(c, HS, "OTG_HS")
    w(c, HS + DIEPCTL(1), 64 | (2 << 18) | (1 << 22) | (1 << 15) | (1 << 28))
    w(c, HS + 0x934, 0x08000000)
    w(c, HS + DIEPTSIZ(1), (1 << 19) | 8)
    w(c, HS + DIEPCTL(1), r(c, HS + DIEPCTL(1)) | EPENA | CNAK)
    assert t.inn(1) == ("DATA0", bytes(8))
    assert any("cannot address the internal Flash" in m for m in warns(c))
