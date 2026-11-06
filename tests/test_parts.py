"""Board parts (chipem/parts): every type's semantics, net integration, YAML round trip, stimulus, CLI --part,
and the logic_demo board end to end."""
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from chipem.board import Board, load_board, parse_board, parse_part_spec
from chipem.chip import Chip
from chipem.cli import load_desc
from chipem.parts import catalog, create
from chipem.session import Session

ROOT = Path(__file__).resolve().parent.parent
MIRROR = ROOT / "firmware/samples/build/button_mirror.elf"
PORTIRQ = ROOT / "firmware/samples/build/port_irq.elf"
LOGIC = ROOT / "boards/logic_demo.yaml"
needs_fw = pytest.mark.skipif(not (MIRROR.exists() and PORTIRQ.exists()), reason="sample firmware not built")

_DESC = None


def _bare(n=1):
    global _DESC
    _DESC = _DESC or load_desc("mk22fn1m0vll12")
    return Board({cid: Chip(_DESC) for cid in "ABC"[:n]})


def _session(**kw):
    return Session("mk22fn1m0vll12", MIRROR, clock_hz=120e6, **kw)


def _parts_events(b, pid):
    return [e for e in b.log.filter("part") if e.part == pid]


# ------------------------------------------------------------------------------------------------ catalog
def test_catalog_lists_every_standard_type():
    cat = {c["type"]: c for c in catalog()}
    for t in ("switch", "button", "const", "vcc", "gnd", "pulse", "pattern", "led", "probe", "counter", "not", "buf",
              "and", "nand", "or", "nor", "xor", "xnor", "mux2", "dff", "latch"):
        assert t in cat, t
    assert cat["and"]["ports"] == {"in0": "in", "in1": "in", "out": "out"} and cat["and"]["params"] == {"inputs": 2}
    assert cat["led"]["params"] == {"active": "H", "color": "red"} and cat["led"]["ports"] == {"in": "in"}
    assert cat["dff"]["ports"] == {"d": "in", "clk": "in", "rst": "in", "q": "out", "qn": "out"}
    assert cat["pulse"]["params"]["period"] == "1ms"
    with pytest.raises(ValueError, match="unknown part type"):
        create("flux_capacitor", "X1")
    with pytest.raises(ValueError, match="unknown parameter"):
        create("led", "L1", {"brightness": 3})


# ------------------------------------------------------------------------------------------------ logic truth tables
def _gate_board(typ, n_in=2, **params):
    b = _bare()
    srcs = [b.add_part(f"S{i}", "switch", {"level": "X"}, {"out": f"I{i}"}) for i in range(n_in)]
    if typ in ("not", "buf"):
        g = b.add_part("G", typ, params, {"in": "I0", "out": "Y"})
    else:
        g = b.add_part("G", typ, {"inputs": n_in, **params}, {**{f"in{i}": f"I{i}" for i in range(n_in)}, "out": "Y"})
    return b, srcs, g


@pytest.mark.parametrize("typ,table", [
    ("and", {"LL": "L", "LH": "L", "HL": "L", "HH": "H", "LX": "L", "XH": "X", "XX": "X"}),
    ("nand", {"LL": "H", "LH": "H", "HH": "L", "XL": "H", "XH": "X"}),
    ("or", {"LL": "L", "LH": "H", "HL": "H", "HH": "H", "HX": "H", "XL": "X"}),
    ("nor", {"LL": "H", "LH": "L", "HH": "L", "XH": "L", "XL": "X"}),
    ("xor", {"LL": "L", "LH": "H", "HL": "H", "HH": "L", "XL": "X", "HX": "X"}),
    ("xnor", {"LL": "H", "LH": "L", "HH": "H", "XH": "X"}),
])
def test_gate_truth_tables_with_x(typ, table):
    b, srcs, g = _gate_board(typ)
    y = b.net("Y")
    for ins, want in table.items():
        for s, lvl in zip(srcs, ins):
            b.set_part(s.id, level=lvl)
        assert y.level == want, (typ, ins, y.level)
        assert g.state()["out"] == want


