"""MCS-51 (8051) interrupt architecture: IE / IP / IPH, four priority levels, polling order, LCALL to the vector,
RETI, the "one more instruction" rule, reset and the CPU's own SFRs on the bus.

Signal path (Intel MCS-51 user's manual, "Interrupts"; NXP P89CV51RB2/RC2/RD2 data sheet section 6.11):

  peripheral flag (TF0, IE0, RI|TI, SPIF&SPIE, CF|CCFn, TF2|EXF2) --set_irq(line, level)--> source flag
  source flag & IE enable bit & IE.EA --> requests at priority (IPH.x:IP.x) = 0..3
  service(): the highest requesting level above every level in progress (one "priority-level-active" flip-flop per
  level) wins; ties go to the polling order of the chip table.  Taking it: the level's flip-flop is set, the
  hardware-cleared flags of that source are acknowledged (`on_vector[line]()`: TF0/TF1, IE0/IE1 in edge mode), and
  the core performs the LCALL (PC pushed low byte first, 2 machine cycles).  RETI (StopReason.EXC_RETURN) clears the
  highest active flip-flop.
  After RETI and after any write to IE / IP / IPH at least one more instruction executes before an interrupt is
  taken (`core.inhibit`; the Chip's masked-slice path then runs exactly one instruction before `service()` again).

The source table comes from the chip YAML (`mcs51: {interrupts: [...]}`): name, vector, enable (bit index in IE and
in IP/IPH), lines (peripheral line names whose levels are ORed), poll (polling order, 1 = first), pd_wake (the source
can end power-down).  `irqs:` maps interrupt numbers n (vector = 8n + 3) to the same names; `exc` numbers used in
traces and statistics are those n.

CPU SFRs (ACC, B, PSW, SP, DPL, DPH and AUXR1 when configured) live in the core; the arch maps an MMIO block `CPU`
over their addresses so tools and the Bus see them.  IE/IP/IPH are the `INTC` block.  Both are sparse blocks (the
Bus maps only their `ranges`): their offsets are SFR addresses, their base is the SFR space tag 0x30000.
"""
from __future__ import annotations

from typing import Any, Callable

from ...core.base import StopReason
from ...periph.base import MMIOBlock, Reg

SFR_BASE = 0x30000
REG_NAMES = ("pc", "a", "b", "psw", "sp", "dptr", "r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7", "dps")
DEFAULT_SFRS = {"ie": 0xA8, "ip": 0xB8, "iph": None, "auxr1": None}
DEFAULT_SOURCES = [                                    # the 8051 base set (8052 adds Timer 2 at 0x2B)
    {"name": "INT0", "vector": 0x03, "enable": 0, "lines": ["IE0"], "poll": 1, "pd_wake": True},
    {"name": "TIMER0", "vector": 0x0B, "enable": 1, "lines": ["TF0"], "poll": 2},
    {"name": "INT1", "vector": 0x13, "enable": 2, "lines": ["IE1"], "poll": 3, "pd_wake": True},
    {"name": "TIMER1", "vector": 0x1B, "enable": 3, "lines": ["TF1"], "poll": 4},
    {"name": "SERIAL", "vector": 0x23, "enable": 4, "lines": ["UART"], "poll": 5},
]


class MCS51Fault(Exception):
    """Unrecoverable interrupt-model condition."""


def _int(v: Any) -> int:
    return int(v, 0) if isinstance(v, str) else int(v)


class _Source:
    __slots__ = ("idx", "name", "vector", "num", "bit", "lines", "poll", "pd_wake")

    def __init__(self, idx: int, d: dict):
        self.idx = idx
        self.name = str(d["name"])
        self.vector = _int(d["vector"])
        self.num = (self.vector - 3) // 8
        self.bit = 1 << _int(d["enable"])
        self.lines = [str(x) for x in (d.get("lines") or [self.name])]
        self.poll = _int(d.get("poll", idx + 1))
        self.pd_wake = bool(d.get("pd_wake", False))


