#!/usr/bin/env python3
"""Generate chips/tms320f2808_regs.yaml (TMS320F2808 register table, 16-bit units) and check it against the data sheet.

TI publishes no SVD for the C2000 280x family.  The 280x peripherals are the same IP (same register layouts) as the
F2833x/F2823x ones already transcribed in chips/tms320f28232_regs.yaml: CPU timers, PIE, ePWM/HRPWM, eCAP, eQEP,
eCAN, SCI, SPI, ADC, I2C, flash and CSM registers, GPIO ports A/B, XINT.  This script copies those blocks and applies
the documented 280x differences (each one cited below), so the table is reproducible:

* SYSCTRL (SPRU712H Table 3-1, Figures 3-3..3-20): XCLK at 0x7010; PLLSTS has CLKINDIV (bit 1) instead of DIVSEL;
  PCLKCR0 has SPI-B/C/D gates (bits 9/6/7) and no SCI-C/McBSP gates; PCLKCR1 has eCAP1-4 only; no PCLKCR3 (the CPU
  timers and GPIO inputs have no clock gate) and no MAPCNF.
* GPIO (SPRS230P Table 9-16, SPRU712H Table 4-1, Figures 4-15/4-16): ports A and B only (GPIO0-34, GPIO35 internal),
  no port C, no GPIOXINT3-7SEL; GPBPUD resets to 0x18 (GPIO35 pull-up off).
* XINT (SPRS230P Table 9-30): XINT1, XINT2 and XNMI only.
* DEV_EMU (SPRS230P Table 9-27, SPRU712H Figures 5-1..5-3): PARTID at 0x0882 (0x003C = F2808), REVID 0x0003
  (silicon rev. C), DEVICECNF reset from Figure 5-1.
* I2C-A (SPRS230P Table 9-15, SPRU721): no I2CEMDR.
* SPI-B/C/D (SPRS230P Tables 9-12..9-14): SPI-A's layout at 0x7740/0x7760/0x7780.
* Interrupts: SPRS230P Table 9-28 (INT6 holds SPI-A..D, no DMA/XINT3-7/McBSP).
Blocks the F2808 does not have (XINTF, DMA, McBSP) are left out.

    .venv/bin/python -I tools/gen_tms320f2808_regs.py                    # writes chips/tms320f2808_regs.yaml
    .venv/bin/python -I tools/gen_tms320f2808_regs.py --check-ds ds.txt  # compare with `pdftotext -layout` output of
                                                                         # docs/datasheets/ti/tms320f2808.pdf
The check parses the register tables of the data sheet text (Tables 9-2..9-31) and reports every register whose
address differs or that is missing on either side; it exits 1 on a mismatch.
"""
from __future__ import annotations

import argparse
import copy
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "chips" / "tms320f28232_regs.yaml"
DST = ROOT / "chips" / "tms320f2808_regs.yaml"

INTERRUPTS = {
    13: "INT13", 14: "INT14", 18: "NMI", 19: "ILLEGAL",
    32: "SEQ1INT", 33: "SEQ2INT", 35: "XINT1", 36: "XINT2", 37: "ADCINT", 38: "TINT0", 39: "WAKEINT",
    **{40 + i: f"EPWM{i + 1}_TZINT" for i in range(6)}, **{48 + i: f"EPWM{i + 1}_INT" for i in range(6)},
    **{56 + i: f"ECAP{i + 1}_INT" for i in range(4)}, 64: "EQEP1_INT", 65: "EQEP2_INT",
    72: "SPIRXINTA", 73: "SPITXINTA", 74: "SPIRXINTB", 75: "SPITXINTB",
    76: "SPIRXINTC", 77: "SPITXINTC", 78: "SPIRXINTD", 79: "SPITXINTD",
    88: "I2CINT1A", 89: "I2CINT2A",
    96: "SCIRXINTA", 97: "SCITXINTA", 98: "SCIRXINTB", 99: "SCITXINTB",
    100: "ECAN0INTA", 101: "ECAN1INTA", 102: "ECAN0INTB", 103: "ECAN1INTB",
}

# blocks copied unchanged from the F2823x table (same IP and offsets on the 280x)
SAME = ["FLASH", "CSM", "ADCMIRROR", "CPUTIMER0", "CPUTIMER1", "CPUTIMER2", "PIE", "ECANA", "ECANB",
        "EPWM1", "EPWM2", "EPWM3", "EPWM4", "EPWM5", "EPWM6", "ECAP1", "ECAP2", "ECAP3", "ECAP4", "EQEP1", "EQEP2",
        "SCIA", "SCIB", "SPIA", "ADC"]
