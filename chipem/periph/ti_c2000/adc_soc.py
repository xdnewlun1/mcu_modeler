"""Piccolo "type 3" SOC-based 12-bit ADC (TMS320F2802x / F2806x): `ti_c2000.adc_soc` at 0x7100 (AdcRegs) and
`ti_c2000.adc_soc_result` at 0x0B00 (AdcResult, Peripheral Frame 0).  SPRUI09A chapter 6 (2802x) = SPRUH18I chapter 8
(2806x); register offsets SPRUI09 Table 6-3 / SPRS523P Tables 9-21, 9-22.

* SOCs (6.2): 16 configurations ADCSOCxCTL = TRIGSEL (15:11), CHSEL (9:6), ACQPS (5:0).  A trigger sets ADCSOCFLG1.x;
  a trigger while the flag is already set is lost and sets ADCSOCOVF1.x (cleared by ADCSOCOVFCLR1).  Triggers:
  ADCSOCFRC1 (software), TRIGSEL 1-3 CPU timer 0-2 TINTn, 4 XINT2 (XINT2SOC), 5.. ePWMn SOCA/SOCB (5 = ePWM1 SOCA,
  6 = ePWM1 SOCB, ... 12 = ePWM4 SOCB; 2806x up to 20 = ePWM8 SOCB), and ADCINT1 / ADCINT2 through ADCINTSOCSEL1/2
  (ORed with TRIGSEL).  The result goes to ADCRESULTx of the SOC, whatever the channel.
* Priority (6.4): SOCPRICTL.SOCPRIORITY = n makes SOC0..n-1 high priority (lowest number first, 16 = all); the others
  go round robin from RRPOINTER + 1 (RRPOINTER = last round-robin SOC converted; 0x20 after reset, an ADC reset or a
  SOCPRICTL write).  ONESHOT (6.3): only the next round-robin SOC after RRPOINTER may start; triggers of other
  round-robin SOCs are ignored.
* Timing (6.11, Figures 6-8..6-11), in ADCCLK = SYSCLKOUT / (ADCCTL2.CLKDIV2EN ? 2 : 1) on the 2802x; the 2806x adds
  CLKDIV4EN (SPRUH18 Table 8-6: /1, /1, /2, /4).  A conversion starts sampling 2 ADCCLK after its trigger, samples
  ACQPS + 1 ADCCLK (minimum 7, ACQPS < 6 is invalid: one `warn`), converts 13 ADCCLK and latches the result 2 ADCCLK
  after the conversion ends.  Sequential mode overlaps the next SOC's sample window with the conversion: it starts 6
  ADCCLK after the previous window ended (Figure 6-8: windows at 2-9, 15-22; results at 24, 37), and a conversion
  waits for the previous one.  ADCCTL2.ADCNONOVERLAP holds the next sample window until the conversion has ended
  (Figure 6-12).  Simultaneous mode (ADCSAMPLEMODE.SIMULENx, 6.6) samples ADCINAn and ADCINBn of CHSEL[2:0] together,
  converts A then B (13 ADCCLK each) into ADCRESULT even/odd, and the next window starts 19 ADCCLK after this one
  ended (Figure 6-10).  ADCCTL1.ADCBSY / ADCBSYCHN: busy from the start of a window until 4 (simultaneous: 14) ADCCLK
  after it ended; ADCBSYCHN = the SOC being processed or the last one.
* EOC and interrupts (6.7): EOCx at the start of the conversion (ADCCTL1.INTPULSEPOS = 0) or 1 ADCCLK before the
  result latches (= 1); in simultaneous mode the even EOC comes from the A conversion, the odd one from B.  ADCINTy
  (INTSELxNy: INTySEL = EOC source, INTyE, INTyCONT): an EOC sets ADCINTFLG.y and pulses the PIE line(s); while the
  flag is set a further EOC sets ADCINTOVF.y and, unless INTyCONT, pulses nothing; in non-continuous mode both FLG and
  OVF must be cleared (ADCINTFLGCLR / ADCINTOVFCLR) before the next pulse.  PIE lines (SPRS523 Table 9-18): ADCINT1
  -> 1.1 and 10.1, ADCINT2 -> 1.2 and 10.2, ADCINT3-8 -> 10.3-10.8, ADCINT9 -> 1.6 (`params.irq_lines` overrides).
* Analog values: volts on the pins (`set_input("ADCINA3" | pin name, volts)`, `set_channel("ADCINA3" | "A3", value)`
  with a float = volts, an int = raw code; `adc_volts`).  Internal reference (ADCCTL1.ADCREFSEL = 0): code =
  4096 (V - VREFLO) / 3.3 V, truncated, clamped to 0..4095 (6.10.1).  External reference: VREFLO..VREFHI, VREFHI is
  the ADCINA0/VREFHI pin voltage (2802x) or the VREFHI pin (`params.vrefhi_pin`).  ADCOFFTRIM (9-bit two's
  complement) is added to every result (6.9.2).  ADCCTL1.TEMPCONV puts the temperature sensor on A5
  (`params.temp_c`, default 25 C; code = temp / slope + offset with the device's factory slope/offset words,
  `params.temp_slope` (Q15 deg C per LSB) / `temp_offset`, defaults typical), VREFLOCONV puts VREFLO on B5.  An AIO
  pin driven as a digital output (AIOMUX1/AIODIR) is converted at 0 V or VDDA (`params.vdda`, 3.3 V): the ADC path
  stays connected (SPRUI09 Figure 1-55 note A).  Unset inputs read 0 V.
* Power and gating (6.8): conversions need PCLKCR0.ADCENCLK and ADCCTL1 ADCPWDN, ADCBGPWD, ADCREFPWD and ADCENABLE
  set; the first conversion must wait 1 ms after power-up (a conversion started earlier converts but logs one `warn`).
  Without power/enable a trigger still sets its SOC flag; the conversion starts when the ADC is enabled.
  ADCCTL1.RESET resets the whole module (registers, trims, results).
* ADCSOCAO / ADCSOCBO pins: every ePWM SOCA / SOCB of any ePWM pulses the signal low for 32 SYSCLKOUT (SPRS523
  Figure 9-35 "Pulse Stretch (32 SYSCLKOUT Cycles, Active-Low Output)") on a pin muxed to it.
Not modelled: the reference/gain trims (ADCREFTRIM is storage), sample-capacitor crosstalk and residual charge
(6.5 note), comparator hysteresis (COMPHYSTCTL storage, read by the comparator model), FREE_SOFT/emulation.
"""
from __future__ import annotations

