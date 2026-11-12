"""STM32F2 system blocks checked against the official RM0033 Rev 10
(docs/datasheets/st/RM0033_stm32f2_reference_manual.pdf): one regression test per discrepancy found when the ST PDF
replaced the third-party HTML copy (RCC, DBGMCU, DMA, the device electronic signature).  Peripheral chapters:
tests/test_stm32f2_rm0033_periph.py / _conn.py."""
from __future__ import annotations

import pytest

from test_stm32f2_system import desc, rd, spin_chip, warns, wr

RCC, FLASHIF, DBGMCU, DMA2 = 0x40023800, 0x40023C00, 0xE0042000, 0x40026400
CR, PLLCFGR, CFGR, AHB1ENR, CSR, SSCGR, PLLI2SCFGR = 0x00, 0x04, 0x08, 0x30, 0x74, 0x80, 0x84
PLLON, PLLRDY, PLLI2SON, HSEON, HSEBYP = 1 << 24, 1 << 25, 1 << 26, 1 << 16, 1 << 18


@pytest.fixture
def chip():
    return spin_chip(desc())


def _pll_on(chip, cfgr_val):
    wr(chip, RCC + CR, rd(chip, RCC + CR) | HSEON)
    wr(chip, RCC + PLLCFGR, cfgr_val)
    wr(chip, RCC + CR, rd(chip, RCC + CR) | PLLON)


# ------------------------------------------------------------------------------------------------------------ RCC
def test_rcc_pll_vco_output_range(chip):
    """5.3.2 PLLN: "ensure that the VCO output frequency is between 192 and 432 MHz" (PLLN 192..432 alone allows
    up to 864 MHz with a 2 MHz input): checked while the PLL runs, also when it only feeds PLL48CLK."""
    _pll_on(chip, 4 | (300 << 6) | (3 << 16) | (1 << 22) | (12 << 24))      # 8 MHz / 4 = 2 MHz x 300 = 600 MHz
    assert any("PLL VCO output 600 MHz (allowed 192..432 MHz)" in w and "RM0033 5.3.2" in w for w in warns(chip))
    c2 = spin_chip(desc())
    _pll_on(c2, 8 | (240 << 6) | (1 << 22) | (5 << 24))                     # 240 MHz VCO: legal
    assert not any("VCO" in w for w in warns(c2))


def test_rcc_pllm_pllq_wrong_configurations(chip):
    """5.3.2: PLLM 0/1 and PLLQ 0/1 are "wrong configuration" (2 <= PLLM <= 63, 2 <= PLLQ <= 15)."""
    _pll_on(chip, 8 | (240 << 6) | (1 << 22) | (1 << 24))
    assert any("PLLQ = 1 (allowed 2..15)" in w for w in warns(chip))
    c2 = spin_chip(desc())
    _pll_on(c2, 1 | (240 << 6) | (1 << 22) | (5 << 24))
    assert any("PLLM = 1 (allowed 2..63)" in w for w in warns(c2))


def test_rcc_pll_fields_locked_while_running(chip):
    """5.2.3 / 5.3.2: PLLM and PLLSRC "can be written only when the PLL and PLLI2S are disabled", PLLN / PLLP only
    while the PLL is off (PLLQ "should be": written, with a warn); 5.3.23: PLLI2SN only while the PLLI2S is off."""
    v = 8 | (240 << 6) | (1 << 22) | (5 << 24)
    _pll_on(chip, v)
    wr(chip, RCC + PLLCFGR, 4 | (200 << 6) | (1 << 16) | (6 << 24))         # new M, N, P, SRC = HSI, Q = 6
    b29 = 1 << 29                                                             # reserved, reads its reset value 1
    assert rd(chip, RCC + PLLCFGR) == b29 | (v & ~(0xF << 24)) | (6 << 24)    # only PLLQ changed
    assert any("RCC_PLLCFGR write while the PLL runs" in w for w in warns(chip))
    assert any("PLLQ / PLLI2SR changed while the PLL runs" in w for w in warns(chip))
    assert chip.peripherals["RCC"].pll_hz() == 120e6
    # PLLI2S running: PLLM / PLLSRC locked even with the main PLL off; PLLI2SN locked, PLLI2SR written
    c2 = spin_chip(desc())
    wr(c2, RCC + PLLCFGR, v)
    wr(c2, RCC + PLLI2SCFGR, (2 << 28) | (192 << 6))
    wr(c2, RCC + CR, rd(c2, RCC + CR) | PLLI2SON)
    wr(c2, RCC + PLLCFGR, 16 | (240 << 6) | (5 << 24))                        # M 8 -> 16, SRC HSE -> HSI
    assert rd(c2, RCC + PLLCFGR) == v | (1 << 29)
    wr(c2, RCC + PLLI2SCFGR, (3 << 28) | (300 << 6))
    assert rd(c2, RCC + PLLI2SCFGR) == (3 << 28) | (192 << 6)
    wr(c2, RCC + CR, rd(c2, RCC + CR) & ~PLLI2SON)                            # off: everything writable again
    wr(c2, RCC + PLLI2SCFGR, (3 << 28) | (300 << 6))
    wr(c2, RCC + PLLCFGR, 16 | (240 << 6) | (5 << 24))
    assert rd(c2, RCC + PLLI2SCFGR) == (3 << 28) | (300 << 6)
    assert rd(c2, RCC + PLLCFGR) == (1 << 29) | 16 | (240 << 6) | (5 << 24)


