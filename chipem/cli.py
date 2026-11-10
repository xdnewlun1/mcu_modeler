"""`chipem` command line: info | run | shell | serve."""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent      # project root (contains chips/)
CHIPS_DIR = PKG_ROOT / "chips"
DEFAULT_CHIP = "mk22fn1m0vll12"


def resolve_chip(name_or_path: str) -> Path:
    """A path to a chip YAML, or a chip name looked up as chips/<name>.yaml (case-insensitive)."""
    p = Path(name_or_path)
    if p.suffix in (".yaml", ".yml") or p.exists():
        if p.exists():
            return p
        raise FileNotFoundError(f"chip description not found: {p}")
    cand = CHIPS_DIR / f"{name_or_path.lower()}.yaml"
    if cand.exists():
        return cand
    known = ", ".join(sorted(x.stem for x in CHIPS_DIR.glob("*.yaml") if not x.stem.endswith("_regs")))
    raise FileNotFoundError(f"unknown chip {name_or_path!r} (known: {known})")


def load_desc(name_or_path: str):
    from .chipdesc import load_chip
    return load_chip(resolve_chip(name_or_path))


def _int(s: str) -> int:
    return int(s, 0)


def load_image(path: str, desc, load_addr: int | None, fmt: str | None = None, *, quiet: bool = False):
    """Load firmware; for raw .bin images without --load-addr, auto-detect the base from the vector table."""
    from .firmware import guess_load_addr, guess_load_addr_scores, load_firmware
    ub = max(int(getattr(desc.core, "addr_unit_bits", 8)) // 8, 1)
    img = load_firmware(path, load_addr=load_addr, fmt=fmt, unit_bytes=ub)    # 16-bit chips: --load-addr is a word address
    if img.fmt == "bin" and load_addr is None and ub == 1:
        best = guess_load_addr(img, desc)
        if best != img.start:
            if not quiet:
                scores = guess_load_addr_scores(img, desc)
                print(f"note: auto-detected load address 0x{best:X} (vector scores "
                      + ", ".join(f"0x{b:X}:{s}" for b, s in scores.items()) + "); override with --load-addr",
                      file=sys.stderr)
            img = img.rebased(best)
    return img


def cmd_info(args) -> int:
    from .firmware import analyze, format_report
    desc = load_desc(args.chip)
    img = load_image(args.firmware, desc, args.load_addr, args.format)
    report = analyze(img, desc, vtor=args.vtor, disasm=not args.no_disasm)
    print(format_report(report))
    return 0


def cmd_not_implemented(args) -> int:
    print(f"chipem {args.cmd}: not implemented yet")
    return 2


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="chipem", description="Description-driven MCU emulator")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, optional_fw=False):
        if optional_fw:
            p.add_argument("firmware", nargs="?", default=None,
                           help="firmware image (.bin/.hex/.elf/.srec); optional with --board")
        else:
            p.add_argument("firmware", help="firmware image (.bin/.hex/.elf/.srec)")
        p.add_argument("--chip", default=DEFAULT_CHIP, help="chip name (chips/<name>.yaml) or path to a chip YAML")
        p.add_argument("--load-addr", type=_int, default=None, help="load address for raw .bin images")
        p.add_argument("--format", default=None, help="force firmware format: bin|hex|elf|srec|coff|c28img")

    p = sub.add_parser("info", help="static analysis of a firmware image against a chip description")
    common(p)
    p.add_argument("--vtor", type=_int, default=None, help="vector table address (default: start of image)")
    p.add_argument("--no-disasm", action="store_true", help="skip the capstone code scan")
    p.set_defaults(func=cmd_info)

    # Session-level options shared by run/shell/serve (parsed here; consumed by chipem.session.Session.from_args)
    def session_opts(p):
        p.add_argument("--clock", type=float, default=None, help="override core clock in Hz (default: derived from MCG / chip YAML)")
        p.add_argument("--precise", action="store_true", help="exact per-instruction timing (slower)")
        p.add_argument("--vtor", type=_int, default=None, help="vector table address (default: load address)")
        p.add_argument("--part", action="append", default=[], metavar="ID:TYPE[:KEY=VAL,...]",
                       help="add a board part: KEYs naming a port wire it to a pin or net, others are parameters, "
                            "e.g. SW1:switch:out=PTC6,level=H  LED1:led:in=PTC5,active=L  CLK:pulse:out=PTC6,period=2ms "
                            "G1:and:in0=PTC5,in1=CLKNET,out=B.PTC7  P1:probe:in=PTC5. Types: switch button const vcc gnd "
                            "pulse pattern led probe counter not buf and nand or nor xor xnor mux2 dff latch (repeatable; "
                            "works on one chip too)")
        p.add_argument("--set", action="append", default=[], metavar="PIN=LEVEL|PART=VALUE",
                       help="drive an input pin at t=0, e.g. PTC6=1, or set a part, e.g. SW1=H, BTN1=pressed "
                            "(repeatable)")
        p.add_argument("--at", action="append", default=[], metavar="TIME:PIN=LEVEL|TIME:PART=VALUE|TIME:UARTn=TEXT",
                       help="drive a pin at a virtual time, e.g. 50ms:PTC6=0, act on a part, e.g. 5ms:SW1=H, "
                            "2ms:BTN1=pressed, 3ms:CLK1.period=500us, or start typing TEXT into a UART "
                            "receiver then, e.g. 100ms:UART2=help\\r\\n (repeatable)")
        p.add_argument("--uart-in", action="append", default=[], metavar="[UARTn:]TEXT",
                       help="bytes a host sends to a UART receiver from t=0, paced at its baud (escapes allowed), "
                            "default UART0; bytes the receiver is not ready for are dropped, use --at for later input")
        p.add_argument("--adc", action="append", default=[], metavar="CHANNEL=VALUE",
                       help="analog input: an integer is a raw ADC code (ADC0_SE14=2048, ADCINA3=0x800), a decimal or a "
                            "V/mV suffix is volts (PA0=1.65V, ADC123_IN0=1650mV on the STM32; CMP0_IN0=1.2V on a "
                            "Kinetis comparator, which takes volts only)")
        p.add_argument("--spi-flash", action="append", default=[], metavar="SPIn:cs=PIN,jedec=ID,size=N,file=PATH",
                       help="attach a SPI NOR flash to a DSPI, e.g. SPI0:cs=PTC4 (Micron M25P16 by default)")
        p.add_argument("--sd-card", action="append", default=[], metavar="HOST:size=N,kind=sdhc|sdsc,image=PATH",
                       help="insert an SD card into an SD host (SDIO on the STM32, SDHC on the K22), e.g. "
                            "SDIO:size=64M,image=card.img,save=out.img (writeback=1 saves to the image); empty "
                            "socket otherwise")
        p.add_argument("--log-mmio", action="store_true", help="log every peripheral register access")
        p.add_argument("--pin-report", action="store_true", help="after the run, print what the firmware did with each pin (chipem.pinreport)")
        p.add_argument("--vcd", default=None, help="write pin waveforms to a VCD file (board: one file per chip, x.A.vcd)")
        p.add_argument("--board", default=None, metavar="BOARD.yaml",
                       help="run several chips connected by nets/links (boards/*.yaml); pin, UART and ADC names "
                            "may then be qualified with a chip id, e.g. --at 5ms:B.PTC6=0")
        p.add_argument("--quantum", default=None, metavar="TIME",
                       help="board lockstep quantum: max time one chip runs ahead of the others (default 10us)")

    # Subcommands run/shell/serve live in their own modules (chipem/cmd_run.py, cmd_shell.py, cmd_serve.py),
    # each exposing add_args(parser) and main(args) -> int.  They are imported lazily so a missing module only
    # breaks its own subcommand.
    for name, hlp in (("run", "run firmware and log pin activity"), ("shell", "interactive REPL"),
                      ("serve", "browser UI")):
        p = sub.add_parser(name, help=hlp)
        common(p, optional_fw=True)
        session_opts(p)
        try:
            mod = importlib.import_module(f"chipem.cmd_{name}")
        except ImportError:
            p.set_defaults(func=cmd_not_implemented)
            continue
        if hasattr(mod, "add_args"):
            mod.add_args(p)
        p.set_defaults(func=mod.main)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except (FileNotFoundError, ValueError) as e:
        print(f"chipem: error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