import re
from typing import Any

from ..registry import register
from ._util import C2000Block, build_regs, find_model

# ADCCTL1 (SPRUI09 Table 6-4)
RESET, ADCENABLE, ADCBSY = 1 << 15, 1 << 14, 1 << 13
ADCPWDN, ADCBGPWD, ADCREFPWD, ADCREFSEL, INTPULSEPOS, VREFLOCONV, TEMPCONV = 1 << 7, 1 << 6, 1 << 5, 1 << 3, \
    1 << 2, 1 << 1, 1 << 0
POWER = ADCPWDN | ADCBGPWD | ADCREFPWD
CLKDIV2EN, NONOVERLAP, CLKDIV4EN = 1 << 0, 1 << 1, 1 << 2
POWERUP_NS = 1_000_000.0                 # 6.8: 1 ms after the power-up bits before the first conversion
SOCO_CYCLES = 32                         # ADCSOCxO low pulse (SPRS523 Figure 9-35)
DEFAULT_LINES = {1: ("1.1", "10.1"), 2: ("1.2", "10.2"), 3: ("10.3",), 4: ("10.4",), 5: ("10.5",), 6: ("10.6",),
                 7: ("10.7",), 8: ("10.8",), 9: ("1.6",)}
_CH = re.compile(r"^(?:ADC_)?(?:ADCIN)?([AB])(\d)$")


def channel_index(name: str) -> int | None:
    m = _CH.match(str(name).strip().upper())
    if not m or int(m.group(2)) > 7:
        return None
    return (0 if m.group(1) == "A" else 8) + int(m.group(2))


