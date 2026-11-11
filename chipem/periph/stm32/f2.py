"""STM32F205/F207/F215/F217 (STM32F2, Cortex-M3, 120 MHz) system blocks that differ from the STM32F405/407: RCC, PWR,
RTC, plus the F2 DMA request map.  Subclasses of the F405 models (rcc.py, system.py, rtc.py); everything else on the
F2 runs on the unchanged stm32.* models with the F2 register table chips/stm32f2xx_regs.yaml (STM32F21x.svd), whose
reserved bits are read-only (read 0, writes ignored).

Sources: RM0033 Rev 10 (STM32F205xx/207xx/215xx/217xx reference manual), read from the docs.dirba.io HTML rendition of
the ST PDF because st.com refused the download (the PDF is not in the repository; docs/LIMITATIONS.md "STM32F2"),
the data sheets DS15818 Rev 15 (STM32F205xx/207xx, docs/datasheets/st/DS_stm32f207zg.pdf) and DS17050 Rev 9
(STM32F215xx/217xx, docs/datasheets/st/DS_stm32f217zg.pdf), and ST's CMSIS device headers stm32f2xx (cmsis-device-f2
@ 49321f1e) for bit positions.

stm32.rcc_f2 (RM0033 ch. 5):
* Frequency limits (5.2, DS15818 Table 14): HCLK 120 MHz, PCLK2 (APB2) 60 MHz, PCLK1 (APB1) 30 MHz; above them one
  `warn` (silicon is out of spec, the model keeps running).  There is no voltage scaling (no PWR_CR.VOS) and no
  TIMPRE: timers on APBx run at PCLKx when PPREx = 1, else 2 x PCLKx (same rule as the F405).
* PLL (5.3.2): VCO input = PLL input / PLLM must be 1..2 MHz, VCO output 192..432 MHz (PLLN 192..432; the F405
  allows 50/100); PLL general output (PLLP) at most 120 MHz (checked while the PLL drives SYSCLK); PLL48CLK (PLLQ) 48 MHz for USB OTG FS / SDIO / RNG.
  A wrong configuration logs one `warn` each and the nominal frequency is still used.
* PLLI2S (5.3.23): PLLI2SCLK = PLL input / PLLM x PLLI2SN / PLLI2SR, PLLI2SN 192..432, VCO 192..432 MHz, R 2..7,
  I2S clock at most 192 MHz; there is no PLLI2SQ (reset 0x20003000).
* Reset values from the F2 table equal RM0033 (AHB1ENR 0: there is no CCM data RAM and no CCMDATARAMEN bit,
  AHB1LPENR 0x7E6791FF, AHB2LPENR 0xF1, APB1LPENR 0x36FEC9FF, APB2LPENR 0x00075F33, PLLI2SCFGR 0x20003000).
* Flash wait states (RM0033 2.3.4 Table 3): after each clock change FLASH_ACR.LATENCY (3 bits, 0..7 WS) is compared
  with the minimum for HCLK at `params.vdd_range` (default "2.7-3.6": one WS per 30 MHz; "2.4-2.7": 24 MHz,
  "2.1-2.4": 18 MHz, "1.8-2.1": 16 MHz); too few wait states log one `warn` (the model has no wait-state timing).

stm32.pwr_f2 (RM0033 4.4): PWR_CR has no VOS (bits 31:10 reserved, reset 0x00000000); FPDS (bit 9), DBP, PLS, PVDE,
CSBF, CWUF, PDDS, LPDS as on the F405.  PWR_CSR has no VOSRDY (bits 31:10 reserved; BRE 9, EWUP 8, BRR 3, PVDO 2,
SBF 1, WUF 0).  Sleep / Stop / Standby, PVD and the wake-up sources are the F405 model's (RM0033 4.3 matches RM0090
5.3 for these modes).

stm32.rtc_f2 (RM0033 ch. 22, the first STM32 calendar RTC): no sub-second register (RTC_SSR 0x28), no shift control
(RTC_SHIFTR 0x2C), no smooth calibration (RTC_CALR 0x3C), no time-stamp sub-seconds (RTC_TSSSR 0x38), no alarm
sub-second registers (RTC_ALRMASSR/ALRMBSSR 0x44/0x48): those offsets are reserved (read 0, writes ignored).
RTC_CR bit 5 (BYPSHAD on the F4) and bit 19 (COSEL) are reserved: the calendar is always read through the shadow
registers (reading TR locks DR until DR is read, RSF every 2 RTCCLK, 22.3.6).  RTC_ISR has no SHPF (bit 3),
TAMP2F (14) or RECALPF (16); RTC_PRER.PREDIV_S is 13 bits (12:0; reset 0x007F00FF); RTC_TAFCR has TAMP1E/TAMP1TRG/
TAMPIE/TAMP1INSEL/TSINSEL/ALARMOUTTYPE only (one tamper input).  Coarse calibration (RTC_CALIBR, CR.DCE) is storage,
as on the F405.  Everything else (calendar, alarms A/B, wake-up timer, write protection, initialisation, backup
registers BKP0R-BKP19R) is the F405 model.

DMA request map "f2" (RM0033 Tables 22/23): identical to the F405/F407 map (RM0090 Tables 42/43) for every request of
an existing F2 block.  The tables also print I2S2_ext / I2S3_ext requests (DMA1 channels 2/3), but the F2 has no
I2Sxext blocks (RM0033 Table 1 memory map, 25.x: SPI2/I2S2 at 0x40003800 and SPI3/I2S3 at 0x40003C00 only); those
cells are kept for fidelity to the table and never asserted.
"""
from __future__ import annotations

