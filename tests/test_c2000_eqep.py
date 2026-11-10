"""C2833x eQEP (chipem/periph/ti_c2000/eqep.py, SPRUI07 ch. 6 Type-0 eQEP = SPRUG05A) on the TMS320F28232 with a
spinning C28x core: quadrature / direction-count / up / down decoding, polarity and swap, phase error, QPOSMAX
rollover, index reset and markers, index / strobe latch and init, software init, unit timer, watchdog, capture unit,
position compare with shadow, flags and PIE interrupt, QPEN software reset, clock gate, reset.  Edges are injected on
GPIO20/21/22/23 (MUX1 = EQEP1A/B/S/I) cycle-exactly."""
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.core.c28x.asm import assemble

CHIP = Path(__file__).resolve().parents[1] / "chips" / "tms320f28232.yaml"
EALLOW = 1 << 6
PCLKCR1, WDCR, GPAMUX2_LO, GPAMUX2_HI = 0x701D, 0x7029, 0x6F88, 0x6F89
Q1, Q2 = 0x6B00, 0x6B40
QPOSCNT, QPOSINIT, QPOSMAX, QPOSCMP, QPOSILAT, QPOSSLAT, QPOSLAT, QUTMR, QUPRD = 0, 2, 4, 6, 8, 0xA, 0xC, 0xE, 0x10
QWDTMR, QWDPRD, QDECCTL, QEPCTL, QCAPCTL, QPOSCTL, QEINT, QFLG, QCLR, QFRC, QEPSTS = range(0x12, 0x1D)
QCTMR, QCPRD, QCTMRLAT, QCPRDLAT = 0x1D, 0x1E, 0x1F, 0x20
# QDECCTL
QSRC_DIR, QSRC_UP, QSRC_DOWN = 1 << 14, 2 << 14, 3 << 14
XCR, SWAP, IGATE, QAP, QBP, QIP, QSP = 1 << 11, 1 << 10, 1 << 9, 1 << 8, 1 << 7, 1 << 6, 1 << 5
# QEPCTL
PCRM_IDX, PCRM_MAX, PCRM_FIRST, PCRM_UTO = 0 << 12, 1 << 12, 2 << 12, 3 << 12
SEI_RISE, SEI_DIR, IEI_RISE, IEI_FALL = 2 << 10, 3 << 10, 2 << 8, 3 << 8
SWI, SEL_DIR, IEL_RISE, IEL_FALL, IEL_MARK = 1 << 7, 1 << 6, 1 << 4, 2 << 4, 3 << 4
QPEN, QCLM, UTE, WDE = 1 << 3, 1 << 2, 1 << 1, 1 << 0
# QCAPCTL / QPOSCTL
CEN = 1 << 15
PCSHDW, PCLOAD, PCE_EN = 1 << 15, 1 << 14, 1 << 12
# QFLG
INT, PCE, PHE, QDC, WTO, PCU, PCO, PCR, PCM, SEL, IEL, UTO = (1 << i for i in range(12))
# QEPSTS
PCEF, FIMF, CDEF, COEF, QDLF, QDF, FIDF, UPEVNT = (1 << i for i in range(8))
A, B, S, I = "GPIO20", "GPIO21", "GPIO22", "GPIO23"
FWD = [(A, "H"), (B, "H"), (A, "L"), (B, "L")]          # A leads B: clockwise / forward
REV = [(B, "H"), (A, "H"), (B, "L"), (A, "L")]


@pytest.fixture
def chip():
    ch = Chip(load_chip(CHIP))
    ch.load_firmware(assemble('''
        .sect "boot", 0x33FFF6
        LB start
        .sect "code", 0x8000
start:  SB start,UNC
''').image())
    ch.reset()
    ch.core.write_reg("st1", ch.core.read_reg("st1") | EALLOW)
    w(ch, WDCR, 0x0068)                                              # watchdog off
    w(ch, PCLKCR1, 0xC000)                                           # EQEP1/2 clocks
    w(ch, GPAMUX2_LO, 0x5500)                                        # GPIO20-23 = EQEP1A/B/S/I
    for p in (A, B, S, I):
        ch.pins.set_external(p, "L")
    return ch


