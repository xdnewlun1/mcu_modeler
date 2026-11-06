"""End-to-end: the TMS320F28232 samples (firmware/samples_c2000/*.asm, assembled in-process with
chipem.core.c28x.asm) on the real C28x core through Session, and the C2000 <-> K22 UART board."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "firmware" / "samples_c2000"
HAVE_CORE = importlib.util.find_spec("chipem.core.c28x") is not None
pytestmark = pytest.mark.skipif(not HAVE_CORE, reason="chipem.core.c28x (C28x interpreter) not available")


def build(name: str, tmp_path: Path) -> Path:
    from chipem.core.c28x.asm import assemble
    src = SAMPLES / f"{name}.asm"
    out = tmp_path / f"{name}.c28img"
    assemble(src.read_text(), filename=str(src)).write_c28img(out)
    return out


def session(name: str, tmp_path: Path, **kw):
    from chipem.session import Session
    return Session("tms320f28232", build(name, tmp_path), quiet=True, **kw)


def edges(s, pin: str, field: str = "level") -> list[tuple[float, str]]:
    return [(e.t_ns, e.get("new")) for e in s.events(kinds={"pin"}) if e.get("pin") == pin and e.get("field") == field]


PLL_LOCK_NS = 131072 / 20e6 * 1e9          # the samples wait for the PLL (6.55 ms) before doing anything visible


def test_blinky_toggles_gpio34(tmp_path):
    s = session("blinky", tmp_path)
    s.run_for(12e6)
    ev = [t for t, v in edges(s, "GPIO34")]
    assert len(ev) >= 6
    assert s.chip.clock.hz == 100e6
    gaps = [b - a for a, b in zip(ev[2:], ev[3:])]
    assert all(abs(g - 1.0e6) < 0.01e6 for g in gaps), gaps
    assert ev[1] >= PLL_LOCK_NS
    assert s.chip.pins.get("GPIO34").direction == "out"


def test_button_mirror(tmp_path):
    s = session("button_mirror", tmp_path)
    s.apply_stimulus("1ms:GPIO12=0")
    s.apply_stimulus("2ms:GPIO12=1")
    s.run_for(0.5e6)
    assert s.chip.pins.get("GPIO34").level() == "H"          # pull-up on GPIO12 → reads 1
    s.run_for(1.0e6)
    assert s.chip.pins.get("GPIO34").level() == "L"
    s.run_for(1.0e6)
    assert s.chip.pins.get("GPIO34").level() == "H"
    lv = edges(s, "GPIO34")
    t_low = next(t for t, v in lv if v == "L" and t > 0.9e6)
    assert 1e6 <= t_low < 1e6 + 5e3                           # reacts within a few µs (10 MHz polling loop)


def test_timer_irq_every_ms(tmp_path):
    s = session("timer_irq", tmp_path)
    s.run_for(12.2e6)
    assert s.irq_counts.get(38, 0) == 5                         # TINT0 = PIE 1.7 = vector 38
    ev = [t for t, v in edges(s, "GPIO34")][1:]
    gaps = [b - a for a, b in zip(ev, ev[1:])]
    assert gaps and all(abs(g - 1e6) < 1e3 for g in gaps), gaps
    assert s.chip.sleeping or s.state()["pc"] >= 0x330000      # IDLE between interrupts
    assert s.chip.insns < 0.1 * s.chip.clock.cycles             # mostly asleep
    summary = s.irq_summary()
    assert summary == [(38, "TINT0", 5)]


def test_sci_echo(tmp_path):
    s = session("sci_echo", tmp_path)
    s.schedule_uart(7e6, "SCIA", b"hi\n")      # SCI-A listens after the ~6.6 ms PLL lock
    s.run_for(10e6)
    assert s.uart_output("SCIA") == b"hi\n"
    sci = s.chip.peripherals["SCIA"]
    assert abs(sci.baud() - 115200) / 115200 < 0.01 and sci.frame() == "8N1"
    assert s.chip.pins.get("GPIO29").owner == "SCITXDA" and s.chip.pins.get("GPIO28").owner == "SCIRXDA"


def test_sci_echo_from_args(tmp_path):
    from chipem.cli import build_parser
    from chipem.session import Session
    fw = build("sci_echo", tmp_path)
    args = build_parser().parse_args(["run", str(fw), "--chip", "tms320f28232", "--at", "7ms:SCIA=ok"])
    s = Session.from_args(args)
    s.run_for(8e6)
    assert s.uart_output("SCIA") == b"ok"


def test_c2000_k22_board(tmp_path):
    k22 = ROOT / "firmware" / "samples" / "build" / "uart_link_sender.elf"
    if not k22.exists():
        pytest.skip("K22 samples not built (firmware/samples/build.sh)")
    from chipem.board import load_board
    from chipem.session import Session
    bd = load_board(ROOT / "boards" / "c2000_k22_uart.yaml")
    for c in bd.chips:                                          # use a freshly assembled echo image
        if c.id == "B":
            c.firmware = build("uart_link_echo", tmp_path)
    s = Session.from_board(bd, quiet=True)
    s.run_for(15e6)
    out_b = s.uart_output("B.SCIA")
    out_a = s.uart_output("A.UART0")
    assert out_a.startswith(b"ping 1\r\n")
    # the F28232's SCI-A listens only after its ~6.6 ms PLL lock: the K22's earlier pings are lost on the wire,
    # exactly as on hardware, and the echo starts with the first line received after that
    drops = [e for e in s.events(kinds={"uart"}, chip="B") if e.get("dir") == "drop"]
    assert drops and all(e.t_ns < 7e6 and e.get("reason") in ("clock_gated", "reset", "pin_not_muxed")
                         for e in drops)
    echoed = out_b.lstrip(b"\r\n")
    assert echoed.startswith(b"PING ") and echoed in out_a.upper() and len(echoed) >= 4 * len(b"PING 4\r\n")
    assert not [e for e in s.events(kinds={"warn"}) if "link" in (e.get("msg") or "")]
    toggles = [e for e in s.events(kinds={"pin"}, chip="B") if e.get("pin") == "GPIO34" and e.get("field") == "level"]
    assert len(toggles) >= len(out_b) - 1


def test_coff_image_runs(tmp_path):
    """A TI COFF2 `.out` (built here from the assembled blinky) loads and runs like the .c28img."""
    import struct
    from chipem.core.c28x.asm import assemble
    from chipem.firmware import detect_format, load_firmware
    from chipem.session import Session
    r = assemble((SAMPLES / "blinky.asm").read_text())
    path = tmp_path / "blinky.out"
    path.write_bytes(make_coff(r.segments, r.entry, r.symbols))
    assert detect_format(path) == "coff"
    img = load_firmware(path)
    assert img.unit_bytes == 2 and img.entry == r.entry and img.symbols["_c_int00"] == r.entry
    s = Session("tms320f28232", path, quiet=True)
    s.run_for(9e6)
    assert len(edges(s, "GPIO34")) >= 3


def make_coff(segments, entry, symbols) -> bytes:
    """Minimal TI COFF2 writer (file header, optional header, section headers, raw data, symbols, string table)."""
    import struct
    nsec = len(segments)
    hdr_end = 22 + 28 + 48 * nsec
    raw = b""
    shdrs = b""
    for i, (addr, data) in enumerate(segments):
        name = f".sec{i}".encode().ljust(8, b"\0")
        shdrs += name + struct.pack("<iiiiiiIII", addr, addr, len(data) // 2, hdr_end + len(raw), 0, 0, 0, 0, 0x20) \
            + struct.pack("<HH", 0, 0)
        raw += data
    strtab = b""
    syms = b""
    for nm, v in symbols.items():
        b = nm.encode()
        if len(b) <= 8:
            n8 = b.ljust(8, b"\0")
        else:
            n8 = struct.pack("<II", 0, 4 + len(strtab))
            strtab += b + b"\0"
        syms += n8 + struct.pack("<ihHBB", v, 1, 0, 2, 0)
    symptr = hdr_end + len(raw)
    fh = struct.pack("<HHiiiHHH", 0x00C2, nsec, 0, symptr, len(symbols), 28, 0x0103, 0x009D)
    oh = struct.pack("<hhiiiiii", 0x0108, 0, 0, 0, 0, entry, 0, 0)
    return fh + oh + shdrs + raw + syms + struct.pack("<I", 4 + len(strtab)) + strtab


def test_c2000_stm32_board(tmp_path):
    stm = ROOT / "firmware" / "samples_stm32" / "build" / "uart_link_echo.elf"
    if not stm.exists() or not (ROOT / "chips" / "stm32f405zg.yaml").exists():
        pytest.skip("STM32 chip or samples not available")
    from chipem.board import load_board
    from chipem.session import Session
    bd = load_board(ROOT / "boards" / "c2000_stm32_uart.yaml")
    for c in bd.chips:
        if c.id == "A":
            c.firmware = build("uart_link_sender", tmp_path)
    s = Session.from_board(bd, quiet=True)
    s.run_for(30e6)
    out_a, out_b = s.uart_output("A.SCIA"), s.uart_output("B.USART1")
    assert out_a.startswith(b"ping 1\r\nping 2\r\n") and b"ping 10\r\n" in out_a     # multi-digit decimal
    assert out_b == out_a.upper()[:len(out_b)] and out_b.count(b"\n") >= 10
    replies = s.ctxs["A"].chip.bus.read(0x0101, 1)                                     # REPLIES counter in M0
    assert replies == out_b.count(b"\n")
    assert not [e for e in s.events(kinds={"warn"}) if "link" in (e.get("msg") or "")]
