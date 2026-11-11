"""STM32F401/F411 chip descriptions checked against their reference manuals: RM0368 Rev 6 (STM32F401xB/C/D/E) and
RM0383 Rev 4 (STM32F411xC/E), docs/datasheets/st/.  Memory map (2.3 Table 1), memory sizes (2.3.1), vector tables
(RM0368 10.1 Table 38, RM0383 10.1 Table 37), EXTI line mapping (10.2.5, 10.3), DMA request maps (RM0368 9.3.3
Tables 28/29, RM0383 9.3.3 Tables 27/28), DBGMCU (23.6.1, 23.16) and the device electronic signature (24.1, 24.2)."""
from __future__ import annotations

import pytest

from test_stm32f4x1_system import desc, rd, spin_chip, wr

# RM0368 / RM0383 2.3 Table 1 "register boundary addresses" (identical for both lines except SPI5)
TABLE1 = {
    "TIM2": 0x40000000, "TIM3": 0x40000400, "TIM4": 0x40000800, "TIM5": 0x40000C00, "RTC": 0x40002800,
    "WWDG": 0x40002C00, "IWDG": 0x40003000, "I2S2ext": 0x40003400, "SPI2": 0x40003800, "SPI3": 0x40003C00,
    "I2S3ext": 0x40004000, "USART2": 0x40004400, "I2C1": 0x40005400, "I2C2": 0x40005800, "I2C3": 0x40005C00,
    "PWR": 0x40007000, "TIM1": 0x40010000, "USART1": 0x40011000, "USART6": 0x40011400, "ADC1": 0x40012000,
    "SDIO": 0x40012C00, "SPI1": 0x40013000, "SPI4": 0x40013400, "SYSCFG": 0x40013800, "EXTI": 0x40013C00,
    "TIM9": 0x40014000, "TIM10": 0x40014400, "TIM11": 0x40014800, "GPIOA": 0x40020000, "GPIOB": 0x40020400,
    "GPIOC": 0x40020800, "GPIOD": 0x40020C00, "GPIOE": 0x40021000, "GPIOH": 0x40021C00, "CRC": 0x40023000,
    "RCC": 0x40023800, "FLASH": 0x40023C00, "DMA1": 0x40026000, "DMA2": 0x40026400, "OTG_FS": 0x50000000,
}
# blocks the chip YAML lists outside Table 1: ADC common registers (inside the ADC1 window 0x40012000-0x400123FF),
# DBGMCU (23.16, PPB) and the OTP / signature area (3.3, 24.1/24.2)
EXTRA = {"ADC_Common": 0x40012300, "DBGMCU": 0xE0042000, "SYSMEM": 0x1FFF7800}


@pytest.mark.parametrize("fn,spi5", [("stm32f401re", False), ("stm32f401cc", False), ("stm32f401ve", False),
                                     ("stm32f411re", True), ("stm32f411ce", True), ("stm32f411ve", True)])
def test_register_boundary_table(fn, spi5):
    """RM0368/RM0383 2.3 Table 1: exactly these blocks at these bases (SPI5 0x40015000 on the F411 only)."""
    want = dict(TABLE1, **EXTRA)
    if spi5:
        want["SPI5"] = 0x40015000
    got = {p.name: p.base for p in desc(fn).peripherals}
    assert got == want


@pytest.mark.parametrize("fn,flash_kb,sram_kb", [("stm32f401cc", 256, 64), ("stm32f401re", 512, 96),
                                                 ("stm32f401ve", 512, 96), ("stm32f411ce", 512, 128),
                                                 ("stm32f411re", 512, 128), ("stm32f411ve", 512, 128)])
def test_memory_sizes(fn, flash_kb, sram_kb):
    """RM0368 2.3.1 (64 KB xB/C, 96 KB xD/E SRAM) / RM0383 2.3.1 (128 KB); flash at 0x08000000 (3.3)."""
    mem = {m.name: m for m in desc(fn).memory}
    assert (mem["FLASH"].base, mem["FLASH"].size) == (0x08000000, flash_kb * 1024)
    assert (mem["SRAM"].base, mem["SRAM"].size) == (0x20000000, sram_kb * 1024)


