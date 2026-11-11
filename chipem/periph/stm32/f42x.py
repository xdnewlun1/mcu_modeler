"""STM32F42xxx/43xxx system blocks: RCC (PLLSAI, DCKCFGR, TIMPRE), PWR (over-drive, under-drive, VOS scales) and the
dual-bank FLASH interface. Subclasses of the STM32F405/407 models (rcc.py, system.py); the F405 behaviour is kept and
only the F42x additions of RM0090 Rev 22 are layered on top.

stm32.rcc_f42x (RM0090 ch. 6 "Reset and clock control for STM32F42xxx and STM32F43xxx"):
* PLLSAI (6.3.24, RCC_PLLSAICFGR 0x88, reset 0x24003000): VCO = PLL input / PLLM x PLLSAIN, PLLSAI_Q = VCO / PLLSAIQ,
  PLLSAI_R = VCO / PLLSAIR (LCD, F429/F439 only). CR.PLLSAION (bit 28) -> CR.PLLSAIRDY (bit 29) at once, like the
  other PLLs of the model (no lock time). PLLI2S_Q = PLLI2S VCO / PLLI2SQ (6.3.23, PLLI2SCFGR bits 27:24).
* DCKCFGR (6.3.25, 0x8C): PLLI2SDIVQ / PLLSAIDIVQ (/1../32), SAI1ASRC / SAI1BSRC (00 PLLSAI_Q / PLLSAIDIVQ,
  01 PLLI2S_Q / PLLI2SDIVQ, 10 I2S_CKIN pin `params.i2s_ckin_hz`, 11 wrong configuration = 0 Hz + `warn`),
  `sai_clk_hz("A"|"B")`; TIMPRE: timers on APBx run at HCLK when PPREx is /1, /2 or /4, else at 4 x PCLKx.
  `listeners` are also called when a SAI kernel clock changes.
* STM32F427.svd lacks RCC_PLLSAICFGR / RCC_DCKCFGR and several enable bits (CR PLLSAION, AHB1ENR GPIOJ/K, CCMDATARAM,
  DMA2D, APB2ENR SAI1, matching reset / low-power bits): the registers are added here and the bits made writable
  (the generated table chips/stm32f427_regs.yaml is not edited).
* Frequency limits (DS9405 Table 17): HCLK 180 MHz in scale 1 with over-drive active (PWR_CSR.ODSWRDY), 168 MHz in
  scale 1 without / scale 2 with over-drive, 144 MHz scale 2, 120 MHz scale 3; APB1 45 / APB2 90 MHz with over-drive,
  42 / 84 MHz without. Exceeding them logs a `warn` (silicon is out of spec, the model keeps running).
* Stop / Standby entry also clear PLLSAION (6.3.1).

stm32.pwr_f42x (RM0090 5.1.4 "Entering/Exiting Over-drive mode", 5.5.1 PWR_CR, 5.5.2 PWR_CSR for STM32F42xxx/43xxx):
* CR reset 0x0000C000 (VOS = 11 scale 1); VOS[1:0] (00 = scale 3 too) is writable only while the main PLL is off
  (`warn` and ignored otherwise).
* ODEN / ODSWEN may change only while SYSCLK is HSI or HSE (`warn` + ignored otherwise). ODEN = 1 sets CSR.ODRDY after
  `od_ready_us` (default 20 us: neither RM0090 nor DS9405 gives the over-drive regulator ready time, an assumption);
  ODSWEN = 1 is accepted only with ODRDY = 1, then CSR.ODSWRDY is set after the switch time Tod_swen (DS9405 Table 23:
  45 us with HSI, 45..100 us with an HSE crystal (100 us at 4 MHz, 45 us at 26 MHz, interpolated), 40 us with an
  HSE bypass clock). ODSWEN = 0 (with or without ODEN) clears ODSWRDY after Tod_swdis (20 us HSI, 80..20 us HSE,
  15 us bypass); ODEN = 0 clears ODSWEN by hardware and ODRDY. The system clock stall during the switch is not modelled.
* Stop entry clears ODEN, ODSWEN, ODRDY, ODSWRDY (5.3.4: "Entering Stop mode disables the Over-drive mode") and, when
  UDEN = 11 and the regulator used in Stop has its under-drive bit (LPDS = 1: LPUDS; LPDS = 0: MRUDS), sets
  CSR.UDRDY = 11 (write 1 to clear, 5.5.2).
* `od_active()` / `vos_scale()` for the RCC frequency limits.

stm32.flash_f42x (RM0090 3.5.1/3.9, Tables 6-9):
* Sectors: 2 MB parts: bank 1 sectors 0-11 (4 x 16 KB, 64 KB, 7 x 128 KB) at 0x08000000, bank 2 sectors 12-23 with the
  same layout at 0x08100000. 1 MB parts: single bank sectors 0-11 (OPTCR.DB1M = 0), or with DB1M = 1 two 512 KB banks:
  sectors 0-7 and 12-19 (bank 2 at 0x08080000). CR.SNB[4:0] encodes sector 12+ as 0b1xxxx (16 = sector 12, 27 = 23);
  the "not allowed" codes (12-15, 28-31), sectors beyond the part and, with DB1M = 1, sectors 8-11 (3.9.10: "the
  erase operation on Bank 2 sector is not performed") erase nothing (`warn`).
* CR.MER erases bank 1, CR.MER1 bank 2, both = mass erase (3.6.3); MERx with SER = mass erase.
* Option bytes: OPTKEYR 0x08192A3B, 0x4C5D6E7F clears OPTCR.OPTLOCK (wrong key: locked until reset); OPTCR/OPTCR1
  writes are ignored while OPTLOCK = 1; OPTSTRT "programs" the option bytes (kept across system resets, like flash);
  the option bits (DB1M, nWRP ...) are loaded at reset release (3.9.10), so DB1M takes effect on the next reset.
  Write protection (nWRP) and read protection (RDP) are stored but not enforced.
"""
from __future__ import annotations

