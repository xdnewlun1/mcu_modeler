"""C2833x XINTF (chipem/periph/ti_c2000/xintf.py, SPRUI07 ch. 14) on the TMS320F28232: registers, EALLOW, XHARDRESET,
the XINTFENCLK gate, external SRAM (chipem/devices/sram.py) on zones 0/6/7 through the GPIO mux, 16/32-bit buses,
lead/active/trail + XBANK timing charged to the CPU, the write buffer, DMA, and the board `sram` device."""
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.core.c28x.asm import assemble
from chipem.devices.sram import ParallelSram

ROOT = Path(__file__).resolve().parents[1]
CHIP = ROOT / "chips" / "tms320f28232.yaml"
EALLOW = 1 << 6
PCLKCR3, WDCR = 0x7020, 0x7029
XTIMING0, XTIMING6, XTIMING7, XINTCNF2, XBANK, XREVISION, XRESET = (0x0B20, 0x0B2C, 0x0B2E, 0x0B34, 0x0B38,
                                                                      0x0B3A, 0x0B3D)
Z0, Z6, Z7 = 0x4000, 0x100000, 0x200000


def make(prog: str | None = None):
    ch = Chip(load_chip(CHIP))
    ch.load_firmware(assemble(prog or '''
        .sect "boot", 0x33FFF6
        LB start
        .sect "code", 0x8000
start:  SB start,UNC
''').image())
    ch.reset()
    ch.core.write_reg("st1", ch.core.read_reg("st1") | EALLOW)
    w(ch, WDCR, 0x0068)
    return ch


def w(ch, a, v, n=1):
    ch.bus.write(a, n, v)


def r(ch, a, n=1):
    return ch.bus.read(a, n)


def warns(ch):
    return [e.get("msg") for e in ch.log if e.kind == "warn"]


def gpio(ch, name, v):
    g = ch.peripherals["GPIO"]
    reg = g.regs[name]
    ch.bus.write(g.base + reg.offset, reg.width, v)


def mux_xintf(ch, bus32=False):
    """TI's InitXintf16Gpio / InitXintf32Gpio: XA, XD, XZCS0/6/7, XWE0, XR/W to their XINTF function (mux 3)."""
    gpio(ch, "GPAMUX2", 0xFF000000)            # GPIO28-31: XZCS6, XA19, XA18, XA17
    gpio(ch, "GPBMUX1", 0xFFFFFFC0)            # GPIO35-47: XR/W, XZCS0, XZCS7, XWE0, XA16, XA0/XWE1, XA1-7
    gpio(ch, "GPCMUX1", 0xFFFFFFFF)            # GPIO64-79: XD15-0
    gpio(ch, "GPCMUX2", 0x0000FFFF)            # GPIO80-87: XA8-15
    if bus32:
        gpio(ch, "GPBMUX2", 0xFFFFFFFF)        # GPIO48-63: XD31-16


def ready(ch, *, bus32=False, clock=True):
    if clock:
        w(ch, PCLKCR3, r(ch, PCLKCR3) | 1 << 12)
    mux_xintf(ch, bus32)


# ---------------------------------------------------------------- registers
def test_reset_values_and_eallow():
    ch = make()
    assert r(ch, XTIMING0, 2) == r(ch, XTIMING6, 2) == r(ch, XTIMING7, 2) == 0x0043FFFF
    assert r(ch, XINTCNF2, 2) == 0x00010C04            # XTIMCLK = /2, CLKMODE = 1, HOLDS/HOLDAS inactive
    assert r(ch, XBANK) == 0x003F and r(ch, XREVISION) == 0 and r(ch, XRESET) == 0
    assert r(ch, PCLKCR3) >> 12 & 1 == 0              # XINTF clock off after reset (SPRUI07 Figure 1-16)
    x = ch.peripherals["XINTF"]
    assert x.access_cycles(6) == 54 and x.access_cycles(6, write=True) == 54   # 2 x (6 + 15 + 6)
    assert x.xclkout_hz() == pytest.approx(ch.clock.hz / 4)
    ch.core.write_reg("st1", ch.core.read_reg("st1") & ~EALLOW)
    w(ch, XBANK, 0x0000)
    assert r(ch, XBANK) == 0x003F and any("EALLOW" in m for m in warns(ch))
    ch.core.write_reg("st1", ch.core.read_reg("st1") | EALLOW)
    w(ch, XTIMING6, 0xFFFFFFFF, 2)
    assert r(ch, XTIMING6, 2) == 0x0043FFFF            # reserved bits read 0
    w(ch, XINTCNF2, 0x0000FFFF, 2)                     # WLEVEL/HOLDS/HOLDAS read-only, XTIMCLK = 0
    assert r(ch, XINTCNF2, 2) == 0x00000C0F | 0x200
    assert x.access_cycles(7) == 27 and x.xclkout_hz() == 0     # XTIMCLK = SYSCLKOUT; CLKOFF = 1
    w(ch, 0x083D, 1)                                   # XHARDRESET through SPRUI07's address (alias)
    assert r(ch, XTIMING6, 2) == 0x0043FFFF and r(ch, XINTCNF2, 2) == 0x00010C04 and r(ch, XRESET) == 0


