"""Tests for tools/svd2chip.py and chipem/regtable.py."""
import importlib.util
import io
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "tiny.svd"
GENERATED = ROOT / "chips" / "mk22_regs.yaml"

_spec = importlib.util.spec_from_file_location("svd2chip", ROOT / "tools" / "svd2chip.py")
svd2chip = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(svd2chip)

from chipem.regtable import RegTable, load_regs, regs_for  # noqa: E402


def _roundtrip(**kw):
    doc = svd2chip.convert(FIX, **kw)
    text = svd2chip.emit_yaml(doc, "test")
    loaded = yaml.safe_load(text)
    return doc, loaded, text


def _regs(p):
    return {r["name"]: r for r in p["registers"]}


def test_numbers():
    assert svd2chip.parse_int("0x1F") == 31
    assert svd2chip.parse_int("#101") == 5
    assert svd2chip.parse_int("42") == 42
    assert svd2chip.parse_int("4k") == 4096
    assert svd2chip.parse_enum_value("#1x0") == [4, 6]


def test_device_and_interrupts():
    _, d, _ = _roundtrip(flat=True)
    assert d["device"] == "TINY1"
    assert d["cpu"] == {"name": "CM3", "fpu": False, "nvic_prio_bits": 3}
    assert d["interrupts"] == {59: "PORTA", 60: "PORTB"}


def test_dim_expansion_and_fields():
    _, d, _ = _roundtrip(flat=True)
    regs = _regs(d["peripherals"]["PORTA"])
    for i in range(4):
        assert regs[f"PCR{i}"]["offset"] == 4 * i
        assert regs[f"PCR{i}"]["reset"] == 0x742
    f = {x["name"]: x for x in regs["PCR3"]["fields"]}
    assert f["MUX"]["lsb"] == 8 and f["MUX"]["width"] == 3
    assert f["MUX"]["enum"] == {0: "ALT0", 1: "ALT1", 7: "ALT7"}
    assert f["ISF"]["lsb"] == 24 and f["ISF"]["write_effect"] == "oneToClear"
    # [%s] arrays, 8-bit, read-only, resetMask applied
    assert regs["B0"]["offset"] == 0xB0 and regs["B1"]["offset"] == 0xB1
    assert regs["B1"]["width"] == 1 and regs["B1"]["access"] == "read-only"
    assert regs["B0"]["reset"] == 0x05 and regs["B0"]["reset_mask"] == 0x0F
    assert d["peripherals"]["PORTA"]["size"] == 0x1000


def test_register_derived_from():
    _, d, _ = _roundtrip(flat=True)
    regs = _regs(d["peripherals"]["PORTA"])
    assert regs["GPCLR"]["offset"] == 0x80 and regs["GPCLR"]["access"] == "write-only"
    assert regs["GPCHR"]["offset"] == 0x84 and regs["GPCHR"]["access"] == "write-only"


def test_peripheral_derived_from_nonflat_and_flat():
    _, d, _ = _roundtrip(flat=False)
    pb = d["peripherals"]["PORTB"]
    assert pb["derived_from"] == "PORTA" and pb["registers"] == [] and pb["base"] == 0x4004A000
    _, f, _ = _roundtrip(flat=True)
    pb = f["peripherals"]["PORTB"]
    assert pb["derived_from"] is None
    assert [r["name"] for r in pb["registers"]] == [r["name"] for r in f["peripherals"]["PORTA"]["registers"]]
    # regtable resolves derived_from transparently
    rt = RegTable(d)
    assert rt.peripheral("PORTB")["PCR2"].offset == 8
    assert rt.peripheral("PORTB").base == 0x4004A000


def test_peripheral_filter_keeps_base():
    doc = svd2chip.convert(FIX, ["PORTB"], flat=False)
    assert set(doc["peripherals"]) == {"PORTB", "PORTA"}
    doc = svd2chip.convert(FIX, ["PORTB"], flat=True)
    assert set(doc["peripherals"]) == {"PORTB"}


