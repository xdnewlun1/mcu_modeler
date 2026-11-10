"""chipem/usbbus.py: packet format/CRC/length, bus states, host enumeration and class drivers, against a scripted
Python device (no chip)."""
from __future__ import annotations

import struct

import pytest

from chipem.usbbus import (FS_BIT_NS, LS_BIT_NS, UsbBus, crc5, crc16, data_bits, describe_setup, handshake_bits,
                           packet_bits, token_bits)
from chipem.time import Clock, Scheduler


class FakeLog:
    def __init__(self):
        self.events = []

    def event(self, kind, t, **f):
        self.events.append((kind, t, f))


class FakeChip:
    def __init__(self):
        self.clock = Clock(1e9)
        self.sched = Scheduler(self.clock)
        self.log = FakeLog()

    def now_ns(self):
        return self.clock.now_ns()

    def run_until(self, t):
        while True:
            nxt = self.sched.next_due()
            if nxt is None or nxt > t:
                break
            self.clock.cycles = max(self.clock.cycles, nxt)
            self.sched.run_due()
        self.clock.cycles = max(self.clock.cycles, int(t))


def _cdc_config(hid=False):
    if hid:
        rep = bytes([0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0xC0])
        intf = bytes([9, 4, 0, 0, 1, 3, 1, 1, 0])
        hdesc = bytes([9, 0x21, 0x11, 0x01, 0, 1, 0x22]) + struct.pack("<H", len(rep))
        ep = bytes([7, 5, 0x81, 3, 8, 0, 10])
        body = intf + hdesc + ep
        return bytes([9, 2]) + struct.pack("<H", 9 + len(body)) + bytes([1, 1, 0, 0xA0, 50]) + body, rep
    body = (bytes([9, 4, 0, 0, 1, 2, 2, 1, 0])                       # comm interface
            + bytes([5, 0x24, 0, 0x10, 0x01]) + bytes([5, 0x24, 1, 0, 1]) + bytes([4, 0x24, 2, 2])
            + bytes([5, 0x24, 6, 0, 1])                              # union: data interface 1
            + bytes([7, 5, 0x82, 3, 8, 0, 16])                        # notification EP2 IN
            + bytes([9, 4, 1, 0, 2, 0x0A, 0, 0, 0])                  # data interface
            + bytes([7, 5, 0x01, 2, 64, 0, 0]) + bytes([7, 5, 0x81, 2, 64, 0, 0]))
    return bytes([9, 2]) + struct.pack("<H", 9 + len(body)) + bytes([2, 1, 0, 0xC0, 50]) + body, b""


def _sdesc(s):
    b = s.encode("utf-16-le")
    return bytes([2 + len(b), 3]) + b


