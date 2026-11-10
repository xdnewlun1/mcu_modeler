"""End-to-end runs of the sample firmware (firmware/samples/build/*.elf) on the MK22FN1M0VLL12 description,
exercising SIM/PORT/GPIO/WDOG/MCG through real compiled code, plus a construction check of MK22FX512VLL12."""
from __future__ import annotations

from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.firmware import load_firmware

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "firmware" / "samples" / "build"
K22 = ROOT / "chips" / "mk22fn1m0vll12.yaml"
FX512 = ROOT / "chips" / "mk22fx512vll12.yaml"
MY_MODELS = {"kinetis.sim", "kinetis.port", "kinetis.gpio", "kinetis.wdog", "kinetis.mcg", "kinetis.osc",
             "kinetis.smc", "kinetis.pmc", "kinetis.rcm", "kinetis.llwu", "kinetis.vbat", "kinetis.ftfe",
             "kinetis.adc", "kinetis.crc", "kinetis.dac", "kinetis.cmp", "kinetis.ewm", "kinetis.vref"}


@pytest.fixture(scope="module")
def desc():
    return load_chip(K22)


def boot(desc, name: str, **kw) -> tuple[Chip, list]:
    elf = BUILD / f"{name}.elf"
    if not elf.exists():
        pytest.skip(f"{elf} not built")
    chip = Chip(desc, **kw)
    chip.load_firmware(load_firmware(elf))
    chip.reset()
    events: list = []
    chip.pins.subscribe(lambda e: events.append(e) if e.pin == "PTC5" and e.field == "level" else None)
    return chip, events


def warns(chip) -> list[str]:
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "warn"]


def test_blinky_period(desc):
    chip, ev = boot(desc, "blinky")
    chip.run_for(50e6)
    assert chip.pins.get("PTC5").owner == "GPIO" and chip.pins.get("PTC5").direction == "out"
    levels = [e.new for e in ev]
    assert len(levels) > 100
    assert levels[0] == "L"                               # PCOR after PDDR
    assert all(a != b for a, b in zip(levels, levels[1:]))   # strictly alternating
    rises = [e.t_ns for e in ev if e.new == "H"]
    periods = [b - a for a, b in zip(rises, rises[1:])]
    # 1000 iterations x 5 instructions per half period = 10,000 cycles per period at 20.97 MHz (FEI reset)
    hz = chip.clock.hz
    assert hz == pytest.approx(20_971_520)
    mean = sum(periods) / len(periods)
    assert mean == pytest.approx(10_000 / hz * 1e9, rel=0.02)
    # pin timestamps are instruction-precise (not slice-granular): every half period is the same 5000(+3 loop
    # overhead) instructions = 238.4 us at 20.97152 MHz
    halves = [b.t_ns - a.t_ns for a, b in zip(ev[1:], ev[2:])]
    if getattr(chip.core, "exact_counts", False):
        assert max(halves) - min(halves) < 1.0
        assert halves[0] == pytest.approx(5000 / hz * 1e9, abs=10 / hz * 1e9)
    else:
        assert max(periods) - min(periods) <= 2 * chip.max_slice / hz * 1e9 + 1
    assert "WDOG disabled" in [e.fields.get("msg") for e in chip.log if e.kind == "info"]
    assert not warns(chip)


def test_button_mirror(desc):
    chip, ev = boot(desc, "button_mirror")
    pins = chip.pins
    chip.run_for(1e6)
    assert pins.get("PTC6").owner == "GPIO" and pins.get("PTC6").pull == "up"
    assert pins.get("PTC6").level() == "H"                # pull-up, nothing connected
    assert pins.get("PTC5").level() == "L"
    for ext, want in (("L", "H"), ("H", "L"), ("L", "H"), (None, "L"), ("L", "H"), ("H", "L")):
        pins.set_external("PTC6", ext)
        chip.run_for(200e3)
        assert pins.get("PTC5").level() == want, (ext, want)
    assert [e.new for e in ev] == ["L", "H", "L", "H", "L", "H", "L"]
    assert not warns(chip)


def test_port_irq_falling_edges(desc):
    chip, ev = boot(desc, "port_irq")
    pins = chip.pins
    port = chip.peripherals["PORTC"]
    chip.run_for(1e6)
    assert chip.sleeping                                  # main loop is WFI
    assert pins.get("PTC5").level() == "L" and ev[-1].new == "L"
    n0 = len(ev)
    expected = "L"
    for k in range(5):
        pins.set_external("PTC6", "H")
        chip.run_for(300e3)
        assert len(ev) == n0 + k                          # rising edge: nothing
        pins.set_external("PTC6", "L")                    # falling edge -> one toggle
        chip.run_for(300e3)
        expected = "H" if expected == "L" else "L"
        assert len(ev) == n0 + k + 1 and ev[-1].new == expected
        assert port.get("ISFR") == 0 and not port.pcr(6) & (1 << 24)
        assert 16 + 61 not in chip.arch.pending
    pins.set_external("PTC6", None)                       # pull-up -> rising, no toggle
    chip.run_for(300e3)
    assert len(ev) == n0 + 5
    assert not warns(chip)


