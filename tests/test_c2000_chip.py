"""chips/tms320f28232.yaml: memory map, 176-pin PGF pin table (SPRS439Q Table 6-1 / 8-18..8-20), IRQs, register
table, and that every peripheral builds on a (fake) C28x core."""
from pathlib import Path

import pytest

from chipem.chipdesc import load_chip
from chipem.regtable import regs_for

from c28x_fake import make_fake_chip

CHIP = Path(__file__).resolve().parent.parent / "chips" / "tms320f28232.yaml"
REGS = CHIP.parent / "tms320f28232_regs.yaml"


@pytest.fixture(scope="module")
def desc():
    return load_chip(CHIP)


def test_core_and_memory(desc):
    assert desc.core.arch == "c28x" and desc.core.addr_unit_bits == 16 and desc.core.fpu is False
    assert desc.gpio_name_format == "GPIO{num}" and desc.family == "ti_c2000"
    flash = desc.memory_at(0x33FFF6)
    assert flash.name == "FLASH" and flash.base == 0x330000 and flash.size == 0x10000     # 64K words, sectors A-D
    assert desc.memory_at(0x0400).name == "M1" and desc.memory_at(0x8000).name == "L0L5"
    assert desc.memory_at(0xDFFF).name == "L0L5" and desc.memory_at(0xE000) is None       # F28232: L0-L5 only
    assert desc.memory_at(0x3FFFC0).name == "BOOTROM" and desc.memory_at(0x380400).name == "OTP"
    assert desc.extra["boot"]["entry"] == 0x33FFF6
    assert desc.extra["auto_stub_ranges"] == [[0x0800, 0x0FFF], [0x5000, 0x7FFF]]


def test_pin_table(desc):
    assert len(desc.pins) == 176
    nums = sorted(p.num for p in desc.pins)
    assert nums == list(range(1, 177))
    gp = [p for p in desc.pins if p.kind == "gpio"]
    assert len(gp) == 88 and {p.gpio_name for p in gp} == {f"GPIO{i}" for i in range(88)}
    p28 = desc.pin("GPIO28")
    assert p28.num == 141 and p28.port == "A" and p28.bit == 28 and p28.functions["MUX1"] == "SCIRXDA"
    p29 = desc.pin("GPIO29")
    assert p29.num == 2 and p29.functions["MUX1"] == "SCITXDA" and p29.functions["MUX2"] == "XA19"
    p34 = desc.pin("GPIO34")
    assert p34.num == 142 and p34.port == "B" and p34.bit == 2 and p34.functions["MUX1"] == "ECAP1"
    p12 = desc.pin("GPIO12")
    assert p12.num == 21 and p12.functions == {"GPIO": "GPIO12", "MUX1": "TZ1", "MUX2": "CANTXB", "MUX3": "MDXB"}
    assert desc.pin("GPIO87").num == 174 and desc.pin("GPIO87").port == "C" and desc.pin("GPIO87").bit == 23
    assert desc.pin("GPIO64").functions["MUX1"] == "GPIO64" and desc.pin("GPIO64").functions["MUX2"] == "XD15"
    a0 = desc.pin("ADCINA0")
    assert a0.num == 42 and a0.kind == "analog"
    assert desc.pin("ADCINB7").num == 53
    xrs = desc.pin("XRS")
    assert xrs.num == 80 and xrs.kind == "special"
    assert desc.pin(87).name == "TCK" and desc.pin(149).name == "XRD" and desc.pin(105).name == "XCLKIN"
    power = [p for p in desc.pins if p.kind == "power"]
    assert len(power) == 52 and sum(p.name == "VSS" for p in power) == 22 and sum(p.name == "VDD" for p in power) == 13


def test_irqs(desc):
    names = desc.irqs
    assert names[38] == "TINT0" and names[35] == "XINT1" and names[96] == "SCIRXINTA" and names[97] == "SCITXINTA"
    assert names[13] == "INT13" and names[14] == "INT14"
    assert "SCIRXINTC" not in names.values()                 # no SCI-C on the F28232


def test_register_table():
    sci = regs_for("SCIA", REGS)
    assert sci.base == 0x7050 and sci.reg("SCICTL2").reset == 0x00C0 and sci.reg("SCIFFTX").reset == 0xA000
    assert regs_for("SCIB", REGS).base == 0x7750
    gpio = regs_for("GPIO", REGS)
    assert gpio.reg("GPBTOGGLE").offset == 0x4E and gpio.reg("GPBMUX1").offset == 0x16
    assert gpio.reg("GPAPUD").reset == 0x00000FFF and gpio.reg("GPBDIR").width == 2
    sc = regs_for("SYSCTRL", REGS)
    assert sc.base + sc.reg("PLLCR").offset == 0x7021 and sc.base + sc.reg("WDCR").offset == 0x7029
    pie = regs_for("PIE", REGS)
    assert pie.base + pie.reg("PIEIFR12").offset == 0x0CF9
    t1 = regs_for("CPUTIMER1", REGS)
    assert t1.base == 0x0C08 and t1.reg("TCR").offset == 4


def test_chip_builds(desc):
    chip = make_fake_chip(desc)
    chip.reset()
    for p in desc.peripherals:
        blk = chip.peripherals[p.name]
        assert blk.model == p.model, p.name
        assert blk.unit_bytes == 2
    assert not [e for e in chip.log if e.kind == "warn"]
    assert chip.core.pc == 0x33FFF6 and chip.clock.hz == 10e6          # 20 MHz OSCCLK / 2 (boot ROM DIVSEL)
    assert chip.bus.read(0x380090, 1) == 0x00E6                        # PARTID seeded
    chip.bus.write(0x8123, 1, 0xBEEF)
    assert chip.bus.read(0x3F8123, 1) == 0xBEEF                        # L0-L3 dual mapping
