"""chips/tms320f2808.yaml: memory map, 100-pin PZ pin table (SPRS230P Table 7-1 / Table 9-17 / Figure 7-1), IRQs
(Table 9-28), the generated register table (tools/gen_tms320f2808_regs.py), the boot ROM stand-in and that every
peripheral builds.  Spot values below were copied by eye from the data sheet PDF (page numbers in comments)."""
import sys
from pathlib import Path

import pytest
import yaml

from chipem.chipdesc import load_chip
from chipem.regtable import regs_for

from c28x_fake import make_fake_chip

ROOT = Path(__file__).resolve().parent.parent
CHIP = ROOT / "chips" / "tms320f2808.yaml"
REGS = ROOT / "chips" / "tms320f2808_regs.yaml"
sys.path.insert(0, str(ROOT / "tools"))
import gen_tms320f2808_regs as gen  # noqa: E402


@pytest.fixture(scope="module")
def desc():
    return load_chip(CHIP)


def test_core_and_memory(desc):
    assert desc.name == "TMS320F2808" and desc.package.startswith("LQFP100")
    assert desc.core.arch == "c28x" and desc.core.addr_unit_bits == 16 and desc.core.fpu is False
    assert desc.core.clock_hz == 10e6                         # 20 MHz OSCCLK / 2 after reset (CLKINDIV = 0)
    flash = desc.memory_at(0x3F7FF6)                          # Figure 9-19 / Table 9-19 (p. 109, 113)
    assert flash.name == "FLASH" and flash.base == 0x3E8000 and flash.size == 0x10000
    assert desc.memory_at(0x3E7FFF) is None
    ram = desc.memory_at(0xA000)                              # H0 at 0xA000-0xBFFF; 18K SARAM in total
    assert ram.base == 0x8000 and ram.size == 0x4000 and desc.memory_at(0xC000) is None
    assert sum(m.size for m in desc.memory if m.kind == "ram") == 18 * 1024
    otp = desc.memory_at(0x3D7800)
    assert otp.name == "OTP" and otp.size == 0x400
    rom = desc.memory_at(0x3FFFC0)
    assert rom.name == "BOOTROM" and rom.base == 0x3FF000 and rom.size == 0x1000
    assert desc.extra["boot"]["entry"] == 0x3FFB50           # InitBoot (SPRU722C Table 1)
    assert not [p for p in desc.peripherals if p.name in ("XINTF", "DMA", "MCBSPA", "SCIC")]


def test_pin_table(desc):
    assert len(desc.pins) == 100 and sorted(p.num for p in desc.pins) == list(range(1, 101))
    gp = [p for p in desc.pins if p.kind == "gpio"]
    assert len(gp) == 35 and {p.gpio_name for p in gp} == {f"GPIO{i}" for i in range(35)}
    pin = desc.pin
    # Table 7-1 (pp. 16-19) and Table 9-17 (p. 106)
    assert pin("GPIO28").num == 92 and pin("GPIO28").functions == {"GPIO": "GPIO28", "MUX1": "SCIRXDA", "MUX3": "TZ5"}
    assert pin("GPIO29").num == 4 and pin("GPIO29").name == "GPIO29/SCITXDA/TZ6"
    assert pin("GPIO34").num == 43 and pin("GPIO34").port == "B" and pin("GPIO34").bit == 2
    assert pin("GPIO34").functions == {"GPIO": "GPIO34"}
    assert pin("GPIO12").num == 1 and pin("GPIO12").name == "GPIO12/TZ1/CANTXB/SPISIMOB"
    assert pin("GPIO32").num == 100 and pin("GPIO32").functions["MUX1"] == "SDAA"
    assert pin("GPIO32").functions["MUX3"] == "ADCSOCAO" and pin("GPIO33").num == 5
    assert pin("GPIO18").num == 54 and pin("GPIO18").functions == {"GPIO": "GPIO18", "MUX1": "SPICLKA",
                                                                   "MUX2": "SCITXDB"}
    assert pin("GPIO1").num == 44 and pin("GPIO1").functions["MUX2"] == "SPISIMOD"
    assert pin("GPIO0").num == 47 and pin("GPIO6").num == 56 and pin("GPIO6").functions["MUX3"] == "EPWMSYNCO"
    assert pin("GPIO8").functions["MUX2"] == "CANTXB" and pin("GPIO10").functions["MUX2"] == "CANRXB"
    assert pin("GPIO27").num == 79 and pin("GPIO27").functions["MUX3"] == "SPISTEB"
    assert pin("GPIO31").num == 7 and pin("GPIO31").name == "GPIO31/CANTXA"
    # special / analog pins (Table 7-1, pp. 14-15)
    assert pin("XRS").num == 78 and pin("XRS").kind == "special"
    assert pin(88).name == "X1" and pin(86).name == "X2" and pin(90).name == "XCLKIN" and pin(66).name == "XCLKOUT"
    assert pin(84).name == "TRST" and pin(75).name == "TCK" and pin(97).name == "TEST1"
    assert pin("ADCINA0").num == 23 and pin("ADCINA0").kind == "analog" and pin("ADCINB7").num == 34
    assert pin(24).name == "ADCLO" and pin(38).name == "ADCRESEXT"
    power = [p for p in desc.pins if p.kind == "power"]       # p. 16: 6 VDD, 4 VDDIO, 11 VSS + analog/flash supplies
    assert len(power) == 30 and sum(p.name == "VDD" for p in power) == 6
    assert sum(p.name == "VDDIO" for p in power) == 4 and sum(p.name == "VSS" for p in power) == 11
    assert pin(96).name == "VDD3VFL" and pin(12).name == "VDD1A18" and pin(13).name == "VSS1AGND"


