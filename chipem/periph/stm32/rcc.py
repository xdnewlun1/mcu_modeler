"""STM32F4 RCC (reset and clock control, RM0090 ch. 7): oscillators, PLL, bus prescalers, clock gates.

Status mirrors control immediately (no start-up/lock delays), so HAL/LL polling loops terminate:
CR.HSIRDY = HSION, HSERDY = HSEON, PLLRDY = PLLON, PLLI2SRDY = PLLI2SON; CSR.LSIRDY = LSION; BDCR.LSERDY = LSEON;
CFGR.SWS follows CFGR.SW when the selected source is ready (otherwise SWS keeps the old source and one `warn`
is logged; silicon also refuses the switch).

Clock tree:
  HSI    = 16 MHz;  HSE = params `ext_hz` (default 8 MHz)
  PLL    = (PLLSRC ? HSE : HSI) / PLLM x PLLN / PLLP        (PLLP field 0..3 -> /2, /4, /6, /8)
  SYSCLK = HSI | HSE | PLL (CFGR.SWS);  HCLK (core) = SYSCLK / HPRE (1, 2, 4 ... 512)
  PCLK1  = HCLK / PPRE1, PCLK2 = HCLK / PPRE2 (1, 2, 4, 8, 16)
  timer clock on APBx = PCLKx when PPREx == 1, else 2 x PCLKx
Whenever HCLK changes the model calls chip.clock.set_hz(HCLK) unless `frozen` (params, Chip(clock_hz=...)
different from the chip YAML, or chip.clock_override set by Session for --clock), like kinetis.mcg.
`core_hz`, `apb1_hz`, `apb2_hz`, `timer_hz(bus)` are the derived values; `effective_apb_hz(bus)` /
`effective_timer_hz(bus)` scale them to the clock the core actually runs at (what USART/TIM models use).
Other models subscribe to clock-tree changes with `rcc.listeners.append(cb)` (cb() without arguments; also called when
only PLL48CLK = PLLQ output or I2SCLK (`i2s_clk_hz()`: PLLI2S = PLL input / PLLM x PLLI2SN / PLLI2SR, or the
I2S_CKIN pin frequency `params.i2s_ckin_hz` when CFGR.I2SSRC = 1) starts, stops or changes) and to clock
gate changes with `rcc.gate_listeners.append(cb)` (cb(enr_name, old, new) after a write that changes an xxxENR).

Clock gates: AHB1ENR/AHB2ENR/AHB3ENR/APB1ENR/APB2ENR; `clock_enabled(name)` / `check_gate(name, block)` (one
`warn` per peripheral when a gated-off block is accessed; silicon ignores such accesses).  Writing 1 to a bit of
AHBxRSTR/APBxRSTR resets the corresponding peripheral model.  CSR reset flags (PINRSTF/PORRSTF/BORRSTF at power-on,
SFTRSTF/IWDGRSTF/WWDGRSTF set by the respective resets) and BDCR survive system resets; CSR.RMVF clears the flags.
"""
from __future__ import annotations

from typing import Any, Callable

from ..base import MMIOBlock
from ..registry import register
from ._util import HSI_HZ, build_regs, fmt_hz

# CR
HSION, HSIRDY, HSEON, HSERDY, PLLON, PLLRDY, PLLI2SON, PLLI2SRDY = (1 << 0, 1 << 1, 1 << 16, 1 << 17, 1 << 24,
                                                                  1 << 25, 1 << 26, 1 << 27)
# CSR
LSION, LSIRDY, RMVF = 1 << 0, 1 << 1, 1 << 24
BORRSTF, PINRSTF, PORRSTF, SFTRSTF, IWDGRSTF, WWDGRSTF, LPWRRSTF = (1 << b for b in range(25, 32))
LSEON, LSERDY = 1 << 0, 1 << 1
HPRE_DIV = {8: 2, 9: 4, 10: 8, 11: 16, 12: 64, 13: 128, 14: 256, 15: 512}
PPRE_DIV = {4: 2, 5: 4, 6: 8, 7: 16}
SRC_NAMES = ("HSI", "HSE", "PLL", "?")

