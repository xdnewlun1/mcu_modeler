"""SD card device (chipem/devices/sdcard.py) and the STM32F4 SDIO host (chipem/periph/stm32/sdio.py): card protocol
(CRC7, state machine, R1/R2/R3/R6/R7, illegal commands, SDHC/SDSC registers, CMD6, erase, images), host command
path timing and flags, data path FIFO/flags/errors, DMA, pins, and the samples (bare-metal, board, ST HAL)."""
from pathlib import Path

import pytest

from chipem.devices.sdcard import (ADDRESS_ERROR, APP_CMD, ILLEGAL_COMMAND, OUT_OF_RANGE, READY_FOR_DATA, SDCard,
                                   crc7, crc16, crc16_lines)
from chipem.session import Session
from test_stm32 import GPIOA, RCC, desc, rd, spin_chip, warns, wr  # noqa: F401  (desc: fixture)

ROOT = Path(__file__).resolve().parents[1]
SDIO = 0x40012C00
POWER, CLKCR, ARG, CMD, RESPCMD, RESP1, RESP2, RESP3, RESP4 = 0, 4, 8, 0xC, 0x10, 0x14, 0x18, 0x1C, 0x20
DTIMER, DLEN, DCTRL, DCOUNT, STA, ICR, MASK, FIFOCNT, FIFO = 0x24, 0x28, 0x2C, 0x30, 0x34, 0x38, 0x3C, 0x48, 0x80
CCRCFAIL, DCRCFAIL, CTIMEOUT, DTIMEOUT, TXUNDERR, RXOVERR, CMDREND, CMDSENT, DATAEND = (1 << i for i in range(9))
DBCKEND, CMDACT, TXACT, RXACT, TXFIFOHE, RXFIFOHF = 1 << 10, 1 << 11, 1 << 12, 1 << 13, 1 << 14, 1 << 15
TXFIFOE, RXFIFOE, RXDAVL = 1 << 18, 1 << 19, 1 << 21
SHORT, LONG, CPSMEN = 1 << 6, 3 << 6, 1 << 10
SDIO_IRQ = 49
GPIOC, GPIOD = 0x40020800, 0x40020C00
STM_SAMPLES = ROOT / "firmware" / "samples_stm32" / "build"
HAL_SAMPLES = ROOT / "firmware" / "samples_stm32_hal" / "build"


# ------------------------------------------------------------------------------------------------ CRCs
def test_crc7_known_commands():
    # CMD0 arg 0 -> 0x40 00 00 00 00 95 ; CMD8 arg 0x1AA -> 0x48 00 00 01 AA 87 (SD spec examples)
    assert (crc7(0x4000000000, 40) << 1) | 1 == 0x95
    assert (crc7(0x48000001AA, 40) << 1) | 1 == 0x87
    assert (crc7(0x5100000000, 40) << 1) | 1 == 0x55      # CMD17 arg 0


def test_crc16_and_lines():
    assert crc16(b"\xff" * 512) == 0x7FA1                    # SD spec: 512 bytes of 0xFF
    lines = crc16_lines(bytes(range(256)) * 2, 4)
    assert len(lines) == 4 and crc16_lines(b"\xff" * 512, 1) == [0x7FA1]


# ------------------------------------------------------------------------------------------------ card protocol
def _init(card, *, hcs=True, cmd8=True, t=0.0):
    card.command(0, 0, t)
    if cmd8:
        assert card.command(8, 0x1AA, t).payload & 0xFFF == 0x1AA
    r = None
    for i in range(100):
        t += 0.1e6
        assert card.command(55, 0, t).status & APP_CMD
        r = card.command(41, 0x00FF8000 | (0x40000000 if hcs else 0), t)
        if r.payload & 0x80000000:
            break
    return r, t


