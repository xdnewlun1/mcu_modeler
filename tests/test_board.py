"""Multi-chip boards (chipem/board.py + Session board mode): parsing, lockstep, nets, UART links, stimulus, CLI."""
import subprocess
import sys
from pathlib import Path

import pytest

from chipem.board import Board, load_board, parse_board, split_qualified
from chipem.chip import Chip
from chipem.cli import load_desc
from chipem.core import StopReason
from chipem.session import Session, parse_uart_in

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "firmware/samples/build"
SENDER = BUILD / "uart_link_sender.elf"
ECHO = BUILD / "uart_link_echo.elf"
BLINKY = BUILD / "blinky.elf"
MIRROR = BUILD / "button_mirror.elf"
BOARD = ROOT / "boards/two_k22_uart.yaml"
BOARD_PINS = ROOT / "boards/two_k22_uart_pins.yaml"
needs_fw = pytest.mark.skipif(not (SENDER.exists() and ECHO.exists() and BLINKY.exists()),
                              reason="sample firmware not built (firmware/samples/build.sh)")


def _two(fw_a=None, fw_b=None, **extra):
    d = {"name": "t", "chips": [{"id": "A", "chip": "mk22fn1m0vll12", "firmware": str(fw_a) if fw_a else None,
                                 "clock_hz": 120e6},
                                {"id": "B", "chip": "mk22fn1m0vll12", "firmware": str(fw_b) if fw_b else None,
                                 "clock_hz": 120e6}]}
    d.update(extra)
    return parse_board(d, base=ROOT)


def _bare_board():
    desc = load_desc("mk22fn1m0vll12")
    return Board({"A": Chip(desc), "B": Chip(desc)})


# ------------------------------------------------------------------------------------------------ parsing
def test_load_board_yaml():
    bd = load_board(BOARD)
    assert bd.name == "two_k22_uart" and bd.chip_ids() == ["A", "B"]
    assert Path(bd.chips[0].firmware) == SENDER and Path(bd.chips[1].firmware) == ECHO   # relative to board file
    assert bd.chips[0].clock_hz == 120e6
    assert bd.quantum_ns == 10e3
    assert len(bd.links) == 1 and bd.links[0].a == ("A", "UART0") and bd.links[0].b == ("B", "UART0")
    assert bd.links[0].bidir
    assert bd.nets[0].name == "BTN" and bd.nets[0].pins == [("A", "PTC6"), ("B", "PTC6")]
    assert bd.nets[0].pull == "up"
    pins = load_board(BOARD_PINS)
    assert [n.name for n in pins.nets][:2] == ["A_TX_B_RX", "B_TX_A_RX"] and not pins.links
    assert pins.stimulus == ["5ms:A.PTC6=0", "6ms:A.PTC6=Z"]


def test_parse_board_options_and_errors(tmp_path):
    bd = parse_board({"chips": [{"id": "A", "chip": "mk22fn1m0vll12", "firmware": "fw.elf", "load_addr": "0x4000"},
                                {"id": "B", "chip": "mk22fn1m0vll12"}],
                      "nets": [{"pins": ["a.PTC6", "B.PTC6"], "drive": 1}],
                      "links": [{"type": "uart", "from": "A.UART1", "to": "B.uart2", "dir": "tx"}],
                      "devices": [{"type": "spi_flash", "chip": "B", "spi": "SPI0", "cs": "PTC4", "file": "img.bin"}],
                      "stimulus": "B.PTC6=0", "quantum": "50us"}, base=tmp_path)
    assert bd.chips[0].firmware == str(tmp_path / "fw.elf") and bd.chips[0].load_addr == 0x4000
    assert bd.nets[0].pins == [("A", "PTC6"), ("B", "PTC6")] and bd.nets[0].drive == "H"
    assert bd.links[0].b == ("B", "UART2") and not bd.links[0].bidir
    assert bd.devices[0].chip == "B" and bd.devices[0].options["file"] == str(tmp_path / "img.bin")
    assert bd.stimulus == ["B.PTC6=0"] and bd.quantum_ns == 50e3
    two = [{"id": "A"}, {"id": "B"}]
    with pytest.raises(ValueError, match="qualified"):
        parse_board({"chips": two, "nets": [{"pins": ["PTC6", "B.PTC6"]}]})
    with pytest.raises(ValueError, match="already|nets"):
        parse_board({"chips": two, "nets": [{"pins": ["A.PTC6"]}, {"pins": ["a.ptc6"]}]})
    with pytest.raises(ValueError, match="unsupported"):
        parse_board({"chips": two, "links": [{"type": "spi", "from": "A.SPI0", "to": "B.SPI0"}]})
    with pytest.raises(ValueError, match="duplicate"):
        parse_board({"chips": [{"id": "A"}, {"id": "a"}]})
    with pytest.raises(ValueError, match="no chips"):
        parse_board({"chips": []})
    assert split_qualified("b.PTC6", ["A", "B"]) == ("B", "PTC6")
    assert split_qualified("PTC6", ["A", "B"], "A") == ("A", "PTC6")
    assert parse_uart_in("B.UART0:hi\\n") == ("B.UART0", b"hi\n")