def test_clusters():
    _, d, _ = _roundtrip(flat=True)
    dma = d["peripherals"]["DMA"]
    regs = _regs(dma)
    assert regs["TCDA_SADDR"]["offset"] == 0x1000
    assert regs["TCDA_SOFF"]["offset"] == 0x1004 and regs["TCDA_SOFF"]["reset"] == 0x12
    assert regs["TCDB_SADDR"]["offset"] == 0x1020
    assert regs["TCDB_SOFF"]["width"] == 2       # inherited <size>16</size> from peripheral
    assert regs["CR"]["width"] == 4 and regs["CR"]["reset"] == 0x400
    assert dma["size"] == 0x2000                  # no addressBlock: max end rounded up to 4 KiB


def test_regtable_api(tmp_path):
    _, _, text = _roundtrip(flat=False)
    p = tmp_path / "tiny_regs.yaml"
    p.write_text(text)
    rt = load_regs(p)
    porta = rt.peripheral("PORTA")
    assert porta.at(0x84).name == "GPCHR"
    assert porta.containing(0xB1).name == "B1"
    assert porta["PCR1"].field("MUX").mask == 0x700
    assert porta["PCR1"].w1c_mask == 1 << 24
    assert porta["B0"].ro_mask == 0xFF                       # read-only register
    assert porta["PCR0"].ro_mask == ~(0x1 | 0x700 | 1 << 24) & 0xFFFFFFFF   # reserved bits
    assert porta.reset_values()["PCR0"] == 0x742
    assert regs_for("PORTB", rt)["ISFR"].offset == 0xA0
    assert regs_for("NOPE", rt) is None
    spec = {s["name"]: s for s in porta.reg_specs()}
    assert spec["PCR0"] == dict(name="PCR0", offset=0, width=4, reset=0x742,
                                ro_mask=porta["PCR0"].ro_mask, w1c_mask=1 << 24, unit_bits=8)
    pr, reg = rt.by_address(0x4004A084)
    assert pr.name == "PORTB" and reg.name == "GPCHR"


def test_check(tmp_path):
    chip = tmp_path / "chip.yaml"
    chip.write_text("peripherals:\n"
                    "  - {name: PORTA, model: kinetis.port, base: 0x40049000, size: 0x1000}\n"
                    "  - {name: PORTB, model: kinetis.port, base: 0x4004B000, size: 0x1000}\n"
                    "irqs: {59: PORTA, 60: PORTX}\n")
    out = io.StringIO()
    assert svd2chip.check(FIX, chip, out) == 2
    assert "PORTB" in out.getvalue() and "0x4004A000" in out.getvalue()
    assert svd2chip.check(FIX, tmp_path / "missing.yaml", io.StringIO()) == 0


def test_check_omitted(tmp_path):
    """`omitted:` peripherals/IRQs are reported as OMITTED (reason), not compared; instantiating one is a mismatch."""
    chip = tmp_path / "chip.yaml"
    chip.write_text("peripherals:\n"
                    "  - {name: PORTA, model: kinetis.port, base: 0x40049000, size: 0x1000}\n"
                    "omitted:\n"
                    "  - {name: PORTB, irqs: [60], reason: not bonded out}\n"
                    "irqs: {59: PORTA, 60: Reserved76}\n")
    out = io.StringIO()
    assert svd2chip.check(FIX, chip, out) == 0
    text = out.getvalue()
    assert "OMITTED PORTB@0x4004A000 (not bonded out)" in text and "OMITTED irq 60" in text
    assert "PORTB@0x4004A000," not in text and not text.rstrip().count("PORTB@0x4004A000") > 1
    chip.write_text(chip.read_text().replace("size: 0x1000}", "size: 0x1000}\n"
                                             "  - {name: PORTB, model: kinetis.port, base: 0x4004A000}"))
    out = io.StringIO()
    assert svd2chip.check(FIX, chip, out) == 1
    assert "PORTB: listed under omitted: but still instantiated" in out.getvalue()


