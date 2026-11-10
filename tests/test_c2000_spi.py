"""TI C2000 SPI-A (ti_c2000.spi, SPRUI07 ch. 9) on the TMS320F28232 description and the fake C28x core: bit timing
from LSPCLK / SPIBRR, the four clocking schemes on the pins, left-justified transmit / right-justified receive,
INT_FLAG / OVERRUN / SPIRXINTA, SPITXBUF back to back, FIFO mode (levels, interrupts, TXDLY, overflow), loopback,
SPISWRESET, a slave F28232 on a board, TALK / SPISTE gating and the SPI NOR flash device."""
from pathlib import Path

import pytest

from chipem.board import Board
from chipem.chipdesc import load_chip
from chipem.devices.spi_flash import SpiNorFlash

from c28x_fake import make_fake_chip

CHIP = Path(__file__).resolve().parent.parent / "chips" / "tms320f28232.yaml"
EALLOW = 1 << 6
B = 0x7040
CCR, CTL, STS, BRR, RXEMU, RXBUF, TXBUF, DAT, FFTX, FFRX, FFCT, PRI = 0, 1, 2, 4, 6, 7, 8, 9, 0xA, 0xB, 0xC, 0xF
SWRESET, POL, LBK = 0x80, 0x40, 0x10
INTENA, TALK, MASTER, PHASE, OVRINTENA = 1, 2, 4, 8, 16
BUFFULL, INT_FLAG, OVERRUN = 0x20, 0x40, 0x80
GPAMUX2, GPBMUX2 = 0x6F80 + 0x08, 0x6F80 + 0x18
PIEIER6 = 0x0CEC
_DESC = None


def _chip(alt=False):
    global _DESC
    _DESC = _DESC or load_chip(CHIP)
    c = make_fake_chip(_DESC)
    c.reset()
    c.core.regs["st1"] |= EALLOW
    c.bus.write(0x7029, 1, 0x0068)                     # watchdog off
    if alt:
        c.bus.write(GPBMUX2, 2, 0x55 << 12)            # GPIO54-57 = SIMO/SOMI/CLK/STE
    else:
        c.bus.write(GPAMUX2, 2, 0x55)                  # GPIO16-19 = SIMO/SOMI/CLK/STE
    c.bus.write(PIEIER6, 1, 0x3)
    return c


def w(c, o, v):
    c.bus.write(B + o, 1, v)


def r(c, o):
    return c.bus.read(B + o, 1)


def edges(c, pin):
    out = []
    c.pins.subscribe(lambda e: out.append((c.now_cycles() if hasattr(c, "now_cycles") else e.t_ns, e.new))
                     if e.field == "level" and e.pin == pin else None)
    return out


def ns_edges(c, pin):
    out = []
    c.pins.subscribe(lambda e: out.append((e.t_ns, e.new)) if e.field == "level" and e.pin == pin else None)
    return out


def master(c, ccr=0x07, ctl=MASTER | TALK, brr=3):
    w(c, CCR, ccr)                                     # held in reset while configuring
    w(c, CTL, ctl)
    w(c, BRR, brr)
    w(c, CCR, ccr | SWRESET)


def until(c, f, n=10000, step=100):
    for _ in range(n):
        if f():
            return
        c.run_cycles(step)
    raise AssertionError("timeout")


