"""TMS320F2802x / F2806x (Piccolo) chip descriptions: pin tables, register tables (generator reproducibility and the
data sheet address check), boot ROM stand-in words, memory map, IRQs and the part variants."""
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CHIPS = ROOT / "chips"
sys.path.insert(0, str(ROOT / "tools"))

F2802X = ["tms320f28027", "tms320f28027da", "tms320f28026", "tms320f28023", "tms320f28022", "tms320f28021",
          "tms320f28020", "tms320f280200"]
F2806X = ["tms320f28069", "tms320f28069pn", "tms320f28069m", "tms320f28067", "tms320f28066", "tms320f28065",
          "tms320f28064", "tms320f28063", "tms320f28062"]


def load(name):
    from chipem.chipdesc import load_chip
    return load_chip(CHIPS / f"{name}.yaml")


def chip(name):
    from chipem.chip import Chip
    c = Chip(load(name))
    c.reset()
    return c


@pytest.mark.parametrize("name", F2802X + F2806X)
def test_pin_table_strict_and_builds(name):
    from pin_table_check import check_file
    rep = check_file(CHIPS / f"{name}.yaml")                                       # --strict: no warnings either
    assert not rep.errors and not rep.warnings, rep.errors + rep.warnings
    c = chip(name)
    assert c.clock.hz == 10e6                         # INTOSC1 / 1 after the boot ROM's DIVSEL = 3


@pytest.mark.parametrize("name,gpios,aios,adc", [
    ("tms320f28027", 22, 6, 13), ("tms320f28027da", 20, 6, 7), ("tms320f28069", 54, 6, 16),
    ("tms320f28069pn", 40, 6, 12)])
def test_package_counts(name, gpios, aios, adc):
    d = load(name)
    assert len([p for p in d.pins if p.kind == "gpio"]) == gpios                   # SPRS523 Table 6-1 / SPRS698 5-1
    assert len([p for p in d.pins if "AIO" in p.functions]) == aios
    chans = {s for p in d.pins for s in p.functions.get("ANALOG", "").split("/") if s.startswith("ADCIN")}
    assert len(chans) == adc


def test_f28027_pin_spot_checks():
    d = load("tms320f28027")
    by = {p.num: p for p in d.pins}
    assert by[1].name == "GPIO29/SCITXDA/SCLA/TZ3" and by[1].functions["MUX1"] == "SCITXDA"
    assert by[48].functions["MUX1"] == "SCIRXDA"                                    # LaunchPad backchannel RX
    assert by[9].name == "ADCINA2/COMP1A/AIO2" and by[9].functions["AIO"] == "AIO2"
    assert by[28].functions == {"GPIO": "GPIO1", "MUX1": "EPWM1B", "MUX3": "COMP1OUT"}
    assert by[19].name == "GPIO34/COMP2OUT" and by[19].port == "B" and by[19].bit == 2
    assert by[47].name == "GPIO12/TZ1/SCITXDA"                                      # LaunchPad push button S3
    leds = {p.name.split("/")[0] for p in d.pins if p.port == "A" and p.bit in (0, 1, 2, 3)}
    assert leds == {"GPIO0", "GPIO1", "GPIO2", "GPIO3"}
    da = load("tms320f28027da")
    assert not [p for p in da.pins if p.port == "B" and p.bit in (0, 1)]           # no GPIO32/33 on the DA
    assert "COMP2" not in {p.name for p in da.peripherals}


def test_f28069_pin_spot_checks():
    d = load("tms320f28069")
    names = {p.name.split("/")[0]: p for p in d.pins if p.kind == "gpio"}
    assert names["GPIO30"].functions["MUX1"] == "CANRXA" and names["GPIO31"].functions["MUX1"] == "CANTXA"
    assert names["GPIO39"].port == "B" and names["GPIO39"].bit == 7                 # LaunchPad LED D10
    assert names["GPIO58"].bit == 26 and "GPIO45" not in names                     # GPIO45-49 do not exist
    assert len(d.pins) == 100 and d.core.fpu is True


def test_memory_maps_and_peripheral_frames():
    d = load("tms320f28027")
    mem = {m.name: (m.base, m.size) for m in d.memory}
    assert mem["FLASH"] == (0x3F0000, 0x8000) and mem["L0"] == (0x8000, 0x1000)   # SPRS523 Figure 9-1, Table 9-3
    assert mem["BOOTROM"] == (0x3FE000, 0x2000)
    per = {p.name: p for p in d.peripherals}
    assert per["ADC"].base == 0x7100 and per["ADCRESULT"].base == 0x0B00 and per["NMIINT"].base == 0x7060
    assert per["COMP1"].base == 0x6400 and per["EPWM4"].base == 0x68C0 and per["SCIA"].params["fifo_depth"] == 4
    d9 = load("tms320f28069")
    mem = {m.name: (m.base, m.size) for m in d9.memory}
    assert mem["FLASH"] == (0x3D8000, 0x20000) and mem["L0L8"] == (0x8000, 0xC000)
    per = {p.name: p for p in d9.peripherals}
    assert per["ECANA"].params["clock_div"] == 2 and per["SYSCTRL"].size == 0x30 and per["EPWM8"].base == 0x69C0
    assert load("tms320f28026").memory[4].size == 0x4000 and load("tms320f280200").memory[4].base == 0x3F6000
    assert "CLA1" not in {p.name for p in load("tms320f28067").peripherals}
    assert "USB0" in {p.name for p in load("tms320f28069m").peripherals}