def w(ch, a, v):
    ch.bus.write(a, 1, v)


def w32(ch, a, v):
    ch.bus.write(a, 2, v)


def r(ch, a):
    return ch.bus.read(a, 1)


def r32(ch, a):
    return ch.bus.read(a, 2)


def at(ch, cycle):
    ch.run_cycles(cycle - ch.now_cycles())
    assert ch.now_cycles() == cycle


def edges(ch, seq, n=1, gap=10):
    """Drive the pin sequence `n` times, one edge every `gap` cycles."""
    for _ in range(n):
        for pin, lvl in seq:
            ch.run_cycles(gap)
            ch.pins.set_external(pin, lvl)


def cnt(ch, blk=Q1):
    return r32(ch, blk + QPOSCNT)


def enable(ch, ctl=PCRM_MAX | QPEN, dec=0, mx=0xFFFFFFFF, blk=Q1):
    w(ch, blk + QDECCTL, dec)
    w32(ch, blk + QPOSMAX, mx)
    w(ch, blk + QEPCTL, ctl)


# ------------------------------------------------------------------------------------------------ registers
def test_registers_not_eallow_protected_reset_values_and_access(chip):
    chip.core.write_reg("st1", chip.core.read_reg("st1") & ~EALLOW)
    w32(chip, Q1 + QPOSINIT, 0x12345678)
    w(chip, Q1 + QDECCTL, 0xFFFF)
    assert r32(chip, Q1 + QPOSINIT) == 0x12345678
    assert r(chip, Q1 + QDECCTL) == 0xFFE0                           # bits 4:0 reserved
    assert not [e for e in chip.log if e.kind == "warn" and "EALLOW" in (e.get("msg") or "")]
    w(chip, Q1 + QCAPCTL, 0xFFFF)
    assert r(chip, Q1 + QCAPCTL) == 0x807F                           # CEN, CCPS, UPPS
    w32(chip, Q1 + QPOSILAT, 5)
    w(chip, Q1 + QFLG, 0xFFFF)
    w(chip, Q1 + QCPRDLAT, 7)
    assert r32(chip, Q1 + QPOSILAT) == 0 and r(chip, Q1 + QFLG) == 0 and r(chip, Q1 + QCPRDLAT) == 0
    assert chip.peripherals["EQEP2"].base == Q2 and chip.peripherals["EQEP1"].model == "ti_c2000.eqep"


# ------------------------------------------------------------------------------------------------ decoder
def test_quadrature_counts_every_edge_and_direction(chip):
    enable(chip)
    edges(chip, FWD, 3)
    assert cnt(chip) == 12 and r(chip, Q1 + QEPSTS) & QDF
    assert r(chip, Q1 + QFLG) & QDC                                  # QDF 0 -> 1 at the first edge
    w(chip, Q1 + QCLR, 0xFFF)
    edges(chip, REV, 2)
    assert cnt(chip) == 4 and not r(chip, Q1 + QEPSTS) & QDF and r(chip, Q1 + QFLG) == QDC


def test_reversal_on_the_same_signal_toggles_direction(chip):
    enable(chip)
    edges(chip, [(A, "H"), (B, "H")])                                # +2
    edges(chip, [(B, "L")])                                          # B rose, now falls: toggle -> -1
    assert cnt(chip) == 1 and not r(chip, Q1 + QEPSTS) & QDF
    edges(chip, [(B, "H")])                                          # toggle again -> +1
    assert cnt(chip) == 2 and r(chip, Q1 + QEPSTS) & QDF


def test_swap_and_polarity_reverse_the_count(chip):
    enable(chip, dec=SWAP)
    edges(chip, FWD, 2)
    assert cnt(chip) == 0xFFFFFFF8                                   # counted down through 0 -> QPOSMAX
    enable(chip, dec=QAP)                                            # A inverted: B now leads
    w32(chip, Q1 + QPOSCNT, 100)
    edges(chip, FWD)
    assert cnt(chip) == 96


