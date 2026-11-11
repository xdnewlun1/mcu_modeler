"""Speed / fidelity settings (chipem/perf.py): presets, Session/CLI/board/shell/web plumbing, the speed meter, and the
exactness of the fast paths (C28x self-branch skip, poll skip, auto quantum, decode/MMIO caches)."""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

import pytest

from chipem.core import StopReason
from chipem.core.c28x.asm import assemble
from chipem.core.c28x.core import C28xCore
from chipem.perf import PRESETS, PerfSettings, SpeedMeter, preset
from chipem.session import Session, parse_speed

ROOT = Path(__file__).resolve().parent.parent
K22_BLINKY = ROOT / "firmware" / "samples" / "build" / "blinky.elf"
C2000_EPWM_C = ROOT / "firmware" / "samples_c2000_c" / "build" / "epwm.out"
UART_BOARD = ROOT / "boards" / "c2000_stm32_uart.yaml"
UART_BOARD_FW = [ROOT / "firmware" / "samples_c2000" / "build" / "uart_link_sender.c28img",
                 ROOT / "firmware" / "samples_stm32" / "build" / "uart_link_echo.elf"]
PWM_LED = ROOT / "boards" / "pwm_led.yaml"
I2C_BOARD = ROOT / "boards" / "k22_stm32_i2c.yaml"


def _built(*paths: Path) -> bool:
    return all(p.exists() for p in paths)


def _events(s: Session, kinds=("uart", "pin", "irq", "can", "warn", "fault")) -> list:
    # (sorted: on a board the merged log is in the order the chips ran, which the lockstep plan may change)
    return sorted((round(e.t_ns, 3), e.kind, tuple(sorted((k, str(v)) for k, v in e.fields.items())))
                  for e in s.log.filter(kinds))


# ------------------------------------------------------------------------------------------------ settings
def test_presets_and_validation():
    assert set(PRESETS) == {"accurate", "default", "fast"}
    d = PerfSettings()
    assert d == preset("default") and d.max_slice == 2000 and d.poll_skip_ns == 0 and not d.auto_quantum
    f = d.updated(preset="fast")
    assert f.preset == "fast" and f.max_slice == 20000 and f.auto_quantum and f.poll_skip_ns == 100e3
    assert "mmio" in f.drop_kinds and f.quantum_ns is None          # no preset changes the board quantum
    c = f.updated(max_slice=1234)
    assert c.preset == "custom" and c.max_slice == 1234 and c.auto_quantum
    assert f.updated(max_slice=20000).preset == "fast"              # unchanged value: still the preset
    assert c.updated(drop_kinds="i2s, mmio").drop_kinds == frozenset({"i2s", "mmio"})
    for bad in ({"max_slice": 0}, {"pin_log": "some"}, {"drop_kinds": "nosuch"}, {"drop_kinds": "fault"},
                {"quantum_ns": -1}, {"ui_interval_s": 0}, {"poll_skip_ns": -5}, {"bogus": 1}, {"preset": "turbo"}):
        with pytest.raises(ValueError):
            d.updated(**bad)
    assert f.to_dict()["drop_kinds"] == ["mmio"]


def test_parse_speed():
    assert parse_speed("1") == 1.0 and parse_speed("x0.1") == 0.1 and parse_speed("10x") == 10.0
    assert parse_speed("×2") == 2.0
    for s in ("max", "MAX", "inf", "off"):
        assert parse_speed(s) is None
    for bad in ("0", "-1", "fast-ish", "inf1"):
        with pytest.raises(ValueError):
            parse_speed(bad)


@pytest.mark.skipif(not K22_BLINKY.exists(), reason="sample firmware not built")
def test_session_apply_and_speed_meter():
    s = Session("mk22fn1m0vll12", K22_BLINKY, clock_hz=120e6, quiet=True)
    try:
        assert s.speed_info()["ratio"] is None and s.speed_info()["requested"] is None
        s.set_perf(preset="fast")
        assert s.chip.max_slice == 20000 and s.chip.poll_skip_ns == 100e3 and s.chip.log.drop == {"mmio"}
        s.set_perf(pin_log="off")
        assert "pin" in s.chip.log.drop and s.perf.preset == "custom"
        s.run_for(2e6)
        assert not s.log.filter("pin")                    # no pin events recorded at all
        s.set_perf(pin_log="all")
        assert s.chip.log.pin_burst is None and "pin" not in s.chip.log.drop
        s.set_perf(preset="default")
        assert s.chip.log.pin_burst == 1000 and s.chip.max_slice == 2000 and s.chip.poll_skip_ns == 0
        s.run_for(2e6)
        info = s.speed_info()
        assert info["ratio"] > 0 and info["chips"]["A"]["mips"] > 0 and info["chips"]["A"]["mhz"] == 120.0
        assert info["bottleneck"] is None and info["settings"]["preset"] == "default"
        s.set_realtime(True, 0.5)
        assert s.speed_info()["requested"] == 0.5
    finally:
        s.close()


