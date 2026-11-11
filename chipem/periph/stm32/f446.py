"""STM32F446xx system blocks, checked against RM0390 Rev 9 (docs/datasheets/st/RM0390_stm32f446_reference_manual.pdf):
RCC, PWR, FLASH, SYSCFG, the FMC register differences, the DMA request maps and the device electronic signature.
Subclasses of the STM32F42x models (f42x.py: same over-drive / PLLSAI IP); only the F446 differences are layered on
top.  The data sheet DS10693 is not in the repo (frequency limits per voltage scale come from the RM0390 3.4.1 note).

stm32.rcc_f446 (RM0390 ch. 6):
* Every register's reset value and writable bits follow 6.3.1-6.3.27 (`_RM_REGS`); reserved bits read 0 (or their
  reset value) and ignore writes.  The SVD gets AHB1ENR (CCMDATARAMEN), AHB1LPENR / AHB2LPENR (F405 values),
  APB1RSTR.CECRST, CFGR bit 23, PLLCFGR.PLLR, CR.PLLSAION and the PLLI2S / PLLSAI reset values wrong.
* PLLCFGR.PLLR (30:28): PLL_R = VCO / PLLR (2..7); CFGR.SW/SWS = 11 selects PLL_R (6.3.3); like PLL_P it needs PLLRDY,
  and PLLON / the PLL's oscillator cannot be switched off while PLL_R is SYSCLK (6.3.1).
* PLLSAI (6.3.24): own M divider (5:0), N, P (/2,4,6,8: 48 MHz option, at most 120 MHz), Q; no PLLSAI_R / LCD clock.
  PLLI2S (6.3.23): own M, N, P (SPDIFRX option, at most 120 MHz), Q (SAI), R (I2S, at most 192 MHz).  All three PLLs:
  VCO input 1..2 MHz, N 50..432, VCO 100..432 MHz; PLL_P / PLL_R at most 180 MHz as SYSCLK.  Violations: one `warn`
  each (the nominal frequency is still used), as for the other STM32 RCC models.  No lock times.
* DCKCFGR (6.3.25): PLLI2SDIVQ (4:0) / PLLSAIDIVQ (12:8) /1../32; SAI1SRC (21:20): 00 PLLSAI_Q / PLLSAIDIVQ, 01 PLLI2S_Q
  / PLLI2SDIVQ, 10 PLL_R, 11 I2S_CKIN (`params.i2s_ckin_hz`); SAI2SRC (23:22): the same but 11 = HSI or HSE (PLLSRC)
  -- `sai_kernel_hz(name)` for stm32.sai; TIMPRE (24) as on the F42x; I2S1SRC (26:25, APB1 I2S2/I2S3) / I2S2SRC
  (28:27, APB2 I2S1/SPI4): 00 PLLI2S_R, 01 I2S_CKIN, 10 PLL_R, 11 HSI or HSE -- `i2s_kernel_hz(spi_name)`.
* DCKCFGR2 (6.3.27): CK48MSEL (27) PLL_Q / PLLSAI_P -- `clk48_hz()` for OTG FS/HS; SDIOSEL (28) CK48 / SYSCLK --
  `sdio_kernel_hz()`; FMPI2C1SEL (23:22) 00/11 APB1, 01 SYSCLK, 10 HSI -- `fmpi2c_clk_hz()`; CECSEL (26) HSI/488 or
  LSE (0 Hz while LSERDY = 0) -- `cec_clk_hz()`; SPDIFRXSEL (29) PLL_R or PLLI2S_P -- `spdifrx_clk_hz()`.
  CKGATENR (6.3.26) is storage (clock gating of the bus bridges is not modelled).
* Limits: HCLK 120 / 144 (168 with over-drive) / 168 (180 with over-drive) MHz in voltage scale 3 / 2 / 1 (RM0390
  3.4.1 note), APB1 45 MHz, APB2 90 MHz (6.3.3); FLASH_ACR.LATENCY against Table 5 (3.4.1) for `params.vdd_range`
  ("2.7-3.6" default, "2.4-2.7", "2.1-2.4", "1.8-2.1": 30 / 24 / 22 / 20 MHz per wait state): one `warn` when too low.

stm32.pwr_f446 (RM0390 5.4): stm32.pwr_f42x (over-drive / under-drive / VOS, 5.1.3) plus CR FISSR/FMSSR (21:20) and
ADCDC1 (13) (stored); CSR reset 0, VOSRDY = main PLL locked; EWUP1 (PA0) and EWUP2 (PC13) wake-up pins.

stm32.flash_f446 (RM0390 ch. 3): one bank, sectors 0-3 16 KB, 4 64 KB, 5-7 128 KB (Table 4; the 256 KB "C" parts end
at sector 5); SNB > 7 erases nothing (`warn`); ACR.LATENCY 4 bits (3.8.1); SR.RDERR rc_w1 (3.8.4, never set: PCROP is
not enforced); OPTCR nWRP[7:0] (23:16), SPRMOD (31) stored; no OPTCR1, no DB1M / MER1.

stm32.syscfg_f446 (RM0390 8.2), stm32.fmc_f446 (ch. 11), stm32.sysmem_f446 (ch. 34): see the class docstrings.

DMA request maps "f446" (chip YAML `params: {requests: f446}`): RM0390 Tables 28 / 29, every entry checked.

SPDIFRX, HDMI-CEC and FMPI2C1 are real models: spdifrx.py, cec.py, fmpi2c.py.
"""
from __future__ import annotations

