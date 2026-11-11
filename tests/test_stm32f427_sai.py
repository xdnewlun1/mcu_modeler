"""STM32F427/F437 SAI1 (chipem/periph/stm32/sai.py, RM0090 Rev 22 ch. 29): SAI_CK from PLLSAI (RCC_DCKCFGR), MCLK /
SCK / FS of the clock generator (29.9), the I2S frame on the pins (29.7/29.8), FIFO levels and FREQ (29.10), underrun
/ overrun (29.13.1), WCKCFG (29.13.5), SAIxEN disable at the end of the frame (29.15), synchronous block B (29.5) with
AFSDET, MUTE / MUTEDET (29.12.1), IRQ 87 and DMA2 (Table 44), plus the f42x_sai_tone sample on the codec board.

The core runs the STM32F405 uart_echo sample (same die family register map; it only touches PA9/PA10); the tests
drive SAI1 from Python.  PLLSAI: HSE 8 MHz / PLLM 8 x 344 / 7 = 49.142857 MHz."""
from __future__ import annotations

from pathlib import Path

import pytest

from chipem.periph.stm32.sai import rx_flth, tx_flth
from chipem.session import Session

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "firmware" / "samples_stm32" / "build" / "uart_echo.elf"
SAMPLE = ROOT / "firmware" / "samples_stm32_variants" / "build" / "f42x_sai_tone.elf"
BOARD = ROOT / "boards" / "stm32f427vg_sai_codec.yaml"
pytestmark = pytest.mark.skipif(not FW.exists(), reason="firmware/samples_stm32 not built")

RCC, GPIOE, SAI, DMA2 = 0x40023800, 0x40021000, 0x40015800, 0x40026400
A, B = 0, 0x20
CR1, CR2, FRCR, SLOTR, IM, SR, CLRFR, DR = 4, 8, 0xC, 0x10, 0x14, 0x18, 0x1C, 0x20
OVRUDR, MUTEDET, WCKCFG, FREQ, AFSDET, LFSDET = 1, 2, 4, 8, 0x20, 0x40
SAIEN, DMAEN, DS16, CKSTR, SYNCEN, NODIV = 1 << 16, 1 << 17, 4 << 5, 1 << 9, 1 << 10, 1 << 19
SLAVE_RX = 3
SAI_IRQ = 16 + 87
CK = 344e6 / 7                      # PLLSAI_Q
FS_HZ = CK / 4 / 256                # MCKDIV 2: MCLK = CK / 4, FS = MCLK / 256 = 47.991 kHz
I2S_FRCR = 31 | 15 << 8 | 1 << 16 | 1 << 18           # FRL 31, FSALL 15, FSDEF, FSOFF, FSPOL 0 (HAL I2S standard)