# ------------------------------------------------------------------ timing / pins
def test_master_timing_and_waveform_rising_edge_without_delay():
    c = _chip()
    clk, simo, ste = ns_edges(c, "GPIO18"), ns_edges(c, "GPIO16"), ns_edges(c, "GPIO19")
    master(c, ccr=0x07, brr=9)                         # 8 bits, LSPCLK/10 = 2.5 MHz at 100 MHz SYSCLKOUT
    blk = c.peripherals["SPIA"]
    assert blk.baud() == pytest.approx(c.clock.hz / 4 / 10)
    assert c.pins.get("GPIO18").level() == "L" and c.pins.get("GPIO19").level() == "H"
    c.run_cycles(10)
    t0 = c.clock.now_ns()
    w(c, DAT, 0xA5 << 8)                               # left-justified
    c.run_cycles(8 * 40 + 10)
    assert r(c, STS) & INT_FLAG
    # SPICLK: 8 pulses, inactive half then active half of 20 cycles each (period 400 ns at 100 MHz)
    cyc = 1e9 / c.clock.hz                             # ns per SYSCLKOUT cycle (10 MHz after reset)
    rises = [t for t, v in clk if v == "H" and t >= t0]
    falls = [t for t, v in clk if v == "L" and t >= t0]
    assert len(rises) == 8 and len(falls) == 8
    assert rises[0] - t0 == pytest.approx(20 * cyc) and rises[1] - rises[0] == pytest.approx(40 * cyc)
    assert falls[0] - rises[0] == pytest.approx(20 * cyc)
    # STE low for the whole character, high again after the last bit
    ste = [(t, v) for t, v in ste if t >= t0]
    assert [v for _, v in ste] == ["L", "H"]
    assert ste[1][0] - ste[0][0] == pytest.approx(8 * 40 * cyc)
    # data output on the rising edge (CLKPOLARITY = 0, CLK_PHASE = 0): sample SIMO at each falling edge
    def level_at(evs, t, init="L"):
        lv = init
        for te, v in evs:
            if te <= t:
                lv = v
        return lv
    bits = "".join("1" if level_at(simo, t) == "H" else "0" for t in falls)
    assert bits == "10100101"
    # SOMI floating with pull-up -> received 1s
    assert r(c, RXBUF) & 0xFF == 0xFF and not r(c, STS) & INT_FLAG


@pytest.mark.parametrize("pol,phase", [(0, 0), (0, 1), (1, 0), (1, 1)])
def test_four_clock_schemes_between_two_f28232(pol, phase):
    a, b = _chip(), _chip()
    board = Board({"A": a, "B": b}, quantum_ns=200)
    for pin in ("GPIO16", "GPIO17", "GPIO18", "GPIO19"):
        board.add_net(pin, [("A", pin), ("B", pin)])
    ccr = 0x07 | (POL if pol else 0)
    ctl_ph = PHASE if phase else 0
    w(b, CCR, ccr)
    w(b, CTL, TALK | ctl_ph)                           # slave
    w(b, CCR, ccr | SWRESET)
    w(b, TXBUF, 0x3C << 8)
    master(a, ccr=ccr, ctl=MASTER | TALK | ctl_ph, brr=7)
    idle = "H" if pol else "L"
    assert a.pins.get("GPIO18").level() == idle and b.pins.get("GPIO18").level() == idle
    w(a, TXBUF, 0xC3 << 8)
    for _ in range(200):
        board.run_for(1000)
        if r(a, STS) & INT_FLAG and r(b, STS) & INT_FLAG:
            break
    assert r(a, RXBUF) & 0xFF == 0x3C
    assert r(b, RXBUF) & 0xFF == 0xC3
    assert a.pins.get("GPIO18").level() == idle and a.pins.get("GPIO19").level() == "H"
    # second word both ways (slave loaded from SPIDAT directly)
    w(b, DAT, 0x81 << 8)
    w(a, DAT, 0x7E << 8)
    for _ in range(200):
        board.run_for(1000)
        if r(a, STS) & INT_FLAG and r(b, STS) & INT_FLAG:
            break
    assert r(a, RXBUF) & 0xFF == 0x81 and r(b, RXBUF) & 0xFF == 0x7E


def test_loopback_right_justified_with_leftover_bits_and_no_data_pin_activity():
    c = _chip()
    simo = ns_edges(c, "GPIO16")
    master(c, ccr=0x00 | LBK)                           # 1-bit characters, loopback
    w(c, DAT, 0x737B)                                  # Example 9-1
    c.run_cycles(40)
    assert r(c, STS) & INT_FLAG
    assert r(c, RXEMU) == 0xE6F6 and r(c, STS) & INT_FLAG      # emulation read keeps INT_FLAG
    assert r(c, RXBUF) == 0xE6F6 and not r(c, STS) & INT_FLAG  # looped bit = transmitted MSB (0)
    assert simo == []                                  # SIMO not driven in loopback
    assert c.peripherals["SPIA"].frames[-1] == (0, 0)