def test_speed_meter_window_and_bottleneck():
    class C:
        def __init__(self, hz):
            self.insns, self.clock = 0, type("K", (), {"hz": hz, "t": 0.0, "now_ns": lambda k: k.t})()

    class B:
        def __init__(self, chips):
            self.chips, self.chip_wall = chips, {"A": 0.0, "B": 0.0}

        def now_ns(self):
            return min(c.clock.t for c in self.chips.values())

    chips = {"A": C(100e6), "B": C(16e6)}
    sess = type("S", (), {})()
    sess.chips, sess.board = chips, B(chips)
    m = SpeedMeter(window_s=1.0)
    m.begin(sess)
    for _ in range(4):
        for c in chips.values():
            c.clock.t += 1e6
        chips["A"].insns += 100_000
        chips["B"].insns += 16_000
        sess.board.chip_wall["A"] += 0.08
        sess.board.chip_wall["B"] += 0.02
        m.sample(sess, 0.1)
    d = m.info()
    assert d["ratio"] == pytest.approx(0.01) and d["bottleneck"] == "A"
    assert d["chips"]["A"]["mips"] == pytest.approx(1.0) and d["chips"]["A"]["share"] == pytest.approx(0.8)
    for c in chips.values():                              # a reset to t=0 starts the window over
        c.clock.t = 0.0
    m.sample(sess, 0.1)
    assert m.info()["ratio"] is None


# ------------------------------------------------------------------------------------------------ CLI / shell / board
@pytest.mark.skipif(not K22_BLINKY.exists(), reason="sample firmware not built")
def test_cli_options_reach_the_session():
    from chipem.cli import build_parser
    ap = build_parser()
    a = ap.parse_args(["run", str(K22_BLINKY), "--fast", "--max-slice", "7000", "--pin-log", "off",
                       "--drop-events", "i2s", "--poll-skip", "20us", "--speed", "max"])
    s = Session.from_args(a)
    try:
        p = s.perf
        assert p.max_slice == 7000 and p.pin_log == "off" and p.drop_kinds == {"i2s"} and p.poll_skip_ns == 20e3
        assert p.auto_quantum and s.pacer is None and s.chip.max_slice == 7000
    finally:
        s.close()
    s = Session.from_args(ap.parse_args(["shell", str(K22_BLINKY), "--accurate", "--speed", "0.25"]))
    try:
        assert s.perf.preset == "accurate" and s.chip.max_slice == 500 and s.pacer.speed == 0.25
    finally:
        s.close()


@pytest.mark.skipif(not _built(*UART_BOARD_FW), reason="sample firmware not built")
def test_board_yaml_keys_and_runtime_quantum(tmp_path):
    from chipem.board import load_board
    text = UART_BOARD.read_text().replace("quantum: 10us", "quantum: 10us\nauto_quantum: true\nperformance: fast\n"
                                                                    "max_slice: 9000")
    p = tmp_path / "b.yaml"
    p.write_text(text.replace("../firmware", str(ROOT / "firmware")))
    bd = load_board(p)
    assert bd.perf == {"preset": "fast", "auto_quantum": True, "max_slice": 9000}
    s = Session.from_board(bd, quiet=True)
    try:
        assert s.perf.max_slice == 9000 and s.board.auto_quantum and s.perf.poll_skip_ns == 100e3
        assert all(c.max_slice == 9000 for c in s.chips.values())
        out = s.board.to_yaml()
        assert "performance: fast" in out and "auto_quantum: true" in out and "max_slice: 9000" in out
        s.set_perf(quantum_ns=50e3)
        assert s.board.quantum_ns == 50e3 and s.speed_info()["quantum_ns"] == 50e3
        s.set_perf(quantum_ns=None)                      # back to the board file's value
        assert s.board.quantum_ns == 10e3
    finally:
        s.close()


