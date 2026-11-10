"""Kinetis SDHC host (chipem/periph/kinetis/sdhc.py) with the SD card device: registers and reset values, SD clock
from SIM_SOPT2/SYSCTL, INITA, command path (CC/CTOE/CCE/CIE, CMDRSP mapping), buffer watermarks (BREN/BWEN), data
timeouts, internal DMA (simple DMA, ADMA2, descriptor errors), IRQ gating, resets, FEVT, and the sample/board."""
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.devices.sdcard import SDCard
from chipem.session import Session

ROOT = Path(__file__).resolve().parents[1]
K22 = ROOT / "chips" / "mk22fn1m0vll12.yaml"
FW = ROOT / "firmware" / "samples" / "build" / "sdhc_card.elf"
SDHC = 0x400B1000
DSADDR, BLKATTR, CMDARG, XFERTYP, CMDRSP0, CMDRSP1, CMDRSP2, CMDRSP3, DATPORT = (0, 4, 8, 0xC, 0x10, 0x14, 0x18,
                                                                                0x1C, 0x20)
PRSSTAT, PROCTL, SYSCTL, IRQSTAT, IRQSTATEN, IRQSIGEN, AC12ERR, HTCAPBLT, WML = (0x24, 0x28, 0x2C, 0x30, 0x34, 0x38,
                                                                                0x3C, 0x40, 0x44)
FEVT, ADMAES, ADSADDR, VENDOR, HOSTVER = 0x50, 0x54, 0x58, 0xC0, 0xFC
SIM_SOPT2, SIM_SCGC3, SIM_SCGC5 = 0x40048004, 0x40048030, 0x40048038
PORTE = 0x4004D000
RAM = 0x20000000
CC, TC, DINT, BWR, BRR, CINS = 1, 2, 8, 0x10, 0x20, 0x40
CTOE, CCE, CIE, DTOE, DCE, AC12E, DMAE = 1 << 16, 1 << 17, 1 << 19, 1 << 20, 1 << 21, 1 << 24, 1 << 28
CIHB, CDIHB, DLA, SDSTB, BWEN, BREN, PS_CINS = 1, 2, 4, 8, 0x400, 0x800, 1 << 16
R48, R48B, R136, CCCEN, CICEN, DPSEL, READ = 2 << 16, 3 << 16, 1 << 16, 1 << 19, 1 << 20, 1 << 21, 0x10
DMAEN, BCEN, AC12EN, MSBSEL = 1, 2, 4, 0x20
R1 = R48 | CCCEN | CICEN
SDHC_IRQ = 81


@pytest.fixture(scope="module")
def desc():
    return load_chip(K22)


def rd(chip, off, n=4):
    return chip.bus.read(SDHC + off, n)


def wr(chip, off, v, n=4):
    chip.bus.write(SDHC + off, n, v)


def _chip(desc, card=None, *, pins=True):
    chip = Chip(desc, clock_hz=120e6)
    chip.clock_override = 120e6                          # MCG frozen: 120 MHz core as the samples assume
    img = bytearray(0x502)
    img[0:8] = (0x20010000).to_bytes(4, "little") + (0x501).to_bytes(4, "little")
    img[0x40C:0x410] = b"\xfe\xff\xff\xff"                # FSEC: unsecured
    img[0x500:0x502] = b"\xfe\xe7"                        # b .
    from chipem.firmware import FirmwareImage
    chip.load_firmware(FirmwareImage([(0, bytes(img))], entry=None, source="spin", fmt="bin"))
    chip.reset()
    chip.peripherals["MCG"].frozen = True
    chip.clock.set_hz(120e6)
    chip.bus.write(SIM_SCGC3, 4, chip.bus.read(SIM_SCGC3, 4) | 1 << 17)
    chip.bus.write(SIM_SCGC5, 4, chip.bus.read(SIM_SCGC5, 4) | 1 << 13)
    if pins:
        for p in range(6):
            chip.bus.write(PORTE + 4 * p, 4, 4 << 8 | (0 if p == 2 else 3))
    if card is not None:
        chip.peripherals["SDHC"].attach_card(card)
    return chip


def _clock(chip, fs=0x20, dvs=4):
    wr(chip, SYSCTL, 0xE << 16 | fs << 8 | dvs << 4 | 8)   # 120 MHz / (64 x 5) = 375 kHz