class Rig:
    def __init__(self, pins=True, loop=False):
        self.ses = Session("stm32f427vg", FW)
        self.chip = c = self.ses.chip
        self.w(RCC, self.r(RCC) | 1 << 16)                                    # HSEON
        self.w(RCC + 4, 8 | 50 << 6 | 1 << 22 | 4 << 24)                      # PLLM 8, source HSE
        self.w(RCC + 0x88, 344 << 6 | 7 << 24 | 2 << 28)                      # PLLSAIN 344, PLLSAIQ 7
        self.w(RCC, self.r(RCC) | 1 << 28)                                    # PLLSAION
        self.w(RCC + 0x30, self.r(RCC + 0x30) | 1 << 4 | 1 << 22)             # GPIOE, DMA2
        self.w(RCC + 0x44, self.r(RCC + 0x44) | 1 << 22)                      # SAI1EN
        if pins:
            for p in (2, 3, 4, 5, 6):
                self.w(GPIOE, (self.r(GPIOE) & ~(3 << 2 * p)) | 2 << 2 * p)
                self.w(GPIOE + 0x20, (self.r(GPIOE + 0x20) & ~(0xF << 4 * p)) | 6 << 4 * p)
        if loop:
            brd = self.ses.ensure_board()
            cid = brd.ids()[0]
            brd.connect([(cid, "PE6"), (cid, "PE3")], name="LOOP")           # SD_A -> SD_B
        self.sai = c.peripherals["SAI1"]
        self.edges: list = []
        c.pins.subscribe(lambda e: self.edges.append((e.t_ns, e.pin, e.new))
                         if e.field == "level" and e.pin in ("PE2", "PE3", "PE4", "PE5", "PE6") else None)

    def r(self, a):
        return self.chip.bus.read(a, 4)

    def w(self, a, v):
        self.chip.bus.write(a, 4, v)

    def i2s(self, blk=A, cr1=DS16 | CKSTR | 2 << 20, fth=1):
        self.w(SAI + blk + FRCR, I2S_FRCR)
        self.w(SAI + blk + SLOTR, 1 << 8 | 3 << 16)
        self.w(SAI + blk + CR2, fth)
        self.w(SAI + blk + CR1, cr1)

    def run(self, ns):
        self.ses.run_for(ns)

    def times(self, pin, lvl):
        return [t for t, p, n in self.edges if p == pin and n == lvl]

    def bits(self):
        """(FS, SD) at every rising SCK edge (the sampling edge with CKSTR = 1)."""
        lv = {"PE4": "?", "PE6": "?"}
        out = []
        for _t, p, n in self.edges:
            if p == "PE5" and n == "H":
                out.append((lv["PE4"], lv["PE6"]))
            elif p in lv:
                lv[p] = n
        return out


def words(bits, start, n=16):
    return int("".join("1" if b[1] == "H" else "0" for b in bits[start:start + n]), 2)


def test_flth_tables():
    """29.17.6 FLTH encodings: transmitter <= 1/4, <= 1/2 ...; receiver < 1/4, < 1/2 ..."""
    assert [tx_flth(n) for n in range(9)] == [0, 1, 1, 2, 2, 3, 3, 4, 5]
    assert [rx_flth(n) for n in range(9)] == [0, 1, 2, 2, 3, 3, 4, 4, 5]


def test_clock_tree_and_i2s_frame_on_pins():
    g = Rig()
    assert g.chip.peripherals["RCC"].sai_clk_hz("A") == pytest.approx(CK)
    g.i2s()
    for v in (0x1234, 0xABCD, 0x8001, 0x7FFE):
        g.w(SAI + DR, v)
    assert g.r(SAI + SR) == 2 << 16                      # FLTH 2 (3..4 words), FREQ 0 (FTH 1/4)
    g.w(SAI + CR1, g.r(SAI + CR1) | SAIEN)
    sub = g.sai.sub("A")
    assert sub.mclk_hz() == pytest.approx(CK / 4) and sub.sck_hz() == pytest.approx(32 * FS_HZ)
    g.run(130e3)
    fs = g.times("PE4", "L")
    per = [b - a for a, b in zip(fs, fs[1:])]
    assert len(per) >= 4 and sum(per) / len(per) == pytest.approx(1e9 / FS_HZ, rel=2e-3)     # 20.837 us
    sck = g.times("PE5", "H")
    assert (sck[-1] - sck[0]) / (len(sck) - 1) == pytest.approx(1e9 / (32 * FS_HZ), rel=2e-3)  # 651 ns
    bits = g.bits()
    first = next(i for i, b in enumerate(bits) if b[0] == "L")     # FS falls one bit before the left MSB
    assert all(b[0] == "L" for b in bits[first:first + 16]) and bits[first + 16][0] == "H"
    assert [words(bits, first + 1 + 16 * k) for k in range(4)] == [0x1234, 0xABCD, 0x8001, 0x7FFE]
    assert list(sub.tx_frames)[:2] == [(0x1234, 0xABCD), (0x8001, 0x7FFE)]
    # FIFO empty afterwards: underrun, zeros, FREQ (FTH 1/4)
    assert sub.underruns and g.r(SAI + SR) & (OVRUDR | FREQ) == OVRUDR | FREQ
    assert list(sub.tx_frames)[2] == (0, 0)