from typing import Any

from ..base import Reg, pure_read
from ..registry import register
from . import dma as _dma
from ._util import HSI_HZ
from .f42x import FLASHF42x, PWRF42x, RCCF42x
from ._util import rcc as rcc_of
from .rcc import HSEON, HSION, PLLON, PLLRDY
from ..stm32f1.system import SystemMemory
from .exti import SYSCFG
from .fmc import FMC
from .system import CSR_VOSRDY

CSR_EWUP2 = 1 << 7

PLLSAIRDY = 1 << 29
PLLI2SRDY = 1 << 27


def _reraw(reg: Reg, mem: bytearray | None = None, *, reset: int | None = None, ro: int | None = None) -> None:
    r0, ro0, w1c = reg._raw
    reg._raw = (r0 if reset is None else reset, ro0 if ro is None else ro & 0xFFFFFFFF, w1c)
    reg.set_unit_bits(reg.unit_bits)
    if mem is not None:
        mem[reg.boff:reg.boff + reg.bw] = reg.reset.to_bytes(reg.bw, "little")


# ---------------------------------------------------------------------------------------------------------- RCC
# register -> (reset value or None = keep, writable bits), RM0390 6.3.1-6.3.27 (bit diagrams and reset values).
# Status bits (xxxRDY, SWS, CIR flags) are set by the model's hooks, not by software.
_RM_REGS: dict[str, tuple[int | None, int]] = {
    "CR": (None, 0x150D00F9),            # 6.3.1: HSION, HSITRIM, HSEON, HSEBYP, CSSON, PLLON, PLLI2SON, PLLSAION
    "PLLCFGR": (0x24003010, 0x7F437FFF),  # 6.3.2: PLLM, PLLN, PLLP, PLLSRC, PLLQ, PLLR
    "CFGR": (0x00000000, 0xFF7FFCF3),     # 6.3.3: bit 23 reserved (no I2SSRC on the F446), 9:8 reserved
    "CIR": (0x00000000, 0x00FF7F00),      # 6.3.4
    "AHB1RSTR": (0, 0x206010FF),          # 6.3.5: GPIOA-H, CRC, DMA1/2, OTGHS
    "AHB2RSTR": (0, 0x00000081),          # 6.3.6: DCMI, OTGFS
    "AHB3RSTR": (0, 0x00000003),          # 6.3.7: FMC, QSPI
    "APB1RSTR": (0, 0x3FFFC9FF),          # 6.3.8: incl. SPDIFRX 16, FMPI2C1 24, CEC 27
    "APB2RSTR": (0, 0x00C77933),          # 6.3.9: ADCRST (8) for all ADCs, SAI1/SAI2 22/23
    "AHB1ENR": (0, 0x606410FF),           # 6.3.10: no CCMDATARAMEN (bit 20 reserved, reset 0)
    "AHB2ENR": (0, 0x00000081),           # 6.3.11
    "AHB3ENR": (0, 0x00000003),           # 6.3.12
    "APB1ENR": (0, 0x3FFFC9FF),           # 6.3.13
    "APB2ENR": (0, 0x00C77F33),           # 6.3.14
    "AHB1LPENR": (0x606790FF, 0x606790FF),   # 6.3.15 (incl. FLITF 15, SRAM1/2 16/17, BKPSRAM 18)
    "AHB2LPENR": (0x00000081, 0x00000081),   # 6.3.16
    "AHB3LPENR": (0x00000003, 0x00000003),   # 6.3.17
    "APB1LPENR": (0x3FFFC9FF, 0x3FFFC9FF),   # 6.3.18
    "APB2LPENR": (0x00C77F33, 0x00C77F33),   # 6.3.19
    "SSCGR": (0, 0xCFFFFFFF),             # 6.3.22
    "PLLI2SCFGR": (0x24003010, 0x7F037FFF),  # 6.3.23: PLLI2SM, N, P, Q, R
    "PLLSAICFGR": (0x04003010, 0x0F037FFF),  # 6.3.24: PLLSAIM, N, P, Q (no PLLSAIR)
    "DCKCFGR": (0, 0x1FF01F1F),           # 6.3.25
    "CKGATENR": (0, 0x0000007F),          # 6.3.26
    "DCKCFGR2": (0, 0x3CC00000),          # 6.3.27
}
# FLASH_ACR.LATENCY: one more wait state per this many MHz of HCLK (RM0390 3.4.1 Table 5, by supply range)
WS_STEP_MHZ = {"2.7-3.6": 30.0, "2.4-2.7": 24.0, "2.1-2.4": 22.0, "1.8-2.1": 20.0}