@pytest.mark.skipif(not _built(*UART_BOARD_FW), reason="sample firmware not built")
def test_shell_speed_perf_quantum(capsys):
    from chipem.cli import main
    main(["shell", "--board", str(UART_BOARD), "--exec",
          "perf fast; quantum 20us; run 1ms; speed; speed 0.5; perf poll_skip=off max_slice=3000; quantum board"])
    out = capsys.readouterr().out
    assert "preset fast: max_slice 2000" in out and "quantum 20 us: cross-chip latency up to 20 us" in out
    assert "speed: requested max, achieved x" in out and "requested x0.5" in out
    assert "preset custom: max_slice 3000" in out and "poll_skip 0 us" in out and "quantum 10 us" in out


# ------------------------------------------------------------------------------------------------ exactness
def _c28x(src: str) -> tuple[C28xCore, dict]:
    r = assemble('        .sect "code", 0x8000\n' + src)
    c = C28xCore()
    c.map_memory("RAM", 0x0000, 0x10000)
    for a, d in r.segments:
        c.write_mem(a, d)
    c.reset(0x8000, 0x400)
    return c, r.symbols


def test_c28x_self_branch_spends_the_slice():
    src = "        MOVB ACC,#1\nloop:   SB loop,UNC\n"
    for skip in (True, False):
        c, sym = _c28x(src)
        c.spin_skip = skip
        reason, n = c.run(1000)
        assert reason is StopReason.COUNT and n == 1000 and c.pc == sym["loop"] and c.acc == 1
        assert c.spin_pc == (sym["loop"] if skip else None)
    c, sym = _c28x("        MOVB ACC,#0\nloop:   SB loop,EQ\n        MOVB ACC,#5\n")
    reason, n = c.run(50)                                 # a conditional self-branch that is taken: same
    assert n == 50 and c.pc == sym["loop"] and c.spin_pc == sym["loop"]
    c, sym = _c28x("        MOVB ACC,#1\nloop:   SB loop,EQ\n        MOVB ACC,#5\n        ESTOP0\n")
    reason, n = c.run(50)                                 # not taken: falls through
    assert reason is StopReason.BREAKPOINT and c.acc == 5 and c.spin_pc is None


def test_c28x_decode_cache_and_count_semantics():
    c, sym = _c28x("        MOVB ACC,#0\nloop:   ADDB ACC,#1\n        SB loop,UNC\n")
    assert c.run(0) == (StopReason.COUNT, 0)
    assert c.run(7) == (StopReason.COUNT, 7) and c.acc == 3      # 1 + 3 x (ADDB, SB)
    c.write_mem(sym["loop"], b"\x02\x9c")                          # self-modifying: ADDB ACC,#2 (code page write)
    c.run(2)
    assert c.acc == 5                                              # the decode cache saw the new instruction


@pytest.mark.skipif(not C2000_EPWM_C.exists(), reason="TI cl2000 samples not built")
def test_c28x_spin_skip_is_exact_on_a_sample():
    def run(skip):
        s = Session("tms320f28232", C2000_EPWM_C, quiet=True)
        try:
            s.chip.core.spin_skip = skip
            s.chip.core.flush_code_cache()
            s.run_for(12e6)
            return _events(s), s.chip.insns
        finally:
            s.close()
    a, b = run(True), run(False)
    assert a == b and len(a[0]) > 50


@pytest.mark.skipif(not _built(*UART_BOARD_FW), reason="sample firmware not built")
def test_poll_skip_and_auto_quantum_are_exact_on_the_uart_board():
    def run(**perf):
        s = Session.from_board(UART_BOARD, quiet=True)
        s.pacer = None
        try:
            if perf:
                s.set_perf(**perf)
            s.run_for(14e6)
            return _events(s), s.uart_output("B.USART1"), sum(c.poll_skipped for c in s.chips.values())
        finally:
            s.close()
    ref, out, _ = run()
    assert out.startswith(b"PING 1\r\nPING 2\r\nPING 3")
    ev, out2, skipped = run(poll_skip_ns=1e9)
    assert ev == ref and out2 == out and skipped > 500_000           # skipped iterations, same events and times
    ev, out2, _ = run(auto_quantum=True, poll_skip_ns=100e3, max_slice=50000)
    assert ev == ref and out2 == out


@pytest.mark.skipif(not PWM_LED.exists(), reason="board missing")
def test_auto_quantum_with_sleeping_chips_is_exact():
    try:
        Session.from_board(PWM_LED, quiet=True).close()
    except Exception as e:                                # noqa: BLE001 - firmware not built
        pytest.skip(str(e))

    def run(auto):
        s = Session.from_board(PWM_LED, quiet=True)
        try:
            s.set_perf(auto_quantum=auto)
            t0 = time.perf_counter()
            s.run_for(6e6)
            return _events(s), time.perf_counter() - t0
        finally:
            s.close()
    (a, _), (b, _) = run(False), run(True)
    assert a == b and len(a) > 20


