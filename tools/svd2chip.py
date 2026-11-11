#!/usr/bin/env python3
"""svd2chip: convert a CMSIS-SVD file into a CHIP_EM register table (chips/<part>_regs.yaml).

Usage:
    python tools/svd2chip.py <in.svd> -o chips/<name>_regs.yaml [--peripherals SIM,PORTA,...] [--flat]
    python tools/svd2chip.py <in.svd> --check chips/<part>.yaml        # compare chip YAML bases/IRQs vs SVD

The SVD is treated as untrusted data: it is parsed with xml.etree only (no DTD/entity expansion
beyond what the stdlib parser does by default, no code execution).  See chips/README.md for the
output schema.

Supported SVD features: device/peripheral/cluster/register property inheritance (size, access,
resetValue, resetMask), <dim>/<dimIncrement>/<dimIndex> arrays of registers, clusters and fields
(``NAME%s`` lists and ``NAME[%s]`` arrays), clusters (nested, flattened into ``CLUSTER_REG`` names),
derivedFrom on peripherals, clusters, registers, fields and enumeratedValues, bitOffset/bitWidth,
lsb/msb and bitRange field positions, enumeratedValues (names only), hex/decimal/#binary numbers.
"""
from __future__ import annotations

import argparse
import copy
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ACCESS_VALUES = ("read-only", "write-only", "read-write", "writeOnce", "read-writeOnce")


# --------------------------------------------------------------------------------------------- numbers
def parse_int(text: str | None, default: int | None = None) -> int | None:
    """Parse an SVD scaledNonNegativeInteger: 0x.., 0X.., #binary, 0b.., decimal, optional k/M/G."""
    if text is None:
        return default
    s = text.strip().replace("_", "")
    if not s:
        return default
    mult = 1
    if s[-1] in "kKmMgGtT" and not s.lower().startswith("0x"):
        mult = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}[s[-1].lower()]
        s = s[:-1]
    if s.startswith("+"):
        s = s[1:]
    if s.lower().startswith("0x"):
        return int(s[2:], 16) * mult
    if s.startswith("#"):
        return int(s[1:], 2) * mult
    if s.lower().startswith("0b"):
        return int(s[2:], 2) * mult
    return int(s, 10) * mult


def parse_enum_value(text: str | None) -> list[int]:
    """Enumerated values may contain don't-care bits ('#1x0'); expand them to all matching ints."""
    if text is None:
        return []
    s = text.strip().lower()
    if s.startswith("#") or (s.startswith("0b") and "x" in s[2:]):
        bits = s[1:] if s.startswith("#") else s[2:]
        if "x" not in bits:
            return [int(bits, 2)]
        out = [0]
        for b in bits:
            if b == "x":
                out = [v << 1 for v in out] + [(v << 1) | 1 for v in out]
            else:
                out = [(v << 1) | int(b) for v in out]
        return sorted(out)
    return [parse_int(s)]


def _txt(el: ET.Element | None, tag: str) -> str | None:
    if el is None:
        return None
    c = el.find(tag)
    return None if c is None or c.text is None else c.text.strip()


# --------------------------------------------------------------------------------------------- dim
def dim_indices(el: ET.Element) -> list[str] | None:
    dim = parse_int(_txt(el, "dim"))
    if dim is None:
        return None
    idx = _txt(el, "dimIndex")
    if idx is None:
        return [str(i) for i in range(dim)]
    m = re.fullmatch(r"\s*([0-9]+)\s*-\s*([0-9]+)\s*", idx)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return [str(i) for i in range(a, b + 1)]
    m = re.fullmatch(r"\s*([A-Z])\s*-\s*([A-Z])\s*", idx)
    if m:
        return [chr(c) for c in range(ord(m.group(1)), ord(m.group(2)) + 1)]
    return [x.strip() for x in idx.split(",")]


