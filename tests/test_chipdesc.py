import re
from pathlib import Path

import pytest

from chipem.chipdesc import load_chip

ROOT = Path(__file__).resolve().parent.parent
CHIPS = ROOT / "chips"


@pytest.fixture(scope="module", params=["mk22fn1m0vll12", "mk22fx512vll12"])
def desc(request):
    return load_chip(CHIPS / f"{request.param}.yaml")


def test_core_and_package(desc):
    assert desc.package == "LQFP100"
    assert desc.family == "kinetis"
    assert desc.core.cpu == "cortex-m4" and desc.core.fpu and desc.core.clock_hz == 120e6
    assert desc.core.num_irqs == 96 and desc.core.addr_unit_bits == 8


def test_pin_count_and_numbers(desc):
    assert len(desc.pins) == 100
    assert sorted(p.num for p in desc.pins) == list(range(1, 101))


def test_gpio_pins_have_port_bit_and_alt1(desc):
    n = 0
    for p in desc.pins:
        m = re.match(r"PT([A-E])(\d+)", p.name)
        if not m:
            assert p.port is None and p.bit is None
            assert p.kind in ("analog", "power", "special")
            continue
        n += 1
        assert p.kind == "gpio"
        assert p.port == m.group(1) and p.bit == int(m.group(2))
        assert p.functions, p.name
        assert p.functions["ALT1"] == p.name                      # e.g. "PTC5/LLWU_P9"
        assert p.functions["ALT1"].split("/")[0] == p.gpio_name   # i.e. its PTxn name
        assert p.default
    assert n == 66
    assert len({p.gpio_name for p in desc.pins if p.gpio_name}) == n


def test_pin_spot_checks(desc):
    p = desc.pin(77)
    assert p.name == "PTC5/LLWU_P9" and p.functions["ALT2"] == "SPI0_SCK" and p.functions["ALT7"] == "FTM0_CH2"
    assert p.functions["ALT3"] == "LPTMR0_ALT2" and p.default == "DISABLED"
    p = desc.pin(34)
    assert p.name == "PTA0" and p.functions["ALT7"] == "JTAG_TCLK/SWD_CLK" and p.functions["EzPort"] == "EZP_CLK"
    p = desc.pin(1)
    assert p.name == "PTE0" and p.functions["ALT5"] == "TRACE_CLKOUT" and p.functions["ALT7"] == "RTC_CLKOUT"
    assert desc.pin("PTA1").functions["ALT2"] == "UART0_RX"
    assert desc.pin("PTA2").functions["ALT2"] == "UART0_TX"
    assert desc.pin(42).functions["ALT7"] == "FTM1_QD_PHA"
    assert desc.pin(3).functions["ALT3"] == "UART1_CTS_b"
    assert desc.pin(90).functions["ALT5"] == "FB_CS5_b/FB_TSIZ1/FB_BE23_16_BLS15_8_b"
    assert desc.pin(100).functions == {"ALT1": "PTD7", "ALT2": "CMT_IRO", "ALT3": "UART0_TX", "ALT4": "FTM0_CH7",
                                       "ALT6": "FTM0_FLT1"}
    assert desc.pin(8).kind == "power" and desc.pin(10).kind == "special" and desc.pin(52).kind == "special"
    assert desc.pin(14).kind == "analog" and desc.pin(29).kind == "special" and desc.pin(30).kind == "power"


def test_pin_lookup_aliases(desc):
    assert desc.pin("PTC5").num == 77
    assert desc.pin("LLWU_P9").num == 77
    assert desc.pin("ptc5/llwu_p9").num == 77


def test_irqs(desc):
    assert desc.irqs[0] == "DMA0" and desc.irqs[22] == "WDOG_EWM" and desc.irqs[31] == "UART0_RX_TX"
    assert desc.irqs[48] == "PIT0" and desc.irqs[58] == "LPTMR0"
    assert [desc.irqs[i] for i in range(59, 64)] == ["PORTA", "PORTB", "PORTC", "PORTD", "PORTE"]
    assert desc.irqs[81] == "SDHC" and len(desc.irqs) == 82


def test_peripherals(desc):
    names = {p.name for p in desc.peripherals}
    for n in ("SIM", "WDOG", "MCG", "OSC", "PIT", "LPTMR0", "RTC", "VBAT", "FTFE", "ADC0", "ADC1",
              "UART0", "UART4", "PORTA", "PORTE", "GPIOA", "GPIOE", "MCM", "DMA", "SPI2", "DAC0"):
        assert n in names
    # not on the 100-pin K22 (K22P100M120SF5V2RM Table 3-5 / Table 4-3): listed under omitted:, IRQs reserved
    assert "UART5" not in names and "DAC1" not in names
    assert {o["name"] for o in desc.extra["omitted"]} == {"UART5", "DAC1"}
    assert [desc.irqs[n] for n in (68, 69, 72)] == ["Reserved84", "Reserved85", "Reserved88"]
    assert desc.peripheral("SIM").size == 0x2000
    assert desc.peripheral("PORTC").base == 0x4004B000 and desc.peripheral("PORTC").params == {"port": "C"}
    assert desc.peripheral("GPIOC").base == 0x400FF080 and desc.peripheral("GPIOC").size == 0x40
    assert desc.peripheral("UART2").params == {"index": 2}
    assert desc.peripheral_at(0x4004B014).name == "PORTC"
    assert "SCS" not in names and desc.peripheral_at(0xE000E010) is None
    # no overlaps
    ps = sorted(desc.peripherals, key=lambda p: p.base)
    for a, b in zip(ps, ps[1:]):
        assert a.base + a.size <= b.base, (a.name, b.name)
    for p in desc.peripherals:
        assert p.model in ("stub", "ram") or p.model.startswith("kinetis.")


def test_memory_maps():
    fn = load_chip(CHIPS / "mk22fn1m0vll12.yaml")
    fx = load_chip(CHIPS / "mk22fx512vll12.yaml")
    assert fn.name == "MK22FN1M0VLL12" and fx.name == "MK22FX512VLL12"
    assert [(m.name, m.base, m.size, m.kind) for m in fn.memory] == [
        ("FLASH", 0, 0x100000, "flash"), ("SRAM_L", 0x1FFF0000, 0x10000, "ram"), ("SRAM_U", 0x20000000, 0x10000, "ram")]
    assert fx.memory_at(0x10000000).kind == "flexnvm" and fx.memory_at(0x14000000).kind == "flexram"
    assert fx.memory_at(0x80000) is None and fx.memory_at(0x7FFFF).name == "FLASH"
    assert fx.pins == fn.pins and fx.irqs == fn.irqs


@pytest.mark.parametrize("part", ["mk22fn1m0vll12", "mk22fx512vll12"])
def test_k22_systick_has_no_external_reference(part):
    """K22 RM section 3.2.1.2: SysTick always runs from FCLK (NOREF=1), modelled as systick_ext_div: 0."""
    assert load_chip(CHIPS / f"{part}.yaml").extra.get("systick_ext_div") == 0
