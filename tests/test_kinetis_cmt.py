"""Kinetis CMT (chipem/periph/kinetis/cmt.py): carrier generator and modulator timing, baseband / FSK / extended space
modes, the CMT_IRO pin (IROPEN, IROL, CMTPOL), EOCF clearing, IRQ / DMA requests, and the cmt_nec sample (NEC frame
decoded back from the modulation cycles and the carrier measured on the pin)."""
from pathlib import Path

import pytest

from chipem.session import Session

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "firmware/samples/build/button_mirror.elf"
pytestmark = pytest.mark.skipif(not FW.exists(), reason="sample firmware not built")

CGH1, CGL1, CGH2, CGL2, OC, MSC, CMD1, CMD2, CMD3, CMD4, PPS, DMA = range(12)
MCGEN, EOCIE, FSK, BASE, EXSPC, EOCF = 1, 2, 4, 8, 0x10, 0x80
IROL, CMTPOL, IROPEN = 0x80, 0x40, 0x20


def _session(mux=True):
    s = Session("mk22fn1m0vll12", FW, clock_hz=120e6)
    s.run_for(50e3)
    chip = s.chip
    bus = chip.bus
    bus.write(0x40047034, 4, bus.read(0x40047034, 4) | (1 << 2))         # SIM_SCGC4.CMT
    bus.write(0x40047038, 4, bus.read(0x40047038, 4) | (1 << 12))        # SIM_SCGC5.PORTD
    if mux:
        chip.peripherals["PORTD"].write(4 * 7, 4, 2 << 8)                # PTD7 = CMT_IRO
    cmt = chip.peripherals["CMT"]
    cmt.write(PPS, 1, 14)                                                 # bus 120 MHz / 15 = 8 MHz
    return s, chip, cmt


def _cmd(cmt, mark, space):
    cmt.write(CMD1, 1, mark >> 8)
    cmt.write(CMD2, 1, mark & 0xFF)
    cmt.write(CMD3, 1, space >> 8)
    cmt.write(CMD4, 1, space & 0xFF)


def _edges(chip, pin="PTD7"):
    ev = []
    chip.pins.subscribe(lambda e: ev.append((e.t_ns, e.new)) if e.field == "level" and e.pin == pin else None)
    return ev


def test_iro_latch_polarity_and_enable():
    s, chip, cmt = _session()
    pin = chip.pins.get("PTD7")
    assert pin.direction == "in"                               # IROPEN = 0: not driven
    cmt.write(OC, 1, IROPEN)                                   # active low, latch 0 -> inactive = high
    assert pin.direction == "out" and pin.level() == "H"
    cmt.write(OC, 1, IROPEN | IROL)
    assert pin.level() == "L" and cmt.read(OC, 1) & IROL
    cmt.write(OC, 1, IROPEN | IROL | CMTPOL)                   # active high
    assert pin.level() == "H"
    cmt.write(OC, 1, IROL | CMTPOL)
    assert pin.direction == "in"


def test_time_mode_carrier_and_mark_space():
    s, chip, cmt = _session()
    ev = _edges(chip)
    cmt.write(CGH1, 1, 2)
    cmt.write(CGL1, 1, 6)                                      # 8 x 125 ns = 1 us carrier, 25 % duty
    cmt.write(OC, 1, IROPEN | CMTPOL)
    _cmd(cmt, 9, 5)                                            # mark 10 us, space 5 us
    assert cmt.carrier_hz() == pytest.approx(1e6) and cmt.cmtclk_hz() == pytest.approx(8e6)
    t0 = chip.now_ns()
    cmt.write(MSC, 1, MCGEN)
    assert cmt.read(MSC, 1) & EOCF                             # initial cycle begins
    s.run_for(40e3)
    st, me, end, kind = cmt.cycles[0]
    delay = (14 + 2) / 120e6 * 1e9                             # PPSDIV + 2 bus clocks (Table 41-5)
    assert st - t0 == pytest.approx(delay, abs=10) and kind == "time"
    assert me - st == pytest.approx(10_000, abs=1) and end - me == pytest.approx(5_000, abs=1)
    rises = [t for t, v in ev if v == "H" and st <= t < me]
    falls = [t for t, v in ev if v == "L" and st <= t <= me]
    assert len(rises) == 10 and all(b - a == pytest.approx(1000, abs=1) for a, b in zip(rises, rises[1:]))
    assert falls[0] - rises[0] == pytest.approx(250, abs=1)
    assert not [t for t, v in ev if v == "H" and me <= t < end]    # space: no carrier
    assert cmt.cycles[1][0] == pytest.approx(end, abs=1)