def test_int_flag_overrun_and_spirxinta():
    c = _chip()
    master(c, ccr=0x07 | LBK, ctl=MASTER | TALK | INTENA | OVRINTENA)
    pie = c.arch.pie
    w(c, DAT, 0x1100)
    c.run_cycles(200)
    assert r(c, STS) & INT_FLAG and pie.ifr[6] & 1
    pie.ifr[6] = 0
    w(c, DAT, 0x2200)                                  # completes with INT_FLAG still set -> OVERRUN
    c.run_cycles(200)
    assert r(c, STS) & OVERRUN and r(c, RXBUF) & 0xFF == 0x22
    w(c, STS, OVERRUN)                                 # w1c
    assert not r(c, STS) & (OVERRUN | INT_FLAG)


def test_txbuf_back_to_back_buffull():
    c = _chip()
    ste = ns_edges(c, "GPIO19")
    master(c, ccr=0x07)
    w(c, TXBUF, 0x1200)                                # falls through to SPIDAT, starts
    assert not r(c, STS) & BUFFULL
    w(c, TXBUF, 0x3400)
    assert r(c, STS) & BUFFULL
    c.run_cycles(16 * 8 + 4)
    assert not r(c, STS) & BUFFULL                     # moved to SPIDAT, shifting
    c.run_cycles(16 * 8 + 4)
    blk = c.peripherals["SPIA"]
    assert [f[0] for f in blk.frames] == [0x12, 0x34]
    assert [v for _, v in ste] == ["L", "H"]           # STE stays low across back-to-back characters


def test_fifo_levels_interrupts_txdly_and_overflow():
    c = _chip()
    pie = c.arch.pie
    w(c, CCR, 0x07 | LBK)
    w(c, CTL, MASTER | TALK)
    w(c, BRR, 3)
    w(c, FFTX, 0xE040 | 0x20 | 1)                      # SPIRST, SPIFFENA, TXFIFO, TXFFINTCLR, TXFFIENA, TXFFIL=1
    w(c, FFRX, 0x2040 | 0x20 | 4)                      # RXFIFORESET, RXFFINTCLR, RXFFIENA, RXFFIL=4
    w(c, FFCT, 2)                                      # 2 SPICLK cycles between words
    w(c, CCR, 0x07 | LBK | SWRESET)
    assert r(c, FFTX) & 0x80 and pie.ifr[6] & 2        # TXFFST 0 <= 1 -> TXFFINT, SPITXINTA (6.2)
    w(c, FFTX, r(c, FFTX) | 0x40)
    for v in (1, 2, 3, 4, 5):
        w(c, TXBUF, v << 8)
    assert (r(c, FFTX) >> 8) & 0x1F == 4               # one word already in the shift register
    c.run_cycles(4 * (8 * 16 + 2 * 16) + 8 * 16 + 20)
    assert (r(c, FFRX) >> 8) & 0x1F == 5 and r(c, FFRX) & 0x80 and pie.ifr[6] & 1
    assert [r(c, RXBUF) & 0xFF for _ in range(5)] == [1, 2, 3, 4, 5]
    blk = c.peripherals["SPIA"]
    # TXDLY: next word starts 2 SPICLK cycles (32 core cycles) after the previous one ends
    assert blk.frames[0][0] == 1
    # overflow: 17 words without reading
    for v in range(17):
        w(c, TXBUF, v << 8)
        c.run_cycles(8 * 16 + 2 * 16 + 4)
    assert r(c, FFRX) & 0x8000 and (r(c, FFRX) >> 8) & 0x1F == 16
    assert r(c, RXBUF) & 0xFF == 1                    # oldest (0) lost
    w(c, FFRX, r(c, FFRX) | 0x4000)
    assert not r(c, FFRX) & 0x8000


