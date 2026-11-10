"""TI C2000 McBSP (ti_c2000.mcbsp, SPRUI07 ch. 12) on the TMS320F28232 description: register reset values, digital
loopback with every word length and RJUST mode, the RBR/RSR receive pipeline (RFULL), underflow, frame syncs from
the sample rate generator (FPER/FWID) and per DXR copy (FSGM = 0), exact pin timing of MCLKX/MFSX/MDX, interrupts
(MFFINT, RINTM/XINTM 0/2/3), unexpected frame syncs (RSYNCERR/XSYNCERR, RFIG), two chips exchanging words on board
nets, clock-stop (SPI) master against the SPI NOR flash and against a McBSP SPI slave in the four clock schemes,
DMA MXEVTA/MREVTA triggers (real C28x core), companding, the clock gate and the live pin roles."""
from pathlib import Path

import pytest

from chipem.board import Board
from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.core.c28x.asm import assemble
from chipem.devices.spi_flash import SpiNorFlash
from chipem.periph.ti_c2000 import mcbsp as M

from c28x_fake import make_fake_chip

CHIP = Path(__file__).resolve().parent.parent / "chips" / "tms320f28232.yaml"
EALLOW = 1 << 6
B = 0x5000
DRR2, DRR1, DXR2, DXR1, SPCR2, SPCR1, RCR2, RCR1, XCR2, XCR1, SRGR2, SRGR1, MCR2, MCR1 = range(14)
PCR, MFFINT = 0x12, 0x23
PCLKCR0 = 0x701C
# SPCR1 / SPCR2
RRST, RRDY, RFULL, RSYNCERR, DLB = 1, 2, 4, 8, 0x8000
XRST, XRDY, XEMPTY, XSYNCERR, GRST, FRST = 1, 2, 4, 8, 0x40, 0x80
# PCR
CLKRP, CLKXP, FSRP, FSXP, CLKRM, CLKXM, FSRM, FSXM = 1, 2, 4, 8, 0x100, 0x200, 0x400, 0x800
FSGM, CLKSM = 0x1000, 0x2000
PIEIFR6 = 0x0CE0 + 3 + 2 * 5
_DESC = None


def _desc():
    global _DESC
    _DESC = _DESC or load_chip(CHIP)
    return _DESC


def _chip(mux=True):
    c = make_fake_chip(_desc())
    c.reset()
    c.core.regs["st1"] |= EALLOW
    c.bus.write(0x7029, 1, 0x0068)                          # watchdog off
    c.bus.write(PCLKCR0, 1, c.bus.read(PCLKCR0, 1) | 1 << 12)   # MCBSPAENCLK
    if mux:
        c.bus.write(0x6F80 + 0x08, 2, 0xAA00)               # GPAMUX2: GPIO20-23 = MDXA/MDRA/MCLKXA/MFSXA (MUX2)
        c.bus.write(0x6F80 + 0x06, 2, 0x8800)               # GPAMUX1: GPIO5 = MFSRA, GPIO7 = MCLKRA (MUX2)
    return c


def w(c, o, v):
    c.bus.write(B + o, 1, v)


def r(c, o):
    return c.bus.read(B + o, 1)


def wdlen(bits):
    return {8: 0, 12: 1, 16: 2, 20: 3, 24: 4, 32: 5}[bits] << 5


def config(c, *, bits=16, delay=1, pcr=CLKXM | CLKRM | FSXM | FSRM, srgr2=CLKSM, clkgdv=1, fwid=0, spcr1=0,
           xcr1=None, xcr2=None, rcr1=None, rcr2=None):
    w(c, SPCR2, 0)
    w(c, SPCR1, spcr1)
    w(c, RCR1, wdlen(bits) if rcr1 is None else rcr1)
    w(c, RCR2, delay if rcr2 is None else rcr2)
    w(c, XCR1, wdlen(bits) if xcr1 is None else xcr1)
    w(c, XCR2, delay if xcr2 is None else xcr2)
    w(c, SRGR1, fwid << 8 | clkgdv)
    w(c, SRGR2, srgr2)
    w(c, PCR, pcr)


def start(c, spcr1=0, frst=True):
    w(c, SPCR2, GRST)
    w(c, SPCR1, spcr1 | RRST)
    w(c, SPCR2, GRST | XRST | (FRST if frst else 0))


