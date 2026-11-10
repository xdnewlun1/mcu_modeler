"""STM32F4 I2S mode of SPI2/SPI3 and the I2S2ext full-duplex extension (chipem/periph/stm32/i2s.py, spi.py):
I2SxCLK from PLLI2S / I2S_CKIN, bit clock and Fs for I2SDIV/ODD/MCKOE/CHLEN, Philips / MSB / LSB / PCM waveforms on
the pins, 24/32-bit data in two DR accesses, CKPOL, CHSIDE, UDR / OVR / FRE, slave mode from the pins, DMA, IRQ, the
I2S2ext, the i2s_codec board part and the i2s_tone sample board.

The core runs uart_echo (it only touches PA9/PA10); the tests drive the registers from Python between board steps.
PLLI2S: HSI 16 MHz / PLLM 16 x N / R."""
from __future__ import annotations

from pathlib import Path

import pytest

from chipem.periph.stm32.i2s import Geo
from chipem.session import Session

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "firmware" / "samples_stm32" / "build" / "uart_echo.elf"
pytestmark = pytest.mark.skipif(not FW.exists(), reason="firmware/samples_stm32 not built")

RCC, GPIOB, GPIOC = 0x40023800, 0x40020400, 0x40020800
SPI2, I2S2EXT, DMA1 = 0x40003800, 0x40003400, 0x40026000
CR1, CR2, SR, DR, I2SCFGR, I2SPR = 0, 4, 8, 0xC, 0x1C, 0x20
RXNE, TXE, CHSIDE, UDR, OVR, BSY, FRE = 1, 2, 4, 8, 0x40, 0x80, 0x100
RXDMAEN, TXDMAEN, ERRIE, RXNEIE, TXEIE = 1, 2, 0x20, 0x40, 0x80
I2SMOD, I2SE = 1 << 11, 1 << 10
MASTER_TX, MASTER_RX, SLAVE_TX, SLAVE_RX = 2 << 8, 3 << 8, 0, 1 << 8
PHILIPS, MSB, LSB, PCM = 0, 1 << 4, 2 << 4, 3 << 4
SPI2_IRQ = 36


def cfg(mode=MASTER_TX, std=PHILIPS, datlen=0, chlen=0, ckpol=0, pcmsync=0):
    return I2SMOD | mode | std | datlen << 1 | chlen | ckpol << 3 | pcmsync << 7


class Rig:
    def __init__(self, n=200, r=2, pins=True, ext_pin=True):
        self.ses = Session("stm32f405zg", FW)
        self.chip = c = self.ses.chip
        self.board = self.ses.ensure_board()
        self.w(RCC + 0x30, self.r(RCC + 0x30) | 0x6 | 1 << 21)               # GPIOB, GPIOC, DMA1
        self.w(RCC + 0x40, self.r(RCC + 0x40) | 3 << 14)                       # SPI2, SPI3
        self.w(RCC + 0x84, r << 28 | n << 6)                                   # PLLI2SCFGR
        self.w(RCC, self.r(RCC) | 1 << 26)                                     # PLLI2SON
        if pins:
            for p in (12, 13, 15):
                self.af(GPIOB, p, 5)
            self.af(GPIOC, 6, 5)                                               # I2S2_MCK
        if ext_pin:
            self.af(GPIOB, 14, 6)                                              # I2S2ext_SD
        self.spi = c.peripherals["SPI2"]
        self.ext = c.peripherals["I2S2ext"]
        self.edges: list = []
        c.pins.subscribe(lambda e: self.edges.append((e.t_ns, e.pin, e.new))
                         if e.field == "level" and e.pin in ("PB12", "PB13", "PB14", "PB15", "PC6") else None)

    def r(self, a):
        return self.chip.bus.read(a, 4)

    def w(self, a, v):
        self.chip.bus.write(a, 4, v)

    def af(self, port, p, af):
        self.w(port, (self.r(port) & ~(3 << 2 * p)) | 2 << 2 * p)
        a = port + 0x20 + 4 * (p >> 3)
        self.w(a, (self.r(a) & ~(0xF << 4 * (p & 7))) | af << 4 * (p & 7))

    def run(self, ns):
        self.ses.run_for(ns)

    def samples(self, sd="PB15", ckpol=0):
        """(WS, SD) at every sampling CK edge (rising for CKPOL = 0) from the recorded pin edges."""
        lv = {"PB12": "?", "PB13": "L" if not ckpol else "H", sd: "?"}
        out = []
        for _t, pin, new in self.edges:
            if pin == "PB13":
                if new == ("H" if not ckpol else "L") and lv["PB13"] != new:
                    out.append((lv["PB12"], lv[sd]))
                lv["PB13"] = new
            elif pin in lv:
                lv[pin] = new
        return out