# gate name -> (register, bit)   (RM0090 7.3.10-7.3.14, STM32F405/407)
GATES: dict[str, tuple[str, int]] = {
    **{f"GPIO{p}": ("AHB1ENR", i) for i, p in enumerate("ABCDEFGHI")},
    "CRC": ("AHB1ENR", 12), "BKPSRAM": ("AHB1ENR", 18), "CCMDATARAM": ("AHB1ENR", 20), "DMA1": ("AHB1ENR", 21),
    "DMA2": ("AHB1ENR", 22), "ETH": ("AHB1ENR", 25), "OTG_HS": ("AHB1ENR", 29),
    "DCMI": ("AHB2ENR", 0), "CRYP": ("AHB2ENR", 4), "HASH": ("AHB2ENR", 5), "RNG": ("AHB2ENR", 6),
    "OTG_FS": ("AHB2ENR", 7), "FSMC": ("AHB3ENR", 0),
    "TIM2": ("APB1ENR", 0), "TIM3": ("APB1ENR", 1), "TIM4": ("APB1ENR", 2), "TIM5": ("APB1ENR", 3),
    "TIM6": ("APB1ENR", 4), "TIM7": ("APB1ENR", 5), "TIM12": ("APB1ENR", 6), "TIM13": ("APB1ENR", 7),
    "TIM14": ("APB1ENR", 8), "WWDG": ("APB1ENR", 11), "SPI2": ("APB1ENR", 14), "SPI3": ("APB1ENR", 15),
    "USART2": ("APB1ENR", 17), "USART3": ("APB1ENR", 18), "UART4": ("APB1ENR", 19), "UART5": ("APB1ENR", 20),
    "I2C1": ("APB1ENR", 21), "I2C2": ("APB1ENR", 22), "I2C3": ("APB1ENR", 23), "CAN1": ("APB1ENR", 25),
    "CAN2": ("APB1ENR", 26), "PWR": ("APB1ENR", 28), "DAC": ("APB1ENR", 29),
    "TIM1": ("APB2ENR", 0), "TIM8": ("APB2ENR", 1), "USART1": ("APB2ENR", 4), "USART6": ("APB2ENR", 5),
    "ADC1": ("APB2ENR", 8), "ADC2": ("APB2ENR", 9), "ADC3": ("APB2ENR", 10), "SDIO": ("APB2ENR", 11),
    "SPI1": ("APB2ENR", 12), "SYSCFG": ("APB2ENR", 14), "TIM9": ("APB2ENR", 16), "TIM10": ("APB2ENR", 17),
    "TIM11": ("APB2ENR", 18),
}
_RST_REG = {"AHB1ENR": "AHB1RSTR", "AHB2ENR": "AHB2RSTR", "AHB3ENR": "AHB3RSTR", "APB1ENR": "APB1RSTR",
            "APB2ENR": "APB2RSTR"}