def test_reserved_xsize_and_xtimclk_warn():
    ch = make()
    w(ch, XTIMING7, 0x00420000 | 0x3FFF, 2)
    w(ch, XINTCNF2, 0x00030004, 2)
    m = warns(ch)
    assert any("XSIZE = 2 is reserved" in s for s in m) and any("XTIMCLK = 3 is reserved" in s for s in m)


# ---------------------------------------------------------------- data path
def test_clock_off_access_not_performed():
    ch = make()
    dev = ParallelSram(0x1000, 16)
    ch.peripherals["XINTF6"].attach_memory(dev)
    ready(ch, clock=False)
    w(ch, Z6 + 5, 0x1234)
    assert r(ch, Z6 + 5) == 0 and dev.writes == 0
    assert any("XINTFENCLK = 0" in m for m in warns(ch))


def test_no_device_reads_pullups():
    ch = make()
    ready(ch)
    assert r(ch, Z7 + 3) == 0xFFFF                     # XD pins with their reset pull-ups, nothing drives the bus
    w(ch, Z7 + 3, 0)
    assert r(ch, Z7 + 3) == 0xFFFF
    gpio(ch, "GPCPUD", 0x0000000F)                     # XD15..12 (GPIO64-67) pull-ups off
    assert r(ch, Z7 + 3) == 0x0FFF
    assert any("no external device on XINTF zone 7" in m for m in warns(ch))


def test_sram_16bit_zones_and_aliasing():
    ch = make()
    ready(ch)
    s6, s0 = ParallelSram("64K", 16), ParallelSram(0x1000, 16)
    ch.peripherals["XINTF6"].attach_memory(s6)
    ch.peripherals["XINTF0"].attach_memory(s0)
    w(ch, Z6 + 0x1234, 0xBEEF)
    w(ch, Z6 + 0x10, 0x89ABCDEF, 2)                    # 32-bit access on a 16-bit bus: two bus cycles, low word first
    assert r(ch, Z6 + 0x1234) == 0xBEEF and r(ch, Z6 + 0x10, 2) == 0x89ABCDEF
    assert s6.mem[0x10] == 0xCDEF and s6.mem[0x11] == 0x89AB
    assert r(ch, Z6 + 0x10000 + 0x1234) == 0xBEEF      # 64K device: XA16+ not decoded, aliases
    w(ch, Z0 + 0xFFF, 0x0042)
    assert s0.mem[0xFFF] == 0x42 and r(ch, Z0 + 0xFFF) == 0x42
    assert not [m for m in warns(ch) if ("XINTF0" in m or "XINTF6" in m) and "USEREADY" not in m]


def test_wiring_follows_gpio_mux():
    ch = make()
    w(ch, PCLKCR3, r(ch, PCLKCR3) | 1 << 12)
    dev = ParallelSram(0x100, 16)
    ch.peripherals["XINTF7"].attach_memory(dev)
    w(ch, Z7 + 1, 0x5555)                              # nothing muxed: XZCS7 never asserted
    assert dev.writes == 0 and r(ch, Z7 + 1) == 0xFFFF
    assert any("XZCS7 is not muxed" in m for m in warns(ch))
    mux_xintf(ch)
    gpio(ch, "GPCMUX1", 0xFFFFFFFF & ~(3 << 30))       # GPIO79 = XD0 back to GPIO (pull-up on)
    w(ch, Z7 + 1, 0x5554)
    assert dev.mem[1] == 0x5555                        # XD0 floats high at the device
    gpio(ch, "GPBMUX1", 0xFFFFFFC0 & ~(3 << 18))       # GPIO41 = XA1 not muxed: address bit 1 reads as 0
    w(ch, Z7 + 2, 0x0777)
    assert dev.mem[0] == 0x0777
    assert any("XA1" in m for m in warns(ch))