class Rig:
    def __init__(self, n=1, quantum_ns=1000, mux=True):
        self.chips = [_chip(mux) for _ in range(n)]
        self.board = Board({"AB"[i]: c for i, c in enumerate(self.chips)}, quantum_ns=quantum_ns)
        self.c = self.chips[0]

    def run(self, ns):
        self.board.run_for(ns)

    def until(self, f, limit_us=2000, step=1000):
        for _ in range(int(limit_us * 1000 / step)):
            if f():
                return
            self.board.run_for(step)
        raise AssertionError("timeout")

    def blk(self, c=None):
        return (c or self.c).peripherals["MCBSPA"]


def _edges(chip, pins):
    out = []
    chip.pins.subscribe(lambda e: out.append((e.t_ns, e.pin, e.new)) if e.field == "level" and e.pin in pins
                        else None)
    return out


def _warns(c):
    return [e.get("msg") for e in c.log if e.kind == "warn"]


# ---------------------------------------------------------------------------------------------------- registers
def test_register_reset_values_and_read_only_bits():
    c = _chip(mux=False)
    assert r(c, SRGR2) == 0x2000 and r(c, SRGR1) == 0x0001          # CLKSM = 1, CLKGDV = 1 (SPRUI07 Fig. 12-75/76)
    for o in (DRR2, DRR1, SPCR2, SPCR1, RCR1, RCR2, XCR1, XCR2, PCR, MFFINT, MCR1, MCR2):
        assert r(c, o) == 0
    w(c, SPCR1, 0xFFFF)
    assert r(c, SPCR1) == 0xFFFF & ~0x0706                          # reserved 10-8, RFULL, RRDY read-only
    w(c, SPCR2, 0xFFFF & ~(XRST | GRST))
    assert r(c, SPCR2) == 0x03F8 & ~GRST                            # 15-10, XEMPTY, XRDY read-only
    w(c, PCR, 0xFFFF)
    assert r(c, PCR) == 0x0F8F
    w(c, MFFINT, 0xFFFF)
    assert r(c, MFFINT) == 0x0005
    w(c, DRR1, 0x1234)
    assert r(c, DRR1) == 0


# ---------------------------------------------------------------------------------------------------- loopback
@pytest.mark.parametrize("bits", [8, 12, 16, 20, 24, 32])
def test_digital_loopback_word_lengths(bits):
    rig = Rig(mux=False)
    c = rig.c
    config(c, bits=bits, spcr1=DLB)
    start(c, DLB)
    mask = (1 << bits) - 1
    words = [0xA5C3F00F & mask, 0x12345678 & mask, mask]
    got = []
    for v in words:
        rig.until(lambda: r(c, SPCR2) & XRDY)
        w(c, DXR2, v >> 16)
        w(c, DXR1, v & 0xFFFF)
    for _ in words:
        rig.until(lambda: r(c, SPCR1) & RRDY)
        hi = r(c, DRR2)
        got.append((hi << 16 | r(c, DRR1)) if bits > 16 else r(c, DRR1))
    assert got == words
    assert list(rig.blk().received) == words


@pytest.mark.parametrize("rjust,expect", [(0, 0x0A5), (1, 0xFFA5), (2, 0xA500)])
def test_rjust_modes_8_bit(rjust, expect):
    rig = Rig(mux=False)
    c = rig.c
    config(c, bits=8, spcr1=DLB | rjust << 13)
    start(c, DLB | rjust << 13)
    w(c, DXR1, 0xA5)
    rig.until(lambda: r(c, SPCR1) & RRDY)
    assert r(c, DRR1) == expect


def test_rjust_sign_extension_24_bit():
    rig = Rig(mux=False)
    c = rig.c
    config(c, bits=24, spcr1=DLB | 1 << 13)
    start(c, DLB | 1 << 13)
    w(c, DXR2, 0x0080)
    w(c, DXR1, 0x1234)
    rig.until(lambda: r(c, SPCR1) & RRDY)
    assert (r(c, DRR2), r(c, DRR1)) == (0xFF80, 0x1234)