def test_phase_error_on_simultaneous_a_and_b_edges(chip):
    enable(chip)
    w(chip, Q1 + QEINT, PHE)
    edges(chip, [(A, "H")])
    chip.run_cycles(10)
    chip.pins.set_external(A, "L")
    chip.pins.set_external(B, "H")                                   # same cycle: 10 -> 01 is invalid
    assert r(chip, Q1 + QFLG) == PHE | QDC | INT and cnt(chip) == 0
    assert chip.arch.pie.ifr[5] & 1                                  # EQEP1_INT = PIE 5.1


def test_direction_count_mode_2x_and_1x(chip):
    enable(chip, dec=QSRC_DIR)
    chip.pins.set_external(B, "H")                                   # direction input: up
    edges(chip, [(A, "H"), (A, "L")], 3)
    assert cnt(chip) == 6
    chip.pins.set_external(B, "L")                                   # down
    edges(chip, [(A, "H"), (A, "L")])
    assert cnt(chip) == 4
    enable(chip, dec=QSRC_DIR | XCR)                                 # 1x: rising edges of A only
    chip.pins.set_external(B, "H")
    edges(chip, [(A, "H"), (A, "L")], 3)
    assert cnt(chip) == 7


def test_up_and_down_count_modes(chip):
    enable(chip, dec=QSRC_UP)
    edges(chip, [(A, "H"), (A, "L"), (B, "H"), (B, "L")], 2)         # B edges do not count
    assert cnt(chip) == 4 and r(chip, Q1 + QEPSTS) & QDF
    enable(chip, dec=QSRC_DOWN | XCR)
    edges(chip, [(A, "H"), (A, "L")], 3)
    assert cnt(chip) == 1 and not r(chip, Q1 + QEPSTS) & QDF


def test_qposmax_rollover_sets_pco_and_pcu(chip):
    enable(chip, mx=5)
    w32(chip, Q1 + QPOSCNT, 4)
    edges(chip, [(A, "H")])
    assert cnt(chip) == 5 and not r(chip, Q1 + QFLG) & PCO
    edges(chip, [(B, "H")])
    assert cnt(chip) == 0 and r(chip, Q1 + QFLG) & PCO
    edges(chip, [(B, "L")])                                          # reverse: 0 -> QPOSMAX
    assert cnt(chip) == 5 and r(chip, Q1 + QFLG) & PCU


def test_qpen_clear_ignores_edges_and_resets_status(chip):
    enable(chip)
    edges(chip, FWD)
    assert r(chip, Q1 + QEPSTS) & QDF
    w(chip, Q1 + QEPCTL, PCRM_MAX)                                   # QPEN = 0: software reset
    assert r(chip, Q1 + QEPSTS) == 0 and cnt(chip) == 4              # QPOSCNT kept (write it now)
    edges(chip, FWD)
    assert cnt(chip) == 4
    assert r(chip, Q1 + QFLG) & QDC                                  # QFLG is not reset by QPEN


def test_software_init(chip):
    enable(chip)
    w32(chip, Q1 + QPOSINIT, 1000)
    w(chip, Q1 + QEPCTL, PCRM_MAX | QPEN | SWI)
    assert cnt(chip) == 1000
    edges(chip, FWD)
    w(chip, Q1 + QEPCTL, PCRM_MAX | QPEN | SWI)                      # SWI still set: writing 1 again re-inits
    assert cnt(chip) == 1000 and r(chip, Q1 + QEPCTL) & SWI


