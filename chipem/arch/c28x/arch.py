"""C28x interrupt architecture: PIE grouping, CPU IFR/IER/INTM, vector fetch, reset and ITRAP.

Signal path (TMS320x2833x System Control and Interrupts guide, SPRUFB0, "Peripheral Interrupt Expansion"):

  peripheral line ──rising edge──▶ PIEIFRx.y ──(& PIEIERx.y, PIEACKx clear)──▶ PIEACKx := 1, CPU IFR bit x-1
  CPU: IFR & IER and INTM clear ──▶ service(): highest priority INTx (INT1 first) → IFR bit cleared → if ENPIE (and
  VMAP=1) the PIE picks the highest-priority pending channel y of group x (INTx.1 first), clears PIEIFRx.y and hands
  out vector n = 32 + 8(x-1) + (y-1) from 0x0D00 + 2n; else the CPU vector table (0x3FFFC0 + 2x with VMAP=1, 0x000000
  + 2x with VMAP=0) is used → `core.take_interrupt(target, x)` saves the context and jumps.
  The ISR acknowledges the group by writing 1 to PIEACK bit x-1; a still-pending enabled flag then re-raises INTx.

INT13 (CPU timer 1 / XINT13) and INT14 (CPU timer 2) bypass the PIE (CPU IFR bits 12/13 directly); NMI is vector 18.
Peripheral models drive lines with `MMIOBlock.irq(line, level)` → `set_irq(line, level)`, where `line` is a
"group.channel" string ("1.7"), a (group, channel) tuple, a vector number (int, 32..127 for PIE channels, 13/14),
"INT13"/"INT14"/"NMI", or an IRQ name from the chip YAML (`irqs: {vector_number: name}`).

Core interface used (docs/CONTRACTS.md "16-bit address units and the C28x core"): read_reg/write_reg of ifr, ier,
st0, st1, sp, dp, pc; reset(pc, sp); take_interrupt(target, int_number); request_stop(from_mmio=True); running.
The arch also installs `core.itrap_handler` (illegal opcode → vector 19 ILLEGAL) and `core.vector_fetch(n)`
(22-bit target of vector n through the active table, for TRAP #n / INTR).
"""
from __future__ import annotations

from typing import Any

from ...core.base import StopReason
from .pie import PIE_BASE, PIE_SIZE, PIEBlock

# CPU vector numbers 0..31 (the PIE table repeats them at 0x0D00..0x0D3F)
CPU_VECTOR_NAMES = (["RESET"] + [f"INT{i}" for i in range(1, 15)] + ["DLOGINT", "RTOSINT", "EMUINT", "NMI", "ILLEGAL"]
                    + [f"USER{i}" for i in range(1, 13)])
VEC_ILLEGAL = 19
VEC_NMI = 18
ST1_INTM, ST1_DBGM, ST1_VMAP, ST1_EALLOW, ST1_M0M1MAP, ST1_OBJMODE = 1 << 0, 1 << 1, 1 << 3, 1 << 6, 1 << 11, 1 << 9
ST1_RESET = ST1_INTM | ST1_DBGM | ST1_VMAP | ST1_M0M1MAP | ST1_OBJMODE     # 0x0A0B (OBJMODE set as by the boot ROM)
RESET_SP = 0x0400
DEFAULT_BOOT_ENTRY = 0x33FFF6
BROM_VECTORS = 0x3FFFC0
REG_NAMES = ("pc", "sp", "acc", "p", "xt", "xar0", "xar1", "xar2", "xar3", "xar4", "xar5", "xar6", "xar7", "dp",
             "st0", "st1", "ier", "ifr", "dbgier", "rpc")


class C28xFault(Exception):
    """Unrecoverable interrupt-model condition."""


def pie_vector(group: int, channel: int) -> int:
    return 32 + 8 * (group - 1) + (channel - 1)


def vector_name(n: int, irqs: dict | None = None) -> str:
    if irqs and n in irqs:
        return irqs[n]
    if 0 <= n < 32:
        return CPU_VECTOR_NAMES[n]
    if 32 <= n < 128:
        g, c = divmod(n - 32, 8)
        return f"INT{g + 1}.{c + 1}"
    return f"VEC{n}"


def _int(v: Any) -> int:
    return int(v, 0) if isinstance(v, str) else int(v)