def test_receive_pipeline_rfull_and_overrun():
    """Word 1 -> DRR, 2 -> RBR, 3 -> RSR with RFULL (SPRUI07 12.5.2); word 4 overwrites RSR."""
    rig = Rig(mux=False)
    c = rig.c
    config(c, bits=8, spcr1=DLB)
    start(c, DLB)
    for v in (1, 2, 3, 4):
        rig.until(lambda: r(c, SPCR2) & XRDY)
        w(c, DXR1, v)
    rig.until(lambda: len(rig.blk().received) == 4)
    assert r(c, SPCR1) & (RRDY | RFULL) == RRDY | RFULL
    assert r(c, DRR1) == 1
    assert r(c, SPCR1) & (RRDY | RFULL) == RRDY                    # RBR -> DRR, RSR -> RBR
    assert r(c, DRR1) == 2
    assert r(c, DRR1) == 4                                          # word 3 was overwritten in RSR
    assert not r(c, SPCR1) & RRDY


def test_xrdy_double_buffer_and_underflow_resends_old_data():
    rig = Rig(mux=False)
    c = rig.c
    config(c, bits=8, spcr1=DLB, srgr2=CLKSM | FSGM | 15)          # FSG frames every 16 CLKG cycles
    w(c, SPCR2, GRST)
    w(c, SPCR1, DLB | RRST)
    w(c, SPCR2, GRST | XRST)
    assert r(c, SPCR2) & (XRDY | XEMPTY) == XRDY                    # out of reset: ready and empty
    w(c, DXR1, 0x11)                                                # copied to XSR at once: XRDY again
    assert r(c, SPCR2) & (XRDY | XEMPTY) == XRDY | XEMPTY
    w(c, DXR1, 0x22)                                                # waits in DXR
    assert not r(c, SPCR2) & XRDY
    w(c, SPCR2, GRST | XRST | FRST)
    rig.until(lambda: len(rig.blk().received) >= 4)
    assert list(rig.blk().received)[:4] == [0x11, 0x22, 0x22, 0x22]
    assert not r(c, SPCR2) & XEMPTY


def test_underflow_after_reset_sends_zeros():
    rig = Rig(mux=False)
    c = rig.c
    config(c, bits=8, spcr1=DLB, srgr2=CLKSM | FSGM | 15)
    w(c, DXR1, 0x5A)                                                # written before XRST: copied at XRST = 1
    start(c, DLB)
    rig.until(lambda: len(rig.blk().received) >= 2)
    assert list(rig.blk().received)[:2] == [0x5A, 0x5A]
    w(c, SPCR2, GRST | FRST)                                        # transmitter reset, then out again
    w(c, SPCR2, GRST | FRST | XRST)
    n = len(rig.blk().received)
    rig.until(lambda: len(rig.blk().received) >= n + 2)
    assert list(rig.blk().received)[n + 1] == 0                     # nothing written since XRST: zeros


def test_dual_phase_frame():
    rig = Rig(mux=False)
    c = rig.c
    x1 = 1 << 8 | wdlen(8)                                          # phase 1: 2 x 8 bits
    x2 = 0x8000 | wdlen(16) | 1                                     # phase 2: 1 x 16 bits, 1-bit delay
    config(c, spcr1=DLB, xcr1=x1, xcr2=x2, rcr1=x1, rcr2=x2)
    start(c, DLB)
    for v in (0xAB, 0xCD, 0xBEEF):
        rig.until(lambda: r(c, SPCR2) & XRDY)
        w(c, DXR1, v)
    got = []
    for _ in range(3):
        rig.until(lambda: r(c, SPCR1) & RRDY)
        got.append(r(c, DRR1))
    assert got == [0xAB, 0xCD, 0xBEEF]


