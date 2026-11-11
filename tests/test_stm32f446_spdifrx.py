"""STM32F446 SPDIFRX (chipem/periph/stm32/spdifrx.py, RM0390 Rev 9 ch. 27) fed by the IEC 60958 source part
(chipem/parts/spdif_source.py): synchronisation and SYNCD / WIDTH5 / DIR (27.3.4, 27.5.3, 27.5.9), TERR without a
stream (27.3.8), SERR with a too slow SPDIFRX_CLK (27.3.9 Table 171), the control flow CSR / SOB / CSRNE (27.3.7), the
data formats DRFMT 00/01/10 and masks (27.5.5-27.5.7), parity error PERR / PE (27.3.8), FERR -> STATE_STOP and the
return to IDLE (27.3.5), overrun with RXSTEO alignment (27.3.8, Figure 351), register protection (27.3.12 Table 172)
and the sample board boards/stm32f446re_spdifrx.yaml (skipped when firmware/samples_stm32f446 is not built).
SPDIFRX_CLK = PLLI2S_P = 100 MHz (HSI 16 MHz / 16 x 200 / 2) unless a test says otherwise; stream 48 kHz."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from chipem.board import load_board
from chipem.session import Session

from engine_util import asm

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "firmware" / "samples_stm32f446" / "build"
FLASH = 0x08000000
RCC, GPIOB, X = 0x40023800, 0x40020400, 0x40004000
CR, IMR, SR, IFCR, DR, CSR, DIR = range(0, 0x1C, 4)
RXNE, CSRNE, PERR, OVR, SBD, SYNCD, FERR, SERR, TERR = 1, 2, 4, 8, 0x10, 0x20, 0x40, 0x80, 0x100
WFA, CUMSK, PTMSK, PMSK, VMSK, RXSTEO, RXDMAEN, CHSEL = 0x4000, 0x100, 0x200, 0x40, 0x80, 8, 4, 0x800
MS = 1e6
FRAME = 1e9 / 48000


def board(tmp: Path, part: str | None = "{id: TX1, type: spdif_source, rate: 48000, signal: counter, ports: {out: A.PB7}}",
          plli2sn: int = 200) -> Session:
    code = asm("spin: b spin", FLASH + 0x200)
    img = bytearray(0x200 + len(code))
    img[0:8] = struct.pack("<II", 0x20020000, (FLASH + 0x200) | 1)
    img[0x200:] = code
    (tmp / "spin.bin").write_bytes(bytes(img))
    y = ["name: spdif_test", "chips:",
         f"  - {{id: A, chip: stm32f446re, firmware: {tmp / 'spin.bin'}, load_addr: 0x08000000}}"]
    if part:
        y += ["parts:", f"  - {part}"]
    (tmp / "b.yaml").write_text("\n".join(y) + "\n")
    s = Session.from_board(load_board(tmp / "b.yaml"), quiet=True)
    c = s.chips["A"]
    w = c.bus.write
    w(RCC + 0x30, 4, 0x3)                                      # GPIOA, GPIOB
    w(RCC + 0x40, 4, 1 << 16)                                  # SPDIFRXEN
    w(RCC + 0x84, 4, 16 | (plli2sn << 6) | (2 << 24) | (2 << 28))   # PLLI2S: /16 x N, P /2
    w(RCC + 0x00, 4, c.bus.read(RCC, 4) | (1 << 26))
    w(RCC + 0x94, 4, 1 << 29)                                  # SPDIFRXSEL = PLLI2S_P
    w(GPIOB + 0x20, 4, 8 << 28)                                # PB7 AF8 = SPDIFRX_IN0
    w(GPIOB + 0x00, 4, c.bus.read(GPIOB, 4) | (2 << 14))
    return s


def rd(c, off):
    return c.bus.read(X + off, 4)


def wr(c, off, v):
    c.bus.write(X + off, 4, v)


def warns(s):
    return [e.fields.get("msg", "") for e in s.events(kinds={"warn"})]


# ------------------------------------------------------------------------ 27.3.4 synchronisation, 27.5.3 / 27.5.9
def test_sync_width5_dir(tmp_path):
    s = board(tmp_path)
    c = s.chips["A"]
    wr(c, CR, WFA | 1)
    s.run_for(1.5 * FRAME)
    assert not rd(c, SR) & SYNCD                               # coarse + fine synchronisation take ~1.4 frames
    s.run_for(1 * FRAME)
    sr = rd(c, SR)
    assert sr & SYNCD and not sr & (TERR | SERR | FERR)
    w5 = sr >> 16
    assert w5 == round(5 * 100e6 / (64 * 48000))               # 163: Fs = 5 x clk / (WIDTH5 x 64) ~ 47.9 kHz
    d = rd(c, DIR)
    assert d >> 16 == round(1.5 * 100e6 / (128 * 48000)) and d & 0x1FFF == round(2.5 * 100e6 / (128 * 48000))
    s.close()


def test_terr_without_stream_and_wfa(tmp_path):
    s = board(tmp_path, part=None)
    c = s.chips["A"]
    wr(c, CR, 1)                                               # WFA = 0: TRCNT overflows after 8192 clocks
    s.run_for(0.08 * MS)
    assert rd(c, SR) & TERR == 0
    s.run_for(0.01 * MS)                                       # 8192 / 100 MHz = 81.92 us
    assert rd(c, SR) & TERR
    wr(c, CR, rd(c, CR) | 3)                                   # STATE_STOP: stays stopped
    wr(c, CR, 0)                                               # IDLE clears TERR / SERR / FERR (27.5.3)
    assert rd(c, SR) & (TERR | SERR | FERR) == 0
    wr(c, CR, WFA | 1)                                         # WFA = 1: waits for activity, no TERR
    s.run_for(1 * MS)
    assert rd(c, SR) & TERR == 0
    s.close()


def test_slow_clock_serr(tmp_path):
    s = board(tmp_path, plli2sn=60)                            # PLLI2S_P = 30 MHz < 704 x 48 kHz = 33.8 MHz
    c = s.chips["A"]
    wr(c, CR, (1 << 12) | 1)                                   # NBTR = 01: 3 re-tries = 4 attempts
    s.run_for(3.5 * FRAME)
    assert rd(c, SR) & SERR == 0
    s.run_for(1 * FRAME)
    assert rd(c, SR) & SERR and not rd(c, SR) & SYNCD
    assert any("704 x Fs" in m for m in warns(s))
    s.close()


# ---------------------------------------------------------------------------------------- 27.3.7 control flow
def test_channel_status_csr(tmp_path):
    s = board(tmp_path)
    c = s.chips["A"]
    wr(c, CR, 1)
    words = []
    for _ in range(400):
        s.run_for(FRAME)
        if rd(c, SR) & CSRNE:
            words.append(rd(c, CSR))
            assert rd(c, SR) & CSRNE == 0                      # cleared by the CSR read
        if len(words) >= 30:
            break
    sob = [i for i, w in enumerate(words) if w >> 24 & 1]
    assert sob, "no start of block"
    i = sob[0]
    cs = [(w >> 16) & 0xFF for w in words[i:i + 4]]
    assert cs == [0x04, 0x00, 0x10, 0x02]                      # consumer PCM, copy ok, channel 1, 48 kHz
    assert len(sob) == 1 or sob[1] - sob[0] == 24               # 192 frames / 8 per word
    assert rd(c, SR) & SBD
    s.close()


# ------------------------------------------------------------------------ 27.5.5-27.5.7 data formats, masks
@pytest.mark.parametrize("fmt", [0, 1, 2])
def test_data_formats(tmp_path, fmt):
    s = board(tmp_path)
    c = s.chips["A"]
    wr(c, CR, (fmt << 4) | RXSTEO | 3)
    got = []
    for _ in range(4000):
        s.run_for(FRAME / 4)
        if rd(c, SR) & RXNE:
            got.append(rd(c, DR))
        if len(got) >= 4:
            break
    if fmt == 0:
        a, b = got[0], got[1]
        assert (a >> 28) & 3 == 1 and (b >> 28) & 3 == 3       # PT: B preamble first (27.3.5), then W
        assert a & 0xFFFFFF == 192 * ((a & 0xFFFFFF) // 192) and b & 0xFFFFFF == (~a) & 0xFFFFFF
        assert got[2] & 0xFFFFFF == (a & 0xFFFFFF) + 1 and (got[2] >> 28) & 3 == 2   # M
    elif fmt == 1:
        a, b = got[0], got[1]
        assert (a >> 4) & 3 == 1 and (a >> 8) == 192 * ((a >> 8) // 192) and (b >> 8) == (~(a >> 8)) & 0xFFFFFF
    else:
        for w in got:                                          # [31:16] = ChA bits 23:8, [15:0] = ChB
            assert (w & 0xFFFF) == (~(w >> 16)) & 0xFFFF
    s.close()


def test_masks_and_parity_error(tmp_path):
    s = board(tmp_path, part="{id: TX1, type: spdif_source, rate: 48000, signal: counter, parity_error_every: 2, "
                             "validity: 1, ports: {out: A.PB7}}")
    c = s.chips["A"]
    wr(c, CR, PTMSK | CUMSK | 3)                               # PE and V copied, PT/C/U forced to 0
    while not rd(c, SR) & RXNE:
        s.run_for(FRAME / 4)
    a = rd(c, DR)
    s.run_for(FRAME / 2)
    b = rd(c, DR)
    assert (a >> 24) & 0x3F == 0b000010                        # A: V = 1, no parity error
    assert (b >> 24) & 0x3F == 0b000011                        # B: every 2nd sub-frame has a parity error
    assert rd(c, SR) & PERR
    wr(c, IFCR, PERR)
    s.close()


# ----------------------------------------------------------------------- 27.3.8 FERR -> STATE_STOP (27.3.5)
def test_framing_error_stops(tmp_path):
    s = board(tmp_path, part="{id: TX1, type: spdif_source, rate: 48000, signal: counter, corrupt_at: 600, "
                             "ports: {out: A.PB7}}")
    c = s.chips["A"]
    wr(c, CR, 1)
    s.run_for(310 * FRAME)
    sr = rd(c, SR)
    assert sr & FERR and sr & SYNCD
    wr(c, CR, 0)
    assert rd(c, SR) & FERR == 0
    s.close()


def test_stream_stop_terr(tmp_path):
    s = board(tmp_path, part="{id: TX1, type: spdif_source, rate: 48000, signal: silence, stop: 1ms, "
                             "ports: {out: A.PB7}}")
    c = s.chips["A"]
    wr(c, CR, 1)
    s.run_for(1 * MS)
    assert rd(c, SR) & SYNCD and not rd(c, SR) & TERR
    s.run_for(0.3 * MS)
    assert rd(c, SR) & TERR                                     # no transition for 8192 SPDIFRX_CLK periods
    s.close()


# --------------------------------------------------------------------- 27.3.8 overrun, RXSTEO alignment (Fig. 351)
def test_overrun_keeps_stereo_alignment(tmp_path):
    s = board(tmp_path)
    c = s.chips["A"]
    wr(c, CR, (1 << 4) | RXSTEO | 3)
    while not rd(c, SR) & RXNE:
        s.run_for(FRAME / 4)
    s.run_for(3 * FRAME)                                        # DR + RX_BUF full, then samples lost
    assert rd(c, SR) & OVR
    words = [rd(c, DR) >> 8 for _ in range(2)]
    for _ in range(12):
        s.run_for(FRAME / 2)
        while rd(c, SR) & RXNE:
            words.append(rd(c, DR) >> 8)
    a0, b0 = words[0], words[1]
    assert b0 == (~a0) & 0xFFFFFF                               # DR, RX_BUF: A then B
    for i in range(2, len(words) - 1, 2):                       # every later pair starts with channel A
        assert words[i + 1] == (~words[i]) & 0xFFFFFF and words[i] < 0x800000
    wr(c, IFCR, OVR)
    assert rd(c, SR) & OVR == 0
    s.close()


# ------------------------------------------------------------------------ 27.3.12 Table 172 register protection
def test_register_protection(tmp_path):
    s = board(tmp_path)
    c = s.chips["A"]
    wr(c, CR, 0xFFFFFFFF & ~3)
    assert rd(c, CR) == 0x00077FFC                              # reserved bits read 0
    wr(c, CR, (1 << 4) | 1)
    wr(c, CR, (2 << 16) | WFA | CHSEL | (1 << 4) | 1)           # INSEL / WFA / CHSEL: read-only outside IDLE
    assert rd(c, CR) == (1 << 4) | 1
    wr(c, CR, (2 << 4) | 3)                                     # DRFMT may change in the write entering RCV
    assert rd(c, CR) == (2 << 4) | 3
    wr(c, CR, (0 << 4) | RXSTEO | 3)                            # ... but not in STATE_RCV
    assert rd(c, CR) == (2 << 4) | 3
    wr(c, CR, 1)                                                # RCV -> SYNC is not allowed
    assert rd(c, CR) & 3 == 3
    wr(c, CR, 2)                                                # 10 reserved
    assert rd(c, CR) & 3 == 3
    wr(c, IFCR, 0x3C)
    assert rd(c, IFCR) == 0
    s.close()


# ------------------------------------------------------------------------------------------------ sample board
def test_board_spdifrx():
    if not (BUILD / "spdifrx_rx.elf").exists():
        pytest.skip("spdifrx_rx.elf not built")
    s = Session.from_board(ROOT / "boards" / "stm32f446re_spdifrx.yaml", quiet=True)
    s.run_for(30 * MS)
    out = s.uart_output("A.USART2")
    assert out.startswith(b"SPDIFRX wait sync\r\nSYNCD WIDTH5 163 Fs ~47929 Hz\r\nCS 04 00 10 02 PCM 48 kHz\r\n")
    assert b"pairs ok 8/8\r\n" in out and out.endswith(b"done\r\n")
    assert not [e for e in s.events(kinds={"warn", "fault"})]
    s.close()
