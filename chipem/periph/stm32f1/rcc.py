"""STM32F1 RCC (reset and clock control, RM0008 ch. 7: low-, medium-, high- and XL-density devices).

Verified against RM0008 Rev 21 ch. 7 (7.1 resets, 7.2 clocks, 7.3.1-7.3.10 registers).  The connectivity line RCC
(ch. 8: PLL2/PLL3, PREDIV1/2, AHBRSTR, CFGR2) is a subclass in its own module; see "Subclass hooks" below.

Clock tree (7.2, Figure 8):
  HSI    = 8 MHz RC (CR.HSICAL: params `hsical`, factory value, default 0; HSITRIM resets to 16, 7.3.1)
  HSE    = params `ext_hz` (default 8 MHz; 4-16 MHz crystal, up to 25 MHz with HSEBYP, 7.2.1)
  PLL    = (PLLSRC ? HSE / (PLLXTPRE ? 2 : 1) : HSI / 2) x PLLMUL   (PLLMUL 0000..1110 -> x2..x16, 1111 -> x16, 7.3.2)
  SYSCLK = HSI | HSE | PLL (CFGR.SW; CFGR.SWS shows the source in use);  HCLK = SYSCLK / HPRE (1, 2 ... 512)
  PCLK1  = HCLK / PPRE1 (36 MHz max), PCLK2 = HCLK / PPRE2 (72 MHz max); timer clock x2 when PPREx != 1 (7.2)
  ADCCLK = PCLK2 / 2, 4, 6, 8 (14 MHz max);  USBCLK = PLLCLK / 1.5 (USBPRE = 0) or PLLCLK (USBPRE = 1)
  RTCCLK = LSE (params `lse_hz`, 32.768 kHz) | LSI (params `lsi_hz`, 40 kHz) | HSE / 128 (BDCR.RTCSEL, 7.2.8)
  MCO    = none (0xx) | SYSCLK | HSI | HSE | PLL / 2 (CFGR.MCO, 7.2.10; the pin is owned by stm32f1.gpio)
Limits checked with one `warn` each: PLL output and HCLK above 72 MHz, PCLK1 above 36 MHz, ADCCLK above 14 MHz,
USBCLK not 48 MHz while the USB clock is enabled (7.2.3), USB on a PLL fed by HSI/2.

Register behaviour (each verified, RM0008 section in brackets):
* CR (7.3.1): reset 0x0000XX83 (HSION, HSIRDY, HSITRIM = 16, HSICAL factory).  HSION/HSEON/PLLON cannot be cleared
  while that clock is used as SYSCLK, directly or through the PLL, or is selected (SW) to become SYSCLK (7.2.6).
  HSEBYP is writable only while the HSE is off.  xxxRDY follows xxxON after a start-up time (params `hsi_startup_ns`,
  `hse_startup_ns`, `pll_lock_ns`, `lsi_startup_ns`, `lse_startup_ns`; all default 0 = ready at once; the data sheet
  values are about 2 us HSI, 2 ms HSE crystal, 200 us PLL lock, 85 us LSI, 1-2 s LSE) and drops at once when the
  oscillator is switched off (silicon: after 6 / 3 cycles).
* CFGR (7.3.2): PLLSRC/PLLXTPRE/PLLMUL are writable only while the PLL is off (PLLON = PLLRDY = 0; writes keep the old
  fields, one `warn`); USBPRE cannot be cleared while the USB clock (APB1ENR.USBEN) is on.  A switch (SW) to a source
  that is not ready is pending: SWS follows as soon as the source is ready (7.2.6); SW = 11 is not allowed (ignored).
* CIR (7.3.3): a ready flag (LSIRDYF ... PLLRDYF) is set when the oscillator becomes ready *and* its xxxRDYIE is set;
  xxxRDYC / CSSC (write 1, read 0) clear the flags.  The RCC vector is asserted while (flags & enables); CSSF goes to
  the NMI, not to the RCC vector (7.2.7).
* APB2RSTR / APB1RSTR (7.3.4-7.3.5): writing 1 resets the peripheral model (rising edge).  No AHBRSTR on these lines.
* AHBENR (7.3.6, reset 0x14: SRAM and FLITF clocks on) / APB2ENR / APB1ENR (7.3.7-7.3.8): clock gates (`GATES`,
  including the XL-density TIM9-TIM14 bits).  A gated-off access logs one `warn` (silicon reads 0 / ignores).
* BDCR (7.3.9): LSEON, LSEBYP, RTCSEL and RTCEN are in the backup domain: writable only with PWR_CR.DBP = 1 (else
  kept, one `warn`), kept over system resets; BDRST is not write-protected and holds the whole backup domain (BDCR,
  RTC core, BKP) in reset while set.  RTCSEL cannot change once non-zero until a backup domain reset (7.2.8).  LSEBYP
  is writable only while the LSE is off.
* CSR (7.3.10): reset 0x0C000000; LSION is reset by every system reset, the reset flags only by a power-on reset;
  RMVF clears them.  Flags set: power-on PORRSTF | PINRSTF; SYSRESETREQ SFTRSTF | PINRSTF; IWDG / WWDG timeout
  IWDGRSTF / WWDGRSTF | PINRSTF; low-power management reset (option bytes nRST_STOP / nRST_STDBY = 0) LPWRRSTF |
  PINRSTF; NRST and the Standby exit (a power reset whose pulse is output on NRST, 7.1.2) PINRSTF.  An internal reset is
  output on the NRST pin (Figure 7), which is why PINRSTF accompanies every source.  While the IWDG runs the LSI is
  forced on (7.2.9).
* CSS (7.2.7): CSSON is stored; `hse_failure()` (board/test hook: the crystal stops) acts only with CSSON = 1 and the
  HSE ready: HSEON/HSERDY cleared, CSSF set and the NMI pended; if the HSE was SYSCLK, directly or through the PLL,
  SYSCLK switches to HSI (SW = SWS = 00, HSION forced) and a PLL fed by the HSE is switched off.  Not modelled: the
  clock-failure event on the TIM1/TIM8 break input, the NMI re-entering until CSSC.

Whenever HCLK changes the model calls chip.clock.set_hz(HCLK) unless `frozen` (Chip(clock_hz=...) differs from the
chip YAML, or chip.clock_override for --clock).  `effective_apb_hz(bus)` / `effective_timer_hz(bus)` scale to the clock
the core actually runs at (used by the reused stm32.usart / stm32.tim / stm32.spi / stm32.i2c models).

Subclass hooks (the connectivity-line RCC, RM0008 ch. 8, subclasses `RCC` in its own module):
* class attributes `GATES` (gate name -> (enable register, bit)), `RST_REGS` (enable register -> reset register;
  add "AHBENR": "AHBRSTR"), `OSCS` (oscillator name -> (register, ON bit, RDY bit, CIR flag, start-up param); add
  PLL2/PLL3), `PLL_CFG_MASK` (CFGR bits locked while the PLL runs);
* methods `pll_in_hz()`, `pll_mul()`, `pll_hz()`, `usb_hz()`, `src_hz(sws)`, `rtc_clock_hz()`, `osc_on(name)`
  (extra "forced on" rules), and `_cfgr_write_extra(old, new)` / `_cr_write_extra(old, new)` (return the value to
  store) for the CL fields;
* `enter_stop()` is what stm32f1.pwr calls on Stop entry (HSE/PLL off, SYSCLK = HSI, 5.3.4).
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
PLL_CFG_MASK = PLLSRC | PLLXTPRE | (0xF << 18)
# CIR flags (bit n) / enables (bit n + 8) / clears (bit n + 16): LSI, LSE, HSI, HSE, PLL; CSSF bit 7, CSSC bit 23
LSIRDYF, LSERDYF, HSIRDYF, HSERDYF, PLLRDYF, CSSF = 1, 2, 4, 8, 16, 0x80
CSSC = 1 << 23
# CSR
LSION, LSIRDY, RMVF = 1 << 0, 1 << 1, 1 << 24
PINRSTF, PORRSTF, SFTRSTF, IWDGRSTF, WWDGRSTF, LPWRRSTF = (1 << b for b in range(26, 32))
RST_FLAGS = 0xFC000000
# BDCR
LSEON, LSERDY, LSEBYP, RTCEN, BDRST = 1 << 0, 1 << 1, 1 << 2, 1 << 15, 1 << 16
BDCR_PROTECTED = LSEON | LSEBYP | (3 << 8) | RTCEN          # backup-domain bits (7.3.9 note): need PWR_CR.DBP
HPRE_DIV = {8: 2, 9: 4, 10: 8, 11: 16, 12: 64, 13: 128, 14: 256, 15: 512}
PPRE_DIV = {4: 2, 5: 4, 6: 8, 7: 16}
SRC_NAMES = ("HSI", "HSE", "PLL", "?")

# gate name -> (register, bit)   (RM0008 7.3.6-7.3.8; low/medium/high/XL density bit positions)
GATES: dict[str, tuple[str, int]] = {
    "DMA1": ("AHBENR", 0), "DMA2": ("AHBENR", 1), "SRAM": ("AHBENR", 2), "FLITF": ("AHBENR", 4),
    "CRC": ("AHBENR", 6), "FSMC": ("AHBENR", 8), "SDIO": ("AHBENR", 10),
    "AFIO": ("APB2ENR", 0), **{f"GPIO{p}": ("APB2ENR", 2 + i) for i, p in enumerate("ABCDEFG")},
    "ADC1": ("APB2ENR", 9), "ADC2": ("APB2ENR", 10), "TIM1": ("APB2ENR", 11), "SPI1": ("APB2ENR", 12),
    "TIM8": ("APB2ENR", 13), "USART1": ("APB2ENR", 14), "ADC3": ("APB2ENR", 15),
    "TIM9": ("APB2ENR", 19), "TIM10": ("APB2ENR", 20), "TIM11": ("APB2ENR", 21),
    "TIM2": ("APB1ENR", 0), "TIM3": ("APB1ENR", 1), "TIM4": ("APB1ENR", 2), "TIM5": ("APB1ENR", 3),
    "TIM6": ("APB1ENR", 4), "TIM7": ("APB1ENR", 5), "TIM12": ("APB1ENR", 6), "TIM13": ("APB1ENR", 7),
    "TIM14": ("APB1ENR", 8), "WWDG": ("APB1ENR", 11), "SPI2": ("APB1ENR", 14),
    "SPI3": ("APB1ENR", 15), "USART2": ("APB1ENR", 17), "USART3": ("APB1ENR", 18), "UART4": ("APB1ENR", 19),
    "UART5": ("APB1ENR", 20), "I2C1": ("APB1ENR", 21), "I2C2": ("APB1ENR", 22), "USB": ("APB1ENR", 23),
    "CAN": ("APB1ENR", 25), "BKP": ("APB1ENR", 27), "PWR": ("APB1ENR", 28), "DAC": ("APB1ENR", 29),
}
RST_REGS = {"APB1ENR": "APB1RSTR", "APB2ENR": "APB2RSTR"}      # no AHBRSTR outside the connectivity line
# oscillator -> (register, ON bit, RDY bit, CIR ready flag, start-up time parameter)
OSCS: dict[str, tuple[str, int, int, int, str]] = {
    "HSI": ("CR", HSION, HSIRDY, HSIRDYF, "hsi_startup_ns"),
    "HSE": ("CR", HSEON, HSERDY, HSERDYF, "hse_startup_ns"),
    "PLL": ("CR", PLLON, PLLRDY, PLLRDYF, "pll_lock_ns"),
    "LSI": ("CSR", LSION, LSIRDY, LSIRDYF, "lsi_startup_ns"),
    "LSE": ("BDCR", LSEON, LSERDY, LSERDYF, "lse_startup_ns"),
}


@register("stm32f1.rcc")
class RCC(MMIOBlock):
    GATES = GATES                 # read by the reused stm32.* models (gate bit / reset bit of their block)
    RST_REGS = RST_REGS
    OSCS = OSCS
    PLL_CFG_MASK = PLL_CFG_MASK

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov: dict[str, dict] = {"CR": {"on_write": self._cr_write}, "CFGR": {"on_write": self._cfgr_write},
                               "CIR": {"on_write": self._cir_write},
                               "CSR": {"on_write": self._csr_write}, "BDCR": {"on_write": self._bdcr_write}}
        for enr, rst in self.RST_REGS.items():
            ov[rst] = {"on_write": self._rstr_write}
            ov[enr] = {"on_write": self._enr_write}
        ov.setdefault("AHBENR", {"on_write": self._enr_write})
        hsical = int(params.get("hsical", 0)) & 0xFF
        if hsical:
            ov["CR"]["reset"] = 0x83 | (hsical << 8)
        r = build_regs((name, "RCC"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.ext_hz = float(params.get("ext_hz", 8_000_000))
        self.lse_hz = float(params.get("lse_hz", LSE_HZ))
        self.lsi_hz = float(params.get("lsi_hz", LSI_HZ))
        self.max_hz = float(params.get("max_hz", 72e6))
        self.frozen = bool(params.get("frozen", False))
        self.startup_ns = {k: float(params.get(v[4], 0.0)) for k, v in self.OSCS.items()}
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
        self._osc_ev: dict[str, Any] = {}
        self._pending_sw: int | None = None
        self.css_failures = 0
        self.pending_reset_flags = 0            # CSR flags the next reset sets (IWDG/WWDG/low-power reset)

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
        for name in list(self._osc_ev):
            self._cancel_osc(name)
        keep = {"CSR": self.get("CSR"), "BDCR": self.get("BDCR")} if self._powered else {}
        super().reset()
        if self._powered:
            flags = self.pending_reset_flags
            why = getattr(getattr(self.chip, "arch", None), "reset_requested", None)
            if not flags:
                flags = SFTRSTF | PINRSTF if why == "SYSRESETREQ" else PINRSTF
            csr = (keep.get("CSR", 0) & RST_FLAGS) | flags
            self.set("CSR", (self.get("CSR") & ~RST_FLAGS) | csr)
            self.set("BDCR", keep.get("BDCR", 0))
        else:
            self.set("CSR", (self.get("CSR") & ~RST_FLAGS) | PORRSTF | PINRSTF)
        self._powered = True
        self.pending_reset_flags = 0
        self._warned.clear()
        self._logged = None
        self._pending_sw = None
        if self._line and self.chip is not None:
            self.irq(self._irqn, False)
        self._line = False
        if self.chip is not None:
            for name in self.OSCS:
                self._osc_update(name, flag=False)
        self.update()

    # ------------------------------------------------------------ gates (stm32.rcc interface)
    def gate_location(self, name: str | None) -> tuple[int, int] | None:
        g = self.GATES.get(name or "")
        if g is None or g[0] not in self.regs:
            return None
        reg, bit = g
        return self.regs[reg].offset + bit // 8, 1 << (bit % 8)

    def clock_enabled(self, name: str) -> bool:
        g = self.GATES.get(name)
        if g is None or g[0] not in self.regs:
            return True
        reg, bit = g
        return bool((self.get(reg) >> bit) & 1)

    def check_gate(self, name: str, block: str | None = None) -> bool:
        if self.clock_enabled(name):
            return True
        if name not in self._warned:
            self._warned.add(name)
            reg, bit = self.GATES[name]
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
        return min(self._f("CFGR", 18, 4) + 2, 16)          # 1110 and 1111 are both x16 (7.3.2)

    def pll_in_hz(self) -> float:
        cfgr = self.get("CFGR")
        if not cfgr & PLLSRC:
            return HSI_HZ / 2
        return self.ext_hz / (2 if cfgr & PLLXTPRE else 1)

    def pll_hz(self) -> float:
        return self.pll_in_hz() * self.pll_mul()

    def pll_uses_hse(self) -> bool:
        return bool(self.get("CFGR") & PLLSRC)

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

    def mco_hz(self) -> float:
        """Clock on the MCO pin (CFGR.MCO, 7.2.10): 0xx none, 100 SYSCLK, 101 HSI, 110 HSE, 111 PLL / 2."""
        sel = self._f("CFGR", 24, 3)
        cr = self.get("CR")
        return {4: self.sysclk_hz, 5: HSI_HZ if cr & HSIRDY else 0.0, 6: self.ext_hz if cr & HSERDY else 0.0,
                7: self.pll_hz() / 2 if cr & PLLRDY else 0.0}.get(sel, 0.0)

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

    # ------------------------------------------------------------ oscillators
    def osc_on(self, name: str) -> bool:
        """Is oscillator `name` requested on (its ON bit, or forced: the LSI while the IWDG runs, 7.2.9)?"""
        reg, on, _, _, _ = self.OSCS[name]
        if name == "LSI" and self._iwdg_running():
            return True
        return bool(self.get(reg) & on)

    def _cancel_osc(self, name: str) -> None:
        ev = self._osc_ev.pop(name, None)
        if ev is not None and self.chip is not None:
            self.chip.sched.cancel(ev)

    def _osc_update(self, name: str, flag: bool = True) -> bool:
        """Bring RDY in line with ON: start-up delay on enable, immediate drop on disable.  True if RDY changed."""
        reg, _, rdy, cirf, _ = self.OSCS[name]
        v = self.get(reg)
        if self.osc_on(name):
            if v & rdy or name in self._osc_ev:
                return False
            delay = self.startup_ns.get(name, 0.0)
            if delay > 0 and self.chip is not None:
                self._osc_ev[name] = self.chip.sched.at_ns(self.chip.now_ns() + delay, self._osc_ready, name)
                return False
            self.set(reg, v | rdy)
            if flag:
                self._ready_flag(cirf)
            return True
        self._cancel_osc(name)
        if v & rdy:
            self.set(reg, v & ~rdy)
            return True
        return False

    def _osc_ready(self, name: str) -> None:
        self._osc_ev.pop(name, None)
        reg, _, rdy, cirf, _ = self.OSCS[name]
        if not self.osc_on(name):
            return
        self.set(reg, self.get(reg) | rdy)
        self._ready_flag(cirf)
        self.emit("info", block=self.name, msg=f"{name} ready")
        self.update()
        self._notify()

    def _ready_flag(self, flag: int) -> None:
        """CIR.xxxRDYF is set when the oscillator becomes ready and xxxRDYIE is set (7.3.3)."""
        cir = self.get("CIR")
        if cir & (flag << 8):
            self.set("CIR", cir | flag)
            self._update_irq()

    # ------------------------------------------------------------ register hooks
    def _ready(self, sw: int) -> bool:
        if sw == 3:
            return False
        return bool(self.get("CR") & (HSIRDY, HSERDY, PLLRDY)[sw])

    def _sysclk_users(self) -> int:
        """CR ON bits that cannot be cleared: the SYSCLK source in use (SWS) or selected (SW), directly or through
        the PLL (7.3.1, 7.2.6)."""
        cfgr = self.get("CFGR")
        keep = 0
        for s in {cfgr & 3, (cfgr >> 2) & 3}:
            if s == 0:
                keep |= HSION
            elif s == 1:
                keep |= HSEON
            elif s == 2:
                keep |= PLLON | (HSEON if self.pll_uses_hse() else HSION)
        return keep

    def _cr_write_extra(self, old: int, new: int) -> int:
        return new

    def _cfgr_write_extra(self, old: int, new: int) -> int:
        return new

    def _cr_write(self, blk, reg, old, new):
        keep = self._sysclk_users() & old
        if keep & ~new:
            new |= keep
            self._warn_once("cr_keep", "RCC_CR: an oscillator used (or selected) as SYSCLK cannot be switched off: "
                            "write ignored for it (RM0008 7.3.1)")
        if old & (HSEON | HSERDY):                     # HSEBYP: writable only while the HSE is off
            new = (new & ~HSEBYP) | (old & HSEBYP)
        new = self._cr_write_extra(old, new)
        self._store(reg, new)
        for name in ("HSI", "HSE", "PLL"):
            self._osc_update(name)
        new = self.get("CR")
        if old != new:
            self.update()
            if (old ^ new) & HSEON:
                self._notify()                 # OSC_IN/OSC_OUT pin ownership (stm32f1.gpio) follows HSEON
        return None                            # stored above (update() may have changed CFGR.SWS meanwhile)

    def _cfgr_write(self, blk, reg, old, new):
        if self.get("CR") & (PLLON | PLLRDY) and (old ^ new) & self.PLL_CFG_MASK:
            new = (new & ~self.PLL_CFG_MASK) | (old & self.PLL_CFG_MASK)
            self._warn_once("pllcfg", "RCC_CFGR: PLLSRC/PLLXTPRE/PLLMUL written while the PLL is on: ignored (they "
                            "can only be changed with PLLON = 0, RM0008 7.2.3)")
        if old & USBPRE and not new & USBPRE and self.clock_enabled("USB"):
            new |= USBPRE
            self._warn_once("usbpre", "RCC_CFGR.USBPRE cannot be cleared while the USB clock is enabled (RM0008 7.3.2)")
        new = self._cfgr_write_extra(old, new)
        self._store(reg, new)
        if old != new:
            self.update()                      # may set SWS: keep the stored value (return None)
        return None

    def _cir_write(self, blk, reg, old, new):
        clear = ((new >> 16) & 0x1F) | (CSSF if new & CSSC else 0)
        v = (old & (0x1F | CSSF) & ~clear) | (new & 0x1F00)
        self._store(reg, v)
        self._update_irq()
        return v

    def _update_irq(self) -> None:
        cir = self.get("CIR")
        lvl = bool((cir & 0x1F) & (cir >> 8) & 0x1F)          # CSSF goes to the NMI (7.2.7), not to this vector
        if lvl != self._line and self.chip is not None and self._irqn is not None:
            self._line = lvl
            self.irq(self._irqn, lvl)

    def _iwdg_running(self) -> bool:
        iw = find_model(self.chip, "stm32.iwdg", "IWDG") if self.chip is not None else None
        return bool(iw is not None and getattr(iw, "running", False))

    def _csr_write(self, blk, reg, old, new):
        v = new
        if new & RMVF:
            v &= ~(RST_FLAGS | RMVF)
        else:
            v = (v & ~RST_FLAGS) | (old & RST_FLAGS)        # the flags are read-only (cleared by RMVF only)
        v = (v & ~LSIRDY) | (old & LSIRDY)
        self._store(reg, v)
        if self._osc_update("LSI"):
            self._notify()
        return None

    def _dbp(self) -> bool:
        pwr = find_model(self.chip, "stm32f1.pwr", "PWR") if self.chip is not None else None
        return pwr is None or bool(pwr.get("CR") & (1 << 8))

    def _bdcr_write(self, blk, reg, old, new):
        if not self._dbp() and (old ^ new) & BDCR_PROTECTED:
            new = (new & ~BDCR_PROTECTED) | (old & BDCR_PROTECTED)
            self._warn_once("bdcr_dbp", "RCC_BDCR: LSEON/LSEBYP/RTCSEL/RTCEN write ignored: backup domain write "
                            "protection (PWR_CR.DBP = 0, RM0008 7.3.9)")
        if new & BDRST:
            if not old & BDRST:
                self._backup_domain_reset()
            new = BDRST                                        # the backup domain is held in reset while BDRST = 1
            old = 0
        if old & (LSEON | LSERDY):                             # LSEBYP: writable only while the LSE is off
            new = (new & ~LSEBYP) | (old & LSEBYP)
        sel_old = (old >> 8) & 3
        if sel_old and ((new >> 8) & 3) != sel_old:
            new = (new & ~(3 << 8)) | (sel_old << 8)
            self._warn_once("rtcsel", "RCC_BDCR.RTCSEL change ignored: the RTC clock source can only be changed "
                            "after a backup domain reset (BDRST, RM0008 7.2.8)")
        new = (new & ~LSERDY) | (old & LSERDY)
        self._store(reg, new)
        self._osc_update("LSE")
        if self.get("BDCR") != old:
            self._notify()
        return None

    def _backup_domain_reset(self) -> None:
        chip = self.chip
        self._cancel_osc("LSE")
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
            g = self.GATES.get("USB")
            if g is not None and reg.name == g[0] and (old ^ new) & (1 << g[1]):
                self._usb_check()
        return None

    def _rstr_write(self, blk, reg, old, new):
        rising = new & ~old
        if rising and self.chip is not None:
            enr = next(e for e, r in self.RST_REGS.items() if r == reg.name)
            for gname, (greg, bit) in self.GATES.items():
                if greg == enr and rising >> bit & 1:
                    blks = [b for b in self.chip.peripherals.values()
                            if b is not self and (b.name == gname or getattr(b, "gate", None) == gname)]
                    for b in blks:
                        b.reset()
                    if blks:
                        self.emit("info", block=self.name, msg=f"{gname} reset through RCC_{reg.name}")
        return None

    # ------------------------------------------------------------ low power / CSS
    def enter_stop(self) -> None:
        """Stop entry (5.3.4): PLL, HSE (and HSI) stop; on the exit SYSCLK is the HSI (SW = SWS = 00, HSION forced,
        7.3.1/7.3.2), so the HSI-on, HSE/PLL-off state is applied now (as firmware sees it after the Stop exit)."""
        self._cancel_osc("HSE")
        self._cancel_osc("PLL")
        self.set("CR", (self.get("CR") | HSION | HSIRDY) & ~(HSEON | HSERDY | PLLON | PLLRDY))
        self.set("CFGR", self.get("CFGR") & ~0xF)
        self.update()
        self._notify()

    def hse_failure(self) -> bool:
        """The HSE clock fails (board / test hook).  With CSSON and the HSE ready (clock detector on, 7.3.1): the HSE
        is switched off, CIR.CSSF is set and the NMI is pended (7.2.7); SYSCLK on the HSE (directly or through the PLL)
        switches to the HSI and a PLL fed by the HSE is switched off.  Returns True if the CSS detected it."""
        cr = self.get("CR")
        if not (cr & CSSON and cr & HSERDY):
            return False
        self.css_failures += 1
        cfgr = self.get("CFGR")
        sws = (cfgr >> 2) & 3
        on_hse = sws == 1 or (sws == 2 and self.pll_uses_hse())
        cr &= ~(HSEON | HSERDY)
        if self.pll_uses_hse() and (sws == 2 or cfgr & 3 == 2):
            cr &= ~(PLLON | PLLRDY)
            self._cancel_osc("PLL")
        if on_hse:
            cr |= HSION | HSIRDY
            self.set("CFGR", cfgr & ~0xF)
        self._cancel_osc("HSE")
        self.set("CR", cr)
        self.set("CIR", self.get("CIR") | CSSF)
        self.emit("warn", block=self.name, msg="HSE clock failure detected by the CSS: HSE off, CSSF set, NMI"
                  + (", SYSCLK switched to HSI" if on_hse else ""))
        if self.chip is not None:
            f = getattr(self.chip.arch, "set_pending_exc", None)
            if f is not None:
                f(2)
        self.update()
        self._notify()
        return True

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
                self._warn_once("usb48", f"RCC: USB clock enabled with USBCLK = {fmt_hz(u)} (must be 48 MHz: PLL "
                                f"72 MHz with USBPRE = 0 or PLL 48 MHz with USBPRE = 1, RM0008 7.2.3)")

    def update(self) -> None:
        cfgr = self.get("CFGR")
        sw = cfgr & 3
        sws = (cfgr >> 2) & 3
        if sw != sws:
            if sw != 3 and self._ready(sw):
                sws = sw
                self._pending_sw = None
            elif sw != 3 and self._pending_sw != sw:
                self._pending_sw = sw
                reg, on, _, _, _ = self.OSCS[SRC_NAMES[sw]]
                if self.get(reg) & on:
                    self.emit("info", block=self.name, msg=f"RCC: switch to {SRC_NAMES[sw]} pending until it is ready "
                              f"(SWS stays {SRC_NAMES[sws]}, RM0008 7.2.6)")
                else:
                    self.emit("warn", block=self.name, msg=f"RCC: switch to {SRC_NAMES[sw]} pending but "
                              f"{SRC_NAMES[sw]} is off (SWS stays {SRC_NAMES[sws]} until it is started and ready)")
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
                                f"maximum (RM0008 7.3.2)")
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
