"""STM32F1 12-bit ADC (ADC1 / ADC2 / ADC3, RM0008 ch. 11: SR CR1 CR2 SMPR1-2 JOFR1-4 HTR LTR SQR1-3 JSQR JDR1-4 DR).

Verified against RM0008 Rev 21 ch. 11; timing figures RM0008 does not give come from the STM32F103x4/x6 data sheet
(DS15060: Table 46 tSTAB / tCAL / tlat / tlatr, Table 50 temperature sensor, Table 12 VREFINT).  Registers from ST's
STM32F103xx.svd (chips/stm32f103_regs.yaml); the SVD's TSVREFE in ADC2/ADC3 is made reserved here (11.12.3).  Analog
inputs use the same store and naming as stm32.adc (pin values shared with the board / `--adc` / scripted parts).

Parameters: `index` (from the name), `triggers` ("adc12" Tables 67/68, "adc3" Tables 69/70; default by index), `dma`
(default: ADC1 and ADC3), `tsvrefe` (default ADC1), `vss_channels` (default ADC2: 16, 17; ADC3: 9, 14-17), `irq`
(default "ADC1_2", ADC3 "ADC3"), `vref`, `default`, `tstab_us` (1), `cal_adcclk` (83), `rstcal_adcclk` (2), `cal_code`.

Verified (RM0008 section):
* Power (11.3.1, 11.12.3 ADON): ADON 0 -> 1 wakes the ADC (ready tSTAB later, 1 us DS15060); writing ADON = 1 again
  while it holds 1 starts a regular conversion, whatever EXTTRIG is, unless another CR2 bit changes in the same write
  ("then conversion is not triggered").  ADON 1 -> 0 powers down (conversions and calibration lost).  A conversion
  requested before tSTAB starts when the ADC is ready.
* Calibration (11.4): CAL set by software, cleared by hardware after tCAL = 83 ADCCLK (DS15060); the calibration code
  is then stored in DR.  The ADC must have been on (ADON = 1) for at least 2 ADCCLK: an earlier CAL logs one `warn` and
  starts 2 ADCCLK after ADON.  CAL with ADON = 0 does nothing (one `warn`).
* Channels / groups (11.3.3): regular SQ1..SQ16 (SQR1.L + 1) with SCAN (or DISCEN), injected JL + 1 channels starting
  at JSQ(4 - JL) (11.12.12 note), results to JDR1.. in order; SQRx / JSQR modified during a conversion restart the group.
  Single / continuous (11.3.4/11.3.5), scan (11.3.8: EOC / JEOC at the end of the group), discontinuous (11.3.10:
  DISCNUM + 1 regular channels per trigger, no rollover, "an EOC event is generated at each conversion"; JDISCEN one
  injected channel per trigger, JEOC and EOC after the last one).
* Injected (11.3.9): a triggered injection during a regular conversion interrupts it and the regular group resumes with
  the interrupted channel; a regular trigger during an injected sequence waits for its end.  JAUTO converts the injected
  group after the regular group, with a 1 ADCCLK (ADC prescaler 4-8) or 2 ADCCLK (prescaler 2) gap each way; injected
  triggers are ignored with JAUTO (one `warn`).
* SR (11.12.1): bits rc_w0; EOC "at the end of a group channel conversion (regular or injected)" and cleared by reading
  DR; JEOC at the end of all injected channels; STRT / JSTRT at the start.  No overrun on the F1 (a new result
  overwrites DR).
* Data (11.5): right / left alignment; injected data = code - JOFRx, sign-extended (right: SEXT in 15:12; left: shifted
  by 3, bit 15 sign); the analog watchdog compares the raw 12-bit code before alignment (11.3.7).
* Analog watchdog (11.3.7 Table 66): AWDEN (regular), JAWDEN (injected), AWDSGL + AWDCH; outside [LTR, HTR] sets AWD.
  HTR resets to 0x0FFF (11.12.7).
* Sample time (11.6, 11.12.4/5): Tconv = SMPx (1.5 .. 239.5) + 12.5 ADCCLK; ADCCLK = PCLK2 / ADCPRE (RCC), above
  14 MHz one `warn` (11.1).  First conversion after a trigger: + tlatr 2 ADCCLK (regular) / tlat 3 ADCCLK (injected).
* External triggers (11.7, rising edge only): ADC1/ADC2 Tables 67/68 (TIM1_CC1/CC2/CC3, TIM2_CC2, TIM3_TRGO, TIM4_CC4,
  EXTI11 or TIM8_TRGO; TIM1_TRGO/CC4, TIM2_TRGO/CC1, TIM3_CC4, TIM4_TRGO, EXTI15 or TIM8_CC4), ADC3 Tables 69/70; the
  110 entry follows AFIO_MAPR ADCx_ETRGREG_REMAP / ADCx_ETRGINJ_REMAP (9.4.2).  SWSTART / JSWSTART (11.12.3) start a
  group only when selected by EXTSEL / JEXTSEL = 111 with EXTTRIG / JEXTTRIG = 1 (SWSTART stays set otherwise, one
  `warn`), and are cleared when the conversion starts.  Timer signals come from stm32.tim `add_trigger_listener`.
* DMA (11.8, CR2.DMA): ADC1 ("ADC1", DMA1 channel 1) and ADC3 ("ADC3", DMA2 channel 5) request after each regular
  conversion; the request is served when DR is read.  ADC2 has no DMA request (one `warn`).
* Internal channels (11.10, 11.12.2): ADC1 16 = temperature sensor, 17 = VREFINT, enabled by ADC1 CR2.TSVREFE (0 V while
  disabled): V_SENSE = 1.43 V - 4.3 mV/degC x (T - 25) (DS15060 Table 50 typical; "TEMP" in degC, default 25), VREFINT
  1.20 V (DS15060 Table 12).  ADC2 channels 16/17 and ADC3 channels 9, 14-17 are wired to VSS.
* Interrupts (11.11): ADC1 and ADC2 share "ADC1_2"; ADC3 has "ADC3".  Line = EOC & EOCIE | JEOC & JEOCIE | AWD & AWDIE.
* Dual mode (11.9, ADC1 CR1.DUALMOD, reserved in ADC2/ADC3; ADC1 master, ADC2 slave whose own triggers for the
  synchronised group are ignored with one `warn`): 0001 regular + injected simultaneous, 0010 regular simultaneous +
  alternate trigger, 0011 injected simultaneous + fast interleaved, 0100 injected simultaneous + slow interleaved,
  0101 injected simultaneous, 0110 regular simultaneous, 0111 fast interleaved, 1000 slow interleaved, 1001 alternate
  trigger; 1010-1111 reserved (one `warn`, independent).  Simultaneous: one ADC1 trigger starts both groups (11.9.1/2).
  Fast interleaved: ADC2 at the trigger, ADC1 7 ADCCLK later (11.9.3).  Slow interleaved: ADC2 at the trigger, ADC1
  14 ADCCLK later, then each ADC restarts every 28 ADCCLK without CONT (11.9.4; CONT set logs one `warn`).  Alternate
  trigger: successive injected triggers convert ADC1's group, then ADC2's (11.9.5; with JDISCEN one channel each).
  ADC1 DR bits 31:16 (ADC2DATA) show ADC2's regular result in dual mode when ADC1 CR2.DMA = 1 (11.9 note: "the DMA bit
  must be enabled even if it is not used"), so a 32-bit ADC1 DMA transfer moves both.
Approximate / not modelled:
* RSTCAL clears after `rstcal_adcclk` = 2 ADCCLK (RM0008 gives no time; "additional cycles" when set during a conversion
  are not added); the calibration code is `cal_code` (0: RM0008 gives no value; it is a per-chip analog result).
* EXTI line 11 / 15 triggers (EXTSEL / JEXTSEL = 110 without the TIM8 remap) are not modelled (one `warn`).
* Dual mode: the equal-sampling-time / same-channel rules (11.9.1-11.9.4 notes) are not checked; in combined regular
  simultaneous + alternate trigger mode (11.9.8) only the ADC that converts the injected group pauses its regular group
  (silicon stops and resumes both); the prescaler-4 interleaved spacing of 11.9.9 (8/6 ADCCLK) is not modelled.
* Sensor start-up time and the 17.1 us minimum sampling time (11.10) are not checked; one scheduler event per
  conversion; clock changes during a conversion do not re-time it.  The registers' "access by words" rule (11.12) is not
  enforced.
"""
from __future__ import annotations

