"""Kinetis I2S/SAI (chipem/periph/kinetis/sai.py): MCLK divider, bit clock and frame sync timing on the pins, FIFO
flags and watermarks, underrun (FEF, repeat last word), word start flag, word masks, RX synchronous to TX, a slave
receiver clocked from the pins, DMA requests, the word-level fast path without pins, the i2s_codec part and the
i2s_tone sample board.  The firmware (button_mirror) only idles; the tests drive the SAI registers directly."""
from pathlib import Path

import pytest

from chipem.session import Session

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "firmware/samples/build/button_mirror.elf"
pytestmark = pytest.mark.skipif(not FW.exists(), reason="sample firmware not built")

TCSR, TCR1, TCR2, TCR3, TCR4, TCR5, TDR0, TFR0, TMR = 0x0, 0x4, 0x8, 0xC, 0x10, 0x14, 0x20, 0x40, 0x60
RCSR, RCR1, RCR2, RCR3, RCR4, RCR5, RDR0, RFR0, RMR = 0x80, 0x84, 0x88, 0x8C, 0x90, 0x94, 0xA0, 0xC0, 0xE0
MCR, MDR = 0x100, 0x104
TE, BCE, FR, WSF, SEF, FEF, FWF, FRF = 1 << 31, 1 << 28, 1 << 25, 1 << 20, 1 << 19, 1 << 18, 1 << 17, 1 << 16
FRDE, FRIE, MOE, DUF = 1, 1 << 8, 1 << 30, 1 << 31
NS = 1e9 / 120e6
PINS = {"PTB18": 4, "PTB19": 4, "PTC1": 6}                  # TX_BCLK, TX_FS, TXD0


def cr2(div=0, bcp=0, msel=0, bcd=1, sync=0):
    return sync << 30 | msel << 26 | bcp << 25 | bcd << 24 | div


def cr4(frsz=1, sywd=0, mf=1, fse=0, fsp=0, fsd=1):
    return frsz << 16 | sywd << 8 | mf << 4 | fse << 3 | fsp << 1 | fsd


def cr5(wnw=7, w0w=7, fbt=7):
    return wnw << 24 | w0w << 16 | fbt << 8


def _session(pins=PINS):
    s = Session("mk22fn1m0vll12", FW, clock_hz=120e6)
    s.run_for(50e3)
    chip = s.chip
    bus = chip.bus
    bus.write(0x40047038, 4, bus.read(0x40047038, 4) | (0x1F << 9))      # SIM_SCGC5: PORTA-E
    bus.write(0x4004703C, 4, bus.read(0x4004703C, 4) | (1 << 15))        # SIM_SCGC6.I2S
    for pin, alt in pins.items():
        chip.peripherals[f"PORT{pin[2]}"].write(4 * int(pin[3:]), 4, alt << 8)
    return s, chip, chip.peripherals["I2S0"]


def _edges(chip, names):
    ev = []
    chip.pins.subscribe(lambda e: ev.append((e.t_ns, e.pin, e.new)) if e.field == "level" and e.pin in names
                        else None)
    return ev


def _of(ev, pin, t0):
    return [(round((t - t0) / NS), lvl) for t, p, lvl in ev if p == pin]


def test_mclk_divider_duf_and_mics_lock():
    s, chip, sai = _session({})
    assert sai.read(MCR, 4) == 0 and sai.read(TCSR, 4) == 0
    sai.write(MDR, 4, (31 << 12) | 1874)                     # MOE = 0: no DUF
    assert not sai.read(MCR, 4) & DUF
    sai.write(MCR, 4, MOE)                                   # SYSCLK = 120 MHz
    assert sai.mclk_hz() == pytest.approx(120e6 * 32 / 1875)
    sai.write(MDR, 4, (0 << 12) | 9)                         # / 10 while enabled: DUF for 2 MCLK periods
    assert sai.read(MCR, 4) & DUF and sai.mclk_hz() == pytest.approx(12e6)
    sai.write(MDR, 4, (0 << 12) | 4)                         # blocked while DUF
    assert sai.read(MDR, 4) == 9
    s.run_for(1000)
    assert not sai.read(MCR, 4) & DUF
    sai.write(MCR, 4, MOE | (3 << 24))                       # MICS cannot change while MOE = 1
    assert (sai.read(MCR, 4) >> 24) & 3 == 0
    sai.write(MCR, 4, 0)
    sai.write(MCR, 4, 3 << 24)                               # MCGPLLCLK selected (divider off: MCLK pin input)
    assert (sai.read(MCR, 4) >> 24) & 3 == 3 and sai.mclk_hz() == 0.0