class _Conv:
    """One SOC being processed: sample window [s0, s1), conversion(s) from c0, result latch times, EOC times."""
    __slots__ = ("soc", "simul", "ch", "s0", "s1", "c0", "t_res", "t_eoc", "done")

    def __init__(self, soc, simul, ch, s0, s1, c0, t_res, t_eoc):
        self.soc, self.simul, self.ch = soc, simul, ch
        self.s0, self.s1, self.c0 = s0, s1, c0
        self.t_res, self.t_eoc = t_res, t_eoc          # lists (A[, B])
        self.done = set()


@register("ti_c2000.adc_soc")
class AdcSoc(C2000Block):
    adc_volts = True
    eallow_regs = ("ADCCTL1", "ADCCTL2", "INTSEL1N2", "INTSEL3N4", "INTSEL5N6", "INTSEL7N8", "INTSEL9N10",
                   "SOCPRICTL", "ADCSAMPLEMODE", "ADCINTSOCSEL1", "ADCINTSOCSEL2",
                   *(f"ADCSOC{i}CTL" for i in range(16)), "ADCREFTRIM", "ADCOFFTRIM", "COMPHYSTCTL")

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov: dict[str, dict] = {
            "ADCCTL1": {"on_write": self._ctl1_write, "on_read": self._ctl1_read, "on_peek": self._ctl1_read},
            "ADCCTL2": {"on_write": self._ctl_write},
            "ADCINTFLG": {"ro_mask": 0xFFFF},
            "ADCINTFLGCLR": {"on_write": self._intflgclr_write, "on_read": lambda b, r: 0, "on_peek": lambda b, r: 0},
            "ADCINTOVF": {"ro_mask": 0xFFFF},
            "ADCINTOVFCLR": {"on_write": self._intovfclr_write, "on_read": lambda b, r: 0, "on_peek": lambda b, r: 0},
            "SOCPRICTL": {"on_write": self._socprictl_write, "on_read": self._socprictl_read, "on_peek": self._socprictl_read},
            "ADCSOCFLG1": {"ro_mask": 0xFFFF, "on_read": self._socflg_read, "on_peek": self._socflg_read},
            "ADCSOCFRC1": {"on_write": self._socfrc_write, "on_read": lambda b, r: 0, "on_peek": lambda b, r: 0},
            "ADCSOCOVF1": {"ro_mask": 0xFFFF},
            "ADCSOCOVFCLR1": {"on_write": self._socovfclr_write, "on_read": lambda b, r: 0, "on_peek": lambda b, r: 0},
            "ADCSAMPLEMODE": {"on_write": self._ctl_write},
        }
        for i in range(16):
            ov[f"ADCSOC{i}CTL"] = {"on_write": self._socctl_write}
        regs = build_regs((name, "ADC"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, regs, params=params, log=log)
        self.gate = params.get("gate", "ADCENCLK")
        self.n_trig = int(params.get("triggers", 12))            # highest valid TRIGSEL (2806x: 20)
        self.clkdiv4 = bool(params.get("clkdiv4", self._has_field("ADCCTL2", "CLKDIV4EN")))   # 2806x
        self.vdda = float(params.get("vdda", 3.3))
        self.vreflo = float(params.get("vreflo", 0.0))
        self.vrefhi_pin = str(params.get("vrefhi_pin", "ADCINA0")).upper()
        self.temp_c = float(params.get("temp_c", 25.0))
        self.temp_slope = int(params.get("temp_slope", 5898))     # Q15 deg C per LSB: 0.18 (SPRS523 9.9.1.1.3.3.1)
        self.temp_offset = int(params.get("temp_offset", 1750))   # LSB at 0 deg C
        self.irq_lines = {int(k): tuple(v) if isinstance(v, (list, tuple)) else (v,)
                          for k, v in (params.get("irq_lines") or DEFAULT_LINES).items()}
        self.volts: dict[int, float] = {}          # channel index 0..15 -> volts
        self.codes: dict[int, int] = {}            # raw-code overrides (set_channel with an int)
        self.pin_volts: dict[str, float] = {}      # other analog pins by name (VREFHI, ...)
        self.results = [0] * 16
        self.flags = 0                             # ADCSOCFLG1
        self.rr = 0x20                             # RRPOINTER
        self._busy_soc = 0                         # ADCBSYCHN
        self._convs: list[_Conv] = []              # SOCs in the pipeline
        self._samp_free = 0                        # cycle from which the next sample window may start
        self._conv_free = 0                        # cycle from which the next conversion may start
        self._busy_until = 0
        self._arb_ev = None
        self._ev = None
        self._power_t: float | None = None         # wall time at which all three power bits became 1
        self._warned: set[str] = set()
        self._soco_ev: dict[str, Any] = {}
        self._gpio = None
        self.conversions = 0
        self.lost = 0
        self.int_pulses = [0] * 10
        self.result_listeners: list = []           # cb(soc, value) when a result latches
        self.adcint_listeners: list = []           # cb(n) on every ADCINTn pulse (DMA triggers, other ADC SOCs)

    def _has_field(self, reg: str, field: str) -> bool:
        from ._util import table
        try:
            pr = table((self.name, "ADC"), self.params.get("regs_file"))
            return pr is not None and reg in pr and pr[reg].has_field(field)
        except Exception:
            return False

    # ------------------------------------------------------------------ lifecycle
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._gpio = find_model(chip, "ti_c2000.gpio")
        for b in chip.peripherals.values():
            if getattr(b, "model", "") in ("ti_c2000.epwm", "ti_c2000.epwm_dc") and hasattr(b, "soc_listeners"):
                b.soc_listeners.append(self._epwm_soc)
            elif getattr(b, "model", "") == "ti_c2000.cputimer" and hasattr(b, "tint_listeners"):
                b.tint_listeners.append(self._tint)
            elif getattr(b, "model", "") == "ti_c2000.xint" and hasattr(b, "int_listeners"):
                b.int_listeners.append(self._xint)

    def reset(self) -> None:
        self._cancel()
        super().reset()
        self._soft_state()

    def _cancel(self) -> None:
        if self.chip is not None:
            for ev in (self._arb_ev, self._ev, *self._soco_ev.values()):
                if ev is not None:
                    self.chip.sched.cancel(ev)
        self._arb_ev = self._ev = None
        self._soco_ev = {}

    def _soft_state(self) -> None:
        self.results = [0] * 16
        self.flags = 0
        self.rr = 0x20
        self._busy_soc = 0
        self._convs = []
        self._samp_free = self._conv_free = self._busy_until = 0
        self._power_t = None
        if self.chip is not None and self.chip.pins is not None:
            for sig in ("ADCSOCAO", "ADCSOCBO"):
                self.chip.pins.release_signal(sig)

    def clock_resumed(self, dt_cycles: int) -> None:
        for c in self._convs:
            c.s0 += dt_cycles
            c.s1 += dt_cycles
            c.c0 += dt_cycles
            c.t_res = [t + dt_cycles for t in c.t_res]
            c.t_eoc = [t + dt_cycles for t in c.t_eoc]
        self._samp_free += dt_cycles
        self._conv_free += dt_cycles
        self._busy_until += dt_cycles

    # ------------------------------------------------------------------ analog inputs
    def set_channel(self, name: str, value) -> bool:
        k = channel_index(name)
        if k is None:
            return self.set_input(name, value)
        if isinstance(value, float):
            self.volts[k] = value
            self.codes.pop(k, None)
        else:
            self.codes[k] = int(value) & 0xFFF
        return True

    def set_input(self, name: str, volts) -> bool:
        """Voltage on an analog pin, by pin id or name part (ADCINA2, "ADCINA2/COMP1A/AIO2", VREFHI)."""
        if not isinstance(volts, float):
            k = channel_index(name)
            if k is None:
                return False
            self.codes[k] = int(volts) & 0xFFF
            return True
        names = [str(name).strip().upper()]
        pins = getattr(self.chip, "pins", None) if self.chip is not None else None
        if pins is not None:
            try:
                p = pins.get(name)
                names = [s.strip().upper() for s in p.name.split("/")]
            except KeyError:
                pass
        hit = False
        for nm in names:
            k = channel_index(nm)
            if k is not None:
                self.volts[k] = float(volts)
                self.codes.pop(k, None)
                hit = True
            elif nm in ("VREFHI", "VREFLO"):
                self.pin_volts[nm] = float(volts)
                hit = True
        return hit

    def _vref(self) -> tuple[float, float]:
        if not self.get("ADCCTL1") & ADCREFSEL:
            return self.vreflo, self.vreflo + 3.3
        hi = self.pin_volts.get("VREFHI")
        if hi is None:
            k = channel_index(self.vrefhi_pin)
            hi = self.volts.get(k, 0.0) if k is not None else 0.0
        return self.pin_volts.get("VREFLO", self.vreflo), hi

    def _aio_level(self, k: int) -> float | None:
        """0 V / VDDA if the channel's AIO pin is driven as a digital output, else None."""
        g = self._gpio
        f = getattr(g, "aio_drive", None) if g is not None else None
        if f is None:
            return None
        lvl = f("ADCIN" + ("A" if k < 8 else "B") + str(k & 7))
        return None if lvl is None else (self.vdda if lvl == "H" else 0.0)

    def channel_code(self, k: int) -> int:
        """Conversion result of channel k (0..15 = A0..A7, B0..B7) now, before the offset trim."""
        ctl = self.get("ADCCTL1")
        if k == 5 and ctl & TEMPCONV:
            code = int(self.temp_c * 32768 / self.temp_slope) + self.temp_offset if self.temp_slope else 0
            return max(0, min(4095, code))
        if k == 13 and ctl & VREFLOCONV:
            v = self._vref()[0]
        else:
            if k in self.codes and self._aio_level(k) is None:
                return self.codes[k]
            v = self._aio_level(k)
            if v is None:
                v = self.volts.get(k, 0.0)
        lo, hi = self._vref()
        if hi <= lo:
            self._warn("vref", f"{self.name}: external reference VREFHI ({hi:.3f} V) <= VREFLO: results are 0")
            return 0
        code = int(4096.0 * (v - lo) / (hi - lo))
        return max(0, min(4095, code))

    # ------------------------------------------------------------------ clocks / helpers
    def adcclk_div(self) -> int:
        c2 = self.get("ADCCTL2") if "ADCCTL2" in self.regs else 0
        if not c2 & CLKDIV2EN:
            return 1
        return 4 if (self.clkdiv4 and c2 & CLKDIV4EN) else 2

    def _warn(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    def _ready(self) -> bool:
        ctl = self.get("ADCCTL1")
        return (ctl & POWER) == POWER and bool(ctl & ADCENABLE)

    def _sel(self, soc: int) -> int:
        return self.get(f"ADCSOC{soc}CTL")

    def _simul(self, soc: int) -> bool:
        return bool(self.get("ADCSAMPLEMODE") >> (soc // 2) & 1)

    # ------------------------------------------------------------------ triggers
    def _trigger(self, mask: int, why: str) -> None:
        """Set the SOC flags of `mask`; a flag that is already set overflows (ADCSOCOVF1)."""
        if not mask:
            return
        if self.get("SOCPRICTL") & 0x8000:                 # ONESHOT: only the next round-robin SOC may start
            nxt = (0 if self.rr >= 16 else self.rr + 1) & 15
            prio = self._prio_n()
            rr_mask = mask & ~((1 << prio) - 1) if prio < 16 else 0
            mask = (mask & ((1 << prio) - 1 if prio < 16 else 0xFFFF)) | (rr_mask & (1 << nxt))
            if not mask:
                return
        ovf = self.flags & mask
        if ovf:
            self.set("ADCSOCOVF1", self.get("ADCSOCOVF1") | ovf)
            self.lost += bin(ovf).count("1")
        self.flags |= mask
        self._schedule_arb()

    def _matching(self, trigsel: int) -> int:
        m = 0
        for i in range(16):
            if (self._sel(i) >> 11) == trigsel:
                m |= 1 << i
        return m

    def _epwm_soc(self, index: int, which: str) -> None:
        sig = "ADCSOCAO" if which == "A" else "ADCSOCBO"
        self._soco_pulse(sig)
        t = 5 + 2 * (index - 1) + (0 if which == "A" else 1)
        if t <= self.n_trig:
            self._trigger(self._matching(t), f"ePWM{index} SOC{which}")

    def _tint(self, index: int) -> None:
        if 0 <= index <= 2:
            self._trigger(self._matching(1 + index), f"TINT{index}")

    def _xint(self, n: int) -> None:
        if n == 2:
            self._trigger(self._matching(4), "XINT2")

    def _adcint_soc(self, n: int) -> None:
        """ADCINT1/2 pulse: SOCs selected in ADCINTSOCSEL1/2 (6.13.5.2)."""
        m = 0
        for i in range(16):
            reg = "ADCINTSOCSEL1" if i < 8 else "ADCINTSOCSEL2"
            if (self.get(reg) >> (2 * (i & 7))) & 3 == n:
                m |= 1 << i
        self._trigger(m, f"ADCINT{n}")

    def _soco_pulse(self, sig: str) -> None:
        pins = getattr(self.chip, "pins", None)
        if pins is None or not pins.signal_pins(sig):
            return
        ev = self._soco_ev.get(sig)
        if ev is not None:
            self.chip.sched.cancel(ev)
        pins.drive_signal(sig, "L")
        self._soco_ev[sig] = self.chip.sched.in_cycles(SOCO_CYCLES, self._soco_end, sig)

    def _soco_end(self, sig: str) -> None:
        self._soco_ev.pop(sig, None)
        self.chip.pins.drive_signal(sig, "H")

    # ------------------------------------------------------------------ arbitration and pipeline
    def _prio_n(self) -> int:
        p = self.get("SOCPRICTL") & 0x1F
        return 16 if p >= 16 else p

    def _pick(self) -> int | None:
        f = self.flags
        if not f:
            return None
        prio = self._prio_n()
        hp = f & ((1 << prio) - 1)
        if hp:
            return (hp & -hp).bit_length() - 1
        start = 0 if self.rr >= 16 else self.rr + 1
        for j in range(16):
            i = (start + j) & 15
            if i >= prio and f >> i & 1:
                return i
        return None

    def _schedule_arb(self) -> None:
        if self.chip is None or self._arb_ev is not None or not self.flags:
            return
        if not self._ready():
            if not self.get("ADCCTL1") & ADCENABLE or (self.get("ADCCTL1") & POWER) != POWER:
                self._warn("off", f"{self.name}: SOC triggered while the ADC is powered down / not enabled "
                           "(ADCCTL1 ADCPWDN/ADCBGPWD/ADCREFPWD/ADCENABLE): the SOC stays pending")
            return
        sc = self._sysctrl
        if sc is not None and not sc.clock_enabled(self.gate):
            self._warn("gate", f"{self.name}: SOC triggered with PCLKCR0.ADCENCLK = 0: no conversion")
            return
        now = self.chip.now_cycles()
        d = self.adcclk_div()
        t = max(now + 2 * d, self._samp_free)
        self._arb_ev = self.chip.sched.at_cycles(t, self._start_next)

    def _start_next(self) -> None:
        self._arb_ev = None
        soc = self._pick()
        if soc is None or not self._ready():
            return
        now = self.chip.now_cycles()
        d = self.adcclk_div()
        ctl = self._sel(soc)
        acq = ctl & 0x3F
        if acq < 6:
            self._warn("acqps", f"{self.name}: ADCSOC{soc}CTL.ACQPS = {acq} is invalid (minimum 6 = 7 ADCCLK)")
        ch = (ctl >> 6) & 0xF
        simul = self._simul(soc)
        if simul and ch > 7:
            self._warn("simch", f"{self.name}: SOC{soc} CHSEL = {ch} is invalid in simultaneous mode")
            ch &= 7
        if self._power_t is not None and self.chip.now_ns() < self._power_t + POWERUP_NS:
            self._warn("pwr", f"{self.name}: conversion started less than 1 ms after the ADC power-up (SPRUI09 6.8): "
                       "the result is not valid on silicon")
        self.flags &= ~(1 << soc)
        if soc >= self._prio_n():
            self.rr = soc
        s0 = now
        s1 = s0 + (acq + 1) * d
        c0 = max(s1, self._conv_free)
        conv = 13 * d
        if simul:
            t_res = [c0 + conv + 2 * d, c0 + 2 * conv + 2 * d]
            late = [t - d for t in t_res]
            early = [c0, c0 + conv]
            self._conv_free = c0 + 2 * conv
            self._samp_free = s1 + 19 * d
            self._busy_until = s1 + 14 * d
        else:
            t_res = [c0 + conv + 2 * d]
            late = [t_res[0] - d]
            early = [c0]
            self._conv_free = c0 + conv
            self._samp_free = s1 + 6 * d
            self._busy_until = s1 + 4 * d
        if self.get("ADCCTL2") & NONOVERLAP:
            self._samp_free = max(self._samp_free, self._conv_free)
        t_eoc = late if self.get("ADCCTL1") & INTPULSEPOS else early
        base = soc & ~1 if simul else soc
        self._convs.append(_Conv(base if simul else soc, simul, ch, s0, s1, c0, t_res, t_eoc))
        self._busy_soc = soc
        self._schedule_next_event()
        if self.flags:
            self._schedule_arb()

    def _schedule_next_event(self) -> None:
        if self._ev is not None:
            self.chip.sched.cancel(self._ev)
            self._ev = None
        times = [t for c in self._convs for i, t in enumerate(c.t_res + c.t_eoc)
                 if ("r" if i < len(c.t_res) else "e", i % len(c.t_res)) not in c.done]
        if times:
            self._ev = self.chip.sched.at_cycles(min(times), self._step)

    def _step(self) -> None:
        self._ev = None
        now = self.chip.now_cycles()
        for c in list(self._convs):
            n = len(c.t_res)
            for i in range(n):                    # EOC before the result latch (late pulse is 1 ADCCLK earlier)
                if ("e", i) not in c.done and c.t_eoc[i] <= now:
                    c.done.add(("e", i))
                    self._eoc(c.soc + i if c.simul else c.soc)
                if ("r", i) not in c.done and c.t_res[i] <= now:
                    c.done.add(("r", i))
                    k = (c.ch + 8 * i) if c.simul else c.ch
                    self._latch(c.soc + i if c.simul else c.soc, k)
            if len(c.done) == 2 * n:
                self._convs.remove(c)
        self._schedule_next_event()

    def _latch(self, soc: int, k: int) -> None:
        code = self.channel_code(k)
        off = self.get("ADCOFFTRIM") & 0x1FF if "ADCOFFTRIM" in self.regs else 0
        if off & 0x100:
            off -= 0x200
        v = max(0, min(4095, code + off))
        self.results[soc] = v
        self.conversions += 1
        for cb in list(self.result_listeners):
            cb(soc, v)

    def _eoc(self, n: int) -> None:
        for y in range(1, 10):
            reg = f"INTSEL{y if y % 2 else y - 1}N{y + 1 if y % 2 else y}"
            if reg not in self.regs:
                continue
            v = self.get(reg) >> (0 if y % 2 else 8)
            if not v & 0x20 or (v & 0x1F) != n:
                continue
            flg, ovf = self.get("ADCINTFLG"), self.get("ADCINTOVF")
            bit = 1 << (y - 1)
            if flg & bit:
                self.set("ADCINTOVF", ovf | bit)
                if not v & 0x40:
                    continue
            elif ovf & bit and not v & 0x40:
                continue                          # non-continuous: OVF must be cleared too
            self.set("ADCINTFLG", self.get("ADCINTFLG") | bit)
            self._pulse(y)

    def _pulse(self, y: int) -> None:
        self.int_pulses[y] += 1
        for line in self.irq_lines.get(y, ()):
            self.pulse_irq(line)
        for cb in list(self.adcint_listeners):
            cb(y)
        if y in (1, 2):
            self._adcint_soc(y)

    # ------------------------------------------------------------------ registers
    def _ctl1_read(self, blk, reg):
        v = self.get("ADCCTL1") & ~(ADCBSY | 0x1F00 | RESET)
        now = self.chip.now_cycles() if self.chip is not None else 0
        busy = any(c.s0 <= now for c in self._convs) and now < self._busy_until
        return v | (ADCBSY if busy else 0) | (self._busy_soc << 8)

    def _ctl1_write(self, blk, reg, old, new):
        if new & RESET:
            self._cancel()
            C2000Block.reset(self)
            self._soft_state()
            self.emit("info", block=self.name, msg="ADCCTL1.RESET: ADC module reset (trims cleared: call "
                      "Device_cal again)")
            return self.get("ADCCTL1")
        new &= ~(RESET | ADCBSY | 0x1F00)
        self.set("ADCCTL1", new)
        if (new & POWER) == POWER and (old & POWER) != POWER:
            self._power_t = self.chip.now_ns() if self.chip is not None else 0.0
        elif (new & POWER) != POWER:
            self._power_t = None
        self._schedule_arb()
        return new

    def _ctl_write(self, blk, reg, old, new):
        self._store(reg, new)
        return None

    def _intflgclr_write(self, blk, reg, old, new):
        self.set("ADCINTFLG", self.get("ADCINTFLG") & ~new & 0x1FF)
        return 0

    def _intovfclr_write(self, blk, reg, old, new):
        self.set("ADCINTOVF", self.get("ADCINTOVF") & ~new & 0x1FF)
        return 0

    def _socprictl_write(self, blk, reg, old, new):
        self.rr = 0x20                                # a write resets RRPOINTER (6.4)
        v = new & 0x801F
        self.set("SOCPRICTL", v)
        return v

    def _socprictl_read(self, blk, reg):
        return (self.get("SOCPRICTL") & 0x801F) | (self.rr << 5)

    def _socflg_read(self, blk, reg):
        return self.flags

    def _socfrc_write(self, blk, reg, old, new):
        self._trigger(new & 0xFFFF, "ADCSOCFRC1")
        return 0

    def _socovfclr_write(self, blk, reg, old, new):
        self.set("ADCSOCOVF1", self.get("ADCSOCOVF1") & ~new)
        return 0

    def _socctl_write(self, blk, reg, old, new):
        trig = new >> 11
        if trig > self.n_trig:
            self._warn(f"trig{trig}", f"{self.name}.{reg.name}: TRIGSEL = {trig} is an invalid selection")
        self._store(reg, new & ~0x400)
        return None


@register("ti_c2000.adc_soc_result")
class AdcSocResult(C2000Block):
    """ADCRESULT0-15 at 0x0B00 (Peripheral Frame 0, 0-wait reads): 12-bit right-justified results of the
    `ti_c2000.adc_soc` model, read-only."""

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov = {f"ADCRESULT{i}": {"on_read": self._read, "ro_mask": 0xFFFF} for i in range(16)}
        regs = build_regs((name, "ADCRESULT"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, regs, params=params, log=log)
        self._adc = None

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._adc = find_model(chip, "ti_c2000.adc_soc")

    def _read(self, blk, reg):
        a = self._adc
        return a.results[reg.offset] if a is not None and reg.offset < 16 else 0

    _read.pure_read = True        # type: ignore[attr-defined]

    def read(self, offset: int, size: int) -> int:
        a = self._adc
        if a is not None and offset < 16 and not self.log_all:
            if size == 1:
                return a.results[offset]
            return a.results[offset] | (a.results[offset + 1] << 16 if offset + 1 < 16 else 0)
        return C2000Block.read(self, offset, size)
