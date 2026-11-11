"""Serial cable chain parts (chipem/parts/serial.py): K22 console -> rs232_transceiver -> RJ50 jack -> DB9-RJ50 cable
-> usb_serial_adapter, with an in-memory host. A correct chain passes bytes both ways; a swapped cable, a missing
transceiver, a missing ground wire, an unplugged cable or a baud mismatch fail the way the hardware would."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from chipem.board import parse_board
from chipem.hostserial import LineSettings
from chipem.session import Session

ROOT = Path(__file__).resolve().parent.parent
BOARD = ROOT / "boards" / "k22_rs232_console.yaml"
FW = ROOT / "firmware" / "samples" / "build" / "uart_console.elf"
pytestmark = pytest.mark.skipif(not FW.exists(), reason="uart_console sample not built")

BANNER = b"\r\nCHIP_EM K22 console - type help\r\n> "


def board_data() -> dict:
    d = yaml.safe_load(BOARD.read_text())
    for p in d["parts"]:
        if p["id"] == "PC":
            p["port"] = "memory"
    return d


def part(d: dict, pid: str) -> dict:
    return next(p for p in d["parts"] if p["id"] == pid)


def session(d: dict) -> Session:
    return Session.from_board(parse_board(copy.deepcopy(d), base=BOARD.parent, path=BOARD))


def warns(s: Session) -> list[str]:
    return [str(e.fields.get("msg")) for e in s.events(kinds={"warn"})]


def drops(s: Session) -> list[str]:
    return [e.fields.get("reason") for e in s.events(kinds={"uart"}) if e.fields.get("dir") == "drop"]


def test_board_file_parses_with_pty():
    bd = parse_board(yaml.safe_load(BOARD.read_text()), base=BOARD.parent, path=BOARD)
    assert {p.type for p in bd.parts} >= {"usb_serial_adapter", "rs232_transceiver", "rj50", "cable", "gnd"}


def test_correct_chain_passes_bytes_both_ways():
    s = session(board_data())
    pc = s.board.part("PC")
    try:
        s.run_for(5e6)
        assert pc.host.host_recv(1.0) == BANNER
        pc.host.host_send(b"echo hi\r")
        s.run_for(5e6)
        assert pc.host.host_recv(1.0) == b"echo hi\r\nhi\r\n> "
        pc.host.host_send(b"led on\r")
        s.run_for(5e6)
        assert s.board.part("LED1").state()["on"] is True
        assert not warns(s) and not drops(s)
        # levels on the nets: RS-232 lines idle at mark (negative = L), the TTL side high
        lv = {n.name: n.level for n in s.board.nets}
        assert lv["RS_TXD"] == "L" and lv["RS_RXD"] == "L" and lv["U1_t1in"] == "H" and lv["U1_r1out"] == "H"
        assert s.board.part("U1").state()["t1out"] == "-6.0V"
        assert s.board.part("PC").state()["txd"] == "-6.0V"
        paths = s.board._serial_router.describe()
        assert all(p["ok"] for p in paths) and len(paths) == 2
    finally:
        s.close()


def test_bytes_arrive_at_stop_bit_paced_at_baud():
    s = session(board_data())
    pc = s.board.part("PC")
    try:
        s.run_for(5e6)
        pc.host.host_recv(1.0)
        t0 = s.board.now_ns()
        pc.host.host_send(b"abcd")
        s.run_for(2e6)
        rx = [e.t_ns for e in s.events(kinds={"uart"}) if e.fields.get("dir") == "rx" and e.t_ns >= t0]
        assert len(rx) == 4
        char = 10e9 / 115200
        gaps = [b - a for a, b in zip(rx, rx[1:])]
        assert all(abs(g - char) < 0.05 * char for g in gaps), gaps
    finally:
        s.close()


def test_swapped_cable_reaches_no_receiver():
    d = board_data()
    part(d, "W1")["map"] = {3: 3, 4: 2, 2: 5, 5: 4, 6: 8}       # TXD and RXD crossed the wrong way
    s = session(d)
    pc = s.board.part("PC")
    try:
        s.run_for(5e6)
        pc.host.host_send(b"help\r")
        s.run_for(5e6)
        assert pc.host.host_recv(0.2) == b""
        assert pc.to_host == 0
        # the console never saw the bytes: no echo, no rx events
        assert not [e for e in s.events(kinds={"uart"}) if e.fields.get("dir") == "rx"]
        infos = [str(e.fields.get("msg")) for e in s.events(kinds={"info"})]
        assert any("reaches no receiver" in m for m in infos)
    finally:
        s.close()


def test_missing_transceiver_fails_both_ways():
    d = board_data()
    d["parts"] = [p for p in d["parts"] if p["id"] != "U1"]
    d["nets"] = [{"name": "RS_TXD", "pins": ["A.PTA2"]}, {"name": "RS_RXD", "pins": ["A.PTA1"]}]
    s = session(d)
    pc = s.board.part("PC")
    try:
        s.run_for(5e6)
        pc.host.host_send(b"help\r")
        s.run_for(5e6)
        assert pc.host.host_recv(0.2) == b""
        w = " | ".join(warns(s))
        assert "inverted polarity" in w                        # TTL TX into the PC's RS-232 receiver
        assert "RS-232 levels" in w and "A.PTA1" in w          # +/-6 V on the 3.3 V RX pin
        assert {"polarity", "over_voltage"} <= set(drops(s))
        assert pc.dropped > 0
    finally:
        s.close()


def test_missing_ground_wire():
    d = board_data()
    part(d, "W1")["map"] = {3: 2, 4: 3, 5: 4, 6: 8}               # no GND wire
    s = session(d)
    pc = s.board.part("PC")
    try:
        s.run_for(5e6)
        pc.host.host_send(b"help\r")
        s.run_for(5e6)
        assert pc.host.host_recv(0.2) == b""
        assert any("PC.gnd is not connected" in m for m in warns(s))
        assert set(drops(s)) == {"no_ground"}
    finally:
        s.close()


def test_unplugged_cable_and_replug():
    s = session(board_data())
    pc = s.board.part("PC")
    try:
        s.board.set_part("W1", plug_b=None)
        s.run_for(5e6)
        assert pc.host.host_recv(0.2) == b""
        s.board.set_part("W1", plug_b="PC")
        pc.host.host_send(b"echo x\r")
        s.run_for(5e6)
        assert pc.host.host_recv(1.0).endswith(b"echo x\r\nx\r\n> ")
    finally:
        s.close()


def test_gender_mismatch_is_an_error():
    d = board_data()
    part(d, "W1")["b"] = "db9m"                                   # male cable end onto the adapter's male DB9
    with pytest.raises(ValueError, match="cannot mate"):
        session(d)


def test_adapter_baud_mismatch_decodes_garbage_with_one_warning():
    d = board_data()
    part(d, "PC")["baud"] = 9600
    s = session(d)
    pc = s.board.part("PC")
    try:
        s.run_for(5e6)
        banner = pc.host.host_recv(0.2)                          # 115200 baud banner read by a 9600 baud PC
        assert b"console" not in banner
        pc.host.host_send(b"help\r")
        s.run_for(10e6)
        assert b"help" not in pc.host.host_recv(0.2)
        w = [m for m in warns(s) if "framing" in m]
        assert len(w) == 2                                        # one per direction
        assert "framing" not in set(drops(s))                    # decoded, not dropped
        rx = [e.fields for e in s.events(kinds={"uart"}, chip="A") if e.fields.get("dir") == "rx"]
        assert rx and all(f.get("fe") for f in rx)                # 9600 baud bits seen at 115200: breaks
    finally:
        s.close()


def test_modem_lines_propagate():
    s = session(board_data())
    pc = s.board.part("PC")
    try:
        s.run_for(5e6)
        pins = s.chip.pins
        # memory host connected, modem state unknown -> DTR asserted: RS-232 +V, transceiver output low on PTC6
        assert pins.get("PTC6").level() == "L"
        pc.host.set_host_modem(dtr=False)
        s.run_for(1e6)
        assert pins.get("PTC6").level() == "H"
        pc.host.host_send(b"dtr\r")
        s.run_for(5e6)
        assert b"DTR off" in pc.host.host_recv(1.0)
        # firmware asserts CTS (PTC7 low) once the console is up: the PC sees CTS
        assert pc.state()["cts"] is True and pc.host.info()["modem_out"]["cts"] is True
    finally:
        s.close()


def test_ttl_adapter_wired_straight_to_the_pins():
    d = {"name": "ftdi", "chips": [{"id": "A", "chip": "mk22fn1m0vll12", "firmware": str(FW), "clock_hz": 120e6}],
         "parts": [{"id": "GND", "type": "gnd", "ports": {"out": "GND"}},
                   {"id": "PC", "type": "usb_serial_adapter", "levels": "ttl", "port": "memory",
                    "ports": {"rxd": "A.PTA2", "txd": "A.PTA1", "gnd": "GND"}}]}
    s = Session.from_board(parse_board(d))
    pc = s.board.part("PC")
    try:
        s.run_for(5e6)
        assert pc.host.host_recv(1.0) == BANNER
        pc.host.host_send(b"echo ttl\r")
        s.run_for(5e6)
        assert pc.host.host_recv(1.0).endswith(b"ttl\r\n> ")
        # the same adapter in RS-232 mode on the TTL pins: over-voltage / polarity, nothing gets through
        d["parts"][1]["levels"] = "rs232"
        s2 = Session.from_board(parse_board(d))
        try:
            s2.run_for(5e6)
            assert s2.board.part("PC").host.host_recv(0.2) == b""
        finally:
            s2.close()
    finally:
        s.close()


def test_cable_null_modem_preset_and_conductor_levels():
    from chipem.board import Board
    s = Session("mk22fn1m0vll12", FW, clock_hz=120e6)
    b = s.ensure_board()
    try:
        b.add_part("J1", "db9", {"gender": "male"})
        b.add_part("J2", "db9", {"gender": "male"})
        b.add_part("W", "cable", {"a": "db9f", "b": "db9f", "preset": "null_modem", "plug_a": "J1", "plug_b": "J2"})
        b.add_part("S", "switch", {"level": "H"}, {"out": "J1_4"})   # DTR on J1 pin 4
        assert b.net("J2_6").level == "H" and b.net("J2_1").level == "H"   # -> DSR + DCD on the far side
        b.set_part("S", level="L")
        assert b.net("J2_6").level == "L"
        b.add_part("S2", "switch", {"level": "H"}, {"out": "J2_6"})        # a second driver on the same wire
        assert b.net("J2_6").conflict or b.net("J1_4").conflict
        b.remove_part("S2")
        b.set_part("S", level=None)
        assert b.net("J2_6").level == "X"                                  # nothing drives the wire any more
        assert isinstance(b, Board)
    finally:
        s.close()


def test_board_yaml_roundtrip_keeps_the_chain():
    s = session(board_data())
    try:
        text = s.board.to_yaml(BOARD.parent)
        d2 = yaml.safe_load(text)
        w1 = next(p for p in d2["parts"] if p["id"] == "W1")
        assert w1["plug_a"] == "J1" and w1["plug_b"] == "PC" and not w1.get("ports")
        s2 = Session.from_board(parse_board(d2, base=BOARD.parent))
        try:
            pc = s2.board.part("PC")
            s2.run_for(5e6)
            assert pc.host.host_recv(1.0) == BANNER
        finally:
            s2.close()
    finally:
        s.close()


def test_host_line_settings_from_transport_are_used():
    s = session(board_data())
    pc = s.board.part("PC")
    try:
        pc.host.set_line(LineSettings(57600))
        s.run_for(5e6)
        got = pc.host.host_recv(0.2)
        assert got and b"console" not in got                    # the PC decodes at 57600: garbage
        assert any("baud 57600" in m for m in warns(s))
    finally:
        s.close()


def test_adapter_without_transport_takes_stimulus_and_transceiver_shutdown():
    d = board_data()
    part(d, "PC")["port"] = None
    u1 = part(d, "U1")
    u1["shdn"] = True
    u1["ports"]["shdn_n"] = "SHDN"
    d["parts"].append({"id": "SW", "type": "switch", "level": "H", "ports": {"out": "SHDN"}})
    d["stimulus"] = ["5ms:PC=echo z\\r", "10ms:SW=L", "11ms:PC=echo y\\r"]
    s = session(d)
    pc = s.board.part("PC")
    try:
        s.run_for(10e6)
        assert bytes(pc.rx_tail).endswith(b"echo z\r\nz\r\n> ")
        s.run_for(10e6)                                  # drivers shut down: the echo of "echo y" is lost
        assert b"echo y" not in bytes(pc.rx_tail)
        assert "stage_off" in drops(s)
        assert s.board.part("U1").state()["drivers_on"] is False
        assert any("drivers off" in m for m in warns(s))
    finally:
        s.close()


def test_breaks_and_parity_travel_through_the_chain():
    """A break from the PC crosses the RS-232 transceiver to the chip's UART (FE + 0x00 + break length); a PC set to
    8E1 talking to an 8N1 UART: the 9-bit frame's parity bit lands where the UART samples its stop bit."""
    s = session(board_data())
    pc = s.board.part("PC")
    try:
        s.run_for(5e6)
        pc.host.host_recv(1.0)
        pc.set(**{"break": 0})
        s.run_for(1e6)
        rx = [e.fields for e in s.events(kinds={"uart"}, chip="A") if e.fields.get("dir") == "rx"]
        assert rx and rx[-1]["fe"] and rx[-1]["byte"] == 0 and rx[-1]["brk"] >= 13
        pc.host.host_break()                                       # the same from the host program
        s.run_for(1e6)
        rx = [e.fields for e in s.events(kinds={"uart"}, chip="A") if e.fields.get("dir") == "rx"]
        assert sum(1 for f in rx if f.get("brk")) == 2
    finally:
        s.close()
    d = board_data()
    part(d, "PC")["format"] = "8E1"
    s = session(d)
    pc = s.board.part("PC")
    try:
        s.run_for(5e6)
        pc.host.host_send(b"A")                                    # 0x41: even parity bit 0 -> stop sample 0: FE
        s.run_for(1e6)
        rx = [e.fields for e in s.events(kinds={"uart"}, chip="A") if e.fields.get("dir") == "rx"]
        assert rx[-1]["byte"] == 0x41 and rx[-1]["fe"]
    finally:
        s.close()