def test_check_size_and_include(tmp_path):
    (tmp_path / "base.yaml").write_text("peripherals:\n"
                                        "  - {name: PORTA, model: kinetis.port, base: 0x40049000, size: 0x800}\n")
    chip = tmp_path / "chip.yaml"
    chip.write_text("include: base.yaml\nname: X\n")
    out = io.StringIO()
    assert svd2chip.check(FIX, chip, out) == 1          # included peripheral list; 0x800 < addressBlock 0x1000
    assert "PORTA" in out.getvalue() and "size 0x800" in out.getvalue()


def test_cli(tmp_path):
    out = tmp_path / "o.yaml"
    assert svd2chip.main([str(FIX), "-o", str(out), "--flat", "--peripherals", "PORTB"]) == 0
    d = yaml.safe_load(out.read_text())
    assert list(d["peripherals"]) == ["PORTB"]


@pytest.mark.skipif(not GENERATED.exists(), reason="chips/mk22_regs.yaml not generated")
def test_generated_mk22():
    rt = load_regs(GENERATED)
    assert rt.device == "MK22F12"
    assert rt.cpu["fpu"] is True and rt.cpu["nvic_prio_bits"] == 4
    sim = rt.peripheral("SIM")
    assert sim.base == 0x40047000
    assert sim["SCGC5"].offset == 0x1038 and sim["SCGC5"].reset == 0x00040182
    assert sim["CLKDIV1"].offset == 0x1044
    assert rt.peripheral("PORTA")["PCR0"].reset == 0x742
    assert rt.peripheral("PORTA")["ISFR"].offset == 0xA0
    assert rt.peripheral("GPIOC").base == 0x400FF080
    assert rt.peripheral("VBAT").name == "RFVBAT" and rt.peripheral("VBAT").base == 0x4003E000
    wdog = rt.peripheral("WDOG")
    assert wdog["STCTRLH"].width == 2 and wdog["STCTRLH"].reset == 0x01D3
    assert wdog["REFRESH"].offset == 0xC and wdog["UNLOCK"].offset == 0xE
    mcg = rt.peripheral("MCG")
    assert mcg["S"].offset == 6 and mcg["S"].reset == 0x10 and mcg["C1"].reset == 0x04
    assert rt.peripheral("LLWU")["F1"].offset == 5
    assert rt.peripheral("FTFE").base == 0x40020000
    assert rt.peripheral("ADC0")["SC3"].offset == 0x24
    assert rt.peripheral("UART4").base == 0x400EA000
    assert rt.interrupts[31] == "UART0_RX_TX" and rt.interrupts[59] == "PORTA"
    for name in ("UART4", "SDHC", "CAN0", "FTFE"):
        assert name in rt


def test_regtable_unit_bits_16():
    """16-bit-unit tables (C2000): masks span width x 16 bits; reg_specs carry unit_bits."""
    from pathlib import Path
    from chipem.regtable import load_regs
    rt = load_regs(Path(__file__).resolve().parent.parent / "chips" / "tms320f28232_regs.yaml")
    assert rt.unit_bits == 16
    gpio = rt.peripheral("GPIO")
    mux = gpio["GPAMUX1"]                                  # width 2 words = 32 bits, all GPIOn fields writable
    assert mux.width == 2 and mux.full_mask == 0xFFFFFFFF and mux.ro_mask == 0
    spec = {s["name"]: s for s in gpio.reg_specs()}
    assert spec["GPAMUX1"]["unit_bits"] == 16
    sel = gpio["GPIOXINT1SEL"]                             # 1 word, GPIOSEL bits 0-4: the rest is reserved
    assert sel.full_mask == 0xFFFF and sel.ro_mask == 0xFFE0
    assert spec["GPIOXINT1SEL"]["ro_mask"] == 0xFFE0
