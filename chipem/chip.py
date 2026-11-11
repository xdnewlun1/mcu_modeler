"""Chip runtime: assembles clock, scheduler, pins, event log, bus, core, arch and peripherals from a
ChipDescription and runs firmware in slices.

Slice loop (run_cycles / run_for):
  fire due scheduler events → arch.service() (exception entry between slices) → core.run(slice) where
  slice = min(cycles to next event, max_slice, remaining); then by stop reason:
  COUNT → advance time; WFI → sleep, i.e. jump to the next event (or the end of the window) until an exception
  can wake the core; EXC_RETURN/SVC → arch.on_stop; FAULT → HardFault entry if possible, else log and return;
  USER → reschedule (MMIO-requested) or return (chip.stop()).
Instruction counts for early stops come from Unicorn's internal counter (located once by calibration in
core/unicorn_cm.py), so fast mode is exact; only if that calibration fails (logged once as a `warn`) does the core
charge half the slice for an early stop, and slices are then limited to `handler_slice` while an exception is
active to keep the error small.
"""
from __future__ import annotations

import importlib
import math
import threading
from typing import Any

from .bus import DEFAULT_CATCH_ALL, Bus, MemoryRegion
from .chipdesc import ChipDescription
from .core import StopReason, create_core
from .periph import registry
from .time import Clock, Scheduler

ARMV7M_ARCHES = ("armv7e-m", "armv7-m", "armv6-m", "armv7m", "armv6m")
C28X_ARCHES = ("c28x",)
MCS51_ARCHES = ("mcs51", "8051")
ARCH_OWNED_MODELS = ("armv7m.scs", "ti_c2000.pie", "mcs51.cpu", "mcs51.intc")   # MMIO blocks the arch creates itself


def _make_core(cd, precise: bool):
    """Core backend for the chip: Unicorn (ARM) via create_core, or a pure-Python interpreter package
    chipem.core.<backend> (c28x, mcs51; imported lazily so ARM chips never need it)."""
    if cd.backend in ("c28x", "mcs51"):
        try:
            mod = importlib.import_module(f"chipem.core.{cd.backend}")
        except ImportError as e:
            raise ValueError(f"chip needs the {cd.backend} core backend (chipem.core.{cd.backend}), which is not "
                             f"available: {e}") from e
        factory = getattr(mod, "create_core", None)
        if factory is not None:
            return factory(cd.cpu, fpu=cd.fpu, precise=precise)
        cls = getattr(mod, "C28xCore", None)
        if cls is None:
            raise ValueError("chipem.core.c28x provides neither create_core() nor C28xCore")
        return cls(cd.cpu, fpu=cd.fpu, precise=precise)
    return create_core(cd.backend, cd.cpu, fpu=cd.fpu, precise=precise)


def _catch_all(desc: ChipDescription):
    """Auto-stub windows: `extra.auto_stub_ranges: [[lo, hi], ...]` (inclusive bounds, chip address units), else the
    Kinetis/ARM defaults (peripheral space and the private peripheral bus)."""
    rng = desc.extra.get("auto_stub_ranges")
    if rng is None:
        return DEFAULT_CATCH_ALL
    out = []
    for lo, hi in rng:
        lo = int(lo, 0) if isinstance(lo, str) else int(lo)
        hi = int(hi, 0) if isinstance(hi, str) else int(hi)
        out.append((lo, hi + 1))
    return tuple(out)


class _FallbackLog:
    def __init__(self):
        self.events: list = []

    def event(self, kind, t_ns, **fields):
        self.events.append((kind, t_ns, fields))


def _make_log():
    try:
        from .log import EventLog
        return EventLog()
    except Exception:          # pragma: no cover - log module missing
        return _FallbackLog()


def _make_pins(desc: ChipDescription, clock: Clock):
    try:
        from .pins import PinBoard
    except Exception:          # pragma: no cover
        return None
    if hasattr(PinBoard, "from_desc"):
        return PinBoard.from_desc(desc, clock)
    return PinBoard([], clock)