def slots(samples, L, std=PHILIPS):
    """Decode (WS, SD) samples into a list of (ws_of_slot, value) per channel slot."""
    out = []
    ws = [w for w, _ in samples]
    bits = [1 if d == "H" else 0 for _, d in samples]
    # slot boundaries: Philips: one bit after a WS change; justified: at the WS change
    starts = [k + (1 if std == PHILIPS else 0) for k in range(1, len(ws)) if ws[k] != ws[k - 1]]
    for s in starts:
        if s + L <= len(bits):
            v = 0
            for b in bits[s:s + L]:
                v = v << 1 | b
            out.append((ws[s], v))
    return out


# ------------------------------------------------------------------------------------------------ clocks
@pytest.mark.parametrize("div,odd,mckoe,chlen", [(25, 0, 0, 0), (25, 1, 0, 1), (12, 1, 1, 0), (6, 0, 1, 1)])
def test_bit_clock_and_fs(div, odd, mckoe, chlen):
    g = Rig(n=256, r=5, pins=False, ext_pin=False)
    assert g.chip.peripherals["RCC"].plli2s_hz() == pytest.approx(16e6 / 16 * 256 / 5)
    g.w(SPI2 + I2SPR, div | odd << 8 | mckoe << 9)
    g.w(SPI2 + I2SCFGR, cfg(chlen=chlen))
    d = 2 * div + odd
    clk = 51.2e6
    fs = clk / (256 * d) if mckoe else clk / ((64 if chlen else 32) * d)
    eng = g.spi.i2s
    assert eng.fs_hz() == pytest.approx(fs)
    assert eng.bitclk_hz() == pytest.approx(fs * (64 if chlen else 32))
    if mckoe:
        assert eng.mck_out_hz() == pytest.approx(256 * fs)


def test_8khz_mck_wave_and_frame_rate():
    g = Rig(n=256, r=5)
    g.w(SPI2 + I2SPR, 12 | 1 << 8 | 1 << 9)                 # 2.048 MHz MCK -> 8 kHz
    g.w(SPI2 + I2SCFGR, cfg() | I2SE)
    g.run(1e6)
    ws_rises = [t for t, p, v in g.edges if p == "PB12" and v == "H"]
    gaps = [b - a for a, b in zip(ws_rises, ws_rises[1:])]
    assert gaps[1:] and all(x == pytest.approx(125e3, abs=1) for x in gaps[1:])   # [0]: idle-high WS before the lead-in
    g.spi.i2s._wave.flush()
    waves = [e for e in g.ses.log if e.kind == "pin" and e.fields.get("pin") == "PC6" and e.fields.get("field") == "wave"]
    assert waves and waves[0].fields["freq_hz"] == pytest.approx(2.048e6)


def test_no_plli2s_warns():
    g = Rig(pins=False, ext_pin=False)
    g.w(RCC, g.r(RCC) & ~(1 << 26))
    g.w(SPI2 + I2SCFGR, cfg() | I2SE)
    g.run(20e3)
    assert not g.spi.i2s.running
    assert any("I2SxCLK" in e.fields.get("msg", "") for e in g.ses.log if e.kind == "warn")
    g.w(RCC, g.r(RCC) | 1 << 26)                              # PLLI2S comes up: the master starts
    assert g.spi.i2s.running