def test_three_input_and_and_unary_gates():
    b, srcs, g = _gate_board("and", 3)
    assert set(g.ports) == {"in0", "in1", "in2", "out"}
    for lv in ("HHH", "HHL", "HXH"):
        for s, l in zip(srcs, lv):
            b.set_part(s.id, level=l)
        assert b.net("Y").level == {"HHH": "H", "HHL": "L", "HXH": "X"}[lv]
    for typ, table in (("not", {"H": "L", "L": "H", "X": "X"}), ("buf", {"H": "H", "L": "L", "X": "X"})):
        b, srcs, g = _gate_board(typ, 1)
        for i, o in table.items():
            b.set_part("S0", level=i)
            assert b.net("Y").level == o


def test_mux2_and_unconnected_inputs_read_x():
    b = _bare()
    b.add_part("SA", "switch", {"level": "L"}, {"out": "NA"})
    b.add_part("SB", "switch", {"level": "H"}, {"out": "NB"})
    b.add_part("S", "switch", {"level": "L"}, {"out": "NS"})
    m = b.add_part("M", "mux2", {}, {"a": "NA", "b": "NB", "sel": "NS", "out": "Y"})
    assert b.net("Y").level == "L"
    b.set_part("S", level="H")
    assert b.net("Y").level == "H"
    b.set_part("S", level="X")
    assert b.net("Y").level == "X"
    b.set_part("SA", level="H")
    assert b.net("Y").level == "H"                      # X select, both inputs agree
    b.disconnect_port("M", "a")
    assert m.inputs["a"] == "X" and b.net("Y").level == "X"
    g = b.add_part("G", "or", {}, {"out": "Z"})          # nothing on the inputs
    assert g.state()["out"] == "X" and b.net("Z").level == "X"


def test_dff_edges_reset_and_latch():
    b = _bare()
    b.add_part("D", "switch", {"level": "H"}, {"out": "ND"})
    b.add_part("C", "switch", {"level": "L"}, {"out": "NC"})
    b.add_part("R", "switch", {"level": "L"}, {"out": "NR"})
    ff = b.add_part("FF", "dff", {}, {"d": "ND", "clk": "NC", "rst": "NR", "q": "Q", "qn": "QN"})
    assert b.net("Q").level == "L" and b.net("QN").level == "H"             # init L
    b.set_part("D", level="H")
    b.set_part("C", level="H")                                               # rising edge
    assert b.net("Q").level == "H" and b.net("QN").level == "L" and ff.state()["clocks"] == 1
    b.set_part("D", level="L")
    assert b.net("Q").level == "H"                                           # no edge: holds
    b.set_part("C", level="L")
    assert b.net("Q").level == "H"                                           # falling edge ignored
    b.set_part("C", level="H")
    assert b.net("Q").level == "L"
    b.set_part("D", level="H")
    b.set_part("R", level="H")                                               # async reset
    assert b.net("Q").level == "L"
    b.set_part("C", level="L")
    b.set_part("C", level="H")
    assert b.net("Q").level == "L"                                           # held in reset
    b.set_part("R", level="L")
    b.set_part("C", level="L")
    b.set_part("C", level="H")
    assert b.net("Q").level == "H"
    # fall-edge flip-flop
    ff2 = b.add_part("FF2", "dff", {"edge": "fall"}, {"d": "ND", "clk": "NC", "q": "Q2"})
    b.set_part("C", level="L")
    assert b.net("Q2").level == "H" and ff2.state()["clocks"] == 1
    # latch: transparent while en is H
    b.add_part("E", "switch", {"level": "H"}, {"out": "NE"})
    b.add_part("LA", "latch", {}, {"d": "ND", "en": "NE", "q": "LQ"})
    assert b.net("LQ").level == "H"
    b.set_part("E", level="L")
    b.set_part("D", level="L")
    assert b.net("LQ").level == "H"
    b.set_part("E", level="H")
    assert b.net("LQ").level == "L"


