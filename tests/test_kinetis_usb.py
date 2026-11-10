"""Kinetis K22 USB0 (kinetis.usbfs, RM ch. 43) and USBDCD (kinetis.usbdcd, RM ch. 44): register-level tests driving
the usbbus device protocol directly, and end-to-end runs of firmware/samples/usb_cdc.c on a UsbBus + host."""
from __future__ import annotations

from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.firmware import load_firmware
from chipem.usbbus import Transaction, UsbBus

from engine_util import build_image

ROOT = Path(__file__).resolve().parent.parent
K22 = ROOT / "chips" / "mk22fn1m0vll12.yaml"
ELF = ROOT / "firmware" / "samples" / "build" / "usb_cdc.elf"

USB0, DCD, SIM, MCG = 0x40072000, 0x40035000, 0x40047000, 0x40064000
SOPT1, SOPT2, SCGC4, SCGC6, CLKDIV2 = SIM + 0x0, SIM + 0x1004, SIM + 0x1034, SIM + 0x103C, SIM + 0x1048
ISTAT, INTEN, ERRSTAT, ERREN, STAT, CTL, ADDR = 0x80, 0x84, 0x88, 0x8C, 0x90, 0x94, 0x98
BDT = 0x1FFF8000
BUF = 0x1FFF9000
OWN, DATA1, DTS, BSTALL = 0x80, 0x40, 0x08, 0x04
FAST = dict(debounce_ns=2e6, reset_ns=1e6, reset_recovery_ns=1e6, set_address_recovery_ns=0.5e6, resume_ns=2e6,
            resume_recovery_ns=1e6)


@pytest.fixture(scope="module")
def desc():
    return load_chip(K22)


def warns(chip) -> list[str]:
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "warn"]


def pee(chip) -> None:
    """MCG to PEE 120 MHz (8 MHz crystal / 2 x 30), like the sample's clock_120mhz()."""
    w = chip.bus.write
    w(SIM + 0x1044, 4, 0x01240000)          # SIM_CLKDIV1: core /1, bus /2, FlexBus /3, flash /5
    w(MCG + 1, 1, 0x24)
    w(MCG + 0, 1, 0x98)
    w(MCG + 4, 1, 0x01)
    w(MCG + 5, 1, 0x46)
    w(MCG + 0, 1, 0x18)


def usb_clock(chip, usbdiv: int = 4, usbfrac: int = 1) -> None:
    chip.bus.write(SOPT2, 4, chip.bus.read(SOPT2, 4) | (1 << 18) | (1 << 16))
    chip.bus.write(CLKDIV2, 4, (usbdiv << 1) | usbfrac)


def mkchip(desc, *, clock=True, gate=True):
    chip = Chip(desc, clock_hz=120e6)
    img, _ = build_image({"reset": "spin:\n b spin\n"})
    chip.load_firmware(img)
    chip.reset()
    for v in (0xC520, 0xD928):                               # WDOG unlock, then WDOGEN = 0 (as startup.c does)
        chip.bus.write(0x4005200E, 2, v)
    chip.bus.write(0x40052000, 2, chip.bus.read(0x40052000, 2) & ~1)
    if clock:
        pee(chip)
        usb_clock(chip)
    if gate:
        chip.bus.write(SCGC4, 4, chip.bus.read(SCGC4, 4) | (1 << 18))
        chip.bus.write(SCGC6, 4, chip.bus.read(SCGC6, 4) | (1 << 21))
    return chip


def usb(chip):
    return chip.peripherals["USB0"]


def wr(chip, off, v):
    chip.bus.write(USB0 + off, 1, v)


def rd(chip, off):
    return chip.bus.read(USB0 + off, 1)


def enable(chip):
    """Device setup as the NXP driver does it: BDT, transceiver awake, EP0 control, module on."""
    wr(chip, 0x9C, (BDT >> 8) & 0xFE)
    wr(chip, 0xB0, (BDT >> 16) & 0xFF)
    wr(chip, 0xB4, BDT >> 24)
    wr(chip, 0x100, 0)                      # USBCTRL: SUSP = PDE = 0
    wr(chip, 0xC0, 0x0D)                    # ENDPT0: HSHK | TXEN | RXEN
    wr(chip, CTL, 0x01)


def bd(ep, tx, odd):
    return BDT | (ep << 5) | (tx << 4) | (odd << 3)