def _cmd(chip, idx, arg=0, flags=R1, limit=5e6):
    wr(chip, IRQSTAT, 0xFFFFFFFF)
    wr(chip, CMDARG, arg)
    t0 = chip.now_ns()
    wr(chip, XFERTYP, idx << 24 | flags)
    while not rd(chip, IRQSTAT) & (CC | CTOE):
        chip.run_for(2000)
        assert chip.now_ns() - t0 < limit
    return rd(chip, IRQSTAT), chip.now_ns() - t0


def _identify(chip):
    _cmd(chip, 0, 0, 0)
    _cmd(chip, 8, 0x1AA)
    for _ in range(1000):                                  # the card needs 1 ms to power up
        _cmd(chip, 55, 0)
        _cmd(chip, 41, 0x40FF8000, R48)
        if rd(chip, CMDRSP0) & 0x80000000:
            break
    assert rd(chip, CMDRSP0) & 0x80000000
    _cmd(chip, 2, 0, R136 | CCCEN)
    _cmd(chip, 3)
    rca = rd(chip, CMDRSP0) >> 16
    _cmd(chip, 7, rca << 16, R48B | CCCEN | CICEN)
    while rd(chip, PRSSTAT) & CDIHB:
        chip.run_for(2000)
    return rca


# ------------------------------------------------------------------------------------------------ registers
def test_reset_values_and_capabilities(desc):
    chip = _chip(desc)
    assert rd(chip, SYSCTL) == 0x8008 and rd(chip, PROCTL) == 0x20 and rd(chip, IRQSTATEN) == 0x117F013F
    assert rd(chip, HTCAPBLT) == 0x07F30000 and rd(chip, WML) == 0x00100010 and rd(chip, HOSTVER) == 0x1201
    assert rd(chip, BLKATTR) >> 16 == 1                   # BLKCNT reads 1 while MSBSEL = 0
    assert rd(chip, VENDOR) == 1
    ps = rd(chip, PRSSTAT)
    assert ps & SDSTB and not ps & PS_CINS and ps >> 24 == 0xFF
    assert not rd(chip, IRQSTAT) & 0xC0                    # CINSSEN / CRMSEN are 0 after reset
    wr(chip, IRQSTATEN, 0x117F01FF)
    assert rd(chip, IRQSTAT) & 0x80                        # CRM: no card in the socket
    chip.peripherals["SDHC"].attach_card(SDCard("64M"))
    assert rd(chip, PRSSTAT) & PS_CINS and rd(chip, IRQSTAT) & 0xC0 == CINS


def test_sd_clock_from_sopt2_and_sysctl(desc):
    chip = _chip(desc)
    h = chip.peripherals["SDHC"]
    _clock(chip)
    assert h.sdclk_hz() == pytest.approx(375e3)
    _clock(chip, 0x01, 2)
    assert h.sdclk_hz() == pytest.approx(20e6)
    _clock(chip, 0x00, 0)
    assert h.sdclk_hz() == pytest.approx(120e6) and any("50 MHz" in e.fields.get("msg", "")
                                                        for e in chip.log if e.kind == "warn")
    chip.bus.write(SIM_SOPT2, 4, chip.bus.read(SIM_SOPT2, 4) | 2 << 28)        # OSCERCLK (8 MHz)
    _clock(chip, 0x01, 0)
    assert h.sdclk_hz() == pytest.approx(4e6)
    wr(chip, SYSCTL, rd(chip, SYSCTL) & ~8)
    assert h.sdclk_hz() == 0


def test_inita_80_clocks(desc):
    chip = _chip(desc)
    _clock(chip)
    wr(chip, SYSCTL, rd(chip, SYSCTL) | 1 << 27)
    assert rd(chip, SYSCTL) & 1 << 27
    chip.run_for(80 / 375e3 * 1e9 - 5000)
    assert rd(chip, SYSCTL) & 1 << 27
    chip.run_for(10000)
    assert not rd(chip, SYSCTL) & 1 << 27


def test_command_timing_responses_and_errors(desc):
    card = SDCard("64M")
    chip = _chip(desc, card)
    _clock(chip)
    T = 1e9 / 375e3
    st, dt = _cmd(chip, 0, 0, 0)
    assert st & CC and dt == pytest.approx((48 + 8) * T, abs=2000)
    st, dt = _cmd(chip, 8, 0x1AA)
    assert st & CC and not st & (CCE | CIE) and rd(chip, CMDRSP0) == 0x1AA
    assert dt == pytest.approx((48 + 2 + 48) * T, abs=2000)
    _cmd(chip, 55)
    st, _ = _cmd(chip, 41, 0x40FF8000, R48 | CCCEN)          # R3 with CRC check: CCE (and CC)
    assert st & CC and st & CCE
    st, dt = _cmd(chip, 9, 0, R136 | CCCEN)                  # CMD9 in idle: no response
    assert st & CTOE and not st & CC and dt == pytest.approx((48 + 64) * T, abs=2000)
    assert not rd(chip, PRSSTAT) & CIHB


