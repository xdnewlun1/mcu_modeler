"""Board wiring: automatic UART links from nets, runtime nets/links, YAML export, three-chip lockstep."""
import time
from pathlib import Path

import pytest
import yaml

from chipem.board import Board, load_board, parse_board, uart_signal
from chipem.chip import Chip
from chipem.cli import load_desc
from chipem.core import StopReason
from chipem.session import Session

ROOT = Path(__file__).resolve().parent.parent
THREE = ROOT / "boards/three_chip_demo.yaml"
FW = [ROOT / "firmware/samples/build/uart_link_sender.elf", ROOT / "firmware/samples_stm32/build/uart_link_echo.elf",
      ROOT / "firmware/samples_c2000/build/uart_link_echo.c28img"]
needs_fw = pytest.mark.skipif(not all(p.exists() for p in FW), reason="sample firmware not built")


def _bare(n=2):
    desc = load_desc("mk22fn1m0vll12")
    return Board({cid: Chip(desc) for cid in "ABC"[:n]})


def _infos(b, needle="auto UART link"):
    return [e.msg for e in b.log.filter("info") if needle in e.msg]


def _send(b, cid, uart, byte):
    blk = b.chips[cid].peripherals[uart]
    for cb in list(blk.tx_listeners):
        cb(blk, byte)


# ------------------------------------------------------------------------------------------------ auto links
def test_uart_signal_names():
    assert uart_signal("UART0_TX") == ("UART0", "tx")
    assert uart_signal("USART1_RX") == ("USART1", "rx")
    assert uart_signal("LPUART0_TX") == ("LPUART0", "tx")
    assert uart_signal("SCITXDA") == ("SCIA", "tx") and uart_signal("SCIRXDB") == ("SCIB", "rx")
    assert uart_signal("PTA2/UART0_TX") == ("UART0", "tx")
    for o in ("UART0_RTS_b", "UART0_CTS_b", "CAN0_TX", "GPIO", "I2S0_TXD0", "SPI0_SOUT", "disabled", None, ""):
        assert uart_signal(o) is None, o


def test_auto_link_follows_pin_owners():
    b = _bare()
    A, B = b.chips["A"], b.chips["B"]
    net = b.add_net("TX", [("A", "PTA2"), ("B", "PTA1")])
    assert not b.links                                    # reset owners are not UART functions
    A.pins.mcu_update("PTA2", owner="UART0_TX")
    assert not b.links                                    # TX alone: nothing to link to
    w = b.wiring
    B.pins.mcu_update("PTA1", owner="UART0_RX")           # firmware muxes the RX pin at runtime
    assert len(b.links) == 1 and b.wiring > w
    ln = b.links[0]
    assert ln.auto and ln.net == "TX" and ln.a == ("A", "UART0") and ln.b == ("B", "UART0") and not ln.bidir
    assert _infos(b) == ["net TX: auto UART link A.UART0 → B.UART0"]
    _send(b, "A", "UART0", 0x41)
    assert ln.bytes_ab == 1
    _send(b, "B", "UART0", 0x42)                          # one-way: B's TX is not on this net
    assert ln.bytes_ba == 0
    # an explicit link covering that direction replaces the auto link (no double delivery) ...
    ex = b.add_link(("A", "UART0"), ("B", "UART0"), bidir=False)
    assert b.links == [ex]
    _send(b, "A", "UART0", 0x43)
    assert ex.bytes_ab == 1 and ln.bytes_ab == 1
    with pytest.raises(ValueError, match="already"):
        b.add_link(("B", "UART0"), ("A", "UART0"))
    # ... and removing it brings the auto link back
    b.remove_link((("A", "UART0"), ("B", "UART0")))
    assert len(b.links) == 1 and b.links[0].auto
    with pytest.raises(ValueError, match="automatic"):
        b.remove_link(0)
    # re-muxing the RX pin to GPIO removes the auto link
    B.pins.mcu_update("PTA1", owner="GPIO")
    assert not b.links and _infos(b)[-1] == "net TX: auto UART link A.UART0 → B.UART0 removed"
    B.pins.mcu_update("PTA1", owner="UART0_RX")
    assert len(b.links) == 1
    b.remove_net("TX")                                    # removing the net drops its auto links
    assert not b.links and net not in b.nets