@register("stm32.rcc_f446")
class RCCF446(RCCF42x):
    model = "stm32.rcc_f446"
    SRC_NAMES = ("HSI", "HSE", "PLL_P", "PLL_R")

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, regs, params=params, log=log)
        params = dict(params or {})
        # reset values and writable bits of RM0390 6.3 (the SVD carries F405 values for several of them; the F42x
        # parent made F42x-only bits writable): every reserved bit reads 0 and ignores writes
        for rn, (reset, wmask) in _RM_REGS.items():
            rg = self.regs.get(rn)
            if rg is not None:
                _reraw(rg, self.mem, reset=rg.reset if reset is None else reset, ro=~wmask)
        self.vdd_range = str(params.get("vdd_range", "2.7-3.6"))
        if self.vdd_range not in WS_STEP_MHZ:
            raise ValueError(f"{name}: params.vdd_range must be one of {sorted(WS_STEP_MHZ)}, not {self.vdd_range!r}")
        self._flash: Any = None
        self._ws_warned: set = set()
        if "DCKCFGR2" in self.regs:
            self.regs["DCKCFGR2"].on_write = self._ctl_write
        self._f446: tuple = ()

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        from ._util import find_model
        self._flash = find_model(chip, "stm32.flash", "FLASH")

    def reset(self) -> None:
        self._ws_warned = set()
        super().reset()

    # ------------------------------------------------------------ registers
    def _ctl_write(self, blk, reg, old, new):
        if reg.name == "CR" and ((self.get("CFGR") >> 2) & 3) == 3:
            # PLL_R is SYSCLK: PLLON cannot be reset while the PLL is the system clock, nor HSEON / HSION while that
            # oscillator feeds it (RM0390 6.3.1)
            new |= PLLON | (HSEON if self._f("PLLCFGR", 22, 1) else HSION)
            self._store(reg, new)
        return super()._ctl_write(blk, reg, old, new)

    def _ready(self, sw: int) -> bool:
        if sw == 3:
            return bool(self.get("CR") & PLLRDY)
        return super()._ready(sw)

    def _bad(self, key: str, cond: bool, what: str, ref: str) -> None:
        if cond and key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=f"RCC: {what} is a wrong configuration (RM0390 {ref})")

    def _vco(self, reg: str, ref: str, tag: str) -> float:
        """VCO of the main PLL (PLLCFGR), PLLI2S or PLLSAI, each with its own M divider (RM0390 6.3.2 / 6.3.23 /
        6.3.24): input = PLL source / M in 1..2 MHz, N in 50..432, VCO in 100..432 MHz (one `warn` each; the nominal
        frequency is still used).  M or N below 2 -> 0 Hz."""
        m, n = self._f(reg, 0, 6), self._f(reg, 6, 9)
        if m < 2 or n < 2:
            return 0.0
        vin = self.pll_in_hz() / m
        vco = vin * n
        self._bad(f"{tag}in", not 0.95e6 <= vin <= 2.1e6, f"{tag} VCO input {vin / 1e6:g} MHz (allowed 1..2 MHz)", ref)
        self._bad(f"{tag}n", not 50 <= n <= 432, f"{tag}N = {n} (allowed 50..432)", ref)
        self._bad(f"{tag}vco", not 100e6 * 0.999 <= vco <= 432e6 * 1.001,
                  f"{tag} VCO {vco / 1e6:g} MHz (allowed 100..432 MHz)", ref)
        return vco

    # ------------------------------------------------------------ PLL outputs
    def _main_vco(self) -> float:
        return self._vco("PLLCFGR", "6.3.2", "PLL") if self.get("CR") & PLLRDY else \
            (lambda m, n: self.pll_in_hz() / m * n if m >= 2 and n >= 2 else 0.0)(self._f("PLLCFGR", 0, 6),
                                                                                  self._f("PLLCFGR", 6, 9))

    def pll_hz(self) -> float:
        out = super().pll_hz()
        if out and self.get("CR") & PLLRDY:
            self._main_vco()                    # range checks of a running PLL
            if self._f("CFGR", 2, 2) == 2:      # 6.3.2: PLL_P must not exceed 180 MHz on the SYSCLK domain
                self._bad("pllp", out > 180e6 * 1.001, f"PLL_P {out / 1e6:g} MHz (at most 180 MHz)", "6.3.2")
        return out

    def pll_r_hz(self) -> float:
        """PLL_R = VCO / PLLR (PLLCFGR bits 30:28, 2..7; 0/1 are wrong configurations -> 0 Hz; RM0390 6.3.2)."""
        r = self._f("PLLCFGR", 28, 3)
        out = self._main_vco() / r if r >= 2 else 0.0
        if self._f("CFGR", 2, 2) == 3:
            self._bad("pllr", out > 180e6 * 1.001, f"PLL_R {out / 1e6:g} MHz as SYSCLK (at most 180 MHz)", "6.3.3")
        return out

    def src_hz(self, sws: int) -> float:
        if (sws & 3) == 3:
            return self.pll_r_hz()
        return super().src_hz(sws)

    def _sub_vco(self, reg: str) -> float:
        tag = "PLLSAI" if reg == "PLLSAICFGR" else "PLLI2S"
        return self._vco(reg, "6.3.24" if tag == "PLLSAI" else "6.3.23", tag)

    def pllsai_p_hz(self) -> float:
        """PLLSAI_P = VCO / (2, 4, 6, 8): the 48 MHz clock option (RM0390 6.3.24; at most 120 MHz)."""
        if not self.get("CR") & PLLSAIRDY:
            return 0.0
        out = self._sub_vco("PLLSAICFGR") / (2 * (self._f("PLLSAICFGR", 16, 2) + 1))
        self._bad("pllsaip", out > 120e6, f"PLLSAI_P {out / 1e6:g} MHz (at most 120 MHz)", "6.3.24")
        return out

    def pllsai_q_hz(self) -> float:
        """PLLSAI_Q = VCO / PLLSAIQ (2..15; RM0390 6.3.24)."""
        if not self.get("CR") & PLLSAIRDY:
            return 0.0
        q = self._f("PLLSAICFGR", 24, 4)
        return self._sub_vco("PLLSAICFGR") / q if q >= 2 else 0.0

    def pllsai_r_hz(self) -> float:
        return 0.0                                     # no PLLSAI_R (no LTDC) on the F446 (6.3.24: bits 31:28 reserved)

    def ltdc_clk_hz(self) -> float:
        return 0.0

    def plli2s_hz(self) -> float:
        """PLLI2S_R = PLL input / PLLI2SM x PLLI2SN / PLLI2SR (RM0390 6.3.23; I2S clock at most 192 MHz)."""
        if not self.get("CR") & PLLI2SRDY:
            return 0.0
        r = self._f("PLLI2SCFGR", 28, 3)
        out = self._sub_vco("PLLI2SCFGR") / r if r >= 2 else 0.0
        self._bad("plli2sr", out > 192e6, f"PLLI2S_R {out / 1e6:g} MHz (at most 192 MHz)", "6.3.23")
        return out

    def plli2s_p_hz(self) -> float:
        """PLLI2S_P = VCO / (2, 4, 6, 8): SPDIFRX clock option (RM0390 6.3.23; at most 120 MHz)."""
        if not self.get("CR") & PLLI2SRDY:
            return 0.0
        out = self._sub_vco("PLLI2SCFGR") / (2 * (self._f("PLLI2SCFGR", 16, 2) + 1))
        self._bad("plli2sp", out > 120e6, f"PLLI2S_P {out / 1e6:g} MHz (at most 120 MHz)", "6.3.23")
        return out

    def plli2s_q_hz(self) -> float:
        """PLLI2S_Q = VCO / PLLI2SQ (2..15; RM0390 6.3.23)."""
        if not self.get("CR") & PLLI2SRDY:
            return 0.0
        q = self._f("PLLI2SCFGR", 24, 4)
        return self._sub_vco("PLLI2SCFGR") / q if q >= 2 else 0.0

    def _pll_r_running(self) -> float:
        return self.pll_r_hz() if self.get("CR") & PLLRDY else 0.0

    # ------------------------------------------------------------ kernel clock muxes (RM0390 6.3.25 / 6.3.27)
    def sai_kernel_hz(self, name: str, block: str = "A") -> float:
        """SAI1/SAI2 kernel clock (DCKCFGR.SAI1SRC / SAI2SRC; one source for both sub-blocks of an SAI):
        00 PLLSAI_Q / PLLSAIDIVQ, 01 PLLI2S_Q / PLLI2SDIVQ, 10 PLL_R, 11: SAI1 the I2S_CKIN pin, SAI2 HSI or HSE
        (PLLCFGR.PLLSRC)."""
        d = self.get("DCKCFGR")
        sai2 = str(name).upper().endswith("2")
        src = (d >> (22 if sai2 else 20)) & 3
        if src == 0:
            return self.pllsai_q_hz() / (((d >> 8) & 0x1F) + 1)
        if src == 1:
            return self.plli2s_q_hz() / ((d & 0x1F) + 1)
        if src == 2:
            return self._pll_r_running()
        return self.pll_in_hz() if sai2 else self.i2s_ckin_hz

    def sai_clk_hz(self, block: str = "A") -> float:
        return self.sai_kernel_hz("SAI1", block)

    def i2s_kernel_hz(self, spi: str = "SPI2") -> float:
        """I2S clock of SPIx: I2S1SRC (26:25) for the APB1 I2S2/I2S3, I2S2SRC (28:27) for the APB2 I2S1 (SPI1/SPI4):
        00 PLLI2S_R, 01 I2S_CKIN, 10 PLL_R, 11 HSI or HSE (PLLSRC)."""
        apb2 = str(spi).upper() in ("SPI1", "SPI4")
        src = (self.get("DCKCFGR") >> (27 if apb2 else 25)) & 3
        if src == 0:
            return self.plli2s_hz()
        if src == 1:
            return self.i2s_ckin_hz
        if src == 2:
            return self._pll_r_running()
        return self.pll_in_hz()

    def i2s_clk_hz(self) -> float:
        return self.i2s_kernel_hz("SPI2")

    def clk48_hz(self) -> float:
        """48 MHz domain (OTG FS/HS, SDIO via CK48): PLL_Q (CK48MSEL = 0) or PLLSAI_P (1)."""
        if self.get("DCKCFGR2") >> 27 & 1:
            return self.pllsai_p_hz()
        return self.pll_q_hz() if self.get("CR") & PLLRDY else 0.0

    def sdio_kernel_hz(self) -> float:
        return self.sysclk_hz if self.get("DCKCFGR2") >> 28 & 1 else self.clk48_hz()

    def fmpi2c_clk_hz(self) -> float:
        """FMPI2C1SEL: 00 / 11 APB1 clock, 01 SYSCLK, 10 HSI."""
        sel = (self.get("DCKCFGR2") >> 22) & 3
        return (self.apb1_hz, self.sysclk_hz, HSI_HZ, self.apb1_hz)[sel]

    def cec_clk_hz(self) -> float:
        """CECSEL: 0 HSI / 488, 1 LSE (0 Hz while the LSE is not ready, BDCR.LSERDY)."""
        if self.get("DCKCFGR2") >> 26 & 1:
            return 32768.0 if self.get("BDCR") & 2 else 0.0
        return HSI_HZ / 488

    def spdifrx_clk_hz(self) -> float:
        """SPDIFRXSEL: 0 PLL_R, 1 PLLI2S_P."""
        return self.plli2s_p_hz() if self.get("DCKCFGR2") >> 29 & 1 else self._pll_r_running()

    # ------------------------------------------------------------ limits
    def clock_limits(self) -> tuple[float, float, float, str]:
        """RM0390 3.4.1 note: HCLK at most 120 MHz in scale 3 (VOS = 01), 144 MHz in scale 2 (168 with over-drive),
        168 MHz in scale 1 (180 with over-drive); RM0390 6.3.3: APB1 at most 45 MHz, APB2 at most 90 MHz."""
        pwr = self._pwr
        if pwr is None:
            return 180e6, 45e6, 90e6, ""
        od = pwr.od_active()
        scale = pwr.vos_scale()
        hmax = {1: 180e6 if od else 168e6, 2: 168e6 if od else 144e6, 3: 120e6}[scale]
        return hmax, 45e6, 90e6, (f" (voltage scale {scale}, over-drive {'on' if od else 'off'}; RM0390 3.4.1 / "
                                  "6.3.3)")

    def min_wait_states(self, hz: float) -> int:
        """FLASH_ACR.LATENCY needed for HCLK `hz` in the configured supply range (RM0390 3.4.1 Table 5)."""
        step = WS_STEP_MHZ[self.vdd_range] * 1e6
        ws = 0
        while hz > step * (ws + 1) * 1.0001 and ws < 15:
            ws += 1
        return ws

    # ------------------------------------------------------------ update
    def update(self) -> None:
        super().update()
        key = (self.clk48_hz(), self.sdio_kernel_hz(), self.sai_kernel_hz("SAI1"), self.sai_kernel_hz("SAI2"),
               self.i2s_kernel_hz("SPI2"), self.i2s_kernel_hz("SPI1"))
        if key != self._f446:
            self._f446 = key
            for cb in list(self.listeners):
                cb()
        fl = self._flash
        if fl is not None and self.chip is not None and not self.frozen:
            need = self.min_wait_states(self.core_hz)
            have = fl.get("ACR") & 0xF
            k = (round(self.core_hz), have)
            if have < need and k not in self._ws_warned:
                self._ws_warned.add(k)
                self.emit("warn", block=self.name, msg=f"RCC: HCLK {self.core_hz / 1e6:g} MHz needs FLASH_ACR.LATENCY "
                          f">= {need} wait states ({self.vdd_range} V, RM0390 3.4.1 Table 5), it is {have}: silicon "
                          "would fetch wrong data")