def test_eocf_clear_irq_and_dma(monkeypatch):
    import chipem.periph.kinetis.cmt as mod
    reqs = []
    monkeypatch.setattr(mod, "dma_request", lambda chip, src: reqs.append(src) or True)
    s, chip, cmt = _session(mux=False)
    cmt.write(CGH1, 1, 1)
    cmt.write(CGL1, 1, 1)
    _cmd(cmt, 4, 4)
    cmt.write(MSC, 1, MCGEN | EOCIE)
    assert cmt._line                                          # CMT interrupt requested
    cmt.write(CMD2, 1, 4)                                      # without an MSC read first: stays set
    assert cmt.read(MSC, 1) & EOCF
    cmt.write(CMD2, 1, 4)                                      # MSC read, then CMD2 access: cleared
    assert not cmt.read(MSC, 1) & EOCF and not cmt._line
    cmt.write(DMA, 1, 1)
    s.run_for(20e3)
    assert reqs and set(reqs) == {47} and not cmt._line        # routed to the DMA instead of the IRQ


def test_baseband_extended_space_and_stop():
    s, chip, cmt = _session()
    ev = _edges(chip)
    cmt.write(OC, 1, IROPEN | CMTPOL)
    _cmd(cmt, 19, 10)                                          # 20 us mark, 10 us space
    cmt.write(MSC, 1, MCGEN | BASE)
    s.run_for(35e3)
    st, me, end, kind = cmt.cycles[0]
    assert kind == "baseband" and me - st == pytest.approx(20_000, abs=1)
    hi = [(t, v) for t, v in ev if st - 1 <= t <= end + 1]
    assert hi[0] == (pytest.approx(st, abs=1), "H") and hi[1] == (pytest.approx(me, abs=1), "L")
    cmt.write(MSC, 1, MCGEN | BASE | EXSPC)
    s.run_for(70e3)
    kinds = [c[3] for c in cmt.cycles]
    assert "space" in kinds
    sp = [c for c in cmt.cycles if c[3] == "space"][0]
    assert sp[1] == sp[0] and sp[2] - sp[0] == pytest.approx(30_000, abs=1)   # mark + space, no mark
    cmt.write(MSC, 1, BASE)                                    # MCGEN cleared: the running cycle finishes
    assert cmt.running
    s.run_for(40e3)
    assert not cmt.running and chip.pins.get("PTD7").level() == "L"
    n = len(cmt.cycles)
    s.run_for(40e3)
    assert len(cmt.cycles) == n


def test_fsk_alternates_carriers():
    s, chip, cmt = _session(mux=False)
    cmt.write(CGH1, 1, 1)
    cmt.write(CGL1, 1, 2)                                      # 375 ns
    cmt.write(CGH2, 1, 3)
    cmt.write(CGL2, 1, 1)                                      # 500 ns
    _cmd(cmt, 3, 2)                                            # mark 4 carrier periods, space 2
    cmt.write(MSC, 1, MCGEN | FSK)
    s.run_for(20e3)
    c = list(cmt.cycles)[:4]
    assert [k for *_x, k in c] == ["fsk", "fsk2", "fsk", "fsk2"]
    assert c[0][1] - c[0][0] == pytest.approx(4 * 375, abs=1) and c[0][2] - c[0][1] == pytest.approx(750, abs=1)
    assert c[1][1] - c[1][0] == pytest.approx(4 * 500, abs=1) and c[1][2] - c[1][1] == pytest.approx(1000, abs=1)


def test_cmt_nec_sample_frame():
    elf = ROOT / "firmware/samples/build/cmt_nec.elf"
    if not elf.exists():
        pytest.skip("cmt_nec not built")
    s = Session("mk22fn1m0vll12", elf, clock_hz=120e6)
    ev = _edges(s.chip)
    s.run_for(75e6)
    assert s.chip.uart_out("UART0").decode() == "NEC 04 08 sent, 22 cycles\r\n"
    cyc = list(s.chip.peripherals["CMT"].cycles)
    assert len(cyc) == 34
    carrier = 1e9 / (8e6 / 210)                                 # 38.095 kHz period
    mark0, space0 = cyc[0][1] - cyc[0][0], cyc[0][2] - cyc[0][1]
    assert 9000e3 <= mark0 <= 9000e3 + carrier and 4500e3 - 1 <= space0 <= 4500e3 + carrier
    bits = 0
    for i, (st, me, end, _k) in enumerate(cyc[1:33]):
        assert 560e3 <= me - st <= 560e3 + carrier
        sp = end - me
        assert sp == pytest.approx(560e3, abs=carrier) or sp == pytest.approx(1690e3, abs=carrier)
        bits |= (1 if sp > 1000e3 else 0) << i
    assert bits & 0xFF == 0x04 and (bits >> 8) & 0xFF == 0xFB and (bits >> 16) & 0xFF == 0x08
    assert (bits >> 24) & 0xFF == 0xF7
    rises = [t for t, v in ev if v == "H" and cyc[0][0] <= t < cyc[0][1]]
    per = [b - a for a, b in zip(rises, rises[1:])]
    assert len(rises) > 300 and min(per) == pytest.approx(carrier, abs=1) and max(per) == pytest.approx(carrier, abs=1)
    s.close()
