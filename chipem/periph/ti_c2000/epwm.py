"""C2833x enhanced PWM module ePWM1-6 (TMS320x2833x/2823x ePWM reference guide, SPRUG04A).

Time base (TB): TBCLK = SYSCLKOUT / (HSPCLKDIV x CLKDIV) (HSPCLKDIV 0 -> 1, n -> 2n; CLKDIV n -> 2^n), counted in
exact core cycles. It runs while TBCTL.CTRMODE != 3 (freeze), PCLKCR0.TBCLKSYNC and PCLKCR1.EPWMxENCLK are set.
* up-count: 0 .. TBPRD, period TBPRD+1; down-count: TBPRD .. 0, period TBPRD+1; up-down: 0 .. TBPRD .. 0,
  period 2 x TBPRD. Events CTR=ZERO, CTR=PRD, CTR=CMPA / CMPB (CAU/CAD, CBU/CBD by count direction).
* TBPRD is shadowed (loaded at CTR=ZERO) unless TBCTL.PRDLD; TBCTR reads/writes are live; TBSTS.CTRDIR is live.
* synchronization: SYNCI (SWFSYNC, or SYNCO of the previous module: ePWM1 -> 2 -> ... -> 6) loads TBCTR from TBPHS
  when PHSEN (PHSDIR picks the direction in up-down mode); SYNCO per SYNCOSEL: pass SYNCI through, CTR=ZERO,
  CTR=CMPB, or off.
Counter compare (CC): CMPA/CMPB shadowed unless CMPCTL.SHDWxMODE, loaded per LOADxMODE (CTR=0, CTR=PRD, both,
  freeze); CMPCTL.SHDWxFULL shows a pending shadow value.
Action qualifier (AQ): AQCTLA/B (ZRO, PRD, CAU, CAD, CBU, CBD: nothing / clear / set / toggle); simultaneous events
  follow the priority table of SPRUG04 (up-count: PRD > CBU > CAU > ZRO; down: ZRO > CBD > CAD > PRD; up-down
  counting up: CBU > CAU > ZRO > CBD > CAD, counting down: CBD > CAD > PRD > CBU > CAU); AQSFRC one-time and
  AQCSFRC continuous software forcing.
Dead band (DB): DBCTL IN_MODE (sources of the rising/falling-edge delay), OUT_MODE (bypass / FED on B / RED on A /
  both), POLSEL (invert the RED path = A, the FED path = B), DBRED / DBFED in TBCLK cycles (a pulse shorter than the
  delay disappears).
Trip zone (TZ): software forcing (TZFRC OST/CBC; CBC is released at the next CTR=ZERO, OST by TZCLR), TZCTL TZA/TZB
  (force high / low / no change; high impedance shows as L), TZFLG / TZEINT -> EPWMx_TZINT. EALLOW-protected registers
  as on silicon (TZSEL, TZCTL, TZEINT, TZCLR, TZFRC, HRCNFG). The TZ1-6 pins are not sampled.
Event trigger (ET): ETSEL INTSEL/SOCASEL/SOCBSEL (ZRO, PRD, CAU, CAD, CBU, CBD), ETPS prescaling (1st/2nd/3rd event,
  INTCNT/SOCxCNT live), ETFLG / ETCLR / ETFRC; INT -> EPWMx_INT (PIE 3.x); SOCA/SOCB start the ADC sequencers
  (ti_c2000.adc.epwm_soc).
Pins: EPWMxA / EPWMxB follow the outputs (PinBoard.drive_signal). Not modelled: PWM chopper (PCCTL, one `warn`),
HRPWM (TBPHSHR/CMPAHR/HRCNFG stored), TZ pins and digital compare, external EPWMSYNCI, emulation FREE_SOFT.
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ._util import C2000Block, build_regs, find_model, irq_line, sysctrl

UP, DOWN, UPDOWN, FREEZE = 0, 1, 2, 3
PHSEN, PRDLD, SWFSYNC, PHSDIR = 1 << 2, 1 << 3, 1 << 6, 1 << 13
SHDWAMODE, SHDWBMODE, SHDWAFULL, SHDWBFULL = 1 << 4, 1 << 6, 1 << 8, 1 << 9
TZ_INT, TZ_CBC, TZ_OST = 1 << 0, 1 << 1, 1 << 2
ET_INT, ET_SOCA, ET_SOCB = 1 << 0, 1 << 2, 1 << 3
# AQCTL field positions per event; ETSEL codes per event
AQ_SHIFT = {"ZRO": 0, "PRD": 2, "CAU": 4, "CAD": 6, "CBU": 8, "CBD": 10}
ET_CODE = {"ZRO": 1, "PRD": 2, "CAU": 4, "CAD": 5, "CBU": 6, "CBD": 7}
# processing order, lowest priority first (the last action applied wins)
ORDER = {UP: ("ZRO", "CAU", "CBU", "PRD"), DOWN: ("PRD", "CAD", "CBD", "ZRO"),
         "updown_up": ("CAD", "CBD", "ZRO", "CAU", "CBU"), "updown_down": ("CAU", "CBU", "PRD", "CAD", "CBD")}


def _aq(level: bool, act: int) -> bool:
    return {1: False, 2: True, 3: not level}.get(act, level)


@register("ti_c2000.epwm")
class EPWM(C2000Block):
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
            "AQCTLA": cfg, "AQCTLB": cfg, "DBCTL": cfg, "DBRED": cfg, "DBFED": cfg, "TZCTL": cfg, "TZSEL": cfg,
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
        regs_ = build_regs((name, "EPWM1"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, regs_, params=params, log=log)
        self.gate = params.get("gate", f"EPWM{self.index}ENCLK")
        self.running = False
        self._cyc0 = 0              # anchor cycle (a TBCLK edge) ...
        self._t0 = 0                # ... and the absolute tick count there (position = tick % period)
        self.tick = 2               # core cycles per TBCLK
        self.prd = self.cmpa = self.cmpb = 0       # active values
        self._prd_pending = False
        self._ev = None
        self.aqa = self.aqb = False                # action-qualifier outputs
        self._red = self._fed = False              # dead-band delayed paths
        self._red_src = self._fed_src = False
        self._red_ev = self._fed_ev = None
        self._out = {"A": None, "B": None}
        self._dir_up = True                        # direction while frozen
        self._cnt = {"INT": 0, "SOCA": 0, "SOCB": 0}
        self._int_line = self._tz_line = None
        self._int_lvl = self._tz_lvl = False
        self._started_at_zero = False
        self.periods = 0

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._int_line = self.params.get("irq") or irq_line(chip, f"EPWM{self.index}_INT", f"3.{self.index}")
        self._tz_line = self.params.get("tz_irq") or irq_line(chip, f"EPWM{self.index}_TZINT", f"2.{self.index}")
        sc = sysctrl(chip)
        if sc is not None and hasattr(sc, "pclk_listeners"):
            sc.pclk_listeners.append(self._clocks_changed)
        nxt = self.params.get("sync_next", f"EPWM{self.index + 1}")
        self._next = chip.peripherals.get(nxt) if nxt else None
        self._adc = find_model(chip, "ti_c2000.adc")

    def reset(self) -> None:
        if self.chip is not None:
            self._stop()
            self._cancel_db()
            for side in ("A", "B"):
                if self._out[side] is not None and self.chip.pins is not None:
                    self.chip.pins.release_signal(f"EPWM{self.index}{side}", "L")
            if self._int_lvl:
                self.irq(self._int_line, False)
            if self._tz_lvl:
                self.irq(self._tz_line, False)
        super().reset()
        self.prd = self.cmpa = self.cmpb = 0
        self._prd_pending = False
        self.aqa = self.aqb = self._red = self._fed = self._red_src = self._fed_src = False
        self._out = {"A": None, "B": None}
        self._dir_up = True
        self._cnt = {"INT": 0, "SOCA": 0, "SOCB": 0}
        self._int_lvl = self._tz_lvl = False

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

    def _pos_of(self, ctr: int, up: bool) -> int:
        m, prd = self._mode(), self.prd
        ctr = min(ctr, prd)
        if m == DOWN:
            return prd - ctr
        if m == UPDOWN:
            return ctr if (up or ctr == 0) and ctr < prd else (2 * prd - ctr) % max(2 * prd, 1)
        return ctr

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
        return self._ctr_dir(t % self._period())

    def _anchor(self, ctr: int, up: bool) -> None:
        """Counter is (ctr, up) at the current TBCLK edge (now): re-anchor and reschedule."""
        p = self._pos_of(ctr, up)
        self._cyc0 = self.chip.now_cycles()
        self._t0 = p
        self._reschedule(first=False)

    def _stop(self) -> None:
        if self.running:
            ctr, up = self._state_now()
            self.set("TBCTR", ctr)
            self._dir_up = up
        if self._ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None
        self.running = False

    def _update_run(self) -> None:
        """Start or stop the time base from CTRMODE / TBCLKSYNC / EPWMxENCLK."""
        want = self._mode() != FREEZE and self._clock_on() and self.chip is not None
        if want == self.running:
            return
        if not want:
            self._stop()
            return
        self.tick = self._tbclk_div()
        self.running = True
        ctr = self.get("TBCTR")
        self._cyc0 = self.chip.now_cycles()
        self._t0 = self._pos_of(ctr, self._dir_up)
        self._reschedule(first=True)

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
        if self._prd_pending or self.get("TZFLG") & TZ_CBC:
            rel.add("ZRO")
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
            if v > prd:
                continue
            if m == UP and u in rel:
                out.add(v)
            elif m == DOWN and d in rel:
                out.add(prd - v)
            elif m == UPDOWN:
                if u in rel and 0 < v <= prd:
                    out.add(v)
                if d in rel and v < prd:
                    out.add((2 * prd - v) % max(2 * prd, 1))
        return sorted(out)

    def _reschedule(self, first: bool) -> None:
        if self._ev is not None:
            self.chip.sched.cancel(self._ev)
            self._ev = None
        if not self.running:
            return
        L = self._period()
        pos = self._positions(self._relevant())
        if not pos:
            return
        t_now, frac = self._now_tick()
        base = t_now if (first or frac == 0) and first else t_now + 1
        cur = base % L
        best = None
        for p in pos:
            d = (p - cur) % L
            if best is None or d < best:
                best = d
        t = base + best
        self._ev = self.chip.sched.at_cycles(self._cyc0 + (t - self._t0) * self.tick, self._event, t)

    def _event(self, t: int) -> None:
        self._ev = None
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
            if ctr == v:
                if m == UP:
                    kinds.append(u)
                elif m == DOWN:
                    kinds.append(d)
                else:
                    kinds.append(u if (0 < p <= self.prd) else d)
        self._process(kinds, m, p)
        if self.running and self._ev is None:
            t_cur, _ = self._now_tick()                 # a TBPRD load or a sync may have re-anchored the count
            self._reschedule_from(t_cur)

    def _reschedule_from(self, t: int) -> None:
        """Schedule the next relevant event strictly after absolute tick t."""
        L = self._period()
        pos = self._positions(self._relevant())
        if not pos:
            return
        cur = (t + 1) % L
        best = min((p - cur) % L for p in pos)
        nt = t + 1 + best
        self._ev = self.chip.sched.at_cycles(self._cyc0 + (nt - self._t0) * self.tick, self._event, nt)

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
            if self.get("TZFLG") & TZ_CBC:                  # cycle-by-cycle trip ends at CTR=ZERO
                self.set("TZFLG", self.get("TZFLG") & ~TZ_CBC)
        self._et(present)
        so = (self.get("TBCTL") >> 4) & 3
        if (so == 1 and "ZRO" in present) or (so == 2 and present & {"CBU", "CBD"}):
            self._sync_out()
        self._shadow_loads(present)
        self._outputs()

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
        if self._prd_pending and "ZRO" in present:
            self._prd_pending = False
            ctr0 = 0
            self.prd = self.get("TBPRD")
            # the counter is at zero now: re-anchor in the new period
            t, _ = self._now_tick()
            self._cyc0 = self._cyc0 + (t - self._t0) * self.tick
            self._t0 = self._pos_of(ctr0, True)

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
            self._cnt[name] = min(self._cnt[name] + 1, 3)
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
        elif self._adc is not None and hasattr(self._adc, "epwm_soc"):
            self._adc.epwm_soc("A" if flag == ET_SOCA else "B")

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
        if self._next is not None and hasattr(self._next, "sync_in"):
            self._next.sync_in()

    def sync_in(self) -> None:
        """EPWMxSYNCI: load TBCTR from TBPHS when PHSEN; pass the pulse on when SYNCOSEL = 00."""
        tb = self.get("TBCTL")
        if tb & PHSEN:
            ph = self.get("TBPHS")
            up = bool(tb & PHSDIR) if self._mode() == UPDOWN else self._mode() != DOWN
            if self.running:
                self._anchor(ph, up)
            else:
                self.set("TBCTR", ph)
                self._dir_up = up
        self.set("TBSTS", self.get("TBSTS") | 2)          # SYNCI latched
        if (tb >> 4) & 3 == 0:
            self._sync_out()

    # ------------------------------------------------------------ outputs: AQ -> DB -> TZ -> pins
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
        cs = self.get("AQCSFRC")
        a = {1: False, 2: True}.get(cs & 3, a)
        b = {1: False, 2: True}.get((cs >> 2) & 3, b)
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
            cs = self.get("AQCSFRC")
            a = {1: False, 2: True}.get(cs & 3, self.aqa)
            b = {1: False, 2: True}.get((cs >> 2) & 3, self.aqb)
        db = self.get("DBCTL")
        om, pol = db & 3, (db >> 2) & 3
        oa = (self._red ^ bool(pol & 1)) if om & 2 else a
        ob = (self._fed ^ bool(pol & 2)) if om & 1 else b
        tz = self.get("TZFLG")
        if tz & (TZ_OST | TZ_CBC):
            ctl = self.get("TZCTL")
            oa = {0: False, 1: True, 2: False}.get(ctl & 3, oa)
            ob = {0: False, 1: True, 2: False}.get((ctl >> 2) & 3, ob)
        pins = self.chip.pins
        if pins is None:
            return
        for side, lvl in (("A", oa), ("B", ob)):
            v = "H" if lvl else "L"
            if self._out[side] != v:
                self._out[side] = v
                pins.drive_signal(f"EPWM{self.index}{side}", v)

    # ------------------------------------------------------------ register hooks
    def _tbctl_write(self, blk, reg, old, new):
        if self.running and (old ^ new) & 0x1F83:        # mode / clock divider changed while running
            ctr, up = self._state_now()
            self._store(reg, new)
            self.tick = self._tbclk_div()
            self.set("TBCTR", ctr)
            self._dir_up = up
            self._stop()
        else:
            self._store(reg, new)
        self._update_run()
        if new & SWFSYNC:
            self.sync_in()
        if self.running:
            self._reschedule(first=False)
        return new & ~SWFSYNC

    def _tbsts_read(self, blk, reg):
        v = self.get("TBSTS") & ~1
        _ctr, up = self._state_now()
        return v | int(up)

    def _tbsts_write(self, blk, reg, old, new):
        return old & ~(new & 6) & 0xFFFF                     # SYNCI / CTRMAX: write 1 to clear

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
                self._anchor(min(ctr, self.prd), up)
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
        self._outputs()
        return new & ~((1 << 2) | (1 << 5))

    def _tzfrc_write(self, blk, reg, old, new):
        trips = new & (TZ_CBC | TZ_OST)
        if trips:
            flg = self.get("TZFLG") | trips
            if self.get("TZEINT") & trips:
                flg |= TZ_INT
                self._tz_set(True)
            self.set("TZFLG", flg)
            self._outputs()
            if self.running:
                self._reschedule(first=False)
        return 0

    def _tzclr_write(self, blk, reg, old, new):
        flg = self.get("TZFLG") & ~(new & 7)
        self.set("TZFLG", flg)
        if not flg & TZ_INT:
            self._tz_set(False)
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

    def _pcctl_write(self, blk, reg, old, new):
        if new & 1:
            self.emit("warn", block=self.name, msg=f"{self.name}: PWM chopper (PCCTL.CHPEN) is not modelled")
        return None
