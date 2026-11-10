"""Pure-Python TMS320C28x (+FPU32) interpreter behind the `Core` interface (docs/CONTRACTS.md, "16-bit address units
and the C28x core").

Memory is one flat `array('H')` over the 22-bit word space (8 MB).  Two 4096-entry page tables (1024 words per
page) say whether a read / write of that page may go straight to the array: pages fully covered by one RAM region
are fast for both; ROM pages are fast for reads; MMIO, unmapped, mixed pages and pages holding decoded code
(writes must invalidate the decode cache) take the slow path, which bisects the region list.

Decoding is table driven (isa.py) and cached per PC: `cache[pc]` is a closure with its operands bound that executes
the instruction and returns the next PC.  The run loop is `pc = cache[pc]()`.
"""
from __future__ import annotations

import bisect
import sys
from array import array
from typing import Any, Callable

from ..base import Core, StopReason
from .fpu import SEMANTICS as FPU_SEMANTICS
from .isa import get_isa
from .sem import SEMANTICS as BASE_SEMANTICS, Fault, M16, M22, M32, StopExc

SEMANTICS = {**BASE_SEMANTICS, **FPU_SEMANTICS}
SPACE = 1 << 22
PAGE_SHIFT = 10
NPAGES = SPACE >> PAGE_SHIFT

ST1_BITS = (("arp", 13, 7), ("xf", 12, 1), ("m0m1map", 11, 1), ("objmode", 9, 1), ("amode", 8, 1),
            ("idlestat", 7, 1), ("eallow", 6, 1), ("loop", 5, 1), ("spa", 4, 1), ("vmap", 3, 1), ("page0", 2, 1),
            ("dbgm", 1, 1), ("intm", 0, 1))


class _Illegal(Exception):
    def __init__(self, pc: int, opcode: int):
        super().__init__(f"illegal opcode 0x{opcode:04X} at 0x{pc:06X}")
        self.pc, self.opcode = pc, opcode