def test_32bit_zone():
    ch = make()
    ready(ch, bus32=True)
    w(ch, XTIMING6, (0x0043FFFF & ~(3 << 16)) | 1 << 16, 2)     # XSIZE = 1: 32-bit bus
    dev = ParallelSram(0x400, 32)
    ch.peripherals["XINTF6"].attach_memory(dev)
    x = ch.peripherals["XINTF"]
    w(ch, Z6 + 8, 0x11112222, 2)
    assert dev.mem[4] == 0x11112222
    w(ch, Z6 + 9, 0xAAAA)                              # odd word: XWE1, high half only
    assert dev.mem[4] == 0xAAAA2222 and r(ch, Z6 + 8) == 0x2222 and r(ch, Z6 + 9) == 0xAAAA
    # a 16-bit device on the 32-bit zone only sees the even words
    d16 = ParallelSram(0x400, 16)
    ch.peripherals["XINTF6"].attach_memory(d16)
    w(ch, Z6 + 4, 0x12345678, 2)
    assert d16.mem[2] == 0x5678 and r(ch, Z6 + 5) == 0xFFFF
    assert x.zones[6] is ch.peripherals["XINTF6"]


# ---------------------------------------------------------------- timing
LOOP_RD = '''
        .sect "boot", 0x33FFF6
        LB start
        .sect "code", 0x8000
start:  MOVL XAR4,#0x100000
loop:   MOV AL,*XAR4
        SB loop,UNC
'''
LOOP_WR = LOOP_RD.replace("MOV AL,*XAR4", "MOV *XAR4,AL")


def timed(prog, xt=None, cnf=None):
    ch = make(prog)
    ready(ch)
    ch.peripherals["XINTF6"].attach_memory(ParallelSram("4K", 16))
    if xt is not None:
        w(ch, XTIMING6, xt, 2)
    if cnf is not None:
        w(ch, XINTCNF2, cnf, 2)
    return ch, ch.peripherals["XINTF"]


def test_read_wait_states_charged():
    ch, x = timed(LOOP_RD)
    c0, i0 = ch.clock.cycles, ch.insns
    ch.run_cycles(20000)
    n = x.bus_cycles
    assert n > 100
    assert x.stall_cycles == n * 53                    # default timing: 54 SYSCLKOUT per read, 1 is the instruction
    assert n == pytest.approx((ch.clock.cycles - c0) / 55, abs=2)   # MOV (1 + 53 stall) + SB (1) per read
    # lead 1, active 2, trail 0, X2TIMING = 0, USEREADY = 0, XTIMCLK = SYSCLKOUT -> 1 + 3 + 0 = 4 cycles
    ch, x = timed(LOOP_RD, xt=0x00030000 | 1 << 12 | 2 << 9 | 1 << 5 | 2 << 2 | 1, cnf=0x00000004)
    assert x.access_cycles(6) == 4 and x.access_cycles(6, True) == 5     # write trail 1
    ch.run_cycles(3000)
    assert x.stall_cycles == x.bus_cycles * 3 and not any("USEREADY" in m for m in warns(ch))


def test_write_buffer():
    xt = 0x00030000 | 3 << 12 | 7 << 9 | 3 << 7 | 3 << 5 | 7 << 2 | 3     # X2 = 0: 3 + 8 + 3 = 14 per write
    ch, x = timed(LOOP_WR, xt=xt, cnf=0x00000004)                         # WRBUFF = 0
    ch.run_cycles(3000)
    assert x.access_cycles(6, True) == 14 and x.stall_cycles == x.bus_cycles * 13
    ch, x = timed(LOOP_WR, xt=xt, cnf=0x00000007)                         # WRBUFF = 3
    for _ in range(40):
        ch.run_cycles(1)
        if x.bus_cycles >= 3:
            break
    assert x.bus_cycles == 3 and x.stall_cycles == 0                      # three writes absorbed by the buffer
    assert r(ch, XINTCNF2, 2) >> 6 & 3 >= 2                               # WLEVEL: writes still pending
    ch.run_cycles(5000)
    n = x.bus_cycles
    # steady state: one write per 14 cycles; the loop itself takes 2 cycles per write
    assert (ch.clock.cycles - 0) / n == pytest.approx(14, rel=0.1)