import re
from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ..stm32.adc import _CH_NAME, _INT_NAME, _PIN_FN, _store
from ._util import GatedBlock, add_ro, afio, apb_hz, build_regs, dma_request, irq_number, rcc

# SR
AWD, EOC, JEOC, JSTRT, STRT = 1, 2, 4, 8, 16
SR_FLAGS = 0x1F
# CR1
EOCIE, AWDIE, JEOCIE, SCAN, AWDSGL, JAUTO, DISCEN = 1 << 5, 1 << 6, 1 << 7, 1 << 8, 1 << 9, 1 << 10, 1 << 11
JDISCEN, JAWDEN, AWDEN = 1 << 12, 1 << 22, 1 << 23
# CR2
ADON, CONT, CAL, RSTCAL, DMA, ALIGN = 1, 2, 4, 8, 1 << 8, 1 << 11
JEXTTRIG, EXTTRIG, JSWSTART, SWSTART, TSVREFE = 1 << 15, 1 << 20, 1 << 21, 1 << 22, 1 << 23

SMP_HALF = (1.5, 7.5, 13.5, 28.5, 41.5, 55.5, 71.5, 239.5)
CONV_ADCCLK = 12.5
TLATR_ADCCLK, TLAT_ADCCLK = 2, 3            # DS15060 Table 46: regular / injected trigger latency
# EXTSEL / JEXTSEL -> (block, signal); 7 = software start.  RM0008 11.7 Tables 67/68 (ADC1, ADC2): 110 is EXTI line 11
# (regular) / 15 (injected), or TIM8_TRGO / TIM8_CC4 when AFIO_MAPR ADCx_ETRGREG_REMAP / ADCx_ETRGINJ_REMAP is set
# (high/XL density, 9.4.2); ("EXTI", n, timer signal) marks that entry.  Tables 69/70: ADC3's own lists.
TRIGGERS = (("TIM1", "CC1"), ("TIM1", "CC2"), ("TIM1", "CC3"), ("TIM2", "CC2"), ("TIM3", "TRGO"), ("TIM4", "CC4"),
            ("EXTI", "11", "TRGO"), None)
JTRIGGERS = (("TIM1", "TRGO"), ("TIM1", "CC4"), ("TIM2", "TRGO"), ("TIM2", "CC1"), ("TIM3", "CC4"), ("TIM4", "TRGO"),
             ("EXTI", "15", "CC4"), None)
TRIGGERS_ADC3 = (("TIM3", "CC1"), ("TIM2", "CC3"), ("TIM1", "CC3"), ("TIM8", "CC1"), ("TIM8", "TRGO"), ("TIM5", "CC1"),
                 ("TIM5", "CC3"), None)