# ------------------------------------------------------------------------------------------------ inputs / indicators
def test_switch_button_const_on_chip_pins():
    b = _bare()
    A = b.chips["A"]
    b.add_part("SW1", "switch", {"level": "H"}, {"out": "A.PTC6"})
    assert A.pins.get("PTC6").external == "H" and b.part_net("SW1", "out").name == "SW1_out"
    b.set_part("SW1", level="L")
    assert A.pins.get("PTC6").external == "L"
    btn = b.add_part("BTN1", "button", {"pressed": "L"}, {"out": "A.PTC7"})     # contract spelling: level
    assert btn.params["pressed_level"] == "L" and A.pins.get("PTC7").external == "H"
    b.set_part("BTN1", pressed=True)
    assert A.pins.get("PTC7").external == "L" and btn.state() == {"pressed": True, "level": "L", "presses": 1}
    b.set_part("BTN1", pressed="released")
    assert A.pins.get("PTC7").external == "H"
    b.add_part("V", "vcc", {}, {"out": "A.PTC8"})
    b.add_part("G", "gnd", {}, {"out": "A.PTC9"})
    b.add_part("K", "const", {"level": "L"}, {"out": "A.PTC10"})
    assert [A.pins.get(p).external for p in ("PTC8", "PTC9", "PTC10")] == ["H", "L", "L"]
    with pytest.raises(ValueError):
        b.set_part("V", level="L")
    # a switch fighting a fixed net driver -> X + one warn
    b.add_part("SW2", "switch", {"level": "H"}, {"out": "A.PTC9"})
    net = b.part_net("SW2", "out")
    assert net.level == "X" and net.conflict
    assert any("drive conflict" in e.msg and "SW2.out=H" in e.msg for e in b.log.filter("warn"))
    b.set_part("SW2", level="Z")                    # open switch: no longer drives
    assert net.level == "L" and not net.conflict


def test_led_active_low_and_change_count():
    b = _bare()
    b.add_part("S", "switch", {"level": "H"}, {"out": "N"})
    led = b.add_part("L1", "led", {"active": "L", "color": "green"}, {"in": "N"})
    hi = b.add_part("L2", "led", {}, {"in": "N"})
    assert led.state() == {"on": False, "changes": 0, "level": "H", "color": "green"}
    assert hi.state()["on"] is True and hi.state()["changes"] == 0           # first defined level: not a change
    b.set_part("S", level="L")
    assert led.state()["on"] is True and led.state()["changes"] == 1 and hi.state()["on"] is False
    b.set_part("S", level="X")
    assert led.state()["on"] is False                                        # X is dark
    ons = [e.on for e in _parts_events(b, "L1") if "on" in e.fields and "port" in e.fields]
    assert ons == [True, False]


def test_counter_and_probe_edges():
    b = _bare()
    b.add_part("S", "switch", {"level": "L"}, {"out": "N"})
    c = b.add_part("C", "counter", {}, {"in": "N"})
    cb = b.add_part("CB", "counter", {"edge": "both"}, {"in": "N"})
    p = b.add_part("P", "probe", {}, {"in": "N"})
    for lvl in "HLHLH":
        b.set_part("S", level=lvl)
    assert c.state()["count"] == 3 and cb.state()["count"] == 5
    st = p.state()
    assert st["rises"] == 3 and st["falls"] == 2 and st["level"] == "H"
    b.set_part("C", count=0)
    assert c.state()["count"] == 0
    b.set_part("P", clear=1)
    assert p.state()["rises"] == 0