ORDER = ["DEV_EMU", "FLASH", "CSM", "ADCMIRROR", "CPUTIMER0", "CPUTIMER1", "CPUTIMER2", "PIE", "ECANA", "ECANB",
         "EPWM1", "EPWM2", "EPWM3", "EPWM4", "EPWM5", "EPWM6", "ECAP1", "ECAP2", "ECAP3", "ECAP4", "EQEP1", "EQEP2",
         "GPIO", "SYSCTRL", "SPIA", "SCIA", "XINT", "ADC", "SPIB", "SCIB", "SPIC", "SPID", "I2CA"]


def _r(name, off, width=1, reset=0, access="read-write", fields=None, desc=None):
    d = {"name": name, "offset": off, "width": width, "reset": reset,
         "reset_mask": 0xFFFFFFFF if width == 2 else 0xFFFF, "access": access}
    if desc:
        d["description"] = desc
    if fields:
        d["fields"] = [dict(name=f[0], lsb=f[1], width=f[2], **({"access": f[3]} if len(f) > 3 else {}))
                       for f in fields]
    return d


def sysctrl() -> dict:
    """SPRU712H Table 3-1 and Figures 3-3..3-20."""
    regs = [
        _r("XCLK", 0x00, fields=[("XCLKOUTDIV", 0, 2)], desc="XCLKOUT divide (00 = SYSCLKOUT/4 at reset)"),
        _r("PLLSTS", 0x01, reset=0x0001, fields=[("PLLLOCKS", 0, 1, "read-only"), ("CLKINDIV", 1, 1),
                                                 ("PLLOFF", 2, 1), ("MCLKSTS", 3, 1, "read-only"),
                                                 ("MCLKCLR", 4, 1), ("OSCOFF", 5, 1), ("MCLKOFF", 6, 1)]),
        _r("HISPCP", 0x0A, reset=0x0001, fields=[("HSPCLK", 0, 3)]),
        _r("LOSPCP", 0x0B, reset=0x0002, fields=[("LSPCLK", 0, 3)]),
        _r("PCLKCR0", 0x0C, fields=[("TBCLKSYNC", 2, 1), ("ADCENCLK", 3, 1), ("I2CAENCLK", 4, 1),
                                    ("SPICENCLK", 6, 1), ("SPIDENCLK", 7, 1), ("SPIAENCLK", 8, 1),
                                    ("SPIBENCLK", 9, 1), ("SCIAENCLK", 10, 1), ("SCIBENCLK", 11, 1),
                                    ("ECANAENCLK", 14, 1), ("ECANBENCLK", 15, 1)]),
        _r("PCLKCR1", 0x0D, fields=[*((f"EPWM{i}ENCLK", i - 1, 1) for i in range(1, 7)),
                                    *((f"ECAP{i}ENCLK", 7 + i, 1) for i in range(1, 5)),
                                    ("EQEP1ENCLK", 14, 1), ("EQEP2ENCLK", 15, 1)]),
        _r("LPMCR0", 0x0E, reset=0x00FC, fields=[("LPM", 0, 2), ("QUALSTDBY", 2, 6), ("WDINTE", 15, 1)]),
        _r("PLLCR", 0x11, fields=[("DIV", 0, 4)]),
        _r("SCSR", 0x12, reset=0x0005, fields=[("WDOVERRIDE", 0, 1), ("WDENINT", 1, 1),
                                               ("WDINTS", 2, 1, "read-only")]),
        _r("WDCNTR", 0x13, access="read-only", fields=[("WDCNTR", 0, 8)]),
        _r("WDKEY", 0x15, fields=[("WDKEY", 0, 8)]),
        _r("WDCR", 0x19, fields=[("WDPS", 0, 3), ("WDCHK", 3, 3), ("WDDIS", 6, 1), ("WDFLAG", 7, 1)]),
    ]
    return {"base": 0x7010, "size": 0x20, "derived_from": None,
            "description": "System control (280x): XCLK, PLL (CLKINDIV), clock gates, low-power modes, watchdog",
            "group": "SYSCTRL", "interrupts": {39: "WAKEINT"}, "registers": regs}