# ------------------------------------------------------------------------------------------------ lockstep
@needs_fw
def test_lockstep_time_skew_bounded():
    s = Session.from_board(BOARD, quantum_ns=5e3)
    chips = s.chips
    skews = []
    for cid, chip in chips.items():
        orig = chip.run_for

        def wrapped(ns, _orig=orig):
            r = _orig(ns)
            ts = [c.clock.now_ns() for c in chips.values()]
            skews.append(max(ts) - min(ts))
            return r
        chip.run_for = wrapped
    assert s.run_for(3e6) is StopReason.COUNT
    assert len(skews) > 500
    assert max(skews) <= 5e3 + 10                      # one quantum (+ < 1 cycle rounding)
    assert all(abs(c.clock.now_ns() - 3e6) < 10 for c in chips.values())
    assert s.board.skew_ns() < 10
    # stop() from inside a run (here from a UART tx hook) ends board.run_for early
    s.chips["B"].peripherals["UART0"].tx_listeners.append(lambda u, b: s.stop())
    assert s.run_for(5e6) is StopReason.USER
    assert s.board.now_ns() < 8e6 - 1e3


# ------------------------------------------------------------------------------------------------ nets
def test_net_output_drives_input_and_conflict():
    b = _bare_board()
    A, B = b.chips["A"], b.chips["B"]
    net = b.add_net("N", [("A", "PTC5"), ("B", "PTC6")])
    assert net.level == "X" and B.pins.get("PTC6").external == "X"
    A.pins.mcu_update("PTC5", owner="GPIO", direction="out", drive="H")
    assert net.level == "H" and B.pins.get("PTC6").level() == "H"
    A.pins.mcu_update("PTC5", drive="L")
    assert B.pins.get("PTC6").level() == "L"
    # B also drives, at the other level -> conflict: X and one warning per conflict episode
    B.pins.mcu_update("PTC6", owner="GPIO", direction="out", drive="H")
    assert net.level == "X" and net.conflict
    assert A.pins.get("PTC5").level() == "L" and B.pins.get("PTC6").level() == "H"   # drivers show their own drive
    B.pins.mcu_update("PTC6", drive="L")              # agree again
    assert net.level == "L" and not net.conflict
    B.pins.mcu_update("PTC6", drive="H")              # second conflict episode
    warns = [e for e in b.log.filter("warn") if e.fields.get("net") == "N"]
    assert len(warns) == 2 and "conflict" in warns[0].msg and warns[0].chip == "board"
    B.pins.mcu_update("PTC6", direction="in")
    assert net.level == "L" and B.pins.get("PTC6").level() == "L"