def test_rcc_hsebyp_writable_only_with_hse_off(chip):
    """5.3.1: "The HSEBYP bit can be written only if the HSE oscillator is disabled"."""
    wr(chip, RCC + CR, rd(chip, RCC + CR) | HSEBYP)
    assert rd(chip, RCC + CR) & HSEBYP
    wr(chip, RCC + CR, rd(chip, RCC + CR) | HSEON)
    wr(chip, RCC + CR, rd(chip, RCC + CR) & ~HSEBYP)
    assert rd(chip, RCC + CR) & HSEBYP and any("HSEBYP write ignored" in w for w in warns(chip))
    wr(chip, RCC + CR, rd(chip, RCC + CR) & ~HSEON)
    wr(chip, RCC + CR, rd(chip, RCC + CR) & ~HSEBYP)
    assert not rd(chip, RCC + CR) & HSEBYP


def test_rcc_csr_reset_flags_cleared_only_by_rmvf(chip):
    """5.3.21: the reset flags are set by hardware and "cleared by writing to the RMVF bit": a plain write (e.g.
    RCC_CSR = LSION) neither clears nor sets them."""
    assert rd(chip, RCC + CSR) == 0x0E000000                                  # POR: PORRSTF | PINRSTF | BORRSTF
    wr(chip, RCC + CSR, 1)                                                    # LSION only
    assert rd(chip, RCC + CSR) == 0x0E000003
    wr(chip, RCC + CSR, 0xF0000001)                                           # software cannot set flags
    assert rd(chip, RCC + CSR) == 0x0E000003
    wr(chip, RCC + CSR, (1 << 24) | 1)                                        # RMVF
    assert rd(chip, RCC + CSR) == 0x00000003


def test_rcc_sscgr_written_while_pll_on_warns(chip):
    """5.3.22: SSCGR "must be written either before the main PLL is enabled or after the main PLL disabled";
    bits 29:28 reserved."""
    wr(chip, RCC + SSCGR, 0xFFFFFFFF)
    assert rd(chip, RCC + SSCGR) == 0xCFFFFFFF and not warns(chip)
    _pll_on(chip, 8 | (240 << 6) | (1 << 22) | (5 << 24))
    wr(chip, RCC + SSCGR, 0)
    assert any("RCC_SSCGR written while the main PLL is on" in w for w in warns(chip))


# ------------------------------------------------------------------------------------------------------------ DBGMCU
def test_dbgmcu_rtc_stop_and_por_only_registers(chip):
    """32.16.4: DBGMCU_APB1_FZ bit 10 DBG_RTC_STOP is rw (read-only in the SVD); writable bits 0-8, 10-12, 21-23,
    25-26.  32.16.3-32.16.5: CR / APB1_FZ / APB2_FZ are reset by POR only, not by a system reset."""
    wr(chip, DBGMCU + 0x08, 0xFFFFFFFF)
    assert rd(chip, DBGMCU + 0x08) == 0x06E01DFF
    wr(chip, DBGMCU + 0x04, 0xFFFFFFFF)
    wr(chip, DBGMCU + 0x0C, 0xFFFFFFFF)
    assert (rd(chip, DBGMCU + 0x04), rd(chip, DBGMCU + 0x0C)) == (0xE7, 0x00070003)
    chip.reset()
    assert (rd(chip, DBGMCU + 0x04), rd(chip, DBGMCU + 0x08), rd(chip, DBGMCU + 0x0C)) == (0xE7, 0x06E01DFF,
                                                                                          0x00070003)
    assert rd(chip, DBGMCU) == 0x20036411                                    # DEV_ID 0x411, REV_ID 0x2003 (rev X)


# ------------------------------------------------------------------------------------------------------------ DMA
def test_dma_fcr_reserved_bits(chip):
    """9.5.10 DMA_SxFCR: FEIE (7), DMDIS (2), FTH (1:0) rw, FS (5:3) read-only, bits 31:8 and 6 reserved;
    reset 0x21."""
    wr(chip, RCC + AHB1ENR, 1 << 22)
    assert rd(chip, DMA2 + 0x24) == 0x21
    wr(chip, DMA2 + 0x24, 0xFFFFFFFF)
    assert rd(chip, DMA2 + 0x24) == 0xA7


# ------------------------------------------------------------------------------------------------------------ signature
@pytest.mark.parametrize("fn", ["stm32f207zg", "stm32f205rg", "stm32f217igh"])
def test_unique_id_and_flash_size(fn):
    """33.1: 96-bit unique ID at 0x1FFF7A10 (read only); 33.2: F_SIZE at 0x1FFF7A22 (KB, 0x0400 = 1 MB);
    2.3.3 Table 2: OTP 0x1FFF7800-0x1FFF7A0F (erased: 0xFF)."""
    chip = spin_chip(desc(fn))
    assert chip.bus.read(0x1FFF7A22, 2) == 0x0400
    uid = [chip.bus.read(0x1FFF7A10 + 4 * k, 4) for k in range(3)]
    assert any(uid)
    assert chip.bus.read(0x1FFF7800, 4) == 0xFFFFFFFF
    chip.bus.write(0x1FFF7A10, 4, 0)
    assert chip.bus.read(0x1FFF7A10, 4) == uid[0]


# ------------------------------------------------------------------------------------------------------------ confirmed
def test_flash_acr_and_bor_default(chip):
    """2.3.4 FLASH_ACR: LATENCY[2:0], PRFTEN (8), ICEN (9), DCEN (10), ICRST (11), DCRST (12), reset 0.  4.2.2: "By
    default, BOR is off" (OPTCR.BOR_LEV = 11)."""
    assert rd(chip, FLASHIF) == 0
    wr(chip, FLASHIF, 0xFFFFFFFF)
    assert rd(chip, FLASHIF) == 0x1F07
    assert (rd(chip, FLASHIF + 0x14) >> 2) & 3 == 3
