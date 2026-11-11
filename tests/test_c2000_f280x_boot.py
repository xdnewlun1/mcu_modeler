"""TMS320F2808 boot ROM loaders (chips/tms320f2808_bootrom.asm, SPRU722C): SCI-A with autobaud, SPI-A serial EEPROM,
I2C-A EEPROM, eCAN-A and parallel GPIO, end to end on the real C28x core with the stream built by
tools/c28x_bootstream.py.  Each test resets with the boot-mode pins (GPIO18/29/34) of one loader, sends a small RAM
program over the peripheral and checks that it ran (a marker word and the entry point), plus the key / fallback
rules.  The ROM runs at the reset clock (SYSCLKOUT = OSCCLK / 2 = 10 MHz, LSPCLK = 2.5 MHz)."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import c28x_bootstream as bs  # noqa: E402

HAVE_CORE = importlib.util.find_spec("chipem.core.c28x") is not None
pytestmark = pytest.mark.skipif(not HAVE_CORE, reason="chipem.core.c28x (C28x interpreter) not available")

# the image every session starts with: no entry point (reset runs the boot ROM), a flash program at the boot-to-flash
# entry that writes 0x00F1 (the loaders' fallback for a bad key)
_FLASH_PROBE = """
        .sect "codestart", 0x3F7FF6
        LB      flash_main
        .sect "text", 0x3E8000
flash_main:
        MOVW    DP, #DP(0x0100)
        MOV     @0x0100, #0x00F1
f_loop: SB      f_loop, UNC
"""
# the program the loaders bring in: two blocks (code in L0 at 0x8000, a data block in M1 at 0x0500); it copies the
# data word to 0x0101, writes 0xB007 to 0x0100 and loops
_RAM_PROG = """
        .sect "code", 0x008000
start:  MOVW    DP, #DP(0x0100)
        MOVL    XAR1, #0x0500
        MOV     AL, *XAR1
        MOV     @0x0101, AL
        MOV     @0x0100, #0xB007
r_loop: SB      r_loop, UNC
        .sect "data", 0x000500
        .word   0x1234