# ------------------------------------------------------------------------------------------------ index / strobe
def test_index_reset_markers_fimf_and_position_error(chip):
    enable(chip, ctl=PCRM_IDX | QPEN, mx=7)                          # 2-line encoder: 8 counts / revolution
    w32(chip, Q1 + QPOSCNT, 3)
    edges(chip, [(A, "H")])                                          # 4
    chip.pins.set_external(I, "H")                                   # index event: armed
    edges(chip, [(B, "H")])                                          # first marker (B rising, forward)
    assert r32(chip, Q1 + QPOSILAT) == 5 and cnt(chip) == 0          # latched after the count, then reset
    sts = r(chip, Q1 + QEPSTS)
    assert sts & FIMF and sts & FIDF and sts & QDLF and sts & PCEF   # 5 is neither 0 nor QPOSMAX
    assert r(chip, Q1 + QFLG) & (PCE | IEL) == PCE | IEL
    chip.pins.set_external(I, "L")
    w(chip, Q1 + QCLR, 0xFFF)
    w(chip, Q1 + QEPSTS, FIMF)                                       # w1c
    edges(chip, [(A, "L"), (B, "L")], 1)                             # 2
    edges(chip, FWD)                                                 # 6
    edges(chip, [(A, "H")])                                          # 7
    chip.pins.set_external(I, "H")
    edges(chip, [(A, "L")])                                          # not the marker edge (B rising): reverse!
    assert cnt(chip) == 6
    edges(chip, [(A, "H")])                                          # 7
    edges(chip, [(B, "H")])                                          # marker: 7 -> 0 (rollover), latched 0
    assert cnt(chip) == 0 and r32(chip, Q1 + QPOSILAT) == 0
    sts = r(chip, Q1 + QEPSTS)
    assert not sts & PCEF and not sts & FIMF                         # FIMF is not set again
    assert r(chip, Q1 + QFLG) & (PCE | IEL) == IEL


def test_index_reset_in_reverse_loads_qposmax(chip):
    enable(chip, ctl=PCRM_IDX | QPEN, mx=7)
    chip.pins.set_external(I, "H")
    edges(chip, [(B, "H")])                                          # first marker in reverse (B rising, reverse)
    assert cnt(chip) == 7 and not r(chip, Q1 + QEPSTS) & FIDF
    chip.pins.set_external(I, "L")
    edges(chip, [(A, "H"), (B, "L"), (A, "L")])                      # reverse: 6, 5, 4
    assert cnt(chip) == 4
    chip.pins.set_external(I, "H")
    edges(chip, [(B, "H")])                                          # reverse marker: same edge as the first
    assert cnt(chip) == 7 and r32(chip, Q1 + QPOSILAT) == 3


def test_first_index_mode_resets_once(chip):
    enable(chip, ctl=PCRM_FIRST | QPEN, mx=99)
    w32(chip, Q1 + QPOSCNT, 50)
    chip.pins.set_external(I, "H")
    edges(chip, [(A, "H")])
    assert cnt(chip) == 0
    chip.pins.set_external(I, "L")
    edges(chip, [(B, "H"), (A, "L")])
    chip.pins.set_external(I, "H")
    edges(chip, [(B, "L")])
    assert cnt(chip) == 3 and not r(chip, Q1 + QEPSTS) & FIMF        # FIMF/FIDF not applicable here


def test_index_latch_rising_falling_and_marker(chip):
    enable(chip, ctl=PCRM_MAX | QPEN | IEL_RISE)
    edges(chip, FWD)
    chip.pins.set_external(I, "H")
    assert r32(chip, Q1 + QPOSILAT) == 4 and r(chip, Q1 + QFLG) & IEL and r(chip, Q1 + QEPSTS) & QDLF
    edges(chip, FWD)
    chip.pins.set_external(I, "L")
    assert r32(chip, Q1 + QPOSILAT) == 4                             # falling: no latch
    w(chip, Q1 + QEPCTL, PCRM_MAX | QPEN | IEL_FALL)
    chip.pins.set_external(I, "H")
    edges(chip, [(A, "H")])
    chip.pins.set_external(I, "L")
    assert r32(chip, Q1 + QPOSILAT) == 9
    w(chip, Q1 + QEPCTL, PCRM_MAX | QPEN | IEL_MARK)                 # software index marker: latch, no reset
    chip.pins.set_external(I, "H")
    edges(chip, [(B, "H")])
    assert r32(chip, Q1 + QPOSILAT) == 10 and cnt(chip) == 10 and r(chip, Q1 + QEPSTS) & FIMF


