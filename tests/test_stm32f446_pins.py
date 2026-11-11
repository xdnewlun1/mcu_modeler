"""STM32F446xx chip descriptions (chips/stm32f446re.yaml LQFP64 base, stm32f446ve LQFP100, stm32f446ze LQFP144,
stm32f446me WLCSP81): counts from ST's open pin data (IONb), pin_table_check, svd2chip --check against
STM32F446.svd, spot checks, and a second-source check of every alternate function against the STM32F446xx
GPIO_AFn_<periph> defines of the STM32CubeF4 HAL (stm32f4xx_hal_gpio_ex.h). The data sheet DS10693 is not in the repo."""
from __future__ import annotations

import functools
import io
import re
import sys
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip

ROOT = Path(__file__).resolve().parent.parent
CHIPS = ROOT / "chips"
SVD = ROOT / "svd" / "dl-stm32f446" / "STM32F446.svd"
GPIO_EX = ROOT / "firmware" / "vendor" / "STM32CubeF4" / "Drivers" / "STM32F4xx_HAL_Driver" / "Inc" / \
    "stm32f4xx_hal_gpio_ex.h"

# file, name, package, pin/ball count, GPIO count (open pin data IONb)
PARTS = [
    ("stm32f446re", "STM32F446RE", "LQFP64", 64, 50),
    ("stm32f446ve", "STM32F446VE", "LQFP100", 100, 81),
    ("stm32f446ze", "STM32F446ZE", "LQFP144", 144, 114),
    ("stm32f446me", "STM32F446ME", "WLCSP81", 81, 63),
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


def gpio(fn: str, g: str):
    return next(p for p in desc_of(fn).pins if p.gpio_name == g)


@pytest.mark.parametrize("fn,name,package,npins,ngpio", PARTS, ids=IDS)
def test_description(fn, name, package, npins, ngpio):
    d = desc_of(fn)
    assert (d.name, d.package, d.family) == (name, package, "stm32")
    assert len(d.pins) == npins and sorted(p.num for p in d.pins) == list(range(1, npins + 1))
    assert len({p.gpio_name for p in d.pins if p.kind == "gpio"}) == ngpio
    mem = {m.name: (m.base, m.size) for m in d.memory}
    assert mem == {"FLASH": (0x08000000, 0x80000), "SRAM": (0x20000000, 0x20000), "BKPSRAM": (0x40024000, 0x1000)}
    assert d.core.clock_hz == 16e6 and d.core.fpu and d.core.num_irqs == 97
    assert {d.irqs[n] for n in (87, 91, 92, 93, 94, 95, 96)} == {"SAI1", "SAI2", "QUADSPI", "CEC", "SPDIF_RX",
                                                                "FMPI2C1_EV", "FMPI2C1_ER"}
    assert 61 not in d.irqs and 79 not in d.irqs and 88 not in d.irqs      # no ETH, CRYP, LTDC on the F446


@pytest.mark.parametrize("fn,name,package,npins,ngpio", PARTS, ids=IDS)
def test_pin_table_check(fn, name, package, npins, ngpio):
    rep = tool("pin_table_check").check_file(CHIPS / f"{fn}.yaml", expect=npins)
    assert rep.ok and not rep.warnings, rep.text()


@pytest.mark.parametrize("fn", IDS)
def test_svd_check(fn):
    out = io.StringIO()
    assert tool("svd2chip").check(SVD, CHIPS / f"{fn}.yaml", out=out) == 0, out.getvalue()


@pytest.mark.parametrize("fn", IDS)
def test_chip_builds_without_warnings(fn):
    chip = Chip(desc_of(fn))
    assert not [e for e in chip.log if e.kind == "warn"]
    models = {n: b.model for n, b in chip.peripherals.items()}
    assert models["QUADSPI"] == "stm32.quadspi" and models["RCC"] == "stm32.rcc_f446"
    assert models["SPDIFRX"] == "stm32.spdifrx" and models["FMPI2C1"] == "stm32.fmpi2c" and models["CEC"] == "stm32.cec"
    assert "stub" not in models.values()


def test_shared_gpios_agree_between_packages():
    """One die: a GPIO has the same name, default and AF numbers on every package.  ST's open pin data leaves out the
    signals of blocks a package does not bond out (FMC on the LQFP64, ...), so a smaller package's functions are a
    subset of the LQFP144 ones."""
    ref = {p.gpio_name: p for p in desc_of("stm32f446ze").pins if p.kind == "gpio"}
    assert len(ref) == 114
    for fn in IDS:
        for p in desc_of(fn).pins:
            if p.kind != "gpio":
                continue
            r = ref.get(p.gpio_name)
            if r is None:                                  # WLCSP81 GPIOs not on the LQFP144: none expected
                pytest.fail(f"{fn} {p.gpio_name} not on the LQFP144")
            assert (p.name, p.default) == (r.name, r.default), (fn, p.gpio_name)
            for k, v in p.functions.items():
                assert set(v.split("/")) <= set(r.functions.get(k, "").split("/")), (fn, p.gpio_name, k, v)
    assert gpio("stm32f446re", "PC0").functions.get("AF12") is None          # no FMC on the LQFP64
    assert gpio("stm32f446ze", "PC0").functions["AF12"] == "FMC_SDNWE"


def test_spot_checks():
    # NUCLEO-F446RE: USART2 VCP on PA2/PA3 (AF7), LD2 on PA5; QUADSPI CLK PB2 AF9, BK1_NCS PB6 AF10
    assert gpio("stm32f446re", "PA2").functions["AF7"] == "USART2_TX" and gpio("stm32f446re", "PA2").num == 16
    assert gpio("stm32f446re", "PB2").functions["AF9"] == "QUADSPI_CLK"
    assert gpio("stm32f446re", "PB6").functions["AF10"] == "QUADSPI_BK1_NCS"
    assert gpio("stm32f446re", "PC9").functions["AF9"] == "QUADSPI_BK1_IO0"
    assert gpio("stm32f446re", "PA15").default == "JTDI" and gpio("stm32f446re", "PA15").functions["AF4"] == "CEC"
    assert gpio("stm32f446re", "PC6").functions["AF4"] == "FMPI2C1_SCL"
    assert gpio("stm32f446re", "PB7").functions["AF8"] == "SPDIFRX_IN0"
    assert gpio("stm32f446re", "PA4").functions["analog"] == "DAC_OUT1"
    assert gpio("stm32f446re", "PA0").functions["ADC"] == "ADC123_IN0"
    nums = {p.name: p.num for p in desc_of("stm32f446re").pins if p.kind != "gpio"}
    assert nums["NRST"] == 7 and nums["BOOT0"] == 60 and nums["VCAP_1"] == 30
    # the LQFP64 package has no QUADSPI_BK1_IO2 (quad mode needs the 100/144-pin or WLCSP packages)
    fns = [v for p in desc_of("stm32f446re").pins for v in p.functions.values()]
    assert not any("QUADSPI_BK1_IO2" in v for v in fns)
    assert gpio("stm32f446ze", "PF7").functions["AF9"] == "QUADSPI_BK1_IO2"
    assert gpio("stm32f446ze", "PF8").functions["AF10"] == "QUADSPI_BK1_IO0"
    assert gpio("stm32f446ze", "PD11").functions["AF12"] == "FMC_A16/FMC_CLE"
    import yaml
    raw = yaml.safe_load((CHIPS / "stm32f446me.yaml").read_text())["pins"]
    me = {r["ball"]: r["num"] for r in raw}
    assert me["A1"] == 1 and me["J9"] == 81 and len(me) == 81


def _hal_af_tokens() -> set[tuple[int, str]]:
    text = GPIO_EX.read_text()
    start = text.index("#if defined(STM32F446xx)\n")
    end = text.index("#endif /* STM32F446xx */", start)
    return {(int(a), b) for a, b in re.findall(r"GPIO_AF(\d+)_(\w+)\s", text[start:end])}


def _token(sig: str) -> str | None:
    rules = [(r"QUADSPI_.*", "QSPI"), (r"OTG_HS_(DM|DP|ID|SOF)", "OTG_HS_FS"), (r"OTG_HS_.*", "OTG_HS"),
             (r"OTG_FS_.*", "OTG_FS"), (r"I2S(\d)_.*", r"SPI\1"), (r"MCO\d", "MCO"),
             (r"JT.*|NJTRST|JTDO|TRACESWO", "SWJ"), (r"TRACE.*", "TRACE"), (r"RTC_REFIN", "RTC_50Hz"),
             (r"SAI(\d)_.*", r"SAI\1"), (r"SPDIFRX_.*", "SPDIFRX"), (r"FMPI2C1_.*", "FMPI2C1"),
             (r"(TIM\d+|USART\d|UART\d|CAN\d|I2C\d|SPI\d)_.*", r"\1"), (r"(FMC|SDIO|DCMI)_.*", r"\1"),
             (r"CEC", "CEC"), (r"EVENTOUT", "EVENTOUT")]
    for rx, rep in rules:
        if re.fullmatch(rx, sig):
            return re.sub(rx, rep, sig)
    return None


def test_alternate_functions_against_hal_defines():
    hal = _hal_af_tokens()
    missing = set()
    for p in desc_of("stm32f446ze").pins + desc_of("stm32f446me").pins:
        for k, v in p.functions.items():
            if not k.startswith("AF"):
                continue
            for sig in v.split("/"):
                tok = _token(sig)
                if tok is None or sig == "I2S_CKIN":     # I2S_CKIN (AF5) has no own HAL token
                    continue
                if (int(k[2:]), tok) not in hal:
                    missing.add((p.gpio_name, k, sig))
    assert not missing, sorted(missing)