def test_master_tx_timing_frame_sync_and_bits():
    s, chip, sai = _session()
    ev = _edges(chip, PINS)
    sai.write(TCR2, 4, cr2(div=29))                         # bus 120 MHz / 60 = 2 MHz bit clock, BCP = 0
    sai.write(TCR3, 4, 1 << 16)
    sai.write(TCR4, 4, cr4(frsz=1, sywd=0))
    sai.write(TCR5, 4, cr5())
    assert sai.read(TCSR, 4) & (FRF | FWF) == FRF | FWF      # empty FIFO: watermark and warning
    for w in (0xA5, 0x3C, 0x81, 0x7E):
        sai.write(TDR0, 4, w)
    assert sai.read(TFR0, 4) == 4 << 16
    t0 = chip.now_ns()
    sai.write(TCSR, 4, TE)
    assert sai.read(TCSR, 4) & BCE
    assert chip.pins.get("PTB18").direction == "out" and chip.pins.get("PTB19").direction == "out"
    s.run_for(16 * 500 + 3000)
    bclk = _of(ev, "PTB18", t0)
    rises = [t for t, lvl in bclk if lvl == "H"]
    assert rises[:3] == [30, 90, 150] and all(b - a == 60 for a, b in zip(rises, rises[1:]))   # 2 MHz
    fs = [x for x in _of(ev, "PTB19", t0) if x[0] > 0]                           # (idle level at enable)
    assert fs[0] == (30 + 4 * 60, "H") and fs[1] == (30 + 5 * 60, "L")           # 4 idle bit clocks, 1 bit wide
    assert fs[2] == (30 + 4 * 60 + 16 * 60, "H")                                 # next frame 16 bits later
    tx = _of(ev, "PTC1", t0)
    bits = []
    for k in range(16):                                       # level after each drive (rising) edge of the frame
        t = 30 + (4 + k) * 60
        lv = [lvl for tt, lvl in tx if tt <= t]
        bits.append(1 if lv and lv[-1] == "H" else 0)
    assert int("".join(map(str, bits[:8])), 2) == 0xA5 and int("".join(map(str, bits[8:])), 2) == 0x3C
    assert [w for _t, w in sai.tx.words[0]][:3] == [0xA5, 0x3C, 0x81]


def test_underrun_repeats_last_word_and_fef_and_wsf():
    s, chip, sai = _session({})
    sai.write(TCR2, 4, cr2(div=29))
    sai.write(TCR3, 4, (1 << 16) | 1)                        # WDFL = 1: word start flag on the second word
    sai.write(TCR4, 4, cr4())
    sai.write(TCR5, 4, cr5())
    sai.write(TDR0, 4, 0x11)
    sai.write(TCSR, 4, TE | (1 << 10))                       # FEIE
    s.run_for(10000)
    st = sai.read(TCSR, 4)
    assert st & FEF and st & WSF
    words = [w for _t, w in sai.tx.words[0]]
    assert words[0] == 0x11 and set(words[1:]) == {0x11}     # the last valid word is repeated
    assert sai.tx._line                                       # I2S0_Tx IRQ line (FEF & FEIE)
    sai.write(TCSR, 4, TE | FR | FEF | WSF)                  # FIFO reset, clear FEF
    sai.write(TDR0, 4, 0x22)
    sai.write(TDR0, 4, 0x33)
    s.run_for(16 * 500)
    assert [w for _t, w in sai.tx.words[0]][-2:] in ([0x22, 0x33], [0x33, 0x33])


