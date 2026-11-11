"""Scripted board parts (chipem/parts/script.py): API semantics, timing, faults, hot reload, protocol helpers,
the boards/parts examples end to end, the new-part scaffold and the web endpoints (consent, editor, reload)."""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
import urllib.request
from pathlib import Path

import pytest

from chipem.board import load_board, parse_part_spec
from chipem.parts import catalog
from chipem.session import Session

ROOT = Path(__file__).resolve().parent.parent
MIRROR = ROOT / "firmware/samples/build/button_mirror.elf"
EX = ROOT / "boards/parts"
needs_k22 = pytest.mark.skipif(not MIRROR.exists(), reason="sample firmware not built")


def _fw(*rel):
    paths = [ROOT / r for r in rel]
    return pytest.mark.skipif(not all(p.exists() for p in paths), reason="sample firmware not built")


def _session():
    return Session("mk22fn1m0vll12", MIRROR, clock_hz=120e6)


def _code(src: str) -> str:
    return "from chipem.parts.script import *\n" + textwrap.dedent(src)


def _warns(s, pid=None):
    return [e for e in s.board.log.filter("warn") if pid is None or e.fields.get("part") == pid]


# ------------------------------------------------------------------------------------------------ basics
def test_catalog_and_registration():
    cat = {c["type"]: c for c in catalog()}
    assert "script" in cat and cat["script"]["ports"] == {}
    assert set(cat["script"]["params"]) >= {"file", "code", "class", "params"}


@needs_k22
def test_inline_part_timers_edges_reset_and_params():
    s = _session()
    p = s.add_part("U1", "script", {"code": _code('''
        class Blinker(ScriptPart):
            PORTS = {"OUT": "out", "IN": "in"}
            PARAMS = {"half": "1ms", "gain": Param(2.0, min=0, max=10)}
            def on_reset(self):
                self.state["rises"] = 0
                self.state["changes"] = 0
                self.lv = 0
                self.drive("OUT", 0)
                self.repeat(self.params["half"], self.toggle)
            def toggle(self):
                self.lv ^= 1
                self.drive("OUT", self.lv)
            def on_change(self, port, level, t_ns):
                self.state["changes"] += 1
            @on_rising("IN")
            def rise(self):
                self.state["rises"] += 1
                self.state["t"] = self.now_ns
        ''')}, {"OUT": "LOOP", "IN": "LOOP"})
    s.run_for(5.5e6)
    st = p.state()
    assert st["rises"] == 3 and abs(st["t"] - 5e6) < 1                # edges at 1 ms, 3 ms, 5 ms exactly
    assert st["params"] == {"half": "1ms", "gain": 2.0}
    s.set_part("U1", gain="3.5")
    assert p.state()["params"]["gain"] == 3.5                          # coerced to the default's type
    with pytest.raises(ValueError, match="outside"):
        s.set_part("U1", gain=11)
    with pytest.raises(ValueError, match="cannot set"):
        s.set_part("U1", nope=1)
    s.reset()                                                          # timers restart, state is fresh
    s.run_for(1.5e6)
    assert p.state()["rises"] == 1 and p.state()["params"]["gain"] == 3.5   # settings survive a reset
    s.close()


@needs_k22
def test_chip_pin_drives_and_reads():
    s = _session()                                     # button_mirror: PTC6 (input) mirrored to PTC5 (LED)
    p = s.add_part("U1", "script", {"code": _code('''
        class Echo(ScriptPart):
            PORTS = {"BTN": "out", "LED": "in"}
            def on_reset(self):
                self.drive("BTN", 1)
                self.state["led"] = None
            def on_change(self, port, level, t_ns):
                self.state["led"] = level
            @action
            def press(self):
                self.drive("BTN", 0)
        ''')}, {"BTN": "PTC6", "LED": "PTC5"})
    s.run_for(1e6)
    led0 = p.state()["led"]
    s.set_part("U1", action="press")
    s.run_for(1e6)
    assert p.obj.read("BTN") == 0 and p.state()["led"] is not None and p.state()["led"] != led0
    s.close()


