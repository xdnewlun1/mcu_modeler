"""STM32F446 against RM0390 Rev 9 (docs/datasheets/st/RM0390_stm32f446_reference_manual.pdf): one regression test per
discrepancy found when the chip, built without its manual, was checked chapter by chapter.  QUADSPI regressions live
in test_stm32f446_quadspi.py, FMPI2C1 / CEC / SPDIFRX in their own files."""
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
RCC, FLASH_IF, PWR, SYSCFG, EXTI = 0x40023800, 0x40023C00, 0x40007000, 0x40013800, 0x40013C00
FMC, SDIO, OTG_FS, SAI1 = 0xA0000000, 0x40012C00, 0x50000000, 0x40015800


@pytest.fixture(scope="module")
def descs():
    return {fn: load_chip(ROOT / "chips" / f"{fn}.yaml") for fn in ("stm32f446re", "stm32f446ze", "stm32f446me")}


def chip(descs, fn="stm32f446re", **kw) -> Chip:
    code = asm("spin: b spin", FLASH + 0x200)
    img = bytearray(0x200 + len(code))
    img[0:8] = struct.pack("<II", 0x20020000, (FLASH + 0x200) | 1)
    img[0x200:] = code
    c = Chip(descs[fn], **kw)
    c.load_firmware(FirmwareImage([(FLASH, bytes(img))]))
    c.reset()
    return c


def rd(c, a, n=4):
    return c.bus.read(a, n)


def wr(c, a, v, n=4):
    c.bus.write(a, n, v)


def warns(c) -> list[str]:
    return [e.fields.get("msg", "") for e in c.log if e.kind == "warn"]


# ------------------------------------------------------------------------------------------------------- RCC (ch. 6)
def test_rcc_reset_values_rm0390(descs):
    """RM0390 6.3.10 / 6.3.15 / 6.3.16: AHB1ENR resets to 0 (no CCMDATARAMEN), AHB1LPENR 0x606790FF, AHB2LPENR 0x81
    (the SVD carries the F405 values 0x00100000 / 0x7E6791FF / 0xF1)."""
    c = chip(descs)
    assert rd(c, RCC + 0x30) == 0
    assert rd(c, RCC + 0x50) == 0x606790FF
    assert rd(c, RCC + 0x54) == 0x00000081
    assert rd(c, RCC + 0x58) == 0x3 and rd(c, RCC + 0x60) == 0x3FFFC9FF and rd(c, RCC + 0x64) == 0x00C77F33
    wr(c, RCC + 0x30, 1 << 20)
    assert rd(c, RCC + 0x30) == 0                                    # bit 20 reserved


def test_rcc_cecrst_and_cfgr_bit23(descs):
    """RM0390 6.3.8: APB1RSTR.CECRST (bit 27) exists (the SVD marks it reserved); 6.3.3: CFGR bit 23 is reserved (no
    I2SSRC: the I2S clocks come from DCKCFGR.I2SxSRC)."""
    c = chip(descs)
    wr(c, RCC + 0x20, 1 << 27)
    assert rd(c, RCC + 0x20) == 1 << 27
    wr(c, RCC + 0x20, 0)
    wr(c, RCC + 0x08, 1 << 23)
    assert rd(c, RCC + 0x08) & (1 << 23) == 0


def test_sai2src_11_and_fmpi2c1sel_11(descs):
    """RM0390 6.3.25: SAI2SRC = 11 is HSI/HSE (PLLSRC), SAI1SRC = 11 the I2S_CKIN pin; 6.3.27: FMPI2C1SEL = 11 is
    the APB clock (same as 00)."""
    c = chip(descs)
    r = c.peripherals["RCC"]
    r.i2s_ckin_hz = 12.288e6
    wr(c, RCC + 0x8C, (3 << 22) | (3 << 20))
    assert r.sai_kernel_hz("SAI2") == 16e6                           # HSI (PLLSRC = 0)
    assert r.sai_kernel_hz("SAI1") == 12.288e6
    wr(c, RCC + 0x94, 3 << 22)
    assert r.fmpi2c_clk_hz() == r.apb1_hz > 0
    wr(c, RCC + 0x94, 1 << 26)                                       # CECSEL = LSE, LSE off: no clock
    assert r.cec_clk_hz() == 0.0


