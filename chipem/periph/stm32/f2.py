"""STM32F205/F207/F215/F217 (STM32F2, Cortex-M3, 120 MHz) system blocks that differ from the STM32F405/407: RCC, PWR,
RTC, plus the F2 DMA request map.  Subclasses of the F405 models (rcc.py, system.py, rtc.py); everything else on the
F2 runs on the unchanged stm32.* models with the F2 register table chips/stm32f2xx_regs.yaml (STM32F21x.svd), whose
reserved bits are read-only (read 0, writes ignored).

Sources: RM0033 Rev 10 (May 2025; docs/datasheets/st/RM0033_stm32f2_reference_manual.pdf, the official ST PDF, which
replaced the third-party HTML copy the models were first written from; every section cited below was re-checked against
it), the data sheets DS15818 Rev 15 (STM32F205xx/207xx, docs/datasheets/st/DS_stm32f207zg.pdf) and DS17050 Rev 9
(STM32F215xx/217xx, docs/datasheets/st/DS_stm32f217zg.pdf), and ST's CMSIS device headers stm32f2xx (cmsis-device-f2
@ 49321f1e) for bit positions.  The flash programming manual PM0059 (FLASH_KEYR/OPTKEYR/SR/CR/OPTCR) is not in the
repository: only FLASH_ACR (RM0033 2.3.4) is verified, the rest of `stm32.flash` is the RM0090 model.

stm32.rcc_f2 (RM0033 ch. 5; every register's reset value and writable bits 5.3.1-5.3.23 equal the F2 table):
* Frequency limits (5.2, DS15818 Table 14): HCLK 120 MHz, PCLK2 (APB2) 60 MHz, PCLK1 (APB1) 30 MHz; above them one
  `warn` (silicon is out of spec, the model keeps running).  There is no voltage scaling (no PWR_CR.VOS) and no
  TIMPRE: timers on APBx run at PCLKx when PPREx = 1, else 2 x PCLKx (5.2, same rule as the F405).
* PLL (5.3.2): PLLM 2..63, VCO input = PLL input / PLLM 1..2 MHz, PLLN 192..432, VCO output 192..432 MHz, PLLQ 2..15;
  PLL general output (PLLP) at most 120 MHz (checked while the PLL drives SYSCLK); PLL48CLK (PLLQ) 48 MHz for USB OTG
  FS / SDIO / RNG.  Checked while the PLL runs; a wrong configuration logs one `warn` each and the nominal frequency
  is still used.
* PLLI2S (5.3.23): PLLI2SCLK = PLL input / PLLM x PLLI2SN / PLLI2SR, PLLI2SN 192..432, VCO 192..432 MHz, R 2..7,
  I2S clock at most 192 MHz; there is no PLLI2SQ (reset 0x20003000).
* Write rules: PLLM and PLLSRC keep their value while the PLL or the PLLI2S runs, PLLN / PLLP while the PLL runs,
  PLLI2SN while the PLLI2S runs ("can be written only when ... disabled", 5.2.3, 5.3.2, 5.3.23; one `warn`); PLLQ /
  PLLI2SR ("should be written only ...") are written with a `warn`.  CR.HSEBYP keeps its value while the HSE is on
  (5.3.1).  CSR reset flags (31:25) change only through RMVF / hardware, never by a plain write (5.3.21).  SSCGR written
  while PLLON = 1: one `warn` (5.3.22; spread spectrum is not modelled).
* Reset values from the F2 table equal RM0033 (AHB1ENR 0: there is no CCM data RAM and no CCMDATARAMEN bit,
  AHB1LPENR 0x7E6791FF, AHB2LPENR 0xF1, APB1LPENR 0x36FEC9FF, APB2LPENR 0x00075F33, PLLI2SCFGR 0x20003000).
* Flash wait states (RM0033 2.3.4 Table 3): after each clock change FLASH_ACR.LATENCY (3 bits, 0..7 WS) is compared
  with the minimum for HCLK at `params.vdd_range` (default "2.7-3.6": one WS per 30 MHz; "2.4-2.7": 24 MHz,
  "2.1-2.4": 18 MHz, "1.8-2.1": 16 MHz); too few wait states log one `warn` (the model has no wait-state timing).

stm32.pwr_f2 (RM0033 4.4): PWR_CR has no VOS (bits 31:10 reserved, reset 0x00000000); FPDS (bit 9), DBP, PLS (2.0, 2.1,
2.3, 2.5 ... 2.9 V as on the F405), PVDE, CSBF, CWUF, PDDS, LPDS (4.4.1).  PWR_CSR has no VOSRDY (bits 31:10 reserved;
BRE 9, EWUP 8, BRR 3, PVDO 2, SBF 1, WUF 0; 4.4.2).  Sleep / Stop / Standby, PVD and the wake-up sources are the F405
model's (RM0033 4.2.3, 4.3.3-4.3.5 match RM0090 5.2.3, 5.3.3-5.3.5).

stm32.rtc_f2 (RM0033 ch. 22, the first STM32 calendar RTC; verified against the ST PDF, Rev 10, 22.6.1-22.6.15
Table 80): no sub-second register (RTC_SSR 0x28), no shift control (RTC_SHIFTR 0x2C), no smooth calibration (RTC_CALR
0x3C), no time-stamp sub-seconds (RTC_TSSSR 0x38), no alarm sub-second registers (RTC_ALRMASSR/ALRMBSSR 0x44/0x48):
those offsets are reserved (read 0, writes ignored; with `params.reserved_zero` every other hole, e.g. 0x4C and
0xA0-0x3FF, too).  RTC_CR bit 5 (BYPSHAD on the F4) and bit 19 (COSEL) are reserved (22.6.3): the calendar is always
read through the shadow registers (reading TR locks DR until DR is read, RSF every 2 RTCCLK, 22.3.6).  RTC_ISR has no
SHPF (bit 3), TAMP2F (14) or RECALPF (16) (22.6.4, bits 31:14 reserved, reset 0x00000007); RTC_PRER.PREDIV_S is 13
bits (12:0; reset 0x007F00FF, 22.6.5); RTC_CALIBR DCS (7) / DC[4:0] (22.6.7); RTC_TAFCR has TAMP1E/TAMP1TRG/TAMPIE
(2:0) and TAMP1INSEL/TSINSEL/ALARMOUTTYPE (18:16) only (one tamper input, 22.6.13; the "two tamper detection inputs"
sentence of 22.3.11 is a leftover: the register map, TAMP1F and the pin tables have one); TSTR/TSDR at 0x30/0x34
(22.6.11-12); 20 backup registers BKP0R-BKP19R at 0x50-0x9C (22.6.14).  Write protection (22.3.5: all registers but
ISR[13:8], TAFCR and BKPxR; key 0xCA, 0x53; not affected by a system reset) and the system / backup-domain reset split
(22.3.7) are the F405 model's.  Coarse calibration (RTC_CALIBR, CR.DCE, 22.3.9) is storage, as on the F405; the
"bits 7, 6 and 4 of RTC_CR in initialisation mode only" rule (22.6.3) is enforced for FMT only.  Everything else
(calendar, alarms A/B, wake-up timer 22.3.4 incl. the WUCKSEL 11x +2^16 extension, initialisation) is the F405 model.

DMA request map "f2" (RM0033 9.3.3 Tables 22/23, checked cell by cell against the ST PDF): identical to the F405/F407
map (RM0090 Tables 42/43) for every request of an existing F2 block.  The official tables also print I2S2_ext /
I2S3_ext requests (DMA1 channels 2/3), but the F2 has no I2Sxext blocks: Table 1 (2.3) maps only SPI2/I2S2 at
0x40003800 and SPI3/I2S3 at 0x40003C00, and 25.2.2 lists "half-duplex communication (only transmitter or receiver)"
for the I2S; those cells are kept for fidelity to the table and never asserted.
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
        if "SSCGR" in self.regs:
            self.regs["SSCGR"].on_write = self._sscgr_write
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
        if self.get("CR") & (1 << 25):                          # check only a running PLL (PLLRDY)
            self._bad("pllm", not 2 <= m <= 63, f"PLLM = {m} (allowed 2..63)", "5.3.2")
            q = self._f("PLLCFGR", 24, 4)
            self._bad("pllq", q < 2, f"PLLQ = {q} (allowed 2..15)", "5.3.2")
        if out and self.get("CR") & (1 << 25):
            vin = self.pll_in_hz() / m
            self._bad("pllin", not 0.95e6 <= vin <= 2.1e6, f"PLL VCO input {vin / 1e6:g} MHz (allowed 1..2 MHz)",
                      "5.3.2")
            self._bad("plln", not 192 <= n <= 432, f"PLLN = {n} (allowed 192..432)", "5.3.2")
            vco = vin * n
            self._bad("pllvco", not 192e6 * 0.999 <= vco <= 432e6 * 1.001,
                      f"PLL VCO output {vco / 1e6:g} MHz (allowed 192..432 MHz)", "5.3.2")
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

    # ------------------------------------------------------------ register write rules (RM0033 5.2.3, 5.3)
    _PLL_M, _PLL_N, _PLL_P, _PLL_SRC, _PLL_Q = 0x3F, 0x7FC0, 0x30000, 1 << 22, 0xF << 24
    _I2S_N, _I2S_R = 0x7FC0, 0x7 << 28

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    def _ctl_write(self, blk, reg, old, new):
        cr = self.get("CR")
        pll, i2s = bool(cr & (1 << 24)), bool(cr & (1 << 26))
        if reg.name == "CR":
            # 5.3.1: "The HSEBYP bit can be written only if the HSE oscillator is disabled"
            if old & ((1 << 16) | (1 << 17)) and (old ^ new) & (1 << 18):
                new = (new & ~(1 << 18)) | (old & (1 << 18))
                self._store(reg, new)
                self._warn_once("hsebyp", "RCC_CR.HSEBYP write ignored: writable only while the HSE is off "
                                "(RM0033 5.3.1)")
        elif reg.name in ("PLLCFGR", "PLLI2SCFGR"):
            # 5.2.3 / 5.3.2 / 5.3.23: PLLM and PLLSRC "can be written only when the PLL and PLLI2S are disabled",
            # PLLN / PLLP only while the PLL is off, PLLI2SN only while the PLLI2S is off; PLLQ / PLLI2SR "should be
            # written only" while their PLL is off (written, with a `warn`)
            if reg.name == "PLLCFGR":
                keep = (self._PLL_N | self._PLL_P if pll else 0) | (self._PLL_M | self._PLL_SRC if pll or i2s else 0)
                soft = self._PLL_Q if pll else 0
            else:
                keep, soft = (self._I2S_N, self._I2S_R) if i2s else (0, 0)
            if (old ^ new) & keep:
                new = (new & ~keep) | (old & keep)
                self._store(reg, new)
                self._warn_once(f"lock{reg.name}", f"RCC_{reg.name} write while the PLL runs: the M/N/P/SRC fields "
                                "keep their value (writable only while the PLL is off, RM0033 5.3.2 / 5.3.23)")
            if (old ^ new) & soft:
                self._warn_once(f"soft{reg.name}", f"RCC_{reg.name}: PLLQ / PLLI2SR changed while the PLL runs "
                                "(should be written only while it is off, RM0033 5.3.2 / 5.3.23)")
        super()._ctl_write(blk, reg, old, new)
        return None                             # (stored above / by the base hook, which may update SWS)

    def _csr_write(self, blk, reg, old, new):
        # 5.3.21: the reset flags (bits 31:25) are set by hardware and cleared only through RMVF
        new = (new & ~0xFE000000) | (old & 0xFE000000)
        return super()._csr_write(blk, reg, old, new)

    def _sscgr_write(self, blk, reg, old, new):
        # 5.3.22: "must be written either before the main PLL is enabled or after the main PLL disabled"
        if old != new and self.get("CR") & (1 << 24):
            self._warn_once("sscgr", "RCC_SSCGR written while the main PLL is on (write it before PLLON = 1, "
                            "RM0033 5.3.22); spread spectrum is not modelled")
        return None

    def min_wait_states(self, hz: float) -> int:
        """FLASH_ACR.LATENCY needed for HCLK `hz` in the configured supply range (RM0033 Table 3)."""
        step = WS_STEP_MHZ[self.vdd_range] * 1e6
        ws = 0
        while hz > step * (ws + 1) * 1.0001 and ws < 7:
            ws += 1
        return ws

    def update(self) -> None:
        super().update()
        if self.get("CR") & (1 << 25):
            self.pll_hz()                       # PLL range checks also when the PLL only feeds PLL48CLK
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