@needs_k22
def test_open_drain_pullup_and_release():
    s = _session()
    p = s.add_part("U1", "script", {"code": _code('''
        class Od(ScriptPart):
            PORTS = {"L": "od pullup", "Z": "inout"}
        ''')}, {"L": "NL", "Z": "NZ"})
    net = s.board.net("NL")
    assert net.level == "H"                            # pulled up by the part's resistor
    p.obj.drive("L", 0)
    assert net.level == "L"
    p.obj.drive("L", 1)                                # open drain: 1 releases
    assert net.level == "H" and p.obj.driving("L") is None
    p.obj.drive("Z", "H")
    assert s.board.net("NZ").level == "H"
    p.obj.release("Z")
    assert s.board.net("NZ").level == "X"
    with pytest.raises(ValueError, match="level must be"):
        p.obj.drive("Z", 7)
    s.close()


@needs_k22
def test_zero_delay_combinational_and_loop_detection():
    s = _session()
    inv = _code('''
        class Inv(ScriptPart):
            PORTS = {"A": "in", "Y": "out"}
            def on_change(self, port, level, t_ns):
                self.drive("Y", None if level is None else 1 - level)
        ''')
    s.add_part("SW", "switch", {"level": "L"}, {"out": "A"})
    s.add_part("I1", "script", {"code": inv}, {"A": "A", "Y": "B"})
    s.add_part("I2", "script", {"code": inv}, {"A": "B", "Y": "C"})
    assert s.board.net("B").level == "H" and s.board.net("C").level == "L"
    s.set_part("SW", level="H")                        # same event, zero delay through both scripts
    assert s.board.net("B").level == "L" and s.board.net("C").level == "H"
    ring = inv.replace("None if level is None else 1 - level", "0 if level is None else 1 - level")
    s.add_part("RING", "script", {"code": ring}, {"A": "R", "Y": "R"})  # an inverter on its own output
    assert any("combinational loop" in str(e.fields.get("msg")) for e in s.board.log.filter("warn"))
    s.close()


# ------------------------------------------------------------------------------------------------ errors
@needs_k22
def test_exception_faults_part_with_line_and_stops_calls():
    s = _session()
    p = s.add_part("U1", "script", {"code": _code('''
        class Bad(ScriptPart):
            PORTS = {"OUT": "out"}
            def on_reset(self):
                self.state["ticks"] = 0
                self.repeat("100us", self.tick)
            def tick(self):
                self.state["ticks"] += 1
                if self.state["ticks"] == 3:
                    raise RuntimeError("sensor exploded")
        ''')}, {"OUT": "N1"})
    s.run_for(1e6)                                     # keeps running: the fault does not stop the session
    st = p.state()
    assert st["ticks"] == 3 and "RuntimeError: sensor exploded" in st["fault"] and ":11 in tick" in st["fault"]
    w = _warns(s, "U1")
    assert len(w) == 1 and "faulted" in w[0].msg and "raise RuntimeError" in w[0].fields["traceback"]
    assert p.fault["line"] == 11
    with pytest.raises(ValueError, match="faulted"):
        s.set_part("U1", action="x")
    s.reset()                                          # reset clears the fault and calls on_reset again
    s.run_for(0.25e6)
    assert "fault" not in p.state() and p.state()["ticks"] == 2
    s.close()


@needs_k22
def test_api_misuse_inside_callback_is_a_fault_not_a_crash():
    s = _session()
    p = s.add_part("U1", "script", {"code": _code('''
        class Bad(ScriptPart):
            PORTS = {"IN": "in"}
            def on_reset(self):
                self.drive("IN", 1)
        ''')}, {"IN": "N1"})
    assert "port IN is an input" in p.state()["fault"]
    s.close()


def test_syntax_and_load_errors_name_file_and_line(tmp_path):
    f = tmp_path / "broken.py"
    f.write_text("from chipem.parts.script import ScriptPart\nclass X(ScriptPart):\n    PORTS = {'A': 'in'\n")
    from chipem.parts import create
    with pytest.raises(ValueError, match=r"syntax error in .*broken.py, line 3"):
        create("script", "U1", {"file": str(f)})
    f.write_text("from chipem.parts.script import ScriptPart\nundefined_name\n")
    with pytest.raises(ValueError, match=r"NameError.*broken.py:2"):
        create("script", "U1", {"file": str(f)})
    f.write_text("x = 1\n")
    with pytest.raises(ValueError, match="no class derived"):
        create("script", "U1", {"file": str(f)})
    f.write_text("from chipem.parts.script import *\nclass X(ScriptPart):\n    PORTS = {'A': 'sideways'}\n")
    with pytest.raises(ValueError, match="unknown kind"):
        create("script", "U1", {"file": str(f)})
    f.write_text("from chipem.parts.script import *\nclass X(ScriptPart):\n    PORTS = {'A': 'in'}\n"
                 "    @on_rising('B')\n    def r(self): pass\n")
    with pytest.raises(ValueError, match="no such port"):
        create("script", "U1", {"file": str(f)})
    with pytest.raises(ValueError, match="script not found"):
        create("script", "U1", {"file": str(tmp_path / "missing.py")})
    with pytest.raises(ValueError, match="unknown parameter"):
        create("script", "U1", {"code": _code("class X(ScriptPart):\n    PORTS = {'A': 'in'}\n"), "speed": 3})