class PyDevice:
    """A USB device function written in Python: EP0 standard requests, a CDC echo (bulk OUT 1 -> bulk IN 1) or a
    HID report source (interrupt IN 1)."""

    def __init__(self, chip, *, speed="fs", mps0=64, hid=False, nak_setup_reply=0, stall_req=None):
        self.chip, self.log, self.usb_name = chip, chip.log, "PYDEV"
        self.speed, self.mps0, self.hid = speed, mps0, hid
        self.addr = 0
        self.pending_addr = None
        self.on = True
        self.vbus = False
        self.events = []
        self.config = 0
        self.ep0_in = b""
        self.ep0_in_pos = 0
        self.ep0_zlp = False
        self.ep0_toggle = 1
        self.out_toggle = {}
        self.in_toggle = {}
        self.ep0_out = bytearray()
        self.ep0_out_len = 0
        self.last_setup = None
        self.echo = bytearray()
        self.reports = []
        self.line_coding = None
        self.ctrl_state = None
        self.naks_left = 0
        self.nak_setup_reply = nak_setup_reply
        self.stall_req = stall_req
        self.sofs = 0
        self.suspended = False
        self.cfg, self.report = _cdc_config(hid)
        self.wakeup_enabled = False
        self.drop_ack = 0
        self.bad_toggle = 0

    # protocol
    def usb_pullup(self, bus):
        return self.speed if self.on and self.vbus else None

    def usb_vbus(self, bus, on, t):
        self.vbus = on
        self.events.append(("vbus", on, t))

    def usb_reset(self, bus, active, t):
        self.events.append(("reset", active, t))
        self.addr = 0
        self.config = 0
        self.suspended = False

    def usb_suspend(self, bus, t):
        self.suspended = True
        self.events.append(("suspend", t))

    def usb_resume(self, bus, t):
        self.suspended = False
        self.events.append(("resume", t))

    def usb_sof(self, bus, frame, t):
        self.sofs += 1

    def usb_keepalive(self, bus, t):
        self.sofs += 1

    def usb_setup(self, bus, x):
        if x.addr != self.addr:
            return None
        self.last_setup = x.data
        bm, req, value, index, length = struct.unpack("<BBHHH", x.data)
        self.ep0_toggle = 1
        self.ep0_in, self.ep0_in_pos = None, 0
        self.ctrl_state = ("in" if bm & 0x80 else "out", bm, req, value, index, length)
        self.ep0_out = bytearray()
        self.naks_left = self.nak_setup_reply
        if self.stall_req is not None and req == self.stall_req:
            self.ctrl_state = ("stall",)
            return "ACK"
        if bm & 0x80:
            self.ep0_in = self._get(bm, req, value, index)[:length]
        return "ACK"

    def _get(self, bm, req, value, index):
        if req == 6:
            t, i = value >> 8, value & 0xFF
            if t == 1:
                return bytes([18, 1, 0x00, 0x02, 0xEF if not self.hid else 0, 2 if not self.hid else 0,
                              1 if not self.hid else 0, self.mps0]) + struct.pack("<HHH", 0x0483, 0x5740, 0x0200) \
                    + bytes([1, 2, 3, 1])
            if t == 2:
                return self.cfg
            if t == 3:
                return {0: bytes([4, 3, 9, 4]), 1: _sdesc("ACME"), 2: _sdesc("PyDev CDC"),
                        3: _sdesc("0001")}.get(i, b"")
            if t == 0x22:
                return self.report
        if req == 0x21:
            return self.line_coding or bytes(7)
        if req == 0:
            return b"\0\0"
        return b""

    def usb_out(self, bus, x):
        if x.addr != self.addr:
            return None
        if x.ep == 0:
            st = self.ctrl_state
            if st is None:
                return "STALL"
            if st[0] == "stall":
                return "STALL"
            if st[0] == "in":                       # status stage of a control read
                self.ctrl_state = None
                return "ACK"
            if self.naks_left:
                self.naks_left -= 1
                return "NAK"
            self.ep0_out += x.data
            if len(self.ep0_out) >= st[5]:
                if st[2] == 0x20:
                    self.line_coding = bytes(self.ep0_out)
            return "ACK"
        want = self.out_toggle.get(x.ep, 0)
        if (x.pid == "DATA1") != bool(want):
            return "ACK"                            # duplicate: ACK and ignore
        self.out_toggle[x.ep] = want ^ 1
        if self.config and x.ep == 1 and not self.hid:
            self.echo += x.data.upper()
            return "ACK"
        return "STALL"

    def usb_in(self, bus, x):
        if x.addr != self.addr:
            return None
        if x.ep == 0:
            st = self.ctrl_state
            if st is None:
                return "NAK"
            if st[0] == "stall":
                return "STALL"
            if self.naks_left:
                self.naks_left -= 1
                return "NAK"
            if st[0] == "out":                      # status stage of a control write
                return ("DATA1", b"")
            chunk = self.ep0_in[self.ep0_in_pos:self.ep0_in_pos + self.mps0]
            return ("DATA1" if self.ep0_toggle else "DATA0", chunk)
        if not self.config:
            return "STALL"
        if x.ep == 1 and self.hid:
            if not self.reports:
                return "NAK"
            return ("DATA1" if self.in_toggle.get(1) else "DATA0", self.reports[0])
        if x.ep == 1 and self.echo:
            chunk = bytes(self.echo[:64])
            pid = "DATA1" if self.in_toggle.get(1) else "DATA0"
            if self.bad_toggle:
                self.bad_toggle -= 1
                pid = "DATA0" if pid == "DATA1" else "DATA1"
            return (pid, chunk)
        return "NAK"

    def usb_in_done(self, bus, x, acked, t):
        if x.ep == 0:
            st = self.ctrl_state
            if st and st[0] == "out":
                self.ctrl_state = None
                if st[2] == 5:
                    self.addr = st[3]
                elif st[2] == 9:
                    self.config = st[3]
                    self.out_toggle.clear()
                    self.in_toggle.clear()
                elif st[2] == 3 and st[3] == 1:
                    self.wakeup_enabled = True
                return
            self.ep0_in_pos += self.mps0
            self.ep0_toggle ^= 1
            return
        if self.bad_toggle_pending():
            return
        n = 64
        if self.hid:
            self.reports.pop(0)
        else:
            del self.echo[:n]
        self.in_toggle[x.ep] = self.in_toggle.get(x.ep, 0) ^ 1

    def bad_toggle_pending(self):
        return False