# ---------------------------------------------------------------------------------------------------------- PWR
@register("stm32.pwr_f446")
class PWRF446(PWRF42x):
    """RM0390 5.4: PWR_CR adds FISSR/FMSSR (21:20) and ADCDC1 (13) to the F42x layout; PWR_CSR resets to 0, VOSRDY
    (bit 14) reads 1 once the main PLL runs with the selected scale (5.4.1: "the new value programmed is active only
    when the PLL is ON"), EWUP1 (bit 8) enables WKUP1 = PA0 and EWUP2 (bit 7) WKUP2 = PC13 (pin from the STM32CubeF4
    PWR_WAKEUP_PIN2 / ST open pin data; RM0390 does not name the pin): a rising edge wakes the part from Standby, the
    pin is forced to input pull-down in Standby, and enabling it while the pin is high sets WUF (5.4.2 note)."""
    model = "stm32.pwr_f446"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, regs, params=params, log=log)
        _reraw(self.regs["CR"], self.mem, ro=0xFFC01000)                 # + FMSSR/FISSR (21:20), ADCDC1 (13)
        csr = self.regs["CSR"]
        _reraw(csr, self.mem, reset=0, ro=csr.ro_mask & ~CSR_EWUP2)       # 5.4.2: reset 0; EWUP2 writable
        self._wkup2_pin: Any = None
        self._wkup2_level = 0

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        board = getattr(chip, "pins", None)
        pin = board.gpio("C", 13) if board is not None else None
        if pin is not None:
            self._wkup2_pin = pin
            board.input_listeners[pin.id].append(self._wkup2_edge)

    def reset(self) -> None:
        keep = self.get("CSR") & CSR_EWUP2 if self._standby_wake else 0     # CSR survives a Standby wake-up
        super().reset()
        self.set("CSR", (self.get("CSR") & ~CSR_EWUP2) | keep)
        if self._wkup2_pin is not None:
            self._wkup2_level = 1 if self._wkup2_pin.level() == "H" else 0

    @pure_read
    def _csr_read(self, blk, reg):
        v = self.get("CSR") & ~CSR_VOSRDY
        r = rcc_of(self.chip) if self.chip is not None else None
        if r is None or r.get("CR") & PLLRDY:
            v |= CSR_VOSRDY
        if v & (1 << 9):
            v |= 1 << 3                                                  # BRR follows BRE
        return v

    def _csr_write(self, blk, reg, old, new):
        new = super()._csr_write(blk, reg, old, new)
        if new is not None and new & ~old & CSR_EWUP2 and self._wkup2_level:
            new |= 1                                                     # WUF: WKUP2 enabled while already high
        return new

    def _wkup2_edge(self, pin: Any) -> None:
        b = 1 if pin.level() == "H" else 0
        prev, self._wkup2_level = self._wkup2_level, b
        if b and not prev and self.get("CSR") & CSR_EWUP2:
            self.wakeup_event("WKUP2 pin (PC13)")

    def _enter_standby(self, chip: Any) -> None:
        super()._enter_standby(chip)
        if self.mode == "standby" and self.get("CSR") & CSR_EWUP2 and self._wkup2_pin is not None:
            board = getattr(chip, "pins", None)
            if board is not None:
                board.mcu_update(self._wkup2_pin, direction="in", pull="down")
            self._wkup2_level = 1 if self._wkup2_pin.level() == "H" else 0