# ---------------------------------------------------------------------------------------------------- pins / timing
def test_pin_waveforms_clkg_fsg_and_data_timing():
    """LSPCLK = 2.5 MHz after reset; CLKGDV = 1 -> CLKG 1.25 MHz (800 ns, high first); FSG every FPER + 1 = 20
    cycles, FWID + 1 = 2 cycles wide; 1-bit data delay; MSB first on MDXA, driven on the rising CLKX edge."""
    rig = Rig()
    c = rig.c
    config(c, bits=8, srgr2=CLKSM | FSGM | 19, fwid=1)
    ev = _edges(c, ("GPIO20", "GPIO22", "GPIO23"))
    w(c, DXR1, 0xA5)
    start(c)
    rig.run(40000)
    clk = [(t, v) for t, p, v in ev if p == "GPIO22"]
    rises = [t for t, v in clk if v == "H"]
    assert {b - a for a, b in zip(rises, rises[1:])} == {800.0}
    falls = [t for t, v in clk if v == "L"]
    assert falls[0] - rises[0] == 400.0
    fs = [(t, v) for t, p, v in ev if p == "GPIO23"]
    t_fs = [t for t, v in fs if v == "H"]
    assert t_fs[1] - t_fs[0] == 20 * 800.0                          # FPER + 1 CLKG cycles
    assert [t for t, v in fs if v == "L"][0] - t_fs[0] == 2 * 800.0  # FWID + 1
    assert (t_fs[0] - rises[0]) % 800.0 == 0 and (t_fs[0] - rises[0]) // 800 == 20   # first FSG after FPER + 1
    dx = [(t, v) for t, p, v in ev if p == "GPIO20"]
    bit0 = t_fs[0] + 800.0                                          # data delay 1
    lvl = {}
    cur = "L"
    k = 0
    for i in range(8):                                              # level of MDXA during each bit cell
        t = bit0 + 800.0 * i + 400.0
        while k < len(dx) and dx[k][0] <= t:
            cur = dx[k][1]
            k += 1
        lvl[i] = cur
    assert "".join("1" if lvl[i] == "H" else "0" for i in range(8)) == "10100101"
    assert all((t - rises[0]) % 800.0 == 0 for t, _ in dx)         # data changes on rising CLKX edges


def test_pin_roles_follow_pcr_and_reset():
    rig = Rig()
    c = rig.c
    role = lambda pid: c.pins.get(pid).direction
    assert role("GPIO20") == "in" and role("GPIO22") == "in"        # MDXA high impedance in reset; MCLKXA input
    config(c)
    assert (role("GPIO22"), role("GPIO23"), role("GPIO7"), role("GPIO5")) == ("out",) * 4
    start(c)
    assert role("GPIO20") == "out" and role("GPIO21") == "in"
    w(c, SPCR2, GRST)                                               # transmitter reset: MDX high impedance
    assert role("GPIO20") == "in"
    w(c, PCR, 0)
    assert (role("GPIO22"), role("GPIO23")) == ("in", "in")


def test_fs_output_inactive_while_transmitter_in_reset_with_fsxp_active_low():
    rig = Rig()
    c = rig.c
    config(c, bits=8, srgr2=CLKSM | FSGM | 9, pcr=CLKXM | FSXM | FSXP)
    ev = _edges(c, ("GPIO23",))
    w(c, SPCR2, GRST | FRST)                                        # XRST = 0
    rig.run(30000)
    assert c.pins.get("GPIO23").level() == "H" and not ev
    w(c, SPCR2, GRST | FRST | XRST)
    rig.run(30000)
    assert [v for _t, _p, v in ev][:2] == ["L", "H"]


# ---------------------------------------------------------------------------------------------------- interrupts
def test_interrupts_mffint_and_modes():
    rig = Rig(mux=False)
    c = rig.c
    pie = c.arch.pie if hasattr(c.arch, "pie") else None
    hits = []
    orig = c.arch.pulse_irq
    c.arch.pulse_irq = lambda n: (hits.append(n), orig(n))
    config(c, bits=8, spcr1=DLB)
    start(c, DLB)
    w(c, DXR1, 1)
    rig.until(lambda: r(c, SPCR1) & RRDY)
    assert hits == []                                               # MFFINT = 0: no MRINT/MXINT
    r(c, DRR1)
    w(c, MFFINT, 0x5)
    w(c, DXR1, 2)
    rig.until(lambda: r(c, SPCR1) & RRDY)
    assert 76 in hits and 77 in hits                                # MRINTA 6.5, MXINTA 6.6
    hits.clear()
    w(c, SPCR1, DLB | RRST | 2 << 4)                                # RINTM = 2: every frame sync
    r(c, DRR1)
    for v in (3, 4):
        rig.until(lambda: r(c, SPCR2) & XRDY)
        w(c, DXR1, v)
    rig.run(40000)
    assert hits.count(76) == 2
    del pie


def test_unexpected_frame_sync_sets_rsyncerr_and_xsyncerr_unless_ignored():
    """FSG every 10 CLKG cycles with 16-bit frames: each new pulse is unexpected."""
    for fig, expect in ((0, True), (4, False)):
        rig = Rig(mux=False)
        c = rig.c
        config(c, bits=16, spcr1=DLB, srgr2=CLKSM | FSGM | 9, rcr2=1 | fig, xcr2=1 | fig)
        w(c, MFFINT, 0x5)
        start(c, DLB | 3 << 4)
        w(c, SPCR2, GRST | FRST | XRST | 3 << 4)
        w(c, DXR1, 0x1234)
        rig.run(60000)
        assert bool(r(c, SPCR1) & RSYNCERR) is expect
        assert bool(r(c, SPCR2) & XSYNCERR) is expect
        w(c, SPCR1, DLB | RRST | 3 << 4)                            # write 0 clears
        assert not r(c, SPCR1) & RSYNCERR or not expect