def test_fpu_test(desc):
    chip, ev = boot(desc, "fpu_test")
    chip.run_for(2e6)
    assert chip.pins.get("PTC5").level() == "H"
    assert chip.pins.get("PTC4").level() == "H"
    assert not [e for e in chip.log if e.kind == "fault"]


def test_crc_dac_cmp_sample(desc):
    """crc_dac.c: CRC-32 and CRC-16 from the CRC module, VREF start-up, the DAC0 buffer stepped by software
    triggers, and CMP0 (CMP0_IN0 vs. the 6-bit DAC at 1.65 V) reported on UART0 and on PTC5 (CMP0_OUT)."""
    chip, _ = boot(desc, "crc_dac")
    cmp0 = chip.peripherals["CMP0"]
    assert cmp0.set_channel("CMP0_IN0", 1.0)                   # volts (the --adc path: CMP0_IN0=1.0V)
    chip.run_for(40e6)
    out = chip.uart_out("UART0")
    assert out == (b"CRC32=CBF43926\r\nCRC16=29B1\r\nVREF ok\r\n"
                   b"DAC rp=1\r\nDAC rp=2\r\nDAC rp=3\r\nDAC rp=0\r\nDAC rp=1\r\nDAC sr=7\r\nCOUT=0\r\n")
    dac = chip.peripherals["DAC0"]
    assert dac.code() == 0x800 and dac.voltage() == pytest.approx(3.3 * 0x801 / 4096)
    assert chip.pins.get("PTC5").owner == "CMP0_OUT" and chip.pins.get("PTC5").level() == "L"
    cmp0.set_channel("CMP0_IN0", 2.0)                         # above 1.65 V
    chip.run_for(5e6)
    assert chip.pins.get("PTC5").level() == "H"
    cmp0.set_channel("CMP0_IN0", 1.5)
    chip.run_for(5e6)
    assert chip.uart_out("UART0").endswith(b"COUT=0\r\nCOUT=1\r\nCOUT=0\r\n")
    assert chip.pins.get("PTC5").level() == "L"
    assert not [e for e in chip.log if e.kind in ("warn", "fault")]


def test_crc_dac_cli_with_adc_option(capsys):
    """`chipem run ... --adc CMP0_IN0=2V` reaches the comparator through Session.set_adc (volts only)."""
    elf = BUILD / "crc_dac.elf"
    if not elf.exists():
        pytest.skip(f"{elf} not built")
    from chipem.cli import main
    assert main(["run", str(elf), "--for", "40ms", "--adc", "CMP0_IN0=2V", "--summary-only"]) == 0
    out = capsys.readouterr().out
    assert "CRC32=CBF43926" in out and "COUT=1" in out and "COUT=0" not in out


def test_samples_run_with_clock_override(desc):
    chip, ev = boot(desc, "blinky", clock_hz=50e6)      # differs from the YAML's 120 MHz -> MCG frozen
    chip.run_for(10e6)
    assert chip.clock.hz == 50e6 and chip.peripherals["MCG"].frozen
    rises = [e.t_ns for e in ev if e.new == "H"]
    mean = (rises[-1] - rises[0]) / (len(rises) - 1)
    assert mean == pytest.approx(10_000 / 50e6 * 1e9, rel=0.05)


def test_fx512_constructs_without_fallback_stubs():
    d = load_chip(FX512)
    chip = Chip(d)
    fallbacks = [e for e in chip.log if e.kind == "warn" and "not available" in e.fields.get("msg", "")]
    assert not [e for e in fallbacks if e.fields.get("model") in MY_MODELS]
    for p in d.peripherals:
        if p.model in MY_MODELS:
            assert chip.peripherals[p.name].model == p.model
    stubbed = sorted(e.fields.get("model") for e in fallbacks)
    print("models still falling back to stub:", stubbed or "none")


