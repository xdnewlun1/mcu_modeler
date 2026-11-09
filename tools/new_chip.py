#!/usr/bin/env python3
"""new_chip: scaffold a CHIP_EM chip description (+ register table + test skeleton) from an SVD.

Usage:
    python -I tools/new_chip.py --part STM32F103C8 --package LQFP48 --family stm32 --arch armv7-m --cpu cortex-m3 \
        --svd path/or/https-url.svd [--pins pins.csv] [--mem FLASH:flash:0x08000000:0x10000 ...] [--force]
    python -I tools/new_chip.py --part TMS320F28234 --package LQFP176 --family ti_c2000 \
        --regs chips/tms320f28234_regs.yaml          # no SVD (TI): use a hand-written register table instead

Writes (relative to --root, default: this checkout):
    chips/<part>_regs.yaml       register table, generated with tools/svd2chip.py --flat (only with --svd)
    chips/<part>.yaml            chip YAML skeleton: core, memory, peripherals (models mapped per family, see
                                 FAMILIES below), IRQ table from the SVD, gpio_name_format, pins from --pins or a TODO
    tests/test_<part>_chip.py    test skeleton with the standard assertions (the spot-check test fails until written)
Existing files are never overwritten without --force.  Every guess is marked `TODO` in the output: grep for it.

Model mapping: a peripheral gets a family model when its SVD name matches the rule's regex AND the SVD peripheral
has the registers the model is written against (`needs`).  Otherwise it becomes `stub` (with a TODO comment when
the name matched but the registers did not: a different register layout needs a new model, e.g. STM32F1 GPIO has
CRL/CRH instead of MODER).  Blocks in the Cortex-M private peripheral bus (0xE0000000-0xE00FFFFF) are skipped
unless a rule maps them (the arch layer owns the SCS; the rest is auto-stubbed), and so are blocks that lie inside a
declared memory region (e.g. the Kinetis flash configuration field).

pins.csv columns (header row required): num,name,kind,port,bit,functions[,default]
    functions = "KEY=VALUE;KEY=VALUE" (e.g. "AF7=USART1_TX;AF8=UART4_TX;ADC=ADC123_IN0"); port/bit empty for
    non-GPIO pins; kind one of gpio|analog|power|special.

Downloaded SVDs are untrusted: they are stored under svd/dl-<part>/ and parsed with xml.etree only (svd2chip).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
for p in (str(ROOT), str(TOOLS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import svd2chip  # noqa: E402
import yaml  # noqa: E402


# ------------------------------------------------------------------------------------------------ family tables
@dataclass
class Rule:
    pattern: str                    # full-match regex on the SVD peripheral name
    model: str
    needs: tuple[str, ...] = ()     # registers the model relies on; missing -> stub + TODO
    params: dict | None = None      # static params; "{1}" in a string value = regex group 1, "#1" = int(group 1)
    rename: str | None = None       # chip-YAML name if it differs from the SVD name
    note: str | None = None         # comment written next to the entry

    def match(self, name: str):
        return re.fullmatch(self.pattern, name)


def _p(**kw):
    return kw


FAMILIES: dict[str, dict] = {
    "stm32": {
        "doc": "stm32.* models implement the STM32F4 register layout (RM0090): GPIO MODER/AFR, USART SR/DR/BRR, RCC "
               "PLLCFGR/AHB1ENR.  Other series (F0/F1/F3/F7/L4/G0/H7) differ: check every mapped block.",
        "gpio_name_format": "P{port}{bit}",
        "granule": 0x400,
        "arch": "armv7e-m", "cpu": "cortex-m4", "clock_hz": 16_000_000,
        "clock_note": "reset clock = HSI (16 MHz on F4, 8 MHz on F1/F0): TODO check the RM's reset clock",
        "memory": [("FLASH", "flash", 0x08000000, 0x100000), ("SRAM", "ram", 0x20000000, 0x20000)],
        "extra": {"vtor": 0x08000000, "systick_ext_div": 8},
        "rules": [
            Rule(r"GPIO([A-K])", "stm32.gpio", ("MODER", "AFRL", "IDR", "ODR", "BSRR"), _p(port="{1}")),
            Rule(r"USART(\d+)", "stm32.usart", ("SR", "DR", "BRR", "CR1"), _p(index="#1", kind="usart")),
            Rule(r"UART(\d+)", "stm32.usart", ("SR", "DR", "BRR", "CR1"), _p(index="#1", kind="uart")),
            Rule(r"RCC", "stm32.rcc", ("CR", "PLLCFGR", "CFGR", "AHB1ENR"), _p(ext_hz=8_000_000),
                 note="TODO ext_hz = the board's HSE crystal"),
            Rule(r"TIM([1-8])", "stm32.tim", ("CR1", "PSC", "ARR", "SR", "DIER")),
            Rule(r"TIM(9|1[0-4])", "stub", (),
                 note="TODO stm32.tim models TIM1-8 only (TIM9-14: 1-2 channels, no DIR/CMS); stub until it does"),
            Rule(r"IWDG", "stm32.iwdg", ("KR", "PR", "RLR", "SR")),
            Rule(r"WWDG", "stm32.wwdg", ("CR", "CFR", "SR")),
            Rule(r"EXTI", "stm32.exti", ("IMR", "RTSR", "FTSR", "PR")),
            Rule(r"SYSCFG", "stm32.syscfg", ("EXTICR1",)),
            Rule(r"FLASH", "stm32.flash", ("ACR", "KEYR", "SR", "CR"), note="flash interface registers"),
            Rule(r"PWR", "stm32.pwr", ("CR", "CSR")),
            Rule(r"DBG(MCU)?", "stm32.dbgmcu", ("DBGMCU_IDCODE",),
                 note="TODO DBGMCU_IDCODE: check DEV_ID for this part (stm32.dbgmcu returns 0x413)"),
        ],
    },
    "kinetis": {
        "doc": "kinetis.* models implement the K22F (K22P100M120SF5RM) register layout; KL/KE/KV parts differ.",
        "gpio_name_format": "PT{port}{bit}",
        "granule": 0x1000,
        "arch": "armv7e-m", "cpu": "cortex-m4", "clock_hz": 20_971_520,
        "clock_note": "reset clock: MCG FEI = 32768 * 640; kinetis.mcg recomputes it at reset",
        "memory": [("FLASH", "flash", 0x00000000, 0x100000), ("SRAM_L", "ram", 0x1FFF0000, 0x10000),
                   ("SRAM_U", "ram", 0x20000000, 0x10000)],
        "extra": {},
        "rules": [
            Rule(r"PORT([A-E])", "kinetis.port", ("PCR0", "ISFR"), _p(port="{1}")),
            Rule(r"(?:GPIO|PT)([A-E])", "kinetis.gpio", ("PDOR", "PDDR", "PDIR"), _p(port="{1}")),
            Rule(r"UART(\d+)", "kinetis.uart", ("BDH", "BDL", "C2", "S1", "D"), _p(index="#1")),
            Rule(r"SIM", "kinetis.sim", ("SCGC5", "CLKDIV1")),
            Rule(r"MCG", "kinetis.mcg", ("C1", "C2", "C4", "C5", "C6", "S")),
            Rule(r"OSC0?", "kinetis.osc", ("CR",)),
            Rule(r"WDOG", "kinetis.wdog", ("STCTRLH", "REFRESH", "UNLOCK")),
            Rule(r"PIT", "kinetis.pit", ("MCR", "LDVAL0", "TCTRL0", "TFLG0")),
            Rule(r"FTM\d+", "kinetis.ftm", ("SC", "CNT", "MOD", "C0SC", "C0V", "CNTIN", "COMBINE")),
            Rule(r"LPTMR0", "kinetis.lptmr", ("CSR", "PSR", "CMR", "CNR")),
            Rule(r"RTC", "kinetis.rtc", ("TSR", "TPR", "SR", "CR")),
            Rule(r"FTF[ELA]", "kinetis.ftfe", ("FSTAT", "FCCOB0")),
            Rule(r"ADC(\d+)", "kinetis.adc", ("SC1A", "SC3", "RA"), _p(index="#1")),
            Rule(r"SPI(\d+)", "kinetis.dspi", ("MCR", "SR", "PUSHR", "POPR"), _p(index="#1")),
            Rule(r"DMA", "kinetis.dma", ("CR", "ERQ")),
            Rule(r"DMAMUX0?", "kinetis.dmamux", ("CHCFG0",)),
            Rule(r"PDB0", "kinetis.pdb", ("SC", "MOD", "CNT")),
            Rule(r"SMC", "kinetis.smc", ("PMPROT", "PMCTRL")),
            Rule(r"PMC", "kinetis.pmc", ("REGSC",)),
            Rule(r"RCM", "kinetis.rcm", ("SRS0",)),
            Rule(r"LLWU", "kinetis.llwu", ("F1",)),
            Rule(r"(?:RF)?VBAT", "kinetis.vbat", (), rename="VBAT", note="battery-backed register file (plain RAM)"),
            Rule(r"RFSYS", "ram", ()),
        ],
        # blocks the SVD does not list: (SVD block, its base, alias name, model, alias base, size, params, note).
        # K22 FTM2 is also decoded at 0x4003A000 (K22P100M120SF5RM ch. 4 memory map; FTM2 slot on AIPS0)
        "aliases": [
            ("FTM2", 0x400B8000, "FTM2_ALIAS", "kinetis.ftm_alias", 0x4003A000, 0x1000, {"target": "FTM2"},
             "second address window of FTM2 (not in the SVD)"),
        ],
    },
    "ti_c2000": {
        "doc": "ti_c2000.* models implement the F2833x/F2823x layout (SPRUFB0); 16-bit address units.",
        "gpio_name_format": "GPIO{num}",
        "granule": None,
        "arch": "c28x", "cpu": "c28x", "clock_hz": 100_000_000, "backend": "c28x",
        "clock_note": "nominal; ti_c2000.sysctrl derives SYSCLKOUT from ext_hz/PLLCR/DIVSEL",
        "memory": [("M0", "ram", 0x000000, 0x0400), ("M1", "ram", 0x000400, 0x0400)],
        "extra": {"boot": {"entry": 0x33FFF6, "sp": 0x0400}},
        "rules": [
            Rule(r"GPIO", "ti_c2000.gpio", ("GPAMUX1", "GPADIR")),
            Rule(r"SCI([A-C])", "ti_c2000.sci", ("SCICCR", "SCICTL1", "SCITXBUF", "SCIRXBUF")),
            Rule(r"CPUTIMER(\d)", "ti_c2000.cputimer", ("TIM", "PRD", "TCR"), _p(index="#1")),
            Rule(r"SYSCTRL", "ti_c2000.sysctrl", ("PLLSTS", "PLLCR", "WDCR"), _p(ext_hz=20_000_000, boot_divsel=2),
                 note="TODO ext_hz = the board's crystal/oscillator"),
            Rule(r"ADC", "ti_c2000.adc", ("ADCTRL1", "ADCTRL2")),
            Rule(r"ADCMIRROR", "ti_c2000.adcmirror", ("ADCRESULT0",)),
            Rule(r"XINT", "ti_c2000.xint", ("XINT1CR",)),
            Rule(r"SPI([A-D])", "ti_c2000.spi", ("SPICCR", "SPITXBUF")),
            Rule(r"FLASH", "ti_c2000.flash", ("FOPT",), _p(regs="FLASH"), rename="FLASHREGS"),
            Rule(r"CSM", "ti_c2000.csm", ("CSMSCR",)),
            Rule(r"PIE", "ti_c2000.pie", ("PIECTRL",), note="arch-owned (chipem/arch/c28x), incl. vectors"),
            Rule(r"EPWM(\d)", "ti_c2000.epwm", ("TBCTL", "TBPRD", "CMPA", "AQCTLA", "DBCTL", "ETSEL"),
                 _p(gate="{0}ENCLK", index="#1")),
            Rule(r"(?:ECAP|EQEP)\d|ECAN[AB]|MCBSP[AB]|I2CA|DMA", "ti_c2000.storage", (), _p(gate="{0}ENCLK"),
                 note="storage only; gate = PCLKCRx bit (TODO verify the name)"),
            Rule(r".*", "ti_c2000.storage", (), note="storage only"),
        ],
    },
}
GENERIC = {"doc": "new family: every block starts as `stub`", "gpio_name_format": "P{port}{bit}", "granule": 0x400,
           "arch": "armv7-m", "cpu": "cortex-m3", "clock_hz": 8_000_000, "clock_note": "TODO reset clock",
           "memory": [("FLASH", "flash", 0x00000000, 0x10000), ("SRAM", "ram", 0x20000000, 0x5000)],
           "extra": {}, "rules": []}
PPB = (0xE0000000, 0xE0100000)          # Cortex-M private peripheral bus


def family_table(family: str) -> dict:
    return FAMILIES.get(family, GENERIC)


@dataclass
class Periph:
    name: str
    model: str
    base: int
    size: int
    params: dict = field(default_factory=dict)
    note: str = ""


def map_model(family: str, name: str, regs: list[str]) -> tuple[str, dict, str, str]:
    """(model, params, chip-YAML name, note) for an SVD peripheral `name` with register names `regs`."""
    have = set(regs)
    for rule in family_table(family)["rules"]:
        m = rule.match(name)
        if not m:
            continue
        missing = [r for r in rule.needs if r not in have]
        if missing:
            return ("stub", {}, name, f"TODO {rule.model} needs {'/'.join(missing)} (not in the register table): "
                                      f"different layout, write a model or keep the stub")
        params = {}
        for k, v in (rule.params or {}).items():
            if isinstance(v, str) and v.startswith("#"):
                v = int(m.group(int(v[1:])))
            elif isinstance(v, str) and "{" in v:
                v = v.format(*([m.group(0)] + list(m.groups())))
            params[k] = v
        return rule.model, params, rule.rename or name, rule.note or ""
    return "stub", {}, name, ""


def _round_up(v: int, g: int) -> int:
    return (v + g - 1) // g * g


def plan_peripherals(family: str, table: dict, memory: list[tuple], exclude: set[str]) -> list[Periph]:
    """Chip-YAML peripheral entries from a regtable dict (svd2chip schema)."""
    fam = family_table(family)
    g = fam["granule"]
    arm = fam["arch"] != "c28x"
    items = sorted(((n, p) for n, p in (table.get("peripherals") or {}).items()), key=lambda kv: kv[1]["base"])
    bases = [p["base"] for _, p in items]
    out: list[Periph] = []
    for i, (name, p) in enumerate(items):
        if name in exclude:
            continue
        base, extent = int(p["base"]), int(p.get("size") or 0x400)
        regs = [r["name"] for r in p.get("registers") or []]
        model, params, yname, note = map_model(family, name, regs)
        if arm and PPB[0] <= base < PPB[1] and model == "stub":
            continue                            # SCS (arch-owned) or auto-stubbed debug blocks
        if any(mb <= base < mb + ms for (_, _, mb, ms) in memory):
            continue                            # lives inside a memory region (e.g. flash config field)
        nxt = next((b for b in bases[i + 1:] if b > base), None)
        size = extent if not g else _round_up(base + extent, g) - base
        if nxt is not None and base + size > nxt:
            size = max(nxt - base, 1)
            if size < extent:
                note = (note + "; " if note else "") + (f"TODO SVD extent {extent:#x} overlaps the next block at "
                                                       f"{nxt:#x}: check the RM memory map")
        out.append(Periph(yname, model, base, size, params, note))
    have = {(p.name, p.base, p.model) for p in out}
    for src, sbase, aname, amodel, abase, asize, aparams, anote in fam.get("aliases", ()):
        if any(n == src and b == sbase and m != "stub" for n, b, m in have) and aname not in exclude \
                and not any(p.base <= abase < p.base + p.size for p in out):
            out.append(Periph(aname, amodel, abase, asize, dict(aparams), anote))
    out.sort(key=lambda p: p.base)
    return out


# ------------------------------------------------------------------------------------------------ YAML text
_PLAIN = re.compile(r"[A-Za-z_][A-Za-z0-9_./+\-]*\Z")
_RESERVED = {"y", "n", "yes", "no", "true", "false", "on", "off", "null", "~"}


def ys(v) -> str:
    """A YAML scalar for flow context."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    s = str(v)
    if _PLAIN.match(s) and s.lower() not in _RESERVED:
        return s
    return json.dumps(s)


