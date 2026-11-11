"""TMS320F2833x / F2823x family descriptions (chips/tms320f2833[2-5]*.yaml, tms320f2823[45].yaml): per-device
differences from SPRS439Q Tables 5-1 / 5-2 and 8-31, the generated superset register table, the BGA ball tables
(ZHH / ZJZ) and the variant mechanism (`include:` + `drop:` / `peripheral_params:`).  Spot checks are copied by eye
from the data sheet, not from the generated tables."""
from pathlib import Path

import pytest
import yaml

from chipem.chipdesc import load_chip

ROOT = Path(__file__).resolve().parent.parent
CHIPS = ROOT / "chips"

# part: (fpu, MHz, flash base, flash words, SARAM words in L0-Lx, PARTID, CLASSID, eCAP count, SCI letters,
#        McBSP letters, HRPWM ePWMs)                                       SPRS439Q Tables 5-1 / 5-2 / 8-22..8-24 / 8-31
PARTS = {
    "F28335": (True, 150, 0x300000, 0x40000, 0x8000, 0xEF, 0xEF, 6, "ABC", "AB", 6),
    "F28334": (True, 150, 0x320000, 0x20000, 0x8000, 0xEE, 0xEF, 4, "ABC", "AB", 6),
    "F28333": (True, 100, 0x300000, 0x40000, 0x8000, 0xE0, 0xEF, 6, "ABC", "AB", 6),
    "F28332": (True, 100, 0x330000, 0x10000, 0x6000, 0xED, 0xEF, 4, "AB", "A", 4),
    "F28235": (False, 150, 0x300000, 0x40000, 0x8000, 0xE8, 0xE8, 6, "ABC", "AB", 6),
    "F28234": (False, 150, 0x320000, 0x20000, 0x8000, 0xE7, 0xE8, 4, "ABC", "AB", 6),
    "F28232": (False, 100, 0x330000, 0x10000, 0x6000, 0xE6, 0xE8, 4, "AB", "A", 4),
}


def chip_of(part: str):
    from chipem.chip import Chip
    from chipem.log import EventLog
    log = EventLog()
    ev: list = []
    log.subscribe(ev.append)
    c = Chip(load_chip(CHIPS / f"tms320{part.lower()}.yaml"), log=log)
    c.reset()
    return c, ev


@pytest.mark.parametrize("part", PARTS)
def test_device_differences(part):
    fpu, mhz, fb, fs, ram, partid, classid, necap, scis, mcbsps, nhr = PARTS[part]
    d = load_chip(CHIPS / f"tms320{part.lower()}.yaml")
    assert d.name == f"TMS320{part}" and d.family == "ti_c2000"
    assert d.core.fpu is fpu and d.core.clock_hz == mhz * 1e6
    mem = {m.name: m for m in d.memory}
    assert (mem["FLASH"].base, mem["FLASH"].size) == (fb, fs)
    assert mem["FLASH"].base + mem["FLASH"].size == 0x340000            # all flash sizes end at the password
    lram = [m for m in d.memory if m.base == 0x8000]
    assert len(lram) == 1 and lram[0].size == ram
    names = {p.name for p in d.peripherals}
    assert {f"ECAP{i}" for i in range(1, 7)} & names == {f"ECAP{i}" for i in range(1, necap + 1)}
    assert {f"SCI{x}" for x in "ABC"} & names == {f"SCI{x}" for x in scis}
    assert {f"MCBSP{x}" for x in "AB"} & names == {f"MCBSP{x}" for x in mcbsps}
    assert {"DMA", "XINTF", "ECANA", "ECANB", "EQEP1", "EQEP2", "SPIA", "I2CA"} <= names
    hr = [p.name for p in d.peripherals if p.model == "ti_c2000.epwm" and p.params.get("hrpwm")]
    assert hr == [f"EPWM{i}" for i in range(1, nhr + 1)]
    irqs = set(d.irqs.values())
    assert ("LVF" in irqs) is fpu and ("LUF" in irqs) is fpu                  # PIE 12.7 / 12.8
    assert ("SCIRXINTC" in irqs) is ("C" in scis) and ("ECAP5_INT" in irqs) is (necap == 6)
    assert ("MRINTB" in irqs) is ("B" in mcbsps)
    c, ev = chip_of(part)
    assert not [e for e in ev if e.kind in ("warn", "fault")]
    assert c.bus.read(0x380090, 1) == partid                                  # PARTID in OTP (Table 8-31)
    assert c.bus.read(0x0882, 1) == classid                                   # CLASSID
    assert c.core.fpu is fpu if hasattr(c.core, "fpu") else True