def test_card_identification_sdhc():
    card = SDCard("64M")
    r, t = _init(card)
    assert r.kind == "R3" and r.payload & 0xC0000000 == 0xC0000000 and not r.crc_ok()   # CCS=1; R3 has no CRC
    assert card.current_state() == "ready"
    cid = card.command(2, 0, t)
    assert cid.kind == "R2" and cid.crc_ok() and card.current_state() == "ident"
    r6 = card.command(3, 0, t)
    rca = r6.payload >> 16
    assert r6.kind == "R6" and r6.crc_ok() and rca and card.current_state() == "stby"
    assert (r6.payload >> 9) & 0xF == 2                     # state when CMD3 arrived: ident
    csd = card.command(9, rca << 16, t).payload
    assert csd >> 126 == 1 and ((csd >> 48) & 0x3FFFFF) + 1 == 128   # CSD v2, 128 x 512 KiB
    assert card.command(9, (rca + 1) << 16, t) is None       # other RCA: no response
    r1 = card.command(7, rca << 16, t)
    assert r1.kind == "R1b" and card.current_state() == "tran" and (r1.status >> 9) & 0xF == 3
    st = card.command(13, rca << 16, t).status
    assert (st >> 9) & 0xF == 4 and st & READY_FOR_DATA


def test_sdhc_needs_hcs_and_cmd8():
    card = SDCard("64M")
    r, _ = _init(card, hcs=False)
    assert not r.payload & 0x80000000                       # never ready without HCS
    card2 = SDCard("64M")
    r, _ = _init(card2, cmd8=False)
    assert not r.payload & 0x80000000                       # host treated as v1: SDHC stays busy
    card3 = SDCard("64M", kind="sdsc")
    r, _ = _init(card3, hcs=False)
    assert r.payload & 0xC0000000 == 0x80000000             # SDSC: ready, CCS = 0


def test_init_time_and_inquiry():
    card = SDCard("64M", init_time="5ms")
    card.command(0, 0, 0)
    card.command(8, 0x1AA, 0)
    card.command(55, 0, 0)
    assert card.command(41, 0, 0).payload == 0x00FF8000      # inquiry: OCR, init not started
    card.command(55, 0, 1e6)
    assert not card.command(41, 0x40FF8000, 1e6).payload & 0x80000000
    card.command(55, 0, 6.1e6)
    assert card.command(41, 0x40FF8000, 6.1e6).payload & 0x80000000


def test_illegal_command_reported_on_next():
    card = SDCard("64M")
    r, t = _init(card)
    assert card.command(17, 0, t) is None                   # read in ready state: illegal, no response
    card.command(2, 0, t)                                   # valid: the error is reported "one command later"
    assert card.command(7, 0, t) is None                    # CMD7 in ident state: illegal
    r6 = card.command(3, 0, t)
    assert r6.payload & (1 << 14)                           # R6 bit 14 = ILLEGAL_COMMAND (status bit 22)
    rca = r6.payload >> 16
    st = card.command(13, rca << 16, t).status
    assert not st & ILLEGAL_COMMAND                         # cleared once reported (condition B)
    assert card.command(5, 0, t) is None                    # unsupported command
    assert card.command(13, rca << 16, t).status & ILLEGAL_COMMAND


def test_v1_card_ignores_cmd8_and_cmd6():
    card = SDCard("32M", kind="sdsc", version=1)
    card.command(0, 0, 0)
    assert card.command(8, 0x1AA, 0) is None
    r, t = _init(card, cmd8=False, hcs=False)
    assert r.payload & 0x80000000
    assert SDCard("32M", kind="sdsc", version=1).scr >> 56 & 0xF == 0     # SD_SPEC 1.0


def _tran(card):
    _, t = _init(card)
    card.command(2, 0, t)
    rca = card.command(3, 0, t).payload >> 16
    card.command(7, rca << 16, t)
    return rca, t