def test_unicorn_poll_skip_matches_plain_execution():
    """ARM: a loop polling a register that an event sets later; skipping the iterations ends at the same
    instruction count and time as executing them."""
    from engine_util import RAM, make_chip
    from chipem.periph.base import MMIOBlock

    def build(skip):
        chip, _ = make_chip({"reset": """
            ldr r1, =0x40001000
            movs r2, #0
        poll:
            ldr r0, [r1]
            cmp r0, #0
            beq poll
            adds r2, #1
            ldr r3, =0x20000100
            str r0, [r3]
        done:
            b done
        """}, max_slice=50000)
        blk = MMIOBlock("FLAG", 0x40001000, 0x400)
        blk.attach(chip)
        chip.bus.map_mmio(blk)
        chip.sched.at_cycles(250_000, lambda: blk.mem.__setitem__(slice(0, 4), (7).to_bytes(4, "little")))
        chip.poll_skip_ns = 1e9 if skip else 0
        chip.core.set_poll_watch(skip)
        return chip
    res = []
    for skip in (False, True):
        chip = build(skip)
        chip.run_cycles(400_000)
        res.append((chip.insns, chip.clock.cycles, chip.core.read_reg("r2"), chip.bus.read(RAM + 0x100, 4),
                    chip.poll_skipped))
    assert res[0][:4] == res[1][:4] and res[0][3] == 7
    assert res[1][4] > 200_000 and res[0][4] == 0


# ------------------------------------------------------------------------------------------------ web
@pytest.fixture
def app():
    if not K22_BLINKY.exists():
        pytest.skip("sample firmware not built")
    from chipem.web.server import WebApp
    s = Session("mk22fn1m0vll12", K22_BLINKY, clock_hz=120e6)
    a = WebApp(s, "127.0.0.1", 0).start()
    yield a
    a.stop()
    s.close()


def _post(app, path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(app, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{app.port}{path}", timeout=10) as r:
        return json.loads(r.read())


def test_web_perf_and_rate_api(app):
    d = _get(app, "/api/perf")
    assert d["settings"]["preset"] == "default" and set(d["presets"]) == {"accurate", "default", "fast"}
    st, d = _post(app, "/api/perf", {"preset": "fast"})
    assert st == 200 and d["settings"]["max_slice"] == 20000 and d["settings"]["poll_skip_ns"] == 100e3
    st, d = _post(app, "/api/perf", {"max_slice": 4000, "poll_skip": "50us", "drop_kinds": "i2s", "ui_interval": "100ms",
                                     "pin_log": "all"})
    assert st == 200 and d["settings"]["preset"] == "custom" and d["settings"]["poll_skip_ns"] == 50e3
    assert d["settings"]["drop_kinds"] == ["i2s"] and d["settings"]["ui_interval_s"] == pytest.approx(0.1)
    assert _post(app, "/api/perf", {"max_slice": 0})[0] == 400
    assert _post(app, "/api/perf", {"pin_log": "sometimes"})[0] == 400
    st, d = _post(app, "/api/realtime", {"on": True, "speed": "0.5"})
    assert st == 200 and d["realtime"]["speed"] == 0.5
    st, d = _post(app, "/api/realtime", {"speed": "max"})
    assert st == 200 and d["realtime"] == {"enabled": False}
    _post(app, "/api/run", {"ms": 5})
    end = time.monotonic() + 10
    sp = None
    while time.monotonic() < end:
        sp = _get(app, "/api/state").get("speed")
        if sp and sp.get("ratio"):
            break
        time.sleep(0.05)
    assert sp["ratio"] > 0 and sp["requested"] is None and sp["chips"]["A"]["mips"] > 0


def test_web_slow_rate_stays_responsive(app):
    """At x0.001 a worker chunk is ~0.1 ms of virtual time, so the state keeps moving and pause acts at once."""
    _post(app, "/api/realtime", {"on": True, "speed": 0.001})
    _post(app, "/api/run", {})
    time.sleep(0.6)
    t1 = _get(app, "/api/state")["time_ns"]
    time.sleep(0.6)
    t2 = _get(app, "/api/state")["time_ns"]
    assert 0 < t2 - t1 < 5e6                              # moving, at a few hundred µs per 0.6 s (not 5 ms chunks)
    t0 = time.monotonic()
    _post(app, "/api/pause", {})
    assert time.monotonic() - t0 < 1.0
    st, d = _post(app, "/api/step", {"n": 3})
    assert st == 200 and time.monotonic() - t0 < 2.0