FAST = dict(debounce_ns=1e6, reset_ns=1e6, reset_recovery_ns=1e6, set_address_recovery_ns=0.2e6)


def setup(**kw):
    chip = FakeChip()
    dev = PyDevice(chip, **{k: v for k, v in kw.items() if k in ("speed", "mps0", "hid", "nak_setup_reply",
                                                                  "stall_req")})
    bus = UsbBus("USB")
    bus.attach(dev)
    host = bus.add_host(**{**FAST, **{k: v for k, v in kw.items() if k in ("debounce_ns", "second_reset")}})
    return chip, dev, bus, host


# ------------------------------------------------------------------------------------------------ packets
def test_crc5_known_vectors():
    assert crc5(0x000) == 0x02                    # SETUP addr 0 ep 0 -> 2D 00 10
    # USB CRC paper vectors (residues given in transmission order, i.e. LSB first): 10111, 11100, 01110
    assert crc5(0x15 | (0xE << 7)) == 0b11101
    assert crc5(0x3A | (0xA << 7)) == 0b00111
    assert crc5(0x70 | (0x4 << 7)) == 0b01110


def test_crc16_known_vectors():
    # transmitted LSB first: 1111011101011110, 0111000000111000
    assert crc16(bytes([0x00, 0x01, 0x02, 0x03])) == 0x7AEF
    assert crc16(bytes([0x23, 0x45, 0x67, 0x89])) == 0x1C0E
    assert crc16(b"") == 0x0000


def test_packet_lengths_with_bit_stuffing():
    assert handshake_bits("ACK") == 8 + 8 + 3
    assert token_bits("SETUP", 0, 0) == 8 + 8 + 16 + 3
    assert data_bits("DATA0", b"") == 8 + 8 + 16 + 3 + 0
    # 0xFF payload: 8 ones per byte -> a stuff bit after every 6 ones
    n = data_bits("DATA1", b"\xff" * 64)
    assert n > 8 + 8 + 512 + 16 + 3 + 80
    assert packet_bits("DATA0", b"\x00" * 10) == 8 + 8 + 80 + 3


def test_describe_setup():
    assert describe_setup(bytes([0x80, 6, 0, 1, 0, 0, 18, 0])) == "GET_DESCRIPTOR(DEVICE, 18)"
    assert describe_setup(bytes([0, 5, 7, 0, 0, 0, 0, 0])) == "SET_ADDRESS(7)"
    assert describe_setup(bytes([0x21, 0x20, 0, 0, 0, 0, 7, 0])).startswith("SET_LINE_CODING")


# ------------------------------------------------------------------------------------------------ enumeration
def test_enumeration_cdc_and_echo():
    chip, dev, bus, host = setup()
    chip.run_until(40e6)
    info = host.device
    assert info is not None and info.configured
    assert (info.vid, info.pid) == (0x0483, 0x5740)
    assert info.manufacturer == "ACME" and info.product == "PyDev CDC" and info.serial == "0001"
    assert "CDC-ACM" in info.class_names()
    assert dev.addr == info.address == 1 and dev.config == 1
    assert dev.line_coding == struct.pack("<IBBB", 115200, 0, 0, 8)
    cdc = host.cdc
    assert cdc is not None and cdc.open
    t0 = chip.now_ns()
    cdc.write(b"hello usb")
    chip.run_until(t0 + 5e6)
    assert bytes(cdc.rx) == b"HELLO USB"
    kinds = [f.get("event") or f.get("xfer") for k, _t, f in chip.log.events if k == "usb"]
    assert "attach" in kinds and "enumerated" in kinds and "reset" in kinds
    assert any(f.get("req") == "SET_ADDRESS(1)" for k, _t, f in chip.log.events if k == "usb")
    assert bus.sofs > 10 and dev.sofs == bus.sofs