def test_ckin_source():
    g = Rig(pins=False, ext_pin=False)
    g.chip.peripherals["RCC"].i2s_ckin_hz = 12.288e6
    g.w(RCC + 0x08, g.r(RCC + 0x08) | 1 << 23)              # I2SSRC = I2S_CKIN
    g.w(SPI2 + I2SPR, 3 | 1 << 9)
    g.w(SPI2 + I2SCFGR, cfg())
    assert g.spi.i2s.fs_hz() == pytest.approx(12.288e6 / 256 / 6)


# ------------------------------------------------------------------------------------------------ waveforms
def _master(g, c, words, div=25):
    g.w(SPI2 + I2SPR, div)                                   # 100 MHz / 50 = 2 MHz CK
    g.w(SPI2 + I2SCFGR, c | I2SE)
    sent = list(words)
    for _ in range(400):
        if not sent:
            break
        if g.r(SPI2 + SR) & TXE:
            g.w(SPI2 + DR, sent.pop(0))
        g.run(500)
    g.run(80e3)


@pytest.mark.parametrize("std", [PHILIPS, MSB, LSB])
def test_stereo_16bit_waveform(std):
    g = Rig()
    words = [0x1234, 0xABCD, 0x5555, 0x0F0F, 0x8001, 0x7FFE]
    _master(g, cfg(std=std), words)
    fr = [f for f in g.spi.i2s.tx_frames if f != (0, 0)]
    assert fr[:3] == [(0x1234, 0xABCD), (0x5555, 0x0F0F), (0x8001, 0x7FFE)]
    smp = g.samples()
    sl = slots(smp, 16, std)
    vals = [v for _w, v in sl]
    seq = [w for f in fr for w in f]
    k = vals.index(seq[1])                                    # the first slot follows the start (no WS edge)
    assert vals[k:k + 5] == seq[1:6]
    # left channel: WS low (Philips) / high (justified)
    ws_left = "L" if std == PHILIPS else "H"
    assert sl[k + 1][0] == ws_left and sl[k][0] != ws_left


def test_philips_msb_one_bit_after_ws():
    g = Rig()
    _master(g, cfg(), [0x8000, 0x8000, 0x8000, 0x8000])
    smp = g.samples()
    # every WS edge is followed one bit later by the MSB (=1) of the next slot once data flows
    hits = [k for k in range(1, len(smp) - 1) if smp[k][0] != smp[k - 1][0]]
    assert any(smp[k + 1][1] == "H" for k in hits)
    assert all(smp[k][1] == "L" for k in hits)               # the WS-change bit is the previous slot's LSB (0)


def test_lsb_justified_16_in_32():
    g = Rig()
    _master(g, cfg(std=LSB, chlen=1), [0xA5A5, 0x5A5A, 0xA5A5, 0x5A5A])
    sl = slots(g.samples(), 32, LSB)
    assert any(v == 0xA5A5 for _w, v in sl) and any(v == 0x5A5A for _w, v in sl)   # right aligned, 16 zero bits first


def test_24bit_two_accesses_philips_and_lsb():
    for std, words, exp in ((PHILIPS, [0x1234, 0x5600, 0xABCD, 0xEF00], (0x123456, 0xABCDEF)),
                            (LSB, [0x0012, 0x3456, 0x00AB, 0xCDEF], (0x123456, 0xABCDEF))):
        g = Rig()
        _master(g, cfg(std=std, datlen=1), words * 3)
        fr = list(g.spi.i2s.tx_frames)
        assert exp in fr
        sl = slots(g.samples(), 32, std)
        vals = [v for _w, v in sl]
        wire = (exp[0] << 8) if std == PHILIPS else exp[0]
        assert wire in vals


def test_32bit_data():
    g = Rig()
    _master(g, cfg(datlen=2), [0xDEAD, 0xBEEF, 0x0123, 0x4567] * 3)
    assert (0xDEADBEEF, 0x01234567) in list(g.spi.i2s.tx_frames)
    assert 0xDEADBEEF in [v for _w, v in slots(g.samples(), 32)]