# ---------------------------------------------------------------------------------------------------------- FLASH
@register("stm32.flash_f446")
class FLASHF446(FLASHF42x):
    """RM0390 3.8: one bank of 8 sectors; ACR.LATENCY is 4 bits (3:0); SR.RDERR (bit 8, PCROP read error) is
    rc_w1 (never set: PCROP is not enforced); CR has SNB[3:0] and no MER1; OPTCR has SPRMOD (31) and nWRP[7:0]
    (23:16), bits 30:24 and 4 reserved; there is no OPTCR1."""
    model = "stm32.flash_f446"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, regs, params=params, log=log)
        _reraw(self.regs["ACR"], self.mem, reset=0, ro=~0x00001F0F)       # 3.8.1: LATENCY[3:0], PRFTEN/ICEN/DCEN/..
        sr = self.regs["SR"]
        _reraw(sr, None, ro=sr.ro_mask & ~0x100)
        sr.w1c_mask |= 0x100                                              # 3.8.4: RDERR cleared by writing 1
        _reraw(self.regs["CR"], None, ro=~0x8301037F)                     # 3.8.5: SNB[6:3], PSIZE, no MER1
        _reraw(self.regs["OPTCR"], None, ro=0x7F000010)                  # 3.8.6: nWRP[7:0], SPRMOD; no DB1M / BFB2
        self.option_bytes = {"OPTCR": 0x0FFFAAEC, "OPTCR1": 0}

    @property
    def db1m(self) -> bool:
        return False

    def _layout(self, size: int) -> dict[int, tuple[int, int]]:
        from .f42x import _BANK
        return {i: s for i, s in enumerate(_BANK[:8]) if s[0] < size}      # 3.3 Table 4