def prime(chip, ep, tx, odd, n, flags=0, buf=None, data=b""):
    a = bd(ep, tx, odd)
    buf = BUF + 0x100 * (ep * 4 + tx * 2 + odd) if buf is None else buf
    if data:
        chip.bus.write_bytes(buf, data)
    chip.bus.write(a + 4, 4, buf)
    chip.bus.write(a, 4, (n << 16) | OWN | flags)
    return buf


def bdctl(chip, ep, tx, odd):
    return chip.bus.read(bd(ep, tx, odd), 4)


def setup_tok(data=bytes([0x80, 6, 0, 1, 0, 0, 18, 0]), addr=0, ep=0):
    return Transaction("SETUP", addr, ep, "DATA0", data)


def out_tok(data, pid="DATA0", addr=0, ep=1):
    return Transaction("OUT", addr, ep, pid, data)


def in_tok(addr=0, ep=0):
    return Transaction("IN", addr, ep)


# ------------------------------------------------------------------------------------------------ registers
def test_reset_values_and_read_only_ids(desc):
    chip = mkchip(desc)
    assert [rd(chip, o) for o in (0x0, 0x4, 0x8, 0xC)] == [0x04, 0xFB, 0x33, 0x01]
    assert rd(chip, 0x100) == 0xC0 and rd(chip, 0x104) == 0x50     # USBCTRL SUSP|PDE, OBSERVE DPPD|DMPD
    wr(chip, 0x0, 0xFF)
    assert rd(chip, 0x0) == 0x04
    assert [chip.bus.read(DCD + o, 4) for o in (0x0, 0x4, 0x8, 0x10, 0x14, 0x18)] == \
        [0x00010000, 0xC1, 0, 0x00100000, 0x000A0028, 0x00280001]


def test_istat_w1c_and_irq_line(desc):
    chip = mkchip(desc)
    u = usb(chip)
    enable(chip)
    u.usb_reset(None, True, 0)
    assert rd(chip, ISTAT) == 0x01 and not u._line
    wr(chip, INTEN, 0x01)
    assert u._line
    wr(chip, ISTAT, 0x00)                    # writing 0 keeps it
    assert rd(chip, ISTAT) == 0x01
    wr(chip, ISTAT, 0x01)
    assert rd(chip, ISTAT) == 0 and not u._line
    # ERRSTAT bits reach ISTAT.ERROR only when enabled in ERREN
    u._err(0x20)
    assert rd(chip, ERRSTAT) == 0x20 and not rd(chip, ISTAT) & 0x02
    wr(chip, ERREN, 0x20)
    u._err(0x20)
    assert rd(chip, ISTAT) & 0x02


def test_setup_bdt_writeback_and_txsuspend(desc):
    chip = mkchip(desc)
    u = usb(chip)
    enable(chip)
    buf = prime(chip, 0, 0, 0, 64)
    req = bytes([0x80, 6, 0, 1, 0, 0, 18, 0])
    assert u.usb_setup(None, setup_tok(req)) == "ACK"
    c = bdctl(chip, 0, 0, 0)
    assert not c & OWN and (c >> 2) & 0xF == 0xD and (c >> 16) & 0x3FF == 8
    assert chip.bus.read_bytes(buf, 8) == req
    assert rd(chip, ISTAT) & 0x08 and rd(chip, STAT) == 0x00
    assert rd(chip, CTL) & 0x20                              # TXSUSPENDTOKENBUSY
    prime(chip, 0, 1, 0, 3, DATA1 | DTS, data=b"abc")
    assert u.usb_in(None, in_tok()) == "NAK"                 # token processing suspended
    wr(chip, CTL, 0x01)
    wr(chip, ISTAT, 0x08)
    assert rd(chip, STAT) == 0 and not rd(chip, ISTAT) & 0x08
    x = in_tok()
    assert u.usb_in(None, x) == ("DATA1", b"abc")
    assert bdctl(chip, 0, 1, 0) & OWN                        # committed only on the host's ACK
    u.usb_in_done(None, x, True, 0)
    c = bdctl(chip, 0, 1, 0)
    assert not c & OWN and (c >> 2) & 0xF == 0x9 and (c >> 16) == 3 and c & DATA1
    assert rd(chip, STAT) == 0x08                            # EP0, TX, even
    assert u.usb_in(None, in_tok()) == "NAK"                 # ping-pong now on the odd BD (not owned)
    prime(chip, 0, 1, 1, 0)
    x = in_tok()
    assert u.usb_in(None, x) == ("DATA0", b"")
    u.usb_in_done(None, x, True, 0)
    wr(chip, ISTAT, 0x08)
    assert rd(chip, STAT) == 0x0C                            # EP0, TX, odd


