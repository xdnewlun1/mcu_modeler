"""STM32F401 / STM32F411 chip descriptions (chips/stm32f401{re,cc,ve}.yaml, chips/stm32f411{re,ce,ve}.yaml): counts,
pin_table_check --strict, svd2chip --check against the cached STM32F401.svd / STM32F411.svd, Chip() builds without
warnings, agreement with ST's open pin data (the source) re-read independently here, AF numbers against the
STM32CubeF4 HAL GPIO_AFx_<periph> definitions (an independent second source: stm32f4xx_hal_gpio_ex.h), and spot checks
of well-known Nucleo / Black Pill pins.  The data sheets DS9716 / DS10086 / DS10314 are not in the
repository (RM0368 / RM0383 have no pin tables)."""
from __future__ import annotations

import functools
import io
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip

ROOT = Path(__file__).resolve().parent.parent
CHIPS = ROOT / "chips"
OPD = ROOT / "svd" / "dl-open-pin-data-f4x1"
HAL_GPIO_EX = ROOT / "firmware/vendor/STM32CubeF4/Drivers/STM32F4xx_HAL_Driver/Inc/stm32f4xx_hal_gpio_ex.h"

# file, name, package, pins, GPIOs, flash, SRAM, open-pin-data file, line
PARTS = [
    ("stm32f401re", "STM32F401RE", "LQFP64", 64, 50, 0x80000, 0x18000, "STM32F401R(D-E)Tx.xml", "f401"),
    ("stm32f401cc", "STM32F401CC", "UFQFPN48", 48, 36, 0x40000, 0x10000, "STM32F401C(B-C)Ux.xml", "f401"),
    ("stm32f401ve", "STM32F401VE", "LQFP100", 100, 81, 0x80000, 0x18000, "STM32F401V(D-E)Tx.xml", "f401"),
    ("stm32f411re", "STM32F411RE", "LQFP64", 64, 50, 0x80000, 0x20000, "STM32F411R(C-E)Tx.xml", "f411"),
    ("stm32f411ce", "STM32F411CE", "UFQFPN48", 48, 36, 0x80000, 0x20000, "STM32F411C(C-E)Ux.xml", "f411"),
    ("stm32f411ve", "STM32F411VE", "LQFP100", 100, 81, 0x80000, 0x20000, "STM32F411V(C-E)Tx.xml", "f411"),
]
IDS = [p[0] for p in PARTS]
SVDS = {"f401": ROOT / "svd/dl-stm32f401/STM32F401.svd", "f411": ROOT / "svd/dl-stm32f411/STM32F411.svd"}


def tool(name: str):
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        return __import__(name)
    finally:
        sys.path.pop(0)


@functools.lru_cache(maxsize=None)
def desc_of(fn: str):
    return load_chip(CHIPS / f"{fn}.yaml")


def gpio(fn: str, g: str):
    return next(p for p in desc_of(fn).pins if p.gpio_name == g)


@pytest.mark.parametrize("fn,name,package,npins,ngpio,flash,sram,opd,line", PARTS, ids=IDS)
def test_description(fn, name, package, npins, ngpio, flash, sram, opd, line):
    d = desc_of(fn)
    assert (d.name, d.package, d.family) == (name, package, "stm32")
    assert len(d.pins) == npins and sorted(p.num for p in d.pins) == list(range(1, npins + 1))
    assert len({p.gpio_name for p in d.pins if p.kind == "gpio"}) == ngpio
    mem = {m.name: (m.base, m.size) for m in d.memory}
    assert mem == {"FLASH": (0x08000000, flash), "SRAM": (0x20000000, sram)}      # no CCM, no backup SRAM
    assert d.core.clock_hz == 16e6 and d.core.fpu and d.core.num_irqs == (85 if line == "f401" else 86)
    names = {p.name for p in d.peripherals}
    assert not names & {"TIM6", "TIM7", "TIM8", "TIM12", "TIM13", "TIM14", "USART3", "UART4", "UART5", "CAN1", "DAC",
                        "Ethernet_MAC", "FSMC", "RNG", "DCMI", "OTG_HS", "ADC2", "ADC3"}
    assert ("SPI5" in names) == (line == "f411") and ("SPI5" in d.irqs.values()) == (line == "f411")
    per = {p.name: p for p in d.peripherals}
    assert per["RCC"].params["line"] == line and per["PWR"].params["line"] == line
    assert per["DMA1"].params["requests"] == line and per["DMA2"].params["requests"] == line