def test_combinational_loop_warns_once_and_stops():
    b = _bare()
    b.add_part("K", "const", {"level": "L"}, {"out": "EN"})
    b.add_part("G", "nand", {}, {"in0": "EN", "in1": "LOOP", "out": "LOOP"})     # ring oscillator once enabled
    assert b.net("LOOP").level == "H"
    b.set_part("K", level="H")
    warns = [e.msg for e in b.log.filter("warn") if "combinational loop" in e.msg]
    assert len(warns) == 1 and "LOOP" in warns[0]
    b.set_part("K", level="L")                                               # breaks the loop: NAND out = H
    assert b.net("LOOP").level == "H"
    # a long chain is not a loop
    b2 = _bare()
    b2.add_part("S", "switch", {"level": "L"}, {"out": "N0"})
    for i in range(30):
        b2.add_part(f"B{i}", "not", {}, {"in": f"N{i}", "out": f"N{i + 1}"})
    b2.set_part("S", level="H")
    assert b2.net("N30").level == "H" and not [e for e in b2.log.filter("warn") if "loop" in e.msg]


def test_runtime_wiring_api():
    b = _bare(2)
    w = b.wiring
    sw = b.add_part(None, "switch", {"level": "H"}, {"out": "A.PTC6"})
    assert sw.id == "SW1" and b.wiring > w
    led = b.add_part(None, "led", {}, {})
    assert led.id == "LED1" and b.part_net("LED1", "in") is None
    n = b.connect_port("LED1", "in", "B.PTC5")
    assert n.name == "LED1_in" and led.state()["level"] == "X"
    b.connect_port("LED1", "in", "SW1_out")                  # moves: the anonymous net disappears
    assert "LED1_in" not in [x.name for x in b.nets] and led.state()["on"]
    assert b.net_of("A", "PTC6") is b.part_net("LED1", "in")
    # pins joined later merge into the part's net; parts move with it
    b.connect([("A", "PTC6"), ("B", "PTC7")])
    assert b.chips["B"].pins.get("PTC7").external == "H" and [p.id for p, _ in b.net("SW1_out").ports] == ["SW1", "LED1"]
    with pytest.raises(ValueError, match="already exists"):
        b.add_part("SW1", "switch")
    with pytest.raises(ValueError, match="no port"):
        b.connect_port("SW1", "nope", "X")
    with pytest.raises(ValueError, match="clashes"):
        b.add_part("A", "switch")
    b.remove_net("SW1_out")
    assert led.state()["level"] == "X" and b.part_net("SW1", "out") is None
    b.remove_part("SW1")
    assert "SW1" not in b.parts and _parts_events(b, "SW1")[-1].action == "remove"
    d = b.to_dict()
    assert d["parts"] == [{"id": "LED1", "type": "led", "active": "H", "color": "red", "ports": {"in": None},
                           "state": {"on": False, "changes": 1, "level": "X", "color": "red"}}]


# ------------------------------------------------------------------------------------------------ YAML
BOARD_YAML = """
name: parts_rt
chips:
  - {id: A, chip: mk22fn1m0vll12}
nets:
  - {name: BTN, pins: [A.PTC6], pull: up}
parts:
  - {id: SW1, type: switch, level: H, ports: {out: BTN}}
  - {id: LED1, type: led, active: L, ports: {in: A.PTC5}}
  - {id: G1, type: and, inputs: 3, ports: {in0: BTN, in1: A.PTB19, in2: LOGIC, out: Y}}
  - {id: CLK, type: pulse, period: 250us, duty: 0.25}
  - {id: PAT1, type: pattern, steps: [[0, L], [1ms, H]], repeat: true, period: 2ms, ports: {out: LOGIC}}
stimulus: ["5ms:SW1=L"]
"""


