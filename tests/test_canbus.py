"""chipem/canbus.py: frame format/length, arbitration, ACK and error confinement, on scripted nodes (no chips), and
the board `can` link syntax."""
from __future__ import annotations

import pytest

from chipem.canbus import (CanBus, CanErrorCounters, CanFrame, ERROR_FRAME, INTERMISSION, crc15, frame_bits,
                           parse_frame)
from chipem.time import Clock, Scheduler

BIT = 2000.0                                   # 500 kbit/s


class FakeChip:
    def __init__(self):
        self.clock = Clock(1e9)                 # 1 ns cycles
        self.sched = Scheduler(self.clock)
        self.log = None

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


class Node:
    """Scripted controller: a queue of frames, an ACK switch, error counters."""

    def __init__(self, chip, name, bit=BIT, state="active", connected=True):
        self.chip, self.can_name, self.bit, self.state, self.connected = chip, name, bit, state, connected
        self.log = None
        self.queue: list[CanFrame] = []
        self.rx: list[tuple[float, float, CanFrame]] = []
        self.done: list[tuple[float, float, bool, str | None, CanFrame]] = []
        self.rx_errors: list[tuple[float, str]] = []
        self.errors = CanErrorCounters()
        self.bus = None

    def send(self, bus, frame, t=None):
        self.bus = bus
        self.queue.append(frame)
        bus.request(self, t)

    def can_bit_ns(self):
        return self.bit

    def can_tx_frame(self, bus):
        return min(self.queue, key=lambda f: f.arb) if self.queue else None

    def can_tx_connected(self, bus):
        return self.connected

    def can_tx_done(self, bus, frame, ok, err, t_sof, t):
        self.done.append((t_sof, t, ok, err, frame))
        if ok:
            self.queue.remove(frame)
            self.errors.tx_ok()
        else:
            self.errors.tx_error(err)
        if self.queue:
            bus.request(self, t)

    def can_rx_state(self, bus):
        return self.state

    def can_rx(self, bus, frame, t_sof, t):
        self.rx.append((t_sof, t, frame))

    def can_rx_error(self, bus, kind, t):
        self.rx_errors.append((t, kind))

    def can_error_passive(self):
        return self.errors.state == "passive"


def setup(*names, **kw):
    chip = FakeChip()
    bus = CanBus("TEST")
    nodes = [Node(chip, n, **kw) for n in names]
    for n in nodes:
        bus.attach(n)
    return chip, bus, nodes


# ------------------------------------------------------------------------------------------------ frames
def test_crc15_check_value():
    bits = [int(c) for byte in b"123456789" for c in format(byte, "08b")]
    assert crc15(bits) == 0x059E                 # CRC-15/CAN catalogue check value


@pytest.mark.parametrize("frame,base", [(CanFrame(0x123, b"\xde\xad\xbe\xef"), 44 + 32),
                                        (CanFrame(0x555, b""), 44),
                                        (CanFrame(0x1ABCDEF0, b"\x01\x02", ext=True), 64 + 16),
                                        (CanFrame(0x7FF, rtr=True, dlc=8), 44)])
def test_frame_length_is_base_plus_stuff_bits(frame, base):
    total, stuffed, _crc = frame_bits(frame.id, frame.ext, frame.rtr, frame.dlc, frame.data)
    assert total == base + stuffed == frame.bits
    assert 0 <= stuffed <= (base - 10 - 1) // 4


def test_frame_stuffing_examples():
    assert CanFrame(0x123, b"\xde\xad\xbe\xef").bits == 78          # 76 + 2 stuff bits
    assert CanFrame(0, bytes(8)).bits == 108 + 16                    # long runs of dominant bits
    assert CanFrame(0x7FF, rtr=True, dlc=8).data == b""             # remote frames carry no data


def test_parse_and_format():
    f = parse_frame("123#DEADBEEF")
    assert (f.id, f.ext, f.rtr, f.dlc, f.data) == (0x123, False, False, 4, bytes.fromhex("DEADBEEF"))
    assert str(f) == "123#DEADBEEF"
    e = parse_frame("00000123#01")
    assert e.ext and str(e) == "00000123#01"
    r = parse_frame("321#R2")
    assert r.rtr and r.dlc == 2 and str(r) == "321#R2"
    with pytest.raises(ValueError):
        parse_frame("123")