from typing import Any

from ..base import Reg, pure_read
from ..registry import register
from ._util import find_model
from .dma import _T42, _T43, register_request_table
from .rcc import PLLI2SRDY, RCC
from .rtc import RTC
from .system import CSR_VOSRDY, PWR, PWR_VOS


def _set_reg(blk: Any, name: str, *, reset: int | None = None, ro_mask: int | None = None) -> None:
    """Change the reset value / read-only mask of a register built from the table (and the stored value)."""
    r = blk.regs.get(name)
    if r is None:
        return
    rst, ro, w1c = r._raw
    r._raw = (rst if reset is None else reset, ro if ro_mask is None else ro_mask & 0xFFFFFFFF, w1c)
    r.set_unit_bits(r.unit_bits)
    blk.mem[r.boff:r.boff + r.bw] = r.reset.to_bytes(r.bw, "little")


# ------------------------------------------------------------------------------------------------------------ RCC
WS_STEP_MHZ = {"2.7-3.6": 30.0, "2.4-2.7": 24.0, "2.1-2.4": 18.0, "1.8-2.1": 16.0}     # RM0033 Table 3


@register("stm32.rcc_f2")
class RCCF2(RCC):
    model = "stm32.rcc_f2"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, regs, params=params, log=log)
        rng = str((params or {}).get("vdd_range", "2.7-3.6"))
        if rng not in WS_STEP_MHZ:
            raise ValueError(f"{name}: params.vdd_range must be one of {sorted(WS_STEP_MHZ)}, not {rng!r}")
        self.vdd_range = rng
        self._flash: Any = None
        self._ws_warned: set = set()

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._flash = find_model(chip, "stm32.flash", "FLASH")

    def reset(self) -> None:
        self._ws_warned = set()
        super().reset()

    def clock_limits(self) -> tuple[float, float, float, str]:
        return 120e6, 30e6, 60e6, " (STM32F2: HCLK 120 MHz, APB1 30 MHz, APB2 60 MHz)"

    def _bad(self, key: str, cond: bool, what: str, ref: str) -> None:
        if cond and key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=f"RCC: {what} is a wrong configuration (RM0033 {ref})")

    def pll_hz(self) -> float:
        out = super().pll_hz()
        m, n = self._f("PLLCFGR", 0, 6), self._f("PLLCFGR", 6, 9)
        if out and self.get("CR") & (1 << 25):                  # check only a running PLL (PLLRDY)
            vin = self.pll_in_hz() / m
            self._bad("pllin", not 0.95e6 <= vin <= 2.1e6, f"PLL VCO input {vin / 1e6:g} MHz (allowed 1..2 MHz)",
                      "5.3.2")
            self._bad("plln", not 192 <= n <= 432, f"PLLN = {n} (allowed 192..432)", "5.3.2")
            # PLLP output: at most 120 MHz "on this domain" (SYSCLK); only checked while the PLL drives SYSCLK
            # (sdio_card runs the PLL for PLL48CLK only, with SYSCLK on HSI)
            if self._f("CFGR", 0, 2) == 2 or self._f("CFGR", 2, 2) == 2:
                self._bad("pllout", out > 120e6 * 1.001, f"PLL output {out / 1e6:g} MHz (at most 120 MHz)", "5.3.2")
        return out

    def plli2s_hz(self) -> float:
        if not self.get("CR") & PLLI2SRDY:
            return 0.0
        m = self._f("PLLCFGR", 0, 6)
        n, r = self._f("PLLI2SCFGR", 6, 9), self._f("PLLI2SCFGR", 28, 3)
        if m < 2 or n < 2 or r < 2:
            return 0.0
        vco = self.pll_in_hz() / m * n
        out = vco / r
        self._bad("plli2sn", not 192 <= n <= 432, f"PLLI2SN = {n} (allowed 192..432)", "5.3.23")
        self._bad("plli2svco", not 192e6 <= vco <= 432e6, f"PLLI2S VCO {vco / 1e6:g} MHz (allowed 192..432 MHz)",
                  "5.3.23")
        self._bad("plli2sout", out > 192e6, f"I2S clock {out / 1e6:g} MHz (at most 192 MHz)", "5.3.23")
        return out

    def min_wait_states(self, hz: float) -> int:
        """FLASH_ACR.LATENCY needed for HCLK `hz` in the configured supply range (RM0033 Table 3)."""
        step = WS_STEP_MHZ[self.vdd_range] * 1e6
        ws = 0
        while hz > step * (ws + 1) * 1.0001 and ws < 7:
            ws += 1
        return ws

    def update(self) -> None:
        super().update()
        fl = self._flash
        if fl is not None and self.chip is not None:
            need = self.min_wait_states(self.core_hz)
            have = fl.get("ACR") & 0x7
            key = (round(self.core_hz), have)
            if have < need and key not in self._ws_warned:
                self._ws_warned.add(key)
                self.emit("warn", block=self.name, msg=f"RCC: HCLK {self.core_hz / 1e6:g} MHz needs FLASH_ACR.LATENCY "
                          f">= {need} wait states ({self.vdd_range} V, RM0033 Table 3), it is {have}: silicon would "
                          "fetch wrong data")


