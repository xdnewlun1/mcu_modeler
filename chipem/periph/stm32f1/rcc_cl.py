"""STM32F105/F107 connectivity-line RCC (RM0008 ch. 8 "Connectivity line devices: reset and clock control").

A subclass of `stm32f1.rcc` (RM0008 ch. 7, chipem/periph/stm32f1/rcc.py), which already implements everything the
two RCCs share (HSI/HSE/LSI/LSE, SYSCLK switch, AHB/APB prescalers, BDCR, CSR reset flags, CSS, Stop entry).  This
module adds the connectivity-line clock tree and registers, checked against RM0008 Rev 21 ch. 8:

Clock tree (8.2, Figure 11):
  PREDIV2  = HSE / (CFGR2.PREDIV2 + 1)                                        (8.3.12, /1 .. /16)
  PLL2     = PREDIV2 x PLL2MUL,  PLL3 = PREDIV2 x PLL3MUL   (x8 .. x14, x16, x20; 00xx, 010x, 1101 reserved, 8.3.12)
  PREDIV1  = (CFGR2.PREDIV1SRC ? PLL2 : HSE) / (CFGR2.PREDIV1 + 1)            (8.3.12; PREDIV1 bit 0 = CFGR.PLLXTPRE)
  PLL      = (CFGR.PLLSRC ? PREDIV1 : HSI / 2) x PLLMUL   (0010..0111 = x4..x9, 1101 = x6.5; others reserved, 8.3.2)
  OTGFSCLK = PLL VCO (2 x PLLCLK) / 3 (CFGR.OTGFSPRE = 0) or / 2 (OTGFSPRE = 1); must be 48 MHz (8.2, 8.3.2)
  I2S2CLK / I2S3CLK = SYSCLK (CFGR2.I2SxSRC = 0) or PLL3 VCO = 2 x PLL3CLK (I2SxSRC = 1)                  (8.2, 8.3.12)
  MCO      = CFGR.MCO[3:0] (bits 27:24, 8.2.10 / 8.3.2): 00xx none, 0100 SYSCLK, 0101 HSI, 0110 HSE, 0111 PLL / 2,
             1000 PLL2, 1001 PLL3 / 2, 1010 XT1 (the 3-25 MHz oscillator, for the Ethernet PHY), 1011 PLL3; 11xx none
  Ethernet MAC clocks come from the external PHY (or from MCO wired to the PHY / REF_CLK; stm32f1.eth).
  The ST reference clock tree (25 MHz crystal): PREDIV2 /5 = 5 MHz, PLL2 x8 = 40 MHz, PREDIV1 (PLL2) /5 = 8 MHz,
  PLL x9 = 72 MHz SYSCLK, OTGFSCLK = 144 / 3 = 48 MHz, PLL3 x10 = 50 MHz (RMII REF_CLK on MCO).

Registers (8.3; reset values Table 19):
* CR: PLL2ON / PLL2RDY (bits 26/27), PLL3ON / PLL3RDY (28/29) next to the ch. 7 bits.  PLL2RDY / PLL3RDY follow
  PLL2ON / PLL3ON after the lock time (params `pll2_lock_ns`, `pll3_lock_ns`; default 0 = at once, as the base
  model's `pll_lock_ns`).  PLL2ON cannot be cleared while PLL2 is used indirectly as SYSCLK (PLL fed by PREDIV1 with
  PREDIV1SRC = PLL2, PLL selected or in use as SYSCLK, 8.3.1); the HSE cannot be stopped while it feeds SYSCLK
  through PLL2 either (8.3.1 HSEON).  A PLL whose PLLMUL / PLLxMUL is a reserved value never locks (PLLxRDY stays 0,
  one `warn`): the RM gives no frequency for them.
* CFGR: PLLMUL table above, OTGFSPRE (bit 22: cannot be cleared while the OTG FS clock is enabled, 8.3.2), MCO 4 bits.
  PLLSRC / PLLXTPRE / PLLMUL are locked while the PLL is on (base model).  PLLXTPRE is the same bit as CFGR2 bit 0:
  writing either changes both (8.3.2, 8.3.12).
* CIR: PLL2RDYF / PLL3RDYF (bits 5/6), PLL2RDYIE / PLL3RDYIE (13/14), PLL2RDYC / PLL3RDYC (21/22) (8.3.3).
* AHBRSTR (0x28, 8.3.11): OTGFSRST (12) and ETHMACRST (14) reset the OTG_FS / Ethernet models.
* AHBENR (8.3.6, reset 0x14): OTGFSEN (12), ETHMACEN (14), ETHMACTXEN (15), ETHMACRXEN (16) besides DMA1/2, SRAM,
  FLITF, CRC.  APB1ENR / APB1RSTR: CAN1 (25) and CAN2 (26) (8.3.5, 8.3.8); no USB device, TIM12-14, FSMC, SDIO,
  TIM8, ADC3, GPIOF/G on this line (their bits are reserved, read 0).
* CFGR2 (0x2C, 8.3.12): PREDIV1 / PREDIV1SRC writable only while the PLL is off, PLL2MUL only while PLL2 is off,
  PLL3MUL only while PLL3 is off, PREDIV2 only while PLL2 and PLL3 are both off (writes keep the old field, one
  `warn` each); I2S2SRC / I2S3SRC always writable.
* Stop / Standby entry clears PLL2ON and PLL3ON as well (8.3.1).
Limits checked with one `warn` each, besides the base model's: OTG FS clock enabled (AHBENR.OTGFSEN) while OTGFSCLK is
not 48 MHz +-0.25 % ("For proper USB OTG FS operation, the PLL should be configured to output 72 MHz or 48 MHz", 8.2);
HCLK below 25 MHz with the Ethernet MAC clock enabled (8.2 / 8.3.2 HPRE caution).  The PLL2/PLL3 input and output
ranges of the data sheet (DS6014, not available here) are not checked.

Interfaces for other models: `GATES` names "OTG_FS" (AHBENR 12; also aliased "USB" so the base model's USB-clock
rules apply to the OTG FS clock), "ETH" (14), "ETHMACTX" (15), "ETHMACRX" (16), "CAN1", "CAN2"; `clk48_hz()` /
`pll_q_hz()` (stm32.otg_fs), `i2s_kernel_hz(block)` (stm32.spi I2S), `mco_hz()` / `mco_enabled()` (stm32f1.gpio,
stm32f1.eth), `pll2_hz()`, `pll3_hz()`, `predivs()`.
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ._util import HSI_HZ, fmt_hz
from .rcc import (CSSC, CSSF, GATES as _GATES, HSEON, HSERDY, HSIRDY, OSCS as _OSCS, PLL_CFG_MASK, PLLON, PLLRDY,
                  PLLSRC, PLLXTPRE, RCC as _RCC)

# CR (8.3.1)
PLL2ON, PLL2RDY, PLL3ON, PLL3RDY = 1 << 26, 1 << 27, 1 << 28, 1 << 29
# CFGR (8.3.2)
OTGFSPRE = 1 << 22
# CIR (8.3.3)
PLL2RDYF, PLL3RDYF = 1 << 5, 1 << 6
CIR_FLAGS = 0x7F                       # LSI, LSE, HSI, HSE, PLL, PLL2, PLL3 ready flags (bits 0-6)
# CFGR2 (8.3.12)
PREDIV1_M, PREDIV2_M, PLL2MUL_M, PLL3MUL_M = 0xF, 0xF << 4, 0xF << 8, 0xF << 12
PREDIV1SRC, I2S2SRC, I2S3SRC = 1 << 16, 1 << 17, 1 << 18
# PLLMUL (CFGR bits 21:18): 0010..0111 -> x4..x9, 1101 -> x6.5 (8.3.2); PLL2MUL / PLL3MUL (8.3.12)
PLLMUL_CL = {2: 4.0, 3: 5.0, 4: 6.0, 5: 7.0, 6: 8.0, 7: 9.0, 13: 6.5}
PLLXMUL = {6: 8, 7: 9, 8: 10, 9: 11, 10: 12, 11: 13, 12: 14, 14: 16, 15: 20}
MCO_NAMES = {4: "SYSCLK", 5: "HSI", 6: "HSE", 7: "PLL/2", 8: "PLL2", 9: "PLL3/2", 10: "XT1", 11: "PLL3"}

# Gates (8.3.6-8.3.8): the ch. 7 table minus what the connectivity line does not have, plus its own bits.
_NOT_CL = {"FSMC", "SDIO", "TIM8", "ADC3", "GPIOF", "GPIOG", "TIM9", "TIM10", "TIM11", "TIM12", "TIM13", "TIM14",
           "USB", "CAN"}
GATES_CL: dict[str, tuple[str, int]] = {k: v for k, v in _GATES.items() if k not in _NOT_CL}
GATES_CL.update({"OTG_FS": ("AHBENR", 12), "ETH": ("AHBENR", 14), "ETHMACTX": ("AHBENR", 15),
                 "ETHMACRX": ("AHBENR", 16), "CAN1": ("APB1ENR", 25), "CAN2": ("APB1ENR", 26),
                 "USB": ("AHBENR", 12),       # alias: the base model's USB-clock rules (USBPRE) apply to OTGFSEN
                 "OTG": ("AHBENR", 12)})      # alias: stm32f1.gpio maps the OTG_FS_* signals to block "OTG"
RST_REGS_CL = {"APB1ENR": "APB1RSTR", "APB2ENR": "APB2RSTR", "AHBENR": "AHBRSTR"}
OSCS_CL = dict(_OSCS)
OSCS_CL.update({"PLL2": ("CR", PLL2ON, PLL2RDY, PLL2RDYF, "pll2_lock_ns"),
                "PLL3": ("CR", PLL3ON, PLL3RDY, PLL3RDYF, "pll3_lock_ns")})


@register("stm32f1.rcc_cl")
class RCCCL(_RCC):
    GATES = GATES_CL
    RST_REGS = RST_REGS_CL
    OSCS = OSCS_CL
    PLL_CFG_MASK = PLL_CFG_MASK

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        params.setdefault("ext_hz", 25_000_000)          # the 25 MHz crystal of ST's reference designs (8.2)
        super().__init__(name, base, size, regs, params=params, log=log)
        cf2 = self.regs.get("CFGR2")
        if cf2 is not None:
            cf2.on_write = self._cfgr2_write

    # ------------------------------------------------------------ derived clocks (8.2)
    def predivs(self) -> tuple[int, int]:
        """(PREDIV1, PREDIV2) division factors."""
        c2 = self.get("CFGR2")
        return (c2 & PREDIV1_M) + 1, ((c2 & PREDIV2_M) >> 4) + 1

    def _pllx_mul(self, which: int) -> int | None:
        v = (self.get("CFGR2") >> (8 if which == 2 else 12)) & 0xF
        return PLLXMUL.get(v)

    def pll2_hz(self) -> float:
        """PLL2CLK = HSE / PREDIV2 x PLL2MUL (0 for a reserved multiplier)."""
        m = self._pllx_mul(2)
        return self.ext_hz / self.predivs()[1] * m if m else 0.0

    def pll3_hz(self) -> float:
        m = self._pllx_mul(3)
        return self.ext_hz / self.predivs()[1] * m if m else 0.0

    def pll_mul(self) -> float:                                   # type: ignore[override]
        return PLLMUL_CL.get(self._f("CFGR", 18, 4), 0.0)

    def prediv1_in_hz(self) -> float:
        return self.pll2_hz() if self.get("CFGR2") & PREDIV1SRC else self.ext_hz

    def pll_in_hz(self) -> float:
        if not self.get("CFGR") & PLLSRC:
            return HSI_HZ / 2
        return self.prediv1_in_hz() / self.predivs()[0]

    def pll_uses_pll2(self) -> bool:
        return bool(self.get("CFGR") & PLLSRC and self.get("CFGR2") & PREDIV1SRC)

    def otgfs_hz(self) -> float:
        """OTGFSCLK = PLL VCO (2 x PLLCLK) / 3 (OTGFSPRE = 0) or / 2 (OTGFSPRE = 1); 0 while the PLL is not locked."""
        if not self.get("CR") & PLLRDY:
            return 0.0
        return 2 * self.pll_hz() / (2 if self.get("CFGR") & OTGFSPRE else 3)

    def usb_hz(self) -> float:                                    # the base model's USB checks use this
        return self.otgfs_hz()

    def clk48_hz(self) -> float:
        """48 MHz clock of the OTG FS PHY (stm32.otg_fs)."""
        return self.otgfs_hz()

    def pll_q_hz(self) -> float:
        return self.otgfs_hz()

    def i2s_kernel_hz(self, block: str) -> float:
        """I2S2CLK (SPI2) / I2S3CLK (SPI3): SYSCLK or the PLL3 VCO (2 x PLL3CLK, needs PLL3RDY) (8.2, 8.3.12)."""
        bit = I2S3SRC if block.upper().endswith("3") else I2S2SRC
        if not self.get("CFGR2") & bit:
            return self.sysclk_hz
        return 2 * self.pll3_hz() if self.get("CR") & PLL3RDY else 0.0

    def mco_enabled(self) -> bool:
        return self._f("CFGR", 24, 4) in MCO_NAMES

    def mco_hz(self) -> float:
        """Clock on the MCO pin (CFGR.MCO[3:0], 8.2.10); 0 when the selected source is not running."""
        sel = self._f("CFGR", 24, 4)
        cr = self.get("CR")
        hse = self.ext_hz if cr & HSERDY else 0.0
        return {4: self.sysclk_hz, 5: HSI_HZ if cr & HSIRDY else 0.0, 6: hse,
                7: self.pll_hz() / 2 if cr & PLLRDY else 0.0, 8: self.pll2_hz() if cr & PLL2RDY else 0.0,
                9: self.pll3_hz() / 2 if cr & PLL3RDY else 0.0, 10: hse,
                11: self.pll3_hz() if cr & PLL3RDY else 0.0}.get(sel, 0.0)

    # ------------------------------------------------------------ oscillators
    def osc_on(self, name: str) -> bool:
        on = super().osc_on(name)
        if on and name in ("PLL", "PLL2", "PLL3"):
            reserved = (self.pll_mul() == 0.0) if name == "PLL" else (self._pllx_mul(2 if name == "PLL2" else 3)
                                                                      is None)
            if reserved:
                fld = "RCC_CFGR.PLLMUL" if name == "PLL" else f"RCC_CFGR2.{name}MUL"
                self._warn_once(f"mul{name}", f"{fld} is a reserved value: {name} does not lock (RM0008 "
                                f"{'8.3.2' if name == 'PLL' else '8.3.12'})")
                return False
        return on

    def _sysclk_users(self) -> int:
        keep = super()._sysclk_users()
        if keep & PLLON and self.pll_uses_pll2():
            keep |= PLL2ON | HSEON                 # PLL2 (and the HSE behind it) indirectly used as SYSCLK (8.3.1)
        return keep

    # ------------------------------------------------------------ register hooks
    def _cr_write(self, blk, reg, old, new):
        # the base model keeps the ON bits of `_sysclk_users()` (PLL2ON / HSEON included here, 8.3.1)
        v = super()._cr_write(blk, reg, old, new)
        changed = False
        for name in ("PLL2", "PLL3"):
            changed |= self._osc_update(name)
        v = self.get("CR")
        if changed:
            self.update()
            self._notify()
        return v

    def _cfgr_write_extra(self, old: int, new: int) -> int:
        if old & OTGFSPRE and not new & OTGFSPRE and self.clock_enabled("OTG_FS"):
            new |= OTGFSPRE                     # (the base model checks the "USB" alias first; kept for clarity)
        if (old ^ new) & PLLXTPRE:              # PLLXTPRE is CFGR2 bit 0 (8.3.2)
            c2 = self.get("CFGR2")
            self.set("CFGR2", (c2 & ~1) | (1 if new & PLLXTPRE else 0))
        return new

    def _cfgr2_write(self, blk, reg, old, new):
        cr = self.get("CR")
        locks = ((PREDIV1_M | PREDIV1SRC, cr & (PLLON | PLLRDY), "PREDIV1/PREDIV1SRC", "the PLL"),
                 (PLL2MUL_M, cr & (PLL2ON | PLL2RDY), "PLL2MUL", "PLL2"),
                 (PLL3MUL_M, cr & (PLL3ON | PLL3RDY), "PLL3MUL", "PLL3"),
                 (PREDIV2_M, cr & (PLL2ON | PLL2RDY | PLL3ON | PLL3RDY), "PREDIV2", "PLL2 or PLL3"))
        for mask, locked, fld, what in locks:
            if locked and (old ^ new) & mask:
                new = (new & ~mask) | (old & mask)
                self._warn_once(f"cfgr2{fld}", f"RCC_CFGR2.{fld} written while {what} is on: ignored (RM0008 8.3.12)")
        self._store(reg, new)
        cfgr = self.get("CFGR")
        if bool(cfgr & PLLXTPRE) != bool(new & 1):                # bit 0 is CFGR.PLLXTPRE (8.3.12 note)
            self.set("CFGR", cfgr ^ PLLXTPRE)
        if old != new:
            self.update()
            self._notify()
        return new

    def _cir_write(self, blk, reg, old, new):
        clear = ((new >> 16) & CIR_FLAGS) | (CSSF if new & CSSC else 0)
        v = (old & (CIR_FLAGS | CSSF) & ~clear) | (new & (CIR_FLAGS << 8))
        self._store(reg, v)
        self._update_irq()
        return v

    def _update_irq(self) -> None:
        cir = self.get("CIR")
        lvl = bool((cir & CIR_FLAGS) & (cir >> 8) & CIR_FLAGS)
        if lvl != self._line and self.chip is not None and self._irqn is not None:
            self._line = lvl
            self.irq(self._irqn, lvl)

    def _enr_write(self, blk, reg, old, new):
        r = super()._enr_write(blk, reg, old, new)
        if reg.name == "AHBENR" and (new & ~old) & (1 << 14):
            self._eth_check()
        return r

    # ------------------------------------------------------------ low power
    def enter_stop(self) -> None:
        """Stop / Standby entry also clears PLL2ON and PLL3ON (8.3.1)."""
        for name in ("PLL2", "PLL3"):
            self._cancel_osc(name)
        self.set("CR", self.get("CR") & ~(PLL2ON | PLL2RDY | PLL3ON | PLL3RDY))
        super().enter_stop()

    # ------------------------------------------------------------ checks
    def _usb_check(self) -> None:
        if not self.clock_enabled("OTG_FS"):
            return
        if self.get("CR") & PLLON and not self.get("CFGR") & PLLSRC:
            self._warn_once("usbhsi", "RCC: OTG FS clock enabled with the PLL fed by HSI/2: OTGFSCLK must come from "
                            "the HSE crystal (RM0008 8.2)")
        u = self.otgfs_hz()
        if abs(u - 48e6) > 48e6 * 0.0025:
            self._warn_once("usb48", f"RCC: OTG FS clock enabled with OTGFSCLK = {fmt_hz(u)} (must be 48 MHz: PLL "
                            f"72 MHz with OTGFSPRE = 0 or PLL 48 MHz with OTGFSPRE = 1, RM0008 8.2 / 8.3.2)")

    def _eth_check(self) -> None:
        if self.core_hz < 25e6 * 0.999:
            self._warn_once("eth25", f"RCC: Ethernet MAC clock enabled with HCLK = {fmt_hz(self.core_hz)}: the AHB "
                            f"clock must be at least 25 MHz when the Ethernet is used (RM0008 8.2)")

    def update(self) -> None:
        super().update()
        if self.chip is not None and self.clock_enabled("ETH"):
            self._eth_check()
