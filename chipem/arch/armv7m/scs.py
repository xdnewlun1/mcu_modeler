"""System Control Space (0xE000E000-0xE000EFFF): SysTick, NVIC, SCB, FPU control registers.

Registered as peripheral model `armv7m.scs`; normally created by ArmV7M (chip.arch.scs) and mapped by the Chip.
DWT (0xE0001000) and ITM (0xE0000000) are not part of this block.

SysTick counts core cycles through the Scheduler (one event per wrap).  CLKSOURCE=1 counts at the core clock;
CLKSOURCE=0 counts at the implementation-defined external reference, modelled as core clock / `systick_ext_div`
(chip YAML `systick_ext_div`, default 8 = STM32 HCLK/8).  `systick_ext_div: 0` means the chip has no external
reference (Kinetis K22, RM section 3.2.1.2): SYST_CSR.CLKSOURCE reads as 1 and ignores writes, and SYST_CALIB reads
0x80000000 (NOREF=1, SKEW=0, TENMS=0).  CVR reads mid-slice are instruction-exact
(clock.cycles + core.insns_in_slice()).
"""
from __future__ import annotations

from typing import Any

from ...periph.base import MMIOBlock, Reg
from ...periph.registry import register

SCS_BASE = 0xE000E000
SCS_SIZE = 0x1000

CSR_ENABLE, CSR_TICKINT, CSR_CLKSOURCE, CSR_COUNTFLAG = 1, 2, 4, 1 << 16

ICSR_NMIPENDSET = 1 << 31
ICSR_PENDSVSET, ICSR_PENDSVCLR = 1 << 28, 1 << 27
ICSR_PENDSTSET, ICSR_PENDSTCLR = 1 << 26, 1 << 25
ICSR_ISRPENDING, ICSR_RETTOBASE = 1 << 22, 1 << 11