def test_read_write_multi_and_stop():
    card = SDCard("64M")
    rca, t = _tran(card)
    assert card.command(25, 10, t).data == "write" and card.current_state() == "rcv"
    ok, busy = card.write_block(b"A" * 512, t, 1)
    assert ok and busy == card.busy_ns
    t += 1e6
    ok, _ = card.write_block(b"B" * 512, t, 1)
    r = card.command(12, 0, t + 1)
    assert r.kind == "R1b" and r.busy_ns > 0 and card.current_state(t + 1) == "prg"
    assert card.current_state(t + 1e6) == "tran"
    assert card.read_bytes(10 * 512, 2) == b"AA" and card.read_bytes(11 * 512, 1) == b"B"
    assert card.command(18, 10, t + 2e6).data == "read"
    assert card.read_block(t + 2e6, 1) == b"A" * 512
    assert card.read_block(t + 2e6, 1) == b"B" * 512
    card.command(12, 0, t + 2e6)
    assert card.read_block(t + 2e6, 1) is None and card.current_state() == "tran"
    assert card.ops == [("write", 10, 2), ("read", 10, 2)]


def test_cmd23_block_count_and_wrong_width():
    card = SDCard("64M")
    rca, t = _tran(card)
    card.command(23, 2, t)
    card.command(25, 0, t)
    card.write_block(b"x" * 512, t, 1)
    card.write_block(b"y" * 512, t, 1)
    assert card.current_state(t) == "prg"                   # pre-defined count reached: no CMD12 needed
    t += 1e6
    card.command(24, 5, t)
    ok, _ = card.write_block(b"z" * 512, t, 4)              # host on 4 bits, card still on 1
    assert not ok and card.current_state() == "tran" and card.read_bytes(5 * 512, 1) == b"\0"


def test_address_errors():
    card = SDCard("1M", kind="sdhc")                        # SDHC rounds up to 512 KiB units
    rca, t = _tran(card)
    assert card.command(17, card.nblocks, t).status & OUT_OF_RANGE
    sdsc = SDCard("1M", kind="sdsc")
    rca, t = _tran(sdsc)
    assert sdsc.command(24, 100, t).status & ADDRESS_ERROR  # byte address not block aligned
    assert sdsc.command(24, 512, t).data == "write"


def test_cmd6_high_speed_and_scr_status():
    card = SDCard("64M")
    rca, t = _tran(card)
    card.command(6, 0x00FFFFF1, t)                          # check mode, group 1 function 1
    st = card.read_block(t, 1)
    assert len(st) == 64 and st[16] & 0xF == 1 and not card.high_speed
    card.command(6, 0x80FFFFF1, t)
    card.read_block(t, 1)
    assert card.high_speed
    card.command(55, rca << 16, t)
    card.command(51, 0, t)
    scr = card.read_block(t, 1)
    assert len(scr) == 8 and scr[0] >> 4 == 0 and scr[0] & 0xF == 2 and scr[1] & 0xF == 0b0101
    card.command(55, rca << 16, t)
    card.command(6, 2, t)
    card.command(55, rca << 16, t)
    card.command(13, 0, t)
    sds = card.read_block(t, 4)
    assert sds[0] >> 6 == 2 and sds[8] == 4                 # 4-bit bus, speed class 4 (class 10 coding 4)


def test_erase_and_image_roundtrip(tmp_path):
    img = tmp_path / "card.img"
    img.write_bytes(b"\x11" * 2048)
    out = tmp_path / "out.img"
    card = SDCard(image=img, save=out, kind="sdsc")
    rca, t = _tran(card)
    assert card.command(17, 512, t).data == "read" and card.read_block(t, 1) == b"\x11" * 512
    card.command(32, 0, t)
    card.command(33, 512, t)
    r = card.command(38, 0, t)
    assert r.kind == "R1b" and r.busy_ns == card.erase_ns
    assert card.read_bytes(0, 1024) == bytes(1024) and card.read_bytes(1024, 1) == b"\x11"
    card.write_bytes(4096, b"new")
    card.save_image()
    data = out.read_bytes()
    assert data[:1024] == bytes(1024) and data[1024:2048] == b"\x11" * 1024 and data[4096:4099] == b"new"