@needs_k22
def test_slow_callback_warns_once():
    s = _session()
    p = s.add_part("U1", "script", {"slow_ms": 1, "code": _code('''
        import time
        class Slow(ScriptPart):
            PORTS = {"A": "in"}
            @every("100us")
            def tick(self):
                time.sleep(0.003)
        ''')}, {"A": "N1"})
    s.run_for(0.35e6)
    w = [e for e in _warns(s, "U1") if "wall time" in e.msg]
    assert len(w) == 1 and "tick took" in w[0].msg and p.fault is None
    s.close()


# ------------------------------------------------------------------------------------------------ hot reload
@needs_k22
def test_hot_reload_keeps_wiring_and_settings_rejects_new_ports(tmp_path):
    f = tmp_path / "dev.py"
    f.write_text(_code('''
        class Dev(ScriptPart):
            PORTS = {"OUT": "out"}
            PARAMS = {"level": 1}
            def on_reset(self):
                self.drive("OUT", self.params["level"])
                self.state["version"] = 1
        '''))
    s = _session()
    p = s.add_part("U1", "script", {"file": str(f)}, {"OUT": "N1"})
    s.set_part("U1", level=0)
    s.reset()
    assert s.board.net("N1").level == "L" and p.state()["version"] == 1
    s.run_for(1e6)
    f.write_text(f.read_text().replace('"version"] = 1', '"version"] = 2').replace(
        'self.params["level"])', '1 - self.params["level"])'))
    from chipem.cmd_shell import Shell
    import io
    out = io.StringIO()
    sh = Shell(s, stdout=out)
    sh.onecmd("part reload U1")
    assert "reloaded" in out.getvalue()
    assert p.state()["version"] == 2 and p.state()["params"]["level"] == 0      # the setting survives
    assert s.board.net("N1").level == "H"                                       # nets re-resolved at once
    assert s.board.part_net("U1", "OUT").name == "N1"
    good = f.read_text()
    f.write_text(good.replace('{"OUT": "out"}', '{"OUT": "out", "EXTRA": "in"}'))
    with pytest.raises(ValueError, match="ports changed.*reload the board"):
        p.reload()
    f.write_text(good.replace("def on_reset(self):", "def on_reset(self) oops"))
    with pytest.raises(ValueError, match="syntax error"):
        p.reload()
    assert p.state()["version"] == 2                                            # old code keeps running
    s.run_for(1e6)
    s.close()


@needs_k22
def test_save_source_validates_before_writing(tmp_path):
    f = tmp_path / "dev.py"
    good = _code('''
        class Dev(ScriptPart):
            PORTS = {"OUT": "out"}
            def on_reset(self):
                self.state["v"] = 1
        ''')
    f.write_text(good)
    s = _session()
    p = s.add_part("U1", "script", {"file": str(f)}, {"OUT": "N1"})
    with pytest.raises(ValueError, match="syntax error"):
        p.save_source("class (")
    assert f.read_text() == good
    p.save_source(good.replace("= 1", "= 7"))
    assert p.state()["v"] == 7 and "= 7" in f.read_text()
    s.close()


# ------------------------------------------------------------------------------------------------ protocol helpers
def _bitbang_spi_read(s, nbits, cs="CS", sck="SCK", so="SO"):
    """Mode 0 master on switch parts: sample SO, then a SCK rising and falling edge per bit."""
    s.set_part(cs, level="L")
    w = 0
    for _ in range(nbits):
        w = (w << 1) | (1 if s.board.net(so).level == "H" else 0)
        s.set_part(sck, level="H")
        s.set_part(sck, level="L")
    s.set_part(cs, level="H")
    return w