def test_irqs_from_data_sheet():
    d = load("tms320f28027")
    assert d.irqs[32] == "ADCINT1" and d.irqs[104] == "ADCINT1_10" and d.irqs[37] == "ADCINT9"
    assert d.irqs[38] == "TINT0" and d.irqs[96] == "SCIRXINTA" and d.irqs[120] == "XINT3" and d.irqs[40] == "EPWM1_TZINT"
    d9 = load("tms320f28069")
    assert d9.irqs[100] == "ECAN0_INTA" and d9.irqs[80] == "DINTCH1" and d9.irqs[112] == "CLA1_INT1"


@pytest.mark.parametrize("name,pid,cid", [("tms320f28027", 0xCF, 0xCF), ("tms320f28027da", 0xCE, 0xCF),
                                          ("tms320f28026", 0xC7, 0xC7), ("tms320f280200", 0xC1, 0xC7),
                                          ("tms320f28069", 0x9E, 0x9F), ("tms320f28069pn", 0x9C, 0x9F),
                                          ("tms320f28062", 0x66, 0x6F)])
def test_partid_classid(name, pid, cid):
    c = chip(name)
    addr = 0x3D7E80 if "2806" in name else 0x3D7FFF                             # SPRS698 Table 8-10 / SPRS523 9-11
    assert c.bus.read(addr, 1) == pid and c.bus.read(0x0882, 1) == cid


def test_register_tables_reproducible():
    import gen_piccolo_regs as g
    src = yaml.safe_load(g.SRC.read_text())
    for fam in ("2802x", "2806x"):
        assert g.dump(g.build(src, fam), fam) == g.DST[fam].read_text()


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="pdftotext not installed")
@pytest.mark.parametrize("fam,pdf", [("2802x", "tms320f28027.pdf"), ("2806x", "tms320f28069.pdf")])
def test_register_tables_match_data_sheet(fam, pdf, tmp_path):
    src = ROOT / "docs" / "datasheets" / "ti" / pdf
    if not src.exists():
        pytest.skip("data sheet not present")
    txt = tmp_path / "ds.txt"
    subprocess.run(["pdftotext", "-layout", str(src), str(txt)], check=True)
    r = subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "gen_piccolo_regs.py"), "--check-ds", fam, str(txt)],
                       capture_output=True, text=True)
    assert r.returncode == 0 and " 0 mismatch(es)" in r.stdout, r.stdout[-2000:]


@pytest.mark.parametrize("fam,base", [("2802x", "tms320f28027"), ("2806x", "tms320f28069")])
def test_bootrom_words_match_asm(fam, base):
    import gen_piccolo_regs as g
    words = g.bootrom_words(CHIPS / f"tms320f{fam}_bootrom.asm")
    d = yaml.safe_load((CHIPS / f"{base}.yaml").read_text())
    rom = {int(k): v for k, v in d["boot"]["rom_words"].items()}
    if fam == "2806x":                                     # the temperature getters live in the PARTID OTP page
        temp = words.pop(0x3D7E82)
        part = next(p for p in d["peripherals"] if p["name"] == "PARTID")["params"]["words"]
        assert [part[2 + i] for i in range(len(temp))] == temp
    assert rom == words
    assert d["boot"]["entry"] == min(a for a in words if a >= 0x3F8000)


def test_regs_tables_have_piccolo_fields():
    from chipem.regtable import load_regs
    rt = load_regs(CHIPS / "tms320f2802x_regs.yaml")
    sc = rt.peripheral("SYSCTRL")
    assert sc["PLLSTS"].has_field("DIVSEL") and not sc["PLLSTS"].has_field("CLKINDIV")
    assert sc["PCLKCR3"].reset == 0x2700 and sc["PLLLOCKPRD"].reset == 0xFFFF
    adc = rt.peripheral("ADC")
    assert adc["ADCSOC15CTL"].offset == 0x2F and adc["ADCOFFTRIM"].offset == 0x41
    rt9 = load_regs(CHIPS / "tms320f2806x_regs.yaml")
    assert rt9.peripheral("ADC")["ADCCTL2"].has_field("CLKDIV4EN")
    assert rt9.peripheral("SYSCTRL")["PLLCR"].field("DIV").width == 5               # 0..18 (SPRUH18 Figure 1-25)
