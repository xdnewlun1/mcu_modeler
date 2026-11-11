"""STM32F427xx/STM32F437xx pin tables (chips/stm32f427vg.yaml LQFP100, chips/stm32f437ai.yaml UFBGA169 and the extra
package files stm32f427zg/ig/igh, stm32f437zi/ii): counts from DS9405/DS9484 Table 2, pin_table_check, spot checks
read by eye from DS9405 Rev 14 / DS9484 Rev 14 Table 10 (pin and ball definitions), Table 12 (alternate function
mapping) and the pinout figures, and agreement of the shared GPIOs between all package files."""
from __future__ import annotations

import functools
import sys
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip

ROOT = Path(__file__).resolve().parent.parent
CHIPS = ROOT / "chips"


def tool(name: str):
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        return __import__(name)
    finally:
        sys.path.pop(0)


# file, name, package, pin/ball count, GPIO count (DS9405/DS9484 Table 2), flash bytes
PARTS = [
    ("stm32f427vg", "STM32F427VG", "LQFP100", 100, 82, 0x100000),
    ("stm32f427zg", "STM32F427ZG", "LQFP144", 144, 114, 0x100000),
    ("stm32f427ig", "STM32F427IG", "LQFP176", 176, 140, 0x100000),
    ("stm32f427igh", "STM32F427IGH", "UFBGA176+25", 201, 140, 0x100000),
    ("stm32f437ai", "STM32F437AI", "UFBGA169", 169, 130, 0x200000),
    ("stm32f437zi", "STM32F437ZI", "LQFP144", 144, 114, 0x200000),
    ("stm32f437ii", "STM32F437II", "LQFP176", 176, 140, 0x200000),
]
IDS = [p[0] for p in PARTS]


@functools.lru_cache(maxsize=None)
def desc_of(fn: str):
    return load_chip(CHIPS / f"{fn}.yaml")


def gpio(fn: str, g: str):
    return next(p for p in desc_of(fn).pins if p.gpio_name == g)


def num(fn: str, name: str) -> list[int]:
    return sorted(p.num for p in desc_of(fn).pins if p.name == name)


def balls(fn: str) -> dict[str, object]:
    import yaml
    raw = yaml.safe_load((CHIPS / f"{fn}.yaml").read_text())["pins"]
    by_num = {p.num: p for p in desc_of(fn).pins}
    return {r["ball"]: by_num[r["num"]] for r in raw}


@pytest.mark.parametrize("fn,name,package,npins,ngpio,flash", PARTS, ids=IDS)
def test_description(fn, name, package, npins, ngpio, flash):
    d = desc_of(fn)
    assert (d.name, d.package, d.family) == (name, package, "stm32")
    assert len(d.pins) == npins and sorted(p.num for p in d.pins) == list(range(1, npins + 1))
    assert len({p.gpio_name for p in d.pins if p.kind == "gpio"}) == ngpio
    assert {m.name: (m.base, m.size) for m in d.memory}["FLASH"] == (0x08000000, flash)
    # no LTDC alternate function (STM32F429/F439 only) and no data-sheet mixed-case UART names
    for p in d.pins:
        for v in p.functions.values():
            assert "LCD_" not in v and not v.endswith(("_Tx", "_Rx")), (p.name, v)


@pytest.mark.parametrize("fn,name,package,npins,ngpio,flash", PARTS, ids=IDS)
def test_pin_table_check(fn, name, package, npins, ngpio, flash):
    rep = tool("pin_table_check").check_file(CHIPS / f"{fn}.yaml", expect=npins)
    assert rep.ok and not rep.warnings, rep.text()


@pytest.mark.parametrize("fn", IDS)
def test_chip_builds_without_pin_warnings(fn):
    # Only model-availability warnings ("... not available", a model of a new block not registered yet) are
    # tolerated here; anything else at build time (pins, mux, unknown function keys) fails.
    chip = Chip(desc_of(fn))
    bad = [e for e in chip.log if e.kind == "warn" and "not available" not in str(e)]
    assert not bad, bad