def test_stat_fifo_four_entries_then_nak(desc):
    chip = mkchip(desc)
    u = usb(chip)
    enable(chip)
    wr(chip, 0xC4, 0x1D)                                     # ENDPT1: bulk RX/TX, no SETUP
    for odd in (0, 1):
        prime(chip, 1, 0, odd, 64)
    stats = []
    for i in range(5):
        if i < 4:
            prime(chip, 1, 0, i & 1, 64, DTS | (DATA1 if i & 1 else 0))
        r = u.usb_out(None, out_tok(bytes([i]), "DATA1" if i & 1 else "DATA0"))
        stats.append(r)
    assert stats == ["ACK"] * 4 + ["NAK"]                    # STAT + 3 holding entries full
    seen = []
    while rd(chip, ISTAT) & 0x08:
        seen.append(rd(chip, STAT))
        wr(chip, ISTAT, 0x08)
    assert seen == [0x10, 0x14, 0x10, 0x14]
    assert rd(chip, STAT) == 0


def test_dts_mismatch_ignored_and_oversize_clipped(desc):
    chip = mkchip(desc)
    u = usb(chip)
    enable(chip)
    wr(chip, 0xC4, 0x1D)
    prime(chip, 1, 0, 0, 4, DTS)                             # expects DATA0, 4-byte buffer
    assert u.usb_out(None, out_tok(b"xx", "DATA1")) == "ACK"
    assert bdctl(chip, 1, 0, 0) & OWN and not rd(chip, ISTAT) & 0x08
    buf = BUF + 0x100 * 4
    assert u.usb_out(None, out_tok(b"123456", "DATA0")) == "ACK"
    c = bdctl(chip, 1, 0, 0)
    assert not c & OWN and (c >> 16) == 4 and (c >> 2) & 0xF == 0x1
    assert chip.bus.read_bytes(buf, 6)[:4] == b"1234" and rd(chip, ERRSTAT) & 0x20   # DMAERR


def test_oddrst_and_endpoint_stall(desc):
    chip = mkchip(desc)
    u = usb(chip)
    enable(chip)
    wr(chip, 0xC4, 0x1D)
    prime(chip, 1, 1, 0, 1, data=b"A")
    x = in_tok(ep=1)
    u.usb_in(None, x)
    u.usb_in_done(None, x, True, 0)
    assert u._odd[3] == 1
    wr(chip, CTL, 0x03)                                      # ODDRST
    assert u._odd == [0] * 32
    wr(chip, CTL, 0x01)
    wr(chip, ISTAT, 0xFF)
    # EPSTALL: STALL for any token on the endpoint, ISTAT.STALL
    wr(chip, 0xC4, 0x1F)
    assert u.usb_in(None, in_tok(ep=1)) == "STALL" and rd(chip, ISTAT) & 0x80
    wr(chip, 0xC4, 0x1D)
    # BDT_STALL: STALL, BD not consumed, EPSTALL set
    prime(chip, 1, 0, 0, 64, BSTALL)
    assert u.usb_out(None, out_tok(b"z")) == "STALL"
    assert bdctl(chip, 1, 0, 0) & OWN and rd(chip, 0xC4) & 0x02
    # disabled direction / endpoint: no answer
    wr(chip, 0xC4, 0x05)
    assert u.usb_out(None, out_tok(b"z")) is None
    assert u.usb_setup(None, setup_tok(ep=1)) is None


def test_address_match_and_bus_reset(desc):
    chip = mkchip(desc)
    u = usb(chip)
    enable(chip)
    wr(chip, ADDR, 5)
    prime(chip, 0, 0, 0, 64)
    assert u.usb_setup(None, setup_tok(addr=0)) is None
    u.usb_reset(None, True, 0)
    assert rd(chip, ADDR) == 0 and rd(chip, ISTAT) & 0x01
    assert u.usb_setup(None, setup_tok(addr=0)) == "ACK"
    u.usb_sof(None, 0x5A3, 0)
    assert rd(chip, 0xA0) == 0xA3 and rd(chip, 0xA4) == 0x5 and rd(chip, ISTAT) & 0x04


