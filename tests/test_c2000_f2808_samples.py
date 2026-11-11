"""End-to-end on the real C28x core: the TMS320F2808 samples (firmware/samples_c2000/f2808_*.asm, assembled
in-process), the boot ROM stand-in (boot-mode pins), the TI-compiled C samples (firmware/samples_c2000_c/f2808_*.c,
skipped when not built) and the boards boards/f2808_*.yaml.  Runs are kept to a few ms of virtual time; every sample
first relocks the PLL (131072 OSCCLK = 6.55 ms at 10 MHz SYSCLKOUT), then runs at 100 MHz."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "firmware" / "samples_c2000"
CBUILD = ROOT / "firmware" / "samples_c2000_c" / "build"
HAVE_CORE = importlib.util.find_spec("chipem.core.c28x") is not None
pytestmark = pytest.mark.skipif(not HAVE_CORE, reason="chipem.core.c28x (C28x interpreter) not available")
PLL_LOCK_NS = 131072 / 20e6 * 1e9


def build(name: str, tmp_path: Path, text: str | None = None) -> Path:
    from chipem.core.c28x.asm import assemble
    src = SAMPLES / f"{name}.asm"
    out = tmp_path / f"{name}.c28img"
    assemble(text if text is not None else src.read_text(), filename=str(src)).write_c28img(out)
    return out


def session(name: str, tmp_path: Path, **kw):
    from chipem.session import Session
    return Session("tms320f2808", build(name, tmp_path), quiet=True, **kw)


def syms(img: Path) -> dict:
    return json.loads(img.read_text())["symbols"]


def edges(s, pin: str, chip: str | None = None) -> list[tuple[float, str]]:
    return [(e.t_ns, e.get("new")) for e in s.events(kinds={"pin"}) if e.get("pin") == pin
            and e.get("field") == "level" and (chip is None or e.get("chip") == chip)]


def word(chip, addr: int) -> int:
    return chip.bus.read(addr, 1)


# ------------------------------------------------------------------ assembler samples
def test_blinky_1ms_after_pll_lock(tmp_path):
    s = session("f2808_blinky", tmp_path)
    s.run_for(12e6)
    ev = [t for t, _ in edges(s, "GPIO34")]
    assert s.chip.clock.hz == 100e6 and len(ev) >= 5
    assert ev[0] >= PLL_LOCK_NS
    gaps = [b - a for a, b in zip(ev[1:], ev[2:])]
    assert gaps and all(abs(g - 1e6) < 1e3 for g in gaps), gaps
    p = s.chip.pins.get("GPIO34")
    assert p.direction == "out" and p.owner == "GPIO" and p.num == 43


def test_timer_irq_every_ms(tmp_path):
    s = session("f2808_timer_irq", tmp_path)
    s.run_for(12.2e6)
    assert s.irq_summary() == [(38, "TINT0", 5)]
    ev = [t for t, _ in edges(s, "GPIO34")][1:]
    gaps = [b - a for a, b in zip(ev, ev[1:])]
    assert gaps and all(abs(g - 1e6) < 1e3 for g in gaps), gaps
    assert s.chip.insns < 70_000                                      # PLL wait (65.5k), then asleep in IDLE


def test_epwm_complementary_pair(tmp_path):
    s = session("f2808_epwm", tmp_path)
    s.run_for(PLL_LOCK_NS + 0.5e6)
    a0, b0 = len(edges(s, "GPIO0")), len(edges(s, "GPIO1"))
    s.run_for(0.5e6)                                                  # 10 periods of 50 us
    ea, eb = edges(s, "GPIO0")[a0:], edges(s, "GPIO1")[b0:]
    rises = [t for t, lvl in ea if lvl == "H"]
    falls = [t for t, lvl in ea if lvl == "L"]
    assert len(rises) == 10 and all(abs((b - a) - 50e3) < 1 for a, b in zip(rises, rises[1:]))
    highs = [f - r for r, f in zip(rises, [f for f in falls if f > rises[0]])]
    assert all(abs(h - 14.5e3) < 20 for h in highs)                   # 30 % - 0.5 us dead band
    assert len([1 for _, v in eb if v == "H"]) == 10
    assert s.chip.pins.get("GPIO0").owner == "EPWM1A" and s.chip.pins.get("GPIO1").owner == "EPWM1B"


def test_adc_epwm_soc_with_interrupt(tmp_path):
    img = build("f2808_adc", tmp_path)
    from chipem.session import Session
    s = Session("tms320f2808", img, quiet=True)
    adc = s.chip.peripherals["ADC"]
    adc.set_channel("ADCINA0", 1234)
    adc.set_channel("A1", 4000)
    s.run_for(PLL_LOCK_NS + 5e6 + 1.05e6)                             # PLL, 5 ms ADC settling, 1 ms of samples
    sy = syms(img)
    n = word(s.chip, sy["adc_count"])
    assert 9 <= n <= 11 and s.irq_summary() == [(32, "SEQ1INT", n)]
    assert word(s.chip, sy["adc_a0"]) == 1234 and word(s.chip, sy["adc_a1"]) == 4000
    ev = [t for t, _ in edges(s, "GPIO34")][1:]
    assert all(abs((b - a) - 100e3) < 50 for a, b in zip(ev, ev[1:]))   # 10 kHz SOCA
    adc.set_channel("ADCINA0", 77)
    s.run_for(0.25e6)
    assert word(s.chip, sy["adc_a0"]) == 77


def test_spi_flash_board_on_spib(tmp_path):
    from chipem.board import load_board
    from chipem.session import Session
    img = build("f2808_spi_flash", tmp_path)
    data = tmp_path / "flash.bin"
    data.write_bytes(bytes([0xDE, 0xAD, 0xBE, 0xEF]))
    bd = load_board(ROOT / "boards" / "f2808_spi_flash.yaml")
    bd.chips[0].firmware = img
    bd.devices[0].options["file"] = str(data)
    s = Session.from_board(bd, quiet=True)
    s.run_for(PLL_LOCK_NS + 0.5e6)
    chip = s.ctxs["A"].chip
    sy = syms(img)
    got = [word(chip, sy[n]) for n in ("spi_id0", "spi_id1", "spi_id2", "spi_data0", "spi_data1", "spi_data2",
                                        "spi_data3", "spi_done")]
    assert got == [0xEF, 0x40, 0x16, 0xDE, 0xAD, 0xBE, 0xEF, 1]
    assert chip.pins.get("GPIO26").owner == "SPICLKB" and chip.pins.get("GPIO27").level() == "H"
    clk = edges(s, "GPIO26")
    assert len([1 for _, v in clk if v == "H"]) == 12 * 8             # 12 characters x 8 SPICLKB pulses
    rises = [t for t, v in clk if v == "H"]
    assert abs((rises[1] - rises[0]) - 200) < 1                       # 5 MHz (LSPCLK 25 MHz / 5)
    assert not [e for e in s.events(kinds={"warn"})]


def test_ecan_selftest_loopback_500k(tmp_path):
    img = build("f2808_ecan_loopback", tmp_path)
    from chipem.session import Session
    s = Session("tms320f2808", img, quiet=True)
    s.run_for(PLL_LOCK_NS + 3.5e6)
    sy = syms(img)
    assert word(s.chip, sy["can_mdl_lo"]) == 0x3344 and word(s.chip, sy["can_mdl_hi"]) == 0x1122
    assert word(s.chip, sy["can_rx"]) == 4 and word(s.chip, sy["can_tx"]) == 4
    ec = s.chip.peripherals["ECANA"]
    assert ec.bitrate() == pytest.approx(500e3)                       # 100 MHz / (20 x 10 tq): no /2 on the 280x
    ev = [t for t, _ in edges(s, "GPIO34")][1:]
    assert all(abs((b - a) - 1e6) < 5e3 for a, b in zip(ev, ev[1:]))  # one frame per ms (CPU timer 0)


def test_sci_b_echo_uppercase(tmp_path):
    s = session("f2808_sci_echo", tmp_path)
    s.schedule_uart(7e6, "SCIB", b"hi!\n")                            # SCI-B listens after the ~6.6 ms PLL lock
    s.run_for(8e6)
    assert s.uart_output("SCIB") == b"HI!\n"
    sci = s.chip.peripherals["SCIB"]
    assert abs(sci.baud() - 115200) / 115200 < 0.01 and sci.frame() == "8N1"
    assert s.chip.pins.get("GPIO18").owner == "SCITXDB" and s.chip.pins.get("GPIO19").owner == "SCIRXDB"


# ------------------------------------------------------------------ boot ROM stand-in (SPRS230P Table 9-1)
_BOOT_PROBE = """
        .sect "codestart", 0x3F7FF6
        LB      flash_main
        .sect "text", 0x3E8000