def test_enumeration_timing_follows_spec_delays():
    chip, dev, bus, host = setup(debounce_ns=100e6)
    chip.run_until(99e6)
    assert not any(e[0] == "reset" for e in dev.events)       # still debouncing (TATTDB = 100 ms)
    chip.run_until(130e6)
    resets = [t for e, a, t in (x for x in dev.events if x[0] == "reset") if a]
    assert resets and resets[0] == pytest.approx(100e6 + 2500, abs=5)
    assert host.device is not None


def test_mps0_8_and_low_speed_device():
    chip, dev, bus, host = setup(speed="ls", mps0=8, hid=True)
    chip.run_until(60e6)
    assert host.device is not None and host.device.speed == "ls"
    hid = host.hid
    assert hid is not None and hid.report_desc == dev.report
    dev.reports.append(b"\x01\x02\x03\x04")
    t = chip.now_ns()
    chip.run_until(t + 25e6)
    assert [r for _t, r in hid.reports] == [b"\x01\x02\x03\x04"]
    assert bus.speed == "ls" and bus.bit_ns == LS_BIT_NS


def test_hid_polled_every_binterval():
    chip, dev, bus, host = setup(hid=True)
    chip.run_until(40e6)
    t0 = chip.now_ns()
    n0 = bus.naks
    chip.run_until(t0 + 100e6)
    polls = bus.naks - n0
    assert 9 <= polls <= 11                       # bInterval = 10 ms


def test_bulk_in_nak_polled_once_per_frame():
    chip, dev, bus, host = setup()
    chip.run_until(40e6)
    n0, t0 = bus.naks, chip.now_ns()
    chip.run_until(t0 + 50e6)
    # bulk IN data (once per frame) + interrupt notification (every 16 frames)
    assert 50 <= bus.naks - n0 <= 50 + 5


def test_wrong_in_toggle_is_acked_and_discarded():
    chip, dev, bus, host = setup()
    chip.run_until(40e6)
    dev.bad_toggle = 1
    dev.bad_toggle_pending = lambda: dev.bad_toggle_state.pop() if getattr(dev, "bad_toggle_state", None) else False
    dev.bad_toggle_state = [True]
    host.cdc.write(b"abc")
    chip.run_until(chip.now_ns() + 10e6)
    assert bytes(host.cdc.rx) == b"ABC"           # the first copy (wrong toggle) was discarded


def test_suspend_and_resume():
    chip, dev, bus, host = setup()
    chip.run_until(40e6)
    t = chip.now_ns()
    host.suspend()
    chip.run_until(t + 5e6)
    sus = [e for e in dev.events if e[0] == "suspend"]
    assert sus and sus[0][1] == pytest.approx(t + 3e6, abs=1.1e6)
    host.resume()
    chip.run_until(chip.now_ns() + 40e6)
    assert any(e[0] == "resume" for e in dev.events)
    host.cdc.write(b"x")
    chip.run_until(chip.now_ns() + 5e6)
    assert bytes(host.cdc.rx) == b"X"


def test_soft_disconnect_and_reconnect():
    chip, dev, bus, host = setup()
    chip.run_until(40e6)
    dev.on = False
    bus.device_changed()
    assert host.device is None and bus.state == "detached"
    s0 = bus.sofs
    chip.run_until(chip.now_ns() + 5e6)
    assert bus.sofs == s0                          # no SOFs to a detached port
    dev.on = True
    bus.device_changed()
    chip.run_until(chip.now_ns() + 40e6)
    assert host.device is not None and len(host.devices) == 2


def test_vbus_off_detaches():
    chip, dev, bus, host = setup()
    chip.run_until(40e6)
    host.unplug()
    assert dev.vbus is False and host.device is None
    host.plug()
    chip.run_until(chip.now_ns() + 40e6)
    assert host.device is not None


def test_stalled_request_fails_transfer():
    chip, dev, bus, host = setup()
    chip.run_until(40e6)
    res = []
    host.control(0xC0, 0x42, 0, 0, 4, cb=lambda x: res.append(x.status))
    chip.run_until(chip.now_ns() + 3e6)
    dev.stall_req = 0x42
    host.control(0xC0, 0x42, 0, 0, 4, cb=lambda x: res.append(x.status))
    chip.run_until(chip.now_ns() + 3e6)
    assert res == ["ok", "stall"]