@needs_k22
def test_max31855_example_over_bitbanged_spi():
    s = _session()
    s.add_part("CS", "switch", {"level": "H"}, {"out": "CS"})
    s.add_part("SCK", "switch", {"level": "L"}, {"out": "SCK"})
    tc = s.add_part("TC1", "script", {"file": str(EX / "max31855.py")}, {"CS": "CS", "SCK": "SCK", "SO": "SO"})
    assert s.board.net("SO").level == "X"              # released while not selected
    assert _bitbang_spi_read(s, 32) == (100 << 18) | (384 << 4)      # 25.0 C, cold junction 24.0 C
    s.set_part("TC1", temperature=-10.25, fault="oc")
    w = _bitbang_spi_read(s, 32)
    assert w >> 18 == (-41) & 0x3FFF and w & (1 << 16) and w & 7 == 1
    assert tc.state()["reads"] == 2
    s.close()


@needs_k22
@pytest.mark.parametrize("mode", [0, 1, 2, 3])
def test_spi_slave_modes_full_duplex(mode):
    s = _session()
    cpol, cpha = mode >> 1, mode & 1
    s.add_part("CS", "switch", {"level": "H"}, {"out": "CS"})
    s.add_part("SCK", "switch", {"level": "H" if cpol else "L"}, {"out": "SCK"})
    s.add_part("MO", "switch", {"level": "L"}, {"out": "MOSI"})
    dev = s.add_part("D", "script", {"code": _code(f'''
        class Echo(ScriptPart):
            PORTS = {{"CS": "in", "SCK": "in", "MOSI": "in", "MISO": "out"}}
            def on_reset(self):
                self.spi_slave(mode={mode})
                self.got = []
            def on_spi_start(self):
                return 0xA5
            def on_spi_byte(self, v):
                self.got.append(v)
                return v ^ 0xFF
        ''')}, {"CS": "CS", "SCK": "SCK", "MOSI": "MOSI", "MISO": "MISO"})
    idle, act = ("H", "L") if cpol else ("L", "H")
    s.set_part("CS", level="L")
    rx = []
    for byte in (0x3C, 0x81):
        w = 0
        for i in range(8):
            bit = (byte >> (7 - i)) & 1
            if cpha == 0:                              # data valid before the leading edge
                s.set_part("MO", level="H" if bit else "L")
                w = (w << 1) | (s.board.net("MISO").level == "H")
                s.set_part("SCK", level=act)
                s.set_part("SCK", level=idle)
            else:                                      # slave shifts on the leading edge, sampled on the trailing
                s.set_part("SCK", level=act)
                s.set_part("MO", level="H" if bit else "L")
                w = (w << 1) | (s.board.net("MISO").level == "H")
                s.set_part("SCK", level=idle)
        rx.append(w)
    s.set_part("CS", level="H")
    assert dev.obj.got == [0x3C, 0x81] and rx == [0xA5, 0x3C ^ 0xFF]
    s.close()


@needs_k22
def test_analog_between_script_parts():
    s = _session()
    src = s.add_part("SRC", "script", {"code": _code('''
        class Dac(ScriptPart):
            PORTS = {"V": "analog_out"}
            def on_reset(self):
                self.analog("V", 1.2)
        ''')}, {"V": "AN"})
    mon = s.add_part("MON", "script", {"code": _code('''
        class Meter(ScriptPart):
            PORTS = {"V": "analog_in"}
            def on_analog(self, port, volts):
                self.state["v"] = volts
        ''')}, {"V": "AN"})
    assert mon.obj.read_analog("V") == 1.2
    src.obj.analog("V", 2.5)
    assert mon.state()["v"] == 2.5 and s.board.net("AN").level == "X"      # no digital drive
    s.close()


@needs_k22
def test_uart_helper_with_memory_host_endpoint():
    s = _session()
    p = s.add_part("DEV", "script", {"file": str(EX / "uart_device.py"), "params": {"host": "memory"}},
                   {"RX": "N_RX", "TX": "N_TX"})
    assert p in s.host_endpoints() and p.host_port.kind == "memory"
    p.host_port.host_send("ping 7\nid?\n")
    s.run_for(5e6)
    out = p.host_port.host_recv(0.5)
    assert out == b"pong 7\r\nCHIPEM-DEV 1.0\r\n"
    assert p.state()["commands"] == 2
    s.close()