def test_cid_csd_response_mapping(desc):
    card = SDCard("64M")
    chip = _chip(desc, card)
    _clock(chip)
    _cmd(chip, 0, 0, 0)
    _cmd(chip, 8, 0x1AA)
    for _ in range(20):
        _cmd(chip, 55)
        _cmd(chip, 41, 0x40FF8000, R48)
        if rd(chip, CMDRSP0) & 0x80000000:
            break
    st, _ = _cmd(chip, 2, 0, R136 | CCCEN)
    assert st & CC and not st & CCE
    v = card.cid >> 8
    assert [rd(chip, o) for o in (CMDRSP0, CMDRSP1, CMDRSP2)] == [v & 0xFFFFFFFF, (v >> 32) & 0xFFFFFFFF,
                                                                  (v >> 64) & 0xFFFFFFFF]
    assert rd(chip, CMDRSP3) & 0xFFFFFF == (v >> 96) & 0xFFFFFF
    st, _ = _cmd(chip, 3, 0, R48 | CICEN)
    rca = rd(chip, CMDRSP0) >> 16
    st, _ = _cmd(chip, 13, rca << 16, R48 | CICEN)
    assert st & CC and not st & CIE
    st, _ = _cmd(chip, 13, rca << 16, R136 | CICEN | CCCEN)  # long expected, short response
    assert st & CCE


def test_empty_socket_irq_gating(desc):
    chip = _chip(desc)
    _clock(chip)
    wr(chip, IRQSIGEN, CTOE)
    _cmd(chip, 8, 0x1AA)
    assert (16 + SDHC_IRQ) in chip.arch.asserted
    wr(chip, IRQSTAT, CTOE)
    assert (16 + SDHC_IRQ) not in chip.arch.asserted
    wr(chip, IRQSTATEN, 0x117F013F & ~CTOE)                  # disabled status bits are not set
    wr(chip, IRQSTAT, 0xFFFFFFFF)
    wr(chip, XFERTYP, 8 << 24 | R1)
    chip.run_for(1e6)
    assert not rd(chip, IRQSTAT) & CTOE and not rd(chip, PRSSTAT) & CIHB


def test_unmuxed_pins_time_out(desc):
    chip = _chip(desc, SDCard("64M"), pins=False)
    _clock(chip)
    st, _ = _cmd(chip, 8, 0x1AA)
    assert st & CTOE and any("not muxed" in e.fields.get("msg", "") for e in chip.log if e.kind == "warn")


def test_cpu_buffer_watermarks(desc):
    card = SDCard("64M")
    card.write_bytes(7 * 512, bytes(range(256)) * 2)
    chip = _chip(desc, card)
    _clock(chip)
    _identify(chip)
    wr(chip, WML, 8 << 16 | 8)
    wr(chip, BLKATTR, 1 << 16 | 512)
    st, _ = _cmd(chip, 17, 7, R1 | DPSEL | READ)
    ps = rd(chip, PRSSTAT)
    assert ps & CDIHB and ps & DLA and ps & 0x200                # RTA
    words = []
    while len(words) < 128:
        if rd(chip, PRSSTAT) & BREN:
            assert rd(chip, IRQSTAT) & BRR
            wr(chip, IRQSTAT, BRR)
            words += [rd(chip, DATPORT) for _ in range(8)]
        else:
            chip.run_for(5000)
    while not rd(chip, IRQSTAT) & TC:
        chip.run_for(5000)
    assert b"".join(w.to_bytes(4, "little") for w in words) == bytes(range(256)) * 2
    assert not rd(chip, PRSSTAT) & (CDIHB | DLA | 0x200)
    # big-endian mode swaps the bytes of each word
    wr(chip, PROCTL, rd(chip, PROCTL) & ~0x30)
    _cmd(chip, 17, 7, R1 | DPSEL | READ)
    while not rd(chip, PRSSTAT) & BREN:
        chip.run_for(5000)
    assert rd(chip, DATPORT) == 0x00010203