flash_main:
        MOVW    DP, #DP(0x0100)
        MOV     @0x0100, #0x00F1      ; marker: booted to flash
f_loop: SB      f_loop, UNC
        .sect "m0", 0x000000
m0_main:
        MOVW    DP, #DP(0x0100)
        MOV     @0x0100, #0x0030      ; marker: booted to M0
m_loop: SB      m_loop, UNC
"""


@pytest.mark.parametrize("pins,marker", [({}, 0x00F1),                            # pull-ups: 1 1 1 -> flash
                                         ({"GPIO18": "L", "GPIO34": "L"}, 0x0030)])  # 0 1 0 -> M0 SARAM
def test_boot_rom_jump_modes(tmp_path, pins, marker):
    from chipem.session import Session
    img = build("boot_probe", tmp_path, _BOOT_PROBE)                  # no _c_int00: reset runs the boot ROM
    s = Session("tms320f2808", img, quiet=True)
    for p, lvl in pins.items():
        s.chip.pins.set_external(p, lvl)
    s.reset()
    s.run_for(50e3)
    assert word(s.chip, 0x0100) == marker
    assert s.chip.peripherals["CSM"].secure is False                  # dummy read of the erased password
    assert s.chip.peripherals["SYSCTRL"].wd_enabled()                 # a direct jump leaves the watchdog on


def test_boot_rom_loader_mode_disables_watchdog(tmp_path):
    from chipem.session import Session
    s = Session("tms320f2808", build("boot_probe", tmp_path, _BOOT_PROBE), quiet=True)
    s.chip.pins.set_external("GPIO34", "L")                           # 1 1 0: SCI-A loader (not implemented)
    s.reset()
    s.run_for(10e6)                                                   # beyond the 6.55 ms watchdog period
    assert word(s.chip, 0x0100) == 0
    assert not s.chip.peripherals["SYSCTRL"].wd_enabled()
    assert 0x3FF000 <= s.state()["pc"] < 0x400000
    assert not [e for e in s.events(kinds={"reset"}) if e.get("reason") == "watchdog"]


# ------------------------------------------------------------------ TI-compiled C (cl2000; skipped when not built)
def _cimg(name):
    p = CBUILD / f"{name}.out"
    if not p.exists():
        pytest.skip(f"{p.name} not built (firmware/samples_c2000_c/build.sh)")
    return p


def test_c_blinky(tmp_path):
    from chipem.session import Session
    s = Session("tms320f2808", _cimg("f2808_blinky"), quiet=True)
    s.run_for(10e6)
    ev = [t for t, _ in edges(s, "GPIO34")]
    gaps = [b - a for a, b in zip(ev[1:], ev[2:])]
    assert len(ev) >= 3 and ev[0] >= PLL_LOCK_NS and all(abs(g - 1e6) < 1e3 for g in gaps), ev


def test_c_adc_interrupt_and_console():
    from chipem.session import Session
    s = Session("tms320f2808", _cimg("f2808_adc"), quiet=True)
    s.chip.peripherals["ADC"].set_channel("ADCINA0", 1234)
    s.chip.peripherals["ADC"].set_channel("ADCINA1", 4000)
    s.run_for(24e6)                                                   # PLL + 5 ms settling + ~12 ms of samples
    out = s.uart_output("SCIA").decode()
    assert out.startswith("ADC 10 1234 4000\r\nADC 20 1234 4000\r\n"), out
    (vec, name, n), = s.irq_summary()
    assert (vec, name) == (32, "SEQ1INT") and 23 <= n <= 26          # one SOCA every 500 us


def test_c_can_board_with_stm32():
    from chipem.session import Session
    _cimg("f2808_can")
    stm = ROOT / "firmware" / "samples_stm32" / "build" / "can_echo.elf"
    if not stm.exists():
        pytest.skip("STM32 can_echo.elf not built")
    s = Session.from_board(ROOT / "boards" / "f2808_stm32_can.yaml", quiet=True)
    try:
        s.run_for(32e6)
        evs = s.events(kinds={"uart"})
        a_out = bytes(e.get("byte") for e in evs if e.get("chip") == "A" and e.get("dir") == "tx").decode()
        b_out = bytes(e.get("byte") for e in evs if e.get("chip") == "B" and e.get("dir") == "tx").decode()
        assert a_out.startswith("CAN F2808\r\nTX 100 00\r\nRX 202 [4] 00 00 4B 32\r\nTX 100 01\r\nRX 202 [4] 00 01 4B 32"), a_out
        assert "RX 100 [4] 00 01 4B 32" in b_out
        ec = s.ctxs["A"].chip.peripherals["ECANB"]
        assert ec.bitrate() == pytest.approx(500e3) and ec.errors.tec == 0
    finally:
        s.close()


# ------------------------------------------------------------------ board: F2808 <-> K22 over UART
def test_k22_board_echo(tmp_path):
    from chipem.board import load_board
    from chipem.session import Session
    k22 = ROOT / "firmware" / "samples" / "build" / "uart_link_sender.elf"
    if not k22.exists():
        pytest.skip("K22 uart_link_sender.elf not built")
    bd = load_board(ROOT / "boards" / "f2808_k22_uart.yaml")
    bd.chips[1].firmware = build("f2808_sci_echo", tmp_path)
    s = Session.from_board(bd, quiet=True)
    try:
        s.run_for(13e6)
        evs = s.events(kinds={"uart"})
        echo = bytes(e.get("byte") for e in evs if e.get("chip") == "B" and e.get("dir") == "tx")
        assert b"PING " in echo and b"\r\n" in echo
        sent = bytes(e.get("byte") for e in evs if e.get("chip") == "A" and e.get("dir") == "tx")
        assert sent.count(b"ping ") >= 5
        assert len(edges(s, "GPIO34", "B")) >= len(echo)              # one toggle per echoed byte
    finally:
        s.close()