# ------------------------------------------------------------------------------------------------------------ PWR
@register("stm32.pwr_f2")
class PWRF2(PWR):
    model = "stm32.pwr_f2"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, regs, params=params, log=log)
        # RM0033 4.4.1 / 4.4.2: no VOS / VOSRDY; bits 31:10 reserved in both registers
        _set_reg(self, "CR", reset=0, ro_mask=0xFFFFFC00)
        _set_reg(self, "CSR", reset=0)

    def reset(self) -> None:
        super().reset()
        self.set("CSR", self.get("CSR") & ~CSR_VOSRDY)
        self.set("CR", self.get("CR") & ~PWR_VOS)

    @pure_read
    def _csr_read(self, blk, reg):
        return super()._csr_read(blk, reg) & ~CSR_VOSRDY


# ------------------------------------------------------------------------------------------------------------ RTC
CR_RESERVED_F2 = (1 << 5) | (1 << 19)               # BYPSHAD, COSEL (RM0033 22.6.3: reserved)


@register("stm32.rtc_f2")
class RTCF2(RTC):
    model = "stm32.rtc_f2"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, regs, params=params, log=log)
        cr = self.regs.get("CR")
        if cr is not None:
            _set_reg(self, "CR", ro_mask=cr.ro_mask | CR_RESERVED_F2)
        # the F4 offsets SSR, SHIFTR, TSSSR, CALR, ALRMASSR, ALRMBSSR are reserved on the F2: read 0, writes ignored
        for off, nm in ((0x28, "SSR"), (0x2C, "SHIFTR"), (0x38, "TSSSR"), (0x3C, "CALR"), (0x44, "ALRMASSR"),
                        (0x48, "ALRMBSSR")):
            if nm not in self.regs and off not in self._by_off:
                self.add_reg(Reg(f"RESERVED_{off:02X}", off, 4, reset=0, ro_mask=0xFFFFFFFF))

    def _cr_write(self, blk, reg, old, new):
        return super()._cr_write(blk, reg, old, new & ~CR_RESERVED_F2)