def dev_emu() -> dict:
    """SPRS230P Table 9-27, SPRU712H Figures 5-1..5-3."""
    return {"base": 0x0880, "size": 0x180, "derived_from": None,
            "description": "Device emulation registers (DEVICECNF, PARTID, REVID, block protection)", "group": "DEV_EMU",
            "registers": [
                _r("DEVICECNF", 0x00, width=2, reset=0x000F002B,
                   fields=[("VMAPS", 3, 1, "read-only"), ("XRS", 5, 1, "read-only"), ("ENPROT", 19, 1)]),
                _r("PARTID", 0x02, reset=0x003C, access="read-only", desc="0x003C = TMS320F2808 (flash device)",
                   fields=[("PARTNO", 0, 8), ("PARTTYPE", 8, 8)]),
                _r("REVID", 0x03, reset=0x0003, access="read-only", desc="silicon revision C"),
                _r("PROTSTART", 0x04, reset=0x0100),
                _r("PROTRANGE", 0x05, reset=0x00FF),
            ]}


def build(src: dict) -> dict:
    sp = src["peripherals"]
    out: dict[str, dict] = {}
    for b in SAME:
        out[b] = copy.deepcopy(sp[b])
    for b in ("EPWM5", "EPWM6"):
        out[b]["description"] = (f"e{b[1:4]}{b[-1]} (no HRPWM on the F2808: SPRS230P Table 9-3 lists TBPHSHR/CMPAHR as "
                                 "N/A and HRCNFG as F2809-only; the locations are kept as plain storage)")
    out["DEV_EMU"] = dev_emu()
    out["SYSCTRL"] = sysctrl()
    g = copy.deepcopy(sp["GPIO"])
    g["registers"] = [r for r in g["registers"]
                      if not r["name"].startswith("GPC") and r["name"] not in {f"GPIOXINT{i}SEL" for i in range(3, 8)}]
    for r in g["registers"]:
        if r["name"] == "GPBPUD":
            r["reset"] = 0x00000018          # SPRU712H Figure 4-16: GPIO35 (internal) and bit 4 reset to 1
        if r["name"] == "GPAPUD":
            r["reset"] = 0x00000FFF          # Figure 4-15: GPIO0-11 pull-ups disabled
    g["description"] = "GPIO ports A (GPIO0-31) and B (GPIO32-34, GPIO35 internal): control, data, XINT/LPM select"
    out["GPIO"] = g
    x = copy.deepcopy(sp["XINT"])
    x["registers"] = [r for r in x["registers"] if r["name"] not in {f"XINT{i}CR" for i in range(3, 8)}]
    x["interrupts"] = {35: "XINT1", 36: "XINT2"}
    x["description"] = "External interrupts XINT1, XINT2, XNMI (SPRS230P Table 9-30)"
    out["XINT"] = x
    i2c = copy.deepcopy(sp["I2CA"])
    i2c["registers"] = [r for r in i2c["registers"] if r["name"] != "I2CEMDR"]
    i2c["size"] = 0x40
    i2c["description"] = "I2C-A (SPRU721; SPRS230P Table 9-15, no I2CEMDR on the 280x)"
    out["I2CA"] = i2c
    for letter, base, irq in (("B", 0x7740, 74), ("C", 0x7760, 76), ("D", 0x7780, 78)):
        s = copy.deepcopy(sp["SPIA"])
        s["base"] = base
        s["description"] = f"SPI-{letter} (same layout as SPI-A, SPRS230P Tables 9-12..9-14)"
        s["interrupts"] = {irq: f"SPIRXINT{letter}", irq + 1: f"SPITXINT{letter}"}
        out[f"SPI{letter}"] = s
    out["SPIA"]["interrupts"] = {72: "SPIRXINTA", 73: "SPITXINTA"}
    doc = {"device": "TMS320F2808", "svd_version": "hand-written", "address_unit_bits": 16,
           "cpu": {"name": "C28x", "fpu": False}, "interrupts": dict(sorted(INTERRUPTS.items())),
           "peripherals": {k: out[k] for k in ORDER}}
    return doc