JTRIGGERS_ADC3 = (("TIM1", "TRGO"), ("TIM1", "CC4"), ("TIM4", "CC3"), ("TIM8", "CC2"), ("TIM8", "CC4"), ("TIM5", "TRGO"),
                  ("TIM5", "CC4"), None)
TRIGGER_TABLES = {"adc12": (TRIGGERS, JTRIGGERS), "adc3": (TRIGGERS_ADC3, JTRIGGERS_ADC3)}
# AFIO_MAPR (RM0008 9.4.2): ADC1_ETRGINJ_REMAP 17, ADC1_ETRGREG_REMAP 18, ADC2_ETRGINJ_REMAP 19, ADC2_ETRGREG_REMAP 20
ETRG_REMAP_BIT = {(1, False): 18, (1, True): 17, (2, False): 20, (2, True): 19}
ADCCLK_MAX_HZ = 14e6                        # RM0008 11.1: ADCCLK must not exceed 14 MHz
SLOW_PERIOD_ADCCLK = 28                     # 11.9.4: a new ADC2 start every 28 ADCCLK
# DUALMOD -> (regular mode, injected mode)
DUAL_MODES = {0: (None, None), 1: ("sim", "sim"), 2: ("sim", "alt"), 3: ("fast", "sim"), 4: ("slow", "sim"),
              5: (None, "sim"), 6: ("sim", None), 7: ("fast", None), 8: ("slow", None), 9: (None, "alt")}
FAST_DELAY_ADCCLK, SLOW_DELAY_ADCCLK = 7, 14
V25, AVG_SLOPE, VREFINT_V = 1.43, 0.0043, 1.20
INTERNAL = {16: "TEMP", 17: "VREFINT"}


