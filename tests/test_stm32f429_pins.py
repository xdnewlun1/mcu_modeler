"""STM32F429xx/STM32F439xx chip descriptions (chips/stm32f429zi.yaml base + package variants): ordering-code facts,
pin tables (pin_table_check --strict, LCD_* alternate functions, agreement with the F427/F437 tables and between
packages, LQFP208/TFBGA216 spot checks), cross-checks against ST's open pin data (positions and LCD AF numbers) and
STM32F429.svd / STM32F427.svd (`svd2chip --check`), and that every chip builds without warnings."""
from __future__ import annotations

import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

from chipem.chip import Chip
from chipem.chipdesc import load_chip

ROOT = Path(__file__).resolve().parent.parent
CHIPS = ROOT / "chips"
OPD = ROOT / "svd" / "dl-open-pin-data-f429"
NS = {"n": "http://dummy.com"}

# file, name, package, package pins, GPIOs (DS9405 Table 2 / DS9484 Table 2), open pin data file, F427 file with
# the same package (None: LQFP208/TFBGA216, new here)
PARTS = [
    ("stm32f429zi", "STM32F429ZI", "LQFP144", 144, 114, "STM32F429ZITx.xml", "stm32f427zg"),
    ("stm32f429vi", "STM32F429VI", "LQFP100", 100, 82, "STM32F429VITx.xml", "stm32f427vg"),
    ("stm32f429ii", "STM32F429II", "LQFP176", 176, 140, "STM32F429IITx.xml", "stm32f427ig"),
    ("stm32f429iih", "STM32F429IIH", "UFBGA176+25", 201, 140, "STM32F429I(E-G-I)Hx.xml", "stm32f427igh"),
    ("stm32f429ai", "STM32F429AI", "UFBGA169", 169, 130, "STM32F429A(G-I)Hx.xml", "stm32f437ai"),
    ("stm32f429bi", "STM32F429BI", "LQFP208", 208, 168, "STM32F429B(E-G-I)Tx.xml", None),
    ("stm32f429ni", "STM32F429NI", "TFBGA216", 216, 168, "STM32F429NIHx.xml", None),
    ("stm32f439zi", "STM32F439ZI", "LQFP144", 144, 114, "STM32F439Z(G-I)Tx.xml", "stm32f427zg"),
    ("stm32f439ii", "STM32F439II", "LQFP176", 176, 140, "STM32F439I(G-I)Tx.xml", "stm32f427ig"),
    ("stm32f439ai", "STM32F439AI", "UFBGA169", 169, 130, "STM32F439AIHx.xml", "stm32f437ai"),
    ("stm32f439bi", "STM32F439BI", "LQFP208", 208, 168, "STM32F439B(G-I)Tx.xml", None),
    ("stm32f439ni", "STM32F439NI", "TFBGA216", 216, 168, "STM32F439N(G-I)Hx.xml", None),
]
IDS = [p[0] for p in PARTS]
_D: dict = {}


def desc_of(fn):
    if fn not in _D:
        _D[fn] = load_chip(CHIPS / f"{fn}.yaml")
    return _D[fn]


def balls(fn) -> dict:
    """pin num -> ball name of a BGA file (the `ball:` key of the YAML, not kept by load_chip)."""
    raw = yaml.safe_load((CHIPS / f"{fn}.yaml").read_text()).get("pins") or []
    return {r["num"]: r["ball"] for r in raw if "ball" in r}


def gpio(p) -> str:
    return p.name.split("/")[0]


def gpios(d) -> dict:
    return {gpio(p): p for p in d.pins if p.kind == "gpio"}


def tool(*args):
    return subprocess.run([sys.executable, "-I", *map(str, args)], capture_output=True, text=True, cwd=ROOT)


@pytest.mark.parametrize("fn,name,package,npins,ngpio,xml,f427", PARTS, ids=IDS)
def test_description(fn, name, package, npins, ngpio, xml, f427):
    d = desc_of(fn)
    assert (d.name, d.package) == (name, package)
    assert len(d.pins) == npins and len(gpios(d)) == ngpio
    flash = next(m for m in d.memory if m.name == "FLASH")
    assert flash.size == 0x200000                         # "I" = 2048 KB (section 8 ordering information)
    assert d.irqs[88] == "LCD_TFT" and d.irqs[89] == "LCD_TFT_1"
    assert any(p.name == "LTDC" and p.model == "stm32.ltdc" for p in d.peripherals)


@pytest.mark.parametrize("fn,name,package,npins,ngpio,xml,f427", PARTS, ids=IDS)
def test_pin_table_check_and_svd_check(fn, name, package, npins, ngpio, xml, f427):
    r = tool(ROOT / "tools" / "pin_table_check.py", CHIPS / f"{fn}.yaml", "--strict", "--expect", npins)
    assert r.returncode == 0 and "0 error(s), 0 warning(s)" in r.stdout, r.stdout + r.stderr
    for svd in ("dl-stm32f429/STM32F429.svd", "dl-stm32f427/STM32F427.svd"):
        r = tool(ROOT / "tools" / "svd2chip.py", ROOT / "svd" / svd, "--check", CHIPS / f"{fn}.yaml")
        assert r.stdout.strip().endswith("0 mismatch(es)"), r.stdout + r.stderr


@pytest.mark.parametrize("fn", IDS)
def test_chip_builds_without_warnings(fn):
    c = Chip(desc_of(fn))
    assert not [e for e in c.log if e.kind == "warn"]
    lt = c.peripherals["LTDC"]
    assert type(lt).__name__ == "LTDC"
    assert bool(getattr(c.peripherals["CRYP"], "absent", False)) == fn.startswith("stm32f429")   # F439 only


