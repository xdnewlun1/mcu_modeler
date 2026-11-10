"""C2833x enhanced PWM module ePWM1-6 (TMS320x2833x/2823x ePWM reference guide, SPRUG04A; SPRS439 for pin timing).

Time base (TB): TBCLK = SYSCLKOUT / (HSPCLKDIV x CLKDIV) (HSPCLKDIV 0 -> 1, n -> 2n; CLKDIV n -> 2^n), counted in
exact core cycles. It runs while TBCTL.CTRMODE != 3 (freeze), PCLKCR0.TBCLKSYNC and PCLKCR1.EPWMxENCLK are set.
* up-count: 0 .. TBPRD, period TBPRD+1; down-count: TBPRD .. 0, period TBPRD+1; up-down: 0 .. TBPRD .. 0,
  period 2 x TBPRD. Events CTR=ZERO, CTR=PRD, CTR=CMPA / CMPB (CAU/CAD, CBU/CBD by count direction).
  A TBCTL mode / divider change while running continues from the current TBCTR and direction.
* TBCTR above TBPRD (a TBCTR write or a TBPHS load above the period, or TBPRD lowered below TBCTR with PRDLD): up
  count, and up-down while counting up, count on to 0xFFFF and wrap to 0 (CTR=ZERO at the wrap); down count, and
  up-down while counting down, count down to TBPRD and carry on normally.  TBSTS.CTRMAX latches when the counter
  reaches 0xFFFF (also with TBPRD = 0xFFFF; write 1 to clear).  No compare events are generated above TBPRD.
  SPRUG04 does not describe this; the behaviour follows the counter logic (direction changes only at TBCTR = TBPRD
  while counting up and at 0) and later TI TRMs.
* compare values outside the period (SPRUG04 "Behavior if CMPA/CMPB is greater than the period"): up-count, no event
  when CMPx > TBPRD; down-count, CxD at TBCTR = TBPRD when CMPx >= TBPRD; up-down, CxU *and* CxD at TBCTR = TBPRD when
  CMPx >= TBPRD (the down-count priority applies there, so CAD wins over CAU), otherwise CxU while counting up
  (TBCTR = 0 included: CMPx = 0 gives CxU at zero, no CxD) and CxD while counting down (TBPRD-1 .. 1).
* TBPRD is shadowed (loaded at CTR=ZERO) unless TBCTL.PRDLD; TBCTR reads are live; TBSTS.CTRDIR is live.  A TBCTR
  write takes effect at once (SPRUG04 TBCTR: "not synchronized to TBCLK") and the counter steps on at the next edge
  of the running TBCLK (the prescaler phase is kept).
* synchronization (SPRUG04 2.2.3.2; F2833x scheme 3, SPRS439 Figure 8-4): EPWM1SYNCO -> ePWM2, ePWM4 and eCAP1;
  ePWM2 -> ePWM3; ePWM4 -> ePWM5 -> ePWM6.  SYNCI (that pulse, TBCTL.SWFSYNC, or for ePWM1 a rising edge on the
  EPWMSYNCI pin) latches TBSTS.SYNCI and, with PHSEN, loads TBCTR from TBPHS at the next TBCLK edge, at least 2
  SYSCLKOUT after the pulse ("2 x SYSCLKOUT if TBCLK = SYSCLKOUT, else 1 TBCLK"); PHSDIR picks the direction in
  up-down mode; events at the loaded count fire at that edge.  SYNCO per SYNCOSEL: SYNCI passed through (SWFSYNC
  included), CTR=ZERO, CTR=CMPB, or off.
  `sync_listeners`: callables cb(t_cycles) called whenever EPWMxSYNCO pulses (t = the core cycle of the pulse); the
  eCAP model subscribes to EPWM1's.  `params.sync_next` overrides the downstream ePWMs (name or list).
* EPWMSYNCI pin (GPIO6 MUX2 / GPIO32 MUX2): the input is the level of the one pin muxed to it (0 when none or more
  than one is, SPRUI07 Table 1-46); its rising edge is a SYNCI for ePWM1.  A mux change itself is not an edge.
  EPWMSYNCO pin (GPIO6 MUX3 / GPIO33 MUX2): ePWM1's SYNCO as an 8-SYSCLKOUT high pulse (SPRS439 tw(SYNCOUT)), driven
  only while a pin is muxed to it.
Counter compare (CC): CMPA/CMPB shadowed unless CMPCTL.SHDWxMODE, loaded per LOADxMODE (CTR=0, CTR=PRD, both,
  freeze); CMPCTL.SHDWxFULL shows a pending shadow value.
Action qualifier (AQ): AQCTLA/B (ZRO, PRD, CAU, CAD, CBU, CBD: nothing / clear / set / toggle); simultaneous events
  follow the priority table of SPRUG04 (up-count: PRD > CBU > CAU > ZRO; down: ZRO > CBD > CAD > PRD; up-down
  counting up: CBU > CAU > ZRO > CBD > CAD, counting down (TBCTR = TBPRD .. 1): CBD > CAD > PRD > CBU > CAU);
  AQSFRC one-time and AQCSFRC continuous software forcing; AQCSFRC is shadowed and loaded per AQSFRC.RLDCSF
  (00 at CTR=ZERO, 01 at CTR=PRD, 10 at either, 11 immediately; reads return the shadow / written value).
Dead band (DB): DBCTL IN_MODE (sources of the rising/falling-edge delay), OUT_MODE (bypass / FED on B / RED on A /
  both), POLSEL (invert the RED path = A, the FED path = B), DBRED / DBFED in TBCLK cycles (a pulse shorter than the
  delay disappears).
PWM chopper (PC, SPRUG04 2.6): with PCCTL.CHPEN each output is ANDed, while high, with the one-shot pulse
  (8 x (OSHTWTH+1) SYSCLKOUT from the rising edge) OR the carrier (period 8 x (CHPFREQ+1) SYSCLKOUT, high for
  CHPDUTY+1 eighths; CHPDUTY = 7 is reserved and acts as 7/8).  The carrier phase counts from the PCCTL write that
  enabled CHPEN.  Every carrier edge is a scheduled event while an input is high (one `warn`: slow to simulate).
Trip zone (TZ): TZ1-TZ6 inputs (active low; GPIO12-15 MUX1, GPIO16/17 MUX3) are the level of the one pin muxed to
  TZn (1 = inactive when none or more than one is, SPRUI07 Table 1-46).  TZSEL OSHTn: a low TZn latches the one-shot
  trip (TZFLG.OST, cleared by TZCLR.OST; set again at once while the pin is still low); CBCn: a low TZn trips
  cycle-by-cycle (TZFLG.CBC, set again at once after TZCLR.CBC while low), the action ends at the first CTR=ZERO with
  every selected pin high.  Software forcing TZFRC OST/CBC as a pin trip (the forced CBC action ends at the next
  CTR=ZERO).  TZCTL TZA/TZB: 00 high impedance, 01 force high, 10 force low, 11 no change.  High impedance releases
  the EPWMxA/B signal (PinBoard.release_signal; `signal_driven(side)` answers False), but the C2000 GPIO model has
  no hook for a released peripheral output: the pin stays an output at the mux idle level (L) instead of floating.
  TZFLG / TZEINT -> EPWMx_TZINT; TZCLR.INT with an enabled OST/CBC flag still set generates another TZINT pulse.
  The pins are watched only while TZSEL selects one.  EALLOW-protected registers as on silicon (TZSEL, TZCTL,
  TZEINT, TZCLR, TZFRC, HRCNFG).
Event trigger (ET): ETSEL INTSEL/SOCASEL/SOCBSEL (ZRO, PRD, CAU, CAD, CBU, CBD), ETPS prescaling (1st/2nd/3rd event,
  INTCNT/SOCxCNT live; INTCNT stops at INTPRD while ETFLG.INT is set), ETFLG / ETCLR / ETFRC; INT -> EPWMx_INT (PIE
  3.x); SOCA/SOCB start the ADC sequencers and pulse the ADCSOCAO/BO pins (ti_c2000.adc.epwm_soc), and call
  `soc_listeners` cb(index, "A" | "B") (the DMA's ePWMxSOCA/B triggers).
Pins: EPWMxA / EPWMxB follow the outputs (PinBoard.drive_signal); a GPIO muxed to EPWMxA/B later shows the current
  level.  TBPRD = 0 with actions or events enabled schedules an event every TBCLK (correct but slow: one `warn`).
HRPWM (SPRUI07 ch. 4 = SPRUG02, type-0 HRPWM; `params.hrpwm: true` on the instances that have it, ePWM1-4 on the
  F28232, SPRS439 Table 5-2; elsewhere the locations are plain storage): TBPHSHR (0x02) and CMPAHR (0x08) hold the
  8-bit MEP step count in bits 15:8 (bits 7:0 read 0), HRCNFG (0x20, EALLOW) EDGMODE (00 off, 01 rising, 10 falling,
  11 both edges), CTLMODE (0: CMPAHR = duty control, 1: TBPHSHR = phase control), HRLOAD (CMPAHR shadow loaded at
  CTR=ZERO / CTR=PRD).  There is no TBPRDHR on this HRPWM type.  CMPAHR is shadowed like CMPA (CMPCTL.SHDWAMODE = 1:
  immediate), but its load event comes from HRLOAD.  The MEP sits between the action qualifier and the dead band on
  output A only: an AQ edge of the selected kind made by a counter event is delayed by steps x `params.mep_step_ps`
  (default 150 ps, SPRS439 MEP step typ. 150 / max 310 ps); steps = 0 disables the MEP (SPRUI07: minimum 1).  The MEP
  is inactive for an edge in the first 3 SYSCLKOUT of the period (SPRUI07 4.2.3.3; one `warn`), and for software
  forced (AQSFRC/AQCSFRC) changes.  Phase control (CTLMODE = 1) delays the selected edges by TBPHSHR steps at all
  times (not only after a TBPHS load).  Resolution: pin edges are timestamped in core cycles (10 ns at 100 MHz), so
  a delayed edge is shown at the nearest SYSCLKOUT edge; the exact edge times of the MEP output (before dead band)
  are kept in `hr_rise_ns` / `hr_fall_ns`, `hr_period_ns` / `hr_high_ns` / `hr_duty` (fractional ns, from the
  last complete period) and `hr_state()`; an `info` event reports every HRCNFG change and active step count change
  (the first 16 of the latter).  Not modelled: the MEP calibration / diagnostics logic used by TI's SFO library
  (reserved registers: SFO_MepDis/SFO_MepEn never return a scale factor), MEP step variation with temperature and
  voltage.
Not modelled: TBCTL.FREE_SOFT (stored; CHIP_EM has no emulation-suspend
  state, so the counter always runs free); the GPIO input qualification of TZn / EPWMSYNCI (edges act at once);
  INTCNT/SOCxCNT do not count while the source is disabled (ETSEL xxEN = 0).
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ._util import C2000Block, build_regs, find_model, irq_line, sysctrl, listen_input, unlisten_input

UP, DOWN, UPDOWN, FREEZE = 0, 1, 2, 3
PHSEN, PRDLD, SWFSYNC, PHSDIR = 1 << 2, 1 << 3, 1 << 6, 1 << 13
SHDWAMODE, SHDWBMODE, SHDWAFULL, SHDWBFULL = 1 << 4, 1 << 6, 1 << 8, 1 << 9
TZ_INT, TZ_CBC, TZ_OST = 1 << 0, 1 << 1, 1 << 2
ET_INT, ET_SOCA, ET_SOCB = 1 << 0, 1 << 2, 1 << 3
CTRMAX, SYNCI_FLAG = 1 << 2, 1 << 1
CHPEN = 1 << 0
EDGMODE, CTLMODE, HRLOAD = 3, 1 << 2, 1 << 3     # HRCNFG
MEP_INACTIVE = 3                   # SYSCLKOUT cycles at the start of the period without MEP (SPRUI07 4.2.3.3)
HR_NOTES = 16                      # active MEP step changes reported as `info` events
SYNC_DELAY = 2                     # SYSCLKOUT cycles from a SYNCI pulse to the earliest TBPHS load (SPRUG04 2.2.3.2)
SYNCO_PIN_CYCLES = 8               # EPWMSYNCO pulse width, SPRS439 tw(SYNCOUT) = 8 tc(SCO)
# F2833x synchronization scheme 3 (SPRS439 Figure 8-4)
SYNC_NEXT = {1: ("EPWM2", "EPWM4"), 2: ("EPWM3",), 4: ("EPWM5",), 5: ("EPWM6",)}
# AQCTL field positions per event; ETSEL codes per event
AQ_SHIFT = {"ZRO": 0, "PRD": 2, "CAU": 4, "CAD": 6, "CBU": 8, "CBD": 10}
ET_CODE = {"ZRO": 1, "PRD": 2, "CAU": 4, "CAD": 5, "CBU": 6, "CBD": 7}
# processing order, lowest priority first (the last action applied wins)
ORDER = {UP: ("ZRO", "CAU", "CBU", "PRD"), DOWN: ("PRD", "CAD", "CBD", "ZRO"),
         "updown_up": ("CAD", "CBD", "ZRO", "CAU", "CBU"), "updown_down": ("CAU", "CBU", "PRD", "CAD", "CBD")}
TZ_SIGNALS = tuple(f"TZ{n}" for n in range(1, 7))


def _aq(level: bool, act: int) -> bool:
    return {1: False, 2: True, 3: not level}.get(act, level)


def muxed_pins(board: Any, signal: str) -> list:
    """Pins whose current owner (GPIO mux) is `signal`."""
    return [p for p in board.signal_pins(signal) if board.signal_matches(p.owner, signal)]


@register("ti_c2000.epwm")
class EPWM(C2000Block):
    cycle_anchors = ("_cyc0", "_sync_at", "_pc_base", "_osht_end")
    eallow_regs = ("TZSEL", "TZCTL", "TZEINT", "TZCLR", "TZFRC", "HRCNFG")

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.index = int(params.get("index", name[-1] if name[-1].isdigit() else 1))
        cfg = {"on_write": self._cfg_write}
        ov = {
            "TBCTL": {"on_write": self._tbctl_write},
            "TBSTS": {"on_read": self._tbsts_read, "on_write": self._tbsts_write},
            "TBCTR": {"on_read": self._tbctr_read, "on_write": self._tbctr_write},
            "TBPRD": {"on_write": self._tbprd_write},
            "CMPA": {"on_write": self._cmp_write("A")}, "CMPB": {"on_write": self._cmp_write("B")},
            "CMPCTL": {"on_write": self._cmpctl_write, "ro_mask": SHDWAFULL | SHDWBFULL},
            "AQCTLA": cfg, "AQCTLB": cfg, "DBCTL": cfg, "DBRED": cfg, "DBFED": cfg, "TZCTL": cfg,
            "TZSEL": {"on_write": self._tzsel_write},
            "TZEINT": cfg, "ETSEL": cfg,
            "AQSFRC": {"on_write": self._aqsfrc_write}, "AQCSFRC": cfg,
            "TZFLG": {"ro_mask": 0xFFFF}, "TZCLR": {"on_write": self._tzclr_write, "on_read": lambda b, r: 0},
            "TZFRC": {"on_write": self._tzfrc_write, "on_read": lambda b, r: 0},
            "ETPS": {"on_read": self._etps_read, "on_write": self._cfg_write, "ro_mask": 0xCC0C},
            "ETFLG": {"ro_mask": 0xFFFF},
            "ETCLR": {"on_write": self._etclr_write, "on_read": lambda b, r: 0},
            "ETFRC": {"on_write": self._etfrc_write, "on_read": lambda b, r: 0},
            "PCCTL": {"on_write": self._pcctl_write},
        }
        self.hr = bool(params.get("hrpwm", False))
        if self.hr:
            ov.update({"CMPAHR": {"on_write": self._cmpahr_write, "ro_mask": 0x00FF},
                       "TBPHSHR": {"on_write": self._tbphshr_write, "ro_mask": 0x00FF},
                       "HRCNFG": {"on_write": self._hrcnfg_write, "ro_mask": 0xFFF0}})
        regs_ = build_regs((name, "EPWM1"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, regs_, params=params, log=log)
        self.gate = params.get("gate", f"EPWM{self.index}ENCLK")
        self.sync_listeners: list = []             # cb(t_cycles) on every EPWMxSYNCO pulse (eCAP1 on EPWM1)
        self.soc_listeners: list = []              # cb(index, "A" | "B") on every SOCA/SOCB pulse (DMA triggers)
        self.running = False
        self._cyc0 = 0              # anchor cycle (a TBCLK edge) ...
        self._t0 = 0                # ... and the absolute tick count there (position = tick % period)
        self._exc = None            # (ctr0, up) while the counter is above TBPRD (ticks _t0 .. _te-1)
        self._te = 0                # first tick of the normal cycle
        self._cm_from = 0           # TBSTS.CTRMAX: ticks before this one are already accounted for
        self.tick = 2               # core cycles per TBCLK
        self.prd = self.cmpa = self.cmpb = 0       # active values
        self._prd_pending = False
        self._ev = None
        self._sync_ev = None                       # pending TBPHS load (next TBCLK edge after a SYNCI)
        self._sync_at = 0
        self.aqa = self.aqb = False                # action-qualifier outputs
        self._red = self._fed = False              # dead-band delayed paths
        self._red_src = self._fed_src = False
        self._red_ev = self._fed_ev = None
        self._out = {"A": None, "B": None}         # pin signal: None (never driven), "H", "L", "Z" (released)
        self._dir_up = True                        # direction while frozen
        self._cnt = {"INT": 0, "SOCA": 0, "SOCB": 0}
        self._int_line = self._tz_line = None
        self._int_lvl = self._tz_lvl = False
        self._cbc_act = False                      # cycle-by-cycle trip action active (until CTR=ZERO)
        self._cbc_pin = False                      # a CBC-selected TZn pin is low now
        self._csf = 0                              # active AQCSFRC (the register holds the shadow)
        self._csf_pending = False
        self._chop_warned = self._tiny_warned = False
        self._pc_on = False                        # PCCTL.CHPEN
        self._pc_base = 0                          # carrier phase origin (cycle)
        self._chop_in = {"A": False, "B": False}
        self._osht_end = {"A": 0, "B": 0}
        self._chop_ev = None
        self._synco_ev = None
        self._synci_lvl = False
        self._synci_pins: list = []
        self._synco_on = bool(params.get("synco_pin", self.index == 1))    # ePWM1 drives the EPWMSYNCO pin
        self._tz_pins: list = []
        self._tz_ids: set = set()
        self._tz_unsub = None
        self.periods = 0
        self.mep_step_ps = float(params.get("mep_step_ps", 150.0))
        self._hr_init()

    def _hr_init(self) -> None:
        self._hr_mode = 0                          # HRCNFG.EDGMODE while the MEP is on (0: off, hot path untouched)
        self._hr_cmp = 0                           # active CMPAHR steps
        self._hr_pending = False                   # CMPAHR shadow waiting for its HRLOAD event
        self._hr_pos: int | None = None            # period position of the counter event being processed
        self._hra_src = self._hra = False          # AQ output A before / after the MEP
        self._hr_ev = None
        self._hr_warned: set[str] = set()
        self._hr_notes = 0
        self._hr_last: tuple | None = None
        self.hr_rise_ns: float | None = None       # exact times (fractional ns) of the last MEP-output edges
        self.hr_fall_ns: float | None = None
        self.hr_period_ns: float | None = None     # exact period / high time / duty of the last complete period
        self.hr_high_ns: float | None = None
        self.hr_duty: float | None = None

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._int_line = self.params.get("irq") or irq_line(chip, f"EPWM{self.index}_INT", f"3.{self.index}")
        self._tz_line = self.params.get("tz_irq") or irq_line(chip, f"EPWM{self.index}_TZINT", f"2.{self.index}")
        sc = sysctrl(chip)
        if sc is not None and hasattr(sc, "pclk_listeners"):
            sc.pclk_listeners.append(self._clocks_changed)
        nxt = self.params.get("sync_next", SYNC_NEXT.get(self.index, ()))
        if isinstance(nxt, str):
            nxt = [nxt]
        self._next = [b for b in (chip.peripherals.get(n) for n in (nxt or ())) if b is not None]
        self._adc = find_model(chip, "ti_c2000.adc")
        board = getattr(chip, "pins", None)
        if board is not None:
            if self.params.get("synci_pin", self.index == 1):
                self._synci_pins = list(board.signal_pins("EPWMSYNCI"))
                for p in self._synci_pins:
                    listen_input(chip, p, self._synci_changed)
            seen = []
            for s in TZ_SIGNALS:
                for p in board.signal_pins(s):
                    if p not in seen:
                        seen.append(p)
            self._tz_pins = seen
            self._tz_ids = {p.id for p in seen}

    def reset(self) -> None:
        if self.chip is not None:
            self._stop()
            self._cancel_db()
            for attr in ("_chop_ev", "_synco_ev"):
                ev = getattr(self, attr)
                if ev is not None:
                    self.chip.sched.cancel(ev)
                setattr(self, attr, None)
            if self.chip.pins is not None:
                for side in ("A", "B"):
                    if self._out[side] is not None:
                        self.chip.pins.release_signal(f"EPWM{self.index}{side}", "L")
                        if self._out[side] == "Z":
                            self._out[side] = None
                            self._repin(f"EPWM{self.index}{side}")     # driven (at L) again
                if self._synco_on:
                    self.chip.pins.release_signal("EPWMSYNCO")
            if self._int_lvl:
                self.irq(self._int_line, False)
            if self._tz_lvl:
                self.irq(self._tz_line, False)
        super().reset()
        self.prd = self.cmpa = self.cmpb = 0
        self._prd_pending = False
        self._exc = None
        self.aqa = self.aqb = self._red = self._fed = self._red_src = self._fed_src = False
        self._out = {"A": None, "B": None}
        self._dir_up = True
        self._cnt = {"INT": 0, "SOCA": 0, "SOCB": 0}
        self._int_lvl = self._tz_lvl = False
        self._cbc_act = self._cbc_pin = self._csf_pending = False
        self._csf = 0
        self._pc_on = False
        self._chop_in = {"A": False, "B": False}
        if self._hr_ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._hr_ev)
        self._hr_init()
        self._watch_tz()                            # TZSEL is 0 again: stop watching the trip pins

    # ------------------------------------------------------------ geometry
    def _mode(self) -> int:
        return self.get("TBCTL") & 3

    def _period(self) -> int:
        m = self._mode()
        return max(2 * self.prd, 1) if m == UPDOWN else self.prd + 1

    def _ctr_dir(self, p: int) -> tuple[int, bool]:
        """Counter and direction (True = up) at period position p."""
        m, prd = self._mode(), self.prd
        if m == DOWN:
            return prd - p, False
        if m == UPDOWN:
            return (p, p < prd) if p <= prd else (2 * prd - p, False)
        return p, True

    def _ctr_dir_t(self, t: int) -> tuple[int, bool]:
        """Counter and direction at absolute tick t (above TBPRD while t < _te)."""
        exc = self._exc
        if exc is not None and t < self._te:
            d = t - self._t0
            return (exc[0] + d, True) if exc[1] else (exc[0] - d, False)
        return self._ctr_dir(t % self._period())

    def _pos_of(self, ctr: int, up: bool) -> int:
        m, prd = self._mode(), self.prd
        ctr = min(ctr, prd)
        if m == DOWN:
            return prd - ctr
        if m == UPDOWN:
            return ctr if (up or ctr == 0) and ctr < prd else (2 * prd - ctr) % max(2 * prd, 1)
        return ctr

    def _place(self, ctr: int, up: bool) -> tuple[int, Any, int]:
        """(t0, excursion, te) for a counter (ctr, up) at the anchor: above TBPRD the counter first runs to the wrap
        (up: 0xFFFF -> 0) or down to TBPRD, then enters the normal cycle at tick te (te % period = entry position)."""
        m, prd = self._mode(), self.prd
        if ctr <= prd:
            p = self._pos_of(ctr, up)
            return p, None, p
        if m == UP or (m == UPDOWN and up):
            n, entry, d = 0x10000 - ctr, 0, True
        else:
            n, entry, d = ctr - prd, (0 if m == DOWN else prd), False
        return entry - n, (ctr, d), entry

    def _tbclk_div(self) -> int:
        tb = self.get("TBCTL")
        hsp = (tb >> 7) & 7
        return (2 * hsp if hsp else 1) * (1 << ((tb >> 10) & 7))

    def _clock_on(self) -> bool:
        sc = self._sysctrl
        if sc is None:
            return True
        return sc.clock_enabled("TBCLKSYNC") and sc.clock_enabled(self.gate)

    # ------------------------------------------------------------ time
    def _now_tick(self) -> tuple[int, int]:
        """(absolute tick, cycles into it) now."""
        c = self.chip.now_cycles() - self._cyc0
        return self._t0 + c // self.tick, c % self.tick

    def _state_now(self) -> tuple[int, bool]:
        if not self.running:
            return self.get("TBCTR"), self._dir_up
        t, _ = self._now_tick()
        return self._ctr_dir_t(t)

    def _set_anchor(self, cyc: int, ctr: int, up: bool, latch: bool = True) -> None:
        if latch:
            self._latch_ctrmax()                        # account for a 0xFFFF reached under the old anchor
        self._cyc0 = cyc
        self._t0, self._exc, self._te = self._place(ctr, up)
        self._cm_from = self._t0

    def _anchor(self, ctr: int, up: bool) -> None:
        """Counter is (ctr, up) from now on; it steps at the next edge of the running TBCLK (the phase is kept)."""
        now = self.chip.now_cycles()
        self._set_anchor(now - (now - self._cyc0) % self.tick, ctr, up)
        self._reschedule(first=False)

    def _max_tick(self) -> int | None:
        """First tick >= _cm_from at which TBCTR = 0xFFFF, or None."""
        s = self._cm_from
        exc = self._exc
        if exc is not None and s < self._te:
            c0, up = exc
            if up:
                t = self._t0 + 0xFFFF - c0
                if t >= s:
                    return t
            elif c0 == 0xFFFF and s <= self._t0:
                return self._t0
            s = self._te
        if self.prd != 0xFFFF:
            return None
        m = self._mode()
        target = 0 if m == DOWN else 0xFFFF
        return s + (target - s) % self._period()

    def _latch_ctrmax(self) -> None:
        if not self.running or self.chip is None:
            return
        t, _ = self._now_tick()
        mt = self._max_tick()
        if mt is not None and mt <= t:
            self.set("TBSTS", self.get("TBSTS") | CTRMAX)
            self._cm_from = t + 1

    def _stop(self) -> None:
        if self.running:
            self._latch_ctrmax()
            ctr, up = self._state_now()
            self.set("TBCTR", ctr)
            self._dir_up = up
        for attr in ("_ev", "_sync_ev"):
            ev = getattr(self, attr)
            if ev is not None and self.chip is not None:
                self.chip.sched.cancel(ev)
            setattr(self, attr, None)
        self._exc = None
        self.running = False

    def _update_run(self, first: bool = True) -> None:
        """Start or stop the time base from CTRMODE / TBCLKSYNC / EPWMxENCLK.  `first`: an event at the starting
        count fires now (a fresh start); False when the count was only re-anchored (TBCTL change while running)."""
        want = self._mode() != FREEZE and self._clock_on() and self.chip is not None
        if want == self.running:
            return
        if not want:
            self._stop()
            return
        self.tick = self._tbclk_div()
        self.running = True
        self._set_anchor(self.chip.now_cycles(), self.get("TBCTR"), self._dir_up, latch=False)
        self._reschedule(first=first)

    def _clocks_changed(self) -> None:
        self._update_run()

    # ------------------------------------------------------------ event scheduling
    def _relevant(self) -> set[str]:
        """Event kinds that do something now (only these are scheduled)."""
        rel: set[str] = set()
        aq = self.get("AQCTLA") | self.get("AQCTLB")
        for k, sh in AQ_SHIFT.items():
            if (aq >> sh) & 3:
                rel.add(k)
        et = self.get("ETSEL")
        for en, sh in ((1 << 3, 0), (1 << 11, 8), (1 << 15, 12)):
            if et & en:
                code = (et >> sh) & 7
                rel.update(k for k, c in ET_CODE.items() if c == code)
        so = (self.get("TBCTL") >> 4) & 3
        if so == 1:
            rel.add("ZRO")
        elif so == 2:
            rel.update(("CBU", "CBD"))
        cc = self.get("CMPCTL")
        if self._prd_pending or self._cbc_act:
            rel.add("ZRO")
        if self._hr_pending:
            rel.add("PRD" if self.get("HRCNFG") & HRLOAD else "ZRO")
        if self._csf_pending:
            rl = (self.get("AQSFRC") >> 6) & 3
            if rl in (0, 2):
                rel.add("ZRO")
            if rl in (1, 2):
                rel.add("PRD")
        for full, mode_sh in ((SHDWAFULL, 0), (SHDWBFULL, 2)):
            if cc & full:
                lm = (cc >> mode_sh) & 3
                if lm in (0, 2):
                    rel.add("ZRO")
                if lm in (1, 2):
                    rel.add("PRD")
        if self._mode() == UP:
            rel -= {"CAD", "CBD"}
        elif self._mode() == DOWN:
            rel -= {"CAU", "CBU"}
        return rel

    def _positions(self, rel: set[str]) -> list[int]:
        m, prd = self._mode(), self.prd
        out = set()
        if "ZRO" in rel:
            out.add(prd if m == DOWN else 0)
        if "PRD" in rel:
            out.add(0 if m == DOWN else prd)
        for v, u, d in ((self.cmpa, "CAU", "CAD"), (self.cmpb, "CBU", "CBD")):
            if m == UP:
                if u in rel and v <= prd:
                    out.add(v)
            elif m == DOWN:
                if d in rel:
                    out.add(prd - min(v, prd))
            elif v >= prd:                                   # up-down, CMPx >= TBPRD: CxU and CxD at the peak
                if u in rel or d in rel:
                    out.add(prd)
            else:
                if u in rel:
                    out.add(v)                               # counting up, 0 included
                if d in rel and v > 0:
                    out.add(2 * prd - v)                     # counting down, TBPRD-1 .. 1
        if out and self.prd == 0 and not self._tiny_warned:
            self._tiny_warned = True
            self.emit("warn", block=self.name, msg=f"{self.name}: TBPRD = 0 with actions/events enabled: an event "
                      "every TBCLK (slow to simulate)")
        return sorted(out)

    def _reschedule(self, first: bool) -> None:
        if self._ev is not None:
            self.chip.sched.cancel(self._ev)
            self._ev = None
        if not self.running:
            return
        t_now, _frac = self._now_tick()
        self._schedule_from(t_now if first else t_now + 1)

    def _reschedule_from(self, t: int) -> None:
        """Schedule the next relevant event strictly after absolute tick t."""
        self._schedule_from(t + 1)

    def _schedule_from(self, base: int) -> None:
        """Schedule the first relevant event at absolute tick >= base."""
        if self._exc is not None and base < self._te:
            base = self._te                              # above TBPRD: nothing until the counter is back in range
        L = self._period()
        pos = self._positions(self._relevant())
        if not pos:
            return
        cur = base % L
        best = min((p - cur) % L for p in pos)
        t = base + best
        self._ev = self.chip.sched.at_cycles(self._cyc0 + (t - self._t0) * self.tick, self._event, t)

    def _event(self, t: int) -> None:
        self._ev = None
        if self._sync_ev is not None and self._sync_at <= self.chip.now_cycles():
            return                                       # a TBPHS load replaces this count (it reschedules)
        if self._exc is not None:
            self._latch_ctrmax()
            self._exc = None                             # back in the normal cycle (t >= _te)
        L = self._period()
        p = t % L
        ctr, up = self._ctr_dir(p)
        m = self._mode()
        kinds = []
        if ctr == 0:
            kinds.append("ZRO")
        if ctr == self.prd:
            kinds.append("PRD")
        for v, u, d in ((self.cmpa, "CAU", "CAD"), (self.cmpb, "CBU", "CBD")):
            if m == UP:
                if ctr == v:
                    kinds.append(u)
            elif m == DOWN:
                if ctr == min(v, self.prd):
                    kinds.append(d)
            elif v >= self.prd:
                if p == self.prd:
                    kinds += (u, d)
            elif ctr == v:
                kinds.append(u if p < self.prd else d)
        self._process(kinds, m, p)
        if self.running and self._ev is None:
            t_cur, _ = self._now_tick()                 # a TBPRD load or a sync may have re-anchored the count
            self._reschedule_from(t_cur)

    def _process(self, kinds: list[str], m: int, p: int) -> None:
        present = set(kinds)
        if m == UPDOWN:
            order = ORDER["updown_up"] if p < self.prd else ORDER["updown_down"]
        else:
            order = ORDER[m]
        qa, qb = self.get("AQCTLA"), self.get("AQCTLB")
        for k in order:
            if k in present:
                self.aqa = _aq(self.aqa, (qa >> AQ_SHIFT[k]) & 3)
                self.aqb = _aq(self.aqb, (qb >> AQ_SHIFT[k]) & 3)
        if "ZRO" in present:
            self.periods += 1
            self._cbc_act = self._cbc_pin                  # the CBC action ends at CTR=ZERO unless a pin still trips
        if self._csf_pending:
            rl = (self.get("AQSFRC") >> 6) & 3
            if (rl in (0, 2) and "ZRO" in present) or (rl in (1, 2) and "PRD" in present):
                self._csf_pending = False
                self._csf = self.get("AQCSFRC") & 0xF
        self._et(present)
        so = (self.get("TBCTL") >> 4) & 3
        if (so == 1 and "ZRO" in present) or (so == 2 and present & {"CBU", "CBD"}):
            self._sync_out()
        self._shadow_loads(present)
        self._hr_pos = p
        self._outputs()
        self._hr_pos = None

    def _shadow_loads(self, present: set[str]) -> None:
        cc = self.get("CMPCTL")
        new = cc
        for side, full, sh in (("A", SHDWAFULL, 0), ("B", SHDWBFULL, 2)):
            if cc & full:
                lm = (cc >> sh) & 3
                if (lm in (0, 2) and "ZRO" in present) or (lm in (1, 2) and "PRD" in present):
                    if side == "A":
                        self.cmpa = self.get("CMPA")
                    else:
                        self.cmpb = self.get("CMPB")
                    new &= ~full
        if new != cc:
            self.set("CMPCTL", new)
        if self._hr_pending and ("PRD" if self.get("HRCNFG") & HRLOAD else "ZRO") in present:
            self._hr_pending = False
            self._hr_set_cmp(self.get("CMPAHR") >> 8)
        if self._prd_pending and "ZRO" in present:
            self._prd_pending = False
            self.prd = self.get("TBPRD")
            # the counter is at zero now: re-anchor in the new period
            t, _ = self._now_tick()
            self._set_anchor(self._cyc0 + (t - self._t0) * self.tick, 0, True)

    # ------------------------------------------------------------ event trigger / interrupts
    def _et(self, present: set[str]) -> None:
        et = self.get("ETSEL")
        ps = self.get("ETPS")
        codes = {ET_CODE[k] for k in present if k in ET_CODE}
        for name, en, sh, prd_sh, flag in (("INT", 1 << 3, 0, 0, ET_INT), ("SOCA", 1 << 11, 8, 8, ET_SOCA),
                                           ("SOCB", 1 << 15, 12, 12, ET_SOCB)):
            if not et & en or ((et >> sh) & 7) not in codes:
                continue
            prd = (ps >> prd_sh) & 3
            if not prd:
                continue
            self._cnt[name] = min(self._cnt[name] + 1, prd)        # stops at xxPRD (flag pending)
            if self._cnt[name] >= prd:
                if name == "INT":
                    if not self.get("ETFLG") & ET_INT:
                        self._cnt[name] = 0
                        self._set_et(flag)
                else:
                    self._cnt[name] = 0
                    self._set_et(flag)

    def _set_et(self, flag: int) -> None:
        self.set("ETFLG", self.get("ETFLG") | flag)
        if flag == ET_INT:
            self._int_set(True)
        else:
            which = "A" if flag == ET_SOCA else "B"
            if self._adc is not None and hasattr(self._adc, "epwm_soc"):
                self._adc.epwm_soc(which)
            for cb in list(self.soc_listeners):
                cb(self.index, which)

    def _int_set(self, lvl: bool) -> None:
        if lvl != self._int_lvl:
            self._int_lvl = lvl
            if self._int_line is not None:
                self.irq(self._int_line, lvl)

    def _tz_set(self, lvl: bool) -> None:
        if lvl != self._tz_lvl:
            self._tz_lvl = lvl
            if self._tz_line is not None:
                self.irq(self._tz_line, lvl)

    # ------------------------------------------------------------ synchronization
    def _sync_out(self) -> None:
        """EPWMxSYNCO pulse: the downstream ePWMs, `sync_listeners` (eCAP1), and for ePWM1 the EPWMSYNCO pin."""
        t = self.chip.now_cycles()
        for m in self._next:
            m.sync_in()
        for cb in list(self.sync_listeners):
            cb(t)
        if self._synco_on:
            self._synco_pin(t)

    def _synco_pin(self, t: int) -> None:
        pins = self.chip.pins
        if pins is None or not any(p.direction == "out" for p in muxed_pins(pins, "EPWMSYNCO")):
            return
        pins.drive_signal("EPWMSYNCO", "H")
        if self._synco_ev is not None:
            self.chip.sched.cancel(self._synco_ev)
        self._synco_ev = self.chip.sched.at_cycles(t + SYNCO_PIN_CYCLES, self._synco_end)

    def _synco_end(self) -> None:
        self._synco_ev = None
        self.chip.pins.release_signal("EPWMSYNCO")

    def sync_in(self) -> None:
        """EPWMxSYNCI (or SWFSYNC): latch TBSTS.SYNCI; with PHSEN load TBCTR from TBPHS at the next TBCLK edge at
        least 2 SYSCLKOUT later; pass the pulse on at once when SYNCOSEL = 00."""
        tb = self.get("TBCTL")
        self.set("TBSTS", self.get("TBSTS") | SYNCI_FLAG)
        if tb & PHSEN:
            if self.running:
                now = self.chip.now_cycles()
                tick = self.tick
                e = now + tick - (now - self._cyc0) % tick         # next TBCLK edge strictly after now
                while e < now + SYNC_DELAY:
                    e += tick
                if self._sync_ev is not None:
                    self.chip.sched.cancel(self._sync_ev)
                self._sync_at = e
                self._sync_ev = self.chip.sched.at_cycles(e, self._sync_load)
            else:
                self.set("TBCTR", self.get("TBPHS"))
                self._dir_up = self._phase_dir(tb)
        if (tb >> 4) & 3 == 0:
            self._sync_out()

    def _phase_dir(self, tb: int) -> bool:
        m = tb & 3
        return bool(tb & PHSDIR) if m == UPDOWN else m != DOWN

    def _sync_load(self) -> None:
        self._sync_ev = None
        if not self.running:
            return
        if self._ev is not None:                                 # events of the old count from here on are void
            self.chip.sched.cancel(self._ev)
            self._ev = None
        self._set_anchor(self.chip.now_cycles(), self.get("TBPHS"), self._phase_dir(self.get("TBCTL")))
        self._reschedule(first=True)                             # events at the loaded count fire at this edge

    def _synci_changed(self, pin: Any = None) -> None:
        own = muxed_pins(self.chip.pins, "EPWMSYNCI")
        lvl = len(own) == 1 and own[0].level() == "H"           # default 0 (SPRUI07 Table 1-46)
        rise = lvl and not self._synci_lvl
        self._synci_lvl = lvl
        if rise:
            self.sync_in()

    # ------------------------------------------------------------ trip-zone pins
    def _watch_tz(self) -> None:
        """Watch the TZ1-6 pins (levels and mux changes) only while TZSEL selects one."""
        board = getattr(self.chip, "pins", None) if self.chip is not None else None
        if board is None:
            return
        want = bool(self.get("TZSEL") & 0x3F3F) and bool(self._tz_pins)
        if want and self._tz_unsub is None:
            for p in self._tz_pins:
                listen_input(self.chip, p, self._tz_changed)
            self._tz_unsub = board.subscribe(self._tz_pin_event)
        elif not want and self._tz_unsub is not None:
            for p in self._tz_pins:
                unlisten_input(self.chip, p, self._tz_changed)
            self._tz_unsub()
            self._tz_unsub = None
            self._cbc_pin = False

    def _tz_pin_event(self, ev: Any) -> None:
        if ev.field == "owner" and ev.pin in self._tz_ids:
            self._tz_changed()

    def tz_low(self) -> int:
        """Bit n-1 set when TZn is low (active): the one pin muxed to TZn reads L."""
        board = self.chip.pins
        low = 0
        for n, s in enumerate(TZ_SIGNALS):
            own = muxed_pins(board, s)
            if len(own) == 1 and own[0].level() == "L":
                low |= 1 << n
        return low

    def _tz_changed(self, pin: Any = None) -> None:
        sel = self.get("TZSEL")
        if not sel & 0x3F3F or self.chip is None or self.chip.pins is None:
            return
        low = self.tz_low()
        cbc = bool(low & sel & 0x3F)
        ost = bool(low & (sel >> 8) & 0x3F)
        self._cbc_pin = cbc
        trips = (TZ_CBC if cbc else 0) | (TZ_OST if ost else 0)
        if trips:
            self._trip(trips)

    def _trip(self, trips: int) -> None:
        """A trip event (pin or TZFRC): latch the flags, start the action, raise EPWMx_TZINT if enabled."""
        flg = self.get("TZFLG")
        act_new = bool(trips & TZ_CBC) and not self._cbc_act
        if trips & TZ_CBC:
            self._cbc_act = True
        new = trips & ~flg
        flg |= trips
        if self.get("TZEINT") & trips and not flg & TZ_INT:
            flg |= TZ_INT
            self._tz_set(True)
        self.set("TZFLG", flg)
        if new or act_new:
            self._outputs()
            if self.running:
                self._reschedule(first=False)

    def _tzsel_write(self, blk, reg, old, new):
        self._store(reg, new)
        self._watch_tz()
        self._tz_changed()
        return None

    # ------------------------------------------------------------ outputs: AQ -> DB -> PC -> TZ -> pins
    def _cancel_db(self) -> None:
        for attr in ("_red_ev", "_fed_ev"):
            ev = getattr(self, attr)
            if ev is not None and self.chip is not None:
                self.chip.sched.cancel(ev)
            setattr(self, attr, None)

    def _outputs(self) -> None:
        if self.chip is None:
            return
        a, b = self.aqa, self.aqb
        cs = self._csf
        a = {1: False, 2: True}.get(cs & 3, a)
        b = {1: False, 2: True}.get((cs >> 2) & 3, b)
        if self._hr_mode:
            a = self._mep(a)
        db = self.get("DBCTL")
        inm = (db >> 4) & 3
        red_src = a if inm in (0, 2) else b
        fed_src = a if inm in (0, 1) else b
        now = self.chip.now_cycles()
        if red_src != self._red_src:
            self._red_src = red_src
            if self._red_ev is not None:
                self.chip.sched.cancel(self._red_ev)
                self._red_ev = None
            n = (self.get("DBRED") & 0x3FF) * self.tick
            if red_src and n:
                self._red_ev = self.chip.sched.at_cycles(now + n, self._db_fire, "red")
            else:
                self._red = red_src
        if fed_src != self._fed_src:
            self._fed_src = fed_src
            if self._fed_ev is not None:
                self.chip.sched.cancel(self._fed_ev)
                self._fed_ev = None
            n = (self.get("DBFED") & 0x3FF) * self.tick
            if not fed_src and n:
                self._fed_ev = self.chip.sched.at_cycles(now + n, self._db_fire, "fed")
            else:
                self._fed = fed_src
        self._final(a, b)

    def _db_fire(self, which: str) -> None:
        if which == "red":
            self._red_ev = None
            self._red = self._red_src
        else:
            self._fed_ev = None
            self._fed = self._fed_src
        self._final(None, None)

    def _final(self, a: bool | None, b: bool | None) -> None:
        if a is None:
            cs = self._csf
            a = {1: False, 2: True}.get(cs & 3, self.aqa)
            b = {1: False, 2: True}.get((cs >> 2) & 3, self.aqb)
            if self._hr_mode:
                a = self._hra
        db = self.get("DBCTL")
        om, pol = db & 3, (db >> 2) & 3
        oa: Any = (self._red ^ bool(pol & 1)) if om & 2 else a
        ob: Any = (self._fed ^ bool(pol & 2)) if om & 1 else b
        if self._pc_on:
            oa, ob = self._chop(oa, ob)
        if self._cbc_act or self.get("TZFLG") & TZ_OST:
            ctl = self.get("TZCTL")
            oa = {0: None, 1: True, 2: False}.get(ctl & 3, oa)
            ob = {0: None, 1: True, 2: False}.get((ctl >> 2) & 3, ob)
        pins = self.chip.pins
        if pins is None:
            return
        for side, lvl in (("A", oa), ("B", ob)):
            v = "Z" if lvl is None else ("H" if lvl else "L")
            old = self._out[side]
            if old != v:
                self._out[side] = v
                sig = f"EPWM{self.index}{side}"
                if v == "Z":
                    self._repin(sig)                       # the GPIO pin becomes an input first (no idle blip)
                    pins.release_signal(sig)               # high impedance: no longer driven
                else:
                    pins.drive_signal(sig, v)
                    if old == "Z":
                        self._repin(sig)

    def signal_driven(self, side: str) -> bool:
        """Does the module drive EPWMx`side` now?  False only while tripped to high impedance (TZCTL = 00): the
        GPIO model then shows a pin muxed to the signal as an input."""
        return self._out.get(side) != "Z"

    def _repin(self, sig: str) -> None:
        gp = find_model(self.chip, "ti_c2000.gpio")
        if gp is not None and hasattr(gp, "resync_signal"):
            gp.resync_signal(sig)

    # ------------------------------------------------------------ HRPWM (micro edge positioner on output A)
    def _hr_steps(self) -> int:
        return self.get("TBPHSHR") >> 8 if self.get("HRCNFG") & CTLMODE else self._hr_cmp

    def hr_offset_ns(self) -> float:
        """MEP delay applied to the selected edges of output A now (0 when the MEP is off)."""
        return self._hr_steps() * self.mep_step_ps / 1000.0 if self._hr_mode else 0.0

    def hr_state(self) -> dict:
        """Exact HRPWM state: configuration, MEP steps and delay, and the exact edge times / duty of output A after
        the MEP (fractional ns; the pins show edges at core-cycle resolution)."""
        cfg = self.get("HRCNFG") if self.hr else 0
        return {"hrpwm": self.hr, "edgmode": cfg & EDGMODE, "ctlmode": (cfg >> 2) & 1, "hrload": (cfg >> 3) & 1,
                "steps": self._hr_steps(), "step_ps": self.mep_step_ps, "offset_ns": self.hr_offset_ns(),
                "cmpahr_pending": self._hr_pending, "rise_ns": self.hr_rise_ns, "fall_ns": self.hr_fall_ns,
                "period_ns": self.hr_period_ns, "high_ns": self.hr_high_ns, "duty": self.hr_duty}

    def _hr_warn(self, key: str, msg: str) -> None:
        if key not in self._hr_warned:
            self._hr_warned.add(key)
            self.emit("warn", block=self.name, msg=f"{self.name}: {msg}")

    def _hr_note(self, why: str) -> None:
        """One `info` event per HRPWM configuration / active step change (step changes: the first HR_NOTES)."""
        if self.chip is None:
            return
        cur = (self._hr_mode, self.get("HRCNFG") & CTLMODE, self._hr_steps())
        if cur == self._hr_last:
            return
        steps_only = self._hr_last is not None and cur[:2] == self._hr_last[:2]
        self._hr_last = cur
        if steps_only:
            self._hr_notes += 1
            if self._hr_notes > HR_NOTES:
                if self._hr_notes == HR_NOTES + 1:
                    self.emit("info", block=self.name, msg=f"{self.name}: further MEP step changes not logged "
                              "(see hr_state())")
                return
        mode = self._hr_mode
        if not mode:
            self.emit("info", block=self.name, msg=f"EPWM{self.index}A HRPWM off ({why})")
            return
        edge = {1: "rising", 2: "falling", 3: "both"}[mode]
        src = "TBPHSHR" if cur[1] else "CMPAHR"
        off = self.hr_offset_ns()
        self.emit("info", block=self.name, hr_steps=cur[2], hr_offset_ns=off,
                  msg=f"EPWM{self.index}A HRPWM {edge} edge(s) +{off:.3f} ns ({cur[2]} MEP steps x "
                      f"{self.mep_step_ps:g} ps from {src}; pin edges at core-cycle resolution) ({why})")
        per_ns = 1e9 / self.chip.clock.hz
        if off > per_ns + 2 * self.mep_step_ps / 1000.0:
            self._hr_warn("range", f"MEP delay {off:.3f} ns exceeds one SYSCLKOUT period ({per_ns:.3f} ns): the "
                          "MEP covers one coarse step (SFO scale factor); applied as is")

    def _hr_set_cmp(self, steps: int) -> None:
        if steps != self._hr_cmp:
            self._hr_cmp = steps
            if self._hr_mode and not self.get("HRCNFG") & CTLMODE:
                self._hr_note("CMPAHR")

    def _mep(self, a: bool) -> bool:
        """Output A through the MEP: a change of the AQ output of the selected kind, made by a counter event, reaches
        the dead band (and the pin) steps x step later (rounded to the nearest core cycle)."""
        if a == self._hra_src:
            return self._hra
        self._hra_src = a
        sched = self.chip.sched
        if self._hr_ev is not None:
            sched.cancel(self._hr_ev)                     # a pending delayed edge is overtaken (pulse < delay)
            self._hr_ev = None
        now_ns = self.chip.now_ns()
        steps = self._hr_steps() if self._hr_mode & (1 if a else 2) else 0
        pos = self._hr_pos
        if steps and pos is not None and not self.get("HRCNFG") & CTLMODE and pos * self.tick < MEP_INACTIVE:
            self._hr_warn("inactive", f"HRPWM edge in the first {MEP_INACTIVE} SYSCLKOUT of the period: the MEP is "
                          "inactive there (SPRUI07 4.2.3.3), edge not refined")
            steps = 0
        if not steps or pos is None:
            self._hr_edge(a, now_ns)
            self._hra = a
            return a
        d_ns = steps * self.mep_step_ps / 1000.0
        self._hr_edge(a, now_ns + d_ns)
        n = int(d_ns * self.chip.clock.hz / 1e9 + 0.5)
        if n <= 0:
            self._hra = a
            return a
        self._hr_ev = sched.at_cycles(self.chip.now_cycles() + n, self._mep_fire, a)
        return self._hra

    def _mep_fire(self, a: bool) -> None:
        self._hr_ev = None
        self._hra = a
        self._outputs()

    def _hr_edge(self, a: bool, t: float) -> None:
        if a:
            r0, f = self.hr_rise_ns, self.hr_fall_ns
            if r0 is not None and f is not None and r0 <= f <= t and t > r0:
                self.hr_period_ns = t - r0
                self.hr_high_ns = f - r0
                self.hr_duty = self.hr_high_ns / self.hr_period_ns
            self.hr_rise_ns = t
        else:
            self.hr_fall_ns = t

    def _hrcnfg_write(self, blk, reg, old, new):
        mode = new & EDGMODE
        if mode and not self._hr_mode and self.chip is not None:
            cs = self._csf                                # start from the current AQ output: no glitch
            self._hra_src = self._hra = {1: False, 2: True}.get(cs & 3, self.aqa)
        if not mode and self._hr_ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._hr_ev)
            self._hr_ev = None
            self._hra = self._hra_src
        self._hr_mode = mode
        if (old ^ new) & 0xF:
            self._hr_note("HRCNFG")
        if self.chip is not None:
            self._outputs()
            if self.running:
                self._reschedule(first=False)
        return None

    def _cmpahr_write(self, blk, reg, old, new):
        if self.get("CMPCTL") & SHDWAMODE or not self.running:
            self._hr_pending = False
            self._hr_set_cmp(new >> 8)
        else:
            self._hr_pending = True
            self._reschedule(first=False)
        return None

    def _tbphshr_write(self, blk, reg, old, new):
        if self._hr_mode and self.get("HRCNFG") & CTLMODE:
            self._hr_note("TBPHSHR")
        return None

    # ------------------------------------------------------------ PWM chopper
    def _pc_params(self) -> tuple[int, int, int]:
        pc = self.get("PCCTL")
        f = ((pc >> 5) & 7) + 1
        return 8 * f, min((pc >> 8) & 7, 6) * f + f, 8 * (((pc >> 1) & 0xF) + 1)    # period, high, one-shot

    def _chop(self, oa: bool, ob: bool) -> tuple[bool, bool]:
        now = self.chip.now_cycles()
        per, hi, w = self._pc_params()
        m = (now - self._pc_base) % per
        carrier = m < hi
        out = []
        nxt = None
        for side, lvl in (("A", oa), ("B", ob)):
            if lvl and not self._chop_in[side]:
                self._osht_end[side] = now + w                     # first (one-shot) pulse from the rising edge
            self._chop_in[side] = lvl
            if not lvl:
                out.append(False)
                continue
            oe = self._osht_end[side]
            out.append(now < oe or carrier)
            if now < oe:                                           # next change of (one-shot OR carrier)
                mo = (oe - self._pc_base) % per
                t = oe - mo + hi if mo < hi else oe
            else:
                t = now - m + (hi if m < hi else per)
            nxt = t if nxt is None else min(nxt, t)
        if self._chop_ev is not None:
            self.chip.sched.cancel(self._chop_ev)
            self._chop_ev = None
        if nxt is not None:
            self._chop_ev = self.chip.sched.at_cycles(nxt, self._chop_fire)
        return out[0], out[1]

    def _chop_fire(self) -> None:
        self._chop_ev = None
        self._final(None, None)

    def _pcctl_write(self, blk, reg, old, new):
        self._store(reg, new)
        on = bool(new & CHPEN)
        if on and not self._pc_on:
            self._pc_base = self.chip.now_cycles() if self.chip is not None else 0
            self._chop_in = {"A": False, "B": False}
            if not self._chop_warned:
                self._chop_warned = True
                self.emit("warn", block=self.name, msg=f"{self.name}: PWM chopper (PCCTL.CHPEN) on: every carrier "
                          "edge is simulated (SYSCLKOUT/8 carrier, slow)")
        self._pc_on = on
        if not on and self._chop_ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._chop_ev)
            self._chop_ev = None
        if self.chip is not None:
            self._final(None, None)
        return None

    # ------------------------------------------------------------ register hooks
    def _tbctl_write(self, blk, reg, old, new):
        was = self.running
        changed = was and bool((old ^ new) & 0x1F83)      # mode / clock divider changed while running
        if changed:
            self._store(reg, old)                         # (the bus stored `new` already)
            self._stop()                                  # freeze TBCTR / CTRDIR under the old settings first
        self._store(reg, new)
        self._update_run(first=not changed)               # restart (or start) with the new settings
        if new & SWFSYNC:
            self.sync_in()
        if self.running and was and not changed:          # a fresh start was just scheduled by _update_run
            self._reschedule(first=False)
        return new & ~SWFSYNC

    def _tbsts_read(self, blk, reg):
        self._latch_ctrmax()
        v = self.get("TBSTS") & ~1
        _ctr, up = self._state_now()
        return v | int(up)

    def _tbsts_write(self, blk, reg, old, new):
        self._store(reg, old)
        self._latch_ctrmax()
        v = self.get("TBSTS") & ~(new & (CTRMAX | SYNCI_FLAG)) & 0xFFFF   # SYNCI / CTRMAX: write 1 to clear
        if new & CTRMAX and self.running:
            self._cm_from = self._now_tick()[0] + 1
        return v

    def _tbctr_read(self, blk, reg):
        return self._state_now()[0] if self.running else None

    def _tbctr_write(self, blk, reg, old, new):
        if self.running:
            _c, up = self._state_now()
            self._anchor(new & 0xFFFF, up)
        return None

    def _tbprd_write(self, blk, reg, old, new):
        if self.get("TBCTL") & PRDLD or not self.running:
            ctr, up = self._state_now()
            self.prd = new & 0xFFFF
            if self.running:
                self._anchor(ctr, up)                     # may leave the counter above the new TBPRD
        else:
            self._prd_pending = True
            self._reschedule(first=False)
        return None

    def _cmp_write(self, side: str):
        def wr(blk, reg, old, new):
            cc = self.get("CMPCTL")
            shadow_off = cc & (SHDWAMODE if side == "A" else SHDWBMODE)
            if shadow_off or not self.running:
                if side == "A":
                    self.cmpa = new & 0xFFFF
                else:
                    self.cmpb = new & 0xFFFF
            else:
                self.set("CMPCTL", cc | (SHDWAFULL if side == "A" else SHDWBFULL))
            if self.running:
                self._reschedule(first=False)
            return None
        return wr

    def _cmpctl_write(self, blk, reg, old, new):
        self._store(reg, new)
        if self.running:
            self._reschedule(first=False)
        return None

    def _cfg_write(self, blk, reg, old, new):
        self._store(reg, new)
        if reg.name == "AQCSFRC":
            if (self.get("AQSFRC") >> 6) & 3 == 3:          # RLDCSF = 11: the CPU writes the active register
                self._csf, self._csf_pending = new & 0xF, False
            else:
                self._csf_pending = True                   # loaded at CTR=ZERO / PRD per RLDCSF
        if reg.name in ("AQCSFRC", "DBCTL", "TZCTL"):
            self._outputs()
        if self.running:
            self._reschedule(first=False)
        return None

    def _aqsfrc_write(self, blk, reg, old, new):
        if new & (1 << 2):                                  # OTSFA: one-time force on A
            self.aqa = _aq(self.aqa, new & 3)
        if new & (1 << 5):                                  # OTSFB
            self.aqb = _aq(self.aqb, (new >> 3) & 3)
        if (new >> 6) & 3 == 3 and self._csf_pending:       # switched to immediate: the shadow value applies now
            self._csf, self._csf_pending = self.get("AQCSFRC") & 0xF, False
        self._store(reg, new & ~((1 << 2) | (1 << 5)))
        self._outputs()
        if self.running:
            self._reschedule(first=False)
        return None

    def _tzfrc_write(self, blk, reg, old, new):
        trips = new & (TZ_CBC | TZ_OST)
        if trips:
            self._trip(trips)
        return 0

    def _tzclr_write(self, blk, reg, old, new):
        """Clears TZFLG bits.  TZCLR.CBC clears only the flag: the CBC action is held until the next CTR=ZERO.
        TZCLR.INT with an enabled OST/CBC flag still set generates another EPWMx_TZINT pulse (SPRUG04 TZCLR).  A trip
        pin that is still low sets its flag again at once."""
        flg = self.get("TZFLG") & ~(new & 7)
        if not flg & TZ_INT:
            self._tz_set(False)
            if new & TZ_INT and flg & self.get("TZEINT") & (TZ_CBC | TZ_OST):
                flg |= TZ_INT
                self._tz_set(True)
        self.set("TZFLG", flg)
        self._tz_changed()
        self._outputs()
        return 0

    def _etps_read(self, blk, reg):
        v = self.get("ETPS") & ~0xCC0C
        return v | (self._cnt["INT"] << 2) | (self._cnt["SOCA"] << 10) | (self._cnt["SOCB"] << 14)

    def _etclr_write(self, blk, reg, old, new):
        flg = self.get("ETFLG") & ~(new & (ET_INT | ET_SOCA | ET_SOCB))
        self.set("ETFLG", flg)
        if new & ET_INT:
            self._int_set(False)
            prd = self.get("ETPS") & 3
            if prd and self._cnt["INT"] >= prd:              # events counted while the flag was set
                self._cnt["INT"] = 0
                self._set_et(ET_INT)
        return 0

    def _etfrc_write(self, blk, reg, old, new):
        for flag in (ET_INT, ET_SOCA, ET_SOCB):
            if new & flag:
                self._set_et(flag)
        return 0
