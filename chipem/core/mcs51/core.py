"""Pure-Python MCS-51 (8051) interpreter behind the `Core` interface (docs/MCS51_CORE.md, docs/CONTRACTS.md
"MCS-51 address spaces").

Address spaces are tagged ranges of one flat 24-bit space (8-bit address units), so the generic Bus, memory view,
events and symbols keep working:

    0x00000-0x0FFFF  CODE   program memory (MOVC, instruction fetch)
    0x10000-0x1FFFF  XDATA  the external data bus as seen by MOVX (P0/P2/RD/WR); on-chip expanded RAM is separate
    0x20000-0x200FF  IDATA  internal RAM (0x00-0x7F direct+indirect, 0x80-0xFF indirect only); R0-R7 banks at 0x00
    0x30080-0x300FF  SFR    special function registers (direct addressing of 0x80-0xFF)
    0x40000-0x4FFFF  XRAM   on-chip expanded RAM ("ERAM"/"AUX RAM", reached by MOVX below `xram_size` while
                            the family's EXTRAM bit is clear)

Time: the core counts OSCILLATOR periods. `run(max_cycles)` executes instructions until at least `max_cycles`
oscillator periods have elapsed and returns the periods used; one machine cycle is `clocks_per_mc` (12, or 6 in
6-clock/X2 mode) periods, and every instruction costs its fixed machine-cycle count from isa.py.

Decoding is cached per PC: `cache[pc]` is a closure with its operands (and the SFR access functions they need)
bound; it executes the instruction and returns the next PC.  `costs[pc]` is its cost in oscillator periods.

Core-owned SFRs (kept in `self.sfr`, handled inline): ACC 0xE0, B 0xF0, PSW 0xD0 (P computed from ACC on every read),
SP 0x81, DPL 0x82, DPH 0x83 and, when configured (`configure(auxr1=0xA2)`), AUXR1 with the dual-DPTR select DPS
(bit 0) and a hard-wired 0 in bit 2.  Every other SFR goes to the MMIO callbacks the Bus maps (peripheral models);
read-modify-write instructions use a model's latch reader when it registered one (`set_latch_reader`): ports.
"""
from __future__ import annotations

import sys
from functools import partial
from typing import Any, Callable

from ..base import Core, StopReason
from .isa import OPCODES

CODE_BASE, XDATA_BASE, IDATA_BASE, SFR_BASE, XRAM_BASE = 0x00000, 0x10000, 0x20000, 0x30000, 0x40000
SPACE_END = 0x50000
ACC, B, PSW, SP, DPL, DPH = 0xE0, 0xF0, 0xD0, 0x81, 0x82, 0x83
PAR = bytes(bin(i).count("1") & 1 for i in range(256))       # PSW.P: 1 when ACC holds an odd number of ones
REG_NAMES = ("pc", "a", "b", "psw", "sp", "dptr", "r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7", "dps")


class _Stop(Exception):
    def __init__(self, reason: str, npc: int):
        super().__init__(reason)
        self.reason, self.npc = reason, npc


class Fault(Exception):
    def __init__(self, kind: str, **info: Any):
        super().__init__(kind)
        self.kind, self.info = kind, info


class _Spin(Exception):
    """A jump to itself was taken (SJMP $, AJMP $, LJMP $, JB/JNB bit,$): only an interrupt or a scheduler event can
    change anything, so the run loop spends the rest of the slice in it at once (the Chip then runs to the next
    event)."""


def _self_jump(op: int, pc: int, npc: int, b1: int, b2: int) -> bool:
    """A jump to itself: SJMP/AJMP/LJMP $, and JB/JNB bit,$ (a wait loop on a flag: every peripheral model sets its
    flags from scheduler events, and RAM bits change only in interrupt handlers, so nothing can end the loop before
    the next event either)."""
    if op in (0x20, 0x30):
        return (npc + (b2 - 256 if b2 & 0x80 else b2)) & 0xFFFF == pc
    if op == 0x80:
        return (npc + (b1 - 256 if b1 & 0x80 else b1)) & 0xFFFF == pc
    if op == 0x02:
        return (b1 << 8 | b2) == pc
    if op & 0x1F == 0x01:
        return (npc & 0xF800) | ((op >> 5) << 8) | b1 == pc
    return False


def _spin_wrap(pc: int, f: Callable[[], int]) -> Callable[[], int]:
    def g():
        t = f()
        if t == pc:
            raise _Spin
        return t
    return g


def space_of(addr: int) -> str:
    """Name of the address space of a tagged address: CODE, XDATA, IDATA, SFR, XRAM or '?'."""
    return {0: "CODE", 1: "XDATA", 2: "IDATA", 3: "SFR", 4: "XRAM"}.get(addr >> 16, "?")