def test_pin_table_check_clean():
    import pin_table_check
    rep = pin_table_check.check_file(CHIP, 100)
    assert not rep.errors and not rep.warnings


def test_irqs(desc):
    n = desc.irqs                                             # Table 9-28 (p. 121)
    assert n[38] == "TINT0" and n[35] == "XINT1" and n[36] == "XINT2" and n[32] == "SEQ1INT"
    assert n[72] == "SPIRXINTA" and n[74] == "SPIRXINTB" and n[76] == "SPIRXINTC" and n[79] == "SPITXINTD"
    assert n[96] == "SCIRXINTA" and n[99] == "SCITXINTB" and n[102] == "ECAN0INTB" and n[89] == "I2CINT2A"
    assert not [v for v in n if 80 <= v <= 87 or v >= 120]   # no DMA (group 7), no XINT3-7 (group 12)


def test_register_table_addresses():
    """Addresses from SPRS230P Tables 9-2..9-31 (pp. 76-125) and PARTID (Table 9-27, p. 118)."""
    def addr(block, reg):
        pr = regs_for(block, REGS)
        return pr.base + pr.reg(reg).offset
    assert addr("SYSCTRL", "XCLK") == 0x7010 and addr("SYSCTRL", "PLLSTS") == 0x7011
    assert addr("SYSCTRL", "PCLKCR1") == 0x701D and addr("SYSCTRL", "PLLCR") == 0x7021
    assert addr("SYSCTRL", "WDCR") == 0x7029 and "PCLKCR3" not in regs_for("SYSCTRL", REGS)
    sc = regs_for("SYSCTRL", REGS)
    assert sc.reg("PLLSTS").has_field("CLKINDIV") and not sc.reg("PLLSTS").has_field("DIVSEL")
    assert sc.reg("PCLKCR0").field("SPIBENCLK").lsb == 9 and sc.reg("PCLKCR0").field("SPICENCLK").lsb == 6
    assert addr("SPIB", "SPICCR") == 0x7740 and addr("SPIC", "SPITXBUF") == 0x7768 and addr("SPID", "SPIPRI") == 0x778F
    assert addr("SCIB", "SCIFFCT") == 0x775C and addr("I2CA", "I2CFFRX") == 0x7921
    assert "I2CEMDR" not in regs_for("I2CA", REGS)
    assert addr("GPIO", "GPBPUD") == 0x6F9C and addr("GPIO", "GPIOLPMSEL") == 0x6FE8
    assert "GPCMUX1" not in regs_for("GPIO", REGS) and "GPIOXINT3SEL" not in regs_for("GPIO", REGS)
    assert regs_for("GPIO", REGS).reg("GPAPUD").reset == 0x0FFF          # GPIO0-11 pull-ups off at reset
    assert addr("XINT", "XNMICTR") == 0x707F and "XINT3CR" not in regs_for("XINT", REGS)
    assert addr("ADC", "ADCOFFTRIM") == 0x711D and addr("ADCMIRROR", "ADCRESULT15") == 0x0B0F
    assert addr("ECANB", "CANTOS") == 0x6232 and addr("ECANB", "MBOX0_MSGID") == 0x6300
    assert addr("EPWM4", "HRCNFG") == 0x68E0 and addr("EQEP2", "QCPRDLAT") == 0x6B60
    assert addr("ECAP4", "ECFRC") == 0x6A79 and addr("CPUTIMER2", "TPRH") == 0x0C17
    assert addr("PIE", "PIEIFR12") == 0x0CF9
    dev = regs_for("DEV_EMU", REGS)
    assert dev.base + dev.reg("PARTID").offset == 0x0882 and dev.reg("PARTID").reset == 0x003C


def test_register_table_is_generated():
    """chips/tms320f2808_regs.yaml is exactly what the generator writes (no hand edits)."""
    doc = gen.build(yaml.safe_load((ROOT / "chips" / "tms320f28232_regs.yaml").read_text()))
    assert yaml.safe_load(REGS.read_text()) == doc
    assert REGS.read_text() == gen.dump(doc)


def test_bootrom_words_match_source(desc):
    """`boot.rom_words` in the chip YAML are the assembled chips/tms320f2808_bootrom.asm."""
    want = gen.bootrom_words()
    got = {int(k): list(v) for k, v in desc.extra["boot"]["rom_words"].items()}
    assert got == want
    assert want[0x3FFFC0][:2] == [0xFB50, 0x003F]                         # RESET -> InitBoot


def test_chip_builds(desc):
    chip = make_fake_chip(desc)
    chip.reset()
    for p in desc.peripherals:
        blk = chip.peripherals[p.name]
        assert blk.model == p.model, p.name
        assert blk.unit_bytes == 2
    assert not [e for e in chip.log if e.kind == "warn"]
    assert chip.core.pc == 0x3FFB50 and chip.clock.hz == 10e6              # boot ROM stand-in, OSCCLK / 2
    assert chip.bus.read(0x0882, 1) == 0x003C                             # PARTID: F2808
    assert chip.bus.read(0x3FFFC0, 2) == 0x3FFB50 and chip.bus.read(0x3FFFE6, 2) != 0xFFFFFFFF
    chip.bus.write(0xA123, 1, 0xBEEF)
    assert chip.bus.read(0x3FA123, 1) == 0xBEEF                           # L0/L1/H0 dual mapping
