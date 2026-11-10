"""C2833x/C2823x enhanced quadrature encoder pulse module eQEP1/eQEP2 (Type-0 eQEP; TI SPRUI07 chapter 6, the same
module as SPRUG05A).

Register map (SPRUI07 Table 6-1 / 6-4, 16-bit words): QPOSCNT 0x00, QPOSINIT 0x02, QPOSMAX 0x04, QPOSCMP 0x06,
QPOSILAT 0x08, QPOSSLAT 0x0A, QPOSLAT 0x0C, QUTMR 0x0E, QUPRD 0x10 (32 bits, low word first), QWDTMR 0x12,
QWDPRD 0x13, QDECCTL 0x14, QEPCTL 0x15, QCAPCTL 0x16, QPOSCTL 0x17, QEINT 0x18, QFLG 0x19, QCLR 0x1A, QFRC 0x1B,
QEPSTS 0x1C, QCTMR 0x1D, QCPRD 0x1E, QCTMRLAT 0x1F, QCPRDLAT 0x20.  No register is EALLOW-protected (the "Write
Protection" column of Table 6-4 is empty).  Clock: PCLKCR1.EQEPxENCLK (sysctrl `pclk_listeners`).

Inputs: the levels of the pins muxed to EQEPxA/B/I/S (PinBoard input listeners; the pins stay inputs, never driven).
A pin edge is processed at the core cycle it happens (`chip.now_cycles()`).  A mux change resamples the inputs without
producing an edge.

Quadrature decoder (QDECCTL): QAP/QBP/QIP/QSP invert the inputs, SWAP exchanges A and B, IGATE gates the index with
the strobe (QI = QEPI & QEPS).  QSRC = 0 quadrature count: every A/B edge is a QCLK, the direction follows the truth
table of SPRUI07 Table 6-2 (A edge: up when A != B afterwards; B edge: up when A == B), so a reversal toggles the
direction.  An A and a B edge in the same SYSCLKOUT cycle is a phase error: QFLG.PHE, that edge is not counted.
QSRC = 1 direction-count (QCLK from A, direction = B), 2 up-count, 3 down-count; in these modes XCR = 0 counts both
A edges (2x), XCR = 1 rising edges only (1x).  XCR has no effect in quadrature mode (always 4x).

Position counter (QEPCTL.QPEN = 1): on every QCLK it counts up/down, rolls over QPOSMAX -> 0 (QFLG.PCO) and
0 -> QPOSMAX (QFLG.PCU) in every PCRM mode; QEPSTS.QDF = the QCLK direction, a change sets QFLG.QDC.
* PCRM = 00 (reset on index): an index event (rising edge of QI) arms the next index marker.  The first marker is the
  first QCLK after it; that edge (input, level, direction) is remembered and later markers are the same edge in the
  forward direction / the opposite level in reverse (SPRUI07 6.5.1.1).  At a marker the counter (after this QCLK)
  is latched to QPOSILAT, QDLF := direction, QFLG.IEL, PCEF := latched value not 0 and not QPOSMAX (QFLG.PCE when
  set), then QPOSCNT := 0 (forward) / QPOSMAX (reverse).  The first marker sets QEPSTS.FIMF (once until reset or
  QPEN = 0) and FIDF := direction.
* PCRM = 01 reset on QPOSMAX (the rollover above), 10 first index (the 00 reset on the first marker only, then 01),
  11 unit time: QPOSCNT := 0 (QPOSMAX in down-count mode) at every unit time out.
* Index latch (PCRM != 00): IEL = 01 / 10 latch QPOSCNT into QPOSILAT on the rising / falling QI edge, 11 on the
  index marker (with FIMF / FIDF); each latch sets QDLF := QDF and QFLG.IEL.
* Strobe latch: SEL = 0 QPOSSLAT := QPOSCNT on the rising QS edge, SEL = 1 rising when QDF = 1 (forward) / falling in
  reverse; QFLG.SEL.  Initialization QPOSCNT := QPOSINIT: IEI = 10 / 11 rising / falling QI edge, SEI = 10 rising QS
  edge, SEI = 11 rising forward / falling reverse, SWI = 1 at each QEPCTL write with SWI set (not self-clearing).
  Latches happen before an initialization on the same edge.
* QPEN = 0 is the module's software reset: QEPSTS, QPOSILAT, QPOSSLAT, QPOSLAT, QCTMRLAT, QCPRDLAT and the internal
  state (index marker, unit position prescaler) are cleared and QCLKs are ignored; QPOSCNT, the configuration and
  QFLG keep their values (QPOSCNT is meant to be written while QPEN = 0).
* CPU reads of QPOSCNT (its low word, or the 32-bit register) with QCLM = 0 latch QCTMR / QCPRD into QCTMRLAT /
  QCPRDLAT.  QPOSLAT is latched only at unit time out with QCLM = 1 (SPRUI07 QEPCTL.QCLM, Table 6-12).
* Position compare (QPOSCTL.PCE = 1): QFLG.PCM when a QCLK (or index reset) makes QPOSCNT equal to the active QPOSCMP.
  PCSHDW = 0: writes go to the active register; PCSHDW = 1: to the shadow, loaded at the next QPOSCNT = 0 (PCLOAD = 0)
  or compare match (PCLOAD = 1), which sets QFLG.PCR.  QPOSCMP reads return the active value.

Timers (computed from anchors, never stepped; a scheduled event only for the next match):
* Unit timer (QEPCTL.UTE): QUTMR counts SYSCLKOUT; at QUTMR = QUPRD it reads 0 on the next cycle (period QUPRD + 1
  cycles) and a unit time out sets QFLG.UTO and, with QCLM = 1 and QPEN = 1, latches QPOSCNT, QCTMR and QCPRD into
  QPOSLAT, QCTMRLAT and QCPRDLAT.
* Watchdog (QEPCTL.WDE): QWDTMR counts SYSCLKOUT / 64 (prescaler phase from the WDE enable), every QCLK clears it;
  when it reaches QWDPRD QFLG.WTO is set and it rolls over to 0 on the next tick.
* Capture unit (QCAPCTL.CEN): QCTMR (16 bits) counts SYSCLKOUT / 2^CCPS; every 2^UPPS QCLKs (unit position event)
  QCPRD := QCTMR, QCTMR := 0 and QEPSTS.UPEVNT = 1.  QEPSTS.COEF when QCTMR wrapped past 0xFFFF since the last unit
  position event (also noticed on a QEPSTS read: the counter wraps, QCPRD gets the low 16 bits), CDEF when the
  direction changed in between.

Flags and interrupt: QFLG is read-only; QCLR writes of 1 clear flags (reads 0), QFRC writes of 1 set them (reads 0).
QEPSTS.UPEVNT, COEF, CDEF and FIMF are write-1-to-clear.  An event flag enabled in QEINT while QFLG.INT = 0 sets INT
and raises EQEPx_INT (PIE 5.x, looked up by name); the line is held until QCLR.INT, which raises another interrupt if
enabled flags are still set (same scheme as eCAP / ePWM ETFLG).  Chip reset cancels every scheduled event.

Position-compare sync output (SPRUI07 6.4.3): with QDECCTL.SOEN the pin muxed to EQEPxI (SPSEL = 0) or EQEPxS
(SPSEL = 1) becomes an output; each compare match drives PCSOUT active (high, low with QPOSCTL.PCPOL) for
4 x (PCSPW + 1) SYSCLKOUT, retriggered by a new match while active.  That pin is then not an index / strobe input
for this module (our choice: the guide does not say whether the module sees its own pulse).

Not modelled: emulation suspend (QEPCTL.FREE_SOFT is stored; the timers always run: there is no debugger halt),
the SYSCLKOUT synchronization delay (inputs pass the GPIO input qualification, `_util.listen_input`),
the 1-SYSCLKOUT boundary case of SPRUI07 6.5.1.1's note (an index marker is always the QCLK after the index edge).
Changing the polarity / gating bits does not produce an edge.
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ._util import C2000Block, build_regs, gpio_resync, irq_line, sysctrl, listen_input, unlisten_input

MASK32 = 0xFFFFFFFF
# QDECCTL
SOEN, SPSEL, XCR, SWAP, IGATE, QAP, QBP, QIP, QSP = 1 << 13, 1 << 12, 1 << 11, 1 << 10, 1 << 9, 1 << 8, 1 << 7, \
    1 << 6, 1 << 5
# QEPCTL
SWI, SELB, QPEN, QCLM, UTE, WDE = 1 << 7, 1 << 6, 1 << 3, 1 << 2, 1 << 1, 1 << 0
# QCAPCTL
CEN = 1 << 15
# QPOSCTL
PCSHDW, PCLOAD, PCPOL, PCE_EN = 1 << 15, 1 << 14, 1 << 13, 1 << 12
# QFLG / QEINT / QCLR / QFRC
INT, PCE, PHE, QDC, WTO, PCU, PCO, PCR, PCM, SEL, IEL, UTO = (1 << i for i in range(12))
EVENT_FLAGS = 0x0FFE
# QEPSTS
PCEF, FIMF, CDEF, COEF, QDLF, QDF, FIDF, UPEVNT = (1 << i for i in range(8))
WD_DIV = 64


def _adv(v0: int, d: int, prd: int, mod: int) -> int:
    """Up-counter that reads `prd` for one tick and then 0 (and wraps at `mod` when above `prd`): value `d` ticks
    after it read `v0`."""
    if v0 <= prd:
        return (v0 + d) % (prd + 1)
    k = mod - v0
    return v0 + d if d < k else (d - k) % (prd + 1)


def _to_match(v0: int, prd: int, mod: int) -> int:
    """Ticks (> 0) from a reading of `v0` until the counter next reads `prd`."""
    if v0 < prd:
        return prd - v0
    if v0 == prd:
        return prd + 1
    return mod - v0 + prd


@register("ti_c2000.eqep")
class EQEP(C2000Block):
    cycle_anchors = ("_ut_c0", "_wd_c0", "_wd_p0", "_ct_c0", "_ct_p0")
    eallow_regs = None                     # SPRUI07 Table 6-4: no eQEP register is EALLOW-protected

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.index = int(params.get("index", name[-1] if name[-1].isdigit() else 1))
        zero = lambda b, r: 0                                         # noqa: E731
        ov = {
            "QPOSCMP": {"on_write": self._qposcmp_write},
            "QUTMR": {"on_read": self._qutmr_read, "on_write": self._qutmr_write},
            "QUPRD": {"on_write": self._qupr_write},
            "QWDTMR": {"on_read": self._qwdtmr_read, "on_write": self._qwdtmr_write},
            "QWDPRD": {"on_write": self._qwdprd_write},
            "QDECCTL": {"on_write": self._qdecctl_write},
            "QPOSCTL": {"on_write": self._qposctl_write},
            "QEPCTL": {"on_write": self._qepctl_write},
            "QCAPCTL": {"on_write": self._qcapctl_write},
            "QEINT": {"on_write": self._qeint_write},
            "QFLG": {"ro_mask": 0xFFFF},
            "QCLR": {"on_write": self._qclr_write, "on_read": zero},
            "QFRC": {"on_write": self._qfrc_write, "on_read": zero},
            "QEPSTS": {"on_read": self._qepsts_read},
            "QCTMR": {"on_read": self._qctmr_read, "on_write": self._qctmr_write},
        }
        regs_ = build_regs((name, "EQEP1"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, regs_, params=params, log=log)
        self.gate = params.get("gate", f"EQEP{self.index}ENCLK")
        self.signals = {k: f"EQEP{self.index}{k}" for k in "ABIS"}
        self._pin_key: dict[str, str] = {}          # pin id -> "A" | "B" | "I" | "S"
        self._pins: dict[str, list] = {k: [] for k in "ABIS"}
        self._raw = {k: 0 for k in "ABIS"}            # input levels (before polarity)
        self._qi = self._qs = 0                       # effective index / strobe (polarity, gating)
        self._int_line = None
        self._int_lvl = False
        self._ut_ev = self._wd_ev = None
        self._pcs_ev = None
        self._pcs_active = False                      # position-compare sync output pulse (PCSOUT) active
        self.pcs_pulses = 0
        self._clock = False                           # PCLKCR1 gate (cached; updated by the sysctrl listener)
        self._init_state()
        self.edges = 0                                # QCLKs counted (statistics)

    def _init_state(self) -> None:
        self._ut_on = False
        self._ut_c0 = self._ut_v0 = 0
        self._wd_on = False
        self._wd_p0 = self._wd_c0 = self._wd_v0 = 0
        self._ct_on = False
        self._ct_p0 = self._ct_c0 = self._ct_v0 = 0
        self._ct_ovf = False                          # COEF already raised for the current interval
        self._cmp_shadow = 0
        self._cmp_pending = False
        self._cmp_active = 0
        self._internal_reset()
        self._last_edge = (-1, "")                    # (cycle, decoder input) of the last A/B edge
        self._tiny_warned = False

    def _internal_reset(self) -> None:
        self._idx_pending = False
        self._marker: tuple[str, int] | None = None  # (decoder input, level in the forward direction)
        self._fim_done = False
        self._first_idx_done = False
        self._up_cnt = 0
        self._cap_dirchg = False

    # ------------------------------------------------------------ lifecycle
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._int_line = self.params.get("irq") or irq_line(chip, f"EQEP{self.index}_INT", f"5.{self.index}")
        sc = sysctrl(chip)
        if sc is not None and hasattr(sc, "pclk_listeners"):
            sc.pclk_listeners.append(self._clocks_changed)
        board = getattr(chip, "pins", None)
        if board is not None:
            for k, sig in self.signals.items():
                self._pins[k] = list(board.signal_pins(sig))
                for p in self._pins[k]:
                    self._pin_key[p.id] = k
                    listen_input(chip, p, self._pin_changed)
            if self._pin_key:
                board.subscribe(self._pin_event)
        self._clock = self._clock_on()
        self._resample()

    def reset(self) -> None:
        if self.chip is not None:
            for ev in (self._ut_ev, self._wd_ev, self._pcs_ev):
                if ev is not None:
                    self.chip.sched.cancel(ev)
            if self._int_lvl and self._int_line is not None:
                self.irq(self._int_line, False)
        self._ut_ev = self._wd_ev = self._pcs_ev = None
        was_out = self._pcs_active or self._sync_key() is not None
        super().reset()
        self._pcs_active = False
        if was_out and self.chip is not None:
            self._pcs_resync()
        self._int_lvl = False
        self._init_state()
        if self.chip is not None:
            self._clock = self._clock_on()
            self._resample()

    def _clock_on(self) -> bool:
        sc = self._sysctrl
        return sc is None or sc.clock_enabled(self.gate)

    def _now(self) -> int:
        return self.chip.now_cycles()

    def _clocks_changed(self) -> None:
        on = self._clock_on()
        if on == self._clock:
            return
        self._clock = on
        self._update_timers()

    def _counting(self) -> bool:
        return self._clock and bool(self.get("QEPCTL") & QPEN)

    # ------------------------------------------------------------ inputs
    def _sig_level(self, k: str) -> int:
        board = self.chip.pins
        for p in self._pins[k]:
            if board.signal_matches(p.owner, self.signals[k]):
                return 1 if p.level() == "H" else 0
        return 0

    def _resample(self) -> None:
        """Take the current input levels without producing edges (reset, mux change)."""
        if self.chip is None or self.chip.pins is None:
            return
        for k in "ABIS":
            self._raw[k] = self._sig_level(k)
        self._qi, self._qs = self._eff_is()

    def _eff_is(self) -> tuple[int, int]:
        dc = self.get("QDECCTL")
        qs = self._raw["S"] ^ bool(dc & QSP)
        qi = self._raw["I"] ^ bool(dc & QIP)
        if dc & IGATE:
            qi &= qs
        return int(qi), int(qs)

    def _pin_event(self, ev: Any) -> None:
        if ev.field == "owner" and ev.pin in self._pin_key:
            self._resample()

    def _pin_changed(self, pin: Any) -> None:
        k = self._pin_key.get(pin.id)
        if k is None or not self.chip.pins.signal_matches(pin.owner, self.signals[k]):
            return
        if k == self._sync_key():
            return                                    # the pin carries our own sync output
        lvl = pin.level()
        if lvl == "X":
            return
        v = 1 if lvl == "H" else 0
        if v == self._raw[k]:
            return
        self._raw[k] = v
        if k in "IS":
            qi, qs = self._eff_is()
            dqs, dqi = qs != self._qs, qi != self._qi
            self._qi, self._qs = qi, qs
            if not self._counting():
                return
            if dqs:
                self._strobe_edge(bool(qs))
            if dqi:
                self._index_edge(bool(qi))
            return
        if not self._counting():
            return
        self._ab_edge(k)

    # ------------------------------------------------------------ decoder
    def _ab_edge(self, k: str) -> None:
        dc = self.get("QDECCTL")
        a = self._raw["A"] ^ bool(dc & QAP)
        b = self._raw["B"] ^ bool(dc & QBP)
        if dc & SWAP:
            a, b = b, a
            inp = "B" if k == "A" else "A"
        else:
            inp = k
        qsrc = dc >> 14
        if qsrc == 0:                                          # quadrature count (x4)
            now = self._now()
            if self._last_edge[0] == now and self._last_edge[1] != inp:
                self._last_edge = (now, inp)                   # A and B in the same cycle: phase error
                self._set_flags(PHE)
                return
            self._last_edge = (now, inp)
            up = (a != b) if inp == "A" else (a == b)
            self._qclk(up, inp, int(a if inp == "A" else b))
            return
        if inp != "A":                                         # QB is only the direction input here
            return
        if dc & XCR and not a:                                 # 1x: rising edges of QA only
            return
        up = bool(b) if qsrc == 1 else qsrc == 2
        self._qclk(up, "A", int(a))

    def _is_marker(self, inp: str, lvl: int, up: bool) -> bool:
        if self._marker is None:
            return True
        m_inp, m_lvl = self._marker
        return inp == m_inp and lvl == (m_lvl if up else 1 - m_lvl)

    def _qclk(self, up: bool, inp: str, lvl: int) -> None:
        self.edges += 1
        if self._wd_on:
            self._wd_c0, self._wd_v0 = self._now(), 0          # any QCLK clears the watchdog timer
        sts = self.get("QEPSTS")
        flags = 0
        if up != bool(sts & QDF):
            sts ^= QDF
            flags |= QDC
            self._cap_dirchg = True
        cnt, mx = self.get("QPOSCNT"), self.get("QPOSMAX")
        if up:
            if cnt == mx:
                new, flags = 0, flags | PCO
            else:
                new = (cnt + 1) & MASK32
        elif cnt == 0:
            new, flags = mx, flags | PCU
        else:
            new = cnt - 1
        if self._idx_pending and self._is_marker(inp, lvl, up):
            self._idx_pending = False
            ctl = self.get("QEPCTL")
            pcrm, iel = (ctl >> 12) & 3, (ctl >> 4) & 3
            first = self._marker is None
            if first:
                self._marker = (inp, lvl if up else 1 - lvl)
            sw_marker = pcrm == 0 or iel == 3
            if first and sw_marker and not self._fim_done:
                self._fim_done = True
                sts = (sts & ~FIDF) | FIMF | (FIDF if up else 0)
            if sw_marker:
                self.set("QPOSILAT", new)
                sts = (sts & ~QDLF) | (QDLF if up else 0)
                flags |= IEL
                if pcrm == 0:
                    err = new not in (0, mx)
                    sts = (sts & ~PCEF) | (PCEF if err else 0)
                    if err:
                        flags |= PCE
            if pcrm == 0 or (pcrm == 2 and not self._first_idx_done):
                self._first_idx_done = True
                new = 0 if up else mx
        self.set("QPOSCNT", new)
        flags |= self._compare(new)
        if self._ct_on:
            self._up_cnt += 1
            if self._up_cnt >= 1 << (self.get("QCAPCTL") & 0xF):
                self._up_cnt = 0
                sts = self._unit_position_event(sts)
        self.set("QEPSTS", sts)
        if flags:
            self._set_flags(flags)

    def _compare(self, cnt: int) -> int:
        pc = self.get("QPOSCTL")
        if not pc & PCE_EN:
            return 0
        flags = 0
        if cnt == self.get("QPOSCMP"):
            flags |= PCM
            self._pcs_pulse(pc)
            if pc & PCSHDW and pc & PCLOAD and self._cmp_pending:
                flags |= self._load_cmp()
        if cnt == 0 and pc & PCSHDW and not pc & PCLOAD and self._cmp_pending:
            flags |= self._load_cmp()
        return flags

    def _load_cmp(self) -> int:
        self.set("QPOSCMP", self._cmp_shadow)
        self._cmp_pending = False
        return PCR

    def _index_edge(self, rising: bool) -> None:
        ctl = self.get("QEPCTL")
        pcrm, iel, iei = (ctl >> 12) & 3, (ctl >> 4) & 3, (ctl >> 8) & 3
        if rising and (pcrm == 0 or (pcrm == 2 and not self._first_idx_done) or (pcrm != 0 and iel == 3)):
            self._idx_pending = True
        if pcrm != 0 and iel in (1, 2) and rising == (iel == 1):
            sts = self.get("QEPSTS")
            self.set("QPOSILAT", self.get("QPOSCNT"))
            self.set("QEPSTS", (sts & ~QDLF) | (QDLF if sts & QDF else 0))
            self._set_flags(IEL)
        if (iei == 2 and rising) or (iei == 3 and not rising):
            self.set("QPOSCNT", self.get("QPOSINIT"))

    def _strobe_edge(self, rising: bool) -> None:
        ctl = self.get("QEPCTL")
        fwd = bool(self.get("QEPSTS") & QDF)
        if (not ctl & SELB and rising) or (ctl & SELB and rising == fwd):
            self.set("QPOSSLAT", self.get("QPOSCNT"))
            self._set_flags(SEL)
        sei = (ctl >> 10) & 3
        if (sei == 2 and rising) or (sei == 3 and rising == fwd):
            self.set("QPOSCNT", self.get("QPOSINIT"))

    # ------------------------------------------------------------ timers
    def _cancel(self, attr: str) -> None:
        ev = getattr(self, attr)
        if ev is not None and self.chip is not None:
            self.chip.sched.cancel(ev)
        setattr(self, attr, None)

    def _update_timers(self) -> None:
        if self.chip is None:
            return
        ctl = self.get("QEPCTL")
        now = self._now()
        # unit timer
        want = self._clock and bool(ctl & UTE)
        if want != self._ut_on:
            if want:
                self._ut_on = True
                self._ut_c0, self._ut_v0 = now, self.get("QUTMR")
                self._ut_sched()
            else:
                self.set("QUTMR", self._ut_val(now))
                self._ut_on = False
                self._cancel("_ut_ev")
        # watchdog
        want = self._clock and bool(ctl & WDE)
        if want != self._wd_on:
            if want:
                self._wd_on = True
                self._wd_p0 = self._wd_c0 = now
                self._wd_v0 = self.get("QWDTMR")
                self._wd_sched()
            else:
                self.set("QWDTMR", self._wd_val(now))
                self._wd_on = False
                self._cancel("_wd_ev")
        # capture timer
        want = self._clock and bool(self.get("QCAPCTL") & CEN)
        if want != self._ct_on:
            if want:
                self._ct_on = True
                self._ct_p0 = self._ct_c0 = now
                self._ct_v0 = self.get("QCTMR")
                self._ct_ovf = False
                self._up_cnt = 0
            else:
                self.set("QCTMR", self._ct_val(now))
                self._ct_on = False

    # unit timer (SYSCLKOUT)
    def _ut_val(self, cyc: int) -> int:
        if not self._ut_on:
            return self.get("QUTMR")
        return _adv(self._ut_v0, cyc - self._ut_c0, self.get("QUPRD"), 1 << 32)

    def _ut_sched(self) -> None:
        self._cancel("_ut_ev")
        if not self._ut_on:
            return
        prd = self.get("QUPRD")
        if prd == 0 and not self._tiny_warned:
            self._tiny_warned = True
            self.emit("warn", block=self.name, msg=f"{self.name}: unit timer period QUPRD = 0: a unit time out "
                      "every cycle (slow to simulate)")
        t = self._ut_c0 + _to_match(self._ut_v0, prd, 1 << 32)
        self._ut_ev = self.chip.sched.at_cycles(t, self._ut_fire, t)

    def _ut_fire(self, t: int) -> None:
        self._ut_ev = None
        if not self._ut_on:
            return
        self._ut_c0, self._ut_v0 = t, self.get("QUPRD")      # QUTMR = QUPRD now, 0 on the next cycle
        self._unit_timeout(t)
        self._ut_sched()

    def _unit_timeout(self, t: int) -> None:
        ctl = self.get("QEPCTL")
        if ctl & QPEN:
            if ctl & QCLM:
                self.set("QPOSLAT", self.get("QPOSCNT"))
                self.set("QCTMRLAT", self._ct_val(t))
                self.set("QCPRDLAT", self.get("QCPRD"))
            if (ctl >> 12) & 3 == 3:
                self.set("QPOSCNT", self.get("QPOSMAX") if self.get("QDECCTL") >> 14 == 3 else 0)
        self._set_flags(UTO)

    # watchdog (SYSCLKOUT / 64)
    def _wd_ticks(self, a: int, b: int) -> int:
        p0 = self._wd_p0
        return (b - p0) // WD_DIV - (a - p0) // WD_DIV

    def _wd_val(self, cyc: int) -> int:
        if not self._wd_on:
            return self.get("QWDTMR")
        return _adv(self._wd_v0, self._wd_ticks(self._wd_c0, cyc), self.get("QWDPRD"), 1 << 16)

    def _wd_match_cycle(self) -> int:
        n = _to_match(self._wd_v0, self.get("QWDPRD"), 1 << 16)
        k = (self._wd_c0 - self._wd_p0) // WD_DIV + n
        return self._wd_p0 + WD_DIV * k

    def _wd_sched(self) -> None:
        self._cancel("_wd_ev")
        if self._wd_on:
            t = self._wd_match_cycle()
            self._wd_ev = self.chip.sched.at_cycles(t, self._wd_fire, t)

    def _wd_fire(self, t: int) -> None:
        self._wd_ev = None
        if not self._wd_on:
            return
        t2 = self._wd_match_cycle()
        if t2 > t:                                   # a QCLK cleared the timer meanwhile: not yet
            self._wd_ev = self.chip.sched.at_cycles(t2, self._wd_fire, t2)
            return
        self._wd_c0, self._wd_v0 = t, self.get("QWDPRD")
        self._set_flags(WTO)
        self._wd_sched()

    # capture timer (SYSCLKOUT / 2^CCPS)
    def _ct_ticks(self, cyc: int) -> int:
        sh = (self.get("QCAPCTL") >> 4) & 7
        p0 = self._ct_p0
        return ((cyc - p0) >> sh) - ((self._ct_c0 - p0) >> sh)

    def _ct_total(self, cyc: int) -> int:
        return self._ct_v0 + self._ct_ticks(cyc)

    def _ct_val(self, cyc: int) -> int:
        if not self._ct_on:
            return self.get("QCTMR")
        return self._ct_total(cyc) & 0xFFFF

    def _unit_position_event(self, sts: int) -> int:
        now = self._now()
        total = self._ct_total(now)
        if total > 0xFFFF:
            sts |= COEF
        if self._cap_dirchg:
            sts |= CDEF
        self._cap_dirchg = False
        self.set("QCPRD", total & 0xFFFF)
        self._ct_c0, self._ct_v0 = now, 0
        self._ct_ovf = False
        return sts | UPEVNT

    # ------------------------------------------------------------ interrupts
    def _set_flags(self, bits: int) -> None:
        self.set("QFLG", self.get("QFLG") | bits)
        self._eval_int()

    def _eval_int(self) -> None:
        flg = self.get("QFLG")
        if not flg & INT and flg & self.get("QEINT") & EVENT_FLAGS:
            flg |= INT
            self.set("QFLG", flg)
        lvl = bool(flg & INT)
        if lvl != self._int_lvl:
            self._int_lvl = lvl
            if self._int_line is not None:
                self.irq(self._int_line, lvl)

    # ------------------------------------------------------------ register access
    def read(self, offset: int, size: int) -> int:
        if offset == 0 and self.chip is not None and not self.get("QEPCTL") & QCLM:
            self.set("QCTMRLAT", self._ct_val(self._now()))       # CPU read of QPOSCNT latches the capture unit
            self.set("QCPRDLAT", self.get("QCPRD"))
        return C2000Block.read(self, offset, size)

    def write(self, offset: int, size: int, value: int) -> None:
        if self.chip is not None:                       # 16-bit writes merge with the live timer values
            if offset in (0x0E, 0x0F) and self._ut_on:
                self.set("QUTMR", self._ut_val(self._now()))
            elif offset == 0x12 and self._wd_on:
                self.set("QWDTMR", self._wd_val(self._now()))
            elif offset == 0x1D and self._ct_on:
                self.set("QCTMR", self._ct_val(self._now()))
        if offset in (0x06, 0x07) and self.get("QPOSCTL") & PCSHDW:
            self._cmp_active = self.get("QPOSCMP")
            self.set("QPOSCMP", self._cmp_shadow)       # a 16-bit write merges with the shadow value
        C2000Block.write(self, offset, size, value)

    def _qposcmp_write(self, blk, reg, old, new):
        self._cmp_shadow = new
        if self.get("QPOSCTL") & PCSHDW:
            self._cmp_pending = True
            return self._cmp_active
        self._cmp_pending = False
        return None

    def _qutmr_read(self, blk, reg):
        return self._ut_val(self._now()) if self._ut_on else None

    def _qutmr_write(self, blk, reg, old, new):
        if self._ut_on:
            self._ut_c0, self._ut_v0 = self._now(), new
            self._ut_sched()
        return None

    def _qupr_write(self, blk, reg, old, new):
        if self._ut_on and old != new:
            now = self._now()
            self._store(reg, old)
            v = self._ut_val(now)                       # the count so far, under the old period
            self._store(reg, new)
            self._ut_c0, self._ut_v0 = now, v
            self._ut_sched()
        return None

    def _qwdtmr_read(self, blk, reg):
        return self._wd_val(self._now()) if self._wd_on else None

    def _qwdtmr_write(self, blk, reg, old, new):
        if self._wd_on:
            self._wd_c0, self._wd_v0 = self._now(), new
            self._wd_sched()
        return None

    def _qwdprd_write(self, blk, reg, old, new):
        if self._wd_on and old != new:
            now = self._now()
            self._store(reg, old)
            v = self._wd_val(now)
            self._store(reg, new)
            self._wd_c0, self._wd_v0 = now, v
            self._wd_sched()
        return None

    def _qctmr_read(self, blk, reg):
        return self._ct_val(self._now()) if self._ct_on else None

    def _qctmr_write(self, blk, reg, old, new):
        if self._ct_on:
            self._ct_c0, self._ct_v0 = self._now(), new
            self._ct_ovf = False
        return None

    def _qdecctl_write(self, blk, reg, old, new):
        self._store(reg, new)
        self._qi, self._qs = self._eff_is()
        if (old ^ new) & (SOEN | SPSEL):
            self._pcs_resync()
        return None

    def _qposctl_write(self, blk, reg, old, new):
        if (old ^ new) & PCPOL:
            self._store(reg, new)
            self._pcs_resync()
        return None

    # ------------------------------------------------------------ position-compare sync output (SPRUI07 6.4.3)
    def owned_signals(self):
        return (self.signals["I"], self.signals["S"])

    def _sync_key(self) -> str | None:
        dc = self.get("QDECCTL")
        if not dc & SOEN:
            return None
        return "S" if dc & SPSEL else "I"

    def signal_state(self, sig: str):
        """EQEPxI / EQEPxS: an output carrying PCSOUT while QDECCTL.SOEN selects it (SPSEL), else the static input."""
        k = self._sync_key()
        if k is not None and sig == self.signals[k]:
            return ("out", "H" if self._pcs_active != bool(self.get("QPOSCTL") & PCPOL) else "L")
        return None

    def _pcs_resync(self) -> None:
        if self.chip is None:
            return
        gpio_resync(self.chip, self.signals["I"])
        gpio_resync(self.chip, self.signals["S"])

    def _pcs_pulse(self, pc: int) -> None:
        """Position-compare match with SOEN: PCSOUT active for 4 x (PCSPW + 1) SYSCLKOUT; a new match while active
        restarts the width (pulse stretcher, Figure 6-14)."""
        if self._sync_key() is None or self.chip is None:
            return
        sched = self.chip.sched
        if self._pcs_ev is not None:
            sched.cancel(self._pcs_ev)
        self._pcs_ev = sched.in_cycles(4 * ((pc & 0xFFF) + 1), self._pcs_end)
        self.pcs_pulses += 1
        if not self._pcs_active:
            self._pcs_active = True
            gpio_resync(self.chip, self.signals[self._sync_key()])

    def _pcs_end(self) -> None:
        self._pcs_ev = None
        self._pcs_active = False
        k = self._sync_key()
        if k is not None:
            gpio_resync(self.chip, self.signals[k])

    def _qepctl_write(self, blk, reg, old, new):
        self._store(reg, new)
        if old & QPEN and not new & QPEN:              # software reset of the operating flags / latches
            for r in ("QEPSTS", "QPOSILAT", "QPOSSLAT", "QPOSLAT", "QCTMRLAT", "QCPRDLAT"):
                self.set(r, 0)
            self._internal_reset()
        if new & SWI:
            self.set("QPOSCNT", self.get("QPOSINIT"))
        if (old ^ new) & (UTE | WDE):
            self._update_timers()
        return None

    def _qcapctl_write(self, blk, reg, old, new):
        if self._ct_on and (old ^ new) & (7 << 4):      # new prescaler: re-anchor at the current count
            now = self._now()
            self._store(reg, old)
            v = self._ct_total(now)
            self._store(reg, new)
            self._ct_p0 = self._ct_c0 = now
            self._ct_v0 = v
        else:
            self._store(reg, new)
        if (old ^ new) & 0xF:
            self._up_cnt = 0
        if (old ^ new) & CEN:
            self._update_timers()
        return None

    def _qepsts_read(self, blk, reg):
        if self._ct_on and not self._ct_ovf and self._ct_total(self._now()) > 0xFFFF:
            self._ct_ovf = True                         # the capture timer wrapped: COEF (once per interval)
            self.set("QEPSTS", self.get("QEPSTS") | COEF)
            return self.get("QEPSTS")
        return None

    def _qeint_write(self, blk, reg, old, new):
        self._store(reg, new)
        self._eval_int()
        return None

    def _qclr_write(self, blk, reg, old, new):
        flg = self.get("QFLG") & ~(new & 0x0FFF)
        self.set("QFLG", flg)
        if not flg & INT and self._int_lvl:    # end of this interrupt: the line drops ...
            self._int_lvl = False
            if self._int_line is not None:
                self.irq(self._int_line, False)
        self._eval_int()                       # ... and enabled flags still set raise another one
        return 0

    def _qfrc_write(self, blk, reg, old, new):
        if new & EVENT_FLAGS:
            self._set_flags(new & EVENT_FLAGS)
        return 0
