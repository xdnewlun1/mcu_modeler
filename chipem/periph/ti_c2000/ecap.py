"""C2833x enhanced capture module eCAP1-4 (TMS320x2833x/2823x eCAP reference guide, SPRUFG4A).

Register map (SPRUFG4A Table 13, 16-bit words): TSCTR 0x00, CTRPHS 0x02, CAP1-CAP4 0x04/0x06/0x08/0x0A (32 bits,
low word first), ECCTL1 0x14, ECCTL2 0x15, ECEINT 0x16, ECFLG 0x17, ECCLR 0x18, ECFRC 0x19.  None of them is
EALLOW-protected (SPRUFG4A lists no protection; TI's examples write them after EDIS).

Time base: TSCTR is a 32-bit up-counter at SYSCLKOUT (exact core cycles, computed from an anchor, never stepped).
It runs while ECCTL2.TSCTRSTOP = 1 and PCLKCR1.ECAPxENCLK is set (sysctrl `pclk_listeners`).  FFFFFFFF -> 0 sets
ECFLG.CTROVF (one scheduled event per wrap).  Sync: SYNCI (ePWM1 SYNCO for eCAP1 via the ePWM block's
`sync_listeners`, else the previous eCAP's SYNCO) and ECCTL2.SWSYNC load TSCTR from CTRPHS when SYNCI_EN; SYNCO per
SYNCO_SEL (00 pass SYNCI / SWSYNC through, 01 CTR=PRD in APWM mode, 1x off) feeds the next eCAP.

Capture mode (ECCTL2.CAP_APWM = 0): level changes of a pin muxed to ECAPx (PinBoard input listeners, timestamped
with the current core cycle) pass the event prescaler (ECCTL1.PRESCALE: 0 = bypass, n = divide by 2n: the
prescaled signal toggles every n rising input edges), then the Mod4 sequencer: event k (Mod4 = k-1) is qualified by
CAPkPOL (0 rising, 1 falling).  A qualified event stores TSCTR into CAPk when CAPLDEN, sets ECFLG.CEVTk, resets
TSCTR to 0 when CTRRSTk (delta mode: the next capture holds the cycle count between the two events), and advances
Mod4: continuous mode wraps to 0 after event STOP_WRAP+1; one-shot mode freezes the sequencer there (no loads, no
CEVT flags) until ECCTL2.REARM (which also clears Mod4; REARM works in both modes).

APWM mode (CAP_APWM = 1): CAP1 = active period (APRD), CAP2 = active compare (ACMP), CAP3 / CAP4 their shadows.
TSCTR counts 0..CAP1 (period CAP1 + 1 cycles; CTR=PRD resets it).  The output goes active when the counter starts a
period (CTR = 0) unless CAP2 = 0, and inactive at CTR = CAP2 (CAP2 > CAP1: 100 %); APWMPOL = 1 inverts it.  A CAP1 /
CAP2 write in APWM mode also writes CAP3 / CAP4 and takes effect at once; a CAP3 / CAP4 write is loaded into CAP1 /
CAP2 at the next CTR=PRD.  ECFLG.CTR_EQ_PRD / CTR_EQ_CMP are set at the matches.  A TSCTR above CAP1 counts on to
FFFFFFFF and wraps (CTROVF).  The ECAPx pin is an output in APWM mode and follows the signal (`drive_signal`), an
input otherwise: the model sets the direction of pins owned by ECAPx whenever the mux or the mode changes.

Interrupts: an event flag enabled in ECEINT while ECFLG.INT = 0 sets INT and raises ECAPx_INT (PIE 4.x, looked up by
name); the line is held until ECCLR.INT, which pends another interrupt if enabled flags are still set (SPRUFG4A 4.6).
ECCLR clears flags (reads 0), ECFRC sets them (reads 0).  Chip reset cancels every scheduled event.

Not modelled: emulation suspend (ECCTL1.FREE_SOFT is stored; the counter always runs free), GPIO input
qualification and synchronization delay (GPxCTRL / GPxQSEL: an edge is captured on the cycle it reaches the pin), the
CTR=PRD sync-out and comparators in capture mode, eCAP5/6 (not on the F28232).  The internal prescaler state is
cleared when ECCTL1.PRESCALE changes and on REARM.  TSCTR / CTRPHS loads (CPU write, sync) produce no CTR=0 / compare
event at the load instant.
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ._util import C2000Block, build_regs, irq_line, sysctrl

MASK32 = 0xFFFFFFFF
WRAP = 1 << 32
# ECCTL1
CAPLDEN = 1 << 8
# ECCTL2
CONT_ONESHT, REARM, TSCTRSTOP, SYNCI_EN, SWSYNC, CAP_APWM, APWMPOL = 1 << 0, 1 << 3, 1 << 4, 1 << 5, 1 << 8, \
    1 << 9, 1 << 10
# ECFLG / ECEINT / ECCLR / ECFRC
INT, CTROVF, CTR_PRD, CTR_CMP = 1 << 0, 1 << 5, 1 << 6, 1 << 7
EVENT_FLAGS = 0xFE


@register("ti_c2000.ecap")
class ECAP(C2000Block):
    eallow_regs = None                     # SPRUFG4A: no eCAP register is EALLOW-protected

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.index = int(params.get("index", name[-1] if name[-1].isdigit() else 1))
        self.signal = params.get("signal", f"ECAP{self.index}")
        zero = lambda b, r: 0                                         # noqa: E731
        ov = {
            "TSCTR": {"on_read": self._tsctr_read, "on_write": self._tsctr_write},
            "CAP1": {"on_write": self._cap_write}, "CAP2": {"on_write": self._cap_write},
            "CAP3": {"on_write": self._cap_write}, "CAP4": {"on_write": self._cap_write},
            "ECCTL1": {"on_write": self._ecctl1_write},
            "ECCTL2": {"on_write": self._ecctl2_write},
            "ECEINT": {"on_write": self._eceint_write},
            "ECFLG": {"ro_mask": 0xFFFF},
            "ECCLR": {"on_write": self._ecclr_write, "on_read": zero},
            "ECFRC": {"on_write": self._ecfrc_write, "on_read": zero},
        }
        regs_ = build_regs((name, "ECAP1"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, regs_, params=params, log=log)
        self.gate = params.get("gate", f"ECAP{self.index}ENCLK")
        self.sync_listeners: list = []      # cb(t_cycles) on SYNCO (the next eCAP's sync_in)
        self.running = False
        self._cyc0 = 0                      # anchor cycle ...
        self._c0 = 0                        # ... and TSCTR there
        self._ev = None
        self._apwm = False                  # mode the schedule / pins were set up for
        self._out: bool | None = None       # APWM output level driven on the signal (None = not driven)
        self._active = False                # APWM output before polarity
        self._shadow = [False, False]       # CAP3 / CAP4 written since the last CTR=PRD load
        self._mod4 = 0
        self._frozen = False                # one-shot stop reached
        self._ps_cnt = 0                    # prescaler: rising edges counted ...
        self._ps_out = False                # ... and the prescaled signal
        self._last: dict[str, str] = {}     # last input level per pin
        self._pins: list[Any] = []
        self._pin_ids: set[str] = set()
        self._int_line = None
        self._int_lvl = False
        self._tiny_warned = False
        self._fixing = False
        self.captures = 0

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._int_line = self.params.get("irq") or irq_line(chip, f"ECAP{self.index}_INT", f"4.{self.index}")
        sc = sysctrl(chip)
        if sc is not None and hasattr(sc, "pclk_listeners"):
            sc.pclk_listeners.append(self._clocks_changed)
        src = self.params.get("sync_from", "EPWM1" if self.index == 1 else f"ECAP{self.index - 1}")
        blk = chip.peripherals.get(src) if src else None
        lst = getattr(blk, "sync_listeners", None) if blk is not None else None
        if isinstance(lst, list):
            lst.append(self.sync_in)
        board = getattr(chip, "pins", None)
        if board is not None:
            self._pins = list(board.signal_pins(self.signal))
            self._pin_ids = {p.id for p in self._pins}
            for p in self._pins:
                board.input_listeners[p.id].append(self._pin_changed)
                self._last[p.id] = p.level()
            if self._pins:
                board.subscribe(self._pin_event)

    def reset(self) -> None:
        if self.chip is not None:
            if self._ev is not None:
                self.chip.sched.cancel(self._ev)
            if self._out is not None and self.chip.pins is not None:
                self.chip.pins.release_signal(self.signal, "L")
            if self._int_lvl and self._int_line is not None:
                self.irq(self._int_line, False)
        super().reset()
        self._ev = None
        self.running = False
        self._cyc0 = self._c0 = 0
        self._apwm = False
        self._out = None
        self._active = False
        self._shadow = [False, False]
        self._mod4 = 0
        self._frozen = False
        self._ps_cnt, self._ps_out = 0, False
        self._int_lvl = False
        for p in self._pins:
            self._last[p.id] = p.level()

    # ------------------------------------------------------------ time base
    def _clock_on(self) -> bool:
        sc = self._sysctrl
        return sc is None or sc.clock_enabled(self.gate)

    def _now(self) -> int:
        return self.chip.now_cycles()

    def _ctr_at(self, cyc: int) -> int:
        if not self.running:
            return self.get("TSCTR")
        return (self._c0 + cyc - self._cyc0) & MASK32

    def _anchor(self, cyc: int, ctr: int) -> None:
        self._cyc0, self._c0 = cyc, ctr & MASK32
        if not self.running:
            self.set("TSCTR", ctr)

    def _update_run(self) -> None:
        want = bool(self.get("ECCTL2") & TSCTRSTOP) and self._clock_on() and self.chip is not None
        if want == self.running:
            return
        now = self._now()
        if want:
            self.running = True
            self._anchor(now, self.get("TSCTR"))
            self._reschedule(now, inclusive=True)
        else:
            self.set("TSCTR", self._ctr_at(now))
            self.running = False
            self._cancel()

    def _clocks_changed(self) -> None:
        self._update_run()

    def write(self, offset: int, size: int, value: int) -> None:
        if offset < 2 and self.running:            # a 16-bit TSCTR write merges with the live count
            self.set("TSCTR", self._ctr_at(self._now()))
        C2000Block.write(self, offset, size, value)

    # ------------------------------------------------------------ scheduling
    def _cancel(self) -> None:
        if self._ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None

    def _reschedule(self, cyc: int, inclusive: bool) -> None:
        """Schedule the next event at or after (`inclusive`) / strictly after cycle `cyc`."""
        self._cancel()
        if not self.running:
            return
        c = self._ctr_at(cyc)
        best: int | None = None
        kinds: set[str] = set()

        def add(dt: int, kind: str) -> None:
            nonlocal best, kinds
            if dt < 0 or (dt == 0 and not inclusive):
                return
            if best is None or dt < best:
                best, kinds = dt, {kind}
            elif dt == best:
                kinds.add(kind)

        prd = self.get("CAP1")
        if self._apwm and c <= prd:
            if prd == 0 and not self._tiny_warned:
                self._tiny_warned = True
                self.emit("warn", block=self.name, msg=f"{self.name}: APWM period CAP1 = 0: an event every "
                          "cycle (slow to simulate)")
            add(-c, "ZRO")
            cmp = self.get("CAP2")
            if cmp <= prd:
                add(cmp - c, "CMP")
            add(prd - c, "PRD")
        else:
            add(WRAP - c, "OVF")                   # the cycle at which the counter reads 0 again
        if best is not None:
            t = cyc + best
            self._ev = self.chip.sched.at_cycles(t, self._event, t, frozenset(kinds))

    def _event(self, t: int, kinds: frozenset) -> None:
        self._ev = None
        if not self.running:
            return
        flg = 0
        if "OVF" in kinds or "RST" in kinds:          # the counter reads 0 at cycle t (wrap / CTR=PRD reset)
            if "OVF" in kinds:
                flg |= CTROVF
            self._anchor(t, 0)
            kinds = frozenset()
            if self._apwm:                             # CTR = 0 events of this same cycle
                kinds = frozenset({"ZRO"} | ({"CMP"} if self.get("CAP2") == 0 else set())
                                  | ({"PRD"} if self.get("CAP1") == 0 else set()))
        if "ZRO" in kinds:
            self._set_active(self.get("CAP2") != 0)
        if "CMP" in kinds:
            flg |= CTR_CMP
            self._set_active(False)
        if "PRD" in kinds:
            flg |= CTR_PRD
            if self._shadow[0]:
                self.set("CAP1", self.get("CAP3"))
            if self._shadow[1]:
                self.set("CAP2", self.get("CAP4"))
            self._shadow = [False, False]
        if flg:
            self._set_flags(flg)
        if "PRD" in kinds and (self.get("ECCTL2") >> 6) & 3 == 1:
            self._sync_out(t)
        if self.running and self._ev is None:
            if "PRD" in kinds:                         # TSCTR reads CAP1 now and restarts at 0 on the next cycle
                self._ev = self.chip.sched.at_cycles(t + 1, self._event, t + 1, frozenset({"RST"}))
            else:
                self._reschedule(t, inclusive=False)

    # ------------------------------------------------------------ APWM output / pins
    def _set_active(self, act: bool) -> None:
        self._active = act
        self._drive()

    def _drive(self) -> None:
        if not self._apwm or self.chip is None or self.chip.pins is None:
            return
        lvl = self._active ^ bool(self.get("ECCTL2") & APWMPOL)
        if lvl != self._out:
            self._out = lvl
            self.chip.pins.drive_signal(self.signal, "H" if lvl else "L")

    def _pin_event(self, ev: Any) -> None:
        """Mux changes: keep the direction of pins owned by ECAPx in step with the mode."""
        if ev.field != "owner" and ev.field != "direction":
            return
        if ev.pin in self._pin_ids and not self._fixing:
            self._fix_dir(self.chip.pins.get(ev.pin))

    def _fix_dir(self, pin: Any) -> None:
        board = self.chip.pins
        if not board.signal_matches(pin.owner, self.signal):
            return
        want = "out" if self._apwm else "in"
        if pin.direction == want:
            return
        self._fixing = True
        try:
            if want == "out":
                board.mcu_update(pin, direction="out", drive="H" if self._out else "L")
            else:
                board.mcu_update(pin, direction="in", drive=None)
        finally:
            self._fixing = False

    def _set_mode(self, apwm: bool) -> None:
        if apwm == self._apwm:
            return
        self._apwm = apwm
        pins = self.chip.pins if self.chip is not None else None
        if apwm:
            self._active = False
            self._out = None
            self._drive()
        elif self._out is not None and pins is not None:
            pins.release_signal(self.signal, "L")
            self._out = None
        if pins is not None:
            for p in self._pins:
                self._fix_dir(p)
        if self.running:
            self._reschedule(self._now(), inclusive=False)

    # ------------------------------------------------------------ capture
    def _pin_changed(self, pin: Any) -> None:
        lvl = pin.level()
        old = self._last.get(pin.id)
        self._last[pin.id] = lvl
        if old == lvl or lvl == "X" or old == "X" or old is None:
            return
        if self._apwm or not self.chip.pins.signal_matches(pin.owner, self.signal):
            return
        if not self._clock_on():
            return
        self._edge(lvl == "H")

    def _edge(self, rising: bool) -> None:
        c1 = self.get("ECCTL1")
        ps = (c1 >> 9) & 0x1F
        if ps:                                          # PSout toggles every `ps` rising input edges
            if not rising:
                return
            self._ps_cnt += 1
            if self._ps_cnt < ps:
                return
            self._ps_cnt = 0
            self._ps_out = not self._ps_out
            rising = self._ps_out
        if self._frozen:
            return
        k = self._mod4
        if bool((c1 >> (2 * k)) & 1) == rising:        # CAPkPOL: 0 rising, 1 falling
            return
        now = self._now()
        ctr = self._ctr_at(now)
        if c1 & CAPLDEN:
            self.set(f"CAP{k + 1}", ctr)
        self.captures += 1
        if self.log_all:
            self.emit("info", block=self.name, msg=f"{self.name}: CEVT{k + 1} {'rise' if rising else 'fall'} "
                      f"CAP{k + 1}={ctr:#x}")
        if (c1 >> (2 * k + 1)) & 1:                     # CTRRSTk: reset after the time stamp
            self._anchor(now, 0)
            if self.running:
                self._reschedule(now, inclusive=False)
        c2 = self.get("ECCTL2")
        stop = (c2 >> 1) & 3
        if k == stop:
            if c2 & CONT_ONESHT:
                self._frozen = True
            else:
                self._mod4 = 0
        else:
            self._mod4 = (k + 1) & 3
        self._set_flags(1 << (k + 1))

    # ------------------------------------------------------------ interrupts
    def _set_flags(self, bits: int) -> None:
        self.set("ECFLG", self.get("ECFLG") | bits)
        self._eval_int()

    def _eval_int(self) -> None:
        flg = self.get("ECFLG")
        if not flg & INT and flg & self.get("ECEINT") & EVENT_FLAGS:
            self.set("ECFLG", flg | INT)
            flg |= INT
        lvl = bool(flg & INT)
        if lvl != self._int_lvl:
            self._int_lvl = lvl
            if self._int_line is not None:
                self.irq(self._int_line, lvl)

    # ------------------------------------------------------------ sync
    def sync_in(self, t_cycles: int | None = None) -> None:
        """SYNCI: load TSCTR from CTRPHS when SYNCI_EN; pass the pulse on when SYNCO_SEL = 00."""
        if self.chip is None or not self._clock_on():
            return
        t = self._now() if t_cycles is None else int(t_cycles)
        c2 = self.get("ECCTL2")
        if c2 & SYNCI_EN:
            self._anchor(t, self.get("CTRPHS"))
            if self.running:
                self._reschedule(t, inclusive=False)
        if (c2 >> 6) & 3 == 0:
            self._sync_out(t)

    def _sync_out(self, t: int) -> None:
        for cb in list(self.sync_listeners):
            cb(t)

    # ------------------------------------------------------------ register hooks
    def _tsctr_read(self, blk, reg):
        return self._ctr_at(self._now()) if self.running else None

    def _tsctr_write(self, blk, reg, old, new):
        if self.running:
            now = self._now()
            self._anchor(now, new)
            self._reschedule(now, inclusive=False)
        return None

    def _cap_write(self, blk, reg, old, new):
        if not self._apwm:
            return None
        i = int(reg.name[3])
        if i <= 2:                                       # active register: also the shadow (immediate mode)
            self.set(f"CAP{i + 2}", new)
            self._shadow[i - 1] = False
            if self.running:
                self._reschedule(self._now(), inclusive=False)
        else:
            self._shadow[i - 3] = True
        return None

    def _ecctl1_write(self, blk, reg, old, new):
        if (old ^ new) & (0x1F << 9):
            self._ps_cnt, self._ps_out = 0, False
        return None

    def _ecctl2_write(self, blk, reg, old, new):
        self._store(reg, new & ~(REARM | SWSYNC))
        if new & REARM:
            self._mod4 = 0
            self._frozen = False
            self._ps_cnt, self._ps_out = 0, False
        if (old ^ new) & CAP_APWM:
            self._set_mode(bool(new & CAP_APWM))
        elif (old ^ new) & APWMPOL:
            self._drive()
        self._update_run()
        if new & SWSYNC:
            self.sync_in()
        return new & ~(REARM | SWSYNC)

    def _eceint_write(self, blk, reg, old, new):
        self._store(reg, new)
        self._eval_int()
        return None

    def _ecclr_write(self, blk, reg, old, new):
        flg = self.get("ECFLG") & ~(new & 0xFF)
        self.set("ECFLG", flg)
        if not flg & INT and self._int_lvl:    # end of this interrupt: the line drops ...
            self._int_lvl = False
            if self._int_line is not None:
                self.irq(self._int_line, False)
        self._eval_int()                       # ... and enabled flags still set raise another one
        return 0

    def _ecfrc_write(self, blk, reg, old, new):
        if new & EVENT_FLAGS:
            self._set_flags(new & EVENT_FLAGS)
        return 0