def test_index_init_rising_and_falling(chip):
    enable(chip, ctl=PCRM_MAX | QPEN | IEI_RISE)
    w32(chip, Q1 + QPOSINIT, 500)
    edges(chip, FWD)
    chip.pins.set_external(I, "H")
    assert cnt(chip) == 500
    edges(chip, FWD)
    chip.pins.set_external(I, "L")
    assert cnt(chip) == 504
    w(chip, Q1 + QEPCTL, PCRM_MAX | QPEN | IEI_FALL)
    chip.pins.set_external(I, "H")
    assert cnt(chip) == 504
    chip.pins.set_external(I, "L")
    assert cnt(chip) == 500


def test_index_polarity_and_gating_with_strobe(chip):
    enable(chip, ctl=PCRM_MAX | QPEN | IEI_RISE, dec=IGATE)
    w32(chip, Q1 + QPOSINIT, 77)
    chip.pins.set_external(I, "H")                                   # strobe low: gated off
    assert cnt(chip) == 0
    chip.pins.set_external(S, "H")                                   # QI = I & S rises now
    assert cnt(chip) == 77
    w(chip, Q1 + QDECCTL, QIP)                                       # inverted index (no edge on the change)
    edges(chip, FWD)
    chip.pins.set_external(I, "L")                                   # inverted: a rising QI
    assert cnt(chip) == 77


def test_strobe_latch_and_init(chip):
    enable(chip, ctl=PCRM_MAX | QPEN)                                # SEL = 0: rising strobe latches
    edges(chip, FWD)
    chip.pins.set_external(S, "H")
    assert r32(chip, Q1 + QPOSSLAT) == 4 and r(chip, Q1 + QFLG) & SEL
    chip.pins.set_external(S, "L")
    w(chip, Q1 + QEPCTL, PCRM_MAX | QPEN | SEL_DIR | SEI_DIR)        # direction dependent latch and init
    w32(chip, Q1 + QPOSINIT, 40)
    edges(chip, REV)                                                 # 0, reverse
    chip.pins.set_external(S, "H")                                   # rising in reverse: nothing
    assert r32(chip, Q1 + QPOSSLAT) == 4 and cnt(chip) == 0
    chip.pins.set_external(S, "L")                                   # falling in reverse: latch, then init
    assert r32(chip, Q1 + QPOSSLAT) == 0 and cnt(chip) == 40
    w(chip, Q1 + QEPCTL, PCRM_MAX | QPEN | SEI_RISE)
    w(chip, Q1 + QDECCTL, QSP)                                       # strobe inverted: pin falling = QS rising
    w32(chip, Q1 + QPOSINIT, 9)
    chip.pins.set_external(S, "H")
    chip.pins.set_external(S, "L")
    assert cnt(chip) == 9


# ------------------------------------------------------------------------------------------------ timers
def test_unit_timer_period_latch_and_qutmr(chip):
    enable(chip, ctl=PCRM_MAX | QPEN)
    w32(chip, Q1 + QUPRD, 999)
    w(chip, Q1 + QEPCTL, PCRM_MAX | QPEN | QCLM | UTE)
    t0 = chip.now_cycles()
    edges(chip, FWD, 2, gap=50)                                      # 8 counts by t0 + 400
    at(chip, t0 + 998)
    assert r32(chip, Q1 + QUTMR) == 998 and not r(chip, Q1 + QFLG) & UTO
    at(chip, t0 + 999)
    assert r(chip, Q1 + QFLG) & UTO and r32(chip, Q1 + QPOSLAT) == 8 and r32(chip, Q1 + QUTMR) == 999
    at(chip, t0 + 1000)
    assert r32(chip, Q1 + QUTMR) == 0
    w(chip, Q1 + QCLR, UTO)
    edges(chip, FWD, gap=50)
    at(chip, t0 + 1998)
    assert not r(chip, Q1 + QFLG) & UTO
    at(chip, t0 + 1999)                                              # period QUPRD + 1
    assert r(chip, Q1 + QFLG) & UTO and r32(chip, Q1 + QPOSLAT) == 12
    w(chip, Q1 + QEPCTL, PCRM_MAX | QPEN | QCLM)                     # UTE off: QUTMR stops
    chip.run_cycles(100)
    v = r32(chip, Q1 + QUTMR)
    chip.run_cycles(100)
    assert r32(chip, Q1 + QUTMR) == v