def test_shared_gpios_agree_between_packages():
    """A GPIO has the same pin name, functions and reset default on every package (one die, DS Table 12)."""
    ref: dict[str, tuple] = {}
    for fn in IDS:
        for p in desc_of(fn).pins:
            if p.kind != "gpio":
                continue
            key = (p.name, p.functions, p.default)
            assert ref.setdefault(p.gpio_name, key) == key, (fn, p.gpio_name)
    assert len(ref) == 140


def test_lqfp144_matches_stm32f407zg_positions():
    """Pin-compatible with the STM32F407ZG: same pin at every LQFP144 position (DS9405 section 2.1)."""
    f407 = {p.num: p.name for p in load_chip(CHIPS / "stm32f407zg.yaml").pins}
    assert {p.num: p.name for p in desc_of("stm32f427zg").pins} == f407


# ---------------------------------------------- LQFP100 (DS9405 Rev 14 Table 10 pages 53-71, Table 12 pages 75-86)
def test_lqfp100_spot_checks():
    fn = "stm32f427vg"
    pe2 = gpio(fn, "PE2")                    # p.53: pin 1, TRACECLK, SPI4_SCK, SAI1_MCLK_A, ETH_MII_TXD3, FMC_A23
    assert pe2.num == 1 and pe2.functions == {"AF0": "TRACECLK", "AF5": "SPI4_SCK", "AF6": "SAI1_MCLK_A",
                                              "AF11": "ETH_MII_TXD3", "AF12": "FMC_A23", "AF15": "EVENTOUT"}
    assert gpio(fn, "PE3").functions["AF6"] == "SAI1_SD_B"            # p.53 / p.79 (Table 12 AF6)
    assert gpio(fn, "PE4").functions["AF6"] == "SAI1_FS_A"
    assert gpio(fn, "PE5").functions["AF6"] == "SAI1_SCK_A"
    assert gpio(fn, "PE6").functions["AF6"] == "SAI1_SD_A"
    pe7, pe8 = gpio(fn, "PE7"), gpio(fn, "PE8")  # p.60: pins 38/39 "UART7_Rx"/"UART7_Tx" (case corrected), FMC_D4/D5
    assert (pe7.num, pe7.functions["AF8"], pe7.functions["AF12"]) == (38, "UART7_RX", "FMC_D4")
    assert (pe8.num, pe8.functions["AF8"], pe8.functions["AF12"]) == (39, "UART7_TX", "FMC_D5")
    assert gpio(fn, "PE1").functions["AF8"] == "UART8_TX" and gpio(fn, "PE0").functions["AF8"] == "UART8_RX"
    pa9, pa10 = gpio(fn, "PA9"), gpio(fn, "PA10")  # p.65: pins 68/69, Table 12 p.75 AF7 USART1_TX/RX
    assert (pa9.num, pa9.functions["AF7"], pa9.functions["additional"]) == (68, "USART1_TX", "OTG_FS_VBUS")
    assert (pa10.num, pa10.functions["AF7"]) == (69, "USART1_RX")
    assert gpio(fn, "PA0").name == "PA0/WKUP" and gpio(fn, "PA0").functions["ADC"] == "ADC123_IN0"
    assert gpio(fn, "PA0").functions["AF1"] == "TIM2_CH1/TIM2_ETR"    # DS9405 spelling (DS8626: TIM2_CH1_ETR)
    assert gpio(fn, "PA4").functions["analog"] == "DAC_OUT1"
    assert gpio(fn, "PC14").name == "PC14/OSC32_IN" and gpio(fn, "PH0").num == 12
    assert gpio(fn, "PA13").default == "JTMS-SWDIO" and gpio(fn, "PB4").default == "NJTRST"
    # SDRAM: PC0/PC2/PC3 carry SDNWE/SDNE0/SDCKE0 (p.56) but FMC_SDCLK (PG8) is not on the LQFP100
    assert gpio(fn, "PC2").functions["AF12"] == "FMC_SDNE0" and gpio(fn, "PC3").functions["AF12"] == "FMC_SDCKE0"
    assert not any("FMC_SDCLK" in v for p in desc_of(fn).pins for v in p.functions.values())
    assert num(fn, "NRST") == [14] and num(fn, "BOOT0") == [94]       # p.56, p.70
    assert num(fn, "VCAP_1") == [49] and num(fn, "VCAP_2") == [73]    # p.66, p.68
    assert num(fn, "VSS") == [10, 27, 74, 99] and num(fn, "VDD") == [11, 19, 28, 50, 75, 100]
    assert num(fn, "VBAT") == [6] and num(fn, "VREF+") == [21] and num(fn, "VSSA") == [20]
    assert desc_of(fn).pin(100).name == "VDD"                          # last pin (p.71, Figure 11)


