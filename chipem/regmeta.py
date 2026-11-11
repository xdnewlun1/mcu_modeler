"""CPU register metadata for user interfaces: width in bits and display format per register name.

The web UI (header register popover, Debug tab) formats register values from this data instead of guessing from
the architecture: `{name: {"bits": n, "fmt": "hex" | "dec" | "flag" | "addr"}}`. An arch may provide its own table
through `arch.reg_meta()`; otherwise the defaults below apply, chosen by `chip.arch_kind`. Names not listed are
state fields, not registers (counters, mode names, lists), and the UI shows them as plain values.
"""
from __future__ import annotations

from typing import Any

_ARM = {**{f"r{i}": 32 for i in range(13)}, "sp": 32, "lr": 32, "pc": 32, "msp": 32, "psp": 32, "xpsr": 32,
        "control": 32, "primask": 32, "basepri": 32, "faultmask": 32, "fpscr": 32, "vtor": 32,
        **{f"s{i}": 32 for i in range(32)}}
_C28X = {"pc": 22, "rpc": 22, "sp": 16, "acc": 32, "p": 32, "xt": 32, **{f"xar{i}": 32 for i in range(8)},
         **{f"ar{i}": 16 for i in range(8)}, "dp": 16, "st0": 16, "st1": 16, "ier": 16, "ifr": 16, "dbgier": 16,
         **{f"r{i}h": 32 for i in range(8)}, "stf": 32, "pieack": 16}
_MCS51 = {"pc": 16, "a": 8, "b": 8, "psw": 8, "sp": 8, "dptr": 16, "dptr0": 16, "dptr1": 16, "dps": 8,
          **{f"r{i}": 8 for i in range(8)}, "ie": 8, "ip": 8, "iph": 8, "rs": 2}
_ADDR = {"pc", "rpc", "lr", "sp", "msp", "psp"}
_TABLES = {"armv7m": (_ARM, set()), "c28x": (_C28X, {"intm", "eallow", "vmap", "enpie"}),
           "mcs51": (_MCS51, {"cy", "ac", "ov", "f0"})}


def cpu_reg_meta(chip: Any) -> dict[str, dict]:
    """{register name: {"bits", "fmt"}} for the chip's CPU (see the module docstring)."""
    f = getattr(getattr(chip, "arch", None), "reg_meta", None)
    if callable(f):
        try:
            return dict(f())
        except Exception:
            pass
    kind = getattr(chip, "arch_kind", "armv7m")
    widths, flags = _TABLES.get(kind, _TABLES["armv7m"])
    out = {n: {"bits": b, "fmt": "addr" if n in _ADDR else "hex"} for n, b in widths.items()}
    if kind == "mcs51":
        out["rs"] = {"bits": 2, "fmt": "dec"}
    for n in flags:
        out[n] = {"bits": 1, "fmt": "flag"}
    if kind == "mcs51":                         # 'p' is the parity flag in the 8051 state, not a register
        out["p"] = {"bits": 1, "fmt": "flag"}
    return out
