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


class Core(ABC):
    last_fault: dict | None = None
    stop_info: Any = None      # EXC_RETURN value after StopReason.EXC_RETURN
    precise: bool = False
    running: bool = False
    cpu: str = ""

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