@pytest.mark.parametrize("fn,name,package,npins,ngpio,xml,f427", PARTS, ids=IDS)
def test_positions_match_open_pin_data(fn, name, package, npins, ngpio, xml, f427):
    root = ET.parse(OPD / xml).getroot()
    opd = {p.get("Name").split("-")[0].split("/")[0]: p.get("Position") for p in root.findall("n:Pin", NS)
           if p.get("Type") == "I/O"}
    b = balls(fn) or balls("stm32f429zi" if fn.endswith("zi") else fn)
    mine = {gpio(p): b.get(p.num, str(p.num)) for p in desc_of(fn).pins if p.kind == "gpio"}
    assert mine == opd


def _lcd_from_modes() -> dict:
    root = ET.parse(OPD / "GPIO-STM32F427_gpio_v1_0_Modes.xml").getroot()
    out: dict = {}
    for g in root.findall("n:GPIO_Pin", NS):
        for s in g.findall("n:PinSignal", NS):
            if not s.get("Name", "").startswith("LTDC_"):
                continue
            for v in s.iter("{http://dummy.com}PossibleValue"):
                m = re.match(r"GPIO_AF(\d+)_", v.text or "")
                if m:
                    out.setdefault(g.get("Name"), {})[f"AF{m.group(1)}"] = "LCD_" + s.get("Name")[5:]
    return out


def test_lcd_alternate_functions_match_open_pin_data():
    modes = _lcd_from_modes()
    d = gpios(desc_of("stm32f429ni"))                       # TFBGA216 has all 168 GPIOs
    mine = {g: {k: v for k, v in p.functions.items() if str(v).startswith("LCD_")} for g, p in d.items()}
    mine = {g: f for g, f in mine.items() if f}
    assert mine == modes
    assert sum(len(f) for f in mine.values()) == 81 and len(mine) == 79


@pytest.mark.parametrize("fn,name,package,npins,ngpio,xml,f427", [p for p in PARTS if p[6]], ids=[p[0] for p in PARTS if p[6]])
def test_same_as_f427_except_lcd(fn, name, package, npins, ngpio, xml, f427):
    """The F429 tables are the F427/F437 tables (same positions, names, power pins) plus the LCD_* functions."""
    a, b = desc_of(fn), load_chip(CHIPS / f"{f427}.yaml")
    assert len(a.pins) == len(b.pins)
    assert balls(fn) == balls(f427) if f427 != "stm32f427zg" else True
    for p, q in zip(a.pins, b.pins):
        assert (p.num, p.name, p.kind) == (q.num, q.name, q.kind)
        assert {k: v for k, v in p.functions.items() if not str(v).startswith("LCD_")} == dict(q.functions)


def test_shared_gpios_agree_between_packages():
    ref: dict = {}
    for fn in IDS:
        for g, p in gpios(desc_of(fn)).items():
            ref.setdefault(g, dict(p.functions))
            assert dict(p.functions) == ref[g], (fn, g)
    assert len(ref) == 168


def test_spot_checks_discovery_lcd_pins():
    d = gpios(desc_of("stm32f429zi"))
    assert d["PG7"].functions["AF14"] == "LCD_CLK" and d["PC6"].functions["AF14"] == "LCD_HSYNC"
    assert d["PA4"].functions["AF14"] == "LCD_VSYNC" and d["PF10"].functions["AF14"] == "LCD_DE"
    assert d["PB0"].functions["AF9"] == "LCD_R3" and d["PB1"].functions["AF9"] == "LCD_R6"
    assert d["PG10"].functions["AF9"] == "LCD_G3" and d["PG10"].functions["AF14"] == "LCD_B2"
    assert d["PG12"].functions["AF9"] == "LCD_B4" and d["PG12"].functions["AF14"] == "LCD_B1"
    assert d["PE4"].functions["AF14"] == "LCD_B0" and d["PA9"].functions["AF7"] == "USART1_TX"
    assert d["PB5"].functions["AF12"] == "FMC_SDCKE1" and d["PB6"].functions["AF12"] == "FMC_SDNE1"
    assert [p.num for p in desc_of("stm32f429zi").pins if p.name == "PG7"] == [92]


def test_spot_checks_lqfp208_tfbga216():
    bi = {p.num: p for p in desc_of("stm32f429bi").pins}
    assert gpio(bi[1]) == "PE2" and gpio(bi[43]) == "PA0" and gpio(bi[208]) == "PI7"
    assert bi[10].name == "PC15/OSC32_OUT" and bi[14].name == "VSS" and bi[147].name == "PA13"
    bb = balls("stm32f429ni")
    ni = {bb[p.num]: p for p in desc_of("stm32f429ni").pins}
    assert gpio(ni["A3"]) == "PE2" and gpio(ni["N3"]) == "PA0" and gpio(ni["A15"]) == "PA13"
    assert ni["N1"].name == "VREF-" and ni["L5"].name == "BYPASS_REG" and gpio(ni["B3"]) == "PG13"
    k = gpios(desc_of("stm32f429ni"))
    assert k["PK7"].functions == {"AF14": "LCD_DE", "AF15": "EVENTOUT"}
    assert k["PI14"].functions == {"AF14": "LCD_CLK", "AF15": "EVENTOUT"}
    assert k["PJ0"].functions == {"AF14": "LCD_R1", "AF15": "EVENTOUT"}
    assert sum(1 for p in desc_of("stm32f429ni").pins if p.kind == "gpio" and p.port in "JK") == 24