@register("armv7m.scs")
class SCSBlock(MMIOBlock):
    def __init__(self, arch: Any, *, cpuid: int = 0x410FC241, base: int = SCS_BASE, size: int = SCS_SIZE,
                 params: dict | None = None):
        if not hasattr(arch, "set_pending_exc"):     # constructed through the registry: (name, base, size)
            raise TypeError("armv7m.scs is created by the ArmV7M arch, not from the peripheral list")
        super().__init__("SCS", base, size, ram_like=True, params=params)
        self.arch = arch
        self._st_ev = None
        self._st_next: int | None = None
        self.ext_div = 8                 # CLKSOURCE=0: core cycles per SysTick count (set from the chip in attach)
        nw = (arch.num_irqs + 31) // 32
        self.nwords = nw
        R = self.add_reg
        # --- SysTick
        R(Reg("ICTR", 0x004, reset=max(nw - 1, 0), ro_mask=0xFFFFFFFF))
        R(Reg("ACTLR", 0x008))
        R(Reg("SYST_CSR", 0x010, ro_mask=CSR_COUNTFLAG, on_read=self._csr_read, on_write=self._csr_write))
        R(Reg("SYST_RVR", 0x014, on_write=self._rvr_write))
        R(Reg("SYST_CVR", 0x018, on_read=self._cvr_read, on_write=self._cvr_write))
        R(Reg("SYST_CALIB", 0x01C, reset=0x40000000, ro_mask=0xFFFFFFFF))
        # --- NVIC
        for k in range(nw):
            R(Reg(f"NVIC_ISER{k}", 0x100 + 4 * k, on_read=self._en_read(k), on_write=self._iser_write(k, True)))
            R(Reg(f"NVIC_ICER{k}", 0x180 + 4 * k, on_read=self._en_read(k), on_write=self._iser_write(k, False)))
            R(Reg(f"NVIC_ISPR{k}", 0x200 + 4 * k, on_read=self._pend_read(k), on_write=self._ispr_write(k, True)))
            R(Reg(f"NVIC_ICPR{k}", 0x280 + 4 * k, on_read=self._pend_read(k), on_write=self._ispr_write(k, False)))
            R(Reg(f"NVIC_IABR{k}", 0x300 + 4 * k, ro_mask=0xFFFFFFFF, on_read=self._act_read(k)))
        for i in range(0, arch.num_irqs, 4):
            R(Reg(f"NVIC_IPR{i // 4}", 0x400 + i, on_write=self._ipr_write(16 + i)))
        # --- SCB
        R(Reg("CPUID", 0xD00, reset=cpuid, ro_mask=0xFFFFFFFF))
        R(Reg("ICSR", 0xD04, on_read=self._icsr_read, on_write=self._icsr_write))
        R(Reg("VTOR", 0xD08, on_read=lambda b, r: self.arch.vtor, on_write=self._vtor_write))   # reads track arch.vtor (reset value = reset_vtor)
        R(Reg("AIRCR", 0xD0C, on_read=self._aircr_read, on_write=self._aircr_write))
        R(Reg("SCR", 0xD10))
        R(Reg("CCR", 0xD14, reset=0x200, ro_mask=0xFFFFFFFF if arch.v6m else 0, on_write=self._ccr_write))
        R(Reg("SHPR1", 0xD18, on_write=self._ipr_write(4)))
        R(Reg("SHPR2", 0xD1C, on_write=self._ipr_write(8)))
        R(Reg("SHPR3", 0xD20, on_write=self._ipr_write(12)))
        R(Reg("SHCSR", 0xD24))
        R(Reg("CFSR", 0xD28, w1c_mask=0xFFFFFFFF))
        R(Reg("HFSR", 0xD2C, w1c_mask=0xFFFFFFFF))
        R(Reg("DFSR", 0xD30, w1c_mask=0x1F))
        R(Reg("MMFAR", 0xD34))
        R(Reg("BFAR", 0xD38))
        R(Reg("AFSR", 0xD3C))
        R(Reg("CPACR", 0xD88))
        R(Reg("MPU_TYPE", 0xD90, ro_mask=0xFFFFFFFF))
        R(Reg("STIR", 0xF00, on_write=self._stir_write))
        R(Reg("FPCCR", 0xF34, reset=0xC0000000, on_write=self._fpccr_write))
        R(Reg("FPCAR", 0xF38))
        R(Reg("FPDSCR", 0xF3C))

    # ------------------------------------------------------------------ lifecycle
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        desc = getattr(chip, "desc", None)
        extra = getattr(desc, "extra", None) or {}
        div = int((self.params or {}).get("systick_ext_div", extra.get("systick_ext_div", 8)))
        csr = self.regs["SYST_CSR"]
        if div <= 0:                     # no external reference: CLKSOURCE fixed at 1, NOREF=1, TENMS=0
            self.ext_div = 1
            csr._raw = (CSR_CLKSOURCE, CSR_COUNTFLAG | CSR_CLKSOURCE, 0)
            calib = 0x80000000
        else:
            self.ext_div = div
            csr._raw = (0, CSR_COUNTFLAG, 0)
            # TENMS: reload for 10 ms of the CLKSOURCE=0 reference, which runs at core/div. It is a fixed per-product
            # value (PM0214: "HCLK max/8"), so use the nominal (maximum) core clock of the chip description, not
            # the clock at attach time; `systick_calib` (chip YAML) overrides the whole register.
            fixed = (self.params or {}).get("systick_calib", extra.get("systick_calib"))
            if fixed is not None:
                calib = int(fixed, 0) if isinstance(fixed, str) else int(fixed)
            else:
                core = getattr(desc, "core", None)
                hz = getattr(core, "clock_hz", 0) or getattr(chip.clock, "hz", 0) or 0
                calib = 0x40000000 | (max(int(hz / div / 100) - 1, 0) & 0xFFFFFF)
        csr.set_unit_bits(csr.unit_bits)
        self.set("SYST_CSR", csr.reset)
        self.regs["SYST_CALIB"].reset = calib
        self.set("SYST_CALIB", calib)

    def reset(self) -> None:
        self._st_stop()
        super().reset()

    # convenience properties used by the exception model
    @property
    def cfsr(self) -> int:
        return self.get("CFSR")

    @cfsr.setter
    def cfsr(self, v: int) -> None:
        self.set("CFSR", v)

    @property
    def hfsr(self) -> int:
        return self.get("HFSR")

    @hfsr.setter
    def hfsr(self, v: int) -> None:
        self.set("HFSR", v)

    @property
    def bfar(self) -> int:
        return self.get("BFAR")

    @bfar.setter
    def bfar(self, v: int) -> None:
        self.set("BFAR", v)

    # ------------------------------------------------------------------ SysTick
    def _now(self) -> int:
        chip = self.chip
        return chip.clock.cycles + chip.core.insns_in_slice()

    def _st_stop(self) -> None:
        if self._st_ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._st_ev)
        self._st_ev = None
        self._st_next = None

    def _st_schedule(self, when: int) -> None:
        self._st_next = when
        self._st_ev = self.chip.sched.at_cycles(when, self._st_wrap)

    def _div(self) -> int:
        """Core cycles per SysTick count: 1 with CLKSOURCE=1 (processor clock), else the external reference."""
        return 1 if self.get("SYST_CSR") & CSR_CLKSOURCE else self.ext_div

    def _st_start(self, cvr: int) -> None:
        self._st_stop()
        rvr = self.get("SYST_RVR") & 0xFFFFFF
        if cvr == 0:
            if rvr == 0:
                return
            cvr = rvr + 1          # next clock reloads RVR, then counts down to 0
        self._st_schedule(self._now() + cvr * self._div())

    def _st_wrap(self) -> None:
        r = self.regs["SYST_CSR"]
        csr = self.get("SYST_CSR") | CSR_COUNTFLAG
        self._store(r, csr)
        when = self._st_next
        self._st_ev = None
        self._st_next = None
        rvr = self.get("SYST_RVR") & 0xFFFFFF
        if rvr:
            self._st_schedule(when + (rvr + 1) * self._div())
        if csr & CSR_TICKINT:
            self.arch.set_pending_exc(15)

    def live_cvr(self) -> int:
        if self._st_next is None:
            return self.get("SYST_CVR") & 0xFFFFFF if not (self.get("SYST_CSR") & CSR_ENABLE) else 0
        rvr = self.get("SYST_RVR") & 0xFFFFFF
        div = self._div()
        c = -(-(self._st_next - self._now()) // div)          # counts left (rounded up)
        if c <= 0:
            return 0
        if c > rvr:
            c %= (rvr + 1)
        return c

    def _csr_read(self, blk, reg) -> int:
        v = self.get("SYST_CSR")
        if v & CSR_COUNTFLAG:
            self._store(reg, v & ~CSR_COUNTFLAG)
        return v

    def _csr_write(self, blk, reg, old, new):
        if (new ^ old) & CSR_CLKSOURCE and new & old & CSR_ENABLE and self._st_next is not None:
            # source switch while running: keep the current count, continue at the new rate
            self._store(reg, old)
            cvr = self.live_cvr()
            self._store(reg, new)
            self._st_start(cvr)
            return None
        if (new ^ old) & CSR_ENABLE:
            if new & CSR_ENABLE:
                self._st_start(self.get("SYST_CVR") & 0xFFFFFF)
            else:
                self.set("SYST_CVR", self.live_cvr())
                self._st_stop()
        return None

    def _rvr_write(self, blk, reg, old, new):
        new &= 0xFFFFFF
        if self.get("SYST_CSR") & CSR_ENABLE and self._st_next is None and new:
            self._st_start(0)
        return new

    def _cvr_read(self, blk, reg) -> int:
        return self.live_cvr()

    def _cvr_write(self, blk, reg, old, new):
        csr = self.get("SYST_CSR")
        self._store(self.regs["SYST_CSR"], csr & ~CSR_COUNTFLAG)
        if csr & CSR_ENABLE:
            self._st_start(0)
        return 0

    # ------------------------------------------------------------------ NVIC
    def _en_read(self, k: int):
        def rd(blk, reg, _k=k):
            en = self.arch.enabled
            base = 16 + 32 * _k
            return sum(1 << i for i in range(32) if base + i in en)
        return rd

    def _pend_read(self, k: int):
        def rd(blk, reg, _k=k):
            pend = self.arch.pending
            base = 16 + 32 * _k
            return sum(1 << i for i in range(32) if base + i in pend)
        return rd

    def _act_read(self, k: int):
        def rd(blk, reg, _k=k):
            base = 16 + 32 * _k
            return sum(1 << (a - base) for a in self.arch.active if base <= a < base + 32)
        return rd

    def _iser_write(self, k: int, enable: bool):
        def wr(blk, reg, old, new, _k=k):
            arch = self.arch
            i = 0
            while new:
                if new & 1:
                    irq = 32 * _k + i
                    if irq < arch.num_irqs:
                        arch.enable_irq(irq, enable)
                new >>= 1
                i += 1
            return 0
        return wr

    def _ispr_write(self, k: int, pend: bool):
        def wr(blk, reg, old, new, _k=k):
            arch = self.arch
            i = 0
            while new:
                if new & 1:
                    irq = 32 * _k + i
                    if irq < arch.num_irqs:
                        if pend:
                            arch.set_pending_exc(16 + irq)       # software pend: does not assert the line
                        else:
                            arch.clear_pending_exc(16 + irq)     # re-pends if the line is still asserted
                new >>= 1
                i += 1
            return 0
        return wr

    def _ipr_write(self, first_exc: int):
        def wr(blk, reg, old, new, _e=first_exc):
            arch = self.arch
            m = arch.prio_mask
            out = 0
            for b in range(4):
                exc = _e + b
                if exc < 4 or exc >= arch.nexc or exc in (7, 8, 9, 10, 13):
                    continue
                if arch.v6m and exc < 11:
                    continue
                p = (new >> (8 * b)) & m
                arch.set_priority(exc, p)
                out |= p << (8 * b)
            return out
        return wr

    def _stir_write(self, blk, reg, old, new):
        irq = new & 0x1FF
        if irq < self.arch.num_irqs:
            self.arch.set_pending_exc(16 + irq)
        return 0

    # ------------------------------------------------------------------ SCB
    def _icsr_read(self, blk, reg) -> int:
        arch = self.arch
        pend = arch.pending
        v = arch.active[-1] if arch.active else 0
        if len(arch.active) <= 1:
            v |= ICSR_RETTOBASE
        b = arch.best_pending()
        if b is not None:
            v |= (b & 0x1FF) << 12
        if any(e >= 16 for e in pend):
            v |= ICSR_ISRPENDING
        if 15 in pend:
            v |= ICSR_PENDSTSET
        if 14 in pend:
            v |= ICSR_PENDSVSET
        if 2 in pend:
            v |= ICSR_NMIPENDSET
        return v

    def _icsr_write(self, blk, reg, old, new):
        arch = self.arch
        if new & ICSR_PENDSVCLR:
            arch.clear_pending_exc(14)
        if new & ICSR_PENDSTCLR:
            arch.clear_pending_exc(15)
        if new & ICSR_NMIPENDSET:
            arch.set_pending_exc(2)
        if new & ICSR_PENDSVSET:
            arch.set_pending_exc(14)
        if new & ICSR_PENDSTSET:
            arch.set_pending_exc(15)
        return 0

    def _vtor_write(self, blk, reg, old, new):
        v = new & 0xFFFFFF80
        self.arch.vtor = v
        return v

    def _aircr_read(self, blk, reg) -> int:
        return 0xFA050000 | (self.arch.prigroup << 8)

    def _aircr_write(self, blk, reg, old, new):
        arch = self.arch
        if (new >> 16) != 0x05FA:
            return 0
        if not arch.v6m:
            arch.set_prigroup((new >> 8) & 7)
        if new & 2:
            arch.active.clear()
        if new & 4:
            arch.request_reset("SYSRESETREQ")
        elif new & 1 and not arch.v6m:
            arch.request_reset("VECTRESET")
        return 0

    def _ccr_write(self, blk, reg, old, new):
        self.arch.stkalign = bool(new & 0x200)
        return None

    def _fpccr_write(self, blk, reg, old, new):
        self.arch.aspen = bool(new & (1 << 31))
        return None