# ---------------------------------------------------------------------------------------------------------- DMA
_F446_DMA1 = [
    ("SPI3_RX", "SPDIFRX_DT", "SPI3_RX", "SPI2_RX", "SPI2_TX", "SPI3_TX", "SPDIFRX_CS", "SPI3_TX"),
    ("I2C1_RX", "I2C3_RX", "TIM7_UP", None, "TIM7_UP", "I2C1_RX", "I2C1_TX", "I2C1_TX"),
    ("TIM4_CH1", None, "FMPI2C1_RX", "TIM4_CH2", None, "FMPI2C1_TX", "TIM4_UP", "TIM4_CH3"),
    (None, "TIM2_UP/TIM2_CH3", "I2C3_RX", None, "I2C3_TX", "TIM2_CH1", "TIM2_CH2/TIM2_CH4", "TIM2_UP/TIM2_CH4"),
    ("UART5_RX", "USART3_RX", "UART4_RX", "USART3_TX", "UART4_TX", "USART2_RX", "USART2_TX", "UART5_TX"),
    (None, None, "TIM3_CH4/TIM3_UP", None, "TIM3_CH1/TIM3_TRIG", "TIM3_CH2", None, "TIM3_CH3"),
    ("TIM5_CH3/TIM5_UP", "TIM5_CH4/TIM5_TRIG", "TIM5_CH1", "TIM5_CH4/TIM5_TRIG", "TIM5_CH2", None, "TIM5_UP", None),
    (None, "TIM6_UP", "I2C2_RX", "I2C2_RX", "USART3_TX", "DAC1", "DAC2", "I2C2_TX"),
]
_F446_DMA2 = [
    ("ADC1", "SAI1_A", "TIM8_CH1/TIM8_CH2/TIM8_CH3", "SAI1_A", "ADC1", "SAI1_B", "TIM1_CH1/TIM1_CH2/TIM1_CH3", "SAI2_B"),
    (None, "DCMI", "ADC2", "ADC2", "SAI1_B", None, None, "DCMI"),
    ("ADC3", "ADC3", None, None, None, None, None, None),
    ("SPI1_RX", None, "SPI1_RX", "SPI1_TX", "SAI2_A", "SPI1_TX", "SAI2_B", "QUADSPI"),
    ("SPI4_RX", "SPI4_TX", "USART1_RX", "SDIO", None, "USART1_RX", "SDIO", "USART1_TX"),
    (None, "USART6_RX", "USART6_RX", "SPI4_RX", "SPI4_TX", None, "USART6_TX", "USART6_TX"),
    ("TIM1_TRIG", "TIM1_CH1", "TIM1_CH2", "TIM1_CH1", "TIM1_CH4/TIM1_TRIG/TIM1_COM", "TIM1_UP", "TIM1_CH3", None),
    (None, "TIM8_UP", "TIM8_CH1", "TIM8_CH2", "TIM8_CH3", None, None, "TIM8_CH4/TIM8_TRIG/TIM8_COM"),
]
_dma.register_request_table("f446", _F446_DMA1, _F446_DMA2)