def test_clock_not_48mhz_no_answer_and_warn(desc):
    chip = mkchip(desc, clock=False)                         # FEI 20.97 MHz FLL, USBSRC = 0: no clock
    u = usb(chip)
    enable(chip)
    prime(chip, 0, 0, 0, 64)
    assert u.usb_setup(None, setup_tok()) is None
    assert any("48 MHz" in w and "does not answer" in w for w in warns(chip))
    pee(chip)
    usb_clock(chip, usbdiv=4, usbfrac=0)                     # 120 / 5 = 24 MHz
    assert u.usb_clock_hz() == pytest.approx(24e6)
    assert u.usb_setup(None, setup_tok()) is None
    usb_clock(chip)                                          # x 2/5 = 48 MHz
    assert u.usb_clock_hz() == pytest.approx(48e6)
    assert u.usb_setup(None, setup_tok()) == "ACK"


def test_gate_off_warns_and_ignores_bus(desc):
    chip = mkchip(desc, gate=False)
    u = usb(chip)
    enable(chip)
    assert any("SIM_SCGC4 bit 18" in w for w in warns(chip))
    assert u.usb_setup(None, setup_tok()) is None
    assert any("SCGC4[USBOTG]" in w for w in warns(chip))


def test_transceiver_suspended_ignores_tokens(desc):
    chip = mkchip(desc)
    u = usb(chip)
    enable(chip)
    wr(chip, 0x100, 0x80)                                    # USBCTRL.SUSP
    prime(chip, 0, 0, 0, 64)
    assert u.usb_setup(None, setup_tok()) is None
    assert any("USBCTRL.SUSP" in w for w in warns(chip))


def test_pullup_connects_through_bus(desc):
    chip = mkchip(desc)
    bus = UsbBus("USB1")
    bus.attach(usb(chip))
    bus.add_host(**FAST)
    enable(chip)
    assert bus.state == "detached" and not rd(chip, 0x104) & 0x80
    wr(chip, 0x108, 0x10)                                    # CONTROL.DPPULLUPNONOTG
    assert bus.state == "attached" and rd(chip, 0x104) & 0x80
    assert rd(chip, CTL) & 0x80                              # JSTATE
    wr(chip, 0x108, 0)                                       # soft disconnect
    assert bus.state == "detached"
    wr(chip, 0x1C, 0x84)                                     # OTGCTL: OTGEN | DPHIGH
    assert bus.state == "attached"
    wr(chip, 0x10C, 0x80)                                    # USBTRC0.USBRESET: module reset, reads 0
    assert rd(chip, 0x10C) == 0 and rd(chip, 0x1C) == 0 and bus.state == "detached"


# ------------------------------------------------------------------------------------------------ USBDCD
def _dcd_run(chip, plugged: bool):
    if plugged:
        bus = UsbBus("USB1")
        bus.attach(usb(chip))
        bus.add_host(**FAST)
    w = lambda o, v: chip.bus.write(DCD + o, 4, v)          # noqa: E731
    w(0x0, 1 << 25)                                          # SR
    w(0x4, (60 << 2) | 1)                                    # CLOCK: 60 MHz = the bus clock
    w(0x0, (1 << 16) | (1 << 24))                            # IE, START
    return lambda o: chip.bus.read(DCD + o, 4)               # noqa: E731


def test_usbdcd_standard_host(desc):
    chip = mkchip(desc)
    r = _dcd_run(chip, plugged=True)
    d = chip.peripherals["USBDCD"]
    assert r(0x8) & (1 << 22)                                # ACTIVE
    chip.run_for(10.5e6)                                     # TDCD_DBNC 10 ms
    assert (r(0x8) >> 18) & 3 == 1 and not r(0x0) & (1 << 8)
    assert r(0x10) & 0xFFF == 16 + 10                        # TUNITCON = TSEQ_INIT + elapsed
    chip.run_for(40e6)                                       # + 1 ms + TVDPSRC_ON 40 ms = 51 ms
    assert (r(0x8) >> 16) & 3 == 0
    chip.run_for(1e6)
    st = r(0x8)
    assert (st >> 16) & 3 == 1 and (st >> 18) & 3 == 2 and not st & (1 << 22) and not st & (1 << 20)
    assert r(0x0) & (1 << 8) and d._line
    chip.bus.write(DCD, 4, (1 << 16) | 1)                    # IACK
    assert not r(0x0) & (1 << 8) and not d._line
    chip.bus.write(DCD, 4, 1 << 25)                          # SR
    assert r(0x8) == 0 and r(0x10) & 0xFFF == 0


