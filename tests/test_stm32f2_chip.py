"""STM32F205/F207/F215/F217 chip descriptions (chips/stm32f2*.yaml, base chips/stm32f207zg.yaml): pin tables from
DS15818 Rev 15 (STM32F205xx/207xx) / DS17050 Rev 9 (STM32F215xx/217xx), register table chips/stm32f2xx_regs.yaml
(STM32F21x.svd), core settings, the shared-pin agreement between package files, and spot checks read by eye from the
data sheet tables."""
from __future__ import annotations

import functools
import io
import sys
from pathlib import Path

import pytest
import yaml

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.regtable import load_regs

ROOT = Path(__file__).resolve().parent.parent
CHIPS = ROOT / "chips"
SVD = ROOT / "svd" / "dl-stm32f2"

# file, name, package, pins, GPIOs (DS15818 Table 2/3), Ethernet+DCMI (F207/F217), CRYP/HASH (F215/F217)
PARTS = [
    ("stm32f205rg", "STM32F205RG", "LQFP64", 64, 51, False, False),
    ("stm32f205rgy", "STM32F205RGY", "WLCSP64+2", 66, 51, False, False),
    ("stm32f215rg", "STM32F215RG", "LQFP64", 64, 51, False, True),
    ("stm32f205vg", "STM32F205VG", "LQFP100", 100, 82, False, False),
    ("stm32f207vg", "STM32F207VG", "LQFP100", 100, 82, True, False),
    ("stm32f215vg", "STM32F215VG", "LQFP100", 100, 82, False, True),
    ("stm32f217vg", "STM32F217VG", "LQFP100", 100, 82, True, True),
    ("stm32f205zg", "STM32F205ZG", "LQFP144", 144, 114, False, False),
    ("stm32f207zg", "STM32F207ZG", "LQFP144", 144, 114, True, False),
    ("stm32f215zg", "STM32F215ZG", "LQFP144", 144, 114, False, True),
    ("stm32f217zg", "STM32F217ZG", "LQFP144", 144, 114, True, True),
    ("stm32f207ig", "STM32F207IG", "LQFP176", 176, 140, True, False),
    ("stm32f217ig", "STM32F217IG", "LQFP176", 176, 140, True, True),
    ("stm32f207igh", "STM32F207IGH", "UFBGA176+25", 201, 140, True, False),
    ("stm32f217igh", "STM32F217IGH", "UFBGA176+25", 201, 140, True, True),
]
IDS = [p[0] for p in PARTS]


def tool(name: str):
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        return __import__(name)
    finally:
        sys.path.pop(0)


@functools.lru_cache(maxsize=None)
def desc_of(fn: str):
    return load_chip(CHIPS / f"{fn}.yaml")


@functools.lru_cache(maxsize=None)
def _balls(fn: str) -> dict[str, int]:
    return {p["ball"]: p["num"] for p in yaml.safe_load((CHIPS / f"{fn}.yaml").read_text())["pins"] if "ball" in p}


def gpio_pins(d) -> dict:
    return {p.gpio_name: p for p in d.pins if p.kind == "gpio"}


# --------------------------------------------------------------------------------------------- descriptions
@pytest.mark.parametrize("fn,name,package,npins,ngpio,eth,crypto", PARTS, ids=IDS)
def test_description(fn, name, package, npins, ngpio, eth, crypto):
    d = desc_of(fn)
    assert (d.name, d.package, d.family) == (name, package, "stm32")
    assert len(d.pins) == npins and sorted(p.num for p in d.pins) == list(range(1, npins + 1))
    assert len(gpio_pins(d)) == ngpio
    c = d.core
    assert (c.arch, c.cpu, c.fpu, c.clock_hz, c.num_irqs) == ("armv7-m", "cortex-m3", False, 16_000_000, 81)
    assert d.extra["vtor"] == 0x08000000 and d.extra["systick_ext_div"] == 8
    mem = {m.name: (m.base, m.size) for m in d.memory}
    assert mem == {"FLASH": (0x08000000, 0x100000), "SRAM": (0x20000000, 0x20000), "BKPSRAM": (0x40024000, 0x1000)}
    base = desc_of("stm32f207zg")
    assert d.irqs == base.irqs and [(p.name, p.base) for p in d.peripherals] == [(p.name, p.base)
                                                                                for p in base.peripherals]
    chip = Chip(d)
    assert not [e for e in chip.log if e.kind == "warn"]
    present = {n: not getattr(chip.peripherals[n], "absent", False) for n in ("Ethernet_MAC", "DCMI", "CRYP", "HASH")}
    assert present == {"Ethernet_MAC": eth, "DCMI": eth, "CRYP": crypto, "HASH": crypto}