# ------------------------------------------------------------------------------------------------ host model
def _sdio_chip(desc, card=None, *, pins=True, pll=True):
    chip = spin_chip(desc, clock_hz=16e6)
    wr(chip, RCC + 0x30, rd(chip, RCC + 0x30) | 1 << 2 | 1 << 3 | 1 << 22)   # GPIOC, GPIOD, DMA2
    wr(chip, RCC + 0x44, rd(chip, RCC + 0x44) | 1 << 11)                     # SDIOEN
    if pll:
        wr(chip, RCC + 0x04, 16 | 336 << 6 | 7 << 24)                        # HSI/16 x 336 /7 = 48 MHz
        wr(chip, RCC, rd(chip, RCC) | 1 << 24)
    if pins:
        moder, afrh = rd(chip, GPIOC), rd(chip, GPIOC + 0x24)
        for p in range(8, 13):
            moder = (moder & ~(3 << 2 * p)) | 2 << 2 * p
            afrh = (afrh & ~(0xF << 4 * (p - 8))) | 12 << 4 * (p - 8)
        wr(chip, GPIOC + 0x24, afrh)
        wr(chip, GPIOC, moder)
        wr(chip, GPIOD + 0x20, (rd(chip, GPIOD + 0x20) & ~0xF00) | 12 << 8)
        wr(chip, GPIOD, (rd(chip, GPIOD) & ~0x30) | 2 << 4)
    if card is not None:
        chip.peripherals["SDIO"].attach_card(card)
    wr(chip, SDIO + POWER, 3)
    wr(chip, SDIO + CLKCR, 1 << 8 | 118)                                     # 400 kHz
    return chip


def _cmd(chip, idx, arg=0, resp=SHORT, limit=2e6):
    wr(chip, SDIO + ICR, 0x7FF)
    wr(chip, SDIO + ARG, arg)
    t0 = chip.now_ns()
    wr(chip, SDIO + CMD, idx | resp | CPSMEN)
    done = CMDSENT if resp == 0 else (CMDREND | CCRCFAIL | CTIMEOUT)
    while not rd(chip, SDIO + STA) & done:
        chip.run_for(1000)
        assert chip.now_ns() - t0 < limit
    return rd(chip, SDIO + STA), chip.now_ns() - t0


def _identify(chip, sdhc=True):
    _cmd(chip, 0, 0, 0)
    _cmd(chip, 8, 0x1AA)
    for _ in range(50):
        _cmd(chip, 55, 0)
        sta, _ = _cmd(chip, 41, 0x40FF8000)
        if rd(chip, SDIO + RESP1) & 0x80000000:
            break
    _cmd(chip, 2, 0, LONG)
    _cmd(chip, 3)
    rca = rd(chip, SDIO + RESP1) >> 16
    _cmd(chip, 7, rca << 16)
    return rca


def test_command_timing_and_flags(desc):
    card = SDCard("64M")
    chip = _sdio_chip(desc, card)
    sta, dt = _cmd(chip, 0, 0, 0)
    assert sta & CMDSENT and not sta & CMDACT
    assert dt == pytest.approx((48 + 8) * 2500, abs=1500)  # 400 kHz: 2.5 us per clock, + NCC
    sta, dt = _cmd(chip, 8, 0x1AA)
    assert sta & CMDREND and rd(chip, SDIO + RESPCMD) == 8 and rd(chip, SDIO + RESP1) == 0x1AA
    assert dt == pytest.approx((48 + 2 + 48) * 2500, abs=1500)
    _cmd(chip, 55, 0)
    sta, _ = _cmd(chip, 41, 0x40FF8000)
    assert sta & CCRCFAIL and not sta & CMDREND and rd(chip, SDIO + RESPCMD) == 0x3F   # R3: no CRC
    sta, dt = _cmd(chip, 9, 0, LONG)                       # CMD9 in idle: illegal -> no response
    assert sta & CTIMEOUT and dt == pytest.approx((48 + 64) * 2500, abs=1500)
    wr(chip, SDIO + ICR, 0x7FF)
    assert rd(chip, SDIO + STA) & 0x7FF == 0