def test_usbdcd_timeout_without_cable(desc):
    chip = mkchip(desc)
    r = _dcd_run(chip, plugged=False)
    chip.run_for(983e6)
    assert not r(0x8) & (1 << 21)
    chip.run_for(2e6)                                        # TUNITCON reaches 1000 (16 + 984 ms)
    st = r(0x8)
    assert st & (1 << 21) and st & (1 << 20) and st & (1 << 22) and (st >> 18) & 3 == 0
    assert r(0x0) & (1 << 8)


# ------------------------------------------------------------------------------------------------ sample firmware
def _symbols() -> dict[str, int]:
    from elftools.elf.elffile import ELFFile
    with open(ELF, "rb") as f:
        st = ELFFile(f).get_section_by_name(".symtab")
        return {n: st.get_symbol_by_name(n)[0]["st_value"] for n in
                ("usb_resets", "usb_suspends", "usb_resumes", "usb_configured", "cdc_rx", "cdc_dtr", "usb_stalls")}


@pytest.fixture
def board(desc):
    if not ELF.exists():
        pytest.skip(f"{ELF} not built")
    chip = Chip(desc)
    chip.load_firmware(load_firmware(ELF))
    chip.reset()
    bus = UsbBus("USB1")
    bus.attach(usb(chip))
    host = bus.add_host(**FAST)
    syms = _symbols()
    return chip, bus, host, (lambda n: chip.bus.read(syms[n], 4))


def test_sample_enumerates_and_echoes(board):
    chip, bus, host, sym = board
    chip.run_for(12e6)
    dev = host.device
    assert dev is not None and dev.configured
    assert (dev.vid, dev.pid) == (0x1FC9, 0x0094)
    assert (dev.manufacturer, dev.product, dev.serial) == ("CHIP_EM", "K22 bare-metal CDC", "0001")
    assert dev.class_names() == ["CDC-ACM", "CDC-Data"]
    assert chip.clock.hz == pytest.approx(120e6)             # the firmware's PEE switch
    assert bytes(host.cdc.rx) == b"K22 CDC ready\r\n"
    assert sym("usb_configured") == 1 and sym("cdc_dtr") == 1 and sym("usb_resets") == 2
    host.cdc.take()
    host.cdc.write("hello\r\n")
    host.cdc.write("x" * 100 + "\n")                         # 2 OUT packets; echo needs 2 IN packets each
    chip.run_for(5e6)
    assert bytes(host.cdc.take()) == b"echo: hello\r\necho: " + b"x" * 100 + b"\n"
    assert sym("cdc_rx") == 108
    assert not warns(chip)
    assert host.summary()["failures"] == []


def test_sample_suspend_resume(board):
    chip, bus, host, sym = board
    chip.run_for(12e6)
    host.suspend()
    chip.run_for(5e6)
    assert sym("usb_suspends") == 1 and rd(chip, ISTAT) & 0x10 == 0
    host.resume()
    chip.run_for(4e6)
    assert sym("usb_resumes") == 1 and host.state == "configured"
    host.cdc.take()
    host.cdc.write("again\n")
    chip.run_for(3e6)
    assert bytes(host.cdc.take()) == b"echo: again\n"


def test_sample_unplug_replug_and_soft_disconnect(board):
    chip, bus, host, sym = board
    chip.run_for(12e6)
    host.unplug()
    chip.run_for(2e6)
    assert bus.state == "detached" and host.device is None
    host.plug()
    chip.run_for(12e6)
    assert host.state == "configured" and sym("usb_resets") == 4
    assert bytes(host.cdc.rx) == b"K22 CDC ready\r\n"        # DTR raised again by the new session
    wr(chip, 0x108, 0)                                       # what firmware would do: CONTROL.DPPULLUPNONOTG = 0
    chip.run_for(1e6)
    assert bus.state == "detached" and host.device is None
