"""chipem.trace: VCD writer and live event printer."""
import io

from chipem.log import EventLog
from chipem.pins import Pin, PinBoard
from chipem.time import Clock
from chipem.trace import PinEventPrinter, VcdWriter, escape_bytes


def _board():
    clock = Clock(1e9)                               # 1 cycle = 1 ns
    pins = [Pin(1, "PTA1", kind="gpio", port="A", bit=1, owner="GPIO", direction="in"),
            Pin(2, "PTA2/LLWU_P3", kind="gpio", port="A", bit=2, owner="GPIO", direction="in", pull="up"),
            Pin(3, "VDD", kind="power", fixed="H")]
    return clock, PinBoard(pins, clock)


def test_vcd_scripted_sequence(tmp_path):
    clock, board = _board()
    path = tmp_path / "t.vcd"
    vcd = VcdWriter(path, board.pins, clock, board=board, owners=True)
    clock.advance(100)
    board.set_external("PTA1", "H")
    clock.advance(50)
    board.set_external("PTA1", "L")
    board.set_external("PTA2", "L")
    clock.advance(25)
    board.mcu_update("PTA1", owner="UART0_RX")
    board.set_external("PTA1", None)                 # floating -> x
    clock.advance(25)
    vcd.close()
    lines = path.read_text().splitlines()
    assert "$timescale 1ns $end" in lines
    var = {l.split()[4]: l.split()[3] for l in lines if l.startswith("$var wire")}
    assert set(var) == {"PTA1", "PTA2"}               # power pins are not traced
    a1, a2 = var["PTA1"], var["PTA2"]
    body = lines[lines.index("$enddefinitions $end") + 1:]
    assert body[0] == "#0" and body[1] == "$dumpvars"
    assert f"x{a1}" in body[:6] and f"1{a2}" in body[:6]
    i100 = body.index("#100")
    assert body[i100 + 1] == f"1{a1}"
    i150 = body.index("#150")
    assert body[i150 + 1:i150 + 3] == [f"0{a1}", f"0{a2}"]
    i175 = body.index("#175")
    seg = body[i175 + 1:i175 + 3]
    assert seg[0].startswith("sUART0_RX ") and seg[1] == f"x{a1}"
    assert body[-1] == "#200"
    # time stamps strictly increasing
    ts = [int(l[1:]) for l in body if l.startswith("#")]
    assert ts == sorted(set(ts))


def test_printer_formats_and_coalesces():
    out = io.StringIO()
    log = EventLog()
    pr = PinEventPrinter(out).attach(log)
    log.event("pin", 0, pin="PTC5", field="owner", old="disabled", new="GPIO")
    log.event("pin", 0, pin="PTC5", field="direction", old="in", new="out")
    log.event("pin", 12.345e6, pin="PTC5", field="level", old="L", new="H")
    for i, b in enumerate(b"hello\r\n"):
        log.event("uart", 100e6 + i * 1000, uart="UART0", dir="tx", byte=b)
    log.event("uart", 101.5e6, uart="UART0", dir="tx", byte=ord("!"))
    log.event("reset", 1200e6, reason="watchdog")
    log.event("mmio", 1201e6, msg="ignored by default")
    log.event("warn", 1202e6, msg="careful")
    pr.detach()
    assert out.getvalue().splitlines() == [
        "[     0.000 ms] PTC5  owner disabled -> GPIO (out)",
        "[    12.345 ms] PTC5  L -> H",
        '[   100.000 ms] UART0 TX: "hello\\r\\n"',
        '[   101.500 ms] UART0 TX: "!"',
        "[  1200.000 ms] RESET watchdog",
        "[  1202.000 ms] WARN careful",
    ]


def test_printer_kind_filter_and_escape():
    out = io.StringIO()
    log = EventLog()
    PinEventPrinter(out, kinds={"uart"}).attach(log)
    log.event("pin", 0, pin="PTC5", field="level", old="L", new="H")
    log.event("uart", 0, uart="UART1", dir="rx", byte=0x01)
    log.event("info", 2e6, msg="flush")             # filtered out, but uart still flushed at detach
    assert out.getvalue() == ""
    assert escape_bytes(b'a"\\\x00\t') == 'a\\"\\\\\\x00\\t'


def test_vcd_owner_strings_on_by_default(tmp_path):
    clock, board = _board()
    path = tmp_path / "o.vcd"
    vcd = VcdWriter(path, board.pins, clock, board=board)
    board.mcu_update("PTA2", owner="UART0_TX", direction="out", drive="H")
    vcd.close()
    text = path.read_text()
    assert "$scope module owner $end" in text and "$var string 1" in text
    assert "sUART0_TX " in text and "sGPIO " in text
    VcdWriter(tmp_path / "n.vcd", board.pins, clock, owners=False).close()
    assert "$scope module owner" not in (tmp_path / "n.vcd").read_text()