def test_withings_console_answers_help():
    """Real image: with a SPI NOR flash on SPI0 (CS=PTC4) and the UART2 RX line idle-high (adapter connected) the
    firmware starts its shell on UART2 (RX by interrupt, IRQ 35) and answers `help`."""
    fw = ROOT / "s1_off000084_503080.bin"
    if not fw.exists():
        pytest.skip("Withings image not present")
    from chipem.session import Session
    s = Session(FX512, fw, quiet=True)
    s.attach_spi_flash("SPI0:cs=PTC4")
    s.apply_stimulus("PTD2=1")
    s.apply_stimulus(r"100ms:UART2=help\r\n")       # the shell is up at ~35 ms; earlier bytes would be dropped
    s.run_for(5e9)
    out = s.uart_output("UART2")
    # (SPI transfers take real time, so another task's log line may interleave with the banner)
    import re
    banner = re.sub(rb"\[VAS\][^\r]*\r\n", b"", out)
    assert b"UART IS CONNECTED" in banner and b"boot_trace" in out and out.rstrip().endswith(b"shell>")
    assert not [e for e in s.chip.log if e.kind in ("warn", "fault")]
    assert not [e for e in s.chip.log if e.kind == "uart" and e.get("dir") == "drop"]


def test_i2c_eeprom_board_writes_and_reads_back():
    """boards/k22_i2c_eeprom.yaml: K22 I2C0 master + i2c_eeprom part on open-drain nets. The firmware probes an absent
    address (NACK), page-writes a string with acknowledge polling of the 5 ms write cycle and reads it back."""
    from chipem.board import load_board
    from chipem.session import Session
    if not (BUILD / "i2c_eeprom.elf").exists():
        pytest.skip("i2c_eeprom not built")
    s = Session.from_board(load_board(ROOT / "boards" / "k22_i2c_eeprom.yaml"))
    s.run_for(30e6)
    out = bytes(s.chip.peripherals["UART0"].output).decode()
    assert "0x51: NACK" in out and 'wrote "Hello over I2C!", read "Hello over I2C!"' in out and "PASS" in out
    ee = s.board.parts["EE1"]
    assert ee.dump(0x10, 15) == b"Hello over I2C!" and ee.state()["writes"] == 2      # pages 0x10-0x17, 0x18-0x1E
    assert ee.state()["naks"] > 0 and not ee.busy                                     # acknowledge polling happened
    i2c = s.chip.peripherals["I2C0"]
    assert i2c.baud() == pytest.approx(60e6 / 640)                                    # bus 60 MHz, ICR 0x2D
    # SCL edges on PTB2 are real: one SCL period apart inside a byte
    rises = [e.t_ns for e in s.log if e.kind == "pin" and e.fields.get("pin") == "PTB2"
             and e.fields.get("field") == "level" and e.fields.get("new") == "H"]
    gaps = [b - a for a, b in zip(rises, rises[1:])]
    assert min(gaps) == pytest.approx(1e9 / (60e6 / 640), rel=1e-3)
    s.close()


def test_can_echo_sample_with_host_node():
    """firmware/samples/can_echo.c alone on its FlexCAN0 bus with an acknowledging host node: a frame every 10 ms
    (100#<n>4B32, 500 kbit/s; SysTick starts after the ~1.1 ms banner), frames 0x200-0x27F printed on UART0; other
    IDs are filtered out."""
    from chipem.canbus import CanFrame
    from chipem.session import Session
    elf = BUILD / "can_echo.elf"
    if not elf.exists():
        pytest.skip(f"{elf} not built")
    s = Session("mk22fn1m0vll12", elf, clock_hz=120e6, quiet=True)
    try:
        blk = s.chip.peripherals["CAN0"]
        host = blk.can_bus.add_host()
        host.send(CanFrame(0x211, b"\x01\x02"), 3e6)
        host.send(CanFrame(0x311, b"\x03"), 4e6)            # outside the RXMGMASK range
        s.run_for(32e6)
        sent = [(t, f) for t, f in host.received]
        assert [str(f) for _t, f in sent] == ["100#00004B32", "100#00014B32", "100#00024B32"]
        assert blk.bitrate() == pytest.approx(500e3)
        evs = [e for e in s.events(kinds={"can"}) if e.fields["node"] == "CAN0"]
        for e, (t, f) in zip(evs, sent):
            assert e.t_ns == pytest.approx(t) and e.t_ns - e.fields["sof_ns"] == pytest.approx(f.bits * 2000.0)
        assert 11e6 < sent[0][0] < 11.5e6 and sent[1][0] - sent[0][0] == pytest.approx(10e6, rel=0.02)
        out = s.chip.uart_out("UART0").decode()
        assert out.startswith("CAN ECHO K22\r\n") and "RX 211 [2] 01 02\r\n" in out and "RX 311" not in out
        assert "TX 1\r\nTX 2\r\n" in out.replace("RX 211 [2] 01 02\r\n", "")
        assert blk.errors.tec == 0 and blk.rx_frames == 2 and blk.rx_lost == 1
    finally:
        s.close()