def test_xbank_cycles():
    prog = '''
        .sect "boot", 0x33FFF6
        LB start
        .sect "code", 0x8000
start:  MOVL XAR4,#0x100000
        MOVL XAR5,#0x200000
loop:   MOV AL,*XAR4
        MOV AL,*XAR5
        SB loop,UNC
'''
    ch, x = timed(prog, cnf=0x00000004)
    ch.peripherals["XINTF7"].attach_memory(ParallelSram("4K", 16))
    fast = 0x00030000 | 1 << 12 | 1 << 9 | 1 << 5 | 1 << 2 | 1                  # 1 + 2 + 0 = 3 cycles
    w(ch, XTIMING6, fast, 2)
    w(ch, XTIMING7, fast, 2)
    w(ch, XBANK, 2 << 3 | 7)                                                     # 2 XTIMCLK cycles in/out of zone 7
    ch.run_cycles(4000)
    n = x.bus_cycles
    assert x.stall_cycles == pytest.approx(n * (3 - 1 + 2), abs=4)               # every access switches zones


def test_dma_to_xintf():
    ch = make()
    ready(ch)
    s0, s7 = ParallelSram(0x100, 16), ParallelSram(0x100, 16)
    ch.peripherals["XINTF0"].attach_memory(s0)
    ch.peripherals["XINTF7"].attach_memory(s7)
    w(ch, PCLKCR3, r(ch, PCLKCR3) | 1 << 11)          # DMAENCLK
    s0.mem[0] = 0xABCD
    ch5 = 0x1000 + 0x20 * 5
    for off, v in ((0, 1 << 8), (2, 0), (6, 0), (0xA, 0xFFFF), (0xD, 0xFFFF)):
        w(ch, ch5 + off, v)
    for off, v in ((0x10, Z0), (0x12, Z0), (0x18, Z7 + 0x10), (0x1A, Z7 + 0x10)):
        w(ch, ch5 + off, v, 2)
    w(ch, ch5 + 1, 1)                                  # RUN
    w(ch, ch5 + 1, 8)                                  # PERINTFRC
    ch.run_cycles(20)
    assert s7.mem[0x10] == 0xABCD


def test_board_sram_device(tmp_path):
    from chipem.session import Session
    img = tmp_path / "sram.bin"
    img.write_bytes(bytes([0x34, 0x12, 0x78, 0x56]))
    fw = ROOT / "firmware" / "samples_c2000" / "build" / "blinky.c28img"
    if not fw.exists():
        pytest.skip("C2000 samples not built")
    b = tmp_path / "b.yaml"
    b.write_text(f'''
name: t
chips:
  - {{id: A, chip: {CHIP}, firmware: {fw}}}
devices:
  - {{type: sram, chip: A, bus: XINTF6, size: 64K, width: 16, file: {img.name}}}
''')
    s = Session.from_board(b)
    try:
        dev = s.ctxs["A"].devices[0]
        assert isinstance(dev, ParallelSram) and dev.mem[0] == 0x1234 and dev.mem[1] == 0x5678
        assert s.ctxs["A"].chip.peripherals["XINTF6"].device is dev
    finally:
        s.close()


def test_sample_board_xintf_sram():
    from chipem.session import Session
    fw = ROOT / "firmware" / "samples_c2000" / "build" / "xintf_sram.c28img"
    if not fw.exists():
        pytest.skip("C2000 samples not built (firmware/samples_c2000/build.sh)")
    s = Session.from_board(ROOT / "boards" / "c2000_xintf_sram.yaml")
    try:
        s.run_for(8e6)
        ch = s.ctxs["A"].chip
        dev = s.ctxs["A"].devices[0]
        assert ch.bus.read(0x0500, 1) == 0 and ch.bus.read(0x0501, 1) == 0xD00E
        assert dev.mem[0] == 0x1234 and dev.mem[255] == (0x1234 + 3 * 255) & 0xFFFF and dev.writes == 256
        assert ch.pins.get("GPIO34").level() == "H"
        assert not [e for e in ch.log if e.kind == "warn"]
        ev = [e for e in ch.log if e.kind == "pin" and e.get("pin") == "GPIO34" and e.get("field") == "level"]
        dt_us = (ev[-1].t_ns - ev[-2].t_ns) / 1e3              # GPIO34 low (setup) -> high (test passed)
        # 256 writes bus-limited at 5 cycles (write buffer) + 256 reads at 5 + 3 loop cycles, 100 MHz
        assert 30 < dt_us < 38
    finally:
        s.close()
