"""Static analysis of MCS-51 (8051) images for `chipem info` (called from chipem.firmware.analyze / format_report).

* reset: the instruction at 0000H (LJMP / AJMP / SJMP target, or code falling through) -> reset PC;
* interrupt vectors: the chip's source table (chip YAML `mcs51.interrupts`): every vector slot holding code (LJMP /
  AJMP / SJMP / RETI) -> handler address;
* recursive-descent disassembly from the reset PC and every handler (branches, calls, jump tables via JMP @A+DPTR are
  not followed) -> SFR references per peripheral block (direct and bit operands >= 80H, named through the chip's
  register table) and stores (MOV/ORL/ANL/XRL/INC/DEC/POP/XCH/DJNZ to an SFR, SETB/CLR/CPL/MOV bit,C/JBC on an SFR
  bit);
* port pins written bit-wise (SETB/CLR/CPL/MOV Px.y,C) or byte-wise (MOV/ORL/ANL/XRL Px) -> provisional pin roles.
"""
from __future__ import annotations

import time
from typing import Any

from .core.mcs51.isa import OPCODES, operands

STORE_DIR = {0x42, 0x43, 0x52, 0x53, 0x62, 0x63, 0x05, 0x15, 0xD5, 0x75, 0x85, 0xF5, 0xD0, 0xC5} | set(range(0x86, 0x90))
STORE_BIT = {0xC2, 0xD2, 0xB2, 0x92, 0x10}
STOP_OPS = {0x22, 0x32, 0x02, 0x80, 0x73} | {p << 5 | 0x01 for p in range(8)}


def _sfr_owner(desc: Any) -> dict[int, tuple[str, str]]:
    """SFR address -> (block name, register name) from the chip's register table."""
    from pathlib import Path
    from .regtable import load_regs
    out: dict[int, tuple[str, str]] = {}
    rf = getattr(desc, "regs_file", None)
    src = getattr(desc, "source_path", None)
    if not rf:
        return out
    path = Path(rf)
    if not path.is_absolute() and src is not None:
        path = Path(src).parent / path
    if not path.exists():
        return out
    rt = load_regs(path)
    blocks = {p.name: p for p in desc.peripherals}
    for pname in rt.names():
        blk = pname
        for b in blocks:                                     # the chip YAML's block name for this table entry
            if b.upper() == pname.upper():
                blk = b
        for r in rt.peripheral(pname).registers:
            out[r.offset] = (blk, r.name)
    return out


