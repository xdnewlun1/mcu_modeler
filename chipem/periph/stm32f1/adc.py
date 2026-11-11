"""STM32F1 12-bit ADC (ADC1 / ADC2, RM0008 ch. 11 layout: SR CR1 CR2 SMPR1-2 JOFR1-4 HTR LTR SQR1-3 JSQR JDR1-4 DR).

Not verified against RM0008 (the reference manual was not available when this was written): behaviour from the
RM0008 ADC chapter as known and as used by the HAL/LL/SPL drivers; timing data from the STM32F103x4/x6 data sheet
(DS15060: Table 46 tSTAB / tCAL / tlat / tlatr, Table 50 temperature sensor, Table 12 VREFINT).  Registers from ST's
STM32F103xx.svd (chips/stm32f103_regs.yaml).  Analog inputs use the same store and naming as stm32.adc (pin values
shared with the board / `--adc` / scripted analog parts).

Power and calibration (CR2):
* ADON 0 -> 1 powers the ADC up; it is ready tSTAB later (`params.tstab_us`, 1 us, DS15060 Table 46).  Writing CR2
  again with ADON = 1 and no other bit changed starts a regular conversion (the F1 "second ADON write" start; RM0008:
  a conversion is not triggered if any other CR2 bit changes in the same write).  It is done only with EXTTRIG = 0
  (assumption: with EXTTRIG = 1 the regular group waits for its selected trigger).  ADON 1 -> 0 powers down: running
  conversions and a calibration in progress are lost (CAL / RSTCAL clear).  A conversion requested before tSTAB has
  elapsed starts when the ADC is ready.
* CAL (set by software, cleared by hardware): calibration takes tCAL = 83 ADCCLK (`params.cal_adcclk`), then CAL reads
  0 and DR holds the calibration code (`params.cal_code`, 0: an ideal converter).  RSTCAL clears after
  `params.rstcal_adcclk` ADCCLK (2; RM0008 gives no figure).  Software cannot clear either bit.  Setting CAL while
  ADON = 0 starts nothing (one `warn`; CAL stays set); setting it before tSTAB has elapsed logs one `warn` (RM0008:
  power the ADC for at least 2 ADCCLK before calibrating) and calibrates from the ready time.  Conversion triggers
  during calibration are ignored (one `warn`).

Regular group:
* Triggers: SWSTART (only with EXTTRIG = 1 and EXTSEL = 111; otherwise it stays set without effect, one `warn`), the
  second ADON write (above), or the EXTSEL source with EXTTRIG = 1 (rising edge only):
  000 TIM1_CC1, 001 TIM1_CC2, 010 TIM1_CC3, 011 TIM2_CC2, 100 TIM3_TRGO, 101 TIM4_CC4, 110 EXTI line 11 (not modelled:
  one `warn`), 111 SWSTART.  Timer signals come from stm32.tim `add_trigger_listener` (TIMx_CCy = OCyREF of an output
  channel or a pulse per capture; TRGO per CR2.MMS); a timer absent on the part (TIM4 on the F103T6) logs one `warn`.
  SR.STRT is set at the trigger; SWSTART reads 0 again at once.  A trigger while a regular conversion runs, while the
  ADC is off or calibrating, or with the RCC clock (APB2ENR.ADCxEN) off is ignored.
* Sequence: SQR1.L + 1 channels SQ1..SQ16 with CR1.SCAN (or DISCEN), SQ1 alone otherwise.  CR2.CONT restarts the
  sequence at its end, back to back.  CR1.DISCEN: each trigger converts DISCNUM + 1 channels, continuing where the
  previous subgroup stopped (CONT with DISCEN is not allowed: one `warn`, DISCEN wins).  Writing a different value to
  SQR1-3 during a regular conversion restarts the group from SQ1 (JSQR likewise for the injected group).
* Timing (ADCCLK = rcc.adc_hz() = PCLK2 / ADCPRE): a conversion takes SMPx (1.5, 7.5, 13.5, 28.5, 41.5, 55.5, 71.5,
  239.5) + 12.5 ADCCLK; the first conversion after a trigger starts after the regular trigger latency tlatr = 2 ADCCLK
  (injected: tlat = 3 ADCCLK), the next ones follow back to back.  One scheduler event per conversion; clock changes
  during a conversion do not re-time it.
* Result: 12-bit code = floor(V / VREF+ x 4096) clipped to 0..4095 (VREF+ = `params.vref`, 3.3 V) taken at the end of
  the conversion; CR2.ALIGN right (bits 11..0) or left (bits 15..4).  SR.EOC is set at the end of the regular group
  (after every conversion with SCAN = 0; at the end of the whole sequence with SCAN or DISCEN), cleared by reading DR.
  There is no overrun detection on the F1: a new result overwrites DR.  SR bits are rc_w0 (write 0 clears).
* DMA (CR2.DMA, ADC1 only): the request "ADC1" (DMA1 channel 1) after every regular conversion, deasserted when DR is
  read.  ADC2 has no DMA request (setting its DMA bit logs one `warn`); in dual mode ADC1's DR bits 31..16 hold the
  ADC2 regular result, so a 32-bit ADC1 DMA transfer moves both.

Injected group: JSQR.JL + 1 channels, the last JL + 1 of JSQ1..JSQ4 (JL = 0: JSQ4 alone), the i-th converted channel
goes to JDRi with offset JOFRi subtracted (signed: right alignment sign-extended to 16 bits, left alignment shifted by
3 with bit 15 the sign).  Triggers: JSWSTART (JEXTTRIG = 1 and JEXTSEL = 111) or JEXTSEL with JEXTTRIG = 1 (rising
edge): 000 TIM1_TRGO, 001 TIM1_CC4, 010 TIM2_TRGO, 011 TIM2_CC1, 100 TIM3_CC4, 101 TIM4_TRGO, 110 EXTI line 15 (not
modelled), 111 JSWSTART.  SR.JSTRT at the trigger; JEOC at the end of the injected group, and EOC as well (RM0008 SR:
"EOC: end of a group channel conversion (regular or injected)").  An injected trigger during a regular conversion
interrupts it; the regular sequence resumes with that channel after the injected group.  CR1.JAUTO converts the
injected group right after the regular group (injected triggers ignored, one `warn`); CR1.JDISCEN converts one injected
channel per trigger.
Analog watchdog: CR1.AWDEN (regular) / JAWDEN (injected), all channels or AWDSGL + AWDCH; the raw 12-bit code outside
[LTR, HTR] sets SR.AWD.
Interrupt: one NVIC line "ADC1_2" shared by both ADCs: EOC & EOCIE | JEOC & JEOCIE | AWD & AWDIE (level).

Channels: 0-9 on the pins whose ADC function is ADC12_INn (PA0-PA7, PB0, PB1 on the F103T6), 10-15 on no pin of this
package (values stored as "ADCn_INm"); ADC1 channel 16 = temperature sensor and 17 = VREFINT, both enabled by ADC1
CR2.TSVREFE (0 V while disabled): V_SENSE = V25 - Avg_Slope x (T - 25) = 1.43 V - 4.3 mV/degC x (T - 25) (DS15060
Table 50 typical; T: "TEMP" in degC, default 25), VREFINT 1.20 V (DS15060 Table 12 typical).  On ADC2 channels 16/17
read 0 V.  Sensor start-up time and the 17.1 us minimum sampling time are not checked.  Stimulus API as stm32.adc:
`set_channel(name, value)` (float volts, int raw code; "IN5", "ADC1_IN5", "ADC12_IN5", a pin "PA5", "TEMP").

Dual mode (ADC1 CR1.DUALMOD; ADC1 is the master, the slave ADC2's own triggers for the synchronised group are ignored
with one `warn`):
  0001 regular simultaneous + injected simultaneous, 0010 regular simultaneous + alternate trigger,
  0011 injected simultaneous + fast interleaved, 0100 injected simultaneous + slow interleaved,
  0101 injected simultaneous, 0110 regular simultaneous, 0111 fast interleaved, 1000 slow interleaved,
  1001 alternate trigger.
Regular simultaneous: an ADC1 regular trigger starts both regular groups at once.  Injected simultaneous likewise for
the injected groups.  Fast interleaved: ADC2 starts at the trigger and ADC1 7 ADCCLK later (with CONT each restarts at
its own end, keeping the phase).  Slow interleaved is approximated as fast interleaved with a 14 ADCCLK offset (one
`warn`; the RM's ADC2/ADC1 alternation every 14 ADCCLK without CONT is not modelled).  Alternate trigger: successive
ADC1 injected triggers convert the injected group of ADC1, then ADC2, then ADC1 ...  The RM's requirement of equal
sampling times / sequence lengths in simultaneous modes is not checked.  DUALMOD 1010-1111 are reserved (one `warn`,
independent).
"""
from __future__ import annotations