def test_irq_table_rm0033():
    d = desc_of("stm32f207zg")
    assert len(d.irqs) == 81 and max(d.irqs) == 80
    assert (d.irqs[4], d.irqs[61], d.irqs[79], d.irqs[80]) == ("FLASH", "ETH", "CRYP", "HASH_RNG")
    assert 81 not in d.irqs                                    # no FPU vector on the Cortex-M3


def test_no_i2s_ext_and_no_ccm():
    names = {p.name for p in desc_of("stm32f207zg").peripherals}
    assert not names & {"I2S2ext", "I2S3ext", "CCMRAM"} and "ADC_Common" in names


def test_stm32_family_imports_cleanly():
    import chipem.periph.stm32 as fam
    assert fam.IMPORT_ERRORS == {}


@pytest.mark.parametrize("fn,name,package,npins,ngpio,eth,crypto", PARTS, ids=IDS)
def test_pin_table_check(fn, name, package, npins, ngpio, eth, crypto):
    rep = tool("pin_table_check").check_file(CHIPS / f"{fn}.yaml", expect=npins)
    assert rep.ok and not rep.warnings, rep.text()


@pytest.mark.parametrize("fn,svd", [("stm32f207zg", "STM32F21x.svd"), ("stm32f217igh", "STM32F21x.svd"),
                                    ("stm32f205rg", "STM32F20x.svd"), ("stm32f205rgy", "STM32F20x.svd")])
def test_svd_check(fn, svd):
    if not (SVD / svd).exists():
        pytest.skip(f"{svd} not downloaded")
    out = io.StringIO()
    assert tool("svd2chip").check(str(SVD / svd), str(CHIPS / f"{fn}.yaml"), out=out) == 0, out.getvalue()


def test_register_table_is_the_f21x_svd():
    rt = load_regs(CHIPS / "stm32f2xx_regs.yaml")
    assert rt.device == "STM32F21x"
    rtc = rt.peripheral("RTC")
    assert "SSR" not in rtc and "SHIFTR" not in rtc and "CALR" not in rtc and "ALRMASSR" not in rtc
    assert rtc["PRER"].field("PREDIV_S").width == 13
    assert not rt.peripheral("PWR")["CSR"].has_field("VOSRDY")
    assert rt.peripheral("RCC")["PLLI2SCFGR"].reset == 0x20003000
    assert rt.peripheral("RCC")["AHB1ENR"].reset == 0                     # no CCMDATARAMEN
    assert "HASH_HR0" not in rt.peripheral("HASH") and "CSR51" not in rt.peripheral("HASH")
    assert "CSGCM0R" not in rt.peripheral("CRYP")
    for n in ("I2S2ext", "I2S3ext", "UART7", "SPI4", "SAI1", "LTDC", "GPIOJ", "FPU"):
        assert n not in rt, n


# -------------------------------------------------------------------- shared pins agree across package files
def test_shared_pins_agree_across_packages():
    """A GPIO has the same name, default and functions in every package file of the same feature set (F205/F215:
    no ETH/DCMI; F207/F217: with), and the F205/F215 tables are the F207/F217 ones without AF11 ETH_* / AF13 DCMI_*.
    (DS15818 Table 10 has one row per GPIO for all packages.)"""
    ref_eth = {}
    for fn, _n, _pk, _np, _ng, eth, _c in PARTS:
        if eth:
            for g, p in gpio_pins(desc_of(fn)).items():
                ref_eth.setdefault(g, (fn, p))
    assert len(ref_eth) == 140
    for fn, _n, _pk, _np, _ng, eth, _c in PARTS:
        for g, p in gpio_pins(desc_of(fn)).items():
            rfn, r = ref_eth[g]
            want = r.functions if eth else {k: v for k, v in r.functions.items()
                                            if not v.startswith(("ETH_", "DCMI_"))}
            assert (p.name, p.default, p.functions) == (r.name, r.default, want), (fn, rfn, g)
            if not eth:
                assert not any("ETH_" in v or "DCMI_" in v for v in p.functions.values()), (fn, g)