@pytest.mark.parametrize("fn,name,package,npins,ngpio,flash,sram,opd,line", PARTS, ids=IDS)
def test_pin_table_check_strict(fn, name, package, npins, ngpio, flash, sram, opd, line):
    rep = tool("pin_table_check").check_file(CHIPS / f"{fn}.yaml", expect=npins)
    assert rep.ok and not rep.warnings, rep.text()


@pytest.mark.parametrize("fn,name,package,npins,ngpio,flash,sram,opd,line", PARTS, ids=IDS)
def test_svd_check_zero_mismatches(fn, name, package, npins, ngpio, flash, sram, opd, line):
    svd = SVDS[line]
    if not svd.exists():
        pytest.skip(f"{svd.name} not downloaded")
    out = io.StringIO()
    assert tool("svd2chip").check(str(svd), str(CHIPS / f"{fn}.yaml"), out=out) == 0, out.getvalue()
    assert "OMITTED TIM8" in out.getvalue()                    # SVD superset block (no TIM8 on these dies)


@pytest.mark.parametrize("fn", IDS)
def test_chip_builds_without_warnings(fn):
    chip = Chip(desc_of(fn))
    assert not [e for e in chip.log if e.kind == "warn"]
    from chipem.periph.stm32 import IMPORT_ERRORS
    assert IMPORT_ERRORS == {}


# ------------------------------------------------------------------------------- open pin data (the source)
NS = {"s": "http://dummy.com"}


@pytest.mark.parametrize("fn,name,package,npins,ngpio,flash,sram,opd,line", PARTS, ids=IDS)
def test_positions_match_open_pin_data(fn, name, package, npins, ngpio, flash, sram, opd, line):
    path = OPD / opd
    if not path.exists():
        pytest.skip("open pin data not downloaded")
    root = ET.parse(path).getroot()
    assert root.get("Package") == package
    want = {}
    for p in root.findall("s:Pin", NS):
        m = re.match(r"P[A-H]\d+", p.get("Name"))
        if m and p.get("Type") == "I/O":
            want[int(p.get("Position"))] = m.group(0)
    got = {p.num: p.gpio_name for p in desc_of(fn).pins if p.kind == "gpio"}
    assert got == want


def _hal_afs(device: str) -> set[tuple[int, str]]:
    """(AF number, peripheral) pairs defined by the HAL for `device` (GPIO_AF<n>_<PERIPH>)."""
    text = HAL_GPIO_EX.read_text()
    m = re.search(r"#if defined\(%s\)[^\n]*\n(.*?)#endif /\* %s" % (device, device.split(")")[0]), text, re.S)
    assert m, device
    return {(int(a), p) for a, p in re.findall(r"#define GPIO_AF(\d+)_([A-Za-z0-9_]+)\s", m.group(1))}


@pytest.mark.parametrize("line,device", [("f401", "STM32F401xC"), ("f411", "STM32F411xE")])
def test_af_numbers_match_hal_definitions(line, device):
    """Every AFn signal of the pin tables has a GPIO_AFn_<periph> define for that device in the HAL (I2Sx signals
    share the SPIx AF; the system AFs AF0/AF15 are generic)."""
    if not HAL_GPIO_EX.exists():
        pytest.skip("STM32CubeF4 HAL not vendored")
    hal = _hal_afs(device)
    checked = 0
    for fn in (p[0] for p in PARTS if p[-1] == line):
        for pin in desc_of(fn).pins:
            for k, v in pin.functions.items():
                if not k.startswith("AF") or k in ("AF0", "AF15"):
                    continue
                af = int(k[2:])
                for sig in v.split("/"):
                    per = sig.split("_")[0]
                    m = re.match(r"I2S(\d)(ext)?$", per)
                    cands = {per}
                    if m:
                        cands = {f"SPI{m.group(1)}", f"I2S{m.group(1)}ext", "I2S2ext", "I2S3ext"}
                    if per == "I2S":                       # I2S_CKIN
                        cands = {"SPI1", "SPI2", "SPI3", "SPI4", "SPI5"}
                    if per == "OTG":
                        cands = {"OTG_FS", "OTG"}
                    assert any((af, c) in hal for c in cands), (fn, pin.name, k, sig)
                    checked += 1
    assert checked > 150


