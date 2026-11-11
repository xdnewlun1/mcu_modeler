"""STM32F103 low / medium / high density chip descriptions: spot checks of pins (ST open pin data positions, RM0008
9.3 remaps), memory, IRQs (RM0008 10.1.2 Table 63), omitted SVD blocks, DBGMCU_IDCODE (31.6.1), F_SIZE (30.1.1),
SYST_CALIB (10.1.1), flash page size (3.3.3 Tables 4-6), BKP data registers (6.4.1) read through a live Chip."""
from __future__ import annotations

from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip

ROOT = Path(__file__).resolve().parent.parent
CHIPS = ROOT / "chips"


def desc(fn):
    return load_chip(CHIPS / f"{fn}.yaml")


def pin(d, gpio_or_name):
    return next(p for p in d.pins if (p.kind == "gpio" and p.gpio_name == gpio_or_name) or p.name == gpio_or_name)


def chip_of(fn):
    c = Chip(desc(fn))
    c.reset()
    return c


def rd(chip, addr, n=4):
    return chip.bus.read(addr, n)


# ------------------------------------------------------------------------------------------------ low density
def test_low_density_c6_r6_t4():
    c6, r6, t4 = desc("stm32f103c6"), desc("stm32f103r6"), desc("stm32f103t4")
    assert pin(c6, "PA9").num == 30 and pin(r6, "PA9").num == 42            # DS15060 Table 5
    assert pin(c6, "PC13").name == "PC13/TAMPER-RTC" and pin(c6, "PC13").num == 2
    assert pin(c6, "PD0").name == "OSC_IN/PD0" and pin(c6, "PD0").num == 5
    assert pin(r6, "PD2").num == 54 and pin(r6, "PD2").functions == {"AF": "TIM3_ETR"}
    assert pin(r6, "PC6").functions["REMAP"] == "TIM3_CH1"                  # TIM3 full remap, 64-pin
    assert pin(r6, "VSS_4").num == 18 and pin(c6, "VDD_3").num == 48
    assert pin(c6, "PB12").functions == {"AF": "TIM1_BKIN"}                  # no SPI2 / I2C2 / USART3 here
    assert pin(c6, "PB3").functions["REMAP"] == "SPI1_SCK/TIM2_CH2"
    assert [p.name for p in t4.pins] == [p.name for p in desc("stm32f103t6").pins]
    mem = {m.name: m.size for m in t4.memory}
    assert mem == {"FLASH": 16 * 1024, "SRAM": 6 * 1024}
    c = chip_of("stm32f103c4")
    assert rd(c, 0x1FFFF7E0, 2) == 16 and rd(c, 0xE0042000) & 0xFFF == 0x412


# ------------------------------------------------------------------------------------------------ medium density
def test_medium_density_pins():
    c8, rb, vb = desc("stm32f103c8"), desc("stm32f103rb"), desc("stm32f103vb")
    assert (pin(c8, "PC13").num, pin(c8, "PA9").num, pin(c8, "PA10").num, pin(c8, "PB12").num) == (2, 30, 31, 25)
    assert pin(c8, "PA11").functions == {"AF": "CAN_RX/TIM1_CH4/USART1_CTS", "additional": "USBDM"}
    assert pin(c8, "PB10").functions == {"AF": "I2C2_SCL/USART3_TX", "REMAP": "TIM2_CH3"}
    assert pin(c8, "PB6").functions == {"AF": "I2C1_SCL/TIM4_CH1", "REMAP": "USART1_TX"}
    assert pin(c8, "PB8").functions == {"AF": "TIM4_CH3", "REMAP": "CAN_RX/I2C1_SCL"}
    assert pin(c8, "PA13").default == "JTMS/SWDIO" and pin(c8, "PA13").functions == {"debug": "JTMS/SWDIO"}
    assert pin(rb, "PC10").functions == {"REMAP": "USART3_TX"}                # USART3 partial remap (Table 52)
    assert pin(rb, "PC12").num == 53 and pin(rb, "PD2").num == 54
    assert pin(vb, "PE2").num == 1 and pin(vb, "PE2").functions == {"AF": "TRACECK"}
    assert pin(vb, "OSC_IN").num == 12 and pin(vb, "OSC_IN").kind == "special"
    assert pin(vb, "PD0").functions == {"REMAP": "CAN_RX"}                    # CAN_REMAP = 11, 100-pin only
    assert pin(vb, "PD14").functions == {"REMAP": "TIM4_CH3"}                 # TIM4_REMAP (Table 43)
    assert pin(vb, "PD5").functions == {"REMAP": "USART2_TX"}
    assert pin(vb, "PE9").functions == {"REMAP": "TIM1_CH1"}                  # TIM1 full remap
    assert pin(vb, "PE1").functions == {"additional": "EXTI1"}                # no function on medium density


def test_medium_density_die():
    d = desc("stm32f103c8")
    names = {p.name for p in d.peripherals}
    assert {"TIM4", "SPI2", "USART3", "I2C2", "GPIOE"} <= names
    assert not names & {"TIM5", "TIM8", "DAC", "SDIO", "FSMC", "DMA2", "ADC3", "UART4", "GPIOF"}
    assert {o["name"] for o in d.extra["omitted"]} >= {"TIM5", "DMA2", "FSMC", "SDIO", "ADC3", "TIM9"}
    assert d.core.num_irqs == 43 and max(d.irqs) == 42
    assert (d.irqs[30], d.irqs[33], d.irqs[34], d.irqs[36], d.irqs[39]) == ("TIM4", "I2C2_EV", "I2C2_ER", "SPI2", "USART3")
    assert d.irqs[19] == "USB_HP_CAN1_TX" and d.irqs[42] == "USBWakeUp"
    spi2 = next(p for p in d.peripherals if p.name == "SPI2")
    assert spi2.params["i2s"] is False                                       # I2S is high density only (25.1)
    c = chip_of("stm32f103c8")
    assert rd(c, 0xE0042000) == 0x20036410                                   # DEV_ID 0x410, REV_ID 0x2003
    assert rd(c, 0x1FFFF7E0, 2) == 64                                        # F_SIZE in KB
    assert rd(c, 0xE000E01C) & 0xFFFFFF == 9000                              # SYST_CALIB TENMS (10.1.1)
    assert c.peripherals["FLASH"].page == 1024
    assert c.peripherals["BKP"].n_data == 10
    assert rd(chip_of("stm32f103cb"), 0x1FFFF7E0, 2) == 128