def test_yaml_parse_and_round_trip():
    bd = parse_board(yaml.safe_load(BOARD_YAML))
    assert [p.id for p in bd.parts] == ["SW1", "LED1", "G1", "CLK", "PAT1"]
    assert bd.parts[2].params == {"inputs": 3} and bd.parts[2].ports["in1"] == "A.PTB19"
    s = Session.from_board(bd)
    b = s.board
    assert b.part_net("LED1", "in").name == "LED1_in" and b.part_net("G1", "out").name == "Y"
    assert b.part_net("CLK", "out") is None
    text = b.to_yaml()
    d = yaml.safe_load(text)
    assert "state" not in d["parts"][0] and d["parts"][0] == {"id": "SW1", "type": "switch", "level": "H",
                                                              "ports": {"out": "BTN"}}
    assert {"name": "LED1_in", "pins": ["A.PTC5"]} in d["nets"]
    assert all(n["pins"] for n in d["nets"])                 # part-only nets (Y, LOGIC) are implied by ports
    bd2 = parse_board(d)
    s2 = Session.from_board(bd2)
    b2 = s2.board
    assert list(b2.parts) == list(b.parts)
    for pid in b.parts:
        assert b2.parts[pid].params == b.parts[pid].params, pid
        assert {k: (n.name if n else None) for k in b2.parts[pid].ports for n in [b2.part_net(pid, k)]} == \
               {k: (n.name if n else None) for k in b.parts[pid].ports for n in [b.part_net(pid, k)]}
    assert yaml.safe_load(b2.to_yaml()) == d
    s.close()
    s2.close()
    with pytest.raises(ValueError, match="unknown type"):
        parse_board({"chips": [{"id": "A"}], "parts": [{"id": "X", "type": "nope"}]})
    with pytest.raises(ValueError, match="duplicate part"):
        parse_board({"chips": [{"id": "A"}], "parts": [{"id": "X", "type": "led"}, {"id": "x", "type": "led"}]})


def test_parse_part_spec():
    ps = parse_part_spec("LED1:led:in=PTC5,active=L")
    assert (ps.id, ps.type, ps.params, ps.ports) == ("LED1", "led", {"active": "L"}, {"in": "PTC5"})
    ps = parse_part_spec("and:in0=A,in1=B.PTC6,out=Y,inputs=2")
    assert ps.id is None and ps.type == "and" and ps.ports == {"in0": "A", "in1": "B.PTC6", "out": "Y"}
    ps = parse_part_spec("G:and:inputs=3,in2=N")
    assert ps.ports == {"in2": "N"} and ps.params == {"inputs": "3"}
    ps = parse_part_spec("P:pattern:out=PTC6,steps=0:L 1ms:H 2ms:L")
    assert ps.params == {"steps": "0:L 1ms:H 2ms:L"}
    for bad in ("LED1", "X:nope:in=1", "L:led:in"):
        with pytest.raises(ValueError):
            parse_part_spec(bad)


# ------------------------------------------------------------------------------------------------ timed parts
@needs_fw
def test_pulse_timing_and_set_period():
    s = _session()
    s.add_part("CLK1", "pulse", {"period": "1ms", "duty": 0.25, "phase": "100us"}, {"out": "PTC6"})
    s.run_for(3.2e6)
    ev = [(e.t_ns, e.level) for e in _parts_events(s.board, "CLK1") if "port" in e.fields]
    # low 0.1 + 0.75 ms, then high 0.25 ms, ...
    want = [(0.85e6, "H"), (1.1e6, "L"), (1.85e6, "H"), (2.1e6, "L"), (2.85e6, "H"), (3.1e6, "L")]
    assert [l for _, l in ev] == [l for _, l in want]
    assert all(abs(t - w) < 50 for (t, _), (w, _) in zip(ev, want))
    pins = [e for e in s.events(kinds={"pin"}) if e.pin == "PTC6" and e.field == "level"]
    assert abs(pins[-1].t_ns - 3.1e6) < 50                 # the chip pin follows at the same instant
    s.set_part("CLK1", period="200us")                     # restart at now with the new period
    t0 = s.board.part_now_ns()
    s.run_for(1e6)
    assert s.board.parts["CLK1"].state()["edges"] == 8 and s.board.parts["CLK1"].period_ns == 2e5
    ev = [e.t_ns for e in _parts_events(s.board, "CLK1") if "port" in e.fields and e.t_ns > t0]
    assert abs(ev[0] - (t0 + 250e3)) < 50                  # phase 100us + low phase (0.75 * 200us)
    s.reset()                                              # reset restarts it; old events cancelled
    assert s.board.parts["CLK1"].state()["edges"] == 0
    s.run_for(0.49e6)
    assert s.board.parts["CLK1"].state()["edges"] == 3       # restarted at reset time: +250, +300, +450 us
    s.close()