class C28xCore(Core):
    page_size = 0x40                 # Bus MMIO dispatch granularity (words); the core itself maps any size
    cycles_per_insn = 1
    exact_counts = True
    sp = 0x400                       # plain instance attribute (shadows Core.sp property: hot path)

    def __init__(self, cpu: str = "c28x", *, fpu: bool = True, precise: bool = False):
        self.cpu = cpu
        self.fpu = fpu
        self.precise = precise
        self.isa = get_isa()
        self.mem = array("H", bytes(2 * SPACE))
        self._rslow = bytearray(b"\x01" * NPAGES)
        self._wslow = bytearray(b"\x01" * NPAGES)
        self._page_ram = bytearray(NPAGES)           # 1 = page entirely in one writable RAM region
        self._regions: list[tuple[int, int, str, str, Any, Any]] = []   # (base, end, kind, name, rd, wr)
        self._starts: list[int] = []
        self._unmapped: Callable[[int, int, str], bool] | None = None
        self._cache: dict[int, Callable[[], int]] = {}
        self._saved_cache: dict | None = None
        self._code_pages: dict[int, set[int]] = {}
        self._stop_req = False
        self._extra = 0
        self.running = False
        self.last_fault: dict | None = None
        self.stop_info = None
        self.itrap_handler: Callable[..., bool] | None = None
        self.vector_fetch: Callable[[int], int] | None = None
        self.nmi_pending = False
        self.rom_writes = 0
        # registers
        self.acc = self.p = self.xt = 0
        self.xar = [0] * 8
        self.sp = 0x400
        self.dp = 0
        self._pc = 0
        self.rpc = 0
        self.ier = self.ifr = self.dbgier = self.dbgstat = 0
        self.ovc = self.pm = self.v = self.n = self.z = self.c = self.tc = self.ovm = self.sxm = 0
        self.arp = self.xf = self.m0m1map = self.objmode = self.amode = self.idlestat = 0
        self.eallow = self.loop = self.spa = self.vmap = self.page0 = self.dbgm = self.intm = 0
        self.r = [0] * 8
        self.stf = 0
        self.rb = 0
        self.fshadow = None
        self._rptb_ends: set[int] = set()          # word addresses just after RPTB blocks seen so far
        self._make_accessors()
        self.reset(0, 0x400)

    # ================================================================== memory
    def _make_accessors(self) -> None:
        mem, rs, ws = self.mem, self._rslow, self._wslow
        slow_rd, slow_wr = self._slow_rd, self._slow_wr

        def rd16(a):
            if rs[a >> 10]:
                return slow_rd(a, 1)
            return mem[a]

        def wr16(a, v):
            if ws[a >> 10]:
                slow_wr(a, 1, v & 0xFFFF)
            else:
                mem[a] = v & 0xFFFF

        def rd32(a):
            a &= 0x3FFFFE
            if rs[a >> 10]:
                return slow_rd(a, 2)
            return mem[a] | (mem[a + 1] << 16)

        def wr32(a, v):
            a &= 0x3FFFFE
            if ws[a >> 10]:
                slow_wr(a, 2, v & 0xFFFFFFFF)
            else:
                mem[a] = v & 0xFFFF
                mem[a + 1] = (v >> 16) & 0xFFFF

        def push32(v):
            a = self.sp
            wr32(a, v)
            self.sp = (a + 2) & 0xFFFF

        def pop32():
            a = (self.sp - 2) & 0xFFFF
            self.sp = a
            return rd32(a)

        self.rd16, self.wr16, self.rd32, self.wr32 = rd16, wr16, rd32, wr32
        self.push32, self.pop32 = push32, pop32

    def _region(self, a: int):
        i = bisect.bisect_right(self._starts, a) - 1
        if i >= 0:
            r = self._regions[i]
            if a < r[1]:
                return r
        return None

    def _lookup(self, a: int, n: int, access: str):
        r = self._region(a)
        if r is None and self._unmapped is not None:
            try:
                if self._unmapped(a, n, access):
                    r = self._region(a)
            except Exception:
                r = None
        if r is None:
            kind = {"r": "read_unmapped", "w": "write_unmapped", "x": "fetch_unmapped"}[access]
            raise Fault(kind, addr=a, size=n, access=access)
        return r

    def _slow_rd(self, a: int, n: int) -> int:
        r = self._lookup(a, n, "r")
        if r[2] == "mmio":
            return r[4](a - r[0], n) & (0xFFFF if n == 1 else 0xFFFFFFFF)
        mem = self.mem
        return mem[a] if n == 1 else mem[a] | (mem[a + 1] << 16)

    def _slow_wr(self, a: int, n: int, v: int) -> None:
        p = a >> PAGE_SHIFT
        if p in self._code_pages:
            self._invalidate_page(p)
        r = self._lookup(a, n, "w")
        kind = r[2]
        if kind == "ram":
            mem = self.mem
            mem[a] = v & 0xFFFF
            if n == 2:
                mem[a + 1] = (v >> 16) & 0xFFFF
        elif kind == "mmio":
            r[5](a - r[0], n, v)
        else:
            self.rom_writes += 1

    def _invalidate_page(self, p: int) -> None:
        pcs = self._code_pages.pop(p, ())
        cache, saved = self._cache, self._saved_cache
        for pc in pcs:
            cache.pop(pc, None)
            if saved is not None:
                saved.pop(pc, None)
        self._wslow[p] = 0 if self._page_ram[p] else 1

    def _add_region(self, base: int, size: int, kind: str, name: str, rd=None, wr=None) -> None:
        if size <= 0:
            return
        end = base + size
        if base < 0 or end > SPACE:
            raise ValueError(f"region 0x{base:X}+0x{size:X} outside the 22-bit space")
        for b, e, *_ in self._regions:
            if b < end and base < e:
                raise ValueError(f"mapping {name} 0x{base:X}..0x{end:X} overlaps 0x{b:X}..0x{e:X}")
        i = bisect.bisect_left(self._starts, base)
        self._starts.insert(i, base)
        self._regions.insert(i, (base, end, kind, name, rd, wr))
        for p in range(base >> PAGE_SHIFT, ((end - 1) >> PAGE_SHIFT) + 1):
            self._update_page(p)

    def _update_page(self, p: int) -> None:
        lo, hi = p << PAGE_SHIFT, (p + 1) << PAGE_SHIFT
        r = self._region(lo)
        whole = r is not None and r[0] <= lo and r[1] >= hi
        if whole and r[2] in ("ram", "rom"):
            self._rslow[p] = 0
            self._page_ram[p] = 1 if r[2] == "ram" else 0
        else:
            self._rslow[p] = 1
            self._page_ram[p] = 0
        self._wslow[p] = 0 if (self._page_ram[p] and p not in self._code_pages) else 1

    def map_memory(self, *args, writable: bool = True, executable: bool = True, data: bytes | None = None,
                   name: str | None = None, **_kw) -> None:
        """map_memory(base, size_units, *, writable) or map_memory(name, base, size_units, data=None, writable=True).
        Sizes are in 16-bit words; `data` is a little-endian byte image."""
        if args and isinstance(args[0], str):
            name, args = args[0], args[1:]
        base, size = int(args[0]), int(args[1])
        if len(args) > 2:
            data = args[2]
        if len(args) > 3:
            writable = bool(args[3])
        self._add_region(base, size, "ram" if writable else "rom", name or f"mem_{base:06x}")
        if data:
            self._poke(base, bytes(data))

    def map_mmio(self, base: int, size: int, read: Callable[[int, int], int],
                 write: Callable[[int, int, int], None]) -> None:
        self._add_region(base, size, "mmio", f"mmio_{base:06x}", read, write)

    def set_unmapped_handler(self, fn) -> None:
        self._unmapped = fn

    def flush_code_cache(self) -> None:
        self._cache.clear()
        if self._saved_cache is not None:
            self._saved_cache.clear()
        for p in list(self._code_pages):
            self._code_pages.pop(p)
            self._wslow[p] = 0 if self._page_ram[p] else 1

    def _poke(self, addr: int, data: bytes) -> None:
        """Write a byte image into RAM/ROM regions directly (MMIO through callbacks), invalidating code."""
        if len(data) & 1:
            data = data + bytes([self.read_mem(addr + len(data) // 2, 1)[1]])
        words = array("H")
        words.frombytes(data)
        if sys.byteorder != "little":
            words.byteswap()
        n = len(words)
        i = 0
        while i < n:
            a = addr + i
            r = self._region(a)
            if r is None:
                raise ValueError(f"write_mem: unmapped address 0x{a:06X}")
            if r[2] == "mmio":
                r[5](a - r[0], 1, words[i])
                i += 1
                continue
            cnt = min(n - i, r[1] - a)
            self.mem[a:a + cnt] = words[i:i + cnt]
            for p in range(a >> PAGE_SHIFT, ((a + cnt - 1) >> PAGE_SHIFT) + 1):
                if p in self._code_pages:
                    self._invalidate_page(p)
            i += cnt

    def read_mem(self, addr: int, n: int) -> bytes:
        """`n` words at `addr` as a little-endian byte image."""
        out = array("H")
        i = 0
        while i < n:
            a = addr + i
            r = self._region(a)
            if r is None:
                raise ValueError(f"read_mem: unmapped address 0x{a:06X}")
            if r[2] == "mmio":
                out.append(r[4](a - r[0], 1) & 0xFFFF)
                i += 1
                continue
            cnt = min(n - i, r[1] - a)
            out.extend(self.mem[a:a + cnt])
            i += cnt
        if sys.byteorder != "little":
            out.byteswap()
        return out.tobytes()

    def write_mem(self, addr: int, data: bytes) -> None:
        self._poke(addr, bytes(data))

    # ================================================================== decode
    def _fetch_word(self, a: int) -> int:
        a &= M22
        if not self._rslow[a >> PAGE_SHIFT]:
            return self.mem[a]
        r = self._region(a)
        if r is None:
            raise Fault("fetch_unmapped", addr=a, access="x")
        if r[2] == "mmio":
            return r[4](a - r[0], 1) & 0xFFFF
        return self.mem[a]

    def _decode(self, pc: int) -> Callable[[], int]:
        if self._stop_req:
            raise StopExc("user", pc)
        w1 = self._fetch_word(pc)
        m = self.isa.match(w1, lambda: self._fetch_word(pc + 1))
        if m is None:
            raise _Illegal(pc, w1)
        e, word = m
        if e.fpu and not self.fpu:
            raise _Illegal(pc, w1)                   # FPU32 opcode on a core without the FPU: ITRAP as on silicon
        if self.amode and "L" in e.fields:
            # SPRU430F ST1: PAGE0 = AMODE = 1 traps every loc16/loc32 instruction; 11110xxx/11111xxx are reserved
            # in the AMODE = 1 column of Table 5-1
            if self.page0 or self.isa._field(e, "L", word) >= 0xF0:
                raise _Illegal(pc, w1)
        npc = (pc + e.nwords) & M22
        o = self.isa.operands(e, word, pc)
        try:
            f = SEMANTICS[e.sem](self, o, pc, npc)
        except Fault:
            raise
        except KeyError as ex:
            raise Fault("unimplemented", msg=f"no semantics for {e.syntax()} ({ex})", opcode=w1) from None
        if e.norpt:
            f.norpt = True
        if pc in self._rptb_ends:
            f = self._rptb_wrap(pc, f)
        self._cache[pc] = f
        cp = self._code_pages
        for p in {pc >> PAGE_SHIFT, (npc - 1) >> PAGE_SHIFT}:
            s = cp.get(p)
            if s is None:
                cp[p] = s = set()
                self._wslow[p] = 1
            s.add(pc)
        return f

    # ------------------------------------------------------------------ RPTB (repeat block, FPU cores)
    def _rptb_arm(self, end: int) -> None:
        """Called by RPTB: make sure the instruction at `end` (the word after the block) is decoded with the
        loop-back check."""
        end &= M22
        if end not in self._rptb_ends:
            self._rptb_ends.add(end)
            self._cache.pop(end, None)
            if self._saved_cache is not None:
                self._saved_cache.pop(end, None)

    def _rptb_wrap(self, x: int, f: Callable[[], int]) -> Callable[[], int]:
        """Closure for address x (just after a repeat block): while RB.RA is set and RB.RE names the word before x,
        decrement RC (clearing RA at 0) and execute the block's first instruction instead of the one at x.  Matching
        on RE (7 bits) like the hardware means a block restored with POP RB / IRET (RA = RAS) loops correctly."""
        re_ = (x - 1) & 0x7F

        def g():
            rb = self.rb
            if rb & 0x40000000 and ((rb >> 16) & 0x7F) == re_:
                start = (x - ((rb >> 23) & 0x7F)) & M22
                fs = self._fetch(start)              # first: a stop request raised here leaves RB untouched
                rc = ((rb & M16) - 1) & M16
                rb = (rb & 0xFFFF0000) | rc
                if rc == 0:
                    rb &= ~0x40000000
                self.rb = rb
                return fs()
            return f()
        for attr in ("norpt", "rpt"):
            if hasattr(f, attr):
                setattr(g, attr, getattr(f, attr))
        return g

    def _fetch(self, pc: int) -> Callable[[], int]:
        f = self._cache.get(pc)
        return f if f is not None else self._decode(pc)

    def decode_at(self, pc: int):
        """(entry, operands, length) of the instruction at pc (no caching, no execution); None if illegal."""
        w1 = self._fetch_word(pc)
        m = self.isa.match(w1, lambda: self._fetch_word(pc + 1))
        if m is None:
            return None
        e, word = m
        return e, self.isa.operands(e, word, pc), e.nwords

    # ================================================================== execution
    def reset(self, pc: int, sp: int) -> None:
        self.acc = self.p = self.xt = 0
        for i in range(8):
            self.xar[i] = 0
            self.r[i] = 0
        self.sp = sp & M16
        self.dp = 0
        self._pc = pc & M22
        self.rpc = 0
        self.ier = self.ifr = self.dbgier = self.dbgstat = 0
        self.st0 = 0
        self.st1 = 0x0A0B          # OBJMODE=1 (as left by the boot ROM), M0M1MAP=1, VMAP=1, DBGM=1, INTM=1, AMODE=0
        self.stf = 0
        self.rb = 0
        self.fshadow = None
        self._rptb_ends.clear()
        self.nmi_pending = False
        self.last_fault = None
        self.stop_info = None
        self._cache.clear()
        for p in list(self._code_pages):
            self._code_pages.pop(p)
            self._wslow[p] = 0 if self._page_ram[p] else 1

    def run(self, max_insns: int) -> tuple[StopReason, int]:
        self._extra = 0
        self._stop_req = False
        self.running = True
        reason = StopReason.COUNT
        pc = self._pc
        count = 0
        cache = self._cache
        cget = cache.get
        decode = self._decode
        try:
            while True:
                try:
                    while count < max_insns:
                        f = cget(pc)
                        if f is None:
                            f = decode(pc)
                        pc = f()
                        count += 1
                    reason = StopReason.COUNT
                    break
                except StopExc as s:
                    if s.reason == "user":
                        reason = StopReason.USER
                        break
                    count += 1
                    pc = s.npc
                    reason = StopReason.WFI if s.reason == "idle" else StopReason.BREAKPOINT
                    if s.reason == "idle":
                        self.stop_info = "idle"
                    break
                except _Illegal as e:
                    self.last_fault = {"type": "illegal_opcode", "pc": e.pc, "opcode": e.opcode, "addr": None,
                                       "intno": None}
                    h = self.itrap_handler
                    if h is not None:
                        self._pc = pc
                        taken = h(pc)
                        if taken:
                            count += 1
                            pc = self._pc
                            continue
                    reason = StopReason.FAULT
                    break
                except Fault as e:
                    self.last_fault = {"type": e.kind, "pc": pc, "addr": e.info.get("addr"), "intno": None,
                                       **e.info}
                    reason = StopReason.FAULT
                    break
        finally:
            self._pc = pc
            self.running = False
            if self._saved_cache is not None:
                saved, self._saved_cache = self._saved_cache, None
                cache.update(saved)
            self._stop_req = False
        return reason, count + self._extra

    def insns_in_slice(self) -> int:
        """Instructions executed so far in the running slice (read from the run loop's frame)."""
        if not self.running:
            return 0
        code = C28xCore.run.__code__
        f = sys._getframe(1)
        while f is not None:
            if f.f_code is code:
                return f.f_locals.get("count", 0) + self._extra
            f = f.f_back
        return 0

    def request_stop(self, from_mmio: bool = False) -> bool:
        """End the running slice before the next instruction (StopReason.USER).  Safe from MMIO callbacks and from
        other threads: the decode cache is swapped out so the next fetch takes the decode path, which notices."""
        if not self.running:
            return False
        if not self._stop_req:
            self._stop_req = True
            self._saved_cache = dict(self._cache)
            self._cache.clear()
        return True

    def stop(self) -> bool:
        return self.request_stop()

    # ================================================================== interrupts
    def interrupt_pending(self) -> int | None:
        """Number of the interrupt the CPU would take now (18 = NMI, 1..16 = INT1..14/DLOGINT/RTOSINT), else None."""
        if self.nmi_pending:
            return 18
        if self.intm:
            return None
        m = self.ifr & self.ier & M16
        return (m & -m).bit_length() if m else None

    def _ctx_save(self, ret: int, num: int, maskable: bool) -> None:
        push32 = self.push32
        self.sp = (self.sp + 1) & M16
        push32(((self.xt >> 16) << 16) | self.st0)                     # T:ST0
        push32(self.acc)                                                # AH:AL
        push32(self.p)                                                  # PH:PL
        push32(((self.xar[1] & M16) << 16) | (self.xar[0] & M16))      # AR1:AR0
        push32((self.dp << 16) | self.st1)                              # DP:ST1
        push32((self.dbgstat << 16) | self.ier)                         # DBGSTAT:IER
        push32(ret & M22)                                               # return PC
        rb = self.rb
        if rb & 0xC0000000:                                             # RB: RAS = RA, RA = 0 (SPRUHS1C RB)
            self.rb = (rb & 0x3FFFFFFF) | ((rb & 0x40000000) << 1)
        if maskable and 1 <= num <= 16:
            bit = 1 << (num - 1)
            self.ifr &= ~bit & M16
            self.ier &= ~bit & M16
        self.intm = 1
        self.dbgm = 1
        self.eallow = 0
        self.loop = 0
        self.idlestat = 0

    def take_interrupt(self, vector_addr: int, int_number: int) -> None:
        """Hardware interrupt entry: automatic context save (SPRU430 §3.4), IFR/IER bit of INT1..14/DLOGINT/RTOSINT
        cleared, INTM=DBGM=1, EALLOW=LOOP=IDLESTAT=0, PC = vector_addr.  Return address = current PC."""
        self._ctx_save(self._pc, int_number, True)
        if int_number == 18:
            self.nmi_pending = False
        self._pc = vector_addr & M22

    def _vector(self, n: int) -> int:
        vf = self.vector_fetch
        if vf is not None:
            return vf(n) & M22
        base = 0x3FFFC0 if self.vmap else 0
        return self.rd32(base + 2 * n) & M22

    def _sw_interrupt(self, vec: int, num: int, ret: int, maskable: bool = False) -> int:
        target = self._vector(vec)
        self._ctx_save(ret, num, maskable)
        return target

    def _iret(self) -> int:
        pop32 = self.pop32
        pc = pop32() & M22
        v = pop32()
        self.ier, self.dbgstat = v & M16, v >> 16
        v = pop32()
        self.st1, self.dp = v & M16, v >> 16
        v = pop32()
        xar = self.xar
        xar[0] = (xar[0] & 0xFFFF0000) | (v & M16)
        xar[1] = (xar[1] & 0xFFFF0000) | (v >> 16)
        self.p = pop32()
        self.acc = pop32()
        v = pop32()
        self.st0 = v & M16
        self.xt = (self.xt & M16) | (v & 0xFFFF0000)
        self.sp = (self.sp - 1) & M16
        rb = self.rb
        if rb & 0xC0000000:                                             # RB: RA = RAS, RAS = 0
            self.rb = (rb & 0x3FFFFFFF) | ((rb >> 1) & 0x40000000)
        return pc

    # ================================================================== registers
    @property
    def st0(self) -> int:
        return (((self.ovc & 0x3F) << 10) | (self.pm << 7) | (self.v << 6) | (self.n << 5) | (self.z << 4)
                | (self.c << 3) | (self.tc << 2) | (self.ovm << 1) | self.sxm)

    @st0.setter
    def st0(self, v: int) -> None:
        o = (v >> 10) & 0x3F
        self.ovc = o - 64 if o & 0x20 else o
        self.pm = (v >> 7) & 7
        self.v = (v >> 6) & 1
        self.n = (v >> 5) & 1
        self.z = (v >> 4) & 1
        self.c = (v >> 3) & 1
        self.tc = (v >> 2) & 1
        self.ovm = (v >> 1) & 1
        self.sxm = v & 1

    @property
    def st1(self) -> int:
        v = 0
        for name, sh, m in ST1_BITS:
            v |= (getattr(self, name) & m) << sh
        return v

    @st1.setter
    def st1(self, v: int) -> None:
        old_amode = self.amode
        for name, sh, m in ST1_BITS:
            setattr(self, name, (v >> sh) & m)
        if self.amode != old_amode:
            self.flush_code_cache()

    @property
    def pc(self) -> int:
        return self._pc

    @pc.setter
    def pc(self, value: int) -> None:
        self._pc = value & M22

    def read_reg(self, name: str) -> int:
        n = name.lower()
        f = _READ.get(n)
        if f is not None:
            return f(self)
        if n.startswith("xar") and n[3:].isdigit():
            return self.xar[int(n[3:])]
        if n.startswith("ar") and n[2:].isdigit():
            return self.xar[int(n[2:])] & M16
        if n.startswith("r") and n.endswith("h") and n[1:-1].isdigit():
            return self.r[int(n[1:-1])]
        if n in _FLAG_NAMES:
            v = getattr(self, n)
            return v & 0x3F if n == "ovc" else v
        raise KeyError(f"unknown C28x register {name!r}")

    def write_reg(self, name: str, value: int) -> None:
        n = name.lower()
        v = int(value)
        f = _WRITE.get(n)
        if f is not None:
            f(self, v)
            return
        if n.startswith("xar") and n[3:].isdigit():
            self.xar[int(n[3:])] = v & M32
            return
        if n.startswith("ar") and n[2:].isdigit():
            i = int(n[2:])
            self.xar[i] = (self.xar[i] & 0xFFFF0000) | (v & M16)
            return
        if n.startswith("r") and n.endswith("h") and n[1:-1].isdigit():
            self.r[int(n[1:-1])] = v & M32
            return
        if n in _FLAG_NAMES:
            if n == "ovc":
                v &= 0x3F
                v = v - 64 if v & 0x20 else v
            elif n == "amode":
                if v != self.amode:
                    self.amode = v & 1
                    self.flush_code_cache()
                return
            setattr(self, n, v)
            return
        raise KeyError(f"unknown C28x register {name!r}")

    REG_NAMES = (["acc", "p", "xt"] + [f"xar{i}" for i in range(8)] + ["sp", "dp", "pc", "rpc", "st0", "st1", "ier",
                                                                       "ifr", "dbgier"])
    FPU_REG_NAMES = [f"r{i}h" for i in range(8)] + ["stf", "rb"]


def _set_acc_half(c, hi: bool, v: int) -> None:
    c.acc = ((c.acc & M16) | ((v & M16) << 16)) if hi else ((c.acc & 0xFFFF0000) | (v & M16))


def _set_p_half(c, hi: bool, v: int) -> None:
    c.p = ((c.p & M16) | ((v & M16) << 16)) if hi else ((c.p & 0xFFFF0000) | (v & M16))


def _set_xt_half(c, hi: bool, v: int) -> None:
    c.xt = ((c.xt & M16) | ((v & M16) << 16)) if hi else ((c.xt & 0xFFFF0000) | (v & M16))


_READ: dict[str, Callable[[C28xCore], int]] = {
    "acc": lambda c: c.acc, "ah": lambda c: c.acc >> 16, "al": lambda c: c.acc & M16,
    "p": lambda c: c.p, "ph": lambda c: c.p >> 16, "pl": lambda c: c.p & M16,
    "xt": lambda c: c.xt, "t": lambda c: c.xt >> 16, "tl": lambda c: c.xt & M16,
    "sp": lambda c: c.sp, "dp": lambda c: c.dp, "pc": lambda c: c._pc, "rpc": lambda c: c.rpc,
    "st0": lambda c: c.st0, "st1": lambda c: c.st1, "ier": lambda c: c.ier, "ifr": lambda c: c.ifr,
    "dbgier": lambda c: c.dbgier, "dbgstat": lambda c: c.dbgstat, "stf": lambda c: c.stf, "rb": lambda c: c.rb,
}
_WRITE: dict[str, Callable[[C28xCore, int], None]] = {
    "acc": lambda c, v: setattr(c, "acc", v & M32), "ah": lambda c, v: _set_acc_half(c, True, v),
    "al": lambda c, v: _set_acc_half(c, False, v),
    "p": lambda c, v: setattr(c, "p", v & M32), "ph": lambda c, v: _set_p_half(c, True, v),
    "pl": lambda c, v: _set_p_half(c, False, v),
    "xt": lambda c, v: setattr(c, "xt", v & M32), "t": lambda c, v: _set_xt_half(c, True, v),
    "tl": lambda c, v: _set_xt_half(c, False, v),
    "sp": lambda c, v: setattr(c, "sp", v & M16), "dp": lambda c, v: setattr(c, "dp", v & M16),
    "pc": lambda c, v: setattr(c, "_pc", v & M22), "rpc": lambda c, v: setattr(c, "rpc", v & M22),
    "st0": lambda c, v: setattr(c, "st0", v & M16), "st1": lambda c, v: setattr(c, "st1", v & M16),
    "ier": lambda c, v: setattr(c, "ier", v & M16), "ifr": lambda c, v: setattr(c, "ifr", v & M16),
    "dbgier": lambda c, v: setattr(c, "dbgier", v & M16), "dbgstat": lambda c, v: setattr(c, "dbgstat", v & M16),
    "stf": lambda c, v: setattr(c, "stf", v & M32), "rb": lambda c, v: setattr(c, "rb", v & M32),
}
_FLAG_NAMES = {"ovc", "pm", "v", "n", "z", "c", "tc", "ovm", "sxm"} | {n for n, _, _ in ST1_BITS}