def expand_dim(el: ET.Element, name: str) -> list[tuple[str, int]]:
    """Return [(expanded_name, address_delta)] for a (possibly dim'ed) element."""
    idx = dim_indices(el)
    if idx is None:
        return [(name, 0)]
    inc = parse_int(_txt(el, "dimIncrement"), 0)
    out = []
    for i, ix in enumerate(idx):
        if "[%s]" in name:
            n = name.replace("[%s]", ix)          # arrays: NAME[%s] -> NAME0, NAME1, ...
        else:
            n = name.replace("%s", ix)
        out.append((n, i * inc))
    return out


# --------------------------------------------------------------------------------------------- props
PROP_TAGS = {"size": "size", "access": "access", "resetValue": "reset", "resetMask": "reset_mask"}


def inherit_props(parent: dict, el: ET.Element) -> dict:
    p = dict(parent)
    for tag, key in PROP_TAGS.items():
        t = _txt(el, tag)
        if t is not None:
            p[key] = t if key == "access" else parse_int(t)
    return p


def merge_derived(el: ET.Element, base: ET.Element) -> ET.Element:
    """SVD derivedFrom: element = copy of base with el's own children overriding same-tag children."""
    merged = copy.deepcopy(base)
    own_tags = {c.tag for c in el}
    for c in list(merged):
        if c.tag in own_tags and c.tag not in ("registers", "fields"):
            merged.remove(c)
    for c in el:
        if c.tag in ("registers", "fields") and merged.find(c.tag) is not None:
            # container elements: append/override children by name
            dst = merged.find(c.tag)
            names = {_txt(x, "name"): x for x in dst}
            for child in c:
                n = _txt(child, "name")
                if n in names:
                    dst.remove(names[n])
                dst.append(copy.deepcopy(child))
        else:
            merged.append(copy.deepcopy(c))
    # dim from the base is not inherited when the derived element is a plain element
    if el.find("dim") is None and base.find("dim") is not None and el.find("name") is not None:
        for tag in ("dim", "dimIncrement", "dimIndex"):
            x = merged.find(tag)
            if x is not None:
                merged.remove(x)
    merged.attrib.pop("derivedFrom", None)
    return merged