# ------------------------------------------------------------------------------------------- VCD with parts
def parse_vcd(text: str) -> dict:
    """Minimal VCD reader (no pyvcd): checks the structure and returns {name: [(t, value), ...]} with the
    $dumpvars values at the dump time.  Raises AssertionError on a malformed file."""
    toks = text.split()
    i = 0
    ids: dict[str, str] = {}
    kinds: dict[str, str] = {}
    depth = 0
    scope: list[str] = []
    seen_end = False
    while i < len(toks):
        t = toks[i]
        if t in ("$date", "$version", "$timescale", "$comment"):
            j = toks.index("$end", i)
            i = j + 1
            continue
        if t == "$scope":
            assert toks[i + 1] == "module" and toks[i + 3] == "$end"
            scope.append(toks[i + 2])
            depth += 1
            i += 4
            continue
        if t == "$upscope":
            assert toks[i + 1] == "$end" and depth > 0
            scope.pop()
            depth -= 1
            i += 2
            continue
        if t == "$var":
            typ, size, vid, name, end = toks[i + 1:i + 6]
            assert typ in ("wire", "string") and size == "1" and end == "$end" and vid not in ids
            ids[vid] = ".".join(scope[1:] + [name]) if scope and scope[0] == "chip" and len(scope) > 1 \
                else (f"{scope[-1]}:{name}" if scope and scope[-1] == "parts" else name)
            kinds[vid] = typ
            i += 6
            continue
        if t == "$enddefinitions":
            assert toks[i + 1] == "$end" and depth == 0
            seen_end = True
            i += 2
            break
        raise AssertionError(f"unexpected header token {t!r}")
    assert seen_end
    waves: dict[str, list] = {n: [] for n in ids.values()}
    now = None
    in_dump = False
    while i < len(toks):
        t = toks[i]
        if t.startswith("#"):
            v = int(t[1:])
            assert now is None or v > now, "time stamps must increase"
            now = v
        elif t == "$dumpvars":
            in_dump = True
        elif t == "$end":
            assert in_dump
            in_dump = False
        elif t.startswith("s"):
            vid = toks[i + 1]
            assert kinds.get(vid) == "string", f"string value for undeclared/non-string id {vid!r}"
            waves[ids[vid]].append((now, t[1:]))
            i += 1
        else:
            val, vid = t[0], t[1:]
            assert val in "01xz" and vid in ids, f"bad value change {t!r}"
            assert now is not None
            waves[ids[vid]].append((now, val))
        i += 1
    return waves


def test_vcd_part_added_mid_run_and_quiet_parts(tmp_path):
    from pathlib import Path
    from chipem.session import Session
    root = Path(__file__).resolve().parent.parent
    path = tmp_path / "parts.vcd"
    s = Session("mk22fn1m0vll12", root / "firmware/samples/build/blinky.elf", clock_hz=120e6, vcd_path=path)
    s.add_part("SW1", "switch", {"level": "H"}, {"out": "N_SW"})      # present from the start
    s.run_for(0.2e6)
    t_add = s.chip.now_ns()
    s.add_part("LED1", "led", {}, {"in": "A.PTC5"})                   # added from the "web" mid-run
    s.add_part("P1", "probe", {"quiet": True}, {"in": "A.PTC5"})      # quiet: no per-edge part events
    s.run_for(0.5e6)
    s.board.set_part("SW1", level="L")
    t_rm = s.chip.now_ns()
    s.board.remove_part("LED1")
    s.run_for(0.1e6)
    assert not [e for e in s.board.log.filter("part") if e.part == "P1" and "action" not in e.fields]
    assert [e for e in s.board.log.filter("part") if e.part == "LED1" and "on" in e.fields]   # LED is not quiet
    s.close()
    w = parse_vcd(path.read_text())
    sw, led, probe, ptc5 = w["parts:SW1.out"], w["parts:LED1"], w["parts:P1.in"], w["PTC5"]
    assert sw[0][1] == "1" and sw[-1][1] == "0"                      # declared before the run: in $dumpvars
    assert led[0][1] == "x" and probe[0][1] == "x"                    # did not exist at the start of the trace
    t_led = led[1][0]
    assert abs(t_led - t_add) < 1000                                  # declared at its creation time
    # from its creation on, the LED follows PTC5 (active high) and the quiet probe records every edge
    edges = [(t, v) for t, v in ptc5 if t > t_led]
    assert len(edges) > 4
    led_vals = [v for t, v in led[2:-1]]
    assert led_vals == [v for t, v in edges if t <= t_rm]
    assert [v for t, v in probe if t > t_led] == [v for _, v in edges]
    assert led[-1] == (led[-1][0], "x") and abs(led[-1][0] - t_rm) < 1000          # removed -> x



def test_board_vcd_part_change_from_a_chip_behind_keeps_its_time(tmp_path):
    """The view chip (A) is ahead of B and has already written a change at 5 us; a probe on B's pin then sees B's
    edge at 3 us. The VCD must show the probe edge at 3 us (it used to be pushed to 5 us)."""
    from chipem.board import Board
    from chipem.chip import Chip
    from chipem.cli import load_desc
    desc = load_desc("mk22fn1m0vll12")
    b = Board({"A": Chip(desc, clock_hz=100e6), "B": Chip(desc, clock_hz=100e6)})
    A, B = b.chips["A"], b.chips["B"]
    b.add_part("P1", "probe", {}, {"in": "B.PTC5"})
    w = VcdWriter(tmp_path / "a.vcd", list(A.pins.pins), A.clock, board=A.pins)
    w.add_parts(b)
    B.pins.mcu_update("PTC5", owner="GPIO", direction="out", drive="L")
    A.clock.advance(500)                                   # A at 5 us
    A.pins.mcu_update("PTC5", owner="GPIO", direction="out", drive="H")
    B.clock.advance(300)                                   # B at 3 us
    B.pins.mcu_update("PTC5", drive="H")                   # probe sees H at 3 us
    B.clock.advance(300)
    w.close()
    txt = (tmp_path / "a.vcd").read_text()
    vid = next(l.split()[3] for l in txt.splitlines() if l.startswith("$var") and l.split()[4] == "P1.in")
    t, at = None, None
    for line in txt.split("$enddefinitions")[1].splitlines():
        if line.startswith("#"):
            t = int(line[1:])
        elif line == "1" + vid:
            at = t
    assert at == 3000

