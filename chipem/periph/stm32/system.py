"""STM32F4 system blocks: IWDG, WWDG (stub), FLASH interface, PWR, DBGMCU.

IWDG (RM0090 ch. 21): KR keys 0xCCCC start (cannot be stopped again), 0xAAAA reload, 0x5555 unlock PR/RLR (any
other key or a reload locks them again; locked writes are ignored with one `warn`).  Counter clock = LSI
(params `lsi_hz`, default 32 kHz) / (4 << PR) (PR >= 6: /256); timeout = (RLR + 1) ticks from start/reload.
SR.PVU/RVU read 0 (updates are immediate).  Timeout: `warn`, RCC_CSR.IWDGRSTF on the next reset, then
chip.reset("watchdog").  Scheduled with Scheduler.at_ns (LSI does not depend on the core clock).

WWDG: SVD storage only (CR/CFR reset 0x7F); enabling it (CR.WDGA) logs one `warn` (no reset is ever generated).

FLASH (RM0090 ch. 3): ACR is storage (LATENCY, PRFTEN, ICEN, DCEN read back; wait states have no timing effect).
KEYR 0x45670123 then 0xCDEF89AB clears CR.LOCK (a wrong key locks until reset); writing CR.LOCK=1 locks.  SR.BSY
always 0; EOP and the error flags are write-1-to-clear.  CR.STRT with SER (sector SNB) or MER erases the flash
memory to 0xFF immediately (sectors 0-3 16 KB, 4 64 KB, 5-11 128 KB from the FLASH memory region) and sets EOP when
EOPIE is set.  Programming (PG) needs nothing: the CPU can write the flash region directly.

PWR (RM0090 ch. 5): CR storage (VOS, DBP, ...; VOS made writable, the SVD has it read-only); CSR.VOSRDY reads 1 (regulator always ready); CSR.BRR mirrors BRE.

DBGMCU: IDCODE reads 0x10076413 (DEV_ID 0x413 STM32F405/407, REV_ID 0x1007; the SVD's 0x411 is wrong for this
part); CR / APB1_FZ / APB2_FZ are storage.
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ._util import GatedBlock, build_regs, rcc

KR_START, KR_RELOAD, KR_UNLOCK = 0xCCCC, 0xAAAA, 0x5555
FLASH_KEYS = (0x45670123, 0xCDEF89AB)
CR_PG, CR_SER, CR_MER, CR_STRT, CR_EOPIE, CR_LOCK = 1 << 0, 1 << 1, 1 << 2, 1 << 16, 1 << 24, 1 << 31
SR_EOP = 1 << 0
SECTORS = [(0x0000, 0x4000), (0x4000, 0x4000), (0x8000, 0x4000), (0xC000, 0x4000), (0x10000, 0x10000)] + \
          [(0x20000 * k, 0x20000) for k in range(1, 8)]


@register("stm32.iwdg")
class IWDG(MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov = {"KR": {"on_write": self._kr_write, "on_read": lambda b, r: 0},
              "PR": {"on_write": self._cfg_write}, "RLR": {"on_write": self._cfg_write},
              "SR": {"ro_mask": 0xFFFFFFFF, "on_read": lambda b, r: 0}}
        r = build_regs((name, "IWDG"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.lsi_hz = float(params.get("lsi_hz", 32000.0))
        self.running = False
        self.unlocked = False
        self._ev = None
        self._start_ns = 0.0
        self._warned_locked = False
        self.timeouts = 0

    def reset(self) -> None:
        self._cancel()
        super().reset()
        self.running = False
        self.unlocked = False
        self._warned_locked = False

    def _cancel(self) -> None:
        if self._ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None

    @property
    def prescaler(self) -> int:
        return min(4 << (self.get("PR") & 7), 256)

    @property
    def timeout_ns(self) -> float:
        return ((self.get("RLR") & 0xFFF) + 1) * self.prescaler * 1e9 / self.lsi_hz

    def counter(self) -> int:
        if not self.running or self.chip is None:
            return self.get("RLR") & 0xFFF
        el = int((self.chip.now_ns() - self._start_ns) * self.lsi_hz / self.prescaler / 1e9)
        return max((self.get("RLR") & 0xFFF) - el, 0)

    def _reload(self) -> None:
        self._cancel()
        if not self.running or self.chip is None:
            return
        self._start_ns = self.chip.now_ns()
        self._ev = self.chip.sched.at_ns(self._start_ns + self.timeout_ns, self._timeout)

    def _timeout(self) -> None:
        self._ev = None
        self.timeouts += 1
        self.emit("warn", block=self.name, msg=f"IWDG timeout after {self.timeout_ns / 1e6:.3f} ms without reload "
                  f"(0xAAAA): watchdog reset")
        r = rcc(self.chip)
        if r is not None:
            r.pending_reset_flags |= (1 << 29) | (1 << 26)          # IWDGRSTF, PINRSTF
        self.chip.reset("watchdog")

    def _kr_write(self, blk, reg, old, new):
        key = new & 0xFFFF
        if key == KR_START:
            if not self.running:
                self.running = True
                self.emit("info", block=self.name, msg=f"IWDG started: timeout {self.timeout_ns / 1e6:.3f} ms "
                          f"(LSI {self.lsi_hz / 1e3:.0f} kHz / {self.prescaler}, RLR={self.get('RLR') & 0xFFF}); "
                          f"reload with 0xAAAA", timeout_ns=self.timeout_ns)
            self.unlocked = False
            self._reload()
        elif key == KR_RELOAD:
            self.unlocked = False
            self._reload()
        elif key == KR_UNLOCK:
            self.unlocked = True
        else:
            self.unlocked = False
        return 0

    def _cfg_write(self, blk, reg, old, new):
        if not self.unlocked:
            if not self._warned_locked:
                self._warned_locked = True
                self.emit("warn", block=self.name, msg=f"IWDG_{reg.name} write without the 0x5555 unlock key: ignored")
            return old
        return None


@register("stm32.wwdg")
class WWDG(MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        r = build_regs((name, "WWDG"), size, {"CR": {"on_write": self._cr_write}}, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self._warned = False

    def _cr_write(self, blk, reg, old, new):
        if new & 0x80 and not old & 0x80:
            new |= 0x80
            if not self._warned:
                self._warned = True
                self.emit("warn", block=self.name, msg="WWDG enabled: the window watchdog is not modelled "
                          "(it never resets the chip)")
        elif old & 0x80:
            new |= 0x80                       # WDGA can only be cleared by reset
        return new


@register("stm32.flash")
class FLASH(MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov = {"KEYR": {"on_write": self._keyr_write, "on_read": lambda b, r: 0},
              "OPTKEYR": {"on_read": lambda b, r: 0},
              "SR": {"w1c_mask": 0xF3, "ro_mask": 0xFFFFFF0C},
              "CR": {"on_write": self._cr_write}}
        r = build_regs((name, "FLASH"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self._key_stage = 0
        self._wrong_key = False
        self._warned_lock = False

    def reset(self) -> None:
        super().reset()
        self._key_stage = 0
        self._wrong_key = False
        self._warned_lock = False

    @property
    def latency(self) -> int:
        return self.get("ACR") & 0xF

    def _keyr_write(self, blk, reg, old, new):
        if self._wrong_key or not self.get("CR") & CR_LOCK:
            return 0
        if self._key_stage == 0 and new == FLASH_KEYS[0]:
            self._key_stage = 1
        elif self._key_stage == 1 and new == FLASH_KEYS[1]:
            self._key_stage = 0
            self.set("CR", self.get("CR") & ~CR_LOCK)
        else:
            self._wrong_key = True
            self.emit("warn", block=self.name, msg="FLASH_KEYR: wrong unlock sequence; FLASH_CR locked until reset "
                      "(silicon also raises a bus error)")
        return 0

    def _flash_region(self) -> Any:
        chip = self.chip
        for m in getattr(chip.desc, "memory", []):
            if m.kind == "flash" and m.name.upper().startswith("FLASH"):
                return m
        return None

    def _cr_write(self, blk, reg, old, new):
        if old & CR_LOCK:
            if not self._warned_lock:
                self._warned_lock = True
                self.emit("warn", block=self.name, msg="FLASH_CR write while locked: ignored")
            return old
        if not new & CR_STRT:
            return None
        new &= ~CR_STRT
        mem = self._flash_region()
        if mem is not None and new & (CR_SER | CR_MER):
            if new & CR_MER:
                spans = [(0, mem.size)]
                what = "mass erase"
            else:
                snb = (new >> 3) & 0xF
                spans = [SECTORS[snb]] if snb < len(SECTORS) else []
                what = f"sector {snb} erase"
            for off, n in spans:
                n = max(0, min(n, mem.size - off))
                if n:
                    self.chip.core.write_mem(mem.base + off, b"\xFF" * n)
            flush = getattr(self.chip.core, "flush_code_cache", None)
            if flush is not None:
                flush()
            self.emit("info", block=self.name, msg=f"FLASH {what}")
        if new & CR_EOPIE:
            self.set("SR", self.get("SR") | SR_EOP)
        return new


@register("stm32.pwr")
class PWR(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = "PWR"
        # the SVD marks PWR_CR.VOS (bit 14) read-only; on the F405/F407 it is read-write (RM0090 5.4.1)
        ov = {"CR": {"ro_mask": 0xFFFFBC00}, "CSR": {"reset": 1 << 14, "on_read": self._csr_read}}
        r = build_regs((name, "PWR"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)

    def _csr_read(self, blk, reg):
        v = self.get("CSR") | (1 << 14)                           # VOSRDY
        if v & (1 << 9):
            v |= 1 << 3                                         # BRR follows BRE
        return v


@register("stm32.dbgmcu")
class DBGMCU(MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        idcode = int(params.get("idcode", 0x10076413))
        ov = {"DBGMCU_IDCODE": {"reset": idcode, "ro_mask": 0xFFFFFFFF}}
        r = build_regs((name, "DBGMCU", "DBG"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