@needs_fw
def test_pattern_and_probe_frequency():
    s = _session()
    s.add_part("PAT", "pattern", {"steps": "0:L 100us:H 300us:L", "repeat": True, "period": "500us"},
               {"out": "N"})
    p = s.add_part("P", "probe", {}, {"in": "N"})
    once = s.add_part("ONE", "pattern", {"steps": [["1ms", "H"], ["1.5ms", "L"]]}, {"out": "M"})
    assert s.board.net("N").level == "L" and s.board.net("M").level == "X"        # before the first step: Z
    s.run_for(2.05e6)
    st = p.state()
    assert st["rises"] == 4 and st["falls"] == 4 and abs(st["freq_hz"] - 2000) < 1
    assert abs(st["last_change_ns"] - 1.8e6) < 50
    assert once.state()["level"] == "L" and once.state()["step"] == 2
    s.set_part("PAT", steps="0:H", repeat=False, period="")
    s.run_for(5e6)
    assert p.state()["freq_hz"] is None                    # stopped for > 4 periods
    s.close()


@needs_fw
def test_single_chip_part_option_and_stimulus():
    """`chipem run fw.elf --part ...` style: a lone chip gets a board transparently."""
    s = _session()
    assert s.board is None
    s.add_part_spec("SW1:switch:out=PTC6,level=H")
    s.add_part_spec("LED1:led:in=PTC5,active=L")
    s.add_part_spec("BTN1:button:out=PTC7")
    assert s.board is not None and list(s.chips) == ["A"]
    s.apply_stimulus("SW1=L")
    s.apply_stimulus("1ms:SW1=H")
    s.apply_stimulus("2ms:BTN1=pressed")
    s.apply_stimulus("2.5ms:BTN1=released")
    s.apply_stimulus("500us:PTC8=1")                       # pins still work
    s.run_for(0.5e6)
    led = s.board.parts["LED1"]
    assert led.state()["on"] is False                      # SW1 L -> PTC5 H -> active-low LED dark
    s.run_for(1e6)
    assert led.state()["on"] is True and led.state()["changes"] == 2
    on = [e for e in _parts_events(s.board, "LED1") if e.fields.get("on") is True]
    assert abs(on[-1].t_ns - 1e6) < 1000                   # follows within a few instructions
    s.run_for(0.7e6)
    assert s.board.parts["BTN1"].state()["pressed"] is True and s.chip.pins.get("PTC7").external == "L"
    s.run_for(0.5e6)
    assert s.board.parts["BTN1"].state() == {"pressed": False, "level": "H", "presses": 1}
    assert s.chip.pins.get("PTC8").external == "H"
    st = s.state()
    assert [p["id"] for p in st["board"]["parts"]] == ["SW1", "LED1", "BTN1"]
    s.apply_stimulus("SW1.level=L")
    assert s.board.parts["SW1"].state() == {"level": "L"}
    s.close()