def test_masked_word_tristates_and_te_clear_finishes_frame():
    s, chip, sai = _session()
    sai.write(TCR2, 4, cr2(div=29))
    sai.write(TCR3, 4, 1 << 16)
    sai.write(TCR4, 4, cr4())
    sai.write(TCR5, 4, cr5())
    sai.write(TMR, 4, 0b10)                                  # word 1 masked
    for w in (1, 2, 3, 4):
        sai.write(TDR0, 4, w)
    ev = _edges(chip, PINS)
    sai.write(TCSR, 4, TE)
    s.run_for(40 * 500)
    dirs = []
    chip.pins.subscribe(lambda e: dirs.append(e.new) if e.field == "direction" and e.pin == "PTC1" else None)
    assert [w for _t, w in sai.tx.words[0]][:3] == [1, 2, 3]                     # masked words read nothing
    s.run_for(8 * 500)
    assert "in" in dirs                                       # TXD0 tristated during the masked word
    sai.write(TCSR, 4, 0)
    assert sai.read(TCSR, 4) & TE                             # stays set until the end of the frame
    s.run_for(17 * 500)
    assert not sai.read(TCSR, 4) & TE
    n = len(ev)
    s.run_for(5000)
    assert len([e for e in ev[n:] if e[1] == "PTB19"]) == 0   # no more frames
    _ = n


def test_rx_synchronous_loopback_on_a_net():
    s, chip, sai = _session({**PINS, "PTA15": 6})            # PTA15 = I2S0_RXD0
    b = s.ensure_board()
    b.add_net("LOOP", [("A", "PTC1"), ("A", "PTA15")])
    sai.write(TCR2, 4, cr2(div=29, bcp=1))
    sai.write(TCR3, 4, 1 << 16)
    sai.write(TCR4, 4, cr4(frsz=1, sywd=7, fse=1, fsp=1))
    sai.write(TCR5, 4, cr5())
    sai.write(RCR2, 4, cr2(div=29, bcp=1, sync=1))
    sai.write(RCR3, 4, 1 << 16)
    sai.write(RCR4, 4, cr4(frsz=1, sywd=7, fse=1, fsp=1))
    sai.write(RCR5, 4, cr5())
    data = [0x12, 0x34, 0x56, 0x78, 0x9A, 0xBC]
    for w in data:
        sai.write(TDR0, 4, w)
    sai.write(RCSR, 4, TE)
    assert not sai.rx.enabled                                 # synchronous: waits for the transmitter
    sai.write(TCSR, 4, TE)
    assert sai.rx.enabled
    s.run_for(3 * 16 * 500 + 4000)
    st = sai.read(RCSR, 4)
    assert st & FRF and (sai.read(RFR0, 4) >> 16) - (sai.read(RFR0, 4) & 0xF) == 6
    got = [sai.read(RDR0, 4) for _ in range(6)]
    assert got == data
    assert not sai.read(RCSR, 4) & FRF


def test_slave_receiver_clocked_from_pins():
    pins = {**PINS, "PTC9": 4, "PTC10": 4, "PTA15": 6}      # RX_BCLK, RX_FS, RXD0
    s, chip, sai = _session(pins)
    b = s.ensure_board()
    b.add_net("BCLK", [("A", "PTB18"), ("A", "PTC9")])
    b.add_net("FS", [("A", "PTB19"), ("A", "PTC10")])
    b.add_net("SD", [("A", "PTC1"), ("A", "PTA15")])
    sai.write(TCR2, 4, cr2(div=29))
    sai.write(TCR3, 4, 1 << 16)
    sai.write(TCR4, 4, cr4())
    sai.write(TCR5, 4, cr5())
    sai.write(RCR2, 4, cr2(bcd=0))                           # external bit clock
    sai.write(RCR3, 4, 1 << 16)
    sai.write(RCR4, 4, cr4(fsd=0))                           # external frame sync
    sai.write(RCR5, 4, cr5())
    assert chip.pins.get("PTC9").direction == "in" and chip.pins.get("PTC10").direction == "in"
    for w in (0xC3, 0x5A, 0x0F, 0xF0):
        sai.write(TDR0, 4, w)
    sai.write(RCSR, 4, TE)
    sai.write(TCSR, 4, TE)
    s.run_for(2 * 16 * 500 + 4000)
    got = [w for _t, w in sai.rx.words[0]]
    assert got[:4] == [0xC3, 0x5A, 0x0F, 0xF0]


def test_dma_request_on_frf(monkeypatch):
    import chipem.periph.kinetis.sai as mod
    reqs = []
    monkeypatch.setattr(mod, "dma_request", lambda chip, src: reqs.append(src) or True)
    s, chip, sai = _session({})
    sai.write(TCR3, 4, 1 << 16)
    sai.write(TCSR, 4, FRDE)
    assert reqs and reqs[-1] == 13
    sai.write(RCR3, 4, 1 << 16)
    sai.write(RCSR, 4, FRDE)
    assert 12 not in reqs                                     # RX FIFO empty: no request