def test_long_response_registers(desc):
    card = SDCard("64M")
    chip = _sdio_chip(desc, card)
    _cmd(chip, 0, 0, 0)
    _cmd(chip, 8, 0x1AA)
    for _ in range(20):
        _cmd(chip, 55, 0)
        _cmd(chip, 41, 0x40FF8000)
        if rd(chip, SDIO + RESP1) & 0x80000000:
            break
    sta, _ = _cmd(chip, 2, 0, LONG)
    assert sta & CMDREND
    words = [rd(chip, SDIO + r) for r in (RESP1, RESP2, RESP3, RESP4)]
    assert words == [(card.cid >> 96) & 0xFFFFFFFF, (card.cid >> 64) & 0xFFFFFFFF, (card.cid >> 32) & 0xFFFFFFFF,
                     card.cid & 0xFFFFFFFE]
    assert rd(chip, SDIO + RESPCMD) == 0x3F
    sta, _ = _cmd(chip, 3, 0, LONG)                        # short R6 where a long one was expected
    assert sta & CCRCFAIL


def test_empty_socket_times_out(desc):
    chip = _sdio_chip(desc, None)
    sta, _ = _cmd(chip, 0, 0, 0)
    assert sta & CMDSENT
    sta, _ = _cmd(chip, 8, 0x1AA)
    assert sta & CTIMEOUT


def test_unmuxed_pins_and_no_pll(desc):
    chip = _sdio_chip(desc, SDCard("64M"), pins=False)
    sta, _ = _cmd(chip, 8, 0x1AA)
    assert sta & CTIMEOUT and any("not muxed" in w for w in warns(chip))
    chip = _sdio_chip(desc, SDCard("64M"), pll=False)
    wr(chip, SDIO + ARG, 0)
    wr(chip, SDIO + CMD, 0 | CPSMEN)
    chip.run_for(1e6)
    assert rd(chip, SDIO + STA) & CMDACT and not rd(chip, SDIO + STA) & CMDSENT   # no SDIOCLK: stuck
    assert any("PLL48CLK" in w for w in warns(chip))
    wr(chip, RCC + 0x04, 16 | 336 << 6 | 7 << 24)
    wr(chip, RCC, rd(chip, RCC) | 1 << 24)
    chip.run_for(1e6)
    assert rd(chip, SDIO + STA) & CMDSENT


def _read_block_poll(chip, n_words=128, dctrl=1 | 2 | 9 << 4):
    out = []
    t0 = chip.now_ns()
    while True:
        sta = rd(chip, SDIO + STA)
        if sta & RXDAVL:
            out.append(rd(chip, SDIO + FIFO))
            continue
        if sta & (DATAEND | DCRCFAIL | DTIMEOUT | RXOVERR) or chip.now_ns() - t0 > 50e6:
            return sta, out
        chip.run_for(500)


