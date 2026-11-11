"""Helpers for the P89CV51 (MCS-51) tests: assemble a snippet with the chip's SFR names, build the chip, run."""
from __future__ import annotations

from pathlib import Path

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.core.mcs51.asm import Assembler, symbols_from_regs
from chipem.firmware import FirmwareImage

ROOT = Path(__file__).resolve().parents[1]
CHIP = ROOT / "chips" / "p89cv51rd2.yaml"
REGS = ROOT / "chips" / "p89cv51_regs.yaml"
_SYMS = symbols_from_regs(REGS)
XTAL = 11_059_200
MC_NS = 12e9 / XTAL                      # one machine cycle (12-clock mode) in ns


def asm(src: str):
    sfrs, bits = _SYMS
    return Assembler(sfrs=sfrs, bits=bits).assemble(src)


def chip_for(src: str, *, chip: Path | str = CHIP, clock_hz: float | None = None) -> Chip:
    """A P89CV51RD2 with `src` (full program incl. ORG 0) loaded and reset."""
    r = asm(src)
    c = Chip(load_chip(chip), clock_hz=clock_hz)
    log = c.log                          # pin changes into the event log, as Session does
    c.pins.subscribe(lambda ev: log.event("pin", ev.t_ns, pin=ev.pin, field=ev.field, old=ev.old, new=ev.new))
    c.load_firmware(FirmwareImage(list(r.segments), None, "<test>", "hex", 1, {k: v for k, (t, v) in r.symbols.items()
                                                                             if t == "CODE"}))
    c.reset()
    c.syms = {k: v for k, (t, v) in r.symbols.items()}
    return c


def program(body: str, *, isrs: dict[int, str] | None = None, start: int = 0x50) -> str:
    """ORG 0 LJMP main + interrupt vectors (vector address -> handler source) + main at `start`."""
    out = [" ORG 0", " LJMP main"]
    for vec, code in sorted((isrs or {}).items()):
        out += [f" ORG 0x{vec:04X}", f" LJMP isr_{vec:x}"]
    out += [f" ORG 0x{start:04X}", "main: MOV SP, #6Fh", body, "halt: SJMP halt"]
    for vec, code in sorted((isrs or {}).items()):
        out += [f"isr_{vec:x}:", code]
    return "\n".join(out) + "\n"


def iram(c: Chip, a: int) -> int:
    return c.core.iram[a]


def pin_edges(c: Chip, pin: str) -> list[tuple[float, str]]:
    return [(e[1] if isinstance(e, tuple) else e.t_ns, (e[2] if isinstance(e, tuple) else e.fields).get("new"))
            for e in _events(c) if _kind(e) == "pin" and _f(e).get("pin") == pin and _f(e).get("field") == "level"]


def _events(c):
    return list(iter(c.log))


def _kind(e):
    return e.kind


def _f(e):
    return e.fields