def test_pcm_short_and_long_frame_sync():
    for sync, width in ((0, 1), (1, 13)):
        g = Rig()
        _master(g, cfg(std=PCM, pcmsync=sync), [0xC001] * 6)
        smp = g.samples()
        runs, k = [], 0
        while k < len(smp):
            if smp[k][0] == "H":
                j = k
                while j < len(smp) and smp[j][0] == "H":
                    j += 1
                runs.append((k, j - k))
                k = j
            else:
                k += 1
        assert runs and all(n == width for _k, n in runs[1:-1])
        assert all(b - a[0] == 16 for a, b in zip(runs, [r[0] for r in runs[1:]]))  # one 16-bit slot per frame
        start = runs[2][0] + (1 if sync == 0 else 0)
        v = 0
        for _w, d in smp[start:start + 16]:
            v = v << 1 | (d == "H")
        assert v == 0xC001


def test_ckpol_inverts_idle_and_edges():
    g = Rig()
    _master(g, cfg(ckpol=1), [0x1357, 0x2468] * 3)
    ck = [v for _t, p, v in g.edges if p == "PB13"]
    assert ck[0] == "H"                                        # idles high
    sl = slots(g.samples(ckpol=1), 16)
    assert 0x1357 in [v for _w, v in sl] and 0x2468 in [v for _w, v in sl]


def test_chside_txe_and_master_stop():
    g = Rig()
    g.w(SPI2 + I2SPR, 25)
    g.w(SPI2 + I2SCFGR, cfg() | I2SE)
    sides = []
    for _ in range(6):
        while not g.r(SPI2 + SR) & TXE:
            g.run(200)
        sides.append(bool(g.r(SPI2 + SR) & CHSIDE))
        g.w(SPI2 + DR, 0x1111)
    assert sides[1:] == [True, False, True, False, True] or sides[1:] == [False, True, False, True, False]
    assert g.r(SPI2 + SR) & BSY
    g.w(SPI2 + I2SCFGR, cfg())                                 # I2SE = 0: stops at the end of the slot
    g.run(20e3)
    assert not g.spi.i2s.running and not g.r(SPI2 + SR) & BSY


# ------------------------------------------------------------------------------------------------ receive / flags
def test_master_rx_overrun_and_irq():
    g = Rig()
    g.board.add_net("SDNET", [("A", "PB15")], drive="H")
    g.w(SPI2 + I2SPR, 25)
    g.w(SPI2 + CR2, ERRIE | RXNEIE)
    g.w(SPI2 + I2SCFGR, cfg(mode=MASTER_RX) | I2SE)
    g.run(20e3)
    sr = g.r(SPI2 + SR)
    assert sr & RXNE and sr & OVR and (16 + SPI2_IRQ) in g.chip.arch.asserted
    assert not sr & BSY                                        # master receiver: BSY kept low (RM0090 28.4.7)
    assert g.r(SPI2 + DR) == 0xFFFF
    g.run(20e3)
    assert not g.spi.get("SR") & RXNE                          # OVR set: every later half-word is lost (28.4.8)
    assert g.r(SPI2 + SR) & OVR                                # DR read then SR read clears OVR (returned set)
    assert not g.r(SPI2 + SR) & OVR
    g.run(20e3)
    assert g.r(SPI2 + SR) & RXNE                               # receiving again