from typing import Any

from ..base import Reg
from ..registry import register
from ._util import rcc as rcc_of
from .rcc import HSERDY, HSIRDY, PLLRDY, RCC
from .system import FLASH, PWR

PLLSAION, PLLSAIRDY = 1 << 28, 1 << 29
CR_HSEBYP = 1 << 18
TIMPRE = 1 << 24

# bits that the STM32F427.svd leaves out (reserved -> read-only in the generated table) but RM0090 6.3 defines
_EXTRA_RW = {
    "CR": PLLSAION,
    "AHB1RSTR": (1 << 9) | (1 << 10) | (1 << 23),                         # GPIOJ/K, DMA2D
    "AHB1ENR": (1 << 9) | (1 << 10) | (1 << 20) | (1 << 23),              # GPIOJ/K, CCMDATARAM, DMA2D
    "AHB2ENR": 0x30, "AHB2RSTR": 0x30, "AHB2LPENR": 0x30,                 # CRYP/HASH (F437/F439)
    "APB1RSTR": (1 << 30) | (1 << 31), "APB1ENR": (1 << 30) | (1 << 31),
    "APB2RSTR": (1 << 13) | (1 << 20) | (1 << 21) | (1 << 22),            # SPI4, SPI5, SPI6, SAI1
    "APB2ENR": (1 << 13) | (1 << 20) | (1 << 21) | (1 << 22),
    "PLLI2SCFGR": 0xF << 24,                                              # PLLI2SQ
}