"""
ENTRY = 0x8000
SCI_MODE = {"GPIO34": "L"}                                   # 1 1 0
SPI_MODE = {"GPIO29": "L"}                                   # 1 0 1
I2C_MODE = {"GPIO29": "L", "GPIO34": "L"}                    # 1 0 0
CAN_MODE = {"GPIO18": "L"}                                   # 0 1 1
PAR_MODE = {"GPIO18": "L", "GPIO29": "L", "GPIO34": "L"}     # 0 0 0


def _asm(text: str, path: Path) -> Path:
    from chipem.core.c28x.asm import assemble
    assemble(text, filename=str(path)).write_c28img(path)
    return path


@pytest.fixture
def prog(tmp_path):
    return _asm(_RAM_PROG, tmp_path / "ram_prog.c28img")


def _session(tmp_path, pins: dict):
    from chipem.session import Session
    s = Session("tms320f2808", _asm(_FLASH_PROBE, tmp_path / "flash_probe.c28img"), quiet=True)
    for p, lvl in pins.items():
        s.chip.pins.set_external(p, lvl)
    s.reset()
    return s


def _word(chip, addr: int) -> int:
    return chip.bus.read(addr, 1)


def _ran(s) -> bool:
    return _word(s.chip, 0x0100) == 0xB007 and _word(s.chip, 0x0101) == 0x1234


def test_stream_format_matches_spru722_example():
    """SPRU722C Example 1 / 2: the 16-bit and 8-bit streams of two blocks."""
    segs = [(0x3F9010, bytes.fromhex("0100020003000400" "0500")), (0x3F8000, bytes.fromhex("00772576"))]
    w = bs.stream_words(segs, 0x3F8000, key=bs.KEY16)
    assert w == [0x10AA, 0, 0, 0, 0, 0, 0, 0, 0, 0x003F, 0x8000, 5, 0x003F, 0x9010, 1, 2, 3, 4, 5,
                 2, 0x003F, 0x8000, 0x7700, 0x7625, 0]
    b = bs.stream_bytes(bs.stream_words(segs, 0x3F8000))
    assert b[:2] == b"\xAA\x08" and b[18:22] == bytes([0x3F, 0, 0, 0x80]) and b[-2:] == b"\0\0"
    assert bs.parse_stream(w) == (0x3F8000, [(0x3F9010, [1, 2, 3, 4, 5]), (0x3F8000, [0x7700, 0x7625])])


# ------------------------------------------------------------------ SCI-A
def test_sci_boot_autobaud_echo_and_run(tmp_path, prog):
    s = _session(tmp_path, SCI_MODE)
    s.run_for(100e3)                                          # ROM waits for the autobaud character
    sci = s.chip.peripherals["SCIA"]
    assert sci.get("SCIFFCT") & (1 << 13)                     # CDC set
    assert not s.chip.peripherals["SYSCTRL"].wd_enabled()
    data = b"A" + bs.stream_bytes(bs.from_image(prog))
    sci.feed(data)
    s.run_for(len(data) * 70e3 + 200e3)                       # 64 us per character at BRR = 1 (156 kbaud)
    assert _ran(s)
    assert s.uart_output("SCIA") == data                      # every byte echoed, the 'A' included
    assert s.chip.pins.get("GPIO28").owner == "SCIRXDA" and s.chip.pins.get("GPIO29").owner == "SCITXDA"
    st = s.state()
    assert 0x8000 <= st["pc"] < 0x8010


def test_sci_autobaud_locks_on_a_9600_baud_host(tmp_path, prog):
    """Frames from a 9600-baud sender (as over a board link): 'x' is ignored while CDC = 1, 'a' locks BRR."""
    from chipem.periph.uartwire import Frame
    s = _session(tmp_path, SCI_MODE)
    s.run_for(100e3)
    sci, chip = s.chip.peripherals["SCIA"], s.chip
    data = b"xa" + bs.stream_bytes(bs.from_image(prog))
    t_char = 10 / 9600 * 1e9 * 1.05          # the host waits for each echo (the locked rate is 1.4 % slower)
    t0 = chip.now_ns()
    for i, b in enumerate(data):
        chip.sched.at_ns(t0 + (i + 1) * t_char, sci.receive_frame, Frame(b, 8, "N", 1.0, 9600.0))
    s.run_for(len(data) * t_char + 300e3)
    assert sci.brr() == 32 and abs(sci.baud() - 9600) / 9600 < 0.02    # 2.5 MHz / (8 x 33) = 9470 baud
    assert _ran(s)
    assert s.uart_output("SCIA") == data[1:]
    drops = [e for e in s.events(kinds={"uart"}) if e.get("dir") == "drop"]
    assert [(d.get("byte"), d.get("reason")) for d in drops] == [(ord("x"), "autobaud")]


def test_sci_bad_key_enters_flash(tmp_path):
    s = _session(tmp_path, SCI_MODE)
    s.run_for(100e3)
    s.chip.peripherals["SCIA"].feed(b"A\xAA\x10")             # 0x10AA: the SCI loader takes 8-bit streams only
    s.run_for(600e3)
    assert _word(s.chip, 0x0100) == 0x00F1


# ------------------------------------------------------------------ SPI-A serial EEPROM
def test_spi_eeprom_boot(tmp_path, prog):
    from chipem.devices.spi_flash import SpiNorFlash
    s = _session(tmp_path, SPI_MODE)
    ee = SpiNorFlash(0x000000, 32768, addr_bytes=2, name="X25256")       # 32K x 8 serial EEPROM
    stream = bs.stream_bytes(bs.from_image(prog, reserved=[(0x01 << 8) | 0x02]))   # SPIBRR 1, LOSPCP 2 after the key
    ee.mem[:len(stream)] = stream
    ee.attach(s.chip, "SPIA", "GPIO19")
    s.run_for(4e6)                                            # 0x7F: 19.5 kHz for 3 + 2 bytes, then 625 kHz
    assert _ran(s)
    assert s.chip.peripherals["SPIA"].get("SPIBRR") == 1
    assert s.chip.pins.get("GPIO19").level() == "H"           # chip select released after the load
    assert s.chip.pins.get("GPIO18").owner == "SPICLKA"


# ------------------------------------------------------------------ I2C-A EEPROM at 0x50
def _i2c_board(tmp_path, stream: bytes, address: int = 0x50):
    from chipem.session import Session
    probe = _asm(_FLASH_PROBE, tmp_path / "flash_probe.c28img")
    (tmp_path / "prog.boot").write_bytes(stream)
    yml = tmp_path / "i2c_boot.yaml"
    yml.write_text(f"""
name: f2808_i2c_boot
quantum: 1us
chips:
  - {{id: A, chip: tms320f2808, firmware: {probe}}}
nets:
  - {{name: SDA, pins: [A.GPIO32], open_drain: true}}
  - {{name: SCL, pins: [A.GPIO33], open_drain: true}}