# ------------------------------------------------------------------------------------------------ high density
def test_high_density_pins():
    ze, ve, rc = desc("stm32f103ze"), desc("stm32f103ve"), desc("stm32f103rc")
    assert pin(ze, "PF0").num == 10 and pin(ze, "PF0").functions == {"AF": "FSMC_A0"}
    assert pin(ze, "PG15").num == 132 and pin(ze, "OSC_IN").num == 23 and pin(ze, "NC").num == 106
    assert pin(ze, "PF6").functions == {"AF": "FSMC_NIORD", "ADC": "ADC3_IN4"}
    assert pin(ze, "PA0").functions["ADC"] == "ADC123_IN0"
    assert pin(ze, "PA4").functions == {"AF": "SPI1_NSS/USART2_CK", "ADC": "ADC12_IN4", "analog": "DAC_OUT1"}
    assert pin(ze, "PC12").functions == {"AF": "SDIO_CK/UART5_TX", "REMAP": "USART3_CK"}
    assert pin(ze, "PD2").functions == {"AF": "SDIO_CMD/TIM3_ETR/UART5_RX"}
    assert pin(ze, "PB7").functions["AF"] == "FSMC_NADV/I2C1_SDA/TIM4_CH2"
    assert pin(ze, "PA15").functions == {"debug": "JTDI", "AF": "I2S3_WS/SPI3_NSS", "REMAP": "SPI1_NSS/TIM2_CH1_ETR"}
    assert pin(ve, "PE2").num == 1 and pin(ve, "PA9").num == 68 and pin(ve, "PD14").num == 61
    assert pin(rc, "PC12").num == 53 and pin(rc, "PB7").functions["AF"] == "I2C1_SDA/TIM4_CH2"   # no FSMC on LQFP64
    assert sum(p.kind == "gpio" and p.port in "FG" for p in ve.pins) == 0


def test_high_density_die():
    d = desc("stm32f103ze")
    names = {p.name for p in d.peripherals}
    assert {"TIM5", "TIM6", "TIM7", "TIM8", "SPI3", "UART4", "UART5", "DAC", "GPIOF", "GPIOG", "ADC3", "SDIO",
            "DMA2", "FSMC"} <= names
    assert {o["name"] for o in d.extra["omitted"]} == {f"TIM{n}" for n in range(9, 15)}
    assert d.core.num_irqs == 60 and len(d.irqs) == 60
    assert (d.irqs[43], d.irqs[47], d.irqs[48], d.irqs[49], d.irqs[52], d.irqs[54], d.irqs[59]) == (
        "TIM8_BRK", "ADC3", "FSMC", "SDIO", "UART4", "TIM6", "DMA2_Channel4_5")
    by = {p.name: p for p in d.peripherals}
    assert by["FSMC"].base == 0xA0000000 and by["SDIO"].base == 0x40018000 and by["DMA2"].base == 0x40020400
    assert by["SPI2"].params["i2s"] is True and by["TIM5"].params["bits"] == 16
    c = chip_of("stm32f103ze")
    assert rd(c, 0xE0042000) == 0x10036414                                   # DEV_ID 0x414, REV_ID 0x1003
    assert rd(c, 0x1FFFF7E0, 2) == 512
    assert c.peripherals["FLASH"].page == 2048 and c.peripherals["BKP"].n_data == 42
    assert rd(c, 0x40006C00 + 0xBC) == 0                                     # BKP_DR42 exists (6.4.5)
    c.bus.write(0x40021000 + 0x1C, 4, 1 << 28 | 1 << 27)                    # PWREN, BKPEN
    c.bus.write(0x40007000, 4, 1 << 8)                                       # DBP
    c.bus.write(0x40006C00 + 0xBC, 4, 0x1234)
    assert rd(c, 0x40006C00 + 0xBC) == 0x1234
    rc = chip_of("stm32f103rc")
    assert rd(rc, 0x1FFFF7E0, 2) == 256 and {m.name: m.size for m in rc.desc.memory}["SRAM"] == 48 * 1024


def test_high_density_flash_page_erase_is_2k():
    """RM0008 3.3.3 Table 6: high-density pages are 2 KB; a page erase clears 2048 bytes."""
    c = chip_of("stm32f103ze")
    fl = c.peripherals["FLASH"]
    c.core.write_mem(0x08001000, b"\x00" * 0x1000)
    c.bus.write(0x40022004, 4, 0x45670123)                                  # KEYR
    c.bus.write(0x40022004, 4, 0xCDEF89AB)
    c.bus.write(0x40022010, 4, 1 << 1)                                       # PER
    c.bus.write(0x40022014, 4, 0x08001000)                                   # AR: page 2
    c.bus.write(0x40022010, 4, (1 << 1) | (1 << 6))                          # STRT
    c.run_for(40e6)
    mem = c.core.read_mem(0x08001000, 0x1000)
    assert mem[:0x800] == b"\xff" * 0x800 and mem[0x800:] == b"\x00" * 0x800
    assert fl.erased_pages == 1