def test_auto_links_fan_out_and_reverse():
    b = _bare(3)
    for cid, pin, owner in (("A", "PTA2", "UART0_TX"), ("B", "PTA1", "UART0_RX"), ("C", "PTC3", "UART1_RX"),
                            ("B", "PTA2", "UART0_TX"), ("A", "PTA1", "UART0_RX")):
        b.chips[cid].pins.mcu_update(pin, owner=owner)
    b.add_net("FAN", [("A", "PTA2"), ("B", "PTA1"), ("C", "PTC3")])      # one TX, two RX
    assert sorted(ln.describe() for ln in b.links) == ["A.UART0 -> B.UART0", "A.UART0 -> C.UART1"]
    b.add_net("BACK", [("B", "PTA2"), ("A", "PTA1")])                   # the reverse direction: its own net
    assert "B.UART0 -> A.UART0" in [ln.describe() for ln in b.links if ln.net == "BACK"]
    _send(b, "A", "UART0", 0x31)
    assert all(ln.bytes_ab == 1 for ln in b.links if ln.net == "FAN")


# ------------------------------------------------------------------------------------------------ runtime wiring
def test_runtime_nets_connect_extend_merge_rename_remove():
    b = _bare()
    A, B = b.chips["A"], b.chips["B"]
    w0 = b.wiring
    n = b.connect([("A", "PTC5"), ("B", "PTC6")])
    assert n.name == "NET0" and b.wiring > w0 and b.net_of("B", "PTC6") is n
    A.pins.mcu_update("PTC5", owner="GPIO", direction="out", drive="H")   # behaves like a YAML net
    assert B.pins.get("PTC6").level() == "H"
    n2 = b.connect([("A", "PTD0"), ("B", "PTD0")], name="SIG", pull="down")
    assert n2.name == "SIG" and n2.level == "L"
    # touching one existing net: the other pin joins it
    assert b.connect([("B", "PTC6"), ("A", "PTC7")]) is n and len(n.members) == 3
    assert A.pins.get("PTC7").level() == "H"
    # touching two nets: merged into the first
    m = b.connect([("A", "PTC7"), ("B", "PTD0")])
    assert m is n and [x.name for x in b.nets] == ["NET0"] and len(n.members) == 5 and n.pull == "down"
    assert n.level == "H"                                 # the output still drives the merged net
    b.rename_net("NET0", "BUS")
    assert b.net("bus").name == "BUS"
    with pytest.raises(ValueError, match="already"):
        b.add_net("BUS", [("A", "PTE0")])
    with pytest.raises(ValueError, match="already in net"):
        b.add_net("X", [("A", "PTC5")])
    assert b.add_net("BUS", [("A", "PTE0")], extend=True) is n and len(n.members) == 6
    b.remove_net("BUS")
    assert not b.nets and b.net_of("B", "PTC6") is None
    assert B.pins.get("PTC6").external is None            # the pins float again
    with pytest.raises(ValueError, match="no net"):
        b.remove_net("BUS")


# ------------------------------------------------------------------------------------------------ YAML export
def test_to_yaml_round_trip(tmp_path):
    s = Session.from_board(THREE)
    b = s.board
    b.connect([("B", "PB0"), ("A", "PTC7")], name="RT", pull="up")
    b.add_link(("A", "UART2"), ("B", "USART2"))
    text = b.to_yaml(base=tmp_path)
    assert "auto UART links" not in text or text.count("#   net") == len([x for x in b.links if x.auto])
    (tmp_path / "exported.yaml").write_text(text)
    bd = load_board(tmp_path / "exported.yaml")
    orig = load_board(THREE)
    assert bd.name == orig.name and bd.quantum_ns == orig.quantum_ns
    assert [(c.id, c.chip, Path(c.firmware).resolve(), c.clock_hz) for c in bd.chips] == \
        [(c.id, c.chip, Path(c.firmware).resolve(), c.clock_hz) for c in orig.chips]
    assert [(n.name, [(c, p.upper()) for c, p in n.pins], n.pull) for n in bd.nets] == \
        [(n.name, [(c, n2.upper()) for c, n2 in [(cid, b.chips[cid].pins.get(p).id) for cid, p in n.pins]], n.pull)
         for n in orig.nets] + [("RT", [("B", "PB0"), ("A", "PTC7")], "up")]
    assert [(ln.a, ln.b, ln.bidir) for ln in bd.links] == \
        [(ln.a, ln.b, ln.bidir) for ln in orig.links] + [(("A", "UART2"), ("B", "USART2"), True)]
    # absolute paths by default; loads back into an equivalent Board
    data = yaml.safe_load(b.to_yaml())
    assert Path(data["chips"][0]["firmware"]).is_absolute()
    s2 = Session.from_board(parse_board(data))
    assert [n.name for n in s2.board.nets] == [n.name for n in b.nets]
    assert sorted(ln.describe() for ln in s2.board.links if not ln.auto) == \
        sorted(ln.describe() for ln in b.links if not ln.auto)
    s.close()
    s2.close()