# ---------------------------------------------------------------------------------------------------- two chips
def test_two_chips_exchange_words_over_board_nets():
    """A: internal clocks and FSG frames (master); B: everything from the pins.  A's MDX -> B's MDR, B's MDX ->
    A's MDR; MCLKXA drives B's MCLKX and MCLKR, MFSXA B's MFSX and MFSR."""
    rig = Rig(2, quantum_ns=200)
    A, Bc = rig.chips
    bd = rig.board
    bd.add_net("DAB", [("A", "GPIO20"), ("B", "GPIO21")])
    bd.add_net("DBA", [("B", "GPIO20"), ("A", "GPIO21")])
    bd.add_net("CLK", [("A", "GPIO22"), ("B", "GPIO22"), ("B", "GPIO7")])
    bd.add_net("FS", [("A", "GPIO23"), ("B", "GPIO23"), ("B", "GPIO5")])
    config(A, srgr2=CLKSM | FSGM | 31)
    config(Bc, pcr=0, srgr2=CLKSM)
    w(Bc, SPCR1, RRST)
    w(Bc, SPCR2, XRST)
    w(Bc, DXR1, 0x5A5A)
    w(A, SPCR2, GRST)
    w(A, SPCR1, RRST)
    w(A, SPCR2, GRST | XRST)
    w(A, DXR1, 0x1234)
    w(A, SPCR2, GRST | XRST | FRST)
    got_b, got_a = [], []
    sent = [0x1234, 0xBEEF, 0x0F0F]
    k = 1
    for _ in range(300):
        rig.run(1000)
        if r(Bc, SPCR1) & RRDY:
            got_b.append(r(Bc, DRR1))
            if len(got_b) == 1:
                w(Bc, DXR1, 0xC3C3)
        if r(A, SPCR1) & RRDY:
            got_a.append(r(A, DRR1))
        if r(A, SPCR2) & XRDY and k < len(sent):
            w(A, DXR1, sent[k])
            k += 1
        if len(got_b) >= 3:
            break
    assert got_b[:3] == sent
    assert got_a[0] == 0x5A5A and 0xC3C3 in got_a


# ---------------------------------------------------------------------------------------------------- clock stop / SPI
def _spi_master(c, stp, cxp, bits=32, clkgdv=3):
    config(c, bits=bits, delay=1, spcr1=stp << 11, pcr=CLKXM | FSXM | FSXP | cxp * CLKXP | (cxp ^ (stp == 3)),
           srgr2=CLKSM, clkgdv=clkgdv)
    w(c, SPCR2, GRST)
    w(c, SPCR1, stp << 11 | RRST)
    w(c, SPCR2, GRST | XRST | FRST)                                 # FRST: frame syncs from the DXR copies


@pytest.mark.parametrize("stp", [2, 3])
def test_spi_master_reads_flash_jedec_id(stp):
    rig = Rig()
    c = rig.c
    fl = SpiNorFlash(0x202015).attach(c, "MCBSPA", cs="GPIO23")
    ev = _edges(c, ("GPIO20", "GPIO22", "GPIO23"))
    _spi_master(c, stp, 0)
    rig.run(1000)
    w(c, DXR2, 0x9F00)
    w(c, DXR1, 0x0000)
    rig.until(lambda: r(c, SPCR1) & RRDY)
    assert (r(c, DRR2), r(c, DRR1)) == (0xFF20, 0x2015)
    rig.run(2000)
    assert fl.selected is False
    fs = [(t, v) for t, p, v in ev if p == "GPIO23" and t > 0]
    clk = [(t, v) for t, p, v in ev if p == "GPIO22" and t > 0]
    assert [v for _t, v in fs] == ["L", "H"]                        # MFSX active (low) for the whole packet
    assert len(clk) == 64 and clk[0][1] == "H" and clk[-1][1] == "L"   # 32 pulses, idle low
    assert fs[0][0] < clk[0][0] and clk[-1][0] <= fs[1][0]
    dx = [t for t, p, _v in ev if p == "GPIO20" and t > 0]
    period = 4 * 4 * 100.0                                          # (CLKGDV + 1) LSPCLK cycles of 400 ns
    first_lead = clk[0][0]
    if stp == 2:                                                    # data changes on the leading edge
        assert dx[0] == first_lead
    else:                                                           # half a cycle ahead of the leading edge
        assert first_lead - dx[0] == period / 2


