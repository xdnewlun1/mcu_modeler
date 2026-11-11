"""STM32F103T6 chip description: pin table spot checks (copied by eye from DS15060 Rev 7 Table 5, VFQFPN36 column, and
Figure 7), memory map, IRQ numbers, register table, pin_table_check and svd2chip --check."""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip

ROOT = Path(__file__).resolve().parent.parent
YAML = ROOT / "chips" / "stm32f103t6.yaml"
SVD = ROOT / "svd" / "dl-stm32f103" / "STM32F103xx.svd"
sys.path.insert(0, str(ROOT / "tools"))


@pytest.fixture(scope="module")
def desc():
    return load_chip(YAML)


def pin(desc, name):
    return next(p for p in desc.pins if p.name == name or p.name.split("/")[0] == name
                or name in p.name.split("/"))


def test_identity(desc):
    assert desc.name == "STM32F103T6" and desc.package == "VFQFPN36" and desc.family == "stm32f1"
    assert desc.core.cpu == "cortex-m3" and desc.core.arch == "armv7-m" and desc.core.fpu is False
    assert desc.core.clock_hz == 8_000_000                      # HSI after reset
    assert desc.extra["systick_ext_div"] == 8 and desc.extra["vtor"] == 0x08000000
    assert desc.gpio_name_format == "P{port}{bit}"


def test_memory(desc):
    mem = {m.name: m for m in desc.memory}
    assert (mem["FLASH"].base, mem["FLASH"].size, mem["FLASH"].kind) == (0x08000000, 32 * 1024, "flash")
    assert (mem["SRAM"].base, mem["SRAM"].size) == (0x20000000, 10 * 1024)


def test_pin_spot_checks(desc):
    """DS15060 Table 5 (VFQFPN36) / Figure 7, read from the PDF."""
    assert len(desc.pins) == 36
    assert pin(desc, "VDD_3").num == 1 and pin(desc, "VDD_3").kind == "power"          # first pin
    p = pin(desc, "PD0")
    assert p.num == 2 and p.name == "OSC_IN/PD0" and p.default == "OSC_IN" and (p.port, p.bit) == ("D", 0)
    assert pin(desc, "PD1").num == 3
    assert pin(desc, "NRST").num == 4 and pin(desc, "NRST").kind == "special"
    assert pin(desc, "VSSA").num == 5 and pin(desc, "VDDA").num == 6
    p = pin(desc, "PA0")
    assert p.num == 7 and p.name == "PA0/WKUP"
    assert p.functions == {"AF": "USART2_CTS/TIM2_CH1_ETR", "ADC": "ADC12_IN0", "additional": "WKUP"}
    assert pin(desc, "PA2").num == 9 and "USART2_TX" in pin(desc, "PA2").functions["AF"]
    assert pin(desc, "PA7").functions == {"AF": "SPI1_MOSI/TIM3_CH2", "ADC": "ADC12_IN7", "REMAP": "TIM1_CH1N"}
    assert pin(desc, "PB1").num == 16 and pin(desc, "PB1").functions["REMAP"] == "TIM1_CH3N"
    assert pin(desc, "PB2").name == "PB2/BOOT1" and pin(desc, "PB2").num == 17
    assert pin(desc, "VSS_1").num == 18 and pin(desc, "VDD_1").num == 19
    p = pin(desc, "PA9")                                                                # console TX
    assert p.num == 21 and p.functions["AF"] == "USART1_TX/TIM1_CH2"
    assert pin(desc, "PA10").num == 22 and pin(desc, "PA10").functions["AF"] == "USART1_RX/TIM1_CH3"
    p = pin(desc, "PA11")
    assert p.num == 23 and p.functions["AF"] == "USART1_CTS/CAN_RX/TIM1_CH4" and p.functions["additional"] == "USBDM"
    assert pin(desc, "PA12").num == 24 and pin(desc, "PA12").functions["additional"] == "USBDP"
    assert pin(desc, "PA13").num == 25 and pin(desc, "PA13").default == "JTMS/SWDIO"
    assert pin(desc, "VSS_2").num == 26 and pin(desc, "VDD_2").num == 27
    assert pin(desc, "PA14").num == 28 and pin(desc, "PA15").num == 29
    assert pin(desc, "PA15").functions["REMAP"] == "TIM2_CH1_ETR/SPI1_NSS"
    # data sheet correction: Table 5 omits SPI1_SCK in PB3's remap list (SPI1_REMAP: NSS/SCK/MISO/MOSI on PA15/PB3/PB4/PB5)
    assert pin(desc, "PB3").num == 30 and pin(desc, "PB3").functions["REMAP"] == "TIM2_CH2/SPI1_SCK"
    assert pin(desc, "PB4").default == "NJTRST" and pin(desc, "PB4").functions["REMAP"] == "TIM3_CH1/SPI1_MISO"
    assert pin(desc, "PB6").num == 33 and pin(desc, "PB6").functions == {"AF": "I2C1_SCL", "REMAP": "USART1_TX"}
    assert pin(desc, "PB7").num == 34 and pin(desc, "PB7").functions["REMAP"] == "USART1_RX"
    assert pin(desc, "BOOT0").num == 35 and pin(desc, "BOOT0").kind == "special"
    assert pin(desc, "VSS_3").num == 36                                                  # last pin
    power = [p for p in desc.pins if p.kind == "power"]
    assert len(power) == 8 and sum(p.name.startswith("VSS") for p in power) == 4
    gpios = [p for p in desc.pins if p.kind == "gpio"]
    assert len(gpios) == 26                                     # DS15060 Table 2: 26 GPIOs on the STM32F103Tx