def test_f217_is_f207_pins_and_f215_is_f205_pins():
    for a, b in (("stm32f207zg", "stm32f217zg"), ("stm32f205zg", "stm32f215zg"), ("stm32f207vg", "stm32f217vg"),
                 ("stm32f205vg", "stm32f215vg"), ("stm32f205rg", "stm32f215rg"), ("stm32f207ig", "stm32f217ig"),
                 ("stm32f207igh", "stm32f217igh")):
        pa = [(p.num, p.name, p.kind, p.functions) for p in desc_of(a).pins]
        pb = [(p.num, p.name, p.kind, p.functions) for p in desc_of(b).pins]
        assert pa == pb, (a, b)


def test_f2_vs_f4_pin_differences():
    """Same package positions as the STM32F407 for every GPIO; the AF differences are the F2 spellings of DS15818 and
    the missing I2Sxext_SD (no full-duplex I2S extension on the F2)."""
    f4 = {p.gpio_name: p for p in load_chip(CHIPS / "stm32f407ig.yaml").pins if p.kind == "gpio"}
    f2 = gpio_pins(desc_of("stm32f207ig"))
    assert set(f2) == set(f4) and all(f2[g].num == f4[g].num for g in f2)
    ext = sorted(g for g, p in f4.items() if any("ext_SD" in v for v in p.functions.values()))
    assert ext == ["PB14", "PB4", "PC11", "PC2", "PI2"]
    assert not any("ext_SD" in v for p in f2.values() for v in p.functions.values())
    # LQFP176 pin 48 is VSS on the F2 (BYPASS_REG on the F4), pin 171 RFU (PDR_ON on the F4)
    d = desc_of("stm32f207ig")
    assert (d.pin(48).name, d.pin(171).name) == ("VSS", "RFU")


# ------------------------------------------------------------------ spot checks, read by eye from DS15818 Table 8/10
def test_lqfp144_spot_checks():
    d = desc_of("stm32f207zg")
    nums = {1: "PE2", 6: "VBAT", 7: "PC13", 8: "PC14/OSC32_IN", 23: "PH0/OSC_IN", 25: "NRST", 31: "VSSA",
            32: "VREF+", 33: "VDDA", 34: "PA0/WKUP", 40: "PA4", 48: "PB2/BOOT1", 71: "VCAP_1", 74: "PB13", 101: "PA9",
            105: "PA13", 106: "VCAP_2", 128: "PG13", 133: "PB3", 138: "BOOT0", 142: "PE1", 143: "RFU", 144: "VDD"}
    for n, name in nums.items():
        assert d.pin(n).name == name, n
    pa9 = d.pin("PA9")
    assert pa9.functions["AF7"] == "USART1_TX" and pa9.functions["additional"] == "OTG_FS_VBUS"
    assert d.pin("PA10").functions["AF7"] == "USART1_RX"
    assert d.pin("PA0").functions == {"AF1": "TIM2_CH1_ETR", "AF2": "TIM5_CH1", "AF3": "TIM8_ETR", "AF7": "USART2_CTS",
                                      "AF8": "UART4_TX", "AF11": "ETH_MII_CRS", "AF15": "EVENTOUT", "ADC": "ADC123_IN0",
                                      "additional": "WKUP"}
    assert d.pin("PA4").functions["analog"] == "DAC_OUT1" and d.pin("PA4").functions["ADC"] == "ADC12_IN4"
    assert d.pin("PC13").functions == {"AF15": "EVENTOUT", "additional": "RTC_AF1"}
    assert d.pin("PB13").functions["additional"] == "OTG_HS_VBUS" and d.pin("PB13").functions["AF5"] == "SPI2_SCK/I2S2_SCK"
    assert d.pin("PB15").functions["AF0"] == "RTC_50Hz"
    assert d.pin("PC9").functions["AF5"] == "I2S2_CKIN" and d.pin("PC9").functions["AF6"] == "I2S3_CKIN"
    assert d.pin("PA15").functions["AF1"] == "TIM2_CH1/TIM2_ETR" and d.pin("PA15").default == "JTDI"
    assert d.pin("PB3").default == "JTDO/TRACESWO" and d.pin("PA13").default == "JTMS-SWDIO"
    assert d.pin("PA7").functions["AF11"] == "ETH_MII_RX_DV/ETH_RMII_CRS_DV"
    assert d.pin("BOOT0").kind == "special" and d.pin("RFU").kind == "special" and d.pin("VCAP_1").kind == "power"
    # data sheet corrections (DS15818 Table 10)
    assert d.pin("PG13").functions["AF8"] == "USART6_CTS"                 # printed "UART6_CTS"
    assert d.pin("PA6").functions["AF13"] == "DCMI_PIXCLK"                # printed "DCMI_PIXCK"
    assert d.pin("PC3").functions["AF5"] == "SPI2_MOSI/I2S2_SD"           # I2S2_SD missing from Table 10
    assert d.pin("PB4").functions["AF0"] == "NJTRST" and d.pin("PB4").default == "NJTRST"   # printed "JTRST"