@pytest.mark.parametrize("stp,cxp", [(2, 0), (2, 1), (3, 0), (3, 1)])
def test_spi_master_and_slave_between_two_chips(stp, cxp):
    rig = Rig(2, quantum_ns=100)
    A, Bc = rig.chips
    bd = rig.board
    bd.add_net("SIMO", [("A", "GPIO20"), ("B", "GPIO21")])
    bd.add_net("SOMI", [("B", "GPIO20"), ("A", "GPIO21")])
    bd.add_net("CLK", [("A", "GPIO22"), ("B", "GPIO22")])
    bd.add_net("STE", [("A", "GPIO23"), ("B", "GPIO23")], pull="up")
    _spi_master(A, stp, cxp, bits=16)
    config(Bc, bits=16, delay=0, spcr1=stp << 11, pcr=FSXP | cxp * CLKXP | (cxp ^ (stp == 3)), clkgdv=1)
    w(Bc, SPCR2, GRST)
    w(Bc, SPCR1, stp << 11 | RRST)
    w(Bc, SPCR2, GRST | XRST)
    w(Bc, DXR1, 0xBEEF)
    w(A, DXR1, 0x1234)
    rig.until(lambda: r(A, SPCR1) & RRDY and r(Bc, SPCR1) & RRDY)
    assert r(A, DRR1) == 0xBEEF and r(Bc, DRR1) == 0x1234
    w(Bc, DXR1, 0x0FF0)
    w(A, DXR1, 0xA55A)
    rig.until(lambda: r(A, SPCR1) & RRDY and r(Bc, SPCR1) & RRDY)
    assert r(A, DRR1) == 0x0FF0 and r(Bc, DRR1) == 0xA55A
    assert A.pins.get("GPIO22").level() == ("H" if cxp else "L")    # clock stopped at its idle level


# ---------------------------------------------------------------------------------------------------- DMA
def test_dma_mxevta_mreveta_move_a_buffer_through_loopback():
    ch = Chip(_desc())
    ch.load_firmware(assemble('''
        .sect "boot", 0x33FFF6
        LB start
        .sect "code", 0x8000
start:  SB start,UNC
''').image())
    ch.reset()
    ch.core.write_reg("st1", ch.core.read_reg("st1") | EALLOW)
    W = lambda a, v, n=1: ch.bus.write(a, n, v)
    W(0x7029, 0x0068)
    W(PCLKCR0, ch.bus.read(PCLKCR0, 1) | 1 << 12)
    W(0x7020, 0x0800)                                               # DMAENCLK
    src, dst = 0xC000, 0xD000
    data = [0x1111 * (i + 1) for i in range(8)]
    for i, v in enumerate(data):
        W(src + i, v)

    def chan(n, sel, s, d, sstep, dstep):
        b = 0x1000 + 0x20 * n
        W(b + 0, sel | 1 << 8)                                      # PERINTSEL, PERINTE
        W(b + 2, 0)                                                 # 1 word per burst
        W(b + 6, len(data) - 1)
        W(b + 8, sstep)
        W(b + 9, dstep)
        W(b + 0xA, 0xFFFF)
        W(b + 0xD, 0xFFFF)
        W(b + 0x10, s, 2)
        W(b + 0x12, s, 2)
        W(b + 0x18, d, 2)
        W(b + 0x1A, d, 2)
        W(b + 1, 0x10)                                              # PERINTCLR
        W(b + 1, 1)                                                 # RUN

    chan(1, 14, src, B + DXR1, 1, 0)                                # MXEVTA: memory -> DXR1
    chan(2, 15, B + DRR1, dst, 0, 1)                                # MREVTA: DRR1 -> memory
    m = ch.peripherals["MCBSPA"]
    w(ch, SPCR2, 0)
    w(ch, SPCR1, DLB)
    w(ch, RCR1, wdlen(16))
    w(ch, XCR1, wdlen(16))
    w(ch, RCR2, 1)
    w(ch, XCR2, 1)
    w(ch, SRGR1, 1)
    w(ch, SRGR2, CLKSM)
    w(ch, PCR, CLKXM | CLKRM | FSXM | FSRM)
    w(ch, SPCR2, GRST)
    w(ch, SPCR1, DLB | RRST)
    w(ch, SPCR2, GRST | XRST | FRST)                                # XRDY 0 -> 1: the first MXEVTA
    ch.run_for(500_000)
    assert [ch.bus.read(dst + i, 1) for i in range(8)] == data
    assert list(m.sent) == data
    assert not any("never triggers" in (x or "") for x in _warns(ch))


