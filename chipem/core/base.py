"""Backend-agnostic CPU core interface (instruction execution only; no interrupt logic)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Callable, Iterable, Sequence


class StopReason(Enum):
    COUNT = "count"            # slice completed (max_insns executed)
    WFI = "wfi"                # core executed WFI/WFE and is waiting
    EXC_RETURN = "exc_return"  # branch to an EXC_RETURN value; core.stop_info holds the value
    SVC = "svc"                # svc executed; PC is already past the svc
    FAULT = "fault"            # core.last_fault describes it
    BREAKPOINT = "breakpoint"  # bkpt instruction (PC at the bkpt)
    USER = "user"              # request_stop() (other thread, or an MMIO model asking for a reschedule)
    DEBUG = "debug"            # debugger stop (chipem/debug.py): stop_info = {"type": "breakpoint", "addr"} with PC at
                               # the breakpoint (not executed), or {"type": "watchpoint", "addr", "size", "access",
                               # "value", "pc"} with PC after the accessing instruction (pc = that instruction)


class Core(ABC):
    last_fault: dict | None = None
    stop_info: Any = None      # EXC_RETURN value after StopReason.EXC_RETURN; "wfi"/"wfe" (ARM) or "idle" (C28x)
                               # after StopReason.WFI; a dict after StopReason.DEBUG
    precise: bool = False
    running: bool = False
    cpu: str = ""
    # debugger support (docs/CONTRACTS.md "Debugger"): nothing is hooked while no breakpoint/watchpoint is set
    debug_hook: Callable[[str, dict], bool] | None = None   # (kind "break"|"watch", info) -> stop now?
    bp_skip: int | None = None   # resume: the next run() that starts at this PC executes it without a breakpoint stop

    def set_breakpoints(self, addrs: Iterable[int]) -> None:
        """Replace the set of code breakpoint addresses (address units). A run stops with StopReason.DEBUG before
        executing an instruction at one of them when `debug_hook("break", {"addr"})` returns True (or is None)."""
        raise NotImplementedError

    def set_watchpoints(self, wps: Iterable[tuple[int, int, str]]) -> None:
        """Replace the data watchpoints: (lo, hi_exclusive, access "r" | "w" | "rw"). A CPU access overlapping one
        calls `debug_hook("watch", {"addr", "size", "access", "value", "pc"})`; True stops after that instruction."""
        raise NotImplementedError
    # PC of a branch-to-itself the last run() ended in (None otherwise): until an interrupt changes the PC nothing
    # can happen, so the Chip lets the next slice run to the next event regardless of max_slice
    spin_pc: int | None = None
    # poll skip (chipem/perf.py poll_skip_ns): after run() returned USER because the core detected a tight polling
    # loop (set_poll_watch), the loop's period in instructions; the Chip then advances time by whole periods up to the
    # next event (the loop's state repeats exactly, so skipping iterations is the same as running them)
    poll_skip: int | None = None

    def set_poll_watch(self, on: bool) -> bool:
        """Watch MMIO reads for tight polling loops (see poll_skip). Returns False if the core cannot."""
        return False

    # -- execution ---------------------------------------------------------------------------
    @abstractmethod
    def reset(self, pc: int, sp: int) -> None: ...

    @abstractmethod
    def run(self, max_insns: int) -> tuple[StopReason, int]: ...

    def request_stop(self, from_mmio: bool = False) -> bool:
        """Ask the running core to return early (StopReason.USER). Returns True if a stop was requested."""
        return False

    def insns_in_slice(self) -> int:
        """Instructions executed so far in the current run() call (exact in precise mode, else 0)."""
        return 0

    # -- registers ------------------------------------------------------------------------------
    @abstractmethod
    def read_reg(self, name: str) -> int: ...

    @abstractmethod
    def write_reg(self, name: str, value: int) -> None: ...

    def read_regs(self, names: Sequence[str]) -> list[int]:
        return [self.read_reg(n) for n in names]

    def write_regs(self, names: Sequence[str], values: Iterable[int]) -> None:
        for n, v in zip(names, values):
            self.write_reg(n, v)

    @property
    def pc(self) -> int:
        return self.read_reg("pc")

    @pc.setter
    def pc(self, value: int) -> None:
        self.write_reg("pc", value)

    @property
    def sp(self) -> int:
        return self.read_reg("sp")

    @sp.setter
    def sp(self, value: int) -> None:
        self.write_reg("sp", value)

    # -- memory ------------------------------------------------------------------------------------
    @abstractmethod
    def read_mem(self, addr: int, n: int) -> bytes: ...

    @abstractmethod
    def write_mem(self, addr: int, data: bytes) -> None: ...

    @abstractmethod
    def map_memory(self, base: int, size: int, *, writable: bool = True, executable: bool = True) -> None: ...

    @abstractmethod
    def map_mmio(self, base: int, size: int, read: Callable[[int, int], int],
                 write: Callable[[int, int, int], None]) -> None: ...

    def map_shared(self, base: int, size: int, ptr: int, *, writable: bool = True) -> None:
        """Map `size` bytes at `base` onto host memory at address `ptr` (several mappings may share one buffer:
        mirrored external memories). Optional: cores without it raise NotImplementedError."""
        raise NotImplementedError

    def unmap(self, base: int, size: int) -> None:
        """Remove a mapping made with map_memory / map_mmio / map_shared (also from inside an MMIO callback)."""
        raise NotImplementedError

    def set_unmapped_handler(self, fn: Callable[[int, int, str], bool] | None) -> None:
        """fn(addr, size, access 'r'|'w'|'x') -> True if it mapped something and the access should be retried."""

    def flush_code_cache(self) -> None:
        pass

    page_size: int = 0x400


class PollWatch:
    """State of the polling-loop detector shared by the cores (Core.set_poll_watch). The first MMIO address read is
    the anchor; the reads between two anchor reads form one iteration. A loop is recognised when consecutive
    iterations read the same values from the same addresses, write no MMIO register, and the complete CPU register
    state (PC included) is identical at the anchor read: one iteration = the instructions between the two anchor
    reads (at most MAX_PERIOD). An iteration with more than MAX_READS reads moves the anchor. A register snapshot that
    does not repeat (a loop counter, a timeout) backs off: the next one is taken `cool` iterations later (doubling up
    to MAX_COOL, back to 0 after a hit), so loops that are not pure cost one compare per MMIO read."""
    __slots__ = ("anchor", "seq", "prev", "hits", "snap", "cnt", "cool")
    MIN_HITS = 2
    MAX_PERIOD = 512
    MAX_READS = 8
    MAX_COOL = 4096
    MIN_GAIN = 64            # skip only if at least this many instructions of the slice remain (stopping costs more)

    def __init__(self) -> None:
        self.cool = 0
        self.reset()

    def reset(self) -> None:
        self.anchor = None
        self.seq: list = []
        self.prev: list | None = None
        self.hits = -self.cool
        self.snap = None
        self.cnt = 0

    def missed(self) -> None:
        """The register snapshot did not repeat: back off."""
        self.cool = min(max(2 * self.cool, 8), self.MAX_COOL)
        self.hits = -self.cool

    def read(self, a: int, v: int) -> bool:
        """Record an MMIO read; True when it is an anchor read of a repeating iteration (take a snapshot then)."""
        if a != self.anchor:
            if self.anchor is None or len(self.seq) >= self.MAX_READS:
                self.reset()
                self.anchor = a
                self.seq = [(a, v)]
                return False
            self.seq.append((a, v))
            return False
        same = self.seq == self.prev and self.seq[0][1] == v if self.prev is not None else False
        self.prev = self.seq
        self.seq = [(a, v)]
        if not same:
            if self.hits > 0:
                self.hits = 0
            self.snap = None
            return False
        self.hits += 1
        return self.hits >= self.MIN_HITS
