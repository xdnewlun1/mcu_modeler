"""End-to-end on the real C28x core: the TMS320F28027 / F28069 samples (firmware/samples_c2000/f28027_*.asm assembled
in-process; firmware/samples_c2000_c/f2802[7]_*.c / f28069_*.c TI-compiled, skipped when not built), the Piccolo boot
ROM stand-in (Device_cal, DIVSEL, GetMode / wait from GPIO37/GPIO34) and the boards boards/f2802[7]_*.yaml,
boards/f28069_*.yaml.  Every sample first locks the PLL: PLLLOCKPRD = 65535 OSCCLK = 6.55 ms at INTOSC1 = 10 MHz,
before which the CPU runs at OSCCLK / 4 = 2.5 MHz (DIVSEL = 0 for the PLLCR write)."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "firmware" / "samples_c2000"
CBUILD = ROOT / "firmware" / "samples_c2000_c" / "build"
HAVE_CORE = importlib.util.find_spec("chipem.core.c28x") is not None
pytestmark = pytest.mark.skipif(not HAVE_CORE, reason="C28x core not available")
LOCK_NS = 65535 / 10e6 * 1e9


def build(name, tmp_path, text=None):
    from chipem.core.c28x.asm import assemble
    src = SAMPLES / f"{name}.asm"
    out = tmp_path / f"{name}.c28img"
    assemble(text if text is not None else src.read_text(), filename=str(src)).write_c28img(out)
    return out


def session(name, tmp_path, chip="tms320f28027", **kw):
    from chipem.session import Session
    return Session(chip, build(name, tmp_path), quiet=True, **kw)


def syms(img):
    return json.loads(Path(img).read_text())["symbols"]


def edges(s, pin, chip=None):
    return [(e.t_ns, e.get("new")) for e in s.events(kinds={"pin"}) if e.get("pin") == pin
            and e.get("field") == "level" and (chip is None or e.get("chip") == chip)]


def board(name, **fw):
    from chipem.board import load_board
    from chipem.session import Session
    bd = load_board(ROOT / "boards" / f"{name}.yaml")
    for c in bd.chips:
        if c.id in fw:
            c.firmware = fw[c.id]
    return Session.from_board(bd, quiet=True)


# ------------------------------------------------------------------ assembler samples
def test_blinky_launchpad_leds_count_ms(tmp_path):
    s = session("f28027_blinky", tmp_path)
    s.run_for(12e6)
    assert s.chip.clock.hz == 60e6
    rises = [t for t, v in edges(s, "GPIO0") if v == "H"]
    assert rises and rises[0] >= LOCK_NS
    assert all(abs((b - a) - 2e6) < 100 for a, b in zip(rises, rises[1:]))   # GPIO0 = bit 0 of a 1 ms counter
    assert len([1 for _, v in edges(s, "GPIO1") if v == "H"]) >= 1
    assert not [e for e in s.events(kinds={"warn"}) if "EALLOW" in str(e.get("msg"))]
    regs = s.chip.peripherals["SYSCTRL"]
    assert regs.get("PLLCR") == 12 and (regs.get("PLLSTS") >> 7) & 3 == 2 and regs.get("CLKCTL") == 0x6400


def test_sci_echo_on_launchpad_pins(tmp_path):
    s = session("f28027_sci_echo", tmp_path)
    s.schedule_uart(7.5e6, "SCIA", b"abc!")
    s.run_for(8.5e6)
    assert s.uart_output("SCIA") == b"ABC!"
    sci = s.chip.peripherals["SCIA"]
    assert abs(sci.baud() - 115200) / 115200 < 0.01 and sci.frame() == "8N1" and sci.fifo_depth == 4
    assert s.chip.pins.get("GPIO28").owner == "SCIRXDA" and s.chip.pins.get("GPIO29").owner == "SCITXDA"
    assert len(edges(s, "GPIO0")) >= 4


def test_epwm_triggers_type3_adc_with_interrupt(tmp_path):
    s = session("f28027_adc_epwm", tmp_path)
    s.set_adc("ADCINA1", 1.0)
    s.set_adc("ADCINB1", 2.0)
    s.run_for(LOCK_NS + 1.5e6)                                        # 1 ms ADC power-up wait, then 10 kHz SOCs
    a1, b1, n = (s.chip.bus.read(a, 1) for a in (0x8000, 0x8001, 0x8002))
    assert a1 == int(4096 / 3.3) and b1 == int(4096 * 2 / 3.3)
    assert 3 <= n <= 6
    ev = [t for t, v in edges(s, "GPIO0") if v == "H"]
    assert all(abs((b - a) - 100e3) < 20 for a, b in zip(ev, ev[1:]))           # 10 kHz EPWM1A
    assert dict((v, c) for v, _, c in s.irq_summary())[32] == n                 # PIE 1.1 ADCINT1
    assert s.chip.pins.get("GPIO0").owner == "EPWM1A"


def test_comparator_board_trips_epwm_through_dc(tmp_path):
    s = board("f28027_comp_adc", A=build("f28027_comp_trip", tmp_path))
    try:
        s.run_for(12e6)
        a = edges(s, "GPIO0", "A")
        trip = [t for t, v in edges(s, "GPIO1", "A") if v == "H"]               # COMP1OUT on GPIO1
        assert len(trip) == 1 and abs(trip[0] - 9.15e6) < 30e3                   # 1.652 V on the 8..10 ms ramp
        assert a and a[-1][1] == "L" and a[-1][0] <= trip[0] + 1e3                # EPWM1A forced low
        rises = [t for t, v in a if v == "H"]
        assert len(rises) > 40 and all(abs((y - x) - 50e3) < 20 for x, y in zip(rises, rises[1:]))
        chip = s.ctxs["A"].chip
        assert chip.bus.read(0x8000, 1) == 1 and chip.peripherals["EPWM1"].get("TZFLG") & 0x0C == 0x0C
    finally:
        s.close()


def test_ecap_measures_epwm(tmp_path):
    img = build("f28027_ecap", tmp_path)
    s = board("f28027_ecap_loopback", A=img)
    try:
        s.run_for(LOCK_NS + 1e6)
        chip, sy = s.ctxs["A"].chip, syms(img)
        assert chip.bus.read(sy["ecap_period"], 2) == 3000 and chip.bus.read(sy["ecap_high"], 2) == 900
        assert chip.bus.read(sy["ecap_count"], 1) >= 15
    finally:
        s.close()


def test_spi_flash_board(tmp_path):
    img = build("f28027_spi_flash", tmp_path)
    from chipem.board import load_board
    from chipem.session import Session
    data = tmp_path / "flash.bin"
    data.write_bytes(bytes([0x12, 0x34, 0x56, 0x78]))
    bd = load_board(ROOT / "boards" / "f28027_spi_flash.yaml")
    bd.chips[0].firmware = img
    bd.devices[0].options["file"] = str(data)
    s = Session.from_board(bd, quiet=True)
    s.run_for(LOCK_NS + 0.3e6)
    chip, sy = s.ctxs["A"].chip, syms(img)
    got = [chip.bus.read(sy[n], 1) for n in ("spi_id0", "spi_id1", "spi_id2", "spi_data0", "spi_data1",
                                             "spi_data2", "spi_data3", "spi_done")]
    assert got == [0xEF, 0x40, 0x16, 0x12, 0x34, 0x56, 0x78, 1]
    clk = [t for t, v in edges(s, "GPIO18") if v == "H"]
    assert len(clk) == 12 * 8 and abs((clk[1] - clk[0]) - 266.7) < 1                # 3.75 MHz
    assert not [e for e in s.events(kinds={"warn"})]


def test_k22_uart_board(tmp_path):
    k22 = ROOT / "firmware" / "samples" / "build" / "uart_link_sender.elf"
    if not k22.exists():
        pytest.skip("K22 uart_link_sender.elf not built")
    s = board("f28027_k22_uart", B=build("f28027_sci_echo", tmp_path))
    try:
        s.run_for(13e6)
        evs = s.events(kinds={"uart"})
        echo = bytes(e.get("byte") for e in evs if e.get("chip") == "B" and e.get("dir") == "tx")
        assert b"PING 4\r\n" in echo
    finally:
        s.close()


# ------------------------------------------------------------------ boot ROM stand-in (SPRUI09A 2.2, Table 2-8)
_PROBE = """
        .sect "codestart", 0x3F7FF6
        LB      flash_main
        .sect "text", 0x3F0000
