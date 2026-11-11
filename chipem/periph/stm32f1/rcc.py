"""STM32F1 RCC (reset and clock control, RM0008 ch. 7, low/medium/high-density layout; not the connectivity line).

Written from the STM32F103x4/x6 data sheet (DS15060 Rev 7, Figure 2 clock tree, Tables 22-27) and the SVD register
layout; RM0008 itself could not be obtained (see docs/LIMITATIONS.md, "STM32F103T6"), the register semantics below are
the RM0008 behaviour as documented by ST and used by the HAL/LL/SPL drivers.

Clock tree:
  HSI    = 8 MHz RC;  HSE = params `ext_hz` (default 8 MHz crystal, 4-16 MHz allowed)
  PLL    = (PLLSRC ? HSE / (PLLXTPRE ? 2 : 1) : HSI / 2) x PLLMUL   (PLLMUL field 0..14 -> x2..x16, 15 -> x16)
  SYSCLK = HSI | HSE | PLL (CFGR.SWS);  HCLK (core) = SYSCLK / HPRE (1, 2, 4 ... 512)
  PCLK1  = HCLK / PPRE1 (max 36 MHz), PCLK2 = HCLK / PPRE2 (1, 2, 4, 8, 16)
  timer clock on APBx = PCLKx when PPREx == 1, else 2 x PCLKx
  ADCCLK = PCLK2 / (2, 4, 6, 8) (CFGR.ADCPRE, max 14 MHz);  USBCLK = PLL / 1.5 (USBPRE = 0) or PLL (USBPRE = 1), must
  be 48 MHz.  RTCCLK = LSE (32.768 kHz, params `lse_hz`) | LSI (40 kHz, params `lsi_hz`) | HSE / 128 (BDCR.RTCSEL).
Limits checked with one `warn` each: SYSCLK/HCLK above 72 MHz, PLL output above 72 MHz, PCLK1 above 36 MHz, ADCCLK
above 14 MHz, USBCLK not 48 MHz while the USB clock is enabled.  The PLL configuration (PLLSRC, PLLXTPRE, PLLMUL) and
USBPRE are writable only while the PLL is off (writes with PLLON = 1 keep the old fields, one `warn`).

Status mirrors control immediately (no start-up/lock delay), as in stm32.rcc, so polling loops terminate:
CR.HSIRDY = HSION, HSERDY = HSEON, PLLRDY = PLLON; CSR.LSIRDY = LSION (or the IWDG runs); BDCR.LSERDY = LSEON.
CFGR.SWS follows SW when the source is ready (otherwise SWS keeps the old source, one `warn`).  A clock used as SYSCLK,
directly or through the PLL, cannot be stopped (HSION/HSEON/PLLON writes ignored for it).  Every ready transition sets
the CIR ready flag (LSIRDYF ... PLLRDYF); with its CIR enable (LSIRDYIE ... PLLRDYIE) it drives the RCC vector;
CIR.xxxRDYC (write 1) clears the flag.  CSS (CSSON) is stored; HSE never fails here.

Whenever HCLK changes the model calls chip.clock.set_hz(HCLK) unless `frozen` (Chip(clock_hz=...) different from the
chip YAML, or chip.clock_override for --clock).  `effective_apb_hz(bus)` / `effective_timer_hz(bus)` scale to the clock
the core actually runs at (what the reused stm32.usart / stm32.tim / stm32.spi / stm32.i2c models use).

Clock gates: AHBENR (reset 0x14: SRAM and FLITF clocks on), APB2ENR, APB1ENR.  A gated-off access logs one `warn`.
Writing 1 to an APB2RSTR/APB1RSTR bit resets the peripheral model.  Reset flags (CSR bits 26-31, same positions as the
F4): power-on sets PORRSTF | PINRSTF; SYSRESETREQ sets SFTRSTF | PINRSTF; IWDG / WWDG resets set IWDGRSTF / WWDGRSTF |
PINRSTF (`pending_reset_flags`, set by those models); a Standby wake-up sets none; any other reset PINRSTF; Stop/Standby
entry errors (LPWRRSTF) are not modelled.  The flags survive system resets until CSR.RMVF.
Backup domain: BDCR is writable only with PWR_CR.DBP = 1 (writes ignored with one `warn`), survives system resets;
BDCR.BDRST resets BDCR, the RTC and the BKP data registers.  RTCSEL cannot change once non-zero until BDRST.
"""
from __future__ import annotations