def test_f28335_pins_equal_f28232():
    a = load_chip(CHIPS / "tms320f28335.yaml")
    b = load_chip(CHIPS / "tms320f28232.yaml")
    assert len(a.pins) == 176
    assert [(p.num, p.name, p.kind, p.functions) for p in a.pins] == [(p.num, p.name, p.kind, p.functions) for p in b.pins]


@pytest.mark.parametrize("f,n", [("tms320f28335.yaml", 176), ("tms320f28335zhh.yaml", 179),
                                 ("tms320f28335zjz.yaml", 176), ("tms320f28334.yaml", 176),
                                 ("tms320f28333.yaml", 176), ("tms320f28332.yaml", 176),
                                 ("tms320f28235.yaml", 176), ("tms320f28234.yaml", 176)])
def test_pin_table_check_clean(f, n, capsys):
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "pin_table_check.py"), str(CHIPS / f),
                        "--expect", str(n), "--strict"], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and "0 error(s), 0 warning(s)" in r.stdout, r.stdout[-2000:]


def _balls(f: str) -> dict:
    return {p["ball"]: p for p in yaml.safe_load((CHIPS / f).read_text())["pins"]}


def test_zhh_balls():
    """SPRS439Q Table 6-1 (ZHH column) and Figures 6-2..6-5, read by eye."""
    b = _balls("tms320f28335zhh.yaml")
    assert len(b) == 179
    assert b["M10"]["name"] == "TRST" and b["N12"]["name"] == "TCK" and b["L10"]["name"] == "XRS"
    assert b["J13"]["name"] == "X1" and b["C11"]["name"] == "XCLKOUT"
    assert b["M3"]["name"] == "ADCINA0" and b["P2"]["name"] == "ADCINB0" and b["M2"]["name"] == "ADCLO"
    assert b["P1"]["name"] == "VSSAIO" and b["N2"]["name"] == "VDDAIO" and b["K2"]["name"] == "VDDA2"
    assert b["C1"]["name"] == "GPIO0/EPWM1A" and b["E4"]["name"].startswith("GPIO3/")
    assert b["J1"]["name"].startswith("GPIO17/") and b["H2"]["name"].startswith("GPIO14/")
    assert b["G11"]["name"].startswith("GPIO63/SCITXDC") and b["G10"]["name"] == "GPIO64/XD15"
    assert b["A2"]["name"].startswith("GPIO31/CANTXA") and b["B9"]["name"] == "XRD"
    for ball in ("N11", "P6", "P8"):                                      # the three balls without an LQFP pin
        assert b[ball]["name"] == "VSS" and b[ball]["kind"] == "power"
    assert sum(p["name"] == "VSS" for p in b.values()) == 25 and sum(p["name"] == "VDD" for p in b.values()) == 13
    nums = [p["num"] for p in yaml.safe_load((CHIPS / "tms320f28335zhh.yaml").read_text())["pins"]]
    assert nums == list(range(1, 180))
    assert b["A2"]["num"] == 1 and b["P14"]["num"] == 179                  # row-major over existing balls (no A1)


def test_zjz_balls():
    """SPRS439Q Table 6-1 (ZJZ column) and Figures 6-6..6-9, read by eye."""
    b = _balls("tms320f28335zjz.yaml")
    assert len(b) == 176
    assert b["L11"]["name"] == "TRST" and b["M14"]["name"] == "TCK" and b["M13"]["name"] == "XRS"
    assert b["G14"]["name"] == "X1" and b["A10"]["name"] == "XCLKOUT" and b["M3"]["name"] == "ADCINA0"
    assert b["P1"]["name"] == "VSSA2" and b["P2"]["name"] == "VSS2AGND" and b["N1"]["name"] == "VSSAIO"
    assert b["K3"]["name"] == "VDD1A18" and b["D1"]["name"] == "GPIO0/EPWM1A"
    assert b["J1"]["name"].startswith("GPIO15/") and b["H1"]["name"].startswith("GPIO12/")
    assert b["F12"]["name"].startswith("GPIO63/") and b["E14"]["name"] == "GPIO64/XD15"
    assert b["N14"]["name"] == "VDDIO"                                     # extra VDDIO ball; no ball for PGF pin 167
    assert sum(p["name"] == "VDD" for p in b.values()) == 12 and sum(p["name"] == "VSS" for p in b.values()) == 22
    assert b["A1"]["num"] == 1 and b["P14"]["num"] == 176