HEADER = """\
# TMS320F2808 (280x) register table, GENERATED by tools/gen_tms320f2808_regs.py -- do not edit by hand; change the
# generator and rerun it.  TI publishes no SVD for the 280x.  Blocks with the same IP as the F2823x are copied from
# chips/tms320f28232_regs.yaml; the 280x differences (SYSCTRL, GPIO ports A/B, XINT1/2/XNMI, DEV_EMU PARTID/REVID,
# SPI-B/C/D, I2C without I2CEMDR, PIE table) come from SPRS230P (data sheet Rev. P, Tables 9-2..9-31) and SPRU712H
# (280x System Control and Interrupts), see the generator's docstring.  Every register address was compared with the
# data sheet tables: `.venv/bin/python -I tools/gen_tms320f2808_regs.py --check-ds <pdftotext -layout of the PDF>`.
# Address units are 16-bit words: offsets/sizes/widths count words; a width-2 register is 32 bits, low word first.
"""


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return f"0x{v:X}" if v > 9 else str(v)
    if v is None:
        return "null"
    s = str(v)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.\-]*", s) and s.lower() not in ("yes", "no", "on", "off", "null", "true",
                                                                              "false"):
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _flow(d: dict) -> str:
    parts = []
    for k, v in d.items():
        if k == "fields":
            continue
        if isinstance(v, dict):
            parts.append(f"{k}: {{" + ", ".join(f"{_fmt(a)}: {_fmt(b)}" for a, b in v.items()) + "}")
        else:
            if k in ("reset", "reset_mask") and isinstance(v, int):
                parts.append(f"{k}: 0x{v:0{8 if d.get('width') == 2 else 4}X}")
            elif k == "offset":
                parts.append(f"{k}: 0x{v:02X}")
            else:
                parts.append(f"{k}: {_fmt(v)}")
    return ", ".join(parts)


def dump(doc: dict) -> str:
    out = [HEADER.rstrip("\n")]
    out.append(f"device: {doc['device']}")
    out.append(f'svd_version: "{doc["svd_version"]}"')
    out.append(f"address_unit_bits: {doc['address_unit_bits']}")
    out.append("cpu: {name: C28x, fpu: false}")
    out.append("interrupts:                 # PIE vector numbers (INTx.y = 32 + 8(x-1) + (y-1)); 13/14 INT13/INT14")
    for n, nm in doc["interrupts"].items():
        out.append(f"  {n}: {nm}")
    out.append("peripherals:")
    for name, p in doc["peripherals"].items():
        out.append(f"  {name}:")
        out.append(f"    base: 0x{p['base']:04X}")
        out.append(f"    size: 0x{p['size']:03X}")
        out.append("    derived_from: null")
        out.append(f"    description: {_fmt(p.get('description', ''))}")
        out.append(f"    group: {_fmt(p.get('group', name))}")
        if p.get("interrupts"):
            out.append("    interrupts: {" + ", ".join(f"{k}: {v}" for k, v in p["interrupts"].items()) + "}")
        out.append("    registers:")
        for r in p["registers"]:
            fl = r.get("fields")
            if fl:
                out.append(f"      - {{{_flow(r)}, fields: [")
                for f in fl:
                    out.append(f"          {{{_flow(f)}}},")
                out.append("        ]}")
            else:
                out.append(f"      - {{{_flow(r)}}}")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------------------------- data sheet check
_TABLES = {   # SPRS230P table number -> (block names, one per address column; name mangling)
    "9-2": ("CPUTIMER", None), "9-3": (["EPWM1", "EPWM2", "EPWM3", "EPWM4", "EPWM5", "EPWM6"], None),
    "9-4": (["ECAP1", "ECAP2", "ECAP3", "ECAP4"], None), "9-5": (["EQEP1", "EQEP2"], None),
    "9-6": (["ADC", "ADCMIRROR"], None), "9-8": (["ECANA", "ECANB"], None),
    "9-9": (["SCIA"], "A"), "9-10": (["SCIB"], "B"), "9-11": (["SPIA"], None), "9-12": (["SPIB"], None),
    "9-13": (["SPIC"], None), "9-14": (["SPID"], None), "9-15": (["I2CA"], None), "9-16": (["GPIO"], None),
    "9-27": (["DEV_EMU"], None), "9-29": (["PIE"], None), "9-30": (["XINT"], None), "9-31": (["SYSCTRL"], None),
}
_SKIP_DS = {"AQCSFR"}     # wrapped in the PDF ("AQCSFR" + "C" on the next line); checked through AQCSFRC below