def test_data_read_write_flags(desc):
    card = SDCard("64M")
    card.write_bytes(512 * 3, bytes(range(256)) * 2)
    chip = _sdio_chip(desc, card)
    rca = _identify(chip)
    wr(chip, SDIO + DTIMER, 0xFFFFFF)
    wr(chip, SDIO + DLEN, 512)
    wr(chip, SDIO + DCTRL, 1 | 2 | 9 << 4)
    sta = rd(chip, SDIO + STA)
    assert sta & RXACT and sta & RXFIFOE and rd(chip, SDIO + DCOUNT) == 512 and rd(chip, SDIO + FIFOCNT) == 128
    _cmd(chip, 17, 3)
    sta, words = _read_block_poll(chip)
    assert sta & DATAEND and sta & DBCKEND and not sta & RXACT and rd(chip, SDIO + DCOUNT) == 0
    data = b"".join(w.to_bytes(4, "little") for w in words)
    assert data == bytes(range(256)) * 2
    # write: CMD24 then data; FIFO fed on TXFIFOHE
    _cmd(chip, 24, 4)
    wr(chip, SDIO + ICR, 0x7FF)
    wr(chip, SDIO + DLEN, 512)
    wr(chip, SDIO + DCTRL, 1 | 9 << 4)
    assert rd(chip, SDIO + STA) & (TXACT | TXFIFOE | TXFIFOHE) == TXACT | TXFIFOE | TXFIFOHE
    blk = bytes((i * 3) & 0xFF for i in range(512))
    n = 0
    while not rd(chip, SDIO + STA) & (DATAEND | TXUNDERR | DCRCFAIL | DTIMEOUT):
        if rd(chip, SDIO + STA) & TXFIFOHE and n < 128:
            for _ in range(8):
                wr(chip, SDIO + FIFO, int.from_bytes(blk[4 * n:4 * n + 4], "little"))
                n += 1
        chip.run_for(500)
    assert rd(chip, SDIO + STA) & DATAEND and card.read_bytes(4 * 512, 512) == blk


def test_rx_overrun_and_hwfc(desc):
    card = SDCard("64M")
    chip = _sdio_chip(desc, card)
    _identify(chip)
    wr(chip, SDIO + DTIMER, 0xFFFFFF)
    wr(chip, SDIO + DLEN, 512)
    wr(chip, SDIO + DCTRL, 1 | 2 | 9 << 4)
    _cmd(chip, 17, 0)
    chip.run_for(20e6)                                      # nobody reads the FIFO: 32 words then overrun
    sta = rd(chip, SDIO + STA)
    assert sta & RXOVERR and sta & (1 << 17) and not sta & DATAEND     # RXFIFOF
    for _ in range(32):
        rd(chip, SDIO + FIFO)
    wr(chip, SDIO + ICR, 0x7FF)
    wr(chip, SDIO + CLKCR, 1 << 8 | 1 << 14 | 118)          # HWFC_EN: the clock stops while the FIFO is full
    wr(chip, SDIO + DCTRL, 1 | 2 | 9 << 4)
    _cmd(chip, 17, 0)
    chip.run_for(20e6)
    assert not rd(chip, SDIO + STA) & RXOVERR
    sta, words = _read_block_poll(chip)
    assert sta & DATAEND and len(words) == 128


def test_data_timeout_crc_and_wrong_width(desc):
    card = SDCard("64M")
    chip = _sdio_chip(desc, card)
    _identify(chip)
    wr(chip, SDIO + DTIMER, 400)                            # 1 ms at 400 kHz
    wr(chip, SDIO + DLEN, 512)
    wr(chip, SDIO + DCTRL, 1 | 2 | 9 << 4)
    chip.run_for(1.1e6)                                     # no read command: DTIMEOUT
    assert rd(chip, SDIO + STA) & DTIMEOUT and not rd(chip, SDIO + STA) & RXACT
    wr(chip, SDIO + ICR, 0x7FF)
    wr(chip, SDIO + DTIMER, 0xFFFFFF)
    card.crc_errors = 1
    wr(chip, SDIO + DCTRL, 1 | 2 | 9 << 4)
    _cmd(chip, 17, 0)
    sta, _ = _read_block_poll(chip)
    assert sta & DCRCFAIL
    wr(chip, SDIO + ICR, 0x7FF)
    wr(chip, SDIO + CLKCR, 1 << 8 | 1 << 11 | 118)          # host 4-bit, card still 1-bit
    wr(chip, SDIO + DCTRL, 1 | 2 | 9 << 4)
    _cmd(chip, 17, 0)
    sta, _ = _read_block_poll(chip)
    assert sta & DCRCFAIL