class Chip:
    def __init__(self, desc: ChipDescription, *, clock_hz: float | None = None, log: Any = None,
                 precise: bool = False, max_slice: int = 2000, handler_slice: int = 256, vtor: int | None = None,
                 bitband: bool | None = None):
        self.desc = desc
        self.log = log if log is not None else _make_log()
        self.events = getattr(self.log, "events", self.log)
        self.clock = Clock(clock_hz or desc.core.clock_hz)
        self.sched = Scheduler(self.clock)
        self.pins = _make_pins(desc, self.clock)
        cd = desc.core
        self.unit_bits = int(cd.addr_unit_bits)
        self.core = _make_core(cd, precise)
        warn = getattr(self.core, "calibration_warning", None)
        msg = warn() if warn is not None else None
        if msg:
            self.log.event("warn", 0.0, msg=msg)
        if bitband is None:
            bitband = bool(desc.extra.get("bitband", cd.cpu.lower() in ("cortex-m3", "cortex-m4", "cortex-m4f")))
        self.bus = Bus(self.core, addr_unit_bits=cd.addr_unit_bits, log=self.log, catch_all=_catch_all(desc))
        self.bus.chip = self
        self.max_slice = max_slice * int(getattr(self.core, "slice_scale", 1))   # cycle-counted cores: periods
        self.handler_slice = handler_slice
        self.masked_slice = 1
        self._masked_runs = 0
        self.halt_on_fault = False
        self.lockup_reset = bool(desc.extra.get("lockup_reset", False))   # chip YAML: a core LOCKUP resets the chip
        self.firmware: Any = None
        self.sleeping = False
        # low-power hooks (vendor-free; see CONTRACTS.md "Low-power modes"): objects with optional
        # on_sleep(chip) / on_wake(chip), called when the core stops in WFI/WFE/IDLE and when it resumes
        self.power_hooks: list = []
        self.wake_gate = None          # callable() -> bool: False keeps pending interrupts from ending the sleep
        self._wake_req = False         # chip.wake(): a non-interrupt wake-up source (C2000 GPIO, reset-type wakes)
        self.idle_watch = None         # callable(chip): an event was scheduled on this chip while it is not running
        self.poll_skip_ns = 0.0        # perf.py poll skip: max time skipped at once in a detected polling loop (0: off)
        self.poll_skipped = 0          # instructions (cycles) skipped that way
        self.parked = False            # the last slice ended in a self-branch / skipped polling loop (auto quantum)
        self.insns = 0                 # instructions executed since construction (cumulative, survives resets)
        self.last_fault: dict | None = None
        self._stop_flag = False
        self._was_reset = False
        self._lock = threading.RLock()
        self.sched.on_early = self._early_event
        self._end_slice_at = getattr(self.core, "end_slice_at", None)   # Unicorn: cut the slice at the event exactly
        self._run_thread: int | None = None      # thread running _run (stop() from it: a stop from a callback)
        self.sched.slice_offset = self.core.insns_in_slice     # precise "now" for in_ns/in_cycles mid-slice
        self.clock.slice_offset = self.core.insns_in_slice     # set_hz re-bases at the current instruction
        # a clock change inside a slice also moves the run window's end (target cycles): end the slice there
        self.clock.on_change(self._clock_changed)
        if self.pins is not None:
            self.pins.time_fn = self.now_ns
            self.pins.sched = self.sched            # realtime waveform sources (PinBoard.drive_wave)

        # memory
        for m in desc.memory:
            writable = True if m.writable is None else bool(m.writable)   # flash writable for now (no FTFE yet)
            fill = m.fill if m.kind in ("flash", "flexnvm", "rom") else 0
            self.bus.map_memory(m.name, m.base, m.size, m.kind, writable=writable, fill=fill)

        # architecture
        arch = cd.arch.lower()
        num_irqs = max([cd.num_irqs] + [n + 1 for n in desc.irqs])
        self.arch_kind = "c28x" if arch in C28X_ARCHES else ("mcs51" if arch in MCS51_ARCHES else "armv7m")
        if arch in ARMV7M_ARCHES:
            from .arch.armv7m import ArchFault, ArmV7M
            rv = vtor if vtor is not None else int(desc.extra.get("vtor", 0))
            self.arch = ArmV7M(self, fpu=cd.fpu, num_irqs=num_irqs, variant=arch, cpu=cd.cpu.lower(), reset_vtor=rv)
            self._arch_fault = ArchFault
        elif arch in C28X_ARCHES:
            from .arch.c28x import C28xArch, C28xFault
            self.arch = C28xArch(self)
            self._arch_fault = C28xFault
        elif arch in MCS51_ARCHES:
            from .arch.mcs51 import MCS51Arch, MCS51Fault
            self.arch = MCS51Arch(self)
            self._arch_fault = MCS51Fault
        else:
            raise ValueError(f"unsupported arch {cd.arch!r}")
        self.peripherals: dict[str, Any] = {}
        self.arch_blocks = list(getattr(self.arch, "blocks", None) or [self.arch.scs])
        for blk in self.arch_blocks:
            self.bus.map_mmio(blk)
            self.peripherals[blk.name] = blk

        if desc.family:                  # the family package registers its models (chipem.periph.<family>)
            try:
                importlib.import_module(f"chipem.periph.{desc.family}")
            except ImportError:
                pass
        # peripherals (models resolve their register tables from the chip YAML's `regs_file:`, relative to it)
        regs_file = self._regs_file(desc)
        for p in desc.peripherals:
            if p.model in ARCH_OWNED_MODELS:
                continue
            params = dict(p.params)
            if regs_file is not None:
                params.setdefault("regs_file", regs_file)
            if self.unit_bits != 8:
                params.setdefault("unit_bits", self.unit_bits)
            blk = registry.create(p.model, p.name, p.base, p.size, params, self)
            self.bus.map_mmio(blk)
            self.peripherals[p.name] = blk
        for blk in self.peripherals.values():
            blk.attach(self)
        for blk in self.peripherals.values():
            blk.reset()
        if bitband:
            self.bus.enable_bitband()

    @staticmethod
    def _regs_file(desc: ChipDescription) -> str | None:
        rf = getattr(desc, "regs_file", None)
        if not rf:
            return None
        from pathlib import Path
        path = Path(rf)
        src = getattr(desc, "source_path", None)
        if not path.is_absolute() and src is not None:
            path = Path(src).parent / path
        return str(path) if path.exists() else None

    # ------------------------------------------------------------------ precise time
    def now_cycles(self) -> int:
        """Core cycles including the instructions already executed in the running slice (MMIO callbacks see
        the time of the accessing instruction, not of the slice start)."""
        return self.clock.cycles + self.core.insns_in_slice()

    def now_ns(self) -> float:
        return self.clock.cycles_to_ns(self.clock.cycles + self.core.insns_in_slice())

    # ------------------------------------------------------------------ firmware / reset
    def load_firmware(self, image: Any) -> None:
        ub = self.bus.unit_bytes
        for addr, data in image.segments:
            if ub != 1 and len(data) % ub:
                data = bytes(data) + b"\0" * (ub - len(data) % ub)
            pos, end = addr, addr + len(data) // ub
            while pos < end:
                r = self.bus.lookup(pos)
                if not isinstance(r, MemoryRegion):
                    if ub != 1 and r is not None and getattr(r, "ram_like", False):
                        # 16-bit chips: initialised data for RAM-like MMIO (e.g. the C28x PIE vector table)
                        n = min(end, r.base + r.size) - pos
                        self.bus.write_bytes(pos, data[(pos - addr) * ub:(pos - addr + n) * ub])
                        pos += n
                        continue
                    unit = "byte" if ub == 1 else "word"
                    raise ValueError(f"firmware segment {unit} 0x{pos:08x} is outside mapped memory")
                n = min(end, r.end) - pos
                self.core.write_mem(pos, data[(pos - addr) * ub:(pos - addr + n) * ub])
                pos += n
        self.core.flush_code_cache()
        if self.arch_kind != "armv7m":
            try:
                image.thumb = False         # byte-addressed code without a Thumb bit (symbols.Symbols.for_image)
            except AttributeError:
                pass
        if self.arch_kind == "mcs51" and hasattr(self.arch, "map_symbols"):
            self.arch.map_symbols(image)        # SDCC __xdata symbols -> on-chip XRAM tag (docs/MCS51_CORE.md)
        self.firmware = image
        v = getattr(image, "vtor", None)
        if v is not None and hasattr(self.arch, "reset_vtor"):
            self.arch.reset_vtor = v
        self._was_reset = False

    def _read_word(self, addr: int) -> int | None:
        try:
            return int.from_bytes(self.core.read_mem(addr, 4), "little")
        except Exception:
            return None

    def _vectors_ok(self, vtor: int) -> bool:
        sp, pc = self._read_word(vtor), self._read_word(vtor + 4)
        if sp is None or pc is None or sp in (0, 0xFFFFFFFF) or pc in (0, 0xFFFFFFFF):
            return False
        return isinstance(self.bus.lookup(pc & ~1), MemoryRegion)

    def reset(self, reason: str = "reset") -> None:
        if self.arch_kind != "armv7m":           # C28x, MCS-51: the arch sets the reset PC itself
            return self._reset_c28x(reason)
        with self._lock:
            arch = self.arch
            if not self._vectors_ok(arch.reset_vtor) and self.firmware is not None:
                start = self.firmware.start
                if start % 128 == 0 and self._vectors_ok(start):
                    self.log.event("info", self.clock.now_ns(), msg=f"no vector table at VTOR=0x{arch.reset_vtor:x}; "
                                   f"using image start 0x{start:x}")
                    arch.reset_vtor = start
            self.log.event("reset", self.clock.now_ns(), reason=reason)
            for blk in self.peripherals.values():
                if blk not in self.arch_blocks:
                    blk.reset()
            arch.reset()
            if not self._vectors_ok(arch.vtor) and self.firmware is not None and self.firmware.entry is not None:
                ram = [r for r in self.bus.memory_regions() if r.kind == "ram"]
                sp = max(r.end for r in ram) if ram else 0
                self.core.reset(self.firmware.entry & ~1, sp)
            self.sleeping = False
            self._wake_req = False
            self.last_fault = None
            self._was_reset = True

    def _reset_c28x(self, reason: str) -> None:
        """C28x reset: peripherals, then the arch (PIE, CPU registers, PC = image entry or the boot entry)."""
        with self._lock:
            self.log.event("reset", self.clock.now_ns(), reason=reason)
            for blk in self.peripherals.values():
                if blk not in self.arch_blocks:
                    blk.reset()
            entry = getattr(self.firmware, "entry", None) if self.firmware is not None else None
            self.arch.reset(entry)
            self.sleeping = False
            self._wake_req = False
            self.last_fault = None
            self._was_reset = True

    # ------------------------------------------------------------------ running
    def stop(self) -> None:
        """Ask a running run_for/run_cycles (possibly in another thread) to return StopReason.USER."""
        self._stop_flag = True
        # from this chip's own callbacks (MMIO hooks, UART listeners): end after the current instruction, which must
        # not be aborted half way (an emu_stop in a write callback would run the store again on resume)
        self.core.request_stop(from_mmio=self._run_thread == threading.get_ident())

    def run_for(self, ns: float) -> StopReason:
        target_ns = self.clock.now_ns() + ns
        return self._run(self.clock.cycles + self.clock.ns_to_cycles(ns), target_ns)

    def run_cycles(self, cycles: int) -> StopReason:
        return self._run(self.clock.cycles + int(cycles), None)

    def run_until(self, cond, max_ns: float = 1e9, poll_ns: float = 1e4) -> bool:
        """Run in poll_ns steps until cond(chip) is true or max_ns elapsed. Returns cond's final value."""
        end = self.clock.now_ns() + max_ns
        while not cond(self):
            if self.clock.now_ns() >= end:
                return False
            r = self.run_for(min(poll_ns, end - self.clock.now_ns()))
            if r not in (StopReason.COUNT,):
                return bool(cond(self))
        return True

    def _run(self, target: int, target_ns: float | None) -> StopReason:
        with self._lock:
            if not self._was_reset:
                self.reset()
            clock, sched, core, arch = self.clock, self.sched, self.core, self.arch
            cyc_core = getattr(core, "cycle_counted", False)     # run() returns clock periods (MCS-51), not insns
            self._stop_flag = False
            self._run_thread = threading.get_ident()
            pb = self.pins
            if pb is not None and not hasattr(pb, "_waves"):
                pb = None
            hz = clock.hz
            ArchFault = self._arch_fault
            while True:
                if self._stop_flag:
                    self._stop_flag = False
                    return StopReason.USER
                if arch.reset_requested:
                    self.reset(arch.reset_requested)
                if sched._heap and sched._heap[0].cycles <= clock.cycles:
                    sched.run_due()
                    if arch.reset_requested:          # an event requested a reset (watchdog): before any more code
                        self.reset(arch.reset_requested)
                if target_ns is not None and clock.hz != hz:      # core clock changed: keep the ns window
                    hz = clock.hz
                    target = clock.cycles + clock.ns_to_cycles(target_ns - clock.now_ns())
                now = clock.cycles
                if now >= target:
                    return StopReason.COUNT
                nd = sched.next_due()
                if self.sleeping:
                    if self._wake_req or (arch.wakeup_pending()
                                          and (self.wake_gate is None or self.wake_gate())):
                        self._end_sleep()
                    else:
                        nd = sched.next_due()            # a wake gate may have scheduled an event (oscillator restart)
                        to = target if nd is None else min(nd, target)
                        clock.advance(max(to - now, 0))
                        if pb is not None and pb._waves:
                            pb.sync_waves()
                        continue
                if arch.lockup:
                    if self.lockup_reset and not self.halt_on_fault:   # parts whose reset controller resets on core LOCKUP (Kinetis)
                        self._log_fault({"type": "lockup", "pc": core.pc, "msg": "core lockup → chip reset"},
                                        handled=False)
                        self.reset("lockup")
                        continue
                    return self._fault_stop({"type": "lockup", "pc": core.pc})
                try:
                    masked = False
                    if arch.pending:
                        masked = not arch.service() and arch.best_pending() is not None
                    n = target - now
                    if nd is not None and nd - now < n:
                        n = max(nd - now, 1)
                    if n > self.max_slice and not ((sp := getattr(core, "spin_pc", None)) is not None
                                                   and sp == core.pc):
                        # (a core parked in a branch-to-itself runs to the next event: nothing can change before it)
                        n = self.max_slice
                    if arch.active and not getattr(core, "exact_counts", core.precise) and n > self.handler_slice:
                        n = self.handler_slice
                    if masked:
                        # an enabled IRQ waits behind PRIMASK/BASEPRI: nothing stops the core when the firmware
                        # unmasks (CPSIE/MSR), so run very short slices to take it right after the unmask;
                        # back off exponentially if the mask stays up for long (long critical sections)
                        self._masked_runs += 1
                        lim = self.masked_slice << min(self._masked_runs // 16, 11)
                        if n > lim:
                            n = lim
                    else:
                        self._masked_runs = 0
                    sched.horizon = now + n
                    try:
                        r, ex = core.run(n)
                    finally:
                        sched.horizon = math.inf
                    clock.advance(ex)
                    self.insns += core.last_insns if cyc_core else ex
                    if pb is not None and pb._waves:
                        pb.sync_waves()              # waveform-source edges (UART frames) passed in this slice
                    if r is StopReason.COUNT or r is StopReason.USER:
                        # parked: the core sits in a loop whose state repeats until an event (board auto quantum)
                        self.parked = r is StopReason.COUNT and getattr(core, "spin_pc", None) is not None
                        if r is StopReason.USER and getattr(core, "poll_skip", None):
                            self._skip_poll(target)
                        continue
                    self.parked = False
                    if r is StopReason.EXC_RETURN or r is StopReason.SVC:
                        if not arch.on_stop(r, core.stop_info):
                            return self._fault_stop({"type": "lockup", "pc": core.pc,
                                                     "msg": f"{r.value} could not be taken"})
                        if r is StopReason.EXC_RETURN and not arch.active and arch.sleep_on_exit():
                            self._start_sleep()          # SCR.SLEEPONEXIT: back to sleep without running thread code
                        continue
                    if r is StopReason.WFI:
                        self._start_sleep()
                        continue
                    if r is StopReason.FAULT:
                        info = dict(core.last_fault or {})
                        self.last_fault = info
                        entered = arch.on_stop(r, info)
                        self._log_fault(info, handled=entered)
                        if self.halt_on_fault:
                            return StopReason.FAULT
                        if not entered:
                            if arch.lockup and self.lockup_reset:
                                continue                     # the top of the loop resets the chip ("lockup")
                            return StopReason.FAULT
                        continue
                    if r is StopReason.BREAKPOINT or r is StopReason.DEBUG:
                        return r                     # bkpt instruction / debugger breakpoint or watchpoint
                except ArchFault as e:
                    if arch.lockup and self.lockup_reset and not self.halt_on_fault:
                        self._log_fault({"type": "exception_model", "pc": core.pc, "msg": str(e)}, handled=False)
                        continue                         # the lockup branch resets the chip
                    return self._fault_stop({"type": "exception_model", "pc": core.pc, "msg": str(e)})

    # ------------------------------------------------------------------ low-power modes (generic part)
    def _start_sleep(self) -> None:
        """The core stopped in WFI/WFE/IDLE (or SLEEPONEXIT). Without power hooks this is plain sleep: skip to the
        next event until an interrupt can wake the core. Hooks (family power controllers) decide the mode, stop
        clocks, and may call wake() at once (a level wake-up source already active) or request a reset."""
        hooks = self.power_hooks
        if not hooks:
            if not self.arch.wakeup_pending():
                self.sleeping = True
            return
        self._wake_req = False
        self.sleeping = True
        for h in list(hooks):
            fn = getattr(h, "on_sleep", None)
            if fn is not None:
                fn(self)

    def _end_sleep(self) -> None:
        self.sleeping = False
        self._wake_req = False
        for h in list(self.power_hooks):
            fn = getattr(h, "on_wake", None)
            if fn is not None:
                fn(self)

    def wake(self) -> None:
        """Wake the sleeping core from a source that is not a pending interrupt (taken at the next loop turn;
        ignored while the core is running)."""
        if self.sleeping:
            self._wake_req = True

    def _clock_changed(self, old_hz: float, new_hz: float) -> None:
        """clock.set_hz from an MMIO write mid-slice: the slice length was computed in old-clock cycles (next event,
        end of the run window), so end the slice after this access and let the loop recompute both."""
        if self.core.running:
            self.core.request_stop(from_mmio=True)

    def _early_event(self) -> None:
        """An MMIO access (or an event) scheduled an event inside the running slice: end the slice exactly when it is
        due. A core with `end_slice_at` (Unicorn) lowers the running slice's instruction budget, so the slice ends at
        the event's cycle (at the earliest after the accessing instruction) and nothing stops early; other cores
        (interpreters) stop after the current instruction. While this chip does not run, `idle_watch(chip)` (the
        board's auto quantum) hears about events scheduled on it."""
        core = self.core
        if core.running:
            cut = self._end_slice_at
            if cut is not None:
                nd = self.sched.next_due()
                if nd is not None:
                    n = nd - self.clock.cycles
                    if cut(n):
                        if self.clock.cycles + n < self.sched.horizon:
                            self.sched.horizon = self.clock.cycles + n
                        return
            core.request_stop(from_mmio=True)
        elif self.idle_watch is not None:
            self.idle_watch(self)

    def _skip_poll(self, target: int) -> None:
        """The core stopped in a polling loop whose state repeats every `core.poll_skip` instructions (perf.py
        poll_skip_ns): advance time by whole iterations up to the next event / the run window's end, at most
        `poll_skip_ns` at a time (a status register that a model computes from the time on each read, not from an
        event, is then seen up to that late)."""
        core, clock = self.core, self.clock
        d = core.poll_skip
        core.poll_skip = None
        if not d or self.poll_skip_ns <= 0:
            return
        now = clock.cycles
        lim = min(target, now + clock.ns_to_cycles(self.poll_skip_ns))
        nd = self.sched.next_due()
        if nd is not None and nd < lim:
            lim = nd
        k = (lim - now) // d
        if k > 0:
            clock.advance(k * d)
            self.insns += k * d
            self.poll_skipped += k * d
            self.parked = True

    def yield_run(self) -> None:
        """End the running run_for/run_cycles soon (StopReason.USER) without stopping the emulation: the board's
        lockstep re-plans (auto quantum). Safe from this chip's MMIO callbacks and events: the current instruction
        completes; if the core cannot stop after it, the slice ends normally first."""
        self._stop_flag = True
        if self.core.running:
            self.core.request_stop(from_mmio=True)

    def _log_fault(self, info: dict, handled: bool) -> None:
        pc = info.get("pc")
        addr = info.get("addr")
        msg = f"{info.get('type', 'fault')} at pc=0x{pc or 0:08x}"
        if addr is not None:
            msg += f" addr=0x{addr:08x}"
        if info.get("msg"):
            msg += f": {info['msg']}"
        msg += " → HardFault handler" if handled else " (stopped)"
        self.log.event("fault", self.clock.now_ns(), msg=msg,
                       **{k: v for k, v in info.items() if k not in ("msg",)}, handled=handled)

    def _fault_stop(self, info: dict) -> StopReason:
        self.last_fault = info
        self._log_fault(info, handled=False)
        return StopReason.FAULT

    def step(self) -> StopReason:
        """Execute one instruction (taking a pending exception first if one is due)."""
        with self._lock:
            if not self._was_reset:
                self.reset()
            self.sched.run_due()
            if self.arch.pending:
                self.arch.service()
            if self.sleeping:
                self._end_sleep()
            core = self.core
            core.bp_skip = core.pc                 # a step executes the instruction even if a breakpoint is on it
            r, ex = core.run(1)
            core.bp_skip = core.pc                 # and a resume from where it ends steps over a breakpoint there
            self.clock.advance(max(ex, 1) if r is not StopReason.FAULT else 0)
            self.insns += core.last_insns if getattr(core, "cycle_counted", False) else ex
            if r in (StopReason.EXC_RETURN, StopReason.SVC):
                self.arch.on_stop(r, self.core.stop_info)
            elif r is StopReason.FAULT:
                info = dict(self.core.last_fault or {})
                self.last_fault = info
                self._log_fault(info, handled=self.arch.on_stop(r, info))
            return r

    # ------------------------------------------------------------------ introspection
    def state(self) -> dict:
        cs = getattr(self.arch, "cpu_state", None)
        if cs is not None:
            st = cs()
            st.update({"cycles": self.clock.cycles, "insns": self.insns, "time_ns": self.clock.now_ns(),
                       "hz": self.clock.hz, "sleeping": self.sleeping, "lockup": self.arch.lockup})
            return st
        core = self.core
        pc, sp, msp, psp, xpsr, ctrl, primask = core.read_regs(("pc", "sp", "msp", "psp", "xpsr", "control",
                                                                  "primask"))
        act = self.arch.active[-1] if self.arch.active else None
        st = {"pc": pc, "sp": sp, "msp": msp, "psp": psp, "xpsr": xpsr, "control": ctrl, "primask": primask,
              "cycles": self.clock.cycles, "insns": self.insns, "time_ns": self.clock.now_ns(),
              "hz": self.clock.hz,
              "active": act, "active_name": self.arch.exc_name(act) if act is not None else None,
              "mode": "handler" if act is not None else "thread", "sleeping": self.sleeping,
              "pending": sorted(self.arch.pending), "lockup": self.arch.lockup}
        if not self.arch.v6m:
            st["basepri"], st["faultmask"] = core.read_regs(("basepri", "faultmask"))
        return st

    def uart_blocks(self) -> dict[str, Any]:
        """Peripherals that behave like UARTs (any model exposing the board-link hook `tx_listeners`:
        kinetis.uart, stm32.usart, ti_c2000.sci, ...), in chip YAML order."""
        return {n: b for n, b in self.peripherals.items() if hasattr(b, "tx_listeners")}

    def adc_blocks(self) -> dict[str, Any]:
        """Peripherals that take ADC input values (any model exposing `set_channel(name, value)`: kinetis.adc,
        ti_c2000.adc, stm32.adc, ...), in chip YAML order.  Optional capabilities: `set_input(name, value) -> bool`
        resolves names that are not "<block>_<channel>" (pin names: Session.set_adc falls back to it), and
        `adc_volts = True` marks a model whose float values are volts (otherwise values are raw codes)."""
        return {n: b for n, b in self.peripherals.items() if hasattr(b, "set_channel")}

    def can_blocks(self) -> dict[str, Any]:
        """CAN controllers (any model exposing the CAN node hook `can_attach(bus)`, chipem/canbus.py: kinetis.flexcan,
        ti_c2000.ecan, ...), in chip YAML order."""
        return {n: b for n, b in self.peripherals.items() if hasattr(b, "can_attach")}

    def find_can(self, name: str) -> str | None:
        """Resolve a user-typed CAN controller name: exact (case-insensitive), then ignoring separators and an "E"
        prefix ("eCAN-A" -> ECANA), then by instance suffix ("CANA" -> ECANA, "CAN1" -> CAN1, "FLEXCAN0" -> CAN0)."""
        import re
        cans = self.can_blocks()
        key = str(name).strip().upper()
        if key in cans:
            return key
        norm = re.sub(r"[^A-Z0-9]", "", key)
        for n in cans:
            if re.sub(r"[^A-Z0-9]", "", n.upper()) == norm:
                return n
        pat = re.compile(r"(?:FLEX|E|BX)?CAN([A-Z0-9]+)")
        m = pat.fullmatch(norm)
        if m:
            for n in cans:
                mm = pat.fullmatch(re.sub(r"[^A-Z0-9]", "", n.upper()))
                if mm and mm.group(1) == m.group(1):
                    return n
        return None

    def usb_blocks(self) -> dict[str, Any]:
        """USB device controllers (models exposing the chipem/usbbus.py hook `usb_attach(bus)`: stm32.otg_fs,
        stm32.otg_hs, kinetis.usbfs), in chip YAML order."""
        return {n: b for n, b in self.peripherals.items() if hasattr(b, "usb_attach")}

    def find_usb(self, name: str | None = None) -> str | None:
        """Resolve a USB port name: exact (case-insensitive), ignoring separators and a "_GLOBAL" suffix, "USB" ->
        the first port, "FS"/"HS" -> OTG_FS/OTG_HS, "USB0"/"USBOTG" -> the Kinetis USB0; None -> the first port."""
        import re
        usbs = self.usb_blocks()
        if not usbs:
            return None
        if name is None:
            return next(iter(usbs))
        key = re.sub(r"[^A-Z0-9]", "", str(name).strip().upper())
        key = key.removesuffix("GLOBAL")
        for n in usbs:
            if re.sub(r"[^A-Z0-9]", "", n.upper()).removesuffix("GLOBAL") == key:
                return n
        if key in ("USB", "USBOTG", "OTG"):
            return next(iter(usbs))
        for n in usbs:
            nn = re.sub(r"[^A-Z0-9]", "", n.upper())
            if key in ("FS", "HS") and nn.startswith(f"OTG{key}") or key == "USB0" and nn.startswith("USB"):
                return n
        return None

    def find_uart(self, name: str, *, fallback: bool = False) -> str | None:
        """Resolve a user-typed UART name to a peripheral name, uniformly across families: exact match first
        (case-insensitive), then ignoring separators ("SCI-A" -> SCIA), then by instance suffix so UARTn/USARTn
        and SCIx/UARTx name the same block ("UART1" -> USART1 on an STM32, "UARTA" -> SCIA on a C2000).
        `fallback`: an unknown name resolves to the lowest-numbered UART (used for the default "UART0")."""
        import re
        uarts = self.uart_blocks()
        key = str(name).strip().upper()
        if key in uarts:
            return key
        norm = re.sub(r"[^A-Z0-9]", "", key)
        for n in uarts:
            if re.sub(r"[^A-Z0-9]", "", n.upper()) == norm:
                return n
        pat = re.compile(r"(?:LP)?(?:US?ART|SCI)([A-Z0-9]+)")
        m = pat.fullmatch(norm)
        if m:
            for n in uarts:
                mm = pat.fullmatch(re.sub(r"[^A-Z0-9]", "", n.upper()))
                if mm and mm.group(1) == m.group(1):
                    return n
        if fallback and uarts:                      # lowest instance: USART1 before USART2, SCIA before SCIB
            def inst(n: str):
                mm = pat.fullmatch(re.sub(r"[^A-Z0-9]", "", n.upper()))
                sfx = mm.group(1) if mm else n
                return (0, int(sfx), "") if sfx.isdigit() else (1, 0, sfx)
            return min(uarts, key=inst)
        return None

    def uart_out(self, name: str) -> bytes:
        blk = self.peripherals.get(self.find_uart(name) or name)
        out = getattr(blk, "output", b"") if blk is not None else b""
        return bytes(out)

    def regs(self) -> dict[str, int]:
        rn = getattr(self.arch, "reg_names", None)
        if rn is not None:
            names = list(rn)
            return dict(zip(names, self.core.read_regs(names)))
        names = [f"r{i}" for i in range(13)] + ["sp", "lr", "pc", "xpsr", "msp", "psp", "control", "primask"]
        return dict(zip(names, self.core.read_regs(names)))