@needs_k22
def test_script_without_host_endpoint_is_not_a_host_endpoint():
    s = _session()
    p = s.add_part("U1", "script", {"code": _code("class X(ScriptPart):\n    PORTS = {'A': 'in'}\n")}, {})
    assert not hasattr(p, "pump") and not hasattr(p, "host_port") and s.host_endpoints() == []
    s.close()


# ------------------------------------------------------------------------------------------------ boards / YAML / CLI
def test_board_yaml_resolves_file_and_round_trips(tmp_path):
    bd = load_board(EX / "k22_spi_world.yaml")
    ps = bd.parts[0]
    assert ps.type == "script" and ps.params["file"] == str((EX / "spi_world.py").resolve())
    if not (ROOT / "firmware/samples/build/spi_master.elf").exists():
        pytest.skip("sample firmware not built")
    s = Session.from_board(bd)
    y = s.board.to_yaml(base=EX)
    assert "type: script, file: spi_world.py," in y              # relative to the exported YAML's directory
    (tmp_path / "b.yaml").write_text(s.board.to_yaml(base=tmp_path))
    assert load_board(tmp_path / "b.yaml").parts[0].params["file"] == ps.params["file"]
    s.close()


def test_cli_part_spec_splits_ports_from_params():
    ps = parse_part_spec(f"U9:script:file={EX / 'max31855.py'},CS=PTC4,SO=PTC7,temperature=30")
    assert ps.ports == {"CS": "PTC4", "SO": "PTC7"} and ps.params["temperature"] == "30"


@_fw("firmware/samples/build/spi_master.elf")
def test_example_spi_world_board():
    s = Session.from_board(load_board(EX / "k22_spi_world.yaml"))
    s.run_for(6e6)
    assert s.uart_output("UART0").startswith(b"M 1 WORLD\r\nM 2 WORLD")
    assert s.board.parts["U1"].state()["last_rx"] == "hello"
    s.set_part("U1", reply="EARTH")
    s.run_for(2e6)
    assert b"EARTH" in s.uart_output("UART0")
    s.close()


@_fw("firmware/samples/build/i2c_reg_master.elf")
def test_example_i2c_regs_board():
    s = Session.from_board(load_board(EX / "k22_i2c_regs.yaml"))
    s.run_for(30e6)
    out = s.uart_output("UART0")
    assert b'read "Hello over I2C!"' in out and b"PASS" in out and b"0x43: NACK" in out
    s.close()


@_fw("firmware/samples_stm32/build/adc_tim_trigger.elf")
def test_example_csv_stimulus_feeds_stm32_adc():
    s = Session.from_board(load_board(EX / "stm32_adc_csv.yaml"))
    s.run_for(13e6)
    codes = [int(x.split()[1]) for x in s.uart_output("USART1").decode().splitlines() if x.startswith("adc ")
             and x.split()[1].isdigit()]
    assert codes[:12] == [0, 620, 620, 1241, 1241, 2048, 2048, 3103, 3103, 4095, 4095, 0]
    assert s.board.parts["P1"].state()["rises"] == 3
    s.close()


@_fw("firmware/samples_stm32/build/tim_pwm.elf")
def test_example_pwm_checker():
    s = Session.from_board(load_board(EX / "stm32_pwm_check.yaml"))
    s.run_for(5e6)
    st = s.board.parts["CHK1"].state()
    assert st["violations"] == 0 and st["freq_hz"] == 1000 and st["duty_pct"] == 25 and not _warns(s, "CHK1")
    s.set_part("CHK1", duty_min=40)
    s.run_for(3e6)
    assert s.board.parts["CHK1"].state()["violations"] == 1
    assert "PWM out of spec" in _warns(s, "CHK1")[0].msg
    s.close()


@_fw("firmware/samples/build/uart_link_sender.elf")
def test_example_uart_device_board():
    s = Session.from_board(load_board(EX / "k22_uart_device.yaml"))
    s.run_for(7e6)
    dev = s.board.parts["DEV1"]
    assert dev.state()["commands"] >= 2 and dev.state()["last"].startswith("ping ")
    tx = bytes(e.byte for e in s.board.log.filter("uart") if e.fields.get("uart") == "DEV1.TX")
    assert tx.startswith(b"pong 1\r\npong 2\r\n")
    assert s.board.parts["LED1"].state()["changes"] >= 2          # the firmware saw the reply lines
    s.close()