def analyze_mcs51(image: Any, desc: Any, *, entry: int | None = None, disasm: bool = True,
                  time_budget: float = 4.0):
    from .firmware import AnalysisReport, PeriphRef
    code: dict[int, int] = {}
    segs = []
    for a, d in image.segments:
        for i, b in enumerate(d):
            code[a + i] = b
        m = desc.memory_at(a)
        segs.append((a, len(d), m.name if m else "?"))

    def rd(a):
        return code.get(a & 0xFFFF, 0xFF)

    def target(a):
        op = rd(a)
        if op == 0x02:
            return rd(a + 1) << 8 | rd(a + 2)
        if op & 0x1F == 0x01:
            return ((a + 2) & 0xF800) | ((op >> 5) << 8) | rd(a + 1)
        if op == 0x80:
            r = rd(a + 1)
            return (a + 2 + (r - 256 if r & 0x80 else r)) & 0xFFFF
        return None

    problems = []
    reset_pc = None
    if 0 in code:
        t = target(0)
        reset_pc = t if t is not None else 0
    else:
        problems.append("no code at 0000H (reset)")
    cfg = (getattr(desc, "extra", None) or {}).get("mcs51") or {}
    handlers = []
    for s in cfg.get("interrupts") or []:
        v = int(s["vector"], 0) if isinstance(s["vector"], str) else int(s["vector"])
        if v not in code:
            continue
        if rd(v) == 0x32:
            handlers.append(((v - 3) // 8, f"{s['name']} (RETI)", v))
            continue
        t = target(v)
        handlers.append(((v - 3) // 8, str(s["name"]), t if t is not None else v))
    owners = _sfr_owner(desc)
    refs: dict[str, PeriphRef] = {}
    stores: dict[str, int] = {}
    pins: dict[str, list[str]] = {}
    ninsn = 0
    t0 = time.perf_counter()
    if disasm:
        todo = [a for a in ([reset_pc] if reset_pc is not None else []) + [h[2] for h in handlers]]
        seen: set[int] = set()
        while todo and time.perf_counter() - t0 < time_budget:
            pc = todo.pop()
            while pc not in seen and pc in code:
                seen.add(pc)
                op = rd(pc)
                e = OPCODES[op]
                if e is None:
                    break
                raw = [rd(pc + i) for i in range(e.size)]
                vals = operands(e, pc, raw)
                ninsn += 1
                for k, v in zip(e.ops, vals):
                    sfr = None
                    if k == "dir" and v >= 0x80:
                        sfr = v
                    elif k in ("bit", "/bit") and v >= 0x80:
                        sfr = v & 0xF8
                    if sfr is None:
                        continue
                    blk, reg = owners.get(sfr, ("?", f"SFR {sfr:02X}H"))
                    ref = refs.get(blk)
                    if ref is None:
                        ref = refs[blk] = PeriphRef(blk)
                    ref.count += 1
                    if len(ref.samples) < 8 and (0x30000 + sfr) not in ref.samples:
                        ref.samples.append(0x30000 + sfr)
                    store = (k == "dir" and op in STORE_DIR) or (k == "bit" and op in STORE_BIT)
                    if op == 0x85 and k == "dir" and v != vals[0]:
                        store = False                              # MOV dst,src: only the destination is stored
                    if store:
                        stores[blk] = stores.get(blk, 0) + 1
                        if reg in ("P0", "P1", "P2", "P3"):
                            port = reg[1]
                            if k == "bit":
                                nm = f"P{port}.{v & 7}"
                                note = {0xC2: "CLR", 0xD2: "SETB", 0xB2: "CPL", 0x92: "MOV bit,C", 0x10: "JBC"}.get(
                                    op, e.mnem)
                                pins.setdefault(nm, []).append(f"{note} at 0x{pc:04X}")
                            else:
                                pins.setdefault(f"P{port}", []).append(f"{e.mnem} (byte) at 0x{pc:04X}")
                npc = (pc + e.size) & 0xFFFF
                for k, v in zip(e.ops, vals):
                    if k in ("rel", "a11", "a16") and v not in seen:
                        todo.append(v)
                if op in STOP_OPS:
                    break
                pc = npc
    secs = time.perf_counter() - t0
    ports = {}
    for k, v in pins.items():
        ports[k] = v
    return AnalysisReport(
        chip=desc.name, source=image.source, fmt=image.fmt, segments=segs, load_addr=image.start, vtor=0,
        initial_sp=int(cfg.get("reset_sp", 7)), reset_pc=reset_pc, bootable=reset_pc is not None,
        boot_problems=problems, num_vectors=len(handlers), default_handler=None, handlers=handlers,
        periph_refs=refs, memory_refs={}, store_sites=stores, code_refs={}, pin_roles=[], pins_configured=ports,
        disasm_insns=ninsn, disasm_seconds=secs, warnings=[], arch="mcs51", unit_bytes=1,
        symbols=dict(getattr(image, "symbols", {}) or {}))


def format_mcs51(r: Any) -> str:
    L = []
    A = L.append
    A(f"Firmware: {r.source} ({r.fmt}), chip {r.chip} (MCS-51; CODE space)")
    for a, n, reg in r.segments:
        A(f"  segment 0x{a:04X}..0x{a + n - 1:04X} ({n} bytes) in {reg}")
    A("")
    tgt = "-" if r.reset_pc is None else f"0x{r.reset_pc:04X}"
    A(f"Reset 0x0000 -> {tgt}   SP after reset 0x{r.initial_sp or 0:02X}   "
      f"{'bootable' if r.bootable else 'NOT bootable'}")
    for p in r.boot_problems:
        A(f"  ! {p}")
    if r.symbols:
        code = {k: v for k, v in r.symbols.items() if v < 0x10000}
        A(f"Symbols: {len(r.symbols)} ({len(code)} in CODE)")
    A(f"Interrupt vectors used: {len(r.handlers)}")
    for n, name, v in r.handlers:
        A(f"    [{n}] 0x{8 * n + 3:04X} {name:<16} -> 0x{v:04X}")
    A("")
    A(f"SFR references (recursive disassembly, {r.disasm_insns} instructions, {len(r.periph_refs)} blocks):")
    for name, ref in sorted(r.periph_refs.items()):
        A(f"  {name:<10} {ref.count:5d}  SFRs " + ", ".join(f"{s & 0xFF:02X}H" for s in ref.samples[:8]))
    if r.store_sites:
        A("SFR stores: " + ", ".join(f"{k} {v}" for k, v in sorted(r.store_sites.items()))
          + f"   ({r.disasm_seconds:.2f} s)")
    if r.pins_configured:
        A(f"Port pins written ({len(r.pins_configured)}):")
        for pin, notes in sorted(r.pins_configured.items()):
            A(f"  {pin:<6} " + "; ".join(notes[:3]) + (f" (+{len(notes) - 3} more)" if len(notes) > 3 else ""))
    for w in r.warnings:
        A(f"warning: {w}")
    return "\n".join(L)