def test_arbitration_value_order():
    std, std_rtr, ext = CanFrame(0x100), CanFrame(0x100, rtr=True), CanFrame(0x100 << 18, ext=True)
    assert std.arb < std_rtr.arb < ext.arb or std.arb < ext.arb
    assert std.arb < ext.arb                    # RTR (dominant) beats SRR (recessive)
    assert CanFrame(0x0FF).arb < CanFrame(0x100).arb


# ------------------------------------------------------------------------------------------------ timing
def test_frame_time_and_delivery():
    chip, bus, (a, b) = setup("A", "B")
    f = CanFrame(0x123, b"\xde\xad\xbe\xef")
    chip.run_until(1000)
    a.send(bus, f)
    chip.run_until(1e6)
    end = 1000 + 78 * BIT
    assert b.rx == [(1000, end, f)]
    assert a.done == [(1000, end, True, None, f)]
    assert bus.frames == 1 and bus.error_frames == 0


def test_back_to_back_frames_keep_intermission():
    chip, bus, (a, b) = setup("A", "B")
    f1, f2 = CanFrame(0x10, b"\x01"), CanFrame(0x11, b"\x02")
    a.send(bus, f1)
    a.send(bus, f2)
    chip.run_until(1e6)
    (s1, e1, *_), (s2, e2, *_) = a.done
    assert s2 == e1 + INTERMISSION * BIT
    assert e2 == s2 + f2.bits * BIT


def test_lower_id_wins_and_loser_retries_after_winner():
    chip, bus, (a, b, c) = setup("A", "B", "C")
    fa, fb = CanFrame(0x200, b"\xaa"), CanFrame(0x100, b"\xbb")
    a.queue.append(fa)
    b.queue.append(fb)
    bus.request(a)
    bus.request(b)                               # same bit time: arbitration by ID
    chip.run_until(1e6)
    (sb, eb, okb, _e, _f), = b.done
    (sa, ea, oka, _e, _f), = a.done
    assert okb and oka and sb == 0 and sa == eb + INTERMISSION * BIT
    assert [f for _s, _t, f in c.rx] == [fb, fa]


def test_late_request_within_the_sof_bit_still_arbitrates():
    chip, bus, (a, b, c) = setup("A", "B", "C")
    chip.run_until(10_000)
    a.send(bus, CanFrame(0x300, b"\x01"))
    b.send(bus, CanFrame(0x100, b"\x02"), t=9_000)     # a lagging chip asks 0.5 bit earlier: same SOF bit
    chip.run_until(2e6)
    assert [f.id for _s, _t, f in c.rx] == [0x100, 0x300]
    assert b.done[0][0] == 9_000


def test_earlier_requester_wins_regardless_of_id():
    chip, bus, (a, b, c) = setup("A", "B", "C")
    chip.run_until(10_000)
    a.send(bus, CanFrame(0x100, b"\x01"))
    b.send(bus, CanFrame(0x300, b"\x02"), t=4_000)     # 3 bits earlier: A would have seen a busy bus
    chip.run_until(2e6)
    assert [f.id for _s, _t, f in c.rx] == [0x300, 0x100]


# ------------------------------------------------------------------------------------------------ errors
def test_no_ack_error_frames_and_error_passive():
    chip, bus, (a,) = setup("A")
    f = CanFrame(0x123, b"\x01")
    a.send(bus, f)
    chip.run_until(20e6)
    fails = [d for d in a.done if not d[2]]
    assert fails and all(d[3] == "ack" for d in fails)
    t_ack0 = (f.bits - 8) * BIT
    assert fails[0][1] == t_ack0
    # error active: next SOF after the error frame and intermission
    assert fails[1][0] == t_ack0 + (ERROR_FRAME + INTERMISSION) * BIT
    # 16 ACK errors -> TEC 128 error passive; then ACK errors no longer count, and suspend adds 8 bits
    assert a.errors.tec == 128 and a.errors.state == "passive"
    k = 17
    assert fails[k][0] - fails[k - 1][1] == (ERROR_FRAME + INTERMISSION + 8) * BIT
    assert bus.frames == 0 and bus.error_frames == len(fails)