class MCS51Core(Core):
    page_size = 1                    # the Bus maps every block directly (SFRs are single bytes)
    exact_counts = True
    slice_scale = 12                 # Chip: max_slice is scaled by this (one machine cycle = 12 oscillator periods)
    cycle_counted = True             # run() budgets and returns oscillator periods, not instructions
    page_shift = 0

    def __init__(self, cpu: str = "80c51", *, fpu: bool = False, precise: bool = False):
        self.cpu = cpu
        self.precise = precise
        self.code = bytearray(b"\xff" * 0x10000)
        self.xdata = bytearray(0x10000)
        self.iram = bytearray(256)
        self.sfr = bytearray(256)
        self.xram = bytearray(0x10000)
        self.xram_size = 0             # on-chip expanded RAM (mapped at XRAM_BASE)
        self.extram = False            # family: AUXR.EXTRAM -> every MOVX goes to the external bus
        self._regions: list[tuple[int, int, str, Any, Any]] = []
        self._sfr_map: list[tuple[Callable, Callable, int] | None] = [None] * 256
        self._latch: list[Callable[[], int] | None] = [None] * 256
        self._xmap: list[tuple[int, int, str, Any, Any]] = []
        self._unmapped: Callable[[int, int, str], bool] | None = None
        self._native: dict[int, Callable[[], int]] = {}
        self._bps: set[int] = set()
        self._skip_once: int | None = None
        self._dbg_info: dict | None = None
        self._watch: list | None = None
        self._watch_snaps: list = []
        self._idata_rwatch: tuple[int, int] | None = None
        self.debug_hook = None
        self.bp_skip = None
        self.spin_pc: int | None = None
        self.spin_skip = True              # False: execute jumps-to-self one by one
        self.auxr1 = None              # SFR address of AUXR1 (dual DPTR) or None
        self.on_auxr1: Callable[[int, int], None] | None = None   # family hook (ENBOOT): fn(old, new)
        self.on_sfr_write: dict[int, Callable[[int], None]] = {}
        self.dptr_alt = [0, 0]         # the DPTR not selected by DPS (dpl, dph)
        self.clocks_per_mc = 12
        self._cache: list[Callable[[], int] | None] = [None] * 0x10000
        self._costs: list[int] = [0] * 0x10000
        self._lim = [0]
        self._stop_req = False
        self._sleep_req: str | None = None
        self._extra = 0
        self.inhibit = False           # one more instruction before an interrupt (after RETI / IE/IP writes)
        self.running = False
        self.last_fault: dict | None = None
        self.stop_info = None
        self.insn_count = 0            # instructions executed (cumulative)
        self.last_insns = 0            # instructions of the last run() call
        self.rom_writes = 0
        self._pc = 0
        self._sem = self._build_semantics()

    # ================================================================== configuration
    def configure(self, *, auxr1: int | None = None, xram_size: int | None = None) -> None:
        if auxr1 is not None:
            self.auxr1 = auxr1 & 0xFF
        if xram_size is not None:
            self.xram_size = int(xram_size)
        self.flush_code_cache()

    def set_clocks_per_mc(self, n: int) -> None:
        """12 (standard) or 6 (6-clock / X2 mode): oscillator periods per machine cycle."""
        n = int(n)
        if n != self.clocks_per_mc:
            self.clocks_per_mc = n
            self.flush_code_cache()            # costs are cached in oscillator periods

    def set_latch_reader(self, sfr_addr: int, fn: Callable[[], int] | None) -> None:
        """Read-modify-write instructions on this SFR read `fn()` (the port latch) instead of the pins."""
        self._latch[sfr_addr & 0xFF] = fn
        self.flush_code_cache()

    def install_native(self, addr: int, fn: Callable[[], int] | None) -> None:
        """Run the Python function `fn()` instead of fetching code at `addr` (boot ROM entry points such as the
        IAP entry at 0xFFF0).  `fn` returns the next PC (use `ret()` to return to the caller) and may add cycles
        with `add_cycles`."""
        if fn is None:
            self._native.pop(addr & 0xFFFF, None)
        else:
            self._native[addr & 0xFFFF] = fn
        self._cache[addr & 0xFFFF] = None

    def add_cycles(self, machine_cycles: int) -> None:
        self._extra += int(machine_cycles) * self.clocks_per_mc

    def add_clocks(self, clocks: int) -> None:
        """Charge oscillator periods to the next run() (e.g. the interrupt polling cycle after an Idle wake-up)."""
        self._extra += int(clocks)

    # ================================================================== memory map
    def _add_region(self, base: int, size: int, kind: str, rd=None, wr=None) -> None:
        end = base + size
        if base < 0 or end > SPACE_END:
            raise ValueError(f"region 0x{base:X}+0x{size:X} outside the MCS-51 spaces")
        for b, e, *_ in self._regions:
            if b < end and base < e:
                raise ValueError(f"mapping 0x{base:X}..0x{end:X} overlaps 0x{b:X}..0x{e:X}")
        self._regions.append((base, end, kind, rd, wr))
        self._regions.sort(key=lambda r: r[0])
        if XDATA_BASE <= base < IDATA_BASE:
            self._xmap = [r for r in self._regions if XDATA_BASE <= r[0] < IDATA_BASE]
        if base >> 16 == 4 and kind != "mmio":
            self.xram_size = max(self.xram_size, end - XRAM_BASE)
        self.flush_code_cache()

    def map_memory(self, *args, writable: bool = True, executable: bool = True, data: bytes | None = None,
                   **_kw) -> None:
        if args and isinstance(args[0], str):
            args = args[1:]
        base, size = int(args[0]), int(args[1])
        if len(args) > 2:
            data = args[2]
        if len(args) > 3:
            writable = bool(args[3])
        if base >> 16 == 3:
            raise ValueError("SFR space holds MMIO blocks only")
        self._add_region(base, size, "ram" if writable else "rom")
        if data:
            self.write_mem(base, bytes(data))

    def map_mmio(self, base: int, size: int, read: Callable[[int, int], int],
                 write: Callable[[int, int, int], None]) -> None:
        # Bus wrappers of sparse blocks carry their target (block.read/write and offset delta): call it directly
        delta = getattr(read, "delta", 0)
        rd = getattr(read, "target", read)
        wr = getattr(write, "target", write)
        if base >> 16 == 3:
            for a in range(base, base + size):
                s = a & 0xFF
                if s < 0x80 or a >= SFR_BASE + 0x100:
                    raise ValueError(f"SFR mapping 0x{a:X} outside 0x30080..0x300FF")
                self._sfr_map[s] = (rd, wr, a - base + delta)
            self._regions.append((base, base + size, "mmio", read, write))
            self._regions.sort(key=lambda r: r[0])
            self.flush_code_cache()
            return
        self._add_region(base, size, "mmio", read, write)

    def unmap(self, base: int, size: int) -> None:
        end = base + size
        self._regions = [r for r in self._regions if not (r[0] >= base and r[1] <= end)]
        if base >> 16 == 3:
            for a in range(base, end):
                self._sfr_map[a & 0xFF] = None
        self._xmap = [r for r in self._regions if XDATA_BASE <= r[0] < IDATA_BASE]
        self.flush_code_cache()

    def set_unmapped_handler(self, fn) -> None:
        self._unmapped = fn

    def _region(self, a: int):
        for r in self._regions:
            if r[0] <= a < r[1]:
                return r
        return None

    # ---- byte access through the tagged spaces (tools, Bus, MOVX external path) -----------------------------
    def _backing(self, a: int) -> tuple[bytearray, int] | None:
        sp = a >> 16
        if sp == 0:
            return self.code, a
        if sp == 1:
            return self.xdata, a - XDATA_BASE
        if sp == 2 and a < IDATA_BASE + 0x100:
            return self.iram, a - IDATA_BASE
        if sp == 4:
            return self.xram, a - XRAM_BASE
        return None

    def peek(self, a: int) -> int:
        """One byte of the tagged space (MMIO read callbacks run; core-owned SFRs from the core)."""
        if a >> 16 == 3:
            s = a & 0xFF
            if s < 0x80:
                raise ValueError(f"no SFR at 0x{a:X}")
            return self._sfr_read_any(s)
        r = self._region(a)
        if r is None:
            if a >> 16 == 0:
                return 0xFF                  # unmapped code reads as erased / floating bus
            raise ValueError(f"read_mem: unmapped address 0x{a:05X}")
        if r[2] == "mmio":
            return r[3](a - r[0], 1) & 0xFF
        buf, off = self._backing(a)
        return buf[off]

    def poke(self, a: int, v: int) -> None:
        v &= 0xFF
        if a >> 16 == 3:
            self._sfr_write_any(a & 0xFF, v)
            return
        r = self._region(a)
        if r is None:
            raise ValueError(f"write_mem: unmapped address 0x{a:05X}")
        if r[2] == "mmio":
            r[4](a - r[0], 1, v)
            return
        buf, off = self._backing(a)
        buf[off] = v
        if a < 0x10000:
            self._invalidate(a, a + 1)

    def read_mem(self, addr: int, n: int) -> bytes:
        if addr >> 16 in (0, 1, 2, 4) and n:
            r = self._region(addr)
            if r is not None and r[2] != "mmio" and addr + n <= r[1]:
                buf, off = self._backing(addr)
                return bytes(buf[off:off + n])
        return bytes(self.peek(addr + i) for i in range(n))

    def write_mem(self, addr: int, data: bytes) -> None:
        data = bytes(data)
        if addr >> 16 in (0, 1, 2, 4) and data:
            r = self._region(addr)
            if r is not None and r[2] != "mmio" and addr + len(data) <= r[1]:
                buf, off = self._backing(addr)
                buf[off:off + len(data)] = data
                if addr < 0x10000:
                    self._invalidate(addr, addr + len(data))
                return
        for i, b in enumerate(data):
            self.poke(addr + i, b)

    def code_swap(self, base: int, data: bytes) -> bytes:
        """Replace code bytes [base, base+len) (boot ROM overlay); returns the bytes replaced."""
        n = len(data)
        old = bytes(self.code[base:base + n])
        self.code[base:base + n] = data
        self._invalidate(base, base + n)
        return old

    def _invalidate(self, lo: int, hi: int) -> None:
        cache = self._cache
        for p in range(max(lo - 2, 0), min(hi, 0x10000)):
            cache[p] = None

    def flush_code_cache(self) -> None:
        self._cache[:] = [None] * 0x10000

    # ---- SFR access -------------------------------------------------------------------------------------------
    def _plain_sfrs(self) -> tuple[int, ...]:
        return (ACC, B, SP, DPL, DPH) + ((self.auxr1,) if self.auxr1 is not None else ())

    def _sfr_entry(self, s: int):
        e = self._sfr_map[s]
        if e is None and self._unmapped is not None:
            try:
                if self._unmapped(SFR_BASE + s, 1, "r"):
                    e = self._sfr_map[s]
            except Exception:
                e = None
        return e

    def _sfr_read_any(self, s: int) -> int:
        if s == PSW:
            return self.sfr[PSW] | PAR[self.sfr[ACC]]
        if s in self._plain_sfrs():
            return self.sfr[s]
        e = self._sfr_entry(s)
        if e is None:
            return self.sfr[s]
        return e[0](e[2], 1) & 0xFF

    def _sfr_write_any(self, s: int, v: int) -> None:
        self._dir_writer(s)(v & 0xFF)

    def _sfr_reader(self, s: int) -> Callable[[], int]:
        e = self._sfr_entry(s)
        if e is None:
            sfr = self.sfr
            r = lambda: sfr[s]                                         # noqa: E731
        else:
            r = partial(e[0], e[2], 1)
        if self._watch is not None and self._watched(SFR_BASE + s):
            inner = r

            def r(_i=inner):
                v = _i()
                self._watch_hit(SFR_BASE + s, 1, "r", v)
                return v
        return r

    def _sfr_writer(self, s: int) -> Callable[[int], None]:
        e = self._sfr_entry(s)
        hook = self.on_sfr_write.get(s)
        if e is None:
            sfr = self.sfr

            def w(v):
                sfr[s] = v
        else:
            w = partial(e[1], e[2], 1)
        if hook is not None:
            inner = w

            def w(v, _i=inner, _h=hook):
                _i(v)
                _h(v)
        if self._watch is not None and self._watched(SFR_BASE + s):
            inner2 = w

            def w(v, _i=inner2):
                _i(v)
                self._watch_hit(SFR_BASE + s, 1, "w", v)
        return w

    def _dir_reader(self, a: int) -> Callable[[], int]:
        iram, sfr = self.iram, self.sfr
        if a < 0x80:
            return lambda: iram[a]
        if a == PSW:
            return lambda: sfr[PSW] | PAR[sfr[ACC]]
        if a in self._plain_sfrs():
            return lambda: sfr[a]
        return self._sfr_reader(a)

    def _latch_reader(self, a: int) -> Callable[[], int]:
        if a >= 0x80 and self._latch[a] is not None:
            return self._latch[a]
        return self._dir_reader(a)

    def _dir_writer(self, a: int) -> Callable[[int], None]:
        iram, sfr = self.iram, self.sfr
        if a < 0x80:
            def w(v):
                iram[a] = v
            return w
        if a == PSW:
            def w(v):
                sfr[PSW] = v & 0xFE
            return w
        if self.auxr1 is not None and a == self.auxr1:
            return self._wr_auxr1
        if a in (ACC, B, SP, DPL, DPH):
            hook = self.on_sfr_write.get(a)

            def w(v):
                sfr[a] = v
            if hook is not None:
                def w(v, _h=hook):
                    sfr[a] = v
                    _h(v)
            return w
        return self._sfr_writer(a)

    def _wr_auxr1(self, v: int) -> None:
        sfr = self.sfr
        a = self.auxr1
        old = sfr[a]
        new = v & 0xFB                                  # bit 2 is a hard-wired 0 (INC AUXR1 toggles DPS)
        if (old ^ new) & 1:
            alt = self.dptr_alt
            alt[0], sfr[DPL] = sfr[DPL], alt[0]
            alt[1], sfr[DPH] = sfr[DPH], alt[1]
        sfr[a] = new
        if self.on_auxr1 is not None and old != new:
            self.on_auxr1(old, new)

    # ---- MOVX --------------------------------------------------------------------------------------------------
    def xread(self, a: int) -> int:
        if a < self.xram_size and not self.extram:
            v = self.xram[a]
            if self._watch is not None:
                self._watch_hit(XRAM_BASE + a, 1, "r", v)
            return v
        v = self.ext_read(a)
        if self._watch is not None:
            self._watch_hit(XDATA_BASE + a, 1, "r", v)
        return v

    def xwrite(self, a: int, v: int) -> None:
        if a < self.xram_size and not self.extram:
            self.xram[a] = v
            return
        self.ext_write(a, v)
        if self._watch is not None and not any(r[0] <= XDATA_BASE + a < r[1] and r[2] == "ram" for r in self._xmap):
            self._watch_hit(XDATA_BASE + a, 1, "w", v)      # (RAM writes are found by the snapshot compare)

    def ext_read(self, a: int) -> int:
        """External data bus read (XDATA space): a mapped memory or MMIO block (the family's bus model), else the
        floating bus (0xFF)."""
        t = XDATA_BASE + a
        for r in self._xmap:
            if r[0] <= t < r[1]:
                if r[2] == "mmio":
                    return r[3](t - r[0], 1) & 0xFF
                return self.xdata[a]
        return 0xFF

    def ext_write(self, a: int, v: int) -> None:
        t = XDATA_BASE + a
        for r in self._xmap:
            if r[0] <= t < r[1]:
                if r[2] == "mmio":
                    r[4](t - r[0], 1, v)
                elif r[2] == "ram":
                    self.xdata[a] = v
                return

    def p2_latch(self) -> int:
        """Port 2 latch (the high address byte of MOVX @Ri)."""
        f = self._latch[0xA0]
        if f is not None:
            return f() & 0xFF
        return self._sfr_read_any(0xA0)

    # ================================================================== semantics
    def _build_semantics(self) -> list:
        core = self
        iram, sfr, code = self.iram, self.sfr, self.code
        lim = self._lim
        rdf, wrf, latf = self._dir_reader, self._dir_writer, self._latch_reader

        def add(v, c):
            a = sfr[ACC]
            r = a + v + c
            c6 = ((a & 0x7F) + (v & 0x7F) + c) >> 7
            c7 = r >> 8
            ac = ((a & 0xF) + (v & 0xF) + c) & 0x10
            sfr[ACC] = r & 0xFF
            sfr[PSW] = (sfr[PSW] & 0x3A) | (c7 << 7) | (ac << 2) | ((c6 ^ c7) << 2)

        def subb(v):
            a = sfr[ACC]
            c = sfr[PSW] >> 7
            r = a - v - c
            b7 = 1 if r < 0 else 0
            b6 = 1 if (a & 0x7F) - (v & 0x7F) - c < 0 else 0
            ac = 0x40 if (a & 0xF) - (v & 0xF) - c < 0 else 0
            sfr[ACC] = r & 0xFF
            sfr[PSW] = (sfr[PSW] & 0x3A) | (b7 << 7) | ac | ((b6 ^ b7) << 2)

        def rel(npc, r):
            return (npc + (r - 256 if r & 0x80 else r)) & 0xFFFF

        # ---- bit access --------------------------------------------------------------------------------------
        def bit_reader(bit):
            sh = bit & 7
            if bit < 0x80:
                byte = 0x20 + (bit >> 3)
                return lambda: (iram[byte] >> sh) & 1
            a = bit & 0xF8
            if a == PSW:
                if sh == 0:
                    return lambda: PAR[sfr[ACC]]
                return lambda: (sfr[PSW] >> sh) & 1
            if a in (ACC, B):
                return lambda: (sfr[a] >> sh) & 1
            r = rdf(a)
            return lambda: (r() >> sh) & 1

        def bit_writer(bit):
            m = 1 << (bit & 7)
            nm = 0xFF ^ m
            if bit < 0x80:
                byte = 0x20 + (bit >> 3)

                def s(v):
                    if v:
                        iram[byte] |= m
                    else:
                        iram[byte] &= nm
                return s
            a = bit & 0xF8
            if a in (ACC, B):
                def s(v):
                    if v:
                        sfr[a] |= m
                    else:
                        sfr[a] &= nm
                return s
            r, w = latf(a), wrf(a)

            def s(v):
                x = r()
                w((x | m) if v else (x & nm))
            return s

        def bit_latch_reader(bit):
            if bit < 0x80:
                return bit_reader(bit)
            sh = bit & 7
            r = latf(bit & 0xF8)
            return lambda: (r() >> sh) & 1

        # ---- operand readers ---------------------------------------------------------------------------------
        def reg_reader(kind, b1):
            """0-arg reader for a source operand kind (dir, @Ri, Rn)."""
            if kind == "dir":
                return rdf(b1)
            if kind[0] == "@":
                i = int(kind[2])
                return lambda: iram[iram[(sfr[PSW] & 0x18) | i]]
            n = int(kind[1])
            return lambda: iram[(sfr[PSW] & 0x18) | n]

        def reg_writer(kind, b1):
            if kind == "dir":
                return wrf(b1)
            if kind[0] == "@":
                i = int(kind[2])

                def w(v):
                    iram[iram[(sfr[PSW] & 0x18) | i]] = v
                return w
            n = int(kind[1])

            def w(v):
                iram[(sfr[PSW] & 0x18) | n] = v
            return w

        def reg_latch(kind, b1):
            return latf(b1) if kind == "dir" else reg_reader(kind, b1)

        S: list = [None] * 256

        def mk(op):
            def deco(fn):
                S[op] = fn
                return fn
            return deco

        # ---- control flow ------------------------------------------------------------------------------------
        @mk(0x00)
        def _nop(pc, npc, b1, b2):
            return lambda: npc

        for page in range(8):
            def _ajmp(pc, npc, b1, b2, _p=page):
                t = (npc & 0xF800) | (_p << 8) | b1
                return lambda: t

            def _acall(pc, npc, b1, b2, _p=page):
                t = (npc & 0xF800) | (_p << 8) | b1
                lo, hi = npc & 0xFF, npc >> 8

                def f():
                    s = (sfr[SP] + 1) & 0xFF
                    iram[s] = lo
                    s = (s + 1) & 0xFF
                    iram[s] = hi
                    sfr[SP] = s
                    return t
                return f
            S[page << 5 | 0x01] = _ajmp
            S[page << 5 | 0x11] = _acall

        @mk(0x02)
        def _ljmp(pc, npc, b1, b2):
            t = (b1 << 8) | b2
            return lambda: t

        @mk(0x12)
        def _lcall(pc, npc, b1, b2):
            t = (b1 << 8) | b2
            lo, hi = npc & 0xFF, npc >> 8

            def f():
                s = (sfr[SP] + 1) & 0xFF
                iram[s] = lo
                s = (s + 1) & 0xFF
                iram[s] = hi
                sfr[SP] = s
                return t
            return f

        @mk(0x22)
        def _ret(pc, npc, b1, b2):
            def f():
                s = sfr[SP]
                hi = iram[s]
                s = (s - 1) & 0xFF
                lo = iram[s]
                sfr[SP] = (s - 1) & 0xFF
                return (hi << 8) | lo
            return f

        @mk(0x32)
        def _reti(pc, npc, b1, b2):
            def f():
                s = sfr[SP]
                hi = iram[s]
                s = (s - 1) & 0xFF
                lo = iram[s]
                sfr[SP] = (s - 1) & 0xFF
                core.inhibit = True
                raise _Stop("reti", (hi << 8) | lo)
            return f

        @mk(0x73)
        def _jmp_adptr(pc, npc, b1, b2):
            return lambda: (sfr[ACC] + ((sfr[DPH] << 8) | sfr[DPL])) & 0xFFFF

        @mk(0x80)
        def _sjmp(pc, npc, b1, b2):
            t = rel(npc, b1)
            if t == pc:
                return lambda: t                           # SJMP $ (idle loop)
            return lambda: t

        def cond_jump(test):
            def fac(pc, npc, b1, b2):
                t = rel(npc, b1)
                return test(t, npc)
            return fac

        S[0x40] = cond_jump(lambda t, n: (lambda: t if sfr[PSW] & 0x80 else n))
        S[0x50] = cond_jump(lambda t, n: (lambda: n if sfr[PSW] & 0x80 else t))
        S[0x60] = cond_jump(lambda t, n: (lambda: n if sfr[ACC] else t))
        S[0x70] = cond_jump(lambda t, n: (lambda: t if sfr[ACC] else n))

        @mk(0x20)
        def _jb(pc, npc, b1, b2):
            t = rel(npc, b2)
            r = bit_reader(b1)
            return lambda: t if r() else npc

        @mk(0x30)
        def _jnb(pc, npc, b1, b2):
            t = rel(npc, b2)
            if b1 < 0x80:
                byte, m = 0x20 + (b1 >> 3), 1 << (b1 & 7)
                return lambda: npc if iram[byte] & m else t
            r = bit_reader(b1)
            return lambda: npc if r() else t

        @mk(0x10)
        def _jbc(pc, npc, b1, b2):
            t = rel(npc, b2)
            r, w = bit_latch_reader(b1), bit_writer(b1)

            def f():
                if r():
                    w(0)
                    return t
                return npc
            return f

        def cjne_fac(kind):
            def fac(pc, npc, b1, b2):
                t = rel(npc, b2)
                if kind == "A#":
                    imm = b1

                    def f():
                        a = sfr[ACC]
                        sfr[PSW] = (sfr[PSW] & 0x7F) | (0x80 if a < imm else 0)
                        return t if a != imm else npc
                    return f
                if kind == "Adir":
                    r = rdf(b1)

                    def f():
                        a, v = sfr[ACC], r()
                        sfr[PSW] = (sfr[PSW] & 0x7F) | (0x80 if a < v else 0)
                        return t if a != v else npc
                    return f
                rr = reg_reader(kind, 0)
                imm = b1

                def f():
                    v = rr()
                    sfr[PSW] = (sfr[PSW] & 0x7F) | (0x80 if v < imm else 0)
                    return t if v != imm else npc
                return f
            return fac

        S[0xB4] = cjne_fac("A#")
        S[0xB5] = cjne_fac("Adir")
        S[0xB6], S[0xB7] = cjne_fac("@R0"), cjne_fac("@R1")
        for n in range(8):
            S[0xB8 + n] = cjne_fac(f"R{n}")

        @mk(0xD5)
        def _djnz_dir(pc, npc, b1, b2):
            t = rel(npc, b2)
            if b1 < 0x80:
                a = b1

                def f():
                    v = (iram[a] - 1) & 0xFF
                    iram[a] = v
                    return t if v else npc
                return f
            r, w = latf(b1), wrf(b1)

            def f():
                v = (r() - 1) & 0xFF
                w(v)
                return t if v else npc
            return f

        for n in range(8):
            def _djnz_r(pc, npc, b1, b2, _n=n):
                t = rel(npc, b1)

                def f():
                    a = (sfr[PSW] & 0x18) | _n
                    v = (iram[a] - 1) & 0xFF
                    iram[a] = v
                    return t if v else npc
                return f
            S[0xD8 + n] = _djnz_r

        # ---- data transfer -----------------------------------------------------------------------------------
        @mk(0x74)
        def _mov_a_imm(pc, npc, b1, b2):
            def f():
                sfr[ACC] = b1
                return npc
            return f

        @mk(0x75)
        def _mov_dir_imm(pc, npc, b1, b2):
            if b1 < 0x80:
                def f():
                    iram[b1] = b2
                    return npc
                return f
            w = wrf(b1)

            def f():
                w(b2)
                return npc
            return f

        for i in range(2):
            def _mov_ri_imm(pc, npc, b1, b2, _i=i):
                def f():
                    iram[iram[(sfr[PSW] & 0x18) | _i]] = b1
                    return npc
                return f
            S[0x76 + i] = _mov_ri_imm
        for n in range(8):
            def _mov_rn_imm(pc, npc, b1, b2, _n=n):
                def f():
                    iram[(sfr[PSW] & 0x18) | _n] = b1
                    return npc
                return f
            S[0x78 + n] = _mov_rn_imm

        @mk(0xE5)
        def _mov_a_dir(pc, npc, b1, b2):
            if b1 < 0x80:
                def f():
                    sfr[ACC] = iram[b1]
                    return npc
                return f
            r = rdf(b1)

            def f():
                sfr[ACC] = r()
                return npc
            return f

        for i in range(2):
            def _mov_a_ri(pc, npc, b1, b2, _i=i):
                def f():
                    sfr[ACC] = iram[iram[(sfr[PSW] & 0x18) | _i]]
                    return npc
                return f
            S[0xE6 + i] = _mov_a_ri
        for n in range(8):
            def _mov_a_rn(pc, npc, b1, b2, _n=n):
                def f():
                    sfr[ACC] = iram[(sfr[PSW] & 0x18) | _n]
                    return npc
                return f
            S[0xE8 + n] = _mov_a_rn

        @mk(0xF5)
        def _mov_dir_a(pc, npc, b1, b2):
            if b1 < 0x80:
                def f():
                    iram[b1] = sfr[ACC]
                    return npc
                return f
            w = wrf(b1)

            def f():
                w(sfr[ACC])
                return npc
            return f

        for i in range(2):
            def _mov_ri_a(pc, npc, b1, b2, _i=i):
                def f():
                    iram[iram[(sfr[PSW] & 0x18) | _i]] = sfr[ACC]
                    return npc
                return f
            S[0xF6 + i] = _mov_ri_a
        for n in range(8):
            def _mov_rn_a(pc, npc, b1, b2, _n=n):
                def f():
                    iram[(sfr[PSW] & 0x18) | _n] = sfr[ACC]
                    return npc
                return f
            S[0xF8 + n] = _mov_rn_a

        @mk(0x85)
        def _mov_dir_dir(pc, npc, b1, b2):
            r, w = rdf(b1), wrf(b2)                     # bytes: 85 src dst

            def f():
                w(r())
                return npc
            return f

        for k, kind in enumerate(["@R0", "@R1"] + [f"R{n}" for n in range(8)]):
            def _mov_dir_reg(pc, npc, b1, b2, _k=kind):
                r, w = reg_reader(_k, 0), wrf(b1)

                def f():
                    w(r())
                    return npc
                return f

            def _mov_reg_dir(pc, npc, b1, b2, _k=kind):
                r, w = rdf(b1), reg_writer(_k, 0)

                def f():
                    w(r())
                    return npc
                return f
            S[0x86 + k] = _mov_dir_reg
            S[0xA6 + k] = _mov_reg_dir

        @mk(0x90)
        def _mov_dptr(pc, npc, b1, b2):
            def f():
                sfr[DPH] = b1
                sfr[DPL] = b2
                return npc
            return f

        @mk(0xA3)
        def _inc_dptr(pc, npc, b1, b2):
            def f():
                v = sfr[DPL] + 1
                if v > 0xFF:
                    sfr[DPL] = 0
                    sfr[DPH] = (sfr[DPH] + 1) & 0xFF
                else:
                    sfr[DPL] = v
                return npc
            return f

        @mk(0x93)
        def _movc_dptr(pc, npc, b1, b2):
            def f():
                sfr[ACC] = code[(sfr[ACC] + ((sfr[DPH] << 8) | sfr[DPL])) & 0xFFFF]
                return npc
            return f

        @mk(0x83)
        def _movc_pc(pc, npc, b1, b2):
            def f():
                sfr[ACC] = code[(sfr[ACC] + npc) & 0xFFFF]
                return npc
            return f

        @mk(0xE0)
        def _movx_a_dptr(pc, npc, b1, b2):
            xr = core.xread

            def f():
                sfr[ACC] = xr((sfr[DPH] << 8) | sfr[DPL]) & 0xFF
                return npc
            return f

        @mk(0xF0)
        def _movx_dptr_a(pc, npc, b1, b2):
            xw = core.xwrite

            def f():
                xw((sfr[DPH] << 8) | sfr[DPL], sfr[ACC])
                return npc
            return f

        for i in range(2):
            def _movx_a_ri(pc, npc, b1, b2, _i=i):
                xr, p2 = core.xread, core.p2_latch

                def f():
                    sfr[ACC] = xr((p2() << 8) | iram[(sfr[PSW] & 0x18) | _i]) & 0xFF
                    return npc
                return f

            def _movx_ri_a(pc, npc, b1, b2, _i=i):
                xw, p2 = core.xwrite, core.p2_latch

                def f():
                    xw((p2() << 8) | iram[(sfr[PSW] & 0x18) | _i], sfr[ACC])
                    return npc
                return f
            S[0xE2 + i] = _movx_a_ri
            S[0xF2 + i] = _movx_ri_a

        @mk(0xC0)
        def _push(pc, npc, b1, b2):
            r = rdf(b1)

            def f():
                s = (sfr[SP] + 1) & 0xFF
                sfr[SP] = s
                iram[s] = r()
                return npc
            return f

        @mk(0xD0)
        def _pop(pc, npc, b1, b2):
            w = wrf(b1)

            def f():
                s = sfr[SP]
                v = iram[s]
                sfr[SP] = (s - 1) & 0xFF
                w(v)
                return npc
            return f

        for k, kind in enumerate(["dir", "@R0", "@R1"] + [f"R{n}" for n in range(8)]):
            def _xch(pc, npc, b1, b2, _k=kind):
                r, w = reg_reader(_k, b1), reg_writer(_k, b1)

                def f():
                    v = r()
                    w(sfr[ACC])
                    sfr[ACC] = v
                    return npc
                return f
            S[0xC5 + k] = _xch
        for i in range(2):
            def _xchd(pc, npc, b1, b2, _i=i):
                def f():
                    p = iram[(sfr[PSW] & 0x18) | _i]
                    v, a = iram[p], sfr[ACC]
                    iram[p] = (v & 0xF0) | (a & 0x0F)
                    sfr[ACC] = (a & 0xF0) | (v & 0x0F)
                    return npc
                return f
            S[0xD6 + i] = _xchd

        # ---- arithmetic / logic on A -------------------------------------------------------------------------
        def alu(fn_imm, fn_val):
            """Factory set for ADD/ADDC/SUBB/ORL/ANL/XRL A,src (columns 4..F)."""
            def imm_fac(pc, npc, b1, b2):
                return fn_imm(b1, npc)

            facs = [imm_fac]
            for kind in ["dir", "@R0", "@R1"] + [f"R{n}" for n in range(8)]:
                def fac(pc, npc, b1, b2, _k=kind):
                    return fn_val(reg_reader(_k, b1), npc)
                facs.append(fac)
            return facs

        def add_imm(v, npc):
            def f():
                add(v, 0)
                return npc
            return f

        def add_val(r, npc):
            def f():
                add(r(), 0)
                return npc
            return f

        def addc_imm(v, npc):
            def f():
                add(v, sfr[PSW] >> 7)
                return npc
            return f

        def addc_val(r, npc):
            def f():
                add(r(), sfr[PSW] >> 7)
                return npc
            return f

        def subb_imm(v, npc):
            def f():
                subb(v)
                return npc
            return f

        def subb_val(r, npc):
            def f():
                subb(r())
                return npc
            return f

        def orl_imm(v, npc):
            def f():
                sfr[ACC] |= v
                return npc
            return f

        def orl_val(r, npc):
            def f():
                sfr[ACC] |= r()
                return npc
            return f

        def anl_imm(v, npc):
            def f():
                sfr[ACC] &= v
                return npc
            return f

        def anl_val(r, npc):
            def f():
                sfr[ACC] &= r()
                return npc
            return f

        def xrl_imm(v, npc):
            def f():
                sfr[ACC] ^= v
                return npc
            return f

        def xrl_val(r, npc):
            def f():
                sfr[ACC] ^= r()
                return npc
            return f

        for row, (fi, fv) in ((0x2, (add_imm, add_val)), (0x3, (addc_imm, addc_val)), (0x9, (subb_imm, subb_val)),
                              (0x4, (orl_imm, orl_val)), (0x5, (anl_imm, anl_val)), (0x6, (xrl_imm, xrl_val))):
            for col, fac in enumerate(alu(fi, fv), 4):
                S[row << 4 | col] = fac

        # logical ops with a direct destination (read-modify-write: port latch)
        def dir_logic(op, use_imm):
            def fac(pc, npc, b1, b2):
                if b1 < 0x80:
                    a = b1
                    if use_imm:
                        if op == "|":
                            def f():
                                iram[a] |= b2
                                return npc
                        elif op == "&":
                            def f():
                                iram[a] &= b2
                                return npc
                        else:
                            def f():
                                iram[a] ^= b2
                                return npc
                        return f
                    if op == "|":
                        def f():
                            iram[a] |= sfr[ACC]
                            return npc
                    elif op == "&":
                        def f():
                            iram[a] &= sfr[ACC]
                            return npc
                    else:
                        def f():
                            iram[a] ^= sfr[ACC]
                            return npc
                    return f
                r, w = latf(b1), wrf(b1)
                if use_imm:
                    v = b2
                    if op == "|":
                        def f():
                            w(r() | v)
                            return npc
                    elif op == "&":
                        def f():
                            w(r() & v)
                            return npc
                    else:
                        def f():
                            w(r() ^ v)
                            return npc
                    return f
                if op == "|":
                    def f():
                        w(r() | sfr[ACC])
                        return npc
                elif op == "&":
                    def f():
                        w(r() & sfr[ACC])
                        return npc
                else:
                    def f():
                        w(r() ^ sfr[ACC])
                        return npc
                return f
            return fac

        S[0x42], S[0x43] = dir_logic("|", False), dir_logic("|", True)
        S[0x52], S[0x53] = dir_logic("&", False), dir_logic("&", True)
        S[0x62], S[0x63] = dir_logic("^", False), dir_logic("^", True)

        # INC / DEC
        @mk(0x04)
        def _inc_a(pc, npc, b1, b2):
            def f():
                sfr[ACC] = (sfr[ACC] + 1) & 0xFF
                return npc
            return f

        @mk(0x14)
        def _dec_a(pc, npc, b1, b2):
            def f():
                sfr[ACC] = (sfr[ACC] - 1) & 0xFF
                return npc
            return f

        for k, kind in enumerate(["dir", "@R0", "@R1"] + [f"R{n}" for n in range(8)]):
            def _inc(pc, npc, b1, b2, _k=kind):
                if _k == "dir" and b1 < 0x80:
                    a = b1

                    def f():
                        iram[a] = (iram[a] + 1) & 0xFF
                        return npc
                    return f
                if _k[0] == "R":
                    n = int(_k[1])

                    def f():
                        a = (sfr[PSW] & 0x18) | n
                        iram[a] = (iram[a] + 1) & 0xFF
                        return npc
                    return f
                r, w = reg_latch(_k, b1), reg_writer(_k, b1)

                def f():
                    w((r() + 1) & 0xFF)
                    return npc
                return f

            def _dec(pc, npc, b1, b2, _k=kind):
                if _k == "dir" and b1 < 0x80:
                    a = b1

                    def f():
                        iram[a] = (iram[a] - 1) & 0xFF
                        return npc
                    return f
                if _k[0] == "R":
                    n = int(_k[1])

                    def f():
                        a = (sfr[PSW] & 0x18) | n
                        iram[a] = (iram[a] - 1) & 0xFF
                        return npc
                    return f
                r, w = reg_latch(_k, b1), reg_writer(_k, b1)

                def f():
                    w((r() - 1) & 0xFF)
                    return npc
                return f
            S[0x05 + k] = _inc
            S[0x15 + k] = _dec

        @mk(0xA4)
        def _mul(pc, npc, b1, b2):
            def f():
                p = sfr[ACC] * sfr[B]
                sfr[ACC] = p & 0xFF
                sfr[B] = p >> 8
                sfr[PSW] = (sfr[PSW] & 0x7B) | (0x04 if p > 0xFF else 0)
                return npc
            return f

        @mk(0x84)
        def _div(pc, npc, b1, b2):
            def f():
                d = sfr[B]
                if d == 0:
                    sfr[PSW] = (sfr[PSW] & 0x7B) | 0x04      # OV = 1, CY = 0; A and B undefined (kept)
                    return npc
                a = sfr[ACC]
                sfr[ACC], sfr[B] = divmod(a, d)
                sfr[PSW] &= 0x7B
                return npc
            return f

        @mk(0xD4)
        def _da(pc, npc, b1, b2):
            def f():
                a = sfr[ACC]
                p = sfr[PSW]
                c = p >> 7
                if (a & 0x0F) > 9 or p & 0x40:
                    a += 6
                    if a > 0xFF:
                        c = 1
                        a &= 0xFF
                if (a >> 4) > 9 or c:
                    a += 0x60
                    if a > 0xFF:
                        c = 1
                        a &= 0xFF
                sfr[ACC] = a
                sfr[PSW] = (p & 0x7F) | (c << 7)
                return npc
            return f

        @mk(0xE4)
        def _clr_a(pc, npc, b1, b2):
            def f():
                sfr[ACC] = 0
                return npc
            return f

        @mk(0xF4)
        def _cpl_a(pc, npc, b1, b2):
            def f():
                sfr[ACC] ^= 0xFF
                return npc
            return f

        @mk(0x03)
        def _rr(pc, npc, b1, b2):
            def f():
                a = sfr[ACC]
                sfr[ACC] = (a >> 1) | ((a & 1) << 7)
                return npc
            return f

        @mk(0x23)
        def _rl(pc, npc, b1, b2):
            def f():
                a = sfr[ACC]
                sfr[ACC] = ((a << 1) & 0xFF) | (a >> 7)
                return npc
            return f

        @mk(0x13)
        def _rrc(pc, npc, b1, b2):
            def f():
                a = sfr[ACC]
                p = sfr[PSW]
                sfr[ACC] = (a >> 1) | (p & 0x80)
                sfr[PSW] = (p & 0x7F) | ((a & 1) << 7)
                return npc
            return f

        @mk(0x33)
        def _rlc(pc, npc, b1, b2):
            def f():
                a = sfr[ACC]
                p = sfr[PSW]
                sfr[ACC] = ((a << 1) & 0xFF) | (p >> 7)
                sfr[PSW] = (p & 0x7F) | (a & 0x80)
                return npc
            return f

        @mk(0xC4)
        def _swap(pc, npc, b1, b2):
            def f():
                a = sfr[ACC]
                sfr[ACC] = ((a << 4) & 0xF0) | (a >> 4)
                return npc
            return f

        # ---- boolean (bit) instructions ----------------------------------------------------------------------
        @mk(0xC3)
        def _clr_c(pc, npc, b1, b2):
            def f():
                sfr[PSW] &= 0x7F
                return npc
            return f

        @mk(0xD3)
        def _setb_c(pc, npc, b1, b2):
            def f():
                sfr[PSW] |= 0x80
                return npc
            return f

        @mk(0xB3)
        def _cpl_c(pc, npc, b1, b2):
            def f():
                sfr[PSW] ^= 0x80
                return npc
            return f

        @mk(0xC2)
        def _clr_bit(pc, npc, b1, b2):
            if b1 < 0x80:
                byte, nm = 0x20 + (b1 >> 3), 0xFF ^ (1 << (b1 & 7))

                def f():
                    iram[byte] &= nm
                    return npc
                return f
            w = bit_writer(b1)

            def f():
                w(0)
                return npc
            return f

        @mk(0xD2)
        def _setb_bit(pc, npc, b1, b2):
            if b1 < 0x80:
                byte, m = 0x20 + (b1 >> 3), 1 << (b1 & 7)

                def f():
                    iram[byte] |= m
                    return npc
                return f
            w = bit_writer(b1)

            def f():
                w(1)
                return npc
            return f

        @mk(0xB2)
        def _cpl_bit(pc, npc, b1, b2):
            r, w = bit_latch_reader(b1), bit_writer(b1)

            def f():
                w(r() ^ 1)
                return npc
            return f

        @mk(0xA2)
        def _mov_c_bit(pc, npc, b1, b2):
            r = bit_reader(b1)

            def f():
                sfr[PSW] = (sfr[PSW] & 0x7F) | (r() << 7)
                return npc
            return f

        @mk(0x92)
        def _mov_bit_c(pc, npc, b1, b2):
            w = bit_writer(b1)

            def f():
                w(sfr[PSW] >> 7)
                return npc
            return f

        @mk(0x82)
        def _anl_c_bit(pc, npc, b1, b2):
            r = bit_reader(b1)

            def f():
                if not r():
                    sfr[PSW] &= 0x7F
                return npc
            return f

        @mk(0xB0)
        def _anl_c_nbit(pc, npc, b1, b2):
            r = bit_reader(b1)

            def f():
                if r():
                    sfr[PSW] &= 0x7F
                return npc
            return f

        @mk(0x72)
        def _orl_c_bit(pc, npc, b1, b2):
            r = bit_reader(b1)

            def f():
                if r():
                    sfr[PSW] |= 0x80
                return npc
            return f

        @mk(0xA0)
        def _orl_c_nbit(pc, npc, b1, b2):
            r = bit_reader(b1)

            def f():
                if not r():
                    sfr[PSW] |= 0x80
                return npc
            return f

        return S

    # ================================================================== decode / execution
    def _decode(self, pc: int) -> Callable[[], int]:
        nat = self._native.get(pc)
        if nat is not None:
            self._costs[pc] = 2 * self.clocks_per_mc
            f = nat
        else:
            code = self.code
            op = code[pc]
            e = OPCODES[op]
            if e is None:
                raise Fault("illegal_opcode", addr=pc, opcode=op, msg=f"reserved opcode 0x{op:02X}")
            npc = (pc + e.size) & 0xFFFF
            b1 = code[(pc + 1) & 0xFFFF] if e.size > 1 else 0
            b2 = code[(pc + 2) & 0xFFFF] if e.size > 2 else 0
            f = self._sem[op](pc, npc, b1, b2)
            self._costs[pc] = e.cycles * self.clocks_per_mc
            if self.spin_skip and _self_jump(op, pc, npc, b1, b2):
                f = _spin_wrap(pc, f)
        if pc in self._bps:
            f = self._bp_wrap(pc, f)
        self._cache[pc] = f
        return f

    def reset(self, pc: int = 0, sp: int = 0x07) -> None:
        sfr = self.sfr
        for a in (ACC, B, PSW, DPL, DPH):
            sfr[a] = 0
        sfr[SP] = sp & 0xFF
        if self.auxr1 is not None:
            sfr[self.auxr1] = 0
        self.dptr_alt[0] = self.dptr_alt[1] = 0
        self._pc = pc & 0xFFFF
        self.inhibit = False
        self.extram = False
        self._extra = 0
        self._sleep_req = None
        self.last_fault = None
        self.stop_info = None
        self.spin_pc = None
        self._skip_once = self.bp_skip = self._dbg_info = None

    def run(self, max_insns: int) -> tuple[StopReason, int]:
        """Execute until at least `max_insns` oscillator periods have elapsed (the Chip's cycle budget; see the module
        docstring); returns (reason, periods)."""
        extra, self._extra = self._extra, 0
        lim = self._lim
        lim[0] = max_insns + extra
        self._stop_req = False
        self.inhibit = False                     # this run executes at least the one instruction it delayed
        self.running = True
        self.spin_pc = None
        self._dbg_info = None
        if self.bp_skip is not None:
            if self.bp_skip == self._pc:
                self._skip_once = self._pc
            self.bp_skip = None
        if self._watch is not None:
            return self._run_watched(max_insns, extra)
        reason = StopReason.COUNT
        pc = self._pc
        count = extra
        n = 0
        cache, costs, decode = self._cache, self._costs, self._decode
        try:
            while True:
                try:
                    while count < lim[0]:
                        f = cache[pc]
                        if f is None:
                            f = decode(pc)
                        count += costs[pc]
                        n += 1
                        pc = f()
                    if self._sleep_req is not None:
                        reason = StopReason.WFI
                        self.stop_info, self._sleep_req = self._sleep_req, None
                    elif self._stop_req:
                        reason = StopReason.USER
                    break
                except _Spin:
                    # a jump to itself was taken: nothing changes until an interrupt, spend the slice in it
                    self.spin_pc = pc
                    c = costs[pc]
                    if count < lim[0] and not self._stop_req and self._sleep_req is None:
                        k = -(-(lim[0] - count) // c)
                        count += k * c
                        n += k
                    if self._stop_req:
                        reason = StopReason.USER
                    break
                except _Stop as s:
                    reason = self._stop_reason(s)
                    if s.reason == "dbg":                   # before executing the instruction at s.npc
                        count -= costs[s.npc]
                        n -= 1
                    pc = s.npc
                    break
                except Fault as e:                          # raised by decode: nothing executed
                    self.last_fault = {"type": e.kind, "pc": pc, "addr": e.info.get("addr"), "intno": None,
                                       **e.info}
                    reason = StopReason.FAULT
                    break
        finally:
            self._pc = pc
            self.running = False
            self._stop_req = False
            self.insn_count += n
            self.last_insns = n
            count += self._extra
            self._extra = 0
        return reason, count

    def _stop_reason(self, s: _Stop) -> StopReason:
        if s.reason == "reti":
            self.stop_info = "reti"
            return StopReason.EXC_RETURN
        self.stop_info, self._dbg_info = self._dbg_info, None
        if self.stop_info and self.stop_info.get("type") == "breakpoint":
            self.bp_skip = s.npc                     # a resume from here executes it
        return StopReason.DEBUG

    def _run_watched(self, max_insns: int, extra: int) -> tuple[StopReason, int]:
        """Run loop with data watchpoints: writes to watched RAM (IDATA, XRAM, XDATA memory, CODE) are found by
        comparing the watched bytes after every instruction; SFR and MOVX accesses are checked as they happen."""
        lim = self._lim
        reason = StopReason.COUNT
        pc = self._pc
        count = extra
        n = 0
        cache, costs, decode = self._cache, self._costs, self._decode
        snaps = self._watch_snaps
        rw = self._idata_rwatch
        try:
            while True:
                try:
                    while count < lim[0]:
                        f = cache[pc]
                        if f is None:
                            f = decode(pc)
                        count += costs[pc]
                        n += 1
                        ipc = pc
                        if rw:
                            for a in self.idata_reads(pc):
                                if rw[0] <= IDATA_BASE + a < rw[1]:
                                    self._watch_hit(IDATA_BASE + a, 1, "r", self.iram[a], pc)
                        pc = f()
                        for buf, lo, hi, base, old in snaps:
                            cur = buf[lo:hi]
                            if cur != old:
                                i = next(k for k in range(hi - lo) if cur[k] != old[k])
                                old[:] = cur
                                self._watch_hit(base + lo + i, 1, "w", cur[i], ipc)
                        if self._dbg_info is not None:
                            reason = StopReason.DEBUG
                            self.stop_info, self._dbg_info = self._dbg_info, None
                            break
                    else:
                        if self._sleep_req is not None:
                            reason = StopReason.WFI
                            self.stop_info, self._sleep_req = self._sleep_req, None
                        elif self._stop_req:
                            reason = StopReason.USER
                    break
                except _Spin:
                    self.spin_pc = pc
                    break
                except _Stop as s:
                    reason = self._stop_reason(s)
                    if s.reason == "dbg":
                        count -= costs[s.npc]
                        n -= 1
                    pc = s.npc
                    break
                except Fault as e:
                    self.last_fault = {"type": e.kind, "pc": pc, "addr": e.info.get("addr"), "intno": None,
                                       **e.info}
                    reason = StopReason.FAULT
                    break
        finally:
            self._pc = pc
            self.running = False
            self._stop_req = False
            self.insn_count += n
            self.last_insns = n
            count += self._extra
            self._extra = 0
        return reason, count

    def insns_in_slice(self) -> int:
        """Oscillator periods elapsed so far in the running slice (read from the run loop's frame)."""
        if not self.running:
            return 0
        codes = (MCS51Core.run.__code__, MCS51Core._run_watched.__code__)
        f = sys._getframe(1)
        while f is not None:
            if f.f_code in codes:
                return f.f_locals.get("count", 0) + self._extra
            f = f.f_back
        return 0

    def live_pc(self) -> int:
        """PC of the instruction executing now (core.pc is only written back at the end of a slice)."""
        if not self.running:
            return self._pc
        codes = (MCS51Core.run.__code__, MCS51Core._run_watched.__code__)
        f = sys._getframe(1)
        while f is not None:
            if f.f_code in codes:
                loc = f.f_locals
                return int(loc.get("ipc", loc.get("pc", self._pc)) if f.f_code is codes[1] else loc.get("pc", self._pc))
            f = f.f_back
        return self._pc

    def request_stop(self, from_mmio: bool = False) -> bool:
        """End the running slice after the current instruction (StopReason.USER).  Safe from MMIO callbacks and
        other threads."""
        if not self.running:
            return False
        self._stop_req = True
        self._lim[0] = 0
        return True

    def stop(self) -> bool:
        return self.request_stop()

    def enter_sleep(self, mode: str) -> None:
        """Called by the family power model when firmware sets PCON.IDL / PCON.PD: the core stops after the current
        instruction with StopReason.WFI and stop_info = mode ("idle" / "pd")."""
        self._sleep_req = mode
        if self.running:
            self._lim[0] = 0

    # ================================================================== debugger (chipem/debug.py)
    def set_breakpoints(self, addrs) -> None:
        """Code breakpoints: the decoded closure of each PC is wrapped so executing it stops first (StopReason.DEBUG,
        PC at the breakpoint); no per-instruction cost elsewhere."""
        new = {int(a) & 0xFFFF for a in addrs}
        changed = new ^ self._bps
        self._bps = new
        for pc in changed:
            self._cache[pc] = None

    def breakpoints(self) -> list[int]:
        return sorted(self._bps)

    def _bp_wrap(self, pc: int, f: Callable[[], int]) -> Callable[[], int]:
        def g():
            if self._skip_once == pc:                # resuming from this breakpoint: execute it once
                self._skip_once = None
                return f()
            h = self.debug_hook
            if h is None or h("break", {"addr": pc}):
                self._dbg_info = {"type": "breakpoint", "addr": pc}
                raise _Stop("dbg", pc)
            return f()
        return g

    def set_watchpoints(self, wps) -> None:
        """Data watchpoints [(lo, hi_exclusive, "r"|"w"|"rw")] in tagged addresses. Writes to RAM spaces are found
        by comparison after each instruction (reads of IDATA are not seen); SFR and MOVX (XDATA/XRAM) accesses are
        checked as they happen, reads included."""
        wl = [(int(lo), int(hi), str(acc)) for lo, hi, acc in wps]
        self._watch = wl or None
        snaps = []
        for lo, hi, acc in wl:
            if "w" not in acc:
                continue
            for base, buf, size in ((CODE_BASE, self.code, 0x10000), (XDATA_BASE, self.xdata, 0x10000),
                                    (IDATA_BASE, self.iram, 0x100), (XRAM_BASE, self.xram, 0x10000),
                                    (SFR_BASE, self.sfr, 0x100)):
                a, b = max(lo, base), min(hi, base + size)
                if a < b:
                    snaps.append((buf, a - base, b - base, base, bytearray(buf[a - base:b - base])))
        self._watch_snaps = snaps
        # read watchpoints on internal RAM: the IDATA addresses each instruction reads are computed before it runs
        rd = [(lo, hi) for lo, hi, acc in wl if "r" in acc and lo < IDATA_BASE + 0x100 and hi > IDATA_BASE]
        self._idata_rwatch = (min(r[0] for r in rd), max(r[1] for r in rd)) if rd else None
        self.flush_code_cache()                  # SFR accessors are rebuilt with (or without) the watch check

    def idata_reads(self, pc: int) -> list[int]:
        """Internal-RAM addresses (00-FF) the instruction at `pc` will read in the current state: direct operands
        below 80h that are sources or read-modify-write targets, R0-R7 of the current bank, @Ri pointers and their
        targets, bit-addressable bytes 20h-2Fh read by bit tests, and the stack bytes of POP/RET/RETI."""
        code, iram, sfr = self.code, self.iram, self.sfr
        op = code[pc & 0xFFFF]
        e = OPCODES[op]
        if e is None:
            return []
        b1, b2 = code[(pc + 1) & 0xFFFF], code[(pc + 2) & 0xFFFF]
        bank = sfr[PSW] & 0x18
        out: list[int] = []
        lo = op & 0x0F
        hi = op >> 4
        # register operand encoded in the opcode: columns 8-F are Rn, columns 6-7 @Ri in every row; MOVX @Ri
        # (E2/E3/F2/F3) reads Ri only (its target is XDATA)
        if lo >= 8:
            if hi not in (0x7, 0xA, 0xF):                 # MOV Rn,#imm / MOV Rn,dir / MOV Rn,A only write Rn
                out.append(bank | (lo & 7))
        elif lo in (6, 7):
            r = bank | (lo & 1)
            out.append(r)
            if hi not in (0x7, 0xA, 0xF):                 # MOV @Ri,#imm / MOV @Ri,dir / MOV @Ri,A only write it
                out.append(iram[r])
        elif op in (0xE2, 0xE3, 0xF2, 0xF3):
            out.append(bank | (op & 1))
        # direct operands
        pos = 1
        for i in e.order:
            k = e.ops[i]
            v = code[(pc + pos) & 0xFFFF]
            pos += {"dir": 1, "#imm": 1, "#imm16": 2, "bit": 1, "/bit": 1, "rel": 1, "a11": 1, "a16": 2}.get(k, 0)
            if k == "dir" and v < 0x80:
                dest_only = (op in (0x75, 0xF5, 0xD0) or 0x86 <= op <= 0x8F) and i == 0 or (op == 0x85 and i == 1)
                if not dest_only:
                    out.append(v)
            elif k in ("bit", "/bit") and v < 0x80 and op not in (0x92, 0xC2, 0xD2):
                out.append(0x20 + (v >> 3))
        if op == 0xD0:                                    # POP
            out.append(sfr[SP])
        elif op in (0x22, 0x32):                          # RET / RETI
            out += [sfr[SP], (sfr[SP] - 1) & 0xFF]
        return out

    def _watched(self, a: int) -> list:
        return [w for w in (self._watch or ()) if w[0] <= a < w[1]]

    def _watch_hit(self, addr: int, size: int, acc: str, value: int, pc: int | None = None) -> None:
        for lo, hi, mode in self._watch or ():
            if lo <= addr < hi and acc in mode:
                info = {"addr": addr, "size": size, "access": acc, "value": value,
                        "pc": self.live_pc() if pc is None else pc}
                h = self.debug_hook
                if h is None or h("watch", info):
                    self._dbg_info = {"type": "watchpoint", **info}
                    self.request_stop()
                return

    # ================================================================== interrupts
    def take_interrupt(self, vector: int) -> None:
        """Hardware LCALL to `vector`: push the PC (low byte first), jump; costs 2 machine cycles (charged to the
        next run)."""
        pc = self._pc
        iram, sfr = self.iram, self.sfr
        s = (sfr[SP] + 1) & 0xFF
        iram[s] = pc & 0xFF
        s = (s + 1) & 0xFF
        iram[s] = pc >> 8
        sfr[SP] = s
        self._pc = vector & 0xFFFF
        self._extra += 2 * self.clocks_per_mc

    # ================================================================== registers
    @property
    def pc(self) -> int:
        return self._pc

    @pc.setter
    def pc(self, value: int) -> None:
        self._pc = int(value) & 0xFFFF

    @property
    def sp(self) -> int:
        return self.sfr[SP]

    @sp.setter
    def sp(self, value: int) -> None:
        self.sfr[SP] = int(value) & 0xFF

    def psw(self) -> int:
        return self.sfr[PSW] | PAR[self.sfr[ACC]]

    def read_reg(self, name: str) -> int:
        n = name.lower()
        sfr = self.sfr
        if n == "pc":
            return self._pc
        if n in ("a", "acc"):
            return sfr[ACC]
        if n == "b":
            return sfr[B]
        if n == "psw":
            return self.psw()
        if n == "sp":
            return sfr[SP]
        if n == "dpl":
            return sfr[DPL]
        if n == "dph":
            return sfr[DPH]
        if n == "dptr":
            return (sfr[DPH] << 8) | sfr[DPL]
        if n in ("dptr0", "dptr1"):
            sel = self.dps()
            if int(n[-1]) == sel:
                return (sfr[DPH] << 8) | sfr[DPL]
            return (self.dptr_alt[1] << 8) | self.dptr_alt[0]
        if n == "dps":
            return self.dps()
        if n == "auxr1":
            return sfr[self.auxr1] if self.auxr1 is not None else 0
        if len(n) == 2 and n[0] == "r" and n[1].isdigit() and int(n[1]) < 8:
            return self.iram[(sfr[PSW] & 0x18) | int(n[1])]
        bit = _FLAGS.get(n)
        if bit is not None:
            return (self.psw() >> bit) & (3 if n == "rs" else 1)
        if n == "cycles_per_mc":
            return self.clocks_per_mc
        raise KeyError(f"unknown 8051 register {name!r}")

    def write_reg(self, name: str, value: int) -> None:
        n = name.lower()
        v = int(value)
        sfr = self.sfr
        if n == "pc":
            self._pc = v & 0xFFFF
        elif n in ("a", "acc"):
            sfr[ACC] = v & 0xFF
        elif n == "b":
            sfr[B] = v & 0xFF
        elif n == "psw":
            sfr[PSW] = v & 0xFE
        elif n == "sp":
            sfr[SP] = v & 0xFF
        elif n == "dpl":
            sfr[DPL] = v & 0xFF
        elif n == "dph":
            sfr[DPH] = v & 0xFF
        elif n == "dptr":
            sfr[DPH], sfr[DPL] = (v >> 8) & 0xFF, v & 0xFF
        elif n in ("dptr0", "dptr1"):
            if int(n[-1]) == self.dps():
                sfr[DPH], sfr[DPL] = (v >> 8) & 0xFF, v & 0xFF
            else:
                self.dptr_alt[1], self.dptr_alt[0] = (v >> 8) & 0xFF, v & 0xFF
        elif n in ("dps", "auxr1"):
            if self.auxr1 is not None:
                cur = sfr[self.auxr1]
                self._wr_auxr1(((cur & ~1) | (v & 1)) if n == "dps" else v)
        elif len(n) == 2 and n[0] == "r" and n[1].isdigit() and int(n[1]) < 8:
            self.iram[(sfr[PSW] & 0x18) | int(n[1])] = v & 0xFF
        elif n in _FLAGS:
            bit = _FLAGS[n]
            m = (3 if n == "rs" else 1) << bit
            if n != "p":
                sfr[PSW] = ((sfr[PSW] & ~m) | ((v << bit) & m)) & 0xFE
        else:
            raise KeyError(f"unknown 8051 register {name!r}")

    def dps(self) -> int:
        return self.sfr[self.auxr1] & 1 if self.auxr1 is not None else 0

    REG_NAMES = REG_NAMES


_FLAGS = {"cy": 7, "c": 7, "ac": 6, "f0": 5, "rs": 3, "rs1": 4, "rs0": 3, "ov": 2, "f1": 1, "p": 0}
