"""F2833x / F2823x variants on the real C28x core: the F28232 assembler samples (firmware/samples_c2000) run on the
F28335 family at the part's clock (30 MHz crystal x 10 / 2 = 150 MHz on the 150-MHz parts, 20 MHz x 10 / 2 = 100 MHz
on F28333 / F28332), the instances only the larger parts have (SCI-C, eCAP5/6, McBSP-B, HRPWM on ePWM5/6) and their
PIE lines.  The FPU32 samples are in tests/test_c2000_f2833x_fpu.py."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CHIPS = ROOT / "chips"
SAMPLES = ROOT / "firmware" / "samples_c2000"


def build(name: str, tmp_path: Path, subst: tuple = ()) -> Path:
    from chipem.core.c28x.asm import assemble
    src = SAMPLES / f"{name}.asm"
    text = src.read_text()
    for a, b in subst:
        assert a in text, a
        text = text.replace(a, b)
    out = tmp_path / f"{name}.c28img"
    assemble(text, filename=str(src)).write_c28img(out)
    return out


def session(part: str, img: Path):
    from chipem.session import Session
    return Session(str(CHIPS / f"tms320{part.lower()}.yaml"), img, quiet=True)


def edges(s, pin: str) -> list[float]:
    return [e.t_ns for e in s.events(kinds={"pin"}) if e.get("pin") == pin and e.get("field") == "level"]


@pytest.mark.parametrize("part,mhz", [("F28335", 150), ("F28333", 100), ("F28234", 150)])
def test_blinky_at_part_clock(tmp_path, part, mhz):
    """blinky.asm (delay loop, 1 ms at 100 MHz): 0.667 ms at 150 MHz.  PLL lock = 131072 OSCCLK (4.4 ms at 30 MHz)."""
    s = session(part, build("blinky", tmp_path))
    s.run_for(131072 / s.chip.peripherals["SYSCTRL"].ext_hz * 1e9 + 4.5e6)
    assert s.chip.clock.hz == mhz * 1e6
    ev = edges(s, "GPIO34")
    gaps = [b - a for a, b in zip(ev[2:], ev[3:])]
    assert len(gaps) >= 3 and all(abs(g - 1e6 * 100 / mhz) < 0.01e6 for g in gaps), gaps
    assert ev[1] >= 131072 / (s.chip.peripherals["SYSCTRL"].ext_hz) * 1e9
    assert not [e for e in s.events(kinds={"warn", "fault"})]


SCIC = (("SCICCRA   .set 0x7050", "SCICCRA   .set 0x7770"), ("SCICTL1A  .set 0x7051", "SCICTL1A  .set 0x7771"),
        ("SCIHBAUDA .set 0x7052", "SCIHBAUDA .set 0x7772"), ("SCILBAUDA .set 0x7053", "SCILBAUDA .set 0x7773"),
        ("SCICTL2A  .set 0x7054", "SCICTL2A  .set 0x7774"), ("SCIRXSTA  .set 0x7055", "SCIRXSTA  .set 0x7775"),
        ("SCIRXBUFA .set 0x7057", "SCIRXBUFA .set 0x7777"), ("SCITXBUFA .set 0x7059", "SCITXBUFA .set 0x7779"),
        ("GPAMUX2H  .set 0x6F89", "GPAMUX2H  .set 0x6F99"),         # GPBMUX2 high word: GPIO56-63
        ("OR      @GPAMUX2H, #0x0500", "OR      @GPAMUX2H, #0x5000"),   # GPIO62 SCIRXDC, GPIO63 SCITXDC (mux 1)
        ("OR      @PCLKCR0, #0x0400", "OR      @PCLKCR0, #0x0020"),     # SCICENCLK
        ("BRR       .set 26", "BRR       .set 40"))                     # 37.5 MHz / (41 x 8) = 114329 baud


def test_scic_echo_on_f28335(tmp_path):
    """SCI-C (0x7770, GPIO62/63, Table 8-14) echoes like SCI-A; LSPCLK = 150 MHz / 4."""
    s = session("F28335", build("sci_echo", tmp_path, SCIC))
    s.schedule_uart(5e6, "SCIC", b"C2833x\n")
    s.run_for(7e6)
    assert s.uart_output("SCIC") == b"C2833x\n"
    sci = s.chip.peripherals["SCIC"]
    assert abs(sci.baud() - 114329) < 50 and sci.frame() == "8N1"
    assert s.chip.pins.get("GPIO63").owner == "SCITXDC" and s.chip.pins.get("GPIO62").owner == "SCIRXDC"


def test_scic_absent_on_f28332(tmp_path):
    """The same image on an F28332 (SCI-A/B only): the SCI-C frame is unmodelled storage, nothing is sent."""
    s = session("F28332", build("sci_echo", tmp_path, SCIC))
    assert s.chip.find_uart("SCIC") is None and "SCIC" not in s.chip.peripherals
    s.run_for(7e6)
    assert s.chip.clock.hz == 100e6


def test_superset_instances_and_pie_lines():
    from chipem.chip import Chip
    from chipem.chipdesc import load_chip
    c = Chip(load_chip(CHIPS / "tms320f28335.yaml"))
    c.reset()
    p = c.peripherals
    assert p["ECAP5"]._int_line == 60 and p["ECAP6"]._int_line == 61               # PIE 4.5 / 4.6
    assert (p["MCBSPB"].rint, p["MCBSPB"].xint) == (74, 75) and p["MCBSPB"].letter == "B"   # PIE 6.3 / 6.4
    assert p["SCIC"].letter == "C" and p["SCIC"].gate == "SCICENCLK"
    assert all(p[f"EPWM{i}"].hr for i in range(1, 7))
    c2 = Chip(load_chip(CHIPS / "tms320f28332.yaml"))
    assert [c2.peripherals[f"EPWM{i}"].hr for i in range(1, 7)] == [True] * 4 + [False] * 2
    assert "MCBSPB" not in c2.peripherals and "ECAP5" not in c2.peripherals


def test_f28335_stm32_uart_board(tmp_path):
    """boards/f28335_stm32_uart.yaml: the F28335 sender (150 MHz, BRR 40) and the STM32 uart_link_echo."""
    from chipem.board import load_board
    from chipem.session import Session
    stm = ROOT / "firmware" / "samples_stm32" / "build" / "uart_link_echo.elf"
    if not stm.exists():
        pytest.skip("STM32 uart_link_echo.elf not built")
    bd = load_board(ROOT / "boards" / "f28335_stm32_uart.yaml")
    bd.chips[0].firmware = build("f28335_uart_link_sender", tmp_path)       # the source, not the checked-in image
    s = Session.from_board(bd, quiet=True)
    try:
        s.run_for(11e6)
        evs = s.events(kinds={"uart"})
        echo = bytes(e.get("byte") for e in evs if e.get("chip") == "B" and e.get("dir") == "tx")
        assert echo.startswith(b"PING 1\r\nPING 2\r\n")
        a = s.ctxs["A"].chip
        assert a.clock.hz == 150e6 and abs(a.peripherals["SCIA"].baud() - 114329) < 50
        assert len([e for e in s.events(kinds={"pin"}) if e.get("chip") == "A" and e.get("pin") == "GPIO34"
                    and e.get("field") == "level"]) >= 2                    # one toggle per reply line
    finally:
        s.close()


def test_f28335_stm32_can_board():
    """boards/f28335_stm32_can.yaml: F28335 eCAN-A (75 MHz module clock, BRP 10 x 15 tq) <-> STM32 bxCAN can_echo."""
    from chipem.board import load_board
    from chipem.session import Session
    for f in (ROOT / "firmware" / "samples_c2000_c" / "build" / "f28335_can.out",
              ROOT / "firmware" / "samples_stm32" / "build" / "can_echo.elf"):
        if not f.exists():
            pytest.skip(f"{f.name} not built")
    s = Session.from_board(load_board(ROOT / "boards" / "f28335_stm32_can.yaml"), quiet=True)
    try:
        s.run_for(27e6)
        a = s.ctxs["A"].chip
        out = bytes(a.peripherals["SCIA"].output).decode()
        assert out.startswith("CAN F28335\r\nTX 100 00\r\nRX 202 [4] 00 00 4B 32\r\nTX 100 01\r\n"), out
        ec = a.peripherals["ECANA"]
        assert a.clock.hz == 150e6 and ec.bitrate() == pytest.approx(500e3) and ec.errors.tec == 0
    finally:
        s.close()