def test_packages_of_a_line_agree():
    """A GPIO has the same functions on every package of a die (one AF table per die)."""
    for line in ("f401", "f411"):
        ref: dict[str, tuple] = {}
        for fn in (p[0] for p in PARTS if p[-1] == line):
            for p in desc_of(fn).pins:
                if p.kind == "gpio":
                    key = (p.name, p.functions, p.default)
                    assert ref.setdefault(p.gpio_name, key) == key, (fn, p.gpio_name)
        assert len(ref) == 81


def test_lqfp64_f401_f411_pin_compatible():
    a = {p.num: p.gpio_name or p.name for p in desc_of("stm32f401re").pins}
    b = {p.num: p.gpio_name or p.name for p in desc_of("stm32f411re").pins}
    diff = {n for n in a if a[n] != b[n]}
    assert diff == {13}                                       # F401RE "VREF+" vs F411RE "VDDA" (ST's naming)


# ------------------------------------------------------------------------------- spot checks
def test_nucleo_and_black_pill_pins():
    for fn in ("stm32f401re", "stm32f411re"):
        assert gpio(fn, "PA2").num == 16 and gpio(fn, "PA2").functions["AF7"] == "USART2_TX"    # ST-LINK VCP
        assert gpio(fn, "PA3").num == 17 and gpio(fn, "PA3").functions["AF7"] == "USART2_RX"
        assert gpio(fn, "PA5").num == 21 and gpio(fn, "PA5").functions["AF5"].startswith("SPI1_SCK")   # LD2
        assert gpio(fn, "PC13").num == 2 and gpio(fn, "PC13").functions["additional"] == "RTC_AF1"   # B1
        assert gpio(fn, "PA11").functions["AF8"] == "USART6_TX" and gpio(fn, "PA12").functions["AF8"] == "USART6_RX"
        assert gpio(fn, "PA13").default == "JTMS-SWDIO" and gpio(fn, "PB3").default == "JTDO/TRACESWO"
        assert gpio(fn, "PB3").functions["AF9"] == "I2C2_SDA" and gpio(fn, "PB4").functions["AF9"] == "I2C3_SDA"
        assert gpio(fn, "PA0").name == "PA0/WKUP" and gpio(fn, "PA0").functions["ADC"] == "ADC1_IN0"
        assert gpio(fn, "PA9").functions["additional"] == "OTG_FS_VBUS"
    # F411-only alternate functions: SPI5/I2S5, I2S1/I2S4, USART1 on PA15/PB3, SDIO on PB15/PA9
    assert gpio("stm32f411re", "PB0").functions["AF6"] == "SPI5_SCK/I2S5_CK"
    assert gpio("stm32f411re", "PA10").functions["AF6"] == "SPI5_MOSI/I2S5_SD"
    assert gpio("stm32f411re", "PA5").functions["AF5"] == "SPI1_SCK/I2S1_CK"
    assert gpio("stm32f411re", "PA15").functions["AF7"] == "USART1_TX"
    assert gpio("stm32f411re", "PB12").functions["AF7"] == "SPI3_SCK/I2S3_CK"
    assert "AF6" not in gpio("stm32f401re", "PB0").functions and gpio("stm32f401re", "PA5").functions["AF5"] == "SPI1_SCK"
    # UFQFPN48 (Black Pill): PC13 pin 2, PA0 pin 10, PB2 pin 20, BOOT0 pin 44
    for fn in ("stm32f401cc", "stm32f411ce"):
        assert gpio(fn, "PC13").num == 2 and gpio(fn, "PA0").num == 10 and gpio(fn, "PB2").num == 20
        assert next(p for p in desc_of(fn).pins if p.num == 44).name == "BOOT0"
        assert {p.port for p in desc_of(fn).pins if p.kind == "gpio"} == {"A", "B", "C", "H"}
    # LQFP100: port E (SPI4 on the F401 too)
    assert gpio("stm32f401ve", "PE2").num == 1 and gpio("stm32f401ve", "PE2").functions["AF5"] == "SPI4_SCK"
    assert gpio("stm32f411ve", "PE2").functions["AF5"] == "SPI4_SCK/I2S4_CK"
    assert gpio("stm32f411ve", "PE2").functions["AF6"] == "SPI5_SCK/I2S5_CK"