def test_fifo_threshold_freq_flush_and_full():
    g = Rig(pins=False)
    g.i2s(fth=2)                                         # TX: FREQ while FLTH <= 1/2
    sr = lambda: g.r(SAI + SR)                           # noqa: E731
    assert sr() & FREQ
    for n in range(1, 9):
        g.w(SAI + DR, n)
        assert (sr() >> 16) & 7 == tx_flth(n) and bool(sr() & FREQ) == (tx_flth(n) <= 2)
    g.w(SAI + DR, 99)                                    # full: ignored (29.17.8)
    assert list(g.sai.sub("A").fifo) == list(range(1, 9))
    g.w(SAI + CR2, 2 | 1 << 3)                           # FFLUSH (reads 0)
    assert (sr() >> 16) & 7 == 0 and g.r(SAI + CR2) == 2
    g.w(SAI + CR1, SLAVE_RX | DS16)                      # receiver, FTH 1/2: FREQ while FLTH >= 3
    g.w(SAI + CR2, 2)
    assert not sr() & FREQ and g.r(SAI + DR) == 0        # empty RX FIFO reads 0


def test_underrun_irq_and_clear():
    g = Rig(pins=False)
    g.i2s()
    g.w(SAI + DR, 1)
    g.w(SAI + DR, 2)
    g.w(SAI + IM, OVRUDR)
    g.w(SAI + CR1, g.r(SAI + CR1) | SAIEN)
    g.run(30e3)                                          # second frame underruns
    assert g.r(SAI + SR) & OVRUDR and SAI_IRQ in g.chip.arch.asserted
    g.w(SAI + CLRFR, OVRUDR)
    assert not g.r(SAI + SR) & OVRUDR and SAI_IRQ not in g.chip.arch.asserted
    assert g.r(SAI + CLRFR) == 0


def test_master_tx_waits_for_data_then_word_mode_timing():
    """Master TX: SCK at enable but the frame (FS) starts only with FIFO data (29.4); no pins muxed: one event per
    slot with the same timing; FREQ interrupts refill the FIFO."""
    g = Rig(pins=False)
    g.i2s()
    g.w(SAI + CR1, g.r(SAI + CR1) | SAIEN)
    g.run(50e3)
    sub = g.sai.sub("A")
    assert sub.running and not sub.tx_frames and not g.r(SAI + SR) & OVRUDR      # no frame, no underrun
    for v in range(8):
        g.w(SAI + DR, v)
    t0 = g.chip.now_ns()
    g.run(1e9 / FS_HZ * 4 + 2e3)
    assert list(sub.tx_frames)[:4] == [(0, 1), (2, 3), (4, 5), (6, 7)]
    assert g.chip.now_ns() - t0 > 4e9 / FS_HZ


def test_wckcfg_refuses_non_power_of_two_frame():
    g = Rig(pins=False)
    g.w(SAI + FRCR, 23 | 11 << 8)                        # 24-bit frame, NODIV = 0
    g.w(SAI + IM, WCKCFG)
    g.w(SAI + CR1, DS16 | SAIEN)
    assert not g.r(SAI + CR1) & SAIEN and g.r(SAI + SR) & WCKCFG and SAI_IRQ in g.chip.arch.asserted
    g.w(SAI + CLRFR, WCKCFG)
    g.w(SAI + CR1, DS16 | NODIV | SAIEN)                 # NODIV = 1: any frame length (29.7.1)
    assert g.r(SAI + CR1) & SAIEN and not g.r(SAI + SR) & WCKCFG
    assert g.sai.sub("A").sck_hz() == pytest.approx(CK)


def test_disable_completes_the_frame():
    g = Rig()
    g.i2s()
    for v in range(8):
        g.w(SAI + DR, v)
    g.w(SAI + CR1, g.r(SAI + CR1) | SAIEN)
    g.run(1e9 / FS_HZ * 1.5)                             # middle of the second frame
    g.w(SAI + CR1, g.r(SAI + CR1) & ~SAIEN)
    assert g.r(SAI + CR1) & SAIEN                        # still 1 until the frame ends (29.15)
    g.w(SAI + CR1, g.r(SAI + CR1) | SAIEN)               # setting it while it reads 1: ignored
    g.run(1e9 / FS_HZ * 0.6)
    assert not g.r(SAI + CR1) & SAIEN
    sub = g.sai.sub("A")
    assert len(sub.tx_frames) == 2 and "SCK" not in sub.drv
    n = len(g.edges)
    g.run(50e3)
    assert len(g.edges) == n                             # no more clock