def test_swreset_clears_flags_drives_clk_low_and_stops():
    c = _chip()
    master(c, ccr=0x07 | POL, brr=20)
    assert c.pins.get("GPIO18").level() == "H"         # idle high with CLKPOLARITY = 1
    w(c, DAT, 0xFF00)
    c.run_cycles(100)
    w(c, CCR, 0x07 | POL)                              # SPISWRESET = 0 mid-character
    assert c.pins.get("GPIO18").level() == "L" and c.pins.get("GPIO19").level() == "H"
    c.run_cycles(2000)
    assert not r(c, STS) & INT_FLAG and not c.peripherals["SPIA"].frames
    w(c, CCR, 0x07 | POL | SWRESET)
    assert c.pins.get("GPIO18").level() == "H"


def test_slave_talk_and_ste_gating_alternate_pins():
    a, b = _chip(alt=True), _chip(alt=True)
    board = Board({"A": a, "B": b}, quantum_ns=200)
    for pin in ("GPIO54", "GPIO55", "GPIO56", "GPIO57"):
        board.add_net(pin, [("A", pin), ("B", pin)])
    w(b, CTL, 0)                                       # slave, TALK = 0: SOMI high impedance
    w(b, CCR, 0x07 | SWRESET)
    w(b, DAT, 0x0F00)
    assert b.pins.get("GPIO55").direction == "in"
    master(a, ccr=0x07, brr=7)
    w(a, DAT, 0x5A00)
    board.run_for(30_000)
    assert r(b, RXBUF) & 0xFF == 0x5A                  # receives with TALK = 0
    assert r(a, RXBUF) & 0xFF == 0xFF                  # nobody drives SOMI: pull-up
    w(b, CTL, TALK)
    assert a.pins.get("GPIO57").level() == "H"         # STE inactive (A idle): slave deselected,
    assert b.pins.get("GPIO55").direction == "in"      # SOMI stays released despite TALK = 1
    w(b, DAT, 0x0F00)
    w(a, DAT, 0x3300)
    board.run_for(2_000)
    assert b.pins.get("GPIO55").direction == "out"     # selected while A's STE is low
    board.run_for(30_000)
    assert r(a, RXBUF) & 0xFF == 0x0F and r(b, RXBUF) & 0xFF == 0x33
    assert b.pins.get("GPIO55").direction == "in"


def test_spi_flash_rdid_through_fifo_with_ste_as_chip_select():
    c = _chip()
    flash = SpiNorFlash(0xEF4016).attach(c, "SPIA", cs="GPIO19")
    w(c, CCR, 0x07)
    w(c, CTL, MASTER | TALK | PHASE)                   # mode 0: data valid before the first rising edge
    w(c, BRR, 3)
    w(c, FFTX, 0xE040)
    w(c, FFRX, 0x2040 | 0x10)
    w(c, CCR, 0x07 | SWRESET)
    for v in (0x9F, 0, 0, 0):
        w(c, TXBUF, v << 8)
    until(c, lambda: (r(c, FFRX) >> 8) & 0x1F == 4)
    got = [r(c, RXBUF) & 0xFF for _ in range(4)]
    assert got[1:] == [0xEF, 0x40, 0x16]
    assert c.pins.get("GPIO19").level() == "H" and not flash.selected


def test_no_pins_muxed_timing_only():
    c = make_fake_chip(load_chip(CHIP))
    c.reset()
    c.bus.write(0x7029, 1, 0x0068)
    master(c, ccr=0x0F, brr=127)                       # 16 bits at LSPCLK/128
    w(c, DAT, 0x8001)
    c.run_cycles(16 * 128 * 4 - 10)
    assert not r(c, STS) & INT_FLAG
    c.run_cycles(20)
    assert r(c, STS) & INT_FLAG and r(c, RXBUF) == 0   # SOMI not muxed: 0s