# ---------------------------------------------------------------------------------------------------------- SYSCFG
@register("stm32.syscfg_f446")
class SYSCFGF446(SYSCFG):
    """RM0390 8.2: MEMRMP has MEM_MODE[2:0] and SWP_FMC[11:10] (bit 8, the F42x FB_MODE, is reserved); PMC has only
    ADCxDC2 (18:16; no MII_RMII_SEL); CFGR (0x2C, 8.2.8) holds the FMPI2C1_SCL / FMPI2C1_SDA Fast-mode Plus drive
    bits (stored: pin drive strength is not modelled).  EXTICRx accept the port codes 0-7 (PA..PH)."""
    model = "stm32.syscfg_f446"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, regs, params=params, log=log)
        for rn, w in (("MEMRM", 0x00000C07), ("MEMRMP", 0x00000C07), ("PMC", 0x00070000)):
            if rn in self.regs:
                _reraw(self.regs[rn], None, ro=~w)
        if "CFGR" not in self.regs:
            self.add_reg(Reg("CFGR", 0x2C, 4, reset=0, ro_mask=0xFFFFFFFC))


# stm32.spdifrx (SPDIFRX, RM0390 ch. 27) is a real model: chipem/periph/stm32/spdifrx.py
# stm32.cec (HDMI-CEC, RM0390 ch. 32) is a real model: chipem/periph/stm32/cec.py
# stm32.fmpi2c (FMPI2C1, RM0390 ch. 23) is a real model: chipem/periph/stm32/fmpi2c.py