def test_slave_tx_underrun_and_frame_error():
    g = Rig()
    pins = g.chip.pins
    g.w(SPI2 + CR2, ERRIE)
    g.w(SPI2 + I2SCFGR, cfg(mode=SLAVE_TX) | I2SE)
    ws = "H"

    def bit(w):
        pins.set_external("PB12", 1 if w == "H" else 0)
        pins.set_external("PB13", 1)
        pins.set_external("PB13", 0)
    pins.set_external("PB13", 0)
    for _ in range(16):
        bit(ws)
    for _ in range(3):                                         # three right/left slots, nothing written: UDR
        ws = "L" if ws == "H" else "H"
        for _ in range(16):
            bit(ws)
    sr = g.r(SPI2 + SR)
    assert sr & UDR and sr & BSY
    assert not g.r(SPI2 + SR) & UDR                            # cleared by the SR read
    ws = "L" if ws == "H" else "H"
    for _ in range(5):                                         # WS changes after 5 bits: frame error
        bit(ws)
    ws = "L" if ws == "H" else "H"
    bit(ws)
    assert g.r(SPI2 + SR) & FRE
    assert not g.r(SPI2 + SR) & FRE


def test_slave_rx_from_pins():
    g = Rig()
    pins = g.chip.pins
    g.w(SPI2 + I2SCFGR, cfg(mode=SLAVE_RX, std=MSB) | I2SE)
    pins.set_external("PB13", 0)
    got = []

    def slot(ws, v):
        for i in range(16):
            pins.set_external("PB12", ws)
            pins.set_external("PB15", v >> (15 - i) & 1)
            pins.set_external("PB13", 1)
            pins.set_external("PB13", 0)
            if g.r(SPI2 + SR) & RXNE:
                got.append((g.r(SPI2 + SR) & CHSIDE, g.r(SPI2 + DR)))
    slot(0, 0)
    for v in (0x1111, 0x2222, 0x3333, 0x4444):
        slot(1 if v in (0x1111, 0x3333) else 0, v)
    assert got == [(0, 0x1111), (CHSIDE, 0x2222), (0, 0x3333), (CHSIDE, 0x4444)]
    assert g.spi.i2s.rx_frames[0] == (0x1111, 0x2222)


def test_tx_dma_and_word_mode():
    g = Rig(pins=False, ext_pin=False)
    mem = 0x20001000
    data = [0x1000 + i for i in range(8)]
    for i, v in enumerate(data):
        g.chip.bus.write(mem + 2 * i, 2, v)
    s4 = DMA1 + 0x10 + 0x18 * 4                                # stream 4 channel 0 = SPI2_TX
    g.w(s4 + 4, len(data))
    g.w(s4 + 8, SPI2 + DR)
    g.w(s4 + 12, mem)
    g.w(s4, 1 << 6 | 1 << 10 | 1 << 11 | 1 << 13 | 1)          # M2P, MINC, PSIZE/MSIZE 16, EN
    g.w(SPI2 + CR2, TXDMAEN)
    g.w(SPI2 + I2SPR, 25)
    g.w(SPI2 + I2SCFGR, cfg() | I2SE)
    assert g.spi.i2s.word_mode
    g.run(150e3)
    fr = list(g.spi.i2s.tx_frames)
    flat = [w for f in fr for w in f]
    k = flat.index(0x1000)
    assert flat[k:k + 8] == data


def test_irq_is_shared_with_ext():
    g = Rig(pins=False, ext_pin=False)
    g.w(I2S2EXT + CR2, TXEIE)
    g.w(I2S2EXT + I2SCFGR, cfg(mode=SLAVE_TX))
    assert (16 + SPI2_IRQ) in g.chip.arch.asserted
    g.w(I2S2EXT + CR2, 0)
    assert (16 + SPI2_IRQ) not in g.chip.arch.asserted


# ------------------------------------------------------------------------------------------------ full duplex / codec
def _codec_rig(fmt="i2s", std=PHILIPS):
    g = Rig()
    b = g.board
    b.add_net("BCLK", [("A", "PB13")])
    b.add_net("WS", [("A", "PB12")])
    b.add_net("SDIN", [("A", "PB15")])
    b.add_net("SDOUT", [("A", "PB14")], pull="down")
    codec = b.add_part("CODEC", "i2s_codec", {"bits": 16, "format": fmt, "pattern": "1000,-1000,2000,-2000"},
                       {"bclk": "BCLK", "ws": "WS", "sdin": "SDIN", "sdout": "SDOUT"})
    g.w(SPI2 + I2SPR, 25)
    g.w(I2S2EXT + I2SCFGR, cfg(mode=SLAVE_RX, std=std) | I2SE)
    g.w(SPI2 + I2SCFGR, cfg(std=std) | I2SE)
    return g, codec