def parse_ds(text: str) -> dict[tuple[str, str], int]:
    """{(block, register): word address} from the register tables of the data sheet text."""
    lines = text.splitlines()
    out: dict[tuple[str, str], int] = {}
    cur = None
    for i, ln in enumerate(lines):
        m = re.search(r"Table (9-\d+)\.", ln)
        if m:
            cur = m.group(1) if m.group(1) in _TABLES else None
            continue
        if cur is None:
            continue
        blocks, suffix = _TABLES[cur]
        m = re.match(r"^\s*([A-Z][A-Z0-9]+)(?:\(\d\))?\s+((?:(?:0x[0-9A-F]{4}|N/A)(?:\(\d\))?\s+)+)", ln + " ")
        if not m:
            # TBPHSHR is split over three lines in the PDF ("TBPHSH" / addresses / "R")
            if re.match(r"^\s+0x6802\s", ln) and i > 0 and lines[i - 1].strip() == "TBPHSH":
                m = re.match(r"^\s*()((?:(?:0x[0-9A-F]{4}|N/A)\s+)+)", ln)
                name = "TBPHSHR"
            else:
                continue
        else:
            name = m.group(1)
        if name in ("NAME", "Reserved") or name in _SKIP_DS:
            continue
        addrs = re.findall(r"0x[0-9A-F]{4}|N/A", m.group(2))
        if suffix and name.endswith(suffix):
            name = name[:-1]
        if blocks == "CPUTIMER":
            t = re.match(r"TIMER(\d)(\w+)", name)
            if not t:
                continue
            out[(f"CPUTIMER{t.group(1)}", t.group(2))] = int(addrs[0], 16)
            continue
        for blk, a in zip(blocks, addrs):
            if a != "N/A":
                out[(blk, name)] = int(a, 16)
    # AQCSFRC: wrapped name, addresses on the AQCSFR line
    for i, ln in enumerate(lines):
        if ln.strip().startswith("AQCSFR") and "Continuous" in ln:
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            addrs = re.findall(r"0x[0-9A-F]{4}", nxt)
            for blk, a in zip(_TABLES["9-3"][0], addrs):
                out[(blk, "AQCSFRC")] = int(a, 16)
    return out


def check(doc: dict, text: str) -> int:
    ds = parse_ds(text)
    tbl, halves = {}, {}
    for blk, p in doc["peripherals"].items():
        for r in p["registers"]:
            tbl[(blk, r["name"])] = p["base"] + r["offset"]
            if r["width"] == 2:           # CPU timers: the data sheet lists TIM/TIMH, PRD/PRDH as 16-bit halves
                halves[(blk, r["name"] + "H")] = p["base"] + r["offset"] + 1
    bad = 0
    for (blk, name), a in sorted(ds.items()):
        b = tbl.get((blk, name), halves.get((blk, name)))
        if b is None:
            print(f"MISSING in table: {blk}.{name} @ 0x{a:04X}")
            bad += 1
        elif a != b:
            print(f"MISMATCH {blk}.{name}: data sheet 0x{a:04X}, table 0x{b:04X}")
            bad += 1
    covered = {k[0] for k in ds}
    extra = [k for k in tbl if k[0] in covered and k not in ds]
    mbox = [k for k in extra if re.match(r"(MBOX\d+_|LAM\d|MOTS\d|MOTO\d)", k[1])]
    for blk, name in extra:
        if (blk, name) not in mbox:
            print(f"NOTE {blk}.{name} @ 0x{tbl[(blk, name)]:04X}: not in the data sheet table (from the TRM)")
    if mbox:
        print(f"NOTE {len(mbox)} eCAN mailbox / LAM / MOTS / MOTO registers are not in the data sheet tables "
              "(eCAN guide SPRUEU0)")
    print(f"{len(ds)} data sheet registers compared, {bad} mismatch(es)")
    return bad


BOOTROM_ASM = ROOT / "chips" / "tms320f2808_bootrom.asm"


def bootrom_words() -> dict[int, list[int]]:
    """{word address: [words]} of chips/tms320f2808_bootrom.asm (the boot ROM stand-in seeded by `boot.rom_words`)."""
    sys.path.insert(0, str(ROOT))
    from chipem.core.c28x.asm import assemble
    res = assemble(BOOTROM_ASM.read_text(), filename=str(BOOTROM_ASM))
    return {a: [int.from_bytes(d[i:i + 2], "little") for i in range(0, len(d), 2)] for a, d in res.segments}


def bootrom_yaml() -> str:
    out = []
    for a, words in bootrom_words().items():
        out.append(f"    0x{a:06X}: [")
        for i in range(0, len(words), 12):
            out.append("      " + ", ".join(f"0x{w:04X}" for w in words[i:i + 12]) + ",")
        out.append("    ]")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-o", "--output", default=str(DST))
    ap.add_argument("--check-ds", metavar="TXT", help="pdftotext -layout output of the F280x data sheet (SPRS230P)")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--bootrom-words", action="store_true",
                    help="print the `boot.rom_words` YAML of chips/tms320f2808_bootrom.asm and exit")
    a = ap.parse_args(argv)
    if a.bootrom_words:
        print(bootrom_yaml())
        return 0
    doc = build(yaml.safe_load(SRC.read_text()))
    if not a.no_write:
        Path(a.output).write_text(dump(doc))
        print(f"wrote {a.output}")
    if a.check_ds:
        return 1 if check(doc, Path(a.check_ds).read_text(errors="replace")) else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