def test_new_part_scaffold(tmp_path):
    from chipem.parts import create
    for kind in ("gpio", "spi", "i2c", "uart"):
        f = tmp_path / f"my_{kind}.py"
        r = subprocess.run([sys.executable, "-m", "chipem.cli", "new-part", str(f), "--kind", kind],
                           capture_output=True, text=True, cwd=ROOT)
        assert r.returncode == 0, r.stderr
        p = create("script", "U1", {"file": str(f)})
        assert p.ports and p.user_cls.__name__ == f"My{kind.capitalize()}"
    r = subprocess.run([sys.executable, "-m", "chipem.cli", "new-part", str(tmp_path / "my_gpio")],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 1 and "not overwriting" in r.stderr


# ------------------------------------------------------------------------------------------------ web
@pytest.fixture
def web():
    pytest.importorskip("chipem.web.server")
    if not MIRROR.exists():
        pytest.skip("sample firmware not built")
    from chipem.web.server import WebApp
    s = _session()
    s.add_part("SW1", "switch", {"level": "L"}, {"out": "N0"})
    a = WebApp(s, "127.0.0.1", 0).start()
    yield a
    a.stop()


def _req(app, path, body=None, headers=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", data=data, method=method or
                                 ("POST" if body is not None else "GET"),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_web_script_part_consent_editor_reload(web, tmp_path):
    f = tmp_path / "dev.py"
    f.write_text(_code('''
        class Dev(ScriptPart):
            PORTS = {"OUT": "out"}
            PARAMS = {"v": Param(1, min=0, max=5)}
            def on_reset(self):
                self.state["gen"] = 1
            @action
            def poke(self):
                self.state["poked"] = True
        '''))
    body = {"id": "U7", "type": "script", "params": {"file": str(f)}, "ports": {"OUT": "N7"}}
    code, r = _req(web, "/api/part", body)
    assert code == 409 and r["scripts"] == [str(f)] and "privileges" in r["error"]
    code, r = _req(web, "/api/part", {**body, "allow_scripts": True}, headers={"Origin": "http://evil.example"})
    assert code == 403
    code, r = _req(web, "/api/part", {**body, "allow_scripts": True})
    assert code == 200, r
    sc = r["part"]["script"]
    assert sc["class"] == "Dev" and sc["actions"] == ["poke"] and sc["controls"][0]["max"] == 5
    code, r = _req(web, "/api/part/set", {"id": "U7", "values": {"action": "poke"}})
    assert code == 200 and r["state"]["poked"] is True
    code, r = _req(web, "/api/part/script?id=U7")
    assert code == 200 and r["file"] == str(f) and "class Dev" in r["code"]
    code, r = _req(web, "/api/part/script", {"id": "U7", "code": r["code"].replace("= 1\n", "= 2\n", 1)})
    assert code == 200 and r["state"]["gen"] == 2 and "gen\"] = 2" in f.read_text()
    code, r = _req(web, "/api/part/script", {"id": "U7", "code": "class ("})
    assert code == 400 and "syntax error" in r["error"] and "gen\"] = 2" in f.read_text()
    code, r = _req(web, "/api/part/reload", {"id": "U7"})
    assert code == 200 and r["script"]["reloads"] == 2
    code, r = _req(web, "/api/part/reload", {"id": "SW1"})
    assert code == 400 and "not a script part" in r["error"]
    code, r = _req(web, "/api/part/reload", {"id": "U7"}, headers={"Host": "attacker.example:80"})
    assert code == 403


def test_web_open_board_with_scripts_needs_consent(web):
    path = str(EX / "k22_spi_world.yaml")
    if not (ROOT / "firmware/samples/build/spi_master.elf").exists():
        pytest.skip("sample firmware not built")
    code, r = _req(web, "/api/board/open", {"path": path})
    assert code == 409 and r["scripts"] == [str((EX / "spi_world.py").resolve())]
    code, r = _req(web, "/api/board/open", {"path": path, "allow_scripts": True})
    assert code == 200, r
    deadline = time.time() + 10
    while time.time() < deadline:
        code, info = _req(web, "/api/board")
        if code == 200 and any(p["id"] == "U1" for p in info.get("parts", [])):
            break
        time.sleep(0.05)
    assert any(p["id"] == "U1" and p["type"] == "script" for p in info["parts"])