def test_unit_time_reset_mode_and_no_latch_without_qclm(chip):
    w32(chip, Q1 + QUPRD, 499)
    enable(chip, ctl=PCRM_UTO | QPEN | UTE)
    t0 = chip.now_cycles()
    edges(chip, FWD, gap=50)
    at(chip, t0 + 499)
    assert cnt(chip) == 0 and r32(chip, Q1 + QPOSLAT) == 0           # reset at UTO, QCLM = 0: no latch
    enable(chip, ctl=PCRM_UTO | QPEN | UTE, dec=QSRC_DOWN, mx=1234)
    at(chip, t0 + 999)
    assert cnt(chip) == 1234                                         # down-count mode: QPOSMAX


def test_watchdog_timeout_and_reset_by_qclk(chip):
    enable(chip, ctl=PCRM_MAX | QPEN)
    w(chip, Q1 + QWDPRD, 10)
    w(chip, Q1 + QEPCTL, PCRM_MAX | QPEN | WDE)
    t0 = chip.now_cycles()
    at(chip, t0 + 64 * 5 + 3)
    assert r(chip, Q1 + QWDTMR) == 5
    chip.pins.set_external(A, "H")                                   # QCLK: QWDTMR = 0 (prescaler phase kept)
    assert r(chip, Q1 + QWDTMR) == 0
    at(chip, t0 + 64 * 15 - 1)
    assert not r(chip, Q1 + QFLG) & WTO and r(chip, Q1 + QWDTMR) == 9
    at(chip, t0 + 64 * 15)                                           # the 10th tick after the edge
    assert r(chip, Q1 + QFLG) & WTO and r(chip, Q1 + QWDTMR) == 10
    at(chip, t0 + 64 * 16)
    assert r(chip, Q1 + QWDTMR) == 0                                 # rolls over
    w(chip, Q1 + QCLR, WTO)
    at(chip, t0 + 64 * 26 - 1)
    assert not r(chip, Q1 + QFLG) & WTO
    at(chip, t0 + 64 * 26)                                           # period QWDPRD + 1 ticks
    assert r(chip, Q1 + QFLG) & WTO


def test_capture_unit_period_latch_on_qposcnt_read_and_flags(chip):
    enable(chip)
    w(chip, Q1 + QCAPCTL, 0x0021)                                    # CCPS /4, UPPS /2
    w(chip, Q1 + QCAPCTL, CEN | 0x0021)
    t0 = chip.now_cycles()
    for k, (pin, lvl) in enumerate(FWD * 2, 1):
        at(chip, t0 + 400 * k)
        chip.pins.set_external(pin, lvl)
    assert r(chip, Q1 + QCPRD) == 200                                # 2 edges x 400 cycles / 4
    assert r(chip, Q1 + QEPSTS) & UPEVNT
    w(chip, Q1 + QEPSTS, UPEVNT)
    assert not r(chip, Q1 + QEPSTS) & UPEVNT
    at(chip, t0 + 3200 + 100)
    assert r(chip, Q1 + QCTMR) == 25
    cnt(chip)                                                        # QCLM = 0: a QPOSCNT read latches
    assert r(chip, Q1 + QCTMRLAT) == 25 and r(chip, Q1 + QCPRDLAT) == 200
    sts = r(chip, Q1 + QEPSTS)
    assert sts & CDEF                                                # QDF went 0 -> 1 during the first period
    w(chip, Q1 + QEPSTS, CDEF)
    edges(chip, REV[:1], gap=10)                                     # reverse: direction change
    edges(chip, REV[1:2], gap=4 * 0x10000)                           # > 0xFFFF capture ticks
    assert r(chip, Q1 + QEPSTS) & (COEF | CDEF) == COEF | CDEF