@needs_fw
def test_cli_run_with_parts_and_vcd(tmp_path):
    vcd = tmp_path / "w.vcd"
    r = subprocess.run([sys.executable, "-m", "chipem.cli", "run", str(MIRROR), "--clock", "120e6", "--for", "3ms",
                        "--part", "SW1:switch:out=PTC6,level=H", "--part", "LED1:led:in=PTC5,active=L",
                        "--at", "1ms:SW1=L", "--at", "2ms:SW1=H", "--vcd", str(vcd)],
                       capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "[     1.000 ms] LED1 off" in out and "[     2.000 ms] LED1 ON" in out
    assert "parts (2):" in out and "LED1     led" in out and "ON  (2 changes, active L)" in out
    assert "PTC5  L -> H" in out                           # chip events print untagged as without parts
    text = vcd.read_text()
    assert "$scope module parts $end" in text and "SW1.out" in text and " LED1 $end" in text
    led_id = next(ln.split()[3] for ln in text.splitlines() if ln.endswith(" LED1 $end"))
    assert f"0{led_id}" in text and f"1{led_id}" in text


@needs_fw
def test_shell_parts_commands():
    from chipem.cmd_shell import Shell
    import io
    s = _session()
    s.add_part_spec("SW1:switch:out=PTC6,level=H")
    out = io.StringIO()
    sh = Shell(s, stdout=out)
    sh.onecmd("part add LED1:led:in=PTC5,active=L; run 100us; parts; part SW1 level=L; run 100us; parts; "
              "set SW1 H; part LED1")
    text = out.getvalue()
    assert "added LED1 (led)" in text and "SW1: level=L" in text and "SW1: level=H" in text
    assert "LED1     led      in=LED1_in" in text and "on=True" in text and "on=False" in text
    s.close()


# ------------------------------------------------------------------------------------------------ demo board
@needs_fw
def test_logic_demo_board_end_to_end():
    s = Session.from_board(LOGIC)
    b = s.board
    q = b.quantum_ns
    assert set(b.parts) == {"SW1", "LED1", "CLK1", "INV1", "G1", "P1", "FF1", "LED2", "P2", "CNT1"}
    s.run_for(1.9e6)
    assert b.parts["LED1"].state()["on"] is False           # SW1 L -> A.PTC5 H -> active-low LED dark
    assert b.parts["P1"].state()["rises"] == 2              # GATED = LED_A & CLK
    s.run_for(0.2e6)                                         # SW1 -> H at 2 ms
    led_on = [e for e in _parts_events(b, "LED1") if e.fields.get("on") is True and "port" in e.fields]
    assert led_on and 2e6 <= led_on[-1].t_ns <= 2e6 + q     # within one quantum
    s.run_for(5.95e6)
    st = {pid: p.state() for pid, p in b.parts.items()}
    assert st["LED1"]["on"] is False and st["LED1"]["changes"] == 3
    assert st["P1"]["rises"] == 6 and abs(st["P1"]["freq_hz"] - 1000) < 1
    assert abs(st["P2"]["freq_hz"] - 500) < 1 and st["CNT1"]["count"] == 4          # B toggles per clock rise
    assert st["CLK1"]["edges"] == 16 and st["FF1"]["q"] == "H" and st["LED2"]["on"] is True
    assert not [e for e in s.events(kinds={"warn"}, chip="board")]
    d = b.to_dict()
    assert [p["id"] for p in d["parts"]][:2] == ["SW1", "LED1"] and d["parts"][1]["ports"] == {"in": "LED_A"}
    s.close()


# ------------------------------------------------------------------------------------------------ quiet parts
def test_quiet_part_logs_no_per_change_events_but_follows_state():
    b = _bare()
    b.add_part("S", "switch", {"level": "L"}, {"out": "N"})
    p = b.add_part("P", "probe", {"quiet": "true"}, {"in": "N"})
    g = b.add_part("G", "not", {"quiet": True}, {"in": "N", "out": "Y"})
    seen = []
    g.listeners.append(lambda part, f: seen.append(dict(f)))
    for lvl in ("H", "L", "H"):
        b.set_part("S", level=lvl)
    assert p.state()["rises"] == 2 and p.state()["falls"] == 1          # state follows every edge
    assert g.drive("out") == "L" and [f["level"] for f in seen] == ["L", "H", "L"]
    assert [e.fields.get("action") for e in _parts_events(b, "P")] == ["add"]   # only the add action is logged
    assert [e.fields.get("action") for e in _parts_events(b, "G")] == ["add"]
    assert any("level" in e.fields for e in _parts_events(b, "S"))      # non-quiet parts still log changes
    assert p.to_dict()["quiet"] is True and "quiet" not in b.parts["S"].to_dict()
    assert all(c["common_params"] == {"quiet": False} for c in catalog())
    with pytest.raises(ValueError, match="quiet"):
        create("led", "L9", {"quiet": "maybe"})
