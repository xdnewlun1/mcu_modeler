"""`chipem doctor`: report which requirements and optional tools are present and what that enables.

Cheap and offline: package versions come from importlib.metadata, tools from shutil.which plus a `--version` call
with a short timeout, and the Unicorn instruction-counter calibration (chipem/core/unicorn_cm.py) runs once in
this process.  Never raises; the exit status is 1 only when a *required* piece is missing (Python too old, a
required package not importable, or the checkout's chips/ directory not found), else 0.  See README "Requirements".
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import __version__

ROOT = Path(__file__).resolve().parent.parent          # the checkout (chips/, toolchains/, firmware/)
MIN_PYTHON = (3, 12)

# (distribution name, import name, what it is for)
REQUIRED_PKGS = (
    ("unicorn", "unicorn", "ARM (Cortex-M) execution"),
    ("capstone", "capstone", "disassembly (info, shell, MMIO stop fix-up)"),
    ("pyelftools", "elftools", "ELF / DWARF loading"),
    ("PyYAML", "yaml", "chip, register and board YAML"),
)


@dataclass
class Check:
    group: str                     # required | engine | checkout | optional | tests | build | docs | ui-dev
    name: str
    ok: bool
    required: bool = False
    version: str = ""
    path: str = ""
    detail: str = ""               # what it enables, or what is unavailable without it
    extra: dict = field(default_factory=dict)


def _dist_version(dist: str) -> str:
    try:
        return importlib.metadata.version(dist)
    except Exception:
        return ""


def _importable(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


def _tool_version(path: str, args=("--version",), pattern: str | None = None) -> str:
    """First line (or first regex match) of `path --version`; '' on any failure.  5 s timeout."""
    try:
        r = subprocess.run([path, *args], capture_output=True, text=True, timeout=5)
    except Exception:
        return ""
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    if pattern:
        m = re.search(pattern, out)
        return m.group(1) if m else ""
    for line in out.splitlines():
        if line.strip():
            return line.strip()[:80]
    return ""


def _which(*names: str) -> tuple[str, str]:
    """(name, path) of the first of `names` found on PATH, else ('', '')."""
    for n in names:
        p = shutil.which(n)
        if p:
            return n, p
    return "", ""


def _calibration() -> tuple[bool, str]:
    try:
        from .core import unicorn_cm
        core = unicorn_cm.UnicornCortexM("cortex-m4")
        if core.exact_counts:
            return True, "exact instruction counts in fast mode"
        return False, ("calibration failed" + (f" ({unicorn_cm._CAL_ERROR})" if unicorn_cm._CAL_ERROR else "")
                       + ": early stops are charged half a slice; use --precise for exact timing")
    except Exception as e:
        return False, f"could not run: {type(e).__name__}: {e}"


def _find_cl2000(root: Path, env=None) -> str:
    """cl2000 as firmware/samples_c2000_c/build.sh finds it: $TI_CGT, toolchains/ti-cgt-c2000, newest
    toolchains/**/ti-cgt-c2000*/bin/cl2000."""
    env = os.environ if env is None else env
    cands = []
    if env.get("TI_CGT"):
        cands.append(Path(env["TI_CGT"]) / "bin" / "cl2000")
    cands.append(root / "toolchains" / "ti-cgt-c2000" / "bin" / "cl2000")
    tc = root / "toolchains"
    if tc.is_dir():
        try:
            cands += sorted(tc.glob("**/ti-cgt-c2000*/bin/cl2000"), reverse=True)
        except OSError:
            pass
    for c in cands:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return ""


def collect(root: Path = ROOT, env=None) -> list[Check]:
    checks: list[Check] = []
    add = checks.append

    # ---- required
    py = sys.version_info
    add(Check("required", "python", py >= MIN_PYTHON, True, f"{py.major}.{py.minor}.{py.micro}", sys.executable,
              f"needs >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}"))
    for dist, mod, what in REQUIRED_PKGS:
        ok = _importable(mod)
        ver = _dist_version(dist)
        detail = what if ok else f"missing: pip install {dist}"
        if ok and dist == "unicorn" and ver and not ver.startswith("2.1."):
            detail += f"; tested with 2.1.x (the counter calibration reads Unicorn 2.1 internals)"
        add(Check("required", dist, ok, True, ver, "", detail))

    # ---- engine self-test
    if _importable("unicorn"):
        ok, detail = _calibration()
    else:
        ok, detail = False, "unicorn not installed"
    add(Check("engine", "unicorn counter calibration", ok, False, "", "", detail))

    # ---- checkout layout (chips/ and the web assets are found relative to the source tree)
    chips = root / "chips"
    n = len([p for p in chips.glob("*.yaml") if not p.stem.endswith("_regs")]) if chips.is_dir() else 0
    add(Check("checkout", "chips/", n > 0, True, str(n) if n else "", str(chips),
              "chip descriptions (chips/*.yaml)" if n else "not found: run from a source checkout installed with pip install -e"))
    static = Path(__file__).resolve().parent / "web" / "static"
    add(Check("checkout", "chipem/web/static", static.is_dir(), False, "", str(static),
              "browser UI (chipem serve)" if static.is_dir() else "missing: chipem serve has no UI"))

    # ---- optional run-time
    ok = _importable("serial")
    add(Check("optional", "pyserial", ok, False, _dist_version("pyserial"), "",
              "--uart-port and serial: ports" if ok else "no --uart-port / serial: ports (pip install pyserial)"))
    ok = hasattr(os, "openpty") and sys.platform != "win32"
    add(Check("optional", "pty", ok, False, "", "", "--uart-pty" if ok else "no --uart-pty on this OS"))
    name, path = _which("gdb-multiarch", "arm-none-eabi-gdb", "gdb")
    add(Check("optional", "gdb", bool(path), False, _tool_version(path, pattern=r"GNU gdb.*?(\d+\.\d+\S*)") if path else "",
              path, f"{name} for --gdb (ARM support needed for Cortex-M)" if path
              else "no debugger for --gdb (the GDB server itself still runs)"))

    # ---- tests
    for dist, mod, what in (("pytest", "pytest", "test runner"),
                            ("keystone-engine", "keystone", "assembles test programs (tests/engine_util.py)")):
        ok = _importable(mod)
        add(Check("tests", dist, ok, False, _dist_version(dist), "", what if ok else f"missing: {what}"))

    # ---- building sample firmware
    name, path = _which("clang")
    add(Check("build", "clang", bool(path), False, _tool_version(path, pattern=r"clang version (\S+)") if path else "",
              path, "ARM samples (firmware/samples*/build.sh)" if path else "cannot rebuild ARM samples"))
    name, path = _which("ld.lld")
    add(Check("build", "ld.lld", bool(path), False, _tool_version(path, pattern=r"LLD (\S+)") if path else "",
              path, "linker for the ARM samples" if path else "cannot link ARM samples"))
    name, path = _which("llvm-objcopy")
    add(Check("build", "llvm-objcopy", bool(path), False, "", path,
              ".bin from .elf" if path else "optional: firmware/samples/elf2bin.py is used instead"))
    cl = _find_cl2000(root, env)
    ver = ""
    if cl:
        m = re.search(r"ti-cgt-c2000_([\w.]+)", cl)
        ver = m.group(1) if m else _tool_version(cl, ("--compiler_revision",))
    add(Check("build", "cl2000 (TI C2000 CGT)", bool(cl), False, ver, cl,
              "C28x C samples (firmware/samples_c2000_c/build.sh)" if cl
              else "cannot rebuild firmware/samples_c2000_c (set TI_CGT or install into toolchains/)"))
    name, path = _which("sdcc", "sdcc-sdcc")
    tools = {}
    for key, names in (("as", ("sdas8051", "sdcc-sdas8051")), ("sim", ("s51", "sdcc-s51", "ucsim_51", "sdcc-ucsim_51"))):
        tools[key] = _which(*names)[0]
    add(Check("build", "sdcc", bool(path), False,
              _tool_version(path, pattern=r"(\d+\.\d+\.\d+)") if path else "", path,
              (f"8051 C ({name}; assembler {tools['as'] or 'not found'}, simulator {tools['sim'] or 'not found'})")
              if path else "no 8051 C compiler (the assembler samples use tools/mcs51_asm.py)",
              {"names": {"cc": name, **tools}}))

    # ---- adding chips / docs
    for tool in ("pdftotext", "pdfinfo"):
        name, path = _which(tool)
        add(Check("docs", tool, bool(path), False,
                  _tool_version(path, ("-v",), r"version (\S+)") if path else "", path,
                  "reading datasheets (poppler-utils)" if path else "missing: poppler-utils"))

    # ---- UI development
    name, path = _which("node")
    add(Check("ui-dev", "node", bool(path), False, _tool_version(path) if path else "", path,
              "node --check on chipem/web/static/*.js" if path else "UI syntax-check tests are skipped"))
    env_ = os.environ if env is None else env
    path = env_.get("CHIPEM_UI_BROWSER", "") or _which("firefox")[1]       # as tools/ui_shots.py looks for it
    add(Check("ui-dev", "firefox", bool(path), False,
              _tool_version(path, pattern=r"Firefox (\S+)") if path else "", path,
              "headless screenshots (tools/ui_shots.py)" if path else "no headless UI screenshots"))
    return checks


def features(checks: list[Check]) -> list[tuple[str, bool]]:
    c = {x.name: x for x in checks}
    req = all(x.ok for x in checks if x.required)

    def ok(n):
        return n in c and c[n].ok
    return [
        ("run / info / shell / serve", req),
        ("exact fast-mode timing", ok("unicorn counter calibration")),
        ("browser UI assets", ok("chipem/web/static")),
        ("--uart-port (pyserial)", ok("pyserial")),
        ("--uart-pty", ok("pty")),
        ("gdb client for --gdb", ok("gdb")),
        ("test suite", ok("pytest") and ok("keystone-engine")),
        ("rebuild ARM samples", ok("clang") and ok("ld.lld")),
        ("rebuild C28x C samples", ok("cl2000 (TI C2000 CGT)")),
        ("8051 C (SDCC)", ok("sdcc")),
        ("datasheet text (pdftotext)", ok("pdftotext")),
        ("UI JS syntax tests (node)", ok("node")),
        ("UI screenshots (firefox)", ok("firefox")),
    ]


def report(checks: list[Check]) -> str:
    lines = [f"chipem {__version__} doctor  (checkout {ROOT})"]
    group = None
    for x in checks:
        if x.group != group:
            group = x.group
            lines.append(f"\n[{group}]")
        mark = "ok  " if x.ok else ("FAIL" if x.required else "--  ")
        lines.append(f"  {mark} {x.name:<28} {x.version[:15]:<15} {x.detail}")
    lines.append("\n[features]")
    for name, on in features(checks):
        lines.append(f"  {'yes' if on else 'no ':<4}{name}")
    missing = [x.name for x in checks if x.required and not x.ok]
    lines.append("\n" + ("required: all present" if not missing else f"required missing: {', '.join(missing)}"))
    return "\n".join(lines)


def add_args(p) -> None:
    p.add_argument("--json", action="store_true", help="machine-readable output")


def main(args) -> int:
    try:
        checks = collect()
    except Exception as e:  # pragma: no cover - doctor must never crash
        print(f"chipem doctor: internal error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    missing = any(x.required and not x.ok for x in checks)
    if getattr(args, "json", False):
        print(json.dumps({"version": __version__, "root": str(ROOT), "checks": [asdict(x) for x in checks],
                          "features": dict(features(checks)), "required_ok": not missing}, indent=2))
    else:
        print(report(checks))
    return 1 if missing else 0