@register("stm32.rcc")
class RCC(MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ctl = {"on_write": self._ctl_write}
        ov: dict[str, dict] = {"CR": ctl, "PLLCFGR": ctl, "CFGR": ctl, "PLLI2SCFGR": ctl,
                               "CSR": {"on_write": self._csr_write}, "BDCR": {"on_write": self._bdcr_write}}
        for r in _RST_REG.values():
            ov[r] = {"on_write": self._rstr_write}
        for r in _RST_REG:
            ov[r] = {"on_write": self._enr_write}
        r = build_regs((name, "RCC"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        for rn in ("AHB2ENR", "AHB2RSTR"):        # CRYP (bit 4) / HASH (bit 5): F415/F417 bits the F405 SVD omits
            if rn in self.regs:
                self.regs[rn].ro_mask &= ~0x30
        self.ext_hz = float(params.get("ext_hz", 8_000_000))
        self.i2s_ckin_hz = float(params.get("i2s_ckin_hz", 0.0))    # frequency on the I2S_CKIN pin (CFGR.I2SSRC=1)
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
        self._pll48 = 0.0                       # PLL48CLK last reported to listeners (SDIO, USB)
        self._i2s = 0.0                         # I2SCLK last reported to listeners (SPI2/SPI3 in I2S mode)
        self._powered = False
        self.pending_reset_flags = 0            # CSR flags the next reset sets (IWDG/WWDG/software)

    def attach(self, chip: Any) -> None:
        super().attach(chip)
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
            csr = (keep.get("CSR", 0) & 0xFE000000) | self.pending_reset_flags
            self.set("CSR", (self.get("CSR") & ~0xFE000000) | csr)
            self.set("BDCR", keep.get("BDCR", 0))
        self._powered = True
        self.pending_reset_flags = 0
        self._warned.clear()
        self._logged = None
        self.update()

    # ------------------------------------------------------------ gates
    def gate_location(self, name: str | None) -> tuple[int, int] | None:
        """(byte offset in self.mem, bit mask) of a clock-enable bit, for fast checks by gated models."""
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
        return PPRE_DIV.get(self._f("CFGR", 10 if bus == 1 else 13, 3), 1)

    def pll_in_hz(self) -> float:
        return self.ext_hz if self._f("PLLCFGR", 22, 1) else HSI_HZ

    def pll_hz(self) -> float:
        m = self._f("PLLCFGR", 0, 6)
        n = self._f("PLLCFGR", 6, 9)
        p = 2 * (self._f("PLLCFGR", 16, 2) + 1)
        if m < 2 or n < 2:
            return 0.0
        return self.pll_in_hz() / m * n / p

    def pll_q_hz(self) -> float:
        q = self._f("PLLCFGR", 24, 4)
        if q < 2:
            return 0.0
        m, n = self._f("PLLCFGR", 0, 6), self._f("PLLCFGR", 6, 9)
        return self.pll_in_hz() / m * n / q if m >= 2 and n >= 2 else 0.0

    def plli2s_hz(self) -> float:
        """PLLI2S output PLLI2SCLK = PLL input / PLLM x PLLI2SN / PLLI2SR (RM0090 7.3.23; 0 while PLLI2S is off or the
        dividers are invalid: N < 2, R < 2, M < 2)."""
        if not self.get("CR") & PLLI2SRDY:
            return 0.0
        m = self._f("PLLCFGR", 0, 6)
        n, r = self._f("PLLI2SCFGR", 6, 9), self._f("PLLI2SCFGR", 28, 3)
        if m < 2 or n < 2 or r < 2:
            return 0.0
        return self.pll_in_hz() / m * n / r

    def i2s_clk_hz(self) -> float:
        """I2SCLK of SPI2/SPI3 in I2S mode: PLLI2SCLK (CFGR.I2SSRC = 0) or the I2S_CKIN pin (`i2s_ckin_hz`)."""
        return self.i2s_ckin_hz if self._f("CFGR", 23, 1) else self.plli2s_hz()

    def src_hz(self, sws: int) -> float:
        return (HSI_HZ, self.ext_hz, self.pll_hz(), HSI_HZ)[sws & 3]

    def timer_hz(self, bus: int) -> float:
        apb = self.apb1_hz if bus == 1 else self.apb2_hz
        return apb if self.ppre(bus) == 1 else 2 * apb

    def effective_apb_hz(self, bus: int) -> float:
        """PCLKx consistent with the clock the core actually runs at (honours --clock overrides)."""
        hz = self.chip.clock.hz if self.chip is not None else self.core_hz
        return hz / self.ppre(bus)

    def effective_timer_hz(self, bus: int) -> float:
        apb = self.effective_apb_hz(bus)
        return apb if self.ppre(bus) == 1 else 2 * apb

    # ------------------------------------------------------------ register hooks
    def _ready(self, sw: int) -> bool:
        cr = self.get("CR")
        return bool(cr & (HSIRDY, HSERDY, PLLRDY, 0)[sw & 3]) if sw != 3 else False

    def _ctl_write(self, blk, reg, old, new):
        if reg.name == "CR":
            rdy = 0
            if new & HSION:
                rdy |= HSIRDY
            if new & HSEON:
                rdy |= HSERDY
            if new & PLLON:
                rdy |= PLLRDY
            if new & PLLI2SON:
                rdy |= PLLI2SRDY
            sws = self._f("CFGR", 2, 2)
            if sws == 0 and not new & HSION:            # HSI cannot be stopped while it clocks the system
                new |= HSION
                rdy |= HSIRDY
            if sws == 2 and not new & PLLON:
                new |= PLLON
                rdy |= PLLRDY
            new = (new & ~(HSIRDY | HSERDY | PLLRDY | PLLI2SRDY)) | rdy
            self._store(reg, new)
        if old != new:
            self.update()
        return None

    def _csr_write(self, blk, reg, old, new):
        v = (new & ~LSIRDY) | (LSIRDY if new & LSION else 0)
        if new & RMVF:
            v &= ~(0xFE000000 | RMVF)
        return v

    def _bdcr_write(self, blk, reg, old, new):
        return (new & ~LSERDY) | (LSERDY if new & LSEON else 0)

    def _enr_write(self, blk, reg, old, new):
        if old != new:
            for cb in list(self.gate_listeners):
                cb(reg.name, old, new)
        return None

    def _rstr_write(self, blk, reg, old, new):
        rising = new & ~old
        if rising and self.chip is not None:
            enr = next(e for e, r in _RST_REG.items() if r == reg.name)
            for gname, (greg, bit) in GATES.items():
                if greg == enr and rising >> bit & 1:
                    blk2 = self.chip.peripherals.get(gname) or next(
                        (b for b in self.chip.peripherals.values() if getattr(b, "gate", None) == gname), None)
                    if blk2 is not None and blk2 is not self:     # (by gate name: Ethernet_MAC has gate "ETH")
                        blk2.reset()
                        self.emit("info", block=self.name, msg=f"{gname} reset through RCC_{reg.name}")
        return None

    # ------------------------------------------------------------ update
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
        key = (round(self.core_hz), round(self.apb1_hz), round(self.apb2_hz), self.source)
        pll48 = self.pll_q_hz() if self.get("CR") & PLLRDY else 0.0
        i2s = self.i2s_clk_hz()
        if key == self._logged:
            if pll48 != self._pll48 or i2s != self._i2s:    # PLL48CLK / I2SCLK changed without a bus clock change
                self._pll48 = pll48
                self._i2s = i2s
                for cb in list(self.listeners):
                    cb()
            return
        self._pll48 = pll48
        self._i2s = i2s
        self._logged = key
        chip = self.chip
        if chip is not None and getattr(chip, "clock_override", None):
            self.frozen = True
        if chip is not None:
            if self.core_hz > 168e6 * 1.001:
                self.emit("warn", block=self.name, msg=f"RCC: HCLK {fmt_hz(self.core_hz)} exceeds the 168 MHz maximum")
            if self.apb1_hz > 42e6 * 1.001 or self.apb2_hz > 84e6 * 1.001:
                self.emit("warn", block=self.name, msg=f"RCC: APB clock above maximum (APB1 {fmt_hz(self.apb1_hz)} "
                          f"> 42 MHz or APB2 {fmt_hz(self.apb2_hz)} > 84 MHz)")
            msg = (f"core clock {self.core_hz / 1e6:.3f} MHz (SYSCLK {self.source} {self.sysclk_hz / 1e6:.3f} MHz), "
                   f"APB1 {self.apb1_hz / 1e6:.3f} MHz, APB2 {self.apb2_hz / 1e6:.3f} MHz")
            if self.frozen:
                msg += f" (clock frozen at {chip.clock.hz / 1e6:.3f} MHz by override)"
            self.emit("info", block=self.name, msg=msg, core_hz=self.core_hz, apb1_hz=self.apb1_hz,
                      apb2_hz=self.apb2_hz, source=self.source)
            if not self.frozen and self.core_hz > 0 and chip.clock.hz != self.core_hz:
                chip.clock.set_hz(self.core_hz)
        for cb in list(self.listeners):
            cb()