class _SparseBlock(MMIOBlock):
    """MMIO block whose registers sit at scattered SFR addresses: offset = SFR address, base = the SFR tag, and the
    Bus maps only `ranges` [(absolute base, size)]."""

    def __init__(self, name: str, regs: list[Reg]):
        super().__init__(name, SFR_BASE, 0x100, regs, ram_like=False)
        offs = sorted(o for r in regs for o in range(r.offset, r.offset + r.width))
        rng: list[list[int]] = []
        for o in offs:
            if rng and rng[-1][0] + rng[-1][1] == o:
                rng[-1][1] += 1
            else:
                rng.append([o, 1])
        self.ranges = [(SFR_BASE + o, n) for o, n in rng]


class CpuSfrBlock(_SparseBlock):
    """The core's own SFRs as an MMIO block (Bus / tools view; the core itself accesses them inline)."""
    model = "mcs51.cpu"

    def __init__(self, arch: "MCS51Arch", addrs: dict[str, int]):
        self.arch = arch
        regs = [Reg(n, a, 1, reset, on_read=self._rd, on_write=self._wr)
                for n, a, reset in [("SP", 0x81, 0x07), ("DPL", 0x82, 0), ("DPH", 0x83, 0), ("PSW", 0xD0, 0),
                                    ("ACC", 0xE0, 0), ("B", 0xF0, 0)]]
        if addrs.get("auxr1") is not None:
            regs.append(Reg("AUXR1", addrs["auxr1"], 1, 0, on_read=self._rd, on_write=self._wr))
        super().__init__("CPU", regs)

    def _rd(self, blk, reg):
        return self.arch.chip.core._sfr_read_any(reg.offset)

    def _wr(self, blk, reg, old, new):
        self.arch.chip.core._sfr_write_any(reg.offset, new)
        return None


class IntcBlock(_SparseBlock):
    """IE / IP / IPH."""
    model = "mcs51.intc"

    def __init__(self, arch: "MCS51Arch", addrs: dict[str, int]):
        self.arch = arch
        regs = [Reg("IE", addrs["ie"], 1, 0, on_write=self._wr), Reg("IP", addrs["ip"], 1, 0, on_write=self._wr)]
        if addrs.get("iph") is not None:
            regs.append(Reg("IPH", addrs["iph"], 1, 0, on_write=self._wr))
        super().__init__("INTC", regs)

    def _wr(self, blk, reg, old, new):
        self.arch.config_written()
        return None


