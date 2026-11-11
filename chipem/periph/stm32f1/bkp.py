"""STM32F1 backup registers (BKP, RM0008 ch. 6: DR1-DR10 / DR1-DR42, RTCCR, CR, CSR).

Verified against RM0008 Rev 21 ch. 6 (6.1-6.3 functional description, 6.4.1-6.4.5 registers).  The block is mapped at
the SVD base 0x40006C04 (the first word of the 0x40006C00 page is reserved), so DR1 is at offset 0 (RM offsets 0x04-0x28
DR1-DR10, 0x2C RTCCR, 0x30 CR, 0x34 CSR, 0x40-0xBC DR11-DR42).
* Data registers: `params.data_regs` = 10 (low/medium density, 20 bytes) or 42 (high/XL density and connectivity line,
  84 bytes; 6.2).  Backup domain: kept over system resets, power resets and Standby; cleared only by a backup domain
  reset (RCC_BDCR.BDRST, `backup_domain_reset()`) or a tamper event (6.4.1 note).
* Write access needs the BKP and PWR clocks and PWR_CR.DBP = 1 (6.1): writes without DBP are ignored (one `warn`).
* Tamper (6.3.1, CR.TPE / TPAL; pin TAMPER-RTC = PC13, not bonded on the 36-pin package): the tamper event is an edge of
  (pin at its active level AND TPE), so a pin already active when TPE is set gives an event at once, and after CTE the
  pin must go inactive (or TPE be toggled) before another event.  An event sets CSR.TEF, clears DR1-DRn and, with
  CSR.TPIE, sets TIF and asserts the TAMPER vector.  While TEF = 1 the DRx are held in reset: writes are not stored
  (6.4.4 TEF note).  CTE / CTI (write 1, read 0) clear TEF / TIF; clearing TPIE also clears TIF.  TPIE and TIF are reset
  by a system reset and the Standby exit (6.4.4); TEF, CR and RTCCR stay (backup domain).
* RTCCR: CAL (calibration, stored; the RTC model does not skip pulses), CCO (RTCCLK/64 on the tamper pin), ASOE / ASOS
  (alarm or second pulse on the tamper pin): stored, the pin is not driven.  ASOE/ASOS reset only by a backup domain
  reset (6.4.2).
Not modelled: the tamper pin's GPIO ownership while TPE / CCO / ASOE use it, the "TAMPER interrupt does not wake the
core from low-power modes" rule beyond the generic WFI behaviour, half-word/word-only access (6.4).
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
        ov: dict[str, dict] = {f"DR{k}": {"on_write": self._dr_write, "ro_mask": 0xFFFF0000}
                               for k in range(1, self.n_data + 1)}
        ov["RTCCR"] = {"on_write": self._prot, "ro_mask": 0xFFFFFC00}
        ov["CR"] = {"on_write": self._cr_write, "ro_mask": 0xFFFFFFFC}
        ov["CSR"] = {"on_write": self._csr_write, "ro_mask": 0xFFFFFFF8}
        r = build_regs((name, "BKP"), size, ov, params.get("regs_file"), skip=skip)
        super().__init__(name, base, size, r, log=log, params=params)
        self._powered = False
        self._warned: set[str] = set()
        self._pwr: Any = None
        self._irqn: int | None = None
        self._tamper_pin: Any = None
        self._line = False
        self._tamp_sig = False             # (pin active AND TPE): an event is its rising edge
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
        """System reset / Standby exit: the backup domain keeps its content; CSR.TPIE/TIF are reset (6.4.4)."""
        keep = bytes(self.mem) if self._powered else None
        super().reset()
        if keep is not None:
            self.mem[:] = keep
            self.set("CSR", self.get("CSR") & ~(TPIE | TIF))
        self._powered = True
        self._warned.clear()
        self._line = False
        self._update_irq(force=True)

    def backup_domain_reset(self) -> None:
        MMIOBlock.reset(self)
        self._tamp_sig = False
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

    def _dr_write(self, blk, reg, old, new):
        x = self._prot(blk, reg, old, new)
        if x is not None:
            return x
        if self.get("CSR") & TEF:
            if "tef" not in self._warned:
                self._warned.add("tef")
                self.emit("warn", block=self.name, msg=f"BKP_{reg.name} write ignored: the data registers are held "
                          f"in reset while BKP_CSR.TEF = 1 (clear it with CTE, RM0008 6.4.4)")
            return 0
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
        v = (old & (TEF | TIF)) | (new & TPIE)
        if new & CTE:
            v &= ~TEF
        if new & CTI or not new & TPIE:
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
        active = False
        if pin is not None and cr & TPE:
            lvl = pin.level()
            active = (lvl == "L") if cr & TPAL else (lvl == "H")
        rising = active and not self._tamp_sig
        self._tamp_sig = active
        if not rising:
            return
        self.tamper_events += 1
        for k in range(1, self.n_data + 1):
            self.set(f"DR{k}", 0)
        csr = self.get("CSR") | TEF
        if csr & TPIE:
            csr |= TIF
        self.set("CSR", csr)
        self.emit("info", block=self.name, msg="tamper event: BKP data registers cleared")
        self._update_irq()

    def _update_irq(self, force: bool = False) -> None:
        lvl = bool(self.get("CSR") & TIF and self.get("CSR") & TPIE)
        if (lvl != self._line or force) and self.chip is not None and self._irqn is not None:
            self._line = lvl
            self.irq(self._irqn, lvl)