# ---------------------------------------------------------------------------------------------------------- signature
_PKG = {"LQFP64": 0, "LQFP100": 1, "WLCSP81": 2, "LQFP144": 3, "UFBGA144": 3}


@register("stm32.sysmem_f446")
class SysMemF446(SystemMemory):
    """OTP area 0x1FFF7800-0x1FFF7A0F (erased: 0xFF; RM0390 3.3 Table 4) and the device electronic signature
    (RM0390 ch. 34): 96-bit unique ID at 0x1FFF7A10 (34.1), F_SIZE at 0x1FFF7A22 (34.2, flash size in KB from the chip
    YAML memory), package data at 0x1FFF7BF0 (34.3: PKG[2:0] in bits 10:8 = 000 LQFP64, 001 LQFP100, 010 WLCSP81,
    011 LQFP144/UFBGA144; the other bits read 0).  Block base 0x1FFF7800 (`uid_offset` 0x210, `fsize_offset` 0x222)."""
    model = "stm32.sysmem_f446"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        p = {"uid_offset": 0x210, "fsize_offset": 0x222}
        p.update(params or {})
        super().__init__(name, base, size, regs, params=p, log=log)

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        off = 0x1FFF7BF0 - self.base
        if 0 <= off <= self.size - 2:
            pkg = self.params.get("pkg")
            if pkg is None:
                pkg = _PKG.get(str(getattr(chip.desc, "package", "")).upper(), 0)
            self.mem[off:off + 2] = (int(pkg) << 8).to_bytes(2, "little")


# ---------------------------------------------------------------------------------------------------------- FMC
@register("stm32.fmc_f446")
class FMCF446(FMC):
    """The STM32F446 FMC (RM0390 ch. 11) is the F42x FMC (stm32.fmc) with these register differences (11.6.6,
    11.7.7, 11.8.5, Table 80 register map): BCR1-4 have CPSIZE[18:16] and WFDIS (21; only BCR1's is used) next to
    CCLKEN (20), bits 22-31, 10 and 7 reserved; BWTR1-4 have ACCMOD[29:28], BUSTURN[19:16], DATAST, ADDHLD, ADDSET
    (27:20 reserved, reading their reset value 1); NAND flash bank 3 only (FMC_PCR/SR/PMEM/PATT/ECCR at 0x80-0x94):
    no NAND bank 2 (0x60-0x74) and no PC Card bank 4 (0xA0-0xB0): those words are reserved (read 0, writes ignored)."""
    model = "stm32.fmc_f446"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, regs, params=params, log=log)
        for x in range(1, 5):
            if f"BCR{x}" in self.regs:
                _reraw(self.regs[f"BCR{x}"], self.mem, ro=~0x003FFB7F)
            if f"BWTR{x}" in self.regs:
                _reraw(self.regs[f"BWTR{x}"], self.mem, ro=~0x300FFFFF)
        for rn in ("PCR2", "SR2", "PMEM2", "PATT2", "ECCR2", "PCR4", "SR4", "PMEM4", "PATT4", "PIO4"):
            if rn in self.regs:
                _reraw(self.regs[rn], self.mem, reset=0, ro=0xFFFFFFFF)