# --------------------------------------------------------------------------------------------- parser
class SvdParser:
    def __init__(self, root: ET.Element, enums: str = "auto"):
        self.enums = enums
        if root.tag != "device":
            raise ValueError(f"not a CMSIS-SVD file: root element is <{root.tag}>, expected <device>")
        self.root = root
        self.periph_el: dict[str, ET.Element] = {}
        pers = root.find("peripherals")
        if pers is None:
            raise ValueError("SVD has no <peripherals>")
        for p in pers.findall("peripheral"):
            self.periph_el[_txt(p, "name")] = p
        self.enum_defs: dict[str, ET.Element] = {}
        for ev in root.iter("enumeratedValues"):
            n = _txt(ev, "name")
            if n and ev.get("derivedFrom") is None:
                self.enum_defs.setdefault(n, ev)
        self.dev_props = inherit_props({"size": 32, "access": "read-write", "reset": 0, "reset_mask": None},
                                       root)

    # ------------------------------------------------------------------ device level
    def device(self) -> dict:
        cpu = self.root.find("cpu")
        out = {"device": _txt(self.root, "name"), "version": _txt(self.root, "version"),
               "address_unit_bits": parse_int(_txt(self.root, "addressUnitBits"), 8)}
        if cpu is not None:
            out["cpu"] = {
                "name": _txt(cpu, "name"),
                "fpu": (_txt(cpu, "fpuPresent") or "false").lower() in ("true", "1"),
                "nvic_prio_bits": parse_int(_txt(cpu, "nvicPrioBits")),
            }
        return out

    def interrupts(self, names: set[str] | None = None) -> dict[int, str]:
        irqs: dict[int, str] = {}
        for pname, p in self.periph_el.items():
            for i in p.findall("interrupt"):
                n = parse_int(_txt(i, "value"))
                if n is not None:
                    irqs.setdefault(n, _txt(i, "name"))
        return dict(sorted(irqs.items()))

    # ------------------------------------------------------------------ peripherals
    def resolved_peripheral_el(self, name: str, _seen=()) -> ET.Element:
        el = self.periph_el[name]
        d = el.get("derivedFrom")
        if not d:
            return el
        if d in _seen or d not in self.periph_el:
            raise ValueError(f"peripheral {name}: bad derivedFrom {d!r}")
        return merge_derived(el, self.resolved_peripheral_el(d, _seen + (name,)))

    def peripheral(self, name: str, flat: bool) -> dict:
        el = self.periph_el[name]
        derived = el.get("derivedFrom")
        full = self.resolved_peripheral_el(name)
        props = inherit_props(self.dev_props, full)
        base = parse_int(_txt(el, "baseAddress"))
        if base is None:
            base = parse_int(_txt(full, "baseAddress"), 0)
        if flat or not derived:
            regs = self.registers_of(full, props)
        else:
            # non-flat: only registers the derived peripheral itself defines (additions/overrides)
            own = el.find("registers")
            regs = self.registers_of(el, props, regs_parent=own) if own is not None else []
        size = None
        blocks = full.findall("addressBlock")
        if blocks:
            size = max(parse_int(_txt(b, "offset"), 0) + parse_int(_txt(b, "size"), 0) for b in blocks)
        if not size:
            all_regs = regs if regs or not derived else self.registers_of(full, props)
            end = max((r["offset"] + r["width"] for r in all_regs), default=1)
            size = (end + 0xFFF) & ~0xFFF
        out = {"base": base, "size": size, "derived_from": (None if flat else derived)}
        desc = _txt(full, "description")
        if desc:
            out["description"] = " ".join(desc.split())
        grp = _txt(full, "groupName")
        if grp:
            out["group"] = grp
        irqs = {parse_int(_txt(i, "value")): _txt(i, "name") for i in el.findall("interrupt")}
        if irqs:
            out["interrupts"] = dict(sorted(irqs.items()))
        out["registers"] = regs
        return out

    # ------------------------------------------------------------------ registers / clusters
    def registers_of(self, pel: ET.Element, props: dict, regs_parent: ET.Element | None = None) -> list[dict]:
        container = regs_parent if regs_parent is not None else pel.find("registers")
        out: list[dict] = []
        if container is not None:
            self._walk(container, props, 0, "", out)
        out.sort(key=lambda r: (r["offset"], r["name"]))
        return out

    def _sibling(self, container: ET.Element, ref: str) -> ET.Element | None:
        last = ref.split(".")[-1]
        for c in container:
            if c.tag in ("register", "cluster") and _txt(c, "name") == last:
                return c
        # dotted path from peripheral: PERIPH.REG or PERIPH.CLUSTER.REG
        parts = ref.split(".")
        if len(parts) >= 2 and parts[0] in self.periph_el:
            node = self.resolved_peripheral_el(parts[0]).find("registers")
            for part in parts[1:]:
                nxt = None
                for c in (node if node is not None else []):
                    if c.tag in ("register", "cluster") and _txt(c, "name") == part:
                        nxt = c
                        break
                if nxt is None:
                    return None
                node = nxt
            return node
        return None

    def _walk(self, container: ET.Element, props: dict, base_off: int, prefix: str, out: list[dict]):
        for el in container:
            if el.tag not in ("register", "cluster"):
                continue
            d = el.get("derivedFrom")
            if d:
                src = self._sibling(container, d)
                if src is None:
                    raise ValueError(f"{el.tag} {_txt(el, 'name')}: derivedFrom {d!r} not found")
                el = merge_derived(el, src)
            name = _txt(el, "name")
            off = parse_int(_txt(el, "addressOffset"), 0)
            p = inherit_props(props, el)
            if el.tag == "cluster":
                for n, delta in expand_dim(el, name):
                    self._walk(el, p, base_off + off + delta, f"{prefix}{n}_", out)
                continue
            for n, delta in expand_dim(el, name):
                out.append(self._register(el, p, prefix + n, base_off + off + delta))

    def _register(self, el: ET.Element, p: dict, name: str, offset: int) -> dict:
        size_bits = p["size"] or 32
        width = max(1, size_bits // 8)
        full_mask = (1 << size_bits) - 1
        reset_mask = full_mask if p["reset_mask"] is None else p["reset_mask"] & full_mask
        reg = {"name": name, "offset": offset, "width": width,
               "reset": (p["reset"] or 0) & reset_mask, "reset_mask": reset_mask,
               "access": p["access"] or "read-write"}
        fields_el = el.find("fields")
        fields = []
        if fields_el is not None:
            for f in fields_el.findall("field"):
                fd = f.get("derivedFrom")
                if fd:
                    src = next((x for x in fields_el.findall("field") if _txt(x, "name") == fd.split(".")[-1]),
                               None)
                    if src is not None:
                        f = merge_derived(f, src)
                fname = _txt(f, "name")
                for fn, delta in expand_dim(f, fname):
                    fields.append(self._field(f, fn, delta, reg["access"]))
        fields.sort(key=lambda x: x["lsb"])
        reg["fields"] = fields
        return reg

    def _field(self, f: ET.Element, name: str, delta: int, reg_access: str) -> dict:
        lsb = width = None
        if _txt(f, "bitOffset") is not None:
            lsb = parse_int(_txt(f, "bitOffset"))
            width = parse_int(_txt(f, "bitWidth"), 1)
        elif _txt(f, "lsb") is not None:
            lsb = parse_int(_txt(f, "lsb"))
            width = parse_int(_txt(f, "msb")) - lsb + 1
        elif _txt(f, "bitRange") is not None:
            m = re.fullmatch(r"\[\s*(\w+)\s*:\s*(\w+)\s*\]", _txt(f, "bitRange"))
            if not m:
                raise ValueError(f"field {name}: bad bitRange {_txt(f, 'bitRange')!r}")
            msb, lsb = parse_int(m.group(1)), parse_int(m.group(2))
            width = msb - lsb + 1
        else:
            raise ValueError(f"field {name}: no bit position")
        out = {"name": name, "lsb": lsb + delta, "width": width}
        acc = _txt(f, "access")
        if acc and acc != reg_access:
            out["access"] = acc
        mwv = _txt(f, "modifiedWriteValues")
        if mwv:
            out["write_effect"] = mwv          # e.g. oneToClear -> w1c
        enum: dict[int, str] = {}
        for ev in (f.findall("enumeratedValues") if self.enums != "none" else []):
            if ev.get("derivedFrom"):
                ev = self.enum_defs.get(ev.get("derivedFrom").split(".")[-1], ev)
            for v in ev.findall("enumeratedValue"):
                vn = _txt(v, "name")
                if self.enums == "auto" and (vn is None or re.fullmatch(r"[0-9]+|#?[01x]+|0x[0-9a-fA-F]+", vn)):
                    # Freescale/NXP SVDs name enum values "0", "1", ...: the description carries the meaning
                    d = " ".join((_txt(v, "description") or "").split())
                    if d:
                        vn = d if len(d) <= 80 else d[:77] + "..."
                if _txt(v, "isDefault") in ("true", "1"):
                    continue
                for val in parse_enum_value(_txt(v, "value")):
                    if val is not None:
                        enum.setdefault(val, vn)
        if enum:
            out["enum"] = dict(sorted(enum.items()))
        return out


def convert(svd_path: str | Path, peripherals: list[str] | None = None, flat: bool = False,
            enums: str = "auto") -> dict:
    root = ET.parse(str(svd_path)).getroot()
    sp = SvdParser(root, enums)
    doc = sp.device()
    doc["interrupts"] = sp.interrupts()
    names = list(sp.periph_el)
    if peripherals:
        wanted = [p.strip() for p in peripherals if p.strip()]
        missing = [p for p in wanted if p not in sp.periph_el]
        if missing:
            raise SystemExit(f"svd2chip: unknown peripheral(s): {', '.join(missing)}")
        names = wanted
        if not flat:   # keep bases of derived peripherals so the output is self-contained
            for n in list(names):
                d = sp.periph_el[n].get("derivedFrom")
                while d and d not in names:
                    names.append(d)
                    d = sp.periph_el[d].get("derivedFrom")
    doc["peripherals"] = {n: sp.peripheral(n, flat) for n in names}
    return doc


# --------------------------------------------------------------------------------------------- YAML out
_PLAIN = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*\Z")
_RESERVED = {"y", "n", "yes", "no", "true", "false", "on", "off", "null", "~"}


def ystr(s) -> str:
    if s is None:
        return "null"
    s = str(s)
    if _PLAIN.match(s) and s.lower() not in _RESERVED and not s.endswith((".", "-")):
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def hx(v: int, digits: int = 0) -> str:
    return f"0x{v:0{digits}X}"


def _field_yaml(f: dict) -> str:
    parts = [f"name: {ystr(f['name'])}", f"lsb: {f['lsb']}", f"width: {f['width']}"]
    if "access" in f:
        parts.append(f"access: {f['access']}")
    if "write_effect" in f:
        parts.append(f"write_effect: {ystr(f['write_effect'])}")
    if "enum" in f:
        parts.append("enum: {" + ", ".join(f"{k}: {ystr(v)}" for k, v in f["enum"].items()) + "}")
    return "{" + ", ".join(parts) + "}"


def emit_yaml(doc: dict, source: str | None = None) -> str:
    L: list[str] = []
    L.append("# Generated by tools/svd2chip.py -- do not edit by hand; regenerate from the SVD.")
    if source:
        L.append(f"# source: {source}")
    L.append(f"device: {ystr(doc['device'])}")
    if doc.get("version"):
        L.append(f"svd_version: {ystr(doc['version'])}")
    L.append(f"address_unit_bits: {doc.get('address_unit_bits', 8)}")
    cpu = doc.get("cpu")
    if cpu:
        L.append(f"cpu: {{name: {ystr(cpu['name'])}, fpu: {'true' if cpu['fpu'] else 'false'}, "
                 f"nvic_prio_bits: {cpu['nvic_prio_bits'] if cpu['nvic_prio_bits'] is not None else 'null'}}}")
    L.append("interrupts:")
    for n, name in doc["interrupts"].items():
        L.append(f"  {n}: {ystr(name)}")
    L.append("peripherals:")
    for pname, p in doc["peripherals"].items():
        L.append(f"  {ystr(pname)}:")
        L.append(f"    base: {hx(p['base'], 8)}")
        L.append(f"    size: {hx(p['size'])}")
        L.append(f"    derived_from: {ystr(p['derived_from'])}")
        if "description" in p:
            L.append(f"    description: {ystr(p['description'])}")
        if "group" in p:
            L.append(f"    group: {ystr(p['group'])}")
        if "interrupts" in p:
            L.append("    interrupts: {" + ", ".join(f"{k}: {ystr(v)}" for k, v in p["interrupts"].items()) + "}")
        if not p["registers"]:
            L.append("    registers: []")
            continue
        L.append("    registers:")
        for r in p["registers"]:
            d = r["width"] * 2
            head = (f"      - {{name: {ystr(r['name'])}, offset: {hx(r['offset'])}, width: {r['width']}, "
                    f"reset: {hx(r['reset'], d)}, reset_mask: {hx(r['reset_mask'], d)}, access: {r['access']}")
            if not r["fields"]:
                L.append(head + ", fields: []}")
                continue
            L.append(head + ", fields: [")
            for i, f in enumerate(r["fields"]):
                L.append("          " + _field_yaml(f) + ("," if i < len(r["fields"]) - 1 else "]}"))
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------------------------- --check
ALIASES = {  # chip-YAML name -> candidate SVD names
    "VBAT": ["RFVBAT"], "RFVBAT": ["VBAT"], "OSC0": ["OSC"], "OSC": ["OSC0"],
    "PTA": ["GPIOA"], "PTB": ["GPIOB"], "PTC": ["GPIOC"], "PTD": ["GPIOD"], "PTE": ["GPIOE"],
    "LPTMR": ["LPTMR0"], "USB": ["USB0"], "I2S": ["I2S0"], "PDB": ["PDB0"], "DAC": ["DAC0"],
    "CMP": ["CMP0"], "FTFL": ["FTFE"], "FTFA": ["FTFE"], "NV": ["FTFE_FlashConfig"],
    "SDIO": ["SDMMC"], "SPDIFRX": ["SPDIF_RX"], "CEC": ["HDMI_CEC"],          # STM32F446.svd names (HAL names in the YAML)
}


def _chip_peripherals(chip: dict) -> list[tuple[str, int | None, int | None]]:
    peri = chip.get("peripherals") or []
    items = []
    if isinstance(peri, dict):
        peri = [dict(v or {}, name=k) for k, v in peri.items()]
    for p in peri:
        if not isinstance(p, dict) or "name" not in p:
            continue
        base = p.get("base")
        size = p.get("size")
        if isinstance(base, str):
            base = parse_int(base)
        if isinstance(size, str):
            size = parse_int(size)
        items.append((str(p["name"]), base, size))
    return items


def _chip_omitted(chip: dict) -> tuple[dict[str, str], dict[int, str]]:
    """The chip YAML's `omitted:` list: SVD peripherals deliberately left out of this part, each
    `{name: X, reason: "...", irqs: [n, ...]}`.  Returns ({peripheral: reason}, {irq number: reason})."""
    periph: dict[str, str] = {}
    irqs: dict[int, str] = {}
    for o in chip.get("omitted") or []:
        if not isinstance(o, dict) or "name" not in o:
            continue
        why = str(o.get("reason") or "no reason given")
        periph[str(o["name"])] = why
        for n in o.get("irqs") or []:
            irqs[int(n)] = why
    return periph, irqs


def check(svd_path: str | Path, chip_path: str | Path, out=sys.stdout) -> int:
    """Compare chip-YAML peripheral bases, sizes (>= SVD addressBlock extent) and IRQ numbers with the SVD.
    SVD peripherals and IRQ numbers named in the chip YAML's `omitted:` list are reported as "OMITTED (reason)"
    instead of being compared; an omitted peripheral that the YAML still instantiates is a mismatch.
    Returns the number of mismatches."""
    import yaml
    chip_path = Path(chip_path)
    if not chip_path.exists():
        print(f"svd2chip --check: {chip_path} does not exist (yet); nothing to compare", file=out)
        return 0
    chip = yaml.safe_load(chip_path.read_text()) or {}
    if "include" in chip:                       # same merge rule as chipem.chipdesc.load_chip
        inc = yaml.safe_load((chip_path.parent / chip.pop("include")).read_text()) or {}
        for k, v in inc.items():
            chip.setdefault(k, v)
    root = ET.parse(str(svd_path)).getroot()
    sp = SvdParser(root)
    svd_base = {n: parse_int(_txt(sp.resolved_peripheral_el(n), "baseAddress")) for n in sp.periph_el}

    def svd_extent(n: str) -> int | None:
        blocks = sp.resolved_peripheral_el(n).findall("addressBlock")
        if not blocks:
            return None
        return max(parse_int(_txt(b, "offset"), 0) + parse_int(_txt(b, "size"), 0) for b in blocks)
    bad = 0
    print(f"checking {chip_path} against {svd_path} ({sp.device()['device']})", file=out)
    chip_periphs = _chip_peripherals(chip)
    omit_p, omit_irq = _chip_omitted(chip)
    covered = set()
    for name, base, size in chip_periphs:
        if name in omit_p:
            bad += 1
            print(f"  MISMATCH {name}: listed under omitted: but still instantiated", file=out)
            continue
        cands = [name] + ALIASES.get(name, [])
        hit = next((c for c in cands if c in svd_base), None)
        if hit is None:
            print(f"  NOTE  {name}: not in SVD (base {hx(base, 8) if base is not None else '?'})", file=out)
            continue
        covered.add(hit)
        if base is None:
            print(f"  NOTE  {name}: no base in chip YAML (SVD {hit} = {hx(svd_base[hit], 8)})", file=out)
            continue
        if base != svd_base[hit]:
            bad += 1
            print(f"  MISMATCH {name}: chip YAML base {hx(base, 8)} != SVD {hit} {hx(svd_base[hit], 8)}", file=out)
            continue
        ext = svd_extent(hit)
        nxt = min((b for _, b, _ in chip_periphs if b is not None and b > base), default=None)
        if ext is not None and nxt is not None and base + ext > nxt:
            ext = nxt - base                            # SVD block overruns the next block (F1 BKP at +4): clamp
        sz = 0x1000 if size is None else size          # chipdesc default size
        if ext is not None and sz < ext:
            bad += 1
            print(f"  MISMATCH {name}: chip YAML size {hx(sz)} < SVD {hit} addressBlock extent {hx(ext)}", file=out)
    for n, why in omit_p.items():
        if n in svd_base:
            print(f"  OMITTED {n}@{hx(svd_base[n], 8)} ({why})", file=out)
        else:
            print(f"  NOTE  omitted {n}: not in SVD ({why})", file=out)
    absent = [n for n in svd_base if n not in covered and n not in omit_p]
    if absent:
        print("  NOTE  in SVD but not in chip YAML: " + ", ".join(f"{n}@{hx(svd_base[n], 8)}" for n in absent),
              file=out)
    irqs = chip.get("irqs") or chip.get("interrupts") or {}
    svd_irqs = sp.interrupts()
    svd_by_name = {v: k for k, v in svd_irqs.items()}
    # `svd_irq_names: {n: SVD name}`: the chip YAML deliberately uses another name (the vendor header / reference
    # manual one) for IRQ n; accepted when the SVD really calls IRQ n that way
    renamed = {int(k): str(v) for k, v in (chip.get("svd_irq_names") or {}).items()}
    if isinstance(irqs, dict):
        for n, nm in irqs.items():
            try:
                n = int(n)
            except (TypeError, ValueError):
                continue
            if isinstance(nm, dict):
                nm = nm.get("name")
            if nm is None:
                continue
            if svd_irqs.get(n) == nm:
                continue
            if n in renamed and svd_irqs.get(n) == renamed[n]:
                print(f"  RENAMED irq {n}: SVD {renamed[n]!r}, chip YAML {nm!r}", file=out)
                continue
            if n in omit_irq:
                print(f"  OMITTED irq {n}: SVD {svd_irqs.get(n)!r}, chip YAML {nm!r} ({omit_irq[n]})", file=out)
                continue
            if n not in svd_irqs and nm not in svd_by_name:
                print(f"  NOTE  irq {n}: chip YAML {nm!r}; SVD defines no interrupt {n}", file=out)
                continue
            bad += 1
            where = f" (SVD has {nm} at {svd_by_name[nm]})" if nm in svd_by_name else ""
            print(f"  MISMATCH irq {n}: chip YAML {nm!r} != SVD {svd_irqs.get(n)!r}{where}", file=out)
    print(f"{bad} mismatch(es)", file=out)
    return bad


# --------------------------------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("svd", help="input CMSIS-SVD file")
    ap.add_argument("-o", "--output", help="output YAML (default: stdout)")
    ap.add_argument("--peripherals", help="comma-separated list of peripherals to emit (default: all)")
    ap.add_argument("--flat", action="store_true", help="inline derived peripherals (no derived_from references)")
    ap.add_argument("--check", metavar="CHIP_YAML", help="compare chip YAML peripheral bases/IRQs against the SVD")
    ap.add_argument("--enums", choices=("auto", "names", "none"), default="auto",
                    help="enum value labels: names, none, or auto (= name, or the description when the SVD name "
                         "is just a number, as in NXP/Freescale SVDs)")
    ap.add_argument("--source", help="source note (URL/commit) written into the output header")
    a = ap.parse_args(argv)
    if a.check:
        check(a.svd, a.check)
        if not a.output:
            return 0
    doc = convert(a.svd, a.peripherals.split(",") if a.peripherals else None, flat=a.flat, enums=a.enums)
    text = emit_yaml(doc, a.source)
    if a.output:
        Path(a.output).write_text(text)
        n = sum(len(p["registers"]) for p in doc["peripherals"].values())
        print(f"wrote {a.output}: {len(doc['peripherals'])} peripherals, {n} registers, "
              f"{len(doc['interrupts'])} interrupts, {len(text)} bytes", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
