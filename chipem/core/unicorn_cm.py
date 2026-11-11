"""Cortex-M core on Unicorn 2.x (QEMU TCG).

Unicorn executes instructions only; exception entry/return, NVIC and SysTick live in chipem.arch.armv7m.
Facts this relies on (verified on Unicorn 2.1.4):
  * `wfi` ends emu_start with PC just past the WFI.
  * A branch to an EXC_RETURN value raises UC_HOOK_INTR intno 8 with PC = value & ~1 (Unicorn would then spin
    on intno 3), so the hook records it and calls emu_stop(); the arch layer unstacks.
  * `svc` raises intno 2 with PC already past the svc; `bkpt` raises intno 7 with PC at the bkpt.
  * emu_stop() inside an MMIO *write* callback leaves PC at the store (the store has happened), and inside a *read*
    callback it aborts the load (it runs again on resume).  So a stop requested from an MMIO callback lowers the
    instruction limit of the running emu_start instead (`end_slice_at`, `request_stop(from_mmio=True)`): Unicorn's
    count hook then ends the run in front of the next instruction, after the accessing one completed exactly once,
    whatever it is (load/store multiple, writeback, IT block).  Only without the calibrated counter do we fall back
    to emu_stop + stepping PC past a plain single-register store (any other stop request is refused).
  * Writing IPSR/XPSR/CONTROL through reg_write keeps QEMU's MSP/PSP banking consistent.
  * uc_mem_unmap inside a memory hook (an MMIO callback) works until that emu_start ends, which then raises a
    spurious UC_HOOK_INTR intno 3 (EXCP_PREFETCH_ABORT) at the next instruction ("prefetch abort on the next fetch").
    Mapping inside the unmapped-access hook (auto stubs) is fine. So mapping changes requested while the core runs
    (map_memory / map_mmio / map_shared / unmap from a callback, e.g. Bus.map_window) are queued, the slice ends
    after the accessing instruction (end_slice_at) and the queue is applied once emu_start has returned: the next
    instruction sees the new mapping.
"""
from __future__ import annotations

import ctypes
from typing import Callable, Sequence

from unicorn import (UC_ARCH_ARM, UC_HOOK_BLOCK, UC_HOOK_CODE, UC_HOOK_INTR, UC_HOOK_MEM_PROT, UC_HOOK_MEM_READ,
                     UC_HOOK_MEM_UNMAPPED, UC_HOOK_MEM_WRITE, UC_MEM_WRITE,
                     UC_MEM_FETCH_PROT, UC_MEM_FETCH_UNMAPPED, UC_MEM_READ_PROT, UC_MEM_READ_UNMAPPED,
                     UC_MEM_WRITE_PROT, UC_MEM_WRITE_UNMAPPED, UC_MODE_MCLASS, UC_MODE_THUMB, UC_PROT_EXEC,
                     UC_PROT_READ, UC_PROT_WRITE, Uc, UcError)
from unicorn import arm_const as A
from unicorn import unicorn_const as UCC

try:  # direct batch register access (the Python wrapper costs ~1 µs per register)
    from unicorn.unicorn_py3.unicorn import uclib as _uclib
except Exception:  # pragma: no cover
    _uclib = None

from .base import Core, StopReason

CPU_MODELS = {
    "cortex-m0": A.UC_CPU_ARM_CORTEX_M0,
    "cortex-m0plus": A.UC_CPU_ARM_CORTEX_M0,
    "cortex-m0+": A.UC_CPU_ARM_CORTEX_M0,
    "cortex-m1": A.UC_CPU_ARM_CORTEX_M0,
    "cortex-m3": A.UC_CPU_ARM_CORTEX_M3,
    "cortex-m4": A.UC_CPU_ARM_CORTEX_M4,
    "cortex-m4f": A.UC_CPU_ARM_CORTEX_M4,
    "cortex-m7": A.UC_CPU_ARM_CORTEX_M7,
    "cortex-m33": A.UC_CPU_ARM_CORTEX_M33,
}

REGS: dict[str, int] = {f"r{i}": getattr(A, f"UC_ARM_REG_R{i}") for i in range(16)}
REGS.update(sp=A.UC_ARM_REG_SP, lr=A.UC_ARM_REG_LR, pc=A.UC_ARM_REG_PC, xpsr=A.UC_ARM_REG_XPSR,
            apsr=A.UC_ARM_REG_APSR, ipsr=A.UC_ARM_REG_IPSR, epsr=A.UC_ARM_REG_EPSR, msp=A.UC_ARM_REG_MSP,
            psp=A.UC_ARM_REG_PSP, primask=A.UC_ARM_REG_PRIMASK, basepri=A.UC_ARM_REG_BASEPRI,
            faultmask=A.UC_ARM_REG_FAULTMASK, control=A.UC_ARM_REG_CONTROL, fpscr=A.UC_ARM_REG_FPSCR,
            ip=A.UC_ARM_REG_R12, fp=A.UC_ARM_REG_R11)