def test_to_yaml_bare_board():
    b = _bare()
    b.add_net("N", [("A", "PTC5"), ("B", "PTC5")], drive="H")
    d = yaml.safe_load(b.to_yaml())
    assert d["chips"] == [{"id": "A", "chip": "MK22FN1M0VLL12"}, {"id": "B", "chip": "MK22FN1M0VLL12"}]
    assert d["nets"] == [{"name": "N", "pins": ["A.PTC5", "B.PTC5"], "drive": "H"}]
    assert parse_board(d).nets[0].drive == "H"


# ------------------------------------------------------------------------------------------------ three chips
@needs_fw
def test_three_chip_lockstep_and_byte_flow():
    s = Session.from_board(THREE)
    chips = s.chips
    assert list(chips) == ["A", "B", "C"]
    q = s.board.quantum_ns
    skews = []
    for chip in chips.values():
        orig = chip.run_for

        def wrapped(ns, _orig=orig):
            r = _orig(ns)
            ts = [c.clock.now_ns() for c in chips.values()]
            skews.append(max(ts) - min(ts))           # max - min bounds every pair
            return r
        chip.run_for = wrapped
    t0 = time.perf_counter()
    assert s.run_for(12e6) is StopReason.COUNT
    wall = time.perf_counter() - t0
    assert len(skews) > 1000 and max(skews) <= q + 20
    assert all(abs(c.clock.now_ns() - 12e6) < 20 for c in chips.values())
    a_out = s.uart_output("A.UART0")
    assert a_out.startswith(b"ping 1\r\nping 2\r\n")
    assert s.uart_output("B.USART1").startswith(b"PING 1\r\nPING 2\r\n")     # via the auto link of net A_TX
    assert b"PING 4\r\n" in s.uart_output("C.SCIA")                          # C muxes SCI-A after its PLL lock
    rx_a = bytes(e.byte for e in s.events(kinds={"uart"}, chip="A") if e.dir == "rx" and e.uart == "UART0")
    assert rx_a.startswith(b"PING 1\r\n")                                     # B's reply over the explicit link
    links = {ln.describe(): ln for ln in s.board.links}
    assert links["A.UART0 -> B.USART1"].auto and links["A.UART0 -> C.SCIA"].auto
    assert links["A.UART0 -> B.USART1"].bytes_ab == len(a_out)
    assert links["C.SCIA -> A.UART1"].bytes_ab == len(s.uart_output("C.SCIA")) > 0
    msgs = [e.msg for e in s.events(kinds={"info"}, chip="board")]
    assert "net A_TX: auto UART link A.UART0 → B.USART1" in msgs and "net A_TX: auto UART link A.UART0 → C.SCIA" in msgs
    led = [e for e in s.events(kinds={"pin"}, chip="B") if e.pin == "PA0" and e.field == "level"]
    assert len(led) >= 3                                                       # A's LED reaches B.PA0 over LED_A
    st = s.state()
    assert set(st["chips"]) == {"A", "B", "C"} and len(st["board"]["links"]) == 4
    assert wall < 30
    s.close()


def test_removing_a_part_keeps_a_net_the_user_wired_onto_its_pin():
    b = _bare()
    b.add_part("LED9", "led", {}, {"in": "A.PTB2"})          # anonymous net LED9_in holding A.PTB2
    net = b.connect([("A", "PTB2"), ("B", "PTB2")])         # the user extends it to B
    assert net.name == "LED9_in"
    b.remove_part("LED9")
    assert b.net_of("A", "PTB2") is net and b.net_of("B", "PTB2") is net


def test_merging_nets_keeps_drive_and_stimulus_without_glitches():
    b = _bare()
    b.add_net("X", [("A", "PTB0")], drive="H")
    b.add_net("Y", [("B", "PTB0")])
    edges = []
    b.chips["A"].pins.subscribe(lambda ev: ev.pin == "PTB0" and ev.field == "level" and edges.append(ev.new))
    net = b.connect([("B", "PTB0"), ("A", "PTB0")])          # Y first: X is merged into Y
    assert net.name == "Y" and net.drive == "H" and net.level == "H"
    assert edges == []                                       # A.PTB0 stayed H throughout (no float to X)
    b.add_net("S", [("A", "PTB1")])
    b.set_pin("A", "PTB1", "L")                              # stimulus on S
    b.add_net("T", [("B", "PTB1")], pull="up")
    net = b.connect([("B", "PTB1"), ("A", "PTB1")])
    assert net.stimulus == "L" and net.level == "L"
