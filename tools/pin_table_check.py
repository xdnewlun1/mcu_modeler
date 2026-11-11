#!/usr/bin/env python3
"""pin_table_check: validate the `pins:` table of a chip YAML and print a summary.

Usage:
    python tools/pin_table_check.py chips/<part>.yaml [--expect N] [--strict] [--verbose]

Errors (exit status 1):
  * pin count differs from --expect (default: the number in `package:`, e.g. LQFP144 -> 144; --expect 0 disables)
  * pin numbers are not unique, or not exactly 1..N
  * `kind` not one of gpio | analog | power | special
  * a gpio pin without `port`/`bit`, a bad port letter (A-Z, or 0-9 for 80C51 ports) or a bit outside 0..31; a
    non-gpio pin with port/bit
  * two gpio pins with the same port/bit (same GPIO name)
  * the GPIO name built from `gpio_name_format` (PT{port}{bit}, P{port}{bit}, GPIO{num}) is not one of the
    "/"-separated parts of the pin name (port/bit typo, or the wrong format for the family)
  * a function value names the GPIO of a *different* pin (e.g. ALT1: PTC6 on the PTC5 pin)
  * two gpio/analog pins with the same name
Warnings (exit status 1 only with --strict):
  * a function key that the family's mux model does not know (kinetis ALT0-7/EzPort, stm32 AF0-15/ADC/analog/
    additional, ti_c2000 GPIO/MUX1-3/ANALOG/AIO, stm32f1 AF/REMAP/ADC/analog/additional/debug, mcs51 GPIO/ALT1-3)
  * a power pin whose name does not look like a supply (V*, GND*, AVDD/AVSS ...), or a non-power pin that does
  * a pin whose name looks like a GPIO name but whose kind is not gpio
  * a gpio pin whose `default` matches none of its functions (and is not GPIO/DISABLED/empty)
  * a gpio pin without any functions; the same signal under two keys of one pin (noted, legal on some parts)

The checks use chipem.chipdesc.load_chip, so `include:` files work.  Import `check_desc` / `check_file` from tests.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chipem.chipdesc import ChipDescription, load_chip  # noqa: E402

KINDS = ("gpio", "analog", "power", "special")
FUNCTION_KEYS = {
    "kinetis": re.compile(r"ALT[0-7]|EzPort\Z"),
    "stm32": re.compile(r"AF([0-9]|1[0-5])|ADC|analog|additional\Z"),
    "ti_c2000": re.compile(r"GPIO|MUX[1-3]|ANALOG|AIO\Z"),     # AIO: Piccolo analog-shared digital I/O (AIOMUX1)
    "stm32f1": re.compile(r"AF|REMAP|ADC|analog|additional|debug\Z"),
    "mcs51": re.compile(r"GPIO|ALT[1-3]\Z"),
}
POWER_NAME = re.compile(r"(V[A-Z0-9_+\-]*|GND[A-Z0-9_]*|[AD]?VSS[A-Z0-9_]*|[AD]?VDD[A-Z0-9_]*)\Z", re.I)
SUPPLY_LIKE = re.compile(r"(VDD|VSS|GND)[A-Z0-9_]*\Z", re.I)
NEUTRAL_DEFAULTS = {"", "GPIO", "DISABLED", "NONE"}


@dataclass
class Report:
    path: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    summary: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def text(self, verbose: bool = False) -> str:
        out = [f"pin table check: {self.path}"]
        out += [f"  {s}" for s in self.summary]
        out += [f"  ERROR   {e}" for e in self.errors]
        out += [f"  WARNING {w}" for w in self.warnings]
        notes = self.notes if verbose else self.notes[:3]
        out += [f"  NOTE    {n}" for n in notes]
        if len(notes) < len(self.notes):
            out.append(f"  NOTE    ... {len(self.notes) - len(notes)} more (--verbose)")
        out.append(f"{len(self.errors)} error(s), {len(self.warnings)} warning(s), {len(self.notes)} note(s)")
        return "\n".join(out)


def package_pin_count(package: str) -> int | None:
    """LQFP144 -> 144, 'LQFP176 (PGF)' -> 176, QFN48 -> 48; None when the package names no number."""
    m = re.search(r"(\d+)", package or "")
    return int(m.group(1)) if m else None


def _gpio_like(desc: ChipDescription) -> re.Pattern:
    """Regex for GPIO names in this chip's format (PT{port}{bit} -> PT[A-Z]\\d+, GPIO{num} -> GPIO\\d+)."""
    fmt = desc.gpio_name_format
    rx = re.escape(fmt).replace(r"\{port\}", "[A-Z0-9]").replace(r"\{bit\}", r"\d+").replace(r"\{num\}", r"\d+")
    # re.escape leaves braces escaped as \{ ... \} on some versions and bare on others
    rx = rx.replace("{port}", "[A-Z0-9]").replace("{bit}", r"\d+").replace("{num}", r"\d+")
    return re.compile(rx + r"\Z")