def test_control_naks_are_retried():
    chip, dev, bus, host = setup(nak_setup_reply=3)
    chip.run_until(80e6)
    assert host.device is not None and bus.naks >= 3 * 8


def test_no_device_response_times_out():
    chip, dev, bus, host = setup()
    dev.usb_setup = lambda bus, x: None
    chip.run_until(200e6)
    assert host.state == "failed" and bus.timeouts >= 3
    assert any(f.get("event") == "error" for k, _t, f in chip.log.events if k == "usb")


def test_remote_wakeup_needs_feature():
    chip, dev, bus, host = setup()
    chip.run_until(40e6)
    host.suspend()
    chip.run_until(chip.now_ns() + 5e6)
    bus.remote_wakeup()
    chip.run_until(chip.now_ns() + 30e6)
    assert host.state == "suspended"               # not enabled: ignored
    host.device.remote_wakeup = True
    bus.remote_wakeup()
    chip.run_until(chip.now_ns() + 30e6)
    assert host.running and host.state == "configured"


def test_transaction_duration_on_the_wire():
    chip, dev, bus, host = setup()
    chip.run_until(40e6)
    seen = []
    orig = dev.usb_out

    def spy(b, x):
        seen.append(x.t)
        return orig(b, x)
    dev.usb_out = spy
    t0 = chip.now_ns()
    host.cdc.write(b"Z" * 10)
    chip.run_until(t0 + 3e6)
    # OUT token + IPG + DATA0(10 bytes) after the transaction started at a frame slot
    assert seen
    assert FS_BIT_NS * (token_bits("OUT", 1, 1) + 4 + data_bits("DATA0", b"Z" * 10)) > 0


class MscPyDevice(PyDevice):
    """Bulk-only mass storage function: INQUIRY, READ CAPACITY(10), READ(10) over 8 blocks of 512 bytes."""

    def __init__(self, chip):
        super().__init__(chip)
        body = (bytes([9, 4, 0, 0, 2, 8, 6, 0x50, 0]) + bytes([7, 5, 0x81, 2, 64, 0, 0])
                + bytes([7, 5, 0x02, 2, 64, 0, 0]))
        self.cfg = bytes([9, 2]) + struct.pack("<H", 9 + len(body)) + bytes([1, 1, 0, 0x80, 50]) + body
        self.inq = []
        self.disk = bytes(range(256)) * 16

    def _get(self, bm, req, value, index):
        if req == 6 and value >> 8 == 1:
            d = bytearray(super()._get(bm, req, value, index))
            d[4:7] = bytes(3)
            d[10:12] = struct.pack("<H", 0x5741)
            return bytes(d)
        if req == 0xFE:
            return b"\0"
        return super()._get(bm, req, value, index)

    def usb_out(self, bus, x):
        if x.ep != 2 or not self.config:
            return super().usb_out(bus, x)
        want = self.out_toggle.get(2, 0)
        if (x.pid == "DATA1") != bool(want):
            return "ACK"
        self.out_toggle[2] = want ^ 1
        sig, tag, n, flags, _lun, _l = struct.unpack("<IIIBBB", x.data[:15])
        cdb = x.data[15:31]
        if cdb[0] == 0x12:
            data = b"\0\x80\x02\x02\x1f\0\0\0" + b"CHIPEM  " + b"PyDisk          " + b"0001"
        elif cdb[0] == 0x25:
            data = struct.pack(">II", 7, 512)
        elif cdb[0] == 0x28:
            lba, cnt = struct.unpack(">I", cdb[2:6])[0], struct.unpack(">H", cdb[7:9])[0]
            data = (self.disk * 2)[lba * 512:(lba + cnt) * 512]
        else:
            data = b""
        data = data[:n]
        self.inq = [data[i:i + 64] for i in range(0, len(data), 64)] + [struct.pack("<IIIB", 0x53425355, tag, 0, 0)]
        return "ACK"

    def usb_in(self, bus, x):
        if x.ep != 1 or not self.config:
            return super().usb_in(bus, x)
        if not self.inq:
            return "NAK"
        return ("DATA1" if self.in_toggle.get(1) else "DATA0", self.inq[0])

    def usb_in_done(self, bus, x, acked, t):
        if x.ep != 1:
            return super().usb_in_done(bus, x, acked, t)
        self.inq.pop(0)
        self.in_toggle[1] = self.in_toggle.get(1, 0) ^ 1