@pytest.mark.parametrize("fmt,std", [("i2s", PHILIPS), ("left_justified", MSB)])
def test_full_duplex_with_codec(fmt, std):
    g, codec = _codec_rig(fmt, std)
    words = [0x1111, 0xEEEE, 0x2222, 0xDDDD, 0x3333, 0xCCCC, 0x4444, 0xBBBB]
    got = []
    for _ in range(500):
        sr = g.r(SPI2 + SR)
        if sr & TXE and words:
            g.w(SPI2 + DR, words.pop(0))
        if g.r(I2S2EXT + SR) & RXNE:
            got.append(g.r(I2S2EXT + DR))
        g.run(500)
    assert (0x2222, 0xDDDD - 0x10000) in codec.frames and (0x3333, 0xCCCC - 0x10000) in codec.frames
    pat = [1000, 0x10000 - 1000, 2000, 0x10000 - 2000]
    k = got.index(1000)
    assert got[k:k + 4] == pat
    assert (1000, 0x10000 - 1000) in list(g.ext.i2s.rx_frames)


def test_i2s_events_like_sai():
    g, _codec = _codec_rig()
    g.spi.i2s.event_frames = g.ext.i2s.event_frames = 4
    for i in range(400):
        if g.r(SPI2 + SR) & TXE:
            g.w(SPI2 + DR, 0x100 + i)
        g.run(500)
    evs = [e for e in g.ses.log if e.kind == "i2s"]
    assert {e.fields["dir"] for e in evs} == {"tx", "rx"}
    e = evs[0].fields
    assert e["frames"] == 4 and e["bits"] == 16 and e["block"] in ("SPI2", "I2S2ext")


def test_geo_layouts():
    assert Geo(cfg(datlen=1)).layout == ["D", "H8"] and Geo(cfg(std=LSB, datlen=1)).layout == ["L8", "D"]
    assert Geo(cfg(chlen=1)).layout == ["D", "Z"] and Geo(cfg(std=LSB, chlen=1)).layout == ["Z", "D"]
    assert Geo(cfg(std=PCM)).F == 16 and Geo(cfg(datlen=2)).F == 64


def test_sample_board():
    elf = ROOT / "firmware/samples_stm32/build/i2s_tone.elf"
    if not elf.exists():
        pytest.skip("i2s_tone not built")
    from chipem.board import load_board
    s = Session.from_board(load_board(ROOT / "boards" / "stm32_i2s_codec.yaml"))
    s.run_for(20e6)
    a = s.board.chips["A"]
    out = a.uart_out("USART1").decode()
    assert out.startswith("I2S 8000 Hz: tx ") and " ok\r\n" in out, out
    assert "RX 1234 EDCC 5678 A988" in out
    fr = s.board.parts["CODEC"].frames
    assert (7071, -7071) in fr and (10000, -10000) in fr
    eng = a.peripherals["SPI2"].i2s
    assert eng.fs_hz() == pytest.approx(8000) and eng.mck_out_hz() == pytest.approx(2.048e6)
    s.close()


# ------------------------------------------------------------------------------------------------ RM0090 28.4.5-28.4.8
def test_master_stops_at_the_end_of_the_current_half_word():
    """28.4.5 (receiver switch-off sequences, e.g. 16-bit data in a 32-bit LSB-justified channel: wait 17 CK after the
    second-to-last RXNE): clearing I2SE stops a master at the end of the current 16-bit half-word, not of the slot."""
    g = Rig()
    g.w(SPI2 + I2SPR, 25)
    g.w(SPI2 + I2SCFGR, cfg(std=MSB, chlen=1) | I2SE)
    eng = g.spi.i2s
    while not (eng.p >= 32 and 3 <= eng.p % 32 <= 10):
        g.run(100)
    t_clear = g.chip.now_ns()
    g.w(SPI2 + I2SCFGR, cfg(std=MSB, chlen=1))
    assert not g.r(SPI2 + I2SCFGR) & I2SE
    g.run(30e3)
    assert not eng.running
    rises = [t for t, p, v in g.edges if p == "PB13" and v == "H"]
    n = len(rises) - 2                                         # two lead-in bit clocks
    assert n % 16 == 0 and n % 32 == 16                        # ended after the first half-word of a slot
    assert len([t for t in rises if t > t_clear]) < 16


