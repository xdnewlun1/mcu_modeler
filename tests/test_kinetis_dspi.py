"""Kinetis DSPI (chipem/periph/kinetis/dspi.py) on the pins: CTAR baud rate and delays (tCSC, tASC, tDT), CPOL/CPHA,
LSB first, PCSIS, continuous selection (CONT), SIN sampling, slave mode clocked from the pins, the SPI flash with its
chip select on a PCS pin, and the K22 master <-> STM32 slave board.  The firmware (button_mirror) only idles; the tests
drive the DSPI registers directly between short runs (bus clock = core = 120 MHz: SIM_CLKDIV1 at reset)."""
from pathlib import Path

import pytest

from chipem.periph.kinetis.dspi import ctar_timing
from chipem.session import Session

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "firmware/samples/build/button_mirror.elf"
pytestmark = pytest.mark.skipif(not FW.exists(), reason="sample firmware not built")

MCR, TCR, CTAR0, CTAR1, SR, RSER, PUSHR, POPR = 0x0, 0x8, 0xC, 0x10, 0x2C, 0x30, 0x34, 0x38
MSTR, HALT, MDIS = 1 << 31, 1, 1 << 14
TCF, TXRXS, EOQF, TFUF, RFDF = 1 << 31, 1 << 30, 1 << 28, 1 << 27, 1 << 17
CONT, EOQ = 1 << 31, 1 << 27
NS = 1e9 / 120e6
PINS = ("PTD0", "PTD1", "PTD2", "PTD3")         # SPI0 PCS0, SCK, SOUT, SIN (ALT2); button_mirror uses PTC5/PTC6


def ctar(fmsz=7, cpol=0, cpha=0, lsbfe=0, pcssck=0, pasc=0, pdt=0, pbr=0, cssck=0, asc=0, dt=0, br=0, dbr=0):
    return (dbr << 31 | fmsz << 27 | cpol << 26 | cpha << 25 | lsbfe << 24 | pcssck << 22 | pasc << 20 | pdt << 18
            | pbr << 16 | cssck << 12 | asc << 8 | dt << 4 | br)


def _session():
    s = Session("mk22fn1m0vll12", FW, clock_hz=120e6)
    chip = s.chip
    s.run_for(50e3)                                                              # firmware init done
    chip.bus.write(0x40047038, 4, chip.bus.read(0x40047038, 4) | (1 << 12))      # SIM_SCGC5.PORTD
    chip.bus.write(0x4004703C, 4, chip.bus.read(0x4004703C, 4) | (1 << 12))      # SIM_SCGC6.SPI0
    for pin in (0, 1, 2, 3):
        chip.peripherals["PORTD"].write(4 * pin, 4, 2 << 8)
    return s, chip, chip.peripherals["SPI0"]


def _edges(chip):
    ev = []
    chip.pins.subscribe(lambda e: ev.append((e.t_ns, e.pin, e.new)) if e.field == "level" and e.pin in PINS
                        else None)
    return ev


def _of(ev, pin, t0):
    return [(round((t - t0) / NS), lvl) for t, p, lvl in ev if p == pin]


def test_ctar_timing_table():
    t = ctar_timing(ctar(pbr=1, br=0, dbr=1, cpha=0))          # PBR 3, BR 2, DBR: 3 clocks, 33/66 duty
    assert t["P"] == 3 and t["A"] == 1
    t = ctar_timing(ctar(pbr=1, br=0, dbr=1, cpha=1))          # CPHA=1: 66/33
    assert t["P"] == 3 and t["A"] == 2
    t = ctar_timing(ctar(pbr=3, br=3, pcssck=1, cssck=4, pasc=1, asc=4, pdt=1, dt=14))
    assert t["P"] == 7 * 8 and t["A"] == 28
    assert t["csc"] == 3 * 32 and t["asc"] == 3 * 32 and t["dt"] == 3 * 32768    # Tables 47-131..133
    assert ctar_timing(ctar(br=15))["P"] == 2 * 32768