# RM0368 10.1 Table 38 / RM0383 10.1 Table 37 (CMSIS names; the RM writes "EXTI16 / PVD", "EXTI21 / TAMP_STAMP",
# "EXTI22 / RTC_WKUP", "EXTI17 / RTC_Alarm")
VECTORS = {
    0: "WWDG", 1: "PVD", 2: "TAMP_STAMP", 3: "RTC_WKUP", 4: "FLASH", 5: "RCC", 6: "EXTI0", 7: "EXTI1", 8: "EXTI2",
    9: "EXTI3", 10: "EXTI4", **{11 + s: f"DMA1_Stream{s}" for s in range(7)}, 18: "ADC", 23: "EXTI9_5",
    24: "TIM1_BRK_TIM9", 25: "TIM1_UP_TIM10", 26: "TIM1_TRG_COM_TIM11", 27: "TIM1_CC", 28: "TIM2", 29: "TIM3",
    30: "TIM4", 31: "I2C1_EV", 32: "I2C1_ER", 33: "I2C2_EV", 34: "I2C2_ER", 35: "SPI1", 36: "SPI2", 37: "USART1",
    38: "USART2", 40: "EXTI15_10", 41: "RTC_Alarm", 42: "OTG_FS_WKUP", 47: "DMA1_Stream7", 49: "SDIO", 50: "TIM5",
    51: "SPI3", **{56 + s: f"DMA2_Stream{s}" for s in range(5)}, 67: "OTG_FS", 68: "DMA2_Stream5",
    69: "DMA2_Stream6", 70: "DMA2_Stream7", 71: "USART6", 72: "I2C3_EV", 73: "I2C3_ER", 81: "FPU", 84: "SPI4",
}


@pytest.mark.parametrize("fn,extra", [("stm32f401re", {}), ("stm32f401cc", {}), ("stm32f411re", {85: "SPI5"}),
                                      ("stm32f411ce", {85: "SPI5"})])
def test_vector_table(fn, extra):
    """RM0368 Table 38 (62 vectors, last 84 SPI4) / RM0383 Table 37 (63 vectors, last 85 SPI5)."""
    d = desc(fn)
    want = {**VECTORS, **extra}
    assert {n: str(v) for n, v in d.irqs.items()} == want
    assert d.core.num_irqs == max(want) + 1


@pytest.mark.parametrize("fn", ["stm32f401re", "stm32f411re"])
def test_exti_lines_19_20_reserved(fn):
    """RM0368/RM0383 10.2.5: lines 16 PVD, 17 RTC alarm, 18 OTG FS wakeup, 21 tamper/timestamp, 22 RTC wakeup;
    10.3.1-10.3.6: bits 19/20 (Ethernet / OTG HS wakeup on the F405) are reserved in IMR/EMR/RTSR/FTSR/SWIER/PR."""
    chip = spin_chip(fn)
    base = 0x40013C00
    for off in (0x00, 0x04, 0x08, 0x0C):
        wr(chip, base + off, 0xFFFFFFFF)
        assert rd(chip, base + off) == 0x0067FFFF
    wr(chip, base + 0x10, 1 << 19 | 1 << 20)                       # SWIER on reserved lines: nothing pends
    assert rd(chip, base + 0x14) == 0
    wr(chip, base + 0x10, 1 << 22)                                 # line 22 (RTC wakeup) is implemented
    assert rd(chip, base + 0x14) == 1 << 22


# ------------------------------------------------------------------------------------------------- DMA request maps
def _cells(table):
    return {(s, ch, n) for s in range(8) for ch in range(8) for n in table[s][ch]}


def test_dma_maps_rm0368_tables_28_29():
    """RM0368 9.3.3 Table 28 (DMA1) / Table 29 (DMA2): request positions that RM0090 Tables 43/44 do not have
    (I2C3_RX DMA1 S1 ch1, I2C3_TX DMA1 S5 ch6), and the full count of request positions."""
    from chipem.periph.stm32.dma import REQUEST_TABLES
    t1, t2 = REQUEST_TABLES["f401"][1], REQUEST_TABLES["f401"][2]
    assert t1[1][1] == ("I2C3_RX",) and t1[5][6] == ("I2C3_TX",)
    assert t1[1][0] == () and t1[7][6] == ()                       # F411-only positions
    assert t1[2][1] == () and t1[4][1] == ()                       # RM0090 TIM7_UP: no TIM7
    assert t1[5][4] == ("USART2_RX",) and t1[6][4] == ("USART2_TX",)
    assert t1[3][3] == ("I2S2_EXT_RX",) and t1[4][2] == ("I2S2_EXT_TX",)
    assert t2[2][2] == () and t2[4][4] == () and t2[5][5] == ()    # F411-only positions
    assert t2[3][2] == () and t2[5][7] == ()                       # no SPI5 on the F401
    assert t2[4][6] == ("TIM1_CH4", "TIM1_TRIG", "TIM1_COM")
    assert len(_cells(t1)) == 50 and len(_cells(t2)) == 31     # requests counted in the two tables


def test_dma_maps_rm0383_tables_27_28():
    """RM0383 9.3.3 Table 27 (DMA1) / Table 28 (DMA2): the F401 positions plus I2C1_TX DMA1 S1 ch0, USART2_RX DMA1
    S7 ch6, SPI1_TX DMA2 S2 ch2, SPI5_RX/TX DMA2 S3/S4 ch2, SPI4_RX DMA2 S4 ch4, SPI5_TX DMA2 S5 ch5, SPI5_RX/TX DMA2
    S5/S6 ch7."""
    from chipem.periph.stm32.dma import REQUEST_TABLES
    f401, f411 = REQUEST_TABLES["f401"], REQUEST_TABLES["f411"]
    assert _cells(f411[1]) - _cells(f401[1]) == {(1, 0, "I2C1_TX"), (7, 6, "USART2_RX")}
    assert _cells(f411[2]) - _cells(f401[2]) == {(2, 2, "SPI1_TX"), (3, 2, "SPI5_RX"), (4, 2, "SPI5_TX"),
                                                 (4, 4, "SPI4_RX"), (5, 5, "SPI5_TX"), (5, 7, "SPI5_RX"),
                                                 (6, 7, "SPI5_TX")}
    assert _cells(f401[1]) <= _cells(f411[1]) and _cells(f401[2]) <= _cells(f411[2])