def test_net_pulls_and_stimulus():
    b = _bare_board()
    A, B = b.chips["A"], b.chips["B"]
    net = b.add_net("BTN", [("A", "PTC6"), ("B", "PTC6")], pull="up")
    B.pins.mcu_update("PTC6", owner="GPIO", direction="in", pull="down")   # net pull beats pin pulls
    assert net.level == "H" and B.pins.get("PTC6").level() == "H"
    b.set_pin("B", "PTC6", "L")                       # stimulus drives the whole net
    assert A.pins.get("PTC6").level() == "L" and B.pins.get("PTC6").level() == "L"
    b.set_pin("A", "PTC6", None)                      # released -> net pull again
    assert net.level == "H"
    # without a net pull, member pin pulls decide; disagreeing pulls -> X
    n2 = b.add_net("N2", [("A", "PTD0"), ("B", "PTD0")])
    A.pins.mcu_update("PTD0", owner="GPIO", direction="in", pull="down")
    assert n2.level == "L" and B.pins.get("PTD0").level() == "L"
    B.pins.mcu_update("PTD0", owner="GPIO", direction="in", pull="up")
    assert n2.level == "X"
    # fixed driver
    n3 = b.add_net("VCC", [("A", "PTD1"), ("B", "PTD1")], drive="H")
    assert n3.level == "H" and B.pins.get("PTD1").level() == "H"
    with pytest.raises(ValueError, match="already"):
        b.add_net("dup", [("A", "PTC6")])


@needs_fw
def test_net_firmware_output_drives_other_chip():
    """blinky on A toggles A.PTC5; the net carries it to B.PTC6; button_mirror on B drives B.PTC5 = !PTC6."""
    bd = _two(BLINKY, MIRROR, nets=[{"name": "W", "pins": ["A.PTC5", "B.PTC6"]}])
    s = Session.from_board(bd)
    s.run_for(1e6)
    ev_a = [e for e in s.events(kinds={"pin"}, chip="A") if e.pin == "PTC5" and e.field == "level"]
    ev_b6 = [e for e in s.events(kinds={"pin"}, chip="B") if e.pin == "PTC6" and e.field == "level"]
    ev_b5 = [e for e in s.events(kinds={"pin"}, chip="B") if e.pin == "PTC5" and e.field == "level"]
    assert len(ev_a) > 5 and len(ev_b6) >= len(ev_a) - 2 and len(ev_b5) > 5
    # every A edge reaches B within one quantum
    for ea in ev_a[2:6]:
        eb = min(ev_b6, key=lambda e: abs(e.t_ns - ea.t_ns))
        assert eb.new == ea.new and abs(eb.t_ns - ea.t_ns) <= s.board.quantum_ns
    assert s.chips["B"].pins.get("PTC5").level() == ("L" if s.chips["A"].pins.get("PTC5").level() == "H" else "H")
    rows = s.pin_table()
    assert {r["chip"] for r in rows} == {"A", "B"}
    assert any(r["short"] == "B.PTC6" and r["net"] == "W" for r in rows)
    assert s.pin_table(chip="B")[0]["chip"] == "B"


# ------------------------------------------------------------------------------------------------ UART link
@needs_fw
def test_uart_link_end_to_end_and_baud_mismatch():
    s = Session.from_board(BOARD)
    s.run_for(6e6)
    out_a, out_b = s.uart_output("A.UART0"), s.uart_output("B.UART0")
    assert out_a.startswith(b"ping 1\r\nping 2\r\n")
    assert out_b.startswith(b"PING 1\r\nPING 2\r\n")
    rx_a = bytes(e.byte for e in s.events(kinds={"uart"}, chip="A") if e.dir == "rx")
    assert rx_a.startswith(b"PING 1\r\n")
    a_led = [e for e in s.events(kinds={"pin"}, chip="A") if e.pin == "PTC5" and e.field == "level"]
    assert len(a_led) >= 3                            # dir/initial + one toggle per reply line
    b_led = [e for e in s.events(kinds={"pin"}, chip="B") if e.pin == "PTC5" and e.field == "level"]
    assert len(b_led) >= 16                           # one toggle per echoed byte
    # a byte arrives one character time (10 bits @ 115200) after it was written
    tx = next(e for e in s.events(kinds={"uart"}, chip="A") if e.dir == "tx")
    rx = next(e for e in s.events(kinds={"uart"}, chip="B") if e.dir == "rx")
    assert 80e3 < rx.t_ns - tx.t_ns < 100e3
    st = s.state()
    assert set(st["chips"]) == {"A", "B"} and st["board"]["links"][0]["a_to_b"] == len(out_a)
    evs = s.events()
    assert all("chip" in e.fields for e in evs)
    assert [e.t_ns for e in evs] == sorted(e.t_ns for e in evs)
    assert not [e for e in s.events(kinds={"warn"}, chip="board")]
    # reconfigure B's baud generator: the next byte A sends triggers one mismatch warning
    uart_b = s.chips["B"].peripherals["UART0"]
    uart_b.set("BDL", uart_b.get("BDL") * 2)
    s.run_for(4e6)
    warns = [e for e in s.events(kinds={"warn"}, chip="board")]
    assert len(warns) == 1 and "baud mismatch" in warns[0].msg
    assert len(s.uart_output("B.UART0")) > len(out_b)          # bytes still delivered
    s.close()