def test_txe_reset_while_disabled():
    """28.4.7: TXE is reset when the I2S is disabled; it is set again once enabled with an empty TX buffer."""
    g = Rig()
    g.w(SPI2 + I2SPR, 25)
    g.w(SPI2 + I2SCFGR, cfg() | I2SE)
    assert g.r(SPI2 + SR) & TXE
    g.w(SPI2 + I2SCFGR, cfg())
    assert not g.r(SPI2 + SR) & TXE
    g.run(30e3)
    g.w(SPI2 + I2SCFGR, cfg() | I2SE)
    assert g.r(SPI2 + SR) & TXE


def _slave_bits(g, ws_seq, sd=0):
    pins = g.chip.pins
    out = []
    for w in ws_seq:
        pins.set_external("PB12", w)
        pins.set_external("PB15", sd)
        pins.set_external("PB13", 1)
        pins.set_external("PB13", 0)
        out.append(g.spi.get("SR"))
    return out


def test_slave_bsy_low_one_clock_between_half_words():
    """28.4.7: in slave mode BSY goes low for one I2S clock cycle between transfers (16-bit half-words)."""
    g = Rig()
    g.chip.pins.set_external("PB13", 0)
    g.w(SPI2 + I2SCFGR, cfg(mode=SLAVE_RX, std=MSB) | I2SE)
    assert not g.r(SPI2 + SR) & BSY
    _slave_bits(g, [0] * 4)
    srs = _slave_bits(g, [1] * 16 + [0] * 16 + [1] * 16)        # synchronised at the first WS rise
    bsy = [bool(s & BSY) for s in srs]
    assert bsy == ([True] * 15 + [False]) * 3
    g.r(SPI2 + DR)


def test_slave_frame_error_needs_reenable():
    """28.4.8: after FRE the slave stays desynchronised; it resynchronises only after I2SE is cleared and set."""
    g = Rig()
    g.chip.pins.set_external("PB13", 0)
    g.w(SPI2 + I2SCFGR, cfg(mode=SLAVE_RX, std=MSB) | I2SE)
    _slave_bits(g, [0] * 4 + [1] * 16 + [0] * 5 + [1])          # WS changes 5 bits into the right slot: FRE
    assert g.r(SPI2 + SR) & FRE
    g.r(SPI2 + DR)
    srs = _slave_bits(g, [1] * 15 + ([0] * 16 + [1] * 16) * 2)  # valid frames: ignored
    assert not any(s & (RXNE | BSY) for s in srs)
    g.w(SPI2 + I2SCFGR, cfg(mode=SLAVE_RX, std=MSB))
    g.w(SPI2 + I2SCFGR, cfg(mode=SLAVE_RX, std=MSB) | I2SE)
    srs = _slave_bits(g, [0] * 4 + [1] * 16)
    assert srs[-1] & RXNE


def test_plli2s_wrong_configuration_warns():
    """7.3.23: PLLI2SN must be 50..432, the VCO 100..432 MHz, the I2S clock at most 192 MHz."""
    g = Rig(n=40, r=2, pins=False, ext_pin=False)               # 1 MHz x 40 = 40 MHz VCO, N < 50
    assert g.chip.peripherals["RCC"].plli2s_hz() == pytest.approx(20e6)
    msgs = [e.fields["msg"] for e in g.ses.log if e.kind == "warn"]
    assert any("PLLI2SN = 40" in m for m in msgs) and any("VCO 40 MHz" in m for m in msgs)