def test_write_and_overflow_free_buffer(desc):
    card = SDCard("64M")
    chip = _chip(desc, card)
    _clock(chip)
    _identify(chip)
    wr(chip, BLKATTR, 1 << 16 | 512)
    _cmd(chip, 24, 9, R1 | DPSEL)
    assert rd(chip, PRSSTAT) & BWEN and rd(chip, IRQSTAT) & BWR
    blk = bytes((3 * i) & 0xFF for i in range(512))
    for i in range(128):                                   # the whole block at once: buffer holds 128 words
        wr(chip, DATPORT, int.from_bytes(blk[4 * i:4 * i + 4], "little"))
    assert not rd(chip, PRSSTAT) & BWEN
    while not rd(chip, IRQSTAT) & (TC | DTOE | DCE):
        chip.run_for(10000)
    assert rd(chip, IRQSTAT) & TC and card.read_bytes(9 * 512, 512) == blk


def test_data_timeout_and_crc_error(desc):
    card = SDCard("64M", read_time="50ms")
    chip = _chip(desc, card)
    _clock(chip)
    _identify(chip)
    wr(chip, SYSCTL, (rd(chip, SYSCTL) & ~0xF0000) | 0 << 16)     # DTOCV 0: 2^13 SDCLK = 21.8 ms
    wr(chip, BLKATTR, 1 << 16 | 512)
    _cmd(chip, 17, 0, R1 | DPSEL | READ)
    chip.run_for(25e6)
    assert rd(chip, IRQSTAT) & DTOE and not rd(chip, PRSSTAT) & (CDIHB | DLA)
    card.read_ready_ns = 50e3
    card.command(12, 0, chip.now_ns())
    card.crc_errors = 1
    wr(chip, IRQSTAT, 0xFFFFFFFF)
    _cmd(chip, 17, 0, R1 | DPSEL | READ)
    for _ in range(200):
        if rd(chip, IRQSTAT) & DCE:
            break
        if rd(chip, PRSSTAT) & BREN:
            rd(chip, DATPORT)
        chip.run_for(100e3)                                # 1-bit bus at 375 kHz: ~11 ms per block
    assert rd(chip, IRQSTAT) & DCE


def test_sdma_and_adma2(desc):
    card = SDCard("64M")
    chip = _chip(desc, card)
    _clock(chip, 0x01, 2)
    _identify(chip)
    data = bytes((7 * i + 1) & 0xFF for i in range(1024))
    chip.bus.write_bytes(RAM + 0x1000, data)
    wr(chip, DSADDR, RAM + 0x1000)
    wr(chip, BLKATTR, 2 << 16 | 512)
    _cmd(chip, 25, 20, R1 | DPSEL | MSBSEL | BCEN | AC12EN | DMAEN)
    while not rd(chip, IRQSTAT) & TC:
        chip.run_for(10000)
    st = rd(chip, IRQSTAT)
    assert st & DINT and not st & AC12E and rd(chip, DSADDR) == RAM + 0x1400
    assert card.read_bytes(20 * 512, 1024) == data and rd(chip, BLKATTR) >> 16 == 0
    while card.busy(chip.now_ns()):
        chip.run_for(10000)
    # ADMA2: link + two tran descriptors into two separate buffers
    desc_tab = RAM + 0x3000
    chip.bus.write(desc_tab + 0, 4, (3 << 4) | 1)            # link, valid
    chip.bus.write(desc_tab + 4, 4, desc_tab + 0x40)
    chip.bus.write(desc_tab + 0x40, 4, 512 << 16 | 2 << 4 | 1 | 4)   # tran, valid, int
    chip.bus.write(desc_tab + 0x44, 4, RAM + 0x5000)
    chip.bus.write(desc_tab + 0x48, 4, 512 << 16 | 2 << 4 | 3)       # tran, valid, end
    chip.bus.write(desc_tab + 0x4C, 4, RAM + 0x6000)
    wr(chip, PROCTL, (rd(chip, PROCTL) & ~0x300) | 2 << 8)
    wr(chip, ADSADDR, desc_tab)
    wr(chip, BLKATTR, 2 << 16 | 512)
    _cmd(chip, 18, 20, R1 | DPSEL | READ | MSBSEL | BCEN | AC12EN | DMAEN)
    while not rd(chip, IRQSTAT) & (TC | DMAE):
        chip.run_for(10000)
    assert rd(chip, IRQSTAT) & (TC | DINT) == TC | DINT
    assert chip.bus.read_bytes(RAM + 0x5000, 512) == data[:512] and chip.bus.read_bytes(RAM + 0x6000, 512) == data[512:]
    assert rd(chip, ADSADDR) == desc_tab + 0x50 and card.current_state() == "tran"