@needs_fw
def test_nets_only_board_carries_uart_bytes_via_auto_links():
    s = Session.from_board(BOARD_PINS)
    s.run_for(7e6)
    assert s.uart_output("A.UART0").startswith(b"ping 1")
    assert s.uart_output("B.UART0").startswith(b"PING 1\r\n")   # TX/RX pins on one net -> auto UART link
    autos = {ln.describe(): ln.net for ln in s.board.links if ln.auto}
    assert autos == {"A.UART0 -> B.UART0": "A_TX_B_RX", "B.UART0 -> A.UART0": "B_TX_A_RX"}
    btn = [e for e in s.events(kinds={"pin"}, chip="B") if e.pin == "PTC6" and e.field == "level"]
    assert [e.new for e in btn][-2:] == ["L", "H"] and abs(btn[-2].t_ns - 5e6) < 1e3   # board stimulus via net


# ------------------------------------------------------------------------------------------------ stimulus
@needs_fw
def test_qualified_stimulus_and_inputs():
    s = Session.from_board(BOARD)
    s.apply_stimulus("B.PTC6=0")                      # net BTN: both chips see it
    assert s.chips["A"].pins.get("PTC6").level() == "L"
    s.apply_stimulus("1ms:A.PTC6=Z")                  # released at 1 ms -> net pull-up
    s.apply_stimulus("2ms:B.PTD7=1")                  # not in a net: B only
    s.apply_stimulus("PTD7=0")                        # unqualified = first chip
    s.uart_input(b"x", "B.UART0")
    s.run_for(2.5e6)
    assert s.chips["A"].pins.get("PTC6").level() == "H" and s.chips["B"].pins.get("PTC6").level() == "H"
    assert s.chips["B"].pins.get("PTD7").external == "H" and s.chips["A"].pins.get("PTD7").external == "L"
    assert s.uart_output("B.UART0").startswith(b"X")  # B echoed our byte uppercased
    with pytest.raises(KeyError):
        s.apply_stimulus("B.PTZ99=1")
    with pytest.raises(ValueError):
        s.set_view("C")
    s.set_view("b")
    assert s.chip is s.chips["B"] and s.uart_output() == s.uart_output("B.UART0")


# ------------------------------------------------------------------------------------------------ CLI
@needs_fw
def test_cli_run_board_shows_both_chips():
    r = subprocess.run([sys.executable, "-m", "chipem.cli", "run", "--board", str(BOARD), "--for", "20ms"],
                       capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "A| === summary chip A" in out and "B| === summary chip B" in out
    assert 'B|   UART0 (' in out and "PING 1\\r\\nPING 2" in out
    assert "=== board two_k22_uart" in out and "uart A.UART0 <-> B.UART0" in out
    assert "A.UART0 TX:" in out and "B.PTC5" in out               # live events carry the chip id
    r2 = subprocess.run([sys.executable, "-m", "chipem.cli", "run", "--for", "1ms"], capture_output=True,
                        text=True, cwd=ROOT, timeout=60)
    assert r2.returncode != 0 and "firmware" in r2.stderr
