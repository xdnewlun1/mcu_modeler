"""Piccolo ePWM (type 1) with the digital compare submodule and the type-1 HRPWM additions: `ti_c2000.epwm_dc`.

Extends `ti_c2000.epwm` (epwm.py: time base, counter compare, action qualifier, dead band, chopper, trip zone, event
trigger, HRPWM MEP).  Source: SPRUI09A (2802x TRM) 3.1.3 Table 3-1, 3.2.7 (trip zone), 3.2.9 (digital compare),
3.4.5 / 3.4.8 (registers), 4.4 (HRPWM type 1); SPRUH18I chapters 3/4 for the 2806x (same module type).

Digital compare (3.2.9):
* DCTRIPSEL selects the DCAH/DCAL/DCBH/DCBL inputs: 0-2 = TZ1-TZ3 (the level of the one pin muxed to TZn, used as a
  plain input: active high or low), 8-10 = COMP1OUT-COMP3OUT (the comparator model's output after CMPINV/qualification,
  `ti_c2000.comp`); other codes are reserved (input low).  Re-evaluated whenever a TZ1-3 pin, a comparator output or a
  DC register changes (no polling).
* TZDCSEL qualifies DCAEVT1/2, DCBEVT1/2: 1 H low, 2 H high, 3 L low, 4 L high, 5 L high and H low (others off).
* Event filter (DCFCTL): SRCSEL picks one of the four events; with BLANKE the blanking window (DCFOFFSET TBCLK after
  the CTR=PRD (PULSESEL 0) or CTR=ZERO (1) pulse, DCFWINDOW TBCLK long; BLANKINV inverts it) suppresses it; the result
  is DCEVTFILT.  DCFOFFSETCNT / DCFWINDOWCNT read the live counters.  Each event output takes the raw event or
  DCEVTFILT per DCxCTL.EVTxSRCSEL.
* Event outputs: force -> TZCTL DCAEVT1/DCAEVT2 (EPWMxA) and DCBEVT1/DCBEVT2 (EPWMxB) actions while the event is
  present (priority TZA > DCAEVT1 > DCAEVT2, TZB > DCBEVT1 > DCBEVT2; 11 = no action); TZSEL.DCAEVT1/DCBEVT1 make
  the event a one-shot trip source, DCAEVT2/DCBEVT2 a cycle-by-cycle source; interrupt -> TZFLG.DCxEVTy latched on
  the rising edge (set again at once when cleared while present), EPWMx_TZINT with TZEINT; soc -> DCAEVT1.soc /
  DCBEVT1.soc (DCxCTL.EVT1SOCE) feed ETSEL.SOCASEL / SOCBSEL = 0 through the ETPS prescaler; sync -> DCAEVT1.sync /
  DCBEVT1.sync (EVT1SYNCE) is ORed into EPWMxSYNCI.  TZFRC.DCxEVTy sets the flag (and the interrupt) only.
* Counter capture (DCCAPCTL.CAPE): TBCTR is captured at the rising edge of DCEVTFILT into the active register;
  SHDWMODE = 0 copies it to DCCAP at the PULSESEL pulse (reads return the shadow), 1 reads the active value.
* TZ5 = CLOCKFAIL from the missing-clock logic (`clockfail_changed`), TZ6 = EMUSTOP (never: no debugger halt), TZ4 =
  inverted EQEP1ERR on parts with an eQEP (not connected: inactive).  TZ1-TZ3 come from the GPIO mux.
* The EVTxFRCSYNCSEL synchronous path (one TBCLK of delay) is treated as asynchronous.
Type-1 HRPWM (4.4): HRCNFG adds HRLOAD 4:3 (00 ZRO, 01 PRD, 10 both), SELOUTB (EPWMxB = inverted EPWMxA), SWAPAB
  (outputs swapped), AUTOCONV (CMPAHR/TBPHSHR bits 15:8 are a fraction scaled by HRMSTEP (ePWM1's register): steps =
  (fraction x HRMSTEP + 0x80) >> 8; HRMSTEP = 0, i.e. the SFO library never ran, gives no MEP delay).  Mirror
  registers TBPRDHRM/TBPRDM/CMPAHRM/CMPAM write like TBPRDHR/TBPRD/CMPAHR/CMPA; TBPRDM/CMPAM read the active value
  (TBPRDHRM/CMPAHRM read 0, "TI internal").  HRPCTL.HRPE (high-resolution period) and TBPRDHR are stored only (one
  `warn`); HRPCTL.PWMSYNCSEL picks the PWMSYNC pulse (0 CTR=PRD, 1 CTR=ZERO) sent to `pwmsync_listeners` (the
  comparator ramp generators).  HRPWR / HRMSTEP exist in ePWM1 only.
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ._util import find_model, listen_input
from .epwm import CTLMODE, EPWM, ET_SOCA, ET_SOCB, TZ_CBC, TZ_INT, TZ_OST, muxed_pins

DCA1, DCA2, DCB1, DCB2 = "A1", "A2", "B1", "B2"
EVENTS = (DCA1, DCA2, DCB1, DCB2)
FLAG_BIT = {DCA1: 1 << 3, DCA2: 1 << 4, DCB1: 1 << 5, DCB2: 1 << 6}
SELOUTB, SWAPAB, AUTOCONV = 1 << 5, 1 << 7, 1 << 6


def _qual(code: int, h: bool, lo: bool) -> bool:
    """TZDCSEL event qualification (SPRUI09 Table 3-44)."""
    return {1: not h, 2: h, 3: not lo, 4: lo, 5: lo and not h}.get(code, False)


@register("ti_c2000.epwm_dc")
class EPWMDC(EPWM):
    implements = ("ti_c2000.epwm",)
    eallow_regs = ("TZSEL", "TZDCSEL", "TZCTL", "TZEINT", "TZCLR", "TZFRC", "HRCNFG", "HRPWR", "HRMSTEP", "HRPCTL",
                   "DCTRIPSEL", "DCACTL", "DCBCTL", "DCFCTL", "DCCAPCTL")

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, regs, params=params, log=log)
        hooks = {
            "TZDCSEL": (self._dc_cfg_write, None), "DCTRIPSEL": (self._dc_cfg_write, None),
            "DCACTL": (self._dc_cfg_write, None), "DCBCTL": (self._dc_cfg_write, None),
            "DCFCTL": (self._dcfctl_write, None), "DCCAPCTL": (self._dc_cfg_write, None),
            "DCFOFFSET": (None, None), "DCFWINDOW": (None, None),
            "DCFOFFSETCNT": (None, self._offcnt_read), "DCFWINDOWCNT": (None, self._wincnt_read),
            "DCCAP": (None, self._dccap_read),
            "TZEINT": (self._tzeint_write, None),
            "TBPRDM": (self._mirror_write("TBPRD"), self._active_read("prd")),
            "CMPAM": (self._mirror_write("CMPA"), self._active_read("cmpa")),
            "CMPAHRM": (self._mirror_write("CMPAHR"), lambda b, r: 0),
            "TBPRDHRM": (self._mirror_write("TBPRDHR"), lambda b, r: 0),
            "HRPCTL": (self._hrpctl_write, None), "TBPRDHR": (self._tbprdhr_write, None),
        }
        for rname, (wr, rd) in hooks.items():
            r = self.regs.get(rname)
            if r is None:
                continue
            if wr is not None:
                r.on_write = wr
            if rd is not None:
                r.on_read = r.on_peek = rd              # computed values only: the debugger may peek them
        if "TZFLG" in self.regs:
            self.regs["TZFLG"].ro_mask = 0xFFFF
        if self.hr and "HRCNFG" in self.regs:
            self.regs["HRCNFG"].ro_mask = 0xFF00          # type 1: SWAPAB, AUTOCONV, SELOUTB, HRLOAD 4:3
        self.pwmsync_listeners: list = []                 # cb(index) on every PWMSYNC pulse (comparator ramps)
        self._dc_init()

    def _dc_init(self) -> None:
        self._dc = {e: False for e in EVENTS}             # event outputs after the source select
        self._raw = {e: False for e in EVENTS}
        self._filt = False
        self._blank = False                               # blanking window active (before BLANKINV)
        self._blank_evs: list = []
        self._blank_t: tuple[int, int] | None = None      # (window start cycle, end cycle) of the current window
        self._cap_active = 0
        self._cap_shadow = 0
        self._dc_cbc = False                              # a CBC-selected DC event is present
        self._clockfail = False
        self._dc_ost_prev = {DCA1: False, DCB1: False}
        self._prd_warned = False

    # ------------------------------------------------------------------ lifecycle
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        board = getattr(chip, "pins", None)
        if board is not None:
            seen = []
            for s in ("TZ1", "TZ2", "TZ3"):
                for p in board.signal_pins(s):
                    if p not in seen:
                        seen.append(p)
                        listen_input(chip, p, self._dc_pin)
        for b in chip.peripherals.values():
            if getattr(b, "model", "") == "ti_c2000.comp" and hasattr(b, "out_listeners"):
                b.out_listeners.append(self._dc_comp)

    def reset(self) -> None:
        if self.chip is not None:
            for ev in self._blank_evs:
                self.chip.sched.cancel(ev)
        super().reset()
        self._dc_init()

    # ------------------------------------------------------------------ inputs
    def clockfail_changed(self, on: bool) -> None:
        """CLOCKFAIL from the Piccolo missing-clock logic drives TZ5."""
        if on != self._clockfail:
            self._clockfail = on
            self._tz_changed()

    def tz_low(self) -> int:
        low = 0
        board = self.chip.pins
        for n in range(3):                               # TZ1-TZ3 from the GPIO mux
            own = muxed_pins(board, f"TZ{n + 1}")
            if len(own) == 1 and own[0].level() == "L":
                low |= 1 << n
        if self._clockfail:
            low |= 1 << 4                                # TZ5 = CLOCKFAIL
        return low

    def _comp_out(self, n: int) -> bool:
        b = self.chip.peripherals.get(f"COMP{n}") if self.chip is not None else None
        return bool(getattr(b, "out", False))

    def _dc_input(self, sel: int) -> bool:
        if sel <= 2:
            own = muxed_pins(self.chip.pins, f"TZ{sel + 1}") if self.chip.pins is not None else []
            return len(own) == 1 and own[0].level() == "H"
        if 8 <= sel <= 10:
            return self._comp_out(sel - 7)
        return False

    def _dc_pin(self, pin: Any = None) -> None:
        self._dc_eval()

    def _dc_comp(self, *_a) -> None:
        self._dc_eval()

    # ------------------------------------------------------------------ evaluation
    def _dc_enabled(self) -> bool:
        return bool(self.get("TZDCSEL") & 0xFFF) if "TZDCSEL" in self.regs else False

    def _dc_eval(self) -> None:
        if self.chip is None or "TZDCSEL" not in self.regs:
            return
        sel = self.get("TZDCSEL")
        if not sel & 0xFFF and not any(self._dc.values()):
            return
        ts = self.get("DCTRIPSEL")
        ah, al = self._dc_input(ts & 0xF), self._dc_input((ts >> 4) & 0xF)
        bh, bl = self._dc_input((ts >> 8) & 0xF), self._dc_input((ts >> 12) & 0xF)
        raw = {DCA1: _qual(sel & 7, ah, al), DCA2: _qual((sel >> 3) & 7, ah, al),
               DCB1: _qual((sel >> 6) & 7, bh, bl), DCB2: _qual((sel >> 9) & 7, bh, bl)}
        self._raw = raw
        fc = self.get("DCFCTL")
        src = EVENTS[fc & 3]
        blank = False
        if fc & 4:
            blank = self._blank != bool(fc & 8)          # BLANKINV inverts the window
        filt = raw[src] and not blank
        if filt and not self._filt:
            self._capture()
        self._filt = filt
        out = {}
        for e, reg, bit in ((DCA1, "DCACTL", 0), (DCA2, "DCACTL", 8), (DCB1, "DCBCTL", 0), (DCB2, "DCBCTL", 8)):
            out[e] = filt if (self.get(reg) >> bit) & 1 else raw[e]
        rising = {e for e in EVENTS if out[e] and not self._dc[e]}
        changed = out != self._dc
        self._dc = out
        flg = self.get("TZFLG")
        new_flags = 0
        for e in EVENTS:
            if out[e] and not flg & FLAG_BIT[e]:
                new_flags |= FLAG_BIT[e]
        if new_flags:
            self._set_flags(new_flags)
        for e, reg in ((DCA1, "DCACTL"), (DCB1, "DCBCTL")):
            if e in rising:
                ctl = self.get(reg)
                if ctl & 4:                              # EVT1SOCE
                    self._dc_soc("A" if e == DCA1 else "B")
                if ctl & 8:                              # EVT1SYNCE
                    self.sync_in()
        # trip sources (TZSEL bits 14/15 one-shot, 6/7 cycle-by-cycle)
        tzsel = self.get("TZSEL")
        ost = (out[DCA1] and tzsel & (1 << 14)) or (out[DCB1] and tzsel & (1 << 15))
        cbc = bool((out[DCA2] and tzsel & (1 << 6)) or (out[DCB2] and tzsel & (1 << 7)))
        self._dc_cbc = cbc
        trips = (TZ_OST if ost and (DCA1 in rising or DCB1 in rising or not self.get("TZFLG") & TZ_OST) else 0) \
            | (TZ_CBC if cbc else 0)
        if cbc:
            self._cbc_pin = True
        if trips:
            self._trip(trips)
        elif changed:
            self._outputs()
        self._blank_watch()

    def _set_flags(self, bits: int) -> None:
        flg = self.get("TZFLG") | bits
        if self.get("TZEINT") & bits and not flg & TZ_INT:
            flg |= TZ_INT
            self._tz_set(True)
        self.set("TZFLG", flg)

    def _tz_changed(self, pin: Any = None) -> None:
        super()._tz_changed(pin)
        if self._dc_cbc:
            self._cbc_pin = True                         # a DC CBC source holds the CBC action past CTR=ZERO

    def _trip_actions(self, oa: Any, ob: Any) -> tuple[Any, Any]:
        cfg = self.get("HRCNFG") if self.hr and "HRCNFG" in self.regs else 0
        if cfg & SELOUTB:
            ob = None if oa is None else not oa
        if cfg & SWAPAB:
            oa, ob = ob, oa
        ctl = self.get("TZCTL")
        act = {0: None, 1: True, 2: False}
        for e, sh, side in ((DCA2, 6, 0), (DCA1, 4, 0), (DCB2, 10, 1), (DCB1, 8, 1)):
            if self._dc.get(e) and (ctl >> sh) & 3 != 3:
                if side == 0:
                    oa = act[(ctl >> sh) & 3]
                else:
                    ob = act[(ctl >> sh) & 3]
        return super()._trip_actions(oa, ob)

    def _dc_soc(self, which: str) -> None:
        """DCAEVT1.soc / DCBEVT1.soc as ETSEL SOCxSEL = 0 events through the ETPS prescaler."""
        et, ps = self.get("ETSEL"), self.get("ETPS")
        en, sh, prd_sh, flag, name = (1 << 11, 8, 8, ET_SOCA, "SOCA") if which == "A" else \
            (1 << 15, 12, 12, ET_SOCB, "SOCB")
        if not et & en or (et >> sh) & 7 != 0:
            return
        prd = (ps >> prd_sh) & 3
        if not prd:
            return
        self._cnt[name] = min(self._cnt[name] + 1, prd)
        if self._cnt[name] >= prd:
            self._cnt[name] = 0
            self._set_et(flag)

    # ------------------------------------------------------------------ blanking window and capture
    def _relevant(self) -> set[str]:
        rel = super()._relevant()
        fc = self.get("DCFCTL") if "DCFCTL" in self.regs else 0
        cap = self.get("DCCAPCTL") if "DCCAPCTL" in self.regs else 0
        if fc & 4 or (cap & 1 and not cap & 2):
            rel.add("PRD" if (fc >> 4) & 3 == 0 else "ZRO")
        if self.pwmsync_listeners:
            rel.add("ZRO" if self.get("HRPCTL") & 2 else "PRD") if "HRPCTL" in self.regs else rel.add("PRD")
        return rel

    def _process(self, kinds: list[str], m: int, p: int) -> None:
        super()._process(kinds, m, p)
        present = set(kinds)
        fc = self.get("DCFCTL") if "DCFCTL" in self.regs else 0
        pulse = "PRD" if (fc >> 4) & 3 == 0 else "ZRO"
        if pulse in present:
            cap = self.get("DCCAPCTL") if "DCCAPCTL" in self.regs else 0
            if cap & 1 and not cap & 2:
                self._cap_shadow = self._cap_active
            if fc & 4:
                self._start_window()
        if self.pwmsync_listeners and "HRPCTL" in self.regs:
            sync = "ZRO" if self.get("HRPCTL") & 2 else "PRD"
            if sync in present:
                for cb in list(self.pwmsync_listeners):
                    cb(self.index)

    def _start_window(self) -> None:
        sched = self.chip.sched
        for ev in self._blank_evs:
            sched.cancel(ev)
        now = self.chip.now_cycles()
        t0 = now + self.get("DCFOFFSET") * self.tick
        t1 = t0 + (self.get("DCFWINDOW") & 0xFF) * self.tick
        self._blank_t = (t0, t1)
        self._blank_evs = [sched.at_cycles(t0, self._blank_set, True), sched.at_cycles(t1, self._blank_set, False)]
        if t0 == now:
            pass

    def _blank_set(self, on: bool) -> None:
        self._blank_evs = [e for e in self._blank_evs if not e.cancelled and e.cycles > self.chip.now_cycles()]
        if on != self._blank:
            self._blank = on
            self._dc_eval()

    def _blank_watch(self) -> None:
        pass

    def _capture(self) -> None:
        cap = self.get("DCCAPCTL") if "DCCAPCTL" in self.regs else 0
        if cap & 1:
            self._cap_active = self._state_now()[0]
            if cap & 2:
                self._cap_shadow = self._cap_active

    def _dccap_read(self, blk, reg):
        cap = self.get("DCCAPCTL")
        return self._cap_active if cap & 2 else self._cap_shadow

    def _offcnt_read(self, blk, reg):
        if self._blank_t is None or self.chip is None:
            return 0
        now = self.chip.now_cycles()
        return max(0, (self._blank_t[0] - now) // max(self.tick, 1))

    def _wincnt_read(self, blk, reg):
        if self._blank_t is None or self.chip is None:
            return 0
        now = self.chip.now_cycles()
        if now < self._blank_t[0]:
            return 0
        return max(0, (self._blank_t[1] - now) // max(self.tick, 1)) & 0xFF

    # ------------------------------------------------------------------ registers
    def _dc_cfg_write(self, blk, reg, old, new):
        self._store(reg, new)
        self._dc_eval()
        if self.running:
            self._reschedule(first=False)
        return None

    def _dcfctl_write(self, blk, reg, old, new):
        self._store(reg, new)
        if not new & 4 and self.chip is not None:
            for ev in self._blank_evs:
                self.chip.sched.cancel(ev)
            self._blank_evs = []
            self._blank = False
            self._blank_t = None
        self._dc_eval()
        if self.running:
            self._reschedule(first=False)
        return None

    def _tzsel_write(self, blk, reg, old, new):
        r = super()._tzsel_write(blk, reg, old, new)
        self._dc_eval()
        return r

    def _cfg_write(self, blk, reg, old, new):
        r = super()._cfg_write(blk, reg, old, new)
        if reg.name == "TZCTL":
            self._dc_eval()
        return r

    def _tzeint_write(self, blk, reg, old, new):
        self._store(reg, new)
        flg = self.get("TZFLG")
        if flg & new & 0x7E and not flg & TZ_INT:
            self.set("TZFLG", flg | TZ_INT)
            self._tz_set(True)
        return None

    def _tzclr_write(self, blk, reg, old, new):
        """Clears TZFLG bits incl. the DC flags; TZCLR.INT with an enabled flag still set pulses again; a DC event
        still present sets its flag again at once."""
        flg = self.get("TZFLG") & ~(new & 0x7F)
        if not flg & TZ_INT:
            self._tz_set(False)
            if new & TZ_INT and flg & self.get("TZEINT") & 0x7E:
                flg |= TZ_INT
                self._tz_set(True)
        self.set("TZFLG", flg)
        self._tz_changed()
        again = 0
        for e in EVENTS:
            if self._dc[e] and new & FLAG_BIT[e]:
                again |= FLAG_BIT[e]
        if again:
            self._set_flags(again)
        self._outputs()
        return 0

    def _tzfrc_write(self, blk, reg, old, new):
        trips = new & (TZ_CBC | TZ_OST)
        if trips:
            self._trip(trips)
        dcf = new & 0x78
        if dcf:
            self._set_flags(dcf)
        return 0

    def _mirror_write(self, target: str):
        def wr(blk, reg, old, new, _t=target):
            r = self.regs.get(_t)
            if r is None:
                return 0
            prev = self.get(_t)
            self.set(_t, new)
            hook = r.on_write
            if hook is not None:
                x = hook(self, r, prev, new)
                if x is not None:
                    self.set(_t, x)
            return 0
        return wr

    def _active_read(self, attr: str):
        def rd(blk, reg, _a=attr):
            return getattr(self, _a)
        return rd

    def _hrpctl_write(self, blk, reg, old, new):
        if new & 1 and not self._prd_warned:
            self._prd_warned = True
            self.emit("warn", block=self.name, msg=f"{self.name}: HRPCTL.HRPE (high-resolution period) is stored "
                      "only: the period stays TBPRD + 1 TBCLK")
        self._store(reg, new)
        if self.running:
            self._reschedule(first=False)
        return None

    def _tbprdhr_write(self, blk, reg, old, new):
        return None

    # ------------------------------------------------------------------ HRPWM auto-conversion
    def _hr_steps(self) -> int:
        raw = self.get("TBPHSHR") >> 8 if self.get("HRCNFG") & CTLMODE else self._hr_cmp
        if not self.get("HRCNFG") & AUTOCONV:
            return raw
        e1 = self.chip.peripherals.get("EPWM1") if self.chip is not None else None
        step = e1.get("HRMSTEP") & 0xFF if e1 is not None and "HRMSTEP" in e1.regs else 0
        return (raw * step + 0x80) >> 8