@register("stm32.rcc_f42x")
class RCCF42x(RCC):
    model = "stm32.rcc_f42x"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, regs, params=params, log=log)
        if "PLLSAICFGR" not in self.regs:          # RM0090 6.3.24 (missing from STM32F427.svd)
            self.add_reg(Reg("PLLSAICFGR", 0x88, 4, reset=0x24003000, ro_mask=0x80FF803F, on_write=self._ctl_write))
        else:
            self.regs["PLLSAICFGR"].on_write = self._ctl_write
        if "DCKCFGR" not in self.regs:             # RM0090 6.3.25
            self.add_reg(Reg("DCKCFGR", 0x8C, 4, reset=0, ro_mask=0xFE0CE0E0, on_write=self._ctl_write))
        else:
            self.regs["DCKCFGR"].on_write = self._ctl_write
        for rn, bits in _EXTRA_RW.items():
            if rn in self.regs:
                self.regs[rn].ro_mask &= ~bits
        # reset values the SVD gets wrong for the F42x: PLLI2SQ = 4 (6.3.23); the low-power enables of the F42x
        # blocks (6.3.15 / 6.3.18 / 6.3.19: the SVD has the F405 values, so UART7/8, SPI4-6, SAI1, DMA2D, GPIOJ/K
        # would be clock-gated in Sleep mode); the writable LPENR bits are exactly the ones set at reset
        for rn, reset, ro in (("PLLI2SCFGR", 0x24003000, None), ("AHB1LPENR", 0x7EEF97FF, ~0x7EEF97FF),
                              ("APB1LPENR", 0xF6FEC9FF, ~0xF6FEC9FF), ("APB2LPENR", 0x04777F33, ~0x04777F33)):
            pr = self.regs.get(rn)
            if pr is None:
                continue
            pr._raw = (reset, pr.ro_mask if ro is None else ro & 0xFFFFFFFF, pr.w1c_mask)
            pr.set_unit_bits(pr.unit_bits)
            self.mem[pr.boff:pr.boff + 4] = pr.reset.to_bytes(4, "little")
        self._sai: tuple = (0.0, 0.0, False)
        self._pwr: Any = None

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        from ._util import find_model
        self._pwr = find_model(chip, "stm32.pwr_f42x", "PWR")

    # ------------------------------------------------------------ registers
    def _ctl_write(self, blk, reg, old, new):
        if reg.name == "CR":
            new = (new & ~PLLSAIRDY) | (PLLSAIRDY if new & PLLSAION else 0)
            self._store(reg, new)
            super()._ctl_write(blk, reg, old, new)          # HSI/HSE/PLL/PLLI2S ready bits, SYSCLK protection
            v = self.get("CR")
            v = (v & ~PLLSAIRDY) | (PLLSAIRDY if v & PLLSAION else 0)
            self._store(reg, v)
            if old != v:
                self.update()
            return None
        if old != new:
            self.update()
        return None

    # ------------------------------------------------------------ derived clocks
    def _vco_in(self) -> float:
        m = self._f("PLLCFGR", 0, 6)
        return self.pll_in_hz() / m if m >= 2 else 0.0

    def pllsai_q_hz(self) -> float:
        """PLLSAI_Q = PLL input / PLLM x PLLSAIN / PLLSAIQ (0 while PLLSAI is off or misconfigured)."""
        if not self.get("CR") & PLLSAIRDY:
            return 0.0
        n, q = self._f("PLLSAICFGR", 6, 9), self._f("PLLSAICFGR", 24, 4)
        if n < 2 or q < 2:
            return 0.0
        vco = self._vco_in() * n
        for key, bad, what in (("saivco", not 100e6 <= vco <= 432e6, f"PLLSAI VCO {vco / 1e6:g} MHz (allowed "
                                                                       "100..432 MHz)"),
                               ("sain", not 50 <= n <= 432, f"PLLSAIN = {n} (allowed 50..432)")):
            if bad and key not in self._warned:
                self._warned.add(key)
                self.emit("warn", block=self.name, msg=f"RCC: {what} is a wrong configuration (RM0090 6.3.24)")
        return vco / q

    def plli2s_q_hz(self) -> float:
        """PLLI2S_Q = PLL input / PLLM x PLLI2SN / PLLI2SQ (RM0090 6.3.23)."""
        if not self.get("CR") & (1 << 27):
            return 0.0
        n, q = self._f("PLLI2SCFGR", 6, 9), self._f("PLLI2SCFGR", 24, 4)
        if n < 2 or q < 2:
            return 0.0
        return self._vco_in() * n / q

    def sai_clk_hz(self, block: str = "A") -> float:
        """SAI1 block A/B kernel clock selected by DCKCFGR.SAI1ASRC/SAI1BSRC (RM0090 6.3.25)."""
        d = self.get("DCKCFGR")
        src = (d >> (20 if str(block).upper() == "A" else 22)) & 3
        if src == 0:
            return self.pllsai_q_hz() / (((d >> 8) & 0x1F) + 1)
        if src == 1:
            return self.plli2s_q_hz() / ((d & 0x1F) + 1)
        if src == 2:
            return self.i2s_ckin_hz
        if "saisrc" not in self._warned:
            self._warned.add("saisrc")
            self.emit("warn", block=self.name, msg=f"RCC_DCKCFGR.SAI1{block}SRC = 11 is a wrong configuration "
                      "(RM0090 6.3.25): no SAI clock")
        return 0.0

    def timer_hz(self, bus: int) -> float:
        if not self.get("DCKCFGR") & TIMPRE:
            return super().timer_hz(bus)
        apb = self.apb1_hz if bus == 1 else self.apb2_hz
        return self.core_hz if self.ppre(bus) in (1, 2, 4) else 4 * apb

    def effective_timer_hz(self, bus: int) -> float:
        if not self.get("DCKCFGR") & TIMPRE:
            return super().effective_timer_hz(bus)
        hz = self.chip.clock.hz if self.chip is not None else self.core_hz
        return hz if self.ppre(bus) in (1, 2, 4) else 4 * hz / self.ppre(bus)

    def clock_limits(self) -> tuple[float, float, float, str]:
        pwr = self._pwr
        if pwr is None:
            return 180e6, 45e6, 90e6, ""
        od = pwr.od_active()
        scale = pwr.vos_scale()
        hmax = {1: 180e6 if od else 168e6, 2: 168e6 if od else 144e6, 3: 120e6}[scale]
        a1, a2 = (45e6, 90e6) if od else (42e6, 84e6)
        return hmax, a1, a2, (f" (voltage scale {scale}, over-drive {'on' if od else 'off'}; DS9405 Table 17)")

    # ------------------------------------------------------------ update
    def update(self) -> None:
        super().update()
        sai = (self.sai_clk_hz("A"), self.sai_clk_hz("B"), bool(self.get("DCKCFGR") & TIMPRE))
        if sai != self._sai:                    # SAI kernel clocks or TIMPRE (timer clocks) changed
            self._sai = sai
            for cb in list(self.listeners):
                cb()

    def stop_plls(self) -> None:
        """Stop/Standby entry (6.3.1): PLLSAION and PLLSAIRDY are cleared by hardware."""
        cr = self.get("CR")
        if cr & (PLLSAION | PLLSAIRDY):
            self.set("CR", cr & ~(PLLSAION | PLLSAIRDY))
            self.update()