# ------------------------------------------------------------------------------------------------------------ DMA
# RM0033 Table 22 (DMA1) / Table 23 (DMA2): REQ[channel][stream]
_F2_DMA1 = [
    ("SPI3_RX", None, "SPI3_RX", "SPI2_RX", "SPI2_TX", "SPI3_TX", None, "SPI3_TX"),
    ("I2C1_RX", None, "TIM7_UP", None, "TIM7_UP", "I2C1_RX", "I2C1_TX", "I2C1_TX"),
    ("TIM4_CH1", None, "I2S3_EXT_RX", "TIM4_CH2", "I2S2_EXT_TX", "I2S3_EXT_TX", "TIM4_UP", "TIM4_CH3"),
    ("I2S3_EXT_RX", "TIM2_UP/TIM2_CH3", "I2C3_RX", "I2S2_EXT_RX", "I2C3_TX", "TIM2_CH1", "TIM2_CH2/TIM2_CH4",
     "TIM2_UP/TIM2_CH4"),
    ("UART5_RX", "USART3_RX", "UART4_RX", "USART3_TX", "UART4_TX", "USART2_RX", "USART2_TX", "UART5_TX"),
    (None, None, "TIM3_CH4/TIM3_UP", None, "TIM3_CH1/TIM3_TRIG", "TIM3_CH2", None, "TIM3_CH3"),
    ("TIM5_CH3/TIM5_UP", "TIM5_CH4/TIM5_TRIG", "TIM5_CH1", "TIM5_CH4/TIM5_TRIG", "TIM5_CH2", None, "TIM5_UP", None),
    (None, "TIM6_UP", "I2C2_RX", "I2C2_RX", "USART3_TX", "DAC1", "DAC2", "I2C2_TX"),
]
_F2_DMA2 = [
    ("ADC1", None, "TIM8_CH1/TIM8_CH2/TIM8_CH3", None, "ADC1", None, "TIM1_CH1/TIM1_CH2/TIM1_CH3", None),
    (None, "DCMI", "ADC2", "ADC2", None, None, None, "DCMI"),
    ("ADC3", "ADC3", None, None, None, "CRYP_OUT", "CRYP_IN", "HASH_IN"),
    ("SPI1_RX", None, "SPI1_RX", "SPI1_TX", None, "SPI1_TX", None, None),
    (None, None, "USART1_RX", "SDIO", None, "USART1_RX", "SDIO", "USART1_TX"),
    (None, "USART6_RX", "USART6_RX", None, None, None, "USART6_TX", "USART6_TX"),
    ("TIM1_TRIG", "TIM1_CH1", "TIM1_CH2", "TIM1_CH1", "TIM1_CH4/TIM1_TRIG/TIM1_COM", "TIM1_UP", "TIM1_CH3", None),
    (None, "TIM8_UP", "TIM8_CH1", "TIM8_CH2", "TIM8_CH3", None, None, "TIM8_CH4/TIM8_TRIG/TIM8_COM"),
]


def _check_against_f40x() -> None:
    """Every F2 cell equals the RM0090 F40x cell (the F42x-only requests are the only additions there)."""
    f42x_only = ("UART7", "UART8", "SAI1", "SPI4", "SPI5", "SPI6")
    for f2, f4 in ((_F2_DMA1, _T42), (_F2_DMA2, _T43)):
        for ch in range(8):
            for s in range(8):
                a, b = f2[ch][s], f4[ch][s]
                if b is not None and b.startswith(f42x_only):
                    b = None
                assert a == b, (ch, s, a, b)


_check_against_f40x()
register_request_table("f2", _F2_DMA1, _F2_DMA2)