def test_bga_functions_follow_lqfp_pins():
    lq = {p.name: p for p in load_chip(CHIPS / "tms320f28335.yaml").pins if p.kind == "gpio"}
    for f in ("tms320f28335zhh.yaml", "tms320f28335zjz.yaml"):
        d = load_chip(CHIPS / f)
        g = [p for p in d.pins if p.kind == "gpio"]
        assert len(g) == 88
        for p in g:
            assert p.functions == lq[p.name].functions and (p.port, p.bit) == (lq[p.name].port, lq[p.name].bit)
        assert len(d.peripherals) == len(load_chip(CHIPS / "tms320f28335.yaml").peripherals)


def test_register_table_is_generated(tmp_path):
    import subprocess
    import sys
    out = tmp_path / "regs.yaml"
    subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "gen_tms320f2833x_regs.py"), "-o", str(out)],
                   check=True, capture_output=True, cwd=ROOT)
    assert out.read_text() == (CHIPS / "tms320f2833x_regs.yaml").read_text()


def test_register_table_addresses():
    """Superset table: the added instances sit at the SPRS439Q addresses (Tables 8-6, 8-9, 8-14)."""
    t = yaml.safe_load((CHIPS / "tms320f2833x_regs.yaml").read_text())["peripherals"]
    regs = lambda b: {r["name"]: t[b]["base"] + r["offset"] for r in t[b]["registers"]}   # noqa: E731
    assert regs("SCIC")["SCICCR"] == 0x7770 and regs("SCIC")["SCITXBUF"] == 0x7779 and regs("SCIC")["SCIPRI"] == 0x777F
    assert regs("ECAP5")["ECCTL1"] == 0x6A94 and regs("ECAP6")["ECFRC"] == 0x6AB9
    assert regs("MCBSPB")["PCR"] == 0x5052 and regs("MCBSPB")["DXR1"] == 0x5043
    assert regs("EPWM6")["HRCNFG"] == 0x6960
    dev = {r["name"]: r for r in t["DEV_EMU"]["registers"]}
    assert dev["CLASSID"]["reset"] == 0xEF


def test_register_table_against_datasheet():
    """Run the generator's data sheet check when pdftotext is available (646 registers, 0 mismatches)."""
    import shutil
    import subprocess
    import sys
    pdf = ROOT / "docs" / "datasheets" / "ti" / "tms320f28232.pdf"
    if not shutil.which("pdftotext") or not pdf.exists():
        pytest.skip("pdftotext or the data sheet is not available")
    txt = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True, check=True).stdout
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write(txt)
    r = subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "gen_tms320f2833x_regs.py"), "--no-write",
                        "--check-ds", fh.name], capture_output=True, text=True, cwd=ROOT)
    Path(fh.name).unlink()
    assert r.returncode == 0 and "0 mismatch(es)" in r.stdout, r.stdout[-1500:]


def test_variant_patch_rejects_unknown_names(tmp_path):
    (tmp_path / "base.yaml").write_text((CHIPS / "tms320f28335.yaml").read_text())
    (tmp_path / "v.yaml").write_text("include: base.yaml\nname: X\ndrop: [SCID]\n")
    with pytest.raises(ValueError, match="SCID"):
        load_chip(tmp_path / "v.yaml")
    (tmp_path / "w.yaml").write_text("include: base.yaml\nname: Y\nperipheral_params: {EPWM9: {hrpwm: false}}\n")
    with pytest.raises(ValueError, match="EPWM9"):
        load_chip(tmp_path / "w.yaml")