# ---------------------------------------------------------------------------------------------------------- PWR
CR_ODEN, CR_ODSWEN, CR_UDEN = 1 << 16, 1 << 17, 3 << 18
CR_LPDS, CR_LPUDS, CR_MRUDS = 1 << 0, 1 << 10, 1 << 11
CSR_ODRDY, CSR_ODSWRDY, CSR_UDRDY = 1 << 16, 1 << 17, 3 << 18


@register("stm32.pwr_f42x")
class PWRF42x(PWR):
    model = "stm32.pwr_f42x"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, regs, params=params, log=log)
        params = dict(params or {})
        cr = self.regs["CR"]
        cr._raw = (0xC000, 0xFFF01000, 0)                  # VOS = 11 (scale 1); bits 31:20 and 12 reserved
        cr.set_unit_bits(cr.unit_bits)
        cr.on_write = self._cr_write_f42x
        csr = self.regs["CSR"]
        csr.ro_mask &= ~CSR_UDRDY
        csr.w1c_mask |= CSR_UDRDY                          # UDRDY: rc_w1 (5.5.2)
        self.mem[cr.boff:cr.boff + 4] = (0xC000).to_bytes(4, "little")
        self.od_ready_ns = float(params.get("od_ready_us", 20.0)) * 1e3
        self._ev = None

    def reset(self) -> None:
        self._cancel()
        super().reset()
        self.set("CSR", self.get("CSR") & ~(CSR_ODRDY | CSR_ODSWRDY))

    def _cancel(self) -> None:
        if self._ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None

    # ------------------------------------------------------------ state for the RCC limits
    def od_active(self) -> bool:
        return bool(self.get("CSR") & CSR_ODSWRDY)

    def vos_scale(self) -> int:
        """Effective regulator scale (1..3). With the main PLL off the scale is 3 (5.1.4); VOS 00 = scale 3."""
        r = rcc_of(self.chip) if self.chip is not None else None
        if r is not None and not r.get("CR") & PLLRDY:
            return 3
        return {3: 1, 2: 2}.get((self.get("CR") >> 14) & 3, 3)

    # ------------------------------------------------------------ registers
    def _sysclk_is_osc(self) -> bool:
        r = rcc_of(self.chip) if self.chip is not None else None
        return r is None or ((r.get("CFGR") >> 2) & 3) in (0, 1)

    def _switch_ns(self, enable: bool) -> float:
        """Tod_swen / Tod_swdis, DS9405 Table 23 (typical; HSE crystal interpolated between 4 and 26 MHz)."""
        r = rcc_of(self.chip) if self.chip is not None else None
        hse = r is not None and ((r.get("CFGR") >> 2) & 3) == 1
        if not hse:
            return 45e3 if enable else 20e3
        if r.get("CR") & CR_HSEBYP:
            return 40e3 if enable else 15e3
        f = min(max(r.ext_hz, 4e6), 26e6)
        lo, hi = (45e3, 100e3) if enable else (20e3, 80e3)
        return hi - (hi - lo) * (f - 4e6) / 22e6

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._notes:
            self._notes.add(key)
            self.emit("warn", block=self.name, msg=msg)

    def _cr_write_f42x(self, blk, reg, old, new):
        r = rcc_of(self.chip) if self.chip is not None else None
        if (old ^ new) & (3 << 14) and r is not None and r.get("CR") & (1 << 24):
            new = (new & ~(3 << 14)) | (old & (3 << 14))
            self._warn_once("vos", "PWR_CR.VOS write ignored: the scale can be changed only while the main PLL is "
                            "off (RM0090 5.5.1)")
        if (old ^ new) & (CR_ODEN | CR_ODSWEN) and not self._sysclk_is_osc():
            new = (new & ~(CR_ODEN | CR_ODSWEN)) | (old & (CR_ODEN | CR_ODSWEN))
            self._warn_once("od_sw", "PWR_CR.ODEN/ODSWEN write ignored: HSI or HSE must be the system clock "
                            "(RM0090 5.5.1)")
        if not new & CR_ODEN:
            new &= ~CR_ODSWEN                                 # ODSWEN cleared by hardware with ODEN
        csr = self.get("CSR")
        if new & ~old & CR_ODSWEN and not csr & CSR_ODRDY:
            new &= ~CR_ODSWEN
            self._warn_once("odswen", "PWR_CR.ODSWEN set before CSR.ODRDY: ignored (RM0090 5.5.1: wait for ODRDY "
                            "first)")
        new = super()._cr_write(blk, reg, old, new)          # CWUF/CSBF, PVD; stores and returns the value
        if (old ^ new) & (CR_ODEN | CR_ODSWEN):
            self._od_change(old, new)
        return new

    def _od_change(self, old: int, new: int) -> None:
        chip = self.chip
        if chip is None:
            return
        self._cancel()
        csr = self.get("CSR")
        if new & CR_ODEN and not old & CR_ODEN:
            self._ev = chip.sched.in_ns(self.od_ready_ns, self._od_ready)
        elif new & CR_ODSWEN and not old & CR_ODSWEN:
            self._ev = chip.sched.in_ns(self._switch_ns(True), self._od_switched)
        elif old & CR_ODSWEN and not new & CR_ODSWEN:
            self._ev = chip.sched.in_ns(self._switch_ns(False), lambda: self._od_off(bool(new & CR_ODEN)))
        elif old & CR_ODEN and not new & CR_ODEN:
            self.set("CSR", csr & ~CSR_ODRDY)

    def _od_ready(self) -> None:
        self._ev = None
        if self.get("CR") & CR_ODEN:
            self.set("CSR", self.get("CSR") | CSR_ODRDY)

    def _od_switched(self) -> None:
        self._ev = None
        if self.get("CR") & CR_ODSWEN:
            self.set("CSR", self.get("CSR") | CSR_ODSWRDY)
            self.emit("info", block=self.name, msg="over-drive mode active (PWR_CSR.ODSWRDY)")
            self._rcc_update()

    def _od_off(self, keep_oden: bool) -> None:
        self._ev = None
        csr = self.get("CSR") & ~CSR_ODSWRDY
        if not keep_oden:
            csr &= ~CSR_ODRDY
        self.set("CSR", csr)
        self.emit("info", block=self.name, msg="over-drive mode inactive")
        self._rcc_update()

    def _rcc_update(self) -> None:
        r = rcc_of(self.chip)
        if r is not None:
            r._logged = None                                 # re-check the frequency limits
            r.update()

    # ------------------------------------------------------------ Stop mode
    def _enter_stop(self, chip: Any) -> None:
        super()._enter_stop(chip)
        if self.mode != "stop":
            return                                           # entry ignored (pending EXTI / RTC flag)
        self._cancel()
        cr = self.get("CR")
        csr = self.get("CSR") & ~(CSR_ODRDY | CSR_ODSWRDY)
        lpds = bool(cr & CR_LPDS)
        if (cr & CR_UDEN) == CR_UDEN and cr & (CR_LPUDS if lpds else CR_MRUDS):
            csr |= CSR_UDRDY
        self.set("CSR", csr)
        self.set("CR", cr & ~(CR_ODEN | CR_ODSWEN))
        r = rcc_of(chip)
        if r is not None and hasattr(r, "stop_plls"):
            r.stop_plls()
            r._logged = None
            r.update()

    def _enter_standby(self, chip: Any) -> None:
        super()._enter_standby(chip)
        r = rcc_of(chip)
        if r is not None and hasattr(r, "stop_plls"):
            r.stop_plls()