def test_dma_mcbsp_b_event_warns_on_f28232():
    ch = Chip(_desc())
    ch.reset()
    ch.core.write_reg("st1", ch.core.read_reg("st1") | EALLOW)
    ch.bus.write(0x7020, 1, 0x0800)
    ch.bus.write(0x1020, 1, 16 | 1 << 8)                            # PERINTSEL 16 = MXEVTB
    ch.bus.write(0x1021, 1, 1)
    assert any("no McBSP-B" in (x or "") for x in _warns(ch))


# ---------------------------------------------------------------------------------------------------- misc
def test_companding_g711_round_trip_and_reference_codes():
    assert M.ulaw_compress(0) == 0xFF and M.alaw_compress(0) == 0xD5
    for code in range(256):
        if code != 0x7F:                                            # mu-law has two zeros (0x7F and 0xFF)
            assert M.ulaw_compress(M.ulaw_expand(code)) == code
        assert M.alaw_compress(M.alaw_expand(code)) == code
    assert M.ulaw_expand(0x80) == 32124 and M.ulaw_expand(0x00) == (-32124) & 0xFFFF   # 8031 x 4: 14-bit
    assert M.alaw_expand(0xAA) == 32256                             # A-law 13-bit full scale x 8


def test_mu_law_loopback():
    rig = Rig(mux=False)
    c = rig.c
    config(c, bits=8, spcr1=DLB, xcr2=1 | 2 << 3, rcr2=1 | 2 << 3)
    start(c, DLB)
    w(c, DXR1, 0x4000)
    rig.until(lambda: r(c, SPCR1) & RRDY)
    assert rig.blk().sent[0] == M.ulaw_compress(0x4000)
    assert r(c, DRR1) == M.ulaw_expand(M.ulaw_compress(0x4000))


def test_fsgm0_frame_sync_waits_for_frst():
    rig = Rig(mux=False)
    c = rig.c
    config(c, bits=8, spcr1=DLB)
    start(c, DLB, frst=False)
    w(c, DXR1, 0x3C)
    rig.run(30000)
    assert not rig.blk().received                                   # FRST = 0: no frame sync
    w(c, SPCR2, GRST | XRST | FRST)
    rig.until(lambda: r(c, SPCR1) & RRDY)
    assert r(c, DRR1) == 0x3C


def test_clock_gate_off_stops_the_port_and_multichannel_warns():
    rig = Rig(mux=False)
    c = rig.c
    c.bus.write(PCLKCR0, 1, c.bus.read(PCLKCR0, 1) & ~(1 << 12))
    config(c, bits=8, spcr1=DLB)
    start(c, DLB)
    w(c, DXR1, 0x42)
    rig.run(50000)
    assert not rig.blk().received
    assert any("clock disabled" in (m or "") for m in _warns(c))
    w(c, MCR1, 1)
    assert any("multichannel" in (m or "") for m in _warns(c))


def test_idle_generator_costs_nothing_unless_a_clock_pin_is_muxed():
    """FSGM = 0 frames, clocks not on pins: no CLKG events between words; with MCLKXA muxed the clock runs freely."""
    rig = Rig(mux=False)
    c = rig.c
    config(c, bits=8, spcr1=DLB)
    start(c, DLB)
    w(c, DXR1, 0x81)
    rig.until(lambda: r(c, SPCR1) & RRDY)
    rig.run(20000)
    assert rig.blk()._ev is None
    c.bus.write(0x6F80 + 0x08, 2, 0x2000)                   # GPIO22 -> MCLKXA
    assert rig.blk()._ev is not None
    ev = _edges(c, ("GPIO22",))
    rig.run(8000)
    assert 19 <= len(ev) <= 21                              # 800 ns period: ~10 cycles