def test_pll_range_checks_warn(descs):
    """RM0390 6.3.2 / 6.3.24: VCO input 1..2 MHz, PLLN 50..432, VCO 100..432 MHz, PLLSAI_P at most 120 MHz."""
    c = chip(descs)
    wr(c, RCC + 0x04, 4 | (300 << 6) | (2 << 28) | (4 << 24))       # HSI / 4 = 4 MHz VCO input, VCO 1200 MHz
    wr(c, RCC + 0x00, rd(c, RCC + 0x00) | (1 << 24))
    c.peripherals["RCC"].pll_r_hz()
    m = warns(c)
    assert any("PLL VCO input 4 MHz" in x and "RM0390 6.3.2" in x for x in m)
    assert any("PLL VCO 1200 MHz" in x for x in m)
    wr(c, RCC + 0x88, 8 | (100 << 6) | (4 << 24))                    # PLLSAI: 2 MHz x 100 = 200 MHz, P / 2 = 100 MHz
    wr(c, RCC + 0x00, rd(c, RCC + 0x00) | (1 << 28))
    assert c.peripherals["RCC"].pllsai_p_hz() == 100e6
    assert not any("PLLSAI_P" in x for x in warns(c))


def test_flash_wait_state_check(descs):
    """RM0390 3.4.1 Table 5: 2.7-3.6 V needs one more wait state per 30 MHz of HCLK."""
    c = chip(descs)
    r = c.peripherals["RCC"]
    assert r.min_wait_states(180e6) == 5 and r.min_wait_states(30e6) == 0 and r.min_wait_states(31e6) == 1
    wr(c, RCC + 0x04, 8 | (180 << 6) | (2 << 28) | (4 << 24))        # HSI / 8 x 180 = 360 MHz, P / 2 = 180 MHz
    wr(c, RCC + 0x00, rd(c, RCC + 0x00) | (1 << 24))
    wr(c, FLASH_IF + 0x00, 3)                                        # LATENCY 3: too few for 180 MHz
    wr(c, RCC + 0x08, 2)
    assert any("needs FLASH_ACR.LATENCY >= 5" in x for x in warns(c))


def test_clock_limits_rm0390(descs):
    """RM0390 3.4.1 note: scale 1 = 168 MHz (180 with over-drive); 6.3.3: APB1 45 MHz / APB2 90 MHz."""
    c = chip(descs)
    hmax, a1, a2, why = c.peripherals["RCC"].clock_limits()
    assert (a1, a2) == (45e6, 90e6) and "RM0390" in why


# ------------------------------------------------------------------------------------------------------- PWR (ch. 5)
def test_pwr_csr_reset_and_vosrdy(descs):
    """RM0390 5.4.2: PWR_CSR resets to 0; VOSRDY follows the main PLL (5.4.1: the scale is active only with the PLL
    on)."""
    c = chip(descs)
    wr(c, RCC + 0x40, 1 << 28)
    assert rd(c, PWR + 0x04) == 0
    wr(c, RCC + 0x00, rd(c, RCC + 0x00) | (1 << 24))
    assert rd(c, PWR + 0x04) & (1 << 14)


def test_pwr_ewup2_pc13_wakes_from_standby(descs):
    """RM0390 5.4.2: EWUP2 enables the second wake-up pin (PC13 on the STM32F446, PWR_WAKEUP_PIN2): a rising edge sets
    WUF; enabling it while the pin is high sets WUF too."""
    c = chip(descs)
    wr(c, RCC + 0x40, 1 << 28)
    pwr = c.peripherals["PWR"]
    pin = c.pins.gpio("C", 13)
    c.pins.set_external(pin.id, "L")
    wr(c, PWR + 0x04, 1 << 7)
    assert rd(c, PWR + 0x04) & (1 << 7) and not rd(c, PWR + 0x04) & 1
    c.pins.set_external(pin.id, "H")
    assert rd(c, PWR + 0x04) & 1                                     # WUF
    assert pwr._wkup2_level == 1