def test_master_mode0_timing_pcs_and_sin():
    s, chip, spi = _session()
    ev = _edges(chip)
    chip.pins.set_external("PTD3", "H")
    spi.write(MCR, 4, MSTR | (1 << 16) | HALT)                  # PCS0 inactive high
    spi.write(CTAR0, 4, ctar(br=1, cssck=1, asc=2, dt=3))       # SCK = 120 MHz / (2 x 4) = 15 MHz
    assert chip.pins.get("PTD0").level() == "H" and chip.pins.get("PTD1").level() == "L"
    assert spi.baud() == pytest.approx(15e6)
    spi.write(MCR, 4, MSTR | (1 << 16))
    t0 = chip.now_ns()
    spi.write(PUSHR, 4, (1 << 16) | 0xA5)
    s.run_for(5000)
    assert spi.read(SR, 4) & (TCF | RFDF) == TCF | RFDF and spi.read(POPR, 4) == 0xFF
    pcs, sck, sout = _of(ev, "PTD0", t0), _of(ev, "PTD1", t0), _of(ev, "PTD2", t0)
    assert pcs[0] == (0, "L")
    assert [t for t, lvl in sck if lvl == "H"] == [4 + 8 * k for k in range(8)]        # tCSC = 1 x 4 clocks
    assert [t for t, lvl in sck if lvl == "L"] == [8 + 8 * k for k in range(8)]
    last = 8 + 8 * 7
    assert pcs[1] == (last + 8, "H")                                                     # tASC = 1 x 8
    bits = []                                                                            # SOUT level at each rise
    for r in (4 + 8 * k for k in range(8)):
        lv = [lvl for t, lvl in sout if t <= r]
        bits.append(1 if (lv[-1] if lv else "L") == "H" else 0)
    assert int("".join(map(str, bits)), 2) == 0xA5
    t1 = chip.now_ns()
    spi.write(PUSHR, 4, (1 << 16) | 0x01)                     # next frame: tDT (16 clocks) after PCS negation
    s.run_for(5000)
    pcs2 = [x for x in _of(ev, "PTD0", t0) if x[0] > last + 8]
    assert pcs2[0][1] == "L" and pcs2[0][0] * NS + t0 >= t1 - NS
    assert spi.read(TCR, 4) >> 16 == 2


def test_continuous_selection_cpha1_cpol1_lsb_first():
    s, chip, spi = _session()
    ev = _edges(chip)
    spi.write(MCR, 4, MSTR | (1 << 16) | HALT)
    spi.write(CTAR0, 4, ctar(cpol=1, cpha=1, lsbfe=1, br=1, cssck=1, asc=1, dt=5))
    assert chip.pins.get("PTD1").level() == "H"                                          # CPOL = 1 idles high
    spi.write(MCR, 4, MSTR | (1 << 16))
    t0 = chip.now_ns()
    spi.write(PUSHR, 4, CONT | (1 << 16) | 0x01)
    spi.write(PUSHR, 4, (1 << 16) | 0x80)
    s.run_for(10000)
    pcs = _of(ev, "PTD0", t0)
    assert [lvl for _t, lvl in pcs] == ["L", "H"]                                        # held across both frames
    sck = _of(ev, "PTD1", t0)
    falls = [t for t, lvl in sck if lvl == "L"]                                          # leading edges (CPOL = 1)
    assert falls[:8] == [4 + 8 * k for k in range(8)]
    # CONT: the next frame starts tASC (4) + tCSC (4) after the last SCK edge of the first (4 after the last
    # leading edge), without tDT
    assert falls[8] == falls[7] + 4 + 4 + 4
    sout = _of(ev, "PTD2", t0)
    # CPHA = 1: data changes on leading edges; LSB first: 0x01 -> 1 then seven 0s, 0x80 -> seven 0s then 1
    assert sout[0] == (4, "H") and sout[1] == (12, "L")
    assert sout[-1] == (falls[15], "H")
    assert pcs[1][0] == falls[15] + 4 + 4                                                # last edge + tASC
    assert [f[1] for f in spi.frames] == [0x01, 0x80]