def test_synchronous_receiver_loopback_overrun_and_afsdet():
    """Block B slave receiver synchronous with block A (SYNCEN = 01): it receives A's SD through a board net on
    SD_B; with its FIFO full the next slots are lost (OVRUDR) and storing resumes at the overrun slot (Figure 294)."""
    g = Rig(loop=True)
    g.i2s(blk=B, cr1=SLAVE_RX | DS16 | CKSTR | SYNCEN, fth=0)
    g.i2s()
    vals = [0x1111 * (k + 1) for k in range(12)]
    for v in vals[:8]:
        g.w(SAI + DR, v)
    g.w(SAI + B + CR1, g.r(SAI + B + CR1) | SAIEN)
    g.w(SAI + CR1, g.r(SAI + CR1) | SAIEN)
    g.run(1e9 / FS_HZ * 2.2)
    for v in vals[8:]:
        g.w(SAI + DR, v)
    g.run(1e9 / FS_HZ * 3)
    sb = g.sai.sub("B")
    assert list(sb.rx_frames)[:4] == [(0x1111, 0x2222), (0x3333, 0x4444), (0x5555, 0x6666), (0x7777, 0x8888)]
    assert g.r(SAI + B + SR) & OVRUDR and len(sb.fifo) == 8 and (g.r(SAI + B + SR) >> 16) & 7 == 5
    assert [g.r(SAI + B + DR) for _ in range(8)] == vals[:8]
    # B's frame length disagrees with A's (64 vs 32): A's next FS edge comes early -> AFSDET (29.13.2)
    g.w(SAI + B + CR1, g.r(SAI + B + CR1) & ~SAIEN)
    g.run(1e9 / FS_HZ)
    g.w(SAI + B + FRCR, 63 | 31 << 8 | 1 << 16 | 1 << 18)
    g.w(SAI + B + CLRFR, 0x7F)
    for v in range(8):
        g.w(SAI + DR, v)
    g.w(SAI + B + CR1, g.r(SAI + B + CR1) | SAIEN)
    g.run(1e9 / FS_HZ * 4)
    assert g.r(SAI + B + SR) & AFSDET


def test_mute_and_mutedet():
    g = Rig(loop=True)
    g.i2s(blk=B, cr1=SLAVE_RX | DS16 | CKSTR | SYNCEN, fth=0)
    g.w(SAI + B + CR2, 3 << 7)                           # MUTECNT 3
    g.i2s()
    g.w(SAI + CR2, 1 | 1 << 5)                           # MUTE: frames send 0, FIFO still popped (29.12.1)
    for v in range(1, 9):
        g.w(SAI + DR, v)
    g.w(SAI + B + IM, MUTEDET)
    g.w(SAI + B + CR1, g.r(SAI + B + CR1) | SAIEN)
    g.w(SAI + CR1, g.r(SAI + CR1) | SAIEN)
    g.run(1e9 / FS_HZ * 3.5)
    sa, sb = g.sai.sub("A"), g.sai.sub("B")
    assert list(sa.tx_frames)[:3] == [(0, 0)] * 3 and len(sa.fifo) <= 2      # popped although muted
    assert g.r(SAI + B + SR) & MUTEDET and SAI_IRQ in g.chip.arch.asserted