def test_irq_and_mask(desc):
    chip = _sdio_chip(desc, None)
    wr(chip, SDIO + MASK, CTIMEOUT)
    sta, _ = _cmd(chip, 8, 0x1AA)
    assert sta & CTIMEOUT and (16 + SDIO_IRQ) in chip.arch.asserted
    wr(chip, SDIO + ICR, CTIMEOUT)
    assert (16 + SDIO_IRQ) not in chip.arch.asserted


def test_ck_wave_summary_and_reset(desc):
    chip = _sdio_chip(desc, SDCard("64M"))
    chip.run_for(1e6)
    wr(chip, SDIO + CLKCR, 0)                               # stop the clock: one wave summary for PC12
    waves = [e for e in chip.log if e.kind == "pin" and e.fields.get("field") == "wave" and e.pin == "PC12"]
    assert waves and waves[-1].fields["freq_hz"] == pytest.approx(400e3) and waves[-1].fields["edges"] > 700
    wr(chip, SDIO + CLKCR, 1 << 8 | 118)
    wr(chip, RCC + 0x24, 1 << 11)                           # APB2RSTR.SDIORST
    wr(chip, RCC + 0x24, 0)
    assert rd(chip, SDIO + CLKCR) == 0 and rd(chip, SDIO + POWER) == 0


# ------------------------------------------------------------------------------------------------ samples
def _need(p: Path):
    if not p.exists():
        pytest.skip(f"{p.name} not built")
    return p


@pytest.mark.parametrize("opts,head", [("size=64M", "SD v2 SDHC"), ("size=64M,kind=sdsc", "SD v2 SDSC"),
                                       ("size=32M,kind=sdsc,version=1", "SD v1 SDSC")])
def test_sample_sdio_card(opts, head):
    fw = _need(STM_SAMPLES / "sdio_card.elf")
    s = Session("chips/stm32f405zg.yaml", fw, quiet=True)
    card = s.attach_sd_card(f"SDIO:{opts}")
    s.run_for(40e6)
    out = s.uart_output("USART1").decode()
    assert out.startswith(head) and "WRITE ok\r\nREAD ok CHIP_EM SDIO block\r\nDMA ok\r\ndone" in out, out
    assert card.ops[0] == ("write", 2, 1) and card.ops[-1] == ("read", 2, 2)
    assert card.bus_width == 4 and card.current_state() == "tran"


def test_board_stm32_sdcard(tmp_path):
    _need(STM_SAMPLES / "sdio_card.elf")
    import yaml
    from chipem.board import parse_board
    data = yaml.safe_load((ROOT / "boards" / "stm32_sdcard.yaml").read_text())
    data["devices"][0]["save"] = str(tmp_path / "out.img")
    s = Session.from_board(parse_board(data, base=ROOT / "boards"))
    s.run_for(40e6)
    assert s.uart_output("USART1").decode().endswith("DMA ok\r\ndone\r\n")
    s.close()
    img = (tmp_path / "out.img").read_bytes()
    assert img[1024:1042] == b"CHIP_EM SDIO block"


def test_hal_sd_sample():
    fw = _need(HAL_SAMPLES / "sd_hal.elf")
    s = Session("chips/stm32f405zg.yaml", fw, quiet=True)
    card = s.attach_sd_card("SDIO:size=64M")
    s.run_for(60e6)
    out = s.uart_output("USART1").decode()
    assert out == ("SD HAL\r\nCARD v2 SDHC blocks=131072 bs=512\r\nWRITE ok\r\nREAD ok\r\nMULTI ok\r\nERASE ok\r\n"
                   "STATUS bus=4 class=4\r\ndone\r\n"), out
    assert ("erase", 200, 2) in card.ops


def test_hal_sd_sample_empty_socket():
    fw = _need(HAL_SAMPLES / "sd_hal.elf")
    s = Session("chips/stm32f405zg.yaml", fw, quiet=True)
    s.run_for(30e6)
    assert s.uart_output("USART1").decode() == "SD HAL\r\nINIT error 00000004\r\n"     # CMD_RSP_TIMEOUT