def flow(d: dict, hexkeys: tuple[str, ...] = ()) -> str:
    parts = []
    for k, v in d.items():
        if isinstance(v, dict):
            parts.append(f"{k}: {flow(v, hexkeys)}")
        elif isinstance(v, int) and not isinstance(v, bool) and k in hexkeys:
            parts.append(f"{k}: 0x{v:04X}" if v < 0x10000 else f"{k}: 0x{v:08X}")
        else:
            parts.append(f"{k}: {ys(v)}")
    return "{" + ", ".join(parts) + "}"


def read_pins_csv(path: Path, family: str) -> list[dict]:
    pins = []
    with open(path, newline="") as f:
        rd = csv.DictReader(f)
        need = {"num", "name", "kind", "port", "bit", "functions"}
        if not need <= set(rd.fieldnames or ()):
            raise SystemExit(f"new_chip: {path}: header must contain {', '.join(sorted(need))} "
                             f"(got {rd.fieldnames})")
        for i, row in enumerate(rd, 2):
            kind = (row["kind"] or "gpio").strip()
            if kind not in ("gpio", "analog", "power", "special"):
                raise SystemExit(f"new_chip: {path}:{i}: bad kind {kind!r}")
            pin = {"num": int(row["num"]), "name": row["name"].strip(), "kind": kind}
            port, bit = (row["port"] or "").strip(), (row["bit"] or "").strip()
            if port or bit:
                pin["port"], pin["bit"] = port.upper(), int(bit)
            funcs = {}
            for item in (row["functions"] or "").split(";"):
                if item.strip():
                    if "=" not in item:
                        raise SystemExit(f"new_chip: {path}:{i}: function {item!r} is not KEY=VALUE")
                    k, v = item.split("=", 1)
                    funcs[k.strip()] = v.strip()
            default = (row.get("default") or "").strip()
            if not default:
                default = ("GPIO" if family in ("stm32", "ti_c2000") else funcs.get("ALT0", "DISABLED")) \
                    if kind == "gpio" else pin["name"]
            pin["default"] = default
            if funcs:
                pin["functions"] = funcs
            pins.append(pin)
    return pins