def test_eoq_stops_and_without_pins_only_timing():
    s, chip, spi = _session()
    for pin in (0, 1, 2, 3):
        chip.peripherals["PORTD"].write(4 * pin, 4, 0)                                   # unmux: no edges
    spi.write(MCR, 4, MSTR)
    spi.write(CTAR0, 4, ctar(fmsz=15, br=2))                                             # 16 bits at 120/(2x6)
    spi.write(PUSHR, 4, EOQ | 0x1234)
    spi.write(PUSHR, 4, 0x5678)
    s.run_for(16 * 12 * NS + 50)
    assert spi.read(SR, 4) & EOQF and not spi.read(SR, 4) & TXRXS
    assert [f[1] for f in spi.frames] == [0x1234] and spi.read(POPR, 4) == 0xFFFF       # params.miso: idle high
    spi.write(SR, 4, EOQF)                                                               # restart
    s.run_for(20 * 12 * NS)
    assert [f[1] for f in spi.frames] == [0x1234, 0x5678]


def test_slave_mode_clocked_from_pins():
    s, chip, spi = _session()
    pins = chip.pins
    pins.set_external("PTD0", "H")
    pins.set_external("PTD1", "L")
    spi.write(MCR, 4, 0)                                                                 # slave, running
    spi.write(CTAR0, 4, ctar(fmsz=7))
    assert pins.get("PTD1").direction == "in" and pins.get("PTD2").direction == "in"
    spi.write(PUSHR, 4, 0x3C)
    pins.set_external("PTD0", "L")                                                       # selected: SOUT drives
    assert pins.get("PTD2").direction == "out"
    got = []
    for i in range(8):
        got.append(1 if pins.get("PTD2").level() == "H" else 0)                         # bit valid before rise
        pins.set_external("PTD3", "H" if (0xC5 >> (7 - i)) & 1 else "L")
        pins.set_external("PTD1", "H")
        pins.set_external("PTD1", "L")
    assert int("".join(map(str, got)), 2) == 0x3C
    assert spi.read(SR, 4) & (TCF | RFDF) == TCF | RFDF and spi.read(POPR, 4) == 0xC5
    assert not spi.read(SR, 4) & TFUF
    for _ in range(8):                                                                   # no data queued: TFUF
        pins.set_external("PTD1", "H")
        pins.set_external("PTD1", "L")
    assert spi.read(SR, 4) & TFUF
    pins.set_external("PTD0", "H")
    assert pins.get("PTD2").direction == "in"
    pins.set_external("PTD0", "L")                                                       # partial frame dropped
    pins.set_external("PTD1", "H")
    pins.set_external("PTD1", "L")
    pins.set_external("PTD0", "H")
    assert spi.read(TCR, 4) >> 16 == 2


def test_spi_flash_with_chip_select_on_pcs_pin():
    s, chip, spi = _session()
    s.attach_spi_flash("SPI0:cs=PTD0")
    spi.write(MCR, 4, MSTR | (1 << 16))
    spi.write(CTAR0, 4, ctar(br=1))
    rx = []
    for i, b in enumerate((0x9F, 0, 0, 0)):
        spi.write(PUSHR, 4, (CONT if i < 3 else 0) | (1 << 16) | b)
        s.run_for(2000)
        rx.append(spi.read(POPR, 4))
        spi.write(SR, 4, RFDF | TCF)
    assert rx[1:] == [0x20, 0x20, 0x15]
    assert chip.pins.get("PTD0").level() == "H"


def test_board_k22_master_stm32_slave():
    elf = ROOT / "firmware/samples/build/spi_master.elf"
    peer = ROOT / "firmware/samples_stm32/build/spi_slave.elf"
    if not (elf.exists() and peer.exists()):
        pytest.skip("spi_master / spi_slave samples not built")
    from chipem.board import load_board
    s = Session.from_board(load_board(ROOT / "boards" / "k22_stm32_spi.yaml"))
    s.run_for(7.6e6)
    a, b = s.board.chips["A"], s.board.chips["B"]
    assert a.uart_out("UART0").decode() == "M 1 WORLD\r\nM 2 WORLD\r\nM 3 WORLD\r\n"
    assert b.uart_out("USART1").decode() == "S 1 hello\r\nS 2 hello\r\nS 3 hello\r\n"
    spi = a.peripherals["SPI0"]
    assert spi.baud() == pytest.approx(60e6 / 512)
    assert [f[1] for f in list(spi.frames)[:5]] == list(b"hello")
    s.close()
