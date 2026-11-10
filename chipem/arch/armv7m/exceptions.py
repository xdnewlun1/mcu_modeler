"""ARMv7-M exception model: pending/enabled/active bookkeeping, priority resolution, stack-frame push/pop.

Runs between Unicorn slices.  Entry pushes the 8-word basic frame (r0-r3, r12, lr, return address, xPSR) or the
26-word extended frame (+ s0-s15, FPSCR, reserved; eager stacking) when CONTROL.FPCA is set, on the stack in use
(PSP in thread mode with CONTROL.SPSEL, else MSP), realigning to 8 bytes per CCR.STKALIGN (xPSR bit 9 records the
padding).  Return is triggered by the core reporting a branch to an EXC_RETURN value.

Register write ordering matters with Unicorn/QEMU: MSR-style writes of MSP/PSP/CONTROL are ignored when the CPU
is unprivileged, and an xPSR write that changes IPSR performs the MSP/PSP banking switch.  So entry writes
SP (current stack) → xPSR (enter handler mode) → CONTROL; return writes CONTROL, MSP, PSP while still in handler
mode and only then xPSR (leaving handler mode).
"""
from __future__ import annotations

import struct
from typing import Any

from ...core.base import StopReason

EXC_NAMES = {1: "Reset", 2: "NMI", 3: "HardFault", 4: "MemManage", 5: "BusFault", 6: "UsageFault", 11: "SVCall",
             12: "DebugMonitor", 14: "PendSV", 15: "SysTick"}

CPUID = {"cortex-m0": 0x410CC200, "cortex-m0plus": 0x410CC601, "cortex-m0+": 0x410CC601, "cortex-m1": 0x410CC200,
         "cortex-m3": 0x410FC231, "cortex-m4": 0x410FC241, "cortex-m4f": 0x410FC241, "cortex-m7": 0x411FC272,
         "cortex-m33": 0x410FD213}

_ENTRY_REGS = ("r0", "r1", "r2", "r3", "r12", "lr", "pc", "xpsr", "control", "sp")
_ENTRY_W = ("sp", "xpsr", "control", "lr", "pc")
_RET_R = ("msp", "psp", "control")
_RET_W = ("control", "msp", "psp", "xpsr", "r0", "r1", "r2", "r3", "r12", "lr", "pc")
_FP_REGS = tuple(f"s{i}" for i in range(16)) + ("fpscr",)
_MASK_REGS = ("primask", "basepri", "faultmask")
_MASK_REGS_V6 = ("primask",)
_B8 = struct.Struct("<8I")
_B26 = struct.Struct("<26I")

# CFSR bits
CFSR_IACCVIOL, CFSR_DACCVIOL = 1 << 0, 1 << 1
CFSR_IBUSERR, CFSR_PRECISERR, CFSR_BFARVALID = 1 << 8, 1 << 9, 1 << 15
CFSR_UNDEFINSTR, CFSR_INVSTATE, CFSR_INVPC, CFSR_NOCP, CFSR_UNALIGNED = 1 << 16, 1 << 17, 1 << 18, 1 << 19, 1 << 24
CFSR_MMARVALID = 1 << 7
HFSR_FORCED, HFSR_VECTTBL = 1 << 30, 1 << 1


class ArchFault(Exception):
    """Unrecoverable exception-model condition (lockup, invalid EXC_RETURN with no handler, stacking error)."""