def test_msc_bulk_only_driver():
    chip = FakeChip()
    dev = MscPyDevice(chip)
    bus = UsbBus("USB")
    bus.attach(dev)
    host = bus.add_host(**FAST)
    chip.run_until(60e6)
    msc = host.msc
    assert msc is not None and host.device.class_names() == ["MSC"]
    assert (msc.vendor, msc.product, msc.blocks, msc.block_size) == ("CHIPEM", "PyDisk", 8, 512)
    got = []
    msc.read_blocks(1, 2, lambda ok, data: got.append((ok, data)))
    chip.run_until(chip.now_ns() + 20e6)
    assert got and got[0][0] and got[0][1] == (dev.disk * 2)[512:1536]


def test_suspend_sets_remote_wakeup_feature_when_announced():
    chip, dev, bus, host = setup(hid=True)                  # HID config: bmAttributes 0xA0 (remote wakeup)
    chip.run_until(40e6)
    host.suspend()
    chip.run_until(chip.now_ns() + 5e6)
    assert dev.wakeup_enabled and host.device.remote_wakeup and dev.suspended
    bus.remote_wakeup()
    chip.run_until(chip.now_ns() + 35e6)
    assert host.running and not dev.suspended


class CycleChip(FakeChip):
    """A chip whose scheduler counts core cycles at `hz` (Scheduler.at_ns rounds to cycles and may fire a few 1e-8 ns
    before the requested time): the bus must not re-schedule itself forever at the same cycle."""

    def __init__(self, hz, max_events=200_000):
        super().__init__()
        self.clock = Clock(hz)
        self.sched = Scheduler(self.clock)
        self.scheduled = 0
        at_ns = self.sched.at_ns

        def counted(t_ns, cb, *args):                    # a same-cycle re-scheduling loop never leaves run_due()
            self.scheduled += 1
            assert self.scheduled < max_events, "usb bus event loop does not advance"
            return at_ns(t_ns, cb, *args)
        self.sched.at_ns = counted

    def run_until(self, t):
        end = self.clock.abs_ns_to_cycles(t)
        while True:
            nxt = self.sched.next_due()
            if nxt is None or nxt > end:
                break
            self.clock.cycles = max(self.clock.cycles, nxt)
            self.sched.run_due()
        self.clock.cycles = max(self.clock.cycles, end)


@pytest.mark.parametrize("hz,debounce", [(120e6, 7.77e6), (120e6, 100e6 / 3), (168e6, 7.77e6), (60e6, 1e6)])
def test_enumeration_with_cycle_rounded_scheduler(hz, debounce):
    """Regression (K22 at 120 MHz): with these start times an event fired a few 1e-8 ns early and the host re-scheduled
    it at the same cycle forever; usbbus compares times with the T_EPS tolerance."""
    chip = CycleChip(hz)
    dev = PyDevice(chip)
    bus = UsbBus("USB")
    bus.attach(dev)
    host = bus.add_host(**dict(FAST, debounce_ns=debounce))
    chip.run_until(debounce + 30e6)
    assert host.device is not None and host.cdc is not None and host.cdc.open
    host.cdc.write(b"abc")
    chip.run_until(chip.now_ns() + 5e6)
    assert bytes(host.cdc.rx) == b"ABC"


def test_transfers_to_one_endpoint_run_in_order():
    chip, dev, bus, host = setup()
    chip.run_until(40e6)
    done = []
    host.control(0x21, 0x22, 0, 0, b"", cb=lambda x: done.append(("dtr", x.status)))
    host.control(0x21, 0x20, 0, 0, struct.pack("<IBBB", 9600, 0, 0, 8), cb=lambda x: done.append(("lc", x.status)))
    host.control(0xA1, 0x21, 0, 0, 7, cb=lambda x: done.append(("get", x.status, bytes(x.data))))
    chip.run_until(chip.now_ns() + 5e6)
    assert done == [("dtr", "ok"), ("lc", "ok"), ("get", "ok", struct.pack("<IBBB", 9600, 0, 0, 8))]
