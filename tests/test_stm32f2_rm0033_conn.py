"""STM32F2 connectivity / crypto blocks (DCMI, CRYP, HASH, SDIO, bxCAN, Ethernet, OTG_FS, OTG_HS, FSMC) checked against
RM0033 Rev 10 (docs/datasheets/st/RM0033_stm32f2_reference_manual.pdf): one regression test per discrepancy found,
each citing its section, plus spot checks of the register sets the manual confirmed.  F4 behaviour stays unchanged
(the fixes are F2 chip-file parameters)."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.firmware import FirmwareImage
from engine_util import asm

ROOT = Path(__file__).resolve().parent.parent
FLASH = 0x08000000
RCC = 0x40023800
AHB1ENR, AHB2ENR, AHB3ENR = 0x30, 0x34, 0x38
ETH, OTG_FS, OTG_HS, FSMC = 0x40028000, 0x50000000, 0x40040000, 0xA0000000
CRYP, HASH = 0x50060000, 0x50060400
_D: dict = {}


def make_chip(fn: str = "stm32f207zg") -> Chip:
    if fn not in _D:
        _D[fn] = load_chip(ROOT / "chips" / f"{fn}.yaml")
    code = asm("spin: b spin", FLASH + 0x200)
    img = bytearray(0x200 + len(code))
    img[0:8] = struct.pack("<II", 0x20020000, (FLASH + 0x200) | 1)
    img[0x200:] = code
    chip = Chip(_D[fn])
    chip.load_firmware(FirmwareImage([(FLASH, bytes(img))]))
    chip.reset()
    return chip


def rd(chip, a):
    return chip.bus.read(a, 4)


def wr(chip, a, v):
    chip.bus.write(a, 4, v)


def warns(chip) -> list[str]:
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "warn"]


# ------------------------------------------------------------------------------------------------------------ ETH
def test_eth_mdc_clock_range_1xx_reserved():
    """28.4.1 Table 140 / 28.8.1 ETH_MACMIIAR.CR: 000 /42, 001 /62, 010 /16, 011 /26; 100-111 reserved on the F2 (the
    F4's 100 = HCLK/102 for 150-168 MHz does not exist): writing CR = 100 logs a `warn`; on the F407 it is legal."""
    for fn, reserved in (("stm32f207zg", True), ("stm32f407zg", False)):
        chip = make_chip(fn)
        wr(chip, RCC + AHB1ENR, rd(chip, RCC + AHB1ENR) | (7 << 25))
        eth = chip.peripherals["Ethernet_MAC"]
        assert eth.MDC_DIV.get(1) == 62 and eth.MDC_DIV.get(3) == 26
        wr(chip, ETH + 0x10, (4 << 2) | 1)                     # CR = 100, MB
        assert any("MACMIIAR.CR=4 is reserved" in w for w in warns(chip)) is reserved
    chip = make_chip()
    assert set(chip.peripherals["Ethernet_MAC"].MDC_DIV) == {0, 1, 2, 3}


def test_eth_register_set_matches_rm0033():
    """28.8.5 Table 148: the F2 MAC has MACDBGR (0x34), PTPTSSR (0x728), PTPPPSCR (0x72C), DMARSWTR (0x1024) and
    DMABMR.EDFE (enhanced descriptors, bit 7); reset values MACCR 0x8000, MACA0HR 0x8000FFFF, PTPTSCR 0x2000, DMABMR
    0x00020101."""
    chip = make_chip()
    eth = chip.peripherals["Ethernet_MAC"]
    for off in (0x34, 0x728, 0x72C, 0x1024):
        assert eth.reg_at(off) is not None, hex(off)
    wr(chip, RCC + AHB1ENR, rd(chip, RCC + AHB1ENR) | (7 << 25))
    assert rd(chip, ETH + 0x00) == 0x8000 and rd(chip, ETH + 0x40) == 0x8000FFFF
    assert rd(chip, ETH + 0x700) == 0x2000
    assert eth.regs["DMABMR"].reset == 0x00020101 and not eth.regs["DMABMR"].ro_mask & (1 << 7)


# ------------------------------------------------------------------------------------------------------------ OTG
def test_otg_fs_gusbcfg_reset_0x1440():
    """29.16.2 OTG_FS_GUSBCFG: reset value 0x0000 1440 (TRDT = 5, PHYSEL = 1); RM0090 34.16.2 gives 0x0A40 for the F4
    (kept there).  The OTG_HS GUSBCFG is 0x1440 on both (30.12.2)."""
    for fn, fs in (("stm32f207zg", 0x1440), ("stm32f407zg", 0x0A40)):
        chip = make_chip(fn)
        wr(chip, RCC + AHB2ENR, 1 << 7)
        wr(chip, RCC + AHB1ENR, rd(chip, RCC + AHB1ENR) | (1 << 29))
        assert rd(chip, OTG_FS + 0x0C) == fs
        assert rd(chip, OTG_HS + 0x0C) == 0x1440


def test_otg_reset_values_and_sizes_rm0033():
    """29.16 / 30.12: GOTGCTL 0x00010000, GRSTCTL 0x80000000, GINTSTS 0x04000020, CID FS 0x1200 / HS 0x1100, GRXFSIZ
    FS 0x200 / HS 0x400, HPTXFSIZ FS 0x02000400 / HS 0x02000800, DIEPTXFx FS 0x02000200 / HS 0x02000400; GCCFG FS
    PWRDWN, VBUSASEN, VBUSBSEN, SOFOUTEN, NOVBUSSENS (bit 17 reserved), HS also I2CPADEN (17); FS 4 endpoints / 8
    channels / 1.25 KB FIFO RAM, HS 6 endpoints / 12 channels / 4 KB (29.1 / 30.2)."""
    chip = make_chip()
    wr(chip, RCC + AHB2ENR, 1 << 7)
    wr(chip, RCC + AHB1ENR, rd(chip, RCC + AHB1ENR) | (1 << 29))
    exp = {OTG_FS: (0x1200, 0x200, 0x02000400, 0x02000200), OTG_HS: (0x1100, 0x400, 0x02000800, 0x02000400)}
    for base, (cid, rxf, hptx, txf1) in exp.items():
        assert rd(chip, base + 0x00) == 0x00010000 and rd(chip, base + 0x10) == 0x80000000
        assert rd(chip, base + 0x14) == 0x04000020
        assert (rd(chip, base + 0x3C), rd(chip, base + 0x24), rd(chip, base + 0x100), rd(chip, base + 0x104)) == \
            (cid, rxf, hptx, txf1)
        wr(chip, base + 0x38, 0xFFFFFFFF)
        assert rd(chip, base + 0x38) == (0x003F0000 if base == OTG_HS else 0x003D0000)
        wr(chip, base + 0x38, 0)
    fs, hs = chip.peripherals["OTG_FS"], chip.peripherals["OTG_HS"]
    assert (fs.n_eps, fs.n_channels, fs.fifo_words) == (4, 8, 320)
    assert (hs.n_eps, hs.n_channels, hs.fifo_words) == (6, 12, 1024)


# ------------------------------------------------------------------------------------------------------------ FSMC
def test_fsmc_bcr_cpsize_and_wrapmod_writable():
    """31.5.6 FSMC_BCRx: CPSIZE[2:0] (bits 18:16) and WRAPMOD (bit 10) are rw in every bank, bit 7 reserved (reset 1);
    the F2 SVD marks CPSIZE (and BCR1 WRAPMOD) read-only."""
    chip = make_chip()
    wr(chip, RCC + AHB3ENR, 1)
    for x in range(4):
        a = FSMC + 8 * x
        rst = rd(chip, a)
        wr(chip, a, rst | (0x5 << 16) | (1 << 10))
        assert rd(chip, a) == rst | (0x5 << 16) | (1 << 10), x
        wr(chip, a, 0xFFFFFFFF)
        assert rd(chip, a) == 0x000FFFFF                            # bit 7 reserved (reads its reset value 1)
        wr(chip, a, rst)


def test_fsmc_bwtr_busturn_writable_datlat_clkdiv_reserved():
    """31.5.6 FSMC_BWTRx: ACCMOD (29:28), BUSTURN (19:16), DATAST, ADDHLD, ADDSET rw; bits 27:20 reserved (the F2 SVD
    has DATLAT / CLKDIV there and BUSTURN read-only).  Reset 0x0FFFFFFF."""
    chip = make_chip()
    wr(chip, RCC + AHB3ENR, 1)
    for x in range(4):
        a = FSMC + 0x104 + 8 * x
        assert rd(chip, a) == 0x0FFFFFFF
        wr(chip, a, 0)
        assert rd(chip, a) == 0x0FF00000                            # 27:20 keep their reset value
        wr(chip, a, 0x10050000)
        assert rd(chip, a) == 0x1FF50000


def test_fsmc_reset_values_and_pccard_registers():
    """31.5.6 / 31.6.8: BCR1 0x30DB, BCR2-4 0x30D2, BTRx 0x0FFFFFFF, PCRx 0x18, SRx 0x40, PMEMx / PATTx / PIO4
    0xFCFCFCFC, ECCR2/3 0."""
    chip = make_chip()
    wr(chip, RCC + AHB3ENR, 1)
    assert [rd(chip, FSMC + 8 * x) for x in range(4)] == [0x30DB, 0x30D2, 0x30D2, 0x30D2]
    assert all(rd(chip, FSMC + 4 + 8 * x) == 0x0FFFFFFF for x in range(4))
    for x in (2, 3, 4):
        b = FSMC + 0x40 + 0x20 * (x - 1)
        assert (rd(chip, b), rd(chip, b + 4), rd(chip, b + 8), rd(chip, b + 0xC)) == (0x18, 0x40, 0xFCFCFCFC,
                                                                                      0xFCFCFCFC)
    assert rd(chip, FSMC + 0xB0) == 0xFCFCFCFC


# ------------------------------------------------------------------------------------------------ CRYP / HASH
def test_cryp_f2_register_set_no_gcm_ccm():
    """19.6.1 CRYP_CR: ALGOMODE[2:0] only (bits 5:3; no ALGOMODE[3] / GCM_CCMPH, bits 31:16 reserved), KEYSIZE,
    DATATYPE, FFLUSH, CRYPEN; SR reset 0x3, RISR 0x1 (19.6.2 / 19.6.7); registers 0x00-0x4C only (Table 75: no
    CSGCMCCMxR / CSGCMxR context registers)."""
    chip = make_chip("stm32f217zg")
    wr(chip, RCC + AHB2ENR, (1 << 4) | (1 << 5))
    wr(chip, CRYP, 0xFFFFFFFF & ~(1 << 15))
    assert rd(chip, CRYP) == 0x3FC                                  # FFLUSH reads 0; 31:16, 13:10, 1:0 reserved
    assert rd(chip, CRYP + 4) == 0x3 and rd(chip, CRYP + 0x18) == 0x1
    cryp = chip.peripherals["CRYP"]
    assert max(r.offset for r in cryp.regs.values()) == 0x4C


def test_hash_f2_register_set_sha1_md5_only():
    """21.4.1 HASH_CR: ALGO is one bit (7: SHA-1 / MD5; no ALGO[1] at bit 18, no MDMAT); 21.4.7 HASH_CSR0..CSR50
    (0x0F8-0x1C0), CSR0 resets to 0x00000002; HASH_HR0-4 only at 0x0C-0x1C (no HASH_HR0-7 at 0x310); SR 0x1."""
    chip = make_chip("stm32f217zg")
    wr(chip, RCC + AHB2ENR, (1 << 4) | (1 << 5))
    h = chip.peripherals["HASH"]
    assert not h.sha2
    csr = sorted(r.offset for n, r in h.regs.items() if n.startswith("CSR"))
    assert csr[0] == 0xF8 and csr[-1] == 0x1C0 and len(csr) == 51
    assert all(r.offset < 0x200 for r in h.regs.values())
    assert rd(chip, HASH + 0xF8) == 0x2 and rd(chip, HASH + 0x24) == 0x1
    wr(chip, HASH, (1 << 18) | (1 << 13) | (1 << 7))
    assert not rd(chip, HASH) & ((1 << 18) | (1 << 13))


# ------------------------------------------------------------------------------------------ SDIO / CAN / DCMI
def test_sdio_can_dcmi_register_layouts():
    """26.9 SDIO (CLKCR 14:0, CMD 14:0, DLEN 24:0, DCTRL 11:0, ICR 23:22 + 10:0, MASK 23:0, FIFOCNT 0x48), 27.9 bxCAN
    reset values (MCR 0x00010002, MSR 0x00000C02, TSR 0x1C000000, BTR 0x01230000, FMR 0x2A1C0E01; 28 filter banks),
    12.8 DCMI (CR bits 14 and 11:0; CWSTRT VST 28:16 / HOFFCNT 13:0; CWSIZE VLINE 29:16 / CAPCNT 13:0)."""
    chip = make_chip()
    sd, can, dc = chip.peripherals["SDIO"], chip.peripherals["CAN1"], chip.peripherals["DCMI"]
    for n, rw in (("CLKCR", 0x7FFF), ("CMD", 0x7FFF), ("DLEN", 0x1FFFFFF), ("DCTRL", 0xFFF), ("ICR", 0xC007FF),
                  ("MASK", 0xFFFFFF)):
        assert ~sd.regs[n].ro_mask & 0xFFFFFFFF == rw, n
    assert sd.regs["FIFOCNT"].offset == 0x48
    for n, v in (("MCR", 0x00010002), ("MSR", 0x00000C02), ("TSR", 0x1C000000), ("BTR", 0x01230000),
                 ("FMR", 0x2A1C0E01)):
        assert can.regs[n].reset == v, n
    assert ~can.regs["FA1R"].ro_mask & 0xFFFFFFFF == 0x0FFFFFFF
    for n, rw in (("CR", 0x4FFF), ("CWSTRT", 0x1FFF3FFF), ("CWSIZE", 0x3FFF3FFF)):
        assert ~dc.regs[n].ro_mask & 0xFFFFFFFF == rw, n


def test_f4_fsmc_unchanged():
    """The F2 FSMC masks are chip-file parameters: the STM32F407 keeps its table (CPSIZE read-only, BWTR as in its
    SVD)."""
    chip = make_chip("stm32f407zg")
    f = chip.peripherals["FSMC"]
    assert f.regs["BCR2"].ro_mask == 0xFFF70080 and f.regs["BWTR1"].ro_mask == 0xC00F0000


@pytest.mark.parametrize("fn", ["stm32f205rg", "stm32f215zg"])
def test_eth_absent_on_f205_f215_with_params(fn):
    """1.4 / Table 1: the Ethernet MAC exists on the STM32F207/F217 only; the `mdc_div` parameter does not change
    that."""
    chip = make_chip(fn)
    assert chip.peripherals["Ethernet_MAC"].absent