def test_bitrate_mismatch_is_a_form_error_and_no_ack():
    chip, bus, (a, b) = setup("A", "B")
    b.bit = 4000.0
    a.send(bus, CanFrame(0x1, b"\x01"))
    chip.run_until(400_000)
    assert not b.rx and b.rx_errors and b.rx_errors[0][1] == "form"
    assert a.done[0][3] == "ack"


def test_listen_only_node_neither_acks_nor_sees_unacked_frames():
    chip, bus, (a, lom) = setup("A", "L")
    lom.state = "listen"
    a.send(bus, CanFrame(0x5, b"\x05"))
    chip.run_until(300_000)
    assert not lom.rx and lom.rx_errors[0][1] == "noack"
    c = Node(chip, "C")
    bus.attach(c)                                  # someone acknowledges now
    chip.run_until(2e6)
    assert lom.rx and c.rx and a.done[-1][2]


def test_unconnected_transmitter_fails_locally_and_bus_stays_free():
    chip, bus, (a, b, c) = setup("A", "B", "C")
    a.connected = False
    a.send(bus, CanFrame(0x1, b"\x01"))
    b.send(bus, CanFrame(0x7FF, b"\x02"))
    chip.run_until(400_000)
    assert a.done[0][2] is False and a.done[0][3] == "bit0" and a.done[0][1] - a.done[0][0] == BIT
    assert [f.id for _s, _t, f in c.rx] == [0x7FF]


def test_error_counter_rules():
    e = CanErrorCounters()
    for _ in range(11):
        e.tx_error("bit")
    assert e.tec == 88 and e.state == "active"
    assert e.tx_error("bit") == {"tx_warn"}        # 96
    for _ in range(3):
        e.tx_error("bit")
    assert e.tx_error("bit") == {"passive"} and e.tec == 128
    assert e.tx_error("ack") == set() and e.tec == 128      # exception 1
    for _ in range(15):
        e.tx_error("bit")
    assert e.state == "passive" and e.tec == 248
    assert e.tx_error("bit") == {"busoff"}
    assert e.state == "busoff" and e.tec & 0xFF == 0
    assert e.recover() == {"active"} and (e.tec, e.rec) == (0, 0)
    for _ in range(200):
        e.rx_error()
    assert e.rec == 128 and e.state == "passive"
    e.rx_ok()
    assert e.rec == 127 and e.state == "active"


def test_host_inject_and_listeners():
    chip, bus, (a,) = setup("A")
    seen = []
    bus.listeners.append(lambda b, f, info: seen.append((info["t_ns"], str(f), info["node"])))
    bus.inject("321#0102", 5_000)
    chip.run_until(1e6)
    f = CanFrame(0x321, b"\x01\x02")
    assert a.rx == [(5_000, 5_000 + f.bits * BIT, f)]
    assert seen == [(5_000 + f.bits * BIT, "321#0102", "host")]
    a.send(bus, CanFrame(0x42, b"\x42"))
    chip.run_until(2e6)
    host = bus.nodes[-1]
    assert [str(f) for _t, f in host.received] == ["042#42"] and a.done[-1][2]


# ------------------------------------------------------------------------------------------------ board syntax
def test_board_can_link_syntax():
    from chipem.board import parse_board
    bd = parse_board({"chips": [{"id": "A", "chip": "mk22fn1m0vll12"}, {"id": "B", "chip": "tms320f28232"}],
                      "links": [{"can": ["A.CAN0", "B.ecana"]},
                                {"type": "can", "name": "BUS2", "nodes": ["B.ECANB"], "host": True},
                                {"type": "uart", "from": "A.UART0", "to": "B.SCIA"}]})
    can = [ln for ln in bd.links if ln.type == "can"]
    assert can[0].nodes == [("A", "CAN0"), ("B", "ECANA")] and can[0].name is None
    assert can[1].nodes == [("B", "ECANB")] and can[1].name == "BUS2" and can[1].host
    assert [ln.type for ln in bd.links] == ["can", "can", "uart"]
    with pytest.raises(ValueError):
        parse_board({"chips": [{"id": "A"}], "links": [{"type": "can"}]})