from typing import Any, Callable

from ..base import MMIOBlock
from ..registry import register
from ._util import HSI_HZ, LSE_HZ, LSI_HZ, build_regs, find_model, fmt_hz, irq_number

# CR
HSION, HSIRDY, HSEON, HSERDY, HSEBYP, CSSON, PLLON, PLLRDY = (1 << 0, 1 << 1, 1 << 16, 1 << 17, 1 << 18, 1 << 19,
                                                              1 << 24, 1 << 25)
# CFGR
PLLSRC, PLLXTPRE, USBPRE = 1 << 16, 1 << 17, 1 << 22
PLL_CFG_MASK = PLLSRC | PLLXTPRE | (0xF << 18) | USBPRE
# CIR flags (bit n) / enables (bit n + 8) / clears (bit n + 16): LSI, LSE, HSI, HSE, PLL; CSSF bit 7, CSSC bit 23
LSIRDYF, LSERDYF, HSIRDYF, HSERDYF, PLLRDYF, CSSF = 1, 2, 4, 8, 16, 0x80
# CSR
LSION, LSIRDY, RMVF = 1 << 0, 1 << 1, 1 << 24
PINRSTF, PORRSTF, SFTRSTF, IWDGRSTF, WWDGRSTF, LPWRRSTF = (1 << b for b in range(26, 32))
RST_FLAGS = 0xFC000000
# BDCR
LSEON, LSERDY, LSEBYP, RTCEN, BDRST = 1 << 0, 1 << 1, 1 << 2, 1 << 15, 1 << 16
HPRE_DIV = {8: 2, 9: 4, 10: 8, 11: 16, 12: 64, 13: 128, 14: 256, 15: 512}
PPRE_DIV = {4: 2, 5: 4, 6: 8, 7: 16}
SRC_NAMES = ("HSI", "HSE", "PLL", "?")

# gate name -> (register, bit)   (RM0008 7.3.6-7.3.8; STM32F103 low/medium/high density bit positions)
GATES: dict[str, tuple[str, int]] = {
    "DMA1": ("AHBENR", 0), "DMA2": ("AHBENR", 1), "SRAM": ("AHBENR", 2), "FLITF": ("AHBENR", 4),
    "CRC": ("AHBENR", 6), "FSMC": ("AHBENR", 8), "SDIO": ("AHBENR", 10),
    "AFIO": ("APB2ENR", 0), **{f"GPIO{p}": ("APB2ENR", 2 + i) for i, p in enumerate("ABCDEFG")},
    "ADC1": ("APB2ENR", 9), "ADC2": ("APB2ENR", 10), "TIM1": ("APB2ENR", 11), "SPI1": ("APB2ENR", 12),
    "TIM8": ("APB2ENR", 13), "USART1": ("APB2ENR", 14), "ADC3": ("APB2ENR", 15),
    "TIM2": ("APB1ENR", 0), "TIM3": ("APB1ENR", 1), "TIM4": ("APB1ENR", 2), "TIM5": ("APB1ENR", 3),
    "TIM6": ("APB1ENR", 4), "TIM7": ("APB1ENR", 5), "WWDG": ("APB1ENR", 11), "SPI2": ("APB1ENR", 14),
    "SPI3": ("APB1ENR", 15), "USART2": ("APB1ENR", 17), "USART3": ("APB1ENR", 18), "UART4": ("APB1ENR", 19),
    "UART5": ("APB1ENR", 20), "I2C1": ("APB1ENR", 21), "I2C2": ("APB1ENR", 22), "USB": ("APB1ENR", 23),
    "CAN": ("APB1ENR", 25), "BKP": ("APB1ENR", 27), "PWR": ("APB1ENR", 28), "DAC": ("APB1ENR", 29),
}
RST_REGS = {"APB1ENR": "APB1RSTR", "APB2ENR": "APB2RSTR"}      # no AHBRSTR outside the connectivity line