def check_desc(desc: ChipDescription, expect: int | None = None, path: str = "") -> Report:
    r = Report(path or str(desc.source_path or desc.name))
    pins = desc.pins
    fam = (desc.family or "").lower()
    if expect is None:
        expect = package_pin_count(desc.package)
        src = f"from package {desc.package!r}"
    else:
        src = "from --expect"
    if not pins:
        r.errors.append("no pins: the `pins:` table is empty")
    if expect and len(pins) != expect:
        r.errors.append(f"{len(pins)} pins, expected {expect} ({src})")
    nums = [p.num for p in pins]
    dup_nums = sorted(n for n, c in Counter(nums).items() if c > 1)
    if dup_nums:
        r.errors.append(f"duplicate pin numbers: {dup_nums}")
    if pins and sorted(set(nums)) != list(range(1, max(nums) + 1)):
        missing = sorted(set(range(1, max(nums) + 1)) - set(nums))
        r.errors.append(f"pin numbers are not 1..{max(nums)}: missing {missing[:20]}{' ...' if len(missing) > 20 else ''}")

    gpio_rx = _gpio_like(desc)
    by_gpio: dict[str, list[int]] = defaultdict(list)
    for p in pins:
        if p.kind == "gpio" and p.gpio_name:
            by_gpio[p.gpio_name.upper()].append(p.num)
    names_seen: dict[str, list[int]] = defaultdict(list)
    keys_seen: Counter = Counter()
    key_rx = FUNCTION_KEYS.get(fam)
    ports: dict[str, list[int]] = defaultdict(list)

    for p in pins:
        tag = f"pin {p.num} {p.name!r}"
        parts = [s.strip().upper() for s in p.name.split("/")]
        if p.kind not in KINDS:
            r.errors.append(f"{tag}: kind {p.kind!r} is not one of {', '.join(KINDS)}")
        if p.kind == "gpio":
            if p.port is None or p.bit is None:
                r.errors.append(f"{tag}: gpio pin without port/bit")
            else:
                if not re.fullmatch(r"[A-Z0-9]", str(p.port)):     # letters; digits for 80C51 ports P0-P3
                    r.errors.append(f"{tag}: port {p.port!r} is not a single capital letter or digit")
                if not 0 <= int(p.bit) <= 31:
                    r.errors.append(f"{tag}: bit {p.bit} outside 0..31")
                ports[str(p.port)].append(int(p.bit))
                gname = (p.gpio_name or "").upper()
                if gname not in parts:
                    r.errors.append(f"{tag}: GPIO name {p.gpio_name!r} (gpio_name_format {desc.gpio_name_format!r}, "
                                    f"port {p.port} bit {p.bit}) is not part of the pin name")
            if not p.functions:
                r.warnings.append(f"{tag}: gpio pin without functions")
            elif p.default and p.default.upper() not in NEUTRAL_DEFAULTS:
                toks = {t.strip().upper() for v in p.functions.values() for t in v.split("/")}
                toks |= set(parts)
                if not any(t.strip().upper() in toks for t in p.default.split("/")):
                    r.warnings.append(f"{tag}: default {p.default!r} matches none of its functions")
        else:
            if p.port is not None or p.bit is not None:
                r.errors.append(f"{tag}: kind {p.kind} but has port/bit ({p.port}, {p.bit}); only gpio pins carry them")
            if any(gpio_rx.fullmatch(s) for s in parts):
                r.warnings.append(f"{tag}: name looks like a GPIO ({desc.gpio_name_format}) but kind is {p.kind}")
        if p.kind == "power" and not POWER_NAME.fullmatch(p.name.strip()):
            r.warnings.append(f"{tag}: kind power but the name does not look like a supply")
        if p.kind != "power" and SUPPLY_LIKE.fullmatch(p.name.strip()):
            r.warnings.append(f"{tag}: name looks like a supply but kind is {p.kind}")
        if p.kind in ("gpio", "analog"):
            names_seen[p.name.upper()].append(p.num)
        # functions
        vals: dict[str, list[str]] = defaultdict(list)
        for k, v in p.functions.items():
            keys_seen[k] += 1
            if key_rx is not None and not key_rx.fullmatch(k):
                r.warnings.append(f"{tag}: function key {k!r} is not a {fam} mux key")
            vals[v.upper()].append(k)
            for t in v.split("/"):
                t = t.strip().upper()
                if t in by_gpio and p.num not in by_gpio[t]:
                    r.errors.append(f"{tag}: function {k}={v!r} names GPIO {t} of pin {by_gpio[t]}")
        for v, ks in vals.items():
            if len(ks) > 1:
                r.notes.append(f"{tag}: {v} under {', '.join(ks)}")

    for g, ns in by_gpio.items():
        if len(ns) > 1:
            r.errors.append(f"GPIO {g} on several pins: {ns}")
    for n, ns in names_seen.items():
        if len(ns) > 1:
            r.errors.append(f"pin name {n!r} used by several gpio/analog pins: {ns}")

    kinds = Counter(p.kind for p in pins)
    r.summary.append(f"{desc.name} {desc.package}: {len(pins)} pins ("
                     + ", ".join(f"{k} {kinds[k]}" for k in KINDS if kinds[k])
                     + (f"; expected {expect} {src}" if expect else "") + ")")
    r.summary.append(f"gpio_name_format {desc.gpio_name_format!r}, family {desc.family or '?'}")
    if ports:
        r.summary.append("ports: " + ", ".join(f"{pt}:{min(b)}-{max(b)} ({len(b)})" for pt, b in sorted(ports.items())))
    if keys_seen:
        def keyord(k: str):
            m = re.match(r"([A-Za-z]+)(\d+)\Z", k)
            return (m.group(1), int(m.group(2))) if m else (k, -1)
        r.summary.append("function keys: " + " ".join(f"{k}({keys_seen[k]})" for k in sorted(keys_seen, key=keyord)))
    return r


def check_file(path: str | Path, expect: int | None = None) -> Report:
    return check_desc(load_chip(path), expect, str(path))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("chip", nargs="+", help="chip YAML(s) (path, or a name under chips/)")
    ap.add_argument("--expect", type=int, default=None,
                    help="expected pin count (default: the number in `package:`; 0 = do not check)")
    ap.add_argument("--strict", action="store_true", help="warnings also make the exit status 1")
    ap.add_argument("--verbose", "-v", action="store_true", help="print every NOTE line (default: the first 3)")
    a = ap.parse_args(argv)
    status = 0
    for c in a.chip:
        path = Path(c)
        if not path.exists() and (ROOT / "chips" / f"{c}.yaml").exists():
            path = ROOT / "chips" / f"{c}.yaml"
        rep = check_file(path, a.expect)
        print(rep.text(a.verbose))
        if rep.errors or (a.strict and rep.warnings):
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