@register("stm32f1.adc")
class ADC(GatedBlock):
    model = "stm32f1.adc"
    adc_volts = True                 # set_channel takes volts (float)

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        params.setdefault("reserved_zero", True)        # reserved offsets read 0, writes ignored
        m = re.search(r"(\d+)$", name)
        self.index = int(params.get("index", m.group(1) if m else 1))
        self.gate = f"ADC{self.index}"
        tt = str(params.get("triggers", "adc3" if self.index == 3 else "adc12")).lower()
        if tt not in TRIGGER_TABLES:
            raise ValueError(f"{name}: stm32f1.adc triggers must be one of {sorted(TRIGGER_TABLES)}")
        self.triggers, self.jtriggers = TRIGGER_TABLES[tt]
        # 11.8 / CR2.DMA: only ADC1 and ADC3 generate DMA requests; 11.10 / CR2.TSVREFE: sensor and VREFINT on ADC1 only
        self.has_dma = bool(params.get("dma", self.index in (1, 3)))
        self.dma_name = f"ADC{self.index}"
        self.has_tsvrefe = bool(params.get("tsvrefe", self.index == 1))
        # channels wired to VSS (11.12.2 AWDCH note, 11.12.4): ADC2 16/17; ADC3 9, 14, 15, 16, 17
        self.vss_channels = frozenset(int(c) for c in params.get(
            "vss_channels", (9, 14, 15, 16, 17) if self.index == 3 else (() if self.index == 1 else (16, 17))))
        ov: dict[str, dict] = {
            "SR": {"on_write": self._sr_write},
            "CR1": {"on_write": self._cr1_write},
            "CR2": {"on_write": self._cr2_write},
            "SQR1": {"on_write": self._sqr_write}, "SQR2": {"on_write": self._sqr_write},
            "SQR3": {"on_write": self._sqr_write}, "JSQR": {"on_write": self._jsqr_write},
            "DR": {"on_read": self._dr_read, "ro_mask": 0xFFFFFFFF, "on_peek": self._dr_peek},
        }
        for k in range(1, 5):
            ov[f"JDR{k}"] = {"ro_mask": 0xFFFFFFFF}
        r = build_regs((name, f"ADC{self.index}", "ADC1"), size, ov, params.get("regs_file"))
        if not self.has_tsvrefe:             # 11.12.3: TSVREFE exists in ADC1 only (the SVD lists it in ADC2/ADC3)
            add_ro(next(x for x in r if x.name == "CR2"), TSVREFE)
        if self.index != 1:                  # 11.12.2: DUALMOD is reserved in ADC2 and ADC3
            add_ro(next(x for x in r if x.name == "CR1"), 0xF << 16)
        super().__init__(name, base, size, r, log=log, params=params)
        self.vref = float(params.get("vref", 3.3))
        self.default = float(params.get("default", 0.0))
        self.tstab_us = float(params.get("tstab_us", 1.0))
        self.cal_adcclk = float(params.get("cal_adcclk", 83))
        self.rstcal_adcclk = float(params.get("rstcal_adcclk", 2))
        self.cal_code = int(params.get("cal_code", 0)) & 0x7F
        self._irqn: int | None = None
        self._line = False
        self._ch_key: dict[int, str] = {}
        self._ch_pin: dict[int, Any] = {}
        self._warned: set[str] = set()
        self.vals: dict = {}
        self._ev = self._jev = self._cal_ev = self._rst_ev = None
        self._sub = self._jsub = None
        self._sub_src = self._jsub_src = None
        self._trig_lvl = self._jtrig_lvl = False
        self._init_state()
        self.conversions = 0
        self.injected_conversions = 0
        self.calibrations = 0

    def _init_state(self) -> None:
        self._ready_at = 0           # core cycle at which the powered ADC is ready (tSTAB)
        self._on_at = 0              # core cycle of the ADON 0 -> 1 write
        self._conv_start = 0         # core cycle at which the running regular conversion started (slow interleaved)
        self._pos = 0
        self._burst: int | None = None
        self._jpos = 0
        self._reg_resume = False
        self._reg_pending: bool | None = None
        self._auto_inj = False
        self._dma_req = False
        self._alt = 0                # alternate-trigger dual mode: next ADC (ADC1 only)

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        dflt = "ADC3" if self.index == 3 else "ADC1_2"
        self._irqn = irq_number(chip, str(self.params.get("irq", dflt)), dflt, "ADC")
        self.vals = _store(chip)
        board = getattr(chip, "pins", None)
        for p in (board.pins if board is not None else ()):
            mm = _PIN_FN.match(str((p.functions or {}).get("ADC") or ""))
            if mm and str(self.index) in mm.group(1):
                n = int(mm.group(2))
                self._ch_key[n] = p.id
                self._ch_pin[n] = p

    def reset(self) -> None:
        if self.chip is not None:
            for attr in ("_ev", "_jev", "_cal_ev", "_rst_ev"):
                ev = getattr(self, attr, None)
                if ev is not None:
                    self.chip.sched.cancel(ev)
                setattr(self, attr, None)
        if self._sub is not None:
            self._unsubscribe(False)
        if self._jsub is not None:
            self._unsubscribe(True)
        self._sub_src = self._jsub_src = None
        self._trig_lvl = self._jtrig_lvl = False
        if self._dma_req and self.chip is not None:
            dma_request(self.chip, self.dma_name, False)
        self._init_state()
        super().reset()
        if self.chip is not None:
            self._update_irq()

    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()
        MMIOBlock.write(self, offset, size, value)

    # ------------------------------------------------------------ stimulus
    def set_channel(self, ch: int | str, value: int | float) -> bool:
        """Set an analog input: float = volts, int = raw 12-bit code ("TEMP": degC).  False if the name is the other
        ADC's channel ("ADC2_IN3" on ADC1); ValueError for an unknown name."""
        key = self._key(ch)
        if key is None:
            return False
        if key == "TEMP":
            self.vals[key] = float(value)
        elif isinstance(value, float):
            self.vals[key] = value
        else:
            self.vals[key] = (max(0, min(int(value), 4095)) + 0.5) * self.vref / 4096
        return True

    def set_input(self, name: str, value: int | float) -> bool:
        try:
            return self.set_channel(name, value)
        except ValueError:
            return False

    def _key(self, ch: int | str) -> str | None:
        if isinstance(ch, int):
            return self._chan_key(ch)
        s = str(ch).strip().upper()
        m = _CH_NAME.match(s)
        if m:
            if m.group(1) is not None and str(self.index) not in m.group(1):
                return None
            return self._chan_key(int(m.group(2)))
        m = _INT_NAME.match(s)
        if m:
            k = m.group(1)
            if k == "VBAT":
                raise ValueError("the STM32F1 ADC has no VBAT channel")
            return "TEMP" if k in ("TEMPSENSOR", "TS") else k
        board = getattr(self.chip, "pins", None)
        if board is not None:
            for p in board.pins:
                if s == p.id.upper() or s in (a.upper() for a in p.aliases):
                    if _PIN_FN.match(str((p.functions or {}).get("ADC") or "")):
                        return p.id
                    raise ValueError(f"pin {p.id} has no ADC input")
        raise ValueError(f"bad ADC channel name {ch!r}")

    def _chan_key(self, n: int) -> str:
        if n in INTERNAL:
            return INTERNAL[n]
        if not 0 <= n <= 15:
            raise ValueError(f"ADC channel {n} out of range (0-17)")
        return self._ch_key.get(n) or f"ADC{self.index}_IN{n}"

    def volts(self, ch: int) -> float:
        """Input voltage of channel `ch` now."""
        if ch in self.vss_channels:
            return 0.0
        if ch >= 16:
            if not self.has_tsvrefe or ch > 17:
                if ch > 17:
                    self._warn_once(f"ch{ch}", f"{self.name}: channel {ch} does not exist (0-17): converts 0")
                return 0.0
            if not self.get("CR2") & TSVREFE:
                return 0.0
            if ch == 16:
                return V25 - AVG_SLOPE * (self.vals.get("TEMP", 25.0) - 25.0)
            return self.vals.get("VREFINT", VREFINT_V)
        key = self._ch_key.get(ch) or f"ADC{self.index}_IN{ch}"
        v = self.vals.get(key)
        if v is not None:
            return v
        p = self._ch_pin.get(ch)
        if p is not None:
            lvl = p.level()
            if lvl == "H":
                return self.vref
            if lvl == "L":
                return 0.0
        return self.default

    def code12(self, ch: int) -> int:
        return max(0, min(int(self.volts(ch) / self.vref * 4096), 4095))

    # ------------------------------------------------------------ clocks / configuration
    def adc_hz(self) -> float:
        r = rcc(self.chip)
        fn = getattr(r, "adc_hz", None)
        return float(fn()) if fn is not None else apb_hz(self.chip, 2) / 2

    def adcclk(self) -> float:
        """Core cycles per ADCCLK."""
        return self.chip.clock.hz / self.adc_hz()

    def _cyc(self, n_adcclk: float) -> int:
        return max(1, round(n_adcclk * self.adcclk()))

    def sample_adcclk(self, ch: int) -> float:
        if ch <= 9:
            f = self.get("SMPR2") >> (3 * ch) & 7
        elif ch <= 17:
            f = self.get("SMPR1") >> (3 * (ch - 10)) & 7
        else:
            f = 0
        return SMP_HALF[f]

    def conv_cycles(self, ch: int) -> int:
        """Conversion time of channel `ch` in core cycles: (sampling + 12.5) ADCCLK."""
        return self._cyc(self.sample_adcclk(ch) + CONV_ADCCLK)

    def sequence(self) -> list[int]:
        cr1 = self.get("CR1")
        n = (self.get("SQR1") >> 20 & 0xF) + 1 if cr1 & (SCAN | DISCEN) else 1
        regs = (self.get("SQR3"), self.get("SQR2"), self.get("SQR1"))
        return [regs[i // 6] >> (5 * (i % 6)) & 0x1F for i in range(n)]

    def jsequence(self) -> list[int]:
        jsqr = self.get("JSQR")
        jl = jsqr >> 20 & 3
        seq = [jsqr >> (5 * k) & 0x1F for k in range(3 - jl, 4)]
        return seq if self.get("CR1") & (SCAN | JAUTO | JDISCEN) else seq[:1]

    def _clk_on(self) -> bool:
        loc = self._gate_loc
        if loc is None or self._rcc is None:
            return True
        return bool(self._rcc.mem[loc[0]] & loc[1])

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    def _peer(self, index: int) -> Any:
        if self.chip is None:
            return None
        if index == self.index:
            return self
        b = self.chip.peripherals.get(f"ADC{index}")
        return b if getattr(b, "model", None) == self.model else None

    def dual_mode(self) -> tuple[str | None, str | None]:
        """(regular mode, injected mode) of ADC1 CR1.DUALMOD: "sim" / "fast" / "slow" / "alt" / None.  Dual mode
        pairs ADC1 (master) and ADC2 (slave) only (11.9); ADC3 is always independent."""
        if self.index not in (1, 2):
            return None, None
        a1 = self._peer(1)
        if a1 is None or self._peer(2) is None:
            return None, None
        m = a1.get("CR1") >> 16 & 0xF
        mode = DUAL_MODES.get(m)
        if mode is None:
            a1._warn_once(f"dual{m}", f"{a1.name}: CR1.DUALMOD={m:#06b} is reserved: the ADCs convert independently")
            return None, None
        return mode

    def _can_start(self) -> bool:
        if self.chip is None or not self.get("CR2") & ADON:
            return False
        if "adcclk" not in self._warned and self.adc_hz() > ADCCLK_MAX_HZ * 1.0001:
            self._warn_once("adcclk", f"{self.name}: ADCCLK {self.adc_hz() / 1e6:.3g} MHz exceeds the 14 MHz maximum "
                                      "(RM0008 11.1; RCC_CFGR.ADCPRE)")
        if self._cal_ev is not None or self._rst_ev is not None:
            self._warn_once("cal_trig", f"{self.name}: conversion trigger during calibration: ignored")
            return False
        if not self._clk_on():
            self._warn_once("gate", f"{self.name}: conversion trigger with the clock off (RCC APB2ENR.ADC"
                                    f"{self.index}EN=0): ignored")
            return False
        return True

    # ------------------------------------------------------------ regular group
    def _trigger(self, t: int) -> bool:
        """Start the regular group (or its next DISCEN subgroup) for a trigger at core cycle `t`."""
        if not self._can_start() or self._ev is not None:
            return False
        self.set("SR", self.get("SR") | STRT)
        if self._jev is not None:                    # injected group running: the regular group waits for it
            self._reg_pending = True
            return True
        cr1 = self.get("CR1")
        if cr1 & DISCEN:
            if self.get("CR2") & CONT:
                self._warn_once("disc_cont", f"{self.name}: DISCEN with CONT is not allowed (RM0008): discontinuous "
                                             "mode used")
            self._burst = (cr1 >> 13 & 7) + 1
        else:
            self._burst = None
            self._pos = 0
        seq = self.sequence()
        if self._pos >= len(seq):
            self._pos = 0
        start = max(t, self._ready_at) + self._cyc(TLATR_ADCCLK)
        self._conv_start = start
        self._ev = self.chip.sched.at_cycles(start + self.conv_cycles(seq[self._pos]), self._done)
        return True

    def _done(self) -> None:
        self._ev = None
        chip = self.chip
        seq = self.sequence()
        pos = self._pos if self._pos < len(seq) else 0
        ch = seq[pos]
        last = pos + 1 >= len(seq)
        cr1, cr2 = self.get("CR1"), self.get("CR2")
        c12 = self.code12(ch)
        self.set("DR", (c12 << 4) if cr2 & ALIGN else c12)
        self.conversions += 1
        sr = self.get("SR")
        if cr1 & AWDEN and (not cr1 & AWDSGL or (cr1 & 0x1F) == ch) and self._awd_out(c12):
            sr |= AWD
        if last or cr1 & DISCEN:             # 11.3.10: in discontinuous mode "an EOC event is generated at each conversion"
            sr |= EOC
        self.set("SR", sr)
        self._pos = 0 if last else pos + 1
        self._update_irq()
        if cr2 & DMA:
            if self.has_dma:
                self._dma_req = True
                dma_request(chip, self.dma_name)
            else:
                self._warn_once("dma2", f"{self.name}: CR2.DMA set but ADC2 has no DMA request (use dual mode and "
                                        "the ADC1 DMA)")
        now = chip.now_cycles()
        if last and cr1 & JAUTO and not (self._burst is not None and self._burst > 1):
            self._burst = None
            self._start_inj(now + self._cyc(self._jauto_gap()), auto=True)
            return
        if self._burst is None and last and self.dual_mode()[0] == "slow" and self._peer(2) is not None:
            # 11.9.4: slow interleaved runs on by itself, each ADC starting every 28 ADCCLK (CONT must stay 0)
            if cr2 & CONT:
                self._warn_once("slow_cont", f"{self.name}: CONT must not be set in slow interleaved mode (RM0008 "
                                             "11.9.4)")
            nxt = max(now, self._conv_start + self._cyc(SLOW_PERIOD_ADCCLK))
            self._conv_start = nxt
            self._ev = chip.sched.at_cycles(nxt + self.conv_cycles(seq[0]), self._done)
            return
        if self._burst is not None:
            self._burst -= 1
            if self._burst > 0 and not last:
                self._ev = chip.sched.at_cycles(now + self.conv_cycles(seq[self._pos]), self._done)
            else:
                self._burst = None
            return
        if not last:
            self._ev = chip.sched.at_cycles(now + self.conv_cycles(seq[self._pos]), self._done)
        elif cr2 & CONT:
            self._ev = chip.sched.at_cycles(now + self.conv_cycles(seq[0]), self._done)

    def _awd_out(self, c12: int) -> bool:
        return c12 > (self.get("HTR") & 0xFFF) or c12 < (self.get("LTR") & 0xFFF)

    def _start_regular(self, t: int) -> None:
        reg, _ = self.dual_mode()
        if reg is None:
            self._trigger(t)
            return
        if self.index != 1:
            self._warn_once("slave_reg", f"{self.name}: regular trigger ignored: dual mode (ADC1 CR1.DUALMOD) - the "
                                         "regular conversions of ADC2 are started by ADC1")
            return
        a2 = self._peer(2)
        if reg == "sim":
            a2._trigger(t)                           # ADC2 first: at equal end times its result is in place first
            self._trigger(t)
            return
        delay = FAST_DELAY_ADCCLK if reg == "fast" else SLOW_DELAY_ADCCLK
        a2._trigger(t)
        self._trigger(t + self._cyc(delay))

    # ------------------------------------------------------------ injected group
    def _jtrigger(self, t: int) -> bool:
        if not self._can_start() or self._jev is not None:
            return False
        return self._start_inj(max(t, self._ready_at) + self._cyc(TLAT_ADCCLK), auto=False)

    def _start_inj(self, t: int, auto: bool) -> bool:
        cr1 = self.get("CR1")
        if cr1 & JDISCEN and cr1 & (JAUTO | DISCEN):
            self._warn_once("jdisc", f"{self.name}: JDISCEN together with JAUTO or DISCEN is not allowed (RM0008); "
                                     "JDISCEN ignored with JAUTO")
        if self._ev is not None:                     # the regular conversion in progress is interrupted
            self.chip.sched.cancel(self._ev)
            self._ev = None
            self._reg_resume = True
        self._auto_inj = auto
        self.set("SR", self.get("SR") | JSTRT)
        seq = self.jsequence()
        if self._jpos >= len(seq) or not (cr1 & JDISCEN and not cr1 & JAUTO):
            self._jpos = 0
        self._jev = self.chip.sched.at_cycles(t + self.conv_cycles(seq[self._jpos]), self._jdone)
        return True

    def _jdone(self) -> None:
        self._jev = None
        chip = self.chip
        seq = self.jsequence()
        pos = self._jpos if self._jpos < len(seq) else 0
        ch = seq[pos]
        cr1, cr2 = self.get("CR1"), self.get("CR2")
        c12 = self.code12(ch)
        d = c12 - (self.get(f"JOFR{pos + 1}") & 0xFFF)
        self.set(f"JDR{pos + 1}", ((d << 3) if cr2 & ALIGN else d) & 0xFFFF)
        self.injected_conversions += 1
        sr = self.get("SR")
        if cr1 & JAWDEN and (not cr1 & AWDSGL or (cr1 & 0x1F) == ch) and self._awd_out(c12):
            sr |= AWD
        last = pos + 1 >= len(seq)
        self._jpos = 0 if last else pos + 1
        if last:
            sr |= JEOC | EOC
        self.set("SR", sr)
        self._update_irq()
        now = chip.now_cycles()
        if not last and not (cr1 & JDISCEN and not cr1 & JAUTO):
            self._jev = chip.sched.at_cycles(now + self.conv_cycles(seq[self._jpos]), self._jdone)
            return
        auto, self._auto_inj = self._auto_inj, False
        if auto:
            if cr2 & CONT and cr2 & ADON:
                self._pos = 0
                self._ev = chip.sched.at_cycles(now + self._cyc(self._jauto_gap()) + self.conv_cycles(self.sequence()[0]),
                                                self._done)
            return
        if self._reg_resume:
            self._reg_resume = False
            rseq = self.sequence()
            if self._pos >= len(rseq):
                self._pos = 0
            self._ev = chip.sched.at_cycles(now + self.conv_cycles(rseq[self._pos]), self._done)
        if self._reg_pending is not None and self._ev is None:
            self._reg_pending = None
            self._trigger(now)
        self._reg_pending = None

    def _start_injected(self, t: int) -> None:
        if self.get("CR1") & JAUTO:
            self._warn_once("jauto_trig", f"{self.name}: injected trigger ignored with CR1.JAUTO = 1")
            return
        _, inj = self.dual_mode()
        if inj is None:
            self._jtrigger(t)
            return
        if self.index != 1:
            self._warn_once("slave_inj", f"{self.name}: injected trigger ignored: dual mode (ADC1 CR1.DUALMOD) - the "
                                         "injected conversions of ADC2 are started by ADC1")
            return
        a2 = self._peer(2)
        if inj == "sim":
            a2._jtrigger(t)
            self._jtrigger(t)
            return
        k, self._alt = self._alt, self._alt ^ 1      # alternate trigger: ADC1, ADC2, ADC1 ...
        (self if k == 0 else a2)._jtrigger(t)

    def _jauto_gap(self) -> int:
        """11.3.9 auto-injection: 1 ADCCLK between the regular and injected sequences (and back) with ADCPRE 4..8,
        2 ADCCLK with ADCPRE 2."""
        try:
            pre = round(apb_hz(self.chip, 2) / self.adc_hz())
        except (ZeroDivisionError, TypeError, AttributeError):
            pre = 2
        return 2 if pre <= 2 else 1

    # ------------------------------------------------------------ interrupt
    def _update_irq(self) -> None:
        if self.chip is None or self._irqn is None:
            return
        sr, cr1 = self.get("SR"), self.get("CR1")
        on = bool((sr & EOC and cr1 & EOCIE) or (sr & AWD and cr1 & AWDIE) or (sr & JEOC and cr1 & JEOCIE))
        if on == self._line:
            return
        self._line = on
        shared = getattr(self.chip, "_stm32f1_adc_lines", None)
        if shared is None:
            shared = set()
            try:
                self.chip._stm32f1_adc_lines = shared
            except AttributeError:
                pass
        if on:
            shared.add(self.name)
        else:
            shared.discard(self.name)
        self.irq(self._irqn, bool(shared))

    # ------------------------------------------------------------ external triggers
    def _resubscribe(self) -> None:
        cr2 = self.get("CR2")
        on = bool(cr2 & ADON) and self.chip is not None
        self._subscribe(self.triggers[cr2 >> 17 & 7] if on and cr2 & EXTTRIG else None, inj=False)
        self._subscribe(self.jtriggers[cr2 >> 12 & 7] if on and cr2 & JEXTTRIG else None, inj=True)

    def _subscribe(self, want: tuple | None, inj: bool) -> None:
        if want == (self._jsub_src if inj else self._sub_src):
            return
        if (self._jsub if inj else self._sub) is not None:
            self._unsubscribe(inj)
        if inj:
            self._jsub_src = want
        else:
            self._sub_src = want
        if want is None:
            return
        blk_name, sig = want[0], want[1]
        if blk_name == "EXTI":
            # EXTI line 11/15, or TIM8_TRGO / TIM8_CC4 with AFIO_MAPR ADCx_ETRGxxx_REMAP = 1 (checked per edge)
            blk_name, sig = "TIM8", want[2]
            blk = self.chip.peripherals.get(blk_name)
            if not hasattr(blk, "add_trigger_listener"):
                self._warn_once(f"trigEXTI{want[1]}", f"{self.name}: external {'injected ' if inj else ''}trigger "
                                f"EXTI line {want[1]} is not modelled: no conversions are triggered")
                return
        else:
            blk = self.chip.peripherals.get(blk_name)
        if not hasattr(blk, "add_trigger_listener"):
            self._warn_once(f"trig{blk_name}{sig}", f"{self.name}: external {'injected ' if inj else ''}trigger "
                            f"{blk_name}_{sig} is not present on this part ({blk_name}): no conversions are triggered")
            return
        cb = self._ext_jtrig if inj else self._ext_trig
        blk.add_trigger_listener(sig, cb)
        lvl = bool(blk.trigger_level(sig))
        if inj:
            self._jsub, self._jtrig_lvl = (blk, sig, cb), lvl
        else:
            self._sub, self._trig_lvl = (blk, sig, cb), lvl

    def _unsubscribe(self, inj: bool) -> None:
        blk, sig, cb = self._jsub if inj else self._sub
        if inj:
            self._jsub = None
        else:
            self._sub = None
        blk.remove_trigger_listener(sig, cb)

    lp_frozen: Any = False              # PWR freezer: Stop (no ADC clock, triggers missed)

    def _remapped(self, inj: bool) -> bool:
        """For EXTSEL/JEXTSEL = 110 on ADC1/ADC2: True when AFIO_MAPR routes TIM8 (not the EXTI line) to the ADC."""
        src = self._jsub_src if inj else self._sub_src
        if src is None or src[0] != "EXTI":
            return True
        bit = ETRG_REMAP_BIT.get((self.index, inj))
        a = afio(self.chip)
        if bit is not None and a is not None and a.get("MAPR") >> bit & 1:
            return True
        self._warn_once(f"trigEXTI{src[1]}", f"{self.name}: external {'injected ' if inj else ''}trigger EXTI line "
                        f"{src[1]} (AFIO_MAPR ADC{self.index}_ETRG{'INJ' if inj else 'REG'}_REMAP = 0) is not "
                        "modelled: no conversions are triggered")
        return False

    def _ext_trig(self, t: int, level: bool) -> None:
        if level == self._trig_lvl:
            return
        self._trig_lvl = level
        if level and not self.lp_frozen and self._remapped(False):   # rising edge only on the F1 (11.7)
            self._start_regular(t)

    def _ext_jtrig(self, t: int, level: bool) -> None:
        if level == self._jtrig_lvl:
            return
        self._jtrig_lvl = level
        if level and not self.lp_frozen and self._remapped(True):
            self._start_injected(t)

    # ------------------------------------------------------------ register hooks
    def _sr_write(self, blk, reg, old, new):
        v = (old & new & SR_FLAGS) | (old & ~SR_FLAGS)        # rc_w0
        self._store(reg, v)
        self._update_irq()
        return v

    def _cr1_write(self, blk, reg, old, new):
        if self.index != 1 and new >> 16 & 0xF:
            self._warn_once("dualmod2", f"{self.name}: CR1.DUALMOD is only meaningful in ADC1 (ignored here)")
        self._store(reg, new)
        self._update_irq()
        return None

    def _power_down(self) -> None:
        for attr in ("_ev", "_jev", "_cal_ev", "_rst_ev"):
            ev = getattr(self, attr)
            if ev is not None:
                self.chip.sched.cancel(ev)
                setattr(self, attr, None)
        self._pos, self._burst, self._jpos = 0, None, 0
        self._reg_resume, self._reg_pending, self._auto_inj = False, None, False

    def _cr2_write(self, blk, reg, old, new):
        chip = self.chip
        now = chip.now_cycles() if chip is not None else 0
        # CAL / RSTCAL: set by software, cleared by hardware only
        v = new | (old & (CAL | RSTCAL))
        swstart = bool(new & SWSTART)
        jswstart = bool(new & JSWSTART)
        if (v & (SWSTART | JSWSTART)) and chip is not None:
            v &= ~((SWSTART if swstart and self._sw_ok(new, False) else 0)
                   | (JSWSTART if jswstart and self._sw_ok(new, True) else 0))
        restart = bool(old & ADON and new & ADON and new == old)      # second ADON write, nothing else changed
        if old & ADON and not new & ADON:
            v &= ~(CAL | RSTCAL)
            if chip is not None:
                self._power_down()
        self._store(reg, v)
        if chip is None:
            return v
        if new & ADON and not old & ADON:
            self._on_at = now
            self._ready_at = now + max(0, round(self.tstab_us * 1e-6 * chip.clock.hz))
        if new & DMA and not self.has_dma and not old & DMA:
            self._warn_once("dma2", f"{self.name}: CR2.DMA set but ADC2 has no DMA request (use dual mode and the ADC1 "
                                    "DMA)")
        self._resubscribe()
        if v & ADON:
            if new & RSTCAL and not old & RSTCAL:
                self._start_cal(now, reset=True)
            if new & CAL and not old & CAL:
                self._start_cal(now, reset=False)
        elif new & (CAL | RSTCAL) & ~old:
            self._warn_once("cal_off", f"{self.name}: CAL/RSTCAL set with ADON = 0: the calibration does not run")
        if jswstart and v & ADON and not v & JSWSTART and self._sw_ok(new, True):
            self._start_injected(now)
        if swstart and v & ADON:
            if self._sw_ok(new, False):
                self._start_regular(now)
            else:
                self._warn_once("swstart", f"{self.name}: SWSTART needs EXTTRIG = 1 and EXTSEL = 111: no conversion")
        elif restart:                        # 11.12.3 ADON: "Conversion starts when this bit holds 1 and 1 is written"
            self._start_regular(now)
        return v

    @staticmethod
    def _sw_ok(cr2: int, inj: bool) -> bool:
        if inj:
            return bool(cr2 & JEXTTRIG) and (cr2 >> 12 & 7) == 7
        return bool(cr2 & EXTTRIG) and (cr2 >> 17 & 7) == 7

    def _start_cal(self, now: int, reset: bool) -> None:
        start = now
        if not reset:
            earliest = self._on_at + self._cyc(2)    # 11.4: ADON = 1 for at least two ADC clock cycles before CAL
            if now < earliest:
                self._warn_once("cal_early", f"{self.name}: calibration started less than 2 ADCCLK after ADON was set "
                                             "(RM0008 11.4); it starts 2 ADCCLK after ADON")
            start = max(now, earliest)
        if reset:
            if self._rst_ev is not None:
                return
            self._rst_ev = self.chip.sched.at_cycles(start + self._cyc(self.rstcal_adcclk), self._rstcal_done)
        else:
            if self._cal_ev is not None:
                return
            self._cal_ev = self.chip.sched.at_cycles(start + self._cyc(self.cal_adcclk), self._cal_done)

    def _rstcal_done(self) -> None:
        self._rst_ev = None
        self.set("CR2", self.get("CR2") & ~RSTCAL)

    def _cal_done(self) -> None:
        self._cal_ev = None
        self.set("CR2", self.get("CR2") & ~CAL)
        self.set("DR", self.cal_code)
        self.calibrations += 1

    def _sqr_write(self, blk, reg, old, new):
        """Modifying ADC_SQRx during a regular conversion resets it and restarts the new group."""
        self._store(reg, new)
        if new != old and self._ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
            self._ev = None
            self._pos, self._burst = 0, None
            self._trigger(self.chip.now_cycles())
        return None

    def _jsqr_write(self, blk, reg, old, new):
        self._store(reg, new)
        if new != old and self._jev is not None and self.chip is not None:
            self.chip.sched.cancel(self._jev)
            self._jev = None
            self._jpos = 0
            self._start_inj(self.chip.now_cycles() + self._cyc(TLAT_ADCCLK), auto=self._auto_inj)
        return None

    def _dr_peek(self, blk, reg):
        v = self.get("DR") & 0xFFFF
        if self.index == 1 and self.get("CR1") >> 16 & 0xF and self.get("CR2") & DMA:
            a2 = self._peer(2)
            if a2 is not None:
                v |= (a2.get("DR") & 0xFFFF) << 16
        return v

    def _dr_read(self, blk, reg):
        if self._dma_req:                            # read by the DMA or the CPU: the request is served
            self._dma_req = False
            dma_request(self.chip, self.dma_name, False)
        sr = self.get("SR")
        if sr & EOC:
            self.set("SR", sr & ~EOC)
            self._update_irq()
        v = self.get("DR") & 0xFFFF
        # 11.12.14 / 11.9 note: ADC2DATA holds the ADC2 regular result in dual mode; the master's DMA bit must be set
        # to read it there "even if it is not used to transfer converted regular channel data"
        if self.index == 1 and self.get("CR1") >> 16 & 0xF and self.get("CR2") & DMA:
            a2 = self._peer(2)
            if a2 is not None:
                v |= (a2.get("DR") & 0xFFFF) << 16    # ADC2DATA: the ADC2 regular result in dual mode
        return v