def test_capture_overflow_seen_on_status_read(chip):
    enable(chip)
    w(chip, Q1 + QCAPCTL, CEN)
    chip.run_cycles(0x10000 + 5)
    assert r(chip, Q1 + QEPSTS) & COEF
    w(chip, Q1 + QEPSTS, COEF)
    assert not r(chip, Q1 + QEPSTS) & COEF                           # once per interval


# ------------------------------------------------------------------------------------------------ compare
def test_position_compare_match_and_shadow_load(chip):
    enable(chip)
    w32(chip, Q1 + QPOSCMP, 2)
    w(chip, Q1 + QPOSCTL, PCE_EN)
    edges(chip, [(A, "H")])
    assert not r(chip, Q1 + QFLG) & PCM
    edges(chip, [(B, "H")])
    assert r(chip, Q1 + QFLG) & PCM
    w(chip, Q1 + QCLR, PCM)
    w(chip, Q1 + QPOSCTL, PCE_EN | PCSHDW | PCLOAD)                  # shadow, load on compare match
    w32(chip, Q1 + QPOSCMP, 5)
    assert r32(chip, Q1 + QPOSCMP) == 2                              # active unchanged until the load
    edges(chip, [(A, "L"), (B, "L")])                                # 3, 4
    edges(chip, [(B, "H"), (A, "H")])                                # reverse: 3, 2 -> match, load
    assert r(chip, Q1 + QFLG) & (PCM | PCR) == PCM | PCR and r32(chip, Q1 + QPOSCMP) == 5
    w(chip, Q1 + QCLR, 0xFFF)
    w(chip, Q1 + QPOSCTL, PCE_EN | PCSHDW)                           # load on QPOSCNT = 0
    w32(chip, Q1 + QPOSCMP, 9)
    edges(chip, [(B, "L"), (A, "L")])                                # 1, 0
    assert r(chip, Q1 + QFLG) == PCR and r32(chip, Q1 + QPOSCMP) == 9


# ------------------------------------------------------------------------------------------------ interrupts
def test_interrupt_flag_pie_and_reassert_on_clear(chip):
    pie = chip.arch.pie
    enable(chip)
    w(chip, Q1 + QEINT, PCO)
    w32(chip, Q1 + QPOSMAX, 1)
    edges(chip, [(A, "H")])
    assert r(chip, Q1 + QFLG) == QDC and not pie.ifr[5] & 1          # QDC not enabled
    edges(chip, [(B, "H")])
    assert r(chip, Q1 + QFLG) == QDC | PCO | INT and pie.ifr[5] & 1
    pie.ifr[5] &= ~1
    w(chip, Q1 + QCLR, INT)                                          # PCO still set and enabled: again
    assert r(chip, Q1 + QFLG) == QDC | PCO | INT and pie.ifr[5] & 1
    pie.ifr[5] &= ~1
    w(chip, Q1 + QCLR, INT | PCO | QDC)
    assert r(chip, Q1 + QFLG) == 0 and not pie.ifr[5] & 1
    w(chip, Q1 + QFRC, UTO)                                          # force: reads 0, enabled? no
    assert r(chip, Q1 + QFRC) == 0 and r(chip, Q1 + QFLG) == UTO and not pie.ifr[5] & 1
    w(chip, Q1 + QEINT, PCO | UTO)                                   # enabling a set flag raises INT
    assert r(chip, Q1 + QFLG) == UTO | INT and pie.ifr[5] & 1