class ArmV7M:
    def __init__(self, chip: Any, *, fpu: bool, num_irqs: int, variant: str = "armv7e-m", cpu: str = "cortex-m4",
                 prio_bits: int | None = None, reset_vtor: int = 0):
        from .scs import SCSBlock
        self.chip = chip
        self.fpu = bool(fpu)
        self.v6m = variant == "armv6-m"
        self.variant = variant
        self.cpu = cpu
        self.num_irqs = num_irqs
        self.nexc = 16 + num_irqs
        self.prio_bits = prio_bits if prio_bits is not None else (2 if self.v6m else 4)
        self.prio_mask = (0xFF << (8 - self.prio_bits)) & 0xFF
        self.reset_vtor = reset_vtor
        self.vtor = reset_vtor
        self.prio = [0] * self.nexc
        self.pending: set[int] = set()
        self.asserted: set[int] = set()   # external IRQ lines (as exception numbers) currently driven high
        self.enabled: set[int] = set()
        self.active: list[int] = []
        self.prigroup = 0
        self._gmask = 0xFF & ~1        # group-priority mask for PRIGROUP 0
        self.stkalign = True
        self.aspen = True
        self.reset_requested: str | None = None
        self.lockup = False
        self.entries = 0               # statistics
        self.trace = None              # optional cb(kind: "enter"|"return", exc: int)
        self._mask_regs = _MASK_REGS_V6 if self.v6m else _MASK_REGS
        self.scs = SCSBlock(self, cpuid=CPUID.get(cpu, 0x410FC241))
        self._init_fixed_prio()

    # ----------------------------------------------------------------- convenience
    @property
    def core(self):
        return self.chip.core

    def _init_fixed_prio(self) -> None:
        self.prio = [0] * self.nexc
        self.prio[1], self.prio[2], self.prio[3] = -3, -2, -1

    def set_prigroup(self, g: int) -> None:
        self.prigroup = g & 7
        self._gmask = 0xFF & ~((2 << self.prigroup) - 1)

    def set_priority(self, exc: int, value: int) -> None:
        if 4 <= exc < self.nexc:
            self.prio[exc] = value & self.prio_mask

    def group(self, p: int) -> int:
        return p if p < 0 else p & self._gmask

    def exc_name(self, exc: int) -> str:
        if exc >= 16:
            irqs = getattr(getattr(self.chip, "desc", None), "irqs", None) or {}
            return irqs.get(exc - 16, f"IRQ{exc - 16}")
        return EXC_NAMES.get(exc, f"EXC{exc}")

    # ----------------------------------------------------------------- pending/enable API
    def set_irq(self, irq: int, pending: bool = True) -> None:
        """Drive external interrupt line `irq` (level-sensitive, as on the Kinetis NVIC inputs).

        Asserting a line that was low pends exception 16+irq.  While the line stays asserted the interrupt is
        re-pended on exception return from its handler (and when it is enabled or its pending bit is cleared), so
        an ISR that returns without clearing the peripheral flag is re-entered.  Deasserting the line does not
        clear a pending state that is already latched (ARMv7-M B3.4.1: the pending bit stays set until the
        exception is taken or software writes NVIC_ICPR), so a flag cleared before the IRQ was serviced still
        causes one (spurious) handler entry, as on silicon."""
        exc = 16 + irq
        if not 16 <= exc < self.nexc:
            raise ValueError(f"IRQ {irq} out of range (num_irqs={self.num_irqs})")
        if pending:
            if exc not in self.asserted:
                self.asserted.add(exc)
                self.set_pending_exc(exc)
        else:
            self.asserted.discard(exc)

    def pulse_irq(self, irq: int) -> None:
        """Pend external interrupt `irq` once without asserting its line (pulse/edge sources)."""
        exc = 16 + irq
        if not 16 <= exc < self.nexc:
            raise ValueError(f"IRQ {irq} out of range (num_irqs={self.num_irqs})")
        self.set_pending_exc(exc)

    def set_pending_exc(self, exc: int) -> None:
        if exc in self.pending:
            return
        self.pending.add(exc)
        core = self.chip.core
        if core.running and self._can_preempt_now(exc):
            core.request_stop(from_mmio=True)

    def clear_pending_exc(self, exc: int) -> None:
        self.pending.discard(exc)
        if exc in self.asserted and exc not in self.active:
            self.set_pending_exc(exc)           # level still asserted: pending again immediately

    def enable_irq(self, irq: int, enabled: bool = True) -> None:
        exc = 16 + irq
        if enabled:
            if exc not in self.enabled:
                self.enabled.add(exc)
                if exc in self.asserted and exc not in self.active:
                    self.pending.add(exc)
                core = self.chip.core
                if exc in self.pending and core.running and self._can_preempt_now(exc):
                    core.request_stop(from_mmio=True)
        else:
            self.enabled.discard(exc)

    def _can_preempt_now(self, exc: int) -> bool:
        if exc >= 16 and exc not in self.enabled:
            return False
        return self.group(self.prio[exc]) < self.execution_priority()

    # ----------------------------------------------------------------- priority
    def execution_priority(self, masks: list[int] | None = None, ignore_primask: bool = False) -> int:
        if masks is None:
            masks = self.chip.core.read_regs(self._mask_regs)
        ep = 256
        g = self.group
        prio = self.prio
        for a in self.active:
            p = g(prio[a])
            if p < ep:
                ep = p
        if not self.v6m:
            bp = masks[1] & self.prio_mask
            if bp:
                p = g(bp)
                if p < ep:
                    ep = p
            if masks[2] & 1:
                ep = -1
        if masks[0] & 1 and not ignore_primask and ep > 0:
            ep = 0
        return ep

    def best_pending(self) -> int | None:
        best = None
        bp = 1 << 30
        prio = self.prio
        en = self.enabled
        for e in self.pending:
            if e >= 16 and e not in en:
                continue
            p = prio[e]
            if p < bp or (p == bp and e < best):
                best, bp = e, p
        return best

    def wakeup_pending(self) -> bool:
        """WFI wake-up condition: an enabled pending exception that would preempt if PRIMASK were clear."""
        if not self.pending:
            return False
        b = self.best_pending()
        if b is None:
            return False
        return self.group(self.prio[b]) < self.execution_priority(ignore_primask=True)

    # ----------------------------------------------------------------- service / entry
    def service(self) -> bool:
        if not self.pending or self.lockup:
            return False
        b = self.best_pending()
        if b is None:
            return False
        if self.group(self.prio[b]) >= self.execution_priority():
            return False
        self._enter(b)
        return True

    def _read_word(self, addr: int) -> int:
        return int.from_bytes(self.chip.core.read_mem(addr, 4), "little")

    def _vector(self, exc: int) -> int:
        return self._read_word((self.vtor & ~0x7F) + 4 * exc)

    def _enter(self, exc: int, ret_pc: int | None = None) -> None:
        core = self.chip.core
        r0, r1, r2, r3, r12, lr, pc, xpsr, control, sp = core.read_regs(_ENTRY_REGS)
        thread = not self.active
        use_psp = thread and bool(control & 2)
        fp = self.fpu and bool(control & 4) and self.aspen
        size = 0x68 if fp else 0x20
        pad = (sp >> 2) & 1 if self.stkalign else 0
        frame = (sp - size) & ~(pad << 2) & 0xFFFFFFFF
        ret = (pc if ret_pc is None else ret_pc) & ~1
        xs = (xpsr & ~0x200) | (pad << 9) | 0x01000000
        if fp:
            f = core.read_regs(_FP_REGS)
            data = _B26.pack(r0, r1, r2, r3, r12, lr, ret, xs, *f, 0)
        else:
            data = _B8.pack(r0, r1, r2, r3, r12, lr, ret, xs)
        try:
            core.write_mem(frame, data)
            vec = self._vector(exc)
        except Exception as e:
            self.lockup = True
            raise ArchFault(f"stacking error entering {self.exc_name(exc)}: SP=0x{frame:08x} ({e})") from None
        excret = 0xFFFFFFE1 | (0 if fp else 0x10) | (0x08 if thread else 0) | (0x04 if use_psp else 0)
        core.write_regs(_ENTRY_W, (frame, 0x01000000 | exc, control & 1, excret, vec & ~1))
        self.pending.discard(exc)
        self.active.append(exc)
        self.entries += 1
        if self.trace is not None:
            self.trace("enter", exc)

    def _exc_return(self, excret: int) -> None:
        core = self.chip.core
        if not self.active:
            raise ArchFault(f"EXC_RETURN 0x{excret:08x} in thread mode")
        if (excret & 0xFFFFFFE0) != 0xFFFFFFE0 or (excret & 0xF) not in (1, 9, 13):
            raise ArchFault(f"invalid EXC_RETURN 0x{excret:08x}")
        to_thread = bool(excret & 8)
        use_psp = bool(excret & 4)
        fp = not (excret & 0x10)
        if fp and not self.fpu:
            raise ArchFault(f"EXC_RETURN 0x{excret:08x} requests an FP frame on a core without FPU")
        if to_thread != (len(self.active) == 1):
            raise ArchFault(f"EXC_RETURN 0x{excret:08x} to {'thread' if to_thread else 'handler'} mode with "
                            f"{len(self.active)} active exception(s)")
        msp, psp, control = core.read_regs(_RET_R)
        sp = psp if use_psp else msp
        size = 0x68 if fp else 0x20
        try:
            data = core.read_mem(sp, size)
        except Exception as e:
            raise ArchFault(f"unstacking error at 0x{sp:08x} ({e})") from None
        if fp:
            v = _B26.unpack(data)
        else:
            v = _B8.unpack(data)
        exc = self.active.pop()
        if self.trace is not None:
            self.trace("return", exc)
        if exc in self.asserted:
            self.pending.add(exc)               # level-sensitive line still asserted: re-pend (taken after return)
        xs = v[7]
        new_sp = (sp + size) | (((xs >> 9) & 1) << 2 if self.stkalign else 0)
        if use_psp:
            psp = new_sp
        else:
            msp = new_sp
        ipsr = self.active[-1] if self.active else 0
        # Unicorn's M-profile model runs in the Secure state, so an FP instruction with CONTROL.FPCA=1 but
        # CONTROL.SFPA (bit 3) clear still opens a *new* FP context (FPSCR := FPDSCR); restore both bits.
        ctrl = (control & 1) | (2 if use_psp else 0) | (0xC if fp else 0)
        if fp:
            core.write_regs(_FP_REGS, v[8:25])
        if exc != 2 and not self.v6m:
            core.write_regs(("faultmask",), (0,))   # DeActivate(): FAULTMASK cleared on return (except NMI)
        core.write_regs(_RET_W, (ctrl, msp, psp, (xs & ~0x3FF) | ipsr | 0x01000000,
                                 v[0], v[1], v[2], v[3], v[4], v[5], v[6] & ~1))

    # ----------------------------------------------------------------- stop handling
    def on_stop(self, reason: StopReason, info: Any = None) -> bool:
        """Handle a core stop. Returns True if execution can continue."""
        if reason is StopReason.EXC_RETURN:
            self._exc_return(info if info is not None else self.chip.core.stop_info)
            return True
        if reason is StopReason.SVC:
            return self._sync_exception(11, "SVC")
        if reason is StopReason.FAULT:
            return self.fault(info if info is not None else self.chip.core.last_fault)
        return True

    def _sync_exception(self, exc: int, what: str) -> bool:
        """Synchronous exception (SVC): must be taken now, else escalates to HardFault."""
        if self.group(self.prio[exc]) < self.execution_priority():
            self._enter(exc)
            return True
        self.scs.hfsr |= HFSR_FORCED
        return self._hardfault(f"{what} escalated (priority too low to be taken)", None)

    def fault(self, info: dict | None) -> bool:
        """Core fault → update CFSR/HFSR/BFAR, enter HardFault if possible. Returns False on lockup."""
        info = info or {}
        kind = info.get("type", "fault")
        addr = info.get("addr")
        scs = self.scs
        if kind in ("read_unmapped", "write_unmapped", "read_prot", "write_prot"):
            scs.cfsr |= CFSR_PRECISERR | CFSR_BFARVALID
            scs.bfar = addr or 0
        elif kind in ("fetch_unmapped", "fetch_prot", "prefetch_abort"):
            scs.cfsr |= CFSR_IBUSERR
        elif kind == "undefined_instruction":
            scs.cfsr |= CFSR_UNDEFINSTR
        elif kind == "invalid_state":
            scs.cfsr |= CFSR_INVSTATE
        elif kind == "nocp":
            scs.cfsr |= CFSR_NOCP
        elif kind == "unaligned":
            scs.cfsr |= CFSR_UNALIGNED
        elif kind == "data_abort":
            scs.cfsr |= CFSR_PRECISERR
        scs.hfsr |= HFSR_FORCED
        return self._hardfault(kind, info.get("pc"))

    def _hardfault(self, why: str, pc: int | None) -> bool:
        if 3 in self.active or 2 in self.active or self.execution_priority() <= -1:
            self.lockup = True
            return False
        vec = self._vector(3)
        if vec in (0, 0xFFFFFFFF):
            return False
        self._enter(3, ret_pc=pc)
        return True

    # ----------------------------------------------------------------- reset
    def reset(self) -> None:
        self.vtor = self.reset_vtor
        # Lines still driven by a peripheral whose state survives reset (RTC in the VBAT domain) pend again;
        # every other model deasserts its own line in reset() before this runs.
        self.pending = set(self.asserted)
        self.enabled.clear()
        self.active.clear()
        self._init_fixed_prio()
        self.set_prigroup(0)
        self.reset_requested = None
        self.lockup = False
        self.scs.reset()
        self.stkalign = True
        self.aspen = True
        sp = self._read_word(self.vtor)
        pc = self._read_word(self.vtor + 4)
        self.chip.core.reset(pc & ~1, sp)

    # ----------------------------------------------------------------- low-power support (SCB SCR)
    def sleep_on_exit(self) -> bool:
        """SCR.SLEEPONEXIT: the core goes back to sleep on the return to thread mode."""
        return bool(self.scs.get("SCR") & 0x2)

    @property
    def sleep_deep(self) -> bool:
        """SCR.SLEEPDEEP: the family power controller picks its deep-sleep mode (Kinetis STOP, STM32 Stop/Standby)."""
        return bool(self.scs.get("SCR") & 0x4)

    @property
    def sevonpend(self) -> bool:
        return bool(self.scs.get("SCR") & 0x10)

    def request_reset(self, why: str) -> None:
        self.reset_requested = why
        core = self.chip.core
        if core.running:
            core.request_stop(from_mmio=True) or core.request_stop()

    # ----------------------------------------------------------------- introspection
    def state(self) -> dict:
        return {"active": list(self.active), "pending": sorted(self.pending), "enabled_irqs": sorted(
            e - 16 for e in self.enabled), "vtor": self.vtor, "prigroup": self.prigroup, "lockup": self.lockup}