# ---------------------------------------------------------------------------------------------------------- FLASH
OPT_KEYS = (0x08192A3B, 0x4C5D6E7F)
CR_PG, CR_SER, CR_MER, CR_MER1, CR_STRT, CR_EOPIE, CR_LOCK = 1, 2, 4, 1 << 15, 1 << 16, 1 << 24, 1 << 31
OPTLOCK, OPTSTRT, DB1M = 1, 2, 1 << 30
_BANK = [(0x0000, 0x4000), (0x4000, 0x4000), (0x8000, 0x4000), (0xC000, 0x4000), (0x10000, 0x10000)] + \
        [(0x20000 * k, 0x20000) for k in range(1, 8)]          # 12 sectors of a 1 MB bank (Table 6)


@register("stm32.flash_f42x")
class FLASHF42x(FLASH):
    model = "stm32.flash_f42x"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, regs, params=params, log=log)
        self.regs["CR"].ro_mask = 0x7CFE7C00          # 30:26, 23:17, 14:10 reserved (3.9.8); LOCK is "rs"
        self.regs["OPTKEYR"].on_write = self._optkeyr_write
        self.regs["OPTCR"].on_write = self._optcr_write
        self.regs["OPTCR"].ro_mask = 0x30000000
        if "OPTCR1" in self.regs:
            self.regs["OPTCR1"].on_write = self._optcr1_write
            self.regs["OPTCR1"].ro_mask = 0xF000FFFF
        self.option_bytes = {"OPTCR": 0x0FFFAAEC, "OPTCR1": 0x0FFF0000}   # programmed values (flash cells)
        self._opt_stage = 0
        self._opt_wrong = False

    def reset(self) -> None:
        super().reset()
        # option bits loaded from the option bytes at reset release (3.9.10); OPTLOCK set
        self.set("OPTCR", (self.option_bytes["OPTCR"] & ~OPTSTRT) | OPTLOCK)
        if "OPTCR1" in self.regs:
            self.set("OPTCR1", self.option_bytes["OPTCR1"])
        self._opt_stage = 0
        self._opt_wrong = False

    @property
    def db1m(self) -> bool:
        return bool(self.get("OPTCR") & DB1M)

    # ------------------------------------------------------------ option bytes
    def _optkeyr_write(self, blk, reg, old, new):
        if self._opt_wrong or not self.get("OPTCR") & OPTLOCK:
            return 0
        if self._opt_stage == 0 and new == OPT_KEYS[0]:
            self._opt_stage = 1
        elif self._opt_stage == 1 and new == OPT_KEYS[1]:
            self._opt_stage = 0
            self.set("OPTCR", self.get("OPTCR") & ~OPTLOCK)
        else:
            self._opt_wrong = True
            self.emit("warn", block=self.name, msg="FLASH_OPTKEYR: wrong unlock sequence; FLASH_OPTCR locked until "
                      "reset")
        return 0

    def _optcr_write(self, blk, reg, old, new):
        if old & OPTLOCK:
            self.emit("warn", block=self.name, msg="FLASH_OPTCR write while OPTLOCK = 1: ignored")
            return old
        if new & OPTSTRT:
            self.option_bytes["OPTCR"] = new & ~(OPTSTRT | OPTLOCK)
            if "OPTCR1" in self.regs:
                self.option_bytes["OPTCR1"] = self.get("OPTCR1")
            self.emit("info", block=self.name, msg=f"FLASH option bytes programmed (OPTCR 0x{new & ~3:08X}); they "
                      "take effect at the next reset", optcr=new & ~3)
            new &= ~OPTSTRT
        return new

    def _optcr1_write(self, blk, reg, old, new):
        if self.get("OPTCR") & OPTLOCK:
            return old
        return None

    # ------------------------------------------------------------ erase
    def _layout(self, size: int) -> dict[int, tuple[int, int]]:
        """sector number -> (offset, size) for this part (RM0090 Tables 6-9)."""
        if size >= 0x200000:
            return {**{i: _BANK[i] for i in range(12)}, **{12 + i: (0x100000 + o, n) for i, (o, n) in enumerate(_BANK)}}
        if size >= 0x100000 and self.db1m:
            return {**{i: _BANK[i] for i in range(8)}, **{12 + i: (0x80000 + o, n) for i, (o, n) in enumerate(_BANK[:8])}}
        return {i: s for i, s in enumerate(_BANK) if s[0] < size}

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
        if mem is not None and new & (CR_SER | CR_MER | CR_MER1):
            lay = self._layout(mem.size)
            if new & (CR_MER | CR_MER1):
                b1 = [s for s in lay if s < 12]
                b2 = [s for s in lay if s >= 12]
                secs = (b1 if new & CR_MER else []) + (b2 if new & CR_MER1 else [])
                what = {CR_MER: "bank 1 erase", CR_MER1: "bank 2 erase"}.get(new & (CR_MER | CR_MER1), "mass erase")
            else:
                snb = (new >> 3) & 0x1F
                sec = snb if snb < 12 else (snb - 4 if 16 <= snb <= 27 else None)
                secs = [sec] if sec in lay else []
                what = f"sector {sec} erase" if secs else ""
                if not secs:
                    self.emit("warn", block=self.name, msg=f"FLASH_CR.SNB = {snb}: no such sector on this part"
                              f"{' with DB1M = 1' if self.db1m else ''} (RM0090 3.9.8 / Table 7): nothing erased")
            for s in secs:
                off, n = lay[s]
                n = max(0, min(n, mem.size - off))
                if n:
                    self.chip.core.write_mem(mem.base + off, b"\xFF" * n)
            flush = getattr(self.chip.core, "flush_code_cache", None)
            if flush is not None:
                flush()
            if what:
                self.emit("info", block=self.name, msg=f"FLASH {what}")
        if new & CR_EOPIE:
            self.set("SR", self.get("SR") | 1)
        return new