import re
from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ..stm32.adc import _CH_NAME, _INT_NAME, _PIN_FN, _store
from ._util import GatedBlock, apb_hz, build_regs, dma_request, irq_number, rcc

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
# EXTSEL / JEXTSEL -> (block, signal); 7 = software start
TRIGGERS = (("TIM1", "CC1"), ("TIM1", "CC2"), ("TIM1", "CC3"), ("TIM2", "CC2"), ("TIM3", "TRGO"), ("TIM4", "CC4"),
            ("EXTI", "11"), None)
JTRIGGERS = (("TIM1", "TRGO"), ("TIM1", "CC4"), ("TIM2", "TRGO"), ("TIM2", "CC1"), ("TIM3", "CC4"), ("TIM4", "TRGO"),
             ("EXTI", "15"), None)
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
        m = re.search(r"(\d+)$", name)
        self.index = int(params.get("index", m.group(1) if m else 1))
        self.gate = f"ADC{self.index}"
        ov: dict[str, dict] = {
            "SR": {"on_write": self._sr_write},
            "CR1": {"on_write": self._cr1_write},
            "CR2": {"on_write": self._cr2_write},
            "SQR1": {"on_write": self._sqr_write}, "SQR2": {"on_write": self._sqr_write},
            "SQR3": {"on_write": self._sqr_write}, "JSQR": {"on_write": self._jsqr_write},
            "DR": {"on_read": self._dr_read, "ro_mask": 0xFFFFFFFF},
        }
        for k in range(1, 5):
            ov[f"JDR{k}"] = {"ro_mask": 0xFFFFFFFF}
        r = build_regs((name, f"ADC{self.index}", "ADC1"), size, ov, params.get("regs_file"))
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
        self._irqn = irq_number(chip, str(self.params.get("irq", "ADC1_2")), "ADC1_2", "ADC")
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
            dma_request(self.chip, "ADC1", False)
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
        if ch >= 16:
            if self.index != 1 or ch > 17:
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
        """(regular mode, injected mode) of ADC1 CR1.DUALMOD: "sim" / "fast" / "slow" / "alt" / None."""
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
        if last:
            sr |= EOC
        self.set("SR", sr)
        self._pos = 0 if last else pos + 1
        self._update_irq()
        if cr2 & DMA:
            if self.index == 1:
                self._dma_req = True
                dma_request(chip, "ADC1")
            else:
                self._warn_once("dma2", f"{self.name}: CR2.DMA set but ADC2 has no DMA request (use dual mode and "
                                        "the ADC1 DMA)")
        now = chip.now_cycles()
        if last and cr1 & JAUTO and not (self._burst is not None and self._burst > 1):
            self._burst = None
            self._start_inj(now, auto=True)
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
        if reg == "slow":
            self._warn_once("slow", f"{self.name}: slow interleaved dual mode approximated as interleaved with a "
                                    f"{SLOW_DELAY_ADCCLK} ADCCLK offset")
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
                self._ev = chip.sched.at_cycles(now + self.conv_cycles(self.sequence()[0]), self._done)
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
        self._subscribe(TRIGGERS[cr2 >> 17 & 7] if on and cr2 & EXTTRIG else None, inj=False)
        self._subscribe(JTRIGGERS[cr2 >> 12 & 7] if on and cr2 & JEXTTRIG else None, inj=True)

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
        blk_name, sig = want
        blk = self.chip.peripherals.get(blk_name)
        if blk_name == "EXTI" or not hasattr(blk, "add_trigger_listener"):
            what = "is not modelled" if blk_name == "EXTI" else f"is not present on this part ({blk_name})"
            self._warn_once(f"trig{blk_name}{sig}", f"{self.name}: external {'injected ' if inj else ''}trigger "
                            f"{blk_name}_{sig} {what}: no conversions are triggered")
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

    def _ext_trig(self, t: int, level: bool) -> None:
        if level == self._trig_lvl:
            return
        self._trig_lvl = level
        if level and not self.lp_frozen:              # rising edge only on the F1
            self._start_regular(t)

    def _ext_jtrig(self, t: int, level: bool) -> None:
        if level == self._jtrig_lvl:
            return
        self._jtrig_lvl = level
        if level and not self.lp_frozen:
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
            self._ready_at = now + max(0, round(self.tstab_us * 1e-6 * chip.clock.hz))
        if new & DMA and self.index != 1 and not old & DMA:
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
        elif restart and not new & EXTTRIG:
            self._start_regular(now)
        return v

    @staticmethod
    def _sw_ok(cr2: int, inj: bool) -> bool:
        if inj:
            return bool(cr2 & JEXTTRIG) and (cr2 >> 12 & 7) == 7
        return bool(cr2 & EXTTRIG) and (cr2 >> 17 & 7) == 7

    def _start_cal(self, now: int, reset: bool) -> None:
        if now < self._ready_at and not reset:
            self._warn_once("cal_early", f"{self.name}: calibration started before the ADC power-up time (tSTAB) "
                                         "elapsed; RM0008 requires ADON = 1 for at least 2 ADCCLK first")
        start = max(now, self._ready_at)
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

    def _dr_read(self, blk, reg):
        if self._dma_req:                            # read by the DMA or the CPU: the request is served
            self._dma_req = False
            dma_request(self.chip, "ADC1", False)
        sr = self.get("SR")
        if sr & EOC:
            self.set("SR", sr & ~EOC)
            self._update_irq()
        v = self.get("DR") & 0xFFFF
        if self.index == 1 and self.get("CR1") >> 16 & 0xF:
            a2 = self._peer(2)
            if a2 is not None:
                v |= (a2.get("DR") & 0xFFFF) << 16    # ADC2DATA: the ADC2 regular result in dual mode
        return v