def test_dma_f411_usart2_rx_on_dma1_stream7_channel6():
    """RM0383 Table 27: USART2_RX is also on DMA1 stream 7 channel 6 (not on the F401, RM0368 Table 28)."""
    for fn, moved in (("stm32f411re", True), ("stm32f401re", False)):
        chip = spin_chip(fn)
        wr(chip, 0x40023800 + 0x30, (1 << 21) | 1)                 # AHB1ENR: DMA1, GPIOA
        wr(chip, 0x40023800 + 0x40, 1 << 17)                       # APB1ENR: USART2
        wr(chip, 0x40020000 + 0x20, 7 << 12)                       # GPIOA AFRL: PA3 AF7 (USART2_RX)
        wr(chip, 0x40020000, rd(chip, 0x40020000) & ~(3 << 6) | (2 << 6))   # PA3 alternate function
        u = 0x40004400
        wr(chip, u + 0x08, 16)                                     # 1 Mbaud at 16 MHz
        wr(chip, u + 0x14, 1 << 6)                                 # CR3.DMAR
        wr(chip, u + 0x0C, (1 << 13) | (1 << 2))                   # UE, RE
        s7 = 0x40026000 + 0x10 + 0x18 * 7
        dst = 0x20000200
        wr(chip, s7 + 0x08, u + 0x04)
        wr(chip, s7 + 0x0C, dst)
        wr(chip, s7 + 0x04, 2)
        wr(chip, s7, (6 << 25) | (1 << 10) | 1)                    # CHSEL 6, MINC, P2M, EN
        chip.peripherals["USART2"].feed(b"hi")
        chip.run_for(100e3)
        if moved:
            assert rd(chip, s7 + 0x04) == 0 and chip.bus.read(dst, 2) == int.from_bytes(b"hi", "little")
        else:
            assert rd(chip, s7 + 0x04) == 2


# ------------------------------------------------------------------------------------------------- DBGMCU, signature
@pytest.mark.parametrize("fn,dev_id", [("stm32f401cc", 0x423), ("stm32f401re", 0x433), ("stm32f401ve", 0x433),
                                       ("stm32f411ce", 0x431), ("stm32f411re", 0x431)])
def test_dbgmcu_idcode_and_freeze_bits(fn, dev_id):
    """RM0368/RM0383 23.6.1 DEV_ID (0x423 F401xB/C, 0x433 F401xD/E, 0x431 F411; F411 REV_ID 0x1000 = rev A);
    23.16.3-23.16.5: DBGMCU_CR bits 0-2, 5-7; APB1_FZ TIM2-5 (3:0), RTC/WWDG/IWDG (12:10), I2C1-3 SMBus (23:21);
    APB2_FZ TIM1 (0), TIM9-11 (18:16); everything else reserved."""
    chip = spin_chip(fn)
    idc = rd(chip, 0xE0042000)
    assert idc & 0xFFF == dev_id
    if dev_id == 0x431:
        assert idc >> 16 == 0x1000
    for off, mask in ((0x04, 0x000000E7), (0x08, 0x00E01C0F), (0x0C, 0x00070001)):
        wr(chip, 0xE0042000 + off, 0xFFFFFFFF)
        assert rd(chip, 0xE0042000 + off) == mask


@pytest.mark.parametrize("fn,kb", [("stm32f401cc", 256), ("stm32f401re", 512), ("stm32f411ce", 512)])
def test_unique_id_and_flash_size(fn, kb):
    """RM0368/RM0383 24.1: 96-bit unique ID at 0x1FFF7A10; 24.2: F_SIZE (KB, 16 bits) at 0x1FFF7A22; 3.3 Table 5 / 4:
    OTP area 0x1FFF7800-0x1FFF7A0F (erased = 0xFF), read only."""
    chip = spin_chip(fn)
    assert chip.bus.read(0x1FFF7A22, 2) == kb
    uid = [chip.bus.read(0x1FFF7A10 + 4 * k, 4) for k in range(3)]
    assert any(uid) and uid[0] != 0xFFFFFFFF
    assert chip.bus.read(0x1FFF7800, 4) == 0xFFFFFFFF
    chip.bus.write(0x1FFF7A10, 4, 0)
    assert chip.bus.read(0x1FFF7A10, 4) == uid[0]