def pin_line(p: dict) -> str:
    d = {k: p[k] for k in ("num", "name", "kind", "port", "bit", "default") if k in p}
    s = flow(d)
    if p.get("functions"):
        s = s[:-1] + ", functions: " + flow(p["functions"]) + "}"
    return f"  - {s}"


def chip_yaml(a, fam: dict, periphs: list[Periph], irqs: dict[int, str], unit_bits: int, fpu: bool,
              pins: list[dict] | None, regs_name: str | None, source: str, memory: list[tuple],
              mem_from_args: bool) -> str:
    part = a.part
    expected = re.search(r"(\d+)", a.package or "")
    arch = a.arch or fam["arch"]
    cpu = a.cpu or fam["cpu"]
    backend = "c28x" if arch == "c28x" else "unicorn"
    clock = float(a.clock_hz) if a.clock_hz else fam["clock_hz"]
    num_irqs = 0 if arch == "c28x" else (max(irqs) + 1 if irqs else 32)
    L = [f"# {part} ({a.package or 'TODO package'}): generated by tools/new_chip.py -- TODO review every line.",
         f"# Sources: {source}",
         "# TODO: data sheet (title, revision, pin table / AF table numbers), reference manual (memory map, IRQ table).",
         f"# Family note: {fam['doc']}",
         f"name: {ys(part)}",
         f"package: {ys(a.package or 'TODO')}",
         f"family: {ys(a.family)}"]
    if regs_name:
        L.append(f"regs_file: {regs_name}")
    L.append(f"gpio_name_format: {json.dumps(fam['gpio_name_format'])}")
    L.append(f"# clock_hz: {fam['clock_note']}")
    core = {"arch": arch, "backend": backend, "cpu": cpu, "fpu": fpu, "clock_hz": int(clock),
            "addr_unit_bits": unit_bits, "num_irqs": num_irqs}
    L.append(f"core: {flow(core)}")
    for k, v in fam["extra"].items():
        if isinstance(v, dict):
            L.append(f"{k}: {flow(v, hexkeys=('entry', 'sp'))}    # TODO verify")
        else:
            L.append(f"{k}: 0x{v:08X}    # TODO verify" if k == "vtor" else f"{k}: {ys(v)}    # TODO verify")
    if arch == "c28x":
        L.append("# auto_stub_ranges: [[0x0800, 0x0FFF], [0x5000, 0x7FFF]]   # TODO peripheral frames (word addresses)")
    L += ["", "memory:"]
    for (n, kind, base, size) in memory:
        tail = "" if mem_from_args else "    # TODO base/size from the data sheet memory map"
        L.append(f"  - {{name: {n}, base: 0x{base:08X}, size: {size:#x}, kind: {kind}}}{tail}")
    L += ["", "# armv7m.scs (0xE000E000, SysTick/NVIC/SCB) is created by the arch layer, not listed here." if arch != "c28x"
          else "# Addresses and sizes count 16-bit words.",
          "peripherals:" if periphs else "peripherals: []    # TODO from the data sheet memory map / register table"]
    for p in periphs:
        d = {"name": p.name, "model": p.model, "base": p.base, "size": p.size}
        if p.params:
            d["params"] = p.params
        s = "  - " + flow(d, hexkeys=("base", "size"))
        if p.note:
            s += f"   # {p.note}"
        L.append(s)
    L += ["", "# External interrupt numbers (exception = 16 + n) from the register table." if arch != "c28x"
          else "# PIE vector numbers (INTx.y = 32 + 8(x-1) + (y-1)).", "irqs:"]
    if irqs:
        gaps = [n for n in range(max(irqs) + 1) if n not in irqs] if arch != "c28x" else []
        if gaps:
            L.append(f"  # TODO the table defines no interrupt {gaps}: check the RM vector table (SVDs drop some)")
        for n, nm in sorted(irqs.items()):
            L.append(f"  {n}: {ys(nm)}")
    else:
        L[-1] = "irqs: {}    # TODO IRQ table from the reference manual (vector table)"
    L.append("")
    if pins:
        L.append(f"# {len(pins)} package pins (from {Path(a.pins).name}).  TODO: cross-check against the pinout figure.")
        L.append("pins:")
        L += [pin_line(p) for p in sorted(pins, key=lambda p: p["num"])]
    else:
        n = expected.group(1) if expected else "?"
        L.append(f"# TODO all {n} package pins (data sheet pin table; see docs/AI_NEW_CHIP_PLAYBOOK.md section B):")
        L.append("#   - {num: 1, name: PE2, kind: gpio, port: E, bit: 2, default: GPIO, functions: {AF0: TRACECLK}}")
        L.append("pins: []")
    return "\n".join(L) + "\n"


