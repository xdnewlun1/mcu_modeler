"""Chip description: the data-only model of a chip (pinout, memory map, peripherals, IRQs).

Loaded from chips/*.yaml. Contains no runtime state; see chipem/chip.py for the runtime assembly.
Shared by every module: keep this file small and stable (see docs/CONTRACTS.md).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_GPIO_NAME_FORMAT = "PT{port}{bit}"      # Kinetis; chips override it with `gpio_name_format`


def format_gpio_name(fmt: str | None, port: str | None, bit: int | None) -> str | None:
    """Short GPIO name from a `gpio_name_format`: fields {port} (letter), {bit} (bit within the port) and {num}
    (32 * port_index + bit, port A = 0). Kinetis "PTC5", STM32 "PC5", C2000 "GPIO34"; None for non-GPIO pins."""
    if port is None or bit is None:
        return None
    num = 32 * (ord(str(port).upper()) - ord("A")) + int(bit)
    return (fmt or DEFAULT_GPIO_NAME_FORMAT).format(port=port, bit=bit, num=num)


@dataclass
class CoreDesc:
    arch: str = "armv7e-m"          # armv7e-m | armv7-m | armv6-m | c28x ...
    backend: str = "unicorn"        # core implementation
    cpu: str = "cortex-m4"
    fpu: bool = True
    clock_hz: float = 120_000_000.0  # nominal/default core clock
    addr_unit_bits: int = 8
    num_irqs: int = 96


@dataclass
class MemoryDesc:
    name: str
    base: int
    size: int
    kind: str = "ram"               # flash | ram | flexnvm | flexram | rom
    writable: bool | None = None    # default: flash/rom read-only for the CPU, ram writable
    fill: int = 0xFF                # erased-flash fill value

    @property
    def end(self) -> int:
        return self.base + self.size


@dataclass
class PeripheralDesc:
    name: str
    model: str                      # registry model name, e.g. "kinetis.port"
    base: int
    size: int = 0x1000
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class PinDesc:
    num: int
    name: str                       # datasheet pin name, e.g. "PTC5/LLWU_P9"
    kind: str = "gpio"              # gpio | analog | power | special
    functions: dict[str, str] = field(default_factory=dict)  # "ALT0".."ALT7" (or family-specific keys) -> signal
    default: str | None = None      # function active after reset
    port: str | None = None         # "A".."E" for GPIO-capable pins
    bit: int | None = None          # bit within the port
    gpio_name_format: str = DEFAULT_GPIO_NAME_FORMAT   # set by the loader from the chip's `gpio_name_format`

    @property
    def gpio_name(self) -> str | None:
        """Short GPIO name used everywhere (events, stimulus, reports): Kinetis "PTC5", STM32 "PC5", C2000 "GPIO34".
        Format fields: {port} (letter), {bit} (bit within port), {num} (32 * port_index + bit, port A = 0)."""
        return format_gpio_name(self.gpio_name_format, self.port, self.bit)


@dataclass
class ChipDescription:
    name: str
    core: CoreDesc
    family: str                     # peripheral family package, e.g. "kinetis"
    memory: list[MemoryDesc]
    peripherals: list[PeripheralDesc]
    irqs: dict[int, str]            # IRQ number (0-based, external) -> name
    pins: list[PinDesc]
    package: str = ""               # e.g. "LQFP100"
    regs_file: str | None = None    # optional path to SVD-derived register table (chips/*_regs.yaml)
    gpio_name_format: str = DEFAULT_GPIO_NAME_FORMAT   # Kinetis "PT{port}{bit}", STM32 "P{port}{bit}", C2000 "GPIO{num}"
    extra: dict[str, Any] = field(default_factory=dict)
    source_path: Path | None = None

    # --- lookups -------------------------------------------------------
    def pin(self, key: int | str) -> PinDesc:
        if isinstance(key, int):
            for p in self.pins:
                if p.num == key:
                    return p
            raise KeyError(key)
        k = key.upper()
        for p in self.pins:
            if p.name.upper() == k or p.gpio_name == k or k in p.name.upper().split("/"):
                return p
        raise KeyError(key)

    def peripheral(self, name: str) -> PeripheralDesc:
        for p in self.peripherals:
            if p.name == name:
                return p
        raise KeyError(name)

    def peripheral_at(self, addr: int) -> PeripheralDesc | None:
        for p in self.peripherals:
            if p.base <= addr < p.base + p.size:
                return p
        return None

    def memory_at(self, addr: int) -> MemoryDesc | None:
        for m in self.memory:
            if m.base <= addr < m.end:
                return m
        return None


def _int(v: Any) -> int:
    return int(v, 0) if isinstance(v, str) else int(v)


def load_chip(path: str | Path) -> ChipDescription:
    """Load a chips/*.yaml file (supports `include:` of another YAML whose keys are merged as defaults)."""
    path = Path(path)
    data = yaml.safe_load(path.read_text())
    if "include" in data:
        base = yaml.safe_load((path.parent / data.pop("include")).read_text())
        for k, v in base.items():
            data.setdefault(k, v)
    core = CoreDesc(**{k: (float(v) if k == "clock_hz" else v) for k, v in (data.get("core") or {}).items()})
    memory = [MemoryDesc(name=m["name"], base=_int(m["base"]), size=_int(m["size"]), kind=m.get("kind", "ram"),
                         writable=m.get("writable"), fill=_int(m.get("fill", 0xFF))) for m in data.get("memory", [])]
    periphs = [PeripheralDesc(name=p["name"], model=p["model"], base=_int(p["base"]), size=_int(p.get("size", 0x1000)),
                              params=p.get("params") or {}) for p in data.get("peripherals", [])]
    irqs = {_int(k): str(v) for k, v in (data.get("irqs") or {}).items()}
    fmt = str(data.get("gpio_name_format", DEFAULT_GPIO_NAME_FORMAT))
    pins = []
    for p in data.get("pins", []):
        pins.append(PinDesc(num=_int(p["num"]), name=str(p["name"]), kind=p.get("kind", "gpio"),
                            functions={str(k): str(v) for k, v in (p.get("functions") or {}).items()},
                            default=p.get("default"), port=p.get("port"),
                            bit=None if p.get("bit") is None else _int(p["bit"]), gpio_name_format=fmt))
    known = {"name", "core", "family", "memory", "peripherals", "irqs", "pins", "package", "regs_file", "gpio_name_format"}
    return ChipDescription(name=data["name"], core=core, family=data.get("family", ""), memory=memory,
                           peripherals=periphs, irqs=irqs, pins=pins, package=data.get("package", ""),
                           regs_file=data.get("regs_file"), gpio_name_format=fmt,
                           extra={k: v for k, v in data.items() if k not in known}, source_path=path)
