"""Small C2000 models: register storage with reset values (`ti_c2000.storage`, e.g. device emulation and blocks of
other parts that have no model yet), flash configuration (`ti_c2000.flash`), code security module (`ti_c2000.csm`) and the L0-L3 dual mapping (`ti_c2000.alias`).  SPI-A is `ti_c2000.spi` (spi.py)."""
from __future__ import annotations

from typing import Any

from ..registry import register
from ._util import C2000Block, build_regs


@register("ti_c2000.storage")
class Storage(C2000Block):
    """Register file with the reset values from the register table; no behaviour.  `params.regs` names the table
    entry (default: the block name).  Accesses are not logged as unmodelled when the register is declared."""

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        try:
            regs = build_regs((params.get("regs") or name, name), size, {}, params.get("regs_file"))
        except KeyError:
            regs = []
        super().__init__(name, base, size, regs, params=params, log=log)
        self.gate = params.get("gate")


@register("ti_c2000.flash")
class FlashCtl(C2000Block):
    """Flash/OTP configuration registers (0x0A80, SPRUI07 1.1.4), EALLOW and CSM protected.

    FPWR/FSTATUS power state machine (Figure 1-1): writing a lower PWR moves FSTATUS.PWRS down at once; a higher PWR
    goes through standby (FSTDBYWAIT SYSCLKOUT, STDBYWAITS = 1 meanwhile) and active (FACTIVEWAIT, ACTIVEWAITS = 1).
    The flash resets to sleep and the boot ROM's CSM dummy read wakes it, so PWRS reads active after reset (FPWR 0).
    Approximations: a flash access does not wake a sleeping bank and nothing stalls during the delays (the core reads
    flash as if active); FOPT.ENPIPE, FBANKWAIT and FOTPWAIT are stored only (flash and OTP accesses cost one cycle,
    like every instruction); FSTATUS.3VSTAT is write-1-to-clear and never set.  The TI Flash API (erase / program
    through undocumented pump registers) is not supported: flash is written by ordinary CPU stores instead."""
    eallow_regs = "*"
    cycle_anchors = ()

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov = {"FSTATUS": {"on_read": self._fstatus, "on_write": self._fstatus_write},
              "FPWR": {"on_write": self._fpwr_write}}
        regs = build_regs((name, "FLASH"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, regs, params=params, log=log)
        self.pwrs = 3
        self._busy = 0                       # STDBYWAITS (4) / ACTIVEWAITS (8) while counting
        self._ev = None
        self._csm = None

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        from ._util import find_model
        self._csm = find_model(chip, "ti_c2000.csm")

    def reset(self) -> None:
        if self._ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None
        super().reset()
        self.pwrs = 3                        # sleep at reset, woken by the boot ROM's PWL dummy read
        self._busy = 0

    def _blocked(self) -> bool:
        csm = self._csm
        return csm is not None and csm.blocks_access()

    def read(self, offset: int, size: int) -> int:
        if offset < 8 and self._blocked():
            return 0
        return super().read(offset, size)

    def write(self, offset: int, size: int, value: int) -> None:
        if offset < 8 and self._blocked():
            self.emit("warn", block=self.name, msg="flash register write ignored: CSM secure and code runs outside "
                      "secure memory")
            return
        super().write(offset, size, value)

    def _fstatus(self, blk, reg):
        return (self.get("FSTATUS") & 0x0100) | self._busy | self.pwrs

    def _fstatus_write(self, blk, reg, old, new):
        return old & ~(new & 0x0100) & 0x0100            # 3VSTAT write-1-to-clear

    def _fpwr_write(self, blk, reg, old, new):
        pwr = new & 3
        if pwr == 2:
            return pwr                                    # reserved: no effect
        if self._ev is not None:
            self.chip.sched.cancel(self._ev)
            self._ev = None
            self._busy = 0
        if pwr <= self.pwrs:
            self.pwrs = pwr
        else:
            self._step(pwr)
        return pwr

    def _step(self, target: int) -> None:
        if self.pwrs >= target:
            self._busy = 0
            return
        if self.pwrs == 0:
            self._busy, nxt, n = 4, 1, self.get("FSTDBYWAIT") & 0x1FF
        else:
            self._busy, nxt, n = 8, 3, self.get("FACTIVEWAIT") & 0x1FF
        self._ev = self.chip.sched.in_cycles(n, self._stepped, nxt, target)

    def _stepped(self, nxt: int, target: int) -> None:
        self._ev = None
        self.pwrs = nxt
        self._step(target)


PWL_BASE = 0x33FFF8
# memory the CSM protects on the F28232 (SPRUI07 Table 1-10; words, hi exclusive): L0-L3, flash, OTP, L0-L3 mirror
SECURE_RANGES = ((0x8000, 0xC000), (0x300000, 0x340000), (0x380000, 0x380800), (0x3F8000, 0x3FC000))


@register("ti_c2000.csm")
class CSM(C2000Block):
    """Code security module (0x0AE0, SPRUI07 1.2): KEY0-7 (EALLOW, reset 0xFFFF) and CSMSCR (SECURE, FORCESEC).

    The device is secure after reset.  The password match flow unsecures it: a dummy read of all eight password
    locations PWL0-7 (0x33FFF8-0x33FFFF) and then KEY0-7 = PWL0-7.  PWL all ones: the dummy read alone unsecures; PWL
    all zeros: secure for good.  The match is evaluated continuously after the dummy read (a later wrong KEY write
    secures again).  CSMSCR.FORCESEC = 1 clears KEY (to 0) and secures; a new dummy read is then needed.  The boot ROM
    performs the dummy read at every reset (`params.boot_dummy_read`, default true), so an erased password leaves the
    device unsecure, as on silicon.  While secure, CPU data reads of the protected memory (`SECURE_RANGES`, plus the
    flash registers) return 0 unless the instruction executing is itself in secure memory; instruction fetches are
    never blocked.  Not modelled: blocking of writes from unsecure code to L0-L3 (they still land), the ECSL (no
    emulator; KEY0-3 are storage for it) and the JTAG side."""
    eallow_regs = ["KEY0", "KEY1", "KEY2", "KEY3", "KEY4", "KEY5", "KEY6", "KEY7", "CSMSCR"]

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov = {f"KEY{i}": {"on_write": self._key_write} for i in range(8)}
        ov["CSMSCR"] = {"on_read": self._scr_read, "on_write": self._scr_write}
        regs = build_regs((name, "CSM"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, regs, params=params, log=log)
        self.boot_dummy_read = bool(params.get("boot_dummy_read", True))
        self.secure = True
        self._pwl_read = 0                   # bit i: PWLi read since reset / FORCESEC
        self._filter = False
        self.blocked_reads = 0

    def reset(self) -> None:
        super().reset()
        self.secure = True
        self._pwl_read = 0
        if self.boot_dummy_read and self.chip is not None:
            self._pwl_read = 0xFF
            self._evaluate(quiet=True)
        self._update_filter()

    # ------------------------------------------------------------------ state
    def _pwl(self) -> list[int]:
        try:
            data = self.chip.core.read_mem(PWL_BASE, 8)
        except Exception:
            return [0xFFFF] * 8
        return [int.from_bytes(data[2 * i:2 * i + 2], "little") for i in range(8)]

    def _evaluate(self, quiet: bool = False) -> None:
        was = self.secure
        if self._pwl_read != 0xFF:
            sec = True
        else:
            pwl = self._pwl()
            if all(w == 0 for w in pwl):
                sec = True
            elif all(w == 0xFFFF for w in pwl):
                sec = False
            else:
                sec = [self.get(f"KEY{i}") for i in range(8)] != pwl
        self.secure = sec
        if sec != was:
            if not quiet:
                self.emit("info", block=self.name, msg="CSM " + ("secured" if sec else "unsecured (password match)"))
            self._update_filter()

    def _update_filter(self) -> None:
        arch = getattr(self.chip, "arch", None) if self.chip is not None else None
        fn = getattr(arch, "set_read_filter", None)
        if fn is None:
            return
        want_pwl = self._pwl_read != 0xFF
        if self.secure or want_pwl:
            ranges = list(SECURE_RANGES) if self.secure else [(PWL_BASE, PWL_BASE + 8)]
            self._filter = bool(fn(self._read_filter, ranges))
        elif self._filter:
            fn(None, ())
            self._filter = False

    @staticmethod
    def in_secure(addr: int) -> bool:
        for lo, hi in SECURE_RANGES:
            if lo <= addr < hi:
                return True
        return False

    def blocks_access(self) -> bool:
        """Secure and the executing instruction is outside secure memory (data access to secure memory blocked)."""
        if not self.secure or self.chip is None:
            return False
        arch = self.chip.arch
        pc = arch.live_pc() if hasattr(arch, "live_pc") else self.chip.core.pc
        return not self.in_secure(pc)

    def _read_filter(self, addr: int, n: int, value: int) -> int:
        if PWL_BASE <= addr + n - 1 and addr < PWL_BASE + 8 and self._pwl_read != 0xFF:
            for a in range(addr, addr + n):
                if PWL_BASE <= a < PWL_BASE + 8:
                    self._pwl_read |= 1 << (a - PWL_BASE)
            blocked = self.secure and self.in_secure(addr) and self.blocks_access()
            if self._pwl_read == 0xFF:
                self._evaluate()
            return 0 if blocked else value
        if self.secure and self.in_secure(addr) and self.blocks_access():
            self.blocked_reads += 1
            if self.blocked_reads == 1:
                self.emit("warn", block=self.name, msg=f"CSM: read of secure 0x{addr:06X} from unsecure code "
                          "returns 0")
            return 0
        return value

    # ------------------------------------------------------------------ registers
    def _key_write(self, blk, reg, old, new):
        self._store(reg, new)
        self._evaluate()
        return None

    def _scr_read(self, blk, reg):
        return 0x005E | (1 if self.secure else 0)

    def _scr_write(self, blk, reg, old, new):
        if new & 0x8000:                                   # FORCESEC: clear KEY, secure
            for i in range(8):
                self.set(f"KEY{i}", 0)
            self._pwl_read = 0
            if not self.secure:
                self.emit("info", block=self.name, msg="CSM secured (FORCESEC)")
            self.secure = True
            self._update_filter()
        return 0


@register("ti_c2000.alias")
class Alias(C2000Block):
    """A second view of memory mapped elsewhere (L0-L3 SARAM appear at 0x8000 and 0x3F8000): every access is
    forwarded to `params.target` + offset through the bus."""

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        super().__init__(name, base, 0, (), params=params, log=log)
        self.size = size                    # no backing store of its own
        t = params.get("target", 0)
        self.target = int(t, 0) if isinstance(t, str) else int(t)
        self.log_unknown = False

    def read(self, offset: int, size: int) -> int:
        return self.chip.bus.read(self.target + offset, size)

    def write(self, offset: int, size: int, value: int) -> None:
        self.chip.bus.write(self.target + offset, size, value)

    def reset(self) -> None:
        pass
