"""STM32F1 backup registers (BKP, RM0008 ch. 6 layout; low density: DR1-DR10, RTCCR, CR, CSR).

Not verified against RM0008 (not available when written).  The block is mapped at the SVD base 0x40006C04 (the first
word of the 0x40006C00 page is reserved), so DR1 is at offset 0.

* Backup domain: DR1-DR10 and RTCCR/CR/CSR keep their values over system resets and Standby; only a backup domain
  reset (RCC_BDCR.BDRST, `backup_domain_reset()`) or a tamper event (DRx only) clears them.  Writes need the BKP and
  PWR clocks and PWR_CR.DBP = 1 (after reset the backup domain is write-protected): writes without DBP are ignored
  with one `warn`.  DR11-DR42 (high/XL density) are not present on the low-density parts (`params.data_regs`, 10).
* Tamper (CR.TPE, TPAL; pin TAMPER-RTC = PC13, not bonded on the 36-pin package): an active level on the tamper pin
  with TPE = 1 sets CSR.TEF, clears DR1-DRn and, with CSR.TPIE, sets TIF and raises the TAMPER vector.  CSR.CTE /
  CTI (write 1) clear TEF / TIF (they read 0).  The tamper pin is watched only when the chip has it.
* RTCCR: CAL (calibration value, stored; the RTC model does not skip pulses), CCO / ASOE / ASOS (RTC clock or
  alarm/second pulse output on the tamper pin: stored, not driven).
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ._util import GatedBlock, build_regs, find_model, irq_number

TPE, TPAL = 1 << 0, 1 << 1
CTE, CTI, TPIE, TEF, TIF = 1 << 0, 1 << 1, 1 << 2, 1 << 8, 1 << 9


@register("stm32f1.bkp")
class BKP(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = "BKP"
        self.n_data = int(params.get("data_regs", 10))
        skip = [f"DR{k}" for k in range(self.n_data + 1, 43)]
        ov: dict[str, dict] = {f"DR{k}": {"on_write": self._prot, "ro_mask": 0xFFFF0000}
                               for k in range(1, self.n_data + 1)}
        ov["RTCCR"] = {"on_write": self._prot, "ro_mask": 0xFFFFFC00}
        ov["CR"] = {"on_write": self._cr_write, "ro_mask": 0xFFFFFFFC}
        ov["CSR"] = {"on_write": self._csr_write, "ro_mask": TEF | TIF | 0xFFFFFC00}
        r = build_regs((name, "BKP"), size, ov, params.get("regs_file"), skip=skip)
        super().__init__(name, base, size, r, log=log, params=params)
        self._powered = False
        self._saved: bytes = b""
        self._warned: set[str] = set()
        self._pwr: Any = None
        self._irqn: int | None = None
        self._tamper_pin: Any = None
        self._line = False
        self.tamper_events = 0

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._pwr = find_model(chip, "stm32f1.pwr", "PWR")
        self._irqn = irq_number(chip, "TAMPER")
        board = getattr(chip, "pins", None)
        if board is not None:
            for p in board.signal_pins("TAMPER-RTC"):
                self._tamper_pin = p
                board.input_listeners[p.id].append(self._tamper_level)
                break

    def reset(self) -> None:
        """System reset / Standby: the backup domain keeps its content."""
        keep = bytes(self.mem) if self._powered else None
        super().reset()
        if keep is not None:
            self.mem[:] = keep
        self._powered = True
        self._warned.clear()
        self._update_irq()

    def backup_domain_reset(self) -> None:
        MMIOBlock.reset(self)
        self._update_irq()

    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()
        MMIOBlock.write(self, offset, size, value)

    def _dbp(self) -> bool:
        p = self._pwr
        return p is None or bool(p.get("CR") & (1 << 8))

    def _prot(self, blk, reg, old, new):
        if not self._dbp():
            if "dbp" not in self._warned:
                self._warned.add("dbp")
                self.emit("warn", block=self.name, msg=f"BKP_{reg.name} write ignored: backup domain write "
                          f"protection (set PWR_CR.DBP first)")
            return old
        return None

    def _cr_write(self, blk, reg, old, new):
        if self._prot(blk, reg, old, new) is not None:
            return old
        self._store(reg, new)
        self._tamper_check()
        return None

    def _csr_write(self, blk, reg, old, new):
        if self._prot(blk, reg, old, new) is not None:
            return old
        v = new & ~(CTE | CTI)
        if new & CTE:
            v &= ~TEF
        if new & CTI:
            v &= ~TIF
        self._store(reg, v)
        self._update_irq()
        return v

    # ------------------------------------------------------------ tamper
    def _tamper_level(self, pin: Any) -> None:
        self._tamper_check()

    def _tamper_check(self) -> None:
        pin = self._tamper_pin
        cr = self.get("CR")
        if pin is None or not cr & TPE:
            return
        lvl = pin.level()
        active = (lvl == "L") if cr & TPAL else (lvl == "H")
        if active and not self.get("CSR") & TEF:
            self.tamper_events += 1
            for k in range(1, self.n_data + 1):
                self.set(f"DR{k}", 0)
            csr = self.get("CSR") | TEF
            if csr & TPIE:
                csr |= TIF
            self.set("CSR", csr)
            self.emit("info", block=self.name, msg="tamper event: BKP data registers cleared")
            self._update_irq()

    def _update_irq(self) -> None:
        lvl = bool(self.get("CSR") & TIF and self.get("CSR") & TPIE)
        if lvl != self._line and self.chip is not None and self._irqn is not None:
            self._line = lvl
            self.irq(self._irqn, lvl)
