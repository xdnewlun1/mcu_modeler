"""Kinetis FlexTimer (FTM0-FTM3 on the K22F; K22P100M120SF5V2RM chapter 40, "FlexTimer Module").

Counter (16 bit, FTM clock = SC.CLKS: 01 system (bus) clock, 10 fixed frequency clock MCGFFCLK, 11 external clock
(not modelled: no FTM_CLKINx, one `warn`), divided by 2^SC.PS):
* up counting (SC.CPWMS=0): CNTIN .. MOD, then CNTIN again; TOF when the counter goes MOD -> CNTIN.
* up-down counting (CPWMS=1): CNTIN up to MOD and down to CNTIN; TOF when it goes MOD -> MOD-1.
* CNT reads are live; any CNT write resets the counter to CNTIN.

Channels (n = 0..7; pairs m = n/2 for COMBINE/DEADTIME):
* output compare (MSnB:MSnA=01): on CnV match toggle / clear / set (ELSnB:ELSnA 01/10/11); 00 = software compare
  (flag only).
* edge-aligned PWM (MSnB=1): high-true (ELSnB:ELSnA=10) high from CNTIN while CNT < CnV, low-true (X1) the
  inverse; CnV <= CNTIN gives 0 %, CnV > MOD 100 %.
* center-aligned PWM (CPWMS=1, ELS != 00): high-true is cleared at the match counting up and set at the match
  counting down (high while CNT < CnV up, CNT <= CnV down).
* combine (COMBINEm): channel 2m is set at C(2m)V and cleared at C(2m+1)V (high-true), low at the start of the period;
  channel 2m+1 is the same, or its complement with COMPm. Dead time (DTENm): each rising edge of the pair is
  delayed by DEADTIME (DTVAL x 1/4/16 system-clock cycles), a pulse shorter than that disappears.
* CHnF at every match (also of channels without an output) and the FTM IRQ for TOF&TOIE | CHnF&CHnIE.
  TOF / CHnF / STATUS clear by a read with the flag set followed by a write of 0 (the documented sequence).
* output path: mode -> dead time -> software output control (SWOCTRL) -> POL (inverts) -> OUTMASK (forces the
  inactive state, POLn) -> the pin owned by FTMx_CHn (PinBoard.drive_signal). MODE.INIT loads the channel outputs
  from OUTINIT. ELSnB:ELSnA = 00 (and not combine) leaves the pin at its idle level.
* register updates (40.4.10/40.4.11): with the counter clock off MOD/CNTIN/CnV take effect at once. FTMEN=0: CnV of
  output compare at once; MOD, CNTIN and PWM CnV at the next counter overflow (EPWM: MOD -> CNTIN; CPWM: MOD ->
  MOD-1). FTMEN=1: from the write buffers at the loading point after a software sync (SYNC.SWSYNC; CNTMAX: counter =
  MOD, CNTMIN: counter = CNTIN, neither: the end of the period; SYNC.REINIT also restarts the counter) or at every
  loading point while PWMLOAD.LDOK is set.

Not modelled: input capture / dual edge capture / quadrature decoder (stored only, one `warn`), fault inputs,
hardware triggers (TRIGn, EXTTRIG to the ADC/PDB), INVCTRL, DMA requests, write protection (FMS.WPEN/MODE.WPDIS),
external clock, debug mode, the half-cycle reload; the counter is one 16-bit counter of the FTM clock.
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ._util import GatedBlock, build_regs, find_model
from .clockutil import bus_hz, irq_number

TOF, TOIE, CPWMS = 1 << 7, 1 << 6, 1 << 5
CHF, CHIE, MSB, MSA, ELSB, ELSA, DMA = 1 << 7, 1 << 6, 1 << 5, 1 << 4, 1 << 3, 1 << 2, 1 << 0
FTMEN, INIT = 1 << 0, 1 << 1
SYNC_CNTMIN, SYNC_CNTMAX, SYNC_REINIT, SYNC_SWSYNC = 1 << 0, 1 << 1, 1 << 2, 1 << 7
LDOK = 1 << 9
DTPS_FACTOR = (1, 1, 4, 16)


class _Ch:
    __slots__ = ("n", "v", "kind", "lowtrue", "oc", "ie", "active", "ref", "target", "applied", "ev", "dt_ev",
                 "out", "lazy_k", "armed")

    def __init__(self, n: int):
        self.n = n
        self.v = 0               # active CnV
        self.kind = "off"        # off | sw | oc | ic | epwm | cpwm | comb | comb2
        self.lowtrue = False
        self.oc = 0              # output compare action: 0 none, 1 toggle, 2 clear, 3 set
        self.ie = False          # CHIE or DMA: matches need their exact time
        self.active = False      # needs match events (drives a pin or interrupts)
        self.ref = False         # mode output (high-true sense, before dead time/POL)
        self.target = False      # after the mode's polarity, before dead time
        self.applied = False     # after dead time
        self.ev = None           # next match event
        self.dt_ev = None        # pending dead-time rise
        self.out: str | None = None      # level driven on the pin signal (None: not driving)
        self.lazy_k = -1         # quiet channel: last match position already reflected in CHnF
        self.armed = False       # CHnF read as 1 (a following write of 0 clears it)


@register("kinetis.ftm")
class FTM(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = params.get("gate", name)
        ov: dict[str, dict] = {
            # TOF / CHnF are read-only in the SVD; they clear by "read 1, then write 0", done in the hooks
            "SC": {"on_read": self._sc_read, "on_write": self._sc_write, "ro_mask": 0xFFFFFF00},
            "CNT": {"on_read": self._cnt_read, "on_write": self._cnt_write},
            "MOD": {"on_write": self._buf_write}, "CNTIN": {"on_write": self._buf_write},
            "STATUS": {"on_read": self._status_read, "on_write": self._status_write},
            "MODE": {"on_write": self._mode_write},
            "SYNC": {"on_write": self._sync_write},
            "COMBINE": {"on_write": self._cfg_write}, "POL": {"on_write": self._cfg_write},
            "OUTMASK": {"on_write": self._cfg_write}, "SWOCTRL": {"on_write": self._cfg_write},
            "DEADTIME": {"on_write": self._cfg_write}, "QDCTRL": {"on_write": self._qd_write},
            "PWMLOAD": {"on_write": self._cfg_write},
        }
        for n in range(8):
            ov[f"C{n}SC"] = {"on_read": self._csc_read(n), "on_write": self._csc_write(n), "ro_mask": 0xFFFFFF02}
            ov[f"C{n}V"] = {"on_write": self._buf_write}
        regs_ = build_regs((name, "FTM0"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, regs_, log=log, params=params)
        self.ch = [_Ch(n) for n in range(8) if f"C{n}SC" in self.regs]
        self._ev = None              # next boundary event (mid: TOF of up-down counting; end of the period)
        self._irqn: int | None = None
        self._line = False
        self.running = False
        self.mod = self.cntin = 0    # active values (write buffers: the MOD/CNTIN registers)
        self._start_ns = 0.0         # anchor time
        self._p0 = 0                 # position in the period (ticks since CNTIN counting up) at the anchor
        self._tick_ns = 0.0
        self._pending = False        # write buffers differ from the active values
        self._sync = False           # software sync requested (SWSYNC), loads at the next loading point
        self._tof_armed = False
        self._status_armed = 0
        self._quiet: list[_Ch] = []
        self._warned: set[str] = set()
        self.overflows = 0

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, str(self.params.get("irq") or self.name))
        mcg = find_model(chip, "kinetis.mcg", "MCG")
        if mcg is not None and hasattr(mcg, "listeners"):
            mcg.listeners.append(self._clock_changed)

    def reset(self) -> None:
        if getattr(self, "ch", None) is not None:
            self._stop()
            for c in self.ch:
                self._cancel_dt(c)
                c.ref = c.target = c.applied = False
                c.v, c.lazy_k, c.armed = 0, -1, False
                if c.out is not None and self.chip is not None and self.chip.pins is not None:
                    self.chip.pins.release_signal(self._sig(c), "L")
                c.out = None
            if self._line and self.chip is not None and self._irqn is not None:
                self.irq(self._irqn, False)
        self._line = False
        super().reset()
        if getattr(self, "ch", None) is None:
            return
        self.mod = self.cntin = 0
        self._p0 = 0
        self._pending = self._sync = self._tof_armed = False
        self._status_armed = 0
        self._chan_cfg()

    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()
        MMIOBlock.write(self, offset, size, value)

    def _sig(self, c: _Ch) -> str:
        return f"{self.name}_CH{c.n}"

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=f"{self.name}: {msg}")

    # ------------------------------------------------------------ clock and counting geometry
    def counter_hz(self) -> float:
        sc = self.get("SC")
        clks = (sc >> 3) & 3
        if clks == 1:
            hz = bus_hz(self.chip)
        elif clks == 2:
            mcg = find_model(self.chip, "kinetis.mcg", "MCG")
            hz = mcg.ffclk_hz() if mcg is not None and hasattr(mcg, "ffclk_hz") else 32768.0
        elif clks == 3:
            self._warn_once("extclk", "external clock (SC.CLKS=11, FTM_CLKINx) is not modelled; the counter stops")
            return 0.0
        else:
            return 0.0
        return hz / (1 << (sc & 7))

    def _cpwm(self) -> bool:
        return bool(self.mem[0] & CPWMS)             # SC is at offset 0

    def _m(self) -> int:
        return max((self.mod - self.cntin) & 0xFFFF, 0) if self.mod >= self.cntin else 0

    def _period(self) -> int:
        m = self._m()
        return max(2 * m, 1) if self._cpwm() else m + 1

    def _counter(self, p: int) -> tuple[int, bool]:
        """(counter value, counting down) at position p of the period."""
        m = self._m()
        if self._cpwm() and p > m:
            return (self.cntin + 2 * m - p) & 0xFFFF, True
        return (self.cntin + p) & 0xFFFF, False

    def _boundaries(self) -> list[tuple[int, str]]:
        """Positions of the period's boundary events: (M+1, 'mid') when up-down counting (TOF, MOD -> MOD-1) and
        (period, 'end') (up counting: TOF, MOD -> CNTIN; up-down: back at CNTIN)."""
        p = self._period()
        if self._cpwm() and self._m() > 0:
            return [(self._m() + 1, "mid"), (p, "end")]
        return [(p, "end")]

    def _pos_now(self) -> float:
        if not self.running or self._tick_ns <= 0:
            return float(self._p0)
        return self._p0 + (self.chip.now_ns() - self._start_ns) / self._tick_ns

    def _t(self, pos: int) -> float:
        return self._start_ns + (pos - self._p0) * self._tick_ns

    # ------------------------------------------------------------ start / stop / anchor
    def _stop(self) -> None:
        if self._quiet and self.running:
            self._lazy_flags(self._pos_now())
        self._quiet = []
        if self._ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None
        for c in getattr(self, "ch", ()):
            if c.ev is not None and self.chip is not None:
                self.chip.sched.cancel(c.ev)
            c.ev = None
        if self.running:
            self.set("CNT", self._counter(int(self._pos_now() + 1e-6) % max(self._period(), 1))[0])
        self.running = False

    def _start(self, p0: int, t_ns: float | None = None) -> None:
        """(Re)anchor the counter at period position p0 at time t_ns (default now) and schedule what follows."""
        self._stop()
        hz = self.counter_hz() if self.chip is not None else 0.0
        if hz <= 0:
            self.set("CNT", self._counter(p0)[0])
            return
        self.running = True
        self._tick_ns = 1e9 / hz
        self._start_ns = self.chip.now_ns() if t_ns is None else t_ns
        self._p0 = p0
        self._schedule_boundary(p0)
        self._resync(start=True)

    def _schedule_boundary(self, after: float) -> None:
        for pos, kind in self._boundaries():
            if pos > after + 1e-6:
                self._ev = self.chip.sched.at_ns(self._t(pos), self._boundary, kind, pos)
                return

    def _clock_changed(self) -> None:
        if self.running:
            self._start(int(self._pos_now() + 1e-6) % max(self._period(), 1))

    # ------------------------------------------------------------ boundaries, loading, TOF
    def _boundary(self, kind: str, pos: int) -> None:
        self._ev = None
        if self._quiet:
            self._lazy_flags(None)
        t = self._t(pos)
        cpwm = self._cpwm()
        sync = self.get("SYNC")
        ftmen = self.get("MODE") & FTMEN
        if kind == "mid" or not cpwm:                    # TOF: MOD -> MOD-1 (up-down) / MOD -> CNTIN (up)
            self.overflows += 1
            self.set("SC", self.get("SC") | TOF)
            self._update_line()
        load = False
        if self._pending or self._sync:
            if not ftmen:
                load = (kind == "mid") if cpwm else True
            elif self._sync or self.get("PWMLOAD") & LDOK:
                pts = sync & (SYNC_CNTMIN | SYNC_CNTMAX)
                if not pts:
                    load = kind == "end"
                elif kind == "mid":
                    load = bool(sync & SYNC_CNTMAX)
                else:
                    load = bool(sync & SYNC_CNTMIN) or (not cpwm and bool(sync & SYNC_CNTMAX))
        if kind == "end":
            if load:
                self._load()
            self._start(0, t)
            return
        if load:                                         # mid-period load (up-down): keep counting down from here
            cnt = self._counter(pos)[0]
            self._load()
            m = self._m()
            self._start(min(max(2 * m - ((cnt - self.cntin) & 0xFFFF), m + 1), max(2 * m - 1, 0)), t)
            return
        self._schedule_boundary(pos)

    def _load(self) -> None:
        self.mod = self.get("MOD") & 0xFFFF
        self.cntin = self.get("CNTIN") & 0xFFFF
        for c in self.ch:
            c.v = self.get(f"C{c.n}V") & 0xFFFF
        self._pending = False
        if self._sync:
            self._sync = False
            self.set("SYNC", self.get("SYNC") & ~SYNC_SWSYNC)

    # ------------------------------------------------------------ channel configuration
    def _chan_cfg(self) -> None:
        comb = self.get("COMBINE")
        cpwm = self._cpwm()
        for c in self.ch:
            sc = self.get(f"C{c.n}SC")
            els = (sc >> 2) & 3
            ms = (sc >> 4) & 3
            m = c.n // 2
            pair = (comb >> (8 * m)) & 0xFF
            c.ie = bool(sc & (CHIE | DMA))
            c.oc = 0
            if pair & 0x04 or pair & 0x08 or self.get("QDCTRL") & 1:     # dual edge capture, quadrature decoder
                c.kind = "ic"
            elif pair & 0x01 and not cpwm:                               # COMBINEm
                if c.n % 2 == 0:
                    c.kind, c.lowtrue = "comb", bool(els & 1)
                else:
                    c.kind, c.lowtrue = "comb2", False
            elif cpwm:
                c.kind, c.lowtrue = ("cpwm" if els else "off"), bool(els & 1)
            elif ms == 0:
                c.kind = "ic" if els else "off"
            elif ms == 1:
                c.kind, c.oc = ("oc" if els else "sw"), els
            else:
                c.kind, c.lowtrue = ("epwm" if els else "sw"), bool(els & 1)
            if c.kind == "ic" and els:
                self._warn_once("ic", "input capture / dual edge capture / quadrature decoder are not modelled")
            c.active = c.kind not in ("off", "ic") and (c.ie or self._drives(c))

    def _drives(self, c: _Ch) -> bool:
        if c.kind in ("oc", "epwm", "cpwm", "comb"):
            return True
        if c.kind == "comb2":
            return bool((self.get(f"C{c.n}SC") >> 2) & 3)
        return False

    def _cfg_write(self, blk, reg, old, new):
        self._store(reg, new)
        self._config_changed()
        return None

    def _qd_write(self, blk, reg, old, new):
        self._store(reg, new)
        if new & 1:
            self._warn_once("qd", "quadrature decoder mode (QDCTRL.QUADEN) is not modelled")
        self._config_changed()
        return None

    def _config_changed(self) -> None:
        if self._quiet and self.running:
            self._lazy_flags(self._pos_now())
        self._chan_cfg()
        self._resync(start=False)

    # ------------------------------------------------------------ matches and outputs
    def _match_positions(self, v: int) -> list[int]:
        """Positions in the period where the counter equals v (each counts as a match)."""
        m = self._m()
        d = (v - self.cntin) & 0xFFFF
        if v < self.cntin or d > m:
            return []
        if self._cpwm():
            return sorted({d, 2 * m - d} - {2 * m} if m else {0})
        return [d]

    def _value_of(self, c: _Ch) -> int:
        return c.v

    def _mode_ref(self, c: _Ch, cnt: int, down: bool) -> bool | None:
        """Mode output (high-true sense) derived from the counter, or None for modes that only change at matches."""
        if c.kind == "epwm":
            return cnt < c.v
        if c.kind == "cpwm":
            return cnt <= c.v if down else cnt < c.v
        if c.kind == "comb":
            v1 = self.ch[c.n + 1].v if c.n + 1 < len(self.ch) else 0
            return c.v <= cnt < v1
        return None

    def _resync(self, start: bool) -> None:
        """Re-evaluate the PWM/combine outputs from the counter now and (re)schedule the next match of every
        active channel; quiet channels (no pin, no IRQ/DMA) get their CHnF when read."""
        pos = self._pos_now() if self.running else float(self._p0)
        ipos = int(pos + 1e-6)
        cnt, down = self._counter(ipos % max(self._period(), 1))
        if not self.running:
            cnt = self.get("CNT") & 0xFFFF
        kmin = self._p0 if start else ipos + 1          # a match exactly now has already been handled
        quiet = []
        for c in self.ch:
            if c.ev is not None:
                self.chip.sched.cancel(c.ev)
                c.ev = None
            r = self._mode_ref(c, cnt, down)
            if r is not None:
                c.ref = r
        for c in self.ch:
            self._apply(c)
            if not self.running or c.kind in ("off", "ic"):
                continue
            if c.active:
                self._schedule_match(c, kmin)
            else:
                quiet.append(c)
                c.lazy_k = kmin - 1
        self._quiet = quiet

    def _schedule_match(self, c: _Ch, kmin: int) -> None:
        for k in self._match_positions(c.v):
            if k >= kmin:
                if k == self._p0 and k == kmin and c.kind in ("epwm", "comb", "comb2"):
                    if k == 0:
                        self._match(c, k, False)        # CnV = CNTIN: match at the start of the period
                    continue
                c.ev = self.chip.sched.at_ns(self._t(k), self._match, c, k, True)
                return

    def _match(self, c: _Ch, k: int, resched: bool) -> None:
        c.ev = None
        self.set(f"C{c.n}SC", self.get(f"C{c.n}SC") | CHF)
        self._update_line()
        cnt, down = self._counter(k)
        if c.kind == "oc":
            c.ref = {1: not c.ref, 2: False, 3: True}.get(c.oc, c.ref)
        elif c.kind in ("epwm", "cpwm"):
            c.ref = (cnt <= c.v if down else cnt < c.v) if c.kind == "cpwm" else False
        elif c.kind == "comb":
            c.ref = True                                     # set at C(2m)V
        elif c.kind == "comb2" and c.n > 0:
            self.ch[c.n - 1].ref = False                     # cleared at C(2m+1)V
            self._apply(self.ch[c.n - 1])
        self._apply(c)
        if resched and self.running:
            self._schedule_match(c, k + 1)

    def _deadtime_ns(self) -> float:
        dt = self.get("DEADTIME")
        n = (dt & 0x3F) * DTPS_FACTOR[(dt >> 6) & 3]
        hz = bus_hz(self.chip)
        return n * 1e9 / hz if n and hz > 0 else 0.0

    def _cancel_dt(self, c: _Ch) -> None:
        if c.dt_ev is not None and self.chip is not None:
            self.chip.sched.cancel(c.dt_ev)
        c.dt_ev = None

    def _apply(self, c: _Ch) -> None:
        """Mode output -> (pair) -> dead time -> SWOCTRL -> POL -> OUTMASK -> pin."""
        if self.chip is None or self.chip.pins is None:
            return
        if c.kind == "comb2" and c.n > 0:
            lead = self.ch[c.n - 1]
            comp = (self.get("COMBINE") >> (8 * (c.n // 2))) & 0x02
            tgt = (not lead.target) if comp else lead.target
        else:
            tgt = c.ref ^ c.lowtrue
        c.target = tgt
        dten = (self.get("COMBINE") >> (8 * (c.n // 2))) & 0x10
        if dten and tgt and not c.applied:
            dt = self._deadtime_ns()
            if dt > 0:
                if c.dt_ev is None:
                    c.dt_ev = self.chip.sched.at_ns(self.chip.now_ns() + dt, self._dt_rise, c)
                self._apply_pair_follower(c)
                return
        self._cancel_dt(c)
        c.applied = tgt
        self._drive(c)
        self._apply_pair_follower(c)

    def _apply_pair_follower(self, c: _Ch) -> None:
        if c.kind == "comb" and c.n + 1 < len(self.ch) and self.ch[c.n + 1].kind == "comb2":
            self._apply(self.ch[c.n + 1])

    def _dt_rise(self, c: _Ch) -> None:
        c.dt_ev = None
        if c.target:
            c.applied = True
            self._drive(c)

    def _drive(self, c: _Ch) -> None:
        pins = self.chip.pins
        if not self._drives(c) and not (self.get("SWOCTRL") >> c.n) & 1:
            if c.out is not None:
                pins.release_signal(self._sig(c), "L")
                c.out = None
            return
        lvl = c.applied
        sw = self.get("SWOCTRL")
        if (sw >> c.n) & 1:
            lvl = bool((sw >> (8 + c.n)) & 1)
        pol = bool((self.get("POL") >> c.n) & 1)
        lvl = lvl ^ pol
        if (self.get("OUTMASK") >> c.n) & 1:
            lvl = pol                                       # inactive state
        v = "H" if lvl else "L"
        if c.out != v:
            c.out = v
            pins.drive_signal(self._sig(c), v)

    # ------------------------------------------------------------ flags and IRQ
    def _lazy_flags(self, upto: float | None) -> None:
        sc_changed = False
        for c in self._quiet:
            for k in self._match_positions(c.v):
                if k > c.lazy_k and k >= self._p0 and (upto is None or k <= upto + 1e-6):
                    c.lazy_k = k
                    self.set(f"C{c.n}SC", self.get(f"C{c.n}SC") | CHF)
                    sc_changed = True
        if sc_changed:
            self._update_line()

    def _update_line(self) -> None:
        lvl = bool(self.get("SC") & TOF and self.get("SC") & TOIE)
        if not lvl:
            for c in self.ch:
                sc = self.get(f"C{c.n}SC")
                if sc & CHF and sc & CHIE:
                    lvl = True
                    break
        if lvl != self._line:
            self._line = lvl
            if self._irqn is not None and self.chip is not None:
                self.irq(self._irqn, lvl)

    # ------------------------------------------------------------ register hooks
    def _sc_read(self, blk, reg):
        v = self.get("SC")
        if v & TOF:
            self._tof_armed = True
        return None

    def _sc_write(self, blk, reg, old, new):
        tof = old & TOF
        if not new & TOF and self._tof_armed:
            tof = 0                                         # read-1-then-write-0 clears TOF
        self._tof_armed = False
        new = (new & ~TOF) | tof
        self._store(reg, new)
        if (old ^ new) & (0x1F | CPWMS):                    # CLKS / PS / CPWMS
            if self.running:
                pos = int(self._pos_now() + 1e-6) % max(self._period(), 1)
                cnt = self._counter(pos)[0] if not (old ^ new) & CPWMS else self.get("CNT")
            else:
                cnt = self.get("CNT") & 0xFFFF
            if not self.running:
                self._load()                                # clock off: the buffers are the values
            p = (cnt - self.cntin) & 0xFFFF
            p = p if p <= self._m() else 0
            self._chan_cfg()
            self._start(p)
        else:
            self._chan_cfg()
            self._resync(start=False)
        self._update_line()
        return new

    def _cnt_read(self, blk, reg):
        if not self.running:
            return None
        return self._counter(int(self._pos_now() + 1e-6) % max(self._period(), 1))[0]

    def _cnt_write(self, blk, reg, old, new):
        self._store(reg, self.cntin)
        if self.running:
            self._start(0)
        return self.cntin

    def _buf_write(self, blk, reg, old, new):
        name = reg.name
        if not self.running:
            self._load()                                    # counter clock off: takes effect at once
            self._resync(start=False)
            return None
        if name.startswith("C") and name.endswith("V") and not self.get("MODE") & FTMEN:
            c = self.ch[int(name[1])] if int(name[1]) < len(self.ch) else None
            if c is not None and c.kind in ("oc", "sw", "off", "ic"):
                if self._quiet:
                    self._lazy_flags(self._pos_now())
                c.v = new & 0xFFFF                          # output compare: at once
                self._resync(start=False)
                return None
        self._pending = True
        return None

    def _csc_read(self, n: int):
        def rd(blk, reg):
            if self._quiet and self.running:
                self._lazy_flags(self._pos_now())
            v = self.get(f"C{n}SC")
            if v & CHF and n < len(self.ch):
                self.ch[n].armed = True
            return None if not self._quiet else v
        return rd

    def _csc_write(self, n: int):
        def wr(blk, reg, old, new):
            chf = old & CHF
            c = self.ch[n] if n < len(self.ch) else None
            if c is not None:
                if not new & CHF and c.armed:
                    chf = 0
                c.armed = False
            new = (new & ~CHF) | chf
            self._store(reg, new)
            if (old ^ new) & ~CHF & 0xFF:
                self._config_changed()
            self._update_line()
            return new
        return wr

    def _status_read(self, blk, reg):
        if self._quiet and self.running:
            self._lazy_flags(self._pos_now())
        v = 0
        for c in self.ch:
            if self.get(f"C{c.n}SC") & CHF:
                v |= 1 << c.n
        self._status_armed = v
        self._store(reg, v)
        return v

    def _status_write(self, blk, reg, old, new):
        clear = self._status_armed & ~new
        self._status_armed = 0
        for c in self.ch:
            if clear >> c.n & 1:
                self.set(f"C{c.n}SC", self.get(f"C{c.n}SC") & ~CHF)
        self._update_line()
        v = 0
        for c in self.ch:
            if self.get(f"C{c.n}SC") & CHF:
                v |= 1 << c.n
        return v

    def _mode_write(self, blk, reg, old, new):
        if new & INIT:                                      # initialize the channel outputs from OUTINIT
            oi = self.get("OUTINIT")
            for c in self.ch:
                self._cancel_dt(c)
                c.ref = bool(oi >> c.n & 1) ^ c.lowtrue
                c.applied = c.ref ^ c.lowtrue
            for c in self.ch:
                self._apply(c)
        return new & ~INIT

    def _sync_write(self, blk, reg, old, new):
        self._store(reg, new)
        if new & SYNC_SWSYNC:
            self._sync = True
            if new & SYNC_REINIT and self.running:          # software trigger with REINIT: restart at CNTIN now
                self._load()
                self._start(0)
            elif not self.running:
                self._load()
                self._resync(start=False)
        return None


@register("kinetis.ftm_alias")
class FTMAlias(MMIOBlock):
    """A second address window of an FTM (K22: FTM2 also at 0x4003A000): every access goes to `params.target`."""

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, ram_like=True, log=log, params=params)
        self._target: MMIOBlock | None = None

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._target = chip.peripherals.get(str(self.params.get("target", "")))

    def read(self, offset: int, size: int) -> int:
        t = self._target
        return t.read(offset, size) if t is not None else MMIOBlock.read(self, offset, size)

    def write(self, offset: int, size: int, value: int) -> None:
        t = self._target
        if t is not None:
            t.write(offset, size, value)
        else:
            MMIOBlock.write(self, offset, size, value)