def test_word_level_fast_path_without_pins():
    s, chip, sai = _session({})
    sai.write(TCR2, 4, cr2(div=29))
    sai.write(TCR3, 4, 1 << 16)
    sai.write(TCR4, 4, cr4())
    sai.write(TCR5, 4, cr5())
    for w in range(1, 9):
        sai.write(TDR0, 4, w)
    t0 = chip.now_ns()
    sai.write(TCSR, 4, TE)
    assert sai.tx.word_mode
    s.run_for(4 * 16 * 500 + 4000)
    tw = [(t - t0, w) for t, w in sai.tx.words[0]][:4]
    assert [w for _t, w in tw] == [1, 2, 3, 4]
    assert tw[1][0] - tw[0][0] == pytest.approx(8 * 500, abs=2 * NS)          # one word = 8 bit clocks


def test_codec_part_records_and_plays_back():
    s, chip, sai = _session({**PINS, "PTA15": 6})
    b = s.ensure_board()
    b.add_net("BCLK", [("A", "PTB18")])
    b.add_net("WS", [("A", "PTB19")])
    b.add_net("SDIN", [("A", "PTC1")])
    b.add_net("SDOUT", [("A", "PTA15")])
    codec = b.add_part("CODEC", "i2s_codec", {"bits": 16, "pattern": "1000,-1000"},
                       {"bclk": "BCLK", "ws": "WS", "sdin": "SDIN", "sdout": "SDOUT"})
    i2s = dict(frsz=1, sywd=15, fse=1, fsp=1)
    for base, sync in ((TCR2, 0), (RCR2, 1)):
        sai.write(base, 4, cr2(div=14, bcp=1, sync=sync))   # 4 MHz bit clock
        sai.write(base + 4, 4, 1 << 16)
        sai.write(base + 8, 4, cr4(**i2s))
        sai.write(base + 12, 4, cr5(wnw=15, w0w=15, fbt=15))
    for w in (0x1111, 0xEEEE, 0x2222, 0xDDDD, 0x3333, 0xCCCC):
        sai.write(TDR0, 4, w)
    sai.write(RCSR, 4, TE)
    sai.write(TCSR, 4, TE)
    s.run_for(4 * 32 * 250 + 3000)
    assert codec.frames[:3] == [(0x1111, -0x1112), (0x2222, -0x2223), (0x3333, -0x3334)]
    rx = [w for _t, w in sai.rx.words[0]]
    assert rx[:4] == [1000, 0x10000 - 1000, 1000, 0x10000 - 1000]


def test_i2s_tone_board():
    elf = ROOT / "firmware/samples/build/i2s_tone.elf"
    if not elf.exists():
        pytest.skip("i2s_tone not built")
    from chipem.board import load_board
    s = Session.from_board(load_board(ROOT / "boards" / "k22_i2s_codec.yaml"))
    s.run_for(15e6)
    a = s.board.chips["A"]
    out = a.uart_out("UART0").decode()
    assert out.startswith("I2S 8000 Hz: tx ") and " rx 128 ok\r\n" in out
    assert "RX 1234 EDCC 5678 A988 1234 EDCC 5678 A988\r\n" in out
    codec = s.board.parts["CODEC"]
    fr = codec.frames
    assert len(fr) >= 64 and fr[0] == (0, 0) and fr[1] == (7071, -7071) and fr[2] == (10000, -10000)
    sai = a.peripherals["I2S0"]
    assert sai.mclk_hz() == pytest.approx(2.048e6) and sai.tx.bclk_hz() == pytest.approx(256e3)
    rises = [e.t_ns for e in s.log if e.kind == "pin" and e.fields.get("pin") == "PTB19"
             and e.fields.get("field") == "level" and e.fields.get("new") == "H"]
    gaps = [b - a for a, b in zip(rises, rises[1:])]
    assert len(gaps) > 60 and sorted(gaps)[len(gaps) // 2] == pytest.approx(125e3, rel=1e-3)   # 8 kHz frames
    blocks = [e for e in s.log if e.kind == "i2s"]
    assert blocks and blocks[0].fields["dir"] in ("tx", "rx") and blocks[0].fields["frames"] == 64
    s.close()