# ---------------------------------------------- UFBGA169 (DS9484 Rev 14 Table 10 pages 53-71, Figure 16 page 50)
def test_ufbga169_spot_checks():
    fn = "stm32f437ai"
    b = balls(fn)
    assert len(b) == 169 and b["A1"].num == 1 and b["N13"].num == 169 and b["B1"].num == 14
    assert {k for k, p in b.items() if p.name == "NC"} == {"A1", "A13", "N1", "N13"}    # Figure 16 note 2
    assert b["B2"].name == "PE2" and b["B2"].functions["AF6"] == "SAI1_MCLK_A"            # p.53
    assert (b["N7"].name, b["N7"].functions["AF8"]) == ("PE7", "UART7_RX")                # p.60
    assert (b["J8"].name, b["J8"].functions["AF8"]) == ("PE8", "UART7_TX")
    assert (b["E8"].name, b["E9"].name) == ("PA9", "PA10")                                # p.65
    # SDRAM controller pins (Table 12 AF12, Table 11 "FMC pin definition" SDRAM column)
    assert (b["F13"].name, b["F13"].functions["AF12"]) == ("PG8", "FMC_SDCLK")            # p.64 / p.81
    assert (b["M5"].name, b["M5"].functions["AF12"]) == ("PF11", "FMC_SDNRAS")           # p.59 / p.81
    assert (b["C6"].name, b["C6"].functions["AF12"]) == ("PG15", "FMC_SDNCAS")           # p.69 / p.82
    assert (b["G6"].name, b["G6"].functions["AF12"]) == ("PC0", "FMC_SDNWE")             # p.56
    assert (b["H6"].name, b["H6"].functions["AF12"]) == ("PC2", "FMC_SDNE0")
    assert (b["H7"].name, b["H7"].functions["AF12"]) == ("PC3", "FMC_SDCKE0")
    assert b["H12"].functions["AF12"] == "FMC_A14/FMC_BA0" and b["G13"].functions["AF12"] == "FMC_A15/FMC_BA1"
    assert (b["H2"].name, b["A5"].name, b["C3"].name) == ("NRST", "BOOT0", "PDR_ON")      # p.56, p.70, p.71
    assert (b["N9"].name, b["D12"].name, b["M1"].name) == ("VCAP_1", "VCAP_2", "BYPASS_REG")
    assert (b["J1"].name, b["J2"].name, b["J3"].name, b["J4"].name) == ("VSSA", "VREF-", "VREF+", "VDDA")
    # Figure 16 census: 10 VSS (D7 D11 E7 F5 F6 G7 G9 J6 J7 J10), 14 VDD (D3 D6 D10 E6 F4 F7 F8 G8 G10 H4 H8 J11 K6 L6)
    assert sum(p.name == "VSS" for p in b.values()) == 10 and sum(p.name == "VDD" for p in b.values()) == 14
    assert b["G7"].name == "VSS" and b["L6"].name == "VDD"


def test_data_sheet_corrections():
    """PH6/PH7 AF15: Table 12 prints '-', Table 10 and ST's open pin data give EVENTOUT (as on every other GPIO)."""
    for fn in ("stm32f427ig", "stm32f437ai"):
        assert gpio(fn, "PH6").functions["AF15"] == "EVENTOUT"
        assert gpio(fn, "PH7").functions["AF15"] == "EVENTOUT"
    assert gpio("stm32f427ig", "PH6").functions["AF12"] == "FMC_SDNE1"


def test_ufbga176_thermal_balls():
    b = balls("stm32f427igh")
    thermal = [f"{r}{c}" for r in "FGHJK" for c in range(6, 11)]
    assert all(b[t].name == "VSS" for t in thermal) and b["A1"].num == 1 and b["R15"].num == 201