class MCS51Arch:
    fault_types = (MCS51Fault,)

    def __init__(self, chip: Any):
        self.chip = chip
        desc = getattr(chip, "desc", None)
        cfg = dict((getattr(desc, "extra", None) or {}).get("mcs51") or {})
        addrs = dict(DEFAULT_SFRS)
        for k, v in (cfg.get("sfr") or {}).items():
            addrs[k] = None if v is None else _int(v)
        self.addrs = addrs
        srcs = cfg.get("interrupts") or DEFAULT_SOURCES
        self.sources = [_Source(i, d) for i, d in enumerate(srcs)]
        self._by_line: dict[str, _Source] = {}
        for s in self.sources:
            for ln in s.lines:
                self._by_line[ln.upper()] = s
            self._by_line.setdefault(s.name.upper(), s)
        self._by_num = {s.num: s for s in self.sources}
        self._poll = sorted(self.sources, key=lambda s: s.poll)
        self.irqs: dict[int, str] = dict(getattr(desc, "irqs", None) or {})
        self.reset_sp = _int(cfg.get("reset_sp", 0x07))
        self.boot_pc = 0                     # set by the family (boot vector / status bit) during peripheral reset
        self.cpu_block = CpuSfrBlock(self, addrs)
        self.intc = IntcBlock(self, addrs)
        self.blocks = [self.cpu_block, self.intc]
        self.scs = self.intc
        self.levels: dict[str, bool] = {}
        self._unknown: set[str] = set()
        self.flags = 0                       # bit i: source i has an asserted line
        self.active: list[int] = []          # priority levels in progress (flip-flops), innermost last
        self._active_src: list[_Source] = []
        self.asserted: set[int] = set()
        self.lockup = False
        self.reset_requested: str | None = None
        self.reset_cause = "por"
        self.entries = 0
        self.trace = None                    # cb("enter", interrupt number)
        self.last_vector: int | None = None
        self.on_vector: dict[str, Callable[[], None]] = {}
        self.ie_listeners: list[Callable[[], None]] = []     # called after every IE / IP / IPH write
        self.reg_names = REG_NAMES
        core = chip.core
        if addrs.get("auxr1") is not None and hasattr(core, "configure"):
            core.configure(auxr1=addrs["auxr1"])

    # ------------------------------------------------------------------ helpers
    @property
    def core(self):
        return self.chip.core

    def _log(self, kind: str, **fields) -> None:
        log = getattr(self.chip, "log", None)
        if log is not None:
            log.event(kind, self.chip.now_ns(), **fields)

    def source(self, line: Any) -> _Source:
        if isinstance(line, int):
            s = self._by_num.get(line)
        else:
            s = self._by_line.get(str(line).upper())
            if s is None:
                for n, nm in self.irqs.items():
                    if nm.upper() == str(line).upper():
                        s = self._by_num.get(n)
        if s is None:
            raise ValueError(f"unknown 8051 interrupt line {line!r}")
        return s

    def exc_name(self, n: int) -> str:
        s = self._by_num.get(n)
        return self.irqs.get(n) or (s.name if s else f"INT#{n}")

    def exc_label(self, n: int) -> str:
        s = self._by_num.get(n)
        return f"0x{s.vector:04X}" if s else f"#{n}"

    @property
    def ie(self) -> int:
        return self.intc.get("IE")

    def _prio(self, s: _Source) -> int:
        ip = self.intc.get("IP")
        iph = self.intc.get("IPH") if "IPH" in self.intc.regs else 0
        return (2 if iph & s.bit else 0) | (1 if ip & s.bit else 0)

    # ------------------------------------------------------------------ peripheral lines
    def set_irq(self, line: Any, level: bool = True) -> None:
        key = line if isinstance(line, str) else str(line)
        level = bool(level)
        if self.levels.get(key) is level:
            return
        self.levels[key] = level
        try:
            s = self.source(line)
        except ValueError:
            if level and key not in self._unknown:
                self._unknown.add(key)
                self._log("warn", msg=f"interrupt line {key!r} has no source in the chip's mcs51.interrupts table "
                          "(ignored)")
            return
        bit = 1 << s.idx
        if level:
            self.asserted.add(s.num)
            if not self.flags & bit:
                self.flags |= bit
                self._maybe_stop()
            return
        if any(self.levels.get(ln) for ln in s.lines if ln != key) or \
                (key != s.name and self.levels.get(s.name)):
            return
        self.flags &= ~bit
        self.asserted.discard(s.num)

    def pulse_irq(self, line: Any) -> None:
        self.set_irq(line, True)

    def _maybe_stop(self) -> None:
        core = self.core
        if getattr(core, "running", False) and self.best_pending() is not None:
            core.request_stop(from_mmio=True)

    def config_written(self) -> None:
        """IE / IP / IPH written: one more instruction runs before an interrupt; a newly takeable one ends the
        slice so the Chip loop services it right after that instruction."""
        core = self.core
        core.inhibit = True
        for cb in list(self.ie_listeners):
            cb()
        if getattr(core, "running", False) and self.best_pending() is not None:
            core.request_stop(from_mmio=True)

    def enabled(self, line: str) -> bool:
        """EA and the IE bit of the source of `line` are set (the line's flag would request an interrupt)."""
        ie = self.intc.get("IE")
        if not ie & 0x80:
            return False
        try:
            return bool(ie & self.source(line).bit)
        except ValueError:
            return False

    # ------------------------------------------------------------------ Chip slice-loop interface
    @property
    def pending(self) -> bool:
        return bool(self.flags) and bool(self.intc.get("IE") & 0x80) and self._enabled_flags() != 0

    def _enabled_flags(self) -> int:
        ie = self.intc.get("IE")
        m = 0
        f = self.flags
        for s in self.sources:
            if f >> s.idx & 1 and ie & s.bit:
                m |= 1 << s.idx
        return m

    def _select(self) -> _Source | None:
        ie = self.intc.get("IE")
        if not ie & 0x80 or not self.flags:
            return None
        cur = self.active[-1] if self.active else -1
        best, best_lvl = None, cur
        f = self.flags
        for s in self._poll:
            if f >> s.idx & 1 and ie & s.bit:
                lvl = self._prio(s)
                if lvl > best_lvl:
                    best, best_lvl = s, lvl
        return best

    def best_pending(self) -> int | None:
        """Interrupt number that would be taken now (ignoring the one-more-instruction rule), else None."""
        s = self._select()
        return None if s is None else s.num

    def wakeup_pending(self) -> bool:
        """Idle ends on any enabled interrupt (flag & its IE bit & EA)."""
        return self.intc.get("IE") & 0x80 != 0 and self._enabled_flags() != 0

    def pd_wakeup_pending(self) -> bool:
        """Power-down ends only on an enabled external interrupt that can wake it (level-sensitive INT0/INT1)."""
        if not self.intc.get("IE") & 0x80:
            return False
        ie = self.intc.get("IE")
        return any(s.pd_wake and self.flags >> s.idx & 1 and ie & s.bit for s in self.sources)

    def sleep_on_exit(self) -> bool:
        return False

    def service(self) -> bool:
        core = self.core
        if core.inhibit:
            return False
        s = self._select()
        if s is None:
            return False
        self.active.append(self._prio(s))
        self._active_src.append(s)
        for ln in s.lines:
            if self.levels.get(ln):
                ack = self.on_vector.get(ln)
                if ack is not None:
                    ack()
        core.take_interrupt(s.vector)
        self.entries += 1
        self.last_vector = s.num
        if self.trace is not None:
            self.trace("enter", s.num)
        return True

    def on_stop(self, reason: StopReason, info: Any) -> bool:
        if reason is StopReason.EXC_RETURN:                # RETI: clear the highest priority-level flip-flop
            if self.active:
                self.active.pop()
                self._active_src.pop()
            return True
        if reason is StopReason.FAULT:
            return False
        return True

    def request_reset(self, reason: str) -> None:
        self.reset_cause = reason
        self.reset_requested = reason
        core = self.core
        if getattr(core, "running", False):
            core.request_stop(from_mmio=True)

    # ------------------------------------------------------------------ reset
    def reset(self, entry: int | None = None) -> None:
        for blk in self.blocks:
            blk.reset()
        self.active.clear()
        self._active_src.clear()
        self.lockup = False
        self.reset_requested = None
        self.last_vector = None
        self.core.reset(self.boot_pc, self.reset_sp)
        for blk in list(self.chip.peripherals.values()):     # family hooks that need the core reset first
            f = getattr(blk, "after_reset", None)
            if f is not None:
                f()
        self.reset_cause = "rst"             # the next reset is a RST-pin / user reset unless a model says otherwise

    # ------------------------------------------------------------------ introspection
    def vector_table(self) -> list[dict]:
        out = []
        core = self.core
        for s in sorted(self.sources, key=lambda x: x.vector):
            try:
                first = core.read_mem(s.vector, 3)
            except Exception:
                first = b""
            target = None
            if first[:1] == b"\x02" and len(first) == 3:          # LJMP handler
                target = first[1] << 8 | first[2]
            elif first and first[0] & 0x1F == 0x01 and len(first) >= 2:   # AJMP handler
                target = ((s.vector + 2) & 0xF800) | ((first[0] >> 5) << 8) | first[1]
            elif first[:1] == b"\x32":                             # RETI: unused vector
                continue
            elif first in (b"", b"\xff\xff\xff"):
                continue
            out.append({"num": s.num, "name": s.name, "addr": s.vector, "target": target if target is not None
                        else s.vector})
        return out

    def cpu_state(self) -> dict:
        core = self.core
        vals = dict(zip(REG_NAMES, core.read_regs(REG_NAMES)))
        psw = vals["psw"]
        st = dict(vals)
        act = self._active_src[-1].num if self._active_src else None
        st.update({"cy": psw >> 7, "ac": psw >> 6 & 1, "ov": psw >> 2 & 1, "rs": psw >> 3 & 3, "p": psw & 1,
                   "ie": self.intc.get("IE"), "ip": self.intc.get("IP"),
                   "iph": self.intc.get("IPH") if "IPH" in self.intc.regs else 0,
                   "active": act, "active_name": self.exc_name(act) if act is not None else None,
                   "active_levels": list(self.active), "mode": "handler" if act is not None else "thread",
                   "pending": [s.num for s in self.sources if self.flags >> s.idx & 1],
                   "cycles_per_mc": getattr(core, "clocks_per_mc", 12)})
        return st

    def live_pc(self) -> int:
        f = getattr(self.core, "live_pc", None)
        return f() if f is not None else self.core.pc

    def disasm(self, addr: int, n: int) -> list[tuple[int, bytes, str]]:
        """[(addr, bytes, text)] of n instructions at addr (SFR and bit names from the chip's register table)."""
        from ...core.mcs51.asm import disasm
        names = self.names()
        core = self.core
        return disasm(lambda a: core.code[a & 0xFFFF], addr & 0xFFFF, n, names)

    def names(self, labels: dict[int, str] | None = None):
        from ...core.mcs51.asm import Names, symbols_from_regs
        if getattr(self, "_names", None) is None:
            sfrs, bits = {}, {}
            rf = getattr(self.chip, "_regs_file", None)
            path = rf(self.chip.desc) if callable(rf) else None
            if path:
                try:
                    sfrs, bits = symbols_from_regs(path)
                except Exception:
                    sfrs, bits = {}, {}
            if self.addrs.get("auxr1") is not None:
                sfrs.setdefault("AUXR1", self.addrs["auxr1"])
            if self.addrs.get("iph") is not None:
                sfrs.setdefault("IPH", self.addrs["iph"])
            self._names = Names.standard(sfrs, bits)
        if labels:
            from ...core.mcs51.asm import Names as _N
            return _N(self._names.sfrs, self._names.bits, dict(labels))
        return self._names

    def debug_support(self):
        """Debugger hooks for chipem/debug.py (registers, decode, stack walk)."""
        from .debug import MCS51DebugSupport
        return MCS51DebugSupport(self)

    def backtrace(self, max_frames: int = 16) -> list[tuple[int, int]]:
        """Best-effort call stack from the 8-bit stack: [(stack address, return address)] for every pushed 16-bit
        value that points just after an ACALL/LCALL (or is an interrupted PC when an interrupt is active)."""
        core = self.core
        sp = core.sfr[0x81]
        out = []
        a = sp
        while a > 0x07 and len(out) < max_frames:
            hi, lo = core.iram[a], core.iram[a - 1]
            ret = hi << 8 | lo
            c = core.code
            if (ret >= 3 and c[ret - 3] == 0x12) or (ret >= 2 and c[ret - 2] & 0x1F == 0x11):
                out.append((a - 1, ret))
                a -= 2
            else:
                a -= 1
        return out