def test_eqep2_on_gpio24_27_and_its_pie_line(chip):
    w(chip, GPAMUX2_HI, 0x00AA)                                      # GPIO24-27 = MUX2 = EQEP2A/B/I/S
    assert chip.pins.get("GPIO24").owner == "EQEP2A" and chip.pins.get("GPIO24").direction == "in"
    for p in ("GPIO24", "GPIO25"):
        chip.pins.set_external(p, "L")
    enable(chip, blk=Q2)
    w(chip, Q2 + QEINT, QDC)
    edges(chip, [("GPIO24", "H"), ("GPIO25", "H")])
    assert cnt(chip, Q2) == 2 and cnt(chip) == 0
    assert chip.arch.pie.ifr[5] & 2                                  # EQEP2_INT = PIE 5.2


# ------------------------------------------------------------------------------------------------ gate / reset
def test_clock_gate_stops_counting_and_timers(chip):
    w32(chip, Q1 + QUPRD, 0xFFFFFF)
    enable(chip, ctl=PCRM_MAX | QPEN | UTE)
    edges(chip, FWD)
    w(chip, PCLKCR1, 0x8000)                                         # EQEP1ENCLK off
    v = r32(chip, Q1 + QUTMR)
    edges(chip, FWD)
    chip.run_cycles(500)
    assert cnt(chip) == 4 and r32(chip, Q1 + QUTMR) == v
    w(chip, PCLKCR1, 0xC000)
    chip.run_cycles(100)
    assert r32(chip, Q1 + QUTMR) == v + 100


def test_mux_change_does_not_count(chip):
    w(chip, GPAMUX2_LO, 0)
    enable(chip)
    chip.pins.set_external(A, "H")
    w(chip, GPAMUX2_LO, 0x5500)                                      # muxed while high: resampled, no edge
    assert cnt(chip) == 0
    edges(chip, [(B, "H")])
    assert cnt(chip) == 1


def test_chip_reset_cancels_events(chip):
    enable(chip, ctl=PCRM_MAX | QPEN | UTE | WDE)
    w32(chip, Q1 + QUPRD, 100)
    w(chip, Q1 + QWDPRD, 3)
    blk = chip.peripherals["EQEP1"]
    assert [e for e in chip.sched.pending() if getattr(e.cb, "__self__", None) is blk]
    chip.reset()
    assert not [e for e in chip.sched.pending() if getattr(e.cb, "__self__", None) is blk]
    assert r(chip, Q1 + QEPCTL) == 0 and cnt(chip) == 0


# ------------------------------------------------------------------------------------------------ sync output
def test_position_compare_sync_output_on_index_pin(chip):
    SOEN, SPSEL, PCPOL = 1 << 13, 1 << 12, 1 << 13
    enable(chip, dec=SOEN)
    assert chip.pins.get(I).direction == "out" and chip.pins.get(I).level() == "L"
    w32(chip, Q1 + QPOSCMP, 2)
    w(chip, Q1 + QPOSCTL, PCE_EN | 9)                               # PCSPW = 9: 4 x 10 SYSCLKOUT
    edges(chip, [(A, "H")])
    t0 = chip.now_cycles()
    edges(chip, [(B, "H")], gap=1)
    assert chip.pins.get(I).level() == "H"
    chip.run_cycles(38)
    assert chip.pins.get(I).level() == "H"
    chip.run_cycles(4)
    assert chip.pins.get(I).level() == "L"
    assert chip.now_cycles() > t0
    assert r32(chip, Q1 + QPOSCNT) == 2                              # own pulse is not an index event
    w(chip, Q1 + QPOSCTL, PCE_EN | PCPOL | 9)                       # active low: idles high
    assert chip.pins.get(I).level() == "H"
    w(chip, Q1 + QDECCTL, SOEN | SPSEL)                              # move to the strobe pin
    assert chip.pins.get(S).direction == "out" and chip.pins.get(I).direction == "in"
    w(chip, Q1 + QDECCTL, 0)
    assert chip.pins.get(S).direction == "in"
