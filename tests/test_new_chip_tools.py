"""tools/new_chip.py (chip scaffolder) and tools/pin_table_check.py (pin table validator).

The scaffolder is run on the cached STM32F405 SVD into a temp dir: the chip YAML must load, map the STM32 models the
way chips/stm32f405zg.yaml does, pass `svd2chip --check`, build a Chip without stub fallbacks, and its generated test
file must pass everything except the deliberate TODO spot-check test.  pin_table_check must pass the real chips and
catch a broken table."""
from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest
import yaml

from chipem.chip import Chip
from chipem.chipdesc import load_chip

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
SVD = sorted((ROOT / "svd").glob("dl-stm32f405-*/STM32F405.svd"))
K22_SVD = sorted((ROOT / "svd").glob("dl-*/MK22F12.svd"))
sys.path.insert(0, str(TOOLS))
import new_chip  # noqa: E402
import pin_table_check  # noqa: E402
import svd2chip  # noqa: E402

sys.path.remove(str(TOOLS))

PINS_CSV = """num,name,kind,port,bit,functions,default
1,VDD,power,,,,
2,PA9,gpio,A,9,AF7=USART1_TX,
3,PA10,gpio,A,10,AF7=USART1_RX;AF10=OTG_FS_ID,
4,NRST,special,,,,
5,PB0,gpio,B,0,AF1=TIM1_CH2N;ADC=ADC12_IN8,
6,PC14/OSC32_IN,gpio,C,14,analog=OSC32_IN,
7,BOOT0,special,,,,
8,VSS,power,,,,
"""


@pytest.fixture(scope="module")
def scaffold(tmp_path_factory):
    if not SVD:
        pytest.skip("STM32F405.svd not downloaded")
    root = tmp_path_factory.mktemp("scaffold")
    csv = root / "pins.csv"
    csv.write_text(PINS_CSV)
    rc = new_chip.main(["--part", "TESTF405", "--package", "TSSOP8", "--family", "stm32", "--svd", str(SVD[0]),
                        "--fpu", "--pins", str(csv), "--root", str(root),
                        "--mem", "FLASH:flash:0x08000000:0x100000", "--mem", "SRAM:ram:0x20000000:0x20000"])
    assert rc == 0
    return root


def test_scaffold_writes_three_files(scaffold):
    for rel in ("chips/testf405.yaml", "chips/testf405_regs.yaml", "tests/test_testf405_chip.py"):
        assert (scaffold / rel).is_file(), rel


def test_scaffold_yaml_loads_and_maps_models(scaffold):
    desc = load_chip(scaffold / "chips" / "testf405.yaml")
    assert desc.name == "TESTF405" and desc.package == "TSSOP8" and desc.family == "stm32"
    assert desc.gpio_name_format == "P{port}{bit}" and desc.regs_file == "testf405_regs.yaml"
    assert desc.core.cpu == "cortex-m4" and desc.core.fpu and desc.core.clock_hz == 16e6
    assert desc.extra["vtor"] == 0x08000000
    models = {p.name: p for p in desc.peripherals}
    assert models["GPIOA"].model == "stm32.gpio" and models["GPIOA"].params == {"port": "A"}
    assert models["USART1"].model == "stm32.usart" and models["USART1"].params == {"index": 1, "kind": "usart"}
    assert models["UART4"].params == {"index": 4, "kind": "uart"}
    assert models["RCC"].model == "stm32.rcc" and models["IWDG"].model == "stm32.iwdg"
    assert models["TIM2"].model == "stm32.tim" and models["TIM9"].model == "stm32.tim"
    assert models["EXTI"].model == "stm32.exti" and models["SPI1"].model == "stub"
    assert models["ADC1"].size == 0x100                              # clamped to the next block (ADC2)
    assert not any(0xE000E000 <= p.base < 0xE000F000 for p in desc.peripherals)   # SCS is arch-owned
    assert desc.irqs[37] == "USART1" and desc.irqs[6] == "EXTI0"
    assert [p.num for p in desc.pins] == list(range(1, 9))
    assert desc.pin("PA9").functions["AF7"] == "USART1_TX" and desc.pin("PB0").functions["ADC"] == "ADC12_IN8"
    assert desc.pin(4).kind == "special" and desc.pin("PA9").default == "GPIO"


def test_scaffold_matches_the_real_f405_models(scaffold):
    gen = {p.name: p.model for p in load_chip(scaffold / "chips" / "testf405.yaml").peripherals}
    real = {p.name: p.model for p in load_chip(ROOT / "chips" / "stm32f405zg.yaml").peripherals}
    diff = {n: (real[n], gen[n]) for n in real.keys() & gen.keys() if real[n] != gen[n]}
    assert diff == {}


def test_scaffold_passes_svd_check_and_builds(scaffold):
    out = io.StringIO()
    assert svd2chip.check(SVD[0], scaffold / "chips" / "testf405.yaml", out=out) == 0, out.getvalue()
    chip = Chip(load_chip(scaffold / "chips" / "testf405.yaml"))
    assert not [e for e in chip.log if e.kind == "warn"]
    assert chip.pins.get("PA9").id == "PA9"