def test_lqfp64_spot_checks():
    d = desc_of("stm32f205rg")
    nums = {1: "VBAT", 2: "PC13", 5: "PH0/OSC_IN", 7: "NRST", 12: "VSSA", 13: "VDDA", 14: "PA0/WKUP", 18: "VSS",
            28: "PB2/BOOT1", 31: "VCAP_1", 42: "PA9", 47: "VCAP_2", 55: "PB3", 60: "BOOT0", 63: "VSS", 64: "VDD"}
    for n, name in nums.items():
        assert d.pin(n).name == name, n
    assert {p.port for p in d.pins if p.kind == "gpio"} == {"A", "B", "C", "D", "H"}
    assert "AF11" not in d.pin("PA0").functions                          # F205: no Ethernet
    with pytest.raises(KeyError):
        d.pin("PE2")


def test_wlcsp_spot_checks():
    d = desc_of("stm32f205rgy")
    balls = {"A9": "VBAT", "B8": "PC13", "B9": "PC14/OSC32_IN", "E8": "NRST", "E7": "PA0/WKUP", "H7": "REGOFF",
             "C8": "IRROFF", "D8": "VSS", "B6": "BOOT0", "B4": "PB4", "A2": "PA15"}
    nb = _balls("stm32f205rgy")
    for b, name in balls.items():
        assert d.pin(nb[b]).name == name, b
    assert sorted(nb, key=lambda b: (b[0], int(b[1:])))[0] == "A1" and nb["A1"] == 1
    assert d.pin("REGOFF").kind == "special" and d.pin("IRROFF").kind == "special"


def test_lqfp176_and_ufbga_spot_checks():
    d = desc_of("stm32f207ig")
    assert {7: d.pin(7).name, 50: d.pin(50).name, 120: d.pin(120).name, 172: d.pin(172).name,
            173: d.pin(173).name} == {7: "PI8", 50: "PA4", 120: "PA9", 172: "VDD", 173: "PI4"}
    assert d.pin("PI8").functions == {"AF15": "EVENTOUT", "additional": "RTC_AF2"}
    assert d.pin("PI5").functions["AF13"] == "DCMI_VSYNC"
    h = desc_of("stm32f207igh")
    nb = _balls("stm32f207igh")
    for b, name in {"A2": "PE2", "C1": "VBAT", "D2": "PI8", "N3": "PA0/WKUP", "N4": "PA4", "E15": "PA9",
                    "L4": "REGOFF", "C6": "RFU", "C5": "VDD", "N1": "VREF-", "R15": "PB15", "G8": "VSS"}.items():
        assert h.pin(nb[b]).name == name, b
    assert nb["A1"] == 1 and len(nb) == 201
    assert sum(p.name == "VSS" for p in h.pins) >= 25                   # incl. the 25 thermal balls F6..K10


# ----------------------------------------------------------------------- core: Cortex-M3 without FPU
def test_cortex_m3_cpuid_and_systick_calib():
    from test_stm32f2_system import spin_chip
    chip = spin_chip(desc_of("stm32f207zg"))
    assert chip.bus.read(0xE000ED00, 4) == 0x410FC231                   # CPUID: Cortex-M3 r0p1
    assert chip.bus.read(0xE000E01C, 4) & 0xFFFFFF == 16e6 / 8 / 100 - 1   # TENMS from HCLK/8 (nominal 16 MHz)


def test_fp_instruction_faults_on_cortex_m3():
    from test_stm32f2_system import spin_chip
    chip = spin_chip(desc_of("stm32f207zg"), body="vadd.f32 s0, s0, s1")
    chip.run_for(5e3)
    assert chip.bus.read(0xE000ED2C, 4) & (1 << 30) or chip.bus.read(0xE000ED28, 4) & (1 << 19)