REGS.update({f"s{i}": getattr(A, f"UC_ARM_REG_S{i}") for i in range(32)})

_INTNO_NAMES = {1: "undefined_instruction", 3: "prefetch_abort", 4: "data_abort", 5: "irq", 6: "fiq",
                9: "kernel_trap", 17: "nocp", 18: "invalid_state", 19: "stack_overflow", 20: "lazyfp",
                21: "lserr", 22: "unaligned"}
_ACCESS = {UC_MEM_READ_UNMAPPED: ("r", "read_unmapped"), UC_MEM_WRITE_UNMAPPED: ("w", "write_unmapped"),
           UC_MEM_FETCH_UNMAPPED: ("x", "fetch_unmapped"), UC_MEM_READ_PROT: ("r", "read_prot"),
           UC_MEM_WRITE_PROT: ("w", "write_prot"), UC_MEM_FETCH_PROT: ("x", "fetch_prot")}
_UNTIL = 0xFFFFFFFF  # never a valid Thumb PC

# Unicorn implements emu_start(count=N) with an internal per-instruction hook that increments the uint64
# `uc_struct.emu_counter` (reset by every emu_start).  The library does not export it, so we locate it once by
# calibration (run known counts, find the word equal to count+1 next to the word equal to count) and read it
# via ctypes; this makes fast-mode instruction counts exact for early stops too.  If calibration fails we fall
# back to an estimate (half the slice).
_COUNTER_OFF: int | None = None
_CALIBRATED = False
_CAL_ERROR: str | None = None
_CAL_WARNED = False


def calibration_warning() -> str | None:
    """Message describing a failed counter calibration, returned only once per process (else None)."""
    global _CAL_WARNED
    if _COUNTER_OFF is not None or not _CALIBRATED or _CAL_WARNED:
        return None
    _CAL_WARNED = True
    return ("Unicorn instruction-counter calibration failed" + (f" ({_CAL_ERROR})" if _CAL_ERROR else "")
            + ": fast mode charges half a slice for early stops; in-slice timestamps fall back to slice "
              "granularity (use --precise for exact counts)")


def _calibrate_counter() -> int | None:
    global _COUNTER_OFF, _CALIBRATED, _CAL_ERROR
    if _CALIBRATED:
        return _COUNTER_OFF
    _CALIBRATED = True
    try:
        uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        uc.ctl_set_cpu_model(A.UC_CPU_ARM_CORTEX_M4)
        uc.mem_map(0, 0x1000)
        uc.mem_write(0x100, bytes.fromhex("01300130fce7"))    # adds r0,#1; adds r0,#1; b .-4
        base = uc._uch.value
        cands = None
        for cnt in (1234, 777, 50001):
            uc.emu_start(0x101, _UNTIL, 0, cnt)
            arr = (ctypes.c_uint64 * 256).from_address(base)
            found = {i for i in range(255) if arr[i] == cnt + 1 and arr[i + 1] == cnt}
            cands = found if cands is None else cands & found
        if cands and len(cands) == 1:
            _COUNTER_OFF = 8 * cands.pop()
        else:
            _CAL_ERROR = f"{len(cands or ())} candidate counter locations"
    except Exception as e:  # pragma: no cover - unexpected Unicorn build / layout
        _COUNTER_OFF = None
        _CAL_ERROR = f"{type(e).__name__}: {e}"
    return _COUNTER_OFF


# FP availability (CPACR CP10/CP11, Cortex-M3 without FPU). Unicorn 2.1 executes VFP instructions on every M-profile
# model (its cortex-m3 carries the M4's MVFR0/MVFR1) and ignores CPACR. QEMU's Thumb decoder raises NOCP (intno 17)
# for a coprocessor instruction when the CPU's MVFR0 FP fields are zero, checked at translation time. So the core
# clears ARMCPU.isar.mvfr0/mvfr1 (located once by calibration: a pointer field of uc_struct whose target holds the
# M4's MVFR0/MVFR1 pair) while FP is unavailable and flushes the translation cache when that changes: no hook at all.
_FP_LOC: tuple[int, int] | None = None
_FP_CAL = False
_MVFR_M4 = (0x10110021, 0x11000011)


def _rw_maps() -> list[tuple[int, int]]:
    out = []
    try:
        with open("/proc/self/maps") as f:
            for line in f:
                parts = line.split()
                if len(parts) > 1 and parts[1].startswith("rw"):
                    a, b = parts[0].split("-")
                    out.append((int(a, 16), int(b, 16)))
    except OSError:
        pass
    return out