parts:
  - {{id: EE1, type: i2c_eeprom, address: {address:#x}, size: 32768, ports: {{sda: SDA, scl: SCL}}, file: prog.boot}}
""")
    s = Session.from_board(yml, quiet=True)
    for p, lvl in I2C_MODE.items():
        s.chip.pins.set_external(p, lvl)
    s.reset()
    return s


def test_i2c_eeprom_boot(tmp_path, prog):
    stream = bs.stream_bytes(bs.from_image(prog, reserved=[0, 7, 8]))     # 400 kHz after the header: PSC 0, H 7, L 8
    s = _i2c_board(tmp_path, stream)
    s.run_for(12e6)
    assert _ran(s)
    i2c = s.chip.peripherals["I2CA"]
    assert i2c.get("I2CCLKH") == 7 and i2c.get("I2CCLKL") == 8


def test_i2c_no_eeprom_enters_flash(tmp_path):
    s = _i2c_board(tmp_path, b"\xAA\x08", address=0x51)            # nothing answers at 0x50: NACK -> flash
    s.run_for(2e6)
    assert _word(s.chip, 0x0100) == 0x00F1


# ------------------------------------------------------------------ eCAN-A
def test_ecan_boot(tmp_path, prog):
    from chipem.canbus import CanFrame
    s = _session(tmp_path, CAN_MODE)
    s.run_for(200e3)
    bus = s.chip.peripherals["ECANA"].can_bus
    host = bus.add_host("pc")
    words = bs.from_image(prog)
    t0 = s.chip.now_ns()
    for i, w in enumerate(words):                             # one word per frame, LSB first; 500 kbit/s
        host.send(CanFrame(0x001, w.to_bytes(2, "little")), t0 + i * 150e3, bit_ns=2000)
    s.run_for(len(words) * 150e3 + 300e3)
    assert _ran(s)
    assert len(host.sent) == len(words) and not bus.error_frames                   # every frame acknowledged
    assert s.chip.pins.get("GPIO30").owner == "CANRXA" and s.chip.pins.get("GPIO31").owner == "CANTXA"


# ------------------------------------------------------------------ parallel GPIO0-15
class _ParallelHost:
    """Host side of the SPRU722C Figure 16 handshake: waits for GPIO26 low, puts the word on GPIO0-15, pulls GPIO27
    low; when GPIO26 goes high it releases GPIO27 (acknowledge)."""

    def __init__(self, chip, words, bits16: bool, delay_ns: float = 2e3):
        self.chip, self.delay = chip, delay_ns
        self.items = list(words) if bits16 else [b for w in words for b in (w & 0xFF, w >> 8)]
        self.i = 0
        self.prev = None
        pb = chip.pins
        pb.set_external("GPIO27", "H")
        pb.subscribe(self._ev)

    def _ev(self, ev):
        if ev.pin != "GPIO26" or ev.field != "level":
            return
        lvl = ev.new
        if lvl == self.prev:
            return
        self.prev = lvl
        self.chip.sched.in_ns(self.delay, self._ready if lvl == "L" else self._ack)

    def _ready(self):
        if self.i >= len(self.items):
            return
        v = self.items[self.i]
        self.i += 1
        for n in range(16):
            self.chip.pins.set_external(f"GPIO{n}", "H" if v >> n & 1 else "L")
        self.chip.pins.set_external("GPIO27", "L")

    def _ack(self):
        self.chip.pins.set_external("GPIO27", "H")


@pytest.mark.parametrize("bits16", [True, False])
def test_parallel_boot(tmp_path, prog, bits16):
    s = _session(tmp_path, PAR_MODE)
    words = bs.from_image(prog, key=bs.KEY16 if bits16 else bs.KEY8)
    host = _ParallelHost(s.chip, words, bits16)
    if s.chip.pins.get("GPIO26").level() == "L":              # the ROM is already waiting
        host.prev = "L"
        host._ready()
    s.run_for(3e6)
    assert host.i == len(host.items)
    assert _ran(s)


# ------------------------------------------------------------------ TI's hex2000 -boot as the reference format
def test_stream_equals_hex2000_boot_table():
    """hex2000 -boot -sci8 of a TI-built image gives the same key, entry point and memory contents (hex2000 orders the
    blocks by section, we by address).  Skipped without the TI tools / the built sample."""
    import re
    import shutil
    import subprocess
    tool = next(iter(sorted((ROOT / "toolchains").glob("**/bin/hex2000"))), None) if (ROOT / "toolchains").exists() \
        else None
    img = ROOT / "firmware" / "samples_c2000_c" / "build" / "f2808_blinky.out"
    if tool is None or not img.exists() or shutil.which(str(tool)) is None:
        pytest.skip("hex2000 or f2808_blinky.out not available")
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "b.hex"
        subprocess.run([str(tool), "-boot", "-sci8", "-a", "-o", str(out), str(img)], check=True, cwd=d,
                       capture_output=True)
        txt = out.read_text(errors="replace")
    ref = bytes(int(x, 16) for x in re.findall(r"\b[0-9A-F]{2}\b", txt.split("\x03")[0]))
    ours = bs.stream_bytes(bs.from_image(img))

    def words(b):
        return [int.from_bytes(b[i:i + 2], "little") for i in range(0, len(b) - 1, 2)]
    e1, b1 = bs.parse_stream(words(ref))
    e2, b2 = bs.parse_stream(words(ours))
    assert len(ref) == len(ours) and ref[:18] == ours[:18] and e1 == e2
    assert {a + i: v for a, d in b1 for i, v in enumerate(d)} == {a + i: v for a, d in b2 for i, v in enumerate(d)}