# ------------------------------------------------------------------------------------------------------- FLASH (ch. 3)
def test_flash_acr_latency_4_bits_and_rderr(descs):
    """RM0390 3.8.1: LATENCY is ACR[3:0]; 3.8.4: SR.RDERR (bit 8) is cleared by writing 1."""
    c = chip(descs)
    wr(c, FLASH_IF + 0x00, 0xF)
    assert rd(c, FLASH_IF + 0x00) & 0xF == 0xF
    fl = c.peripherals["FLASH"]
    fl.set("SR", 0x100)
    wr(c, FLASH_IF + 0x0C, 0x100)
    assert rd(c, FLASH_IF + 0x0C) & 0x100 == 0


# ------------------------------------------------------------------------------------------------------- SYSCFG / EXTI
def test_syscfg_f446_registers(descs):
    """RM0390 8.2.1: MEMRMP bit 8 reserved (no FB_MODE); 8.2.2: PMC only ADCxDC2 (18:16); 8.2.8: CFGR at 0x2C with
    FMPI2C1_SCL / FMPI2C1_SDA."""
    c = chip(descs)
    wr(c, RCC + 0x44, 1 << 14)                                       # SYSCFGEN
    wr(c, SYSCFG + 0x00, 0xFFF)
    assert rd(c, SYSCFG + 0x00) == 0xC07
    wr(c, SYSCFG + 0x04, 0xFFFFFFFF)
    assert rd(c, SYSCFG + 0x04) == 0x00070000
    wr(c, SYSCFG + 0x2C, 0xFF)
    assert rd(c, SYSCFG + 0x2C) == 3


def test_exti_line19_edge_bits_reserved(descs):
    """RM0390 10.3.3 / 10.3.4: RTSR / FTSR bit 19 reserved (no Ethernet wake-up line); IMR keeps bits 22:0 (10.3.1)."""
    c = chip(descs)
    for off in (0x08, 0x0C):
        wr(c, EXTI + off, 0x7FFFFF)
        assert rd(c, EXTI + off) == 0x77FFFF
    wr(c, EXTI + 0x00, 0x7FFFFF)
    assert rd(c, EXTI + 0x00) == 0x7FFFFF


# ------------------------------------------------------------------------------------------------------- signature
@pytest.mark.parametrize("fn,pkg,kb", [("stm32f446re", 0, 512), ("stm32f446ze", 3, 512), ("stm32f446me", 2, 512)])
def test_device_signature(descs, fn, pkg, kb):
    """RM0390 34.1-34.3: unique ID at 0x1FFF7A10, F_SIZE at 0x1FFF7A22, package data PKG[2:0] (bits 10:8) at
    0x1FFF7BF0; 3.3 Table 4: OTP area erased."""
    c = chip(descs, fn)
    assert rd(c, 0x1FFF7A22, 2) == kb
    assert (rd(c, 0x1FFF7BF0, 2) >> 8) & 7 == pkg and rd(c, 0x1FFF7BF0, 2) & ~0x700 == 0
    assert rd(c, 0x1FFF7A10) != 0xFFFFFFFF and rd(c, 0x1FFF7800) == 0xFFFFFFFF