def test_irqs(desc):
    irq = desc.irqs
    assert irq[0] == "WWDG" and irq[18] == "ADC1_2" and irq[19] == "USB_HP_CAN1_TX" and irq[20] == "USB_LP_CAN1_RX0"
    assert irq[25] == "TIM1_UP" and irq[37] == "USART1" and irq[41] == "RTC_Alarm" and irq[42] == "USBWakeUp"
    for absent in (30, 33, 34, 36, 39):                         # TIM4, I2C2, SPI2, USART3: not on low density
        assert absent not in irq
    assert desc.core.num_irqs == 43


def test_peripherals_present(desc):
    names = {p.name for p in desc.peripherals}
    assert {"TIM1", "TIM2", "TIM3", "USART1", "USART2", "SPI1", "I2C1", "CAN", "USB", "ADC1", "ADC2", "DMA1",
            "RTC", "BKP", "IWDG", "WWDG", "CRC", "AFIO", "EXTI"} <= names
    assert not names & {"TIM4", "SPI2", "USART3", "I2C2", "DMA2", "DAC", "ADC3", "SDIO", "FSMC"}


def test_register_table():
    from chipem.regtable import load_regs
    rt = load_regs(str(ROOT / "chips" / "stm32f103_regs.yaml"))
    assert rt.peripheral("RCC").base == 0x40021000 and rt.peripheral("RCC")["CR"].reset & 0xFF == 0x83
    assert rt.peripheral("GPIOA")["CRL"].reset == 0x44444444
    assert rt.peripheral("BKP").base == 0x40006C04                # SVD: DR1 at 0x40006C04
    assert rt.peripheral("FLASH")["OBR"].reset == 0x03FFFFFC


def test_pin_table_check():
    import pin_table_check
    r = pin_table_check.check_file(YAML)
    assert r.ok and not r.warnings, r.text(True)


def test_svd_check():
    if not SVD.exists():
        pytest.skip("SVD not downloaded")
    import svd2chip
    out = io.StringIO()
    assert svd2chip.check(SVD, YAML, out) == 0, out.getvalue()
    assert "RENAMED irq 19" in out.getvalue()


def test_chip_builds_without_unknown_models(desc):
    chip = Chip(desc)
    from chipem.periph.stm32f1 import IMPORT_ERRORS
    assert IMPORT_ERRORS == {}
    msgs = [e.fields.get("msg", "") for e in chip.log if e.kind == "warn"]
    assert not [m for m in msgs if "not available" in m or "unknown model" in m.lower()], msgs
    assert all(type(b).__name__ != "StubBlock" for b in chip.peripherals.values())
    assert chip.clock.hz == 8_000_000