def test_dma2_feeds_block_a():
    """DMA2 stream 1 channel 0 = SAI1_A (RM0090 Table 44): memory to the FIFO while FREQ & DMAEN."""
    g = Rig(pins=False)
    buf = 0x20010000
    vals = [0x100 + k for k in range(16)]
    for k, v in enumerate(vals):
        g.chip.bus.write(buf + 4 * k, 4, v)
    s1 = DMA2 + 0x10 + 0x18 * 1
    g.w(s1 + 8, SAI + DR)                                # PAR
    g.w(s1 + 0xC, buf)                                   # M0AR
    g.w(s1 + 4, len(vals))                               # NDTR
    g.w(s1, 0 << 25 | 2 << 13 | 2 << 11 | 1 << 10 | 1 << 6 | 1)    # CH0, 32-bit, MINC, M2P, EN
    g.i2s(cr1=DS16 | CKSTR | 2 << 20 | DMAEN)
    g.w(SAI + CR1, g.r(SAI + CR1) | SAIEN)
    g.run(1e9 / FS_HZ * 9)
    assert list(g.sai.sub("A").tx_frames)[:8] == [(vals[2 * k], vals[2 * k + 1]) for k in range(8)]
    assert g.r(DMA2) & 1 << 11                           # LISR TCIF1


def test_rcc_reset_and_gate():
    g = Rig(pins=False)
    g.i2s()
    g.w(SAI + DR, 5)
    g.w(SAI + CR1, g.r(SAI + CR1) | SAIEN)
    g.w(RCC + 0x24, 1 << 22)                             # APB2RSTR.SAI1RST
    g.w(RCC + 0x24, 0)
    assert g.r(SAI + CR1) == 0x40 and g.r(SAI + SR) == FREQ and not g.sai.sub("A").running
    g.w(RCC + 0x44, g.r(RCC + 0x44) & ~(1 << 22))
    g.r(SAI + SR)
    assert any(e.kind == "warn" and "SAI1" in e.fields.get("msg", "") for e in g.chip.log)


@pytest.mark.skipif(not SAMPLE.exists(), reason="firmware/samples_stm32_variants not built")
def test_sample_tone_to_codec_board():
    """f42x_sai_tone on boards/stm32f427vg_sai_codec.yaml: the codec records the sine (left) / square (right) frames
    at 47.991 kHz, block B returns the codec's pattern, the feeder stops after 96 frames (one underrun interrupt) and
    both blocks end disabled."""
    from chipem.board import load_board
    from tests.test_stm32_samples import data_symbol
    s = Session.from_board(load_board(BOARD))
    s.run_for(3.5e6)
    chip = s.board.chips["A"]
    codec = s.board.parts["CODEC"]
    sine = [0, 1044, 2071, 3061, 4000, 4870, 5657, 6347]
    left = [f[0] for f in codec.frames]
    k = left.index(sine[1]) - 1                          # the codec records from its first full frame
    assert left[k:k + 8] == sine and len(codec.frames) >= 95
    assert all(f[1] in (4000, -4000) for f in codec.frames[k:k + 90])
    rd = lambda n: chip.bus.read(data_symbol(SAMPLE, n), 4)                        # noqa: E731
    assert rd("done") == 1 and rd("underrun_irqs") == 1 and rd("tx_words") == 192
    rx = [chip.bus.read(data_symbol(SAMPLE, "rx_buf") + 2 * i, 2) for i in range(12)]
    j = rx.index(4660)
    assert rx[j:j + 4] == [4660, -4660 & 0xFFFF, 22136, -22136 & 0xFFFF]
    sai = chip.peripherals["SAI1"]
    a = sai.sub("A")
    assert len(a.tx_frames) == 97 and a.tx_frames[-1] == (0, 0)
    assert not a.running and not sai.sub("B").running
    assert a.fs_hz() == pytest.approx(FS_HZ) and a.mclk_hz() == pytest.approx(CK / 4)       # 47.991 kHz, 12.29 MHz
    # FS period of the recorded run: 1 / 47.991 kHz
    fs = [e.t_ns for e in chip.log if e.kind == "pin" and e.fields.get("pin") == "PE4"
          and e.fields.get("new") == "L"]
    if len(fs) > 4:
        per = [b - a for a, b in zip(fs, fs[1:])][:50]
        assert sum(per) / len(per) == pytest.approx(1e9 / FS_HZ, rel=2e-3)