def test_generated_test_file_only_todo_fails(scaffold):
    path = scaffold / "tests" / "test_testf405_chip.py"
    spec = importlib.util.spec_from_file_location("gen_test_testf405", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    desc = load_chip(mod.YAML)
    assert mod.EXPECTED_PINS == 8 and mod.EXPECTED_GPIO == 4 and mod.SVD
    for name in ("test_core_and_package", "test_pin_table_checker", "test_pin_count_numbers_and_gpio",
                 "test_memory_and_irqs", "test_chip_builds_without_stub_fallbacks"):
        getattr(mod, name)(desc)
    mod.test_svd_check_zero_mismatches()
    with pytest.raises(pytest.fail.Exception, match="TODO"):
        mod.test_pin_spot_checks(desc)


def test_scaffold_refuses_to_overwrite(scaffold):
    with pytest.raises(SystemExit, match="exists"):
        new_chip.main(["--part", "TESTF405", "--family", "stm32", "--root", str(scaffold)])


def test_scaffold_without_svd_or_pins_is_marked_todo(tmp_path):
    assert new_chip.main(["--part", "ACME100", "--package", "QFN32", "--family", "acme", "--root", str(tmp_path)]) == 0
    text = (tmp_path / "chips" / "acme100.yaml").read_text()
    assert "pins: []" in text and "TODO all 32 package pins" in text
    desc = load_chip(tmp_path / "chips" / "acme100.yaml")
    assert desc.peripherals == [] and desc.pins == [] and desc.core.cpu == "cortex-m3"
    rep = pin_table_check.check_desc(desc)
    assert not rep.ok and any("empty" in e for e in rep.errors)


def test_model_rule_requires_matching_registers():
    # STM32F1-style GPIO (CRL/CRH, no MODER) must not be mapped to the F4 stm32.gpio model
    model, params, name, note = new_chip.map_model("stm32", "GPIOA", ["CRL", "CRH", "IDR", "ODR", "BSRR"])
    assert model == "stub" and "MODER" in note and "TODO" in note
    assert new_chip.map_model("stm32", "GPIOC", ["MODER", "AFRL", "IDR", "ODR", "BSRR"])[:2] == ("stm32.gpio",
                                                                                                {"port": "C"})
    assert new_chip.map_model("kinetis", "UART3", ["BDH", "BDL", "C2", "S1", "D"])[:2] == ("kinetis.uart", {"index": 3})
    assert new_chip.map_model("kinetis", "RFVBAT", [])[0:3:2] == ("kinetis.vbat", "VBAT")
    assert new_chip.map_model("ti_c2000", "SCIB", ["SCICCR", "SCICTL1", "SCITXBUF", "SCIRXBUF"])[0] == "ti_c2000.sci"
    epwm_regs = ["TBCTL", "TBPRD", "CMPA", "AQCTLA", "DBCTL", "ETSEL"]
    assert new_chip.map_model("ti_c2000", "EPWM3", epwm_regs)[:2] == ("ti_c2000.epwm",
                                                                      {"gate": "EPWM3ENCLK", "index": 3})
    assert new_chip.map_model("ti_c2000", "EPWM3", [])[0] == "stub"         # a different register layout
    ecap_regs = ["TSCTR", "CTRPHS", "CAP1", "CAP2", "ECCTL1", "ECCTL2", "ECEINT", "ECFLG", "ECCLR", "ECFRC"]
    assert new_chip.map_model("ti_c2000", "ECAP2", ecap_regs)[:2] == ("ti_c2000.ecap",
                                                                      {"gate": "ECAP2ENCLK", "index": 2})
    assert new_chip.map_model("ti_c2000", "ECAP2", [])[0] == "stub"         # a different register layout
    assert new_chip.map_model("ti_c2000", "EQEP1", [])[:2] == ("ti_c2000.storage", {"gate": "EQEP1ENCLK"})
    assert new_chip.map_model("ti_c2000", "FLASH", ["FOPT"])[:3] == ("ti_c2000.flash", {"regs": "FLASH"}, "FLASHREGS")
    # TIM9-14 (1-2 channels, up-counting only) use stm32.tim too; their features follow from the SVD registers
    for t in ("TIM9", "TIM12", "TIM14"):
        model, _params, _name, _note = new_chip.map_model("stm32", t, ["CR1", "PSC", "ARR", "SR", "DIER"])
        assert model == "stm32.tim", t
    assert new_chip.map_model("stm32", "TIM8", ["CR1", "PSC", "ARR", "SR", "DIER"])[0] == "stm32.tim"


@pytest.mark.skipif(not K22_SVD, reason="MK22F12.svd not downloaded")
def test_scaffold_kinetis_and_c2000_match_the_real_chips(tmp_path):
    assert new_chip.main(["--part", "K22TEST", "--package", "LQFP100", "--family", "kinetis", "--svd",
                          str(K22_SVD[0]), "--root", str(tmp_path)]) == 0
    assert new_chip.main(["--part", "C2KTEST", "--package", "LQFP176", "--family", "ti_c2000", "--regs",
                          str(ROOT / "chips" / "tms320f28232_regs.yaml"), "--root", str(tmp_path)]) == 0
    for gen, real in (("k22test", "mk22fn1m0vll12"), ("c2ktest", "tms320f28232")):
        g = {p.name: (p.model, p.base) for p in load_chip(tmp_path / "chips" / f"{gen}.yaml").peripherals}
        r = {p.name: (p.model, p.base) for p in load_chip(ROOT / "chips" / f"{real}.yaml").peripherals}
        assert {n: (r[n], g[n]) for n in r.keys() & g.keys() if r[n] != g[n]} == {}, gen
        assert len(r.keys() & g.keys()) >= 30
    k22 = {p.name: p for p in load_chip(tmp_path / "chips" / "k22test.yaml").peripherals}
    alias = k22["FTM2_ALIAS"]                       # not in the SVD: added by the family's alias table
    assert (alias.model, alias.base, alias.size, alias.params) == ("kinetis.ftm_alias", 0x4003A000, 0x1000,
                                                                   {"target": "FTM2"})
    assert k22["FTM2"].model == "kinetis.ftm" and k22["FTM2"].base == 0x400B8000
    out = io.StringIO()
    assert svd2chip.check(K22_SVD[0], tmp_path / "chips" / "k22test.yaml", out=out) == 0, out.getvalue()
    c2k = load_chip(tmp_path / "chips" / "c2ktest.yaml")
    assert c2k.core.arch == "c28x" and c2k.core.addr_unit_bits == 16 and c2k.gpio_name_format == "GPIO{num}"
    assert c2k.extra["boot"]["entry"] == 0x33FFF6


# ------------------------------------------------------------------------------------------------ pin_table_check
@pytest.mark.parametrize("chip", ["mk22fn1m0vll12", "mk22fx512vll12", "stm32f405zg", "tms320f28232"])
def test_pin_table_check_passes_real_chips(chip):
    rep = pin_table_check.check_file(ROOT / "chips" / f"{chip}.yaml")
    assert rep.ok and not rep.warnings, rep.text()


def test_pin_table_check_cli(capsys):
    assert pin_table_check.main([str(ROOT / "chips" / "stm32f405zg.yaml"), "--expect", "144"]) == 0
    out = capsys.readouterr().out
    assert "144 pins" in out and "0 error(s)" in out
    assert pin_table_check.main([str(ROOT / "chips" / "stm32f405zg.yaml"), "--expect", "100"]) == 1


def _broken_chip(tmp_path) -> Path:
    data = yaml.safe_load((ROOT / "chips" / "stm32f405zg.yaml").read_text())
    pins = data["pins"]
    by = {p["name"]: p for p in pins}
    by["PA10"]["bit"] = 9                         # PA10 now claims PA9's port/bit -> name mismatch + duplicate GPIO
    by["PB7"]["functions"]["AF1"] = "PB6"        # function naming another pin's GPIO
    pins[5]["kind"] = "supply"                    # VBAT: unknown kind
    del by["PC0"]["port"]                         # GPIO without port
    pins.append(dict(pins[0]))                    # duplicate pin number 1 (and pin count 145)
    data["regs_file"] = str(ROOT / "chips" / "stm32f405_regs.yaml")
    p = tmp_path / "broken.yaml"
    p.write_text(yaml.safe_dump(data, sort_keys=False))
    return p


def test_pin_table_check_catches_a_broken_table(tmp_path):
    rep = pin_table_check.check_file(_broken_chip(tmp_path))
    text = "\n".join(rep.errors)
    assert not rep.ok
    assert "145 pins, expected 144" in text
    assert "duplicate pin numbers: [1]" in text
    assert "'PA10': GPIO name 'PA9'" in text and "GPIO PA9 on several pins" in text
    assert "names GPIO PB6" in text
    assert "kind 'supply'" in text
    assert "'PC0': gpio pin without port/bit" in text
    assert pin_table_check.main([str(tmp_path / "broken.yaml")]) == 1


def test_pin_table_check_warnings_and_wrong_format(tmp_path):
    data = yaml.safe_load((ROOT / "chips" / "stm32f405zg.yaml").read_text())
    data["gpio_name_format"] = "PT{port}{bit}"    # Kinetis format on an STM32 table: every GPIO name mismatches
    data["pins"][24]["kind"] = "power"            # NRST as power -> warning
    data["pins"][0]["functions"]["XYZ"] = "FOO"  # unknown mux key -> warning
    p = tmp_path / "fmt.yaml"
    p.write_text(yaml.safe_dump(data, sort_keys=False))
    rep = pin_table_check.check_file(p)
    assert sum("is not part of the pin name" in e for e in rep.errors) == 114
    assert any("NRST" in w and "supply" in w for w in rep.warnings)
    assert any("'XYZ' is not a stm32 mux key" in w for w in rep.warnings)