@register("stm32f1.rcc")
class RCC(MMIOBlock):
    GATES = GATES                 # read by the reused stm32.* models (gate bit / reset bit of their block)
    RST_REGS = RST_REGS

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ctl = {"on_write": self._ctl_write}
        ov: dict[str, dict] = {"CR": ctl, "CFGR": dict(ctl, ro_mask=0x0000000C),
                               "CIR": {"on_write": self._cir_write, "ro_mask": 0x0000009F},
                               "CSR": {"on_write": self._csr_write}, "BDCR": {"on_write": self._bdcr_write}}
        for r in RST_REGS.values():
            ov[r] = {"on_write": self._rstr_write}
        for r in RST_REGS:
            ov[r] = {"on_write": self._enr_write}
        ov["AHBENR"] = {"on_write": self._enr_write}
        r = build_regs((name, "RCC"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.ext_hz = float(params.get("ext_hz", 8_000_000))
        self.lse_hz = float(params.get("lse_hz", LSE_HZ))
        self.lsi_hz = float(params.get("lsi_hz", LSI_HZ))
        self.max_hz = float(params.get("max_hz", 72e6))
        self.frozen = bool(params.get("frozen", False))
        self.listeners: list[Callable[[], None]] = []
        self.gate_listeners: list[Callable[[str, int, int], None]] = []    # cb(enr_name, old, new)
        self.sysclk_hz = HSI_HZ
        self.core_hz = HSI_HZ
        self.apb1_hz = HSI_HZ
        self.apb2_hz = HSI_HZ
        self.source = "HSI"
        self._warned: set[str] = set()
        self._logged: tuple | None = None
        self._usb = 0.0
        self._powered = False
        self._irqn: int | None = None
        self._line = False
        self.pending_reset_flags = 0            # CSR flags the next reset sets (IWDG/WWDG)

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, "RCC")
        if getattr(chip, "clock_override", None):
            self.frozen = True
        else:
            desc_hz = getattr(getattr(getattr(chip, "desc", None), "core", None), "clock_hz", None)
            if desc_hz and abs(chip.clock.hz - float(desc_hz)) > 0.5:
                self.frozen = True          # Chip(clock_hz=...) explicitly overrode the YAML clock

    def reset(self) -> None:
        keep = {"CSR": self.get("CSR"), "BDCR": self.get("BDCR")} if self._powered else {}
        super().reset()
        if self._powered:
            flags = self.pending_reset_flags
            why = getattr(getattr(self.chip, "arch", None), "reset_requested", None)
            if not flags:
                if why == "SYSRESETREQ":
                    flags = SFTRSTF | PINRSTF
                elif why != "standby_wakeup":
                    flags = PINRSTF
            csr = (keep.get("CSR", 0) & RST_FLAGS) | flags
            self.set("CSR", (self.get("CSR") & ~RST_FLAGS) | csr)
            self.set("BDCR", keep.get("BDCR", 0))
        else:
            self.set("CSR", (self.get("CSR") & ~RST_FLAGS) | PORRSTF | PINRSTF)
        self._powered = True
        self.pending_reset_flags = 0
        self._warned.clear()
        self._logged = None
        if self._line and self.chip is not None:
            self.irq(self._irqn, False)
        self._line = False
        self.update()

    # ------------------------------------------------------------ gates (stm32.rcc interface)
    def gate_location(self, name: str | None) -> tuple[int, int] | None:
        g = GATES.get(name or "")
        if g is None or g[0] not in self.regs:
            return None
        reg, bit = g
        return self.regs[reg].offset + bit // 8, 1 << (bit % 8)

    def clock_enabled(self, name: str) -> bool:
        g = GATES.get(name)
        if g is None:
            return True
        reg, bit = g
        return bool((self.get(reg) >> bit) & 1)

    def check_gate(self, name: str, block: str | None = None) -> bool:
        if self.clock_enabled(name):
            return True
        if name not in self._warned:
            self._warned.add(name)
            reg, bit = GATES[name]
            self.emit("warn", block=block or name, msg=f"{block or name} accessed while its clock is disabled "
                      f"(RCC_{reg} bit {bit}); real hardware ignores the access")
        return False

    # ------------------------------------------------------------ derived clocks
    def _f(self, reg: str, lsb: int, width: int) -> int:
        return (self.get(reg) >> lsb) & ((1 << width) - 1)

    @property
    def hpre(self) -> int:
        return HPRE_DIV.get(self._f("CFGR", 4, 4), 1)

    def ppre(self, bus: int) -> int:
        return PPRE_DIV.get(self._f("CFGR", 8 if bus == 1 else 11, 3), 1)

    def pll_mul(self) -> int:
        return min(self._f("CFGR", 18, 4) + 2, 16)

    def pll_in_hz(self) -> float:
        cfgr = self.get("CFGR")
        if not cfgr & PLLSRC:
            return HSI_HZ / 2
        return self.ext_hz / (2 if cfgr & PLLXTPRE else 1)

    def pll_hz(self) -> float:
        return self.pll_in_hz() * self.pll_mul()

    def src_hz(self, sws: int) -> float:
        return (HSI_HZ, self.ext_hz, self.pll_hz(), HSI_HZ)[sws & 3]

    def timer_hz(self, bus: int) -> float:
        apb = self.apb1_hz if bus == 1 else self.apb2_hz
        return apb if self.ppre(bus) == 1 else 2 * apb

    def effective_apb_hz(self, bus: int) -> float:
        hz = self.chip.clock.hz if self.chip is not None else self.core_hz
        return hz / self.ppre(bus)

    def effective_timer_hz(self, bus: int) -> float:
        apb = self.effective_apb_hz(bus)
        return apb if self.ppre(bus) == 1 else 2 * apb

    def adc_prescaler(self) -> int:
        return 2 * (self._f("CFGR", 14, 2) + 1)

    def adc_hz(self) -> float:
        """ADCCLK = PCLK2 / ADCPRE (consistent with the clock the core runs at)."""
        return self.effective_apb_hz(2) / self.adc_prescaler()

    def usb_hz(self) -> float:
        """USBCLK = PLLCLK / 1.5 (USBPRE = 0) or PLLCLK (USBPRE = 1); 0 while the PLL is off."""
        if not self.get("CR") & PLLRDY:
            return 0.0
        return self.pll_hz() / (1.0 if self.get("CFGR") & USBPRE else 1.5)

    def rtc_clock_hz(self) -> float:
        """RTCCLK from BDCR.RTCSEL (0: none) and the oscillator states (LSE needs LSERDY, LSI needs LSIRDY)."""
        sel = self._f("BDCR", 8, 2)
        if sel == 1:
            return self.lse_hz if self.get("BDCR") & LSERDY else 0.0
        if sel == 2:
            return self.lsi_hz if self.get("CSR") & LSIRDY else 0.0
        if sel == 3:
            return self.ext_hz / 128 if self.get("CR") & HSERDY else 0.0
        return 0.0

    # ------------------------------------------------------------ register hooks
    def _ready(self, sw: int) -> bool:
        cr = self.get("CR")
        return bool(cr & (HSIRDY, HSERDY, PLLRDY, 0)[sw & 3]) if sw != 3 else False

    def _rdy_flags(self, old_rdy: int, new_rdy: int, flags: dict[int, int]) -> None:
        rising = new_rdy & ~old_rdy
        f = 0
        for bit, flag in flags.items():
            if rising & bit:
                f |= flag
        if f:
            self.set("CIR", self.get("CIR") | f)
            self._update_irq()

    def _ctl_write(self, blk, reg, old, new):
        if reg.name == "CFGR":
            if self.get("CR") & PLLON and (old ^ new) & PLL_CFG_MASK:
                new = (new & ~PLL_CFG_MASK) | (old & PLL_CFG_MASK)
                if "pllcfg" not in self._warned:
                    self._warned.add("pllcfg")
                    self.emit("warn", block=self.name, msg="RCC_CFGR: PLLSRC/PLLXTPRE/PLLMUL/USBPRE written while the "
                              "PLL is on: ignored (they can only be changed with PLLON = 0)")
            new = (new & ~0xC) | (old & 0xC)               # SWS is read-only
            self._store(reg, new)
        if reg.name == "CR":
            rdy = 0
            if new & HSION:
                rdy |= HSIRDY
            if new & HSEON:
                rdy |= HSERDY
            if new & PLLON:
                rdy |= PLLRDY
            sws = self._f("CFGR", 2, 2)
            pll_hse = bool(self.get("CFGR") & PLLSRC)
            if (sws == 0 or (sws == 2 and not pll_hse)) and not new & HSION:
                new |= HSION
                rdy |= HSIRDY
            if (sws == 1 or (sws == 2 and pll_hse)) and not new & HSEON:
                new |= HSEON
                rdy |= HSERDY
            if sws == 2 and not new & PLLON:
                new |= PLLON
                rdy |= PLLRDY
            if old & HSEON or new & HSEON:                 # HSEBYP: writable only while the HSE is off
                new = (new & ~HSEBYP) | (old & HSEBYP)
            new = (new & ~(HSIRDY | HSERDY | PLLRDY)) | rdy
            self._store(reg, new)
            self._rdy_flags(old, new, {HSIRDY: HSIRDYF, HSERDY: HSERDYF, PLLRDY: PLLRDYF})
        if old != new:
            self.update()
            if reg.name == "CR" and (old ^ new) & HSEON:
                self._notify()                 # OSC_IN/OSC_OUT pin ownership (stm32f1.gpio) follows HSEON
        return None

    def _cir_write(self, blk, reg, old, new):
        clear = (new >> 16) & 0x9F                        # LSIRDYC..PLLRDYC (bits 16-20), CSSC (23)
        v = (old & 0x9F & ~clear) | (new & 0x1F00)
        self._store(reg, v)
        self._update_irq()
        return v

    def _update_irq(self) -> None:
        cir = self.get("CIR")
        lvl = bool((cir & 0x1F) & (cir >> 8) & 0x1F) or bool(cir & CSSF)
        if lvl != self._line and self.chip is not None:
            self._line = lvl
            self.irq(self._irqn, lvl)

    def _iwdg_running(self) -> bool:
        iw = find_model(self.chip, "stm32.iwdg", "IWDG") if self.chip is not None else None
        return bool(iw is not None and getattr(iw, "running", False))

    def _csr_write(self, blk, reg, old, new):
        lsi = bool(new & LSION or self._iwdg_running())
        v = (new & ~LSIRDY) | (LSIRDY if lsi else 0)
        if new & RMVF:
            v &= ~(RST_FLAGS | RMVF)
        else:
            v = (v & ~RST_FLAGS) | (old & RST_FLAGS)        # the flags are read-only (cleared by RMVF only)
        self._rdy_flags(old, v, {LSIRDY: LSIRDYF})
        if (old ^ v) & LSIRDY:
            self._notify()
        return v

    def _dbp(self) -> bool:
        pwr = find_model(self.chip, "stm32f1.pwr", "PWR") if self.chip is not None else None
        return pwr is None or bool(pwr.get("CR") & (1 << 8))

    def _bdcr_write(self, blk, reg, old, new):
        if not self._dbp():
            if "bdcr_dbp" not in self._warned:
                self._warned.add("bdcr_dbp")
                self.emit("warn", block=self.name, msg="RCC_BDCR write ignored: backup domain write protection "
                          "(PWR_CR.DBP = 0)")
            return old
        if new & BDRST and not old & BDRST:
            self._backup_domain_reset()
            old = 0
        if old & LSEON or new & LSEON:
            new = (new & ~LSEBYP) | (old & LSEBYP)
        sel_old = (old >> 8) & 3
        if sel_old and ((new >> 8) & 3) != sel_old:
            new = (new & ~(3 << 8)) | (sel_old << 8)
            if "rtcsel" not in self._warned:
                self._warned.add("rtcsel")
                self.emit("warn", block=self.name, msg="RCC_BDCR.RTCSEL change ignored: the RTC clock source can only "
                          "be changed after a backup domain reset (BDRST)")
        v = (new & ~LSERDY) | (LSERDY if new & LSEON else 0)
        self._rdy_flags(old, v, {LSERDY: LSERDYF})
        if old != v:
            self._store(reg, v)
            self._notify()
        return v

    def _backup_domain_reset(self) -> None:
        chip = self.chip
        if chip is None:
            return
        for model, name in (("stm32f1.rtc", "RTC"), ("stm32f1.bkp", "BKP")):
            blk = find_model(chip, model, name)
            fn = getattr(blk, "backup_domain_reset", None)
            if fn is not None:
                fn()
        self.emit("info", block=self.name, msg="backup domain reset (RCC_BDCR.BDRST)")

    def _enr_write(self, blk, reg, old, new):
        if old != new:
            for cb in list(self.gate_listeners):
                cb(reg.name, old, new)
            if reg.name == "APB1ENR" and (old ^ new) & (1 << GATES["USB"][1]):
                self._usb_check()
        return None

    def _rstr_write(self, blk, reg, old, new):
        rising = new & ~old
        if rising and self.chip is not None:
            enr = next(e for e, r in RST_REGS.items() if r == reg.name)
            for gname, (greg, bit) in GATES.items():
                if greg == enr and rising >> bit & 1:
                    blks = [b for b in self.chip.peripherals.values()
                            if b is not self and (b.name == gname or getattr(b, "gate", None) == gname)]
                    for b in blks:
                        b.reset()
                    if blks:
                        self.emit("info", block=self.name, msg=f"{gname} reset through RCC_{reg.name}")
        return None

    # ------------------------------------------------------------ update
    def _notify(self) -> None:
        for cb in list(self.listeners):
            cb()

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    def _usb_check(self) -> None:
        if self.clock_enabled("USB"):
            if self.get("CR") & PLLON and not self.get("CFGR") & PLLSRC:
                self._warn_once("usbhsi", "RCC: USB clock enabled with the PLL fed by HSI/2: the USB 48 MHz clock "
                                "must come from the HSE crystal (DS15060 2.3.20)")
            u = self.usb_hz()
            if abs(u - 48e6) > 48e6 * 0.0025:
                self._warn_once("usb48", f"RCC: USB clock enabled with USBCLK = {fmt_hz(u)} (must be 48 MHz: PLL 72 MHz "
                                f"with USBPRE = 0 or PLL 48 MHz with USBPRE = 1)")

    def update(self) -> None:
        cfgr = self.get("CFGR")
        sw = cfgr & 3
        sws = (cfgr >> 2) & 3
        if sw != sws:
            if sw != 3 and self._ready(sw):
                sws = sw
            elif "sw" not in self._warned:
                self._warned.add("sw")
                self.emit("warn", block=self.name, msg=f"RCC: switch to {SRC_NAMES[sw]} refused: source not ready "
                          f"(SWS stays {SRC_NAMES[sws]})")
            self.set("CFGR", (cfgr & ~0xC) | (sws << 2))
        self.source = SRC_NAMES[sws]
        self.sysclk_hz = self.src_hz(sws)
        self.core_hz = self.sysclk_hz / self.hpre
        self.apb1_hz = self.core_hz / self.ppre(1)
        self.apb2_hz = self.core_hz / self.ppre(2)
        usb = self.usb_hz()
        key = (round(self.core_hz), round(self.apb1_hz), round(self.apb2_hz), self.source, self.adc_prescaler())
        if key == self._logged:
            if usb != self._usb:
                self._usb = usb
                self._usb_check()
                self._notify()
            return
        self._usb = usb
        self._logged = key
        chip = self.chip
        if chip is not None and getattr(chip, "clock_override", None):
            self.frozen = True
        if chip is not None:
            if self.get("CR") & PLLON and self.pll_hz() > self.max_hz * 1.001:
                self._warn_once("pll", f"RCC: PLL output {fmt_hz(self.pll_hz())} exceeds the {fmt_hz(self.max_hz)} "
                                f"maximum (DS15060 Table 27)")
            if self.core_hz > self.max_hz * 1.001:
                self._warn_once("hclk", f"RCC: HCLK {fmt_hz(self.core_hz)} exceeds the {fmt_hz(self.max_hz)} maximum")
            if self.apb1_hz > 36e6 * 1.001:
                self._warn_once("apb1", f"RCC: PCLK1 {fmt_hz(self.apb1_hz)} exceeds the 36 MHz maximum (set PPRE1)")
            adc = self.apb2_hz / self.adc_prescaler()
            if adc > 14e6 * 1.001 and self.clock_enabled("ADC1"):
                self._warn_once("adc", f"RCC: ADCCLK {fmt_hz(adc)} exceeds the 14 MHz maximum (set ADCPRE)")
            msg = (f"core clock {self.core_hz / 1e6:.3f} MHz (SYSCLK {self.source} {self.sysclk_hz / 1e6:.3f} MHz), "
                   f"APB1 {self.apb1_hz / 1e6:.3f} MHz, APB2 {self.apb2_hz / 1e6:.3f} MHz")
            if self.frozen:
                msg += f" (clock frozen at {chip.clock.hz / 1e6:.3f} MHz by override)"
            self.emit("info", block=self.name, msg=msg, core_hz=self.core_hz, apb1_hz=self.apb1_hz,
                      apb2_hz=self.apb2_hz, source=self.source)
            if not self.frozen and self.core_hz > 0 and chip.clock.hz != self.core_hz:
                chip.clock.set_hz(self.core_hz)
            self._usb_check()
        self._notify()