flash_main:
        MOVW    DP, #DP(0x8000)
        MOV     @0x8000, #0x00F1
f_loop: SB      f_loop, UNC
        .sect "otp", 0x3D7800
otp_main:
        MOVW    DP, #DP(0x8000)
        MOV     @0x8000, #0x0070
o_loop: SB      o_loop, UNC
"""


def _probe(tmp_path, pins=(), otp=None):
    from chipem.session import Session
    text = _PROBE + (f"""        .sect "otpkey", 0x3D7BFE
        .word   0x55AA, {otp}
""" if otp is not None else "")
    s = Session("tms320f28027", build("boot_probe", tmp_path, text), quiet=True)
    for p, lvl in pins:
        s.chip.pins.set_external(p, lvl)
    s.reset()
    s.run_for(0.2e6)
    return s


def test_boot_rom_getmode_to_flash_runs_device_cal(tmp_path):
    s = _probe(tmp_path)
    assert s.chip.bus.read(0x8000, 1) == 0x00F1                                     # GPIO37 = GPIO34 = 1: GetMode
    assert s.chip.bus.read(0x0D00, 1) == 0x55AA and s.chip.bus.read(0x0D01, 1) == 3  # EMU_KEY / EMU_BMODE
    assert s.chip.clock.hz == 10e6 and (s.chip.peripherals["SYSCTRL"].get("PLLSTS") >> 7) & 3 == 3
    assert s.chip.peripherals["CSM"].secure is False
    assert s.chip.peripherals["SYSCTRL"].wd_enabled()                               # direct branch: watchdog on
    assert not [e for e in s.events(kinds={"warn"})]


def test_boot_rom_getmode_otp_and_wait(tmp_path):
    s = _probe(tmp_path, otp=6)                                                      # OTP_KEY 0x55AA, OTP_BMODE OTP
    assert s.chip.bus.read(0x8000, 1) == 0x0070
    s = _probe(tmp_path, pins=[("GPIO34", "L")])                                     # 1 0: wait boot
    assert s.chip.bus.read(0x8000, 1) == 0 and s.chip.bus.read(0x0D01, 1) == 2
    assert not s.chip.peripherals["SYSCTRL"].wd_enabled()


# ------------------------------------------------------------------ TI-compiled C (cl2000; skipped when not built)
def _cimg(name):
    p = CBUILD / f"{name}.out"
    if not p.exists():
        pytest.skip(f"{p.name} not built (firmware/samples_c2000_c/build.sh {name})")
    return p


def test_c_launchpad_demo_temperature_and_console():
    from chipem.session import Session
    s = Session("tms320f28027", _cimg("f28027_launchpad"), quiet=True)
    s.schedule_uart(9.2e6, "SCIA", b"ok")
    s.run_for(10.5e6)
    assert s.uart_output("SCIA") == b"F28027 60MHz T=24C\r\nok"                      # 25 C sensor, OTP getters
    assert dict((v, c) for v, _, c in s.irq_summary())[38] >= 3                      # TINT0 every 1 ms


def test_c_f28069_can_board_with_stm32():
    from chipem.session import Session
    _cimg("f28069_can")
    stm = ROOT / "firmware" / "samples_stm32" / "build" / "can_echo.elf"
    if not stm.exists():
        pytest.skip("STM32 can_echo.elf not built")
    s = Session.from_board(ROOT / "boards" / "f28069_stm32_can.yaml", quiet=True)
    try:
        s.run_for(30e6)
        evs = s.events(kinds={"uart"})
        a_out = bytes(e.get("byte") for e in evs if e.get("chip") == "A" and e.get("dir") == "tx").decode()
        assert a_out.startswith("CAN F28069\r\nTX 100 00\r\nRX 202 [4] 00 00 4B 32\r\nTX 100 01"), a_out
        chip = s.ctxs["A"].chip
        assert chip.clock.hz == 90e6 and chip.peripherals["ECANA"].bitrate() == pytest.approx(500e3)
        sy = {}
        assert len(edges(s, "GPIO34", "A")) >= 2 and len(edges(s, "GPIO39", "A")) >= 2   # LaunchPad LEDs
    finally:
        s.close()