def test_adma_invalid_descriptor_and_adma1(desc):
    card = SDCard("64M")
    chip = _chip(desc, card)
    _clock(chip, 0x01, 2)
    _identify(chip)
    chip.bus.write(RAM + 0x3000, 4, 512 << 16 | 2 << 4)      # tran without Valid
    wr(chip, PROCTL, (rd(chip, PROCTL) & ~0x300) | 2 << 8)
    wr(chip, ADSADDR, RAM + 0x3000)
    wr(chip, BLKATTR, 1 << 16 | 512)
    _cmd(chip, 17, 0, R1 | DPSEL | READ | DMAEN)
    while not rd(chip, IRQSTAT) & (TC | DMAE):
        chip.run_for(10000)
    assert rd(chip, IRQSTAT) & DMAE and rd(chip, ADMAES) == 1 | 8 and rd(chip, ADSADDR) == RAM + 0x3000
    wr(chip, SYSCTL, rd(chip, SYSCTL) | 1 << 26)              # RSTD
    assert not rd(chip, PRSSTAT) & (CDIHB | DLA) and not rd(chip, SYSCTL) & 1 << 26
    card.command(12, 0, chip.now_ns())
    wr(chip, PROCTL, (rd(chip, PROCTL) & ~0x300) | 1 << 8)  # ADMA1: not modelled -> DMAE
    wr(chip, IRQSTAT, 0xFFFFFFFF)
    _cmd(chip, 17, 0, R1 | DPSEL | READ | DMAEN)
    chip.run_for(1e5)
    assert rd(chip, IRQSTAT) & DMAE and any("ADMA1" in e.fields.get("msg", "") for e in chip.log if e.kind == "warn")


def test_fevt_and_resets(desc):
    chip = _chip(desc, SDCard("64M"))
    _clock(chip)
    wr(chip, FEVT, 1 << 1 | 1 << 20)                        # AC12TOE + DTOE
    assert rd(chip, AC12ERR) & 2 and rd(chip, IRQSTAT) & DTOE and rd(chip, FEVT) == 0
    wr(chip, WML, 0x00200020)
    wr(chip, SYSCTL, rd(chip, SYSCTL) | 1 << 24)            # RSTA
    assert rd(chip, WML) == 0x00100010 and rd(chip, SYSCTL) == 0x8008 and rd(chip, HTCAPBLT) == 0x07F30000
    assert not rd(chip, IRQSTAT) & DTOE and rd(chip, AC12ERR) == 0


# ------------------------------------------------------------------------------------------------ sample / board
def _need():
    if not FW.exists():
        pytest.skip("sdhc_card.elf not built")


@pytest.mark.parametrize("opts,head", [("size=64M", "SD v2 SDHC"), ("size=64M,kind=sdsc", "SD v2 SDSC"),
                                       ("size=32M,kind=sdsc,version=1", "SD v1 SDSC")])
def test_sample_sdhc_card(opts, head):
    _need()
    s = Session(K22, FW, clock_hz=120e6, quiet=True)
    card = s.attach_sd_card(f"SDHC:{opts}")
    s.run_for(30e6)
    out = s.uart_output("UART0").decode()
    assert out.startswith(head) and out.endswith(
        "WRITE ok\r\nREAD ok CHIP_EM SDHC block\r\nSDMA ok\r\nADMA ok\r\ndone\r\n"), out
    assert card.ops[:2] == [("write", 2, 1), ("read", 2, 1)] and ("write", 4, 2) in card.ops
    assert card.bus_width == 4 and not [e for e in s.events(kinds={"warn"})]


def test_board_k22_sdcard(tmp_path):
    _need()
    img = tmp_path / "card.img"
    img.write_bytes(b"\xee" * 4096)
    import yaml
    from chipem.board import parse_board
    data = yaml.safe_load((ROOT / "boards" / "k22_sdcard.yaml").read_text())
    data["devices"][0].update(image=str(img), writeback=1)
    s = Session.from_board(parse_board(data, base=ROOT / "boards"))
    s.run_for(30e6)
    assert s.uart_output("A.UART0").decode().endswith("ADMA ok\r\ndone\r\n")
    s.close()
    out = img.read_bytes()
    assert out[:16] == b"\xee" * 16 and out[1024:1042] == b"CHIP_EM SDHC block" and len(out) == 4096