TEST_TEMPLATE = '''"""{part}: chip description checks.  Generated by tools/new_chip.py: replace every TODO; the spot-check test
fails until it holds real assertions from the data sheet."""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

import chipem
from chipem.chip import Chip
from chipem.chipdesc import load_chip

ROOT = Path(__file__).resolve().parent.parent
YAML = ROOT / "chips" / "{stem}.yaml"
TOOLS = Path(chipem.__file__).resolve().parent.parent / "tools"
SVD = [p for p in ([ROOT / {svd_rel!r}] if {svd_rel!r} else []) if p.exists()]   # svd/dl-*/ (committed; skipped if absent)
EXPECTED_PINS = {expected}          # package pin count
EXPECTED_GPIO = {n_gpio}            # TODO: GPIO-capable pins per the data sheet


def _tool(name: str):
    sys.path.insert(0, str(TOOLS))
    try:
        return __import__(name)
    finally:
        sys.path.pop(0)


@pytest.fixture(scope="module")
def desc():
    return load_chip(YAML)


def test_core_and_package(desc):
    assert desc.name == {part!r} and desc.package == {package!r} and desc.family == {family!r}
    assert desc.core.arch == {arch!r} and desc.core.cpu == {cpu!r}
    assert desc.core.fpu is {fpu} and desc.core.addr_unit_bits == {unit_bits}
    assert desc.core.clock_hz == {clock_hz}         # reset clock, not the maximum
    assert desc.gpio_name_format == {fmt!r}


def test_pin_table_checker(desc):
    rep = _tool("pin_table_check").check_desc(desc, EXPECTED_PINS)
    assert rep.ok, rep.text()


def test_pin_count_numbers_and_gpio(desc):
    assert len(desc.pins) == EXPECTED_PINS
    assert sorted(p.num for p in desc.pins) == list(range(1, EXPECTED_PINS + 1))
    gpio = [p for p in desc.pins if p.kind == "gpio"]
    assert all(p.port is not None and p.bit is not None for p in gpio)
    assert len({{p.gpio_name for p in gpio}}) == len(gpio) == EXPECTED_GPIO


def test_pin_spot_checks(desc):
    # TODO: 8-15 assertions copied from the data sheet, e.g. (STM32F405ZG):
    #   assert desc.pin("PA9").num == 101 and desc.pin("PA9").functions["AF7"] == "USART1_TX"
    #   assert desc.pin(25).name == "NRST" and desc.pin(25).kind == "special"
    # Cover: the console UART TX/RX, an LED/button pin, a pin with a long multi-function name, the reset pin,
    # every power-pin kind, the first and the last pin, and any data sheet typo you corrected.
    pytest.fail("TODO: pin spot checks from the data sheet")


def test_memory_and_irqs(desc):
    for m in desc.memory:
        assert m.base % 0x400 == 0 and m.size % 0x400 == 0, m.name       # Unicorn 1 KB pages
    assert desc.irqs, "IRQ table is empty"
    # TODO: memory sizes and 3-5 IRQ numbers from the reference manual, e.g. desc.irqs[37] == "USART1"


@pytest.mark.skipif(not SVD, reason="SVD not downloaded")
def test_svd_check_zero_mismatches():
    out = io.StringIO()
    assert _tool("svd2chip").check(SVD[0], YAML, out=out) == 0, out.getvalue()


def test_chip_builds_without_stub_fallbacks(desc):
    chip = Chip(desc)
    bad = [e.fields.get("msg", "") for e in chip.log if e.kind == "warn" and "not available" in e.fields.get("msg", "")]
    assert not bad, bad
    fam = sys.modules.get(f"chipem.periph.{{desc.family}}")
    assert getattr(fam, "IMPORT_ERRORS", {{}}) == {{}}
'''