def _calibrate_fp() -> tuple[int, int] | None:
    global _FP_LOC, _FP_CAL
    if _FP_CAL:
        return _FP_LOC
    _FP_CAL = True
    try:
        span = 0x10000
        uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        uc.ctl_set_cpu_model(A.UC_CPU_ARM_CORTEX_M4)
        uc.mem_map(0, 0x1000)               # Unicorn creates the vCPU lazily, on first use
        maps = _rw_maps()                   # after that: the CPU may sit in a fresh mapping
        if not maps:
            return None
        base = uc._uch.value
        found = set()
        for off in range(0, 0x1000, 8):
            p = ctypes.c_uint64.from_address(base + off).value
            if not p or p % 4 or not any(a <= p and p + span <= b for a, b in maps):
                continue
            arr = (ctypes.c_uint32 * (span // 4)).from_address(p)
            for i in range(span // 4 - 1):
                if arr[i] == _MVFR_M4[0] and arr[i + 1] == _MVFR_M4[1]:
                    found.add((off, 4 * i, p + 4 * i))
        # the heap may hold other instances' CPUs too: keep the candidate whose clearing makes THIS instance trap
        uc.mem_write(0x100, bytes.fromhex("30ee200a00bffee7"))      # vadd.f32 s0, s0, s1; nop; b .
        hit: list = []
        uc.hook_add(UC_HOOK_INTR, lambda u, intno, ud: (hit.append(intno), u.emu_stop()))
        good = []
        for off, d, addr in sorted(found):
            mv = (ctypes.c_uint32 * 2).from_address(addr)
            mv[0] = mv[1] = 0
            uc.ctl_flush_tb()
            hit.clear()
            try:
                uc.emu_start(0x101, _UNTIL, 0, 1)
            except UcError:
                pass
            mv[0], mv[1] = _MVFR_M4
            uc.ctl_flush_tb()
            if hit == [17]:
                good.append((off, d))
        if good:
            _FP_LOC = good[0]
    except Exception:  # pragma: no cover - unexpected Unicorn build / layout
        _FP_LOC = None
    return _FP_LOC


class _RegBatch:
    """Pre-built ctypes arrays for uc_reg_read_batch / uc_reg_write_batch over a fixed list of 32-bit regs."""
    __slots__ = ("uc", "n", "ids", "vals", "ptrs")

    def __init__(self, uc: Uc, ids: Sequence[int]):
        self.uc = uc
        self.n = n = len(ids)
        self.ids = (ctypes.c_int * n)(*ids)
        self.vals = (ctypes.c_uint64 * n)()
        base = ctypes.addressof(self.vals)
        self.ptrs = (ctypes.c_void_p * n)(*[base + 8 * i for i in range(n)])

    def read(self) -> list[int]:
        ctypes.memset(self.vals, 0, 8 * self.n)
        err = _uclib.uc_reg_read_batch(self.uc._uch, self.ids, self.ptrs, self.n)
        if err:
            raise UcError(err)
        return [v & 0xFFFFFFFF for v in self.vals]

    def write(self, values: Sequence[int]) -> None:
        vals = self.vals
        for i, v in enumerate(values):
            vals[i] = v & 0xFFFFFFFF
        err = _uclib.uc_reg_write_batch(self.uc._uch, self.ids, self.ptrs, self.n)
        if err:
            raise UcError(err)


class UnicornCortexM(Core):
    page_size = 0x400
    calibration_warning = staticmethod(calibration_warning)

    def __init__(self, cpu: str = "cortex-m4", *, fpu: bool = True, precise: bool = False):
        cpu = cpu.lower()
        if cpu not in CPU_MODELS:
            raise ValueError(f"unsupported cpu {cpu!r} (supported: {', '.join(CPU_MODELS)})")
        self.cpu = cpu
        self.fpu = fpu
        self.uc = uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        uc.ctl_set_cpu_model(CPU_MODELS[cpu])
        self.page_size = uc.ctl_get_page_size()
        self.last_fault = None
        self.stop_info = None
        self.running = False
        self._stop: StopReason | None = None
        self._user_stop = False
        self._mmio_fix: int | None = None
        self._cut: int | None = None                # lowered instruction limit of the running slice (end_slice_at)
        self._fault_mem: tuple | None = None
        self._unmapped_handler: Callable[[int, int, str], bool] | None = None
        self._batches: dict[tuple, _RegBatch] = {}
        self._mmio_keep: list = []
        self._map_queue: list = []                 # mapping changes requested while running (applied after the run)
        self._in_unmapped = False                  # inside the unmapped-access hook: map now (the access retries)
        self._poll = None                          # PollWatch while poll skip is on (set_poll_watch)
        self._poll_stop = False
        self._max_insns = 0
        self.poll_skip: int | None = None
        self._safe_cache: dict[int, int] = {}
        self._cs = None
        # precise-mode counters
        self.precise = False
        self._block_hook = None
        self._cnt = 0
        self._cnt_before = 0
        self._blk = (0, 0)
        self._blk_cache: dict[tuple[int, int], int] = {}
        self._addr_cache: dict[tuple[int, int], tuple[int, ...]] = {}
        off = _calibrate_counter()
        self._counter = None
        if off is not None:
            try:
                self._counter = ctypes.c_uint64.from_address(uc._uch.value + off)
            except Exception:  # pragma: no cover
                self._counter = None
        self.exact_counts = self._counter is not None
        # FP availability gate (see _calibrate_fp): ARMCPU.isar.mvfr0/1 of this instance
        self._mvfr = None
        self._mvfr_on = (0, 0)
        self.fp_enabled = True
        loc = _calibrate_fp()
        if loc is not None:
            try:
                p = ctypes.c_uint64.from_address(uc._uch.value + loc[0]).value   # (ctl_get_page_size above
                maps = _rw_maps()                                                # initialised the vCPU)
                ok = any(a <= p + loc[1] and p + loc[1] + 8 <= b for a, b in maps)
                mv = (ctypes.c_uint32 * 2).from_address(p + loc[1]) if ok else None
                if mv is not None and mv[0] & 0xF0:                                # the model has single-precision FP fields
                    self._mvfr = mv
                    self._mvfr_on = (mv[0], mv[1]) if fpu else (0, 0)   # no FPU (Cortex-M3, M4 w/o F): never on
            except Exception:  # pragma: no cover
                self._mvfr = None
        self.fp_gate = self._mvfr is not None
        if self._mvfr is not None and not fpu:
            self._fp_now(False)
        # the emu_start count limit sits next to the counter (calibration found the pair counter, count): a
        # watchpoint lowers it to "this instruction" so the run ends right after the accessing instruction
        self._limit = None
        if self._counter is not None:
            try:
                self._limit = ctypes.c_uint64.from_address(uc._uch.value + off + 8)
            except Exception:  # pragma: no cover
                self._limit = None
        # debugger (chipem/debug.py): one UC_HOOK_CODE per breakpoint address and one ranged memory hook per
        # watchpoint; none at all while nothing is set
        self.debug_hook = None
        self.bp_skip = None
        self._bp_hooks: dict[int, int] = {}
        self._wp_hooks: list[int] = []
        self._wps: list = []
        # hooks deleted: Unicorn drops them at the end of the next emu_start, and blocks translated while a hook
        # existed keep its (slower) helper calls, so the code cache is flushed once more after that run
        self._wp_flush = False
        self._skip_once: int | None = None
        uc.hook_add(UC_HOOK_INTR, self._on_intr)
        uc.hook_add(UC_HOOK_MEM_UNMAPPED, self._on_unmapped)
        uc.hook_add(UC_HOOK_MEM_PROT, self._on_prot)
        self.set_precise(precise)

    # ------------------------------------------------------------------ configuration
    def set_precise(self, precise: bool) -> None:
        if precise and self._block_hook is None:
            self._block_hook = self.uc.hook_add(UC_HOOK_BLOCK, self._on_block)
        elif not precise and self._block_hook is not None:
            self.uc.hook_del(self._block_hook)
            self._block_hook = None
        self.precise = precise

    def _defer_map(self, fn, *args) -> bool:
        """While emu_start runs (an MMIO callback asks for a mapping change): queue fn(*args) for the end of the run
        and end the slice after the current instruction (see the module docstring). False: apply it now."""
        if not self.running or (self._in_unmapped and not self._map_queue):
            return False
        self._map_queue.append((fn, args))
        if not self.end_slice_at(0):
            self.request_stop(from_mmio=True)       # no calibrated counter: at the end of the slice at the latest
        return True

    def _apply_maps(self) -> None:
        q, self._map_queue = self._map_queue, []
        for fn, args in q:
            fn(*args)

    def set_fp_enabled(self, on: bool) -> bool:
        """CPACR CP10/CP11 (arch.armv7m SCS): with FP disabled (or no FPU) every VFP / coprocessor instruction raises
        NOCP (intno 17 -> UsageFault NOCP). From a callback the change applies after the accessing instruction.
        False if the gate is not available (calibration failed: FP instructions always execute)."""
        if self._mvfr is None:
            return False
        if self.running and self._defer_map(self._fp_now, bool(on)):
            return True
        self._fp_now(bool(on))
        return True

    def _fp_now(self, on: bool) -> None:
        mv = self._mvfr
        want = self._mvfr_on if on else (0, 0)
        self.fp_enabled = want != (0, 0)
        if (mv[0], mv[1]) != want:
            mv[0], mv[1] = want
            self.flush_code_cache()

    def map_memory(self, base: int, size: int, *, writable: bool = True, executable: bool = True) -> None:
        if self.running and self._defer_map(self._map_memory_now, base, size, writable, executable):
            return
        self._map_memory_now(base, size, writable, executable)

    def _map_memory_now(self, base: int, size: int, writable: bool, executable: bool) -> None:
        perms = UC_PROT_READ | (UC_PROT_WRITE if writable else 0) | (UC_PROT_EXEC if executable else 0)
        self.uc.mem_map(base, size, perms)

    def map_mmio(self, base: int, size: int, read: Callable[[int, int], int],
                 write: Callable[[int, int, int], None]) -> None:
        if self.running and self._defer_map(self._map_mmio_now, base, size, read, write):
            return
        self._map_mmio_now(base, size, read, write)

    def _map_mmio_now(self, base: int, size: int, read: Callable[[int, int], int],
                      write: Callable[[int, int, int], None]) -> None:
        core = self

        def rd(uc, off, sz, ud, _r=read, _b=base):
            v = _r(off, sz)
            if core._poll is not None and core._max_insns >= 128:   # poll skip (perf.py): watch MMIO reads
                core._poll_read(_b + off, v)                         # (short slices: never worth a skip)
            return v

        def wr(uc, off, sz, val, ud, _w=write):
            if core._poll is not None:
                core._poll.anchor = None          # a write: not a pure polling loop (the next read restarts)
            _w(off, sz, val)
        self._mmio_keep.append((rd, wr))
        self.uc.mmio_map(base, size, rd, None, wr, None)

    # ------------------------------------------------------------------ poll skip (Core.poll_skip, chipem/perf.py)
    _POLL_REGS = tuple(f"r{i}" for i in range(13)) + ("sp", "lr", "pc", "xpsr", "msp", "psp", "control", "primask",
                                                       "basepri", "faultmask")

    def set_poll_watch(self, on: bool) -> bool:
        from .base import PollWatch
        if on and (self._counter is None or self.precise):
            self._poll = None
            return False                          # needs the exact instruction counter
        self._poll = PollWatch() if on else None
        return True

    def _poll_read(self, a: int, v: int) -> None:
        """MMIO read callback while poll watching: two consecutive identical iterations (PollWatch) with identical
        registers -> stop in front of this load (emu_stop in a read callback aborts it; it runs again on resume) and
        report the period in `poll_skip`."""
        p = self._poll
        if not p.read(a, v) or not self.running or self._user_stop or self._stop is not None:
            return
        cnt = int(self._counter.value)
        if self._max_insns - cnt < p.MIN_GAIN:
            return                                # the slice ends soon anyway (next event): not worth a stop
        snap = tuple(self._batch(self._POLL_REGS).read())
        if snap == p.snap:
            d = cnt - p.cnt
            if 0 < d <= p.MAX_PERIOD:
                self.poll_skip = d
                self._poll_stop = True
                self._user_stop = True
                self.uc.emu_stop()
                p.cool = 0
                p.reset()
                return
        elif p.snap is not None:
            p.missed()                            # registers changed: not a pure loop (back off)
            return
        p.snap, p.cnt = snap, cnt

    def map_shared(self, base: int, size: int, ptr: int, *, writable: bool = True) -> None:
        if self.running and self._defer_map(self._map_shared_now, base, size, ptr, writable):
            return
        self._map_shared_now(base, size, ptr, writable)

    def _map_shared_now(self, base: int, size: int, ptr: int, writable: bool) -> None:
        perms = UC_PROT_READ | UC_PROT_EXEC | (UC_PROT_WRITE if writable else 0)
        self.uc.mem_map_ptr(base, size, perms, ptr)

    def unmap(self, base: int, size: int) -> None:
        if self.running and self._defer_map(self._unmap_now, base, size):
            return
        self._unmap_now(base, size)

    def _unmap_now(self, base: int, size: int) -> None:
        self.uc.mem_unmap(base, size)
        self._safe_cache.clear()

    def set_unmapped_handler(self, fn):
        self._unmapped_handler = fn

    def flush_code_cache(self) -> None:
        try:
            self.uc.ctl_flush_tb()
        except Exception:
            pass
        self._blk_cache.clear()
        self._addr_cache.clear()
        self._safe_cache.clear()

    # ------------------------------------------------------------------ registers
    def read_reg(self, name: str) -> int:
        return self.uc.reg_read(REGS[name]) & 0xFFFFFFFF

    def write_reg(self, name: str, value: int) -> None:
        self.uc.reg_write(REGS[name], value & 0xFFFFFFFF)

    def _batch(self, names: Sequence[str]) -> _RegBatch:
        key = tuple(names)
        b = self._batches.get(key)
        if b is None:
            b = self._batches[key] = _RegBatch(self.uc, [REGS[n] for n in key])
        return b

    def read_regs(self, names: Sequence[str]) -> list[int]:
        if _uclib is None:
            return [self.read_reg(n) for n in names]
        return self._batch(names).read()

    def write_regs(self, names: Sequence[str], values) -> None:
        """Writes happen in list order (relevant for sp/control/xpsr banking)."""
        if _uclib is None:
            for n, v in zip(names, values):
                self.write_reg(n, v)
            return
        self._batch(names).write(values)

    @property
    def pc(self) -> int:
        return self.uc.reg_read(A.UC_ARM_REG_PC)

    @pc.setter
    def pc(self, value: int) -> None:
        self.uc.reg_write(A.UC_ARM_REG_PC, value & 0xFFFFFFFE)

    @property
    def sp(self) -> int:
        return self.uc.reg_read(A.UC_ARM_REG_SP)

    @sp.setter
    def sp(self, value: int) -> None:
        self.uc.reg_write(A.UC_ARM_REG_SP, value)

    # ------------------------------------------------------------------ memory
    def read_mem(self, addr: int, n: int) -> bytes:
        return bytes(self.uc.mem_read(addr, n))

    def write_mem(self, addr: int, data: bytes) -> None:
        self.uc.mem_write(addr, bytes(data))

    # ------------------------------------------------------------------ reset / run
    def reset(self, pc: int, sp: int) -> None:
        names = ["xpsr", "control", "msp", "psp", "primask", "basepri", "faultmask"] + \
                [f"r{i}" for i in range(13)] + ["lr", "pc"]
        vals = [0x01000000, 0, sp & ~3, 0, 0, 0, 0] + [0] * 13 + [0xFFFFFFFF, pc & ~1]
        self.write_regs(names, vals)
        if self.fpu:
            self.write_regs(["fpscr"] + [f"s{i}" for i in range(32)], [0] * 33)
        self.last_fault = None
        self.stop_info = None
        self.bp_skip = self._skip_once = None

    def run(self, max_insns: int) -> tuple[StopReason, int]:
        uc = self.uc
        self._stop = None
        self._user_stop = False
        self._mmio_fix = None
        self._cut = None
        self._fault_mem = None
        precise = self.precise
        if precise:
            self._cnt = self._cnt_before = 0
            self._blk = (0, 0)
        self.poll_skip = None
        self._poll_stop = False
        self._max_insns = max_insns
        if self._poll is not None:
            self._poll.reset()
        start = uc.reg_read(A.UC_ARM_REG_PC)
        if self.bp_skip is not None and self.bp_skip == start:
            self._skip_once, self.bp_skip = start, None     # resume from a breakpoint: execute it once
        self.running = True
        err: UcError | None = None
        try:
            uc.emu_start(start | 1, _UNTIL, 0, max_insns)
        except UcError as e:
            err = e
        finally:
            self.running = False
            if self._map_queue:          # mapping changes requested by callbacks during the run
                self._apply_maps()
            if self._wp_flush:           # deleted watch hooks are gone now: retranslate without their helpers
                self._wp_flush = False
                self.flush_code_cache()
        if self._cut is not None:        # end_slice_at lowered the budget of this slice
            max_insns = self._cut

        if err is not None:
            if getattr(err, "errno", None) == UCC.UC_ERR_INSN_INVALID and self._is_wfe_fault():
                # Unicorn 2.1 raises WFE as an invalid instruction with PC already past it: it is a halt like WFI
                n = self._executed(max_insns, early=True, fault=True, err=err)
                self.last_fault = None
                self.stop_info = "wfe"
                return StopReason.WFI, min(n if self.precise else n + 1, max_insns)
            return StopReason.FAULT, self._executed(max_insns, early=True, fault=True, err=err)
        reason = self._stop
        if reason is not None:
            if reason is StopReason.DEBUG:
                self._skip_once = None
                if self.stop_info.get("type") == "breakpoint":
                    self.bp_skip = self.stop_info["addr"]   # a resume from here executes it (steps over it)
                if self._counter is not None and not self.precise:
                    # breakpoint: the counter counted the instruction it stopped in front of; watchpoint: the
                    # count limit stopped the run in front of the next instruction, also counted
                    return reason, max(0, min(int(self._counter.value) - 1, max_insns))
            return reason, self._executed(max_insns, early=True)
        if self._user_stop:
            if self._mmio_fix is not None:
                pc = uc.reg_read(A.UC_ARM_REG_PC)
                if pc == self._mmio_fix:
                    uc.reg_write(A.UC_ARM_REG_PC, pc + self._safe_cache.get(pc, 2))
            if self._poll_stop:                   # stopped in front of the polling load: it was counted, not run
                return StopReason.USER, max(0, self._executed(max_insns, early=True) - 1)
            return StopReason.USER, self._executed(max_insns, early=True)
        # Did the slice run to its full budget?  Unicorn's count hook increments the counter before each
        # instruction and stops once it exceeds the budget, so a count-stop leaves counter == max_insns + 1,
        # while any early stop (wfi/wfe halt) leaves counter <= max_insns.  Only an early stop can be a WFI:
        # the halfword test alone misfires when a slice ends right after a 32-bit instruction whose second
        # halfword happens to be 0xBF30/0xBF20 (seen in real firmware: the trailing half of a `b.w`).
        if self._counter is not None:
            if int(self._counter.value) > max_insns:
                return StopReason.COUNT, max_insns
        elif self.precise and self._cnt >= max_insns:
            return StopReason.COUNT, max_insns
        pc = uc.reg_read(A.UC_ARM_REG_PC)
        try:
            hw = int.from_bytes(uc.mem_read(pc - 2, 2), "little")
        except UcError:
            hw = 0
        if hw in (0xBF30, 0xBF20):        # wfi / wfe just executed
            if self._counter is None and not self.precise:
                # No exact count available: at least reject the case where PC-2 is the second half of a
                # 32-bit Thumb instruction (first halfword has top bits 11101/11110/11111).
                try:
                    prev = int.from_bytes(uc.mem_read(pc - 4, 2), "little")
                except UcError:
                    prev = 0
                if (prev >> 11) in (0b11101, 0b11110, 0b11111):
                    return StopReason.COUNT, max_insns
            self.stop_info = "wfe" if hw == 0xBF20 else "wfi"
            return StopReason.WFI, self._executed(max_insns, early=True)
        return StopReason.COUNT, max_insns

    def _is_wfe_fault(self) -> bool:
        try:
            pc = self.uc.reg_read(A.UC_ARM_REG_PC)
            return int.from_bytes(self.uc.mem_read(pc - 2, 2), "little") == 0xBF20
        except UcError:
            return False

    def _executed(self, max_insns: int, *, early: bool, fault: bool = False, err: UcError | None = None) -> int:
        if fault:
            self._record_fault(err)
        if not self.precise:
            if self._counter is not None:
                # counter = instructions whose (internal) count hook ran; a faulting one did not complete
                n = self._counter.value - (1 if fault else 0)
                return max(0, min(n, max_insns))
            # unknown: charge half the budget (the Chip keeps slices short in handler mode)
            return max(1, max_insns // 2) if early else max_insns
        total = self._cnt
        a, size = self._blk
        pc = self.uc.reg_read(A.UC_ARM_REG_PC)
        if size and a <= pc < a + size:
            addrs = self._addr_cache.get(self._blk)
            if addrs is not None:
                total = self._cnt_before + sum(1 for x in addrs if x < pc)
        return min(total, max_insns)

    # ------------------------------------------------------------------ stops
    def end_slice_at(self, n: int) -> bool:
        """Lower the running slice's budget to `n` instructions from its start (at least the instruction executing
        now completes): the run then ends with StopReason.COUNT (or USER after request_stop) and n executed. Called
        from MMIO callbacks (an event scheduled inside the slice, chip.Chip._early_event). Costs nothing per
        instruction: it rewrites the limit Unicorn's count hook already compares against. False if the core cannot
        (no calibrated counter, not running)."""
        lim = self._limit
        if lim is None or not self.running:
            return False
        c = int(self._counter.value)              # includes the instruction executing now
        if n < c:
            n = c
        cur = self._cut if self._cut is not None else self._max_insns
        if n < cur:
            lim.value = n
            self._cut = n
            self._max_insns = n
        return True

    def request_stop(self, from_mmio: bool = False) -> bool:
        if not self.running:
            return False
        if from_mmio and self._limit is not None:   # end after the accessing instruction (see the module docstring)
            self.end_slice_at(0)
            self._user_stop = True
            return True
        if from_mmio:
            pc = self.uc.reg_read(A.UC_ARM_REG_PC)
            n = self._store_len(pc)
            if not n:
                return False
            if self.uc.reg_read(A.UC_ARM_REG_EPSR) & 0x0600FC00:   # inside an IT block: can't step safely
                return False
            self._mmio_fix = pc
        self._user_stop = True
        self.uc.emu_stop()
        return True

    def _store_len(self, pc: int) -> int:
        """Length of the instruction at pc if it is a plain single-register store without writeback, else 0."""
        n = self._safe_cache.get(pc)
        if n is not None:
            return n
        if self._cs is None:
            from capstone import CS_ARCH_ARM, CS_MODE_MCLASS, CS_MODE_THUMB, Cs
            self._cs = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
            self._cs.detail = True
        n = 0
        try:
            insn = next(self._cs.disasm(bytes(self.uc.mem_read(pc, 4)), pc, 1), None)
            if insn is not None and insn.mnemonic.split(".")[0] in ("str", "strb", "strh") and not insn.writeback:
                n = insn.size
        except Exception:
            n = 0
        self._safe_cache[pc] = n
        return n

    def insns_in_slice(self) -> int:
        if not self.running:
            return 0
        if self._counter is not None:
            return max(int(self._counter.value) - 1, 0)
        return self._cnt if self.precise else 0

    # ------------------------------------------------------------------ debugger (chipem/debug.py)
    def set_breakpoints(self, addrs) -> None:
        """One UC_HOOK_CODE hook per address (bit 0 ignored); the translated blocks of changed addresses are
        dropped so the hook takes effect (or disappears) at once."""
        uc = self.uc
        new = {int(a) & ~1 for a in addrs}
        old = self._bp_hooks
        for a in [a for a in old if a not in new]:
            uc.hook_del(old.pop(a))
            self._drop_tb(a)
            self._wp_flush = True
        for a in new - set(old):
            old[a] = uc.hook_add(UC_HOOK_CODE, self._on_bp, None, a, a)
            self._drop_tb(a)

    def _drop_tb(self, a: int) -> None:
        try:
            self.uc.ctl_remove_cache(a, a + 4)
        except Exception:
            self.flush_code_cache()

    def set_watchpoints(self, wps) -> None:
        """Ranged UC_HOOK_MEM_READ / UC_HOOK_MEM_WRITE hooks (they also see MMIO accesses)."""
        uc = self.uc
        wps = [(int(lo), int(hi), str(acc)) for lo, hi, acc in wps]
        if wps == self._wps:
            return
        self._wps = wps
        for h in self._wp_hooks:
            uc.hook_del(h)
            self._wp_flush = True        # blocks translated until then still call the memory-hook helpers
        self._wp_hooks = []
        for lo, hi, acc in wps:
            kind = (UC_HOOK_MEM_READ if "r" in acc else 0) | (UC_HOOK_MEM_WRITE if "w" in acc else 0)
            if kind and hi > lo:
                self._wp_hooks.append(uc.hook_add(kind, self._on_watch, None, int(lo), int(hi) - 1))
        self.flush_code_cache()

    def _on_bp(self, uc, addr, size, ud):
        if addr == self._skip_once:
            self._skip_once = None
            return
        h = self.debug_hook
        if h is not None and not h("break", {"addr": addr}):
            return
        self._stop = StopReason.DEBUG
        self.stop_info = {"type": "breakpoint", "addr": addr}
        uc.emu_stop()

    def _on_watch(self, uc, access, addr, size, value, ud):
        acc = "w" if access == UC_MEM_WRITE else "r"
        info = {"addr": addr, "size": size, "access": acc, "value": value if acc == "w" else None,
                "pc": uc.reg_read(A.UC_ARM_REG_PC)}
        h = self.debug_hook
        if h is not None and not h("watch", info):
            return
        self._stop = StopReason.DEBUG
        self.stop_info = {"type": "watchpoint", **info}
        lim, cnt = self._limit, self._counter
        if lim is not None and cnt is not None:
            lim.value = cnt.value             # end the run in front of the next instruction (this one completes)
        else:                                 # pragma: no cover - no counter: stop now (the access is repeated)
            uc.emu_stop()

    # ------------------------------------------------------------------ hooks
    def _on_intr(self, uc, intno, ud):
        if intno == 8:
            self._stop = StopReason.EXC_RETURN
            self.stop_info = uc.reg_read(A.UC_ARM_REG_PC) | 1
        elif intno == 2:
            self._stop = StopReason.SVC
        elif intno == 7:
            self._stop = StopReason.BREAKPOINT
        elif intno == 3 and self._stop is not None:
            pass                                    # spurious follow-up of an EXC_RETURN
        else:
            self._stop = StopReason.FAULT
            pc = uc.reg_read(A.UC_ARM_REG_PC)
            self.last_fault = {"type": _INTNO_NAMES.get(intno, f"exception_{intno}"), "intno": intno, "pc": pc,
                               "addr": None}
        uc.emu_stop()

    def _on_unmapped(self, uc, access, addr, size, value, ud):
        acc, kind = _ACCESS.get(access, ("?", "unmapped"))
        h = self._unmapped_handler
        if h is not None and acc != "x":
            self._in_unmapped = True              # an auto stub is mapped at once: the access is retried
            try:
                if h(addr, size, acc):
                    return True
            except Exception:
                pass
            finally:
                self._in_unmapped = False
        self._fault_mem = (kind, acc, addr, size, value)
        return False

    def _on_prot(self, uc, access, addr, size, value, ud):
        acc, kind = _ACCESS.get(access, ("?", "prot"))
        self._fault_mem = (kind, acc, addr, size, value)
        return False

    def _record_fault(self, err: UcError | None) -> None:
        pc = self.uc.reg_read(A.UC_ARM_REG_PC)
        if self._fault_mem is not None:
            kind, acc, addr, size, value = self._fault_mem
            self.last_fault = {"type": kind, "pc": pc, "addr": addr, "size": size, "access": acc,
                               "value": value if acc == "w" else None, "intno": None, "error": str(err)}
        else:
            errno = getattr(err, "errno", None)
            kind = "undefined_instruction" if errno == UCC.UC_ERR_INSN_INVALID else "error"
            self.last_fault = {"type": kind, "pc": pc, "addr": None, "intno": None, "error": str(err)}

    def _on_block(self, uc, addr, size, ud):
        key = (addr, size)
        n = self._blk_cache.get(key)
        if n is None:
            n = self._count_block(addr, size)
        self._cnt_before = self._cnt
        self._cnt += n
        self._blk = key

    def _count_block(self, addr: int, size: int) -> int:
        try:
            code = self.uc.mem_read(addr, size)
        except UcError:
            return 1
        addrs = []
        i = 0
        while i < size:
            addrs.append(addr + i)
            hw = code[i] | (code[i + 1] << 8) if i + 1 < size else 0
            i += 4 if (hw & 0xF800) in (0xE800, 0xF000, 0xF800) else 2
        key = (addr, size)
        self._addr_cache[key] = tuple(addrs)
        self._blk_cache[key] = len(addrs)
        return len(addrs)
