"""STM32F401xB/C/D/E and STM32F411xC/E ("access line") system blocks: RCC, PWR, FLASH, plus their DMA request maps.
Subclasses of the STM32F405/407 models (rcc.py, system.py): the F405 behaviour is kept and only the differences of
these smaller dies are layered on top.

Sources: RM0368 Rev 6 (STM32F401xB/C/D/E reference manual) and RM0383 Rev 4 (STM32F411xC/E reference manual), in
docs/datasheets/st/; the register tables come from the CMSIS-SVDs STM32F401.svd / STM32F411.svd (cmsis-svd-data @
c65f8551), whose F405 leftovers are corrected here where the RM differs (the RM wins).  Section numbers below are the
same in both manuals unless stated.  The data sheets DS9716 / DS10086 / DS10314 are not in the repository.

stm32.rcc_f4x1 (`params.line`: "f401" | "f411"; default from the chip name):
* Frequency limits per regulator scale (RM0368 3.4.1 / 6.2, RM0383 3.4.1 / 6.2): F401 HCLK 60 MHz (VOS 01, scale 3) /
  84 MHz (VOS 10, scale 2), APB1 42, APB2 84 MHz; F411 64 / 84 / 100 MHz (scale 3 / 2 / 1), APB1 50, APB2 100 MHz.  The
  scale is the PWR one (PLL off = scale 3, 5.1.3).  Exceeding a limit logs a `warn` (silicon is out of spec, the
  model keeps running).
* PLL limits (6.3.2, 6.3.20): F401 PLLN and PLLI2SN 192..432 with a VCO of 192..432 MHz; F411 50..432 and 100..432 MHz;
  VCO input (PLL input / PLLM, F411 PLLI2S: / PLLI2SM) 1..2 MHz; PLLM / PLLI2SM 2..63, PLLQ 2..15, PLLI2SR 2..7, I2S
  clock <= 192 MHz.  A wrong configuration logs one `warn` each (the nominal frequency is still used).  The fields
  the RM says "can be written only when the PLL is disabled" keep their value while it runs (one `warn`): PLLM / PLLN /
  PLLP / PLLSRC while PLLON = 1, PLLSRC (and on the F401 PLLM, the PLLI2S input divider) also while PLLI2SON = 1,
  PLLI2SN (F411: PLLI2SM) while PLLI2SON = 1.  PLLQ / PLLI2SR ("should be written only ...") are written with a `warn`.
* DCKCFGR (0x8C, 6.3.21; missing from both SVDs): TIMPRE = 1: timers on APBx run at HCLK when PPREx is /1 or /2, else
  at 4 x PCLKx (which is HCLK for /4 too).
* F411 PLLI2SCFGR.PLLI2SM[5:0] (RM0383 6.3.20): the PLLI2S has its own input divider: PLLI2SCLK = PLL input / PLLI2SM
  x PLLI2SN / PLLI2SR.  F401: PLLM is shared (RM0368 6.3.20, bits 5:0 reserved).  Reset value 0x24003000 on both lines
  (6.3.20; bit 26 is reserved and reads 1, read-only; F411 PLLI2SM resets to 0, a wrong configuration until
  programmed).  The SVDs (0x20003000) and the CubeF4 LL header (RCC_PLLI2SCFGR_RST_VALUE F401 0x20003000, F411
  0x20003010) disagree; the RM is followed.
* Bits the SVDs omit: APB2 SPI4 (bit 13: F401 ENR/RSTR/LPENR, F411 RSTR), SPI5 (bit 20, F411), BDCR.LSEMOD (bit 3,
  F411 6.3.17, storage).  Bits the SVDs (or the shared F405 model) allow but the dies do not have are reserved (read 0,
  writes ignored): AHB2ENR / AHB2RSTR bits 4-5 (CRYP/HASH of the F415/F417; 6.3.6, 6.3.10: only OTGFS bit 7).
* Reset values (6.3.9-6.3.16): AHB1ENR 0 (the SVD's 0x00100000 is the F405 CCMDATARAMEN); AHB1LPENR 0x0061900F (GPIOE /
  GPIOH LPEN reset to 0), AHB2LPENR 0x00000080, APB1LPENR 0x10E2C80F, APB2LPENR 0x00077930 (TIM1LPEN resets to 0; the
  F411 SPI5LPEN bit 20 too).
* Flash wait states (RM0368 Table 6, RM0383 Table 5): after each clock change the model compares FLASH_ACR.LATENCY with
  the minimum for HCLK in the voltage range of the PWR model's `vdd` (2.7-3.6 / 2.4-2.7 / 2.1-2.4 / 1.71-2.1 V) and logs
  one `warn` when it is too small (silicon would read wrong instructions; the model has no wait-state timing).

stm32.pwr_f4x1 (5.4):
* CR reset 0x00008000 on both lines (VOS = 10, scale 2; RM0383 5.4.1 "10: Scale 2 mode (reset value)").  Writable bits:
  LPDS, PDDS, CWUF, CSBF, PVDE, PLS, DBP, FPDS, LPLVDS (10), MRLVDS (11), ADCDC1 (13), VOS[15:14]; F411 also FMSSR (20),
  FISSR (21); bit 12 and the rest reserved.  VOS can be changed only while the main PLL is off (`warn` + ignored
  otherwise); with the PLL off the regulator is in scale 3 whatever VOS says.  VOS codes: F401 00 (reserved) / 01 =
  scale 3, 10 / 11 (reserved) = scale 2; F411 00 (reserved) / 01 = scale 3, 10 = scale 2, 11 = scale 1.
* PLS (5.4.1): 2.2, 2.3 ... 2.9 V (not the RM0090 2.0 / 2.1 / 2.3 ... levels).
* CSR reset 0 (5.4.2); VOSRDY reads 1 once the main PLL runs (the VOS value is applied only with the PLL on, 5.1.3; HAL
  waits for VOSRDY after enabling the PLL).
* No over-drive / under-drive (those are F42x/F446 only).
* LPLVDS / MRLVDS (5.3.4 Table 18): low-voltage regulator and flash deep power-down in Stop: only the Stop current and
  wake-up latency change on silicon; reported in the Stop entry `info`.
* ADCDC1 can be set only at 2.7-3.6 V with the prefetch off (5.4.1): otherwise the write is ignored with a `warn`.
* F411 FISSR / FMSSR (RM0383 5.4.1, batch acquisition mode): flash interface clock off / flash memory forced to Stop
  (FPDS = 0) or deep power-down (FPDS = 1) while the system runs.  "Could not be set while executing with the Flash
  itself": a write that sets them from code fetched from flash is ignored with a `warn`.  Flash accesses while the
  flash is off are NOT blocked (silicon would fail them).
* ADCDC1 (with SYSCFG_PMC.ADC1DC2): ADC duty-cycle tuning, storage.

stm32.flash_f4x1 (ch. 3): sector layout by flash size (RM0368 Table 5, RM0383 Table 4): 4 x 16 KB, 64 KB, then 128 KB
sectors: 256 KB parts sectors 0-5, 384 KB 0-6, 512 KB 0-7.  An SNB beyond the part erases nothing and logs a `warn`.
SR.RDERR (bit 8, PCROP read error) is write-1-to-clear.  ACR.LATENCY is 4 bits wide (3.8.1).  OPTCR (3.8.6): reset
0x0FFFAAED; writable SPRMOD (31), nWRP[7:0] (23:16; F401xB/C nWRP[5:0], bits 23:22 reserved), RDP, USER (7:5),
BOR_LEV, OPTSTRT, OPTLOCK; locked after reset until the OPTKEYR sequence 0x08192A3B, 0x4C5D6E7F (a wrong key locks it
until reset); OPTLOCK is write-1-only; OPTSTRT programs the option bytes, which the next reset reloads (3.6.2).  The
option values have no other effect (no write protection, PCROP, RDP or BOR level enforcement, no hardware watchdog).

DMA request maps "f401" / "f411" (chipem/periph/stm32/dma.py `register_request_table`): written out cell by cell from
RM0368 Tables 28/29 (section 9.3.3, both F401 dies) and RM0383 Tables 27/28 (section 9.3.3).  They are not RM0090
Tables 43/44 restricted: both lines add I2C3_RX at DMA1 ch1 S1 and I2C3_TX at ch6 S5; the F411 adds I2C1_TX (DMA1
ch0 S1), USART2_RX (DMA1 ch6 S7), SPI1_TX (DMA2 ch2 S2), SPI4_RX (DMA2 ch4 S4) and SPI5_TX (DMA2 ch5 S5).  I2S on
SPI1/4/5 (F411) uses the SPIx_TX/RX requests, as on silicon.
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ..base import Reg, pure_read
from ._util import find_model
from ._util import rcc as rcc_of
from .dma import register_request_table
from .rcc import PLLRDY, RCC
from .system import FLASH, PWR, SECTORS

TIMPRE = 1 << 24

LINES: dict[str, dict] = {
    # ws: upper HCLK bound of 0, 1, 2 ... wait states per supply range (lowest VDD of the range): RM0368 Table 6
    "f401": {"hmax": {3: 60e6, 2: 84e6, 1: 84e6}, "apb1": 42e6, "apb2": 84e6, "pwr_cr": 0x8000,
             "vos": {0: 3, 1: 3, 2: 2, 3: 2}, "plli2sm": False,
             "ws": {2.7: (30e6, 60e6, 84e6), 2.4: (24e6, 48e6, 72e6, 84e6), 2.1: (18e6, 36e6, 54e6, 72e6, 84e6),
                    1.71: (16e6, 32e6, 48e6, 64e6, 80e6, 84e6)},
             "pll_n": (192, 432), "vco": (192e6, 432e6),          # RM0368 6.3.2 / 6.3.20
             "cr_rw": 0x0000EFFF,
             "apb2_extra": 1 << 13, "apb2rst_extra": 1 << 13},
    # RM0383 Table 5
    "f411": {"hmax": {3: 64e6, 2: 84e6, 1: 100e6}, "apb1": 50e6, "apb2": 100e6, "pwr_cr": 0x8000,
             "vos": {0: 3, 1: 3, 2: 2, 3: 1}, "plli2sm": True,
             "ws": {2.7: (30e6, 64e6, 90e6, 100e6), 2.4: (24e6, 48e6, 72e6, 96e6, 100e6),
                    2.1: (18e6, 36e6, 54e6, 72e6, 90e6, 100e6), 1.71: (16e6, 32e6, 48e6, 64e6, 80e6, 96e6, 100e6)},
             "pll_n": (50, 432), "vco": (100e6, 432e6),            # RM0383 6.3.2 / 6.3.20
             "cr_rw": 0x0000EFFF | (3 << 20),
             "apb2_extra": 1 << 20, "apb2rst_extra": (1 << 13) | (1 << 20)},
}
PVD_LEVELS_F4X1 = (2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9)        # PWR_CR.PLS, 5.4.1 of both manuals


def ws_table(line: str, vdd: float) -> tuple:
    """Wait-state table (upper HCLK bounds for 0, 1, ... WS) for the supply range that contains `vdd`."""
    tables = LINES[line]["ws"]
    for lo in sorted(tables, reverse=True):
        if vdd >= lo - 1e-9:
            return tables[lo]
    return tables[min(tables)]


def line_of(params: dict | None, chip: Any = None) -> str:
    ln = (params or {}).get("line")
    if ln:
        return str(ln).lower()
    name = str(getattr(getattr(chip, "desc", None), "name", "") or "").upper()
    return "f411" if name.startswith("STM32F411") else "f401"


# ------------------------------------------------------------------------------------------------------------ RCC
@register("stm32.rcc_f4x1")
class RCCF4x1(RCC):
    model = "stm32.rcc_f4x1"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, regs, params=params, log=log)
        self.line = str((params or {}).get("line", "f401")).lower()
        if self.line not in LINES:
            raise ValueError(f"{name}: params.line must be one of {sorted(LINES)}, not {self.line!r}")
        L = LINES[self.line]
        if "DCKCFGR" not in self.regs:
            self.add_reg(Reg("DCKCFGR", 0x8C, 4, reset=0, ro_mask=~TIMPRE & 0xFFFFFFFF, on_write=self._ctl_write))
        else:
            self.regs["DCKCFGR"].on_write = self._ctl_write
        extra = {"APB2ENR": L["apb2_extra"] | (1 << 13), "APB2LPENR": L["apb2_extra"] | (1 << 13),
                 "APB2RSTR": L["apb2rst_extra"]}
        if self.line == "f411":
            extra["BDCR"] = 1 << 3                                   # LSEMOD
            extra["PLLI2SCFGR"] = 0x3F                               # PLLI2SM
        for rn, bits in extra.items():
            if rn in self.regs:
                self.regs[rn].ro_mask &= ~bits
        for rn in ("AHB2ENR", "AHB2RSTR"):        # bits 4-5 (F415/F417 CRYP/HASH, opened by stm32.rcc): reserved here
            if rn in self.regs:
                self.regs[rn].ro_mask |= 0x30
        # RM0368/RM0383 6.3.9-6.3.16, 6.3.20 (the SVDs copy F405 values; the RM wins)
        resets = {"AHB1ENR": 0, "AHB1LPENR": 0x0061900F, "AHB2LPENR": 0x00000080, "APB1LPENR": 0x10E2C80F,
                  "APB2LPENR": 0x00077930, "PLLI2SCFGR": 0x24003000}
        for rn, rst in resets.items():
            pr = self.regs.get(rn)
            if pr is None:
                continue
            pr._raw = (rst, pr.ro_mask, pr.w1c_mask)
            pr.set_unit_bits(pr.unit_bits)
            self.mem[pr.boff:pr.boff + 4] = rst.to_bytes(4, "little")
        self._pwr: Any = None
        self._flash: Any = None
        self._ws_warned: set = set()
        self._timpre = False

    def attach(self, chip: Any) -> None:
        if "line" not in (self.params or {}):
            self.line = line_of(None, chip)
        super().attach(chip)
        self._pwr = find_model(chip, "stm32.pwr_f4x1", "PWR")
        self._flash = find_model(chip, "stm32.flash", "FLASH")

    def reset(self) -> None:
        self._ws_warned = set()
        super().reset()

    # ------------------------------------------------------------ PLL configuration checks (6.3.2, 6.3.20)
    def _warn_cfg(self, key: str, what: str, sect: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=f"RCC: {what} is a wrong configuration "
                      f"({'RM0383' if self.line == 'f411' else 'RM0368'} {sect})")

    def _check_vco(self, key: str, m: int, n: int, vco_in: float, sect: str) -> None:
        L = LINES[self.line]
        n_lo, n_hi = L["pll_n"]
        v_lo, v_hi = L["vco"]
        vco = vco_in * n
        for k, bad, what in ((f"{key}m", not 2 <= m <= 63, f"{key.upper()}M = {m} (allowed 2..63)"),
                             (f"{key}in", not 0.999e6 <= vco_in <= 2.001e6,
                              f"{key.upper()} VCO input {vco_in / 1e6:g} MHz (allowed 1..2 MHz)"),
                             (f"{key}n", not n_lo <= n <= n_hi, f"{key.upper()}N = {n} (allowed {n_lo}..{n_hi})"),
                             (f"{key}vco", not v_lo <= vco <= v_hi,
                              f"{key.upper()} VCO {vco / 1e6:g} MHz (allowed {v_lo / 1e6:g}..{v_hi / 1e6:g} MHz)")):
            if bad:
                self._warn_cfg(k, what, sect)

    def plli2s_hz(self) -> float:
        """PLLI2SCLK (6.3.20): F411 PLL input / PLLI2SM x PLLI2SN / PLLI2SR; F401 PLL input / PLLM (shared)."""
        from .rcc import PLLI2SRDY
        if not self.get("CR") & PLLI2SRDY:
            return 0.0
        m = self._f("PLLI2SCFGR", 0, 6) if LINES[self.line]["plli2sm"] else self._f("PLLCFGR", 0, 6)
        n, r = self._f("PLLI2SCFGR", 6, 9), self._f("PLLI2SCFGR", 28, 3)
        if m < 2 or n < 2 or r < 2:
            if r < 2:
                self._warn_cfg("plli2sr", f"PLLI2SR = {r} (allowed 2..7)", "6.3.20")
            if m < 2:
                self._warn_cfg("plli2sm", f"PLLI2S input divider {'PLLI2SM' if LINES[self.line]['plli2sm'] else 'PLLM'}"
                               f" = {m} (allowed 2..63): no I2S clock", "6.3.20")
            return 0.0
        vin = self.pll_in_hz() / m
        self._check_vco("plli2s", m, n, vin, "6.3.20")
        out = vin * n / r
        if out > 192e6:
            self._warn_cfg("plli2sout", f"I2S clock {out / 1e6:g} MHz (at most 192 MHz)", "6.3.20")
        return out

    def _check_main_pll(self) -> None:
        m, n = self._f("PLLCFGR", 0, 6), self._f("PLLCFGR", 6, 9)
        q = self._f("PLLCFGR", 24, 4)
        self._check_vco("pll", m, n, self.pll_in_hz() / m if m else 0.0, "6.3.2")
        if q < 2:
            self._warn_cfg("pllq", f"PLLQ = {q} (allowed 2..15)", "6.3.2")

    # "These bits can be written only when the PLL is disabled" (6.3.2, 6.3.20)
    _PLLCFGR_M, _PLLCFGR_N, _PLLCFGR_P, _PLLCFGR_SRC, _PLLCFGR_Q = 0x3F, 0x7FC0, 0x30000, 1 << 22, 0xF << 24
    _I2S_M, _I2S_N, _I2S_R = 0x3F, 0x7FC0, 0x7 << 28

    def _locked_fields(self, reg: str) -> tuple[int, int]:
        """(fields kept while the PLLs run, fields written with a `warn`) of PLLCFGR / PLLI2SCFGR."""
        cr = self.get("CR")
        pll, i2s = bool(cr & (1 << 24)), bool(cr & (1 << 26))
        if reg == "PLLCFGR":
            keep = (self._PLLCFGR_M | self._PLLCFGR_N | self._PLLCFGR_P | self._PLLCFGR_SRC) if pll else 0
            if i2s:
                keep |= self._PLLCFGR_SRC | (0 if LINES[self.line]["plli2sm"] else self._PLLCFGR_M)
            return keep, self._PLLCFGR_Q if pll else 0
        keep = (self._I2S_N | (self._I2S_M if LINES[self.line]["plli2sm"] else 0)) if i2s else 0
        return keep, self._I2S_R if i2s else 0

    def _ctl_write(self, blk, reg, old, new):
        if reg.name in ("PLLCFGR", "PLLI2SCFGR"):
            keep, soft = self._locked_fields(reg.name)
            rm = "RM0383" if self.line == "f411" else "RM0368"
            if (old ^ new) & keep:
                new = (new & ~keep) | (old & keep)
                self._store(reg, new)
                if f"lock{reg.name}" not in self._warned:
                    self._warned.add(f"lock{reg.name}")
                    self.emit("warn", block=self.name, msg=f"RCC_{reg.name} write while the PLL runs: the PLL "
                              f"M/N/P/SRC fields keep their value (writable only while the PLL is off, {rm} 6.3.2 / "
                              "6.3.20)")
            elif (old ^ new) & soft and f"soft{reg.name}" not in self._warned:
                self._warned.add(f"soft{reg.name}")
                self.emit("warn", block=self.name, msg=f"RCC_{reg.name}: PLLQ / PLLI2SR changed while the PLL runs "
                          f"(should be written only while it is off, {rm} 6.3.2 / 6.3.20)")
            super()._ctl_write(blk, reg, old, new)
            return new
        return super()._ctl_write(blk, reg, old, new)

    def timer_hz(self, bus: int) -> float:
        if not self.get("DCKCFGR") & TIMPRE:
            return super().timer_hz(bus)
        apb = self.apb1_hz if bus == 1 else self.apb2_hz
        return self.core_hz if self.ppre(bus) in (1, 2) else 4 * apb       # 6.3.21 (/4: 4 x PCLK = HCLK)

    def effective_timer_hz(self, bus: int) -> float:
        if not self.get("DCKCFGR") & TIMPRE:
            return super().effective_timer_hz(bus)
        hz = self.chip.clock.hz if self.chip is not None else self.core_hz
        return hz if self.ppre(bus) in (1, 2) else 4 * hz / self.ppre(bus)

    def clock_limits(self) -> tuple[float, float, float, str]:
        L = LINES[self.line]
        scale = self._pwr.vos_scale() if self._pwr is not None else min(L["hmax"])
        hmax = L["hmax"][scale]
        return hmax, L["apb1"], L["apb2"], f" (voltage scale {scale}; STM32{self.line.upper()} limits)"

    def vdd(self) -> float:
        return float(getattr(self._pwr, "vdd", 3.3)) if self._pwr is not None else 3.3

    def min_wait_states(self, hz: float, vdd: float | None = None) -> int:
        tab = ws_table(self.line, self.vdd() if vdd is None else vdd)
        for ws, top in enumerate(tab):
            if hz <= top * 1.001:
                return ws
        return len(tab) - 1

    def update(self) -> None:
        super().update()
        if self.get("CR") & PLLRDY:
            self._check_main_pll()
        timpre = bool(self.get("DCKCFGR") & TIMPRE)
        if timpre != self._timpre:              # timer kernel clocks changed without a bus clock change
            self._timpre = timpre
            for cb in list(self.listeners):
                cb()
        fl = self._flash
        if fl is not None and self.chip is not None:
            need = self.min_wait_states(self.core_hz)
            have = fl.get("ACR") & 0xF
            key = (round(self.core_hz), have)
            if have < need and key not in self._ws_warned:
                self._ws_warned.add(key)
                v = self.vdd()
                rng = next(f"{lo:g}-{hi:g} V" for lo, hi in ((2.7, 3.6), (2.4, 2.7), (2.1, 2.4), (1.71, 2.1), (0, 1.71))
                           if v >= lo - 1e-9)
                self.emit("warn", block=self.name, msg=f"RCC: HCLK {self.core_hz / 1e6:g} MHz needs FLASH_ACR.LATENCY "
                          f">= {need} wait states ({rng}), it is {have}: silicon would fetch wrong data")


# ------------------------------------------------------------------------------------------------------------ PWR
CR_LPDS, CR_LPLVDS, CR_MRLVDS = 1 << 0, 1 << 10, 1 << 11
CR_FMSSR, CR_FISSR, CR_ADCDC1 = 1 << 20, 1 << 21, 1 << 13


@register("stm32.pwr_f4x1")
class PWRF4x1(PWR):
    model = "stm32.pwr_f4x1"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, regs, params=params, log=log)
        self.line = str((params or {}).get("line", "f401")).lower()
        L = LINES[self.line]
        cr = self.regs["CR"]
        cr._raw = (L["pwr_cr"], ~L["cr_rw"] & 0xFFFFFFFF, 0)
        cr.set_unit_bits(cr.unit_bits)
        cr.on_write = self._cr_write_f4x1
        self.mem[cr.boff:cr.boff + 4] = L["pwr_cr"].to_bytes(4, "little")
        csr = self.regs["CSR"]                     # 5.4.2: reset 0x00000000 (VOSRDY is derived, see _csr_read)
        csr._raw = (0, csr.ro_mask, csr.w1c_mask)
        csr.set_unit_bits(csr.unit_bits)
        self.mem[csr.boff:csr.boff + 4] = bytes(4)

    def pvd_threshold(self) -> float:
        return PVD_LEVELS_F4X1[(self.get("CR") >> 5) & 7]

    @pure_read
    def _csr_read(self, blk, reg):
        """VOSRDY (5.4.2) = 1 once the main PLL runs: the VOS scale is applied only with the PLL on (5.1.3)."""
        from .system import CSR_BRE, CSR_BRR, CSR_VOSRDY
        r = rcc_of(self.chip) if self.chip is not None else None
        v = self.get("CSR") & ~CSR_VOSRDY
        if r is not None and r.get("CR") & PLLRDY:
            v |= CSR_VOSRDY
        if v & CSR_BRE:
            v |= CSR_BRR
        return v

    def vos_scale(self) -> int:
        """Effective regulator scale: 3 while the main PLL is off, else from CR.VOS."""
        r = rcc_of(self.chip) if self.chip is not None else None
        if r is not None and not r.get("CR") & PLLRDY:
            return 3
        return LINES[self.line]["vos"][(self.get("CR") >> 14) & 3]

    def _live_pc(self) -> int | None:
        """PC of the instruction doing this access (None for a debugger / test bus write outside a run)."""
        core = getattr(self.chip, "core", None)
        try:
            return int(core.pc) if core is not None and getattr(core, "running", False) else None
        except Exception:
            return None

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._notes:
            self._notes.add(key)
            self.emit("warn", block=self.name, msg=msg)

    def _cr_write_f4x1(self, blk, reg, old, new):
        r = rcc_of(self.chip) if self.chip is not None else None
        if (old ^ new) & (3 << 14) and r is not None and r.get("CR") & (1 << 24):
            new = (new & ~(3 << 14)) | (old & (3 << 14))
            self._warn_once("vos", "PWR_CR.VOS write ignored: the scale can be changed only while the main PLL is off")
        if new & ~old & CR_ADCDC1:
            # 5.4.1: "can only be set when operating at supply voltage range 2.7 to 3.6V and when the Prefetch is OFF"
            fl = find_model(self.chip, "stm32.flash", "FLASH") if self.chip is not None else None
            prften = bool(fl is not None and fl.get("ACR") & (1 << 8))
            if prften or self.vdd < 2.7:
                new &= ~CR_ADCDC1
                self._warn_once("adcdc1", "PWR_CR.ADCDC1 write ignored: it can be set only at VDD 2.7-3.6 V with the "
                                f"flash prefetch off (VDD {self.vdd:g} V, PRFTEN {int(prften)})")
        if self.line == "f411" and new & ~old & (CR_FMSSR | CR_FISSR):
            # RM0383 5.4.1: "This bit could not be set while executing with the Flash itself"
            pc = self._live_pc()
            fl_mem = None
            for m in getattr(getattr(self.chip, "desc", None), "memory", []):
                if m.kind == "flash":
                    fl_mem = m
                    break
            if pc is not None and fl_mem is not None and fl_mem.base <= pc < fl_mem.base + fl_mem.size:
                new = (new & ~(CR_FMSSR | CR_FISSR)) | (old & (CR_FMSSR | CR_FISSR))
                self._warn_once("fissr_flash", f"PWR_CR.FISSR/FMSSR write ignored: executed from flash (PC 0x{pc:08X});"
                                " they can be set only by a routine running from RAM (RM0383 5.4.1)")
            elif new & ~old & CR_FMSSR:
                self.emit("info", block=self.name, msg="flash memory forced to "
                          f"{'deep power-down' if new & (1 << 9) else 'Stop'} while the system runs (PWR_CR.FMSSR, "
                          "FPDS selects): code must run from SRAM (flash accesses are not blocked in CHIP_EM)")
        new = super()._cr_write(blk, reg, old, new)
        if (old ^ new) & (3 << 14) and r is not None:
            r._logged = None
            r.update()
        return new

    def _enter_stop(self, chip: Any) -> None:
        super()._enter_stop(chip)
        if self.mode == "stop":
            cr = self.get("CR")
            lv = cr & (CR_LPLVDS if cr & CR_LPDS else CR_MRLVDS)
            if lv:
                self.emit("info", block=self.name, msg=f"Stop: {'low-power' if cr & CR_LPDS else 'main'} regulator "
                          "in low-voltage mode (PWR_CR.LPLVDS/MRLVDS; longer wake-up on silicon, not modelled)")


# ------------------------------------------------------------------------------------------------------------ FLASH
OPTCR_RESET = 0x0FFFAAED                     # 3.8.6 reset value (bits 27:24 reserved, read as set)
OPTCR_RW = 0x80FFFFEF                        # SPRMOD, nWRP[7:0], RDP, USER[7:5], BOR_LEV, OPTSTRT, OPTLOCK
OPTCR_OPTLOCK, OPTCR_OPTSTRT = 1 << 0, 1 << 1
OPT_KEYS = (0x08192A3B, 0x4C5D6E7F)          # 3.8.3


@register("stm32.flash_f4x1")
class FLASHF4x1(FLASH):
    model = "stm32.flash_f4x1"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, regs, params=params, log=log)
        sr = self.regs["SR"]
        sr.ro_mask &= ~0x100
        sr.w1c_mask |= 0x100                       # RDERR (PCROP read error), rc_w1
        self.regs["ACR"].ro_mask &= ~0x8           # LATENCY[3:0] (3.8.1; RM0090 F405: [2:0])
        oc = self.regs["OPTCR"]                    # 3.8.6
        oc._raw = (OPTCR_RESET, ~OPTCR_RW & 0xFFFFFFFF, 0)
        oc.set_unit_bits(oc.unit_bits)
        oc.on_write = self._optcr_write
        self.mem[oc.boff:oc.boff + 4] = OPTCR_RESET.to_bytes(4, "little")
        self.regs["OPTKEYR"].on_write = self._optkeyr_write
        self.option_bytes = OPTCR_RESET & ~3       # the programmed user option bytes (flash: survive resets)
        self._opt_stage = 0
        self._opt_wrong = False

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        mem = self._flash_region()
        if mem is not None and mem.size <= 0x40000:          # F401xB/C: nWRP[7:6] reserved (3.8.6)
            self.regs["OPTCR"].ro_mask |= 0x00C00000

    def reset(self) -> None:
        super().reset()
        self.set("OPTCR", self.option_bytes | OPTCR_OPTLOCK)    # "loaded with values from flash memory at reset"
        self._opt_stage = 0
        self._opt_wrong = False

    def _optkeyr_write(self, blk, reg, old, new):
        """3.6.2: OPTKEY1 then OPTKEY2 clears OPTLOCK; a wrong sequence keeps it locked until the next reset."""
        if self._opt_wrong or not self.get("OPTCR") & OPTCR_OPTLOCK:
            return 0
        if self._opt_stage == 0 and new == OPT_KEYS[0]:
            self._opt_stage = 1
        elif self._opt_stage == 1 and new == OPT_KEYS[1]:
            self._opt_stage = 0
            self.set("OPTCR", self.get("OPTCR") & ~OPTCR_OPTLOCK)
        else:
            self._opt_wrong = True
            self.emit("warn", block=self.name, msg="FLASH_OPTKEYR: wrong unlock sequence; FLASH_OPTCR locked until "
                      "reset")
        return 0

    def _optcr_write(self, blk, reg, old, new):
        if old & OPTCR_OPTLOCK:
            if (old ^ new) & ~OPTCR_OPTLOCK and "optlock" not in self._notes_f4x1:
                self._notes_f4x1.add("optlock")
                self.emit("warn", block=self.name, msg="FLASH_OPTCR write while OPTLOCK = 1: ignored (unlock with "
                          "FLASH_OPTKEYR)")
            return old                                              # (OPTLOCK is write-1-only: stays set)
        if new & OPTCR_OPTSTRT:
            self.option_bytes = new & ~(OPTCR_OPTSTRT | OPTCR_OPTLOCK)
            new &= ~OPTCR_OPTSTRT                                   # BSY is not modelled: done at once
            self.emit("info", block=self.name, msg=f"FLASH option bytes programmed (OPTCR 0x{self.option_bytes:08X}); "
                      "they take effect at the next reset (no protection is enforced by CHIP_EM)")
        return new

    @property
    def _notes_f4x1(self) -> set:
        s = getattr(self, "_f4x1_notes", None)
        if s is None:
            s = self._f4x1_notes = set()
        return s

    def sectors(self) -> list[tuple[int, int]]:
        mem = self._flash_region() if self.chip is not None else None
        size = mem.size if mem is not None else 0x80000
        return [s for s in SECTORS if s[0] < size]

    def _cr_write(self, blk, reg, old, new):
        if not old & (1 << 31) and new & (1 << 16) and new & 2 and not new & 4:     # STRT + SER (no MER)
            snb = (new >> 3) & 0xF
            n = len(self.sectors())
            if snb >= n:
                self.emit("warn", block=self.name, msg=f"FLASH_CR.SNB = {snb}: no such sector on this part (sectors "
                          f"0-{n - 1}): nothing erased")
                new &= ~(1 << 16)
                if new & (1 << 24):
                    self.set("SR", self.get("SR") | 1)
                return new
        return super()._cr_write(blk, reg, old, new)


# ------------------------------------------------------------------------------------------------------------ DMA
# RM0368 Table 28 "DMA1 request mapping (STM32F401xB/C and STM32F401xD/E)": [channel][stream] (None = no request).
# Differs from RM0090 Table 43 beyond the missing blocks: ch1 S1 I2C3_RX and ch6 S5 I2C3_TX are extra positions.
_F401_DMA1 = [
    ("SPI3_RX", None, "SPI3_RX", "SPI2_RX", "SPI2_TX", "SPI3_TX", None, "SPI3_TX"),
    ("I2C1_RX", "I2C3_RX", None, None, None, "I2C1_RX", "I2C1_TX", "I2C1_TX"),
    ("TIM4_CH1", None, "I2S3_EXT_RX", "TIM4_CH2", "I2S2_EXT_TX", "I2S3_EXT_TX", "TIM4_UP", "TIM4_CH3"),
    ("I2S3_EXT_RX", "TIM2_UP/TIM2_CH3", "I2C3_RX", "I2S2_EXT_RX", "I2C3_TX", "TIM2_CH1", "TIM2_CH2/TIM2_CH4",
     "TIM2_UP/TIM2_CH4"),
    (None, None, None, None, None, "USART2_RX", "USART2_TX", None),
    (None, None, "TIM3_CH4/TIM3_UP", None, "TIM3_CH1/TIM3_TRIG", "TIM3_CH2", None, "TIM3_CH3"),
    ("TIM5_CH3/TIM5_UP", "TIM5_CH4/TIM5_TRIG", "TIM5_CH1", "TIM5_CH4/TIM5_TRIG", "TIM5_CH2", "I2C3_TX", "TIM5_UP", None),
    (None, None, "I2C2_RX", "I2C2_RX", None, None, None, "I2C2_TX"),
]
# RM0368 Table 29 "DMA2 request mapping (STM32F401xB/C and STM32F401xD/E)" (channels 1, 2 and 7 have no requests).
_F401_DMA2 = [
    ("ADC1", None, None, None, "ADC1", None, "TIM1_CH1/TIM1_CH2/TIM1_CH3", None),
    (None,) * 8,
    (None,) * 8,
    ("SPI1_RX", None, "SPI1_RX", "SPI1_TX", None, "SPI1_TX", None, None),
    ("SPI4_RX", "SPI4_TX", "USART1_RX", "SDIO", None, "USART1_RX", "SDIO", "USART1_TX"),
    (None, "USART6_RX", "USART6_RX", "SPI4_RX", "SPI4_TX", None, "USART6_TX", "USART6_TX"),
    ("TIM1_TRIG", "TIM1_CH1", "TIM1_CH2", "TIM1_CH1", "TIM1_CH4/TIM1_TRIG/TIM1_COM", "TIM1_UP", "TIM1_CH3", None),
    (None,) * 8,
]
# RM0383 Table 27 "DMA1 request mapping (STM32F411xC/E)": the F401 map plus ch0 S1 I2C1_TX and ch6 S7 USART2_RX.
_F411_DMA1 = [list(r) for r in _F401_DMA1]
_F411_DMA1[0][1] = "I2C1_TX"
_F411_DMA1[6][7] = "USART2_RX"
_F411_DMA1 = [tuple(r) for r in _F411_DMA1]
# RM0383 Table 28 "DMA2 request mapping (STM32F411xC/E)": SPI5 (ch2 S3/S4, ch5 S5, ch7 S5/S6), ch2 S2 SPI1_TX,
# ch4 S4 SPI4_RX.
_F411_DMA2 = [
    ("ADC1", None, None, None, "ADC1", None, "TIM1_CH1/TIM1_CH2/TIM1_CH3", None),
    (None,) * 8,
    (None, None, "SPI1_TX", "SPI5_RX", "SPI5_TX", None, None, None),
    ("SPI1_RX", None, "SPI1_RX", "SPI1_TX", None, "SPI1_TX", None, None),
    ("SPI4_RX", "SPI4_TX", "USART1_RX", "SDIO", "SPI4_RX", "USART1_RX", "SDIO", "USART1_TX"),
    (None, "USART6_RX", "USART6_RX", "SPI4_RX", "SPI4_TX", "SPI5_TX", "USART6_TX", "USART6_TX"),
    ("TIM1_TRIG", "TIM1_CH1", "TIM1_CH2", "TIM1_CH1", "TIM1_CH4/TIM1_TRIG/TIM1_COM", "TIM1_UP", "TIM1_CH3", None),
    (None, None, None, None, None, "SPI5_RX", "SPI5_TX", None),
]

register_request_table("f401", _F401_DMA1, _F401_DMA2)
register_request_table("f411", _F411_DMA1, _F411_DMA2)