def fetch_svd(src: str, part: str, root: Path) -> Path:
    if not re.match(r"https://", src):
        p = Path(src)
        if not p.exists():
            raise SystemExit(f"new_chip: SVD {src} not found (give a path or an https:// URL)")
        return p
    dest_dir = root / "svd" / f"dl-{part.lower()}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9_.\-]", "_", src.rsplit("/", 1)[-1]) or "device.svd"
    dest = dest_dir / name
    if not dest.exists():
        with urllib.request.urlopen(src, timeout=60) as r:          # noqa: S310 - https only, see above
            data = r.read(64 << 20)
        dest.write_bytes(data)
    print(f"svd: {dest} (sha256 {hashlib.sha256(dest.read_bytes()).hexdigest()}) from {src}", file=sys.stderr)
    return dest


def parse_mem(spec: str) -> tuple:
    try:
        n, kind, base, size = spec.split(":")
        return (n, kind, int(base, 0), int(size, 0))
    except ValueError:
        raise SystemExit(f"new_chip: --mem {spec!r}: expected NAME:KIND:BASE:SIZE (e.g. FLASH:flash:0x08000000:0x10000)")


def write(path: Path, text: str, force: bool, written: list) -> None:
    if path.exists() and not force:
        raise SystemExit(f"new_chip: {path} exists (use --force to overwrite)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    written.append(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--part", required=True, help="part number, e.g. STM32F103C8 (file stem = lower case)")
    ap.add_argument("--package", default="", help="package, e.g. LQFP48 (its number is the expected pin count)")
    ap.add_argument("--family", required=True, help=f"peripheral family: {', '.join(FAMILIES)} or a new name (all stubs)")
    ap.add_argument("--arch", default=None, help="armv6-m | armv7-m | armv7e-m | c28x (default: family's)")
    ap.add_argument("--cpu", default=None, help="cortex-m0 | cortex-m0plus | cortex-m3 | cortex-m4 | cortex-m7 | c28x")
    fp = ap.add_mutually_exclusive_group()
    fp.add_argument("--fpu", dest="fpu", action="store_true", default=None, help="core has an FPU")
    fp.add_argument("--no-fpu", dest="fpu", action="store_false", help="core has no FPU")
    ap.add_argument("--clock-hz", type=float, default=None, help="core clock after reset (not the maximum)")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--svd", help="CMSIS-SVD file or https:// URL (cached under svd/dl-<part>/)")
    src.add_argument("--regs", help="existing register table YAML (svd2chip schema; e.g. hand-written for TI)")
    ap.add_argument("--source", default=None, help="provenance for the regs header (URL @ commit, SVD version)")
    ap.add_argument("--pins", help="pins.csv: num,name,kind,port,bit,functions[,default]")
    ap.add_argument("--mem", action="append", default=[], metavar="NAME:KIND:BASE:SIZE",
                    help="memory region (repeatable); default: the family's typical map, marked TODO")
    ap.add_argument("--exclude", default="", help="comma-separated SVD peripherals to leave out (not on this part)")
    ap.add_argument("--root", default=str(ROOT), help="output root (chips/, tests/ and svd/ below it)")
    ap.add_argument("--force", action="store_true", help="overwrite existing files")
    a = ap.parse_args(argv)

    root = Path(a.root)
    stem = re.sub(r"[^a-z0-9_]", "_", a.part.lower())
    fam = family_table(a.family)
    if a.family not in FAMILIES:
        print(f"new_chip: family {a.family!r} is new: every peripheral is a stub; see the playbook section E",
              file=sys.stderr)
    written: list[Path] = []
    regs_name = None
    svd_path = None
    if a.svd:
        svd_path = fetch_svd(a.svd, a.part, root)
        table = svd2chip.convert(svd_path, flat=True)
        regs_name = f"{stem}_regs.yaml"
        source = a.source or f"{a.svd} (SVD version {table.get('version')})"
        write(root / "chips" / regs_name, svd2chip.emit_yaml(table, source), a.force, written)
        unit_bits = int(table.get("address_unit_bits") or 8)
        svd_fpu = (table.get("cpu") or {}).get("fpu")
        irqs = {int(k): str(v) for k, v in (table.get("interrupts") or {}).items()}
    elif a.regs:
        rp = Path(a.regs)
        table = yaml.load(rp.read_text(), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
        try:
            regs_name = str(rp.resolve().relative_to((root / "chips").resolve()))
        except ValueError:
            regs_name = str(rp.resolve())
        source = a.source or f"register table {a.regs}"
        unit_bits = int(table.get("address_unit_bits") or 8)
        svd_fpu = (table.get("cpu") or {}).get("fpu")
        irqs = {int(k): str(v) for k, v in (table.get("interrupts") or {}).items()}
    else:
        table, source, unit_bits, svd_fpu, irqs = {}, "TODO (no SVD / register table given)", 8, None, {}
    fpu = a.fpu if a.fpu is not None else bool(svd_fpu) if svd_fpu is not None else \
        (a.cpu or fam["cpu"]) in ("cortex-m4", "cortex-m7")
    memory = [parse_mem(m) for m in a.mem] or list(fam["memory"])
    exclude = {x.strip() for x in a.exclude.split(",") if x.strip()}
    periphs = plan_peripherals(a.family, table, memory, exclude)
    pins = read_pins_csv(Path(a.pins), a.family) if a.pins else None

    text = chip_yaml(a, fam, periphs, irqs, unit_bits, fpu, pins, regs_name, source, memory, bool(a.mem))
    yaml.safe_load(text)                                   # must at least parse
    write(root / "chips" / f"{stem}.yaml", text, a.force, written)

    m = re.search(r"(\d+)", a.package or "")
    n_gpio = sum(1 for p in pins or [] if p["kind"] == "gpio") or "None  # TODO"
    svd_rel = ""
    if svd_path is not None:
        try:
            svd_rel = str(svd_path.resolve().relative_to(root.resolve()))
        except ValueError:
            svd_rel = str(svd_path.resolve())
    arch = a.arch or fam["arch"]
    test = TEST_TEMPLATE.format(part=a.part, stem=stem, package=a.package or "TODO", family=a.family, arch=arch,
                                cpu=a.cpu or fam["cpu"], fpu=fpu, unit_bits=unit_bits,
                                clock_hz=float(a.clock_hz) if a.clock_hz else float(fam["clock_hz"]),
                                fmt=fam["gpio_name_format"], expected=int(m.group(1)) if m else "None  # TODO",
                                n_gpio=n_gpio, svd_rel=svd_rel)
    write(root / "tests" / f"test_{stem}_chip.py", test, a.force, written)

    counts: dict[str, int] = {}
    for p in periphs:
        counts[p.model] = counts.get(p.model, 0) + 1
    todo = sum(1 for p in periphs if "TODO" in p.note)
    for w in written:
        print(f"wrote {w}")
    print(f"{len(periphs)} peripherals ({', '.join(f'{k} {v}' for k, v in sorted(counts.items()))}), "
          f"{len(irqs)} IRQs, {len(pins or [])} pins; {todo} peripheral TODOs.  Next: grep TODO "
          f"{root / 'chips' / (stem + '.yaml')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