class C28xArch:
    fault_types = (C28xFault,)

    def __init__(self, chip: Any):
        self.chip = chip
        desc = getattr(chip, "desc", None)
        base, size = PIE_BASE, PIE_SIZE
        name = "PIE"
        for p in getattr(desc, "peripherals", None) or []:
            if p.model == "ti_c2000.pie":
                base, size, name = p.base, max(p.size, PIE_SIZE), p.name
        self.pie = PIEBlock(self, name, base, size)
        self.blocks = [self.pie]
        self.scs = self.pie                 # compatibility with code that looks for the arch-owned block
        self.irqs: dict[int, str] = dict(getattr(desc, "irqs", None) or {})
        self._by_name = {v.upper(): k for k, v in self.irqs.items()}
        extra = getattr(desc, "extra", None) or {}
        boot = extra.get("boot") or {}
        self._boot = boot
        self.boot_entry = _int(boot.get("entry", DEFAULT_BOOT_ENTRY))
        self.reset_sp = _int(boot.get("sp", RESET_SP))
        self.asserted: set[int] = set()     # vector numbers whose line is high
        self.active: list[int] = []         # C28x has no active-exception stack visible to us (IRET is a plain insn)
        self.lockup = False
        self.reset_requested: str | None = None
        self.entries = 0
        self.trace = None                   # cb("enter", vector_number)
        self.last_vector: int | None = None
        self._line_cache: dict[Any, int] = {}
        self._warned: set[str] = set()
        self.reg_names = REG_NAMES
        core = chip.core
        try:
            core.itrap_handler = self.itrap
            core.vector_fetch = self.vector_target
        except AttributeError:             # pragma: no cover - core with __slots__
            pass

    # ------------------------------------------------------------------ helpers
    @property
    def core(self):
        return self.chip.core

    def _warn_once(self, key: str, msg: str, **fields) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        self._log("warn", msg=msg, **fields)

    def _log(self, kind: str, **fields) -> None:
        log = getattr(self.chip, "log", None)
        if log is not None:
            f = getattr(self.chip, "now_ns", None)
            log.event(kind, f() if f is not None else 0.0, **fields)

    def exc_name(self, n: int) -> str:
        return vector_name(n, self.irqs)

    def exc_label(self, n: int) -> str:
        """Short source label of vector n: "PIE 1.7" for PIE channels, else the CPU interrupt name."""
        if 32 <= n < 128:
            g, c = divmod(n - 32, 8)
            return f"PIE {g + 1}.{c + 1}"
        return CPU_VECTOR_NAMES[n] if 0 <= n < 32 else f"VEC{n}"

    def line_vector(self, line: Any) -> int:
        """Vector number of an interrupt line given as "x.y", (x, y), vector number, "INT13", "NMI" or YAML name."""
        v = self._line_cache.get(line)
        if v is not None:
            return v
        if isinstance(line, tuple):
            v = pie_vector(int(line[0]), int(line[1]))
        elif isinstance(line, int):
            v = line
        else:
            s = str(line).strip().upper()
            if s in self._by_name:
                v = self._by_name[s]
            elif "." in s and s.replace("INT", "").replace(".", "").isdigit():
                g, c = s.replace("INT", "").split(".")
                v = pie_vector(int(g), int(c))
            elif s in CPU_VECTOR_NAMES:
                v = CPU_VECTOR_NAMES.index(s)
            else:
                raise ValueError(f"unknown C28x interrupt line {line!r}")
        if not (13 <= v <= 14 or v == VEC_NMI or 32 <= v < 128):
            raise ValueError(f"interrupt line {line!r} (vector {v}) cannot be driven by a peripheral")
        self._line_cache[line] = v
        return v

    # ------------------------------------------------------------------ peripheral lines
    def set_irq(self, line: Any, level: bool = True) -> None:
        v = self.line_vector(line)
        if level:
            if v not in self.asserted:
                self.asserted.add(v)
                self._edge(v)
        else:
            self.asserted.discard(v)

    def pulse_irq(self, line: Any) -> None:
        self._edge(self.line_vector(line))

    def _edge(self, v: int) -> None:
        if v >= 32:
            g, c = divmod(v - 32, 8)
            self.pie.set_flag(g + 1, c + 1)
            self.pie_group_changed(g + 1)
        else:                               # INT13 / INT14 / NMI straight to the CPU
            self._cpu_flag(v - 1 if v in (13, 14) else None, nmi=v == VEC_NMI)

    def pie_group_changed(self, g: int) -> None:
        """Re-evaluate group g after a flag/enable/ack change: forward INTg to the CPU when an enabled flag is
        pending and PIEACKg is clear (the PIE then sets PIEACKg)."""
        pie = self.pie
        if pie.ifr[g] & pie.ier[g] and not pie.ack >> (g - 1) & 1:
            pie.set_ack(g)
            self._cpu_flag(g - 1)

    def _cpu_flag(self, bit: int | None, nmi: bool = False) -> None:
        core = self.core
        if nmi:
            self._nmi_pending = True
        else:
            core.write_reg("ifr", core.read_reg("ifr") | (1 << bit))
        if getattr(core, "running", False):
            if nmi:
                core.request_stop(from_mmio=True)
                return
            ier, st1 = core.read_reg("ier"), core.read_reg("st1")
            if ier >> bit & 1 and not st1 & ST1_INTM:
                core.request_stop(from_mmio=True)

    _nmi_pending = False

    # ------------------------------------------------------------------ Chip slice-loop interface
    @property
    def pending(self) -> bool:
        return self._nmi_pending or bool(self.core.read_reg("ifr") & 0xFFFF)

    def best_pending(self) -> int | None:
        """Highest-priority enabled CPU interrupt (1..16) flagged in IFR (ignoring INTM), else None."""
        core = self.core
        m = core.read_reg("ifr") & core.read_reg("ier") & 0xFFFF
        if self._nmi_pending:
            return VEC_NMI
        if not m:
            return None
        return (m & -m).bit_length()

    def wakeup_pending(self) -> bool:
        """IDLE ends on any enabled interrupt (IFR & IER), whether or not INTM masks it."""
        return self.best_pending() is not None

    def service(self) -> bool:
        core = self.core
        if self._nmi_pending:
            self._nmi_pending = False
            return self._take(VEC_NMI, VEC_NMI)
        ifr, ier, st1 = core.read_regs(("ifr", "ier", "st1"))
        m = ifr & ier & 0xFFFF
        if not m or st1 & ST1_INTM:
            return False
        bit = (m & -m).bit_length() - 1
        n = bit + 1                                         # INT1..INT14, 15 DLOGINT, 16 RTOSINT
        core.write_reg("ifr", ifr & ~(1 << bit))
        vec = n
        if 1 <= n <= 12 and self.pie.enpie and st1 & ST1_VMAP:
            pie = self.pie
            pend = pie.ifr[n] & pie.ier[n]
            if not pend:
                # the flag was cleared after INTn reached the CPU: the hardware fetches an unpredictable vector;
                # we drop the request (the group stays acknowledged until the ISR... there is none: unblock it)
                self._warn_once(f"spurious{n}", f"INT{n} reached the CPU but no enabled PIEIFR{n} flag is pending "
                                "(flag cleared in between): interrupt dropped")
                return False
            ch = (pend & -pend).bit_length()
            pie.clear_flag(n, ch)
            vec = pie_vector(n, ch)
        return self._take(vec, n)

    def vector_address(self, vec: int) -> int:
        """Address of vector `vec` in the active table (PIE if ENPIE and VMAP, else the CPU table)."""
        st1 = self.core.read_reg("st1")
        if st1 & ST1_VMAP:
            if self.pie.enpie:
                return self.pie.base + 0x20 + 2 * vec
            return BROM_VECTORS + 2 * vec
        return 2 * vec

    def vector_target(self, vec: int) -> int:
        a = self.vector_address(vec)
        if a >= self.pie.base and a < self.pie.base + self.pie.size:
            return self.pie.vector(vec)
        try:
            return self.chip.bus.read(a, 2) & 0x3FFFFF
        except Exception:
            return 0x3FFFFF

    def _take(self, vec: int, int_number: int) -> bool:
        a = self.vector_address(vec)
        target = self.vector_target(vec)
        if a >= self.pie.base and a < self.pie.base + self.pie.size:
            self.pie.set_pievect(a)
        if target in (0, 0x3FFFFF) or not self._mapped(target):
            name = self.exc_name(vec)
            self._log("fault", type="bad_vector", vector=vec, addr=a, target=target, pc=self.core.read_reg("pc"),
                      msg=f"{name}: vector at 0x{a:06X} holds 0x{target:06X} (not code): interrupt not taken")
            return False
        self.core.take_interrupt(target, int_number)
        self.entries += 1
        self.last_vector = vec
        if self.trace is not None:
            self.trace("enter", vec)
        return True

    def _mapped(self, addr: int) -> bool:
        from ...bus import MemoryRegion
        try:
            return isinstance(self.chip.bus.lookup(addr), MemoryRegion)
        except Exception:
            return True

    def itrap(self, pc: int | None = None) -> bool:
        """Illegal instruction trap (ITRAP0): take vector 19 (ILLEGAL); not maskable."""
        self._log("fault", type="illegal", pc=pc if pc is not None else self.core.read_reg("pc"),
                  msg=f"illegal instruction at 0x{(pc if pc is not None else self.core.read_reg('pc')):06X} → ITRAP",
                  handled=True)
        return self._take(VEC_ILLEGAL, VEC_ILLEGAL)

    def on_stop(self, reason: StopReason, info: Any) -> bool:
        if reason is StopReason.FAULT:
            info = info or {}
            if str(info.get("type", "")).lower() in ("illegal", "illegal_instruction", "itrap", "undefined"):
                if self.vector_target(VEC_ILLEGAL) not in (0, 0x3FFFFF):
                    return self._take(VEC_ILLEGAL, VEC_ILLEGAL)
            return False
        return True

    # ------------------------------------------------------------------ reset
    def reset(self, entry: int | None = None) -> None:
        self.pie.reset()
        self.asserted.clear()
        self._nmi_pending = False
        self.lockup = False
        self.reset_requested = None
        self.last_vector = None
        pc = entry if entry is not None else self.boot_entry
        core = self.core
        self._seed_brom_reset_vector()
        core.reset(pc, self.reset_sp)
        for name, val in (("st0", 0), ("st1", ST1_RESET), ("ier", 0), ("ifr", 0), ("dp", 0), ("sp", self.reset_sp),
                          ("pc", pc)):
            try:
                core.write_reg(name, val)
            except (KeyError, ValueError, AttributeError):
                pass

    def _seed_brom_reset_vector(self) -> None:
        """The BROM reset vector (0x3FFFC0) points at the boot ROM's InitBoot; we have no boot ROM code, so if the
        location is erased we store the boot-to-flash entry there (informational: reset jumps directly)."""
        bus = self.chip.bus
        try:
            if bus.read(BROM_VECTORS, 2) in (0xFFFFFFFF, 0):
                bus.write(BROM_VECTORS, 2, self.boot_entry)
        except Exception:
            pass
        # `boot.rom_words: {addr: [words]}` (e.g. the ADC_cal LRETR and PARTID in TI OTP), written where erased
        for addr, words in (self._boot.get("rom_words") or {}).items():
            a = _int(addr)
            for i, w in enumerate(words if isinstance(words, (list, tuple)) else [words]):
                try:
                    if bus.read(a + i, 1) == 0xFFFF:
                        bus.write(a + i, 1, _int(w))
                except Exception:
                    break

    # ------------------------------------------------------------------ introspection
    def vector_table(self) -> list[dict]:
        """Non-empty vectors of the active table: [{num, name, addr, target}]."""
        out = []
        for n in range(128):
            a = self.vector_address(n)
            t = self.vector_target(n)
            if t in (0, 0x3FFFFF):
                continue
            out.append({"num": n, "name": self.exc_name(n), "addr": a, "target": t})
        return out

    def cpu_state(self) -> dict:
        core = self.core
        vals = dict(zip(REG_NAMES, core.read_regs(REG_NAMES)))
        st1 = vals["st1"]
        pie = self.pie
        st = dict(vals)
        st.update({"intm": st1 & 1, "eallow": st1 >> 6 & 1, "vmap": st1 >> 3 & 1, "enpie": int(pie.enpie),
                   "pieack": pie.ack, "pieifr": {g: pie.ifr[g] for g in range(1, 13) if pie.ifr[g]},
                   "pieier": {g: pie.ier[g] for g in range(1, 13) if pie.ier[g]},
                   "active": None, "active_name": None, "mode": "thread",
                   "pending": [b + 1 for b in range(16) if vals["ifr"] >> b & 1]})
        return st