# ------------------------------------------------------------------------------------------------------- FMC (ch. 11)
def test_fmc_f446_register_differences(descs):
    """RM0390 11.6.6: BCRx has CPSIZE[18:16] and WFDIS (21), bit 10 reserved; BWTRx has BUSTURN[19:16], 27:20
    reserved; 11.7.7 / Table 80: NAND bank 3 only, no bank 2 (0x60) and no PC Card bank 4 (0xA0) registers."""
    c = chip(descs)
    wr(c, RCC + 0x38, 1)                                             # FMCEN
    wr(c, FMC + 0x08, 0xFFFFFFFF)
    assert rd(c, FMC + 0x08) == 0x003FFBFF                          # bit 7 reserved, reads its reset value 1
    wr(c, FMC + 0x104, 0)
    assert rd(c, FMC + 0x104) == 0x0FF00000
    for off in (0x60, 0x64, 0xA0, 0xB0):
        wr(c, FMC + off, 0x1234)
        assert rd(c, FMC + off) == 0
    assert rd(c, FMC + 0x80) == 0x18 and rd(c, FMC + 0x84) == 0x40


# ------------------------------------------------------------------------------------------------------- SDIO (ch. 29)
def test_sdio_without_ceata(descs):
    """RM0390 29.8.4 / 29.8.12 / 29.8.13: the F446 SDIO has no CE-ATA bits (CMD 14:12, bit 23) and no STBITERR (9)."""
    c = chip(descs)
    wr(c, RCC + 0x44, 1 << 11)
    wr(c, SDIO + 0x3C, 0xFFFFFFFF)
    assert rd(c, SDIO + 0x3C) == 0x007FFDFF
    wr(c, SDIO + 0x0C, 0x7000)
    assert rd(c, SDIO + 0x0C) == 0


# ------------------------------------------------------------------------------------------------------- OTG (ch. 31)
def test_otg2_gccfg_vbden_and_bvalid_override(descs):
    """RM0390 31.15.15: GCCFG has PWRDWN and VBDEN only; 31.15.1: BVALOEN/BVALOVAL override the B-session valid
    signal (what the F446 HAL does when VBUS sensing is off); 31.15.16: CID 0x2000; 31.3 Table 221: 6 endpoints."""
    c = chip(descs)
    wr(c, RCC + 0x34, 1 << 7)
    q = c.peripherals["OTG_FS"]
    assert rd(c, OTG_FS + 0x3C) == 0x2000 and q.n_eps == 6
    wr(c, OTG_FS + 0x38, 0xFFFFFFFF)
    assert rd(c, OTG_FS + 0x38) == 0x00210000
    wr(c, OTG_FS + 0x38, 1 << 16)                                    # PWRDWN only, no sensing
    assert not q._session
    wr(c, OTG_FS + 0x00, (1 << 6) | (1 << 7))                        # BVALOEN + BVALOVAL
    assert q._session and rd(c, OTG_FS + 0x00) & (1 << 19)          # BSVLD
    wr(c, OTG_FS + 0x54, 1)                                          # GLPMCFG.LPMEN
    assert rd(c, OTG_FS + 0x54) == 1 and any("LPM" in m for m in warns(c))
    assert rd(c, OTG_FS + 0x5C) == 0x03F40140                        # GDFIFOCFG (31.15.18)


# ------------------------------------------------------------------------------------------------------- SAI (ch. 28)
def test_sai_gcr_and_unmodelled_protocols_warn(descs):
    """RM0390 28.5.1: SAI_GCR has SYNCOUT[5:4] / SYNCIN[1:0]; 28.3.11 SPDIF output and 28.3.4 external
    synchronisation are not modelled: one warn each when a block is enabled with them."""
    c = chip(descs)
    wr(c, RCC + 0x44, 1 << 22)
    wr(c, SAI1 + 0x00, 0xFF)
    assert rd(c, SAI1 + 0x00) == 0x33
    wr(c, SAI1 + 0x04, (1 << 2) | (2 << 10) | (1 << 0) | (1 << 19))   # slave TX, SPDIF, SYNCEN = 10, NODIV
    wr(c, SAI1 + 0x04, rd(c, SAI1 + 0x04) | (1 << 16))
    m = warns(c)
    assert any("SPDIF protocol" in x for x in m) and any("external synchronisation" in x for x in m)
